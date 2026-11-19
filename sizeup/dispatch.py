"""Parse a transcribed dispatch page into structured fields.

    "Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester
     Avenue, cross of Pearl Street, Port Chester. Time out 1432."
  -> departments ["Port Chester FD"], type "Fire alarm" (category fire), location text
     "209 West Chester Avenue", cross street "Pearl Street", time out "14:32"

The cross streets are the next two streets after the address; a street said with the address's own house number in
front ("47 Sample Avenue ... 47 Model Avenue": the page read again, misheard) is never one.

The incident vocabulary is a first guess at 60 Control phrasing; it is meant to be tuned on
real captured pages (docs/PLAN.md). Unknown types are kept as "Unknown" with the raw text:
the display never invents a type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (display type, category, phrases). First match wins, so specific before generic.
INCIDENT_TYPES: list[tuple[str, str, tuple[str, ...]]] = [
    # The airport's alert levels (#87: an Alert 2 or 3 brings Port Chester): the level is read again by
    # sizeup/cad/airport.py when the page becomes an event.
    ("Airport alert", "fire", ("alert 1", "alert one", "alert 2", "alert two", "alert 3", "alert three", "alert level",
                               "airport alert", "airport incident", "full emergency", "local standby", "aircraft accident",
                               "aircraft emergency", "inbound with an emergency", "declared an emergency", "emergency landing",
                               "aircraft crash", "plane crash", "aircraft down", "plane down", "aircraft in the water",
                               "plane in the water", "runway excursion", "off the runway", "disabled aircraft")),
    ("Working fire", "fire", ("10 75", "ten seventy five", "1075", "working fire", "working structure fire")),
    ("Structure fire", "fire", ("structure fire", "house fire", "building fire", "fire in a building",
                                "apartment fire", "commercial fire", "reported structure", "fire in the building")),
    ("Car fire", "fire", ("car fire", "vehicle fire", "auto fire", "truck fire")),
    ("Brush fire", "fire", ("brush fire", "grass fire", "mulch fire", "outside fire", "dumpster fire",
                            "rubbish fire", "garbage fire")),
    # A CO detector said with the words of a fire alarm's ("carbon monoxide smoke detector activation", #365): before
    # "Fire alarm". A plural ("smoke and carbon monoxide detectors sounding") is still a fire alarm's
    ("CO alarm", "hazard", ("carbon monoxide smoke detector", "carbon monoxide detector", "co detector activation",
                            "c o detector activation")),
    ("Fire alarm", "fire", ("activated fire alarm", "fire alarm", "automatic alarm", "alarm activation",
                            "commercial alarm", "residential alarm", "commercial line",  # ASR of "commercial alarm"
                            "smoke detector", "smoke alarm", "pull station", "water flow alarm", "waterflow")),
    ("CO alarm", "hazard", ("carbon monoxide", "co alarm", "c o alarm", "co detector", "c o detector")),
    # "inside order gas" (60 Control 2026-10-15, #176): STT hears "odor of gas" as "order gas", "order of gas"
    ("Gas leak", "hazard", ("gas leak", "odor of gas", "smell of gas", "natural gas", "gas condition",
                            "propane", "odor gas", "order of gas", "order gas", "odor of natural gas", "gas odor",
                            "smell gas", "odors of gas", "orders of gas")),
    ("Smoke / odor", "fire", ("smoke condition", "odor of smoke", "smell of smoke", "odor investigation",
                              "smoke in the building", "smoke investigation")),
    ("Wires down", "hazard", ("wires down", "wire down", "transformer fire", "transformer", "pole fire",
                              "arcing wires", "electrical fire", "electrical odor")),
    ("MVA", "rescue", ("mva", "m v a", "motor vehicle accident", "motor vehicle collision", "car accident",
                       "auto accident", "vehicle accident", "rollover", "pin job", "entrapment")),
    ("Water condition", "service", ("water condition", "water leak", "flooding", "flooded basement",
                                    "water in the basement", "pumping", "water problem")),
    ("Elevator", "rescue", ("elevator", "stuck in an elevator", "elevator emergency")),
    ("Rescue", "rescue", ("rescue", "water rescue", "ice rescue", "confined space", "trench")),
    ("Hazmat", "hazard", ("hazmat", "haz mat", "hazardous materials", "fuel spill", "spill")),
    ("Lockout", "service", ("lockout", "lock out", "child locked", "locked in a vehicle")),
    ("EMS assist", "ems", ("ems", "e m s", "medical", "ambulance", "lift assist", "cardiac", "unconscious",
                           "difficulty breathing", "fall victim", "assist ems", "medical emergency")),
    ("Investigation", "service", ("investigation", "investigate", "check on", "unknown odor")),
    ("Mutual aid", "service", ("mutual aid", "standby", "stand by", "cover assignment", "fast team", "rit")),
]

DEPARTMENTS = {
    "Port Chester FD": ("port chester fire", "port chester fire department", "port chester department"),
    "Rye Brook FD": ("rye brook fire", "rye brook fire department", "rye brook"),
}

RESPOND = re.compile(r"\b(respond(?:ing)? to|for (?:an?|the)|report(?:ed|s)? of|reporting)\b", re.I)
TIME_OUT = re.compile(r"\btime\s*(?:out)?\s*(?:is\s*)?(\d{1,2})[:\s]?(\d{2})\b", re.I)
WORD_NUM = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9"}

# Apparatus as dispatched: "Engine 25", "Tower Ladder 2", "Rescue 40", chiefs "2391".
UNIT = re.compile(r"\b(tower ladder|engine|ladder|tower|rescue|squad|truck|battalion|car|chief|"
                  r"ambulance|medic|utility|tanker|brush)\s+(\d{1,4})\b")
# Medical / fire priority dispatch codes as spoken: "30 Alpha 5", "69 Delta 6", "10 C 2".
EMD = re.compile(r"\b(\d{1,2})\s+(alpha|bravo|charlie|delta|echo|omega)\s*(\d{1,2})?\b")
# MPDS chief complaints (protocols 1-33).
MPDS = {1: "Abdominal pain", 2: "Allergic reaction", 3: "Animal bite", 4: "Assault", 5: "Back pain",
        6: "Breathing problem", 7: "Burns", 8: "CO / inhalation", 9: "Cardiac arrest", 10: "Chest pain",
        11: "Choking", 12: "Seizure", 13: "Diabetic", 14: "Drowning", 15: "Electrocution",
        16: "Eye problem", 17: "Fall", 18: "Headache", 19: "Heart problem", 20: "Heat / cold exposure",
        21: "Hemorrhage", 22: "Entrapment", 23: "Overdose / poisoning", 24: "Pregnancy / childbirth",
        25: "Psychiatric", 26: "Sick person", 27: "Stab / gunshot", 28: "Stroke", 29: "MVA",
        30: "Traumatic injury", 31: "Unconscious / fainting", 32: "Unknown problem", 33: "Transfer"}
SUFFIX = (r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|place|pl|court|ct|boulevard|blvd|terrace|"
          r"parkway|pkwy|way|circle|highway|turnpike|plaza|square|path|crescent|close|walk|trail|run|"
          r"post road|ridge|hill|heights|extension|expressway|thruway)")
ADDRESS = re.compile(rf"\b(\d{{1,5}}[a-z]?)\s+((?:[a-z]+\s+){{0,4}}?{SUFFIX})\b")
STREET = re.compile(rf"\b((?:[a-z]+\s+){{1,4}}?{SUFFIX})\b")
STOP_WORDS = {"the", "a", "an", "and", "in", "at", "on", "of", "to", "for", "cross", "between", "corner",
              "respond", "responding", "report", "reported", "female", "male", "year", "old"}


@dataclass
class Dispatch:
    text: str
    departments: list[str] = field(default_factory=list)
    incident_type: str = "Unknown"
    category: str = "unknown"      # fire | ems | rescue | hazard | service | unknown
    type_phrase: str = ""          # the words that set the type
    code: str = ""                 # priority dispatch code as heard, "30-A-5"
    units: list[str] = field(default_factory=list)   # "Engine 25"
    numbers: list[int] = field(default_factory=list)  # bare numbers in the unit list ("174, 175, Ladder 1")
    location: str = ""             # the address / place part, for the geocoder
    cross_street: str = ""
    time_out: str = ""             # "14:32"

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _norm(text: str) -> str:
    t = text.lower().replace("-", " ")
    t = re.sub(r"\bc\.\s?o\.", " co ", t)                  # "C.O. detector": AWS's spelling of the acronym
    t = re.sub(r"[^a-z0-9:.,; ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _spoken_digits(text: str) -> str:
    """'time out one four three two' -> 'time out 1432'."""
    def join(m: re.Match) -> str:
        return "".join(WORD_NUM[w] for w in m.group(0).split())
    return re.sub(r"\b(?:(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\s?){2,}\b",
                  lambda m: join(m) + " ", text, flags=re.I).strip()


# A house number the dispatcher said twice that speech-to-text ran together ("412, 412 Sample Road" heard as "412412
# Sample Road"): a number of six digits or more whose two halves are the same, three digits a half at least, before a
# word. Never a house number here (ADDRESS takes five digits at most: the address was lost, and the page matched on its
# street alone), so it is always the repeat. A four-digit doubling ("1717") may be a real number: it stays as heard and
# matching halves it (sizeup/cad/sources.py halved). "412 412" (two words) is parse()'s own repeat rule.
DOUBLED = re.compile(r"\b([1-9]\d{2,4})\1\b(?=[\s,.;:]+[A-Za-z])")


def undouble(text: str) -> str:
    """'412412 Sample Road' -> '412 Sample Road' (DOUBLED); anything else as it is. For a page's text only: parse()
    and the page locator (sizeup/geo/pagegeo.py locate_page) read it so."""
    return DOUBLED.sub(r"\1", text or "")


def _clean_street(s: str) -> str:
    words = s.split()
    while words and words[0] in STOP_WORDS:
        words.pop(0)
    return " ".join(words)


# The stop words that end whatever came before a road's name (#365): "co alarm between sample avenue" is Sample Avenue.
# "old" is often a road's own first word (and "year" / "male" / "female" never come before one)
ROAD_CUT = STOP_WORDS - {"old", "year", "female", "male"}


def _road(s: str, type_phrase: str = "") -> str:
    """A road as the pattern caught it, without the words before it that aren't its name: everything up to the last
    stop word, or to the end of this page's own type phrase where it stands whole ("co alarm example terrace"). Never
    a word of a road's own name that only looks like a call type ("Gas House Lane", "Smoke Rise Road"). '' when only
    the suffix is left."""
    words = s.split()
    cut = max((i for i, w in enumerate(words[:-1]) if w in ROAD_CUT), default=-1)
    tp = type_phrase.split()
    if tp:
        for i in range(len(words) - len(tp)):
            if words[i:i + len(tp)] == tp:
                cut = max(cut, i + len(tp) - 1)
    rest = words[cut + 1:]
    return " ".join(rest) if len(rest) >= 2 else ""


# A highway location by its mile marker: a feed's "I-999 SB MM 5.7" (or a route, a direction and a decimal marker:
# "I999 SB 5.7"), a page's "I 999 southbound mile marker 5.7" (speech-to-text: "mild marker", "mile mark").
# highway() -> MilePlace; the marker is the place (same_marker): one route, the same marker to the tenth, the same
# direction when both say one. A town (mutual aid on the same road in another town is the same marker), an exit or a
# name around it never matter, and a number after "exit" is never a house number (EXIT_NUMBER). Route names: the
# place search's own (sizeup/geo/places.py ROUTE_ALIASES: "Hutchinson River Parkway" and "Hutch" are one road), else
# "I-N", "US-N", "NY-N", "CR N", "Route N" (a bare "Route N" is never taken for US-N or NY-N: two routes, never joined),
# or a named road's own words ("Sample Pkwy"; the words of a call type before it are not its name).
NUMBERED = re.compile(r"\b(interstate|i|u\s?s(?:\s+(?:route|rte|rt|highway|hwy))?|nys?(?:\s+(?:state\s+)?(?:route|rte|rt))?"
                      r"|state\s+(?:route|rte|rt|highway|hwy)|sr|county\s+(?:route|rte|road|rd)|cr|route|rte|rt"
                      r"|highway|hwy)\s*-?\s*(\d{1,3}[a-z]?)\b(?!\.\d|\s*(?:st|nd|rd|th)\b)", re.I)
NAMED = re.compile(r"\b(parkway|pkwy|expressway|expy|thruway|twy|turnpike|tpke|highway|hwy)\b", re.I)
ROUTE_SUFFIX = {"parkway": "Pkwy", "pkwy": "Pkwy", "expressway": "Expy", "expy": "Expy", "thruway": "Thruway",
                "twy": "Thruway", "turnpike": "Tpke", "tpke": "Tpke", "highway": "Hwy", "hwy": "Hwy"}
BOUND = {"eastbound": "EB", "westbound": "WB", "northbound": "NB", "southbound": "SB", "eb": "EB", "wb": "WB",
         "nb": "NB", "sb": "SB"}
_BOUND = r"(?:eastbound|westbound|northbound|southbound|eb|wb|nb|sb)"
_MILE = r"(\d{1,3}(?:\.\d{1,2})?)(?!\d|\.\d)"
MARKER = re.compile(rf"\b(?:mm|mp|milepost|mile\s*post|marker|mark)\s*(?:number\s*)?{_MILE}|\bmile\s+(\d{{1,3}}\.\d{{1,2}})\b"
                    rf"|\b{_BOUND}\s+(\d{{1,3}}\.\d{{1,2}})\b", re.I)
ROUTE_STOP = {"the", "a", "an", "on", "at", "of", "to", "in", "and", "for", "from", "near", "by", "mile", "marker", "exit",
              "with", "is", "into", "onto", "off", "past", "before", "after", "approaching", *BOUND}
MARKER_REACH = 60          # the marker this far (characters) after the route's name at most
BOUND_BEFORE = 16          # a direction this far before the route's name ("southbound on I-999") counts too
EXIT_NUMBER = re.compile(r"\b(?:exit|exits|interchange)\s+\d{1,3}[a-z]?\b", re.I)


@dataclass(frozen=True)
class MilePlace:
    route: str             # "I-999", "Route 9", "Sample Pkwy"
    dir: str               # "EB" ... or ""
    mile: float
    start: int = 0         # where the route's name starts in the text given (the same text, its indices)

    def shown(self) -> str:
        """'I-999 SB MM 5.7'."""
        return " ".join(x for x in (self.route, self.dir, f"MM {self.mile:g}") if x)


_ALIASES: dict[tuple, str] | None = None


def _alias(text: str) -> str:
    """The place search's name for a road (sizeup/geo/places.py ROUTE_ALIASES), '' when it has none."""
    global _ALIASES
    from sizeup.geo.places import _ALIAS_LIST, norm_tokens   # (lazily: the place search's module is heavier)
    if _ALIASES is None:
        _ALIASES = {a: r for a, r in _ALIAS_LIST if not (len(a) == 1 and a[0][0].isdigit())}   # never a bare number
    return _ALIASES.get(tuple(norm_tokens(text)), "")


