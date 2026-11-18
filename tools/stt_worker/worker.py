#!/usr/bin/env python3
"""sizeup's GPU speech-to-text worker (#639): it takes clips from the hub's STT pool, runs them through a local
whisper.cpp whisper-server and sends the text back.

Standard library only, Python 3.10+, so it runs from a plain python.org install on Windows. It only ever connects
out: to the hub (HTTPS, through Cloudflare Access) and to whisper-server on 127.0.0.1. Nothing listens.

    python worker.py run    [-c worker.toml]     the worker (Task Scheduler runs this at logon)
    python worker.py check  [-c worker.toml]     config, hub and whisper-server, once
    python worker.py status                      what the running worker is doing
    python worker.py gaming on|off               manual gaming mode (no new jobs; the heartbeat goes on)
    python worker.py pause | resume              paused (no new jobs) / back to online (clears gaming and pause)

The token comes from SIZEUP_STT_WORKER_TOKEN (preferred) and is never logged or printed. Setup:
tools/stt_worker/README.md."""

from __future__ import annotations

import argparse
import dataclasses
import http.client
import io
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

VERSION = "2"
USER_AGENT = f"sizeup-stt-worker/{VERSION}"
API = "/api/station/stt"
TOKEN = re.compile(r"sw1\.([a-z0-9][a-z0-9_-]{0,39})\.([A-Za-z0-9_-]+)")
BACKOFF = (1, 2, 5, 10, 30)                # hub unreachable / 5xx: waits in a row, then the last one
MAX_CLAIM_WAIT_S = 25
HEARTBEAT_TIMEOUT_S = 4.0                 # a slow answer mustn't hold the next heartbeat past the hub's 12 s
MAX_HEARTBEAT_S = 5                       # the hub takes back a silent worker's leases after 12 s
LOOPBACK = ("127.0.0.1", "localhost", "::1")
LONG_CLASSES = {"RERUN", "LOW", "CATCHUP"}  # long leases: gaming hands these back at once
ENV = {"hub_url": "SIZEUP_STT_HUB_URL", "token": "SIZEUP_STT_WORKER_TOKEN",
       "cf_client_id": "SIZEUP_STT_CF_CLIENT_ID", "cf_client_secret": "SIZEUP_STT_CF_CLIENT_SECRET"}
SECRET_ENV = ("SIZEUP_STT_WORKER_TOKEN", "SIZEUP_STT_CF_CLIENT_ID", "SIZEUP_STT_CF_CLIENT_SECRET")

log = logging.getLogger("sizeup.stt_worker")


# --- config ------------------------------------------------------------------------------------------------------

