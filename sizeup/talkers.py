"""Who keyed up, said the way it's said on the air: a per-call hint for speech-to-text.

P25 calls carry each talker's radio alias (trunk-recorder srcList tags); FG1 carries MDC-1200
IDs. Spoken, "77A4" is "77 Alpha 4", "E60" is "Engine 60", "60 Ctrl OP 7" is "60 Control".
Passing just these names (and optionally the call's HHMM, which 60 Control says at the end of
every transmission) as whisper's prompt steers it toward them instead of "7704-3".
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

LETTER = {"A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot", "G": "Golf",
          "H": "Hotel", "I": "India", "J": "Juliet", "K": "Kilo", "L": "Lima", "M": "Medic", "N": "November",
          "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango", "U": "Uniform",
          "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu"}
APPARATUS = {"E": "Engine", "R": "Rescue", "L": "Ladder", "TL": "Tower Ladder", "T": "Tower", "S": "Squad",
             "TK": "Truck", "B": "Battalion", "U": "Utility", "M": "Medic", "AP": "Airport"}   # AP7: the airport's ARFF (#105)
APPARATUS_WORD = re.compile(r"^((?:Tower )?Ladder|Engine|Rescue|Squad|Truck|Tower|Battalion|Medic|Utility|Airport) \d+\b")


def spoken(tag: str) -> str | None:
    """How a talker alias is said, or None when it isn't something said on the air."""
    tag = " ".join(str(tag).split())
    if m := re.match(r"^(\d+) Ctrl\b", tag):                   # "60 Ctrl OP 7": a 60 Control dispatcher
        return f"{m.group(1)} Control"
    if m := APPARATUS_WORD.match(tag):                          # "Engine 61 Portable 1" (MDC alias)
        return m.group(0)
    if m := re.match(r"^(\d{4})(?: [A-Za-z].*)?$", tag):        # "2398" / "2398 Duty Officer"
        return m.group(1)
    if m := re.match(r"^(\d{1,3})([A-Z])(\d{1,2})$", tag):      # "77A4" -> "77 Alpha 4"
        return f"{m.group(1)} {LETTER[m.group(2)]} {m.group(3)}"
    if m := re.match(r"^(TL|TK|AP|[ERLTSBUM])(\d{1,3})$", tag):   # "E60" -> "Engine 60", "AP7" -> "Airport 7"
        return f"{APPARATUS[m.group(1)]} {m.group(2)}"
    return None


def hint_names(units: list[str] | str = "", rig: str = "", radio_ids: str = "", named=(),
               blocked=()) -> list[str]:
    """Who keyed up, as said on the air, for speech-to-text context: the department's exact names of the radios that
    keyed up (named: a radio ID's own entry in Settings > Radios > Radio IDs with "use as hint" on, e.g. a PD dispatch
    console's "908"; never a range's label: officers swap radios, so it doesn't say who is talking), the units its
    radios are on (rig "E63": "Engine 63"), then the talker aliases the system sent ("77A4": "77 Alpha 4", "60 Ctrl OP
    3": "60 Control"). Never a raw radio ID: a talker that is one of the clip's own radio IDs is left out (an FG1 radio
    with no alias shows its MDC ID), and so is anything of a radio whose hint is off (blocked: its unit codes and
    aliases)."""
    ids = {x.strip().upper() for x in str(radio_ids or "").split(",") if x.strip()}
    blocked = {str(b).strip().upper() for b in blocked if str(b).strip()}
    if isinstance(units, str):
        units = units.split(",")
    said: list[str] = []

    def add(s: str | None) -> None:
        if s and s not in said and s.upper() not in ids:
            said.append(s)
    for n in named:
        add(" ".join(str(n).split()))
    for code in str(rig or "").split(","):
        code = code.strip()
        if code and code.upper() not in blocked:
            add(spoken(code))
    for u in units:
        u = " ".join(str(u).split())
        if u and u.upper() not in ids and u.upper() not in blocked:
            add(spoken(u))
    return said


def talker_prompt(units: list[str] | str, start: float | None = None, with_time: bool = False,
                  tz: str = "America/New_York", rig: str = "", radio_ids: str = "", named=(), blocked=()) -> str:
    said = hint_names(units, rig, radio_ids, named, blocked)
    if with_time and start and any(s.endswith(" Control") for s in said):
        said.append(datetime.fromtimestamp(start, ZoneInfo(tz)).strftime("%H%M"))
    return (", ".join(said) + ".") if said else ""