_TYPE_WORDS: set[str] = set()


def _type_words() -> set[str]:
    """Every word of a call type's phrases ("car", "fire", "accident"): never a road's name."""
    if not _TYPE_WORDS:
        _TYPE_WORDS.update(w for _, _, phrases in INCIDENT_TYPES for p in phrases for w in p.split())
    return _TYPE_WORDS


def _numbered(m) -> str:
    pre, n = " ".join(m.group(1).lower().split()), m.group(2).upper()
    head = pre.split()[0]
    if head in ("i", "interstate"):
        route = f"I-{n}"
    elif head in ("us", "u"):
        route = f"US-{n}"
    elif head in ("ny", "nys", "state", "sr"):
        route = f"NY-{n}"
    elif head in ("county", "cr"):
        route = f"CR {n}"
    else:
        return _alias(f"rte {n}") or f"Route {n}"
    return _alias(route) or route


def _named(t: str, m) -> tuple[str, int] | None:
    """A road named by words before its suffix ("the Sample Parkway"): (its name, where it starts), None without a
    name (the place search's own name for a lone word: "the Thruway")."""
    words = list(re.finditer(r"[a-z]+", t[max(0, m.start() - 48):m.start()], re.I))
    off = max(0, m.start() - 48)
    keep: list = []
    stop = _type_words() | ROUTE_STOP
    for w in reversed(words):
        gap = t[off + w.end():(off + keep[0].start()) if keep else m.start()]
        if gap.strip() or len(keep) == 3 or w.group(0).lower() in stop:
            break
        keep.insert(0, w)
    suffix = m.group(1).lower()
    for i in range(len(keep) + 1):                       # the place search's name, the longest first
        got = _alias(" ".join([*(w.group(0) for w in keep[i:]), suffix]))
        if got:
            return got, off + keep[i].start() if i < len(keep) else m.start()
    if not keep:
        return None
    return " ".join(w.group(0).capitalize() for w in keep) + " " + ROUTE_SUFFIX[suffix], off + keep[0].start()


