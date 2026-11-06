"""Airband AM channel (GitHub #233): rtl_fm -M am -> 16 kHz audio -> carrier squelch -> transmissions.

Westchester County Airport (HPN) ATC is AM voice at 118-137 MHz. The receiver and its squelch are shortfinal's
(the user's KHPN ATC project, core/airband.py: tested on the bench against TWR / DEP / APP), ported:

    demod     rtl_fm -M am -f <MHz> -s 12000 (one channel per dongle, no DC block: the envelope's DC level IS
              the carrier) at shortfinal's gain (36.4 dB). Resampled here to sizeup's 16 kHz so the clips, the
              speech-to-text input and the comms log are the same as every other channel's.
    squelch   on the AM *carrier*, not on voice: the demodulated envelope's mean rises while a transmitter is
              keyed, so a transmission is captured whole. The noise floor is the 20th percentile of the last
              minute; the squelch opens SQUELCH_DB (3.5 dB) over it (bench: noise bursts +1.8..2.4 dB, real
              transmissions +6..+28 dB) and, once open, stays open until the level falls HYSTERESIS_DB (1.5 dB)
              under that opening level, so a transmission hovering at the threshold doesn't chop.
    hang      HANG_FRAMES (0.4 s): the carrier may drop that long inside one transmission. ATC is a string of
              short exchanges (controller, pilot read-back, 0.5-1 s apart): 0.4 s keeps each key-up its own
              transmission, and the detector (sizeup/atc.py) reads a few of them together as the exchange.
    lengths   under 0.6 s of carrier (MIN_FRAMES) is a blip, not kept; anything over 30 s (MAX_FRAMES) is split.
    weak      a transmission whose carrier is under MIN_CARRIER_DB (5 dB) over the floor is marked weak
              (Segment.weak): kept and playable, but speech-to-text invents words from noise, so the app says
              so instead of transcribing it (shortfinal's min_carrier_db).

    schedule  a channel whose frequency changes by the time of day (#260: NY Approach, 126.4 MHz 0700-2300 local and
              120.8 2300-0700, FAA Chart Supplement): Schedule says which frequency is on now, in America/New_York
              wall-clock time (DST-aware: 07:00 is 07:00 EDT or EST). rtl_fm can't retune while running, so the
              receiver stops it and starts it again on the new frequency, only between transmissions: never while
              the squelch holds one (open, or in its hang), and only after RETUNE_QUIET_S with no carrier (an
              exchange's read-back isn't cut off from its call). A channel that never falls quiet that long retunes
              RETUNE_FORCE_S after the time anyway, still at the next gap between transmissions, never mid-clip.
              Each transmission carries the frequency it was heard on (its Channel).

The squelch speaks NoiseSquelch's interface (radio/fm.py: feed, flush, open, busy_since, quieting_db, hiss_db,
floor, holding, open_db, close_db, _start), so AmReceiver is an NfmReceiver with another demodulator and squelch:
the same restart-forever loop, status, live tap and App._segment. Its "quieting_db" is the carrier over the floor.
"""

from __future__ import annotations

import os
import select
import time
from collections import deque
from datetime import datetime
from typing import Callable, Iterable

import numpy as np

from sizeup.radio.fm import FRAME, RATE, STALL_S, Channel, NfmReceiver, Segment
from sizeup.tz import TZ

AM_RATE = 12000                # rtl_fm's AM output rate (shortfinal's RATE)
AM_FRAME = AM_RATE // 20       # 50 ms at 12 kHz
HISTORY_FRAMES = 20 * 60       # noise-floor window: the last minute
LEARN_FRAMES = 40              # 2 s before the squelch may open
SQUELCH_DB = 3.5               # open this far over the floor (shortfinal DEFAULT_SQUELCH_DB)
HYSTERESIS_DB = 1.5            # ... and stay open until this far under that
HANG_FRAMES = 8                # 0.4 s
PREROLL_FRAMES = 4             # 0.2 s before the open is kept (the start of the first word)
MIN_FRAMES = 12                # 0.6 s
MAX_FRAMES = 20 * 30           # 30 s
MIN_CARRIER_DB = 5.0           # weaker: "weak signal", not transcribed (shortfinal [stt] min_carrier_db)
GAIN_DB = 36.4                 # shortfinal's airband gain
FLOOR_MIN = 1e-6
RETUNE_QUIET_S = 2.0           # a scheduled retune waits for this long with no carrier ...
RETUNE_FORCE_S = 300.0         # ... unless it is this late: then at the next gap between transmissions
RETUNE_CHECK_S = 1.0           # how often the schedule is looked at while rtl_fm runs


