"""Paging channel transmissions: alert tones + the voice page that follows.

A page on South 2 is one keyed transmission: tone A/B pairs (one per department paged,
stacked) and then the dispatcher's voice. Every transmission from the squelch goes through
the tone decoder:

    tones found   -> Page(kind="page"): departments from the tone plan, voice = audio after
                     the last tone (what speech-to-text gets)
    no tones      -> Page(kind="voice"): plain dispatcher traffic on the paging channel
                     (repeats, "time out", updates)

Every decoded tone pair is kept with its page, matched or not, so the real codes for PCFD /
Rye Brook can be learned from the air and named in the config.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np

from sizeup.radio.fm import RATE, Segment
from sizeup.radio.tones import ToneDetector, ToneEvent, TonePlan

VOICE_GUARD_S = 0.15   # skip this much after the last tone (tone tail, squelch-tail clicks)
MIN_VOICE_S = 0.8      # shorter than this after the tones: tones only, nothing to transcribe


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
    return p


def assemble(label: str, mhz: float, seg: Segment, plan: TonePlan) -> Page:
    """Classify one squelched transmission into a page (with tones) or plain voice."""
    det = ToneDetector(RATE)
    tones = plan.match_all(det.feed(seg.audio) + det.flush())
    page = Page(uuid.uuid4().hex[:10], label, mhz, seg.start, seg.end, "page" if tones else "voice",
                tones, 0.0, seg.audio, seg.quieting_db)
    if tones:
        page.voice_offset_s = min(len(seg.audio) / RATE, max(t.end for t in tones) + VOICE_GUARD_S)
        if len(seg.audio) / RATE - page.voice_offset_s < MIN_VOICE_S:
            page.status = "no_voice"  # tones only (or the voice came in a separate transmission)
    return page
