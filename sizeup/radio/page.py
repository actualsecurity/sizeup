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

One page, one tone group (GitHub #183). A 60 Control page is TONES (one pair, or several stacked back to back
with nothing said between them: one page to all of those departments), then the MESSAGE (read twice), then the
end; tones after the message has started are the next page. The carrier doesn't always drop between two pages
(South 2 2026-10-15 13:57: Port Chester's page and West Harrison's came as one transmission, shown as one page
with both departments' tones, its voice taken after the last tone, so only West Harrison's message was read, and
queued as ours on Port Chester's tones). So every transmission is cut by what is in it (split_pages), over the
tone decoder's runs and the voice band:

    TONES    a pair; the next pair joins the group when less than SPLIT_VOICE_S was said between them and
             they are under STACK_GAP_S apart
    MESSAGE  SPLIT_VOICE_S of speech (speech_mask: the voice band over the frame's own 4-7.5 kHz hiss band and
             over the transmission's quietest frames, tone frames left out) after the group's last pair
    -> a pair after the message (or after STACK_GAP_S of nothing) closes the page and starts the next one,
       cut just before the new tones (at the carrier break when there is one: split_at_new_tones before #183).
       Each piece is a page of its own: its own audio, tones, voice, transcript and suggestion. A piece with
       under NO_MESSAGE_S said after its tones is "tones only, no message" (status no_voice), not merged into
       the next. Speech before the first tones (a continuation's end of the last page) is a piece of its own too.

A page made before #183 (or by a station on an older build) can still hold two groups: Page.message_tones is the
group its voice answers (the last one; groups apart by STACK_GAP_S, from the tones' times or tone_seq), and only
those make it ours (App._transcribed), never a neighbouring page's tones.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Callable

import numpy as np

from sizeup.radio.fm import _F, _VOICE, FRAME, HF_BAND, MAX_FRAMES, RATE, STEADY_DB, Segment
from sizeup.radio.tones import ToneDetector, ToneEvent, TonePlan

VOICE_GUARD_S = 0.15   # skip this much after the last tone (tone tail, squelch-tail clicks)
MIN_VOICE_S = 0.8      # shorter than this after the tones: tones only, nothing to transcribe
MAX_PAGE_S = MAX_FRAMES * FRAME / RATE   # a joined page is never longer than one transmission may be (2 min)
HOLD_SLACK_S = 0.5     # finalise this long after the squelch's window ends (frames still in the pipe)
TONES_AFTER_BREAK_S = 1.5   # tones this soon after the carrier came back inside the hang: a new page
# One page, one tone group (#183, split_pages). Conservative both ways: a message read twice is 8 s or more of speech,
# and stacked pairs follow each other within a second or so (Quick Call II: A 1 s, B 3 s, the next A straight after).
SPLIT_VOICE_S = 1.0    # this much speech between two pairs: the second is the next page's tones
STACK_GAP_S = 5.0      # two pairs this far apart (nothing said between them): not one stack either
CUT_PAD_S = 0.25       # the next page's audio starts this long before its first tone
NO_MESSAGE_S = 0.5     # a page cut off by the next one's tones with less speech than this after its tones: tones only
TONE_MASK_S = 0.1      # frames this close to a tone run are the tone (its edges), not speech
SPEECH_HF_DB = STEADY_DB   # speech: the voice band this far over the frame's 4-7.5 kHz band (speech_mask) ...
SPEECH_FLOOR_DB = 3.0      # ... and this far over the transmission's quietest frames
_HF = (_F >= HF_BAND[0]) & (_F <= HF_BAND[1])
REASONS = {"quiet": "quiet after hang", "max length": "max length", "stall": "stall (rtl_fm sent no audio)",
           "stopped": "receiver stopped", "rtl_fm ended": "rtl_fm ended", "tones": "new page's tones",
           "drop": "dropped to a weaker carrier", "background": "background carrier",
           "steady": "nothing on the carrier for 30 s (noise held the squelch)"}
TONES_ONLY = "tones only, no message"


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
    # Every tone heard, in order (#115): {hz, start (s into audio), dur, role: A | B | AB | long | "" (in no pair)}
    tone_seq: list = field(default_factory=list)

    @property
    def departments(self) -> list[str]:
        out: list[str] = []
        for t in self.tones:
            for n in t.names:
                if n not in out:
                    out.append(n)
        return out

    @property
    def message_tones(self) -> list[ToneEvent]:
        """The tone group this page's message answers (#183): its only one, for a page made since; for an older
        one that holds two pages' tones (one transmission, cut by nothing), the last group, the one before the
        voice that was transcribed."""
        groups = tone_groups(self.tones, self.tone_seq)
        return groups[-1] if groups else []

    @property
    def message_departments(self) -> list[str]:
        """The departments of message_tones: who this page is for (what makes it ours, App._transcribed)."""
        return list(dict.fromkeys(n for t in self.message_tones for n in t.names))

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
                           "b_hz": round(t.b_hz, 1) if t.b_hz else None, "names": list(t.names),
                           "start": round(t.start, 2), "end": round(t.end, 2)}      # s into the audio (#183)
                          for t in self.tones],
                "raw": self.raw, "confidence": self.confidence, "voice_offset_s": round(self.voice_offset_s, 3),
                "tone_seq": list(self.tone_seq)}


def page_from_dict(d: dict) -> Page:
    """A saved page (Page.as_dict + dispatch), reloaded after a restart; audio stays on disk."""
    tones = [ToneEvent(t["kind"], t["a_hz"], t.get("b_hz"), float(t.get("start") or 0.0), float(t.get("end") or 0.0),
                       tuple(t.get("names", ()))) for t in d.get("tones", [])]
    p = Page(d["id"], d["label"], d["mhz"], d["start"], d["end"], d["kind"], tones, d.get("voice_offset_s", 0.0), None,
             d.get("quieting_db", 0.0), d.get("status", "done"), d.get("raw", ""), d.get("confidence"))
    p.tone_seq = list(d.get("tone_seq") or [])
    p.dispatch = d.get("dispatch")  # type: ignore[attr-defined]
    p.history = d.get("history") or []  # type: ignore[attr-defined]
    p.rerun_at = d.get("rerun_at")  # type: ignore[attr-defined]
    p.model = d.get("model", "")  # type: ignore[attr-defined]
    p.stt = d.get("stt", "")  # type: ignore[attr-defined]   (aws | both: Amazon Transcribe on the hub)
    p.read_twice = d.get("read_twice")  # type: ignore[attr-defined]
    if d.get("alt_model"):
        p.alt_text, p.alt_model = d.get("alt_text", ""), d["alt_model"]  # type: ignore[attr-defined]
    return p


def decode_tones(audio: np.ndarray, plan: TonePlan) -> tuple[list[ToneEvent], list[dict]]:
    """The pairs in a clip, matched against the plan, and every tone heard in order (ToneDetector.sequence)."""
    det = ToneDetector(RATE)
    return plan.match_all(det.feed(audio) + det.flush()), det.sequence()


def _tones(seg: Segment, plan: TonePlan) -> tuple[list[ToneEvent], list[dict]]:
    return decode_tones(seg.audio, plan)


def _part(seg: Segment, gap: float | None = None) -> dict:
    return {"start": seg.start, "end": seg.end, "reason": seg.reason, "q_open": seg.q_open,
            "q_close": seg.q_close, "hiss_db": seg.hiss_db, "floor_db": seg.floor_db, "gap": gap}


def assemble(label: str, mhz: float, seg: Segment, plan: TonePlan,
             tones: list[ToneEvent] | None = None, seq: list[dict] | None = None) -> Page:
    """Classify one squelched transmission into a page (with tones) or plain voice."""
    if tones is None:
        tones, seq = _tones(seg, plan)
    page = Page(uuid.uuid4().hex[:10], label, mhz, seg.start, seg.end, "page" if tones else "voice",
                tones, 0.0, seg.audio, seg.quieting_db, parts=[_part(seg)], tone_seq=list(seq or []))
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


def _spans_from_seq(seq: list[dict], n: int) -> list[tuple[float, float]] | None:
    """Where each pair was, from the tones heard (tone_seq, #115) of a page saved without its tones' times: A + B,
    an A=B code or a long tone, in order. None when they don't line up with the n pairs."""
    spans, i = [], 0
    while i < len(seq):
        r = seq[i]
        nxt = seq[i + 1] if i + 1 < len(seq) else None
        if r.get("role") == "A" and nxt is not None and nxt.get("role") == "B":
            spans.append((float(r["start"]), float(nxt["start"]) + float(nxt["dur"])))
            i += 2
            continue
        if r.get("role") in ("AB", "long"):
            spans.append((float(r["start"]), float(r["start"]) + float(r["dur"])))
        i += 1
    return spans if len(spans) == n else None


def tone_groups(tones: list[ToneEvent], seq: list[dict] | None = None) -> list[list[ToneEvent]]:
    """A page's pairs in their groups (#183): pairs under STACK_GAP_S apart are one stack. From the pairs' own times
    (a page made since #183 carries them), else from tone_seq; with neither, all of them are one group (as before)."""
    if not tones:
        return []
    spans = [(t.start, t.end) for t in tones]
    if not any(e > 0 for _s, e in spans):
        spans = _spans_from_seq(list(seq or []), len(tones))
        if spans is None:
            return [list(tones)]
    groups: list[list[ToneEvent]] = []
    last = 0.0                          # where the group so far ends
    for i in sorted(range(len(tones)), key=lambda k: spans[k][0]):
        s, e = spans[i]
        if groups and s - last < STACK_GAP_S:
            groups[-1].append(tones[i])
            last = max(last, e)
        else:
            groups.append([tones[i]])
            last = e
    return groups


def speech_mask(audio: np.ndarray, tone_spans: list[tuple[float, float]] = ()) -> np.ndarray:
    """Which 50 ms frames of a transmission are speech (#183), the frames on a tone (tone_spans, s) left out. The
    test that tells speech from the carrier is the spectrum's shape, as fm.speech_frames: the voice band SPEECH_HF_DB
    over the frame's own 4-7.5 kHz band (the discriminator's noise, dead carrier or hiss, rises toward 4-7.5 kHz or
    is flat). Over the dead air only SPEECH_FLOOR_DB, not fm.SPEECH_DB: a page is mostly talk with short pauses (the
    5th percentile is the quiet part of the speech then, not dead air), and a pause is never 1 s of speech anyway.
    The muted carrier breaks (zeros) are neither, and don't count toward the floor."""
    x = np.asarray(audio, dtype=np.float64)
    n = len(x) // FRAME
    mask = np.zeros(n, bool)
    if n == 0:
        return mask
    frames = x[:n * FRAME].reshape(n, FRAME)
    live = np.flatnonzero(np.abs(frames).max(axis=1) > 0)
    if len(live) >= 3:
        S = np.abs(np.fft.rfft(frames[live] * np.hanning(FRAME), axis=1)) ** 2
        v, h = S[:, _VOICE].mean(axis=1) + 1e-20, S[:, _HF].mean(axis=1) + 1e-20
        db = 10 * np.log10(v)
        mask[live] = (v >= h * 10 ** (SPEECH_HF_DB / 10)) & (db >= np.percentile(db, 5) + SPEECH_FLOOR_DB)
    for s, e in tone_spans:
        a = max(0, int((s - TONE_MASK_S) * RATE // FRAME))
        b = min(n, int(np.ceil((e + TONE_MASK_S) * RATE / FRAME)))
        mask[a:b] = False
    return mask


@dataclass
class Piece:
    """One page's part of a transmission (split_pages)."""
    seg: Segment
    tones: list[ToneEvent]
    seq: list[dict]
    why: str = ""              # why it ends where it does: the next page's tones ("" = where the transmission did)
    tones_only: bool = False   # cut off by the next page with nothing said after its tones


def split_pages(seg: Segment, tones: list[ToneEvent], seq: list[dict]) -> list[Piece]:
    """A transmission cut into its pages (#183, the rule in the module's docstring): a tone pair after speech (or
    STACK_GAP_S after the last pair) starts the next page, cut just before its tones, or where the carrier broke
    between the speech and them (the long hang held the squelch through the next key-up, #61). Each piece carries
    its own pairs and tones heard, with their times into its own audio."""
    if not tones:
        return [Piece(seg, list(tones), list(seq))]
    ev = sorted(tones, key=lambda t: t.start)
    runs = [(float(r["start"]), float(r["start"]) + float(r["dur"])) for r in seq] + [(t.start, t.end) for t in ev]
    talk = speech_mask(seg.audio, runs)
    step = FRAME / RATE

    def said(lo: float, hi: float) -> tuple[float, float]:
        """Speech between lo and hi (s): how much, and where the last of it ends (lo if none)."""
        i0, i1 = max(0, int(np.ceil(lo / step - 1e-6))), max(0, min(len(talk), int(hi / step + 1e-6)))
        idx = np.flatnonzero(talk[i0:i1]) + i0 if i1 > i0 else np.zeros(0, int)
        return len(idx) * step, ((idx[-1] + 1) * step if len(idx) else lo)

    def boundary(lo: float, hi: float, why: str) -> tuple[float, float, str]:
        """(end of the page before, start of the next, why): after the last speech, before the next tones (a broken
        pair's lone tone ahead of them included), at a carrier break between the two when there is one."""
        _s, last = said(lo, hi)
        nxt = min([a for a, _b in runs if last <= a <= hi] + [hi])
        for g0, g1 in reversed(seg.breaks):
            if g0 >= last - step and g1 <= nxt + 0.1:
                return g0, g1, why
        c = max(lo, last, nxt - CUT_PAD_S)
        return c, c, why

    cuts: list[tuple[float, float, str]] = []
    groups: list[list[ToneEvent]] = [[ev[0]]]
    lead, _ = said(0.0, ev[0].start)
    if lead >= SPLIT_VOICE_S:           # the end of the last page's message (a continuation) before these tones
        cuts.append(boundary(0.0, ev[0].start, f"{lead:.1f} s said before the tones"))
        groups.insert(0, [])
    end = ev[0].end
    for e in ev[1:]:
        talk_s, _ = said(end, e.start)
        why = ""
        if talk_s >= SPLIT_VOICE_S:
            why = f"{talk_s:.1f} s said, then new tones"
        elif e.start - end >= STACK_GAP_S:
            why = f"new tones {e.start - end:.1f} s after the last"
        elif any(end <= g0 + 0.05 and g1 - 0.1 <= e.start <= g1 + TONES_AFTER_BREAK_S and g0 - end >= MIN_VOICE_S
                 for g0, g1 in seg.breaks):
            why = "the carrier came back with new tones"
        if why:
            cuts.append(boundary(end, e.start, why))
            groups.append([e])
        else:
            groups[-1].append(e)
        end = max(end, e.end)
    if not cuts:
        return [Piece(seg, list(tones), list(seq))]
    dur = len(seg.audio) / RATE
    out: list[Piece] = []
    t0 = 0.0
    for k, (group, (c0, c1, why)) in enumerate(zip(groups, cuts + [(dur, dur, "")])):
        a = seg.audio[int(round(t0 * RATE)):int(round(c0 * RATE))]
        piece = replace(seg, start=seg.start + t0, end=seg.start + t0 + len(a) / RATE, audio=a,
                        breaks=[(b0 - t0, b1 - t0) for b0, b1 in seg.breaks if t0 < b1 <= c0],
                        bursts=seg.bursts if k == 0 else [])
        if why:              # not the last piece: it ended on the next page's tones, nothing waits for it
            piece.reason, piece.hold_until = "tones", 0.0
        if k:
            piece.cont = False
        ptones = [replace(t, start=t.start - t0, end=t.end - t0) for t in group]
        pseq = [dict(r, start=round(float(r["start"]) - t0, 2)) for r in seq if t0 - step <= float(r["start"]) < c0]
        quiet = bool(why and ptones and said(max(t.end for t in ptones) + t0, c0)[0] < NO_MESSAGE_S)
        out.append(Piece(piece, ptones, pseq, why, quiet))
        t0 = c1
    return out


def read_twice(text: str) -> bool | None:
    """The page's message read twice (#183: 60 Control reads it, then reads it again): two halves of the transcript
    that say much the same. A confirming signal only (a transcript can be cut short, or one read misheard): None
    when it is too short to tell."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(words) < 8:
        return None
    n = len(words)
    best = 0.0
    for cut in range(int(n * 0.35), int(n * 0.65) + 1):
        best = max(best, SequenceMatcher(None, words[:cut], words[cut:], autojunk=False).ratio())
    return best >= 0.6


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
            f"{REASONS.get(last.get('reason') or '', last.get('reason') or 'ended')}"
            f"{' (' + last['split'] + ')' if last.get('split') else ''}"
            f"{' · ' + TONES_ONLY if page.kind == 'page' and page.status == 'no_voice' else ''} · quieting {db(first.get('q_open'))} "
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
        tones, seq = _tones(seg, self.plan)
        pieces = split_pages(seg, tones, seq)
        done: list[Page] = []
        touched: list[tuple[Page, bool]] = []
        for pc in pieces:
            piece = pc.seg
            page = assemble(label, mhz, piece, self.plan, pc.tones, pc.seq)
            if pc.why:
                page.parts[-1]["split"] = pc.why
            if pc.tones_only:
                page.status = "no_voice"        # the next page's tones came before anything was said (#183)
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
