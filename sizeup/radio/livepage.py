"""A page going out now on the paging channel (South 2): who is being paged, while the carrier is up.

The finished page (radio/page.py) is only built when the squelch closes, 10-60 s after the tones. This
runs the same tone decoder (radio/tones.py ToneDetector) on the channel's audio as it arrives: the
live tap (radio/tap.py) hands it each 50 ms frame of an open carrier as the receiver reads it, so it
costs one 100 ms FFT per 50 ms of audio, only while the carrier is up.

    tones   a pair is shown as soon as it is certain: A, then 1.5 s of B (longer than any A, #115; B's frequency is settled when
            B ends; a single run past 3.5 s is an A=B code); stacked departments are added one by one
    voice   the tones have stopped and someone is talking (speech over the carrier's dead air); a pair after
            that is the next page's (#183): shown as a page of its own, from its tones
    ended   the page is built (App._segment -> finished()): kept RECENT_S as "ended" so the screens
            can hand over to the finished page row without a gap

The carrier dropping for a moment (a distant repeater fading, #61) does not end it: a carrier back
within HOLD_S carries on with the same page; the state only goes when the page is finished, or
HOLD_S after the carrier went with nothing finished (the receiver restarted, the dongle was switched).

Each pair: {a, b, names, ours}. names come from the tone plan as they would on the finished page
(" (heard)" / " (est.)" kept: how sure the code is); an unknown pair has no names and is shown as its
tones, never guessed. ours: one of the names is ours (config tones `ours`, the CAD's monitor
departments). Nothing here is written anywhere: the finished page logs unknown pairs (once per page).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

from sizeup.radio.fm import RATE, speech_seconds
from sizeup.radio.tones import ToneDetector, ToneEvent, TonePlan, base_name

HOLD_S = 4.0          # the carrier back this soon: the same page (a fade, not a new key-up)
RECENT_S = 4.0        # a finished page's live state stays this long, marked ended
VOICE_MIN_S = 0.5     # this much audio after the last tone before "voice" is considered
VOICE_SPEECH_S = 0.3  # ... and this much of it speech over the dead air
TAIL_S = 4.0          # the audio after the last tone kept for that check
VOICE_STEP_S = 0.2    # how often it is checked
NEXT_PAGE_VOICE_S = 1.0   # a pair after this much "voice": the next page's tones (#183, page.SPLIT_VOICE_S)


def _noop() -> None:
    pass




@dataclass
class _Live:
    channel: str
    started: float                     # wall clock: the carrier that brought the first tone
    seg_since: float                   # the carrier being fed now
    det: ToneDetector
    pairs: list[dict] = field(default_factory=list)   # {a, b, names, ours, at (tone start, wall clock)}
    phase: str = "tones"
    tail: np.ndarray = field(default_factory=lambda: np.zeros(0))
    fed_until: float = 0.0             # wall clock at the end of the audio fed so far
    closed_at: float | None = None
    ended: float | None = None
    page_id: str = ""
    voice_s: float = 0.0               # audio heard since "voice" (no tone ringing)


class LivePaging:
    def __init__(self, plan: TonePlan, ours: Iterable[str] = (), clock: Callable[[], float] = time.time,
                 on_change: Callable[[], None] = _noop, rate: int = RATE, hold_s: float = HOLD_S):
        self.plan, self.clock, self.on_change, self.rate = plan, clock, on_change, rate
        self.hold_s = hold_s        # at least the channel's continuation window (#61), so a held page stays shown
        self.ours = {base_name(n) for n in ours}
        self._lock = threading.Lock()
        self._ch: dict[str, _Live] = {}

    def is_ours(self, names: Iterable[str]) -> bool:
        return any(base_name(n) in self.ours for n in names)

    # -- fed by the live tap (radio/tap.py) -----------------------------------------------------------------

    def feed(self, channel: str, since: float, audio: np.ndarray) -> bool:
        """New audio of the transmission on `channel` that opened at `since` (wall clock), in order.
        True when what is shown changed (on_change has been called)."""
        with self._lock:
            cur = self._ch.get(channel)
            if cur is not None and cur.seg_since != since:
                gap = since - (cur.closed_at if cur.closed_at is not None else cur.fed_until)
                if cur.ended is None and gap <= self.hold_s:
                    # The carrier came back: the same page. A fresh decoder (the audio isn't continuous).
                    cur.seg_since, cur.det, cur.closed_at, cur.tail = since, ToneDetector(self.rate, t0=since), None, np.zeros(0)
                else:
                    cur = None
            elif cur is not None and cur.ended is not None:
                return False            # this transmission's page is built already (a late snapshot)
            if cur is None:
                cur = self._ch[channel] = _Live(channel, since, since, ToneDetector(self.rate, t0=since))
            before = self._sig(cur)
            x = np.asarray(audio, dtype=np.float64)
            cur.fed_until = max(cur.fed_until, since) + len(x) / self.rate
            for ev in cur.det.feed(x):
                self._pair(cur, ev, final=True)
            pend = cur.det.pending()
            if pend is not None:
                self._pair(cur, pend, final=False)
            self._phase(cur, x)
            changed = self._sig(cur) != before
        if changed:
            self.on_change()
        return changed

    def close(self, channel: str) -> bool:
        """The squelch closed: the tone that was ringing is decoded to its end; the page stays shown
        (hold_s for the carrier to come back, or until finished())."""
        with self._lock:
            cur = self._ch.get(channel)
            if cur is None or cur.closed_at is not None or cur.ended is not None:
                return False
            before = self._sig(cur)
            for ev in cur.det.flush():
                self._pair(cur, ev, final=True)
            cur.closed_at = self.clock()
            changed = self._sig(cur) != before
        if changed:
            self.on_change()
        return changed

    def finished(self, channel: str, page_id: str = "", end: float | None = None) -> bool:
        """The page on `channel` is built (App._segment): shown as ended for RECENT_S. end (wall clock): where that
        page ended; one that ended before the page shown now started is an earlier page of the same carrier (cut
        off by these tones, #183), and the one shown now goes on."""
        with self._lock:
            cur = self._ch.get(channel)
            if cur is None or cur.ended is not None or (end is not None and end <= cur.started):
                return False
            if not cur.pairs:
                del self._ch[channel]       # never shown: nothing to hand over
                return False
            cur.ended, cur.page_id = self.clock(), page_id
        self.on_change()
        return True

    # -- what it shows ------------------------------------------------------------------------------

    def state(self) -> list[dict]:
        """Pages going out now (and just finished, ended=True), one per paging channel, newest first."""
        now = self.clock()
        out = []
        with self._lock:
            for ch, cur in list(self._ch.items()):
                if cur.ended is not None and now - cur.ended > RECENT_S:
                    del self._ch[ch]
                    continue
                if cur.ended is None and cur.closed_at is not None and now - cur.closed_at > self.hold_s:
                    del self._ch[ch]
                    continue
                if not cur.pairs:
                    continue
                out.append(self._entry(cur))
        return sorted(out, key=lambda e: -e["started"])

    def _entry(self, cur: _Live) -> dict:
        return {"key": f"page:{cur.channel}:{cur.started:.2f}", "channel": cur.channel, "started": cur.started,
                "pairs": [{k: p[k] for k in ("a", "b", "names", "ours")} for p in cur.pairs], "phase": cur.phase,
                "ours": any(p["ours"] for p in cur.pairs), "ended": cur.ended is not None,
                "until": cur.ended + RECENT_S if cur.ended is not None else None, "page_id": cur.page_id}

    @staticmethod
    def _sig(cur: _Live) -> tuple:
        return (tuple((p["a"], p["b"], tuple(p["names"])) for p in cur.pairs), cur.phase, cur.ended)

    # -- inside -------------------------------------------------------------------------------------

    def _pair(self, cur: _Live, ev: ToneEvent, final: bool) -> None:
        """A decoded pair (final) or the one ringing now: added, or the same pair settled (its B, or
        an A=B code that turned out a long tone)."""
        ev = self.plan.match(ev)
        a, b = round(ev.a_hz, 1), (round(ev.b_hz, 1) if ev.b_hz else None)
        names = list(ev.names)
        p = {"a": a, "b": b, "names": names, "ours": self.is_ours(names),
             "at": round(ev.start, 2), "final": final}
        for i, q in enumerate(cur.pairs):
            if abs(q["at"] - p["at"]) >= 0.06:
                continue
            if not q["final"] and (final or q["names"] != names or (q["b"] is None) != (b is None)
                                   or abs((q["b"] or 0) - (b or 0)) > 1.0):
                cur.pairs[i] = p        # settled, or it moved enough to say so
            return
        if cur.phase == "voice" and cur.voice_s >= NEXT_PAGE_VOICE_S:
            # Tones after the message: the next page (#183), not more of this one; the finished pages say the rest
            cur.pairs, cur.started = [], ev.start
        cur.phase, cur.voice_s = "tones", 0.0
        cur.pairs.append(p)
        cur.tail = np.zeros(0)

    def _phase(self, cur: _Live, x: np.ndarray) -> None:
        if not cur.pairs:
            return
        if cur.det.ringing:
            cur.tail = np.zeros(0)
            return
        if cur.phase == "voice":
            cur.voice_s += len(x) / self.rate
        n0 = len(cur.tail)
        cur.tail = np.concatenate([cur.tail, x])[-int(TAIL_S * self.rate):]
        step = int(VOICE_STEP_S * self.rate)       # looked at every 0.2 s of audio, not every frame
        if (cur.phase == "tones" and len(cur.tail) >= VOICE_MIN_S * self.rate
                and (len(cur.tail) // step != n0 // step or len(x) >= step)
                and speech_seconds(cur.tail, self.rate) >= VOICE_SPEECH_S):
            cur.phase = "voice"
