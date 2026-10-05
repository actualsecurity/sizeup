"""Which app holds which RTL-SDR, shared with shortfinal (agreed format, 2026-10-05).

~/.local/state/rtl-claims/<serial>.json = {"app", "pid", "mode", "since"}: written (tmp + rename)
when an app opens a dongle, deleted on release. A claim whose pid is gone is stale and ignored;
either app may delete a stale one. Neither app opens a serial with a live claim from the other.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

APP = "sizeup"
DIR = Path.home() / ".local" / "state" / "rtl-claims"


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


def other(serial: str) -> dict | None:
    """A live claim on this dongle by another app (or another sizeup process)."""
    c = holder(serial)
    return c if c and (c.get("app") != APP or int(c.get("pid", -1)) != os.getpid()) else None


def claim(serial: str, mode: str) -> bool:
    """Record that sizeup holds this dongle. False (nothing written) if another app holds it."""
    if other(serial):
        return False
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = DIR / f".{serial}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"app": APP, "pid": os.getpid(), "mode": mode, "since": time.time()}))
        os.replace(tmp, _path(serial))
    except OSError:
        pass                      # can't record it: still run (the dongle itself is the real lock)
    return True


def release(serial: str) -> None:
    """Remove sizeup's own claim (never another app's)."""
    c = holder(serial)
    if c and c.get("app") == APP and int(c.get("pid", -1)) == os.getpid():
        try:
            _path(serial).unlink()
        except OSError:
            pass
