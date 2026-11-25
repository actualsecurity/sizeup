"""A house-number range: "14-16 Sample Ave" is one building (or two joined ones) numbered at both ends.

    written     "14-16 SAMPLE AVE", "14 - 16 SAMPLE AVE", "149-51 SAMPLE AVE" (the second number's last digits: 149 to
                151), "14-16A SAMPLE AVE" (a letter on the second number)
    spoken      a page: "14 to 16 Sample Avenue", "fourteen to sixteen", "fourteen dash sixteen", "14 through 16"
    a range     two numbers, the first lower, at most SPAN_MAX apart and on one side of the street (the same parity);
                anything else is not one: "10-200" (a block, not a building), "16-14", "15-16", a phone number
                ("914-555-0100"), a fraction ("189-1/2"), a highway's exit or route ("exit 9-10", "I-95/287"), a unit
                ("Engine 14 to 16 ...")
    its numbers the numbers on that side between its ends ("14-18": 14, 16, 18): each is the same building as the range

Pure: the page parser (sizeup/dispatch.py), the feeds' and the console's addresses (sizeup/cad/sources.py), the
address keys (sizeup/cad/addrkey.py), the gazetteer (sizeup/geo/gazetteer.py, pagegeo.py) and the dispatch voice
(sizeup/cad/speech.py) all read a range the same way, here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import MULTIPLIERS, NUMBER_WORDS, STREET_TYPES, TENS, TYPE_ABBRS, UNITS, _parse_run

SPAN_MAX = 50           # a building's range: its ends at most this far apart (more: a block, never one building)
SHORT_SPAN_MAX = 20     # "149-51": the second number's last digits only, its end at most this far from the first
SUFFIX_REACH = 5        # a spoken range ("14 to 16"): a street's suffix within this many words after it


@dataclass(frozen=True)
class HouseRange:
    lo: int
    hi: int
    letter: str = ""     # a letter on the second number ("14-16A": "A"), upper case
    written: str = ""    # as given, spaces dropped ("14-16", "149-51", "14-16A")

    @property
    def text(self) -> str:
        """The range in full: "14-16", "149-151", "14-16A"."""
        return f"{self.lo}-{self.hi}{self.letter}"

    def numbers(self) -> list[int]:
        """Its numbers on its side of the street: "14-18" -> [14, 16, 18]."""
        return list(range(self.lo, self.hi + 1, 2))

    def labels(self) -> frozenset[str]:
        """Its house numbers as keys compare them (lower case): {"14", "16"}; "14-16A": {"14", "16", "16a"}."""
        return frozenset([str(n) for n in self.numbers()] + ([f"{self.hi}{self.letter.lower()}"] if self.letter else []))

    def holds(self, number: str) -> bool:
        """A single house number ("16", "16A") is one of its numbers."""
        return str(number or "").strip().lower() in self.labels()


def make(lo_text: str, hi_text: str, letter: str = "") -> HouseRange | None:
    """The range "lo_text-hi_text[letter]", or None when it is not a building's (module doc)."""
    lo_text, hi_text = str(lo_text or "").strip(), str(hi_text or "").strip()
    if not (lo_text.isdigit() and hi_text.isdigit()) or lo_text[0] == "0" or len(lo_text) > 5 or len(hi_text) > 5:
        return None
    lo, hi = int(lo_text), int(hi_text)
    if len(hi_text) < len(lo_text):                       # "149-51": the end's last digits
        step = 10 ** len(hi_text)
        full = lo - lo % step + hi
        if full <= lo:
            full += step                                  # "199-01": into the next hundred
        if full - lo > SHORT_SPAN_MAX:
            return None
        hi = full
    elif hi_text[0] == "0":
        return None
    if not (0 < lo < hi <= lo + SPAN_MAX) or (hi - lo) % 2:
        return None
    letter = (letter or "").upper()
    return HouseRange(lo, hi, letter, f"{lo_text}-{hi_text}{letter}")


