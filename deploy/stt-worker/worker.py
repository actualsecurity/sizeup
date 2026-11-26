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
deploy/stt-worker/README.md."""

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
LLM_PRIORITIES = ("keep", "yield")          # llm_priority: the LLM keeps its VRAM, or gives it to the speech models
# every speech model is told English, never left to detect a language (a short noisy clip came out in another one):
# language_setting below is the one place that says how, for today's whisper_server and every catalog model alike
STT_LANGUAGE = "en"
ASR_LANGUAGE_NAMES = {"en": "English"}     # Qwen3-ASR's own names for a forced language
# whisper-server's decode, sent with every /inference besides the language: whisper.cpp b5454 takes each as a request
# field (server.cpp get_req_parameters), so they hold whatever an install's whisper_server line says. Non-speech tokens
# suppressed, and the temperature fallback's thresholds (a looping or unsure decode is tried again warmer; no speech:
# nothing)
WHISPER_FIELDS = (("suppress_nst", "true"), ("entropy_thold", "2.4"), ("logprob_thold", "-1.0"),
                  ("no_speech_thold", "0.6"))
TEMPERATURE_INC = "0.2"                    # a full decode: fallback at 0.2, 0.4 ... 1.0 (up to 5 more passes)
FAST_TEMPERATURE_INC = "0.4"               # a pre-alert (fast) one: at 0.4 and 0.8 only (up to 2 more, its lease short)
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
    log_ship: bool = True                  # worker.log's lines to the hub too (CloudWatch /sizeup/stt-worker): LogShip
    # the catalog's models (models.json stt_models; the hub says which): each on a server of its own beside whisper_server
    managed_models: bool = True            # false: only whisper_server (and the LLM), as before
    models_dir: str = "models"             # their files go in <models_dir>/managed (downloaded, checked, pruned)
    whisper_bin: str = ""                  # whisper-server for whisper-ggml models (default: whisper_server's program)
    llama_bin: str = ""                    # llama-server for llama-asr models (default: llm_server's program)
    catalog_file: str = ""                 # a local models.json instead of the hub's (testing); "" the hub's, from GitHub
    vram_budget_mb: float = 0.0            # VRAM for all the servers; 0: the GPU's total less vram_reserve_mb
    vram_reserve_mb: float = 1536.0        # left for Windows, the desktop, a browser
    llm_vram_mb: float = 0.0               # the LLM's share; 0: its GGUF's size and a context's worth
    primary_vram_mb: float = 1000.0        # whisper_server's, when its model isn't in the catalog
    gate_dbfs: float = -55.0               # a clip whose speech level is under this isn't decoded: no speech (every
                                           # model makes words up on silence); -120 never
    llm_priority: str = "yield"            # the local LLM vs a catalog model that doesn't fit beside it: "yield" (speech
                                           # first: the LLM stops for it, the hub hears llm unavailable and intel falls
                                           # back to its rules; back when there is room) or "keep" (the LLM stays; that
                                           # model isn't loaded, the hub hears it unavailable and the channel keeps its
                                           # champion/route). The hub's stt_models llm_priority, when it sends one, wins
    base_dir: str = dataclasses.field(default="", repr=False)  # the config file's folder (not a key)
    ignored_keys: list = dataclasses.field(default_factory=list, repr=False)  # keys this file doesn't know (not a key)

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
    keys = {f.name for f in dataclasses.fields(Config)} - {"base_dir", "ignored_keys"}
    # a key this file doesn't know is warned of and ignored, never refused: a worker.py rolled back (or not yet
    # updated) still starts with a worker.toml that a newer one's key was added to. A known key's bad value is refused.
    unknown = sorted(str(k)[:40] for k in set(doc) - keys)
    cfg = Config(base_dir=base, ignored_keys=unknown)
    for key, value in doc.items():
        if key in keys:
            setattr(cfg, key, _coerce(key, value, getattr(cfg, key)))
    cfg.llm_priority = str(cfg.llm_priority).strip().lower()
    if cfg.llm_priority not in LLM_PRIORITIES:
        raise ConfigError(f"llm_priority: {cfg.llm_priority[:20]}: one of {', '.join(LLM_PRIORITIES)}")
    for key, var in ENV.items():
        if env.get(var):
            setattr(cfg, key, env[var].strip())
    for key in ("token_file", "state_dir", "log_file", "models_dir", "catalog_file"):
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
    if cfg.whisper_server:
        check_whisper_argv(cfg.whisper_server)
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
    if _check_argv("llm_server", argv, LLM_ARGS) != 1:
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


# whisper-server's options the worker lets through in whisper_server (name: takes a value), and nothing else: one local
# model, loopback only, English only, the decode's thresholds, VAD with a local model; no -nf (no temperature fallback:
# a weak clip's loops kept), no -dl / translate / diarize, no public path, no ffmpeg converter, no other request path
WHISPER_ARGS = {"-m": True, "--model": True, "--host": True, "--port": True, "-t": True, "--threads": True,
                "-l": True, "--language": True, "-sns": False, "--suppress-nst": False, "-et": True,
                "--entropy-thold": True, "-lpt": True, "--logprob-thold": True, "-nth": True, "--no-speech-thold": True,
                "-mc": True, "--max-context": True, "--vad": False, "-vm": True, "--vad-model": True, "-vt": True,
                "--vad-threshold": True}


def _check_argv(what: str, argv: list, allowed: dict) -> int:
    """argv: a program, then only `allowed` options (as "-x v" or "--x=v"). The number of -m / --model; ConfigError
    otherwise."""
    models, i = 0, 1
    while i < len(argv):
        name, eq, _value = argv[i].partition("=")
        if name not in allowed:
            raise ConfigError(f"{what}: {argv[i][:40]} not allowed (allowed: {', '.join(sorted(allowed))})")
        if allowed[name] and not eq:
            if i + 1 >= len(argv):
                raise ConfigError(f"{what}: {name} needs a value")
            i += 1
        elif eq and not allowed[name]:
            raise ConfigError(f"{what}: {name} takes no value")
        models += name in ("-m", "--model")
        i += 1
    return models


def check_whisper_argv(argv: list) -> None:
    """whisper_server: a program, then only WHISPER_ARGS, at most one -m, and a language only if it is en. ConfigError
    otherwise (--host loopback only: validate's _bad_host)."""
    argv = [str(a) for a in argv]
    if _check_argv("whisper_server", argv, WHISPER_ARGS) > 1:
        raise ConfigError("whisper_server: one -m <the model file>")
    lang = _arg(argv, ("-l", "--language"))
    if lang is not None and lang != STT_LANGUAGE:
        raise ConfigError(f"whisper_server: -l {lang[:20]}: English only (-l {STT_LANGUAGE})")


def language_setting(kind: str, lang: str = STT_LANGUAGE) -> dict:
    """How a speech model of this kind is told its language (the one place; today's whisper_server is a whisper-ggml):
    whisper-server gets `-l en` (whisper_command) and every /inference request language=en (Whisper.call); a llama-asr
    model (Qwen3-ASR) gets its answer begun as "language English<asr_text>", the way Qwen3-ASR's own toolkit forces a
    language (an assistant prefill)."""
    if kind == "whisper-ggml":
        return {"argv": ["-l", lang], "request": lang, "prefill": ""}
    return {"argv": [], "request": "", "prefill": f"language {ASR_LANGUAGE_NAMES[lang]}<asr_text>"}


def whisper_command(argv: list) -> list:
    """A whisper-server command line as the worker runs it (whisper_server, and a catalog model's): language_setting's
    -l en when it names no language (each request says language=en too)."""
    argv = [str(a) for a in argv]
    if _arg(argv, ("-l", "--language")) is None:
        argv += language_setting("whisper-ggml")["argv"]
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
    if cfg.ignored_keys:
        log.warning("config: ignoring keys this worker.py doesn't know: %s", ", ".join(cfg.ignored_keys[:20]))

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
                raw: bool = False, data: bytes | None = None, extra: dict | None = None):
        """A JSON object in and out (raw: the body's bytes). data: a body already encoded (JSON, with extra headers
        such as Content-Encoding: gzip). HubError on anything but a 2xx JSON object."""
        data = dumps(body) if body is not None else data
        headers = dict(self._headers, **({"Content-Type": "application/json"} if data is not None else {}),
                       **(extra or {}))
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
    """whisper.cpp's whisper-server, called as the station's WhisperClient does (sizeup/stt.py): a multipart POST to
    /inference with response_format=verbose_json, temperature=0.0, language=en (language_setting), WHISPER_FIELDS, the
    prompt (the hub sends none for the airband), temperature_inc TEMPERATURE_INC, or FAST_TEMPERATURE_INC for a fast
    (pre-alert) job: a capped fallback, never none (a greedy pass alone kept a weak clip's loops), and the WAV as
    file=tx.wav. The same for today's whisper_server and a catalog model's. The hub's WAV is already padded and
    processed: it goes as it is."""

    def __init__(self, url: str, timeout_s: float = 60.0):
        self.url, self.timeout_s = url, timeout_s
        u = urlsplit(url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self.path = u.path or "/inference"

    def call(self, wav: bytes, prompt: str = "", fast: bool = False, timeout: float | None = None) -> WhisperCall:
        boundary = uuid.uuid4().hex
        fields = [("response_format", "verbose_json"), ("temperature", "0.0"),
                  ("language", language_setting("whisper-ggml")["request"]), *WHISPER_FIELDS,
                  ("temperature_inc", FAST_TEMPERATURE_INC if fast else TEMPERATURE_INC)]
        if prompt:
            fields.append(("prompt", prompt))
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
            progs = {p.lower().removesuffix(".exe") for p in ((prog,) if isinstance(prog, str) else prog or ()) if p}
            if progs and isinstance(pid, int) and pid > 0:
                found = process_name(pid, self.run)
                if found and found.lower().removesuffix(".exe") in progs:
                    log.warning("%s (pid %s) was left running by a worker that ended: stopping it", name, pid)
                    kill_pid(pid, self.run)
                    ended.append(pid)
            self.set(name, None)
        return ended


_SERVER_LOGS_LOCK = threading.Lock()


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
        """The server's own log (rotated). One file handler per logger, whatever number of WhisperProcess objects
        share its name (a catalog model's server is a new one at each start): the one already on this file is kept,
        any other closed and taken off (two handlers on one file: every line twice, and Windows can't rotate it)."""
        if self._out is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            out = logging.getLogger("sizeup.stt_worker." + self.name.replace("-", "_"))
            out.propagate = False
            out.setLevel(logging.INFO)
            want = os.path.abspath(os.fspath(self.log_path))
            with _SERVER_LOGS_LOCK:
                kept = None
                for h in list(out.handlers):
                    if kept is None and isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == want:
                        kept = h
                        continue
                    out.removeHandler(h)
                    h.close()
                if kept is None:
                    kept = logging.handlers.RotatingFileHandler(self.log_path, maxBytes=1_000_000, backupCount=2,
                                                                encoding="utf-8")
                    kept.setFormatter(logging.Formatter("%(message)s"))
                    out.addHandler(kept)
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


def run_out_err(argv: list) -> str:
    """run_cmd, with stderr after stdout (llama-server writes its --version there); no check of the exit code."""
    p = subprocess.run(argv, capture_output=True, text=True, timeout=15, env=child_env(), errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (p.stdout or "") + (p.stderr or "")


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
        util, used, total, biggest = 0.0, 0.0, 0.0, 0.0
        for line in gpus.strip().splitlines():
            vals = [_num(x) for x in line.split(",")]
            if len(vals) >= 3:
                util = max(util, vals[0] or 0.0)
                used += vals[1] or 0.0
                total += vals[2] or 0.0
                biggest = max(biggest, vals[2] or 0.0)
        ours = self.ours()
        others: list[float | None] = []
        for line in apps.strip().splitlines():
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < 2 or not cols[0].isdigit() or int(cols[0]) in ours:
                continue
            others.append(_num(cols[1]))     # None: "[N/A]" (Windows WDDM doesn't report it)
        self.info = {"util_pct": round(util), "mem_used_mb": round(used), "mem_total_mb": round(total),
                     "others": len(others)}
        if biggest and biggest != total:
            self.info["mem_max_mb"] = round(biggest)        # (more than one GPU: the speech models' budget is one's)
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
UPDATE_FILE = "deploy/stt-worker/worker.py"     # where this file is on main
UPDATE_FILE_BEFORE = "tools/stt_worker/worker.py"  # where it was before it moved to deploy/: a commit from before the
                                                   # move has it only there (a pin to an older version); one from the
                                                   # move on has both (tools/stt_worker/worker.py a byte-identical
                                                   # copy, for the workers that know only that path) until it goes
UPDATE_FILES = (UPDATE_FILE, UPDATE_FILE_BEFORE)   # tried in this order; the next one only when GitHub says 404
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
    hub named. UpdateError otherwise (final when the answer itself is wrong). UPDATE_FILE first; UPDATE_FILE_BEFORE
    only when GitHub has no UPDATE_FILE at that commit (404: a commit from before the move). Never another path for a
    file that is there but wrong (its hash, its size) or for GitHub out of reach."""
    for path in UPDATE_FILES:
        status, headers, body = fetch(GITHUB_RAW, f"/{UPDATE_REPO}/{commit}/{path}", UPDATE_MAX_BYTES)
        if status != 404:
            break
    else:
        raise UpdateError(f"no worker.py at {commit[:12]}", final=True)
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


# --- the worker's own log to the hub, then CloudWatch (/sizeup/stt-worker) ----------------------------------------

LOGS_PATH = f"{API}/logs"
LOG_BATCH_S = 60.0                         # a batch at most this often ...
LOG_BATCH_LINES = 1000                     # ... or as soon as this many lines wait ...
LOG_BATCH_BYTES = 200_000                  # ... or this many bytes (uncompressed; the hub takes 1 MB gzipped)
LOG_MEM_LINES = 5000                       # waiting in memory at most (the spool unwritable): the oldest dropped first
LOG_LINE_MAX = 2000                        # a longer line is cut (after the scrub: a secret is never half kept)
LOG_SPOOL_MAX_BYTES = 5_000_000            # logspool/ on disk at most (gzipped: days of an ordinary worker's log) ...
LOG_SPOOL_MAX_FILES = 500                  # ... and this many batches: the oldest deleted first
LOG_SEND_A_TURN = 20                       # batches sent in one go, at most
LOG_RETRY_S = (30.0, 60.0, 120.0, 300.0, 600.0)   # the hub unreachable / 5xx: waits in a row, then the last one
LOG_NOT_THERE_S = 3600.0                   # a hub without the route (404, one from before it): asked again this often
LOG_REJECT = (400, 413, 415, 422)          # with reason LOG_REJECTED (the hub's logs sink itself said so): the hub will
LOG_REJECTED = "logs_rejected"             # never take this batch: deleted (counted), never sent again. Any other 4xx
                                           # (a hub from before the route parses the gzip as JSON: 400): kept, later
LOG_UNREADABLE_MAX = 5                     # a spool file that can't be read this many times in a row: deleted
LOG_LEVELS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warning", "ERROR": "error", "CRITICAL": "error"}

# defence in depth: worker.log never holds a credential (Redact replaces the known values; nothing logs a header), and
# these shapes are redacted from every line before it is spooled; the hub scrubs again (sizeup/logscrub.py)
_SCRUB = (
    (re.compile(r"\b(s[wz]1\.[a-z0-9][a-z0-9_-]{0,39}\.)[A-Za-z0-9_-]+"), r"\1[redacted]"),   # a worker / station token
    (re.compile(r"(?i)\b(bearer|basic|digest)(\s+)[^\s,;'\"]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)((?:cf-access-client-(?:id|secret)|cf_authorization|authorization|x-api-key|api[_-]?key|"
                r"client[_-]?secret|secret|token|password|passwd|pwd)[\"']?\s*[:=]\s*[\"']?)[^\s,;'\"}\]]+"),
     r"\1[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted]"),     # a JWT
    (re.compile(r"\b[0-9a-f]{32}\.access\b"), "[redacted]"),            # a Cloudflare service token's client id
    (re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{48,}(?![0-9A-Fa-f])"), "[redacted]"),   # its secret (64 hex); the
    # worker logs hashes and commits cut to 12
    (re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*"), "[redacted]"),
)
# 32+ characters of a token alphabet with upper case, lower case and a digit: a secret's shape
_LONG = re.compile(r"(?<![A-Za-z0-9/+_=-])[A-Za-z0-9/+_=-]{32,}(?![A-Za-z0-9/+_=-])")


def scrub_line(text: str, known: list[str] | tuple = ()) -> str:
    """A log line as it may leave the PC: every known secret (the token, its secret part, the Cloudflare secret) and
    every token-like string redacted, then cut to LOG_LINE_MAX."""
    for s in known:
        if s and s in text:
            text = text.replace(s, "[redacted]")
    for rx, to in _SCRUB:
        text = rx.sub(to, text)
    text = _LONG.sub(lambda m: "[redacted]" if re.search(r"[a-z]", m.group()) and re.search(r"[A-Z]", m.group())
                     and re.search(r"[0-9]", m.group()) else m.group(), text)
    return text if len(text) <= LOG_LINE_MAX else text[:LOG_LINE_MAX] + " [cut]"


class LogShip(logging.Handler):
    """worker.log's lines to the hub (POST /api/station/stt/logs, the worker token and the Cloudflare service token,
    gzipped), which writes them to CloudWatch Logs (/sizeup/stt-worker, a stream per worker). The same records as
    worker.log and only those: this logger's, INFO and up (whisper-server's and llama-server's output have loggers
    of their own that never reach it), so never a transcript, a prompt, an LLM answer or a token; scrub_line on top.

        take     emit(): formatted (its traceback too), scrubbed, kept in memory (LOG_MEM_LINES at most): never blocks
                 the thread that logs, never raises
        spool    a batch every LOG_BATCH_S, or at LOG_BATCH_LINES / LOG_BATCH_BYTES, as one gzipped JSON file in
                 <state_dir>/logspool (LOG_SPOOL_MAX_BYTES / LOG_SPOOL_MAX_FILES at most, the oldest deleted first,
                 counted); also at the worker's stop, so a restart (a self-update) sends it
        send     its own thread and hub connection, the oldest batch first: {v, worker, batch, lines: [[ms, level,
                 msg]]}. Deleted once the hub answers accepted; kept and sent again otherwise: the hub not set up
                 ("not configured", its retry_s), unreachable or 5xx (LOG_RETRY_S), refused (unauthorized_retry_s),
                 a hub without the route (LOG_NOT_THERE_S). A 400 / 413 / 415 / 422: deleted, counted, never resent
        off      log_ship = false in worker.toml: nothing kept, nothing sent (worker.log as before)"""

    def __init__(self, cfg: Config, hub: "Hub | None" = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(message)s"))
        self.cfg, self.clock, self.wall = cfg, clock, wall
        self.known = sorted(set(cfg.secrets()), key=len, reverse=True)
        self.hub = hub if hub is not None else Hub(cfg.hub_url, cfg.token, cfg.cf_client_id, cfg.cf_client_secret,
                                                   timeout=20.0)
        self.dir = Path(cfg.state_dir) / "logspool"
        self._mu = threading.Lock()
        self._spool_mu = threading.Lock()           # one spool at a time (the log thread's and the stop's)
        self._unreadable = 0
        self._buf: list[list] = []                  # [ms, level, msg]
        self._buf_bytes = 0
        self._spooled_at = clock()
        self._seq = 0
        self.next_send = 0.0
        self.fails = 0
        self.dropped = 0                            # lines or batches lost to a cap (counted, never silent)
        self.rejected = 0
        self.sent_lines = 0
        self.last_ok: float | None = None           # wall time of the last batch the hub accepted
        self.state = "starting"
        self._said = ""

    # -- take (any thread)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = scrub_line(self.format(record), self.known)
            row = [int(record.created * 1000), LOG_LEVELS.get(record.levelname, "info"), line]
            with self._mu:
                self._buf.append(row)
                self._buf_bytes += len(line) + 24
                while len(self._buf) > LOG_MEM_LINES:
                    old = self._buf.pop(0)
                    self._buf_bytes -= len(old[2]) + 24
                    self.dropped += 1
        except Exception:  # noqa: BLE001 - logging must never fail the worker (nor print to a console it hasn't)
            pass

    def due(self) -> bool:
        with self._mu:
            n, size = len(self._buf), self._buf_bytes
        return bool(n) and (n >= LOG_BATCH_LINES or size >= LOG_BATCH_BYTES
                            or self.clock() - self._spooled_at >= LOG_BATCH_S)

    # -- spool (the shipper's thread; and the worker's stop)

    def _files(self) -> list[Path]:
        try:
            return sorted(p for p in self.dir.glob("*.json.gz") if p.is_file())
        except OSError:
            return []

    def spool(self) -> bool:
        """What waits in memory to one batch file per LOG_BATCH_LINES. False (kept in memory) when it can't be written."""
        with self._spool_mu:
            return self._spool()

    def _spool(self) -> bool:
        self._spooled_at = self.clock()
        while True:
            with self._mu:
                rows, size = [], 0
                for r in self._buf:
                    if rows and (len(rows) >= LOG_BATCH_LINES or size + len(r[2]) + 24 > LOG_BATCH_BYTES):
                        break
                    rows.append(r)
                    size += len(r[2]) + 24
            if not rows:
                return True
            files = self._files()           # (named in order: after what an earlier start left)
            self._seq = max([self._seq] + [int(p.name.split(".")[0]) for p in files if p.name.split(".")[0].isdigit()]) + 1
            doc = {"v": 1, "worker": self.cfg.worker_id, "batch": f"{int(self.wall())}-{self._seq}-{secrets.token_hex(3)}",
                   "lines": rows}
            data = gzip_bytes(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                write_file(self.dir / f"{self._seq:012d}.json.gz", data)
            except OSError as e:
                if self._said != "spool":
                    self._said = "spool"
                    log.warning("logs: can't write %s (%s): kept in memory", self.dir.name, e.strerror or e)
                return False
            with self._mu:
                del self._buf[:len(rows)]
                self._buf_bytes = sum(len(r[2]) + 24 for r in self._buf)
            self._cap()

    def _cap(self) -> None:
        files = self._files()
        sizes = []
        for p in files:
            try:
                sizes.append(p.stat().st_size)
            except OSError:
                sizes.append(0)
        total = sum(sizes)
        while files and (total > LOG_SPOOL_MAX_BYTES or len(files) > LOG_SPOOL_MAX_FILES):
            p, n = files.pop(0), sizes.pop(0)
            try:
                p.unlink()
            except OSError:
                break
            total -= n
            self.dropped += 1

    # -- send (the shipper's thread)

    def send(self) -> str:
        """The oldest batches to the hub, while it accepts them. What happened, in words (for the tests and status)."""
        if self.clock() < self.next_send:
            return "waiting"
        for _ in range(LOG_SEND_A_TURN):
            files = self._files()
            if not files:
                return "empty"
            p = files[0]
            try:
                data = p.read_bytes()
                self._unreadable = 0
            except OSError as e:
                self._unreadable += 1
                if self._unreadable >= LOG_UNREADABLE_MAX:          # (never one file in the way of all the others)
                    self._unlink(p)
                    self._unreadable = 0
                    self.dropped += 1
                    self._say(f"dropped {p.name}: can't read it ({e.strerror or e})", logging.WARNING)
                    continue
                return self._wait(LOG_RETRY_S[0], f"can't read {p.name}")
            try:
                ans = self.hub.request("POST", LOGS_PATH, data=data, extra={"Content-Encoding": "gzip"})
            except HubError as e:
                self.hub.close()
                if e.status in LOG_REJECT and e.reason == LOG_REJECTED:
                    self._unlink(p)
                    self.rejected += 1
                    self._say(f"rejected: hub said {e.status}", logging.WARNING)
                    continue
                if e.status == 404 or 400 <= e.status < 500 and e.status not in (401, 403, 429):
                    # a hub without the route: 404, or 400 from one from before it (it reads the gzip as JSON)
                    return self._wait(LOG_NOT_THERE_S, "the hub doesn't take logs yet")
                if e.refused:
                    return self._wait(self.cfg.unauthorized_retry_s, f"refused ({e.reason or e.status})")
                self.fails += 1
                wait = LOG_RETRY_S[min(self.fails - 1, len(LOG_RETRY_S) - 1)]
                if e.retry_after is not None:
                    wait = min(max(e.retry_after, LOG_RETRY_S[0]), LOG_RETRY_S[-1])
                return self._wait(wait, "hub unreachable" if not e.status else f"hub said {e.status}")
            if ans.get("accepted") is True:
                self._unlink(p)
                self.fails = 0
                self.last_ok = self.wall()
                self.sent_lines += int(_num(ans.get("n")) or 0)
                self.state = "ok"
                self._said = ""
                continue
            why = " ".join(str(ans.get("reason") or "not accepted").split())[:120]
            retry = _num(ans.get("retry_s"))
            return self._wait(min(max(retry or 600.0, LOG_RETRY_S[0]), 6 * 3600.0), why)
        return "more"

    def _unlink(self, p: Path) -> None:
        try:
            p.unlink()
        except OSError:
            pass

    def _wait(self, seconds: float, why: str) -> str:
        self.next_send = self.clock() + seconds
        self.state = f"not shipped: {why}"
        self._say(f"not shipped ({why}): kept on this PC, trying again in {seconds / 60:.0f} min", logging.INFO)
        return why

    def _say(self, what: str, level: int) -> None:
        if self._said != what:                      # said once (each line said is itself a line to ship)
            self._said = what
            log.log(level, "logs: %s", what)

    def step(self) -> None:
        if self.due():
            self.spool()
        self.send()

    def loop(self, stopped: Callable[[], bool], wait: Callable[[float], object]) -> None:
        """The shipper's thread: never in the way of the clips (its own connection; a bug only logged)."""
        while not stopped():
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("logs: unexpected error (carrying on)")
                self.next_send = self.clock() + LOG_RETRY_S[-1]
            wait(1.0)

    def close_spool(self) -> None:
        """At the stop: what is left in memory to disk (the next start sends it)."""
        try:
            self.spool()
        except Exception:  # noqa: BLE001
            pass
        self.hub.close()

    def stats(self) -> dict:
        files = self._files()
        with self._mu:
            waiting = len(self._buf)
        return {"state": self.state, "last_ok": self.last_ok, "batches": len(files), "waiting": waiting,
                "dropped": self.dropped, "rejected": self.rejected}


def gzip_bytes(data: bytes) -> bytes:
    import gzip
    return gzip.compress(data, compresslevel=6, mtime=0)


# --- speech-to-text models: the catalog (models.json), managed downloads, a server each -------------------------------

CATALOG_PATH = "deploy/stt-worker/models.json"  # never the hub's to choose: main of UPDATE_REPO, at the commit it names
CATALOG_KEY = "stt_models"
CATALOG_MAX_BYTES = 256_000
# whisper-ggml: whisper.cpp's whisper-server, one ggml file; context as whisper's initial prompt.
# llama-asr: llama.cpp's llama-server with an audio model (a GGUF and its mmproj, e.g. Qwen3-ASR); context as a system
# message (a short comma list), the audio as an input_audio part of /v1/chat/completions.
MODEL_KINDS = {"whisper-ggml": ("model",), "llama-asr": ("model", "mmproj")}
CONTEXT_FORMS = {"whisper-ggml": "prompt", "llama-asr": "list"}
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
MODEL_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(?:bin|gguf)")
MODEL_SERVICES = ("fire", "ems", "police", "air", "transit", "other")
MODEL_MAX_BYTES = 8_000_000_000
VRAM_MIN_MB, VRAM_MAX_MB = 100, 48_000
CONTEXT_MAX_ITEMS = 12                     # a llama-asr model's context list (Qwen3-ASR: short declarative lists help)
# Where a model file may come from (nothing else, whatever a catalog says): Hugging Face at a pinned revision (a
# 40-digit commit, never a branch name), a ggml-org release asset, or a release asset of this repo (a model built
# once by a pinned recipe: its SHA-256 is the pin). Redirects (the CDNs behind them) only to REDIRECT_HOSTS.
_SEG = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
DOWNLOAD_RULES = (
    ("huggingface.co", re.compile(rf"/{_SEG}/{_SEG}/resolve/[0-9a-f]{{40}}/{_SEG}(?:/{_SEG}){{0,3}}")),
    ("github.com", re.compile(rf"/(?:ggml-org/{_SEG}|{re.escape(UPDATE_REPO)})/releases/download/{_SEG}/{_SEG}")),
)
REDIRECT_HOSTS = {"huggingface.co": (("huggingface.co",), (".hf.co", ".huggingface.co")),
                  "github.com": (("release-assets.githubusercontent.com", "objects.githubusercontent.com"), ())}
REDIRECTS_MAX = 5
MODEL_PORT_FIRST = 8181                    # 8179 the configured whisper-server, 8180 llama-server (the LLM)
MODEL_PORTS = 8
MODEL_CHECK_S = 180.0                      # a new server answers a test decode this soon, else it is stopped and that
                                           # model's hash marked bad (the version it replaced, kept, runs again)
MODEL_BAD_S = 6 * 3600.0                   # a hash marked bad is tried again after this
MODEL_RETRY_S = 1800.0                     # a download that failed another way (the network): again after this
MODEL_SHORT_RETRY_S = 300.0                # a server that failed to load: again after this, doubling each time (to
                                           # MODEL_RETRY_S): out of VRAM or a port another program holds is never the
                                           # model's fault; another failure is, after MODEL_BAD_TRIES
MODEL_BAD_TRIES = 3                        # one hash's server exits or never answers this many times in a row: bad
MODEL_LOG_TAIL = 65536                     # what of a failed server's own log is read to tell why (never logged)
# why a server exited, from its own output: CUDA/Metal/Vulkan out of memory, or its port taken
SERVER_OOM = re.compile(r"out of memory|cudaMalloc failed|failed to allocate|unable to allocate|"
                        r"ErrorOutOfDeviceMemory|insufficient memory|cudaErrorMemoryAllocation", re.I)
SERVER_PORT_TAKEN = re.compile(r"address already in use|couldn't bind|could not bind|failed to bind|bind failed|"
                               r"only one usage of each socket address|WSAEADDRINUSE|error 10048", re.I)
CATALOG_CHECK_S = 600.0                    # GitHub at most this often for a new catalog
DISK_MARGIN = 1_000_000_000                # free space left after a download, at least
DOWNLOAD_CHUNK = 1 << 20
MANAGED_DIR = "managed"                    # models_dir/managed/<sha256[:16]>-<file>: the only files ever deleted
LLAMA_VERSION = re.compile(r"build (\d+)|version:\s*(\d+)\s*\(")
GATE_BLOCK_S = 0.02


class CatalogError(ValueError):
    pass


def model_url_ok(url) -> bool:
    """An https URL of one of DOWNLOAD_RULES' shapes: no port, query, user, empty or dot segment, escapes."""
    if not isinstance(url, str) or len(url) > 400:
        return False
    u = urlsplit(url)
    if u.scheme != "https" or u.port not in (None, 443) or u.query or u.fragment or u.username or u.password:
        return False
    if "%" in u.path or "\\" in u.path or any(s in ("", ".", "..") for s in u.path.split("/")[1:]):
        return False
    return any(u.hostname == host and rx.fullmatch(u.path) for host, rx in DOWNLOAD_RULES)


def redirect_ok(origin: str, url: str) -> bool:
    """A download from `origin` (a DOWNLOAD_RULES host) may follow a redirect to this URL: https, a known CDN."""
    u = urlsplit(url)
    exact, suffixes = REDIRECT_HOSTS.get(origin, ((), ()))
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and u.port in (None, 443) and not u.username and not u.password
            and (host in exact or any(host.endswith(s) for s in suffixes)))


