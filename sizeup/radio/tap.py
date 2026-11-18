"""The live tap: each analog receiver's 50 ms frames as they are read, for what must show while the
carrier is still up. One tap for every FM channel: LiveTap.attach(receiver) puts it on the receiver's
squelch (TapSquelch: the squelch sees each frame first, then the tap, then a finished transmission
goes on to App._segment), with no change to the receiver or the squelch themselves (radio/fm.py):

    paging  (South 2)   every frame of an open carrier -> LivePaging (radio/livepage.py): who is
                        being paged, pair by pair, then "voice"
    traffic (FG1)       the first MDC_WINDOW_S of an open carrier, with the PREROLL_FRAMES read just
                        before the squelch opened -> the MDC-1200 and FleetSync decoders every MDC_EVERY
                        frames until an ID decodes -> LiveTalk.fm_ids: the talker shown at key-up (a
                        pre-ID); an emergency packet (the radio's emergency button) -> on_emergency at
                        once, once per carrier: the alarm at the key-up (sizeup/cad/alarms.py, #128)

Why the pre-roll: the squelch opens one frame (50 ms) or more after the carrier comes up, and a radio
that sends its pre-ID at key-up starts the burst with the carrier. The burst's preamble is 47 ms; lose
more than that and the sync is gone (2026-10-13 13:17, E63 on FG1: the talker only showed at release,
from the post-ID). The frames before the open are only hiss and the start of the carrier; packets are
CRC-checked, so hiss can't make an ID.

The ID look-back (#694): a carrier that rises slowly opens the squelch only once it quiets the hiss open_db, and a burst
sent at key-up can begin more than the pre-roll before that (a 0.4 s ramp: ~0.3 s), so its sync was lost, and an
emergency press raised no alarm. So the first two decode steps also read ID_LOOKBACK_FRAMES (0.4 s) of the channel
before the carrier's buffer, whatever the squelch made of them (hiss, the drop before a key-up cut, the weak tail of the
carrier before). An ID found only that way is taken when its burst ran on into the pre-roll (the last 0.2 s before an
open; on a key-up cut only the drop, never the carrier before's tail): one that ended before that is the carrier
before's, or a weak radio's that never opened the squelch, and is left, unless it is an emergency wholly in frames the
squelch was closed for (the weak radio's button: it alarms, on this key-up's row). An emergency the carrier just before
already raised for the same burst keeps that alarm (one press, one alarm). The clip, the pre-roll and the "ID not
decoded" check (#163) are as they were: only the decode looks further back.

Cheap on purpose (the station is an i5-6400T): the MDC decode takes a few ms on 1.7 s of audio and
runs at most every 0.2 s for the first 1.5 s of a carrier, and not again once an ID is found. Each step decodes only
what came since the step before, with ID_OVERLAP_FRAMES before it (a burst that ran across the last step's end is whole
in it), not the whole buffer again (#706). The look-back is decoded only when its frames, or the first two of the
pre-roll, show a carrier (quieting the hiss RISE_DB or more, the squelch's own measure) or an ID burst: a burst that
began before the pre-roll rode on a carrier already partly up, and a carrier that comes up at once (most do) has only
hiss there. Then its first two steps are one decode of the look-back and the buffer together, each ID placed by where
its sync begins (mdc() gives that: App._mdc_at). Tones
are one FFT per frame, on the paging channel only, while it is open. The IDs found live are handed to
the finished transmission (ids()), so the row is named even when the burst is cut off its segment;
a post-ID at release that differs is kept next to it, never instead of it. A key-up window that ends with no ID
is looked at once more for a burst that was there but didn't decode (radio/fm.py id_burst_frames: both tones of
an MDC / FleetSync pair, well over the dead air): burst() hands that to the finished transmission, which says
"ID not decoded" rather than "no ID sent" (#163).

A carrier the squelch cuts in two (radio/fm.py key-ups: a second radio keyed before the hang ran out; or a stronger
carrier over a background) was decoded as one until the cut was made: the squelch holds a key-up cut until the new
carrier has min_open_s of its own, and the first carrier's key-up window, still open when it sent no ID, decoded the
second radio's pre-ID onto it (#672: the first row named the wrong unit). So on a cut, the first carrier's IDs, its
"ID not decoded" flag and its emergency alarm are what its own frames hold (pre-roll up to the drop the cut was made
at), decoded again only when the decode that found them reached past that point. An ID that was the next radio's
moves to the next carrier, its alarm too (raised once, at once): that carrier's own buffer starts where the squelch
counts it back, and a burst that began while it was still rising out of the hiss doesn't decode there again. A held
cut that is never made changes nothing: one carrier, its IDs exactly as without key-ups. When the squelch cuts a
background off a stronger carrier and makes no row of it (fm.py's rise rule: a carrier still rising, #695), what was
decoded there goes on with the stronger carrier, alarm and all.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from sizeup.radio.fm import FRAME, RATE, id_burst_frames

PREROLL_FRAMES = 4      # 0.2 s read before the squelch opened (the start of a key-up burst)
ID_LOOKBACK_FRAMES = 8  # 0.4 s more before a carrier's buffer, for the ID decode only (a slowly rising carrier, #694)
LOOKBACK_STEPS = 2      # ... in its first two decode steps (a burst that ran into the pre-roll is whole by then)
RISE_DB = 2.0           # ... and only when a frame there quieted the hiss this much (a carrier partly up), or held a burst
ID_OVERLAP_FRAMES = 8   # each decode step re-reads this much (0.4 s) before what is new: a whole burst (MDC: 0.18 s,
                        # a double packet 0.27 s; FleetSync 0.05-0.09 s) that ran across the last step's end
HIST_FRAMES = 64        # a traffic channel's latest frames, open or not: where the look-back comes from
MDC_WINDOW_S = 1.5      # pre-IDs come in the first second (older radios at key-up, APX 0.3-0.8 s in)
MDC_EVERY = 4           # decode every 4 frames (0.2 s) of it
MDC_MIN_FRAMES = 4      # ... once there is this much (a burst is ~175 ms)
KEEP_S = 120.0          # a transmission's live IDs are kept this long for its finished segment (handled on the
                        # channel's worker, radio/fm.py SegmentWorkers: later than its close when that is busy)


@dataclass
class _Chan:
    pre: deque = field(default_factory=lambda: deque(maxlen=PREROLL_FRAMES))
    open: bool = False
    since: float = 0.0
    buf: list = field(default_factory=list)     # pre-roll + the first MDC_WINDOW_S of this carrier (kept to its end)
    t0: float = 0.0                             # when buf[0] began (on the squelch's clock: since + frames)
    hist: deque = field(default_factory=lambda: deque(maxlen=HIST_FRAMES))
    shut: deque = field(default_factory=lambda: deque(maxlen=HIST_FRAMES))   # per hist frame: the squelch was closed
    qd: deque = field(default_factory=lambda: deque(maxlen=HIST_FRAMES))     # ... and its quieting (dB; None: unknown)
    rising: bool = True                         # the look-back is worth a decode (a carrier or a burst in it, #706)
    upto: int = 0                               # buf frames read by the decode steps so far
    back: list = field(default_factory=list)    # the ID look-back: the channel's frames just before buf (#694)
    old: int = 0                                # ... how many of back + buf began before the pre-roll
    before: list | None = None                  # the IDs in those alone (looked at once, when needed)
    weak: int = 0                               # ... the last of those the squelch was closed for (no carrier's own)
    lone: list = field(default_factory=list)    # the emergencies in those closed frames alone
    steps: int = 0                              # decode steps run for this carrier
    looked: list = field(default_factory=list)  # IDs found only with the look-back
    pending: list = field(default_factory=list)  # a weak radio's emergency in the closed frames: alarmed on its own
    pkey: str = ""                              # ... its alarm's key
    ptime: float = 0.0                          # ... about when it was sent
    heard: list = field(default_factory=list)   # the frames whose decode raised this carrier's alarm
    prev: tuple | None = None                   # the carrier before's (alarm, its frames' ids, ended at, IDs)
    hold: tuple | None = None                   # the squelch's held key-up cut (drop start, carrier back), its frames
    n: int = 0                                  # frames of this carrier so far
    ids: list = field(default_factory=list)     # [(unit hex, emergency)] decoded live
    done: bool = False                          # stop decoding (found, or past the window)
    last: list = field(default_factory=list)    # [(since, ids, at, alarm, burst)] finished carriers, for ids()
    alarm: str = ""                             # the emergency alarm raised for this carrier (its key)
    burst: bool = False                         # no ID decoded in the key-up window, but a burst was there
    shown: bool = False                         # LiveTalk told (the squelch keyed: not a noise opening)


def _ids(found) -> list[tuple[str, bool]]:
    """mdc()'s answer as (radio ID, emergency), once each in order (it may also say where each sync begins)."""
    return list(dict.fromkeys((x[0], bool(x[1])) for x in found))


