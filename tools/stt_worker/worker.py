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
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

VERSION = "1"
USER_AGENT = f"sizeup-stt-worker/{VERSION}"
API = "/api/station/stt"
TOKEN = re.compile(r"sw1\.([a-z0-9][a-z0-9_-]{0,39})\.([A-Za-z0-9_-]+)")
BACKOFF = (1, 2, 5, 10, 30)                # hub unreachable / 5xx: waits in a row, then the last one
MAX_CLAIM_WAIT_S = 25
MAX_HEARTBEAT_S = 10
ENV = {"hub_url": "SIZEUP_STT_HUB_URL", "token": "SIZEUP_STT_WORKER_TOKEN",
       "cf_client_id": "SIZEUP_STT_CF_CLIENT_ID", "cf_client_secret": "SIZEUP_STT_CF_CLIENT_SECRET"}

log = logging.getLogger("sizeup.stt_worker")


# --- config ------------------------------------------------------------------------------------------------------

def default_state_dir() -> str:
    if os.name == "nt":
        return str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "sizeup-stt-worker")
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
    state_dir: str = ""                    # control.json (gaming / pause), status.json, worker.log
    auto_gaming: bool = True
    nvidia_smi: str = "nvidia-smi"
    gpu_poll_s: float = 5.0
    gaming_mem_mb: float = 1024.0          # another process using more GPU memory than this ...
    gaming_util_pct: float = 40.0          # ... or the GPU this busy while we are idle ...
    gaming_samples: int = 2                # ... this many samples in a row: gaming
    gaming_clear_s: float = 60.0           # gaming ends after this long below both
    heartbeat_s: float = 5.0               # <= 10
    claim_wait_s: float = 25.0             # <= 25
    whisper_timeout_s: float = 60.0
    whisper_fail_max: int = 3              # whisper-server failures in a row: paused until it answers again
    whisper_check_s: float = 10.0
    unauthorized_retry_s: float = 300.0    # the hub or Cloudflare refused us: try again this often
    lease_margin_s: float = 0.5
    busy_grace_s: float = 3.0              # GPU load this soon after our own inference is still ours
    tick_s: float = 0.5
    log_file: str = ""                     # default <state_dir>/worker.log; "-" = stderr only

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
    """worker.toml (Python 3.11+) or worker.json, flat or under [worker]; then the environment (SIZEUP_STT_*) on top."""
    env = os.environ if env is None else env
    doc: dict = {}
    if path:
        p = Path(path)
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
    fields = {f.name: f for f in dataclasses.fields(Config)}
    unknown = sorted(set(doc) - set(fields))
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")
    cfg = Config()
    for key, value in doc.items():
        setattr(cfg, key, _coerce(key, value, getattr(cfg, key)))
    for key, var in ENV.items():
        if env.get(var):
            setattr(cfg, key, env[var].strip())
    if not cfg.token and cfg.token_file:
        try:
            cfg.token = Path(cfg.token_file).expanduser().read_text(encoding="utf-8-sig").strip()
        except OSError as e:
            raise ConfigError(f"can't read token_file: {e.strerror or e}") from None
    cfg.state_dir = cfg.state_dir or default_state_dir()
    cfg.heartbeat_s = min(max(cfg.heartbeat_s, 0.01), MAX_HEARTBEAT_S)
    cfg.claim_wait_s = min(max(cfg.claim_wait_s, 0.0), MAX_CLAIM_WAIT_S)
    return cfg


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
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("hub_url: https only (the token must not cross the internet in the clear)")
    if not cfg.token:
        raise ConfigError("no token: set SIZEUP_STT_WORKER_TOKEN (an admin issues it in Settings > Station > STT workers)")
    if not TOKEN.fullmatch(cfg.token):
        raise ConfigError("the token isn't a worker token (sw1.<worker>.<secret>): check SIZEUP_STT_WORKER_TOKEN")
    if bool(cfg.cf_client_id) != bool(cfg.cf_client_secret):
        raise ConfigError("Cloudflare Access: set both SIZEUP_STT_CF_CLIENT_ID and SIZEUP_STT_CF_CLIENT_SECRET")
    w = urlsplit(cfg.whisper_url)
    if w.scheme != "http" or not w.hostname:
        raise ConfigError("whisper_url: e.g. http://127.0.0.1:8179/inference")


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
    log.setLevel(logging.INFO)
    for f in list(log.filters):
        if isinstance(f, Redact):
            log.removeFilter(f)
    log.addFilter(Redact(cfg.secrets()))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:             # pythonw.exe (Task Scheduler) has no console
        handlers.append(logging.StreamHandler())
    if cfg.log_file != "-":
        path = Path(cfg.log_file or Path(cfg.state_dir) / "worker.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)


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

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

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
        """JSON in, JSON out (raw: the body's bytes). HubError on anything but a 2xx."""
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = dict(self._headers, **({"Content-Type": "application/json"} if data is not None else {}))
        full = path if path.startswith(self.prefix + "/") and self.prefix else self.prefix + path
        for attempt in (0, 1):             # a kept-alive connection the far end closed: one quiet retry
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
                return json.loads(payload or b"{}")
            except ValueError:
                self.close()
                raise HubError(f"hub sent something that isn't JSON ({r.getheader('Content-Type')}): "
                               "is Cloudflare Access letting the worker through?", r.status) from None
        raise HubError("unreachable")


class Backoff:
    """1, 2, 5, 10, 30, 30 ... s after failures in a row; the hub's Retry-After when it gives one."""

    def __init__(self, steps: tuple = BACKOFF):
        self.steps, self.n = steps, 0

    def fail(self, err: HubError | None = None) -> float:
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


class Whisper:
    """whisper.cpp's whisper-server, called exactly as the station's WhisperClient does (sizeup/stt.py): a multipart
    POST to /inference with response_format=verbose_json, temperature=0.0, the prompt and the WAV as file=tx.wav.
    The hub's WAV is already padded and processed: it goes as it is."""

    def __init__(self, url: str, timeout_s: float = 60.0):
        self.url, self.timeout_s = url, timeout_s
        u = urlsplit(url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open   # never via a proxy

    def transcribe(self, wav: bytes, prompt: str = "", timeout: float | None = None) -> dict:
        boundary = uuid.uuid4().hex
        fields = [("response_format", "verbose_json"), ("temperature", "0.0")]
        if prompt:
            fields.append(("prompt", prompt))
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                 for name, value in fields]
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="tx.wav"\r\n'
                      "Content-Type: audio/wav\r\n\r\n").encode() + wav + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.url, data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with self._open(req, timeout=timeout or self.timeout_s) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise WhisperError(f"whisper-server said {e.code}") from None
        except (OSError, http.client.HTTPException) as e:
            raise WhisperError(f"whisper-server unreachable ({type(e).__name__})") from None
        except ValueError:
            raise WhisperError("whisper-server sent something that isn't JSON") from None
        if not isinstance(out, dict) or ("error" in out and "text" not in out):
            raise WhisperError(f"whisper-server: {str(out.get('error') if isinstance(out, dict) else out)[:80]}")
        return out

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
    no console window, and its pid known for the GPU check. When something already answers on the port, it is used
    and nothing is started."""

    def __init__(self, argv: list, log_path: Path, popen: Callable = subprocess.Popen, clock: Callable = time.monotonic):
        self.argv, self.log_path, self.popen, self.clock = argv, log_path, popen, clock
        self.proc = None
        self.started = -1e9

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else 0

    def running(self) -> bool:
        return self.pid != 0

    def ensure(self, port_alive: bool) -> None:
        if self.running() or port_alive or self.clock() - self.started < 10:
            return
        if self.proc is not None:
            log.warning("whisper-server exited (%s): starting it again", self.proc.poll())
        self.started = self.clock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.log_path, "ab") as out:
                self.proc = self.popen(self.argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.info("started whisper-server (pid %s)", self.proc.pid)
        except OSError as e:
            self.proc = None
            log.error("can't start whisper-server: %s", e)

    def stop(self) -> None:
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --- the GPU: gaming detection -------------------------------------------------------------------------------------

def run_cmd(argv: list) -> str:
    """Runs a command, its stdout. FileNotFoundError when it isn't installed, CalledProcessError when it fails."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout


