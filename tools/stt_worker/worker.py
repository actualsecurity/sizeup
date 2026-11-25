#!/usr/bin/env python3
"""sizeup's GPU speech-to-text worker (#639): it takes clips from the hub's STT pool, runs them through a local
whisper.cpp whisper-server and sends the text back.

Standard library only, Python 3.10+, so it runs from a plain python.org install on Windows (or a Linux distribution's
python3). It only ever connects out: to the hub (HTTPS, through Cloudflare Access), to whisper-server on 127.0.0.1,
and for its self-update to GitHub (api.github.com, raw.githubusercontent.com: main of actualsecurity/sizeup only).
Nothing listens.

    python worker.py run    [-c worker.toml]     the worker (Task Scheduler runs this at logon)
    python worker.py check  [-c worker.toml]     config, hub and whisper-server, once
    python worker.py status                      what the running worker is doing
    python worker.py gaming on|off               manual gaming mode (no new jobs; the heartbeat goes on)
    python worker.py pause | resume              paused (no new jobs) / back to online (clears gaming and pause)
    python worker.py update [retry|rollback]     the self-update: its state / try a hash marked bad again /
                                                 worker.py.prev back by hand
    python worker.py selftest [--result FILE]    imports, reads the config, builds a Worker: no network (the
                                                 self-update runs it on a new file before switching to it)

The token comes from SIZEUP_STT_WORKER_TOKEN (preferred) and is never logged or printed. Setup:
tools/stt_worker/README.md."""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import hashlib
import http.client
import io
import json
import math
import logging
import logging.handlers
import os
import re
import secrets
import shutil
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
    # the local LLM (the hub's infer lane): llama.cpp's llama-server on this PC; llm_url "" = none
    llm_url: str = ""                      # e.g. http://127.0.0.1:8180 (loopback only)
    llm_server: list = dataclasses.field(default_factory=list)  # argv: the worker starts and restarts it
    llm_process: str = "llama-server"      # its process name, so its GPU use isn't taken for a game
    llm_model: str = ""                    # the label the hub shows, e.g. qwen2.5-7b-instruct-q4_k_m
    llm_model_sha256: str = ""             # the GGUF's SHA-256: checked before llama-server first starts (pinned)
    llm_stop_gaming_s: float = -1.0        # gaming this long: llama-server stopped (its VRAM back to the game); below 0
                                           # (default): 60 s for a model up to 6 GB, 20 s for a bigger one
    llm_timeout_s: float = 30.0
    llm_check_s: float = 10.0
    llm_fail_max: int = 3                  # failed generations in a row: no LLM until its health check passes again
    llm_no_think: bool = True              # chat_template_kwargs enable_thinking false (Qwen3; others ignore it)
    auto_update: bool = True               # self-update to the version the hub names (a commit on main + its hash)
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
    if _bad_host(cfg.whisper_server):
        # whisper-server answers any web page (CORS *): on the network, any site could use the GPU
        raise ConfigError("whisper_server: --host must be 127.0.0.1 (whisper-server must not listen on the network)")
    if cfg.llm_url or cfg.llm_server:
        u = urlsplit(cfg.llm_url)
        if u.scheme != "http" or u.hostname not in LOOPBACK or not u.port:
            raise ConfigError("llm_url: llama-server on this PC only, e.g. http://127.0.0.1:8180")
        if not cfg.llm_model:
            raise ConfigError("llm_model: the model's label for the hub, e.g. qwen2.5-7b-instruct-q4_k_m")
        if cfg.llm_model_sha256 and not re.fullmatch(r"[0-9A-Fa-f]{64}", cfg.llm_model_sha256):
            raise ConfigError("llm_model_sha256: the GGUF's SHA-256, 64 hex digits")
        if cfg.llm_server:
            check_llm_argv(cfg.llm_server)
            if not cfg.llm_model_sha256:
                raise ConfigError("llm_model_sha256: required with llm_server (the GGUF's pinned SHA-256, README step 4b)")
            port = _arg(cfg.llm_server, ("--port",))
            if str(port or "8080") != str(u.port):
                raise ConfigError(f"llm_server: --port {port or '8080 (its default)'} isn't llm_url's port {u.port}")


# llama-server's options the worker lets through (name: takes a value), and nothing else: no verbose logs or a prompts
# directory (prompts), no slots endpoint, slot saving, tools, agents or MCP, no CORS origins, no API key on a command
# line (the worker sets its own), no model but one local, pinned GGUF (no -hf / URL, no draft model, LoRA or mmproj)
LLM_ARGS = {"-m": True, "--model": True, "--host": True, "--port": True, "-c": True, "--ctx-size": True,
            "-ngl": True, "--gpu-layers": True, "--n-gpu-layers": True, "-np": True, "--parallel": True,
            "--jinja": False, "--no-webui": False, "--no-ui": False, "--no-slots": False, "--reasoning": True,
            "-rea": True, "-t": True, "--threads": True, "-fa": True, "--flash-attn": True, "-b": True,
            "--batch-size": True, "-ub": True, "--ubatch-size": True, "--no-mmap": False, "-ctk": True,
            "--cache-type-k": True, "-ctv": True, "--cache-type-v": True}


def check_llm_argv(argv: list) -> None:
    """llm_server: a program, then only LLM_ARGS (as "-x v" or "--x=v"), exactly one -m, --host loopback only.
    ConfigError otherwise."""
    argv = [str(a) for a in argv]
    if not argv:
        raise ConfigError("llm_server: llama-server's command line")
    models, i = 0, 1
    while i < len(argv):
        name, eq, _value = argv[i].partition("=")
        if name not in LLM_ARGS:
            raise ConfigError(f"llm_server: {argv[i][:40]} not allowed (allowed: {', '.join(sorted(LLM_ARGS))})")
        if LLM_ARGS[name] and not eq:
            if i + 1 >= len(argv):
                raise ConfigError(f"llm_server: {name} needs a value")
            i += 1
        elif eq and not LLM_ARGS[name]:
            raise ConfigError(f"llm_server: {name} takes no value")
        models += name in ("-m", "--model")
        i += 1
    if models != 1:
        raise ConfigError("llm_server: exactly one -m <the GGUF file>")
    if _bad_host(argv):
        raise ConfigError("llm_server: --host must be 127.0.0.1 (llama-server must not listen on the network)")


def llm_command(argv: list) -> list:
    """llm_server as the worker runs it: --host 127.0.0.1 when it names none, and always --no-slots and --no-webui."""
    argv = [str(a) for a in argv]
    names = {a.partition("=")[0] for a in argv[1:]}
    if "--host" not in names:
        argv += ["--host", "127.0.0.1"]
    for flag in ("--no-slots", "--no-webui"):
        if flag not in names:
            argv.append(flag)
    return argv


def _arg(argv: list, names: tuple) -> str | None:
    """An option's value in an argv ("--port 8180" or "--port=8180"), None when it isn't there."""
    argv = [str(a) for a in argv]
    for i, a in enumerate(argv):
        name, eq, value = a.partition("=")
        if name in names:
            return value if eq else (argv[i + 1] if i + 1 < len(argv) else "")
    return None


def _bad_host(argv: list) -> bool:
    """--host (or --host=...) with anything but loopback: a server that would listen on the network."""
    argv = [str(a) for a in argv]
    for i, a in enumerate(argv):
        name, eq, value = a.partition("=")
        if name == "--host":
            value = value if eq else (argv[i + 1] if i + 1 < len(argv) else "")
            if any(h.strip() not in LOOPBACK for h in value.split(",")):
                return True
    return False


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


