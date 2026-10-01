"""Parse a transcribed dispatch page into structured fields.

    "Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester
     Avenue, cross of Pearl Street, Port Chester. Time out 1432."
  -> departments ["Port Chester FD"], type "Fire alarm" (category fire), location text
     "209 West Chester Avenue", cross street "Pearl Street", time out "14:32"

The incident vocabulary is a first guess at 60 Control phrasing; it is meant to be tuned on
real captured pages (docs/PLAN.md). Unknown types are kept as "Unknown" with the raw text:
the display never invents a type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (display type, category, phrases). First match wins, so specific before generic.
INCIDENT_TYPES: list[tuple[str, str, tuple[str, ...]]] = [
    ("Working fire", "fire", ("10 75", "ten seventy five", "1075", "working fire", "working structure fire")),
    ("Structure fire", "fire", ("structure fire", "house fire", "building fire", "fire in a building",
                                "apartment fire", "commercial fire", "reported structure", "fire in the building")),
    ("Car fire", "fire", ("car fire", "vehicle fire", "auto fire", "truck fire")),
    ("Brush fire", "fire", ("brush fire", "grass fire", "mulch fire", "outside fire", "dumpster fire",
                            "rubbish fire", "garbage fire")),
    ("Fire alarm", "fire", ("activated fire alarm", "fire alarm", "automatic alarm", "alarm activation",
                            "smoke detector", "smoke alarm", "pull station", "water flow alarm", "waterflow")),
    ("CO alarm", "hazard", ("carbon monoxide", "co alarm", "c o alarm", "co detector", "c o detector")),
    ("Gas leak", "hazard", ("gas leak", "odor of gas", "smell of gas", "natural gas", "gas condition",
                            "propane")),
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
CROSS = re.compile(r"\b(?:cross(?:\s+street)?s?(?:\s+of|\s+is|:)?|cross\s+of|between)\s+(.+?)(?:[.,;]|\s+(?:and|time|port chester|rye brook|town of|village of)\b|$)", re.I)
TIME_OUT = re.compile(r"\btime\s*(?:out)?\s*(?:is\s*)?(\d{1,2})[:\s]?(\d{2})\b", re.I)
WORD_NUM = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9"}


@dataclass
class Dispatch:
    text: str
    departments: list[str] = field(default_factory=list)
    incident_type: str = "Unknown"
    category: str = "unknown"      # fire | ems | rescue | hazard | service | unknown
    type_phrase: str = ""          # the words that set the type
    location: str = ""             # the address / place part, for the geocoder
    cross_street: str = ""
    time_out: str = ""             # "14:32"

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _norm(text: str) -> str:
    t = text.lower().replace("-", " ")
    t = re.sub(r"[^a-z0-9:.,; ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _spoken_digits(text: str) -> str:
    """'time out one four three two' -> 'time out 1432'."""
    def join(m: re.Match) -> str:
        return "".join(WORD_NUM[w] for w in m.group(0).split())
    return re.sub(r"\b(?:(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\s?){2,}\b",
                  lambda m: join(m) + " ", text, flags=re.I).strip()


def parse(text: str) -> Dispatch:
    d = Dispatch(text)
    t = _spoken_digits(_norm(text))
    for name, phrases in DEPARTMENTS.items():
        if any(p in t for p in phrases):
            d.departments.append(name)
    for label, cat, phrases in INCIDENT_TYPES:
        hit = next((p for p in phrases if re.search(rf"\b{re.escape(p)}\b", t)), None)
        if hit:
            d.incident_type, d.category, d.type_phrase = label, cat, hit
            break
    m = TIME_OUT.search(t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh < 24 and mm < 60:
            d.time_out = f"{hh:02d}:{mm:02d}"
    c = CROSS.search(t)
    if c:
        d.cross_street = c.group(1).strip(" .,")
    # Location: the clause after the incident type (or after "respond to"), up to the cross street /
    # time out. "respond to an activated fire alarm. 209 west chester avenue, cross of ..."
    start = 0
    if d.type_phrase:
        start = t.find(d.type_phrase) + len(d.type_phrase)
    else:
        r = RESPOND.search(t)
        if r:
            start = r.end()
    rest = t[start:]
    stop = len(rest)
    for pat in (r"\bcross", r"\bbetween\b", r"\btime\s*out\b", r"\btime \d"):
        mm = re.search(pat, rest)
        if mm:
            stop = min(stop, mm.start())
    loc = rest[:stop].strip(" .,;")
    loc = re.sub(r"^(?:at|on|in|of|for|to)\s+", "", loc)
    loc = re.sub(r"\b(?:port chester|rye brook)(?: fire(?: department)?)?\s*$", "", loc).strip(" .,;")
    d.location = loc
    return d
