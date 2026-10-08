"""Amazon Transcribe streaming for the important channels (user, 2026-10-08: "lets try it [AWS
Transcribe] ... test aws transcribe on the important channels like South 2 paging, Fire/EMS 15 and our
fireground for now"). Stdlib only (+numpy): a minimal RFC 6455 WebSocket client, AWS event-stream
framing and SigV4 query signing (sizeup/aws.py).

Billing ($0.024 a minute, at least 15 s per stream) is why it works the way it does: one long-lived
stream per channel, transmissions fed back to back, never one stream per transmission (a 3.7 s
transmission alone would be billed 15 s).

    AwsTranscriber.submit(channel, item, pcm)    pcm: int16, 8 kHz mono (the clips the hub keeps)
        one ChannelWorker thread per channel (South 2, FG1, tg:307 ...), one stream each:
        open on the first transmission (wss://transcribestreaming.<region>.amazonaws.com:8443/
        stream-transcription-websocket?language-code=en-US&media-encoding=pcm&sample-rate=8000
        [&vocabulary-name=sizeup-pcfd], presigned), the audio sent in CHUNK_S pieces at `pace` x real
        time, GAP_S of silence after each transmission so the results segment cleanly. Each
        transmission's place in the stream ([a, b) seconds of audio sent) is kept; the words of final
        results (their StartTime) are given to the transmission whose audio they fall in, so one result
        that runs over two transmissions is split between them.
        A transmission is done when no open (partial) result starts inside it and either a final result
        ended past it or `quiet_s` has gone by since its last audio was sent. A result still open after
        `settle_s` more gets up to `flush_s` of silence (billed) to finish, then the stream is ended
        (which makes Transcribe finalize everything).
        Idle: every stream is billed at least 15 s, so a burst of traffic should share one; the stream
        is ended `idle_s` (default 120) after the last transmission and opened again on demand.
        Transcribe ends a stream that gets no audio for 15 s, so while it waits it gets KEEPALIVE_AUDIO_S
        (0.1 s) of silence every KEEPALIVE_S (10 s): billed as audio, about 1 % of the idle time
        (AWS bills the audio streamed, not the time connected; set idle_s to ~10 if that ever changes).
        4 h: a stream near Transcribe's 4-hour limit is ended between transmissions and a new one opened.
        Errors (BadRequest, LimitExceeded, ServiceUnavailable, InternalFailure, a dropped connection):
        the transmissions not done yet are tried once more in a new stream after a backoff; then
        on_fail(item, why). A 403 (no permission) fails at once. Nothing is ever dropped silently.
    on_done(item, text, confidence)    text "" = nothing said (no_speech)
    on_fail(item, why)                 the caller falls back (the station's whisper) or says it failed

Budget (Usage): billed seconds per calendar month (max(15 s, audio sent) per stream) in the CAD
database; when the month's cost reaches the limit no new audio is sent: available() says why and the
callers use the local model until the next month.

Custom vocabulary (Vocabulary): a phrase list pushed as `sizeup-pcfd` (CreateVocabulary /
UpdateVocabulary, polled with GetVocabulary until READY) and used by new streams once READY. AWS rules
(list form): letters, apostrophes and periods only, the words of a phrase joined with hyphens
("Port-Chester"), at most 256 characters; no digits, so numbers are spelled out the way they are said
on the air ("Engine 60" -> "Engine-sixty", "23 98" -> "twenty-three-ninety-eight"). Transcribe then
writes those words, so respell() turns "Engine sixty" back into "Engine 60" in the text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from sizeup import aws

RATE = 8000
MODEL = "aws-transcribe"
SERVICE = "transcribe"
PRICE_PER_MIN = 0.024
MIN_BILLED_S = 15.0
MAX_STREAM_S = 4 * 3600 - 120     # end a stream between transmissions before Transcribe's 4 h limit
KEEPALIVE_S = 10.0               # Transcribe ends a stream that gets no audio for 15 s: this often ...
KEEPALIVE_AUDIO_S = 0.1          # ... this much silence while it waits for the next transmission
VOCABULARY = "sizeup-pcfd"
RETRIES = 1                      # a transmission caught in a failed stream is tried this many more times


# -- AWS event stream (application/vnd.amazon.eventstream) ---------------------------------------------------

class EventStreamError(ValueError):
    pass


def _headers_bytes(headers: dict) -> bytes:
    out = bytearray()
    for k, v in headers.items():
        kb = k.encode()
        if isinstance(v, (bytes, bytearray)):
            out += bytes([len(kb)]) + kb + b"\x06" + struct.pack(">H", len(v)) + bytes(v)
        else:
            vb = str(v).encode()
            out += bytes([len(kb)]) + kb + b"\x07" + struct.pack(">H", len(vb)) + vb
    return bytes(out)


def encode_message(headers: dict, payload: bytes) -> bytes:
    """prelude (total length, headers length, CRC32 of those 8 bytes), headers, payload, CRC32 of it all."""
    h = _headers_bytes(headers)
    prelude = struct.pack(">II", 16 + len(h) + len(payload), len(h))
    msg = prelude + struct.pack(">I", zlib.crc32(prelude)) + h + payload
    return msg + struct.pack(">I", zlib.crc32(msg))


def _decode_headers(b: bytes) -> dict:
    out, i = {}, 0
    while i < len(b):
        n = b[i]
        name = b[i + 1:i + 1 + n].decode()
        i += 1 + n
        t = b[i]
        i += 1
        if t in (0, 1):
            v: object = t == 0
        elif t == 2:
            v, i = struct.unpack(">b", b[i:i + 1])[0], i + 1
        elif t == 3:
            v, i = struct.unpack(">h", b[i:i + 2])[0], i + 2
        elif t == 4:
            v, i = struct.unpack(">i", b[i:i + 4])[0], i + 4
        elif t in (5, 8):
            v, i = struct.unpack(">q", b[i:i + 8])[0], i + 8
        elif t in (6, 7):
            ln = struct.unpack(">H", b[i:i + 2])[0]
            raw = b[i + 2:i + 2 + ln]
            v, i = (raw.decode() if t == 7 else raw), i + 2 + ln
        elif t == 9:
            v, i = b[i:i + 16].hex(), i + 16
        else:
            raise EventStreamError(f"header {name!r}: unknown type {t}")
        out[name] = v
    return out


def decode_message(buf: bytes) -> tuple[dict, bytes, int]:
    """(headers, payload, bytes used) of the message at the start of buf. EventStreamError on a bad CRC
    or a short buffer."""
    if len(buf) < 16:
        raise EventStreamError("short message")
    total, hlen = struct.unpack(">II", buf[:8])
    if struct.unpack(">I", buf[8:12])[0] != zlib.crc32(buf[:8]):
        raise EventStreamError("prelude CRC mismatch")
    if total < 16 + hlen or len(buf) < total:
        raise EventStreamError("short message")
    if struct.unpack(">I", buf[total - 4:total])[0] != zlib.crc32(buf[:total - 4]):
        raise EventStreamError("message CRC mismatch")
    return _decode_headers(buf[12:12 + hlen]), bytes(buf[12 + hlen:total - 4]), total


def audio_event(pcm: bytes) -> bytes:
    """One AudioEvent; an empty one ends the stream (Transcribe then sends its last results)."""
    return encode_message({":content-type": "application/octet-stream", ":event-type": "AudioEvent",
                           ":message-type": "event"}, pcm)


# -- WebSocket (RFC 6455, client side) --------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsError(OSError):
    pass


class WsHandshakeError(WsError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    a = np.frombuffer(data, np.uint8)
    k = np.frombuffer((key * (len(data) // 4 + 1))[:len(data)], np.uint8)
    return (a ^ k).tobytes()


class WebSocket:
    """A client connection: binary frames out (masked), frames in; ping answered with pong. Thread
    safe for one reader and any number of writers."""

    def __init__(self, sock: socket.socket, leftover: bytes = b""):
        self.sock, self._buf = sock, bytearray(leftover)
        self._wlock = threading.Lock()
        self.closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 10.0, headers: dict | None = None) -> WebSocket:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise WsError(f"not a WebSocket URL: {u.scheme}://")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "wss" else raw
            key = base64.b64encode(os.urandom(16)).decode()
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            lines = [f"GET {path} HTTP/1.1", f"Host: {u.netloc}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = bytearray()
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WsError("connection closed during the WebSocket handshake")
                data += chunk
                if len(data) > 65536:
                    raise WsError("WebSocket handshake answer too long")
            head, _, rest = bytes(data).partition(b"\r\n\r\n")
            status_line, *hdr_lines = head.decode("latin-1").split("\r\n")
            parts = status_line.split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            hdrs = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in hdr_lines)}
            if status != 101:
                body = bytearray(rest)
                want = int(hdrs.get("content-length") or 0)
                try:
                    while len(body) < min(want, 65536):
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        body += chunk
                except OSError:
                    pass
                raise WsHandshakeError(status, body.decode(errors="replace"))
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            if hdrs.get("sec-websocket-accept") != accept:
                raise WsError("bad Sec-WebSocket-Accept in the handshake")
            return cls(sock, rest)
        except BaseException:
            raw.close()
            raise

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        key = os.urandom(4)
        with self._wlock:
            if self.closed:
                raise WsError("WebSocket closed")
            self.sock.sendall(head + key + _mask(payload, key))

    def send_binary(self, data: bytes) -> None:
        self.send_frame(OP_BINARY, data)

    def _frame(self) -> tuple[bool, int, bytes] | None:
        b = self._buf
        if len(b) < 2:
            return None
        fin, op, masked, ln, i = bool(b[0] & 0x80), b[0] & 0x0F, bool(b[1] & 0x80), b[1] & 0x7F, 2
        if ln == 126:
            if len(b) < 4:
                return None
            ln, i = struct.unpack(">H", bytes(b[2:4]))[0], 4
        elif ln == 127:
            if len(b) < 10:
                return None
            ln, i = struct.unpack(">Q", bytes(b[2:10]))[0], 10
        key = b""
        if masked:
            if len(b) < i + 4:
                return None
            key, i = bytes(b[i:i + 4]), i + 4
        if len(b) < i + ln:
            return None
        payload = bytes(b[i:i + ln])
        del b[:i + ln]
        return fin, op, (_mask(payload, key) if masked else payload)

    def recv(self) -> tuple[int, bytes]:
        """The next data message (OP_BINARY / OP_TEXT, fragments joined) or (OP_CLOSE, code + reason).
        socket.timeout (TimeoutError) when nothing came in time: nothing read so far is lost."""
        parts: list[bytes] = []
        first_op = None
        while True:
            fr = self._frame()
            if fr is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WsError("connection closed")
                self._buf += chunk
                continue
            fin, op, payload = fr
            if op == OP_PING:
                try:
                    self.send_frame(OP_PONG, payload)
                except OSError:
                    pass
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                try:
                    self.send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                return OP_CLOSE, payload
            if op != OP_CONT:
                first_op = op
            parts.append(payload)
            if fin:
                return (first_op if first_op is not None else OP_BINARY), b"".join(parts)

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        try:
            self.send_frame(OP_CLOSE, struct.pack(">H", code))
        except OSError:
            pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


# -- one stream -------------------------------------------------------------------------------------------

@dataclass
class Word:
    start: float
    end: float
    content: str
    kind: str = "pronunciation"          # | "punctuation"
    confidence: float | None = None


def words_text(words: list[Word]) -> str:
    out = ""
    for w in words:
        if w.kind == "punctuation":
            out += w.content
        else:
            out += (" " if out else "") + w.content
    return out.strip()


def _words(result: dict) -> list[Word]:
    alt = (result.get("Alternatives") or [{}])[0] or {}
    items = alt.get("Items") or []
    if not items:
        text = str(alt.get("Transcript") or "").strip()
        return [Word(float(result.get("StartTime") or 0), float(result.get("EndTime") or 0), text)] if text else []
    out = []
    for it in items:
        c = it.get("Confidence")
        out.append(Word(float(it.get("StartTime") or 0), float(it.get("EndTime") or 0), str(it.get("Content") or ""),
                        str(it.get("Type") or "pronunciation"), float(c) if isinstance(c, (int, float)) else None))
    return out


class Session:
    """One Transcribe stream: send audio, collect results (a reader thread)."""

    def __init__(self, ws: WebSocket, log: Callable[[str], None] = print, rate: int = RATE):
        self.ws, self.log, self.rate = ws, log, rate
        self.cond = threading.Condition()
        self.words: list[Word] = []                    # every final word so far
        self.partial: tuple[str, float, float, list[Word]] | None = None   # an open result: (id, start, end, words)
        self.final_end = 0.0
        self.error: tuple[str, str] | None = None      # (AWS exception type, message)
        self.closed = False
        self.sent_s = 0.0
        self.opened = self.last_sent = time.time()
        self.ended = False
        threading.Thread(target=self._read, name="aws-stt-read", daemon=True).start()

    def send(self, pcm: bytes) -> None:
        self.ws.send_binary(audio_event(pcm))
        self.sent_s += len(pcm) / 2 / self.rate
        self.last_sent = time.time()

    def end(self) -> None:
        if not self.ended:
            self.ended = True
            self.ws.send_binary(audio_event(b""))

    def _read(self) -> None:
        try:
            self.ws.sock.settimeout(None)
            while True:
                op, data = self.ws.recv()
                if op == OP_CLOSE:
                    code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                    if code not in (1000, 1005) and not self.ended:
                        self._fail("Closed", f"closed by Transcribe ({code} {data[2:].decode(errors='replace')})".strip())
                    break
                if op != OP_BINARY:
                    continue
                headers, payload, _ = decode_message(data)
                kind = headers.get(":message-type")
                if kind == "event" and headers.get(":event-type") == "TranscriptEvent":
                    self._results(json.loads(payload or b"{}"))
                elif kind in ("exception", "error"):
                    try:
                        msg = str(json.loads(payload or b"{}").get("Message") or "")
                    except ValueError:
                        msg = payload.decode(errors="replace")[:300]
                    self._fail(str(headers.get(":exception-type") or headers.get(":error-code") or "Exception"), msg)
                    break
        except (OSError, EventStreamError, ValueError) as e:
            if not self.closed and not (self.ended and isinstance(e, WsError)):
                self._fail("Connection", f"{type(e).__name__}: {e}")
        finally:
            with self.cond:
                self.closed = True
                self.cond.notify_all()
            self.ws.close()

    def _fail(self, kind: str, msg: str) -> None:
        with self.cond:
            if self.error is None:
                self.error = (kind, msg)
            self.cond.notify_all()

    def _results(self, d: dict) -> None:
        with self.cond:
            for r in (d.get("Transcript") or {}).get("Results") or []:
                rid = str(r.get("ResultId") or "")
                if r.get("IsPartial"):
                    self.partial = (rid, float(r.get("StartTime") or 0), float(r.get("EndTime") or 0), _words(r))
                    continue
                self.words.extend(_words(r))
                self.final_end = max(self.final_end, float(r.get("EndTime") or 0))
                if self.partial is not None and (self.partial[0] == rid or self.partial[1] < float(r.get("EndTime") or 0)):
                    self.partial = None
            self.cond.notify_all()

    def close(self) -> None:
        self.ws.close()


# -- usage and the monthly budget -------------------------------------------------------------------------

class Usage:
    """Billed seconds per calendar month (local time), kept through get/put (the CAD's meta table)."""

    def __init__(self, get: Callable[[str], str | None], put: Callable[[str, str], None],
                 now: Callable[[], float] = time.time):
        self._get, self._put, self.now = get, put, now
        self._lock = threading.Lock()

    def month_key(self) -> str:
        return time.strftime("%Y-%m", time.localtime(self.now()))

    def month(self) -> dict:
        key = self.month_key()
        try:
            d = json.loads(self._get(f"aws_usage:{key}") or "{}")
        except ValueError:
            d = {}
        return {"month": key, "billed_s": float(d.get("billed_s") or 0), "audio_s": float(d.get("audio_s") or 0),
                "streams": int(d.get("streams") or 0)}

    def add(self, billed_s: float, audio_s: float) -> None:
        with self._lock:
            m = self.month()
            m["billed_s"] += billed_s
            m["audio_s"] += audio_s
            m["streams"] += 1
            self._put(f"aws_usage:{m['month']}", json.dumps({k: m[k] for k in ("billed_s", "audio_s", "streams")}))


@dataclass
class Settings:
    """[stt] aws_*: where and how (sizeup/config.py)."""
    region: str = ""
    endpoint: str = ""             # the streaming endpoint (tests: ws://127.0.0.1:port); "" = AWS's
    api_endpoint: str = ""         # the JSON API (vocabularies); "" = AWS's
    language: str = "en-US"
    idle_s: float = 120.0          # end a stream this long after its last transmission
    pace: float = 2.0              # audio sent this many times faster than real time
    gap_s: float = 0.3             # silence after each transmission
    quiet_s: float = 1.5           # a transmission is done this long after its audio went, if nothing is open in it
    settle_s: float = 2.0          # ... a result still open in it after this gets silence to finish
    flush_s: float = 1.5           # ... at most this much; then the stream is ended
    chunk_s: float = 0.1
    price_per_min: float = PRICE_PER_MIN
    vocabulary: str = VOCABULARY


@dataclass
class Job:
    channel: str
    item: object
    pcm: np.ndarray
    attempts: int = 0
    a: float = 0.0                 # where its audio starts / ends in the stream (s)
    b: float = 0.0
    sent_at: float = 0.0
    flushed: float = 0.0
    done: bool = False

    @property
    def seconds(self) -> float:
        return len(self.pcm) / RATE


# -- the service ------------------------------------------------------------------------------------------

class AwsTranscriber:
    def __init__(self, s: Settings, usage: Usage, on_done: Callable[[object, str, float | None], None],
                 on_fail: Callable[[object, str], None], log: Callable[[str], None] = print,
                 limit_usd: Callable[[], float] = lambda: 25.0, connect: Callable[..., WebSocket] = WebSocket.connect,
                 creds: Callable[[], aws.Credentials] = aws.credentials, respell: Callable[[str], str] = lambda t: t):
        self.s, self.usage, self.on_done, self.on_fail, self.log = s, usage, on_done, on_fail, log
        self.limit_usd, self.connect, self.creds, self.respell = limit_usd, connect, creds, respell
        self.region = aws.region(s.region)
        self.vocabulary = ""                       # set once the custom vocabulary is READY
        self.pace = max(0.5, float(s.pace))
        self._workers: dict[str, ChannelWorker] = {}
        self._lock = threading.Lock()
        self.stopped = threading.Event()
        self.last_error, self.last_error_at = "", None
        self.done_n = self.failed_n = 0

    # -- what the callers ask --------------------------------------------------------------------------

    def url(self) -> str:
        base = (self.s.endpoint or f"wss://transcribestreaming.{self.region}.amazonaws.com:8443").rstrip("/")
        q = {"language-code": self.s.language, "media-encoding": "pcm", "sample-rate": str(RATE)}
        if self.vocabulary:
            q["vocabulary-name"] = self.vocabulary
        return aws.presign(f"{base}/stream-transcription-websocket?{urllib.parse.urlencode(q)}", self.creds(),
                           self.region, SERVICE, expires=300)

    def spent(self) -> dict:
        """This month: billed seconds (closed streams + the open ones so far), minutes, dollars, the limit."""
        m = self.usage.month()
        live = sum(max(MIN_BILLED_S, w.session.sent_s) for w in list(self._workers.values()) if w.session is not None)
        billed = m["billed_s"] + live
        return {**m, "billed_s": round(billed, 1), "minutes": round(billed / 60, 1),
                "usd": round(billed / 60 * self.s.price_per_min, 2), "limit_usd": float(self.limit_usd()),
                "streams_open": sum(1 for w in list(self._workers.values()) if w.session is not None)}

    def budget_ok(self, extra_s: float = 0.0) -> tuple[bool, str]:
        sp = self.spent()
        limit = sp["limit_usd"]
        if (sp["billed_s"] + extra_s) / 60 * self.s.price_per_min >= limit:
            return False, f"the monthly AWS limit is reached (${sp['usd']:.2f} of ${limit:g} in {sp['month']})"
        return True, ""

    def available(self) -> tuple[bool, str]:
        """(True, "") when a transmission sent now would go to AWS; else why not."""
        try:
            self.creds()
        except aws.AwsError as e:
            return False, str(e)
        return self.budget_ok(MIN_BILLED_S)

    def submit(self, channel: str, item, pcm: np.ndarray) -> None:
        pcm = np.asarray(pcm, dtype=np.int16)
        with self._lock:
            w = self._workers.get(channel)
            if w is None:
                w = self._workers[channel] = ChannelWorker(self, channel)
                w.start()
        w.put(Job(channel, item, pcm))

    def status(self) -> dict:
        return {"region": self.region, "vocabulary": self.vocabulary, "pace": self.pace,
                "workers": {c: w.state() for c, w in list(self._workers.items())},
                "last_error": self.last_error, "last_error_at": self.last_error_at,
                "done": self.done_n, "failed": self.failed_n}

    def stop(self) -> None:
        self.stopped.set()
        for w in list(self._workers.values()):
            w.wake()
        for w in list(self._workers.values()):
            w.join(timeout=8)

    # -- from the workers -------------------------------------------------------------------------------

    def _done(self, job: Job, text: str, conf: float | None) -> None:
        self.done_n += 1
        try:
            self.on_done(job.item, self.respell(text), conf)
        except Exception as e:  # the caller's trouble must not stop the stream
            self.log(f"aws transcribe: could not apply a result: {type(e).__name__}: {e}")

    def _fail(self, job: Job, why: str) -> None:
        self.failed_n += 1
        self.last_error, self.last_error_at = why, time.time()
        try:
            self.on_fail(job.item, why)
        except Exception as e:
            self.log(f"aws transcribe: fallback failed: {type(e).__name__}: {e}")

    def _bill(self, sent_s: float) -> None:
        if sent_s <= 0:
            return
        self.usage.add(max(MIN_BILLED_S, sent_s), sent_s)


class ChannelWorker(threading.Thread):
    """One channel's stream: feeds its transmissions, maps results back, opens and ends the stream."""

    def __init__(self, owner: AwsTranscriber, channel: str):
        super().__init__(name=f"aws-stt-{channel}", daemon=True)
        self.o, self.channel = owner, channel
        self.q: deque[Job] = deque()
        self.cv = threading.Condition()
        self.session: Session | None = None
        self.fed: list[Job] = []          # every job fed into this stream (for mapping words)
        self.pending: list[Job] = []      # fed, not done yet
        self.last_fed = 0.0
        self.errors = 0
        self.opened_n = 0

    def put(self, job: Job) -> None:
        with self.cv:
            self.q.append(job)
            self.cv.notify()

    def wake(self) -> None:
        with self.cv:
            self.cv.notify()

    def state(self) -> dict:
        s = self.session
        return {"open": s is not None, "sent_s": round(s.sent_s, 1) if s else 0.0, "queued": len(self.q),
                "pending": len(self.pending), "opened": self.opened_n}

    def run(self) -> None:
        while not self.o.stopped.is_set():
            with self.cv:
                if not self.q:
                    self.cv.wait(0.05 if (self.session is not None) else 1.0)
                job = self.q.popleft() if self.q else None
            try:
                if job is not None:
                    self._feed(job)
                self._settle()
                self._idle()
            except Exception as e:  # never let one channel's thread die
                self.o.log(f"aws transcribe {self.channel}: {type(e).__name__}: {e}")
                self._broken(("Internal", f"{type(e).__name__}: {e}"))
        if self.session is not None:
            self._end()
        for j in list(self.q):
            self.o._fail(j, "speech-to-text stopped")

    # -- the stream ------------------------------------------------------------------------------------

    def _open(self) -> None:
        url = self.o.url()
        ws = self.o.connect(url, timeout=10.0)
        self.session = Session(ws, self.o.log)
        self.fed, self.pending = [], []
        self.opened_n += 1

    def _feed(self, job: Job) -> None:
        o = self.o
        s = self.session
        if s is not None and (s.error or s.closed):
            self._broken(s.error or ("Closed", "the stream closed"))
            s = None
        if s is not None and (s.sent_s + job.seconds + o.s.gap_s > MAX_STREAM_S or time.time() - s.opened > MAX_STREAM_S):
            self._end()                                   # Transcribe's 4 h limit: a fresh stream
            s = None
        ok, why = o.budget_ok(job.seconds + (MIN_BILLED_S if s is None else 0.0))
        if not ok:
            return o._fail(job, why)
        if s is None:
            try:
                self._open()
            except (OSError, aws.AwsError) as e:
                return self._retry([job], self._kind(e), str(e))
            s = self.session
        step = max(1, int(o.s.chunk_s * RATE))
        pcm = job.pcm.astype("<i2").tobytes()
        job.a = s.sent_s
        try:
            for i in range(0, len(pcm), step * 2):
                if s.error or s.closed:
                    break
                s.send(pcm[i:i + step * 2])
                time.sleep(o.s.chunk_s / o.pace)
            job.b = s.sent_s
            s.send(bytes(int(o.s.gap_s * RATE) * 2))
        except OSError as e:
            self.fed.append(job)
            self.pending.append(job)
            return self._broken(self._why(s, e))
        job.sent_at = self.last_fed = time.time()
        self.fed.append(job)
        self.pending.append(job)
        if s.error or s.closed:
            self._broken(s.error or ("Closed", "the stream closed"))

    def _owner(self, t: float) -> Job | None:
        owner = None
        for j in self.fed:
            if j.a <= t + 1e-6:
                owner = j
            else:
                break
        return owner or (self.fed[0] if self.fed else None)

    def _finish(self, job: Job, words: list[Word]) -> None:
        mine = [w for w in words if self._owner(w.start) is job]
        conf = [w.confidence for w in mine if w.confidence is not None and w.kind != "punctuation"]
        job.done = True
        self.o._done(job, words_text(mine), round(float(np.mean(conf)), 2) if conf else None)

    def _settle(self) -> None:
        s, o = self.session, self.o
        if s is None:
            return
        if s.error or (s.closed and self.pending):
            return self._broken(s.error or ("Closed", "the stream closed"))
        while self.pending:
            now = time.time()
            j = self.pending[0]
            with s.cond:
                partial, final_end, words = s.partial, s.final_end, list(s.words)
            open_in = partial is not None and partial[1] < j.b - 1e-6
            if not open_in and (final_end >= j.b - 1e-6 or now - j.sent_at >= o.s.quiet_s):
                self.pending.pop(0)
                self._finish(j, words)
                continue
            if open_in and not self.q and now - j.sent_at >= o.s.quiet_s + o.s.settle_s:
                if j.flushed < o.s.flush_s:              # a little silence (billed) so the open result ends
                    try:
                        s.send(bytes(int(0.2 * RATE) * 2))
                        j.flushed += 0.2
                    except OSError as e:
                        return self._broken(self._why(s, e))
                else:
                    self._end()                           # end the stream: Transcribe finalizes everything
                    return
            break

    def _idle(self) -> None:
        s, o = self.session, self.o
        if s is None or self.pending or self.q:
            return
        now = time.time()
        if now - self.last_fed >= o.s.idle_s or now - s.opened >= MAX_STREAM_S:
            return self._end()
        if now - s.last_sent >= KEEPALIVE_S:               # keep it open: a little silence (billed)
            try:
                s.send(bytes(int(KEEPALIVE_AUDIO_S * RATE) * 2))
            except OSError as e:
                self._broken(self._why(s, e))

    def _end(self) -> None:
        """End the stream: everything not done yet gets what Transcribe finalizes, then it is billed."""
        s = self.session
        if s is None:
            return
        try:
            if not s.closed and not s.error:
                s.end()
                with s.cond:
                    s.cond.wait_for(lambda: s.closed or s.error is not None, timeout=8.0)
        except OSError:
            pass
        if s.error and self.pending:
            return self._broken(s.error)
        with s.cond:
            words = list(s.words)
            if s.partial is not None:                     # never finalized: its words as they last stood
                words += [w for w in s.partial[3] if w.start >= s.final_end - 1e-6]
        for j in self.pending:
            self._finish(j, words)
        self.pending = []
        self.session = None
        self.fed = []
        s.close()
        self.o._bill(s.sent_s)
        self.errors = 0

    @staticmethod
    def _why(s: Session, e: Exception) -> tuple[str, str]:
        """A send failed: Transcribe's own reason when it gave one (its exception closes the connection)."""
        with s.cond:
            s.cond.wait_for(lambda: s.closed or s.error is not None, timeout=1.0)
            return s.error or ("Connection", f"{type(e).__name__}: {e}")

    @staticmethod
    def _kind(e: Exception) -> str:
        if isinstance(e, WsHandshakeError):
            return "AccessDenied" if e.status == 403 else f"HTTP {e.status}"
        if isinstance(e, aws.AwsError):
            return e.code or "AwsError"
        return "Connection"

    def _broken(self, err: tuple[str, str]) -> None:
        """The stream failed: bill what was sent, retry what wasn't done."""
        s = self.session
        jobs = [j for j in self.pending if not j.done]
        self.pending, self.fed, self.session = [], [], None
        if s is not None:
            s.close()
            self.o._bill(s.sent_s)
        kind, msg = err
        self.o.log(f"aws transcribe {self.channel}: {kind}: {msg}")
        if kind == "BadRequestException" and re.search(r"real.?time|too fast|rate", msg, re.I) and self.o.pace > 1.0:
            self.o.pace = 1.0
            self.o.log("aws transcribe: sending in real time from now on")
        self._retry(jobs, kind, msg)

    def _retry(self, jobs: list[Job], kind: str, msg: str) -> None:
        self.errors += 1
        why = f"AWS Transcribe: {kind}: {msg}"[:300]
        self.o.last_error, self.o.last_error_at = why, time.time()
        if kind in ("AccessDenied", "NoCredentials", "AccessDeniedException"):
            aws.forget_credentials()
            for j in jobs:
                self.o._fail(j, why)
            return
        again = [j for j in jobs if j.attempts < RETRIES]
        for j in jobs:
            if j.attempts >= RETRIES:
                self.o._fail(j, why)
        if not again:
            return
        delay = min(60.0, (5.0 if kind == "LimitExceededException" else 1.0) * 2 ** (self.errors - 1))
        self.o.stopped.wait(delay)
        with self.cv:
            for j in reversed(again):
                j.attempts += 1
                j.a = j.b = j.sent_at = j.flushed = 0.0
                self.q.appendleft(j)


# -- custom vocabulary ------------------------------------------------------------------------------------

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
MAX_PHRASE = 256
MAX_PHRASES = 500


def _two(n: int) -> str:
    return ONES[n] if n < 20 else TENS[n // 10] + (f" {ONES[n % 10]}" if n % 10 else "")


def spoken_number(d: str) -> str:
    """Digits as said on the radio: 60 -> sixty, 31 -> thirty one, 401 -> four oh one, 2398 -> twenty
    three ninety eight, 1500 -> fifteen hundred; longer: digit by digit."""
    n = len(d)
    if n == 1:
        return ONES[int(d)]
    if n == 2:
        return f"oh {ONES[int(d[1])]}" if d[0] == "0" else _two(int(d))
    if n == 3:
        rest = "hundred" if d[1:] == "00" else f"oh {ONES[int(d[2])]}" if d[1] == "0" else _two(int(d[1:]))
        return f"{ONES[int(d[0])]} {rest}"
    if n == 4 and d[0] != "0":
        if d[2:] == "00":
            return f"{_two(int(d[:2]))} hundred"
        return f"{_two(int(d[:2]))} " + (f"oh {ONES[int(d[3])]}" if d[2] == "0" else _two(int(d[2:])))
    return " ".join(ONES[int(c)] for c in d)


def spoken(phrase: str) -> str:
    """A phrase in words: digits spelled out, '/' and '&' said."""
    p = phrase.replace("&", " and ").replace("/", " ").replace("_", " ")
    p = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", p)
    p = re.sub(r"\d+", lambda m: f" {spoken_number(m.group())} ", p)
    return " ".join(p.split())


def to_aws(phrase: str) -> tuple[str | None, str]:
    """(the phrase as AWS takes it, "") or (None, why it can't go)."""
    words = re.sub(r"[^A-Za-z'. -]", " ", spoken(phrase)).replace("-", " ").split()
    words = [w.strip(".'") if not re.fullmatch(r"(?:[A-Za-z]\.)+", w) else w for w in words]
    words = [w for w in words if w]
    if not words:
        return None, "nothing left to say"
    out = "-".join(words)
    if len(out) > MAX_PHRASE:
        return None, f"longer than {MAX_PHRASE} characters"
    return out, ""


def build_vocabulary(phrases: list[str]) -> dict:
    """{phrases: [as sent to AWS], skipped: [{phrase, why}], respell: {"engine sixty": "Engine 60"}}."""
    out, skipped, seen, respell_map = [], [], set(), {}
    for p in phrases:
        p = " ".join(str(p).split())
        if not p:
            continue
        a, why = to_aws(p)
        if a is None:
            skipped.append({"phrase": p, "why": why})
            continue
        if a.lower() in seen:
            continue
        if len(out) >= MAX_PHRASES:
            skipped.append({"phrase": p, "why": f"over {MAX_PHRASES} phrases"})
            continue
        seen.add(a.lower())
        out.append(a)
        if re.search(r"\d", p):
            respell_map[a.replace("-", " ").lower()] = p
    return {"phrases": out, "skipped": skipped, "respell": respell_map}


def respell(text: str, mapping: dict[str, str]) -> str:
    """Spelled-out vocabulary words back to how they're written ("Engine sixty" -> "Engine 60")."""
    for said, written in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![A-Za-z])" + r"[\s-]+".join(re.escape(w) for w in said.split()) + r"(?![A-Za-z])",
                      written, text, flags=re.I)
    return text


