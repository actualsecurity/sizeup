"""The live tap: each analog receiver's 50 ms frames as they are read, for what must show while the
carrier is still up. One tap for every FM channel: LiveTap.attach(receiver) puts it on the receiver's
squelch (TapSquelch: the squelch sees each frame first, then the tap, then a finished transmission
goes on to App._segment), with no change to the receiver or the squelch themselves (radio/fm.py):

    paging  (South 2)   every frame of an open carrier -> LivePaging (radio/livepage.py): who is
                        being paged, pair by pair, then "voice"
    traffic (FG1)       the first MDC_WINDOW_S of an open carrier, with the PREROLL_FRAMES read just
                        before the squelch opened -> the MDC-1200 decoder every MDC_EVERY frames until
                        an ID decodes -> LiveTalk.fm_ids: the talker shown at key-up (a pre-ID)

Why the pre-roll: the squelch opens one frame (50 ms) or more after the carrier comes up, and a radio
that sends its pre-ID at key-up starts the burst with the carrier. The burst's preamble is 47 ms; lose
more than that and the sync is gone (2026-10-13 13:17, E63 on FG1: the talker only showed at release,
from the post-ID). The frames before the open are only hiss and the start of the carrier; packets are
CRC-checked, so hiss can't make an ID.

Cheap on purpose (the station is an i5-6400T): the MDC decode takes a few ms on 1.7 s of audio and
runs at most every 0.2 s for the first 1.5 s of a carrier, and not again once an ID is found; tones
are one FFT per frame, on the paging channel only, while it is open. The IDs found live are handed to
the finished transmission (ids()), so the row is named even when the burst is cut off its segment;
a post-ID at release that differs is kept next to it, never instead of it.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from sizeup.radio.fm import FRAME, RATE

PREROLL_FRAMES = 4      # 0.2 s read before the squelch opened (the start of a key-up burst)
MDC_WINDOW_S = 1.5      # pre-IDs come in the first second (older radios at key-up, APX 0.3-0.8 s in)
MDC_EVERY = 4           # decode every 4 frames (0.2 s) of it
MDC_MIN_FRAMES = 4      # ... once there is this much (a burst is ~175 ms)
KEEP_S = 30.0           # a transmission's live IDs are kept this long for its finished segment


@dataclass
class _Chan:
    pre: deque = field(default_factory=lambda: deque(maxlen=PREROLL_FRAMES))
    open: bool = False
    since: float = 0.0
    buf: list = field(default_factory=list)     # pre-roll + the first MDC_WINDOW_S of this carrier
    n: int = 0                                  # frames of this carrier so far
    ids: list = field(default_factory=list)     # [(unit hex, emergency)] decoded live
    done: bool = False                          # stop decoding (found, or past the window)
    last: list = field(default_factory=list)    # [(since, ids, at)] finished carriers, for ids()


class TapSquelch:
    """A receiver's NoiseSquelch with the tap on it: feed() is the squelch's own, then the tap sees the
    frame (and the squelch's state after it). Everything else is the squelch's (open, _start, _frames ...)."""

    def __init__(self, squelch, tap: "LiveTap", channel):
        self.__dict__.update(_sq=squelch, _tap=tap, _ch=channel)

    def feed(self, frame: np.ndarray):
        seg = self._sq.feed(frame)
        self._tap.frame(self._ch, frame, self._sq)
        return seg

    def __getattr__(self, name):
        return getattr(self._sq, name)

    def __setattr__(self, name, value):
        setattr(self._sq, name, value)


class LiveTap:
    def __init__(self, kind: Callable[[str], str], paging=None, live=None,
                 mdc: Callable[[np.ndarray], list[tuple[str, bool]]] | None = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] | None = None):
        self.kind, self.paging, self.live, self.mdc, self.clock, self.log = kind, paging, live, mdc, clock, log
        self._ch: dict[str, _Chan] = {}
        self._lock = threading.Lock()
        self._errors = 0

    def attach(self, receiver) -> None:
        """On this receiver's squelch, and on every new one it makes (each rtl_fm start)."""
        make = receiver.new_squelch
        receiver.new_squelch = lambda: TapSquelch(make(), self, receiver.channel)
        receiver.squelch = receiver.new_squelch()

    def frame(self, channel, frame: np.ndarray, squelch) -> None:
        """One frame, after squelch.feed(frame) (TapSquelch). Never raises."""
        try:
            self._frame(channel.label, frame, bool(squelch.open), float(getattr(squelch, "_start", 0.0)))
        except Exception as e:  # the tap must never stop the receiver
            self._errors += 1
            if self.log is not None and self._errors in (1, 10, 100):
                self.log(f"live tap error ({self._errors}): {type(e).__name__}: {e}")

    def _frame(self, label: str, frame: np.ndarray, is_open: bool, since: float) -> None:
        kind = self.kind(label)
        with self._lock:
            c = self._ch.setdefault(label, _Chan())
            if is_open and (not c.open or abs(since - c.since) > 0.01):
                self._ended(c)
                c.open, c.since, c.n, c.ids, c.done = True, since, 0, [], False
                c.buf = list(c.pre)
                c.pre.clear()
                opened = True
            else:
                opened = False
            if not is_open:
                was = c.open
                if was:
                    self._ended(c)
                    c.open, c.buf = False, []
                c.pre.append(frame)
            else:
                c.n += 1
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
        if opened:
            self.live.fm_open(label, since)     # at the frame the squelch opened, not the next 0.2 s poll
        self._mdc_step(label, c, frame)

    def _mdc_step(self, label: str, c: _Chan, frame: np.ndarray) -> None:
        if c.done or self.mdc is None:
            return
        c.buf.append(frame)
        past = c.n * FRAME >= MDC_WINDOW_S * RATE
        if not past and (c.n < MDC_MIN_FRAMES or c.n % MDC_EVERY):
            return
        found = self.mdc(np.concatenate(c.buf).astype(np.float32))
        if past:
            c.done, c.buf = True, []
        if found:
            c.ids, c.done, c.buf = list(found), True, []
            self.live.fm_ids(label, list(dict.fromkeys(h for h, _e in found)))

    def _ended(self, c: _Chan) -> None:
        if c.open and c.ids:
            now = self.clock()
            c.last = [x for x in c.last if now - x[2] < KEEP_S] + [(c.since, list(c.ids), now)]

    def ids(self, label: str, start: float, end: float) -> list[tuple[str, bool]]:
        """The IDs decoded live from the carrier that made the transmission start..end (its pre-ID)."""
        with self._lock:
            c = self._ch.get(label)
            if c is None:
                return []
            cands = list(c.last) + ([(c.since, list(c.ids), 0.0)] if c.open and c.ids else [])
        mine = [(since, ids) for since, ids, _at in cands if since <= start + 1.0 and since < end and start - since < 150]
        return list(max(mine)[1]) if mine else []      # the latest carrier that began before it