_TOKEN = re.compile(r"[0-9]+|[A-Za-z]+(?:['’][A-Za-z]+)?|-|[^\sA-Za-z0-9-]")
CONNECTORS = {"to", "through", "thru", "dash", "-"}
# a number after one of these is never a house number's range: a highway's exit, marker or route, an apparatus, a unit
# of a building, a time or a box
NOT_AFTER = {"exit", "exits", "interchange", "mm", "mp", "marker", "milepost", "mile", "route", "rte", "rt", "i",
             "interstate", "us", "ny", "nys", "sr", "cr", "hwy", "highway", "engine", "engines", "ladder", "ladders",
             "tower", "rescue", "squad", "truck", "battalion", "car", "chief", "ambulance", "medic", "utility",
             "tanker", "brush", "unit", "units", "apt", "apartment", "suite", "ste", "room", "rm", "floor", "fl", "flr",
             "lot", "bldg", "building", "time", "box", "channel", "tac", "code", "number", "no", "phone", "callback",
             "between", "ages", "age", "aged", "out"}
_SUFFIX_WORDS = {w for w, a in STREET_TYPES.items() if a in TYPE_ABBRS} | TYPE_ABBRS | {"broadway"}


@dataclass(frozen=True)
class Found:
    start: int           # where it is in the text (characters)
    end: int
    range: HouseRange
    spoken: bool         # said with a word ("to", "through", "dash") or in number words


def _number_at(toks: list[re.Match], i: int) -> tuple[str, int, bool] | None:
    """(digits, the token after it, said in words) for a number starting at token i: digits, or a run of number words
    ("forty two", "one forty", "forty-two")."""
    if i >= len(toks):
        return None
    t = toks[i].group(0)
    if t.isdigit():
        return t, i + 1, False
    run, j = [], i
    while j < len(toks):
        w = toks[j].group(0).lower()
        if w in NUMBER_WORDS and not (not run and w in ("oh", "o")) and w not in MULTIPLIERS or (w in MULTIPLIERS and run):
            run.append(w)
            j += 1
            continue
        # "forty-two": the hyphen inside a number said in words (a tens word, then a unit word)
        if (w == "-" and run and run[-1] in TENS and j + 1 < len(toks)
                and toks[j + 1].group(0).lower() in UNITS and toks[j + 1].group(0).lower() not in ("oh", "o", "zero")
                and toks[j].start() == toks[j - 1].end() and toks[j + 1].start() == toks[j].end()):
            j += 1
            continue
        break
    if not run:
        return None
    got = _parse_run(run)
    if not got or not got.isdigit():
        return None
    return got, j, True


def find(text: str) -> list[Found]:
    """The house-number ranges in free text (a page's transcript, a feed's or a person's location), each before a
    street's words: written ("14-16 Sample Ave": a word after it) or spoken ("14 to 16 Sample Avenue", "fourteen dash
    sixteen ...": a street's suffix within SUFFIX_REACH words after it). Never after NOT_AFTER's words, never glued to
    other digits ("914-555-0100", "189-1/2", "1.5-3")."""
    s = text or ""
    toks = list(_TOKEN.finditer(s))
    out: list[Found] = []
    i = 0
    while i < len(toks):
        a = _number_at(toks, i)
        if a is None:
            i += 1
            continue
        lo, j, lo_words = a
        prev = toks[i - 1] if i else None
        glued_before = prev is not None and prev.end() == toks[i].start() and prev.group(0) in "-./:"
        after_word = prev is not None and prev.group(0).lower() in NOT_AFTER
        if glued_before or after_word or j >= len(toks) or toks[j].group(0).lower() not in CONNECTORS:
            i = j if lo_words else i + 1
            continue
        conn = toks[j].group(0).lower()
        b = _number_at(toks, j + 1)
        if b is None:
            i += 1
            continue
        hi, k, hi_words = b
        letter = ""
        if (not hi_words and k < len(toks) and toks[k].start() == toks[k - 1].end()
                and re.fullmatch(r"[A-Za-z]", toks[k].group(0)) and toks[k].group(0).lower() not in "nsew"):
            letter, k = toks[k].group(0), k + 1           # "16A" (never "16W": the street's direction)
        nxt = toks[k] if k < len(toks) else None
        if nxt is None or not nxt.group(0).isalpha() or nxt.start() == toks[k - 1].end() \
                or (nxt.group(0).lower() in NUMBER_WORDS and hi_words):
            i += 1
            continue
        spoken = conn != "-" or lo_words or hi_words
        if spoken:                                        # said: only before a street (a suffix soon after)
            words = [t.group(0).lower() for t in toks[k:k + SUFFIX_REACH]]
            cut = next((n for n, w in enumerate(words) if not w.isalpha()), len(words))
            if not any(w in _SUFFIX_WORDS for w in words[:cut]):
                i += 1
                continue
        r = make(lo, hi, letter)
        if r is None:
            i += 1
            continue
        out.append(Found(toks[i].start(), toks[k - 1].end(), r, spoken))
        i = k
    return out