def write_secret(path: Path, text: str) -> None:
    """A file only this user may read (0600; on Windows the user's own %LOCALAPPDATA% folder's ACL)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


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

def finite(v):
    """v with every NaN / Infinity as None, through dicts, lists and tuples (the hub's wire is strict JSON: RFC 8259)."""
    if isinstance(v, dict):
        return {k: finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [finite(x) for x in v]
    return None if isinstance(v, float) and not math.isfinite(v) else v


def dumps(obj) -> bytes:
    """A request body as strict JSON, compact: a number that isn't finite goes as null, never the bare token NaN."""
    return json.dumps(finite(obj), separators=(",", ":"), allow_nan=False).encode()


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
        data = dumps(body) if body is not None else None
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


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]


class _ExtendedLimits(ctypes.Structure):     # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on 64-bit Windows)
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class KillOnClose:
    """Windows: a job object with KILL_ON_JOB_CLOSE that the worker's children join, so when the worker ends, however
    it ends (`schtasks /End`, a crash), Windows ends whisper-server and llama-server with it: no orphan holding the GPU
    (and llama-server's ~6 GB) with a key nobody knows. Elsewhere, or when Windows refuses: nothing (best effort; the
    pid file and reap_leftovers() cover a leftover at the next start)."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            h = k32.CreateJobObjectW(None, None)
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if h and k32.SetInformationJobObject(ctypes.c_void_p(h), self.JobObjectExtendedLimitInformation,
                                                 ctypes.byref(info), ctypes.sizeof(info)):
                self.handle = h
            else:
                log.info("children: no kill-on-close job object (Windows said no)")
        except (OSError, AttributeError, ValueError) as e:
            log.info("children: no kill-on-close job object (%s)", type(e).__name__)

    def add(self, proc) -> bool:
        handle = getattr(proc, "_handle", None)
        if self.handle is None or handle is None:
            return False
        try:
            return bool(ctypes.windll.kernel32.AssignProcessToJobObject(ctypes.c_void_p(self.handle),
                                                                        ctypes.c_void_p(int(handle))))
        except (OSError, AttributeError, ValueError):
            return False


def process_name(pid: int, run: Callable[[list], str] = None) -> str:
    """The program name of a live pid ("llama-server"), "" when there is none (best effort)."""
    run = run or run_cmd
    try:
        if os.name == "nt":
            out = run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"])
            row = out.strip().splitlines()[0].strip().strip('"').split('","') if out.strip() else []
            return row[0].removesuffix(".exe") if len(row) > 1 and row[1] == str(pid) else ""
        return Path(run(["ps", "-p", str(int(pid)), "-o", "comm="]).strip()).name
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return ""


def kill_pid(pid: int, run: Callable[[list], str] = None) -> None:
    run = run or run_cmd
    try:
        if os.name == "nt":
            run(["taskkill", "/PID", str(int(pid)), "/T", "/F"])
        else:
            os.kill(int(pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def port_owner(port: int, run: Callable[[list], str] = None) -> int:
    """The pid listening on 127.0.0.1:port (netstat -ano on Windows, lsof elsewhere), 0 when it can't be told."""
    run = run or run_cmd
    try:
        if os.name == "nt":
            for line in run(["netstat", "-ano", "-p", "TCP"]).splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[0] == "TCP" and cols[3] == "LISTENING" \
                        and cols[1].rsplit(":", 1)[-1] == str(int(port)) and cols[4].isdigit():
                    return int(cols[4])
            return 0
        out = run(["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"]).split()
        return int(out[0]) if out and out[0].isdigit() else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


class Children:
    """state_dir/children.json: {name: pid} of the servers this worker started, so the next start can end one a
    worker that died left running (reap_leftovers): only a pid that is still that program."""

    def __init__(self, path: Path, run: Callable[[list], str] | None = None):
        self.path, self.run = path, run
        self._lock = threading.Lock()

    def set(self, name: str, pid: int | None) -> None:
        with self._lock:
            doc = read_json(self.path)
            if pid:
                doc[name] = int(pid)
            else:
                doc.pop(name, None)
            try:
                write_json(self.path, doc)
            except OSError as e:
                log.debug("children file: %s", e)

    def reap_leftovers(self, names: dict) -> list[int]:
        """names: {name in the file: its program name}. The pids still alive as that program: ended. Which."""
        ended = []
        for name, pid in read_json(self.path).items():
            prog = names.get(name)
            if prog and isinstance(pid, int) and pid > 0:
                found = process_name(pid, self.run)
                if found and found.lower().removesuffix(".exe") == prog.lower().removesuffix(".exe"):
                    log.warning("%s (pid %s) was left running by a worker that ended: stopping it", name, pid)
                    kill_pid(pid, self.run)
                    ended.append(pid)
            self.set(name, None)
        return ended


class WhisperProcess:
    """whisper-server started (and restarted) by the worker, when `whisper_server` is set: one Task Scheduler task,
    no console window, its pid known for the GPU check, its output in whisper-server.log (rotated), and none of the
    worker's secrets in its environment. When something already answers on the port, it is used and nothing is
    started. A relative program path is from the config file's folder, which is also its working directory. The same
    for llama-server (name "llama-server", llama-server.log, its own environment: llm_env)."""

    def __init__(self, argv: list, log_path: Path, popen: Callable = subprocess.Popen, clock: Callable = time.monotonic,
                 base_dir: str = "", name: str = "whisper-server", env: Callable[[], dict] | None = None,
                 job: KillOnClose | None = None, children: Children | None = None,
                 command: Callable[[list], list] | None = None):
        """name: the server's (its log lines, its logger); env(): its environment at each start (default child_env);
        job: the kill-on-close job object it joins; children: where its pid is kept for the next start; command:
        what the argv becomes (llama-server's: llm_command)."""
        self.argv, self.log_path, self.popen, self.clock, self.base_dir = argv, log_path, popen, clock, base_dir
        self.name, self.env, self.job, self.children = name, env or child_env, job, children
        self._command = command or (lambda a: a)
        self.proc = None
        self.started = -1e9
        self._out: logging.Logger | None = None

    def command(self) -> list[str]:
        argv = [os.path.expandvars(str(a)) for a in self._command(list(self.argv))]
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
            out = logging.getLogger("sizeup.stt_worker." + self.name.replace("-", "_"))
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
            log.warning("%s exited (%s): starting it again", self.name, self.proc.poll())
        self.started = self.clock()
        try:
            self.proc = self.popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=self.env(), cwd=self.base_dir or None,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.info("started %s (pid %s)", self.name, self.proc.pid)
            if self.job is not None and not self.job.add(self.proc) and self.job.handle is not None:
                log.warning("%s (pid %s) couldn't join the kill-on-close job: it may outlive the worker (the next "
                            "start ends it)", self.name, self.proc.pid)
            if self.children is not None:
                self.children.set(self.name, self.proc.pid)
        except OSError as e:
            self.proc = None
            log.error("can't start %s: %s", self.name, e)
            return
        if getattr(self.proc, "stdout", None) is not None:
            threading.Thread(target=self._pump, args=(self.proc.stdout,), name=f"{self.name}-log", daemon=True).start()

    def stop(self) -> None:
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.children is not None and self.proc is not None:
            self.children.set(self.name, None)


# --- llama-server: the local LLM (the hub's infer lane) ------------------------------------------------------------

class LlmError(Exception):
    pass


def llm_env(key: str) -> dict:
    """llama-server's environment: the worker's secrets out, every LLAMA_* setting out (one could make it listen on the
    network, log prompts or open its slots endpoint), and its API key in: a random one per start, so no web page on this
    PC can use it (llama-server answers any origin) and it never shows on a command line."""
    env = {k: v for k, v in child_env().items() if not k.upper().startswith("LLAMA_")}
    env["LLAMA_API_KEY"] = key
    return env


class LlmCall:
    """One chat completion on its own connection, which another thread can abort(): llama-server stops generating when
    the client goes away (a pre-alert clip came, gaming, the deadline)."""

    def __init__(self, llm: "Llm", body: bytes, timeout: float):
        self.llm, self.body = llm, body
        self.conn = http.client.HTTPConnection(llm.host, llm.port, timeout=timeout)
        self.aborted = False

    def run(self) -> tuple[dict | None, int, int]:
        """(the answer as a JSON object or None when it isn't one, prompt tokens, completion tokens). LlmError."""
        try:
            if self.aborted:
                raise OSError("aborted")
            self.conn.request("POST", "/v1/chat/completions", body=self.body, headers=self.llm.headers())
            r = self.conn.getresponse()
            data = r.read()
        except (OSError, http.client.HTTPException) as e:
            if self.aborted:
                raise LlmError("aborted") from None
            raise LlmError(f"llama-server unreachable ({type(e).__name__})") from None
        finally:
            self.conn.close()
        if r.status != 200:
            raise LlmError(f"llama-server said {r.status}")
        try:
            doc = json.loads(data)
            content = doc["choices"][0]["message"]["content"]
            usage = doc.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmError("llama-server sent something that isn't a chat completion") from None
        text = re.sub(r"^\s*<think>.*?</think>", "", str(content or ""), flags=re.S).strip()
        try:
            out = json.loads(text)
            json.dumps(out, allow_nan=False)   # (a NaN / Infinity / 1e999 in the answer: invalid, as on the hub
        except ValueError:                     # (sizeup/cloud/infer.py Lane._answer), never nulled by dumps on the wire)
            out = None
        return (out if isinstance(out, dict) else None, int(_num(usage.get("prompt_tokens")) or 0),
                int(_num(usage.get("completion_tokens")) or 0))

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


class Llm:
    """llama.cpp's llama-server, OpenAI-compatible: /v1/chat/completions with response_format json_schema (its
    grammar constrains the output to the schema), temperature 0, one answer. key: the API key it was started with."""

    def __init__(self, url: str, timeout_s: float = 30.0, no_think: bool = True):
        self.url, self.timeout_s, self.no_think = url, timeout_s, no_think
        u = urlsplit(url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self.key = ""

    def headers(self) -> dict:
        return {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}

    def call(self, messages: list, schema: dict, max_tokens: int = 256, name: str = "answer",
             timeout: float | None = None) -> LlmCall:
        body = {"messages": messages, "max_tokens": int(max_tokens), "temperature": 0, "stream": False,
                "cache_prompt": True,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or "answer",
                                                    "strict": True, "schema": schema}}}
        if self.no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return LlmCall(self, json.dumps(body).encode(), timeout or self.timeout_s)

    def status(self) -> int:
        """GET /v1/models with the key: 200 once the model is loaded, 503 while loading, 401 when the llama-server on
        the port isn't the one started with this key (a leftover); 0 when nothing answers. A short timeout: it never
        holds the checks that run around it for long."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
        try:
            conn.request("GET", "/v1/models", headers=self.headers())
            r = conn.getresponse()
            r.read()
            return r.status
        except (OSError, http.client.HTTPException):
            return 0
        finally:
            conn.close()

    def health(self) -> bool:
        return self.status() == 200

    def alive(self) -> bool:
        try:
            socket.create_connection((self.host, self.port), timeout=1).close()
            return True
        except OSError:
            return False

    def check(self) -> tuple[dict | None, int, int]:
        """A tiny constrained answer (no GPU time to speak of): LlmError if it fails."""
        return self.call([{"role": "user", "content": 'Reply with {"ok": true}.'}],
                         {"type": "object", "required": ["ok"], "additionalProperties": False,
                          "properties": {"ok": {"type": "boolean"}}}, 16, "check",
                         timeout=max(self.timeout_s, 60.0)).run()


def schema_valid(v, schema: dict) -> bool:
    """v against the JSON schema subset the hub uses (sizeup/cloud/infer.py valid(): the hub checks again)."""
    if not isinstance(schema, dict):
        return False
    t = schema.get("type")
    kinds = {"null": lambda x: x is None, "boolean": lambda x: isinstance(x, bool),
             "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
             "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and x == x
             and abs(x) != float("inf"),
             "string": lambda x: isinstance(x, str), "object": lambda x: isinstance(x, dict),
             "array": lambda x: isinstance(x, list)}
    if t is not None and not any(kinds.get(k, lambda x: False)(v) for k in ([t] if isinstance(t, str) else t)):
        return False
    if "enum" in schema and not any(v == e and isinstance(v, bool) == isinstance(e, bool) for e in schema["enum"]):
        return False
    if "const" in schema and v != schema["const"]:
        return False
    if "anyOf" in schema and not any(schema_valid(v, s) for s in schema["anyOf"]):
        return False
    if isinstance(v, dict):
        props = schema.get("properties") or {}
        if any(k not in v for k in schema.get("required") or ()):
            return False
        if schema.get("additionalProperties", True) is False and set(v) - set(props):
            return False
        if not all(schema_valid(v[k], s) for k, s in props.items() if k in v):
            return False
    if isinstance(v, list):
        if not schema.get("minItems", 0) <= len(v) <= schema.get("maxItems", len(v)):
            return False
        if "items" in schema and not all(schema_valid(x, schema["items"]) for x in v):
            return False
    if isinstance(v, str) and not schema.get("minLength", 0) <= len(v) <= schema.get("maxLength", len(v)):
        return False
    if kinds["number"](v):
        if not schema.get("minimum", v) <= v <= schema.get("maximum", v):
            return False
        if v <= schema.get("exclusiveMinimum", v - 1) or v >= schema.get("exclusiveMaximum", v + 1):
            return False
    return True


def file_sha256(path: Path, cache: Path) -> str:
    """A big file's SHA-256, remembered in `cache` by its path, size and mtime (a 5 GB GGUF takes a while to hash):
    the next start checks it again only when the file changed."""
    st = path.stat()
    key = {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    doc = read_json(cache)
    if {k: doc.get(k) for k in key} == key and isinstance(doc.get("sha256"), str):
        return doc["sha256"]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    write_json(cache, dict(key, sha256=h.hexdigest()))
    return h.hexdigest()


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


# --- self-update: the hub names a commit and a SHA-256; only main of UPDATE_REPO on GitHub is a source -------------

UPDATE_REPO = "actualsecurity/sizeup"      # never the hub's to choose: nor the branch, the file, the hosts
UPDATE_BRANCH = "main"
UPDATE_FILE = "tools/stt_worker/worker.py"
GITHUB_API = "api.github.com"
GITHUB_RAW = "raw.githubusercontent.com"
UPDATE_MAX_BYTES = 2_000_000               # worker.py is ~0.13 MB
UPDATE_API_MAX_BYTES = 256_000             # GitHub's comparison for a commit on main is a few KB
UPDATE_CHECK_S = 600.0                     # GitHub at most this often (unauthenticated: 60 requests an hour per address)
UPDATE_BACKOFF_MAX_S = 6 * 3600.0
UPDATE_EXIT = 75                           # "updated: start me again" (Task Scheduler's 5-minute trigger, systemd Restart=)
UPDATE_CONFIRM_S = 600.0                   # a new version must reach the hub this soon after it starts ...
UPDATE_MAX_STARTS = 3                      # ... and within this many starts, else worker.py.prev comes back
UPDATE_QUIET_S = 120.0                     # no swap this soon after gaming (or the worker's start)
UPDATE_DRAIN_S = 90.0                      # the jobs in hand handed back or done, at most this long
UPDATE_PULLS_MAX_BYTES = 768_000           # GitHub's pull requests of a commit (~20 KB each, 5 at most asked)
UPDATE_RETRY_S = 3600.0                    # a refusal that may pass later (GitHub lag, a busy PC): tried again after this
UPDATE_BAD_S = 6 * 3600.0                  # a hash rolled back by itself is tried again after this (an outage looks the
                                           # same as a broken file); one rolled back by hand: never, until `update retry`
SELFTEST_PROTOCOL = 1
SELFTEST_TIMEOUT_S = 60.0
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class UpdateError(Exception):
    """final: refused for this commit and hash (said once; this process doesn't try that pair again). Otherwise GitHub
    was out of reach or limited us: tried again later (retry_s: when GitHub said)."""

    def __init__(self, msg: str, final: bool = False, retry_s: float | None = None, permanent: bool = False,
                 detail: str = ""):
        super().__init__(msg)
        self.final, self.retry_s, self.permanent = final or permanent, retry_s, permanent
        self.detail = detail                       # for the local log only (never the hub's)


def https_get(host: str, path: str, max_bytes: int, timeout: float = 20.0) -> tuple[int, dict, bytes]:
    """GET https://<host><path> for the self-update: GitHub's two hosts only, the system's CA store, no token (GitHub
    unauthenticated), no redirect followed. At most max_bytes + 1 bytes of the body are read (more: too large)."""
    if host not in (GITHUB_API, GITHUB_RAW):
        raise UpdateError(f"not a GitHub host ({host[:40]})", final=True)
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if host == GITHUB_API:
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read(max_bytes + 1)
    except (OSError, http.client.HTTPException) as e:
        raise UpdateError(f"{host}: {type(e).__name__}") from None
    finally:
        conn.close()


def github_wait(status: int, headers: dict, now: float) -> float | None:
    """How long GitHub asked us to wait (Retry-After, or its rate limit's reset), None when it didn't say."""
    ra = retry_after(headers.get("retry-after"))
    if ra is not None:
        return ra
    if status in (403, 429) and str(headers.get("x-ratelimit-remaining", "")).strip() == "0":
        try:
            return max(0.0, min(float(headers.get("x-ratelimit-reset", "")) - now, UPDATE_BACKOFF_MAX_S))
        except ValueError:
            return None
    return None


def check_on_main(commit: str, fetch: Callable = https_get, now: float | None = None) -> None:
    """The commit is on UPDATE_BRANCH of UPDATE_REPO, as main's own commit: GitHub's comparison main...<commit> says it
    is behind main or the same, with the commit itself as the merge base; and it is the merge commit of a pull request
    merged into main (what the hub's builds are). So not a commit of a fork (GitHub serves those under the parent's
    name too), of a branch not merged, nor one inside a merged branch (reachable from main, but never reviewed as it
    stood). UpdateError(final) otherwise."""
    if not COMMIT.fullmatch(commit):
        raise UpdateError("not a commit", final=True)
    status, headers, body = fetch(GITHUB_API, f"/repos/{UPDATE_REPO}/compare/{UPDATE_BRANCH}...{commit}?per_page=1",
                                  UPDATE_API_MAX_BYTES)
    if status in (404, 422):
        raise UpdateError(f"not in {UPDATE_REPO} ({status})", final=True)
    if status != 200:
        raise UpdateError(f"GitHub's API said {status}", retry_s=github_wait(status, headers,
                                                                              time.time() if now is None else now))
    if len(body) > UPDATE_API_MAX_BYTES:
        raise UpdateError(f"not on {UPDATE_BRANCH} (a large comparison)", final=True)
    try:
        doc = json.loads(body)
    except ValueError:
        raise UpdateError("GitHub's API sent something that isn't JSON") from None
    if not isinstance(doc, dict):
        raise UpdateError("GitHub's API sent something that isn't an object")
    base = doc.get("merge_base_commit") if isinstance(doc.get("merge_base_commit"), dict) else {}
    if doc.get("status") not in ("behind", "identical") or base.get("sha") != commit:
        raise UpdateError(f"not on {UPDATE_BRANCH} ({str(doc.get('status') or '?')[:20]})", final=True)
    status, headers, body = fetch(GITHUB_API, f"/repos/{UPDATE_REPO}/commits/{commit}/pulls?per_page=5",
                                  UPDATE_PULLS_MAX_BYTES)
    if status != 200:
        raise UpdateError(f"GitHub's API said {status}", retry_s=github_wait(status, headers,
                                                                              time.time() if now is None else now))
    try:
        pulls = json.loads(body) if len(body) <= UPDATE_PULLS_MAX_BYTES else None
    except ValueError:
        pulls = None
    if not isinstance(pulls, list):
        raise UpdateError("GitHub's API sent something that isn't a list of pull requests")
    if not any(isinstance(p, dict) and p.get("merge_commit_sha") == commit and p.get("merged_at")
               and isinstance(p.get("base"), dict) and p["base"].get("ref") == UPDATE_BRANCH for p in pulls):
        raise UpdateError(f"not a pull request's merge into {UPDATE_BRANCH}", final=True)


def download_worker(commit: str, sha: str, fetch: Callable = https_get, now: float | None = None) -> bytes:
    """worker.py at that commit, from raw.githubusercontent.com: at most UPDATE_MAX_BYTES, and exactly the hash the
    hub named. UpdateError otherwise (final when the answer itself is wrong)."""
    status, headers, body = fetch(GITHUB_RAW, f"/{UPDATE_REPO}/{commit}/{UPDATE_FILE}", UPDATE_MAX_BYTES)
    if status == 404:
        raise UpdateError(f"no {UPDATE_FILE} at {commit[:12]}", final=True)
    if status != 200:
        raise UpdateError(f"GitHub said {status}", retry_s=github_wait(status, headers, time.time() if now is None else now))
    if len(body) > UPDATE_MAX_BYTES:
        raise UpdateError(f"larger than {UPDATE_MAX_BYTES // 1_000_000} MB", permanent=True)
    got = hashlib.sha256(body).hexdigest()
    if got != sha:
        raise UpdateError(f"hash mismatch (the file is {got[:12]})", permanent=True)
    return body


def _replace(src: Path, dst: Path) -> None:
    """os.replace (a rename: atomic on Windows and POSIX), tried again for a moment while Windows has dst open."""
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def write_file(path: Path, data: bytes, like: Path | None = None) -> None:
    """path holds exactly data, written beside it and renamed over it; with like's permission bits (POSIX: the mode)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if like is not None and like.exists():
            shutil.copymode(like, tmp)
        _replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class Updater:
    """The worker's self-update. The hub's hello and state answers name the version it wants, worker_update {mode,
    commit, sha256} (mode auto: the hub's own build; pin: an admin's; hold: none). When that hash isn't this file's
    and auto_update is on (worker.toml):

    1. GitHub's API: the commit is on main of UPDATE_REPO (check_on_main);
    2. raw.githubusercontent.com: worker.py at that commit, exactly that hash, at most UPDATE_MAX_BYTES;
    3. staged as worker.py.new (this file's mode), and `worker.py.new selftest` passes (it imports, reads the config,
       builds a Worker, no network);
    4. once the worker is idle (no clip, no LLM job, no gaming lately, the hub reachable): no new jobs, worker.py ->
       worker.py.prev (a copy), worker.py.new -> worker.py (a rename), and the worker exits UPDATE_EXIT: Task
       Scheduler's 5-minute trigger, or systemd's Restart=always, starts the new file.
    5. The new file must reach the hub within UPDATE_CONFIRM_S of a start, and within UPDATE_MAX_STARTS starts; else
       worker.py.prev is copied back, the new hash is marked bad (never tried again: `worker.py update retry` clears
       it) and the worker exits UPDATE_EXIT again.

    The hub's url is never used: the host, repo, branch and path are this file's. GitHub at most every UPDATE_CHECK_S,
    longer after errors or when its rate limit says so. state_dir/update.json keeps the bad hashes, the commits of
    known hashes, the swap not confirmed yet ("pending") and the last event; the CLI writes it too (retry, rollback)."""

    def __init__(self, cfg: Config, path: str | os.PathLike | None = None, config_path: str | os.PathLike | None = None,
                 fetch: Callable = https_get, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, selftest: Callable[[Path, str], str] | None = None,
                 python: str = ""):
        self.cfg = cfg
        self.linked = Path(path or __file__).is_symlink()  # run through a link (into a checkout?): never rewritten
        self.path = Path(path or __file__).resolve()
        self.config_path = str(config_path) if config_path else ""
        self.fetch, self.clock, self.wall = fetch, clock, wall
        self.selftest = selftest or self.run_selftest
        self.python = python or sys.executable
        self.state_path = Path(cfg.state_dir) / "update.json"
        try:
            self.own = hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            self.own = ""
        self.started = clock()
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._doc: dict = {}
        self.offered: tuple[str, str, str] | None = None     # (mode, commit, sha256): the hub's latest word
        self._refused: dict[tuple[str, str], tuple[str, float]] = {}   # (commit, sha256) -> (why, again at; inf: never)
        self.staged: tuple[str, str] | None = None
        self.next_at = 0.0
        self.fails = 0
        self.confirmed = False             # this file reached the hub since its start (no rollback from here on)
        self.waited = 0.0                  # seconds this start has waited for the hub (a sleep's jump not counted)
        self._ticked: float | None = None
        last = self.doc().get("last") if isinstance(self.doc().get("last"), dict) else {}
        self.status = (f"rolled back from {str(last.get('from'))[:12]}" if last.get("event") == "rolled back"
                       and last.get("to") == self.own else "starting")

    @property
    def new_path(self) -> Path:
        return self.path.with_name(self.path.name + ".new")

    @property
    def prev_path(self) -> Path:
        return self.path.with_name(self.path.name + ".prev")

    # -- update.json (the CLI writes it too: read again when it changed, every change read-modify-write)

    def doc(self) -> dict:
        try:
            st = self.state_path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig != self._sig:
            self._sig, self._doc = sig, (read_json(self.state_path) if sig else {})
        return self._doc

    def change(self, fn: Callable[[dict], object], strict: bool = False):
        """fn(doc) on update.json as it is now, then written. strict: an OSError is raised (else logged)."""
        with self._lock:
            doc = read_json(self.state_path)
            out = fn(doc)
            try:
                write_json(self.state_path, doc)
            except OSError as e:
                if strict:
                    raise
                log.warning("self-update: can't write update.json: %s", e.strerror or e)
            self._sig, self._doc = None, {}
            return out

    def bad(self) -> dict:
        """The hashes not to take: rolled back by hand (until `update retry`), or by itself within UPDATE_BAD_S."""
        bad = self.doc().get("bad")
        now = self.wall()
        return {k: v for k, v in bad.items() if isinstance(v, dict)
                and (v.get("manual") or not isinstance(v.get("at"), (int, float)) or now - v["at"] < UPDATE_BAD_S)} \
            if isinstance(bad, dict) else {}

    def pending(self) -> dict | None:
        """The swap to this file, not confirmed yet (it hasn't reached the hub since)."""
        p = self.doc().get("pending")
        return p if isinstance(p, dict) and p.get("to") == self.own and self.own else None

    def commit_of(self, sha: str) -> str:
        commits = self.doc().get("commits")
        c = commits.get(sha, "") if isinstance(commits, dict) else ""
        return c if isinstance(c, str) and COMMIT.fullmatch(c) else ""

    def _mark_bad(self, doc: dict, sha: str, commit: str, why: str, manual: bool = False) -> None:
        bad = doc.get("bad") if isinstance(doc.get("bad"), dict) else {}
        bad.pop(sha, None)
        bad[sha] = {"commit": commit, "why": why[:120], "at": self.wall(), **({"manual": True} if manual else {})}
        doc["bad"] = dict(list(bad.items())[-20:])

    def _refuse(self, commit: str, sha: str, why: str, final: bool) -> None:
        """Not taken again by this process: never (final: the file itself is wrong), or not for UPDATE_RETRY_S."""
        self._refused.pop((commit, sha), None)
        self._refused[(commit, sha)] = (why[:100], math.inf if final else self.clock() + UPDATE_RETRY_S)
        while len(self._refused) > 20:
            self._refused.pop(next(iter(self._refused)))

    # -- the hub's word and ours

    def offer(self, ans) -> None:
        """A hello or state answer: its worker_update (none: nothing to update to). Its url is never used."""
        if not isinstance(ans, dict):
            return
        wu = ans.get("worker_update")
        if not isinstance(wu, dict):               # a hub that names none (one from before the self-update): no update
            self.offered = ("none", "", "")
            return
        mode = wu.get("mode") if wu.get("mode") in ("auto", "pin", "hold") else "hold"
        commit, sha = wu.get("commit"), wu.get("sha256")
        if mode != "hold" and isinstance(commit, str) and isinstance(sha, str) and COMMIT.fullmatch(commit) \
                and SHA256.fullmatch(sha):
            self.offered = (mode, commit, sha)
        else:
            self.offered = (mode if mode == "hold" else "none", "", "")

    def report(self) -> dict:
        """What the hub is told (self_update in hello and state): this file's hash and commit, auto_update, the state
        in words, the hashes marked bad."""
        return {"sha256": self.own, "commit": self.commit_of(self.own), "auto": bool(self.cfg.auto_update),
                "state": self.status[:80], "bad": list(self.bad())[-5:]}

    # -- the steps

    def step(self, busy: Callable[[], str]) -> bool:
        """One look (the update thread, about every second). True: the staged file is ready and the worker is idle:
        drain, then swap()."""
        if not self.cfg.auto_update:
            self.status = "off (auto_update)"
            return False
        if (self.path.parent / ".git").exists() or self.linked:
            self.status = "off (a git checkout)"           # never rewrite a tracked file
            return False
        if self.offered is None:
            self.status = "waiting for the hub"
            return False
        mode, commit, sha = self.offered
        if not sha:
            self.status = "held by the hub" if mode == "hold" else "the hub names no version"
            return False
        if sha == self.own:
            self.status = "current"
            self.staged = None
            return False
        if sha in self.bad():
            self.status = f"refused {sha[:12]}: rolled back before"
            return False
        why, until = self._refused.get((commit, sha), ("", 0.0))
        if why and self.clock() < until:
            self.status = f"refused {sha[:12]}: {why}"
            return False
        if self.pending():
            self.status = "new: not confirmed yet"
            return False
        if self.staged != (commit, sha):
            if self.clock() < self.next_at:
                return False
            self.next_at = self.clock() + UPDATE_CHECK_S
            self.staged = None
            self.status = f"checking {sha[:12]}"
            try:
                self.prepare(commit, sha)
            except UpdateError as e:
                if e.final:
                    # the file itself is wrong (its hash, its size): never; else (not on main yet, GitHub lag, a
                    # self-check that timed out on a busy PC) tried again in an hour
                    self._refuse(commit, sha, str(e), final=e.permanent)
                    self.status = f"refused {sha[:12]}: {str(e)[:64]}"
                    log.error("self-update: %s (commit %s): refused: %s", sha[:12], commit[:12], e.detail or e)
                else:
                    self.fails += 1
                    wait = e.retry_s if e.retry_s is not None else UPDATE_CHECK_S * 2 ** min(self.fails - 1, 6)
                    wait = min(max(wait, UPDATE_CHECK_S), UPDATE_BACKOFF_MAX_S)
                    self.next_at = self.clock() + wait
                    self.status = f"GitHub: {str(e)[:40]}; again in {wait / 60:.0f} min"
                    if self.fails == 1:
                        log.warning("self-update: %s: trying again in %.0f min", e, wait / 60)
                return False
            self.fails = 0
            self.staged = (commit, sha)
            log.info("self-update: %s (commit %s) is on %s, checked and staged: switching when idle", sha[:12],
                     commit[:12], UPDATE_BRANCH)
        why = busy()
        if why:
            self.status = f"staged {sha[:12]}, waiting: {why}"
            return False
        self.status = f"updating to {sha[:12]}"
        return True

    def prepare(self, commit: str, sha: str) -> None:
        """On main, downloaded, the hash, staged as worker.py.new, its self-check. UpdateError."""
        check_on_main(commit, self.fetch, self.wall())
        data = download_worker(commit, sha, self.fetch, self.wall())
        try:
            write_file(self.new_path, data, like=self.path)
        except OSError as e:
            raise UpdateError(f"can't write {self.new_path.name}: {e.strerror or e}", final=True) from None
        why = self.selftest(self.new_path, sha)
        if why:
            try:
                self.new_path.unlink()
            except OSError:
                pass
            # the hub hears "exit N" only; the last line of its stderr (a path, a message) stays in the local log
            short, _, rest = why.partition(": ")
            raise UpdateError(f"its self-check failed: {short}", final=True,
                              detail=f"its self-check failed: {why}" if rest else "")

    def run_selftest(self, new: Path, sha: str) -> str:
        """`python worker.py.new selftest --result <file> [-c <this config>]`: "" when it exits 0 and reports ok for
        exactly that hash, else why. The same python, environment and folder the worker runs with."""
        result = new.with_name(f".selftest.{os.getpid()}.json")
        argv = [self.python, str(new), "selftest", "--result", str(result)]
        if self.config_path:
            argv += ["-c", self.config_path]
        try:
            result.unlink(missing_ok=True)
            p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=SELFTEST_TIMEOUT_S,
                               cwd=str(new.parent), stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return f"no answer in {SELFTEST_TIMEOUT_S:.0f} s"
        except OSError as e:
            return f"can't run it ({type(e).__name__})"
        try:
            doc = read_json(result)
        finally:
            try:
                result.unlink(missing_ok=True)
            except OSError:
                pass
        if p.returncode != 0:
            last = (p.stderr or "").strip().splitlines()[-1:] or [""]
            return f"exit {p.returncode}" + (f": {last[0][:100]}" if last[0] else "")
        if doc.get("ok") is not True or doc.get("sha256") != sha or not isinstance(doc.get("protocol"), int) \
                or doc["protocol"] < SELFTEST_PROTOCOL:
            return "it didn't report ok for this file"
        return ""

    def swap(self) -> bool:
        """worker.py -> worker.py.prev (a copy, so a worker.py is always there), the swap written down as pending,
        worker.py.new -> worker.py (a rename, its mode this file's). False (and that pair refused) when it can't."""
        if self.staged is None:
            return False
        commit, sha = self.staged
        try:
            data = self.new_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise UpdateError("the staged file changed", final=True)
            write_file(self.prev_path, self.path.read_bytes(), like=self.path)

            def pend(doc):
                doc["pending"] = {"from": self.own, "to": sha, "commit": commit, "at": self.wall(), "starts": 0}
                commits = doc.get("commits") if isinstance(doc.get("commits"), dict) else {}
                commits[sha] = commit
                doc["commits"] = dict(list(commits.items())[-10:])
            self.change(pend, strict=True)                  # (no record of it: no rollback either, so no swap)
            shutil.copymode(self.path, self.new_path)
            _replace(self.new_path, self.path)
        except (OSError, UpdateError) as e:
            why = str(e) if isinstance(e, UpdateError) else f"can't swap the files: {e.strerror or e}"
            self.change(lambda doc: doc.pop("pending", None))
            self._refuse(commit, sha, why, final=False)
            self.staged = None
            self.status = f"refused {sha[:12]}: {why[:60]}"
            log.error("self-update: %s: %s: staying on %s", sha[:12], why, self.own[:12])
            return False
        log.info("self-update: worker.py is now %s (commit %s), worker.py.prev %s: restarting", sha[:12], commit[:12],
                 self.own[:12])
        return True

    def boot(self) -> bool:
        """At `run`'s start, before anything else: a start of a new file not confirmed yet is counted; past
        UPDATE_MAX_STARTS it is rolled back. True: rolled back (exit UPDATE_EXIT: the old file starts)."""
        raw = self.doc().get("pending")
        if not isinstance(raw, dict):
            return False
        if self.pending() is None:                          # worker.py was replaced some other way: forget it
            self.change(lambda doc: doc.pop("pending", None))
            return False

        def count(doc):
            p = doc.get("pending") if isinstance(doc.get("pending"), dict) else {}
            p["starts"] = (p["starts"] if isinstance(p.get("starts"), int) else 0) + 1
            doc["pending"] = p
            return p["starts"]
        try:
            starts = self.change(count, strict=True)
        except OSError as e:                               # no count, no rollback later: not run unconfirmed
            return self.rollback(f"can't count its start ({e.strerror or e})")
        if starts > UPDATE_MAX_STARTS:
            return self.rollback(f"{starts - 1} starts without reaching the hub")
        log.info("self-update: running %s, not confirmed yet (start %d of %d)", self.own[:12], starts, UPDATE_MAX_STARTS)
        self.status = "new: not confirmed yet"
        return False

    def overdue(self) -> bool:
        """The new file hasn't reached the hub in UPDATE_CONFIRM_S of this start, counted in the update thread's ticks
        (5 s at most each: a PC asleep meanwhile doesn't count, as Windows' monotonic clock would)."""
        now = self.clock()
        dt = 0.0 if self._ticked is None else min(max(now - self._ticked, 0.0), 5.0)
        self._ticked = now
        if self.confirmed or self.pending() is None:
            return False
        self.waited += dt
        return self.waited >= UPDATE_CONFIRM_S

    def confirm(self) -> None:
        """The hub answered: a pending swap to this file is kept."""
        if self.confirmed:
            return
        p = self.pending()
        self.confirmed = True
        if p is None:
            return

        def done(doc):
            q = doc.pop("pending", None)
            if isinstance(q, dict) and q.get("to") == self.own:
                doc["last"] = {"event": "updated", "from": q.get("from"), "to": self.own, "commit": q.get("commit"),
                               "at": self.wall()}
        self.change(done)
        try:
            self.new_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.status = f"updated from {str(p.get('from'))[:12]}"
        log.info("self-update: %s reached the hub: kept (worker.py.prev is %s)", self.own[:12], str(p.get("from"))[:12])

    def rollback(self, why: str, manual: bool = False) -> bool:
        """worker.py.prev copied back over worker.py; this file's hash marked bad. True when it was. Not after this
        file reached the hub (manual: by hand, whatever happened); never a .prev that isn't the file swapped out."""
        if self.confirmed and not manual:
            return False
        try:
            data = self.prev_path.read_bytes()
            if not data:
                raise OSError("empty")
            p = self.doc().get("pending") if isinstance(self.doc().get("pending"), dict) else {}
            if not manual and SHA256.fullmatch(str(p.get("from") or "")) \
                    and hashlib.sha256(data).hexdigest() != p["from"]:
                raise OSError(f"it isn't {str(p['from'])[:12]}, the file swapped out")
        except OSError as e:
            self.change(lambda doc: doc.__setitem__("last", {"event": "rollback failed", "why": why, "at": self.wall()}))
            log.error("self-update: %s: can't roll back (worker.py.prev: %s): carrying on with %s", why,
                      getattr(e, "strerror", None) or e, self.own[:12])
            return False

        def mark(doc):
            q = doc.get("pending") if isinstance(doc.get("pending"), dict) else None
            if q is None and not manual:
                return None                                # confirmed meanwhile (another thread): nothing to undo
            doc.pop("pending", None)
            self._mark_bad(doc, self.own, str((q or {}).get("commit") or ""), why, manual=manual)
            return q or {}
        p = self.change(mark)
        if p is None or (self.confirmed and not manual):
            return False
        try:
            write_file(self.path, data, like=self.path)
        except OSError as e:
            self.change(lambda doc: doc.__setitem__("last", {"event": "rollback failed", "why": why, "at": self.wall()}))
            log.error("self-update: %s: can't roll back (worker.py.prev: %s): carrying on with %s", why,
                      getattr(e, "strerror", None) or e, self.own[:12])
            return False
        old = hashlib.sha256(data).hexdigest()
        self.change(lambda doc: doc.__setitem__("last", {"event": "rolled back", "from": self.own, "to": old,
                                                         "commit": p.get("commit"), "why": why, "at": self.wall()}))
        self.status = f"rolled back: {why}"
        log.error("self-update: %s: rolled back to worker.py.prev (%s); %s is never tried again", why, old[:12],
                  self.own[:12])
        return True


# --- the worker ----------------------------------------------------------------------------------------------------

class Worker:
    """hello, then (a jobs thread): claim (a long poll) -> fetch the audio -> whisper-server -> complete; a heartbeat
    thread reports the state every heartbeat_s (in every state); a monitor thread watches the GPU and whisper-server.
    run()'s own thread only waits, so Ctrl-C stops the worker at once."""

    def __init__(self, cfg: Config, run: Callable[[list], str] = run_cmd, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None, popen: Callable = subprocess.Popen,
                 fullscreen: Callable[[], str] = windows_fullscreen, updater: Updater | None = None):
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
        # the local LLM (the hub's infer lane), when llm_url is set: its own thread, hub connection and counts
        self.llm = Llm(cfg.llm_url, cfg.llm_timeout_s, cfg.llm_no_think) if cfg.llm_url else None
        self.kill_job = KillOnClose()           # (self.job is the clip in hand: not this)
        self.children = Children(Path(cfg.state_dir) / "children.json", run)
        if self.proc is not None:
            self.proc.job, self.proc.children = self.kill_job, self.children
        self.llm_proc = WhisperProcess(cfg.llm_server, Path(cfg.state_dir) / "llama-server.log", popen, clock,
                                       cfg.base_dir, name="llama-server", env=self._llm_start_env, job=self.kill_job,
                                       children=self.children, command=llm_command) \
            if self.llm is not None and cfg.llm_server else None
        self._llm_sig: tuple | None = None   # the checked GGUF's (size, mtime): looked at again before each start
        self.llm_hub = Hub(cfg.hub_url, cfg.token, cfg.cf_client_id, cfg.cf_client_secret)
        self.llm_backoff = Backoff()
        self.llm_ok = False
        self.llm_err = "checking"
        self.llm_fails = 0
        self.llm_verified: bool | None = None if self.llm_proc is not None and cfg.llm_model_sha256 else True
        self._llm_checked = -1e9
        self._llm_started_at = -1e9
        self._llm_until = 0.0              # an older hub without the infer lane: not asked again before this
        self._llm_call: LlmCall | None = None
        self._llm_claiming = False
        self._preempt = threading.Event()  # a pre-alert clip came: the generation in flight steps aside
        self.llm_job: dict | None = None
        self.llm_on: bool | None = None    # the hub's switches for this worker's LLM jobs (None: not said yet)
        self.llm_counts = {"done": 0, "invalid": 0, "released": 0, "lost": 0, "failed": 0}
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
        # the self-update (Updater): no new jobs while it drains; the exit code that starts the new file
        self.updater = updater if updater is not None else Updater(cfg, clock=clock)
        self.draining = False
        self.exit_code = 0
        self._gaming_seen = clock()        # gaming lately (and the start, before the GPU was looked at): no swap yet

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
        if self.draining:
            return "paused", "updating"
        held = self.held()
        if held:
            return held
        if not self.whisper_ok:
            return "paused", f"whisper-server: {self.whisper_err}"
        return "online", ""

    def _busy(self) -> bool:
        return self._inferring > 0 or self.clock() < self._busy_until

    def _our_pids(self) -> set:
        """whisper-server's and llama-server's pids: their GPU memory is ours, never another process's (a game)."""
        pids = {self.cfg.whisper_pid} if self.cfg.whisper_pid else set()
        for proc in (self.proc, self.llm_proc):
            if proc is not None and proc.pid:
                pids.add(proc.pid)
        at, found = self._pids
        if self.clock() - at > 30:
            found = find_pids(self.cfg.whisper_process, self.run_cmd)
            if self.llm is not None:
                found = found | find_pids(self.cfg.llm_process, self.run_cmd)
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
        lcall = self._llm_call
        if lcall is not None:
            lcall.abort()
        if self._llm_claiming:
            self.llm_hub.interrupt()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- the hub

    def _state_body(self) -> dict:
        state, reason = self.state()
        body = {"state": state, "model": self.cfg.model, "version": VERSION, "models": self.models(),
                "self_update": self.updater.report()}
        if reason:
            body["reason"] = reason
        if self.gpu.info:
            body["gpu"] = dict(self.gpu.info)
        return body

    def models(self) -> dict:
        """What the hub's infer lane is told: the STT model and the LLM while llama-server answers ("" otherwise)."""
        ready = self.llm is not None and self.llm_ok
        return {"stt": self.cfg.model, "llm": self.cfg.llm_model if ready else "",
                "llm_version": self.cfg.llm_model_sha256[:12].lower() if ready else ""}

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
        self.updater.offer(ans)
        self.updater.confirm()
        if not self.connected:
            classes = ans.get("classes") if isinstance(ans.get("classes"), list) else []
            log.info("hub: connected as %s (%s); classes %s", self.cfg.worker_id, body["state"],
                     ", ".join(map(str, classes)) or "-")
        self.connected, self.link = True, "ok"

    def heartbeat(self) -> None:
        ans = self.hb_hub.request("POST", f"{API}/state", self._state_body(), timeout=HEARTBEAT_TIMEOUT_S)
        self.updater.offer(ans)
        self.updater.confirm()
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
                sig = (state, self.llm_ok)         # (the LLM coming or going: the hub hears at once too)
                if sig != last_state:
                    if last_state is not None and state != last_state[0]:
                        log.info("state: %s%s", state[0], f" ({state[1]})" if state[1] else "")
                    last_state = sig
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
                if gaming:
                    self._gaming_seen = now
                if (self.proc is not None and self.proc.running() and self._gaming_since is not None
                        and self.cfg.gaming_stop_whisper_min > 0
                        and now - self._gaming_since >= self.cfg.gaming_stop_whisper_min * 60):
                    log.info("gaming for %.0f min: stopping whisper-server (its VRAM back to the game)",
                             self.cfg.gaming_stop_whisper_min)
                    self._whisper_down("stopped while gaming")     # (said first: no check in between sees it gone)
                    self.proc.stop()
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

    def _llm_monitor_loop(self) -> None:
        """llama-server's checks on a thread of their own: a slow answer never holds the GPU (gaming) samples."""
        while not self.stopped:
            try:
                self._llm_monitor(self.clock(), self.held())
            except Exception:  # noqa: BLE001 - the LLM's checks must outlive any bug
                log.exception("llm monitor: unexpected error (carrying on)")
                self._stop.wait(5)
            self._stop.wait(self.cfg.tick_s)

    def llm_stop_after_s(self) -> float:
        """Seconds of gaming before llama-server is stopped: llm_stop_gaming_s, or (below 0, the default) 60 for a model
        up to 6 GB and 20 for a bigger one (Gemma-3-12B: ~8 GB the game wants back sooner)."""
        if self.cfg.llm_stop_gaming_s >= 0:
            return self.cfg.llm_stop_gaming_s
        return 20.0 if (self._llm_sig or (0, 0))[0] > 6e9 else 60.0

    def _llm_model_path(self) -> Path:
        path = Path(os.path.expandvars(_arg(self.cfg.llm_server, ("-m", "--model")) or ""))
        return Path(self.cfg.base_dir) / path if not path.is_absolute() and self.cfg.base_dir else path

    def _llm_changed(self) -> bool:
        """The GGUF isn't the file that was checked any more (its size or time): checked again before a start."""
        try:
            st = self._llm_model_path().stat()
            return (st.st_size, st.st_mtime_ns) != self._llm_sig
        except OSError:
            return True

    def _llm_monitor(self, now: float, held) -> None:
        """llama-server: stopped after llm_stop_after_s() of gaming (its 6-8 GB of VRAM the game wants), started again
        (and checked) once the PC is neither gaming nor paused; never started with a model whose hash isn't pinned (the
        file looked at again before each start: checked again when it changed)."""
        if (self.llm_proc is not None and self.llm_proc.running() and self._gaming_since is not None
                and now - self._gaming_since >= self.llm_stop_after_s()):
            log.info("gaming for %.0f s: stopping llama-server (its VRAM back to the game)", now - self._gaming_since)
            self._llm_down("stopped while gaming")         # (said first: no job goes to it meanwhile)
            self.llm_proc.stop()
        if held or not self.llm_verified:
            return
        if self.llm_proc is not None and not self.llm_proc.running() and now - self._llm_started_at >= 5:
            if self.cfg.llm_model_sha256 and self._llm_changed():
                self.llm_verified = None
                threading.Thread(target=self.verify_llm, name="llm-verify", daemon=True).start()
                return
            self._llm_started_at = now
            self.llm_proc.ensure(self.llm.alive())
        if now - self._llm_checked < self.cfg.llm_check_s or self._llm_call is not None:
            return
        self._llm_checked = now
        code = self.llm.status()
        if code == 200:
            if not self.llm_ok:
                log.info("llama-server: ok (%s)", self.cfg.llm_model)
            self.llm_ok, self.llm_err, self.llm_fails = True, "", 0
        elif code == 401:
            self._llm_down(f"port {self.llm.port} answered 401: not this worker's llama-server")
            if self.llm_proc is not None and not self.llm_proc.running():
                # only the program that holds llm_url's port, and only if it is a llama-server (a leftover of a worker
                # that died): never another llama-server the user runs elsewhere
                pid = port_owner(self.llm.port, self.run_cmd)
                name = process_name(pid, self.run_cmd) if pid else ""
                prog = Path(str(self.cfg.llm_server[0])).name if self.cfg.llm_server else self.cfg.llm_process
                if pid and name and name.lower().removesuffix(".exe") in {
                        prog.lower().removesuffix(".exe"), self.cfg.llm_process.lower().removesuffix(".exe")}:
                    log.warning("stopping a leftover %s (pid %s) that holds port %s", name, pid, self.llm.port)
                    kill_pid(pid, self.run_cmd)
        else:
            self._llm_down("loading" if code == 503 else "not answering")

    def _llm_down(self, why: str) -> None:
        if self.llm_ok:
            log.warning("llama-server: %s: no LLM jobs until it answers", why)
        self.llm_ok, self.llm_err = False, why
        self._llm_checked = self.clock()

    def _llm_start_env(self) -> dict:
        """At each llama-server start: a new API key, known only to this worker (and kept in state_dir/llm-key, for
        `worker.py check`: the user's own folder, the file the user's only)."""
        key = secrets.token_urlsafe(24)
        if self.llm is not None:
            self.llm.key = key
        try:
            write_secret(Path(self.cfg.state_dir) / "llm-key", key)
        except OSError as e:
            log.debug("llm-key: %s", e)
        return llm_env(key)

    def verify_llm(self) -> bool:
        """The GGUF that llm_server loads (-m) against llm_model_sha256, before llama-server first starts. A mismatch or a
        missing file: no LLM (llama-server never started), said in the log and status."""
        path = self._llm_model_path()
        try:
            st = path.stat()
            got = file_sha256(path, Path(self.cfg.state_dir) / "llm-model.json")
        except OSError as e:
            self.llm_verified, self.llm_err = False, f"model file: {e.strerror or e}"
            log.error("llama-server: can't read its model %s: %s", path.name, e.strerror or e)
            return False
        if got.lower() != self.cfg.llm_model_sha256.lower():
            self.llm_verified, self.llm_err = False, "model hash mismatch"
            log.error("llama-server: %s's SHA-256 isn't llm_model_sha256: not started (download it again)", path.name)
            return False
        self._llm_sig = (st.st_size, st.st_mtime_ns)
        self.llm_verified = True
        log.info("llama-server: model %s verified (sha256 %s...)", path.name, got[:12])
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
               "version": VERSION, "update": self.updater.report()}
        if self.llm is not None:
            doc["llm"] = {"model": self.cfg.llm_model, "state": "ok" if self.llm_ok else self.llm_err,
                          "jobs": dict(self.llm_counts)}
        try:
            write_json(self.cfg.status_path, doc)
        except OSError as e:
            log.debug("status file: %s", e)

    # -- the self-update (Updater)

    def _update_loop(self) -> None:
        while not self.stopped:
            try:
                if self.updater.overdue():
                    if self.updater.rollback(f"no hub answer in {UPDATE_CONFIRM_S / 60:.0f} min"):
                        self.exit_code = UPDATE_EXIT
                        self.stop()
                        return
                elif self.updater.step(self.update_busy):
                    self.drain_and_swap()
            except Exception:  # noqa: BLE001 - the self-update never takes the worker down
                log.exception("self-update: unexpected error (carrying on)")
                self._stop.wait(60)
            self._stop.wait(1.0)

    def update_busy(self) -> str:
        """Why the worker can't switch to a new file now ("" when it can): the swap restarts it, and its servers."""
        if not self.connected:
            return "the hub"
        if self.job is not None:
            return "a clip"
        if self.llm_job is not None or self._llm_call is not None:
            return "an LLM job"
        held = self.held()
        if held is not None and held[0] == "gaming":
            return "gaming"
        if self.clock() - self._gaming_seen < UPDATE_QUIET_S:
            return "gaming just ended" if self.clock() - self.updater.started >= UPDATE_QUIET_S else "just started"
        return ""

    def drain_and_swap(self) -> bool:
        """No new jobs (state paused, "updating"); the clip or LLM job in hand handed back or done (UPDATE_DRAIN_S at
        most; stop() hands back what is left); then the files swapped and the worker stopped with UPDATE_EXIT. A swap
        that fails: back to work."""
        self.draining = True
        self._wake.set()
        log.info("self-update: taking no new jobs")
        end = self.clock() + UPDATE_DRAIN_S
        while not self.stopped and self.clock() < end and (self.job is not None or self.llm_job is not None
                                                           or self._claiming or self._llm_claiming):
            self._stop.wait(0.2)
        if self.stopped:
            return False
        if self.updater.swap():
            self.exit_code = UPDATE_EXIT
            self.stop()
            return True
        self.draining = False
        self._wake.set()
        return False

    # -- the jobs loop

    def run(self) -> None:
        log.info("sizeup STT worker %s: worker %s, model %s, hub %s", VERSION, self.cfg.worker_id, self.cfg.model,
                 urlsplit(self.cfg.hub_url).hostname)
        if self.llm is not None:
            log.info("local LLM: %s at %s", self.cfg.llm_model, self.cfg.llm_url)
            if self.llm_proc is not None and not self.cfg.llm_model_sha256:
                log.warning("llm_model_sha256 isn't set: llama-server's model isn't checked against its pin")
        progs = {"whisper-server": Path(str(self.cfg.whisper_server[0])).name if self.cfg.whisper_server else "",
                 "llama-server": Path(str(self.cfg.llm_server[0])).name if self.cfg.llm_server else ""}
        self.children.reap_leftovers(progs)        # what a worker that died left running (no job object elsewhere)
        loops = [(self._heartbeat_loop, "heartbeat"), (self._monitor_loop, "monitor"), (self._update_loop, "update")]
        if self.llm_verified is None:
            loops.append((self.verify_llm, "llm-verify"))
        if self.llm is not None:
            loops += [(self._llm_loop, "llm"), (self._llm_monitor_loop, "llm-monitor")]
        for target, name in loops + [(self._jobs_loop, "jobs")]:     # (the jobs thread last: run() waits on it)
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
            if self.stopped or self.draining:      # (the self-update's drain began after the state was read)
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
        with self._lock:                       # (with do_infer's: a generation either sees this clip or is told)
            self.job = job
            if job.get("fast") is True and self._llm_call is not None:
                self._preempt.set()            # a pre-alert clip: the LLM's generation steps aside (STT first)
        try:
            self._do_job(job, lease)
        finally:
            with self._lock:
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

    # -- the LLM's jobs (the hub's infer lane: sizeup/cloud/infer.py)

    def _llm_loop(self) -> None:
        """Claims infer jobs while connected, online and llama-server answers. Its own hub connection, so a long poll
        here never holds a clip's; the hub gives it nothing while this worker holds a pre-alert clip."""
        said_old = False
        while not self.stopped:
            try:
                if not (self.connected and self.llm_ok and self.state()[0] == "online") or self.clock() < self._llm_until:
                    self._stop.wait(self.cfg.tick_s)
                    continue
                self.llm_step()
                self.llm_backoff.ok()
            except HubError as e:
                self.llm_hub.close()
                if self.stopped:
                    break
                if e.status == 404:            # a hub from before the infer lane
                    if not said_old:
                        log.info("the hub has no LLM lane (an older hub): asking again every 10 min")
                        said_old = True
                    self._llm_until = self.clock() + 600
                    continue
                wait = self.llm_backoff.fail(e)
                if self.llm_backoff.n == 1:
                    log.warning("llm: %s: trying again in %.0f s", e, wait)
                self.sleep(wait if not e.refused else self.cfg.unauthorized_retry_s)
            except Exception:  # noqa: BLE001 - a bug must not end the LLM's loop
                self.llm_hub.close()
                wait = self.llm_backoff.fail()
                log.exception("llm: unexpected error: trying again in %.0f s", wait)
                self.sleep(wait)

    def llm_step(self) -> None:
        self.llm_hub.interrupted = False
        self._llm_claiming = True
        try:
            if self.stopped or self.draining:
                return
            ans = self.llm_hub.request("POST", f"{API}/infer/claim",
                                       {"wait": self.claim_wait, "state": self.state()[0], "models": self.models()},
                                       timeout=self.claim_wait + 15)
        finally:
            self._llm_claiming = False
        if ans.get("llm_on") is False:         # the pool or this worker's "LLM jobs" switched off on the hub
            if self.llm_on is not False:
                log.info("hub: LLM jobs are off for this worker (Settings > Station > STT workers)")
            self.llm_on = False
            self._llm_until = self.clock() + 60
        elif "llm_on" in ans and self.llm_on is False:
            log.info("hub: LLM jobs are on again")
            self.llm_on = True
        job = ans.get("job")
        if isinstance(job, dict):
            self.do_infer(job)

    def _llm_release(self, lease: str, why: str) -> None:
        try:
            self.llm_hub.request("POST", f"{API}/infer/{lease}/release", {"why": why}, timeout=10)
            self.llm_counts["released"] += 1
        except HubError as e:
            self.llm_counts["lost"] += 1
            if not e.lease_lost:
                log.warning("infer release failed (%s): its deadline ends it on the hub", e)

    def do_infer(self, job: dict) -> None:
        """One classification: llama-server under the job's JSON schema, aborted at once for a pre-alert clip, gaming,
        a pause or the job's deadline. Logs the job id, task, times and token counts only: never the prompt or the
        answer."""
        lease, jid, task = str(job.get("lease") or ""), str(job.get("id") or "")[:16], str(job.get("task") or "")[:40]
        if not re.fullmatch(r"[0-9a-f]{16}", lease):
            log.warning("an infer job without a usable lease: ignored")
            return
        t0 = self.clock()
        deadline = t0 + max(0.0, (_num(job.get("deadline_s")) or 10.0) - 0.3)
        messages, schema = job.get("messages"), job.get("schema")
        state = self.state()[0]
        if state != "online" or self.stopped or self.llm is None:
            self._llm_release(lease, "stopping" if self.stopped else state if state in ("gaming", "paused") else "error")
            return
        if not isinstance(messages, list) or not isinstance(schema, dict):
            self._llm_release(lease, "error")
            return
        call = self.llm.call(messages, schema, min(int(_num(job.get("max_tokens")) or 256), 1024), task,
                             timeout=max(1.0, deadline - t0))
        box: dict = {}

        def generate() -> None:
            try:
                box["out"] = call.run()
            except LlmError as e:
                box["err"] = e
            except Exception as e:  # noqa: BLE001 - a bug here: the job handed back (its type only: no prompt in a trace)
                box["err"] = LlmError(f"unexpected {type(e).__name__}")
            finally:
                with self._lock:
                    self._inferring -= 1
                    self._busy_until = self.clock() + self.cfg.busy_grace_s

        with self._lock:                       # (with do_job's: a pre-alert clip held now, or one that comes, stops it)
            fast = isinstance(self.job, dict) and self.job.get("fast") is True
            if not fast:
                self._preempt.clear()
                self.llm_job, self._llm_call = job, call
                self._inferring += 1
        if fast:
            self._llm_release(lease, "preempted")
            log.info("infer %s %s: a pre-alert clip is being decoded: handed back", jid, task)
            return
        th = threading.Thread(target=generate, name=f"llm-{lease}", daemon=True)
        th.start()
        try:
            while th.is_alive():
                th.join(min(0.05, self.cfg.tick_s))
                if not th.is_alive():
                    break
                state = self.state()[0]
                why = ("stopping" if self.stopped else state if state != "online"
                       else "preempted" if self._preempt.is_set() else "")
                if why:
                    call.abort()               # llama-server stops generating: the GPU is the clip's (or the game's)
                    self._llm_release(lease, why)
                    log.info("infer %s %s: %s: handed back", jid, task, why)
                    return
                if self.clock() >= deadline:
                    call.abort()
                    self.llm_counts["lost"] += 1
                    log.info("infer %s %s: no answer by its deadline: dropped", jid, task)
                    return
        finally:
            self.llm_job, self._llm_call = None, None
        infer_ms = round((self.clock() - t0) * 1000)
        if "err" in box:
            self.llm_fails += 1
            self.llm_counts["failed"] += 1
            log.warning("infer %s %s: %s (%d in a row): handed back", jid, task, box["err"], self.llm_fails)
            self._llm_release(lease, "error")
            if self.llm_fails >= self.cfg.llm_fail_max:
                self._llm_down(f"{self.llm_fails} failures in a row")
            return
        self.llm_fails = 0
        out, tin, tout = box["out"]
        ok = out is not None and schema_valid(out, schema)
        try:
            self.llm_hub.request("POST", f"{API}/infer/{lease}/complete",
                                 {"output": out, "tokens_in": tin, "tokens_out": tout, "infer_ms": infer_ms}, timeout=10)
        except HubError as e:
            if not e.lease_lost and not e.status:
                raise                          # the hub went away: the loop reconnects
            self.llm_counts["lost"] += 1
            log.info("infer %s %s: the hub refused the answer (%s)", jid, task, e.status)
            return
        self.llm_counts["done" if ok else "invalid"] += 1
        log.info("infer %s %s: %d ms, %d+%d tokens%s", jid, task, infer_ms, tin, tout,
                 "" if ok else ", not valid under its schema")

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
        if self.llm_proc is not None:
            self.llm_proc.stop()
        self._write_status(("stopped", ""))
        self.hub.close()
        self.hb_hub.close()
        self.llm_hub.close()
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
        llm = st.get("llm")
        if isinstance(llm, dict):
            lj = llm.get("jobs") or {}
            lines.append(f"llm: {llm.get('model')}, {llm.get('state')}; jobs: "
                         + ", ".join(f"{lj.get(k, 0)} {k}" for k in ("done", "invalid", "released", "lost", "failed")))
        gpu = st.get("gpu")
        if gpu:
            lines.append(f"gpu: {gpu.get('util_pct')}%, {gpu.get('mem_used_mb')}/{gpu.get('mem_total_mb')} MB, "
                         f"{gpu.get('others')} other process(es)")
        up = st.get("update")
        if isinstance(up, dict):
            lines.append(f"update: {up.get('state')}; running {str(up.get('sha256') or '?')[:12]}"
                         + (f" (commit {str(up.get('commit'))[:12]})" if up.get("commit") else ""))
    return "\n".join(lines)