class GpuMonitor:
    """nvidia-smi every gpu_poll_s: another process (not our whisper-server) using more than gaming_mem_mb of GPU
    memory, or the GPU over gaming_util_pct while we are not inferring, for gaming_samples samples in a row: gaming.
    It ends after gaming_clear_s below both. No nvidia-smi (a Mac): never gaming here (manual only)."""

    GPU_Q = "--query-gpu=utilization.gpu,memory.used,memory.total"
    APPS_Q = "--query-compute-apps=pid,used_memory"

    def __init__(self, cfg: Config, run: Callable[[list], str] = run_cmd, clock: Callable[[], float] = time.monotonic,
                 busy: Callable[[], bool] = lambda: False, ours: Callable[[], set] = set):
        self.cfg, self.run, self.clock, self.busy, self.ours = cfg, run, clock, busy, ours
        self.available: bool | None = None
        self.gaming = False
        self.why = ""
        self.info: dict | None = None
        self._hot = 0
        self._cool_since: float | None = None

    def sample(self) -> None:
        smi = self.cfg.nvidia_smi
        try:
            gpus = self.run([smi, self.GPU_Q, "--format=csv,noheader,nounits"])
            apps = self.run([smi, self.APPS_Q, "--format=csv,noheader,nounits"])
        except FileNotFoundError:
            if self.available is not False:
                log.info("no nvidia-smi: gaming mode is manual only (worker.py gaming on|off)")
            self.available = False
            return
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("nvidia-smi failed: %s", e)
            return
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
            others.append(_num(cols[1]))     # None: "[N/A]" (Windows WDDM doesn't report it): the util check covers it
        self.info = {"util_pct": round(util), "mem_used_mb": round(used), "mem_total_mb": round(total),
                     "others": len(others)}
        biggest = max((m for m in others if m is not None), default=0.0)
        hot = ""
        if biggest > self.cfg.gaming_mem_mb:
            hot = f"another GPU process uses {round(biggest)} MB"
        elif util > self.cfg.gaming_util_pct and not self.busy():
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
                    log.info("gaming over: GPU quiet for %.0f s", self.cfg.gaming_clear_s)


