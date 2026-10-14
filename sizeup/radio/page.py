"""Paging channel transmissions: alert tones + the voice page that follows.

A page on South 2 is one keyed transmission: tone A/B pairs (one per department paged,
stacked) and then the dispatcher's voice. Every transmission from the squelch goes through
the tone decoder:

    tones found   -> Page(kind="page"): departments from the tone plan, voice = audio after
                     the last tone (what speech-to-text gets)
    no tones      -> Page(kind="voice"): plain dispatcher traffic on the paging channel
                     (repeats, "time out", updates)

Every decoded tone pair is kept with its page, matched or not, so the real codes for PCFD /
Rye Brook can be learned from the air and named: in the config, or from the console ("Name these
tones" on the page's row, Settings section "tones", #80).

Fades (GitHub #61): South 2 is simulcast, and a page can drop under the squelch for a moment and come
back. When the squelch says a transmission may still continue (Segment.hold_until), PageHold keeps
the page open: shown as pending at once, but not finalised (speech-to-text, the hub) until the window
has passed with nothing more. A no-tone transmission the squelch opened in that window (Segment.cont)
is appended to it, one page and one recording, with the time between kept as silence of the same
length (the voice reads as it was said; whisper skips silence). Tones in it: a new page.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable

import numpy as np

from sizeup.radio.fm import FRAME, MAX_FRAMES, RATE, Segment
from sizeup.radio.tones import ToneDetector, ToneEvent, TonePlan

VOICE_GUARD_S = 0.15   # skip this much after the last tone (tone tail, squelch-tail clicks)
MIN_VOICE_S = 0.8      # shorter than this after the tones: tones only, nothing to transcribe
MAX_PAGE_S = MAX_FRAMES * FRAME / RATE   # a joined page is never longer than one transmission may be (2 min)
HOLD_SLACK_S = 0.5     # finalise this long after the squelch's window ends (frames still in the pipe)
TONES_AFTER_BREAK_S = 1.5   # tones this soon after the carrier came back inside the hang: a new page
REASONS = {"quiet": "quiet after hang", "max length": "max length", "stall": "stall (rtl_fm sent no audio)",
           "stopped": "receiver stopped", "rtl_fm ended": "rtl_fm ended", "tones": "new page's tones",
           "drop": "dropped to a weaker carrier", "background": "background carrier",
           "steady": "nothing on the carrier for 30 s (noise held the squelch)"}


@dataclass
class Page:
    id: str
    label: str                     # channel label, "South 2"
    mhz: float
    start: float                   # wall clock
    end: float
    kind: str                      # "page" (tones present) | "voice"
    tones: list[ToneEvent] = field(default_factory=list)
    voice_offset_s: float = 0.0    # where the voice starts inside `audio`
    audio: np.ndarray | None = field(default=None, repr=False)  # whole transmission, 16 kHz
    quieting_db: float = 0.0
    status: str = "pending"        # pending | done | no_voice | error
    raw: str = ""                  # speech-to-text output
    confidence: float | None = None
    parts: list = field(default_factory=list, repr=False)  # per transmission in it: squelch diagnostics

    @property
    def departments(self) -> list[str]:
        out: list[str] = []
        for t in self.tones:
            for n in t.names:
                if n not in out:
                    out.append(n)
        return out

    @property
    def voice(self) -> np.ndarray | None:
        if self.audio is None:
            return None
        return self.audio[int(self.voice_offset_s * RATE):]

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "mhz": self.mhz, "start": round(self.start, 2),
                "end": round(self.end, 2), "kind": self.kind, "status": self.status,
                "departments": self.departments, "quieting_db": round(self.quieting_db, 1),
                "tones": [{"kind": t.kind, "a_hz": round(t.a_hz, 1),
                           "b_hz": round(t.b_hz, 1) if t.b_hz else None, "names": list(t.names)}
                          for t in self.tones],
                "raw": self.raw, "confidence": self.confidence, "voice_offset_s": round(self.voice_offset_s, 3)}


def page_from_dict(d: dict) -> Page:
    """A saved page (Page.as_dict + dispatch), reloaded after a restart; audio stays on disk."""
    tones = [ToneEvent(t["kind"], t["a_hz"], t.get("b_hz"), 0.0, 0.0, tuple(t.get("names", ())))
             for t in d.get("tones", [])]
    p = Page(d["id"], d["label"], d["mhz"], d["start"], d["end"], d["kind"], tones, d.get("voice_offset_s", 0.0), None,
             d.get("quieting_db", 0.0), d.get("status", "done"), d.get("raw", ""), d.get("confidence"))
    p.dispatch = d.get("dispatch")  # type: ignore[attr-defined]
    p.history = d.get("history") or []  # type: ignore[attr-defined]
    p.rerun_at = d.get("rerun_at")  # type: ignore[attr-defined]
    p.model = d.get("model", "")  # type: ignore[attr-defined]
    p.stt = d.get("stt", "")  # type: ignore[attr-defined]   (aws | both: Amazon Transcribe on the hub)
    if d.get("alt_model"):
        p.alt_text, p.alt_model = d.get("alt_text", ""), d["alt_model"]  # type: ignore[attr-defined]
    return p


def _tones(seg: Segment, plan: TonePlan) -> list[ToneEvent]:
    det = ToneDetector(RATE)
    return plan.match_all(det.feed(seg.audio) + det.flush())


def _part(seg: Segment, gap: float | None = None) -> dict:
    return {"start": seg.start, "end": seg.end, "reason": seg.reason, "q_open": seg.q_open,
            "q_close": seg.q_close, "hiss_db": seg.hiss_db, "floor_db": seg.floor_db, "gap": gap}


def assemble(label: str, mhz: float, seg: Segment, plan: TonePlan,
             tones: list[ToneEvent] | None = None) -> Page:
    """Classify one squelched transmission into a page (with tones) or plain voice."""
    tones = _tones(seg, plan) if tones is None else tones
    page = Page(uuid.uuid4().hex[:10], label, mhz, seg.start, seg.end, "page" if tones else "voice",
                tones, 0.0, seg.audio, seg.quieting_db, parts=[_part(seg)])
    if tones:
        page.voice_offset_s = min(len(seg.audio) / RATE, max(t.end for t in tones) + VOICE_GUARD_S)
        if len(seg.audio) / RATE - page.voice_offset_s < MIN_VOICE_S:
            page.status = "no_voice"  # tones only (or the voice came in a separate transmission)
    return page


def join(page: Page, seg: Segment) -> Page:
    """Append a transmission that continues `page` (the carrier came back): the time between is
    silence of the same length, so offsets in the page (voice_offset_s, the tones) are unchanged."""
    gap = max(0.0, seg.start - page.end)
    audio = np.zeros(0, np.float32) if page.audio is None else page.audio.astype(np.float32)
    n0, n1 = len(audio), len(seg.audio)
    page.audio = np.concatenate([audio, np.zeros(int(round(gap * RATE)), np.float32), seg.audio.astype(np.float32)])
    page.quieting_db = float((page.quieting_db * n0 + seg.quieting_db * n1) / max(1, n0 + n1))
    page.end = page.start + len(page.audio) / RATE
    if page.status == "no_voice" and len(page.audio) / RATE - page.voice_offset_s >= MIN_VOICE_S:
        page.status = "pending"       # the voice of a tones-only page, back after a fade
    page.parts.append(_part(seg, gap))
    return page


def split_at_new_tones(seg: Segment, tones: list[ToneEvent]) -> list[Segment]:
    """The long hang can hold the squelch through the end of one page into the next one's key-up
    (which the 0.4 s hang would have made its own transmission). Where the carrier came back inside
    the hang (Segment.breaks) and tones start right there, after voice, cut: a new page starts."""
    cuts = []
    for g0, g1 in seg.breaks:
        before = [t.end for t in tones if t.end <= g0 + 0.05]
        new = any(g1 - 0.1 <= t.start <= g1 + TONES_AFTER_BREAK_S for t in tones)
        if new and g0 - (max(before) if before else 0.0) >= MIN_VOICE_S:
            cuts.append((g0, g1))
    if not cuts:
        return [seg]
    out, t0, last = [], 0.0, None
    for g0, g1 in cuts + [(len(seg.audio) / RATE, None)]:
        a = seg.audio[int(t0 * RATE):int(g0 * RATE)]
        piece = replace(seg, start=seg.start + t0, end=seg.start + t0 + len(a) / RATE, audio=a,
                        breaks=[(b0 - t0, b1 - t0) for b0, b1 in seg.breaks if t0 < b1 <= g0],
                        bursts=seg.bursts if not out else [])
        if g1 is not None:   # not the last piece: it ended on the next page's key-up, nothing waits for it
            piece.reason, piece.hold_until = "tones", 0.0
        if out:
            piece.cont = False
        out.append(piece)
        t0 = g1 if g1 is not None else t0
    return out


def recording_note(page: Page) -> str:
    """One log line per paging recording end (journalctl on the station): enough to tell why the
    next cut-off page ended where it did (GitHub #61)."""
    first, last = (page.parts[0], page.parts[-1]) if page.parts else ({}, {})

    def db(v):
        return "?" if v is None else f"{v:.1f} dB"
    gaps = [p["gap"] for p in page.parts[1:]]
    joined = (f"{len(gaps)} continuation(s) joined (gap {', '.join(f'{g:.1f}' for g in gaps)} s)"
              if gaps else "no continuation")
    floor = f" · background carrier {db(last['floor_db'])}" if last.get("floor_db") is not None else ""
    return (f"{page.label} recording end: {page.kind} {page.end - page.start:.1f} s · "
            f"{REASONS.get(last.get('reason') or '', last.get('reason') or 'ended')} · quieting {db(first.get('q_open'))} "
            f"at open, {db(last.get('q_close'))} at close · idle hiss {db(last.get('hiss_db'))} (relative){floor} · {joined}")


class PageHold:
    """Page assembly on paging channels: a page waits while its channel may continue it (above).

    add() takes each transmission; due() hands back the pages whose window has passed (called a few
    times a second); flush() hands back everything (shutting down). Thread-safe."""

    def __init__(self, plan: TonePlan):
        self.plan = plan
        self.held: dict[str, tuple[Page, float]] = {}    # channel -> (page, squelch's window end)
        self._lock = threading.Lock()

    def add(self, label: str, mhz: float, seg: Segment) -> tuple[list[Page], list[tuple[Page, bool]]]:
        """-> (pages finished now, in order; [(page, joined)] for each piece of this transmission:
        joined = appended to the held page, else a new page, held or in the finished list)."""
        tones = _tones(seg, self.plan)
        pieces = split_at_new_tones(seg, tones)
        done: list[Page] = []
        touched: list[tuple[Page, bool]] = []
        for piece in pieces:
            page = assemble(label, mhz, piece, self.plan, tones if len(pieces) == 1 else None)
            with self._lock:
                prev, _until = self.held.pop(label, (None, 0.0))
                joined = (prev is not None and piece.cont and not page.tones
                          and piece.end - prev.start <= MAX_PAGE_S)
                if joined:
                    page = join(prev, piece)
                elif prev is not None:
                    done.append(prev)
                if piece.hold_until > 0:
                    self.held[label] = (page, piece.hold_until)
                else:
                    done.append(page)
            touched.append((page, joined))
        return done, touched

    def due(self, now: float, busy: Callable[[str], bool] = lambda label: False) -> list[Page]:
        """Pages whose window has passed. busy(label): that channel's squelch is open on a continuation
        right now; its page waits for it (up to the longest a page may be)."""
        out = []
        with self._lock:
            for label, (page, until) in list(self.held.items()):
                if now < until + HOLD_SLACK_S or (busy(label) and now < until + MAX_PAGE_S):
                    continue
                del self.held[label]
                out.append(page)
        return out

    def flush(self) -> list[Page]:
        with self._lock:
            out = [p for p, _u in self.held.values()]
            self.held.clear()
        return out
