"""Unit statuses suggested from what is heard on the radio (GitHub #37): "E64 on location?" when Engine 64 says
"on location" on FG1 or Fire/EMS 15. A suggestion, never a status: nothing changes until a dispatcher taps ✓
(POST /api/cad/hint/<id>/accept); ✕ dismisses it (remembered: the same transmission never suggests it again).

    heard     a transcript arrives (Comms.on_text: a new transmission, or a re-run's new text) on one of our fire
              channels (FG1, Fire/EMS 15, the Airport Incident talkgroup; the data file's "status_hints" changes them)
    phrase    PHRASES below: what is said -> what it means (responding, on location, staging, available, in quarters,
              out of service, incident command); a phrase is skipped when it is negated ("not on location yet"), a
              question ("is Engine 64 responding?"), a condition ("advise when on location"), an order ("go to
              staging"), a time to come ("on location in 2 minutes"), or said of someone else ("PD is on location")
    who       a unit of ours (PCFD's and Rye Brook's fire units, the chiefs 239x: sizeup/cad/feed.py load_cfg) named right
              before the phrase ("Engine 64 on location", "60 Control copies, 2391 responding"), else the talker when
              its radio is one of ours (the registry's rig), else the unit that called ("Engine 64 to 60 Control, on
              location"); nobody of ours: no suggestion
    not       the unit's status already is that (or, for available, any available status); a step backwards
              (responding after on location) unless said so ("now", "again", "back"); on location, staging or command
              for a unit on no call with no call on the channel; a status set after the transmission was made
    where     the event the unit is on, else the one open event the transmission is linked to (comms.py: the FG1
              channel rule, the Airport talkgroup); accepting then puts the unit on it
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
PHRASES: list[tuple[str, re.Pattern]] = [(i, re.compile(p, re.I)) for i, p in (
    ("enroute", r"\bresponding\b(?!\s+(?:units?|compan(?:y|ies)|apparatus|crews?|members|personnel|to\s+(?:the\s+)?"
                r"(?:hospital|ER)\b))"),
    ("enroute", rf"\b{ROUTE}\b(?!\s*\d)"),               # not "in Route 1"
    ("onscene", r"\bon[\s-](?:the\s+)?(?:location|scene)\b"),
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
    lead: str = ""                   # unit | first | start | control


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

    def __init__(self, units: dict[str, str]):
        self.units = [(c, re.compile(r"(?<![\w])(?:" + "|".join(unit_forms(c, sp)) + r")(?![\w])", re.I))
                      for c, sp in units.items()]
        self.codes = set(units)

    def named(self, text: str) -> list[tuple[int, int, str]]:
        """(start, end, code) of every unit of ours the text names, in order (the longest at a place)."""
        spans = sorted(((m.start(), m.end(), c) for c, rx in self.units for m in rx.finditer(text)), key=lambda s: (s[0], -s[1]))
        out: list[tuple[int, int, str]] = []
        for s in spans:
            if not out or s[0] >= out[-1][1]:
                out.append(s)
        return out

    def caller(self, text: str) -> str:
        """The unit of ours that called ("Engine 64 to 60 Control, ..."): the first named, unless it is the one called."""
        named = self.named(text)
        return named[0][2] if named and not TO_BEFORE.search(text[:named[0][0]]) else ""

    def find(self, text: str) -> list[Found]:
        out: list[Found] = []
        for m in CLAUSE.finditer(text or ""):
            clause = m.group(0)
            body = clause.rstrip()
            if not body.strip() or body.endswith("?") or QUESTION_START.match(body):
                continue
            named = self.named(clause)
            hits = sorted(((x.start(), x.end(), i) for i, rx in PHRASES for x in rx.finditer(clause)), key=lambda h: (h[0], -h[1]))
            taken, last_end = [], 0
            for s, e, intent in hits:
                if taken and s < taken[-1][1]:
                    continue                                    # inside a longer phrase already taken
                taken.append((s, e, intent))
                f = self._one(clause, s, e, intent, named, last_end)
                if f is not None:
                    out.append(f)
                    last_end = e
        return out

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
        if intent == "staging" and lead[0] not in ("unit", "first"):
            return None                                     # "Staging on King Street": where staging is, not who is in it
        said = " ".join(clause.strip(" ,").split())
        return Found(intent, " ".join(clause[s:e].split()).lower(), said[:SAID_MAX], lead[1],
                     bool(EXPLICIT.search(clause[since:e])), lead[0])

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
                run.insert(0, c)
                end = a
            return ("unit", run) if run else None
        if CONTROL_END.search(clause[since:rest_end]):
            return ("first", []) if first else ("control", [])
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
            self.phrases = Phrases(self.cfg.units)
            self._for = L

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
            want = self._read(t)
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

    def _read(self, t: dict) -> dict[str, tuple[Found, str]]:
        """unit code -> (what was said of it, who: named | talker | caller), the last phrase for each unit."""
        text = t["text"]
        rigs = [r.strip() for r in (t["rig"] or "").split(",") if r.strip()]
        talker = rigs[0] if len(rigs) == 1 and rigs[0] in self.phrases.codes else ""
        known_other = bool(t["dispatcher"]) or (bool(rigs) and not talker)
        caller = "" if known_other or talker else self.phrases.caller(text)
        out: dict[str, tuple[Found, str]] = {}
        for f in self.phrases.find(text):
            if f.units:
                for u in f.units:
                    out[u] = (f, "named")
            elif f.lead in ("start", "first", "control"):
                if talker:
                    out[talker] = (f, "talker")
                elif caller:
                    out[caller] = (f, "caller")
        return out

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