def find_pids(name: str, run: Callable[[list], str] = run_cmd) -> set:
    """Best effort: the pids of processes called `name` (tasklist on Windows, pgrep elsewhere)."""
    if not name:
        return set()
    try:
        if os.name == "nt":
            exe = name if name.lower().endswith(".exe") else name + ".exe"
            out = run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"])
            return {int(row[1]) for row in (line.strip().strip('"').split('","') for line in out.splitlines())
                    if len(row) > 1 and row[1].isdigit()}
        return {int(x) for x in run(["pgrep", "-f", name]).split() if x.isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()


# --- the worker ----------------------------------------------------------------------------------------------------

class Worker:
    """hello, then: claim (a long poll) -> fetch the audio -> whisper-server -> complete, while a heartbeat thread
    reports the state every heartbeat_s (in every state) and a monitor thread watches the GPU and whisper-server."""

    def __init__(self, cfg: Config, run: Callable[[list], str] = run_cmd, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None, popen: Callable = subprocess.Popen):
        self.cfg, self.run_cmd, self.clock = cfg, run, clock
        self._stop = threading.Event()
        self.sleep = sleep if sleep is not None else self._stop.wait     # backoff and refused waits (tests record them)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.control = Control(cfg.control_path)
        self.whisper = Whisper(cfg.whisper_url, cfg.whisper_timeout_s)
        self.proc = WhisperProcess(cfg.whisper_server, Path(cfg.state_dir) / "whisper-server.log", popen, clock) \
            if cfg.whisper_server else None
        self.gpu = GpuMonitor(cfg, run, clock, busy=self._busy, ours=self._our_pids)
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
        self.job: dict | None = None
        self.counts = {"done": 0, "released": 0, "lost": 0, "failed": 0}
        self.last_job_at = ""
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._threads: list[threading.Thread] = []

    # -- state

    def state(self) -> tuple[str, str]:
        ctl = self.control.read()
        if ctl.get("paused"):
            return "paused", "manual"
        if ctl.get("gaming"):
            return "gaming", "manual"
        if self.cfg.auto_gaming and self.gpu.gaming:
            return "gaming", self.gpu.why
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
        self._stop.set()
        self._wake.set()

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
        if not self.connected:
            log.info("hub: connected as %s (%s); classes %s", self.cfg.worker_id, body["state"],
                     ", ".join(map(str, ans.get("classes") or [])) or "-")
        self.connected, self.link = True, "ok"

    def heartbeat(self) -> None:
        ans = self.hb_hub.request("POST", f"{API}/state", self._state_body())
        if "pool_on" in ans:
            if self.pool_on is not None and bool(ans["pool_on"]) != self.pool_on:
                log.info("hub: the pool is %s", "on" if ans["pool_on"] else "off")
            self.pool_on = bool(ans["pool_on"])

    def _release(self, lease: str, why: str) -> None:
        try:
            self.hub.request("POST", f"{API}/jobs/{lease}/release", {"why": why}, timeout=10)
        except HubError as e:
            log.warning("release failed (%s): the lease runs out on the hub instead", e)

    # -- threads

    def _heartbeat_loop(self) -> None:
        last_sent, last_status, last_state, failing = -1e9, -1e9, None, False
        while not self.stopped:
            state = self.state()
            now = self.clock()
            if state != last_state:
                if last_state is not None:
                    log.info("state: %s%s", state[0], f" ({state[1]})" if state[1] else "")
                last_state = state
                self._wake.set()
                last_sent = -1e9           # tell the hub at once
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
            self._stop.wait(self.cfg.tick_s)

    def _monitor_loop(self) -> None:
        last_gpu, last_alive, last_proc = -1e9, self.clock(), -1e9
        while not self.stopped:
            now = self.clock()
            if self.gpu.available is not False and now - last_gpu >= self.cfg.gpu_poll_s:
                last_gpu = now
                self.gpu.sample()
            if self.proc is not None and now - last_proc >= 5 and not self.proc.running():
                last_proc = now
                self.proc.ensure(self.whisper.alive())
            if not self.whisper_ok:
                if now - self._whisper_checked >= self.cfg.whisper_check_s:
                    self.check_whisper()
            elif not self._inferring and now - last_alive >= self.cfg.whisper_check_s:
                last_alive = now
                if not self.whisper.alive():
                    self._whisper_down("not answering")
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

    def _write_status(self) -> None:
        state, reason = self.state()
        doc = {"pid": os.getpid(), "worker": self.cfg.worker_id, "state": state, "reason": reason, "hub": self.link,
               "pool_on": self.pool_on, "model": self.cfg.model, "gpu": self.gpu.info, "jobs": dict(self.counts),
               "last_job_at": self.last_job_at, "started": self.started_at, "updated": time.time(),
               "version": VERSION}
        try:
            write_json(self.cfg.status_path, doc)
        except OSError as e:
            log.debug("status file: %s", e)

    # -- the main loop

    def run(self) -> None:
        log.info("sizeup STT worker %s: worker %s, model %s, hub %s", VERSION, self.cfg.worker_id, self.cfg.model,
                 urlsplit(self.cfg.hub_url).hostname)
        for target, name in ((self._heartbeat_loop, "heartbeat"), (self._monitor_loop, "monitor")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        try:
            while not self.stopped:
                try:
                    if not self.connected:
                        self.hello()
                    self.step()
                    self.backoff.ok()
                except HubError as e:
                    self.hub.close()
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
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def step(self) -> None:
        state, _ = self.state()
        if state != "online":
            self._wake.wait(self.cfg.tick_s)
            self._wake.clear()
            return
        ans = self.hub.request("POST", f"{API}/claim", {"wait": self.claim_wait, "state": state,
                                                        "model": self.cfg.model},
                               timeout=self.claim_wait + 15)
        job = ans.get("job")
        if job:
            self.do_job(job)

    def do_job(self, job: dict) -> None:
        lease = str(job.get("lease") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", lease):
            log.warning("a job without a usable lease: ignored")
            return
        self.job = job
        try:
            self._do_job(job, lease)
        except KeyboardInterrupt:
            self._release(lease, "shutdown")
            raise
        finally:
            self.job = None
            self.last_job_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _do_job(self, job: dict, lease: str) -> None:
        jid, cls = job.get("id"), job.get("class", "")
        t0 = granted = self.clock()
        lease_s = float(job.get("lease_s") or 10)
        expires = t0 + lease_s
        state, reason = self.state()
        if state != "online" or self.stopped:
            self._release(lease, "shutdown" if self.stopped else f"{state}: {reason}" if reason else state)
            self.counts["released"] += 1
            return
        try:
            wav = self.hub.request("GET", self.hub.path_of(str(job.get("audio") or "")), timeout=30, raw=True)
        except HubError as e:
            if e.lease_lost:
                log.info("job %s: lease lost before the audio came (%s)", jid, e.status)
                self.counts["lost"] += 1
                return
            if not e.status:
                raise                      # the hub went away: reconnect (the lease runs out there)
            self._release(lease, f"audio: {e.status}")
            self.counts["released"] += 1
            return
        fetch_ms = (self.clock() - t0) * 1000
        box: dict = {}

        def infer() -> None:
            try:
                box["out"] = self.whisper.transcribe(wav, str(job.get("prompt") or ""))
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
        extending = True
        while th.is_alive():
            th.join(min(0.05, self.cfg.tick_s))
            if not th.is_alive():
                break
            now = self.clock()
            if self.stopped:
                self._release(lease, "shutdown")
                self.counts["released"] += 1
                return
            state, reason = self.state()
            if state != "online" and extending:
                extending = False          # finish within the lease we have, or hand it back
                log.info("job %s: %s mid-job: finishing only within its lease", jid, state)
            if not extending:
                if now >= expires - self.cfg.lease_margin_s:
                    self._release(lease, f"{state}: {reason}" if reason else state)
                    self.counts["released"] += 1
                    log.info("job %s: released (%s)", jid, state)
                    return
            elif now - granted >= lease_s / 2:
                try:
                    ans = self.hub.request("POST", f"{API}/jobs/{lease}/extend", {}, timeout=10)
                    granted, lease_s = now, float(ans.get("lease_s") or lease_s)
                    expires = now + lease_s
                except HubError as e:
                    if e.lease_lost:
                        log.info("job %s: lease lost (%s cap or expiry): result dropped", jid, cls or "?")
                        self.counts["lost"] += 1
                        return
                    log.warning("job %s: extend failed: %s", jid, e)
                    granted = now          # try again in lease_s/2; the lease may run out
        infer_ms = (self.clock() - t1) * 1000
        if "err" in box:
            err = box["err"]
            self.whisper_fails += 1
            self.counts["failed"] += 1
            log.warning("job %s: %s (%d in a row): released", jid, err, self.whisper_fails)
            self._release(lease, str(err)[:120])
            self.counts["released"] += 1
            if self.whisper_fails >= self.cfg.whisper_fail_max:
                self._whisper_down(f"{self.whisper_fails} failures in a row")
            return
        self.whisper_fails = 0
        body = result_body(box.get("out") or {}, self.cfg.model, round(fetch_ms), round(infer_ms))
        try:
            self.hub.request("POST", f"{API}/jobs/{lease}/complete", body, timeout=15)
        except HubError as e:
            if e.lease_lost:
                log.info("job %s: the hub refused the result (%s: lease expired): dropped", jid, e.status)
                self.counts["lost"] += 1
                return
            raise
        self.counts["done"] += 1
        log.info("job %s %s %.1f s: fetch %d ms, whisper %d ms", jid, cls, float(job.get("seconds") or 0),
                 fetch_ms, infer_ms)

    def shutdown(self) -> None:
        self._stop.set()
        job = self.job
        if job and job.get("lease"):
            self._release(str(job["lease"]), "shutdown")
        for t in self._threads:
            t.join(2)
        if self.connected:               # the last word: no new jobs for this worker
            try:
                body = self._state_body()
                body.update(state="paused", reason="shutdown")
                self.hb_hub.request("POST", f"{API}/state", body, timeout=3)
            except HubError:
                pass
        if self.proc is not None:
            self.proc.stop()
        self._write_status()
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
        if age > 30:
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
        worker = Worker(cfg)

        def _term(signum, frame):
            raise KeyboardInterrupt

        for name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), _term)
        worker.run()
        return 0
    print(_status_text(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
