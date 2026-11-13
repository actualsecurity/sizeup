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


# -- police (another P25 system's PD dispatch: radio-intel PR 3) ------------------------------------------------------
# A profile per source (data/stt/profiles.json): whisper's prompt and the Amazon Transcribe vocabulary `sizeup-pd`, both
# apart from the fire ones above. Nothing here names a person: the callsigns are the headquarters' (data/pd_units.json,
# role "headquarters"), the places come from the gazetteer at runtime (streets as said, public landmarks only), and the
# phrasing is generic. A police "266" is the Rye PD headquarters' callsign in this namespace only: sizeup-pd's respelling
# is used on the police sources alone (transcribe.AwsTranscriber.channel_respell), never on fire text, and the fire
# vocabulary (sizeup-pcfd) is never used on a police source.
PROFILES_FILE = DATA / "stt" / "profiles.json"
PD_UNITS_FILE = DATA / "pd_units.json"
# The APCO-style 10-codes heard on police dispatch, written as said ("10-4" -> "ten-four" on AWS, respelled back).
TEN_CODES = tuple(f"10-{n}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 26,
                                      27, 28, 29, 30, 31, 32, 33, 34, 36, 37, 38, 39, 41, 42, 45, 46, 50, 52, 53, 54, 55,
                                      56, 59, 61, 62, 65, 66, 70, 71, 73, 75, 77, 80, 84, 85, 90, 97, 98, 99))
POLICE_PHRASING = ("respond to", "en route", "on scene", "in the area", "be advised", "all units", "any unit",
                   "headquarters", "dispatch", "complainant", "caller states", "suspect", "vehicle", "plate",
                   "registration", "license", "warrant", "domestic", "disturbance", "aided case", "motor vehicle accident",
                   "M.V.A.", "E.D.P.", "shots fired", "pursuit", "back up", "cover car", "sector", "patrol",
                   "negative", "affirmative", "disregard", "copy", "stand by", "in custody", "transport")
POLICE_PLACES = ("Village of Port Chester", "City of Rye", "Village of Rye Brook", "Town of Rye", "Harrison",
                 "Greenwich", "Byram", "Mamaroneck", "Purchase", "Westchester County")
# Gazetteer landmark categories that are public places (the rest, e.g. offices, small shops, a historic house or a small
# congregation, may carry a person's name). Never a firehouse: its name ("... Engine & Hose ...") is the fire namespace's.
PUBLIC_LANDMARKS = re.compile(r"^(?:school|park|police|library|post_office|cemetery|municipal"
                              r"|amenity=(?:school|library|townhall|courthouse|community_centre"
                              r"|theatre|cinema|grave_yard|public_building)|leisure=(?:park|golf_course|marina|stadium"
                              r"|nature_reserve)|shop=(?:mall|shopping_center|supermarket|department_store)"
                              r"|tourism=(?:attraction|hotel|museum)"
                              r"|historic=(?:memorial|monument|district|battlefield|fort|ruins|milestone))$")
# A place named for a person's home ("<name> House", "... Homestead"): left out whatever its category.
PERSON_PLACE = re.compile(r"\b(?:house|homestead|residence|mansion|estate)\b", re.I)
FIRE_WORDS = re.compile(r"\b(?:fire|engine|ladder|hose|hook|rescue|firehouse)\b", re.I)
LANDMARK_WORDS = 6               # a landmark name longer than this many words isn't said on the air as written


def profiles(path: Path | None = None) -> dict:
    import json
    p = path or PROFILES_FILE
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"profiles": {}, "sources": {}, "services": {}}
    return {k: d.get(k) if isinstance(d.get(k), dict) else {} for k in ("profiles", "sources", "services")}


def profile(source_key: str = "", service: str = "", path: Path | None = None) -> dict | None:
    """A source's speech-to-text profile (data/stt/profiles.json): by its source key, else its service; None for none.
    The profile as written, with "name"."""
    d = profiles(path)
    name = d["sources"].get(str(source_key or "")) or d["services"].get(str(service or "").lower())
    prof = d["profiles"].get(name) if name else None
    return dict(prof, name=name) if isinstance(prof, dict) else None


def vocabulary_sources(name: str, path: Path | None = None) -> list[str]:
    """The source keys whose profile streams with this AWS vocabulary (channel_vocabulary on the hub)."""
    d = profiles(path)
    return [k for k, prof in d["sources"].items()
            if isinstance(d["profiles"].get(prof), dict) and d["profiles"][prof].get("vocabulary") == name]