class TapSquelch:
    """A receiver's NoiseSquelch with the tap on it: feed() is the squelch's own, then the tap sees the
    frame (and the squelch's state after it). Everything else is the squelch's (open, _start, _frames ...)."""

    def __init__(self, squelch, tap: "LiveTap", channel):
        self.__dict__.update(_sq=squelch, _tap=tap, _ch=channel)

    def feed(self, frame: np.ndarray):
        seg = self._sq.feed(frame)
        self._tap.frame(self._ch, frame, self._sq, seg is not None)
        return seg

    def flush(self, reason: str = "stopped"):
        """The audio stopped (a stall, rtl_fm ended): the squelch closes with no frame after it, so the
        tap is told here (a live page would otherwise stay "going out" with nothing to end it, #116)."""
        seg = self._sq.flush(reason)
        self._tap.stopped(self._ch)
        return seg

    def __getattr__(self, name):
        return getattr(self._sq, name)

    def __setattr__(self, name, value):
        setattr(self._sq, name, value)


class LiveTap:
    def __init__(self, kind: Callable[[str], str], paging=None, live=None,
                 mdc: Callable[[np.ndarray], list[tuple]] | None = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] | None = None):
        self.kind, self.paging, self.live, self.mdc, self.clock, self.log = kind, paging, live, mdc, clock, log
        self._ch: dict[str, _Chan] = {}
        # An emergency packet decoded live: on_emergency(channel, carrier start, [(ID, emergency)]) -> the alarm's key
        self.on_emergency: Callable[[str, float, list[tuple[str, bool]]], str] | None = None
        self._lock = threading.Lock()
        self._errors = 0

    def attach(self, receiver) -> None:
        """On this receiver's squelch, and on every new one it makes (each rtl_fm start)."""
        make = receiver.new_squelch
        receiver.new_squelch = lambda: TapSquelch(make(), self, receiver.channel)
        receiver.squelch = receiver.new_squelch()

    def frame(self, channel, frame: np.ndarray, squelch, made: bool = True) -> None:
        """One frame, after squelch.feed(frame) (TapSquelch; made: that feed returned a transmission). Never raises."""
        try:
            q = getattr(squelch, "quieting_db", None)
            self._frame(channel.label, frame, bool(squelch.open), float(getattr(squelch, "_start", 0.0)),
                        bool(getattr(squelch, "keyed", squelch.open)), getattr(squelch, "_frames", None),
                        getattr(squelch, "_hold", None), made, q if isinstance(q, (int, float)) else None)
        except Exception as e:  # the tap must never stop the receiver
            self._errors += 1
            if self.log is not None and self._errors in (1, 10, 100):
                self.log(f"live tap error ({self._errors}): {type(e).__name__}: {e}")

    def stopped(self, channel) -> None:
        """The receiver's audio stopped (TapSquelch.flush): whatever was open there is closed. Never raises."""
        try:
            with self._lock:
                c = self._ch.get(channel.label)
                was = c is not None and c.open
                if was:
                    self._ended(c)
                    c.open, c.buf, c.back = False, [], []
                    c.pre.clear()
                if c is not None:
                    c.hist.clear()      # (what comes next doesn't follow on from it)
                    c.shut.clear()
                    c.qd.clear()
            if was and self.kind(channel.label) == "paging" and self.paging is not None:
                self.paging.close(channel.label)
        except Exception as e:  # the tap must never stop the receiver
            self._errors += 1
            if self.log is not None and self._errors in (1, 10, 100):
                self.log(f"live tap error ({self._errors}): {type(e).__name__}: {e}")

    def _frame(self, label: str, frame: np.ndarray, is_open: bool, since: float, keyed: bool = True,
               frames: list | None = None, hold: tuple | None = None, made: bool = True,
               q: float | None = None) -> None:
        kind = self.kind(label)
        with self._lock:
            c = self._ch.setdefault(label, _Chan())
            if is_open and (not c.open or abs(since - c.since) > 0.01):
                # A new carrier, or the squelch cut a key-up out of the open one (radio/fm.py keyups, the stronger
                # carrier over a background): the new one's frames so far are the squelch's since its start, never
                # the pre-roll of the carrier before it.
                cut = c.open and bool(frames)
                moved, alarm = self._own(c, since) if cut and kind == "traffic" else ([], "")
                drop = (c.hold[1] - c.hold[0] if self._held(c, since) else 0) if cut else None
                rise = cut and kind == "traffic" and not made and not self._held(c, since)
                if rise and since - c.since <= MDC_WINDOW_S:
                    # The stronger carrier over the one before (fm.py's rise rule), and the squelch made no row of that:
                    # it was the start of this one, still rising (#694, #695). What was decoded there is this one's.
                    moved += [x for x in c.ids if x not in moved]
                    alarm, c.ids, c.alarm = alarm or c.alarm, [], ""
                self._ended(c)
                c.open, c.since, c.n, c.alarm, c.burst, c.shown = True, since, 0, alarm, False, False
                c.ids, c.done = moved, bool(moved)      # (the next radio's ID decoded across the cut: this carrier's)
                c.buf = list(frames[:-1]) if cut else list(c.pre)
                c.n = len(c.buf) if cut else 0
                # buf[len(buf)] is this frame, the squelch's frames[-1] (a reopen hands it several at once)
                c.t0 = since + ((len(frames) if frames else 1) - 1 - len(c.buf)) * FRAME / RATE
                c.pre.clear()
                self._look_back(c, since, drop)
            c.hold = tuple(hold[:2]) if is_open and hold else None
            if not is_open:
                was = c.open
                if was:
                    self._ended(c)
                    c.open, c.buf = False, []
                c.pre.append(frame)
            else:
                c.n += 1
            if kind == "traffic":
                c.hist.append(frame)
                c.shut.append(not is_open)
                c.qd.append(q)
        if not is_open:
            if was and kind == "paging" and self.paging is not None:
                self.paging.close(label)
            return
        if kind == "paging":
            if self.paging is not None:
                self.paging.feed(label, since, frame)
            return
        if kind != "traffic" or self.live is None:
            return
        if keyed and not c.shown:
            # Shown once the carrier has held min_open_s (NoiseSquelch.keyed), from when it opened: a noise opening
            # (one frame over open_db, then the hang) is never "TX". The MDC decode and the emergency alarm don't wait.
            c.shown = True
            self.live.fm_open(label, since)
            if c.ids:
                self.live.fm_ids(label, list(dict.fromkeys(h for h, _e in c.ids)))
        self._mdc_step(label, c, frame)

    def _mdc_step(self, label: str, c: _Chan, frame: np.ndarray) -> None:
        if c.done or self.mdc is None:
            return
        c.buf.append(frame)
        past = c.n * FRAME >= MDC_WINDOW_S * RATE
        if not past and (c.n < MDC_MIN_FRAMES or c.n % MDC_EVERY):
            return
        c.steps += 1
        back = c.steps <= LOOKBACK_STEPS and c.rising
        # what is new since the last step, with ID_OVERLAP_FRAMES before it (#706); the look-back's steps: all of it
        a = 0 if back else max(0, c.upto - ID_OVERLAP_FRAMES)
        read = c.buf[a:]
        c.upto = len(c.buf)
        found = self._decode(c, read, np.concatenate(read).astype(np.float32), back=back)
        if c.pending and not c.pkey and self.on_emergency is not None:
            # the button, at once: its own alarm (the radio that opened the squelch may be another, with its own ID)
            c.pkey = self.on_emergency(label, c.ptime, list(c.pending)) or "raised"
        if past:   # once per carrier, at the window's end: the whole buffer
            c.burst = not found and any(id_burst_frames(np.concatenate(c.buf).astype(np.float32)))
            c.done = True
        if found:
            c.ids, c.done = list(found), True
            if any(e for _h, e in found) and not c.alarm and self.on_emergency is not None:
                em = [x for x in found if x[1]]
                again = all(x in c.looked for x in em) and self._raised(c, em)
                c.alarm = again or self.on_emergency(label, c.since, list(found)) or "raised"
                c.heard = (c.back if c.looked else []) + list(read)     # the frames the decode that found it read
            if c.looked and self.log is not None:   # (how often a burst began before the pre-roll: #694)
                self.log(f"{label}: radio ID decoded from before the squelch opened (look-back)")
            if c.shown:
                self.live.fm_ids(label, list(dict.fromkeys(h for h, _e in found)))

    def _own(self, c: _Chan, since: float) -> tuple[list, str]:
        """The open carrier is cut, the next one starting at since: its IDs, burst flag and alarm become what its own
        frames hold (up to the drop the squelch held the cut at, else up to since), if the decode that set them reached
        past that point (#672: the next radio's pre-ID decoded onto it while the squelch held the cut).
        -> (the IDs and the alarm key that move on with the next carrier)."""
        until, step, tail = since, FRAME / RATE, 0
        if c.hold is not None and abs(c.since + c.hold[1] * step - since) < step / 2:
            until = c.since + c.hold[0] * step          # the cut made is the one held: its drop began at g0
            tail = 1                # (and g0 still holds the end of a burst sent to the last moment: decoded with it)
        k = round((until - c.t0) / step)
        if self.mdc is None or k + tail >= len(c.buf):
            return [], ""                   # every decode was of its own frames (or none ran): as it was
        own = c.buf[:max(0, k)]
        audio = np.concatenate(own).astype(np.float32) if own else np.zeros(0, np.float32)
        lead = c.buf[:max(0, k + tail)]
        ids = (self._decode(c, lead, np.concatenate(lead).astype(np.float32), back=bool(c.looked))
               if len(own) >= MDC_MIN_FRAMES else [])
        moved = [x for x in c.ids if x not in ids]
        c.ids, c.done = ids, True
        c.burst = not ids and len(own) >= MDC_MIN_FRAMES and any(id_burst_frames(audio))
        if c.alarm and any(e for _h, e in moved) and not any(e for _h, e in ids):
            alarm, c.alarm = c.alarm, ""    # raised for the next radio's button: that carrier's, not raised again
            return moved, alarm
        return moved, ""

    def _held(self, c: _Chan, since: float) -> bool:
        """The cut made at since is the key-up cut the squelch held (not the stronger carrier over a background)."""
        step = FRAME / RATE
        return c.hold is not None and abs(c.since + c.hold[1] * step - since) < step / 2

    def _look_back(self, c: _Chan, since: float, drop: int | None) -> None:
        """The carrier just begun at since (its buf, t0 set): its ID look-back, the channel's frames just before buf.
        Its pre-roll: the PREROLL_FRAMES just before since when it opened (the carrier before ended at least the hang
        earlier), on a cut only the drop it was cut at, less its first frame (still partly the carrier before: drop
        frames; 0: the rise rule, no drop)."""
        n, h, shut, qd = len(c.buf), list(c.hist), list(c.shut), list(c.qd)
        c.back, c.old, c.before, c.weak, c.lone, c.steps, c.looked = [], 0, None, 0, [], 0, []
        c.pending, c.pkey, c.ptime, c.heard, c.upto, c.rising = [], "", since, [], 0, True
        if len(shut) != len(h) or (n and (len(h) < n or h[-1] is not c.buf[-1])):
            return              # (not the frames the tap saw last: never guess)
        b0 = max(0, len(h) - n - ID_LOOKBACK_FRAMES)
        c.back, shut = h[b0:len(h) - n], shut[b0:]
        at = len(c.back) + round((since - c.t0) * RATE / FRAME)  # since's place in back + buf
        c.old = max(0, at - min(PREROLL_FRAMES, PREROLL_FRAMES if drop is None else drop - 1))
        while c.weak < c.old and shut[c.old - 1 - c.weak]:
            c.weak += 1
        if c.back and len(qd) == len(h):
            # (#706) a burst that began before the pre-roll rode on a carrier already up there: its quieting shows in
            # the look-back or the pre-roll's first two frames (never its last: a carrier that came up at once is
            # partly in that one), else a burst does. Neither (hiss only, most key-ups): the look-back isn't decoded.
            qs = qd[b0:b0 + c.old + 2]
            c.rising = (any(x is None or x >= RISE_DB for x in qs)
                        or any(id_burst_frames(np.concatenate((c.back + c.buf)[:c.old + 2]))))

    def _decode(self, c: _Chan, frames: list, audio: np.ndarray, back: bool = True) -> list:
        """The IDs in these frames of the carrier (from buf[0]; audio: them joined), and with back, the IDs of a burst
        that began in its look-back and ran on into its pre-roll (#694). One that ended before that is not this
        carrier's; an emergency wholly in frames the squelch was closed for (a weak radio's button) is held apart
        (pending): alarmed on its own, and this carrier's only if it names no other radio (_mine)."""
        if not (back and c.back):
            return _ids(self.mdc(audio))
        lead = c.back + list(frames)
        got = list(self.mdc(np.concatenate(lead).astype(np.float32)))
        if all(len(x) > 2 for x in got):
            # one decode of both (#706): an ID whose sync begins in the carrier's own frames is the one its buffer alone
            # decodes; one that begins in the look-back, only the look-back's
            edge = len(c.back) * FRAME / RATE
            found = _ids([x for x in got if x[2] >= edge])
        else:                   # (an mdc() that doesn't say where: its buffer decoded on its own)
            found = _ids(self.mdc(audio))
        more = [x for x in _ids(got) if x not in found]
        if more:
            if c.before is None:
                old = (c.back + c.buf)[:c.old]
                c.before = _ids(self.mdc(np.concatenate(old).astype(np.float32))) if old else []
                lone = old[len(old) - c.weak:] if c.weak else []
                c.lone = [x for x in _ids(self.mdc(np.concatenate(lone).astype(np.float32))) if x[1]] if lone else []
            lone = [x for x in more if x in c.before and x in c.lone and x not in c.pending]
            if lone:
                c.pending += lone
                c.ptime = c.t0 - (len(c.back) - (c.old - c.weak)) * FRAME / RATE
            more = [x for x in more if x not in c.before]
            c.looked += [x for x in more if x not in c.looked]
        return found + more

    def _raised(self, c: _Chan, em: list) -> str:
        """The alarm the carrier just before raised for these emergencies from the same burst: its decode read this
        carrier's pre-roll too (a burst that ran across both: one press, one alarm). A second press is its own ("")."""
        p = c.prev
        if not em or p is None or self.clock() - p[2] > MDC_WINDOW_S or not all(x in p[3] for x in em):
            return ""
        return p[0] if any(id(f) in p[1] for f in (c.back + c.buf)[c.old:]) else ""

    def _mine(self, c: _Chan) -> tuple[list, str, bool]:
        """The carrier's (IDs, alarm, burst flag) for its row: with the weak radio's emergency held apart when it names
        no other radio (that radio came up strong itself)."""
        ids, alarm, burst = list(c.ids), c.alarm, c.burst
        if c.pending and {h for h, _e in ids} <= {h for h, _e in c.pending}:
            ids += [x for x in c.pending if x not in ids]
            alarm, burst = alarm or c.pkey, False
        return ids, alarm, burst

    def _ended(self, c: _Chan) -> None:
        if c.open and not c.done and not c.ids and len(c.buf) >= MDC_MIN_FRAMES and self.mdc is not None:
            c.burst = any(id_burst_frames(np.concatenate(c.buf)))    # closed inside the window (a short key-up)
        if c.open:
            # every carrier, with an ID or none: a transmission's own is always the latest that began before it, so it
            # can never take the ID of an older one (a key-up with no ID seconds after one with an ID)
            now = self.clock()
            ids, alarm, burst = self._mine(c)
            c.last = [x for x in c.last if now - x[2] < KEEP_S] + [(c.since, ids, now, alarm, burst)]
            c.prev = (c.alarm, {id(f) for f in c.heard}, now, list(c.ids)) if c.alarm and c.heard else None

    def ids(self, label: str, start: float, end: float) -> list[tuple[str, bool]]:
        """The IDs decoded live from the carrier that made the transmission start..end (its pre-ID)."""
        got = self._carrier(label, start, end)
        return list(got[1]) if got else []

    def alarm_key(self, label: str, start: float, end: float) -> str:
        """The emergency alarm raised live for the carrier that made this transmission ("" none)."""
        got = self._carrier(label, start, end)
        return got[3] if got and got[3] != "raised" else ""

    def burst(self, label: str, start: float, end: float) -> bool:
        """The key-up window of this transmission's carrier had an ID burst that didn't decode (#163)."""
        got = self._carrier(label, start, end)
        return bool(got and got[4])

    def _carrier(self, label: str, start: float, end: float):
        with self._lock:
            c = self._ch.get(label)
            if c is None:
                return None
            cands = list(c.last) + ([(c.since, *self._mine(c)[:1], 0.0, *self._mine(c)[1:])] if c.open else [])
        mine = [x for x in cands if x[0] <= start + 1.0 and x[0] < end and start - x[0] < 150]
        return max(mine, key=lambda x: x[0]) if mine else None      # the latest carrier that began before it