def _model_file(v, what: str) -> dict:
    """{file, url, bytes, sha256} of one download, checked."""
    if not isinstance(v, dict):
        raise CatalogError(f"{what}: an object")
    if not isinstance(v.get("file"), str) or not MODEL_FILE.fullmatch(v["file"]):
        raise CatalogError(f"{what}: file (a .bin or .gguf name)")
    if not model_url_ok(v.get("url")):
        raise CatalogError(f"{what}: url not allowed (Hugging Face at a pinned revision, or a release asset of "
                           f"ggml-org or {UPDATE_REPO})")
    n = v.get("bytes")
    if not isinstance(n, int) or isinstance(n, bool) or not 0 < n <= MODEL_MAX_BYTES:
        raise CatalogError(f"{what}: bytes")
    if not isinstance(v.get("sha256"), str) or not SHA256.fullmatch(v["sha256"]):
        raise CatalogError(f"{what}: sha256 (64 lower-case hex digits)")
    return {"file": v["file"], "url": v["url"], "bytes": n, "sha256": v["sha256"]}


def check_catalog(doc) -> dict:
    """models.json's stt_models, checked whole: {id: entry}. One entry that breaks a rule refuses the catalog (CI checks
    the repo's with this same function, so such a catalog never reaches main). An entry's `recipe` (how a file was
    built: pinned inputs and tools) is the people's record; the worker only ever downloads the pinned file."""
    if not isinstance(doc, dict) or not isinstance(doc.get(CATALOG_KEY), dict):
        raise CatalogError(f"no {CATALOG_KEY}")
    out: dict = {}
    names: set = set()
    for mid, e in doc[CATALOG_KEY].items():
        if mid.startswith("_"):
            continue                                       # ("_what": a note)
        if not MODEL_ID.fullmatch(mid):
            raise CatalogError(f"{mid[:50]!r}: not a model id")
        if not isinstance(e, dict) or e.get("kind") not in MODEL_KINDS:
            raise CatalogError(f"{mid}: kind ({', '.join(MODEL_KINDS)})")
        files = e.get("files")
        if not isinstance(files, dict) or set(files) != set(MODEL_KINDS[e["kind"]]):
            raise CatalogError(f"{mid}: files: exactly {', '.join(MODEL_KINDS[e['kind']])}")
        fs = {role: _model_file(files[role], f"{mid}: files.{role}") for role in MODEL_KINDS[e["kind"]]}
        for f in fs.values():
            if f["file"] in names:
                raise CatalogError(f"{mid}: {f['file']}: another model's file name")
            names.add(f["file"])
        vram = e.get("vram_mb")
        if not isinstance(vram, int) or isinstance(vram, bool) or not VRAM_MIN_MB <= vram <= VRAM_MAX_MB:
            raise CatalogError(f"{mid}: vram_mb")
        langs, services = e.get("langs"), e.get("services")
        if not isinstance(langs, list) or not langs or not all(isinstance(x, str) and re.fullmatch(r"[a-z]{2,5}", x)
                                                                for x in langs):
            raise CatalogError(f"{mid}: langs")
        if STT_LANGUAGE not in langs:
            raise CatalogError(f"{mid}: langs: no {STT_LANGUAGE!r} (every model is told {STT_LANGUAGE!r})")
        if not isinstance(services, list) or not set(services) <= set(MODEL_SERVICES):
            raise CatalogError(f"{mid}: services ({', '.join(MODEL_SERVICES)})")
        if not isinstance(e.get("context_default", True), bool):
            raise CatalogError(f"{mid}: context_default: true or false")
        rmin = e.get("runtime_min", 0)
        if not isinstance(rmin, int) or isinstance(rmin, bool) or rmin < 0:
            raise CatalogError(f"{mid}: runtime_min (a llama.cpp build number)")
        if "recipe" in e and not isinstance(e["recipe"], dict):
            raise CatalogError(f"{mid}: recipe: an object")
        out[mid] = {"id": mid, "kind": e["kind"], "files": fs, "vram_mb": vram, "langs": list(langs),
                    "services": list(services), "context": CONTEXT_FORMS[e["kind"]],
                    "context_default": e.get("context_default", True), "runtime_min": rmin,
                    "sha256": hashlib.sha256("".join(f["sha256"] for f in fs.values()).encode()).hexdigest()}
    return out


