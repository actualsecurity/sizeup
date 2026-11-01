"""The console's time zone, for every time a person reads: pushes, banners, status lines (GitHub #200).

Times in text are in America/New_York, the scene's "timezone" (App.scene()) and what the CAD uses
(sizeup/cad/store.py TZ), never the process's own zone: the hub container runs in UTC, so time.localtime()
there gave a push "its oldest waiting clip is from 13:29" for a clip from 08:29.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TZ_NAME = "America/New_York"
TZ = ZoneInfo(TZ_NAME)


def clock(t: float | None, fmt: str = "%H:%M", missing: str = "?") -> str:
    """t (epoch seconds) in the console's time zone as fmt ("08:29"); missing when t is unset or not a time."""
    if not t:
        return missing
    try:
        return datetime.fromtimestamp(float(t), TZ).strftime(fmt)
    except (TypeError, ValueError, OverflowError, OSError):
        return missing


def hhmm(t: float | None, missing: str = "?") -> str:
    return clock(t, "%H:%M", missing)


def hhmmss(t: float | None, missing: str = "?") -> str:
    return clock(t, "%H:%M:%S", missing)