def default_state_dir() -> str:
    if os.name == "nt":
        return str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "sizeup-stt" / "state")
    return str(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sizeup-stt-worker")


@dataclasses.dataclass
class Config:
    hub_url: str = ""
    token: str = dataclasses.field(default="", repr=False)
    token_file: str = ""                   # a file holding the token, when the env var isn't used
    cf_client_id: str = ""
    cf_client_secret: str = dataclasses.field(default="", repr=False)
    whisper_url: str = "http://127.0.0.1:8179/inference"
    whisper_server: list = dataclasses.field(default_factory=list)  # argv: the worker starts and restarts it
    whisper_process: str = "whisper-server"  # its process name, so its GPU use isn't taken for a game
    whisper_pid: int = 0
    model: str = "large-v3-turbo-q5_0"
    state_dir: str = ""                    # control.json (gaming / pause), status.json, the logs
    auto_gaming: bool = True
    fullscreen_gaming: bool = True         # Windows: a full-screen game or app (the shell's notification state)
    nvidia_smi: str = "nvidia-smi"
    gpu_poll_s: float = 5.0
    gaming_mem_mb: float = 1024.0          # another process using more GPU memory than this ...
    gaming_util_pct: float = 40.0          # ... or the GPU this busy while we are idle ...
    gaming_samples: int = 2                # ... this many samples in a row: gaming
    gaming_clear_s: float = 60.0           # gaming ends after this long below both
    gaming_stop_whisper_min: float = 10.0  # gaming this long: the worker's own whisper-server stopped (VRAM); 0 never
    heartbeat_s: float = 5.0               # <= 5
    claim_wait_s: float = 25.0             # <= 25
    whisper_timeout_s: float = 60.0
    whisper_fail_max: int = 3              # whisper-server failures in a row: paused until it answers again
    whisper_check_s: float = 10.0
    unauthorized_retry_s: float = 300.0    # the hub or Cloudflare refused us: try again this often
    finish_s: float = 3.0                  # gaming mid-job: finish a short-class job only if its lease ends this soon
    lease_margin_s: float = 0.5
    busy_grace_s: float = 3.0              # GPU load this soon after our own inference is still ours
    tick_s: float = 0.5
    log_file: str = ""                     # default <state_dir>/worker.log; "-" = stderr only
    base_dir: str = dataclasses.field(default="", repr=False)  # the config file's folder (not a key)

    @property
    def worker_id(self) -> str:
        m = TOKEN.fullmatch(self.token or "")
        return m.group(1) if m else ""

    @property
    def control_path(self) -> Path:
        return Path(self.state_dir) / "control.json"

    @property
    def status_path(self) -> Path:
        return Path(self.state_dir) / "status.json"

    def secrets(self) -> list[str]:
        return [s for s in (self.token, self.cf_client_secret, self.token.rpartition(".")[2]) if s and len(s) >= 8]


class ConfigError(ValueError):
    pass


def default_config_path() -> Path | None:
    env = os.environ.get("SIZEUP_STT_WORKER_CONFIG")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    for name in ("worker.toml", "worker.json"):
        if (here / name).exists():
            return here / name
    return None


def load_config(path: str | os.PathLike | None, env: dict | None = None) -> Config:
    """worker.toml (Python 3.11+) or worker.json, flat or under [worker]; then the environment (SIZEUP_STT_*) on top.
    Paths may use %LOCALAPPDATA% / $HOME; relative ones are from the config file's folder."""
    env = os.environ if env is None else env
    doc: dict = {}
    base = ""
    if path:
        p = Path(path)
        base = str(p.resolve().parent)
        try:
            raw = p.read_bytes()
        except OSError as e:
            raise ConfigError(f"can't read {p}: {e.strerror or e}") from None
        if p.suffix.lower() == ".toml":
            try:
                import tomllib
            except ImportError:
                raise ConfigError("a .toml config needs Python 3.11+: use worker.json on 3.10") from None
            try:
                doc = tomllib.loads(raw.decode("utf-8-sig"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
                raise ConfigError(f"{p.name}: {e}") from None
        else:
            try:
                doc = json.loads(raw.decode("utf-8-sig") or "{}")
            except (ValueError, UnicodeDecodeError) as e:
                raise ConfigError(f"{p.name}: {e}") from None
        if not isinstance(doc, dict):
            raise ConfigError(f"{p.name}: not a table")
        doc = doc.get("worker", doc)
    keys = {f.name for f in dataclasses.fields(Config)} - {"base_dir"}
    unknown = sorted(set(doc) - keys)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")
    cfg = Config(base_dir=base)
    for key, value in doc.items():
        setattr(cfg, key, _coerce(key, value, getattr(cfg, key)))
    for key, var in ENV.items():
        if env.get(var):
            setattr(cfg, key, env[var].strip())
    for key in ("token_file", "state_dir", "log_file"):
        if getattr(cfg, key) and getattr(cfg, key) != "-":
            setattr(cfg, key, _path(getattr(cfg, key), base))
    if not cfg.token and cfg.token_file:
        try:
            cfg.token = Path(cfg.token_file).read_text(encoding="utf-8-sig").strip()
        except OSError as e:
            raise ConfigError(f"can't read token_file: {e.strerror or e}") from None
    cfg.state_dir = cfg.state_dir or default_state_dir()
    cfg.heartbeat_s = min(max(cfg.heartbeat_s, 0.01), MAX_HEARTBEAT_S)
    cfg.claim_wait_s = min(max(cfg.claim_wait_s, 0.0), MAX_CLAIM_WAIT_S)
    return cfg


def _path(value: str, base: str) -> str:
    p = Path(os.path.expanduser(os.path.expandvars(value)))
    return str(p if p.is_absolute() or not base else Path(base) / p)


def _coerce(key: str, value, default):
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, list):
            if isinstance(value, str):
                return value.split()
            return [str(v) for v in value]
        return type(default)(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected {type(default).__name__}") from None


def validate(cfg: Config) -> None:
    """For `run` / `check`: what the worker needs to reach the hub. Never quotes the token."""
    u = urlsplit(cfg.hub_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ConfigError("hub_url: set it (SIZEUP_STT_HUB_URL or hub_url), e.g. https://hub.example.org")
    if u.scheme == "http" and u.hostname not in LOOPBACK:
        raise ConfigError("hub_url: https only (the token must not cross the internet in the clear)")
    if not cfg.token:
        raise ConfigError("no token: set SIZEUP_STT_WORKER_TOKEN (an admin issues it in Settings > Station > STT workers)")
    if not TOKEN.fullmatch(cfg.token):
        raise ConfigError("the token isn't a worker token (sw1.<worker>.<secret>): check SIZEUP_STT_WORKER_TOKEN")
    if bool(cfg.cf_client_id) != bool(cfg.cf_client_secret):
        raise ConfigError("Cloudflare Access: set both SIZEUP_STT_CF_CLIENT_ID and SIZEUP_STT_CF_CLIENT_SECRET")
    w = urlsplit(cfg.whisper_url)
    if w.scheme != "http" or w.hostname not in LOOPBACK:
        raise ConfigError("whisper_url: whisper-server on this PC only, e.g. http://127.0.0.1:8179/inference")
    argv = [str(a) for a in cfg.whisper_server]
    for i, a in enumerate(argv):
        if a == "--host" and (i + 1 >= len(argv) or argv[i + 1] not in LOOPBACK):
            # whisper-server answers any web page (CORS *): on the network, any site could use the GPU
            raise ConfigError("whisper_server: --host must be 127.0.0.1 (whisper-server must not listen on the network)")


class Redact(logging.Filter):
    """Belt and braces: a secret that somehow reaches a log line is replaced."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            if any(s in msg for s in self.secrets):
                for s in self.secrets:
                    msg = msg.replace(s, "***")
                record.msg, record.args = msg, None
        return True


def setup_logging(cfg: Config) -> None:
    """worker.log in state_dir (rotated: 1 MB, 3 old copies), and stderr when there is one (pythonw.exe has none).
    An uncaught exception anywhere is logged there too."""
    log.setLevel(logging.INFO)
    for f in list(log.filters):
        if isinstance(f, Redact):
            log.removeFilter(f)
    log.addFilter(Redact(cfg.secrets()))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if cfg.log_file != "-":
        path = Path(cfg.log_file or Path(cfg.state_dir) / "worker.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)

    def _thread_hook(a):
        log.error("thread %s died", a.thread.name if a.thread else "?", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

    threading.excepthook = _thread_hook
    sys.excepthook = lambda t, v, tb: log.critical("worker died", exc_info=(t, v, tb))


def child_env() -> dict:
    """The environment for whisper-server, nvidia-smi and tasklist: without the worker's secrets."""
    return {k: v for k, v in os.environ.items() if k.upper() not in SECRET_ENV}


# --- files: control (written by the CLI) and status (written by the running worker) ------------------------------

def read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    for attempt in range(20):              # Windows: a reader holding the file open makes os.replace fail for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


class Control:
    """control.json: {"gaming": bool, "paused": bool}. The CLI writes it, the worker re-reads it when it changes.
    It outlives restarts."""

    def __init__(self, path: Path):
        self.path = path
        self._sig: tuple | None = None
        self._doc: dict = {}

    def read(self) -> dict:
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig != self._sig:
            self._sig, self._doc = sig, (read_json(self.path) if sig else {})
        return self._doc

    def set(self, **changes) -> dict:
        doc = read_json(self.path)
        doc.update(changes)
        doc["changed"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        write_json(self.path, doc)
        return doc


class InstanceLock:
    """state_dir/worker.lock, locked for the life of the process (msvcrt.locking on Windows, flock elsewhere): the
    OS lets go of it when the process ends, however it ends (`schtasks /End` kills without a word), so a worker
    killed a moment ago never blocks the next one, and a second live one on the same state_dir is refused."""

    def __init__(self, state_dir: str):
        self.path = Path(state_dir) / "worker.lock"
        self._f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()).encode())
        f.flush()
        self._f = f
        return True

    def holder(self) -> str:
        try:
            return self.path.read_text(encoding="ascii", errors="replace").strip() or "?"
        except OSError:
            return "?"

    def release(self) -> None:
        if self._f is not None:
            self._f.close()                # closing the file lets go of the lock
            self._f = None


# --- the hub -----------------------------------------------------------------------------------------------------

class HubError(Exception):
    def __init__(self, msg: str, status: int = 0, retry_after: float | None = None, reason: str = ""):
        super().__init__(msg)
        self.status, self.retry_after, self.reason = status, retry_after, reason

    @property
    def refused(self) -> bool:
        """The token or Cloudflare Access said no: retrying soon won't help."""
        return self.status in (401, 403) or 300 <= self.status < 400

    @property
    def lease_lost(self) -> bool:
        """409: the lease is gone; 410: the hub had no audio and gave the job to another engine; 404: unknown."""
        return self.status in (404, 409, 410)


def retry_after(value) -> float | None:
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 3600 else None


class Hub:
    """One kept-alive HTTPS connection (use one Hub per thread). The bearer token and the Cloudflare Access service
    token go on every request; redirects are never followed (Cloudflare Access answers a bad service token with one)."""

    def __init__(self, base_url: str, token: str, cf_id: str = "", cf_secret: str = "", timeout: float = 15.0):
        u = urlsplit(base_url)
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname or "", u.port or (443 if self.https else 80)
        self.origin = (u.scheme, self.host, self.port)
        self.prefix = (u.path or "").rstrip("/")
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT, "Accept": "application/json"}
        if cf_id and cf_secret:
            self._headers.update({"CF-Access-Client-Id": cf_id, "CF-Access-Client-Secret": cf_secret})
        self._conn: http.client.HTTPConnection | None = None
        self.interrupted = False           # set by interrupt(); the caller clears it before a request it may interrupt

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    def interrupt(self) -> None:
        """From another thread: a request in flight (a long poll) fails now, and isn't tried again."""
        self.interrupted = True
        conn = self._conn
        sock = conn.sock if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self._conn is None:
            if self.https:
                self._conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                                         context=ssl.create_default_context())
            else:
                self._conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        self._conn.timeout = timeout
        if self._conn.sock is not None:
            self._conn.sock.settimeout(timeout)
        return self._conn

    def path_of(self, ref: str) -> str:
        """A job's audio reference: a path on the hub, or a URL on the hub's own origin (the token never goes elsewhere)."""
        if ref.startswith("/") and not ref.startswith("//"):
            return ref
        u = urlsplit(ref)
        if (u.scheme, u.hostname or "", u.port or (443 if u.scheme == "https" else 80)) != self.origin:
            raise HubError("the job's audio isn't on the hub")
        return u.path + (f"?{u.query}" if u.query else "")

    def request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None,
                raw: bool = False):
        """A JSON object in and out (raw: the body's bytes). HubError on anything but a 2xx JSON object."""
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = dict(self._headers, **({"Content-Type": "application/json"} if data is not None else {}))
        full = path if path.startswith(self.prefix + "/") and self.prefix else self.prefix + path
        for attempt in (0, 1):             # a kept-alive connection the far end closed: one quiet retry
            if self.interrupted:
                self.close()
                raise HubError("interrupted")
            conn = self._connection(timeout or self.timeout)
            try:
                conn.request(method, full, body=data, headers=headers)
                r = conn.getresponse()
                payload = r.read()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as e:
                self.close()
                if attempt == 0:
                    continue
                raise HubError(f"hub closed the connection ({type(e).__name__})") from None
            except (OSError, http.client.HTTPException) as e:
                self.close()
                raise HubError(f"can't reach the hub ({type(e).__name__}: {e})") from None
            if r.getheader("Connection", "").lower() == "close":
                self.close()
            if not 200 <= r.status < 300:
                self.close()
                err, reason = "", ""
                try:
                    doc = json.loads(payload)
                    err, reason = str(doc.get("error") or "")[:120], str(doc.get("reason") or "")[:40]
                except (ValueError, AttributeError):
                    pass
                if r.status == 401:
                    msg = f"hub refused the token ({reason or 'no reason'})"
                elif r.status == 403 or 300 <= r.status < 400:
                    reason = reason or "cloudflare_access"
                    msg = f"refused ({r.status}): is the Cloudflare Access service token right?"
                else:
                    msg = f"hub said {r.status}" + (f": {err}" if err else "")
                raise HubError(msg, r.status, retry_after(r.getheader("Retry-After")), reason)
            if raw:
                return payload
            try:
                doc = json.loads(payload or b"{}")
            except ValueError:
                doc = None
            if not isinstance(doc, dict):
                self.close()
                raise HubError(f"hub sent something that isn't a JSON object ({r.getheader('Content-Type')}): "
                               "is Cloudflare Access letting the worker through?", r.status)
            return doc
        raise HubError("unreachable")


