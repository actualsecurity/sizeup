"""Unit statuses suggested from what is heard on the radio (GitHub #37): "E64 on location?" when Engine 64 says
"on location" on FG1 or Fire/EMS 15. A suggestion, never a status: nothing changes until a dispatcher taps ✓
(POST /api/cad/hint/<id>/accept); ✕ dismisses it (remembered: the same transmission never suggests it again).

    heard     a transcript arrives (Comms.on_text: a new transmission, or a re-run's new text) on one of our fire
              channels (FG1, Fire/EMS 15, the Airport Incident talkgroup; the data file's "status_hints" changes them)
    phrase    PHRASES below: what is said -> what it means (responding, on location, staging, available, in quarters,
              out of service, incident command); a phrase is skipped when it is negated ("not on location yet"), a
              question ("is Engine 64 responding?"), a condition ("advise when on location"), an order ("go to
              staging"), a time to come ("on location in 2 minutes"), or said of someone else ("PD is on location")
    misheard  the status words speech-to-text turns into ordinary ones right after our callsign ("Engine 61 is fine"
              for a unit dispatched on an open call, "Ladder 31 around 1412", "Engine 61 on vacation") are put back
              first (sizeup/cad/statusrepair.py); the suggestion keeps the heard words ("en route (heard “is fine”)")
              and the whole transcript as heard
    addressee the radio format is "<addressee>, <who is talking> <status>": a sentence's leading "<callsign>," is the
              one called, never one that gets the status ("23 91, responding." on Engine 61's radio: Engine 61)
    list      units named one after another, then one status ("23 91, Engine 61, Ladder 31 on location"): each of
              them only when the talker is one of them (an officer reporting his group) or it is 60 Control's readback
              (a time after it, or an acknowledgement); otherwise the units right before the status, else the talker.
              Nothing carries past . ; ? !, and callsigns with only a space between ("23 91 Engine 61 responding")
              are the one called, then who is talking: the last one only ("and" keeps a list)
    number    a bare number before the phrase ("64 on scene") is the talker's unit only when its radio is that unit
              (E64); a chief's 4-digit number off in the last digit only, same department prefix, and no unit any
              board knows ("2390 on location" from 2391's radio) is his too
    near      "in the area", "in the vicinity", "on the block", "pulling up": on location (the department has no
              arriving status), only from the talker about itself, clause-final or before a readback time; never
              "back in the area", "in the area of", "if you need", "we can take", "available"
    who       a unit of ours (PCFD's and Rye Brook's fire units, the chiefs 239x: sizeup/cad/feed.py load_cfg) named right
              before the phrase ("Engine 64 on location", "60 Control copies, 2391 responding"), else the talker when
              its radio is one of ours (the registry's rig), else the unit that called ("Engine 64 to 60 Control, on
              location"); nobody of ours: no suggestion
    not       the unit's status already is that (or, for available, any available status); a step backwards
              (responding after on location) unless said so ("now", "again", "back"); on location, staging or command
              for a unit on no call with no call on the channel; a status set after the transmission was made
    where     the event the unit is on, else the one open event the transmission is linked to (comms.py: a manual
              link, the Airport talkgroup), else on FG1 the one emergency event with units on it then (as the FG1
              channel link did before #163: the suggestion still knows the call, the transmission isn't put on it);
              accepting then puts the unit on it
    gone      after expire_s (5 minutes from when it was heard), when the unit's status changes by any means, or when
              a newer suggestion for the unit comes

Every suggestion is kept with how it ended (status_hints.outcome: pending | accepted | dismissed | expired | stale |
superseded | withdrawn) so precision can be measured: GET /api/cad/export/status-hints.csv.

The data file (data/cad_lists.json, optional):

    "status_hints": {"channels": ["FG1"], "talkgroups": [307, 333], "expire_s": 300, "enabled": true}
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sizeup.cad.feed import load_cfg, unit_forms
from sizeup.cad.lists import ListsError
from sizeup.cad.statusrepair import repair
from sizeup.cad.store import TZ, CadError

EXPIRE_S = 300
SAID_MAX = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS status_hints (
  id INTEGER PRIMARY KEY,
  traffic_id TEXT NOT NULL,             -- the transmission (radio_traffic.id; its clip: /api/audio/<id>.wav)
  unit TEXT NOT NULL,                   -- the board code: E64, 2391
  unit_id INTEGER NOT NULL,
  status TEXT NOT NULL,                 -- the suggested unit_statuses code (OL)
  intent TEXT NOT NULL,                 -- enroute | onscene | staging | free | quarters | oos | command
  phrase TEXT NOT NULL DEFAULT '',      -- the words that meant it ("on location")
  said TEXT NOT NULL DEFAULT '',        -- the sentence they were in
  text TEXT NOT NULL DEFAULT '',        -- the whole transcript when suggested
  who TEXT NOT NULL DEFAULT '',         -- named (the text named the unit) | talker (its radio) | caller
  channel TEXT NOT NULL DEFAULT '',
  talkgroup INTEGER,
  heard_at REAL NOT NULL,               -- when the transmission started
  incident_id INTEGER,                  -- the event it is for (NULL: none)
  prev_status TEXT NOT NULL DEFAULT '', -- the unit's status when suggested
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  outcome TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted | dismissed | expired | stale | superseded | withdrawn
  decided_at REAL,
  operator TEXT NOT NULL DEFAULT '',
  UNIQUE (traffic_id, unit)
);
CREATE INDEX IF NOT EXISTS status_hints_pending ON status_hints(outcome) WHERE outcome = 'pending';
"""

