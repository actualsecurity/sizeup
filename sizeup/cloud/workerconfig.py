"""A GPU worker's settings from the hub (#968): one desired-config document per worker, {rev, values}.

An admin sets them (POST /api/cad/stt-workers {action: config, id, set: {key: value | null}}: null gives the key back to
the worker's own worker.toml; in the Settings audit, a line per key). hub.sqlite meta "stt_config" holds
{worker id: values}; rev is a hash of the values (rev()), so the same values are the same rev on every hub.

The worker says config {applied_rev, rev, state, why, keys, errors} in every hello and state (deploy/stt-worker/worker.py
HubConfig.report; sending it is the capability). The hub answers config {rev, values} only when the worker's applied_rev
isn't the document's (sttworkers.WorkerApi): an older worker never sees it, and an older hub never sends it. A standby
hub answers no worker at all (fenced, as the rest of the workers' API).

Only KEYS are the hub's (the audit in #968): worker.toml keeps the bootstrap (hub_url, the token and Cloudflare's, the
paths), which is never sent. The rules here are the worker's own (worker.py HUB_KEYS, check_hub_value, WHISPER_ARGS and
LLM_ARGS less the options the worker owns): tests/test_stt_worker_config.py holds the two equal. A value is checked on
both sides: here before it is kept, and by the worker before it is applied (a key a worker doesn't know yet is reported
"unknown" and left alone).

Health (view()): "<rev> ✓" when the worker applied the hub's rev with every key taken, "pending" until it has, and
"✗ <key>" when a key wasn't taken or the worker rolled the rev back (its self-test failed); the rest is in the hover.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

META = "stt_config"             # {worker id: {key: value}}: what an admin set (a worker not in it: {})
REV = re.compile(r"[0-9a-f]{16}")
MAX_KEYS = 64
FLAGS_MAX = 40
STT_LANGUAGE = "en"
LOG_LEVELS = ("debug", "info", "warning")
LLM_PRIORITIES = ("keep", "yield")
SERVER_OWNED = ("-m", "--model", "--host", "--port")     # a server's options the worker owns: never the hub's
# name: (type, low or the words or the server, high, when): the worker's HUB_KEYS (worker.py), key for key. when: "now"
# (at once), "idle" (no clip or LLM call in hand), "whisper" / "llm" (that server restarted and self-tested)
KEYS = {
    "auto_gaming": ("bool", None, None, "now"),
    "fullscreen_gaming": ("bool", None, None, "now"),
    "gaming_mem_mb": ("float", 0, 65536, "now"),
    "gaming_util_pct": ("float", 0, 100, "now"),
    "gaming_samples": ("int", 1, 60, "now"),
    "gaming_clear_s": ("float", 0, 3600, "now"),
    "gpu_poll_s": ("float", 1, 60, "now"),
    "busy_grace_s": ("float", 0, 60, "now"),
    "gaming_stop_whisper_min": ("float", 0, 1440, "now"),
    "llm_stop_gaming_s": ("float", -1, 3600, "now"),
    "whisper_timeout_s": ("float", 5, 600, "now"),
    "whisper_check_s": ("float", 1, 600, "now"),
    "whisper_fail_max": ("int", 1, 100, "now"),
    "llm_timeout_s": ("float", 5, 600, "now"),
    "llm_check_s": ("float", 1, 600, "now"),
    "llm_fail_max": ("int", 1, 100, "now"),
    "keep_awake": ("bool", None, None, "now"),
    "auto_update": ("bool", None, None, "now"),
    "log_ship": ("bool", None, None, "now"),
    "log_level": ("choice", LOG_LEVELS, None, "now"),
    "gate_dbfs": ("float", -120, 0, "idle"),
    "llm_priority": ("choice", LLM_PRIORITIES, None, "idle"),
    "managed_models": ("bool", None, None, "idle"),
    "vram_budget_mb": ("float", 0, 262144, "idle"),
    "vram_reserve_mb": ("float", 0, 65536, "idle"),
    "llm_vram_mb": ("float", 0, 262144, "idle"),
    "primary_vram_mb": ("float", 0, 65536, "idle"),
    "whisper_flags": ("flags", "whisper", None, "whisper"),
    "llm_flags": ("flags", "llm", None, "llm"),
}
VRAM_KEYS = ("vram_budget_mb", "vram_reserve_mb", "llm_vram_mb", "primary_vram_mb")   # at most the GPU's total
# the servers' options the hub may set (name: takes a value): worker.py WHISPER_ARGS (#900) and LLM_ARGS (#755) less
# SERVER_OWNED
FLAGS = {
    "whisper": {"-t": True, "--threads": True, "-l": True, "--language": True, "-sns": False, "--suppress-nst": False,
                "-et": True, "--entropy-thold": True, "-lpt": True, "--logprob-thold": True, "-nth": True,
                "--no-speech-thold": True, "-mc": True, "--max-context": True, "--vad": False, "-vm": True,
                "--vad-model": True, "-vt": True, "--vad-threshold": True},
    "llm": {"-c": True, "--ctx-size": True, "-ngl": True, "--gpu-layers": True, "--n-gpu-layers": True, "-np": True,
            "--parallel": True, "--jinja": False, "--no-webui": False, "--no-ui": False, "--no-slots": False,
            "--reasoning": True, "-rea": True, "-t": True, "--threads": True, "-fa": True, "--flash-attn": True,
            "-b": True, "--batch-size": True, "-ub": True, "--ubatch-size": True, "--no-mmap": False, "-ctk": True,
            "--cache-type-k": True, "-ctv": True, "--cache-type-v": True},
}
STATES = ("none", "applied", "pending", "rolled back")    # a worker's config.state
KEY_STATES = ("ok", "pending", "invalid", "unknown", "worker.toml only", "failed", "rolled back", "unused")
NOT_TAKEN = ("invalid", "unknown", "worker.toml only", "failed")


class ConfigValueError(ValueError):
    """A value the worker wouldn't take (fixed text, never the value: safe to show)."""


