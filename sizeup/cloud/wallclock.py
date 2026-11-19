"""This station's wall clock as the hub needs it to judge the clock skew it measures (#773, #776, #788).

Every POST /api/station/state carries `at` (this station's wall clock); the hub keeps (its clock - at) as the skew.
Beside it go two fields, so the hub can tell whether two skew samples are comparable:

    boot_id     Linux's /proc/sys/kernel/random/boot_id: this boot of the machine. None where there is none (macOS,
                tests) and where the count can't be kept across a restart (no state folder: a ":memory:" outbox), so
                the hub takes such a station's count as unknown. A new one: the station rebooted, and wall_steps
                started again from 0.
    wall_steps  how many times the wall clock stepped by more than STEP_S in this boot. It never goes down within one
                boot_id, unless saving it fails: a restart of sizeup (an auto-update, about 13 a day) carries it on
                (below), and a restart after the saves failed takes back the last count that was saved, so the steps
                since are lost. A failed save is said in the log and once on Health (the outbox's notes).

Sampled on the station's metrics sampler ("station-metrics", sizeup/cloud/metrics.py, once a second; never a thread
of its own) and by the state thread right before each post, so a post's `at` and its count come from one reading: a
post right after a step carries the new count, and a stalled sampler never hides a step between posts.

A step: the offset (wall clock - boot clock) changed by more than STEP_S between two samples at most WITHIN_S apart
on the boot clock (boot_clock(): CLOCK_BOOTTIME on Linux, CLOCK_MONOTONIC elsewhere; both count a suspend). Two
samples further apart (a stall, a suspend, the hub link turned off) are compared too, with the boot clock's drift over
the gap allowed: more than STEP_S + RESTART_DRIFT x the gap is one step (as across a restart, below).

What is never a step (#788): the station's boot clock runs 2-5 % fast, which moves the offset by 0.25 s at most over
WITHIN_S; and its wall clock is pulled back toward network time in small, frequent steps of about 2 s at most.
Each step is logged once, with its size and direction: "wall clock stepped +60 s".

Across a restart (path: wall_clock.json in the station's state folder, beside the outbox): {boot_id, wall_steps,
offset, boot_at} is written (temp file + rename, no fsync; outside the lock, so a slow disk never holds a sample)
whenever the count changes, at most every SAVE_S otherwise (on the 1 s tick), and at stop. At start, the same boot_id: the count is taken back, and the offset now against the one saved is one
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
RESTART_DRIFT = 0.10        # over a gap or a restart: the boot clock's drift allowed per second (#773: +-10 %)
FILE = "wall_clock.json"

# CLOCK_BOOTTIME (Linux), else CLOCK_MONOTONIC (macOS: it counts sleep, unlike time.monotonic()); both count a suspend
_CLOCKS = tuple(c for c in (getattr(time, "CLOCK_BOOTTIME", None), getattr(time, "CLOCK_MONOTONIC", None))
                if c is not None)


def boot_clock() -> float:
    """Seconds since boot, a suspend counted: CLOCK_BOOTTIME (Linux), else CLOCK_MONOTONIC (macOS), else (neither:
    never on the station) time.monotonic()."""
    for c in _CLOCKS:
        try:
            return time.clock_gettime(c)
        except OSError:
            continue
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
    is kept across a restart (None: nowhere, from 0 each start, and the post's boot_id null). note: a short text for
    Health, once per run of failed saves."""

    def __init__(self, log: Callable[[str], None] = print, wall: Callable[[], float] | None = None,
                 boot: Callable[[], float] = boot_clock, boot_id_path: Path | str = BOOT_ID_PATH,
                 path: Path | str | None = None, note: Callable[[str], None] | None = None):
        self.log, self._boot, self.note = log, boot, note
        self._wall = wall if wall is not None else (lambda: time.time())   # (looked up each time: a test's shift too)
        self.boot_id = read_boot_id(boot_id_path)
        self.path = Path(path) if path is not None else None
        self.steps = 0
        self._last: tuple[float, float] | None = None   # (boot clock, offset) of the last sample
        self._saved: tuple[int, float] | None = None    # (the count, boot clock) last written
        self._save_failed = False
        self._lock = threading.Lock()                   # the samples and the count
        self._writing = threading.Lock()                # one write at a time (never held with _lock)
        self._restore()

    @property
    def post_boot_id(self) -> str | None:
        """The boot_id a post carries: null where the count isn't kept across a restart (the hub: unknown)."""
        return self.boot_id if self.path is not None else None

    def _read(self) -> tuple[float, float] | None:
        try:
            b = float(self._boot())
            w = float(self._wall())
        except Exception:  # noqa: BLE001 - a clock that can't be read: no sample
            return None
        return (b, w) if math.isfinite(b) and math.isfinite(w) else None

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
        b, w = now
        self.steps = d["wall_steps"]
        moved = (w - b) - d["offset"]
        if abs(moved) > STEP_S + RESTART_DRIFT * (b - d["boot_at"]):
            self.steps += 1
            self._say(f"wall clock stepped {moved:+.0f} s while sizeup was stopped")
        self._last = (b, w - b)                        # (the first sample compares with this)

    def _say(self, text: str) -> None:
        try:
            self.log(text)
        except Exception:  # noqa: BLE001
            pass

    def sample(self, save: bool = True) -> tuple[float | None, int]:
        """One look at the two clocks: (the wall clock read, None if it couldn't be; the count, this look's step
        counted). save: the count written when due (the 1 s tick); the state thread's look leaves that to the tick.
        Never raises."""
        with self._lock:                               # (read inside: a later reading never lands before an earlier)
            now = self._read()
            if now is None:
                return None, self.steps
            b, w = now
            off, last = w - b, self._last
            gap = b - last[0] if last is not None else 0.0
            if gap < 0.0:                              # (never on one boot clock: kept as it was)
                return w, self.steps
            self._last = (b, off)
            d = off - last[1] if last is not None else 0.0
            # (a gap over WITHIN_S: the boot clock's drift over it is allowed too, as across a restart)
            stepped = last is not None and abs(d) > STEP_S + (RESTART_DRIFT * gap if gap > WITHIN_S else 0.0)
            if stepped:
                self.steps += 1
            n = self.steps
            due = save and (self._saved is None or self._saved[0] != n or b - self._saved[1] >= SAVE_S)
        if stepped:
            self._say(f"wall clock stepped {d:+.0f} s")
        if due:
            self.save(wait=False)
        return w, n

    def stop(self) -> None:
        """At stop: a last look, and the count saved."""
        self.sample(save=False)
        self.save(wait=True)

    def save(self, wait: bool = True) -> None:
        """{boot_id, wall_steps, offset, boot_at} of the last sample, by a temp file and a rename, outside the lock
        (a snapshot taken under it). wait=False: skipped while another write is going on (the tick: due again next
        second); True waits for it at most 2 s (stop). Never raises."""
        if self.path is None or self.boot_id is None:
            return
        if not (self._writing.acquire(timeout=2.0) if wait else self._writing.acquire(blocking=False)):
            return
        try:
            with self._lock:
                if self._last is None:
                    return
                (b, off), n = self._last, self.steps
            body = json.dumps({"boot_id": self.boot_id, "wall_steps": n, "offset": off, "boot_at": b})
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as e:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                with self._lock:
                    said, self._save_failed = self._save_failed, True
                if not said:
                    self._failed(f"{type(e).__name__}: {e}")
                return
            with self._lock:
                self._saved, self._save_failed = (n, b), False
        finally:
            self._writing.release()

    def _failed(self, why: str) -> None:
        """A save failed (the first of a run of them): a restart now would lose the steps since the last save."""
        self._say(f"wall clock: ERROR can't save the step count ({why}): a restart now would lose the steps since")
        if self.note is not None:
            try:
                self.note("wall clock: can't save the step count (a restart may lose steps)")
            except Exception:  # noqa: BLE001
                pass