CSV_COLUMNS = ["Heard", "Unit", "Suggested", "Was", "Phrase", "Said", "Transcript", "Who", "Channel", "Incident #",
               "Outcome", "Decided", "Operator", "Transmission"]

# -- the phrases ---------------------------------------------------------------------------------------------------
# (intent, regex). Each is a phrase as said on the air; the checks around it (lead_in, the guards) decide whether it
# is a unit telling its status.
ROUTE = r"(?:en|in)[\s-]?route"
# a readback's time ("en route 1412"): 4 digits or with a colon, ending the clause ("1412 King Street" is an address)
CLOCK_AFTER = r"\s*(?:(?:[01]\d|2[0-3]):?[0-5]\d|\d:[0-5]\d)(?!\d)(?=\s*(?:$|[.,;]))"
READBACK = re.compile(r"^\s*,?\s*(?:at\s+)?(?:(?:[01]\d|2[0-3]):?[0-5]\d|\d:[0-5]\d)(?!\d)(?=\s*(?:$|[.,;]))")
ACK = re.compile(r"(?<![\w'])(?:copy|copies|copied|received|10-4|ten-four|roger|affirmative|acknowledged)(?![\w'])", re.I)
PHRASES: list[tuple[str, re.Pattern]] = [(i, re.compile(p, re.I)) for i, p in (
    ("enroute", r"\bresponding\b(?!\s+(?:units?|compan(?:y|ies)|apparatus|crews?|members|personnel|to\s+(?:the\s+)?"
                r"(?:hospital|ER)\b))"),
    ("enroute", rf"\b{ROUTE}\b(?!(?!{CLOCK_AFTER})\s*\d)"),   # not "in Route 1"; "en route 1412" (a readback)
    ("onscene", r"\bon[\s-](?:the\s+)?(?:location|scene)\b"),
    ("onscene", r"\bat[\s-](?:the\s+)?(?:location|scene)\b(?!\s+of\b)"),
    ("onscene", r"\barriving\b(?!\s+(?:shortly|soon|momentarily|in|on|at)\b)"),   # "arriving on location": that one
    # near the scene: the department has no "arriving" status, so on location (said only by the unit itself: NEAR)
    ("onscene", r"\bin\s+the\s+(?:area|vicinity)\b(?!\s+of\b)"),
    ("onscene", r"\bon\s+the\s+block\b(?!\s+of\b)"),
    ("onscene", r"\bpulling\s+up\b(?!\s+(?:the|a|an|hose|lines?|to\s+the\s+hydrant)\b)"),
    ("onscene", r"\barrived\b(?!\s+at\s+(?:the\s+)?(?:hospital|ER)\b)"),
    ("staging", r"\b(?:in|at)\s+(?:the\s+)?staging\b"),
    ("staging", r"\bstaged\b"),
    ("staging", r"\bstaging\b(?!\s+(?:area|officer|manager|sector|location|is|will|for)\b)"),
    ("quarters", r"\b(?:back\s+)?(?:in|at)\s+quarters\b"),
    ("free", r"\b(?:back\s+)?in[\s-]service\b"),
    ("free", r"\bavailable\b(?!\s+(?:on|by|via)\s+(?:the\s+)?(?:radio|phone|cell|channel|landline)\b)"),
    ("free", r"\breturning\b(?!\s+to\s+(?:the\s+)?(?:scene|location|call|incident|fire|address|job|box)\b)"
             r"(?!\s+(?:your|the|a)\s+call\b)"),
    ("free", r"\bclear(?:ing)?\b"),
    ("oos", r"\bout\s+of\s+service\b"),
    ("command", r"\b(?:establish(?:ing|ed)?|assum(?:e|ing|ed)|tak(?:e|ing)|took|(?:will|'ll|gonna)\s+(?:be|have)|"
                r"(?:has|have|is|am|are|'s|'m|'re)(?:\s+in)?)\s+(?:(?!(?:at|to|on|from|with|of|for|over|into|in)\b)"
                r"[\w'-]+\s+){0,3}?command\b(?!\s+(?:post|channel|frequency|vehicle|van|bus|unit|staff|board|officer|"
                r"center|centre|level|structure|is|will)\b)"),
)]
NEAR = re.compile(r"in\s+the\s+(?:area|vicinity)|on\s+the\s+block|pulling\s+up", re.I)
FINE_WORD = re.compile(r"\bf(?:ine|un|unny|ind)\b", re.I)
# "in the area" said of being free nearby, or offered: never an arrival
NEAR_NOT = re.compile(r"(?<![\w'])(?:back\s+in\s+the\s+area|if\s+you\s+need|we\s+can\s+take|can\s+take|available|"
                      r"in\s+the\s+(?:area|vicinity)\s+of)(?![\w'])", re.I)
NEAR_AFTER = re.compile(r"\s*(?:[.;!?,]\s*)?$|\s*(?:at\s+)?(?:(?:[01]\d|2[0-3]):?[0-5]\d|\d:[0-5]\d)(?!\d)"
                        r"(?=\s*(?:$|[.,;]))", re.I)
