"""Amazon Transcribe's custom vocabulary for county-wide fire radio, generated from the repo's public data.

60 Control and the Fire/EMS channels carry every department in the county, not only Port Chester's. The seed list
(channels.vocabulary_suggestions) had PCFD's and Rye Brook's units and streets; this adds, from public files only:

    callsigns     every numbered rig in the county's mutual aid plan (data/westchester_apparatus.csv: Engine 164,
                  Ladder 50, Tower Ladder 24, Rescue 85, Tanker 9, Marine 24, Squad 8 ...), RESERVED numbers and
                  antiques left out, and the chief cars as the plan numbers departments (dept number + rank digit:
                  230 + 1 -> "2301", said "twenty-three oh one"; data/westchester_departments.csv)
    places        the departments and districts (Mamaroneck, Larchmont, New Rochelle, Ardsley ...), the EMS agencies
                  (data/westchester_ems.csv: "Port Chester EMS", "EVAC" ...) and the hamlets cad_lists names
                  (Byram, Cos Cob ...)
    channels      Fire/EMS 10-19 (the battalions' channels), Battalion 10-19, Fire 1-8 (ground ops)
    phrasing      the statuses as units say them ("all units back in service", "to 60 Control")
    streets       the busiest streets of the whole gazetteer, as said ("Boston Post Road"), after the home towns'

county_phrases() gives the phrases as written ("2301", "Engine 164"): Settings shows them, and the push sends them
through transcribe.build_vocabulary (the list form: digits spelled out, "twenty-three-oh-one", and respelled
back to "2301" in the text). table() writes the same phrases in AWS's table form (Phrase, IPA, SoundsLike,
DisplayAs) for a vocabulary file in S3: DisplayAs does the respelling on AWS's side and SoundsLike gives the place
names Transcribe may mishear (Mamaroneck, Greenwich ...). The list form is what Settings pushes; the table form needs
a file in S3 that Transcribe can read (docs/stt-vocabulary.md has the measurements of both).
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# Apparatus types as the plan writes them -> said on the air (the plan's abbreviations are said as letters).
TYPES = {"Engine": "Engine", "Ladder": "Ladder", "Tower Ladder": "Tower Ladder", "Rescue": "Rescue", "Marine": "Marine",
         "Mini Attack": "Mini Attack", "Tanker": "Tanker", "Squad": "Squad", "Utility": "Utility", "Car": "Car",
         "Hazmat": "Hazmat", "Airport": "Airport", "SSU": "S.S.U.", "MSU": "M.S.U.", "MDU": "M.D.U."}
CHIEF_RANKS = 5                  # chief cars per department numbered here (dept + 1 .. 5); cad_lists has the home ones
BATTALIONS = range(10, 20)       # the county's fire battalions; Fire/EMS 10 .. 19 are their channels
GROUND_OPS = range(1, 9)         # ground ops channels Fire 1 .. Fire 8 (westchester_departments.csv notes: F1 .. F8)
TOP_STREETS = 40                 # the busiest streets of the whole gazetteer, after the home towns' own
# The battalions whose callsigns Settings suggests: the Sound Shore's (15: Port Chester, Rye, Harrison, Mamaroneck,
# Larchmont; 18: New Rochelle, Mount Vernon, Yonkers ...; 19: White Plains, Purchase, West Harrison, Scarsdale ...).
# Measured on real county traffic (docs/stt-vocabulary.md): these (~600 phrases) did at least as well as the whole
# county's (~1100), so the larger list isn't worth its size. Every department's name, the channels and the
# phrasing are in either way.
SUGGEST_BATTALIONS = (15, 18, 19)
# Said on the air with a unit or a department: "<unit> to 60 Control", "all units back in service".
PHRASING = ("to 60 Control", "to 60", "all units back in service", "all units in service", "units back in service",
            "is in service", "we're back in service", "show us responding", "show me on location", "on location",
            "responding to", "mutual aid", "Fire Department", "Fire Chief", "Alpha", "Medic", "Battalion")
# How a department's name is said when the plan's spelling isn't it.
SAID = {"Mamaroneck Town": "Town of Mamaroneck", "Mamaroneck Village": "Village of Mamaroneck", "Hastings": "Hastings",
        "Montrose VA": "Montrose V.A.", "Round Hill (Greenwich CT)": "Round Hill",
        "Westchester County DES": "Westchester County D.E.S.", "Bedford Hills Correctional Facility": "Bedford Hills"}
EXTRA_PLACES = ("Mamaroneck", "Greenwich", "Tuckahoe", "New Castle", "Mount Pleasant", "Greenburgh", "Cortlandt",
                "Lewisboro", "North Salem", "Stamford", "Hastings on Hudson", "Croton", "Briarcliff")

# AWS table form: how a place is said, for the names Transcribe gets wrong on its own (syllables, hyphenated).
SOUNDS_LIKE = {"Mamaroneck": "mam-uh-ron-eck", "Larchmont": "larch-mont", "Greenwich": "gren-itch",
               "Chappaqua": "chap-uh-kwah", "Pocantico": "poh-kan-tih-ko", "Tuckahoe": "tuck-uh-hoe",
               "Ossining": "oss-in-ing", "Katonah": "kuh-toe-nuh", "Mohegan": "mo-hee-gun", "Verplanck": "ver-plank",
               "Byram": "by-rum", "Ardsley": "ards-lee", "Valhalla": "val-hal-uh", "Armonk": "ar-monk",
               "Scarsdale": "scars-dale", "Pelham": "pel-um", "Eastchester": "east-chess-ter",
               "Elmsford": "elms-ford", "Banksville": "banks-vill", "Archville": "arch-vill", "Yonkers": "yon-kers"}

_CALLSIGN = re.compile(r"(?P<type>[A-Za-z][A-Za-z ]*?) (?P<num>\d{1,3})")


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = [ln for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return list(csv.DictReader(lines))


def _in(r: dict, battalions) -> bool:
    return battalions is None or (r.get("battalion") or "").strip() in {str(b) for b in battalions}


def apparatus(data: Path = DATA, battalions=None) -> list[str]:
    """The county's numbered rigs as said ("Engine 164", "Tower Ladder 24", "S.S.U. 15"), in the plan's order: no
    RESERVED number (no rig), no antique, nothing the plan doesn't number. battalions: only theirs (None: all)."""
    out = []
    for r in _rows(data / "westchester_apparatus.csv"):
        notes = (r.get("notes") or "").lower()
        if "reserved" in notes or "antique" in notes or not _in(r, battalions):
            continue
        m = _CALLSIGN.fullmatch(" ".join((r.get("unit") or "").split()))
        if not m:
            continue
        typ = TYPES.get(m["type"], m["type"])
        out.append(f"{typ} {int(m['num'])}")
    return list(dict.fromkeys(out))


