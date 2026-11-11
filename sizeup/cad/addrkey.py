"""An incident's address key and building key (#403): what "the same address" means for History here, the Search
page and premise notes.

    addr_key    "123|example ave|port chester|2b": house number, the street as the gazetteer spells it (as a
                street_key: lower case, suffixes abbreviated), town, and the apartment / unit / floor when given
    bldg_key    "123|example ave|port chester": the building, apt / unit / floor / rear stripped; a complex's
                building kept ("...|bldg 3"); 12A, 12 1/2 and 12 are three buildings (History here joins 12A and 12
                only when their pins are within SAME_LOT_M), a range "10-12" is its own key and also the same building
                as 10 and as 12; '' when only a map point is known
    street_key  "example ave|port chester": a street search
    how         gazetteer   the gazetteer read the address with confidence (a misspelt or misheard street is snapped to
                            its street only then: SNAP_CONFIDENCE, and within SNAP_M of the incident's own pin)
                text        no gazetteer here (tests, a bare dev box): the text alone, normalised the same way
                geo         the text is not an address the gazetteer knows (a misheard page street, an intersection, a
                            place with no street address): keyed by its map point, grouped with calls within GROUP_M
                ''          neither an address nor a point
    label       "123 Example Ave, Port Chester": shown on the card and the Search page (proper case)

The variants the department's feeds and pages send: ALL CAPS ("123 EXAMPLE AVE"), towns as UPPER_SNAKE tokens
("PORT_CHESTER"), the town after a comma or at the end, "Avenue" / "Ave." / "AVE", apartments ("APT 2B", "#2B",
"UNIT 3", "3RD FL", "REAR" after the suffix: never Front St, Upper Ridge Rd), a street's direction ("12 W EXAMPLE AVE"
is 12, West Example Ave), a range ("12-14 Example Ave"), a named place ("@ Sample School", "575 Example Rd @ Sample
Market", "@SAMPLE SCHOOL (12 EXAMPLE AVE)").

Pure functions: the gazetteer is passed in as geocode(text) -> [{kind, street, house_number, municipality,
confidence, lat, lon}] (Cad.geocoder: sizeup/app.py _geocode_text), best first.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable

from sizeup.cad.sources import proper_case
from sizeup.geo.normalize import STREET_TYPES, TYPE_ABBRS, street_key

SNAP_CONFIDENCE = 0.7      # the gazetteer's reading of an address is trusted from here (a misspelt street at ~0.74)
SNAP_M = 150.0             # ... and only within this of a pin the incident already has
GROUP_M = 20.0             # a call keyed by its point: the calls this close are the same building
SAME_LOT_M = 10.0          # 12A and 12 (one street): the same building only when their pins are this close

# Unit words: one with an identifier after it ("APT 3", "UNIT 2B", "FL 2", "BLDG 3", "#2B", "3RD FL") anywhere after the
# first street word; a bare one ("REAR", "BSMT", "UPPER") only after the street's suffix, so Front St, Upper Ridge Rd,
# Lower Example Ln, Rear Example Rd and Lot Rd stay street names. BLDG names a building of a complex (in the building
# key); the rest name a part of one (apartment, floor, rear: the address key only).
UNIT_WORD = {"apt": "apt", "apartment": "apt", "unit": "apt", "ste": "apt", "suite": "apt", "rm": "apt", "room": "apt",
             "bldg": "bldg", "building": "bldg", "fl": "fl", "flr": "fl", "floor": "fl", "lot": "lot"}
BARE_UNIT = {"bsmt": "bsmt", "basement": "bsmt", "rear": "rear", "front": "front", "upper": "upper", "lower": "lower"}
UNIT_ID = re.compile(r"^#?(?:[a-z]?\d[a-z0-9-]{0,7}|[a-z])$", re.I)          # 3, 2B, B2, 12-A, B
ORDINAL_FL = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)$", re.I)                   # 3RD (FL)
# The house number: 12, 12A (a letter right after it, never "12 W": that is the street's direction), 12 1/2, 10-12
NUMBER = re.compile(r"^\s*(\d{1,6})(?:([a-z])(?=\s)|\s+(1/2|½)(?=\s)|\s*-\s*(\d{1,6})[a-z]?(?=\s))?\s+", re.I)
# two streets, or a place only: not one address ("12 At Rd" is a street: "at" right before a suffix)
CROSSING = re.compile(r"\s(?:/|&|and)\s|\sat\s(?!(?:rd|st|ave|ln|dr|ct|pl|way|ter|blvd)\b)|^\s*@|\bx\b", re.I)


@dataclass(frozen=True)
class Key:
    addr_key: str = ""
    bldg_key: str = ""
    street_key: str = ""
    how: str = ""
    label: str = ""

    def as_dict(self) -> dict:
        return {"addr_key": self.addr_key, "bldg_key": self.bldg_key, "addr_street": self.street_key,
                "addr_how": self.how, "addr_label": self.label}


@dataclass(frozen=True)
class Parsed:
    number: str            # as shown: "123", "12A", "12 1/2", "10-12"
    street: str            # as written, minus the number, units and town ("EXAMPLE AVE")
    unit: str              # the part of a building: "2b", "fl 3", "rear", "2b rear", ''
    town: str              # a known town named in the text ("Port Chester"), ''
    bldg: str = ""         # a complex's building: "bldg 3", ''
    raw_street: str = ""   # the street with any unit words left in (the gazetteer may know "Rear Example Rd")

    @property
    def lot(self) -> str:
        """The house number in the keys: "12", "12a" (its own building), "12 1/2", "10-12" (a range)."""
        return self.number.lower().replace("½", "1/2")

    @property
    def first(self) -> str:
        """The first house number's digits: 12A -> 12, 10-12 -> 10 (what the gazetteer is asked for)."""
        return re.match(r"\d+", self.number).group(0)

    def ends(self) -> tuple[str, ...]:
        """A range's two numbers ("10-12" -> ("10", "12")): each is also the same building; () for one number."""
        m = re.fullmatch(r"(\d+)-(\d+)", self.lot)
        return (m.group(1), m.group(2)) if m else ()