def rev(values: dict) -> str:
    """The document's rev: a hash of its values (sorted keys, no spaces), 16 hex digits."""
    raw = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _flags(key: str, server: str, v) -> list:
    if not isinstance(v, list) or len(v) > FLAGS_MAX or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in v):
        raise ConfigValueError(f"a list of options (at most {FLAGS_MAX})")
    allowed, i = FLAGS[server], 0
    while i < len(v):
        name, eq, _value = v[i].partition("=")
        if name not in allowed:
            raise ConfigValueError(f"{key}: {v[i][:40]} not allowed (allowed: {', '.join(sorted(allowed))})")
        if allowed[name] and not eq:
            if i + 1 >= len(v):
                raise ConfigValueError(f"{key}: {name} needs a value")
            i += 1
        elif eq and not allowed[name]:
            raise ConfigValueError(f"{key}: {name} takes no value")
        i += 1
    if server == "whisper":
        for i, a in enumerate(v):
            name, eq, value = a.partition("=")
            if name in ("-l", "--language"):
                lang = value if eq else (v[i + 1] if i + 1 < len(v) else "")
                if lang != STT_LANGUAGE:
                    raise ConfigValueError(f"{key}: English only (-l {STT_LANGUAGE})")
                break
    return list(v)


def check_value(key: str, v, gpu_total: float | None = None):
    """One value, checked as the worker checks it (worker.py check_hub_value): the value as kept, or ConfigValueError."""
    if key not in KEYS:
        raise ConfigValueError(f"{str(key)[:40]}: not a setting the hub sets")
    kind, a, b, _when = KEYS[key]
    if kind == "bool":
        if not isinstance(v, bool):
            raise ConfigValueError("true or false")
        return v
    if kind in ("int", "float"):
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and a <= v <= b
        if not ok or (kind == "int" and v != int(v)):
            raise ConfigValueError(f"{'a whole number' if kind == 'int' else 'a number'} from {a} to {b}")
        if key in VRAM_KEYS and gpu_total and v > gpu_total:
            raise ConfigValueError(f"more than the GPU's {gpu_total:.0f} MB")
        return int(v) if kind == "int" else float(v)
    if kind == "choice":
        if v not in a:
            raise ConfigValueError(f"one of {', '.join(a)}")
        return v
    return _flags(key, a, v)


def saved(mdb) -> dict:
    """{worker id: values} as kept (a value no longer valid: left out, never sent)."""
    try:
        doc = json.loads(mdb.get(META, "") or "{}")
    except ValueError:
        doc = {}
    out = {}
    for wid, values in (doc.items() if isinstance(doc, dict) else ()):
        if isinstance(values, dict):
            good = {}
            for k, v in values.items():
                try:
                    good[k] = check_value(k, v)
                except ConfigValueError:
                    continue
            out[str(wid)] = good
    return out


def desired(values: dict) -> dict:
    """What the worker is sent: {rev, values}."""
    return {"rev": rev(values), "values": dict(values)}


