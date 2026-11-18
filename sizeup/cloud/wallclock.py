"""This station's wall clock as the hub needs it to judge the clock skew it measures (#773, #776, #788).

Every POST /api/station/state carries `at` (this station's wall clock); the hub keeps (its clock - at) as the skew.
Beside it go two fields, so the hub can tell whether two skew samples are comparable:

    boot_id     Linux's /proc/sys/kernel/random/boot_id: this boot of the machine. None where there is none (macOS,
                tests). A new one: the station rebooted, and wall_steps started again from 0.
    wall_steps  how many times the wall clock stepped by more than STEP_S in this boot. It never goes down within one
                boot_id: a restart of sizeup (an auto-update, about 13 a day) carries it on (below).

A step: the offset (wall clock - boot clock) changed by more than STEP_S between two samples at most WITHIN_S apart
on the boot clock (CLOCK_BOOTTIME: it counts a suspend; time.monotonic() where there is none). Samples further apart
within one run (a stall, a suspend, the link off) are not compared. Sampled once a second on the station's metrics
sampler ("station-metrics", sizeup/cloud/metrics.py), never a thread of its own.

What is never a step (#788): the station's boot clock runs 2-5 % fast, which moves the offset by 0.25 s at most over
WITHIN_S; and its wall clock is pulled back toward network time in small, frequent steps of about 2 s at most.
Each step is logged once, with its size and direction: "wall clock stepped +60 s".

Across a restart (path: wall_clock.json in the station's state folder, beside the outbox): {boot_id, wall_steps,
offset, boot_at} is written (temp file + rename, no fsync) whenever the count changes, at most every SAVE_S otherwise,
and at stop. At start, the same boot_id: the count is taken back, and the offset now against the one saved is one
more step when it moved by more than STEP_S + RESTART_DRIFT x the boot-clock seconds since it was saved (the boot
clock's drift over the time sizeup was stopped: 5 % of a 2-minute stop alone is 6 s): "wall clock stepped -30 s
while sizeup was stopped". Another boot_id (a reboot: the boot_id shows it), none (macOS: a reboot can't be told), or a
file missing or unreadable: from 0.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Callable

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
STEP_S = 5.0                # the offset changed by more than this: a step ...
WITHIN_S = 5.0              # ... between two samples at most this far apart (boot clock)
SAVE_S = 60.0               # the count saved at least this often while it holds still (and at once when it changes)
RESTART_DRIFT = 0.10        # across a restart: the boot clock's drift allowed per second stopped (#773: +-10 %)
FILE = "wall_clock.json"

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
    """The wall clock's steps in this boot; boot_id read once (it never changes in a process). path: where the count
    is kept across a restart (None: nowhere, from 0 each start)."""

    def __init__(self, log: Callable[[str], None] = print, wall: Callable[[], float] = time.time,
                 boot: Callable[[], float] = boot_clock, boot_id_path: Path | str = BOOT_ID_PATH,
                 path: Path | str | None = None):
        self.log, self._wall, self._boot = log, wall, boot
        self.boot_id = read_boot_id(boot_id_path)
        self.path = Path(path) if path is not None else None
        self.steps = 0
        self._last: tuple[float, float] | None = None   # (boot clock, offset) of the last sample
        self._saved: tuple[int, float] | None = None    # (the count, boot clock) last written
        self._save_failed = False
        self._lock = threading.Lock()
        self._restore()

    def _read(self) -> tuple[float, float] | None:
        try:
            b = float(self._boot())
            off = float(self._wall()) - b
        except Exception:  # noqa: BLE001 - a clock that can't be read: no sample
            return None
        return (b, off) if math.isfinite(b) and math.isfinite(off) else None

    def _restore(self) -> None:
        if self.path is None or self.boot_id is None:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                                     # (none, or unreadable: from 0)
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)   # noqa: E731
        if not (isinstance(d, dict) and d.get("boot_id") == self.boot_id and isinstance(d.get("wall_steps"), int)
                and not isinstance(d["wall_steps"], bool) and d["wall_steps"] >= 0
                and num(d.get("offset")) and num(d.get("boot_at"))):
            return                                     # (another boot, or not ours: from 0)
        now = self._read()
        if now is None or now[0] < d["boot_at"]:
            return                                     # (a boot clock behind the save: not this boot's file)
        self.steps = d["wall_steps"]
        moved = now[1] - d["offset"]
        if abs(moved) > STEP_S + RESTART_DRIFT * (now[0] - d["boot_at"]):
            self.steps += 1
            self._say(f"wall clock stepped {moved:+.0f} s while sizeup was stopped")
        self._last = now                               # (the first sample compares with this)

    def _say(self, text: str) -> None:
        try:
            self.log(text)
        except Exception:  # noqa: BLE001
            pass

    def sample(self) -> None:
        """One look at the two clocks (the metrics sampler's thread), and the count saved when due. Never raises."""
        now = self._read()
        if now is None:
            return
        b, off = now
        with self._lock:
            last, self._last = self._last, now
            d = off - last[1] if last is not None else 0.0
            stepped = last is not None and 0.0 <= b - last[0] <= WITHIN_S and abs(d) > STEP_S
            if stepped:
                self.steps += 1
            due = self._saved is None or self._saved[0] != self.steps or b - self._saved[1] >= SAVE_S
        if stepped:
            self._say(f"wall clock stepped {d:+.0f} s")
        if due:
            self.save()

    def stop(self) -> None:
        """At stop: a last look, and the count saved."""
        self.sample()
        self.save()

    def save(self) -> None:
        """{boot_id, wall_steps, offset, boot_at} of the last sample, by a temp file and a rename. Never raises."""
        if self.path is None or self.boot_id is None:
            return
        with self._lock:
            if self._last is None:
                return
            (b, off), n = self._last, self.steps
            self._saved = (n, b)
            body = json.dumps({"boot_id": self.boot_id, "wall_steps": n, "offset": off, "boot_at": b})
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, self.path)
                self._save_failed = False
                return
            except OSError as e:
                said, self._save_failed = self._save_failed, True
                why = f"{type(e).__name__}: {e}"
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if not said:
            self._say(f"wall clock: can't keep the step count ({why})")
