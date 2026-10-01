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


def _clean_street(s: str) -> str:
    words = s.split()
    while words and words[0] in STOP_WORDS:
        words.pop(0)
    return " ".join(words)


def parse(text: str) -> Dispatch:
    d = Dispatch(text)
    t = _spoken_digits(_norm(text))
    t = re.sub(r"\b(\d+)(?:\s+\1\b)+", r"\1", t)          # "42 42 stratton road": a repeat, not two numbers
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
    if d.incident_type == "Unknown" or d.category == "fire" and d.incident_type.startswith("Fire call"):
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
    # Address: the first "number + street" that isn't a unit number, code or time.
    masked = UNIT.sub(" , ", t)
    masked = EMD.sub(" , ", masked)
    masked = TIME_OUT.sub(" , ", masked)
    a = ADDRESS.search(masked)
    if a:
        d.location = f"{a.group(1)} {_clean_street(a.group(2))}".strip()
        after = masked[a.end():]
        crosses = []
        for sm in STREET.finditer(after[:160]):
            name = _clean_street(sm.group(1))
            if name and name not in crosses and name != _clean_street(a.group(2)):
                crosses.append(name)
            if len(crosses) == 2:
                break
        d.cross_street = " and ".join(crosses)
    else:
        # No street address heard: the clause after the type / "respond to" (place names, intersections).
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
        d.location = loc[:120]
        c = re.search(r"\b(?:cross(?:\s+street)?s?(?:\s+of|\s+is)?|between)\s+(.+?)(?:[.,;]|\s+(?:time|port chester|rye brook)\b|$)", t)
        if c:
            d.cross_street = c.group(1).strip(" .,")
    return d