class Backoff:
    """1, 2, 5, 10, 30, 30 ... s after failures in a row; the hub's Retry-After when it gives one."""

    def __init__(self, steps: tuple = BACKOFF):
        self.steps, self.n = steps, 0

    def fail(self, err: BaseException | None = None) -> float:
        wait = self.steps[min(self.n, len(self.steps) - 1)]
        self.n += 1
        ra = getattr(err, "retry_after", None)
        if ra is not None:
            wait = min(max(ra, 0.5), 300.0)
        return float(wait)

    def ok(self) -> None:
        self.n = 0


# --- whisper-server ----------------------------------------------------------------------------------------------

class WhisperError(Exception):
    pass


def silence_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))
    return buf.getvalue()


class WhisperCall:
    """One /inference request on its own connection, which another thread can abort(): whisper-server stops decoding
    when the client goes away, so a job handed back doesn't keep the GPU busy."""

    def __init__(self, whisper: "Whisper", body: bytes, ctype: str, timeout: float):
        self.whisper, self.body, self.ctype = whisper, body, ctype
        self.conn = http.client.HTTPConnection(whisper.host, whisper.port, timeout=timeout)
        self.aborted = False

    def run(self) -> dict:
        try:
            if self.aborted:
                raise OSError("aborted")
            self.conn.request("POST", self.whisper.path, body=self.body, headers={"Content-Type": self.ctype})
            r = self.conn.getresponse()
            data = r.read()
        except (OSError, http.client.HTTPException) as e:
            if self.aborted:
                raise WhisperError("aborted") from None
            raise WhisperError(f"whisper-server unreachable ({type(e).__name__})") from None
        finally:
            self.conn.close()
        if r.status != 200:
            raise WhisperError(f"whisper-server said {r.status}")
        try:
            out = json.loads(data)
        except ValueError:
            raise WhisperError("whisper-server sent something that isn't JSON") from None
        if not isinstance(out, dict) or ("error" in out and "text" not in out):
            raise WhisperError(f"whisper-server: {str(out.get('error') if isinstance(out, dict) else out)[:80]}")
        return out

    def abort(self) -> None:
        self.aborted = True
        sock = self.conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self.conn.close()
        except OSError:
            pass