def download_catalog(commit: str, sha: str, fetch: Callable = https_get, now: float | None = None) -> bytes:
    """models.json at a commit on main (check_on_main first), exactly the hash the hub named. UpdateError otherwise."""
    check_on_main(commit, fetch, now)
    status, headers, body = fetch(GITHUB_RAW, f"/{UPDATE_REPO}/{commit}/{CATALOG_PATH}", CATALOG_MAX_BYTES)
    if status == 404:
        raise UpdateError(f"no {CATALOG_PATH} at {commit[:12]}", final=True)
    if status != 200:
        raise UpdateError(f"GitHub said {status}", retry_s=github_wait(status, headers, time.time() if now is None else now))
    if len(body) > CATALOG_MAX_BYTES:
        raise UpdateError("the catalog is too large", permanent=True)
    if hashlib.sha256(body).hexdigest() != sha:
        raise UpdateError("the catalog's hash isn't the hub's", permanent=True)
    return body


class DownloadError(Exception):
    def __init__(self, msg: str, final: bool = False):
        super().__init__(msg)
        self.final = final                 # the file itself is wrong (its size, its hash, where it went)


def open_url(url: str, offset: int = 0, timeout: float = 30.0):
    """GET an allowed URL from byte `offset`, its redirects followed only to REDIRECT_HOSTS (each hop checked), the
    system's CA store. (connection, response, resumed: the server answered 206 from offset)."""
    if not model_url_ok(url):
        raise DownloadError("url not allowed", final=True)
    from urllib.parse import urljoin
    origin = urlsplit(url).hostname
    for _hop in range(REDIRECTS_MAX + 1):
        u = urlsplit(url)
        conn = http.client.HTTPSConnection(u.hostname, 443, timeout=timeout, context=ssl.create_default_context())
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*", **({"Range": f"bytes={offset}-"} if offset else {})}
        try:
            conn.request("GET", u.path + (f"?{u.query}" if u.query else ""), headers=headers)
            r = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise DownloadError(f"{u.hostname}: {type(e).__name__}") from None
        if r.status in (301, 302, 303, 307, 308):
            nxt = urljoin(url, r.getheader("Location") or "")
            conn.close()
            if not redirect_ok(origin, nxt):
                raise DownloadError(f"redirected to a host not allowed ({(urlsplit(nxt).hostname or '?')[:40]})",
                                    final=True)
            url = nxt
            continue
        if r.status not in (200, 206):
            conn.close()
            raise DownloadError(f"{u.hostname} said {r.status}")
        if r.status == 206 and not str(r.getheader("Content-Range") or "").startswith(f"bytes {offset}-"):
            conn.close()
            raise DownloadError(f"{u.hostname} resumed at the wrong place")
        return conn, r, r.status == 206
    raise DownloadError("too many redirects", final=True)


