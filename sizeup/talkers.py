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
             "TK": "Truck", "B": "Battalion", "U": "Utility", "M": "Medic"}
APPARATUS_WORD = re.compile(r"^((?:Tower )?Ladder|Engine|Rescue|Squad|Truck|Tower|Battalion|Medic|Utility) \d+\b")


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
    if m := re.match(r"^(TL|TK|[ERLTSBUM])(\d{1,3})$", tag):   # "E60" -> "Engine 60"
        return f"{APPARATUS[m.group(1)]} {m.group(2)}"
    return None


def talker_prompt(units: list[str] | str, start: float | None = None, with_time: bool = False,
                  tz: str = "America/New_York") -> str:
    if isinstance(units, str):
        units = [u for u in units.split(",")]
    said: list[str] = []
    for u in units:
        s = spoken(u)
        if s and s not in said:
            said.append(s)
    if with_time and start and any(s.endswith(" Control") for s in said):
        said.append(datetime.fromtimestamp(start, ZoneInfo(tz)).strftime("%H%M"))
    return (", ".join(said) + ".") if said else ""