def highway(text: str) -> MilePlace | None:
    """The first highway with a mile marker the text names ("I-999 SB MM 5.7", "i 999 southbound mile marker 5.7",
    "I999 SB 5.7", "Sample Pkwy NB MM 3.1"); None without a route or a marker after it, and when another route is
    named between a route and the marker ("the Sample Parkway at the Model Parkway interchange, mile marker 0": whose
    marker?). Its start is an index into text as given."""
    t = (text or "").replace("_", " ").replace("@", " ")    # (the same length: indices hold)
    found: list[tuple[int, int, str]] = []
    for m in NUMBERED.finditer(t):
        found.append((m.start(), m.end(), _numbered(m)))
    for m in NAMED.finditer(t):
        if any(s <= m.start() < e for s, e, _ in found) or re.match(r"\s*-?\s*\d", t[m.end():]):
            continue                                     # "US Highway 1", "Highway 9": numbered
        got = _named(t, m)
        if got is not None:
            found.append((got[1], m.end(), got[0]))
    found.sort()
    pending = False                  # a route named just before, with no marker of its own before this one
    for k, (start, end, route) in enumerate(found):
        nxt = found[k + 1][0] if k + 1 < len(found) else len(t)
        reach = min(end + MARKER_REACH, nxt)
        mk = MARKER.search(t, end, reach)
        if mk is None:
            pending = nxt < end + MARKER_REACH
            continue
        if pending:
            return None
        said = next(g for g in mk.groups() if g)
        b = (re.search(rf"\b{_BOUND}\b", t[end:mk.end()], re.I)
             or re.search(rf"\b{_BOUND}\b(?=\W*(?:on\s+|the\s+)*$)", t[max(0, start - BOUND_BEFORE):start], re.I))
        return MilePlace(route, BOUND[b.group(0).lower()] if b else "", float(said), start)
    return None