def changes(values: dict, sets, gpu_total: float | None = None) -> dict:
    """values with an admin's set {key: value | None} on top (None: the key goes back to worker.toml's). Every key
    checked first: one bad, nothing changed (ConfigValueError naming the key)."""
    if not isinstance(sets, dict) or not sets:
        raise ConfigValueError("set: {key: value, or null for worker.toml's}")
    if len(sets) > MAX_KEYS:
        raise ConfigValueError(f"set: at most {MAX_KEYS} keys")
    new = dict(values)
    for k, v in sets.items():
        if not isinstance(k, str) or k not in KEYS:
            raise ConfigValueError(f"{str(k)[:40]}: not a setting the hub sets (worker.toml keeps it)")
        if v is None:
            new.pop(k, None)
            continue
        try:
            new[k] = check_value(k, v, gpu_total)
        except ConfigValueError as e:
            raise ConfigValueError(f"{k}: {e}") from None
    return new


def said(v) -> str:
    """A value in the audit and the hover (none is a secret: the keys are thresholds, switches and server flags)."""
    if v is None:
        return "worker.toml's"
    if isinstance(v, list):
        return " ".join(v)[:200] or "(none)"
    return json.dumps(v)


def clean_report(v) -> dict | None:
    """A worker's config report, checked and bounded; None when it sent none (a worker from before: never told)."""
    if not isinstance(v, dict):
        return None
    ar, r = v.get("applied_rev"), v.get("rev")
    keys = v.get("keys") if isinstance(v.get("keys"), dict) else {}
    errs = v.get("errors") if isinstance(v.get("errors"), dict) else {}
    word = lambda s, n: " ".join(s.split())[:n] if isinstance(s, str) else ""     # noqa: E731
    return {"applied_rev": ar if isinstance(ar, str) and REV.fullmatch(ar) else "",
            "rev": r if isinstance(r, str) and REV.fullmatch(r) else "",
            "state": v.get("state") if v.get("state") in STATES else "",
            "why": word(v.get("why"), 80),
            "keys": {str(k)[:40]: word(s, 20) for k, s in list(keys.items())[:MAX_KEYS] if isinstance(s, str)},
            "errors": {str(k)[:40]: word(s, 120) for k, s in list(errs.items())[:MAX_KEYS] if isinstance(s, str)}}


def view(want: dict, rep: dict | None) -> dict:
    """Health's Config cell for a worker: {label, tone (ok, warn, dim), title (the hover)}; {} for a worker that says no
    config (one from before)."""
    if rep is None:
        return {}
    r, ar = want["rev"], rep.get("applied_rev") or ""
    about = rep.get("rev") == r or (not rep.get("rev") and ar == r)    # the keys' states are about the hub's rev
    keys = (rep.get("keys") or {}) if about else {}
    errs = (rep.get("errors") or {}) if about else {}
    bad = sorted(k for k, s in keys.items() if s in NOT_TAKEN)
    if about and rep.get("state") == "rolled back":
        label, tone = f"✗ {bad[0] if bad else 'rolled back'}", "warn"
    elif ar == r:
        label, tone = (f"✗ {bad[0]}", "warn") if bad else (f"{r[:7]} ✓", "ok")
    else:
        label, tone = "pending", "dim"
    lines = [f"Hub {r[:7]} · applied {ar[:7] or 'none'}"]
    if rep.get("state") == "pending" and rep.get("why"):
        lines.append(f"Waiting: {rep['why']}")
    if rep.get("state") == "rolled back" and about:
        lines.append(f"Rolled back to {ar[:7] or 'worker.toml'}")
    values = want.get("values") or {}
    for k in sorted(set(values) | set(keys)):
        s = keys.get(k, "")
        lines.append(f"{k} = {said(values.get(k))}" + (f" · {s}" if s and s != "ok" else "")
                     + (f": {errs[k]}" if errs.get(k) else ""))
    return {"label": label, "tone": tone, "title": "\n".join(lines), "rev": r, "applied_rev": ar,
            "state": rep.get("state") or ""}


def schema() -> dict:
    """The keys an admin may set, for a settings view: {key: {type, min, max, words, when}}."""
    out = {}
    for k, (kind, a, b, when) in KEYS.items():
        d: dict = {"type": kind, "when": when}
        if kind in ("int", "float"):
            d.update(min=a, max=b)
        elif kind == "choice":
            d["words"] = list(a)
        elif kind == "flags":
            d["flags"] = sorted(FLAGS[a])
        out[k] = d
    return out