# The words that may stand between whoever is talking and the phrase ("Engine 64 is now on location").
FILLER = (r"is|are|am|was|were|now|currently|just|also|already|be|been|being|will|shall|have|has|had|gonna|going|to|"
          r"and|still|back|arriving|arrived|placing|place|placed|putting|put|ourselves|myself|itself|going|"
          r"copy|copies|okay|ok|roger|10-4|ten-four|yes|yeah|affirmative|alright|so|uh|um|ah|"
          r"we|i|we're|we've|we'll|i'm|i've|i'll|we are|i am|let's|let us|us")
FILLER_END = re.compile(rf"(?:(?<![\w'])(?:{FILLER})[\s,]*)+$", re.I)
FIRST_PERSON = re.compile(r"(?<![\w'])(?:we|i|we're|we've|we'll|i'm|i've|i'll|us|ourselves|myself|let's)(?![\w'])", re.I)
CONTROL_END = re.compile(r"(?<![\w'])(?:(?:\d{2}|sixty)\s+)?(?:control|dispatch|headquarters|hq)[\s,]*$", re.I)
QUESTION_START = re.compile(r"^\s*(?:are|is|was|were|did|do|does|has|have|can|could|would|what|what's|whats|where|"
                            r"when|who|who's|how|any|eta|confirm)\b", re.I)
GUARD = re.compile(r"(?<![\w'])(?:not|n't|no|never|negative|unable|cannot|can't|won't|isn't|aren't|wasn't|haven't|"
                   r"hasn't|when|whenever|once|if|until|till|before|upon|after|soon|go|proceed|report|"
                   r"tell|advise|ask|need|needs|want|wants|should|please|make|request|requesting|expect|expected|"
                   r"estimated|eta|almost|about\s+to|nearly|shortly|minutes?)(?![\w'])", re.I)
SOON_AFTER = re.compile(r"^\s*(?:in|within|about|approximately|approx|like)\s+(?:about\s+|approximately\s+|like\s+)?"
                        r"(?:\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|a\s+couple|couple|few)\b", re.I)
CLEAR_QUALIFIED = re.compile(r"^\s*(?:of\s+)?(?:the\s+)?(?:scene|call|incident|assignment|location|box|alarm|job)\b|"
                             r"^\s*(?:and|,)\s*(?:available|in\s+service|back\s+in\s+service|returning)\b", re.I)
CLEAR_NOT = re.compile(r"^\s*(?:to|the|that|this|it|out|up|on|for|from|with|a|an|smoke|fire|line|channel|air|road|"
                       r"street|area|building|floor|room|house|structure)\b", re.I)
EXPLICIT = re.compile(r"(?<![\w'])(?:now|again|back|re-?respond\w*)(?![\w'])", re.I)
CLAUSE = re.compile(r"[^.;!?\n,]+[.;!?\n,]?")
TO_BEFORE = re.compile(r"(?<![\w'])to\s*$", re.I)
# The order statuses go in (a suggestion that would step back is not made unless said so): dispatched < responding,
# staging < on location < command. Available, quarters and out of service end or pause a call: never "backwards".
RANK = {"dispatched": 1, "": 1, "enroute": 2, "staging": 2, "onscene": 3, "command": 4}
NEEDS_CALL = ("onscene", "staging", "command")     # off any call, with no call on the channel: not suggested


@dataclass
class Found:
    intent: str
    phrase: str
    said: str
    units: list[str]                 # the units of ours the sentence names as doing it ([] = the talker / caller)
    explicit: bool = False
    lead: str = ""                   # unit | first | start | control | number
    number: str = ""                 # lead "number": the bare number said ("64 on scene"), the talker's own or nothing
    listed: list[str] = field(default_factory=list)     # units named only, in the clauses just before ("Engine 61,")
    addressee: list[str] = field(default_factory=list)  # the sentence's leading "<callsign>," (who is called)
    at: int = 0                      # where the phrase starts in the text
    near: bool = False               # "in the area" (NEAR): the talker's own arrival only
    timed: bool = False              # a readback time follows ("en route, 1412")


@dataclass
class Cfg:
    enabled: bool = True
    channels: tuple[str, ...] = ("FG1",)
    talkgroups: tuple[int, ...] = (307, 333)
    expire_s: float = EXPIRE_S
    units: dict[str, str] = field(default_factory=dict)


def load_hint_cfg(lists, channels: tuple[str, ...], talkgroups: tuple[int, ...]) -> Cfg:
    """"status_hints" from the data file over the defaults (our fire channels). ListsError on a bad value."""
    where = lists.path or "cad_lists"
    try:
        d = json.loads(Path(lists.path).read_text()) if lists.path else {}
    except (OSError, ValueError):
        d = {}
    h = d.get("status_hints") or {}
    if not isinstance(h, dict):
        raise ListsError(f"{where}: status_hints: an object {{channels, talkgroups, expire_s, enabled}}")
    ch = h.get("channels", list(channels))
    if not isinstance(ch, list) or not all(isinstance(c, str) and c.strip() for c in ch):
        raise ListsError(f"{where}: status_hints.channels: a list of channel names, e.g. [\"FG1\"]")
    tgs = h.get("talkgroups", list(talkgroups))
    if not isinstance(tgs, list) or not all(isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= 65535 for t in tgs):
        raise ListsError(f"{where}: status_hints.talkgroups: a list of P25 talkgroup numbers, e.g. [307, 333]")
    exp = h.get("expire_s", EXPIRE_S)
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not 30 <= exp <= 3600:
        raise ListsError(f"{where}: status_hints.expire_s: seconds, 30 to 3600")
    on = h.get("enabled", True)
    if not isinstance(on, bool):
        raise ListsError(f"{where}: status_hints.enabled: true or false")
    return Cfg(on, tuple(c.strip() for c in ch), tuple(tgs), float(exp), load_cfg(lists).units)


