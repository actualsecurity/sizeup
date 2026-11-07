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
    sustained a carrier under the open level but within WEAK_MARGIN_DB (1 dB) of it (2.5 dB at 3.5) opens too once it has
              held there WEAK_HOLD_FRAMES (0.4 s) without a break (#270: the tower's ground transmitter arrives far
              under the aircraft overhead). The bench's noise bursts are under 2.4 dB and short; the 0.4 s run is kept as
              the carrier, with the pre-roll before it (tools/atc_eval.py squelch: about 1.5 dB of CNR more is heard,
              and noise with bursts and a drifting floor opens no more often). Segment.opened says which opened it.
    measured  per transmission (#270), sent to the hub with it (its traffic doc) and logged on the station: the carrier
              over the floor (carrier_db), both in dBFS (carrier_dbfs, floor_dbfs), and the voice's SNR (snr_db: the
              envelope's AC power with the carrier up over the pre-roll's, voice_snr_db).
    pre-roll  PREROLL_FRAMES (0.4 s, #257; was shortfinal's 0.2 s) before the open is kept, TAIL_FRAMES (0.2 s) after
              the carrier dropped: a weak or fading carrier opens the squelch late, and "Westchester Tower" came out
              "Chester Tower" (tools/atc_eval.py preroll: opening 0.3 s late, 85% of the first 0.3 s of speech was
              kept with 0.2 s, 100% with 0.4 s).
    lengths   under 0.6 s of carrier (MIN_FRAMES, the carrier's own frames: not the pre-roll) is a blip, not kept;
              anything over 30 s (MAX_FRAMES) is split.
    AGC       the envelope over its own carrier (agc()): the voice as modulation depth, held through a carrier that
              fades inside one transmission; then radio_voice (sizeup/stt.py) band-passes it and sets its loudness.
    tones     a steady tone over the voice (#263: most likely a carrier beating with a second one, 245-605 Hz, its own
              pitch on each transmission) is found and notched out of the transmission (radio/amtones.py detone),
              before its clip and speech-to-text, on every airband channel.
    weak      a transmission whose carrier is under MIN_CARRIER_DB (3 dB, #257; was 5) over the floor is marked weak
              (Segment.weak): kept and playable, but speech-to-text invents words from noise, so the app says so
              instead of transcribing it (shortfinal's min_carrier_db). Under MARGINAL_DB (6 dB) it is transcribed
              and flagged (Segment.marginal; the row's "weak signal"): Amazon Transcribe answers nothing for noise
              rather than invent words, and with the new antenna 9 of 50 rows were skipped under 5 dB. Station and hub may run different releases for a while: the station decides, the hub only
              reads the carrier it is sent.

    schedule  a channel whose frequency changes by the time of day (#260: NY Approach, 126.4 MHz 0700-2300 local and
              120.8 2300-0700, FAA Chart Supplement): Schedule says which frequency is on now, in America/New_York
              wall-clock time (DST-aware: 07:00 is 07:00 EDT or EST). rtl_fm can't retune while running, so the
              receiver stops it and starts it again on the new frequency, only between transmissions: never while
              the squelch holds one (open, or in its hang), and only after RETUNE_QUIET_S with no carrier (an
              exchange's read-back isn't cut off from its call). A channel that never falls quiet that long retunes
              RETUNE_FORCE_S after the time anyway, still at the next gap between transmissions, never mid-clip.
              Each transmission carries the frequency it was heard on (its Channel).

    several   more than one frequency on one dongle (#275): a channel's `also` (the Tower's dongle takes NY Approach's
              overnight 120.8 too, 23:00-07:00, so the tower reopening on 118.575 is never missed while Approach is
              heard), or a schedule with another channel's frequency in it (Ground's dongle on 124.65 while Ground is
              closed). Then rtl_sdr + radio/channelize.py (several AM channels from one capture) replaces rtl_fm,
              and each frequency has its own CarrierSquelch and goes out as the channel it belongs to. One frequency:
              rtl_fm, as before. A frequency another dongle also has is kept by one of them (App._heard_elsewhere).

The squelch speaks NoiseSquelch's interface (radio/fm.py: feed, flush, open, busy_since, quieting_db, hiss_db,
floor, holding, open_db, close_db, _start), so AmReceiver is an NfmReceiver with another demodulator and squelch:
the same restart-forever loop, status, live tap and App._segment. Its "quieting_db" is the carrier over the floor.
"""

from __future__ import annotations

import os
import select
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from sizeup import atc_hours
from sizeup.radio import channelize
from sizeup.radio.amtones import detone, find_tones
from sizeup.radio.fm import FRAME, RATE, STALL_S, Channel, NfmReceiver, Segment
from sizeup.tz import TZ

AM_RATE = 12000                # rtl_fm's AM output rate (shortfinal's RATE)
AM_FRAME = AM_RATE // 20       # 50 ms at 12 kHz
HISTORY_FRAMES = 20 * 60       # noise-floor window: the last minute
LEARN_FRAMES = 40              # 2 s before the squelch may open
SQUELCH_DB = 3.5               # open this far over the floor (shortfinal DEFAULT_SQUELCH_DB)
HYSTERESIS_DB = 1.5            # ... and stay open until this far under that
HANG_FRAMES = 8                # 0.4 s
PREROLL_FRAMES = 8             # 0.4 s before the open is kept (#257: the first syllable, "Westchester" not "Chester")
TAIL_FRAMES = 4                # 0.2 s after the carrier dropped is kept (the last syllable)
MIN_FRAMES = 12                # 0.6 s of carrier
MAX_FRAMES = 20 * 30           # 30 s
WEAK_MARGIN_DB = 1.0           # #270: a carrier this much under the open level (2.5 dB at 3.5) opens too ...
WEAK_HOLD_FRAMES = 8           # ... once it has held there this long without a break (0.4 s; a noise burst doesn't)
MIN_CARRIER_DB = 3.0           # weaker: "weak signal", not transcribed (#257: was shortfinal's 5 dB)
MARGINAL_DB = 6.0              # weaker than this (and >= MIN_CARRIER_DB): transcribed, flagged low confidence
AGC_S = 0.1                    # the carrier's level, followed over this long (AM AGC: under the voice band)
AGC_MIN = 0.5                  # ... never divided by less than this x the transmission's carrier (the pre-roll's hiss)
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
        # #270: the sustained-weak open (a ground transmitter far under the aircraft overhead): weak_db held for
        # WEAK_HOLD_FRAMES. It closes like any other (close_db), kept under weak_db so it doesn't chop.
        self.weak_db = max(0.5, self.open_db - WEAK_MARGIN_DB)
        self.close_db = min(self.close_db, self.weak_db - 0.5)
        self._run: list[float] = []          # the levels of the frames in a row over weak_db (squelch closed)
        self._opened = "carrier"             # how the open transmission opened: carrier | sustained
        self.levels: deque[float] = deque(maxlen=HISTORY_FRAMES)
        self.floor_level = 0.0               # the no-carrier envelope (linear)
        self.floor: float | None = None      # NoiseSquelch's background carrier (there is none here)
        self._frames: list[np.ndarray] = []
        self._levels: list[float] = []
        self._pre: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES + WEAK_HOLD_FRAMES)
        self._npre = 0                       # pre-roll frames at the front of _frames
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
            learnt = self._n >= LEARN_FRAMES
            self._run = self._run + [level] if learnt and self._over(level, self.weak_db) else []
            if learnt and self._over(level, self.open_db):
                self._open(frame, level, [], "carrier")
            elif len(self._run) >= WEAK_HOLD_FRAMES:
                self._open(frame, level, self._run[:-1], "sustained")
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

    def _open(self, frame: np.ndarray, level: float, run: list[float], how: str) -> None:
        """Open on this frame; run: the levels of the frames just before it that were already the carrier (a sustained
        weak open), kept as the carrier, with PREROLL_FRAMES of pre-roll before them."""
        pre = list(self._pre)[-(PREROLL_FRAMES + len(run)):] if PREROLL_FRAMES + len(run) else []
        self._start = self.clock() - len(pre) * FRAME / RATE
        self._frames = pre + [frame]
        self._npre = len(pre) - min(len(run), len(pre))
        self._levels = run[-len(pre):] + [level] if run and pre else [level]
        self._pre = deque(maxlen=PREROLL_FRAMES + WEAK_HOLD_FRAMES)
        self._run, self._quiet, self._opened = [], 0, how

    def flush(self, reason: str = "stopped") -> Segment | None:
        """The audio stopped with the squelch open: what was heard, now."""
        return self._close(reason) if self._frames else None

    def _close(self, reason: str) -> Segment | None:
        tail = min(self._quiet, TAIL_FRAMES)
        frames = self._frames[: len(self._frames) - (self._quiet - tail)]
        levels, start, npre, opened = self._levels, self._start, self._npre, self._opened
        self._frames, self._levels, self._quiet, self._npre, self._opened = [], [], 0, 0, "carrier"
        if len(frames) - npre - tail < MIN_FRAMES:       # the carrier itself was up too short: a blip
            return None
        med = float(np.median(levels or [self.floor_level]))
        carrier = float(20 * np.log10(max(med, 1e-9) / max(self.floor_level, 1e-9)))
        snr = voice_snr_db(frames, npre, len(frames) - tail)     # #270: measured before the AGC, per segment
        audio = agc(np.concatenate(frames), med)
        tones = find_tones(audio, RATE)                # a heterodyne's steady tone over the voice (#263): notched out
        audio = detone(audio, RATE, tones)
        seg = Segment(start, start + len(frames) * FRAME / RATE, audio, quieting_db=carrier, reason=reason)
        seg.q_open = seg.q_close = carrier
        seg.hiss_db = self.hiss_db
        seg.weak = carrier < MIN_CARRIER_DB            # type: ignore[attr-defined]
        seg.marginal = not seg.weak and carrier < MARGINAL_DB   # type: ignore[attr-defined]
        seg.preroll_s = npre * FRAME / RATE            # type: ignore[attr-defined]
        seg.tones = [round(f0, 1) for f0, _spread in tones]   # type: ignore[attr-defined]  (the ones notched out)
        # #270: what the hub needs to tell a weak transmitter from a missing one, per transmission
        seg.carrier_dbfs = round(float(20 * np.log10(max(med, 1e-9))), 1)   # type: ignore[attr-defined]
        seg.snr_db = None if snr is None else round(snr, 1)    # type: ignore[attr-defined]
        seg.opened = opened                                    # type: ignore[attr-defined]  carrier | sustained
        return seg


def voice_snr_db(frames: list[np.ndarray], npre: int, end: int) -> float | None:
    """The voice over the noise (dB), from the envelope itself: its AC power while the carrier is up (the modulation, plus
    the noise riding on it) over its AC power in the pre-roll (the noise alone, no carrier yet). None without a pre-roll."""
    if npre < 2 or end - npre < 2:
        return None
    ac = lambda fs: float(np.mean([np.var(f) for f in fs]))      # noqa: E731  (per 50 ms frame: no carrier step in it)
    noise, voice = ac(frames[:npre]), ac(frames[npre:end])
    if noise <= 0:
        return None
    return float(10 * np.log10(max(voice / noise - 1.0, 1e-3)))


def agc(envelope: np.ndarray, carrier: float, rate: int = RATE) -> np.ndarray:
    """AM AGC: the demodulated envelope over its own carrier, so the voice is the modulation depth whatever the signal
    strength, and a carrier that fades or flutters inside one transmission (an aircraft turning, multipath) doesn't
    take the voice with it. The carrier is the envelope's level followed over AGC_S (well under the 300 Hz voice
    band), subtracted (no step where the carrier comes up) and divided out, never by less than AGC_MIN x the
    transmission's median carrier (so the pre-roll's hiss, before the carrier, stays quieter than the voice). Scaled
    back by that median, so a steady carrier comes out as before (DC removed). radio_voice (sizeup/stt.py) band-passes it and sets
    the speech loudness for the clip and speech-to-text."""
    x = np.asarray(envelope, dtype=np.float64)
    if len(x) < 2 or carrier <= 0:
        return (x - (x.mean() if len(x) else 0.0)).astype(np.float32)
    n = max(1, int(AGC_S * rate))
    k = np.ones(n) / n
    pad = np.concatenate((np.full(n // 2, x[0]), x, np.full(n - 1 - n // 2, x[-1])))
    level = np.convolve(pad, k, mode="valid")[: len(x)]
    # the voice is what rides on the carrier (minus its own slow level: no step where the carrier comes up), over it
    y = (x - level) / np.maximum(level, AGC_MIN * carrier) * carrier   # (x carrier: the input's scale, as before)
    return (y - y.mean()).astype(np.float32)


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


class Extra:
    """Another frequency on the same dongle (#275, config "also"): heard as `label`'s (the channel that frequency is
    on), between `start` and `end` (minutes of the local day; None: all day)."""

    def __init__(self, mhz: float, label: str, start: int | None = None, end: int | None = None):
        self.mhz, self.label, self.start, self.end = float(mhz), label, start, end

    def on_at(self, minute: int) -> bool:
        if self.start is None or self.end is None or self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    def describe(self) -> str:
        hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"      # noqa: E731
        return f"{self.mhz:g}" + ("" if self.start is None or self.end is None else f" {hm(self.start)}-{hm(self.end)}")


class Sub:
    """One frequency being demodulated: the Channel its transmissions are heard on (the label: the channel that
    frequency belongs to), its own squelch and upsampler."""

    def __init__(self, channel: Channel, squelch, up: Upsampler | None = None):
        self.channel, self.squelch, self.up = channel, squelch, up


class AmReceiver(NfmReceiver):
    """rtl_fm in AM on one airband channel, feeding a CarrierSquelch; finished segments go to on_segment.

    Several frequencies at once (#275: `also`, a schedule with another channel's frequency in it): when more than one
    is on, the dongle runs the channelizer (radio/channelize.py: rtl_sdr + an FFT channelizer, its own process) instead
    of rtl_fm, and each frequency gets its own CarrierSquelch (its own floor, tone notch, clips, speech-to-text and
    talkers: each transmission is delivered as the channel that frequency belongs to, owner(mhz)). One frequency on:
    rtl_fm, as before. The set changes like a schedule's retune: between transmissions, never mid-clip.
    covered(mhz, receiver): another dongle hears that frequency too and keeps it (App._heard_elsewhere): not delivered twice."""

    def __init__(self, rtl_fm: str, device, gain_db: float | None, channel: Channel,
                 on_segment: Callable[[Channel, Segment], None], on_event: Callable[[str], None] = lambda m: None,
                 open_db: float = SQUELCH_DB, schedule: Schedule | Iterable[tuple[int, float]] | None = None,
                 also: Iterable[Extra | tuple] = (), owner: Callable[[float], str | None] | None = None,
                 covered: Callable[[float, "AmReceiver"], bool] | None = None, rtl_sdr: str | None = None,
                 python: str | None = None, **_ignored):
        self._up = Upsampler()
        self.base_mhz = float(channel.mhz)
        self.extras = [x if isinstance(x, Extra) else Extra(*x) for x in also]
        self.owner = owner or (lambda mhz: None)
        self.covered = covered
        self.rtl_sdr = rtl_sdr or _sibling(rtl_fm, "rtl_sdr")
        self.python = python or sys.executable
        self.active: tuple[float, ...] = (float(channel.mhz),)
        self.subs: list[Sub] = []
        self.capture: tuple[float, int] | None = None     # (centre MHz, rate) while the channelizer runs
        self._dropped: set[float] = set()                 # frequencies another dongle has (logged once each)
        super().__init__(rtl_fm, device, gain_db, channel, on_segment, on_event, open_db=open_db)
        self.schedule = schedule if isinstance(schedule, Schedule) or not schedule else Schedule(schedule)
        self._retune_to: float | tuple[float, ...] | None = None  # set by _pump when it stopped to retune
        self._due_since: float | None = None      # when the schedule first asked for another frequency
        self._checked = 0.0
        self.active = self.plan_at(self.clock())
        self.channel = Channel(self.active[0], channel.label, channel.ctcss)
        self.subs = self._make_subs(self.squelch)

    # -- which frequencies when (#260 schedule, #275 also) -----------------------------------------------------------

    def plan_at(self, t: float) -> tuple[float, ...]:
        """The frequencies on at t: the channel's own (its schedule's) first, then the extras on then."""
        first = self.schedule.mhz_at(t) if getattr(self, "schedule", None) is not None else self.base_mhz
        d = datetime.fromtimestamp(t, TZ)
        minute = d.hour * 60 + d.minute
        out = [float(first)]
        for x in self.extras:
            if x.on_at(minute) and all(round(x.mhz, 6) != round(f, 6) for f in out):
                out.append(x.mhz)
        return tuple(out)

    def label_for(self, mhz: float) -> str:
        """The channel a frequency belongs to (owner), else this one's: transmissions are heard as that channel's."""
        if round(mhz, 6) == round(self.base_mhz, 6):
            return self.channel.label
        named = next((x.label for x in self.extras if x.label and round(x.mhz, 6) == round(mhz, 6)), None)
        return named or self.owner(mhz) or self.channel.label

    def _make_subs(self, first) -> list[Sub]:
        subs = []
        for k, mhz in enumerate(self.active):
            ch = Channel(mhz, self.label_for(mhz), self.channel.ctcss)
            subs.append(Sub(ch, first if k == 0 else CarrierSquelch(open_db=self.open_db), Upsampler()))
        return subs

    def describe_plan(self) -> str:
        """'118.575 + 120.8 23:00-07:00', '126.4 07:00-23:00, 120.8 23:00-07:00'."""
        head = self.schedule.describe() if self.schedule is not None else f"{self.base_mhz:g}"
        return " + ".join([head, *(x.describe() for x in self.extras)])

    # -- the schedule (#260) and the set (#275) ----------------------------------------------------------------------

    def _tune(self) -> None:
        """Before each start: the set on now (no transmission can be open: nothing is running)."""
        want = self._retune_to if self._retune_to is not None else self.plan_at(self.clock())
        want = (float(want),) if isinstance(want, (int, float)) else tuple(want)
        self._retune_to, self._due_since = None, None
        if _key(want) != _key(self._current()):
            was = self._current()
            self.on_event(f"{self.channel.label}: retuned {_fmt(was)} -> {_fmt(want)} MHz (schedule: {self.describe_plan()})")
        self.active = tuple(want)
        self.channel = Channel(self.active[0], self.channel.label, self.channel.ctcss)
        self.capture = plan_capture(self.active) if len(self.active) > 1 else None

    def _retuning(self) -> bool:
        return self._retune_to is not None

    def _current(self) -> tuple[float, ...]:
        """What it is on: the channel's frequency, then the others with it."""
        return (float(self.channel.mhz), *self.active[1:])

    def _busy(self) -> bool:
        return self.squelch.busy_since is not None or any(s.squelch.busy_since is not None for s in self.subs[1:])

    def retune_due(self) -> float | tuple[float, ...] | None:
        """The frequency (one) or set (several) to retune to now, or None: a change is due and no transmission is open or in its hang on any of
        its frequencies (never mid-clip), after RETUNE_QUIET_S with no carrier (or RETUNE_FORCE_S late, at the next gap)."""
        if self.schedule is None and not self.extras:
            return None
        now = self.clock()
        want = self.plan_at(now)
        if _key(want) == _key(self._current()):
            self._due_since = None
            return None
        if self._due_since is None:
            self._due_since = now
        if self._busy():                                 # a transmission is open (or in its hang): never cut it
            return None
        quiet = now - self.last_heard if self.last_heard is not None else float("inf")
        if quiet >= RETUNE_QUIET_S or now - self._due_since >= RETUNE_FORCE_S:
            return want[0] if len(want) == 1 else want
        return None

    def new_squelch(self) -> CarrierSquelch:
        self._up = Upsampler()
        return CarrierSquelch(open_db=self.open_db)

    def _ok_detail(self) -> str:
        if len(self.active) == 1:
            return f"{self.channel.label} {self.channel.mhz:.4f}"
        c, rate = self.capture or (0.0, 0)
        return f"{_fmt(self.active)} · {rate / 1e6:g} MS/s @ {c:.4f}"

    def argv(self, dev: str | None = None) -> list[str]:
        dev = self.device if dev is None and not callable(self.device) else dev
        if len(self.active) > 1:
            if self.capture is None:
                self.capture = plan_capture(self.active)
            c, rate = self.capture
            return channelize.argv_for(self.python, self.rtl_sdr, c, rate,
                                       [m * 1e6 - c * 1e6 for m in self.active], self.gain_db, dev)
        cmd = [self.rtl_fm, "-M", "am", "-f", f"{self.channel.mhz:.4f}M", "-s", str(AM_RATE)]
        if self.gain_db is not None:
            cmd += ["-g", f"{self.gain_db:g}"]
        if dev:
            cmd += ["-d", str(dev)]
        return cmd + ["-"]

    def status(self) -> dict:
        out = super().status()
        out["am"] = True
        if self.schedule is not None or self.extras:
            out["schedule"] = self.describe_plan()
        now = self.clock()
        out["freqs"] = []
        for k, s in enumerate(self.subs or [Sub(self.channel, self.squelch)]):
            q = s.squelch.quieting_db
            out["freqs"].append({"mhz": s.channel.mhz, "label": s.channel.label, "name": freq_name(s.channel.mhz, now),
                                 "open": bool(s.squelch.open), "quieting_db": None if q is None else round(q, 1),
                                 "elsewhere": round(s.channel.mhz, 6) in self._dropped})
        if any(f["open"] for f in out["freqs"]):
            out["open"] = True
        out["now"] = now_text(out["freqs"])
        if any(round(f["mhz"], 4) in (atc_hours.TOWER_MHZ, atc_hours.GROUND_MHZ) for f in out["freqs"]):
            out["tower"] = atc_hours.tower(now)            # HPN's tower open / closed (CTAF) now
        if self.capture is not None:
            out["capture"] = {"centre_mhz": round(self.capture[0], 6), "rate": self.capture[1]}
        return out

    # -- delivering ---------------------------------------------------------------------------------------------------

    def _deliver(self, seg: Segment | None, channel: Channel | None = None) -> None:
        if seg is None:
            return
        ch = channel or (self.subs[0].channel if self.subs else self.channel)
        if self.covered is not None and ch.label != self.channel.label:   # another channel's frequency
            try:
                gone = self.covered(ch.mhz, self)
            except Exception:  # noqa: BLE001 - never in the way of a transmission
                gone = False
            if gone:                                     # another dongle keeps this frequency: not twice
                if round(ch.mhz, 6) not in self._dropped:
                    self._dropped.add(round(ch.mhz, 6))
                    self.on_event(f"{self.channel.label}: {ch.mhz:g} is heard on another dongle: kept there, not here")
                self.last_heard = seg.end
                return
            self._dropped.discard(round(ch.mhz, 6))
        super()._deliver(seg, ch)

    def _flush(self, reason: str) -> None:
        subs = self.subs or [Sub(self.channel, self.squelch)]
        self._deliver(self.squelch.flush(reason), subs[0].channel)
        for s in subs[1:]:
            self._deliver(s.squelch.flush(reason), s.channel)

    def _pump(self, proc) -> bool:
        fd = proc.stdout.fileno()
        buf, got, last = b"", False, time.monotonic()
        self.subs = self._make_subs(self.squelch)        # the squelch _run has just made, then one for each other one
        multi = len(self.active) > 1
        n = len(self.subs) if multi else 1
        need = FRAME * 4 * n if multi else AM_FRAME * 2
        while not self._stop.is_set() and proc.poll() is None:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                if time.monotonic() - last > STALL_S:
                    self.detail = "rtl_fm stalled" if not multi else "rtl_sdr stalled"
                    return got
                continue
            chunk = os.read(fd, 65536 if multi else 8192)
            if not chunk:
                return got
            got, last = True, time.monotonic()
            buf += chunk
            while len(buf) >= need:
                if multi:                                # FRAME float32 samples per channel, channels in turn, 16 kHz
                    x = np.frombuffer(buf[:need], dtype="<f4").astype(np.float64).reshape(n, FRAME)
                    buf = buf[need:]
                    self._deliver(self.squelch.feed(x[0]), self.subs[0].channel)
                    for k in range(1, n):
                        s = self.subs[k]
                        self._deliver(s.squelch.feed(x[k]), s.channel)
                    continue
                x = np.frombuffer(buf[:need], dtype="<i2").astype(np.float64) / 32768.0
                buf = buf[need:]
                frame = self._up(x)
                if len(frame) == FRAME:
                    self._deliver(self.squelch.feed(frame))
            self._watch_open()
            if (self.schedule is not None or self.extras) and time.monotonic() - self._checked >= RETUNE_CHECK_S:
                self._checked = time.monotonic()
                want = self.retune_due()
                if want is not None:          # between transmissions: stop here, _run starts it on the new set
                    self._retune_to = want
                    return got
        return got


def _key(mhzs) -> tuple:
    return tuple(round(float(m), 6) for m in mhzs)


def _fmt(mhzs) -> str:
    return " + ".join(f"{m:g}" for m in mhzs)


def _sibling(rtl_fm: str, name: str) -> str:
    """rtl_sdr next to the configured rtl_fm (/opt/sizeup/rtl-sdr/bin: the same rtl-sdr-blog build), else on PATH."""
    p = Path(rtl_fm)
    return str(p.with_name(name)) if p.parent != Path(".") or "/" in rtl_fm else name


def plan_capture(mhzs) -> tuple[float, int]:
    return channelize.plan(mhzs)


def now_text(freqs: list[dict]) -> str:
    """What a dongle is on now, short (#278): '126.4', '118.575 CTAF + 120.8'."""
    return " + ".join(f"{f['mhz']:g}" + (f" {f['name']}" if f.get("name") else "") for f in freqs)


def freq_name(mhz: float, t: float) -> str:
    """What a frequency is now, for the rows (#275): 'CTAF' on the tower's frequency while the tower is closed."""
    return atc_hours.freq_name(mhz, t)