def _towns(aliases: dict[str, str]) -> list[tuple[str, str]]:
    return sorted(aliases.items(), key=lambda kv: -len(kv[0]))


def street_text(text: str) -> str:
    """The address part of a location as the CAD and the feeds write it: the street address of a named place, without
    the place ('575 Example Rd @ Sample Market' -> '575 Example Rd'; '@SAMPLE SCHOOL (12 EXAMPLE AVE)' -> '12 EXAMPLE
    AVE'; '@ Sample School' -> ''), underscores as spaces, the rest of a ' - ' / ';' / ':' tail dropped."""
    s = (text or "").strip()
    m = re.match(r"^\s*@[^(]*\(([^)]*)\)", s)
    if m:
        s = m.group(1)
    elif s.startswith("@"):
        return ""
    s = re.split(r"\s:\s|\s*:\s*@|@|;|\s+-\s+", s)[0]
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    return " ".join(s.replace("_", " ").split())


def parse(text: str, aliases: dict[str, str]) -> Parsed | None:
    """'123 EXAMPLE AVE APT 2B, PORT_CHESTER' -> Parsed('123', 'EXAMPLE AVE', 'apt 2b', 'Port Chester'); None when it is
    not one street address (no house number, two streets, a place only)."""
    s = street_text(text)
    if not s or CROSSING.search(f" {s} "):
        return None
    town = ""
    head, _, tail = s.partition(",")
    for part in tail.split(","):
        t = " ".join(re.findall(r"[a-z0-9]+", part.lower()))
        if t in aliases and not town:
            town = aliases[t]
    num = NUMBER.match(head + " ")
    if not num:
        return None
    words = (head + " ")[num.end():].replace(",", " ").split()
    if num.group(2) and num.group(2).lower() in "nsew":      # "12W EXAMPLE AVE": the street's direction, not 12W
        number = num.group(1)
        words = [num.group(2)] + words
    elif num.group(2):
        number = num.group(1) + num.group(2).upper()
    elif num.group(3):
        number = f"{num.group(1)} 1/2"
    elif num.group(4):
        number = f"{num.group(1)}-{num.group(4)}" if num.group(4) != num.group(1) else num.group(1)
    else:
        number = num.group(1)
    low = [re.sub(r"[^a-z0-9#-]", "", w.lower()) for w in words]
    for alias, name in _towns(aliases):           # "123 EXAMPLE AVE PORT CHESTER" / "... APT 3 PORT_CHESTER": the town
        n = len(alias.split())                    # at the end
        if len(low) > n and " ".join(low[-n:]) == alias:
            town = town or name
            words, low = words[:-n], low[:-n]
            break
    street_end, bldg, parts = _units(low)
    raw = " ".join(words).strip(" ,.-")
    street = " ".join(words[:street_end]).strip(" ,.-")
    if not re.search(r"[a-z]", street, re.I):
        return None
    return Parsed(number, street, " ".join(parts), town, bldg, raw)