class Phrases:
    """What a transcript says about our units' statuses: find(text) -> [Found]."""

    def __init__(self, units: dict[str, str], known_numbers=()):
        self.units = [(c, re.compile(r"(?<![\w])(?:" + "|".join(unit_forms(c, sp)) + r")(?![\w])", re.I))
                      for c, sp in units.items()]
        self.codes = set(units)
        self.spoken = dict(units)
        # every unit number any board knows (ours, Rye Brook's, mutual aid's): a heard one is never someone else's
        self.known_numbers = set(known_numbers) | {re.sub(r"\D", "", c) for c in units if c.isdigit()}

    def repair(self, text: str, talker: str = "", fine_ok=None, dispatcher: bool = False) -> tuple[str, list[dict]]:
        """The text with misheard status words after our callsigns put back (sizeup/cad/statusrepair.py)."""
        return repair(text, self.named, self.spoken, talker, fine_ok, dispatcher)

    def own_number(self, talker: str, heard: str) -> bool:
        """A bare number heard before a status is the talker's own: its number, or (a chief, 4 digits) the same
        department prefix with only the last digit off, when the heard number is no unit any board knows."""
        own = re.sub(r"\D", "", talker)
        if not own or not heard:
            return False
        if own == heard:
            return True
        return (len(own) == len(heard) == 4 and own[:3] == heard[:3] and own[3] != heard[3]
                and heard not in self.known_numbers)

    def named(self, text: str) -> list[tuple[int, int, str]]:
        """(start, end, code) of every unit of ours the text names, in order (the longest at a place)."""
        spans = sorted(((m.start(), m.end(), c) for c, rx in self.units for m in rx.finditer(text)), key=lambda s: (s[0], -s[1]))
        out: list[tuple[int, int, str]] = []
        for s in spans:
            if not out or s[0] >= out[-1][1]:
                out.append(s)
        return out

    def caller(self, text: str) -> str:
        """The unit of ours that called ("Engine 64 to 60 Control, ..."): the first named, unless it is the one called
        ("to Engine 64", or the leading "Engine 64, ..." the radio format puts the addressee in)."""
        named = self.named(text)
        if not named or TO_BEFORE.search(text[:named[0][0]]):
            return ""
        if not text[:named[0][0]].strip(" ,") and re.match(r"\s*,", text[named[0][1]:]):
            return ""
        return named[0][2]

    def find(self, text: str) -> list[Found]:
        out: list[Found] = []
        listed: list[str] = []          # the units of ours a run of clauses has only named so far ("Engine 61, Ladder 31,")
        addressee: list[str] = []       # the sentence's leading "<callsign>,": who is being called
        fresh = True                    # at a sentence's start
        near_not = bool(NEAR_NOT.search(text or ""))
        for m in CLAUSE.finditer(text or ""):
            clause = m.group(0)
            body = clause.rstrip()
            ends = body[-1:] in (".", ";", "!", "?", "\n") or clause.endswith("\n")
            start, fresh = fresh, ends
            if not body.strip() or body.endswith("?") or QUESTION_START.match(body):
                listed, addressee = [], []
                continue
            named = self.named(clause)
            only = self._only_units(clause, named)
            if only is not None:
                if ends:
                    listed, addressee = [], []           # "Ladder 31." alone: nothing carries past the full stop
                elif start:
                    listed, addressee = [], only         # "23 91, ...": the one called
                else:
                    listed += [c for c in only if c not in listed]
                continue
            before, called = listed, addressee
            listed = []
            if ends:
                addressee = []
            hits = sorted(((x.start(), x.end(), i) for i, rx in PHRASES for x in rx.finditer(clause)), key=lambda h: (h[0], -h[1]))
            taken, last_end = [], 0
            for s, e, intent in hits:
                if taken and s < taken[-1][1]:
                    continue                                    # inside a longer phrase already taken
                taken.append((s, e, intent))
                near = bool(NEAR.match(clause[s:e]))
                if near and (near_not or not NEAR_AFTER.match(clause[e:])):
                    continue                                    # "back in the area", "in the area if you need us"
                f = self._one(clause, s, e, intent, named, last_end)
                if f is not None:
                    f.at, f.near, f.addressee = m.start() + s, near, list(called)
                    f.timed = bool(READBACK.match(text[m.start() + e:]))
                    if before and last_end == 0 and (f.lead == "start" or (f.lead == "unit" and named and named[0][2] == f.units[0]
                                                                           and not clause[:named[0][0]].strip(" ,"))):
                        f.listed = list(before)                 # "Engine 61, Ladder 31 on location": who gets it, _read
                    out.append(f)
                    last_end = e
        return out

    @staticmethod
    def _only_units(clause: str, named) -> list[str] | None:
        """The units of ours a clause is nothing but ("Engine 61", "23 91 and Ladder 31"), else None."""
        if not named:
            return None
        rest = "".join(clause[a:b] for a, b in zip([0] + [e for _, e, _ in named], [s for s, _, _ in named] + [len(clause)]))
        return [c for _, _, c in named] if re.fullmatch(r"[\s,.;&-]*(?:(?:and|&)[\s,]*)*", rest, re.I) else None

    def _one(self, clause: str, s: int, e: int, intent: str, named, since: int) -> Found | None:
        before, after = clause[since:s], clause[e:]
        if GUARD.search(before) or SOON_AFTER.match(after):
            return None
        lead = self._lead(clause, since, s, named)
        if lead is None:
            return None
        if intent == "free" and re.match(r"clear", clause[s:e], re.I):
            qualified = bool(CLEAR_QUALIFIED.match(after))
            if CLEAR_NOT.match(after) and not qualified:
                return None
            if lead[0] not in ("unit", "first") and not qualified:
                return None                                 # "Clear." alone: a room, a search, the channel...
            if re.search(r"(?<![\w'])(?:all|loud\s+and|search|primary|secondary)\s*$", clause[since:s], re.I):
                return None
        if NEAR.match(clause[s:e]) and lead[0] not in ("unit", "first", "number", "start"):
            return None                                     # "lots of smoke in the area": a place, not a unit's status
        if intent == "staging" and lead[0] not in ("unit", "first", "number"):
            return None                                     # "Staging on King Street": where staging is, not who is in it
        said = " ".join(clause.strip(" ,").split())
        number = lead[1][0] if lead[0] == "number" else ""
        return Found(intent, " ".join(clause[s:e].split()).lower(), said[:SAID_MAX], [] if number else lead[1],
                     bool(EXPLICIT.search(clause[since:e])), lead[0], number)

    def _lead(self, clause: str, since: int, s: int, named) -> tuple[str, list[str]] | None:
        """Who the words right before the phrase say it is: ('unit', [codes]) for units of ours named there (a run:
        "Engine 62 and Engine 64 on location"; not one being called, "to Engine 64"), ('first', []) for "we" / "I",
        ('start', []) for nothing, ('control', []) right after "60 Control"; None for anyone else ("PD", "the hydrant",
        "Engine 191", "all units")."""
        head = clause[since:s]
        stripped = FILLER_END.sub("", head)
        first = bool(FIRST_PERSON.search(head[len(stripped):]))
        rest_end = since + len(stripped.rstrip(" ,-"))
        if not clause[since:rest_end].strip():
            return ("first", []) if first else ("start", [])
        before = [(a, b, c) for a, b, c in named if since <= a and b <= rest_end]
        if before and before[-1][1] == rest_end:
            run, end = [], rest_end
            for a, b, c in reversed(before):
                if not re.fullmatch(r"[\s,]*(?:(?:and|&)[\s,]*)?", clause[b:end], re.I) or TO_BEFORE.search(clause[since:a]):
                    break
                if run and not re.search(r"(?:and|&)", clause[b:end], re.I):
                    break                   # "23 91 Engine 61 responding": the one called, then who is talking
                run.insert(0, c)
                end = a
            return ("unit", run) if run else None
        if CONTROL_END.search(clause[since:rest_end]):
            return ("first", []) if first else ("control", [])
        if m := re.fullmatch(r"\s*(?:the\s+)?(\d{2,4})", clause[since:rest_end]):
            return ("number", [m.group(1)])                 # "64 on scene": a unit by its number alone (the talker's?)
        return None