class Schedule:
    """Which frequency is on when (#260): [(minute of the local day, MHz)], each from its time until the next one, round
    midnight; times are America/New_York wall-clock (sizeup/tz.py TZ), so the change follows DST."""

    def __init__(self, entries: Iterable[tuple[int, float]], tz=TZ):
        self.entries = sorted((int(m), float(f)) for m, f in entries)
        if not self.entries:
            raise ValueError("a schedule needs at least one entry")
        self.tz = tz

    def mhz_at(self, t: float) -> float:
        d = datetime.fromtimestamp(t, self.tz)
        minute = d.hour * 60 + d.minute
        cur = self.entries[-1][1]            # before the day's first time: the last one's, since last night
        for at, mhz in self.entries:
            if minute >= at:
                cur = mhz
        return cur

    def describe(self) -> str:
        """'126.4 07:00-23:00, 120.8 23:00-07:00'"""
        n = len(self.entries)
        hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"      # noqa: E731
        return ", ".join(f"{mhz:g} {hm(at)}-{hm(self.entries[(k + 1) % n][0])}" for k, (at, mhz) in enumerate(self.entries))


class CarrierSquelch:
    """Streaming AM carrier squelch. feed() 50 ms frames (16 kHz, float, the envelope with its DC); finished
    Segments come back (audio: the carrier-up part with the pre-roll, DC removed)."""

    def __init__(self, open_db: float = SQUELCH_DB, clock: Callable[[], float] = time.time):
        self.open_db, self.clock = float(open_db), clock
        self.close_db = max(0.0, self.open_db - HYSTERESIS_DB)
        self.levels: deque[float] = deque(maxlen=HISTORY_FRAMES)
        self.floor_level = 0.0               # the no-carrier envelope (linear)
        self.floor: float | None = None      # NoiseSquelch's background carrier (there is none here)
        self._frames: list[np.ndarray] = []
        self._levels: list[float] = []
        self._pre: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        self._quiet = 0
        self._n = 0
        self._start = 0.0
        self._last_level = 0.0

    # -- what NoiseSquelch's callers read ------------------------------------------------------------------------------

    @property
    def open(self) -> bool:
        return bool(self._frames) and self._quiet <= HANG_FRAMES

    @property
    def holding(self) -> bool:
        return False                          # no continuation window: that is the paging channels'

    @property
    def continuing(self) -> bool:
        return False

    @property
    def busy_since(self) -> float | None:
        return self._start if self._frames else None

    @property
    def quieting_db(self) -> float | None:
        """The carrier over the floor now (dB), or None while the floor is being learned."""
        if self._n < LEARN_FRAMES or self.floor_level <= 0:
            return None
        return float(20 * np.log10(max(self._last_level, 1e-9) / self.floor_level))

    @property
    def hiss_db(self) -> float | None:
        return float(20 * np.log10(self.floor_level)) if self.floor_level > 0 else None

    def _update_floor(self) -> None:
        lv = np.fromiter(self.levels, float)
        self.floor_level = float(max(np.percentile(lv, 20), FLOOR_MIN))

    def _over(self, level: float, db: float) -> bool:
        return level > self.floor_level * 10 ** (db / 20)

    def feed(self, frame: np.ndarray) -> Segment | None:
        level = float(np.mean(frame))
        self._last_level = level
        self.levels.append(level)
        self._n += 1
        if self._n % 20 == 0 or self._n == LEARN_FRAMES:
            self._update_floor()
        if not self._frames:
            if self._n >= LEARN_FRAMES and self._over(level, self.open_db):
                self._start = self.clock() - len(self._pre) * FRAME / RATE
                self._frames = list(self._pre) + [frame]
                self._levels = [level]
                self._pre = deque(maxlen=PREROLL_FRAMES)
                self._quiet = 0
            else:
                self._pre.append(frame)
            return None
        self._frames.append(frame)
        on = self._over(level, self.close_db)
        self._quiet = 0 if on else self._quiet + 1
        if on:
            self._levels.append(level)
        if self._quiet > HANG_FRAMES:
            return self._close("quiet")
        if len(self._frames) >= MAX_FRAMES:
            return self._close("max length")
        return None

    def flush(self, reason: str = "stopped") -> Segment | None:
        """The audio stopped with the squelch open: what was heard, now."""
        return self._close(reason) if self._frames else None

    def _close(self, reason: str) -> Segment | None:
        frames = self._frames[: len(self._frames) - max(0, self._quiet - PREROLL_FRAMES)]
        levels, start = self._levels, self._start
        self._frames, self._levels, self._quiet = [], [], 0
        if len(frames) - PREROLL_FRAMES < MIN_FRAMES:
            return None
        audio = np.concatenate(frames).astype(np.float64)
        audio = (audio - audio.mean()).astype(np.float32)
        carrier = float(20 * np.log10(max(float(np.median(levels or [self.floor_level])), 1e-9) / max(self.floor_level, 1e-9)))
        seg = Segment(start, start + len(frames) * FRAME / RATE, audio, quieting_db=carrier, reason=reason)
        seg.q_open = seg.q_close = carrier
        seg.hiss_db = self.hiss_db
        seg.weak = carrier < MIN_CARRIER_DB            # type: ignore[attr-defined]
        return seg