def same_marker(a: MilePlace | None, b: MilePlace | None) -> bool:
    """One place on a highway: the same route, the same marker to the tenth, the same direction when both say one."""
    return (a is not None and b is not None and a.route == b.route and abs(a.mile - b.mile) < 0.05
            and (not a.dir or not b.dir or a.dir == b.dir))


def near_marker(a: MilePlace | None, b: MilePlace | None, miles: float = 1.0) -> bool:
    """The same route and direction (when both say one), the markers at most this far apart: maybe the same call."""
    return (a is not None and b is not None and a.route == b.route and abs(a.mile - b.mile) <= miles
            and (not a.dir or not b.dir or a.dir == b.dir))


# The clause parse() falls back on when no street was heard: up to its first sentence break or unit, at most
# PLACE_CLAUSE_WORDS words, and a place only when something says so: "at" / "on" / "in" before it, "respond to", or a
# word for a place (PLACE_WORDS). Anything else is the page's own words ("smoke showing", "caller reports ..."): never an
# address. The event stays unlocated (its page's text is in its remarks).
PLACE_CLAUSE_WORDS = 10
PLACE_WORDS = {"school", "mall", "center", "centre", "building", "plaza", "park", "station", "church", "temple",
               "synagogue", "mosque", "hospital", "home", "house", "apartments", "apartment", "complex", "hotel",
               "motel", "restaurant", "store", "market", "supermarket", "bank", "library", "garage", "lot", "field",
               "club", "office", "offices", "airport", "terminal", "bridge", "pier", "marina", "beach", "hall", "academy",
               "college", "university", "factory", "warehouse", "diner", "deli", "pharmacy", "gym", "pool", "rink",
               "stadium", "theater", "theatre", "condos", "condominiums", "towers", "village", "court", "courts",
               "intersection", "corner", "playground", "cemetery", "firehouse", "headquarters", "nursing", "residence"}