def download_file(url: str, dest: Path, size: int, sha: str, stop: Callable[[], bool] = lambda: False,
                  opener: Callable = open_url) -> None:
    """url -> dest: exactly `size` bytes with exactly that SHA-256, written to dest.part (resumed from the part a stopped
    download left) and then renamed. stop(), between chunks (gaming, the worker stopping): the part is kept.
    DownloadError otherwise (final: not the pinned file; its part deleted)."""
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    h = hashlib.sha256()
    if have:
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK), b""):
                h.update(chunk)
    conn = None
    try:
        if have < size:
            conn, r, resumed = opener(url, have)
            if have and not resumed:                       # the whole file again: start over
                h, have = hashlib.sha256(), 0
            with open(part, "ab" if have else "wb") as f:
                while have < size:
                    if stop():
                        raise DownloadError("stopped: resumed later")
                    chunk = r.read(min(DOWNLOAD_CHUNK, size - have))
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    have += len(chunk)
                if have == size and r.read(1):
                    raise DownloadError("larger than pinned", final=True)
    except (OSError, http.client.HTTPException) as e:
        raise DownloadError(f"download: {type(e).__name__}") from None
    except DownloadError as e:
        if e.final:
            part.unlink(missing_ok=True)
        raise
    finally:
        if conn is not None:
            conn.close()
    if have != size:
        raise DownloadError(f"short ({have} of {size} bytes): resumed later")
    if h.hexdigest() != sha:
        part.unlink(missing_ok=True)
        raise DownloadError(f"hash mismatch (the file is {h.hexdigest()[:12]})", final=True)
    _replace(part, dest)


def speech_dbfs(wav: bytes) -> float | None:
    """The loudest 10% of a WAV's 20 ms blocks (16-bit mono), dBFS: the speech level, whatever silence pads it. None
    when it can't be read."""
    import array
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return None
            rate, raw = w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError):
        return None
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder != "little":
        a.byteswap()
    n = max(1, int(rate * GATE_BLOCK_S))
    rms = sorted(math.sqrt(sum(x * x for x in a[i:i + n]) / n) for i in range(0, len(a) - n + 1, n))
    if not rms:
        return -120.0
    top = rms[int(len(rms) * 0.9):] or rms[-1:]
    level = sum(top) / len(top)
    return 20 * math.log10(level / 32768.0) if level > 0 else -120.0


ASR_TAG = re.compile(r"^\s*(?:language\s+[A-Za-z]+\s*)?<asr_text>", re.I)     # (none after a prefill)


class AsrCall:
    """One transcription by an audio model in llama-server: /v1/chat/completions with the context as a system message
    (none when it is off) and the WAV as an input_audio part; abort() closes its connection (llama-server stops)."""

    def __init__(self, server: "ModelServer", wav: bytes, context: list, timeout: float):
        msgs = []
        if context:
            msgs.append({"role": "system", "content": ", ".join(context[:CONTEXT_MAX_ITEMS])})
        msgs.append({"role": "user", "content": [{"type": "input_audio", "input_audio": {
            "data": __import__("base64").b64encode(wav).decode("ascii"), "format": "wav"}}]})
        prefill = language_setting(server.kind)["prefill"]
        if prefill:
            msgs.append({"role": "assistant", "content": prefill})      # (llama-server continues it)
        seconds = max(0.0, (len(wav) - 44) / 32000.0)          # (16 kHz, 16-bit mono)
        self.body = json.dumps({"messages": msgs, "temperature": 0, "stream": False,
                                "max_tokens": int(min(2048, 256 + 8 * seconds))}).encode()
        self.server = server
        self.conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=timeout)
        self.aborted = False

    def run(self) -> dict:
        """whisper-server's verbose_json shape ({text, segments: []}): the hub reads either alike. WhisperError."""
        try:
            if self.aborted:
                raise OSError("aborted")
            self.conn.request("POST", "/v1/chat/completions", body=self.body,
                              headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.server.key}"})
            r = self.conn.getresponse()
            data = r.read()
        except (OSError, http.client.HTTPException) as e:
            if self.aborted:
                raise WhisperError("aborted") from None
            raise WhisperError(f"llama-server unreachable ({type(e).__name__})") from None
        finally:
            self.conn.close()
        if r.status != 200:
            raise WhisperError(f"llama-server said {r.status}")
        try:
            msg = json.loads(data)["choices"][0]["message"]
            if "content" not in msg and "reasoning_content" not in msg:
                raise KeyError("content")
            content, said = str(msg.get("content") or ""), str(msg.get("reasoning_content") or "")
        except (ValueError, KeyError, IndexError, TypeError):
            raise WhisperError("llama-server sent something that isn't a chat completion") from None
        # With the language prefill ("language English<asr_text>"), llama.cpp b12728 answers the transcript as the
        # content and "language English" as reasoning_content (checked on a real server, synthetic speech). A build
        # that parses it another way: a leading tag in the content is cut off, and an empty content with the
        # transcript after <asr_text> in reasoning_content takes that (never a silent "" for words that came back).
        # The language is forced, so no "language None" either: on silence it said "language English" and a stray
        # word. Silence is the speech-level gate's (gate_dbfs).
        text = ASR_TAG.sub("", content)
        if not text.strip() and "<asr_text>" in said:
            text = said.split("<asr_text>", 1)[1]
        return {"text": " ".join(text.split()), "segments": []}

    def abort(self) -> None:
        WhisperCall.abort(self)          # (the same: shut the socket, close the connection)




def plan_models(want: list, catalog: dict, budget_mb: float, primary: str = "", primary_mb: float = 0.0) -> list:
    """The models to run beside the configured whisper-server (primary): in the hub's order, each only when its vram_mb
    fits what the budget has left; at most MODEL_PORTS."""
    left = budget_mb - primary_mb
    out: list = []
    for mid in want:
        if mid == primary or mid in out or mid not in catalog or len(out) >= MODEL_PORTS:
            continue
        if catalog[mid]["vram_mb"] <= left:
            out.append(mid)
            left -= catalog[mid]["vram_mb"]
    return out


