"""Which app holds which RTL-SDR, shared with shortfinal (agreed format, 2026-10-05).

~/.local/state/rtl-claims/<serial>.json = {"app", "pid", "mode", "since"}: written (tmp + rename)
when an app opens a dongle, deleted on release. A claim whose pid is gone is stale and ignored;
either app may delete a stale one. Neither app opens a serial with a live claim from the other.

On the Linux station trunk-recorder runs under its own service (sizeup/radio/trunkrecd.py, GitHub #71), which holds the
Fire/EMS 15 dongle's claim under its own pid, so the claim stays right while sizeup-station restarts. To sizeup that
claim is its own (helpers): never "in use by another app", never overwritten, and only the service removes it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

APP = "sizeup"
DIR = Path.home() / ".local" / "state" / "rtl-claims"
# The pids of sizeup's own helper processes whose claims count as sizeup's: the trunk-recorder service
# (sizeup/radio/recorder.py UnitRecorder sets this). Nothing else ever is.
helpers: Callable[[], set[int]] = lambda: set()


def _path(serial: str) -> Path:
    return DIR / f"{serial}.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:      # someone else's process: alive
        return True
    except (OSError, ValueError):
        return False


def holder(serial: str) -> dict | None:
    """The live claim on this dongle (any app), or None. Stale claims are removed."""
    p = _path(serial)
    try:
        c = json.loads(p.read_text())
        pid = int(c["pid"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if _alive(pid):
        return c
    try:
        p.unlink()
    except OSError:
        pass
    return None


def _pid(c: dict) -> int:
    try:
        return int(c.get("pid", -1))
    except (TypeError, ValueError):
        return -1


def _mine(c: dict) -> bool:
    """This process's claim."""
    return c.get("app") == APP and _pid(c) == os.getpid()


def _helper(c: dict) -> bool:
    """A claim of sizeup's trunk-recorder service (helpers)."""
    if c.get("app") != APP:
        return False
    try:
        return _pid(c) in helpers()
    except Exception:  # noqa: BLE001 - an unreadable status: not a helper
        return False


def other(serial: str) -> dict | None:
    """A live claim on this dongle by another app (or another sizeup process that isn't one of its helpers)."""
    c = holder(serial)
    return c if c and not (_mine(c) or _helper(c)) else None


def claim(serial: str, mode: str) -> bool:
    """Record that sizeup holds this dongle. False (nothing written) if another app holds it. Held by sizeup's
    trunk-recorder service: True for its own mode (its claim stands: the service runs it for us), False for any other
    (it hasn't let go of the dongle yet)."""
    c = holder(serial)
    if c and not _mine(c):
        return _helper(c) and c.get("mode") == mode
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = DIR / f".{serial}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"app": APP, "pid": os.getpid(), "mode": mode, "since": time.time()}))
        os.replace(tmp, _path(serial))
    except OSError:
        pass                      # can't record it: still run (the dongle itself is the real lock)
    return True


def release(serial: str) -> None:
    """Remove sizeup's own claim (never another app's, nor its trunk-recorder service's)."""
    c = holder(serial)
    if c and _mine(c):
        try:
            _path(serial).unlink()
        except OSError:
            pass