class Upsampler:
    """12 kHz -> 16 kHz (3:4), linear, continuous across frames (the last sample of one frame starts the next)."""

    def __init__(self):
        self.prev: float | None = None

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if not len(x):
            return x
        prev = float(x[0]) if self.prev is None else self.prev
        xx = np.concatenate(([prev], x))
        n = len(x) * RATE // AM_RATE
        t = np.arange(1, n + 1) * (len(x) / n)
        self.prev = float(x[-1])
        return np.interp(t, np.arange(len(xx)), xx)


class AmReceiver(NfmReceiver):
    """rtl_fm in AM on one airband channel, feeding a CarrierSquelch; finished segments go to on_segment."""

    def __init__(self, rtl_fm: str, device, gain_db: float | None, channel: Channel,
                 on_segment: Callable[[Channel, Segment], None], on_event: Callable[[str], None] = lambda m: None,
                 open_db: float = SQUELCH_DB, schedule: Schedule | Iterable[tuple[int, float]] | None = None, **_ignored):
        self._up = Upsampler()
        super().__init__(rtl_fm, device, gain_db, channel, on_segment, on_event, open_db=open_db)
        self.schedule = schedule if isinstance(schedule, Schedule) or not schedule else Schedule(schedule)
        self._retune_to: float | None = None      # set by _pump when it stopped to retune
        self._due_since: float | None = None      # when the schedule first asked for another frequency
        self._checked = 0.0

    # -- the schedule (#260) -----------------------------------------------------------------------------------------

    def _tune(self) -> None:
        """Before each rtl_fm start: the schedule's frequency now (no transmission can be open: rtl_fm isn't running)."""
        want = self._retune_to if self._retune_to is not None else (
            self.schedule.mhz_at(self.clock()) if self.schedule is not None else None)
        self._retune_to, self._due_since = None, None
        if want is not None and round(want, 6) != round(self.channel.mhz, 6):
            was = self.channel.mhz
            self.channel = Channel(want, self.channel.label, self.channel.ctcss)
            self.on_event(f"{self.channel.label}: retuned {was:g} -> {want:g} MHz (schedule: {self.schedule.describe()})")

    def _retuning(self) -> bool:
        return self._retune_to is not None

    def retune_due(self) -> float | None:
        """The frequency to retune to now, or None: a schedule change is due and no transmission is open or in its hang
        (never mid-clip), after RETUNE_QUIET_S with no carrier (or RETUNE_FORCE_S late, at the next gap)."""
        if self.schedule is None:
            return None
        now = self.clock()
        want = self.schedule.mhz_at(now)
        if round(want, 6) == round(self.channel.mhz, 6):
            self._due_since = None
            return None
        if self._due_since is None:
            self._due_since = now
        if self.squelch.busy_since is not None:          # a transmission is open (or in its hang): never cut it
            return None
        quiet = now - self.last_heard if self.last_heard is not None else float("inf")
        if quiet >= RETUNE_QUIET_S or now - self._due_since >= RETUNE_FORCE_S:
            return want
        return None

    def new_squelch(self) -> CarrierSquelch:
        self._up = Upsampler()
        return CarrierSquelch(open_db=self.open_db)

    def argv(self, dev: str | None = None) -> list[str]:
        cmd = [self.rtl_fm, "-M", "am", "-f", f"{self.channel.mhz:.4f}M", "-s", str(AM_RATE)]
        if self.gain_db is not None:
            cmd += ["-g", f"{self.gain_db:g}"]
        dev = self.device if dev is None and not callable(self.device) else dev
        if dev:
            cmd += ["-d", str(dev)]
        return cmd + ["-"]

    def status(self) -> dict:
        out = super().status()
        out["am"] = True
        if self.schedule is not None:
            out["schedule"] = self.schedule.describe()
        return out

    def _pump(self, proc) -> bool:
        fd = proc.stdout.fileno()
        buf, got, last = b"", False, time.monotonic()
        need = AM_FRAME * 2
        while not self._stop.is_set() and proc.poll() is None:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                if time.monotonic() - last > STALL_S:
                    self.detail = "rtl_fm stalled"
                    return got
                continue
            chunk = os.read(fd, 8192)
            if not chunk:
                return got
            got, last = True, time.monotonic()
            buf += chunk
            while len(buf) >= need:
                x = np.frombuffer(buf[:need], dtype="<i2").astype(np.float64) / 32768.0
                buf = buf[need:]
                frame = self._up(x)
                if len(frame) == FRAME:
                    self._deliver(self.squelch.feed(frame))
            self._watch_open()
            if self.schedule is not None and time.monotonic() - self._checked >= RETUNE_CHECK_S:
                self._checked = time.monotonic()
                want = self.retune_due()
                if want is not None:          # between transmissions: stop here, _run starts it on the new frequency
                    self._retune_to = want
                    return got
        return got
