"""Two-tone sequential paging (Motorola Quick Call II) decoder.

A fire page starts with alert tones that select which departments' pagers open:

    tone A (~1 s) -> tone B (~3 s) [-> next department's A/B ...] -> voice dispatch

Several departments can be "stacked" back to back; a single long tone (~5-8 s) is a
group / all-call. We find the tones from the audio itself:

  every 50 ms, look at the last 100 ms (Hann, zero-padded FFT, ~2 Hz bins, parabolic peak)
  -> a frame is "tonal" when most of the 250-3500 Hz energy sits around one frequency
     (voice and static spread it out, so they don't count)
  -> consecutive tonal frames at the same frequency (within 1%) form a run
  -> runs pair up by their length, not their order (#115): a short run (A, ~1 s) and the long run (B, ~3 s) that
     follows it straight away = two-tone page; a single run of 5 s or more = long tone. A short run followed by
     another short one, or a long one with no short one before it, is a broken pair: kept in the sequence
     (sequence(): every run with its Hz, start, length and role), never forced into a pair.

A stacked page (two departments) is A1 B1 A2 B2. Pairing by order alone split South 2's 2026-10-14 07:04 page
across the departments: a fade in the middle of B1 cut it into two runs, the second half was taken as the next
pair's A (928.1/903.2) and B2 alone as an A=B code. Now a run bridges up to FADE_S of fade at the same frequency,
B must be long (over A_MAX_S), and A=B codes are one run of SAME_AB_S or more (A 1 s + B 3 s merged, ~4 s;
a lone 3 s B is not one).

TonePlan matches decoded pairs against configured departments (+/- 1.5 %, about the spacing
between neighbouring Motorola tone codes / 2). The codes come from data/westchester_tones.csv (and
[[tones]] in the config), plus the pairs an admin named from the console (Settings, section "tones":
"Name these tones" on an unknown-tones page row, #80), merged in by App._apply_named_tones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

WIN_S = 0.10          # analysis window
HOP_S = 0.05          # step between windows
BAND = (250.0, 3500.0)
PURITY = 0.6          # share of in-band energy within ~±25 Hz of the peak
SAME_TONE = 0.01      # relative frequency change still counted as the same tone
MIN_RUN_S = 0.3       # shorter tonal runs are ignored (voice harmonics, blips)
A_MAX_S = 1.5         # an A tone is at most this long (Quick Call II: 1 s); longer is a B
B_MIN_S = A_MAX_S     # a B tone is longer than this (Quick Call II: 3 s)
PAIR_GAP_S = 0.6      # B must start within this of A ending
LONG_TONE_S = 5.0     # a single run this long is a long tone (group / all-call)
SAME_AB_S = 3.5       # a single run 3.5-5 s is an A=B code (Larchmont 435.1/435.1: A 1 s + B 3 s merge into ~4 s)
FADE_S = 0.3          # a run survives this much non-tonal audio (a fade on a distant repeater, the A->B switch)
DROPOUT_FRAMES = int(round(FADE_S / HOP_S))


@dataclass
class Run:
    hz: float
    start: float
    end: float
    role: str = ""            # "A" | "B" | "AB" (an A=B code) | "long" | "" (in no pair: a broken one)

    @property
    def dur(self) -> float:
        return self.end - self.start


@dataclass
class ToneEvent:
    kind: str                 # "two_tone" | "long_tone"
    a_hz: float
    b_hz: float | None
    start: float
    end: float
    names: tuple[str, ...] = ()   # filled by TonePlan.match

    def describe(self) -> str:
        tones = f"{self.a_hz:.1f}" + (f"/{self.b_hz:.1f}" if self.b_hz else "") + " Hz"
        who = ", ".join(self.names) if self.names else "unknown"
        return f"{self.kind} {tones} ({who})"


class ToneDetector:
    """Streaming decoder: feed() audio (float, any scale) and get ToneEvents back."""

    def __init__(self, rate: int = 16000, t0: float = 0.0):
        self.rate = rate
        self.win = int(round(WIN_S * rate))
        self.hop = int(round(HOP_S * rate))
        self.nfft = 1 << int(np.ceil(np.log2(rate / 2.0)))  # ~2 Hz bins
        self.window = np.hanning(self.win)
        f = np.fft.rfftfreq(self.nfft, 1 / rate)
        self.band = (f >= BAND[0]) & (f <= BAND[1])
        self.lobe = max(2, int(round(25.0 / (rate / self.nfft))))  # ±25 Hz around the peak
        self.t0 = t0
        self._buf = np.zeros(0)
        self._frame = 0              # index of the next frame to analyse
        self._run: list[float] | None = None
        self._run_start = 0.0
        self._run_end = 0.0
        self._miss = 0
        self._pending_a: Run | None = None
        self.runs: list[Run] = []    # every tone heard, in order (sequence())

    # -- frame analysis ----------------------------------------------------------------------

    def tone_of(self, x: np.ndarray) -> float | None:
        """The frame's tone frequency, or None if it isn't a clean tone."""
        spec = np.abs(np.fft.rfft(x * self.window, self.nfft)) ** 2
        band = np.where(self.band, spec, 0.0)
        total = band.sum()
        if total <= 1e-12:
            return None
        k = int(band.argmax())
        lo, hi = max(0, k - self.lobe), k + self.lobe + 1
        if band[lo:hi].sum() / total < PURITY:
            return None
        # Parabolic interpolation on the log spectrum: a fraction of a bin.
        if 0 < k < len(spec) - 1:
            a, b, c = np.log(spec[k - 1:k + 2] + 1e-30)
            d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
        else:
            d = 0.0
        return float((k + d) * self.rate / self.nfft)

    # -- streaming ----------------------------------------------------------------------------

    def feed(self, samples: np.ndarray) -> list[ToneEvent]:
        self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=np.float64)])
        events: list[ToneEvent] = []
        while len(self._buf) >= self.win:
            t_start = self.t0 + self._frame * HOP_S
            events += self._step(self.tone_of(self._buf[:self.win]), t_start, t_start + WIN_S)
            self._buf = self._buf[self.hop:]
            self._frame += 1
        return events

    def flush(self) -> list[ToneEvent]:
        """End of audio: close any open run and report what it completes."""
        events = self._close_run()
        events += self._expire_a(float("inf"))
        return events

    def pending(self) -> ToneEvent | None:
        """The page the tone ringing now already makes, before it ends (the live display,
        radio/livepage.py): an A then more than 1.5 s of B (longer than any A) is that pair whatever B's length (only B's frequency
        can still move a little); a single run past 3.5 s is an A=B code, a long tone from 5 s."""
        if self._run is None:
            return None
        hz, start, end = float(np.median(self._run)), self._run_start, self._run_end
        a = self._pending_a
        if a is not None and end - start > B_MIN_S and start - a.end <= PAIR_GAP_S:
            return ToneEvent("two_tone", a.hz, hz, a.start, end)
        if a is None and end - start >= SAME_AB_S:
            return (ToneEvent("long_tone", hz, None, start, end) if end - start >= LONG_TONE_S
                    else ToneEvent("two_tone", hz, hz, start, end))
        return None

    @property
    def ringing(self) -> bool:
        """A tone is on the air now (a run at least MIN_RUN_S long)."""
        return self._run is not None and self._run_end - self._run_start >= MIN_RUN_S

    def _step(self, hz: float | None, t_start: float, t_end: float) -> list[ToneEvent]:
        events: list[ToneEvent] = []
        if hz is not None and self._run is not None and abs(hz - np.mean(self._run)) <= SAME_TONE * hz:
            self._run.append(hz)
            self._run_end = t_end
            self._miss = 0
            return events
        if hz is None and self._run is not None and self._miss < DROPOUT_FRAMES:
            self._miss += 1
            return events
        events += self._close_run()
        events += self._expire_a(t_start)
        if hz is not None:
            self._run, self._run_start, self._run_end, self._miss = [hz], t_start, t_end, 0
        return events

    def _close_run(self) -> list[ToneEvent]:
        if self._run is None:
            return []
        run = Run(float(np.median(self._run)), self._run_start, self._run_end)
        self._run = None
        if run.dur < MIN_RUN_S:
            return []
        self.runs.append(run)
        a = self._pending_a
        if a is not None and run.dur > B_MIN_S and run.start - a.end <= PAIR_GAP_S:
            self._pending_a = None
            a.role, run.role = "A", "B"
            return [ToneEvent("two_tone", a.hz, run.hz, a.start, run.end)]
        events = self._expire_a(run.start)
        self._pending_a = None              # a short run after a short one: the first is a broken pair
        if run.dur >= LONG_TONE_S:
            run.role = "long"
            return events + [ToneEvent("long_tone", run.hz, None, run.start, run.end)]
        if run.dur >= SAME_AB_S:
            run.role = "AB"
            return events + [ToneEvent("two_tone", run.hz, run.hz, run.start, run.end)]
        if run.dur <= A_MAX_S:
            self._pending_a = run
        return events                       # a B with no A before it: a broken pair, in the sequence only

    def sequence(self) -> list[dict]:
        """Every tone heard so far, in order: {hz, start, dur, role} (role "" = in no pair)."""
        return [{"hz": round(r.hz, 1), "start": round(r.start, 2), "dur": round(r.dur, 2), "role": r.role} for r in self.runs]

    def _expire_a(self, now: float) -> list[ToneEvent]:
        # An A tone with no B after it isn't a page (or its B was lost): drop it quietly.
        if self._pending_a is not None and now - self._pending_a.end > PAIR_GAP_S:
            self._pending_a = None
        return []


SERVICE_LABELS = ("Fire", "EMS", "Police", "Other")       # the tone book's services (sizeup/cad/tonelist.py, #122)


def base_name(n: str) -> str:
    """'Rye Brook FD (est.)' / 'Fairview (heard)' -> the department's name as the config and the CAD say it; a tone
    book name 'Port Chester · Fire · Chiefs (est.)' -> its entity, 'Port Chester · Fire' (the role dropped)."""
    n = n.removesuffix(" (heard)").removesuffix(" (est.)")
    if " · " in n:
        parts = n.split(" · ")
        for i in range(1, len(parts)):
            if parts[i] in SERVICE_LABELS:
                return " · ".join(parts[:i + 1])
    return n


def pair_key(a_hz: float, b_hz: float | None) -> str:
    """A tone pair as the named-tones settings key it: '832.5/1201.0' (a long tone: '1201.0/-')."""
    return f"{float(a_hz):.1f}/" + (f"{float(b_hz):.1f}" if b_hz else "-")


@dataclass(frozen=True)
class ToneCode:
    name: str                 # "Port Chester FD", "Rye Brook FD", "PCFD all call" ...
    a_hz: float
    b_hz: float | None = None  # None: a long (single) tone
    agency: str = ""          # grouping key, e.g. "PCFD"
    # Where the code comes from (Settings > Radios > Tones, #115): the tone list's source column ("rr-wiki+nf2g",
    # "air"), "config" for [[tones]]; fetched: the date it was last seen there. Not part of the code's identity.
    source: str = field(default="", compare=False)
    fetched: str = field(default="", compare=False)


@dataclass
class TonePlan:
    codes: Sequence[ToneCode] = field(default_factory=list)
    tolerance: float = 0.015

    def _near(self, hz: float | None, want: float | None) -> bool:
        if want is None or hz is None:
            return want is None and hz is None
        return abs(hz - want) <= self.tolerance * want

    def match(self, ev: ToneEvent) -> ToneEvent:
        hits = [c.name for c in self.codes if self._near(ev.a_hz, c.a_hz) and self._near(ev.b_hz, c.b_hz)]
        # One name per department, from the strongest evidence: heard on the air > listed > estimated.
        rank = lambda n: 0 if n.endswith(" (heard)") else 2 if n.endswith(" (est.)") else 1
        best: dict[str, str] = {}
        for n in hits:                  # one per entity: "Port Chester · Fire · Chiefs" and "... · All call" are one
            b = base_name(n)
            if b not in best or rank(n) < rank(best[b]):
                best[b] = n
        ev.names = tuple(best.values())
        return ev

    def match_pair(self, a_hz: float, b_hz: float | None) -> tuple[str, ...]:
        """The names a pair (as logged: A, B or None for a long tone) matches now."""
        return self.match(ToneEvent("two_tone" if b_hz else "long_tone", float(a_hz), b_hz, 0.0, 0.0)).names

    def match_all(self, events: Iterable[ToneEvent]) -> list[ToneEvent]:
        return [self.match(e) for e in events]

    def replace(self, codes: Iterable[ToneCode]) -> None:
        """A new list of codes in place: the live paging display, the page hold and the assembler all
        share this one plan, so a pair named from the console counts for every one of them at once."""
        self.codes = list(codes)

    def names(self) -> list[str]:
        """Every department the plan knows, once each, without the (heard) / (est.) tags."""
        return list(dict.fromkeys(base_name(c.name) for c in self.codes))