def place_clause(rest: str, located: bool = False) -> str:
    """The place the clause after a page's type names, '' when it names none. located: said after "respond to"."""
    stop = len(rest)
    for pat in (r"[.;!?](?:\s|$)", r"\bcross", r"\bbetween\b", r"\btime\s*out\b", r"\btime \d"):
        mm = re.search(pat, rest)
        if mm:
            stop = min(stop, mm.start())
    u = UNIT.search(rest)
    if u is not None:
        stop = min(stop, u.start())
    loc = rest[:stop].strip(" .,;:")
    prep = re.match(r"(?:at|on|in|of|for|to)\s+", loc)
    if prep:
        loc = loc[prep.end():]
    loc = re.sub(r"\b(?:port chester|rye brook)(?: fire(?: department)?)?\s*$", "", loc).strip(" .,;:")
    words = loc.split()
    said = located or (prep is not None and prep.group(0).split()[0] in ("at", "on", "in")) \
        or any(w.strip(",") in PLACE_WORDS for w in words)
    return loc[:120] if words and len(words) <= PLACE_CLAUSE_WORDS and said else ""


# "Engine 59/63", "Engines 59 / 63 / 65": one unit word for each number (read before "/" is normalised away).
UNIT_WORDS = ("tower ladder", "engine", "ladder", "tower", "rescue", "squad", "truck", "ambulance", "medic", "tanker")
UNIT_SLASH = re.compile(rf"\b({'|'.join(UNIT_WORDS)})s?\s+(\d{{1,4}}(?:\s*/\s*\d{{1,4}})+)\b", re.I)
STREET_WORDS = {"street", "st", "avenue", "ave", "av", "road", "rd", "place", "pl", "lane", "ln", "drive", "dr", "court",
                "ct", "boulevard", "blvd", "terrace", "ter", "parkway", "pkwy", "highway", "hwy", "way", "circle", "cir",
                "plaza", "turnpike", "tpke", "route", "rt"}


