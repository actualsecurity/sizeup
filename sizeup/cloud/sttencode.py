"""The STT input encoder's worker (#677, #709): the FLAC of each clip's exact whisper input (sizeup/cloud/sttinput.py
_blob), made outside sizeup-station's cgroup. A thread or a child of sizeup-station competes at the station's
CPUWeight=500, above whisper's 100, whatever its niceness: the sizeup-stt-encode service (its own unit, Nice=19,
CPUWeight=10) takes only what's left.

    python -m sizeup.cloud.sttencode --serve SOCKET     the service: a Unix socket, 0600 in a 0700 directory, each
                                                        connection's peer checked (SO_PEERCRED: our own uid only)
    python -m sizeup.cloud.sttencode --stdio            a child process of the station's own (sttinput "process" mode)

The frames, both ways (never pickle: the service decodes nothing it can't bound first):

    >II  header length, payload length     (header at most MAX_HEADER, payload at most MAX_FRAME: refused before
                                            either is read, nothing allocated for them)
    header: JSON object; payload: raw bytes

    station -> worker   {"proto": PROTO, "op": "hello", "code": code_version()}
                        {"proto": PROTO, "op": "encode", "desc": {...}, "fm": bool, "offset": s, "rate": 16000,
                         "samples": n, "dtype": "<f8" | "<f4"} + the samples (n x itemsize bytes, exactly)
    worker -> station   {"ok": true, "code": ...} (hello) | {"refused": "version", "code": ...}
                        {"ok": true, "meta": {...}} + the FLAC | {"none": true} | {"error": "..."}

The hello's code is a hash of the code that makes the bytes (sttinput.py, flac.py, stt.py): a worker of another
release refuses, and the station makes them on its own thread until the service restarts with it (the unit is
PartOf=sizeup-station.service, #709).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

PROTO = 2
SOCKET_ENV = "SIZEUP_STT_ENCODER_SOCKET"
SOCKET = "/run/sizeup-stt-encode/encode.sock"
WORKER_ARGS = ("-m", "sizeup.cloud.sttencode", "--stdio")
MAX_HEADER = 64 * 1024
MAX_FRAME = 40 * 1024 * 1024            # 300 s at 16 kHz in float64 is 38.4 MB
DTYPES = {"<f8": 8, "<f4": 4}
CONNECT_TIMEOUT_S = 5.0
BASE_TIMEOUT_S = 30.0                   # a clip's answer: this, plus ...
PER_AUDIO_S = 4.0                       # ... this per second of its audio (the worker runs on what is left)
SERVER_IDLE_S = 3600.0                  # the service: a connection silent this long is closed


class WorkerError(RuntimeError):
    """The worker couldn't make this one."""


class Dead(WorkerError):
    """Not there, refused, reset, or gone mid-answer: the station's thread makes this clip."""


class Slow(WorkerError):
    """No answer within the clip's time: kept queued (never made on the station's thread: that is what the worker
    is there to avoid)."""


class Mismatch(WorkerError):
    """A worker of another release: the thread until the service restarts."""


class FrameError(ValueError):
    pass


_CODE: str | None = None


def code_version() -> str:
    """A hash of what makes the bytes: this module's, sttinput's, flac's and stt's source (radio_voice)."""
    global _CODE
    if _CODE is None:
        from sizeup import stt
        from sizeup.cloud import flac, sttinput
        h = hashlib.sha256()
        for m in (sys.modules[__name__], sttinput, flac, stt):
            h.update(Path(m.__file__).read_bytes())
        _CODE = h.hexdigest()[:16]
    return _CODE


# -- frames -----------------------------------------------------------------------------------------------------------

def write_frame(f, head: dict, payload: bytes = b"") -> None:
    h = json.dumps(head, separators=(",", ":")).encode()
    f.write(struct.pack(">II", len(h), len(payload)) + h)
    if payload:
        f.write(payload)
    f.flush()