def _update_cli(cfg: Config, what: str, config_path) -> int:
    """`worker.py update`: the self-update's record; `retry`: the hashes marked bad may be tried again; `rollback`:
    worker.py.prev copied back over worker.py by hand (this file's hash marked bad, so it isn't fetched again)."""
    up = Updater(cfg, config_path=config_path)
    doc = up.doc()
    if what == "retry":
        n = up.change(lambda d: len(d.pop("bad", None) or {}))
        print(f"{n} hash(es) marked bad cleared: the worker may try them again (within 10 min)")
        return 0
    if what == "rollback":
        if not up.prev_path.exists():
            print(f"no {up.prev_path.name}: nothing to roll back to")
            return 1
        if not up.rollback("rolled back by hand", manual=True):
            return 1
        print(f"{up.path.name} is {up.prev_path.name} again ({up.own[:12]} marked bad). Restart the worker: "
              + ("schtasks /End /TN sizeup-stt-worker, then schtasks /Run /TN sizeup-stt-worker" if os.name == "nt"
                 else "systemctl --user restart sizeup-stt-worker"))
        return 0
    print(f"running: {up.own[:12] or '?'}" + (f" (commit {up.commit_of(up.own)[:12]})" if up.commit_of(up.own) else ""))
    print(f"auto_update: {'on' if cfg.auto_update else 'off'}")
    st = read_json(cfg.status_path).get("update")
    if isinstance(st, dict) and st.get("state"):
        print(f"state: {st['state']}")
    p = doc.get("pending")
    if isinstance(p, dict):
        print(f"pending: {str(p.get('to'))[:12]} from {str(p.get('from'))[:12]}, {p.get('starts', 0)} start(s), "
              "not confirmed yet")
    last = doc.get("last")
    if isinstance(last, dict):
        print(f"last: {last.get('event')} {str(last.get('from'))[:12]} -> {str(last.get('to') or '')[:12]}"
              + (f" ({last.get('why')})" if last.get("why") else "")
              + (f", {time.strftime('%Y-%m-%d %H:%M', time.localtime(last['at']))}"
                 if isinstance(last.get("at"), (int, float)) else ""))
    for sha, b in up.bad().items():
        print(f"bad: {sha[:12]}" + (f" ({b.get('why')})" if isinstance(b, dict) and b.get("why") else ""))
    if up.prev_path.exists():
        try:
            print(f"{up.prev_path.name}: {hashlib.sha256(up.prev_path.read_bytes()).hexdigest()[:12]}")
        except OSError:
            pass
    return 0


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
    if cfg.llm_url:
        llm = Llm(cfg.llm_url, cfg.llm_timeout_s, cfg.llm_no_think)
        try:                                   # the running worker's llama-server: the key it was started with
            llm.key = (Path(cfg.state_dir) / "llm-key").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        t = time.monotonic()
        try:
            out, tin, tout = llm.check()
            if out != {"ok": True}:
                raise LlmError("llama-server's answer didn't follow the schema")
            print(f"llama-server: ok ({time.monotonic() - t:.1f} s for a constrained answer, {tin}+{tout} tokens)")
        except LlmError as e:
            ok = False
            print(f"llama-server: {e}" + (" (not the worker's llama-server, or the worker restarted it since: its key "
                                           "is in state\\llm-key)" if "401" in str(e) else ""))
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
    u = sub.add_parser("update", parents=[common], help="the self-update: state / retry / rollback")
    u.add_argument("what", nargs="?", choices=("status", "retry", "rollback"), default="status")
    t = sub.add_parser("selftest", parents=[common], help="check this file and the config (no network)")
    t.add_argument("--result", help="write the result (JSON) here")
    args = ap.parse_args(argv)
    config_path = args.config or default_config_path()
    try:
        cfg = load_config(config_path)
        if args.cmd in ("run", "check", "selftest"):
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
    elif args.cmd == "update":
        return _update_cli(cfg, args.what, config_path)
    elif args.cmd == "selftest":
        # what the self-update asks of a new file before it switches to it: it imports, takes this config and builds
        # a Worker (nothing started, no network); the result names this file's hash
        import tempfile
        own = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="sizeup-stt-selftest-") as scratch:
            # what `run` does before its own rollback check runs, in a scratch state_dir (the running worker's lock
            # and logs untouched): the logging, the lock, the update record; then a Worker, nothing started
            test = dataclasses.replace(cfg, state_dir=scratch, log_file=str(Path(scratch) / "worker.log"))
            setup_logging(test)
            lock = InstanceLock(test.state_dir)
            if not lock.acquire():
                raise RuntimeError("the lock in a scratch folder")
            Updater(test).boot()
            Worker(test, updater=Updater(test))._state_body()
            lock.release()
            for h in list(log.handlers):
                log.removeHandler(h)
                h.close()
        doc = {"ok": True, "protocol": SELFTEST_PROTOCOL, "sha256": own, "version": VERSION}
        if args.result:
            write_json(Path(args.result), doc)
        else:
            print(json.dumps(doc))
        return 0
    elif args.cmd == "run":
        setup_logging(cfg)
        lock = InstanceLock(cfg.state_dir)
        if not lock.acquire():
            log.error("another worker (pid %s) is running on %s: not starting a second one", lock.holder(),
                      cfg.state_dir)
            return 3
        updater = Updater(cfg, config_path=config_path)
        if updater.boot():                 # a new file that never reached the hub: the old one is back; it starts next
            lock.release()
            return UPDATE_EXIT
        worker = Worker(cfg, updater=updater)

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
        if worker.exit_code:
            log.info("exiting %d: the new worker.py starts next (Task Scheduler / systemd)", worker.exit_code)
        return worker.exit_code
    print(_status_text(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