STREET_WORDS = {"St": "Street", "Ave": "Avenue", "Rd": "Road", "Dr": "Drive", "Ln": "Lane", "Pl": "Place", "Ct": "Court",
                "Ter": "Terrace", "Blvd": "Boulevard", "Cres": "Crescent", "Pkwy": "Parkway", "Hwy": "Highway",
                "Cir": "Circle", "Tpke": "Turnpike", "Sq": "Square", "Mt": "Mount", "Ext": "Extension"}
STREET_DIRS = {"N": "North", "S": "South", "E": "East", "W": "West"}


def street_spoken(name: str) -> str:
    """'N Main St' -> 'North Main Street'."""
    words = name.split()
    out = []
    for i, w in enumerate(words):
        if w in STREET_DIRS and (i == 0 or i == len(words) - 1):
            out.append(STREET_DIRS[w])
        else:
            out.append(STREET_WORDS.get(w, w))
    return " ".join(out)


class Vocabulary:
    """The custom vocabulary on AWS (CreateVocabulary / UpdateVocabulary / GetVocabulary), its state
    kept through get/put (the CAD's meta table): {state: none | PENDING | READY | FAILED | error,
    reason, phrases, updated, checked}."""

    KEY = "aws_vocabulary"

    def __init__(self, region: str, get: Callable[[str], str | None], put: Callable[[str, str], None],
                 name: str = VOCABULARY, endpoint: str = "", call: Callable[..., dict] = aws.json_call,
                 log: Callable[[str], None] = print, poll_s: float = 10.0, on_ready: Callable[[str], None] = lambda n: None):
        self.region, self.name, self.endpoint, self.call, self.log = region, name, endpoint, call, log
        self._get, self._put, self.poll_s, self.on_ready = get, put, poll_s, on_ready
        self._lock = threading.Lock()
        self._polling = False

    def state(self) -> dict:
        try:
            d = json.loads(self._get(self.KEY) or "{}")
        except ValueError:
            d = {}
        return {"name": self.name, "state": d.get("state") or "none", "reason": d.get("reason") or "",
                "phrases": int(d.get("phrases") or 0), "updated": d.get("updated"), "checked": d.get("checked"),
                "respell": d.get("respell") or {}, "sig": d.get("sig") or ""}

    def _save(self, **kw) -> dict:
        with self._lock:
            d = self.state()
            d.update(kw)
            d.pop("name", None)
            self._put(self.KEY, json.dumps(d))
        return self.state()

    def ready(self) -> bool:
        return self.state()["state"] == "READY"

    def _api(self, op: str, body: dict) -> dict:
        return self.call(SERVICE, f"Transcribe.{op}", body, self.region, self.endpoint)

    def push(self, phrases: list[str]) -> dict:
        """Send the list (as build_vocabulary() makes it): Update when it exists, else Create; then
        GetVocabulary every poll_s until READY or FAILED (in the background)."""
        built = build_vocabulary(phrases)
        if not built["phrases"]:
            return self._save(state="error", reason="no phrases to send")
        body = {"VocabularyName": self.name, "LanguageCode": "en-US", "Phrases": built["phrases"]}
        try:
            try:
                self._api("GetVocabulary", {"VocabularyName": self.name})
                exists = True
            except aws.AwsError as e:
                if e.code not in ("NotFoundException", "BadRequestException") and e.status != 400:
                    raise
                exists = False
            r = self._api("UpdateVocabulary" if exists else "CreateVocabulary", body)
        except aws.AwsError as e:
            self.log(f"aws transcribe: vocabulary {self.name}: {e}")
            return self._save(state="error", reason=str(e)[:300], checked=time.time())
        st = self._save(state=str(r.get("VocabularyState") or "PENDING"), reason=str(r.get("FailureReason") or ""),
                        phrases=len(built["phrases"]), updated=time.time(), checked=time.time(), respell=built["respell"],
                        sig=hashlib.sha1(json.dumps(built["phrases"]).encode()).hexdigest()[:12])
        self.log(f"aws transcribe: vocabulary {self.name}: {len(built['phrases'])} phrases sent ({st['state']})")
        self.poll()
        return st

    def check(self) -> dict:
        try:
            r = self._api("GetVocabulary", {"VocabularyName": self.name})
        except aws.AwsError as e:
            if e.code == "NotFoundException" or (e.status == 400 and "not" in str(e).lower() and "found" in str(e).lower()):
                return self._save(state="none", reason="", checked=time.time())
            return self._save(state="error", reason=str(e)[:300], checked=time.time())
        st = self._save(state=str(r.get("VocabularyState") or "PENDING"), reason=str(r.get("FailureReason") or ""),
                        checked=time.time())
        if st["state"] == "READY":
            self.on_ready(self.name)
        return st

    def poll(self) -> None:
        """GetVocabulary until it isn't PENDING (in a thread; one at a time)."""
        with self._lock:
            if self._polling:
                return
            self._polling = True

        def run():
            try:
                for _ in range(360):                    # an hour at 10 s
                    st = self.check()
                    if st["state"] != "PENDING":
                        self.log(f"aws transcribe: vocabulary {self.name}: {st['state']}"
                                 + (f" ({st['reason']})" if st["reason"] else ""))
                        return
                    time.sleep(self.poll_s)
            finally:
                with self._lock:
                    self._polling = False
        threading.Thread(target=run, name="aws-vocabulary", daemon=True).start()


def pcm8k(audio: np.ndarray, rate: int) -> np.ndarray:
    """float [-1, 1] or int16 audio at 16 or 8 kHz -> int16 at 8 kHz (already band-limited to 3.7 kHz:
    every other sample, as the station's clip_8k)."""
    a = np.asarray(audio)
    if a.dtype != np.int16:
        a = (np.clip(a.astype(np.float32), -1, 1) * 32767).astype(np.int16)
    if rate == 16000:
        a = a[::2]
    elif rate != RATE:
        raise ValueError(f"audio at {rate} Hz: 8 or 16 kHz only")
    return np.ascontiguousarray(a)


def wav_pcm(wav: bytes, skip_s: float = 0.0) -> np.ndarray | None:
    """A mono 16-bit WAV (a clip) -> int16 at 8 kHz, from skip_s on. None if it isn't one."""
    import io
    import wave
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return None
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    except (wave.Error, EOFError):
        return None
    pcm = pcm[int(skip_s * rate):]
    try:
        return pcm8k(pcm, rate)
    except ValueError:
        return None