def _unit_norm(u: str) -> str:
    """'Engine 61' / 'E-61' -> 'E61' (as sizeup/cad/sources.py norm_unit)."""
    parts = re.findall(r"[A-Za-z]+|\d+", u or "")
    return "".join(p[0].upper() if len(p) > 2 else p.upper() for p in parts if p.isalpha()) + \
        "".join(p for p in parts if p.isdigit())


def _unit_slash(text: str, known=None) -> str:
    """"Engine 59/63" -> "Engine 59, Engine 63", only when every number after the first is a configured unit of that
    kind (known: the roster, as written anywhere) and no street word follows ("Engine 61 / 25 Example Street": a unit,
    then the house number). No roster: left as it is."""
    have = {_unit_norm(k) for k in known or () if isinstance(k, str)}

    def one(m: re.Match) -> str:
        word, nums = m.group(1), re.split(r"\s*/\s*", m.group(2))
        after = re.findall(r"[a-z]+", re.split(r"[,.;:]", text[m.end():m.end() + 60])[0].lower())[:3]
        if not have or any(w in STREET_WORDS for w in after) or \
                not all(_unit_norm(f"{word} {n}") in have for n in nums[1:]):
            return m.group(0)
        return ", ".join(f"{word} {n}" for n in nums)
    return UNIT_SLASH.sub(one, text)