def _exact(f, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = f.read(n - len(out))
        if not b:
            raise EOFError("closed mid-frame" if out else "closed")
        out += b
    return bytes(out)


def read_frame(f, max_payload: int = MAX_FRAME, check=None) -> tuple[dict, bytes]:
    """(header, payload). check(header, payload length) may raise FrameError: before the payload is read."""
    hl, pl = struct.unpack(">II", _exact(f, 8))
    if hl > MAX_HEADER or pl > max_payload:
        raise FrameError(f"frame too big ({hl} + {pl} bytes)")
    try:
        head = json.loads(_exact(f, hl))
    except ValueError:
        raise FrameError("header: not JSON") from None
    if not isinstance(head, dict):
        raise FrameError("header: not an object")
    if check is not None:
        check(head, pl)
    return head, _exact(f, pl) if pl else b""


def peer_uid(sock: socket.socket) -> int | None:
    """The uid at the other end of a Unix socket (Linux SO_PEERCRED); None where it can't be asked."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    try:
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i")))
        return uid
    except OSError:
        return None


# -- the station's side -----------------------------------------------------------------------------------------------

class Client:
    """One connection to a worker: the service ("sock", path) or a child process ("argv", [...])."""

    def __init__(self, how: str, where):
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.fin = self.fout = None
        try:
            if how == "sock":
                st = os.stat(where)
                if st.st_uid != os.getuid():
                    raise Dead(f"{where} isn't ours (uid {st.st_uid})")
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(CONNECT_TIMEOUT_S)
                self.sock.connect(where)
                uid = peer_uid(self.sock)
                if uid is not None and uid != os.getuid():
                    raise Dead(f"the worker runs as uid {uid}, not ours")
                self.fin, self.fout = self.sock.makefile("rb"), self.sock.makefile("wb")
            else:
                self.proc = subprocess.Popen(where, stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True)
                self.fin, self.fout = self.proc.stdout, self.proc.stdin
            write_frame(self.fout, {"proto": PROTO, "op": "hello", "code": code_version()})
            head, _ = read_frame(self.fin, 0)
        except WorkerError:
            self.close()
            raise
        except (OSError, EOFError, FrameError, struct.error) as e:
            self.close()
            raise Dead(f"not reached: {type(e).__name__}: {e}") from None
        if head.get("refused") == "version":
            self.close()
            raise Mismatch(f"another release's worker ({str(head.get('code'))[:16]}, ours {code_version()})")
        if head.get("ok") is not True:
            self.close()
            raise Dead("a bad hello")

    def encode(self, desc: dict, fm: bool, offset: float, audio) -> tuple[bytes, dict] | None:
        from sizeup.cloud.sttinput import MAX_BLOB, RATE
        a = np.asarray(audio)
        if a.dtype.str not in DTYPES:
            a = a.astype("<f8")                         # (int or big-endian samples: exactly as float64)
        a = np.ascontiguousarray(a)
        if self.sock is not None:
            self.sock.settimeout(BASE_TIMEOUT_S + PER_AUDIO_S * len(a) / RATE)
        try:
            write_frame(self.fout, {"proto": PROTO, "op": "encode", "desc": desc, "fm": bool(fm),
                                    "offset": float(offset), "rate": RATE, "samples": int(len(a)),
                                    "dtype": a.dtype.str}, a.tobytes())
            head, payload = read_frame(self.fin, MAX_BLOB)
        except (socket.timeout, TimeoutError):
            raise Slow("no answer in time") from None
        except (OSError, EOFError, FrameError, struct.error) as e:
            raise Dead(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from None
        if head.get("ok") is True and isinstance(head.get("meta"), dict) and payload:
            return payload, head["meta"]
        if head.get("none") is True:
            return None
        if isinstance(head.get("error"), str):
            raise RuntimeError(head["error"][:300])     # (this clip: counted "failed", the worker goes on)
        raise Dead("a bad answer")

    def shutdown(self) -> None:
        """From another thread: whatever waits on this worker returns now (stop())."""
        try:
            if self.sock is not None:
                self.sock.shutdown(socket.SHUT_RDWR)
            elif self.proc is not None:
                self.proc.kill()                        # (our own child)
        except OSError:
            pass

    def close(self) -> None:
        for f in (self.fout, self.fin, self.sock):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()                        # (our own child only)
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass


# -- the worker's side ------------------------------------------------------------------------------------------------

def _check_encode(head: dict, pl: int) -> None:
    from sizeup.cloud.sttinput import MAX_SECONDS, RATE
    n, dt = head.get("samples"), head.get("dtype")
    if head.get("proto") != PROTO or head.get("op") != "encode":
        raise FrameError("not an encode request")
    if dt not in DTYPES or not isinstance(n, int) or isinstance(n, bool) or not 0 < n <= MAX_SECONDS * RATE:
        raise FrameError("dtype / samples")
    if head.get("rate") != RATE or pl != n * DTYPES[dt]:
        raise FrameError("rate / length")
    d = head.get("desc")
    if not isinstance(d, dict) or d.get("kind") not in ("traffic", "page") or len(d) > 16 or \
            not all(isinstance(k, str) and (v is None or isinstance(v, (str, int, float))) for k, v in d.items()):
        raise FrameError("desc")
    if not isinstance(head.get("fm"), bool) or not isinstance(head.get("offset"), (int, float)):
        raise FrameError("fm / offset")


def serve_one(fin, fout) -> None:
    """One connection: its hello (the same code, else refused), then its requests until it closes or sends one that
    isn't (answered, then closed: a frame that can't be read leaves the stream where nothing can be trusted)."""
    from sizeup.cloud.sttinput import _blob
    try:
        hello, _ = read_frame(fin, 0)
        if hello.get("proto") != PROTO or hello.get("op") != "hello" or hello.get("code") != code_version():
            write_frame(fout, {"refused": "version", "code": code_version()})
            return
        write_frame(fout, {"ok": True, "code": code_version()})
        while True:
            try:
                head, payload = read_frame(fin, MAX_FRAME, _check_encode)
            except FrameError as e:
                write_frame(fout, {"error": f"refused: {e}"})
                return
            try:
                audio = np.frombuffer(payload, head["dtype"])
                got = _blob(head["desc"], head["fm"], float(head["offset"]), audio)
            except Exception as e:  # noqa: BLE001 - this clip only
                write_frame(fout, {"error": f"{type(e).__name__}: {e}"[:300]})
                continue
            if got is None:
                write_frame(fout, {"none": True})
            else:
                write_frame(fout, {"ok": True, "meta": got[1]}, got[0])
    except (EOFError, OSError, FrameError, struct.error):
        return


def _serve_conn(conn: socket.socket) -> None:
    try:
        uid = peer_uid(conn)
        if uid is not None and uid != os.getuid():
            return                                      # (the socket is 0600 anyway: never another user's)
        conn.settimeout(SERVER_IDLE_S)
        with conn.makefile("rb") as fin, conn.makefile("wb") as fout:
            serve_one(fin, fout)
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    try:
        os.nice(19)                                     # (the unit's CPUWeight does the rest)
    except OSError:
        pass
    if argv[:1] == ["--serve"] and len(argv) == 2:
        path = argv[1]
        d = os.path.dirname(path) or "."
        try:
            if os.stat(d).st_uid == os.getuid():
                os.chmod(d, 0o700)                      # (its RuntimeDirectory: ours alone)
        except OSError:
            pass
        try:
            os.unlink(path)                             # (a stale one from the last run)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)                           # the socket: 0600
        try:
            srv.bind(path)
        finally:
            os.umask(old)
        srv.listen(4)
        print(f"sizeup-stt-encode: serving on {path} (code {code_version()})", file=sys.stderr, flush=True)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=_serve_conn, args=(conn,), name="stt-encode", daemon=True).start()
    if argv == ["--stdio"]:
        fin, fout = sys.stdin.buffer, sys.stdout.buffer
        sys.stdout = sys.stderr                         # (nothing else ever writes to the answers' pipe)
        serve_one(fin, fout)
        return 0
    print("usage: python -m sizeup.cloud.sttencode --serve SOCKET | --stdio", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