def _hms(t: float) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S")


class StatusHints:
    """The suggestions: consider(traffic_id) when a transcript arrives, active() for the console, accept / dismiss."""

    def __init__(self, store, lists, channels: tuple[str, ...] = ("FG1",), talkgroups: tuple[int, ...] = (307, 333),
                 log=print):
        self.store, self.log = store, log
        self._lists = lists if callable(lists) else (lambda: lists)
        self.default_channels, self.default_tgs = tuple(channels), tuple(talkgroups)
        self._for = None
        self._lock = threading.Lock()
        with store.lock:
            store.db.executescript(SCHEMA)

    def _refresh(self) -> None:
        L = self._lists()
        if L is not self._for:
            try:
                self.cfg = load_hint_cfg(L, self.default_channels, self.default_tgs)
            except ListsError as e:
                self.log(f"CAD: status hints off: {e}")
                self.cfg = Cfg(False)
            self.phrases = Phrases(self.cfg.units, {re.sub(r"\D", "", str(n)) for c, u in L.units.items()
                                                    for n in (c, (u.get("spoken") if isinstance(u, dict) else "") or "")
                                                    if re.fullmatch(r"[\d\s-]+", str(n).strip() or "x")})
            self._for = L

    def decorate(self, rows: list[dict]) -> list[dict]:
        """Each row on our fire channels whose status words were put back (statusrepair.py): "fixed_text" (shown) and
        "fixes" [{heard, to}] (on hover); the row's own text stays what speech-to-text wrote."""
        self._refresh()
        if not self.cfg.enabled:
            return rows
        dispatched: dict[str, bool] = {}
        for r in rows:
            if r.get("kind", "radio") != "radio" or not r.get("text"):
                continue
            if r.get("channel") in self.cfg.channels or (r.get("talkgroup") is not None and r["talkgroup"] in self.cfg.talkgroups):
                rigs = [x.strip() for x in (r.get("rig") or "").split(",") if x.strip()]
                talker = rigs[0] if len(rigs) == 1 and rigs[0] in self.phrases.codes else ""
                heard_fine: set[str] = set()
                if FINE_WORD.search(r["text"]):  # "is fine" shown as en route when it was taken so when heard, or now
                    heard_fine = {h["unit"] for h in self.store.q("SELECT unit, phrase FROM status_hints WHERE traffic_id = ?",
                                                                  (r.get("id"),)) if FINE_WORD.search(h["phrase"])}

                def ok(code, heard_fine=heard_fine):
                    if code not in dispatched:
                        dispatched[code] = self.fine_ok(code)
                    return code in heard_fine or dispatched[code]
                fixed, fixes = self.phrases.repair(r["text"], talker, ok, bool(r.get("dispatcher")))
                if fixes:
                    r["fixed_text"], r["fixes"] = fixed, fixes
        return rows

    # -- what a status phrase means, in this department's codes ----------------------------------------------------

    def status_for(self, intent: str) -> str:
        L = self._lists()
        live = [s for s in L.unit_statuses if not s.get("retired")]
        if intent == "command":
            return L.command_status
        if intent == "staging":
            return next((s["code"] for s in live if re.match(r"stag", s.get("name") or "", re.I) or s["code"] == "ST"), "")
        if intent == "quarters":
            return next((s["code"] for s in live if s.get("role") == "free" and re.search(r"quarters", s.get("name") or "", re.I)),
                        L.status_for("free"))
        role = {"enroute": "enroute", "onscene": "onscene", "free": "free", "oos": "oos"}[intent]
        if role == "free":
            return L.status_for("free")
        return next((s["code"] for s in live if s.get("role") == role and s["code"] != L.command_status), "")

    def _rank(self, status: str) -> int | None:
        L = self._lists()
        if status and status == L.command_status:
            return RANK["command"]
        if status and status == self.status_for("staging"):
            return RANK["staging"]
        return RANK.get(L.role(status))

    # -- heard ---------------------------------------------------------------------------------------------------

    def consider(self, tid: str) -> list[dict]:
        """A transmission's transcript (new, or a re-run's new text): its suggestions, made or brought up to date.
        Returns the pending ones made for it."""
        self._refresh()
        if not self.cfg.enabled:
            return []
        t = self.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        if t is None:
            return []
        now = self.store.clock()
        ours_channel = t["channel"] in self.cfg.channels or (t["talkgroup"] is not None and t["talkgroup"] in self.cfg.talkgroups)
        want: dict[str, tuple[Found, str]] = {}
        if ours_channel and now - t["start"] < self.cfg.expire_s and (t["text"] or "").strip():
            want = self._read(t, self.fine_ok)
        made = []
        with self._lock:
            old = {r["unit"]: r for r in self.store.q("SELECT * FROM status_hints WHERE traffic_id = ?", (tid,))}
            for code, r in old.items():           # a re-run that no longer says it: withdrawn
                if r["outcome"] == "pending" and code not in want:
                    self._end(r["id"], "withdrawn")
            for code, (f, who) in want.items():
                h = self._make(t, code, f, who, old.get(code), now)
                if h:
                    made.append(h)
        return made

    def _read(self, t: dict, fine_ok=None) -> dict[str, tuple[Found, str]]:
        """unit code -> (what was said of it, who: named | talker | caller), the last phrase for each unit.

        Who gets a status (the radio format is "<addressee>, <who is talking> <status>"):
          60 Control: the units it names right before the status ("Engine 61, be advised Ladder 31 on location": L31);
            the one called too only in a pure readback, a time after it or an acknowledgement ("Engine 61, Ladder 31,
            en route 1412"; "Engine 61, on location" alone may be a question: nobody)
          a list the talker is in ("23 91, Engine 61, Ladder 31 on location" on 2391's radio): each of them
          otherwise the units right before the status ("Engine 61 on location"), else the talker (or the caller);
          the leading "<callsign>," is the one called, never one that gets the status
          "in the area" (and the like): only the talker's own"""
        rigs = [r.strip() for r in (t["rig"] or "").split(",") if r.strip()]
        talker = rigs[0] if len(rigs) == 1 and rigs[0] in self.phrases.codes else ""
        disp = bool(t["dispatcher"])
        text, fixes = self.phrases.repair(t["text"], talker, fine_ok, disp)
        known_other = disp or (bool(rigs) and not talker)
        caller = "" if known_other or talker else self.phrases.caller(text)
        out: dict[str, tuple[Found, str]] = {}
        for f in self.phrases.find(text):
            heard = [x["heard"] for x in fixes if x.get("at") == f.at]
            if heard:                              # the status words as heard: kept with the suggestion, for audit
                f.phrase = f"{f.phrase} (heard “{heard[0]}”)"
            group = list(dict.fromkeys(f.listed + f.units))
            if talker and talker in f.addressee:
                group = list(dict.fromkeys(f.addressee + group))   # a chief naming himself first
            if f.near:                             # an arrival nearby: only the talker telling its own (never Control)
                if talker and (talker in group or (f.lead in ("start", "first") and not group)
                               or (f.lead == "number" and self.phrases.own_number(talker, f.number))):
                    for u in (group if talker in group else [talker]):
                        out[u] = (f, "talker" if u == talker else "named")
                continue
            if disp:                               # 60 Control: the units it names right before the status ...
                if f.units:
                    for u in group:
                        out[u] = (f, "named")
                elif f.lead in ("start", "control") and (f.addressee or group) and (f.timed or ACK.search(text)):
                    for u in dict.fromkeys(f.addressee + group):   # ... or a pure readback: "Engine 61, en route 1412"
                        out[u] = (f, "named")
                continue
            if talker and talker in group:         # a list the talker is in: one status for each
                for u in group:
                    out[u] = (f, "talker" if u == talker else "named")
            elif f.units:
                for u in f.units:
                    out[u] = (f, "named")
            elif f.lead == "number":               # its number alone: only the talker's own (its radio says who)
                if talker and self.phrases.own_number(talker, f.number):
                    out[talker] = (f, "talker")
            elif f.lead in ("start", "first", "control"):
                if talker:
                    out[talker] = (f, "talker")
                elif caller:
                    out[caller] = (f, "caller")
        return out

    def fine_ok(self, code: str) -> bool:
        """May "<code> is fine" be its en route: dispatched now on an open call (a welfare "fine" is said on scene)."""
        L = self._lists()
        u = self.store.one("SELECT u.status FROM units u JOIN assignments a ON a.unit_id = u.id AND a.cleared_at IS NULL "
                           "JOIN incidents i ON i.id = a.incident_id AND i.status IN ('pending', 'active') WHERE u.name = ?",
                           (code,))
        return u is not None and L.role(u["status"]) == "dispatched"

    def _make(self, t: dict, code: str, f: Found, who: str, old: dict | None, now: float) -> dict | None:
        status = self.status_for(f.intent)
        if not status:
            return None
        if old is not None:
            if old["outcome"] != "pending" or (old["status"] == status):
                return None                        # dismissed / accepted / ended stays so; the same: nothing new
            self._end(old["id"], "withdrawn")      # a re-run that says something else: the new one replaces it
        u = self.store.one("SELECT u.id, u.name, u.status, u.status_at, a.incident_id FROM units u LEFT JOIN assignments a "
                           "ON a.unit_id = u.id AND a.cleared_at IS NULL WHERE u.name = ?", (code,))
        if u is None:
            return None
        L = self._lists()
        cur, cur_role = u["status"], L.role(u["status"])
        if cur == status or (f.intent == "free" and cur_role == "free"):
            return None                            # already so
        if u["status_at"] >= t["start"]:
            return None                            # set since the transmission was made: the desk already has it
        new_rank, cur_rank = self._rank(status), self._rank(cur)
        if new_rank is not None and cur_rank is not None and new_rank < cur_rank and not f.explicit:
            return None                            # backwards (responding after on location), not said so
        iid = u["incident_id"]
        if iid is None:
            linked = self.store.q("SELECT DISTINCT i.id FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                                  "WHERE l.traffic_id = ? AND i.status IN ('pending', 'active')", (t["id"],))
            iid = linked[0]["id"] if len(linked) == 1 else None
        if iid is None and t["talkgroup"] is None and t["channel"] in self.default_channels:
            # FG1 with exactly one call running on it: that call's (a suggestion a person takes or not; the
            # transmission itself is not put on the call without its radio's ID, comms.py #163)
            from sizeup.cad.comms import Comms
            active = self.store.q(Comms.ACTIVE_EVENTS, (t["start"], t["start"]))
            iid = active[0]["incident_id"] if len(active) == 1 else None
        if iid is None and f.intent in NEEDS_CALL:
            return None
        if self.store.one("SELECT 1 FROM status_hints WHERE unit = ? AND status = ? AND outcome = 'pending' "
                          "AND expires_at > ? AND created_at >= ?", (code, status, now, u["status_at"])):
            return None                            # already suggested (60 Control repeating it back)
        with self.store.tx() as db:
            db.execute("UPDATE status_hints SET outcome = 'superseded', decided_at = ? WHERE unit = ? AND outcome = 'pending'",
                       (now, code))
            db.execute("INSERT OR REPLACE INTO status_hints (traffic_id, unit, unit_id, status, intent, phrase, said, text, who, "
                       "channel, talkgroup, heard_at, incident_id, prev_status, created_at, expires_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (t["id"], code, u["id"], status, f.intent, f.phrase, f.said, t["text"], who, t["channel"],
                        t["talkgroup"], t["start"], iid, cur, now, t["start"] + self.cfg.expire_s))
        self.log(f"CAD: radio suggests {code} {L.status_phrase(status)} ({who}: “{f.said}”)")
        return self.store.one("SELECT * FROM status_hints WHERE traffic_id = ? AND unit = ?", (t["id"], code))

    def _end(self, hid: int, outcome: str, operator: str = "") -> None:
        with self.store.tx() as db:
            db.execute("UPDATE status_hints SET outcome = ?, decided_at = ?, operator = ? WHERE id = ? AND outcome = 'pending'",
                       (outcome, self.store.clock(), operator, hid))

    # -- shown ---------------------------------------------------------------------------------------------------

    def sweep(self, now: float | None = None) -> None:
        """Pending suggestions past their time (expired) or whose unit's status changed since (stale): ended."""
        now = self.store.clock() if now is None else now
        rows = self.store.q("SELECT h.id, h.expires_at, h.created_at, u.status_at FROM status_hints h JOIN units u ON u.id = h.unit_id "
                            "WHERE h.outcome = 'pending'")
        gone = [(("expired" if r["expires_at"] <= now else "stale"), r["id"]) for r in rows
                if r["expires_at"] <= now or r["status_at"] > r["created_at"]]
        if gone:
            with self.store.tx() as db:
                db.executemany("UPDATE status_hints SET outcome = ?, decided_at = ? WHERE id = ? AND outcome = 'pending'",
                               [(o, now, i) for o, i in gone])

    def active(self) -> list[dict]:
        """The suggestions to show now, newest first: {id, unit, unit_id, status, status_name, incident_id,
        incident_number, onto (accepting puts the unit on that event), traffic_id, heard_at, said, text, phrase,
        channel, who, expires_at}."""
        self.sweep()
        L = self._lists()
        rows = self.store.q("SELECT h.*, i.number AS incident_number, a.incident_id AS on_incident FROM status_hints h "
                            "LEFT JOIN incidents i ON i.id = h.incident_id "
                            "LEFT JOIN assignments a ON a.unit_id = h.unit_id AND a.cleared_at IS NULL "
                            "WHERE h.outcome = 'pending' ORDER BY h.heard_at DESC, h.id DESC")
        out = []
        for r in rows:
            role = L.role(r["status"])
            out.append({"id": r["id"], "unit": r["unit"], "unit_id": r["unit_id"], "status": r["status"],
                        "status_name": L.status_phrase(r["status"]), "incident_id": r["incident_id"],
                        "incident_number": r["incident_number"] or "",
                        "onto": bool(r["incident_id"] and r["on_incident"] != r["incident_id"] and role not in ("free", "oos")),
                        "traffic_id": r["traffic_id"], "heard_at": r["heard_at"], "said": r["said"], "text": r["text"],
                        "phrase": r["phrase"], "channel": r["channel"], "who": r["who"], "expires_at": r["expires_at"]})
        return out

    # -- decided -------------------------------------------------------------------------------------------------

    def _pending(self, hid: int) -> dict:
        self.sweep()
        h = self.store.one("SELECT * FROM status_hints WHERE id = ?", (hid,))
        if h is None:
            raise CadError(f"no suggestion {hid}")
        if h["outcome"] != "pending":
            raise CadError({"accepted": "already accepted", "dismissed": "already dismissed",
                            "expired": "this suggestion expired", "stale": f"{h['unit']}'s status changed since",
                            "superseded": f"a newer suggestion for {h['unit']} replaced it",
                            "withdrawn": "the transcript no longer says it"}.get(h["outcome"], h["outcome"]))
        return h

    def accept(self, hid: int, operator: str = "") -> dict:
        """✓: the unit's status set as suggested (on the event it is for: put on it when it isn't), with where it came
        from in the chronology: "E64 on location. Status from radio (heard 14:32:10 “Engine 64 on location”)"."""
        with self._lock:
            h = self._pending(hid)
            why = f"Status from radio (heard {_hms(h['heard_at'])} “{h['said']}”)"
            L = self._lists()
            u = self.store.unit(h["unit_id"])
            if h["incident_id"] and u["incident_id"] != h["incident_id"] and L.role(h["status"]) not in ("free", "oos"):
                out = self.store.set_unit_status_on(h["unit_id"], h["status"], h["incident_id"], operator, why=why)
            else:
                out = self.store.set_unit_status(h["unit_id"], h["status"], operator, why=why)
            with self.store.tx() as db:
                db.execute("UPDATE status_hints SET outcome = 'accepted', decided_at = ?, operator = ? WHERE id = ?",
                           (self.store.clock(), operator, hid))
        self.log(f"CAD: {h['unit']} {L.status_phrase(h['status'])} from the radio, accepted" + (f" by {operator}" if operator else ""))
        return dict(out, hint=hid, accepted=True)

    def dismiss(self, hid: int, operator: str = "") -> dict:
        """✕: not so. Remembered: the same transmission never suggests it again (a re-run of its text included)."""
        with self._lock:
            h = self._pending(hid)
            self._end(hid, "dismissed", operator)
        return {"hint": hid, "dismissed": True, "unit": h["unit"], "status": h["status"]}

    def csv(self) -> bytes:
        """Every suggestion kept and how it ended (the precision log)."""
        rows = self.store.q("SELECT h.*, i.number FROM status_hints h LEFT JOIN incidents i ON i.id = h.incident_id "
                            "ORDER BY h.heard_at, h.id")
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(CSV_COLUMNS)
        stamp = lambda t: datetime.fromtimestamp(t, TZ).strftime("%m/%d/%Y %H:%M:%S") if t else ""
        for r in rows:
            w.writerow([stamp(r["heard_at"]), r["unit"], r["status"], r["prev_status"], r["phrase"], r["said"], r["text"], r["who"],
                        r["channel"], r["number"] or "", r["outcome"], stamp(r["decided_at"]), r["operator"], r["traffic_id"]])
        return buf.getvalue().encode("utf-8-sig")
