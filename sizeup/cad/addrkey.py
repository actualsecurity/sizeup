"""An incident's address key and building key (#403): what "the same address" means for History here, the Search
page and premise notes.

    addr_key    "123|example ave|port chester|2b": house number, the street as the gazetteer spells it (as a
                street_key: lower case, suffixes abbreviated), town, and the apartment / unit / floor when given
    bldg_key    "123|example ave|port chester": the building, apt / unit / floor stripped, a house number's letter
                too (12A and 12B are one lot); '' when only a map point is known
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
"UNIT 3", "3RD FL"), a range ("12-14 Example Ave": 12), a named place ("@ Sample School", "575 Example Rd @ Sample
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
from sizeup.geo.normalize import street_key

SNAP_CONFIDENCE = 0.7      # the gazetteer's reading of an address is trusted from here (a misspelt street at ~0.74)
SNAP_M = 150.0             # ... and only within this of a pin the incident already has
GROUP_M = 20.0             # a call keyed by its point: the calls this close are the same building

# An apartment / unit / floor, anywhere after the street: "APT 2B", "APT. 2B", "#2B", "UNIT 3", "STE 100", "FL 2",
# "3RD FL", "2ND FLOOR", "BSMT", "REAR"
UNIT = re.compile(
    r"(?:\b(?:apt|apartment|unit|ste|suite|rm|room|bldg|building|fl|flr|floor|lot)\b\.?\s*(?:no\.?\s*|#\s*)?([a-z0-9-]{1,8})\b"
    r"|#\s*([a-z0-9-]{1,8})\b"
    r"|\b(\d{1,2}(?:st|nd|rd|th))\s+(?:fl|flr|floor)\b"
    r"|\b(bsmt|basement|rear|front|upper|lower)\b)", re.I)
UNIT_WORD = {"apt": "apt", "apartment": "apt", "unit": "unit", "ste": "ste", "suite": "ste", "rm": "rm", "room": "rm",
             "bldg": "bldg", "building": "bldg", "fl": "fl", "flr": "fl", "floor": "fl", "lot": "lot"}
NUMBER = re.compile(r"^\s*(\d{1,6})\s?([a-z])?(?:\s*-\s*\d{1,6}[a-z]?)?\b\s*", re.I)
CROSSING = re.compile(r"\s(?:/|&|and|at)\s|^\s*@|\bx\b", re.I)     # two streets, or a place only: not one address


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
    number: str            # "123", "12A"
    street: str            # as written, minus the number, unit and town ("EXAMPLE AVE")
    unit: str              # "apt 2b", "fl 3", "rear", ''
    town: str              # a known town named in the text ("Port Chester"), ''

    @property
    def lot(self) -> str:
        """The house number without its letter: 12A -> 12 (one lot)."""
        return re.sub(r"\D", "", self.number)


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
    s = head
    unit = ""
    m = UNIT.search(s)
    if m:
        word = m.group(0).split()[0].lower().rstrip(".")
        if m.group(1):                                # APT 2B, UNIT 2B, #2B: one apartment ("2b"); a floor, a building
            w = UNIT_WORD.get(word, word)
            unit = m.group(1).lower() if w in ("apt", "unit", "ste", "rm") else f"{w} {m.group(1).lower()}"
        elif m.group(2):
            unit = m.group(2).lower()
        elif m.group(3):
            unit = "fl " + re.sub(r"\D", "", m.group(3))
        else:
            unit = m.group(4).lower().replace("basement", "bsmt")
        s = s[:m.start()]
    num = NUMBER.match(s)
    if not num:
        return None
    number = num.group(1) + (num.group(2) or "").upper()
    rest = s[num.end():].strip(" ,.-")
    low = " " + " ".join(re.findall(r"[a-z0-9]+", rest.lower())) + " "
    for alias, name in _towns(aliases):           # "123 EXAMPLE AVE PORT CHESTER": the town at the end
        if low.endswith(f" {alias} ") and len(low.split()) > len(alias.split()):
            town = town or name
            words = rest.split()
            rest = " ".join(words[:len(words) - len(alias.split())])
            break
    rest = rest.strip(" ,.-")
    if not re.search(r"[a-z]", rest, re.I):
        return None
    return Parsed(number, rest, unit, town)


def _key(p: Parsed, street: str, town: str, how: str) -> Key:
    sk = street_key(street)
    t = (town or "").lower()
    bldg = f"{p.lot}|{sk}|{t}"
    label = f"{p.number} {proper_case(sk if how == 'text' else street)}" + (f", {town}" if town else "")
    return Key(bldg + (f"|{p.unit}" if p.unit else ""), bldg, f"{sk}|{t}", how, label)


def meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 110_574.0
    dx = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def bbox(lat: float, lon: float, m: float = GROUP_M) -> tuple[float, float, float, float]:
    dlat = m / 110_574.0
    dlon = m / (111_320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def snap(p: Parsed, geocode: Callable[[str], list[dict] | None], town_of: Callable[[str], str],
         point: tuple[float, float] | None = None) -> Key | None:
    """The gazetteer's reading of a parsed address, when it is sure: an address (not a street or a place), the same
    house number, SNAP_CONFIDENCE, and near the incident's own pin if it has one."""
    q = f"{p.number} {p.street}" + (f", {p.town}" if p.town else "")
    try:
        ms = geocode(q) or []
    except Exception:  # noqa: BLE001 - a key is never worth an error
        return None
    for m in ms:
        if m.get("kind") != "address" or float(m.get("confidence") or 0) < SNAP_CONFIDENCE or not m.get("street"):
            continue
        if re.sub(r"\D", "", str(m.get("house_number") or "")) != p.lot:
            continue
        if point is not None and m.get("lat") is not None and meters(point[0], point[1], m["lat"], m["lon"]) > SNAP_M:
            continue
        return _key(p, m["street"], town_of(m.get("municipality") or "") or p.town, "gazetteer")
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
            k = snap(p, geocode, town_of, point)
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