def departments(data: Path = DATA) -> list[dict]:
    return [r for r in _rows(data / "westchester_departments.csv") if r.get("department")]


def chief_cars(data: Path = DATA, have: set[str] = frozenset(), ranks: int = CHIEF_RANKS, battalions=None) -> list[str]:
    """Chief cars by the county's numbering (dept number + rank digit: Rye 242 -> 2421 .. 2425), for every department
    that has a number; a department with any car in `have` (cad_lists' own: 2391 .., 2421 ..) keeps only those."""
    out = []
    for r in departments(data):
        d = (r.get("dept_number") or "").strip()
        if not re.fullmatch(r"\d{3}", d) or any(h.startswith(d) for h in have) or not _in(r, battalions):
            continue
        out += [f"{d}{k}" for k in range(1, ranks + 1)]
    return out


def places(data: Path = DATA, town_aliases=()) -> list[str]:
    """Departments, EMS agencies and hamlets (cad_lists' geo_town_aliases: Byram, Cos Cob ...) as said."""
    out = []
    for r in departments(data):
        out.append(SAID.get(r["department"], r["department"]))
    out += EXTRA_PLACES
    for r in _rows(data / "westchester_ems.csv"):
        for a in (r.get("aliases") or "").split("|"):
            a = a.strip()
            if a and not re.search(r"[()/]", a) and not (a.isupper() and len(a) > 4):   # PCVAC, LMVAC: said otherwise
                out.append(a)
    for alias in town_aliases:
        out.append(" ".join(w.capitalize() for w in str(alias).split()))
    return list(dict.fromkeys(p.replace("-", " ") for p in out))


def channels() -> list[str]:
    return ([f"Fire EMS {b}" for b in BATTALIONS] + [f"Battalion {b}" for b in BATTALIONS]
            + [f"Fire {n}" for n in GROUND_OPS])


def county_streets(gazetteer: Path | None = None, top: int = TOP_STREETS) -> list[str]:
    """The gazetteer's busiest streets (most addresses), every town in it."""
    p = gazetteer or (DATA / "gazetteer.sqlite")
    if not p.is_file() or top <= 0:
        return []
    c = sqlite3.connect(f"file:{p}?immutable=1", uri=True)
    try:
        rows = c.execute("select s.name from addresses a join streets s on s.id = a.street_id group by s.id "
                         "order by count(*) desc, s.name limit ?", (top * 2,)).fetchall()
    finally:
        c.close()
    out = [re.sub(r"\(.*?\)", "", n).strip() for (n,) in rows]
    return list(dict.fromkeys(n for n in out if n))[:top]


def county_phrases(units: dict | None = None, town_aliases=(), data: Path = DATA, gazetteer: Path | None = None,
                   top_streets: int = TOP_STREETS, battalions=None) -> list[str]:
    """The county-wide additions, as written, in priority order (the push keeps the first transcribe.MAX_PHRASES):
    phrasing, channels, places, chief cars, apparatus, streets. units: cad_lists' (the home departments' chief cars
    come from there, not the numbering rule); town_aliases: cad_lists' geo_town_aliases. battalions: the callsigns
    of those battalions only (None: the whole county; Settings suggests SUGGEST_BATTALIONS)."""
    from sizeup.transcribe import street_spoken
    have = {c for c in (units or {}) if re.fullmatch(r"\d{4}", c)}
    out = [*PHRASING, *channels(), *places(data, town_aliases), *chief_cars(data, have, battalions=battalions),
           *apparatus(data, battalions), *(street_spoken(s) for s in county_streets(gazetteer, top_streets))]
    return list(dict.fromkeys(out))


def table(phrases: list[str]) -> str:
    """The phrases in AWS's table form (tab-separated Phrase, IPA, SoundsLike, DisplayAs), as built for the list
    (transcribe.build_vocabulary: the same spelling, skips and limits): DisplayAs = the phrase as written when it
    has digits or letters said one by one ("twenty-three-oh-one" -> 2301, "S.S.U.-fifteen" -> S.S.U. 15); SoundsLike
    for the one-word place names in SOUNDS_LIKE."""
    from sizeup.transcribe import build_vocabulary
    built = build_vocabulary(phrases)
    respell = built["respell"]
    lines = ["Phrase\tIPA\tSoundsLike\tDisplayAs"]
    for a in built["phrases"]:
        said = a.replace("-", " ").lower()
        written = respell.get(said, "")
        if "." in a and not written:
            written = a.replace(".", "").replace("-", " ")
        sounds = SOUNDS_LIKE.get(a, "")
        lines.append(f"{a}\t\t{sounds}\t{written}")
    return "\n".join(lines) + "\n"