class Whisper:
    """whisper.cpp's whisper-server, called exactly as the station's WhisperClient does (sizeup/stt.py): a multipart
    POST to /inference with response_format=verbose_json, temperature=0.0, the prompt, temperature_inc=0.0 for a
    fast (greedy, no fallback) job, and the WAV as file=tx.wav. The hub's WAV is already padded and processed: it goes
    as it is."""

    def __init__(self, url: str, timeout_s: float = 60.0):
        self.url, self.timeout_s = url, timeout_s
        u = urlsplit(url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self.path = u.path or "/inference"

    def call(self, wav: bytes, prompt: str = "", fast: bool = False, timeout: float | None = None) -> WhisperCall:
        boundary = uuid.uuid4().hex
        fields = [("response_format", "verbose_json"), ("temperature", "0.0")]
        if prompt:
            fields.append(("prompt", prompt))
        if fast:
            fields.append(("temperature_inc", "0.0"))
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                 for name, value in fields]
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="tx.wav"\r\n'
                      "Content-Type: audio/wav\r\n\r\n").encode() + wav + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return WhisperCall(self, b"".join(parts), f"multipart/form-data; boundary={boundary}",
                           timeout or self.timeout_s)

    def transcribe(self, wav: bytes, prompt: str = "", fast: bool = False, timeout: float | None = None) -> dict:
        return self.call(wav, prompt, fast, timeout).run()

    def alive(self) -> bool:
        """Something is listening on its port (no GPU work)."""
        try:
            socket.create_connection((self.host, self.port), timeout=2).close()
            return True
        except OSError:
            return False

    def check(self) -> None:
        """A real inference on a second of silence: WhisperError if it fails."""
        self.transcribe(silence_wav(1.0), timeout=max(self.timeout_s, 30.0))


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def result_body(out: dict, model: str, fetch_ms: int, infer_ms: int) -> dict:
    """whisper-server's verbose_json -> the hub's complete body. The text is the engine's own (whitespace folded, as
    the station's WhisperClient does); the hub applies the no-speech and hallucination rules."""
    segments = []
    for s in out.get("segments") or []:
        if not isinstance(s, dict):
            continue
        words = [{"word": str(w.get("word", "")), "start": _num(w.get("start")), "end": _num(w.get("end")),
                  "probability": _num(w.get("probability"))}
                 for w in s.get("words") or [] if isinstance(w, dict)]
        segments.append({"start": _num(s.get("start")), "end": _num(s.get("end")), "text": str(s.get("text", "")),
                         "words": words})
    return {"text": " ".join(str(out.get("text", "")).split()), "segments": segments, "model": model,
            "timing": {"fetch_ms": int(fetch_ms), "infer_ms": int(infer_ms)}}