def split_joined(units, known) -> list[str]:
    """Run-together apparatus numbers ("Engine 6158": Engine 61 and Engine 58 read as one) split in two, only when the
    whole is not a known unit, the word is an apparatus word (UNIT_WORDS, or E / L / TL / R: never "EMS 6158") and
    exactly one split gives two known units of the same kind (two digits a side at least). known: the configured apparatus, as written anywhere ("E61", "Engine 61"): compared as norm(). Order kept,
    no repeats."""
    norm = _unit_norm
    have = {norm(k) for k in known or () if isinstance(k, str)}
    out: list[str] = []
    for u in units or ():
        m = re.fullmatch(r"\s*(.*?\D)\s*(\d{4,8})\s*", str(u))
        parts = [u]
        if m and norm(u) not in have and m.group(1).strip().lower() in (*UNIT_WORDS, "e", "l", "tl", "r"):
            word, n = m.group(1).strip(), m.group(2)
            splits = [(n[:i], n[i:]) for i in range(2, len(n) - 1) if n[i] != "0"
                      and norm(f"{word} {n[:i]}") in have and norm(f"{word} {n[i:]}") in have]
            if len(splits) == 1:
                parts = [f"{word} {x}" if len(word) > 2 else f"{word}{x}" for x in splits[0]]
        for x in parts:
            if x not in out:
                out.append(x)
    return out