def at_start(text: str) -> tuple[HouseRange, int] | None:
    """A location that starts with a range ("14-16 SAMPLE AVE ..."): (the range, where the rest starts); None for one
    that doesn't (a single number, a span too wide, nothing after it)."""
    s = text or ""
    m = re.match(r"\s*\d", s)
    if not m:
        return None
    for f in find(s):
        if not s[:f.start].strip():
            return f.range, f.end
        break
    return None


def canonical(text: str, dash: str = "-") -> str:
    """The text with each range written "lo-hi" ("respond to fourteen to sixteen Sample Avenue" -> "respond to 14-16
    Sample Avenue"): as given ("149-51") when written with digits, the dash as given (dash)."""
    s = text or ""
    out, at = [], 0
    for f in find(s):
        out.append(s[at:f.start])
        r = f.range
        out.append((f"{r.lo}{dash}{r.hi}{r.letter}" if f.spoken else r.written.replace("-", dash)))
        at = f.end
    return "".join(out) + s[at:]


def ranges_in(text: str, street_at=None) -> tuple[str, dict[tuple[int, object], HouseRange]]:
    """For the gazetteer's readers: the text with each range left as its first number ("14-16 Sample Ave" -> "14 Sample
    Ave": the tokenizers would read "14 16" as 1416), and {(first number, the street's id): the range}. street_at(the
    text after it) -> the ids of the street it starts with (none, or no street_at: the id None, any street)."""
    s = text or ""
    out: dict[tuple[int, object], HouseRange] = {}
    parts, at = [], 0
    for f in find(s):
        parts.append(s[at:f.start])
        parts.append(str(f.range.lo))
        at = f.end
        sids = (street_at(s[f.end:f.end + 80]) if street_at else ()) or (None,)
        for sid in sids:
            out.setdefault((f.range.lo, sid), f.range)
    return "".join(parts) + s[at:], out


def range_of(ranges: dict, num: int, sid) -> HouseRange | None:
    """The range said with first number num on street sid (ranges_in), else None."""
    return ranges.get((num, sid)) or ranges.get((num, None))


def parse_number(number: str) -> HouseRange | None:
    """A house number as a key or a location holds it ("14-16", "149-51", "14-16a"): its range, else None."""
    m = re.fullmatch(r"\s*(\d{1,5})\s*-\s*(\d{1,5})([a-z])?\s*", str(number or ""), re.I)
    return make(m.group(1), m.group(2), m.group(3) or "") if m else None


def same_number(a: str, b: str) -> bool:
    """Two house numbers are one building's: the same ("12", "12A"); a single number and a range holding it ("16" and
    "14-16"); two ranges, one holding the other ("14-16" and "14-18"). Never a number outside the range ("18" for
    "14-16", "15" across the street), never two ranges that only touch ("14-16" and "16-18")."""
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    if a == b:
        return True
    ra, rb = parse_number(a), parse_number(b)
    if ra is not None and rb is not None:
        return ra.labels() <= rb.labels() or rb.labels() <= ra.labels()
    if ra is not None:
        return ra.holds(b)
    if rb is not None:
        return rb.holds(a)
    return False
