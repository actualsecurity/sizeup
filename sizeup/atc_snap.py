"""Callsigns snapped to the aircraft on ADS-B (GitHub #257): a callsign speech-to-text nearly got, written as the aircraft
in the air at the time is called.

Amazon Transcribe streams HPN Tower with a fixed vocabulary (sizeup-atc); it can't be told, per transmission, which
aircraft are on the frequency. So the transcript is corrected after: the aircraft within AREA_NM of HPN within
WINDOW_S of the transmission (the Aircraft layer's pictures that sizeup/atc_talkers.py keeps) are said the way ATC
says them, and a stretch of the transcript that clearly sounds like one of them is replaced by its callsign:

    airline     the telephony ("Moxy", FAA JO 7340.2 and data/atc) then the flight number: "Moxie 283" -> "MXY283",
                "Sun Country 666" -> "SCX8766" (one digit dropped). The telephony is compared by spelling and by a
                rough sound key (SequenceMatcher, >= TEL_MIN), the number exactly or one digit off (3+ digits).
    number      the flight number alone, exactly, when the name was lost; two digits only for a departing aircraft
                in a hand-off to departure ("Departure sky 51") ("Partnership 413", "Tower of jet 665"):
                NUMBER_ONLY, so only with no other aircraft near carrying that number, never after "runway", "heading",
                "maintain" ... or before "feet", "knots" (always probable at most: atc_talkers SNAP_EXACT).
    tail        an N-number's characters (digits, then phonetic letters): the whole tail, its last three or more
                (a shortened tail, "34 Charlie" for N534C), or one character off with four or more. Never one that
                starts on or right after a runway, heading, altitude ... number ("runway 34, 7 kilo zulu").

A stretch is snapped only when its best aircraft scores SNAP_MIN or more and every other aircraft scores at least
MARGIN under it; nothing is snapped with no aircraft near, or two close. Each snap is kept ({from, to, hex, score})
so the row shows what was corrected; the transcript itself is never changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from sizeup import atc

SNAP_MIN = 0.8
MARGIN = 0.15
TEL_MIN = 0.6
AREA_NM = 15.0

NUMBER_ONLY = 0.8         # a flight number heard exactly, its name not: snapped only when no other aircraft has it
# a number after these is a runway, heading, altitude, speed, squawk or frequency, never a flight; nor before these
NOT_A_FLIGHT_AFTER = {"runway", "rwy", "heading", "maintain", "climb", "descend", "altitude", "wind", "squawk", "point",
                      "level", "fl", "speed", "contact", "frequency", "altimeter", "qnh", "taxiway", "gate", "spot",
                      "information", "at", "to", "and", "of"}
NOT_A_FLIGHT_BEFORE = {"feet", "foot", "knots", "knot", "degrees", "thousand", "hundred", "miles", "mile", "point", "decimal"}
PLAIN = {"tower", "the", "to", "of", "for", "and", "you", "that", "is", "a", "on", "with", "at", "over", "contact", "good",
         "day", "roger", "thanks", "thank", "right", "left", "cleared", "land", "clear", "runway", "ground", "approach",
         "departure", "traffic", "number", "follow", "behind", "wind", "hold", "short", "taxi", "via", "yes", "no",
         "afternoon", "morning", "evening", "hello", "bye", "copy", "affirmative", "negative", "say", "again", "please"}
_DIG = {**atc.DIGITS, "oh": "0"}
_LET = {k: v for k, v in atc.LETTERS.items()}
_TOKEN = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)?|\d+")


@dataclass(frozen=True)
class Cand:
    callsign: str        # "MXY283", "N534C"
    hex: str
    tels: tuple          # how its operator is said: ("moxy",); ("execjet", "netjets"); () for a tail
    number: str          # "283", "534C"
    phase: str = ""      # the Aircraft layer's: arriving | departing | ground | low


def sound(w: str) -> str:
    """A rough sound key: what an English speller might write for the same sounds ("moxie", "moxy" -> "moksi")."""
    w = re.sub(r"[^a-z]", "", w.lower())
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"), ("c", "k"), ("z", "s"), ("y", "i"), ("ie", "i"),
                 ("ee", "i"), ("ea", "i"), ("oo", "u"), ("gh", "")):
        w = w.replace(a, b)
    w = re.sub(r"(.)\1+", r"\1", w)
    return w[:1] + re.sub(r"[aeiou]", "", w[1:-1]) + w[-1:] if len(w) > 2 else w


def tel_similar(heard: str, tel: str) -> float:
    a, b = heard.lower().replace(" ", ""), tel.lower().replace(" ", "")
    return max(SequenceMatcher(None, a, b).ratio(), SequenceMatcher(None, sound(a), sound(b)).ratio())


_TEL: dict[str, tuple] = {}


def telephony() -> dict[str, tuple]:
    """Designator -> how it is said: HPN's operators and FAA JO 7340.2 (sizeup/atc.py telephony_words), plus the extra
    spoken forms (data/atc/telephony_extra.csv: "NetJets" for EJA, "Republic" for RPA)."""
    if not _TEL:
        import csv
        out = {k: [v] for k, v in atc.telephony_words().items()}
        try:
            with open(atc.ROOT / "data" / "atc" / "telephony_extra.csv", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("designator") and r.get("telephony"):
                        out.setdefault(r["designator"], []).append(" ".join(atc.words_of(r["telephony"])))
        except OSError:
            pass
        _TEL.update({k: tuple(dict.fromkeys(v)) for k, v in out.items()})
    return _TEL


def candidates(planes: Iterable[dict], tel: dict[str, tuple]) -> list[Cand]:
    out: dict[str, Cand] = {}
    for a in planes:
        if a.get("dist") is not None and a["dist"] > AREA_NM:
            continue
        fl = (a.get("flight") or "").strip().upper()
        m = re.fullmatch(r"([A-Z]{3})(\d{1,4}[A-Z]{0,2})", fl)
        if m and tel.get(m.group(1)):
            names = tel[m.group(1)]
            out[fl] = Cand(fl, a.get("hex", ""), tuple(n.lower() for n in ([names] if isinstance(names, str) else names)),
                           m.group(2), str(a.get("phase") or ""))
            continue
        tail = atc._tail(a)
        if tail:
            out[tail] = Cand(tail, a.get("hex", ""), (), tail[1:], str(a.get("phase") or ""))
    return list(out.values())


def _char(w: str) -> str:
    lw = w.lower()
    if w.isdigit():
        return w
    if lw in _DIG:
        return _DIG[lw]
    if lw in _LET and lw != "november":
        return _LET[lw]
    if re.fullmatch(r"x-ray", lw):
        return "X"
    return ""


def _number_score(heard: str, number: str) -> float:
    if heard == number:
        return 1.0
    if len(number) >= 3 and len(heard) >= 3 and atc._edit_distance(heard, number) == 1:
        return 0.75
    return 0.0


def _tail_score(heard: str, key: str, november: bool = False) -> float:
    if not november and not any(c.isalpha() for c in heard):
        return 0.0                                    # digits alone: a runway, a heading, a wind; "November 270" is one
    if heard == key:
        return 1.0
    if len(heard) >= 3 and key.endswith(heard) and any(c.isdigit() for c in heard):
        return 0.85                                   # a shortened tail, its last characters
    if len(heard) >= 4 and atc._edit_distance(heard, key) == 1:
        return 0.8
    return 0.0


def _runs(toks: Sequence[re.Match], text: str = "") -> list[tuple[int, int, str]]:
    """(first token, past the last, characters) of every run of callsign characters (digits and phonetic letters),
    never across punctuation ("20.83" is a frequency, not 2083)."""
    out, i = [], 0
    while i < len(toks):
        j, chars = i, ""
        while j < len(toks) and _char(toks[j].group(0)) and len(chars) < 6:
            if j > i and text and text[toks[j - 1].end():toks[j].start()].strip(" -"):
                break
            c = _char(toks[j].group(0))
            if c[0].isdigit() and chars and chars[-1].isalpha():
                break                                  # a tail's letters come last
            chars += c
            j += 1
        if j > i:
            out.append((i, j, chars))
            i = j
        else:
            i += 1
    return out


def snap(text: str, planes: Iterable[dict], tel: dict | None = None) -> tuple[str, list[dict]]:
    """(the text with each clearly heard callsign written as its aircraft's, the snaps [{from, to, hex, score}])."""
    cands = candidates(planes, telephony() if tel is None else tel)
    if not text or not cands:
        return text, []
    toks = list(_TOKEN.finditer(text))
    # a hand-off to departure ("over to departure XY 450", "Departure sky 51"): a departing aircraft's number, even two
    # digits, among the departing ones only
    handoff = bool(re.search(r"\bdeparture\b", text, re.I))
    spans: list[tuple[float, int, int, Cand, float]] = []     # (score, first char, past last char, cand, runner-up)
    numbers, _rest = atc.number_words([t.group(0).lower() for t in toks])
    for i0, j0, chars in _runs(toks, text):
        if not any(c.isdigit() for c in chars):
            continue
        # a run that starts on an ATC number or right after one ("runway 34, 7 kilo zulu", "runway 347 kilo
        # zulu"): its first digits may be the number's, so it is never snapped to a tail (sizeup/atc.py number_words)
        after_number = i0 in numbers or (i0 - 1) in numbers
        scored: list[tuple[float, int, Cand]] = []
        for c in cands:
            if c.tels:
                for n in (1, 2):                       # the telephony: the word(s) just before the number
                    if i0 - n < 0 or any(_char(t.group(0)) for t in toks[i0 - n:i0]):
                        continue
                    if any(text[toks[k].end():toks[k + 1].start()].strip(" -") for k in range(i0 - n, i0)):
                        continue                       # a comma or a full stop in between: not one callsign
                    heard = " ".join(t.group(0) for t in toks[i0 - n:i0])
                    ts = max(tel_similar(heard, t) for t in c.tels)
                    ns = _number_score(chars, c.number)
                    if ts >= TEL_MIN and ns:
                        scored.append((0.45 * ts + 0.55 * ns, i0 - n, c))
                # the airline's name lost ("Tower of jet 665", "Partnership 413"): its flight number alone, the only
                # aircraft near with it, never after a runway / heading / altitude word (#257)
                short = handoff and c.phase in ("departing", "near") and len(c.number) == 2
                if c.number.isdigit() and (len(c.number) >= 3 or short):
                    for k in range(i0, j0):
                        if "".join(_char(t.group(0)) for t in toks[k:j0]) != c.number:
                            continue
                        prev = toks[k - 1].group(0).lower() if k > 0 else ""
                        nxt = toks[j0].group(0).lower() if j0 < len(toks) else ""
                        decimal = (j0 < len(toks) and text[toks[j0 - 1].end():toks[j0].start()] == "." and nxt.isdigit()) \
                            or (k > 0 and text[toks[k - 1].end():toks[k].start()] == "." and prev.isdigit())
                        if prev in NOT_A_FLIGHT_AFTER or nxt in NOT_A_FLIGHT_BEFORE or decimal:
                            break                  # (a decimal: a frequency or an altimeter, "120.4", "29.92")
                        first = k
                        if k > 0 and prev.isalpha() and prev not in PLAIN and not _char(prev) and \
                                not text[toks[k - 1].end():toks[k].start()].strip(" -"):
                            first = k - 1          # the word just before: what was left of the name
                        scored.append((NUMBER_ONLY, first, c))
                        break
            elif not after_number:
                start = i0 - 1 if i0 > 0 and toks[i0 - 1].group(0).lower() == "november" else i0
                ts = _tail_score(chars, c.number, november=start < i0)
                if ts:
                    scored.append((ts, start, c))
        if not scored:
            continue
        scored.sort(key=lambda s: -s[0])
        best = scored[0]
        other = max((s[0] for s in scored[1:] if s[2].callsign != best[2].callsign), default=0.0)
        if best[0] >= SNAP_MIN and best[0] - other >= MARGIN:
            spans.append((best[0], toks[best[1]].start(), toks[j0 - 1].end(), best[2], other))
    out, snaps, last = [], [], 0
    for score, a, b, c, _o in sorted(spans, key=lambda s: s[1]):
        if a < last:
            continue
        heard = text[a:b]
        out.append(text[last:a])
        out.append(c.callsign)
        last = b
        if heard != c.callsign:
            snaps.append({"from": heard, "to": c.callsign, "hex": c.hex, "score": round(score, 2)})
    out.append(text[last:])
    return "".join(out), snaps