class WhisperProcess:
    """whisper-server started (and restarted) by the worker, when `whisper_server` is set: one Task Scheduler task,
    no console window, its pid known for the GPU check, its output in whisper-server.log (rotated), and none of the
    worker's secrets in its environment. When something already answers on the port, it is used and nothing is
    started. A relative program path is from the config file's folder, which is also its working directory."""

    def __init__(self, argv: list, log_path: Path, popen: Callable = subprocess.Popen, clock: Callable = time.monotonic,
                 base_dir: str = ""):
        self.argv, self.log_path, self.popen, self.clock, self.base_dir = argv, log_path, popen, clock, base_dir
        self.proc = None
        self.started = -1e9
        self._out: logging.Logger | None = None

    def command(self) -> list[str]:
        argv = [os.path.expandvars(str(a)) for a in self.argv]
        if argv and self.base_dir and not Path(argv[0]).is_absolute() and (Path(self.base_dir) / argv[0]).exists():
            argv[0] = str(Path(self.base_dir) / argv[0])
        return argv

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else 0

    def running(self) -> bool:
        return self.pid != 0

    def _logger(self) -> logging.Logger:
        if self._out is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            out = logging.getLogger("sizeup.stt_worker.whisper_server")
            out.propagate = False
            out.setLevel(logging.INFO)
            h = logging.handlers.RotatingFileHandler(self.log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            out.addHandler(h)
            self._out = out
        return self._out

    def _pump(self, stream) -> None:
        out = self._logger()
        try:
            for line in iter(stream.readline, b""):
                out.info(line.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def ensure(self, port_alive: bool) -> None:
        if self.running() or port_alive or self.clock() - self.started < 10:
            return
        if self.proc is not None:
            log.warning("whisper-server exited (%s): starting it again", self.proc.poll())
        self.started = self.clock()
        try:
            self.proc = self.popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=child_env(), cwd=self.base_dir or None,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.info("started whisper-server (pid %s)", self.proc.pid)
        except OSError as e:
            self.proc = None
            log.error("can't start whisper-server: %s", e)
            return
        if getattr(self.proc, "stdout", None) is not None:
            threading.Thread(target=self._pump, args=(self.proc.stdout,), name="whisper-server-log", daemon=True).start()

    def stop(self) -> None:
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --- the GPU: gaming detection -------------------------------------------------------------------------------------

def run_cmd(argv: list) -> str:
    """Runs a command (no console window, none of the worker's secrets), its stdout. FileNotFoundError when it isn't
    installed, CalledProcessError when it fails."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=True, env=child_env(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout


QUNS = {2: "a full-screen app", 3: "a full-screen Direct3D game"}   # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN


def windows_fullscreen() -> str:
    """Windows: what the shell says is full screen (SHQueryUserNotificationState), '' when nothing is or elsewhere.
    It catches a DirectX game that nvidia-smi doesn't list and whose memory WDDM doesn't report."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return ""
        return QUNS.get(state.value, "")
    except (OSError, AttributeError, ImportError, ValueError):
        return ""


class GpuMonitor:
    """Every gpu_poll_s: gaming when, for gaming_samples samples in a row, Windows reports a full-screen game or app,
    or another process (not our whisper-server) uses more than gaming_mem_mb of GPU memory, or the GPU is over
    gaming_util_pct while we are not inferring. A sample taken while we infer (or just did) says nothing about the
    load: it neither counts nor resets. Gaming ends after gaming_clear_s of quiet samples. No nvidia-smi and not
    Windows (a Mac): never gaming here (manual only)."""

    GPU_Q = "--query-gpu=utilization.gpu,memory.used,memory.total"
    APPS_Q = "--query-compute-apps=pid,used_memory"

    def __init__(self, cfg: Config, run: Callable[[list], str] = run_cmd, clock: Callable[[], float] = time.monotonic,
                 busy: Callable[[], bool] = lambda: False, ours: Callable[[], set] = set,
                 fullscreen: Callable[[], str] = windows_fullscreen):
        self.cfg, self.run, self.clock, self.busy, self.ours, self.fullscreen = cfg, run, clock, busy, ours, fullscreen
        self.available: bool | None = None  # nvidia-smi
        self.gaming = False
        self.why = ""
        self.info: dict | None = None
        self._hot = 0
        self._cool_since: float | None = None

    def _smi(self) -> tuple[float, float] | None:
        """(GPU %, the biggest other process's MB) from nvidia-smi, None when there is no reading."""
        if self.available is False:
            return None
        smi = self.cfg.nvidia_smi
        try:
            gpus = self.run([smi, self.GPU_Q, "--format=csv,noheader,nounits"])
            apps = self.run([smi, self.APPS_Q, "--format=csv,noheader,nounits"])
        except FileNotFoundError:
            if self.available is not False:
                log.info("no nvidia-smi: gaming detection from the GPU is off (worker.py gaming on|off)")
            self.available = False
            return None
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("nvidia-smi failed: %s", e)
            return None
        self.available = True
        util, used, total = 0.0, 0.0, 0.0
        for line in gpus.strip().splitlines():
            vals = [_num(x) for x in line.split(",")]
            if len(vals) >= 3:
                util = max(util, vals[0] or 0.0)
                used += vals[1] or 0.0
                total += vals[2] or 0.0
        ours = self.ours()
        others: list[float | None] = []
        for line in apps.strip().splitlines():
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < 2 or not cols[0].isdigit() or int(cols[0]) in ours:
                continue
            others.append(_num(cols[1]))     # None: "[N/A]" (Windows WDDM doesn't report it)
        self.info = {"util_pct": round(util), "mem_used_mb": round(used), "mem_total_mb": round(total),
                     "others": len(others)}
        return util, max((m for m in others if m is not None), default=0.0)

    def sample(self) -> None:
        full = ""
        if self.cfg.fullscreen_gaming:
            try:
                full = self.fullscreen() or ""
            except Exception:  # noqa: BLE001 - a best-effort check never stops the monitor
                full = ""
        smi = self._smi()
        if smi is None and self.available is not False and not full:
            return                           # nvidia-smi failed this time: no reading
        hot = full
        if not hot and smi is not None:
            util, biggest = smi
            if biggest > self.cfg.gaming_mem_mb:
                hot = f"another GPU process uses {round(biggest)} MB"
            elif util > self.cfg.gaming_util_pct:
                if self.busy():
                    return                   # that load may be ours: this sample says nothing
                hot = f"GPU {round(util)}% busy"
        now = self.clock()
        if hot:
            self._hot += 1
            self._cool_since = None
            if self.gaming:
                self.why = hot
            elif self._hot >= self.cfg.gaming_samples:
                self.gaming, self.why = True, hot
                log.info("gaming: %s", hot)
        else:
            self._hot = 0
            if self.gaming:
                if self._cool_since is None:
                    self._cool_since = now
                elif now - self._cool_since >= self.cfg.gaming_clear_s:
                    self.gaming, self.why, self._cool_since = False, "", None
                    log.info("gaming over: quiet for %.0f s", self.cfg.gaming_clear_s)


def find_pids(name: str, run: Callable[[list], str] = run_cmd) -> set:
    """Best effort: the pids of processes called `name` (tasklist on Windows, pgrep elsewhere)."""
    if not name:
        return set()
    try:
        if os.name == "nt":
            exe = name if name.lower().endswith(".exe") else name + ".exe"
            return parse_tasklist(run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"]))
        return {int(x) for x in run(["pgrep", "-f", name]).split() if x.isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()


def parse_tasklist(out: str) -> set:
    """`tasklist /FO CSV /NH` lines ("name","pid",...): the pids; "INFO: No tasks ..." gives none."""
    return {int(row[1]) for row in (line.strip().strip('"').split('","') for line in out.splitlines())
            if len(row) > 1 and row[1].isdigit()}


# --- the worker ----------------------------------------------------------------------------------------------------

class Worker:
    """hello, then (a jobs thread): claim (a long poll) -> fetch the audio -> whisper-server -> complete; a heartbeat
    thread reports the state every heartbeat_s (in every state); a monitor thread watches the GPU and whisper-server.
    run()'s own thread only waits, so Ctrl-C stops the worker at once."""

    def __init__(self, cfg: Config, run: Callable[[list], str] = run_cmd, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None, popen: Callable = subprocess.Popen,
                 fullscreen: Callable[[], str] = windows_fullscreen):
        self.cfg, self.run_cmd, self.clock = cfg, run, clock
        self._stop = threading.Event()
        self.sleep = sleep if sleep is not None else self._stop.wait     # backoff and refused waits (tests record them)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.control = Control(cfg.control_path)
        self.whisper = Whisper(cfg.whisper_url, cfg.whisper_timeout_s)
        self.proc = WhisperProcess(cfg.whisper_server, Path(cfg.state_dir) / "whisper-server.log", popen, clock,
                                   cfg.base_dir) if cfg.whisper_server else None
        self.gpu = GpuMonitor(cfg, run, clock, busy=self._busy, ours=self._our_pids, fullscreen=fullscreen)
        self.hub = Hub(cfg.hub_url, cfg.token, cfg.cf_client_id, cfg.cf_client_secret)
        self.hb_hub = Hub(cfg.hub_url, cfg.token, cfg.cf_client_id, cfg.cf_client_secret)
        self.backoff = Backoff()
        self.connected = False             # hello answered; the heartbeat runs only then
        self.link = "starting"
        self.pool_on: bool | None = None
        self.claim_wait = cfg.claim_wait_s
        self.whisper_ok = False
        self.whisper_err = "checking"
        self.whisper_fails = 0
        self._whisper_checked = -1e9
        self._inferring = 0
        self._busy_until = 0.0
        self._pids: tuple[float, set] = (-1e9, set())
        self._claiming = False
        self._call: WhisperCall | None = None
        self._gaming_since: float | None = None
        self.job: dict | None = None
        self.counts = {"done": 0, "released": 0, "lost": 0, "failed": 0}
        self.last_job_at = ""
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._threads: list[threading.Thread] = []
        self._stopped_cleanly = False

    # -- state

    def held(self) -> tuple[str, str] | None:
        """Gaming or paused by the person or the GPU (not by whisper-server trouble)."""
        ctl = self.control.read()
        if ctl.get("paused"):
            return "paused", "manual"
        if ctl.get("gaming"):
            return "gaming", "manual"
        if self.cfg.auto_gaming and self.gpu.gaming:
            return "gaming", self.gpu.why
        return None

    def state(self) -> tuple[str, str]:
        held = self.held()
        if held:
            return held
        if not self.whisper_ok:
            return "paused", f"whisper-server: {self.whisper_err}"
        return "online", ""

    def _busy(self) -> bool:
        return self._inferring > 0 or self.clock() < self._busy_until

    def _our_pids(self) -> set:
        pids = {self.cfg.whisper_pid} if self.cfg.whisper_pid else set()
        if self.proc is not None and self.proc.pid:
            pids.add(self.proc.pid)
        at, found = self._pids
        if self.clock() - at > 30:
            found = find_pids(self.cfg.whisper_process, self.run_cmd)
            self._pids = (self.clock(), found)
        return pids | found

    def stop(self) -> None:
        """From any thread: the jobs thread hands its job back and every loop ends."""
        self._stop.set()
        self._wake.set()
        call = self._call
        if call is not None:
            call.abort()
        if self._claiming:
            self.hub.interrupt()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- the hub

    def _state_body(self) -> dict:
        state, reason = self.state()
        body = {"state": state, "model": self.cfg.model, "version": VERSION}
        if reason:
            body["reason"] = reason
        if self.gpu.info:
            body["gpu"] = dict(self.gpu.info)
        return body

    def hello(self) -> None:
        body = self._state_body()
        body.pop("reason", None)
        body["worker"] = self.cfg.worker_id
        ans = self.hub.request("POST", f"{API}/hello", body)
        if ans.get("worker") and ans.get("worker") != self.cfg.worker_id:
            log.warning("the hub knows this token as worker %s", ans.get("worker"))
        wait = retry_after(ans.get("poll_s"))
        self.claim_wait = min(self.cfg.claim_wait_s, wait) if wait else self.cfg.claim_wait_s
        if "pool_on" in ans:
            self.pool_on = bool(ans["pool_on"])
        if not self.connected:
            classes = ans.get("classes") if isinstance(ans.get("classes"), list) else []
            log.info("hub: connected as %s (%s); classes %s", self.cfg.worker_id, body["state"],
                     ", ".join(map(str, classes)) or "-")
        self.connected, self.link = True, "ok"

    def heartbeat(self) -> None:
        ans = self.hb_hub.request("POST", f"{API}/state", self._state_body(), timeout=HEARTBEAT_TIMEOUT_S)
        if "pool_on" in ans:
            if self.pool_on is not None and bool(ans["pool_on"]) != self.pool_on:
                log.info("hub: the pool is %s", "on" if ans["pool_on"] else "off")
            self.pool_on = bool(ans["pool_on"])

    def _release(self, lease: str, why: str) -> bool:
        try:
            self.hub.request("POST", f"{API}/jobs/{lease}/release", {"why": why[:150]}, timeout=10)
            self.counts["released"] += 1
            return True
        except HubError as e:
            if e.lease_lost:
                log.info("release: the hub had already taken the job back (%s)", e.status)
                self.counts["lost"] += 1
            else:
                log.warning("release failed (%s): the lease runs out on the hub instead", e)
            return False

    # -- threads

    def _heartbeat_loop(self) -> None:
        last_sent, last_status, last_state, failing = -1e9, -1e9, None, False
        while not self.stopped:
            try:
                state = self.state()
                now = self.clock()
                if state != last_state:
                    if last_state is not None:
                        log.info("state: %s%s", state[0], f" ({state[1]})" if state[1] else "")
                    last_state = state
                    self._wake.set()
                    last_sent = -1e9       # tell the hub at once
                if self.connected and now - last_sent >= self.cfg.heartbeat_s:
                    last_sent = now
                    try:
                        self.heartbeat()
                        if failing:
                            log.info("heartbeat: ok again")
                        failing = False
                    except HubError as e:
                        if e.refused:
                            self.connected = False
                        if not failing:
                            log.warning("heartbeat failed: %s", e)
                        failing = True
                if now - last_status >= self.cfg.heartbeat_s:
                    last_status = now
                    self._write_status()
            except Exception:  # noqa: BLE001 - the heartbeat must outlive any bug
                log.exception("heartbeat: unexpected error (carrying on)")
                self._stop.wait(5)
            self._stop.wait(self.cfg.tick_s)

    def _monitor_loop(self) -> None:
        last_gpu, last_alive, last_proc, gpu_err = -1e9, self.clock(), -1e9, ""
        while not self.stopped:
            try:
                now = self.clock()
                if now - last_gpu >= self.cfg.gpu_poll_s:
                    last_gpu = now
                    try:
                        self.gpu.sample()
                        gpu_err = ""
                    except Exception as e:  # noqa: BLE001 - the GPU check failing mustn't stop the whisper checks
                        if repr(e) != gpu_err:
                            log.exception("monitor: unexpected error in the GPU check (carrying on)")
                        gpu_err = repr(e)
                held = self.held()
                gaming = held is not None and held[0] == "gaming"
                self._gaming_since = (self._gaming_since or now) if gaming else None
                if (self.proc is not None and self.proc.running() and self._gaming_since is not None
                        and self.cfg.gaming_stop_whisper_min > 0
                        and now - self._gaming_since >= self.cfg.gaming_stop_whisper_min * 60):
                    log.info("gaming for %.0f min: stopping whisper-server (its VRAM back to the game)",
                             self.cfg.gaming_stop_whisper_min)
                    self.proc.stop()
                    self._whisper_down("stopped while gaming")
                if self.proc is not None and not held and now - last_proc >= 5 and not self.proc.running():
                    last_proc = now
                    self.proc.ensure(self.whisper.alive())
                if not self.whisper_ok and not held:
                    if now - self._whisper_checked >= self.cfg.whisper_check_s:
                        self.check_whisper()       # a real inference: not while gaming or paused
                elif self.whisper_ok and not self._inferring and now - last_alive >= self.cfg.whisper_check_s:
                    last_alive = now
                    if not self.whisper.alive():
                        self._whisper_down("not answering")
            except Exception:  # noqa: BLE001 - the monitor must outlive any bug
                log.exception("monitor: unexpected error (carrying on)")
                self._stop.wait(5)
            self._stop.wait(self.cfg.tick_s)

    def check_whisper(self) -> bool:
        self._whisper_checked = self.clock()
        with self._lock:
            self._inferring += 1
        err = ""
        try:
            self.whisper.check()
        except WhisperError as e:
            err = str(e).removeprefix("whisper-server").lstrip(": ") or "failing"
        finally:
            with self._lock:
                self._inferring -= 1
                self._busy_until = self.clock() + self.cfg.busy_grace_s
        if err:
            if self.whisper_ok or err != self.whisper_err:
                log.warning("whisper-server: %s", err)
            self.whisper_ok, self.whisper_err = False, err
            return False
        if not self.whisper_ok:
            log.info("whisper-server: ok")
        self.whisper_ok, self.whisper_err, self.whisper_fails = True, "", 0
        self._wake.set()
        return True

    def _whisper_down(self, why: str) -> None:
        if self.whisper_ok:
            log.warning("whisper-server: %s: paused until it answers", why)
        self.whisper_ok, self.whisper_err = False, why
        self._whisper_checked = self.clock()

    def _write_status(self, state: tuple[str, str] | None = None) -> None:
        state, reason = state or self.state()
        doc = {"pid": os.getpid(), "worker": self.cfg.worker_id, "state": state, "reason": reason, "hub": self.link,
               "pool_on": self.pool_on, "model": self.cfg.model, "gpu": self.gpu.info, "jobs": dict(self.counts),
               "last_job_at": self.last_job_at, "started": self.started_at, "updated": time.time(),
               "version": VERSION}
        try:
            write_json(self.cfg.status_path, doc)
        except OSError as e:
            log.debug("status file: %s", e)

    # -- the jobs loop

    def run(self) -> None:
        log.info("sizeup STT worker %s: worker %s, model %s, hub %s", VERSION, self.cfg.worker_id, self.cfg.model,
                 urlsplit(self.cfg.hub_url).hostname)
        for target, name in ((self._heartbeat_loop, "heartbeat"), (self._monitor_loop, "monitor"),
                             (self._jobs_loop, "jobs")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        try:
            while self._threads[-1].is_alive():
                time.sleep(0.2)            # interruptible: Ctrl-C (or SIGTERM / SIGBREAK) lands here
        except KeyboardInterrupt:
            log.info("stopping (Ctrl-C)")
        finally:
            self.shutdown()

    def _jobs_loop(self) -> None:
        while not self.stopped:
            try:
                if not self.connected:
                    self.hello()
                self.step()
                self.backoff.ok()
            except HubError as e:
                self.hub.close()
                if self.stopped:
                    break
                if e.refused:
                    self.connected, self.link = False, f"refused: {e.reason or e.status}"
                    log.error("%s: trying again in %.0f s", e, self.cfg.unauthorized_retry_s)
                    self.sleep(self.cfg.unauthorized_retry_s)
                else:
                    wait = self.backoff.fail(e)
                    if self.connected or self.backoff.n == 1:
                        log.warning("%s: trying again in %.0f s", e, wait)
                    self.connected, self.link = False, f"down: {e}"
                    self.sleep(wait)
            except Exception:  # noqa: BLE001 - a bug must not end the worker: log it, back off, go on
                self.hub.close()
                self.connected, self.link = False, "error (see worker.log)"
                wait = self.backoff.fail()
                log.exception("unexpected error: trying again in %.0f s", wait)
                self.sleep(wait)

    def step(self) -> None:
        state, _ = self.state()
        if state != "online":
            self._wake.wait(self.cfg.tick_s)
            self._wake.clear()
            return
        self.hub.interrupted = False
        self._claiming = True
        try:
            if self.stopped:
                return
            ans = self.hub.request("POST", f"{API}/claim", {"wait": self.claim_wait, "state": state,
                                                            "model": self.cfg.model},
                                   timeout=self.claim_wait + 15)
        finally:
            self._claiming = False
        job = ans.get("job")
        if isinstance(job, dict):
            self.do_job(job)
        elif job is not None:
            log.warning("the hub sent a job that isn't an object: ignored")

    def do_job(self, job: dict) -> None:
        lease = str(job.get("lease") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", lease):
            log.warning("a job without a usable lease: ignored")
            return
        self.job = job
        try:
            self._do_job(job, lease)
        finally:
            self.job = None
            self._call = None
            self.last_job_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _do_job(self, job: dict, lease: str) -> None:
        jid, cls = job.get("id"), str(job.get("class") or "")
        t0 = granted = self.clock()
        lease_s = _num(job.get("lease_s"))
        lease_s = 10.0 if lease_s is None else max(lease_s, 0.0)
        expires = t0 + lease_s
        state, reason = self.state()
        if state != "online" or self.stopped:
            self._release(lease, "shutdown" if self.stopped else f"{state}: {reason}" if reason else state)
            return
        try:
            path = self.hub.path_of(str(job.get("audio") or ""))
        except HubError:
            log.warning("job %s: its audio isn't on the hub: handed back", jid)
            self._release(lease, "audio isn't on the hub")
            return
        try:
            wav = self.hub.request("GET", path, timeout=30, raw=True)
        except HubError as e:
            if e.lease_lost:               # 409 lease lost, 410 the hub had no audio (it went to another engine)
                log.info("job %s: no audio for it (%s): the hub took it back", jid, e.status)
                self.counts["lost"] += 1
                return
            if not e.status:
                raise                      # the hub went away: reconnect (the lease runs out there)
            self._release(lease, f"audio: {e.status}")
            return
        fetch_ms = (self.clock() - t0) * 1000
        fast = job.get("fast") is True     # an older hub sends none: the full decode
        call = self.whisper.call(wav, str(job.get("prompt") or ""), fast=fast)
        self._call = call
        box: dict = {}

        def infer() -> None:
            try:
                box["out"] = call.run()
            except WhisperError as e:
                box["err"] = e
            finally:
                with self._lock:
                    self._inferring -= 1
                    self._busy_until = self.clock() + self.cfg.busy_grace_s

        with self._lock:
            self._inferring += 1
        t1 = self.clock()
        th = threading.Thread(target=infer, name=f"infer-{lease}", daemon=True)
        th.start()
        extending, at_cap = True, False
        while th.is_alive():
            th.join(min(0.05, self.cfg.tick_s))
            if not th.is_alive():
                break
            now = self.clock()
            if self.stopped:
                call.abort()
                self._release(lease, "shutdown")
                return
            state, reason = self.state()
            why = f"{state}: {reason}" if reason else state
            if state != "online" and extending:
                extending = False
                # a pre-alert (fast) job: the hub takes it back on the gaming heartbeat anyway
                if expires - now > self.cfg.finish_s or cls in LONG_CLASSES or job.get("fast") is True:
                    call.abort()           # whisper-server stops: the GPU is the game's now
                    self._release(lease, why)
                    log.info("job %s: %s: handed back", jid, state)
                    return
                log.info("job %s: %s mid-job: finishing within its lease", jid, state)
            if not extending or at_cap:
                if now >= expires - self.cfg.lease_margin_s:
                    call.abort()
                    if extending:          # at the cap: the hub has taken it back already
                        log.info("job %s: its lease reached the cap (%s): dropped", jid, cls or "?")
                        self.counts["lost"] += 1
                    else:
                        self._release(lease, why)
                        log.info("job %s: %s: handed back", jid, state)
                    return
            elif now - granted >= lease_s / 2:
                try:
                    ans = self.hub.request("POST", f"{API}/jobs/{lease}/extend", {}, timeout=10)
                except HubError as e:
                    if e.lease_lost:
                        call.abort()
                        log.info("job %s: lease lost (%s): dropped", jid, e.status)
                        self.counts["lost"] += 1
                        return
                    log.warning("job %s: extend failed: %s", jid, e)
                    granted = now          # try again in lease_s/2; the lease may run out
                    continue
                new = _num(ans.get("lease_s"))
                new = lease_s if new is None else max(new, 0.0)
                if new <= expires - now + 0.05:
                    at_cap = True          # it stopped growing: the class's cap
                granted, expires = now, now + new
                lease_s = new if new > 0 else lease_s
        infer_ms = (self.clock() - t1) * 1000
        if call.aborted:                   # stop() aborted it: not whisper-server's failure
            self._release(lease, "shutdown")
            return
        if "err" in box:
            err = box["err"]
            self.whisper_fails += 1
            self.counts["failed"] += 1
            log.warning("job %s: %s (%d in a row): handed back", jid, err, self.whisper_fails)
            self._release(lease, str(err)[:120])
            if self.whisper_fails >= self.cfg.whisper_fail_max:
                self._whisper_down(f"{self.whisper_fails} failures in a row")
            return
        self.whisper_fails = 0
        body = result_body(box.get("out") or {}, self.cfg.model, round(fetch_ms), round(infer_ms))
        self._complete(job, lease, body, expires, fetch_ms, infer_ms)

    def _complete(self, job: dict, lease: str, body: dict, expires: float, fetch_ms: float, infer_ms: float) -> None:
        """complete; a 5xx or a network error is tried again (twice at most) while the lease lasts; a 4xx other than a
        lost lease hands the job back."""
        jid = job.get("id")
        last: HubError | None = None
        for attempt in range(3):
            try:
                self.hub.request("POST", f"{API}/jobs/{lease}/complete", body, timeout=15)
                self.counts["done"] += 1
                log.info("job %s %s %.1f s: fetch %d ms, whisper %d ms", jid, job.get("class", ""),
                         float(_num(job.get("seconds")) or 0), fetch_ms, infer_ms)
                return
            except HubError as e:
                last = e
                if e.lease_lost:
                    log.info("job %s: the hub refused the result (%s: lease expired): dropped", jid, e.status)
                    self.counts["lost"] += 1
                    return
                if e.refused:
                    raise
                if 400 <= e.status < 500:
                    log.warning("job %s: the hub refused the result (%s): handed back", jid, e)
                    self._release(lease, f"complete refused: {e.status}")
                    return
                wait = e.retry_after if e.retry_after is not None else 0.5 * (attempt + 1)
                wait = min(wait, expires - self.clock() - 0.2)
                if attempt == 2 or wait < 0 or self.stopped:
                    break
                log.info("job %s: complete failed (%s): trying again", jid, e)
                self._stop.wait(wait)
        log.warning("job %s: complete failed (%s): dropped (the lease runs out on the hub)", jid, last)
        self.counts["lost"] += 1
        if last is not None and not last.status:
            raise last                     # the hub went away: reconnect

    def shutdown(self) -> None:
        self.stop()
        for t in self._threads:
            t.join(5)
        job = self.job
        if job and job.get("lease"):
            self._release(str(job["lease"]), "shutdown")
        if self.connected:                 # the last word: no new jobs for this worker
            try:
                body = self._state_body()
                body.update(state="paused", reason="shutdown")
                self.hb_hub.request("POST", f"{API}/state", body, timeout=3)
            except HubError:
                pass
        if self.proc is not None:
            self.proc.stop()
        self._write_status(("stopped", ""))
        self.hub.close()
        self.hb_hub.close()
        log.info("stopped")


# --- the CLI -------------------------------------------------------------------------------------------------------

def _status_text(cfg: Config, now: float | None = None) -> str:
    ctl, st = read_json(cfg.control_path), read_json(cfg.status_path)
    now = time.time() if now is None else now
    lines = [f"manual: gaming {'on' if ctl.get('gaming') else 'off'}, {'paused' if ctl.get('paused') else 'not paused'}"]
    updated = st.get("updated")
    if not isinstance(updated, (int, float)):
        lines.append("worker: never ran here")
    else:
        age = now - updated
        if age > 30 or st.get("state") == "stopped":
            lines.append(f"worker: not running (last seen {time.strftime('%Y-%m-%d %H:%M', time.localtime(updated))})")
        else:
            lines.append(f"worker: {st.get('worker')} pid {st.get('pid')}, {st.get('state')}"
                         + (f" ({st.get('reason')})" if st.get("reason") else ""))
        lines.append(f"hub: {st.get('hub')}" + ("" if st.get("pool_on") is None else
                                                f", pool {'on' if st.get('pool_on') else 'off'}"))
        jobs = st.get("jobs") or {}
        lines.append("jobs: " + ", ".join(f"{jobs.get(k, 0)} {k}" for k in ("done", "released", "lost", "failed"))
                     + (f", last {st.get('last_job_at')}" if st.get("last_job_at") else ""))
        gpu = st.get("gpu")
        if gpu:
            lines.append(f"gpu: {gpu.get('util_pct')}%, {gpu.get('mem_used_mb')}/{gpu.get('mem_total_mb')} MB, "
                         f"{gpu.get('others')} other process(es)")
    return "\n".join(lines)


def _check(cfg: Config) -> int:
    ok = True
    w = Whisper(cfg.whisper_url, cfg.whisper_timeout_s)
    t = time.monotonic()
    try:
        w.check()
        print(f"whisper-server: ok ({time.monotonic() - t:.1f} s for 1 s of silence)")
    except WhisperError as e:
        ok = False
        print(f"whisper-server: {e}")
    hub = Hub(cfg.hub_url, cfg.token, cfg.cf_client_id, cfg.cf_client_secret)
    try:
        ans = hub.request("POST", f"{API}/hello", {"worker": cfg.worker_id, "version": VERSION, "model": cfg.model,
                                                   "state": "paused"})
        print(f"hub: ok (worker {ans.get('worker') or cfg.worker_id})")
    except HubError as e:
        ok = False
        print(f"hub: {e}")
    finally:
        hub.close()
    mon = GpuMonitor(cfg)
    mon.sample()
    if mon.available:
        print(f"gpu: {mon.info}")
    else:
        print("gpu: no nvidia-smi (gaming mode is manual only)")
    return 0 if ok else 1


def main(argv: list | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="worker.toml / worker.json (default: SIZEUP_STT_WORKER_CONFIG, "
                                               "else worker.toml or worker.json next to this file)")
    ap = argparse.ArgumentParser(prog="worker.py", description="sizeup GPU STT worker", parents=[common])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", parents=[common], help="run the worker")
    sub.add_parser("check", parents=[common], help="check config, whisper-server and the hub once")
    sub.add_parser("status", parents=[common], help="what the worker is doing")
    g = sub.add_parser("gaming", parents=[common], help="manual gaming mode")
    g.add_argument("onoff", choices=("on", "off"))
    sub.add_parser("pause", parents=[common], help="take no new jobs")
    sub.add_parser("resume", parents=[common], help="back to online (clears gaming and pause)")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config or default_config_path())
        if args.cmd in ("run", "check"):
            validate(cfg)
    except ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    ctl = Control(cfg.control_path)
    if args.cmd == "gaming":
        ctl.set(gaming=args.onoff == "on")
    elif args.cmd == "pause":
        ctl.set(paused=True)
    elif args.cmd == "resume":
        ctl.set(paused=False, gaming=False)
    elif args.cmd == "check":
        return _check(cfg)
    elif args.cmd == "run":
        setup_logging(cfg)
        lock = InstanceLock(cfg.state_dir)
        if not lock.acquire():
            log.error("another worker (pid %s) is running on %s: not starting a second one", lock.holder(),
                      cfg.state_dir)
            return 3
        worker = Worker(cfg)

        def _term(signum, frame):
            raise KeyboardInterrupt

        for name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), _term)
        try:
            worker.run()
        except Exception:  # noqa: BLE001 - logged for the person reading worker.log; the task starts it again
            log.critical("the worker stopped on an unexpected error", exc_info=True)
            return 1
        return 0
    print(_status_text(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