def _suffix_at(low: list[str]) -> int:
    """Where the street's suffix is (the last word that is one: "st", "avenue"), -1 for none."""
    at = -1
    for i, w in enumerate(low):
        if STREET_TYPES.get(w, w) in TYPE_ABBRS:
            at = i
    return at


def _units(low: list[str]) -> tuple[int, str, list[str]]:
    """(where the street ends, the building "bldg 3" or '', the parts of the building ["2b", "fl 3", "rear"]) from a
    location's words after its house number, lower case."""
    suffix = _suffix_at(low)
    i, start, bldg, parts = 1, None, "", []
    while i < len(low):
        w = low[i].rstrip(".")
        nxt = low[i + 1].lstrip("#") if i + 1 < len(low) else ""
        took = 0
        if w in UNIT_WORD and nxt and UNIT_ID.match(nxt):            # APT 3, UNIT 2B, BLDG 3, FL 2, LOT 7
            kind = UNIT_WORD[w]
            if kind == "bldg":
                bldg = f"bldg {nxt}"
            else:
                parts.append(nxt if kind == "apt" else f"{kind} {nxt}")
            took = 2
        elif w.startswith("#") and UNIT_ID.match(w):                 # #2B
            parts.append(w.lstrip("#"))
            took = 1
        elif ORDINAL_FL.match(w) and nxt in ("fl", "flr", "floor"):  # 3RD FL
            parts.append("fl " + ORDINAL_FL.match(w).group(1))
            took = 2
        elif w in BARE_UNIT and 0 <= suffix < i:                     # after the suffix: REAR, BSMT, UPPER
            parts.append(BARE_UNIT[w])
            took = 1
        if took:
            start = i if start is None else start
            i += took
        elif start is not None:                   # a word after a unit that is not one: keep it out of the street too
            i += 1
        else:
            i += 1
    return (len(low) if start is None else start), bldg, parts


def _key(p: Parsed, street: str, town: str, how: str) -> Key:
    sk = street_key(street)
    t = (town or "").lower()
    bldg = f"{p.lot}|{sk}|{t}" + (f"|{p.bldg}" if p.bldg else "")       # a complex's building is a building
    label = (f"{p.number} {proper_case(sk if how == 'text' else street)}" + (f" {p.bldg.title()}" if p.bldg else "")
             + (f", {town}" if town else ""))
    return Key(bldg + (f"|{p.unit}" if p.unit else ""), bldg, f"{sk}|{t}", how, label)