def hq_callsigns(data: Path = DATA, service: str = "police") -> list[str]:
    """The headquarters' callsigns as said ("908", "266", "Headquarters"), from data/pd_units.json (role headquarters)."""
    import json
    try:
        d = json.loads((data / "pd_units.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for sysd in (d.get("systems") or {}).values():
        for c in (sysd or {}).get("callsigns") or ():
            if c.get("service") == service and c.get("role") == "headquarters" and c.get("said"):
                out.append(str(c["said"]))
    return list(dict.fromkeys(out))


def _clean_name(n: str) -> str:
    return " ".join(re.sub(r"\(.*?\)", "", str(n)).split())


def town_streets(towns, gazetteer: Path | None = None, per_town: int | None = None) -> list[str]:
    """Every street of these towns (addresses or a street line there), as written in the gazetteer, the busiest first;
    per_town: only that many of each town's busiest, taken in turn (for a short prompt)."""
    p = gazetteer or (DATA / "gazetteer.sqlite")
    towns = [str(t) for t in towns]
    if not p.is_file() or not towns:
        return []
    c = sqlite3.connect(f"file:{p}?immutable=1", uri=True)
    try:
        q = ",".join("?" * len(towns))
        rows = c.execute(f"SELECT s.name, x.muni, SUM(x.n) AS n FROM streets s JOIN ("
                         f"SELECT street_id, muni, COUNT(*) AS n FROM addresses WHERE muni IN ({q}) GROUP BY street_id, muni "
                         f"UNION ALL SELECT street_id, muni, 0 FROM lines WHERE muni IN ({q}) GROUP BY street_id, muni"
                         f") x ON x.street_id = s.id GROUP BY s.id, x.muni ORDER BY n DESC, s.name", (*towns, *towns)).fetchall()
    finally:
        c.close()
    if per_town is None:
        return list(dict.fromkeys(n for n in (_clean_name(r[0]) for r in rows) if n))
    by: dict[str, list[str]] = {t: [] for t in towns}
    for name, muni, _n in rows:
        name = _clean_name(name)
        if name and name not in by[muni] and len(by[muni]) < per_town:
            by[muni].append(name)
    out: list[str] = []
    for i in range(per_town):
        for t in towns:
            if i < len(by[t]):
                out.append(by[t][i])
    return list(dict.fromkeys(out))


def town_landmarks(towns, gazetteer: Path | None = None) -> list[str]:
    """The gazetteer's public landmarks in these towns (PUBLIC_LANDMARKS: schools, parks, places of worship, town halls,
    malls ...), by name and alias, as said."""
    p = gazetteer or (DATA / "gazetteer.sqlite")
    towns = [str(t) for t in towns]
    if not p.is_file() or not towns:
        return []
    c = sqlite3.connect(f"file:{p}?immutable=1", uri=True)
    try:
        rows = c.execute(f"SELECT name, aliases, category FROM landmarks WHERE muni IN ({','.join('?' * len(towns))}) "
                         "ORDER BY name", towns).fetchall()
    finally:
        c.close()
    out = []
    for name, aliases, cat in rows:
        if (not PUBLIC_LANDMARKS.match(str(cat or "")) or FIRE_WORDS.search(f"{name} {aliases}")
                or PERSON_PLACE.search(f"{name} {aliases}")):
            continue
        for n in [name, *str(aliases or "").split("|")]:
            n = _clean_name(n)
            if n and len(n.split()) <= LANDMARK_WORDS:
                out.append(n)
    return list(dict.fromkeys(out))


def police_phrases(towns=("Port Chester", "Rye", "Rye Brook"), data: Path = DATA,
                   gazetteer: Path | None = None) -> list[str]:
    """The sizeup-pd vocabulary as written, in priority order (the push keeps the first transcribe.MAX_PHRASES within
    AWS's 50 KB): the headquarters' callsigns, the 10-codes, police phrasing, the towns, every street of the towns as
    said (transcribe.street_spoken), their public landmarks. No fire callsign, no person."""
    from sizeup.transcribe import street_spoken
    out = [*hq_callsigns(data), *TEN_CODES, *POLICE_PHRASING, *(str(t) for t in towns), *POLICE_PLACES,
           *(street_spoken(s) for s in town_streets(towns, gazetteer)), *town_landmarks(towns, gazetteer)]
    return list(dict.fromkeys(" ".join(p.split()) for p in out if p and p.strip()))


def police_prompt(prof: dict, data: Path = DATA, gazetteer: Path | None = None) -> str:
    """whisper's prompt for a police source: the profile's phrasing, the headquarters' callsigns, the towns and the
    busiest prompt_streets streets of the towns as said. Never a talker's or a person's name (no talker_prompt)."""
    from sizeup.transcribe import street_spoken
    towns = [str(t) for t in prof.get("towns") or ()]
    n = int(prof.get("prompt_streets") or 0)
    per = -(-n // len(towns)) if towns and n > 0 else 0
    streets = [street_spoken(s) for s in town_streets(towns, gazetteer, per_town=per)][:n] if per else []
    parts = [str(prof.get("prompt") or "").strip(), ", ".join(hq_callsigns(data)), ", ".join(towns), ", ".join(streets)]
    return " ".join(p if p.endswith(".") else p + "." for p in parts if p)
