"""This station's wall clock as the hub needs it to judge the clock skew it measures (#773, #776, #788).

Every POST /api/station/state carries `at` (this station's wall clock); the hub keeps (its clock - at) as the skew.
Beside it go two fields, so the hub can tell whether two skew samples are comparable:

    boot_id     Linux's /proc/sys/kernel/random/boot_id: this boot of the machine. None where there is none (macOS,
                tests). A new one: the station rebooted, and wall_steps started again from 0.
    wall_steps  how many times the wall clock stepped by more than STEP_S since this station started counting
                (the process's start: nothing is kept on disk, so a reboot, which the boot_id shows, and a restart
                of the station start it from 0 again).

A step: the offset (wall clock - boot clock) changed by more than STEP_S between two samples at most WITHIN_S apart
on the boot clock (CLOCK_BOOTTIME: it counts a suspend; time.monotonic() where there is none). Samples further apart
(a stall, a suspend, the link off) are never a step: the gap itself could hold one, and is not counted. Sampled once
a second on the station's metrics sampler ("station-metrics", sizeup/cloud/metrics.py), never a thread of its own.

What is never a step (#788): the station's boot clock runs 2-5 % fast, which moves the offset by 0.25 s at most over
WITHIN_S; and its wall clock is pulled back toward network time in small, frequent steps of about 2 s at most.
Each step is logged once, with its size and direction: "wall clock stepped +60 s".
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
STEP_S = 5.0                # the offset changed by more than this: a step ...
WITHIN_S = 5.0              # ... between two samples at most this far apart (boot clock)

_BOOTTIME = getattr(time, "CLOCK_BOOTTIME", None)


def boot_clock() -> float:
    """Seconds on CLOCK_BOOTTIME (Linux; it counts a suspend), else time.monotonic()."""
    if _BOOTTIME is not None:
        try:
            return time.clock_gettime(_BOOTTIME)
        except OSError:
            pass
    return time.monotonic()


def read_boot_id(path: Path | str = BOOT_ID_PATH) -> str | None:
    """This boot's id (Linux), None where it can't be read."""
    try:
        s = Path(path).read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    return s if 0 < len(s) <= 64 else None


class WallSteps:
    """The wall clock's steps, counted from the first sample(); boot_id read once (it never changes in a process)."""

    def __init__(self, log: Callable[[str], None] = print, wall: Callable[[], float] = time.time,
                 boot: Callable[[], float] = boot_clock, boot_id_path: Path | str = BOOT_ID_PATH):
        self.log, self._wall, self._boot = log, wall, boot
        self.boot_id = read_boot_id(boot_id_path)
        self.steps = 0
        self._last: tuple[float, float] | None = None   # (boot clock, offset) of the last sample

    def sample(self) -> None:
        """One look at the two clocks (one thread: the metrics sampler). Never raises."""
        try:
            b = float(self._boot())
            off = float(self._wall()) - b
        except Exception:  # noqa: BLE001 - a clock that can't be read: no sample
            return
        if not (math.isfinite(b) and math.isfinite(off)):
            return
        last, self._last = self._last, (b, off)
        if last is None:
            return
        d = off - last[1]
        if not 0.0 <= b - last[0] <= WITHIN_S or abs(d) <= STEP_S:
            return
        self.steps += 1
        try:
            self.log(f"wall clock stepped {d:+.0f} s")
        except Exception:  # noqa: BLE001
            pass