class ModelServer:
    """One catalog model's server on its own loopback port (MODEL_PORT_FIRST, ...), started by the worker:
    whisper-server for a whisper-ggml model, llama-server (its own random API key) for a llama-asr one."""

    def __init__(self, entry: dict, paths: dict, port: int, proc: "WhisperProcess", timeout_s: float):
        self.id, self.kind, self.sha, self.paths, self.port, self.proc = (entry["id"], entry["kind"], entry["sha256"],
                                                                          paths, port, proc)
        self.vram_mb = entry["vram_mb"]
        self.entry = entry
        self.inuse = 0                     # clips being decoded on it now (Models.acquire): never stopped meanwhile
        self.whisper = Whisper(f"http://127.0.0.1:{port}/inference", timeout_s)     # (language=en: Whisper.call)
        self.timeout_s = timeout_s
        self.key = secrets.token_urlsafe(24) if self.kind == "llama-asr" else ""
        self.ok = False                    # it answered a test decode
        self.started = 0.0
        self.fails = 0
        self.log_from = 0                  # its log's size at its start (Models._exit_cause reads from there)

    def call(self, wav: bytes, prompt: str, context: list, fast: bool) -> "WhisperCall | AsrCall":
        if self.kind == "llama-asr":
            return AsrCall(self, wav, context, self.timeout_s)
        return self.whisper.call(wav, prompt, fast=fast)

    def alive(self) -> bool:
        return self.whisper.alive()

    def test(self) -> None:
        """A test decode of a second of silence. WhisperError."""
        if self.kind == "llama-asr":
            AsrCall(self, silence_wav(1.0), [], max(self.timeout_s, 60.0)).run()
        else:
            self.whisper.check()