def parse(text: str, known=None) -> Dispatch:
    """known: the configured apparatus (the station's roster), for "Engine 59/63" (_unit_slash)."""
    d = Dispatch(text)
    # "16-10-191-22": 60 Control's run number, not engines (the normalising below turns "-" into spaces).
    t = _spoken_digits(_norm(_unit_slash(re.sub(r"\b\d{1,3}(?:\s*-\s*\d{1,3}){3,}(?=\s*[,.])", " , ", text), known)))
    t = re.sub(r"\b(\d+)(?:\s+\1\b)+", r"\1", t)          # "42 42 stratton road": a repeat, not two numbers
    t = undouble(t)                                       # "412412 sample road": the repeat run together
    for name, phrases in DEPARTMENTS.items():
        if any(p in t for p in phrases):
            d.departments.append(name)
    for m in UNIT.finditer(t):
        u = f"{m.group(1).title()} {m.group(2)}"
        if u not in d.units:
            d.units.append(u)
    e = EMD.search(t)
    if e:
        proto = int(e.group(1))
        d.code = f"{proto}-{e.group(2)[0].upper()}" + (f"-{e.group(3)}" if e.group(3) else "")
        if proto in MPDS:
            d.incident_type, d.category, d.type_phrase = MPDS[proto], "ems", e.group(0)
        elif 50 <= proto <= 85:  # fire priority dispatch protocols: shown by code until mapped
            d.incident_type, d.category, d.type_phrase = f"Fire call {d.code}", "fire", e.group(0)
    # "carbon dioxide": STT's reading of "carbon monoxide" (#365), unless a CO2 system is meant
    tt = t if re.search(r"\b(?:system|discharge|suppression)\b", t) else re.sub(r"\bcarbon dioxide\b", "carbon monoxide", t)
    if d.incident_type == "Unknown" or d.category == "fire" and d.incident_type.startswith("Fire call"):
        for label, cat, phrases in INCIDENT_TYPES:
            hit = next((p for p in phrases if re.search(rf"\b{re.escape(p)}\b", tt)), None)
            if hit:
                d.incident_type, d.category, d.type_phrase = label, cat, hit
                break
    m = TIME_OUT.search(t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh < 24 and mm < 60:
            d.time_out = f"{hh:02d}:{mm:02d}"
    if not d.time_out:
        # 60 Control ends a page with the time out run into other digits: "... ambulate. 153965."
        # (15:39). Taken only when it is a plausible HHMM.
        tail = re.search(r"\b(\d{4})\d{0,3}\D*$", t)
        if tail:
            hh, mm = int(tail.group(1)[:2]), int(tail.group(1)[2:])
            if hh < 24 and mm < 60:
                d.time_out = f"{hh:02d}:{mm:02d}"
    tw = d.type_phrase
    # Address: the first "number + street" that isn't a unit number, code or time.
    masked = UNIT.sub(" , ", t)
    masked = EMD.sub(" , ", masked)
    masked = TIME_OUT.sub(" , ", masked)
    masked = EXIT_NUMBER.sub(" , ", masked)               # "exit 4 to Sample Avenue": never 4 Sample Avenue
    a = ADDRESS.search(masked)
    hw = highway(masked)                                  # a highway's mile marker: the place, unless an address
    if hw is not None and a is not None and a.start() < hw.start:   # came first ("12 Example Ave, near I-999 MM 4")
        hw = None
    if hw is not None:
        a = None
    # Bare numbers before the address are often engines read without the word ("174, 175, Ladder 1");
    # the app checks them against the apparatus roster.
    head = masked[: hw.start] if hw is not None else masked[: a.start()] if a else ""
    for n in re.findall(r"\b(\d{1,3})\b", head):
        if int(n) not in d.numbers:
            d.numbers.append(int(n))
    if hw is not None:                                    # a highway's mile marker: the place, whatever else is said
        d.location = hw.shown()
    elif a:
        d.location = f"{a.group(1)} {_clean_street(a.group(2))}".strip()
        after = masked[a.end():]
        crosses = []
        again = re.compile(rf"\b{re.escape(a.group(1))}\s+$")
        for sm in STREET.finditer(after[:160]):
            if again.search(after[:sm.start()]):
                continue                                   # the address read again, however misheard: never a cross
            name = _road(sm.group(1), tw)
            if name and name not in crosses and name != _clean_street(a.group(2)):
                crosses.append(name)
            if len(crosses) == 2:
                break
        d.cross_street = " and ".join(crosses)
    elif (roads := [r for r in dict.fromkeys(_road(m.group(1), tw) for m in STREET.finditer(masked)) if r]):
        # No house number (highways, intersections): the first named road, the next one as the cross.
        # "Sprain Brook Parkway southbound at the Bronx River Parkway interchange"
        d.location = roads[0]
        d.cross_street = " and ".join(roads[1:3])          # "between Sample Avenue and Specimen Street": both
    else:
        # No street address heard: the clause after the type / "respond to" (place names, intersections).
        start, located = 0, False
        if d.type_phrase:
            start = t.find(d.type_phrase) + len(d.type_phrase)
        else:
            r = RESPOND.search(t)
            if r:
                start, located = r.end(), r.group(1).startswith("respond")
        d.location = place_clause(t[start:], located)
        c = re.search(r"\b(?:cross(?:\s+street)?s?(?:\s+of|\s+is)?|between)\s+(.+?)(?:[.,;]|\s+(?:time|port chester|rye brook)\b|$)", t)
        if c:
            d.cross_street = c.group(1).strip(" .,")
    return d
