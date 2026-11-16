"""Local address points: the department's own (Settings > Lists > Address points, cad.sqlite section "addresses").

The map data lacks some real addresses: a building behind another one ("189-1/2 Example St"), a station with no point
of its own ("3 Sample St"). An admin types the address and pins it on the map; the gazetteer then finds it like one of
its own points (precision "exact", source "local"), ahead of an interpolation or a fractional number's integer part:
the place search, a page's location, the location box, a map click and the common places import all see it.

House numbers are read with one helper (``parse_number``): "189-1/2", "189 1/2", "189½" and "189.5" are one number,
written "189 1/2". A letter after the digits ("189A") is a suffix, as the address points have them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

FRACTION_CHARS = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3"}
DECIMALS = {"5": "1/2", "50": "1/2", "25": "1/4", "75": "3/4"}
_FRAC = "".join(FRACTION_CHARS)
# digits, a letter right after them (not a word: "12th"), then a fraction: "-1/2", " 1/2", "½", ".5"
_NUM = re.compile(r"(\d{1,6})(?:([a-z])(?![a-z]))?"
                  rf"(?:\s*-\s*(\d)\s*/\s*(\d)|\s+(\d)\s*/\s*(\d)|\s*([{_FRAC}])|\.(\d{{1,2}})(?![\d./]))?", re.I)
# a fractional house number in free text, before a word (the street): "189-1/2 Example St", "189½ Example"
_IN_TEXT = re.compile(r"(?<![\d./])(\d{1,6})(?:([a-z])(?![a-z]))?"
                      rf"(?:\s*-\s*(\d)\s*/\s*(\d)|\s+(\d)\s*/\s*(\d)|\s*([{_FRAC}])|\.(\d{{1,2}})(?![\d./]))(?=\s*[a-z])",
                      re.I)
# a number after these is no house number ("mm 10.5", "rt 9.5", "box 12.25", "i-95"); nor one before these ("10.5 min")
_NOT_HOUSE = re.compile(r"\b(?:mm|mile|miles|marker|mp|milepost|post|exit|ref|route|rte|rt|hwy|highway|i|interstate|"
                        r"pkwy|parkway|box|unit|engine|ladder|car|apt|suite|floor|no|number)[\s#-]*$", re.I)
_NOT_STREET = re.compile(r"\s*(?:min|mins|minutes|mi|miles|ft|feet|hours?|hrs?|secs?|seconds|stor(?:y|ies)|"
                         r"floors?|percent|degrees?|%)\b", re.I)


@dataclass(frozen=True)
class HouseNumber:
    num: int
    letter: str = ""         # "a" (lower case), as the address points' suffix
    frac: str = ""           # "1/2"

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.num, self.letter, self.frac)

    @property
    def label(self) -> str:
        return f"{self.num}{self.letter.upper()}" + (f" {self.frac}" if self.frac else "")


def _frac(m: re.Match, base: int) -> str | None:
    """The fraction of a match (groups from ``base``): '1/2', '' (none) or None (not one: "23.3")."""
    a, b, c, d, ch, dec = m.groups()[base:base + 6]
    if a or c:
        n, k = int(a or c), int(b or d)
        return f"{n}/{k}" if 0 < n < k else None
    if ch:
        return FRACTION_CHARS[ch]
    if dec is not None:
        return DECIMALS.get(dec)
    return ""


def parse_number(text: str) -> HouseNumber | None:
    """A house number as written ("189", "189A", "189-1/2", "189 1/2", "189½", "23.5") -> HouseNumber, else None."""
    t = " ".join((text or "").split())
    m = _NUM.fullmatch(t)
    if not m or int(m.group(1)) == 0:
        return None
    frac = _frac(m, 2)
    if frac is None:
        return None
    return HouseNumber(int(m.group(1)), (m.group(2) or "").lower(), frac)


def split_address(text: str) -> tuple[HouseNumber, str, str] | None:
    """'189-1/2 Example St' -> (HouseNumber(189, '', '1/2'), 'Example St', '189-1/2' as written); None without a house
    number first."""
    t = " ".join((text or "").split())
    m = _NUM.match(t)
    if not m or int(m.group(1)) == 0:
        return None
    rest = t[m.end():]
    if rest and not rest[0].isspace() and rest[0] not in ",":
        return None
    frac = _frac(m, 2)
    if frac is None:
        return None
    rest = rest.strip(" ,")
    return (HouseNumber(int(m.group(1)), (m.group(2) or "").lower(), frac), rest, m.group(0)) if rest else None


def fractions_in(text: str, street_at) -> tuple[str, dict[tuple[int, int], str]]:
    """The fractional house numbers in free text, each tied to the street right after it: ("... 189 Example St ...",
    {(189, the street's id): '1/2'}) for "... 189-1/2 Example St ...". street_at(the text after the number) -> the ids
    of the street it starts with (none: not a house number, left as it is: "3 1/2 story", "10.5 min ago"). The text
    comes back with those fractions taken off (the tokenizers in sizeup/geo/normalize.py would read "23.5" as 235)."""
    out: dict[tuple[int, int], str] = {}

    def keep(m: re.Match) -> str:
        f = _frac(m, 2)
        if not f or _NOT_HOUSE.search(m.string[max(0, m.start() - 16):m.start()]):
            return m.group(0)                    # "mm 10.5 eastbound": a mile marker, not a house
        after = m.string[m.end():m.end() + 80]
        if _NOT_STREET.match(after):
            return m.group(0)
        sids = street_at(after) if street_at else ()
        if not sids:
            return m.group(0)
        for sid in sids:
            out.setdefault((int(m.group(1)), sid), f)
        return m.group(1) + (m.group(2) or "")
    return _IN_TEXT.sub(keep, text or ""), out


# ----------------------------------------------------------------------------------- the gazetteer's side

@dataclass(frozen=True)
class Point:
    sid: int
    muni: str
    number: HouseNumber
    lat: float
    lon: float
    note: str = ""


class LocalAddresses:
    """The local points of one gazetteer, by street and town. Built from the source's rows
    [{number, street (the gazetteer's name), muni, lat, lon, note?}]; a row whose street or number isn't known is
    skipped."""

    def __init__(self, rows, street_id):
        self.rows = rows
        self.points: list[Point] = []
        self._at: dict[tuple[int, str], dict[tuple[int, str, str], Point]] = {}
        self.towns: dict[int, list[str]] = {}
        for r in rows or ():
            try:
                sid = street_id(r.get("street") or "")
                hn = parse_number(str(r.get("number") or ""))
                lat, lon = float(r["lat"]), float(r["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if sid is None or hn is None:
                continue
            p = Point(sid, r.get("muni") or "", hn, lat, lon, r.get("note") or "")
            self.points.append(p)
            self._at.setdefault((sid, p.muni), {})[hn.key] = p
            if p.muni not in self.towns.setdefault(sid, []):
                self.towns[sid].append(p.muni)

    def __bool__(self) -> bool:
        return bool(self.points)

    def get(self, sid: int, muni: str, num: int, letter: str = "", frac: str = "") -> Point | None:
        at = self._at.get((sid, muni))
        return at.get((num, (letter or "").lower(), frac or "")) if at else None

    def like(self, sid: int, muni: str, num: int) -> list[Point]:
        """The points on that street and town with that integer part (189, 189A, 189 1/2)."""
        at = self._at.get((sid, muni))
        return [p for k, p in at.items() if k[0] == num] if at else []

    def nearest(self, lat: float, lon: float) -> tuple[float, Point] | None:
        """(metres, the point) nearest (lat, lon)."""
        best = None
        k = 111_320.0 * math.cos(math.radians(lat))
        for p in self.points:
            d = math.hypot((p.lat - lat) * 110_574.0, (p.lon - lon) * k)
            if best is None or d < best[0]:
                best = (d, p)
        return best


EMPTY = LocalAddresses((), lambda _n: None)


def set_local_addresses(gaz, source) -> None:
    """source() -> the local points [{number, street, muni, lat, lon, note?}] (the CAD's Settings,
    Cad.local_addresses). Read at each lookup (the same rows object while nothing changed); indexed again on a change."""
    gaz._local_addr_source = source
