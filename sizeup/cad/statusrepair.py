"""Unit status words speech-to-text mishears, put back right after one of our unit callsigns (user, 2026-11-10: "en
route and responding and other common verbiage that is clear to hear in this audio, super clear, is not getting
picked up").

Clear audio on Fire/EMS 15 comes back with the status word replaced by a common English phrase of the same shape:
"Engine 61 en route" as "Engine 61 is fine", 60 Control's readback "Engine 61 en route, 1412" as "Engine 61 around
1412", "Engine 61 on location" as "Engine 61 on vacation". The status parser (sizeup/cad/statushints.py) then finds
no status, so neither the suggestion nor the rig automation happens.

    where     only in the status slot: right after a callsign of ours (the board's units: Engine 61, Ladder 31, 23 91),
              with nothing but a comma between. The same words anywhere else stay as heard ("the patient is fine",
              "turn around", "Engine 61 around the back" have no callsign + status slot)
    what      MISHEARD below, each (status as written, pattern). A pattern that is an ordinary phrase ("is fine",
              "around", "on vacation") needs the transmission or the clause to end there, or a time to follow
              (60 Control's readback "<unit> en route, <HHMM>"); "around 5 minutes" is an ETA, never repaired
    callsign  "Engine" misheard before one of our engine numbers ("and she's 63", "imagine 63"; Ladder as "letter"):
              put back only when a status (as said, or repaired) follows the number, never on its own ("and she's
              63 years old" stays)
    kept      the transcript stays what speech-to-text wrote; the repaired text is what the status parser reads and
              what the Radio rows show, with the heard words on hover (fixed_text / fixes on a row, comms.py)

repair(text, named) -> (text, [{heard, to}]): named(text) is Phrases.named (the callsigns of ours in a text).
"""

from __future__ import annotations

import re
from typing import Callable

# a time as 60 Control says it in a readback ("1412", "14 12", "2:12", "14:12"); not an ETA or a count
TIME = (r"(?:[01]?\d|2[0-3])[:\s]?[0-5]\d(?!\d)(?!\s*(?:minutes?|mins?|seconds?|secs?|feet|ft|blocks?|years?|miles?|"
        r"degrees|percent|%|psi|gallons?|pounds?|lbs?)\b)")
# a readback time that can't be a road's number: 4 digits or with a colon ("1412", "14:12", "2:12"; not "Route 120")
CLOCK = r"(?:(?:[01]\d|2[0-3]):?[0-5]\d|\d:[0-5]\d)(?!\d)"
# where an ordinary phrase must stop for it to be the status word: the end, a clause break, or a readback time
STOPS = rf"(?=\s*(?:$|[.,;!?]|(?:at\s+)?{TIME}))"

# (status as written, the heard words: matched at the start of the slot, after a callsign and an optional comma)
MISHEARD: list[tuple[str, re.Pattern]] = [(to, re.compile(p, re.I)) for to, p in (
    # en route
    ("en route", rf"(?:(?:is|was|it's|it\s+is|'s)\s+)(?:fine|funny|find|fun)\b{STOPS}"),
    ("en route", rf"(?:(?:is|was)\s+)?(?:in|on|and|an|n|un|end)[\s-]?(?:routes?|routs?|roots?)\b"
                 rf"(?=\s*(?:$|[.,;!?]|[A-Za-z]|(?:at\s+)?{CLOCK}))"),   # never "on Route 120": a road
    ("en route", r"en[\s-]?(?:rout|root)\b"),
    ("en route", rf"around{STOPS}"),
    # on location (the department's "arrived"), on scene
    ("on location", rf"(?:(?:is|was)\s+)?(?:on|in|at|an|and)\s+(?:the\s+)?(?:vacation|relocation|allocation|allegation|"
                    rf"occasion|lucation|low\s+cation){STOPS}"),
    ("on location", rf"(?:sex|six|its|it's|is)\s+allegation{STOPS}"),
    ("on location", r"(?:on\s*line|un|in|at|an|and)[\s-]*(?:the\s+)?lo[\s-]?cations?\b(?!\s+of\b)"),
    ("on location", r"on[\s-]*lo[\s-]+cations?\b|on[\s-]*locations\b|onlocation\b"),
    ("on scene", r"on\s+(?:the\s+)?(?:seen|sean|scenes)\b"),
)]

# the status said (as the parser reads it): a misheard callsign is put back only before one of these
STATUS_SAID = re.compile(r"(?:(?:is|was|now)\s+)?(?:responding|en[\s-]?route|in[\s-]?route|on\s+(?:the\s+)?(?:location|"
                         r"scene)|arrived|arriving|in\s+service|back\s+in\s+service|available|returning|"
                         r"out\s+of\s+service|staging|staged|in\s+quarters)\b", re.I)

# the apparatus word misheard (#: the board's code letter): "and she's 63" for "Engine 63"
APPARATUS_MISHEARD = {
    "E": ("Engine", r"and\s+she'?s|and\s+you're|and\s+your|imagine|n[\s-]?gene|in[\s-]?gin|en[\s-]?gin|engin"),
    "L": ("Ladder", r"letter|latter|lader|lather"),
    "R": ("Rescue", r"rescuing|rescued|rescues"),
}


def _slot(text: str, end: int) -> tuple[int, re.Match | None, str]:
    """The heard words in the status slot after a callsign ending at end: (where they start, the match, the status)."""
    m0 = re.match(r"\s*,?\s*", text[end:])
    at = end + m0.end()
    for to, rx in MISHEARD:
        m = rx.match(text, at)
        if m and m.group(0).strip():
            if " ".join(m.group(0).lower().split()) == to:
                return at, None, ""                       # said right already
            return at, m, to
    return at, None, ""


def callsigns(text: str, codes: dict[str, str]) -> tuple[str, list[dict]]:
    """A misheard apparatus word before one of our unit numbers, put back when a status follows."""
    fixes: list[dict] = []
    for letter, (word, heard) in APPARATUS_MISHEARD.items():
        nums = sorted({c[len(letter):] for c in codes if re.fullmatch(rf"{letter}\d+", c)}, key=len, reverse=True)
        if not nums:
            continue
        rx = re.compile(rf"(?<![\w'])({heard})\s+({'|'.join(nums)})(?![\w])", re.I)
        out, pos = [], 0
        for m in rx.finditer(text):
            after = text[m.end():]
            rest = re.match(r"\s*,?\s*", after)
            slot = after[rest.end():]
            if not (STATUS_SAID.match(slot) or any(r.match(slot) for _, r in MISHEARD)):
                continue
            out += [text[pos:m.start()], f"{word} {m.group(2)}"]
            fixes.append({"heard": m.group(0), "to": f"{word} {m.group(2)}"})
            pos = m.end()
        text = "".join(out) + text[pos:]
    return text, fixes


def repair(text: str, named: Callable[[str], list[tuple[int, int, str]]], codes: dict[str, str] | None = None
           ) -> tuple[str, list[dict]]:
    """(the text with the status words put back after our callsigns, [{heard, to}] for each). Unchanged: ([], text)."""
    if not text:
        return text, []
    fixes: list[dict] = []
    if codes:
        text, fixes = callsigns(text, codes)
    out, pos = [], 0
    for _, end, _code in named(text):
        if end < pos:
            continue
        at, m, to = _slot(text, end)
        if m is None:
            continue
        out += [text[pos:at], to]
        fixes.append({"heard": " ".join(m.group(0).split()), "to": to})
        pos = m.end()
    return "".join(out) + text[pos:], fixes