class Models:
    """The catalog's models on this worker, beside the whisper-server in worker.toml (the primary, never touched here).
    The hub names the catalog (a commit on main and models.json's SHA-256) and the models it wants, best first (stt_models
    {catalog, want} in its hello and state answers). The models thread then, about every second:

    1. the catalog: models.json at that commit from GitHub (check_on_main, then exactly that hash), checked whole
       (check_catalog), kept in state_dir/catalog.json;
    2. the plan (plan_models): the wanted models that can start here (blocked(): their program there and new
       enough), in the hub's order, each that fits the VRAM budget (the GPU's total less vram_reserve_mb and the
       primary's). The local LLM, by llm_priority: "yield" (the default: speech first) gets what is left and gives way
       (llm_room_ok false: the worker stops llama-server) only when a model that can start now (files ready, no retry
       pending) needs its room; "keep" holds its share first and a model that doesn't fit beside it isn't loaded
       (unavailable: the hub keeps that channel on its champion or route);
    3. each planned model's files in models_dir/managed, downloaded only from an allowed host (download_file: exact
       size and SHA-256), never while gaming or paused;
    4. its server started on its own port, only while the worker is idle (no clip, not gaming); it must answer a test
       decode within MODEL_CHECK_S. One that exits out of VRAM or on a taken port is tried again after
       MODEL_SHORT_RETRY_S (doubling); one that exits or never answers otherwise, again too, and its hash is marked
       bad for MODEL_BAD_S at the MODEL_BAD_TRIES-th time in a row (a new version of a model that ran: the old files
       run meanwhile). A model no longer planned is stopped while idle.

    The hub hears only ids and states (models.stt_* in the heartbeat)."""

    def __init__(self, cfg: Config, popen: Callable = subprocess.Popen, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, fetch: Callable = https_get, download: Callable = download_file,
                 run: Callable[[list], str] | None = None, job=None, children=None):
        self.cfg, self.popen, self.clock, self.wall, self.fetch, self.download = cfg, popen, clock, wall, fetch, download
        self.run = run or run_out_err
        self.job, self.children = job, children
        self.dir = Path(cfg.models_dir) / MANAGED_DIR
        self.state_path = Path(cfg.state_dir) / "models.json"
        self.cat_path = Path(cfg.state_dir) / "catalog.json"
        self._lock = threading.Lock()
        self.catalog: dict = {}
        self.catalog_sig = ("", "")        # (commit, sha256) of the catalog in use
        self.offered: tuple[str, str] | None = None
        self.want: list = []
        self.plan: list = []
        self.servers: dict[str, ModelServer] = {}
        self.status: dict[str, str] = {}
        self.cat_status = "none"
        self.llm_room_ok = True            # the local LLM may run (no ASR model ready to start needs its VRAM)
        self._cat_next = 0.0
        self._cat_refused: dict[tuple[str, str], float] = {}
        self._retry: dict[str, float] = {}       # model id -> not before (a download that may pass later)
        self._llama_build: int | None = None
        self._llama_asked = 0.0
        self._llama_sig: tuple | None = None     # llama-server's file (size, time) and its folder's time when asked
        self._fails: dict[str, int] = {}         # model hash -> its server's failures in a row (MODEL_BAD_TRIES)
        self._sha_retry: dict[str, float] = {}   # model hash -> not started before (after a failure)
        self._short_fails: dict[str, int] = {}   # model id -> loads failed out of VRAM or on a taken port in a row
        self._port_busy: dict[int, float] = {}   # port -> not used before (another program held it)
        self.hub_llm_priority = ""               # the hub's llm_priority ("" none: worker.toml's)
        self.prev: dict = {}                     # model id -> the version a new one replaced (its rollback)
        self._final_fails: dict = {}             # file sha256 -> final download failures (marked bad at the second)
        self._dl_retry: dict = {}                # model id -> no download before (a failure that may pass later)
        self.enabled = bool(cfg.managed_models)
        if cfg.catalog_file:
            self._load_local(cfg.catalog_file)
        else:
            self._load_cached()

    # -- the catalog

    def _load_local(self, path: str) -> None:
        try:
            self.catalog = check_catalog(json.loads(Path(path).read_text(encoding="utf-8")))
            self.catalog_sig, self.cat_status = ("local", "local"), "catalog_file"
        except (OSError, ValueError) as e:
            self.cat_status = f"catalog_file: {str(e)[:80]}"
            log.error("models: %s", self.cat_status)

    def _load_cached(self) -> None:
        doc = read_json(self.cat_path)
        raw = doc.get("doc")
        if isinstance(raw, str) and isinstance(doc.get("sha256"), str) and \
                hashlib.sha256(raw.encode("utf-8")).hexdigest() == doc["sha256"]:
            try:
                self.catalog = check_catalog(json.loads(raw))
                self.catalog_sig = (str(doc.get("commit") or ""), doc["sha256"])
                self.cat_status = f"{doc['sha256'][:12]} (kept)"
            except ValueError:
                self.catalog = {}

    def offer(self, ans) -> None:
        """A hello or state answer: stt_models {catalog: {commit, sha256}, want: [ids]}; none (an older hub): nothing."""
        sm = ans.get("stt_models") if isinstance(ans, dict) else None
        if not isinstance(sm, dict):
            return
        cat = sm.get("catalog") if isinstance(sm.get("catalog"), dict) else {}
        commit, sha = cat.get("commit"), cat.get("sha256")
        if isinstance(commit, str) and isinstance(sha, str) and COMMIT.fullmatch(commit) and SHA256.fullmatch(sha):
            self.offered = (commit, sha)
        want = sm.get("want") if isinstance(sm.get("want"), list) else []
        self.want = list(dict.fromkeys(w for w in want[:16] if isinstance(w, str) and MODEL_ID.fullmatch(w)))
        lp = sm.get("llm_priority")
        self.hub_llm_priority = lp if isinstance(lp, str) and lp in LLM_PRIORITIES else ""

    def llm_priority(self) -> str:
        """"yield" (speech first) or "keep" (the LLM first): the hub's when it says, else worker.toml's."""
        return self.hub_llm_priority or self.cfg.llm_priority

    def forget_offer(self, ans) -> None:
        """An answer without stt_models (the hub turned them off, or an older one): no model wanted any more."""
        if isinstance(ans, dict) and "stt_models" not in ans:
            self.want = []

    def refresh_catalog(self, held: bool) -> None:
        """The hub's catalog fetched when it isn't ours (not while gaming; GitHub at most every CATALOG_CHECK_S)."""
        if self.cfg.catalog_file or self.offered is None or self.offered[1] == self.catalog_sig[1] or held:
            return
        commit, sha = self.offered
        if self.clock() < self._cat_next or self.clock() < self._cat_refused.get((commit, sha), 0.0):
            return
        self._cat_next = self.clock() + CATALOG_CHECK_S
        try:
            body = download_catalog(commit, sha, self.fetch, self.wall())
            cat = check_catalog(json.loads(body))
        except UpdateError as e:
            self.cat_status = f"{sha[:12]}: {str(e)[:60]}"
            if e.final:
                self._cat_refused[(commit, sha)] = self.clock() + (MODEL_BAD_S if e.permanent else UPDATE_RETRY_S)
            log.warning("models: catalog %s", self.cat_status)
            return
        except ValueError as e:                            # (CatalogError, or not JSON)
            self.cat_status = f"{sha[:12]} refused: {str(e)[:60]}"
            self._cat_refused[(commit, sha)] = self.clock() + MODEL_BAD_S
            log.error("models: catalog %s", self.cat_status)
            return
        with self._lock:
            self.catalog, self.catalog_sig = cat, (commit, sha)
        self.cat_status = f"{sha[:12]} (commit {commit[:12]})"
        try:
            write_json(self.cat_path, {"commit": commit, "sha256": sha, "doc": body.decode("utf-8")})
        except OSError as e:
            log.warning("models: can't keep the catalog: %s", e.strerror or e)
        log.info("models: catalog %s from commit %s: %d model(s)", sha[:12], commit[:12], len(cat))

    # -- models.json: the hashes marked bad

    def _doc(self) -> dict:
        return read_json(self.state_path)

    def _change(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            doc = read_json(self.state_path)
            fn(doc)
            try:
                write_json(self.state_path, doc)
            except OSError as e:
                log.warning("models: can't write models.json: %s", e.strerror or e)

    def bad(self, sha: str) -> str:
        """Why this model hash is marked bad, "" when it isn't (or no longer: MODEL_BAD_S on)."""
        bad = self._doc().get("bad")
        b = bad.get(sha) if isinstance(bad, dict) else None
        if isinstance(b, dict) and isinstance(b.get("at"), (int, float)) and self.wall() - b["at"] < MODEL_BAD_S:
            return str(b.get("why") or "bad")
        return ""

    def mark_bad(self, mid: str, sha: str, why: str) -> None:
        def f(doc):
            bad = doc.get("bad") if isinstance(doc.get("bad"), dict) else {}
            bad.pop(sha, None)
            bad[sha] = {"model": mid, "why": why[:120], "at": self.wall()}
            doc["bad"] = dict(list(bad.items())[-20:])
        self._change(f)
        log.error("models: %s (%s) marked bad for %.0f h: %s", mid, sha[:12], MODEL_BAD_S / 3600, why)

    # -- files

    def path_of(self, f: dict) -> Path:
        return self.dir / f"{f['sha256'][:16]}-{f['file']}"

    def paths(self, e: dict) -> dict:
        return {role: self.path_of(f) for role, f in e["files"].items()}

    def _sha_cache(self, f: dict) -> Path:
        return Path(self.cfg.state_dir) / f"model-{f['sha256'][:16]}.json"

    def verified(self, f: dict) -> bool:
        """The file is there with its pinned size and hash (hashed again only when it changed: file_sha256's cache)."""
        p = self.path_of(f)
        try:
            if p.stat().st_size != f["bytes"]:
                return False
            return file_sha256(p, self._sha_cache(f)) == f["sha256"]
        except OSError:
            return False

    def ready_files(self, e: dict) -> bool:
        return all(self.verified(f) for f in e["files"].values())

    def effective(self, mid: str) -> dict | None:
        """The entry a model runs as: the catalog's, or, when the catalog's version is marked bad (or waits to be tried
        again after its server failed) and the version it replaced is still here and good, that one (the rollback of
        a model's new version)."""
        e = self.catalog.get(mid)
        if e is None or not (self.bad(e["sha256"]) or self.clock() < self._sha_retry.get(e["sha256"], 0.0)):
            return e
        p = self.prev.get(mid)
        if p is not None and not self.bad(p["sha256"]) and self.ready_files(p):
            return p
        return e

    def fetch_files(self, e: dict, stop: Callable[[], bool]) -> bool:
        """Its files downloaded and checked. True when all are ready; status says why not."""
        mid = e["id"]
        if self.ready_files(e):
            return True
        if self.clock() < self._dl_retry.get(mid, 0.0) or stop():
            return False
        for role, f in e["files"].items():
            if self.verified(f):
                continue
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(self.dir).free < f["bytes"] + DISK_MARGIN:
                    self.status[mid] = "no disk space"
                    self._dl_retry[mid] = self.clock() + MODEL_RETRY_S
                    return False
                self.status[mid] = f"downloading {role}"
                log.info("models: %s: downloading %s (%.0f MB)", mid, f["file"], f["bytes"] / 1e6)
                self.download(f["url"], self.path_of(f), f["bytes"], f["sha256"], stop)
                self._seed(f)
            except DownloadError as x:
                if str(x).startswith("stopped"):
                    self.status[mid] = "waiting (gaming)"
                    return False
                self.status[mid] = f"failed: {str(x)[:60]}"
                tries = self._final_fails.get(f["sha256"], 0) + 1 if x.final else 0
                self._final_fails[f["sha256"]] = tries
                if x.final and tries >= 2:           # (once more from scratch first: a torn part looks the same)
                    self.mark_bad(mid, e["sha256"], f"{role}: {x}")
                else:
                    self._dl_retry[mid] = self.clock() + (60.0 if x.final else MODEL_RETRY_S)
                log.warning("models: %s: %s: %s", mid, role, x)
                return False
            except OSError as x:
                self.status[mid] = f"failed: {type(x).__name__}"
                self._dl_retry[mid] = self.clock() + MODEL_RETRY_S
                log.warning("models: %s: %s", mid, x)
                return False
        return self.ready_files(e)

    def _seed(self, f: dict) -> None:
        """A file just downloaded with its hash checked: its hash cache written (no 2 GB hashed again)."""
        try:
            st = self.path_of(f).stat()
            write_json(self._sha_cache(f), {"path": str(self.path_of(f)), "size": st.st_size,
                                            "mtime_ns": st.st_mtime_ns, "sha256": f["sha256"]})
        except OSError:
            pass

    def prune(self) -> None:
        """Files in models_dir/managed that no catalog model, kept previous version nor running server uses (nor their
        downloads in progress): deleted. That folder only, and only names of the worker's own pattern."""
        entries = list(self.catalog.values()) + list(self.prev.values())
        keep = {p.name for e in entries for p in self.paths(e).values()}
        keep |= {p.name for s in list(self.servers.values()) for p in s.paths.values()}
        keep |= {k + ".part" for k in keep}
        try:
            for p in self.dir.iterdir():
                if p.is_file() and p.name not in keep and \
                        re.fullmatch(r"[0-9a-f]{16}-[A-Za-z0-9._-]+\.(bin|gguf)(\.part)?", p.name):
                    p.unlink()
                    log.info("models: deleted %s (no longer in the catalog)", p.name)
        except OSError:
            pass

    # -- the plan and the VRAM

    def total_mb(self, gpu: dict | None) -> float:
        """VRAM the worker's servers may use together: vram_budget_mb, else the GPU's total (the largest one's, on a PC
        with more than one) less vram_reserve_mb; 0 with no GPU reading (no catalog model runs then)."""
        if self.cfg.vram_budget_mb > 0:
            return float(self.cfg.vram_budget_mb)
        g = gpu or {}
        total = float(g.get("mem_max_mb") or g.get("mem_total_mb") or 0.0)
        return max(0.0, total - self.cfg.vram_reserve_mb) if total > 0 else 0.0

    def primary_mb(self) -> float:
        """The configured whisper-server's share: started by the worker, or one on this PC it only talks to."""
        if not self.cfg.whisper_server and urlsplit(self.cfg.whisper_url).hostname not in LOOPBACK:
            return 0.0
        e = self.catalog.get(self.cfg.model)
        return float(e["vram_mb"]) if e is not None else float(self.cfg.primary_vram_mb)

    def llm_mb(self) -> float:
        """The local LLM's share (llm_vram_mb, else its GGUF's size and a context's worth); 0 with no LLM."""
        if not self.cfg.llm_server:
            return 0.0
        if self.cfg.llm_vram_mb > 0:
            return float(self.cfg.llm_vram_mb)
        m = os.path.expandvars(_arg(self.cfg.llm_server, ("-m", "--model")) or "")
        try:
            p = Path(m) if Path(m).is_absolute() else Path(self.cfg.base_dir) / m
            return p.stat().st_size / 1e6 * 1.05 + 900 if m else 6500.0
        except OSError:
            return 6500.0

    def make_plan(self, gpu: dict | None) -> list:
        """The wanted models (not marked bad, not the primary, able to start here: blocked()) that fit, in the hub's
        order; and whether the local LLM may run (llm_room_ok). llm_priority "yield": the LLM gets what the planned
        models that run, or could start now (files ready, no retry pending), leave; a model that can't start never
        takes its room. "keep": the LLM's share comes off the budget first (llm_room_ok always), and a model that
        fits only without it is unavailable."""
        total = self.total_mb(gpu)
        want, cat = [], {}
        for mid in self.want:
            e = self.effective(mid)
            if e is None:
                continue
            if self.bad(e["sha256"]):
                self.status[mid] = f"marked bad: {self.bad(e['sha256'])[:50]}"
                continue
            why = self.blocked(e) if mid not in self.servers else ""
            if why:
                self.status[mid] = why            # (not planned: nothing downloaded, nothing stopped for it)
                continue
            want.append(mid)
            cat[mid] = e
        llm = self.llm_mb()
        keep = self.llm_priority() == "keep" and llm > 0 and total > 0
        plan = plan_models(want, cat, total, self.cfg.model, self.primary_mb() + (llm if keep else 0.0))
        roomy = plan_models(want, cat, total, self.cfg.model, self.primary_mb()) if keep else plan
        for mid in want:
            if mid not in plan and mid != self.cfg.model:
                self.status[mid] = ("unavailable: no room beside the LLM (llm_priority keep)" if mid in roomy
                                    else "no room (VRAM)")
        if keep:
            self.llm_room_ok = True
        else:
            used = self.primary_mb() + sum(cat[m]["vram_mb"] for m in plan
                                           if m in self.servers or (self.ready_files(cat[m])
                                                                    and not self.waiting(m, cat[m])))
            self.llm_room_ok = total <= 0 or llm <= 0 or used + llm <= total
        self.plan = plan
        return plan

    def blocked(self, e: dict) -> str:
        """Why this model's server can't start on this PC as it is ("" when it can): no program for its kind, or (a
        llama-asr one) a llama-server older than its runtime_min. Never planned then: never downloaded, and the LLM
        never stopped, for it."""
        if not self._prog(e["kind"]):
            return ("no whisper-server program (whisper_bin)" if e["kind"] == "whisper-ggml"
                    else "no llama-server program (llama_bin)")
        if e["kind"] == "llama-asr" and e["runtime_min"] and self.llama_build() < e["runtime_min"]:
            return f"needs llama-server b{e['runtime_min']}+ (b{self.llama_build() or '?'} here)"
        return ""

    def waiting(self, mid: str, e: dict) -> bool:
        """A start of it waits now (after a failure): not before its retry time."""
        return self.clock() < max(self._retry.get(mid, 0.0), self._sha_retry.get(e["sha256"], 0.0))

    def _prog(self, kind: str) -> str:
        if kind == "whisper-ggml":
            return self.cfg.whisper_bin or (str(self.cfg.whisper_server[0]) if self.cfg.whisper_server else "")
        return self.cfg.llama_bin or (str(self.cfg.llm_server[0]) if self.cfg.llm_server else "")

    # -- servers

    def llama_build(self) -> int:
        """llama-server's build number from `--version` (it writes it to stderr: "version: 12728 (ea722948)", or
        "version: 1.0.0-dev (build 12728, commit ...)"); 0 when it can't be told (asked again after MODEL_RETRY_S).
        Asked again too when llama-server's file or its folder changed (a new llama.cpp unpacked over the old one:
        no restart needed)."""
        prog = self._prog("llama-asr")
        path = _resolve(prog, self.cfg.base_dir) if prog else ""
        sig = _file_sig(path)
        if self._llama_build is None or sig != self._llama_sig or \
                (not self._llama_build and self.clock() >= self._llama_asked + MODEL_RETRY_S):
            self._llama_asked, self._llama_sig = self.clock(), sig
            try:
                out = self.run([path, "--version"]) if prog else ""
            except (OSError, subprocess.SubprocessError):
                out = ""
            m = LLAMA_VERSION.search(out or "")
            self._llama_build = int(m.group(1) or m.group(2)) if m else 0
        return self._llama_build

    def _ports(self) -> list[int]:
        """The ports a catalog model's server may take: MODEL_PORT_FIRST on, never whisper_url's nor llm_url's."""
        taken = {urlsplit(u).port for u in (self.cfg.whisper_url, self.cfg.llm_url) if u}
        taken |= {s.port for s in list(self.servers.values())}
        taken |= {p for p, until in list(self._port_busy.items()) if self.clock() < until}
        return [p for p in range(MODEL_PORT_FIRST, MODEL_PORT_FIRST + MODEL_PORTS) if p not in taken]

    def argv(self, e: dict, port: int) -> list | None:
        p = self.paths(e)
        prog = self._prog(e["kind"])
        if e["kind"] == "whisper-ggml":
            t = _arg(self.cfg.whisper_server, ("-t", "--threads")) or "4"
            return whisper_command([prog, "-m", str(p["model"]), "--host", "127.0.0.1", "--port", str(port), "-t",
                                    str(t)]) if prog else None
        return [prog, "-m", str(p["model"]), "--mmproj", str(p["mmproj"]), "--host", "127.0.0.1", "--port", str(port),
                "-c", "4096", "-ngl", "99", "-np", "1", "--no-webui", "--no-slots",
                *language_setting(e["kind"])["argv"]] if prog else None

    def _later(self, mid: str, why: str) -> None:
        self.status[mid] = why
        self._retry[mid] = self.clock() + MODEL_RETRY_S

    def start(self, e: dict) -> ModelServer | None:
        """Its server started on a free port (one another program holds is skipped). None when it can't be (said in its
        status, tried again after MODEL_RETRY_S)."""
        mid = e["id"]
        why = self.blocked(e)
        if why:
            self._later(mid, why)
            return None
        for port in self._ports():
            argv = self.argv(e, port)
            if argv is None:
                self._later(mid, "no whisper-server program (whisper_bin)" if e["kind"] == "whisper-ggml"
                            else "no llama-server program (llama_bin)")
                return None
            holder: dict = {}
            env = (lambda: llm_env(holder["s"].key)) if e["kind"] == "llama-asr" else None
            proc = WhisperProcess(argv, Path(self.cfg.state_dir) / f"model-{mid}.log", self.popen, self.clock,
                                  self.cfg.base_dir, name=f"model-{mid}", env=env, job=self.job, children=self.children)
            s = holder["s"] = ModelServer(e, self.paths(e), port, proc, self.cfg.whisper_timeout_s)
            if s.alive():
                continue                          # (another program holds this port: the next one)
            s.started = self.clock()
            try:
                s.log_from = proc.log_path.stat().st_size      # (its own lines from here: why it failed, if it does)
            except OSError:
                s.log_from = 0
            proc.ensure(False)
            if not proc.running():
                self._later(mid, "can't start its server")
                return None
            with self._lock:
                self.servers[mid] = s
            self.status[mid] = "loading"
            log.info("models: %s: %s on port %d", mid, "llama-server" if e["kind"] == "llama-asr" else "whisper-server",
                     port)
            return s
        self._later(mid, "no free port")
        return None

    def stop(self, mid: str, why: str, force: bool = True) -> bool:
        """Its server stopped. force False: not while a clip is being decoded on it (acquire()). True when it was."""
        with self._lock:
            s = self.servers.get(mid)
            if s is None or (s.inuse and not force):
                return False
            self.servers.pop(mid, None)
        if s.proc is not None:
            s.proc.stop()
        log.info("models: %s stopped (%s)", mid, why)
        return True

    def stop_all(self, why: str) -> None:
        for mid in list(self.servers):
            self.stop(mid, why)

    def check(self, s: ModelServer) -> bool:
        """A server's health. A new one: its test decode (a failed one is tried again until MODEL_CHECK_S, then its hash
        is marked bad); a ready one that exited or stopped answering: stopped and started again later (not marked)."""
        if self.servers.get(s.id) is not s:
            return False                          # (stopped meanwhile by another thread: nothing to say of it)
        exited = s.proc is not None and not s.proc.running()
        if s.ok:
            if exited or not s.alive():
                if self.stop(s.id, "exited" if exited else "not answering", force=True):
                    self._later(s.id, "failed: its server stopped (again later)")
                return False
            return True
        if not exited and s.alive():
            try:
                s.test()
                s.ok, s.fails = True, 0
                self._fails.pop(s.sha, None)
                self._sha_retry.pop(s.sha, None)
                self._short_fails.pop(s.id, None)
                self.status[s.id] = "ready"
                log.info("models: %s: ready on port %d", s.id, s.port)
                return True
            except WhisperError:
                pass
        if exited or self.clock() - s.started > MODEL_CHECK_S:
            self.stop(s.id, "exited" if exited else "no answer")
            self.failed_load(s, exited)
        return False

    def failed_load(self, s: ModelServer, exited: bool) -> None:
        """A new server that exited, or never answered its test decode. Out of VRAM or on a port another program
        holds: not the model's fault, tried again after MODEL_SHORT_RETRY_S, doubling (that port left alone a while).
        Otherwise again after MODEL_SHORT_RETRY_S, doubling, and at the MODEL_BAD_TRIES-th time in a row with this
        hash it is marked bad for MODEL_BAD_S (its previous version runs meanwhile, when there is one)."""
        cause = self._exit_cause(s) if exited else ""
        if cause:
            n = self._short_fails[s.id] = self._short_fails.get(s.id, 0) + 1
            wait = min(MODEL_SHORT_RETRY_S * 2 ** (n - 1), MODEL_RETRY_S)
            if cause == "port":
                self._port_busy[s.port] = self.clock() + MODEL_RETRY_S
            self._retry[s.id] = self.clock() + wait
            self.status[s.id] = f"failed: {'out of VRAM' if cause == 'oom' else 'its port was taken'} " \
                                f"(again in {wait / 60:.0f} min)"
            log.warning("models: %s: its server %s: again in %.0f min", s.id,
                        "ran out of VRAM" if cause == "oom" else f"couldn't take port {s.port}", wait / 60)
            return
        why = "its server exited" if exited else "its server didn't answer a test decode"
        n = self._fails[s.sha] = self._fails.get(s.sha, 0) + 1
        if n >= MODEL_BAD_TRIES:
            self._fails.pop(s.sha, None)
            self._sha_retry.pop(s.sha, None)
            self.mark_bad(s.id, s.sha, f"{why} ({n} times)")
            self.status[s.id] = "failed: its server (marked bad)"
            return
        wait = min(MODEL_SHORT_RETRY_S * 2 ** (n - 1), MODEL_RETRY_S)
        self._sha_retry[s.sha] = self.clock() + wait
        self.status[s.id] = (f"failed: {'exited' if exited else 'no test answer'} ({n} of {MODEL_BAD_TRIES}; "
                             f"again in {wait / 60:.0f} min)")
        log.warning("models: %s (%s): %s (%d of %d): again in %.0f min", s.id, s.sha[:12], why, n, MODEL_BAD_TRIES,
                    wait / 60)

    def _exit_cause(self, s: ModelServer) -> str:
        """"oom" or "port" when the server's own output since its start says so, else "". Read here only: its lines
        never go to worker.log."""
        try:
            with open(s.proc.log_path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                start = s.log_from if 0 <= s.log_from <= size else 0
                f.seek(max(start, size - MODEL_LOG_TAIL))
                tail = f.read(MODEL_LOG_TAIL).decode("utf-8", "replace")
        except (OSError, AttributeError):
            return ""
        if SERVER_OOM.search(tail):
            return "oom"
        if SERVER_PORT_TAKEN.search(tail):
            return "port"
        return ""

    def ready(self) -> list:
        with self._lock:
            return [m for m, s in self.servers.items() if s.ok]

    def server(self, mid: str) -> ModelServer | None:
        with self._lock:
            s = self.servers.get(mid)
            return s if s is not None and s.ok else None

    def acquire(self, mid: str) -> ModelServer | None:
        """The ready server of a model, held for a clip (release() after): never stopped by the models thread while
        held (gaming and the worker stopping still stop it: the clip is handed back then)."""
        with self._lock:
            s = self.servers.get(mid)
            if s is None or not s.ok:
                return None
            s.inuse += 1
            return s

    def release(self, s: ModelServer) -> None:
        with self._lock:
            s.inuse = max(0, s.inuse - 1)

    def job_failed(self, mid: str, err: str = "") -> None:
        """A clip on it failed with the server's own trouble (no answer, a 5xx; a 4xx is the clip's): whisper_fail_max
        in a row and it is stopped (started again after MODEL_RETRY_S)."""
        s = self.server(mid)
        if s is None or re.search(r"said 4\d\d", err):
            return
        s.fails += 1
        if s.fails >= self.cfg.whisper_fail_max and self.stop(mid, f"{s.fails} failures in a row", force=False):
            self._later(mid, "failed: decodes (again later)")

    def job_ok(self, mid: str) -> None:
        s = self.server(mid)
        if s is not None:
            s.fails = 0

    def step(self, idle: Callable[[], bool], held: bool, gpu: dict | None, llm_running: bool,
             stop: Callable[[], bool]) -> None:
        """One pass of the models thread. idle(): no clip in hand now (asked just before a server starts or stops);
        held: gaming or paused (nothing downloaded or started); llm_running: llama-server (the LLM) holds its VRAM."""
        if not self.enabled:
            return
        self.refresh_catalog(held)
        if not self.catalog:
            return
        for s in list(self.servers.values()):
            self.check(s)
        if held:
            return
        if any(not s.ok for s in list(self.servers.values())):
            return                              # a server still loading: its test decode first, nothing else meanwhile
        plan = self.make_plan(gpu)
        for mid, s in list(self.servers.items()):
            e = self.effective(mid)
            if mid in plan and e is not None and e["sha256"] != s.sha and not self.ready_files(e):
                continue                        # a new version not downloaded yet: the old one runs meanwhile
            if (mid not in plan or e is None or e["sha256"] != s.sha) and idle():
                if self.stop(mid, "not wanted" if mid not in plan else "a new version", force=False):
                    if mid in plan and e is not None and s.entry is not None and s.entry["sha256"] != e["sha256"]:
                        self.prev[mid] = s.entry      # (kept: it runs again if the new version fails)
                    if mid not in plan:
                        self.status.pop(mid, None)
        for mid in plan:
            e = self.effective(mid)
            s = self.servers.get(mid)
            if e is None or (s is not None and s.sha == e["sha256"]):
                continue
            if not self.fetch_files(e, stop) or s is not None or stop():
                continue
            if self.waiting(mid, e) or not idle():
                continue
            if llm_running and not self.llm_room_ok:
                self.status[mid] = "waiting for the LLM to give way"
                return                          # the worker stops llama-server first (llm_room_ok false)
            if self.start(e) is not None:
                return                          # one start a pass: its test decode before the next
        self.prune()

    def report(self) -> dict:
        """What the hub hears in each heartbeat's models: the ids ready, each wanted model's state, the catalog in use
        ({} until this hub names a catalog: an older hub hears what it always did)."""
        if self.offered is None and not self.cfg.catalog_file:
            return {}
        return {"stt_loaded": self.ready(), "stt_state": {k: str(v)[:60] for k, v in list(self.status.items())[:16]},
                "stt_catalog": self.catalog_sig[1][:12]}

def _file_sig(prog: str) -> tuple | None:
    """A program's file (size, time) and its folder's time, found as Popen would (a bare name on PATH); None when
    it isn't there. A change: a new build put in its place."""
    if not prog:
        return None
    found = prog if os.path.dirname(prog) else (shutil.which(prog) or "")
    try:
        st, folder = os.stat(found), os.stat(os.path.dirname(os.path.abspath(found)))
        return (st.st_size, st.st_mtime_ns, folder.st_mtime_ns)
    except OSError:
        return None


def _resolve(prog: str, base: str) -> str:
    prog = os.path.expandvars(prog)
    return str(Path(base) / prog) if base and not Path(prog).is_absolute() and (Path(base) / prog).exists() else prog


# --- the worker ----------------------------------------------------------------------------------------------------

class Worker:
    """hello, then (a jobs thread): claim (a long poll) -> fetch the audio -> whisper-server -> complete; a heartbeat
    thread reports the state every heartbeat_s (in every state); a monitor thread watches the GPU and whisper-server.
    run()'s own thread only waits, so Ctrl-C stops the worker at once."""

    def __init__(self, cfg: Config, run: Callable[[list], str] = run_cmd, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None, popen: Callable = subprocess.Popen,
                 fullscreen: Callable[[], str] = windows_fullscreen, updater: Updater | None = None,
                 logship: LogShip | None = None):
        """logship: the log shipper `run` made (its handler on the log since the start); its thread runs here."""
        self.cfg, self.run_cmd, self.clock = cfg, run, clock
        self.logship = logship
        self._stop = threading.Event()
        self.sleep = sleep if sleep is not None else self._stop.wait     # backoff and refused waits (tests record them)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.control = Control(cfg.control_path)
        self.whisper = Whisper(cfg.whisper_url, cfg.whisper_timeout_s)
        self.proc = WhisperProcess(cfg.whisper_server, Path(cfg.state_dir) / "whisper-server.log", popen, clock,
                                   cfg.base_dir, command=whisper_command) if cfg.whisper_server else None
        self.gpu = GpuMonitor(cfg, run, clock, busy=self._busy, ours=self._our_pids, fullscreen=fullscreen)
        self.hub = Hub(cfg.hub_url, cfg.token, cfg.cf_client_id, cfg.cf_client_secret)
        self.hb_hub = Hub(cfg.hub_url, cfg.token, cfg.cf_client_id, cfg.cf_client_secret)
        # the local LLM (the hub's infer lane), when llm_url is set: its own thread, hub connection and counts
        self.llm = Llm(cfg.llm_url, cfg.llm_timeout_s, cfg.llm_no_think) if cfg.llm_url else None
        self.kill_job = KillOnClose()           # (self.job is the clip in hand: not this)
        self.children = Children(Path(cfg.state_dir) / "children.json", run)
        if self.proc is not None:
            self.proc.job, self.proc.children = self.kill_job, self.children
        # the catalog's models (Models): their files and servers, beside whisper_server
        self.stt_models = Models(cfg, popen, clock, run=run, job=self.kill_job, children=self.children)
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
        for proc in (self.proc, self.llm_proc, *(s.proc for s in list(self.stt_models.servers.values()))):
            if proc is not None and proc.pid:
                pids.add(proc.pid)
        at, found = self._pids
        if self.clock() - at > 30:
            found = find_pids(self.cfg.whisper_process, self.run_cmd)
            if self.llm is not None or any(s.kind == "llama-asr" for s in list(self.stt_models.servers.values())):
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
        """What the hub is told: the STT model (whisper_server's), the LLM while llama-server answers ("" otherwise),
        and the catalog's models (stt_loaded: the ones that answer now; stt_state: each wanted one's state)."""
        ready = self.llm is not None and self.llm_ok
        return {"stt": self.cfg.model, "llm": self.cfg.llm_model if ready else "",
                "llm_version": self.cfg.llm_model_sha256[:12].lower() if ready else "", **self.stt_models.report()}

    def loaded(self) -> list:
        """The models this worker can decode a clip with now: whisper_server's, then the catalog's that answer."""
        return ([self.cfg.model] if self.whisper_ok else []) + [m for m in self.stt_models.ready() if m != self.cfg.model]

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
        self.stt_models.offer(ans)
        self.stt_models.forget_offer(ans)
        if not self.connected:
            classes = ans.get("classes") if isinstance(ans.get("classes"), list) else []
            log.info("hub: connected as %s (%s); classes %s", self.cfg.worker_id, body["state"],
                     ", ".join(map(str, classes)) or "-")
        self.connected, self.link = True, "ok"

    def heartbeat(self) -> None:
        ans = self.hb_hub.request("POST", f"{API}/state", self._state_body(), timeout=HEARTBEAT_TIMEOUT_S)
        self.updater.offer(ans)
        self.updater.confirm()
        self.stt_models.offer(ans)
        self.stt_models.forget_offer(ans)
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
                self._gaming_stop_models(now)
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

    def models_stop_after_s(self) -> float:
        """Seconds of gaming before the catalog models' servers are stopped: the LLM's rule (they hold VRAM the game
        wants as it does, often more), not the primary whisper-server's longer gaming_stop_whisper_min.
        llm_stop_gaming_s, or (below 0, the default) 60 s, 20 s when they hold more than 6 GB together."""
        if self.cfg.llm_stop_gaming_s >= 0:
            return self.cfg.llm_stop_gaming_s
        held = sum(s.vram_mb for s in list(self.stt_models.servers.values()))
        return 20.0 if held > 6000 else 60.0

    def _gaming_stop_models(self, now: float) -> bool:
        """The catalog models' servers stopped after models_stop_after_s() of gaming (started again when it ends).
        True when they were."""
        if not self.stt_models.servers or self._gaming_since is None or \
                now - self._gaming_since < self.models_stop_after_s():
            return False
        log.info("gaming for %.0f s: stopping the speech models' servers (their VRAM back to the game)",
                 now - self._gaming_since)
        self.stt_models.stop_all("gaming")
        return True

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
        if self.llm_proc is not None and self.llm_proc.running() and not self.stt_models.llm_room_ok:
            with self._lock:
                give = self.llm_job is None and self._llm_call is None and not self._llm_claiming
                if give:
                    self._llm_down("gave its VRAM to the speech models")   # (said first: no job comes to it now)
            if give:
                log.info("llama-server: stopped: the speech-to-text models need its VRAM (they come first)")
                self.llm_proc.stop()
        if held or not self.llm_verified or not self.stt_models.llm_room_ok:
            if not self.stt_models.llm_room_ok and self.llm_ok is False:
                self.llm_err = "gave its VRAM to the speech models"
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
               "version": VERSION, "update": self.updater.report(),
               "models": {**self.stt_models.report(), "catalog": self.stt_models.cat_status,
                          "llm_room": self.stt_models.llm_room_ok}}
        if self.logship is not None:
            doc["logs"] = self.logship.stats()
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

    def _models_loop(self) -> None:
        """The catalog's models (Models.step): their catalog, files and servers, about every second."""
        while not self.stopped:
            try:
                held = self.held() is not None or self.draining
                self.stt_models.step(idle=lambda: self.job is None and not self._inferring, held=held,
                                     gpu=self.gpu.info,
                                 llm_running=self.llm_proc is not None and self.llm_proc.running(),
                                 stop=lambda: self.stopped or self.held() is not None or self.draining)
            except Exception:  # noqa: BLE001 - the models never take the worker down
                log.exception("models: unexpected error (carrying on)")
                self._stop.wait(30)
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

    def reap_leftovers(self) -> list[int]:
        """The servers a worker that died left running (children.json), ended: whisper-server, llama-server and each
        catalog model's (model-<id>: a whisper-server or llama-server), only while the pid is still that program."""
        progs = {"whisper-server": Path(str(self.cfg.whisper_server[0])).name if self.cfg.whisper_server else "",
                 "llama-server": Path(str(self.cfg.llm_server[0])).name if self.cfg.llm_server else ""}
        for name in read_json(Path(self.cfg.state_dir) / "children.json"):     # the catalog's models' servers
            if name.startswith("model-"):
                progs[name] = tuple(p for p in (self.cfg.whisper_process, self.cfg.llm_process,
                                                Path(self.cfg.whisper_bin).name, Path(self.cfg.llama_bin).name) if p)
        return self.children.reap_leftovers(progs)

    def run(self) -> None:
        log.info("sizeup STT worker %s: worker %s, model %s, hub %s", VERSION, self.cfg.worker_id, self.cfg.model,
                 urlsplit(self.cfg.hub_url).hostname)
        if self.llm is not None:
            log.info("local LLM: %s at %s", self.cfg.llm_model, self.cfg.llm_url)
            if self.llm_proc is not None and not self.cfg.llm_model_sha256:
                log.warning("llm_model_sha256 isn't set: llama-server's model isn't checked against its pin")
        self.reap_leftovers()                      # what a worker that died left running (no job object elsewhere)
        loops = [(self._heartbeat_loop, "heartbeat"), (self._monitor_loop, "monitor"), (self._update_loop, "update"),
                 (self._models_loop, "models")]
        if self.logship is not None:
            loops.append((lambda: self.logship.loop(lambda: self.stopped, self._stop.wait), "logs"))
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
                                                            "model": self.cfg.model, "models": self.loaded()},
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
        # the model: the one the hub names when it answers here, else whisper_server's (the hub records which wrote it)
        want = str(job.get("model") or "")
        server = self.stt_models.acquire(want) if want and want != self.cfg.model else None
        try:
            self._decode(job, lease, cls, jid, wav, fetch_ms, fast, server, t0, granted, expires, lease_s)
        finally:
            if server is not None:
                self.stt_models.release(server)

    def _decode(self, job: dict, lease: str, cls: str, jid, wav: bytes, fetch_ms: float, fast: bool,
                server: "ModelServer | None", t0: float, granted: float, expires: float, lease_s: float) -> None:
        """_do_job's decode: on the catalog model's server it holds (acquired: never stopped meanwhile by the models
        thread), else on whisper_server."""
        used = server.id if server is not None else self.cfg.model
        plain = job.get("plain") if isinstance(job.get("plain"), list) else []   # models its context is off for
        ctx_on = used not in plain
        prompt = str(job.get("prompt") or "") if ctx_on else ""
        context = [str(c)[:80] for c in (job.get("context") if isinstance(job.get("context"), list) else [])
                   if isinstance(c, str) and c.strip()][:CONTEXT_MAX_ITEMS] if ctx_on else []
        extra = {"context": ("list" if context else "none") if server is not None and server.kind == "llama-asr"
                 else ("prompt" if prompt else "none")}
        level = speech_dbfs(wav) if self.cfg.gate_dbfs > -120 else None
        if level is not None and level < self.cfg.gate_dbfs:
            # (near silence: every model makes words up on it; the hub reads no text as no speech)
            body = dict(result_body({}, used, round(fetch_ms), 0), gate="quiet", level_db=round(level, 1), **extra)
            self._complete(job, lease, body, expires, fetch_ms, 0.0)
            return
        call = server.call(wav, prompt, context, fast) if server is not None else self.whisper.call(wav, prompt,
                                                                                                    fast=fast)
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
            self.counts["failed"] += 1
            if server is not None:
                log.warning("job %s: %s: %s: handed back", jid, used, err)
                self._release(lease, f"{used}: {str(err)[:100]}")
                self.stt_models.job_failed(used, str(err))
                return
            self.whisper_fails += 1
            log.warning("job %s: %s (%d in a row): handed back", jid, err, self.whisper_fails)
            self._release(lease, str(err)[:120])
            if self.whisper_fails >= self.cfg.whisper_fail_max:
                self._whisper_down(f"{self.whisper_fails} failures in a row")
            return
        if server is not None:
            self.stt_models.job_ok(used)
        else:
            self.whisper_fails = 0
        body = dict(result_body(box.get("out") or {}, used, round(fetch_ms), round(infer_ms)), **extra)
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
        self.stt_models.stop_all("the worker stopped")
        self._write_status(("stopped", ""))
        self.hub.close()
        self.hb_hub.close()
        self.llm_hub.close()
        log.info("stopped")
        if self.logship is not None:
            self.logship.close_spool()             # (what is left: sent by the next start)


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
        lg = st.get("logs")
        if isinstance(lg, dict):
            last = lg.get("last_ok")
            lines.append(f"logs: {lg.get('state')}"
                         + (f", last shipped {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}"
                            if isinstance(last, (int, float)) else ", none shipped yet")
                         + f"; {lg.get('batches', 0)} batch(es) waiting"
                         + (f", {lg['dropped']} dropped" if lg.get("dropped") else ""))
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
    if cfg.ignored_keys:
        print(f"config: ignoring keys this worker.py doesn't know: {', '.join(cfg.ignored_keys[:20])}")
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
            ship = LogShip(test) if test.log_ship else None
            if ship is not None:
                log.addHandler(ship)
            Updater(test).boot()
            Worker(test, updater=Updater(test), logship=ship)._state_body()
            if ship is not None:
                log.info("selftest")
                if not ship.spool():                   # (a batch written to the scratch spool: nothing sent)
                    raise RuntimeError("the log spool in a scratch folder")
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
        ship = None
        if cfg.log_ship:                   # from the first line on: the update's own (a rollback's why) shipped too
            ship = LogShip(cfg)
            log.addHandler(ship)
        updater = Updater(cfg, config_path=config_path)
        if updater.boot():                 # a new file that never reached the hub: the old one is back; it starts next
            if ship is not None:
                ship.close_spool()
            lock.release()
            return UPDATE_EXIT
        worker = Worker(cfg, updater=updater, logship=ship)

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
