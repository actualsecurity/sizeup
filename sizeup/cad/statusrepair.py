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
    what      MISHEARD below, each (status as written, pattern). A pattern that is an ordinary phrase ("around", "on
              vacation") needs the clause to end there or a readback time to follow ("<unit> en route, 1412": 4 digits
              or with a colon, never a house or road number); "around 5 minutes" is an ETA, never repaired
    fine      "is fine" / "was fine" is also what a welfare check sounds like, so only for a unit dispatched on an open
              call (fine_ok(code), from the CAD), said on its own radio or in 60 Control's readback, and never in a
              transmission about welfare or a change (PAR, accounted for, no injuries, OK, all good, everyone,
              Command / IC; cancel, return, disregard, hold, not needed, "you can")
    route     "on route" / "and route" (a misheard "en route") never before a road ("on Route 120", "on route to
              King Street"): clause-final from the unit's own radio (talker), or before a readback time
    callsign  "Engine" misheard before one of our engine numbers ("and she's 63", "imagine 63"; Ladder as "letter"):
              put back only when a status (as said, or repaired) follows the number, never on its own ("and she's
              63 years old" stays)
    kept      the transcript stays what speech-to-text wrote; the repaired text is what the status parser reads and
              what the Radio rows show, with the heard words on hover (fixed_text / fixes on a row, comms.py)

repair(text, named, codes, talker, fine_ok) -> (the repaired text, [{heard, to, at}]): named(text) is Phrases.named
(the callsigns of ours in a text); at is where the status as written starts in the repaired text.
"""

from __future__ import annotations

import re
from typing import Callable

# a readback time that can't be a house or road number: 4 digits or with a colon ("1412", "14:12", "2:12"; not "120"),
# and it ends the clause: "1412 North Main Street" is an address
CLOCK = r"(?:(?:[01]\d|2[0-3]):?[0-5]\d|\d:[0-5]\d)(?!\d)(?=\s*(?:$|[.,;]))"
# where an ordinary phrase must stop for it to be the status word: the end, a clause break, or a readback time
STOPS = rf"(?=\s*(?:$|[.,;!?]|(?:at\s+)?{CLOCK}))"
END = r"(?=\s*(?:$|[.,;!?]))"

FINE = "fine"            # the patterns that need fine_ok
ROUTE = "route"          # the patterns that need the talker clause-final, or a readback time

# (status as written, kind, the heard words: matched at the start of the slot, after a callsign and an optional comma)
MISHEARD: list[tuple[str, str, re.Pattern]] = [(to, kind, re.compile(p, re.I)) for to, kind, p in (
    # en route
    ("en route", FINE, rf"(?:(?:is|was|it's|it\s+is|'s)\s+)(?:fine|funny|find|fun)\b{STOPS}"),
    ("en route", ROUTE, r"(?:(?:is|was)\s+)?(?:on|and|an|n|un|end)[\s-]?(?:routes?|routs?|roots?)\b"),
    ("en route", "", r"(?:(?:is|was)\s+)?in[\s-]?(?:routs?|roots?)\b" + STOPS),
    ("en route", "", r"en[\s-]?(?:rout|root)\b"),
    ("en route", "", rf"around{STOPS}"),
    # on location (the department's "arrived"), on scene
    ("on location", "", rf"(?:(?:is|was)\s+)?(?:on|in|at|an|and)\s+(?:the\s+)?(?:vacation|allocation|allegation|"
                        rf"occasion|lucation|low\s+cation){STOPS}"),
    ("on location", "", rf"(?:sex|six|its|it's|is)\s+allegation{STOPS}"),
    ("on location", "", r"(?:on\s*line|un|in|at|an|and)[\s-]*(?:the\s+)?lo[\s-]?cations?\b(?!\s+of\b)"),
    ("on location", "", r"on[\s-]*lo[\s-]+cations?\b|on[\s-]*locations\b|onlocation\b"),
    ("on scene", "", r"on\s+(?:the\s+)?(?:seen|sean|scenes)\b"),
)]
ROUTE_OK = re.compile(rf"\s*,?\s*(?:at\s+)?{CLOCK}")             # "on route 1412": a readback time, not a road
WELFARE = re.compile(r"(?<![\w'])(?:PAR|accounted|cancel\w*|return\w*|disregard|hold\w*|not\s+needed|you\s+can|injur\w*|hurt|ok|okay|all\s+good|everyone|everybody|all\s+members|"
                     r"command|IC|welfare|condition|patient|she|he|they)(?![\w'])", re.I)

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


def _slot(text: str, end: int, code: str, talker: str, fine_ok, welfare: bool, dispatcher: bool = False):
    """The heard words in the status slot after a callsign of code ending at end: (where they start, the match, the
    status as written); (at, None, "") when there is nothing to put back."""
    m0 = re.match(r"\s*,?\s*", text[end:])
    at = end + m0.end()
    for to, kind, rx in MISHEARD:
        m = rx.match(text, at)
        if not m or not m.group(0).strip():
            continue
        if " ".join(m.group(0).lower().split()) == to:
            return at, None, ""                           # said right already
        if kind == FINE and (welfare or fine_ok is None or not fine_ok(code) or not (dispatcher or code == talker)):
            return at, None, ""                           # only the unit's own radio, or 60 Control's readback
        if kind == ROUTE and not (ROUTE_OK.match(text, m.end())
                                  or (code == talker and re.match(END, text[m.end():]))):
            return at, None, ""                           # "on Route 120", "on route to King Street": a road
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
            if not (STATUS_SAID.match(slot) or any(r.match(slot) for _, _, r in MISHEARD)):
                continue
            out += [text[pos:m.start()], f"{word} {m.group(2)}"]
            fixes.append({"heard": m.group(0), "to": f"{word} {m.group(2)}"})
            pos = m.end()
        text = "".join(out) + text[pos:]
    return text, fixes


def repair(text: str, named: Callable[[str], list[tuple[int, int, str]]], codes: dict[str, str] | None = None,
           talker: str = "", fine_ok: Callable[[str], bool] | None = None, dispatcher: bool = False
           ) -> tuple[str, list[dict]]:
    """(the text with the status words put back after our callsigns, [{heard, to, at}] for each); nothing to put
    back: (text, []). talker: the unit whose radio it is ("" unknown); dispatcher: 60 Control's transmission;
    fine_ok(code): may "is fine" be its en route (only from that unit's own radio, or 60 Control's readback)."""
    if not text:
        return text, []
    fixes: list[dict] = []
    if codes:
        text, fixes = callsigns(text, codes)
    welfare = bool(WELFARE.search(text))
    out, pos, length = [], 0, 0
    for _, end, code in named(text):
        if end < pos:
            continue
        at, m, to = _slot(text, end, code, talker, fine_ok, welfare, dispatcher)
        if m is None:
            continue
        lead = text[pos:at]
        if lead and not lead[-1].isspace():
            lead += " "                                  # "Ladder 31's fine" -> "Ladder 31 en route"
        out += [lead, to]
        length += len(lead)
        fixes.append({"heard": " ".join(m.group(0).split()), "to": to, "at": length})
        length += len(to)
        pos = m.end()
    return "".join(out) + text[pos:], fixes