def bldg_parts(bldg_key: str) -> tuple[str, str, str, str]:
    """'12|example ave|port chester|bldg 3' -> ('12', 'example ave', 'port chester', 'bldg 3')."""
    p = (bldg_key.split("|") + ["", "", "", ""])[:4]
    return p[0], p[1], p[2], p[3]


def meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 110_574.0
    dx = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def bbox(lat: float, lon: float, m: float = GROUP_M) -> tuple[float, float, float, float]:
    dlat = m / 110_574.0
    dlon = m / (111_320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def snap(p: Parsed, geocode: Callable[[str], list[dict] | None], town_of: Callable[[str], str],
         point: tuple[float, float] | None = None, town_hint: str = "") -> Key | None:
    """The gazetteer's reading of a parsed address, when it is sure: an address (not a street or a place), the same
    house number, SNAP_CONFIDENCE, near the incident's own pin if it has one, and, with no pin, in the town the text
    names (or the hint) if it names one. The street as written first, then with any unit words left in ("Rear Example
    Rd" may be the street's own name)."""
    want = p.town or town_hint
    tries = [p] + ([Parsed(p.number, p.raw_street, "", p.town, "", p.raw_street)] if p.raw_street and p.raw_street != p.street else [])
    for t in tries:
        q = f"{t.first} {t.street}" + (f", {want}" if want else "")
        try:
            ms = geocode(q) or []
        except Exception:  # noqa: BLE001 - a key is never worth an error
            return None
        for m in ms:
            if m.get("kind") != "address" or float(m.get("confidence") or 0) < SNAP_CONFIDENCE or not m.get("street"):
                continue
            hn = re.match(r"\d+", str(m.get("house_number") or ""))
            if not hn or hn.group(0) != t.first:
                continue
            if point is not None and m.get("lat") is not None and meters(point[0], point[1], m["lat"], m["lon"]) > SNAP_M:
                continue
            town = town_of(m.get("municipality") or "") or t.town
            if point is None and want and town and town != want:
                continue                          # the text says another town: not this one's reading
            return _key(t, m["street"], town, "gazetteer")
    return None


def key_incident(inc: dict, aliases: dict[str, str], town_of: Callable[[str], str] = lambda m: m,
                 geocode: Callable[[str], list[dict] | None] | None = None, town_hint: str = "") -> Key:
    """The key of an incident's location (its own: never the caller's address). Tried in order: the named place's
    street address, the location as shown, the map point's label; the first the gazetteer is sure of wins. With no
    gazetteer: the first that parses, as text. Else its map point (geo), else nothing."""
    lat, lon = inc.get("lat"), inc.get("lon")
    point = (float(lat), float(lon)) if lat is not None and lon is not None else None
    parsed: list[Parsed] = []
    for t in dict.fromkeys(x for x in (inc.get("place_address"), inc.get("address"), inc.get("geo_label")) if x):
        p = parse(t, aliases)
        if p is not None:
            parsed.append(p)
    if geocode is not None:
        for p in parsed:
            k = snap(p, geocode, town_of, point, town_hint)
            if k is not None:
                return k
    elif parsed:
        p = parsed[0]
        return _key(p, p.street, p.town or town_hint, "text")
    if point is not None:
        return Key(f"@{point[0]:.5f},{point[1]:.5f}", "", "", "geo", inc.get("geo_label") or proper_case(inc.get("address") or ""))
    if parsed:                                    # a gazetteer that knows none of it, and no pin: the text, as typed
        p = parsed[0]
        return _key(p, p.street, p.town or town_hint, "text")
    return Key()


def key_text(text: str, aliases: dict[str, str], town_of: Callable[[str], str] = lambda m: m,
             geocode: Callable[[str], list[dict] | None] | None = None) -> Key:
    """A typed address (the Search page's address box, a premise note's address): the same key as an incident with that
    location and no pin."""
    return key_incident({"address": text}, aliases, town_of, geocode)
