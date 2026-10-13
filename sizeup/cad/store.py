"""CAD store: PCFD's own events, units, run cards and dispatch log in one SQLite file.

    state/cad.sqlite        (not under captures/: the janitor there deletes old files)
      units         the two status boards (codes from data/cad_lists.json: E60, L31, 2398, 77A1...)
      incidents     the sheet's events, numbered YY-NNNNNN (26-000001) per local year:
                      queued    from a 60 Control page, waiting to be accepted (no number yet)
                      pending   accepted / entered, no units yet
                      active    units on it
                      closed    with a DISPOSITION;  dismissed: a queued page nobody took
                    emergency = 1: PENDING EVENTS -> ACTIVE EVENTS (run card, dispatch);
                    emergency = 0: NON-EMERGENCY EVENTS (notifications, road closures, hydrants:
                    logged and tracked until closed, units attached but never dispatched)
      assignments   unit x incident, with dispatched / en route / on scene / cleared times
      run_cards     TYPE or TYPE/SUBTYPE -> ordered slots ("3 x engine", "2398")
      events        the chronology (and unit status changes with no incident)
      unit_history  every unit status change: old -> new, event, operator, why (out of service)
      dispatches    what was (or, transmit off, would have been) sent, with its audio
      radios        radio ID -> rig (sizeup/cad/radios.py)
      settings_audit every change made in Settings (who, section, key, old -> new)
      radio_traffic every transcribed transmission, radio_links: which events it belongs to
                    (sizeup/cad/comms.py), radio_link_exclusions: auto links a person took off
      meta          small settings (the radio seed version)
      settings_overrides  the Settings page's edits, laid over data/cad_lists.json (sizeup/cad/settings.py)
      operators           the people at the console; on the hub behind Cloudflare Access, keyed by the
                          email they signed in with (sizeup/cloud/access.py, Settings._person); when each
                          was last seen and from where (last_seen / last_ip / last_ua)
      user_activity       each signed-in person's history on the hub: sign-ins, writes, refusals, kept
                          90 days (sizeup/cad/activity.py)
      prealerts           phone pre-alerts from Tapout's ntfy feed and how each ended (sizeup/prealert.py,
                          made there; its events: kind 'prealert', in the event log, not the unit log)

The lists (types, subtypes, dispositions, unit statuses and what each one means) come from
sizeup/cad/lists.py. One connection, one lock: the web server is threaded and SQLite writes are
short. The schema version is PRAGMA user_version; _migrate() brings an older file up in place.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sizeup.cad import lists as lists_mod
from sizeup.cad.lists import Lists

TZ = ZoneInfo("America/New_York")
# "Name <email>" when the operator signed in through Cloudflare Access (sizeup/cloud/access.py).
OPERATOR_MAX = 320
SCHEMA_VERSION = 10        # 6-9 unused (other branches); v10: the department's own codes

OPEN = ("queued", "pending", "active")
SOURCES = ("manual", "60 Control page")
ON_TIMES = {"dispatched": "dispatched_at", "enroute": "enroute_at", "onscene": "onscene_at"}

SCHEMA = """
CREATE TABLE units (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,            -- the board code: "E60", "2398", "77A1", "CONED-G"
  kind TEXT NOT NULL,                   -- engine | ladder | tower | rescue | ... (cad_lists.json unit_kinds)
  station TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                 -- one of cad_lists.json unit_statuses
  status_at REAL NOT NULL,
  out_of_service TEXT NOT NULL DEFAULT '',   -- why, while out of service
  spare INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,      -- board order: recommendation picks in this order
  spoken TEXT NOT NULL DEFAULT '',      -- as the dispatch voice says it: "Engine 60", "23 98"
  title TEXT NOT NULL DEFAULT '',       -- "Duty Officer"
  board TEXT NOT NULL DEFAULT '',       -- main | other ('' = no longer on a board)
  department TEXT NOT NULL DEFAULT '',
  mutual_aid INTEGER NOT NULL DEFAULT 0 -- another department's (not joint response): never picked by kind
);
CREATE TABLE incidents (
  id INTEGER PRIMARY KEY,
  number TEXT UNIQUE,                   -- 26-000001; NULL while queued (dismissed pages never get one)
  type TEXT NOT NULL DEFAULT '',        -- EVENT TYPE ('' only while queued and the page didn't say)
  subtype TEXT NOT NULL DEFAULT '',     -- EVENT SUBTYPE
  type_note TEXT NOT NULL DEFAULT '',   -- why the type/subtype is blank or unsure (from a page)
  emergency INTEGER NOT NULL DEFAULT 1, -- 0: a non-emergency event (notification, closure): no dispatch
  received_via TEXT NOT NULL DEFAULT '',   -- PHONE | WALK-IN | RADIO | 60 CONTROL | ... (cad_lists.json)
  until_at REAL,                        -- a closure / hydrant out of service: until when (optional)
  priority INTEGER,
  address TEXT NOT NULL DEFAULT '',     -- LOCATION
  cross_street TEXT NOT NULL DEFAULT '',
  lat REAL, lon REAL, geo_label TEXT NOT NULL DEFAULT '',
  caller_name TEXT NOT NULL DEFAULT '', -- NAME
  caller_address TEXT NOT NULL DEFAULT '',   -- ADDRESS (the caller's)
  phone TEXT NOT NULL DEFAULT '',
  remarks TEXT NOT NULL DEFAULT '',
  disposition TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,                 -- manual | 60 Control page
  source_ref TEXT UNIQUE,               -- the monitor incident id (dedupe)
  status TEXT NOT NULL,                 -- queued | pending | active | closed | dismissed
  created_at REAL NOT NULL, accepted_at REAL, closed_at REAL
);
CREATE TABLE seq (year INTEGER PRIMARY KEY, last INTEGER NOT NULL);
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  unit_id INTEGER NOT NULL REFERENCES units(id),
  dispatched_at REAL, enroute_at REAL, onscene_at REAL, cleared_at REAL,
  assigned_at REAL                      -- put on the event (dispatched or attached): its radio traffic counts from here
);
CREATE INDEX assignments_open ON assignments(unit_id) WHERE cleared_at IS NULL;
CREATE TABLE run_cards (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL UNIQUE,            -- "STRU" or "ALARM/COMM"
  spoken TEXT NOT NULL DEFAULT '',      -- '' = the data file's phrase for the type
  is_default INTEGER NOT NULL DEFAULT 0,   -- seeded stand-in, not yet edited to PCFD policy
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE run_card_slots (
  card_id INTEGER NOT NULL REFERENCES run_cards(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT '',        -- "engine", "ladder|tower" (either), "any"; "all": the full department
  count INTEGER NOT NULL DEFAULT 1,
  unit TEXT NOT NULL DEFAULT ''         -- or one named unit ("2398"); kind "all": the department ("Port Chester FD")
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER REFERENCES incidents(id),   -- NULL: a unit status change off any incident
  unit_id INTEGER REFERENCES units(id),
  at REAL NOT NULL,
  kind TEXT NOT NULL,                   -- created | system | accepted | dismissed | note | dispatch | remarks | tones | attach | status | closed | reopened | edit | prealert
  text TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT '',
  units TEXT NOT NULL DEFAULT '',       -- a note's unit tags ("E63,L31": who said it), typed with the unit autocomplete
  places TEXT NOT NULL DEFAULT ''       -- a note's "@" places: JSON [{label, lat, lon, kind, sub, source}] (#67); '' none
);
CREATE INDEX events_incident ON events(incident_id);
CREATE TABLE unit_history (           -- every unit status change (the sheet's Unit History)
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  old_status TEXT NOT NULL,
  new_status TEXT NOT NULL,
  incident_id INTEGER REFERENCES incidents(id),
  operator TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT ''         -- why: out of service / unmanned
);
CREATE TABLE dispatches (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  at REAL NOT NULL,
  operator TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'dispatch', -- dispatch | add-on | upgrade
  text TEXT NOT NULL,
  unit_ids TEXT NOT NULL DEFAULT '[]',
  audio_path TEXT NOT NULL DEFAULT '',
  audio_note TEXT NOT NULL DEFAULT '',  -- why there's no audio (no `say` on this machine, ...)
  transmitted INTEGER NOT NULL DEFAULT 0,
  channel TEXT NOT NULL DEFAULT '',
  tx_note TEXT NOT NULL DEFAULT ''
);
""" + """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS radios (
  system TEXT NOT NULL,                 -- mdc (FG1 MDC-1200, 4 hex digits) | p25 (county radio number)
  radio_id TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '',        -- the board code it's on ('' = no rig / not known)
  label TEXT NOT NULL DEFAULT '',       -- "Portable 1", "Mobile", the P25 alias
  source TEXT NOT NULL,                 -- confirmed | guessed | heard
  alias TEXT NOT NULL DEFAULT '',       -- P25: the talker alias the system sends with it
  notes TEXT NOT NULL DEFAULT '',
  edited INTEGER NOT NULL DEFAULT 0,    -- a person changed it: what is heard never overwrites it
  first_heard REAL, last_heard REAL, heard INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (system, radio_id)
);
-- every change made in Settings: who, which section, what key, old -> new (shared with the settings branch: same shape)
CREATE TABLE IF NOT EXISTS settings_audit (id INTEGER PRIMARY KEY, at REAL NOT NULL, operator TEXT, section TEXT NOT NULL, key TEXT, old TEXT, new TEXT);
CREATE TABLE IF NOT EXISTS radio_traffic (
  id TEXT PRIMARY KEY,                  -- the monitor's clip id (/api/audio/<id>.wav while the audio is kept)
  call_id TEXT NOT NULL DEFAULT '',     -- P25: the call it was cut from, and the talker turn
  turn INTEGER,
  start REAL NOT NULL, end REAL NOT NULL,
  channel TEXT NOT NULL,                -- FG1, Fire/EMS 15, South 2 ...
  talkgroup INTEGER,
  talker TEXT NOT NULL DEFAULT '',      -- as heard: MDC alias / P25 alias
  radio_id TEXT NOT NULL DEFAULT '',    -- raw ID(s)
  rig TEXT NOT NULL DEFAULT '',         -- unit code(s), comma-separated
  dispatcher INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS radio_traffic_start ON radio_traffic(start);
CREATE INDEX IF NOT EXISTS radio_traffic_call ON radio_traffic(call_id);
CREATE TABLE IF NOT EXISTS radio_links (
  traffic_id TEXT NOT NULL REFERENCES radio_traffic(id) ON DELETE CASCADE,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  why TEXT NOT NULL,                    -- unit | channel | dispatcher | manual | page
  unit TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (traffic_id, incident_id)
);
CREATE INDEX IF NOT EXISTS radio_links_incident ON radio_links(incident_id);
"""
RADIO_SCHEMA = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS meta"):]

# v5: Settings. Every statement can run again (IF NOT EXISTS): the radio-ID work (v4) creates the
# same settings_audit table, and either can come first.
SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings_audit (id INTEGER PRIMARY KEY, at REAL NOT NULL, operator TEXT, section TEXT NOT NULL, key TEXT, old TEXT, new TEXT);
CREATE INDEX IF NOT EXISTS settings_audit_section ON settings_audit(section, at);
CREATE TABLE IF NOT EXISTS settings_overrides (
  section TEXT NOT NULL,                -- event_types | units | dispatch_wording | ... (sizeup/cad/settings.py)
  key TEXT NOT NULL,                    -- a code ("STRU"), "_order", "_boards", a template name, "tones"
  value TEXT NOT NULL,                  -- JSON: the item as edited
  updated_at REAL NOT NULL,
  operator TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (section, key)
);
CREATE TABLE IF NOT EXISTS operators (  -- who works the console
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  role TEXT NOT NULL DEFAULT 'dispatcher',   -- admin | dispatcher | viewer (not enforced yet)
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  email TEXT NOT NULL DEFAULT '',            -- the Cloudflare Access sign-in, lower case ('' = none)
  callsign TEXT NOT NULL DEFAULT '',
  last_seen REAL,                            -- signed in, last request (at most a minute stale: activity.py)
  last_ip TEXT NOT NULL DEFAULT '',
  last_ua TEXT NOT NULL DEFAULT ''
);
"""

# Per-person activity on the hub (sizeup/cad/activity.py). Like operators.email: made on every open,
# not by a schema version (other branches hold v6-9), and doing it twice changes nothing.
ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_activity (
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  email TEXT NOT NULL,                  -- who: the verified Cloudflare Access email
  kind TEXT NOT NULL,                   -- signin | action | refused
  route TEXT NOT NULL DEFAULT '',       -- "POST /api/cad/incident/12/dispatch"
  summary TEXT NOT NULL DEFAULT '',     -- "Dispatched 26-000014: E60, L31"
  incident_number TEXT,                 -- 26-000014, when it was about one
  ok INTEGER NOT NULL DEFAULT 1,        -- 0: refused
  ip TEXT NOT NULL DEFAULT '',          -- Cf-Connecting-Ip
  ua TEXT NOT NULL DEFAULT '',          -- the browser (User-Agent, cut short)
  session TEXT NOT NULL DEFAULT ''      -- a sign-in's Access session (Identity.session): one row each
);
CREATE INDEX IF NOT EXISTS user_activity_email ON user_activity(email, at);
CREATE INDEX IF NOT EXISTS user_activity_at ON user_activity(at);
"""

# A transmission a person took off an event that the auto-linker had put on it (sizeup/cad/comms.py
# unlink): never linked to that event again by unit / channel / dispatcher; a manual add clears it. Made
# on every open, like user_activity (2026-10-08).
EXCLUSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_link_exclusions (
  traffic_id TEXT NOT NULL REFERENCES radio_traffic(id) ON DELETE CASCADE,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  at REAL NOT NULL,
  operator TEXT NOT NULL DEFAULT '',    -- who took it off
  why TEXT NOT NULL DEFAULT '',         -- the auto link it was: unit | channel | dispatcher
  unit TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (traffic_id, incident_id)
);
"""

# Schema v1 (before the sheet's vocabulary) -> v2. Old call types -> (TYPE, SUBTYPE).
V1_TYPES = {"Structure fire": ("STRU", ""), "Fire alarm – commercial": ("ALARM", "COMM"),
            "Fire alarm – residential": ("ALARM", "RESD"), "CO alarm": ("ALARM", ""),
            "Odor of gas inside": ("HAZARD", "GAS"), "Outside fire / brush": ("BRUSH", ""), "MVA": ("MVA", ""),
            "MVA with entrapment": ("MVA", "EXTRI"), "Wires down": ("HAZARD", "WIRES"),
            "Water / marine rescue": ("RESCUE", "WATER"), "Mutual aid request": ("MA", "")}
V1_STATUS_ROLES = {"available": "free", "available-in-quarters": "free", "dispatched": "dispatched",
                   "enroute": "enroute", "onscene": "onscene", "out_of_service": "oos"}   # transporting: still committed
V1_UNIT = re.compile(r"(Tower Ladder|Engine|Ladder|Rescue|Marine|Utility) (\d+)")
V1_PREFIX = {"Tower Ladder": "TL", "Engine": "E", "Ladder": "L", "Rescue": "R", "Marine": "M", "Utility": "U"}


def v1_unit_code(name: str) -> str:
    """'Engine 60' -> 'E60', '2398 Duty Officer' -> '2398'; anything else unchanged."""
    if m := V1_UNIT.fullmatch(name):
        return V1_PREFIX[m.group(1)] + m.group(2)
    if m := re.match(r"(\d{4})\b", name):
        return m.group(1)
    return name


# v10: the department's own unit status codes (its code sheet, 2026-10-07). UNMANNED stays.
V10_STATUS = {"AVAILABLE": "AV", "DISPATCHED": "DP", "DISPATCH OTHER": "DO", "ACKNOWLEDGED": "AK",
              "RESPONDING": "RS", "ON LOCATION": "OL", "OUT OF SERVICE": "OS"}
V10_RETIRED_UNITS = ("CODE_ENFORCE", "60CAD", "60CAD1", "60CAD2")     # not on the sheet: retired, history kept
V10_SAME_BOARDS = ("main",)            # the only board that means the same before and after v10


def v10_layout(old: list, boards, rows: dict) -> list[dict]:
    """A board layout saved in Settings (units/_boards) redone on the new boards. Only "main" means the
    same thing before and after (the old "agencies" and "other" boards are now EMS, the mutual-aid
    departments, the MA requests and agencies & resources), so: the main board keeps the old order of
    the units it had (in the places they hold); a unit the old layout had on main stays on main; a unit
    added in Settings goes to main if it was there, else to "agencies" (Other). Every other unit goes
    where the data file puts it. Applying it again changes nothing."""
    old_on = {}                                         # code -> (old board id, position)
    for b in old:
        if isinstance(b, dict):
            for i, c in enumerate(b.get("units") or []):
                if c:
                    old_on[c] = (str(b.get("id") or ""), i)
    new = [{"id": b["id"], "title": b["title"], "units": [c for c in b["units"] if c], **lists_mod.board_meta(b)}
           for b in boards]
    ids = {b["id"] for b in new}
    home = {c: b["id"] for b in new for c in b["units"]}
    for b in new:
        if b["id"] not in V10_SAME_BOARDS:
            continue
        mine = [c for c in b["units"] if old_on.get(c, ("",))[0] == b["id"]]
        it = iter(sorted(mine, key=lambda c: old_on[c][1]))
        b["units"] = [next(it) if c in mine else c for c in b["units"]]
    moved = []
    for c, (bid, i) in sorted(old_on.items(), key=lambda x: (x[1][0], x[1][1])):
        if c in V10_RETIRED_UNITS:
            continue
        same = bid if bid in V10_SAME_BOARDS and bid in ids else ""
        if c in home:
            if same and same != home[c]:
                moved.append((c, same))
            continue
        u = rows.get(("units", c))
        if isinstance(u, dict) and (u.get("deleted") or u.get("retired")):
            continue
        to = same or ("agencies" if "agencies" in ids else new[0]["id"])
        moved.append((c, to))
    for c, to in moved:
        for b in new:
            if c in b["units"]:
                b["units"].remove(c)
        next(b for b in new if b["id"] == to)["units"].append(c)
    return new


class CadError(ValueError):
    """A request that can't be done as asked (shown to the operator as the 400 reason)."""


def local_year(at: float) -> int:
    return datetime.fromtimestamp(at, TZ).year


def event_number(year: int, n: int) -> str:
    """The sheet's EVENT #: 2-digit year, dash, 6-digit sequence (26-000001)."""
    return f"{year % 100:02d}-{n:06d}"


class Store:
    def __init__(self, path: str | Path = ":memory:", clock=time.time, lists: Lists | None = None):
        self.path, self.clock = str(path), clock
        self.lists = lists or lists_mod.load()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        self.rev = 0          # bumped on every change: the page refetches nothing, it's pushed
        self.migrated: list[str] = []      # what a migration did, for the log
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            v = self.db.execute("PRAGMA user_version").fetchone()[0]
            if v > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path}: schema v{v} is newer than this sizeup (v{SCHEMA_VERSION})")
            if v == 0:
                self.db.executescript("BEGIN;" + SCHEMA + SETTINGS_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
                return self._operator_identity()
            if v < SCHEMA_VERSION:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    if v == 1:
                        self._v1_to_v2(self.db)
                    self._v2_to_v3(self.db)
                    self._v3_to_v4(self.db)
                    self._v4_to_v5(self.db)
                    self._v9_to_v10(self.db)
                    self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise
            self._operator_identity()

    def _operator_identity(self) -> None:
        """operators.email / callsign (Cloudflare Access sign-in, 2026-10-07), last_seen / ip / ua and
        user_activity (per-person activity, 2026-10-08). Checked on every open
        rather than a schema version (other branches hold v6-9): adding them twice changes nothing."""
        for stmt in (ACTIVITY_SCHEMA + EXCLUSION_SCHEMA).split(";"):   # per-person activity, removed links (2026-10-08)
            if stmt.strip():
                self.db.execute(stmt)
        ecols = {r["name"] for r in self.db.execute("PRAGMA table_info(events)")}
        if ecols and "places" not in ecols:              # a note's "@" places (#67), 2026-10-13: old notes keep ''
            self.db.execute("ALTER TABLE events ADD COLUMN places TEXT NOT NULL DEFAULT ''")
        tcols = {r["name"] for r in self.db.execute("PRAGMA table_info(radio_traffic)")}
        for col in ("alt_text", "alt_model"):            # the second transcript ("both": whisper next to AWS), 2026-10-08
            if tcols and col not in tcols:
                self.db.execute(f"ALTER TABLE radio_traffic ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        for col, decl in (("rerun_at", "REAL"), ("rerun_result", "TEXT NOT NULL DEFAULT ''")):   # re-transcribe's
            if tcols and col not in tcols:                                                        # result, 2026-10-08
                self.db.execute(f"ALTER TABLE radio_traffic ADD COLUMN {col} {decl}")
        for col, decl in (("tones", "TEXT NOT NULL DEFAULT ''"), ("voice_at", "REAL")):   # pages in the log (#64)
            if tcols and col not in tcols:
                self.db.execute(f"ALTER TABLE radio_traffic ADD COLUMN {col} {decl}")
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(operators)")}
        if not cols:
            return
        for col in ("email", "callsign"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE operators ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                self.migrated.append(f"operators.{col} (Cloudflare Access sign-in)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS operators_email ON operators(email) WHERE email != ''")
        for col, decl in (("last_seen", "REAL"), ("last_ip", "TEXT NOT NULL DEFAULT ''"), ("last_ua", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:                          # when each was last seen (sizeup/cad/activity.py)
                self.db.execute(f"ALTER TABLE operators ADD COLUMN {col} {decl}")

    def _v2_to_v3(self, db) -> None:
        """unit_history.note: the reason given for out of service / unmanned. Old rows keep ''."""
        cols = {r["name"] for r in db.execute("PRAGMA table_info(unit_history)")}
        if "note" not in cols:
            db.execute("ALTER TABLE unit_history ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            self.migrated.append("schema v2 -> v3 (unit history: out-of-service reason)")

    def _v3_to_v4(self, db) -> None:
        """Radio IDs, the communications log and its links to events; assignments.assigned_at
        (old rows: when dispatched, else when the event was accepted)."""
        if "units" not in {r["name"] for r in db.execute("PRAGMA table_info(events)")}:
            db.execute("ALTER TABLE events ADD COLUMN units TEXT NOT NULL DEFAULT ''")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "assigned_at" not in cols:
            db.execute("ALTER TABLE assignments ADD COLUMN assigned_at REAL")
            db.execute("UPDATE assignments SET assigned_at = COALESCE(dispatched_at, (SELECT COALESCE(i.accepted_at, "
                       "i.created_at) FROM incidents i WHERE i.id = assignments.incident_id))")
        for stmt in RADIO_SCHEMA.split(";"):
            if stmt.strip():
                db.execute(stmt)
        self.migrated.append("schema v3 -> v4 (radio IDs, communications log)")

    def _v4_to_v5(self, db) -> None:
        """Settings: overrides over the data file, the audit log, operators. Safe to run again."""
        for stmt in SETTINGS_SCHEMA.split(";"):
            if stmt.strip():
                db.execute(stmt)
        self.migrated.append("schema v5 (settings: edits over the data file, audit log, operators)")

    def _v9_to_v10(self, db) -> None:
        """The department's own codes (its code sheet, 2026-10-07): unit statuses AQ, AV, DP ... OS.
        The old status codes become the new ones wherever a status is stored: the units, unit history
        (old and new), and the Settings edits keyed by status (unit_statuses items and order, status_flow
        rows and steps). The chronology's text ("E63 responding") is what was written: left as it is.
        Unit codes don't change; units off the sheet are retired by the data file (off the boards,
        history kept). A saved board layout (Settings) is redone on the new boards, keeping the edits it
        holds (order within a board, units moved to another board, units added). Every step checks
        first: running it again changes nothing."""
        done = []
        old_codes = list(V10_STATUS)
        marks = ",".join("?" * len(old_codes))
        case = "CASE {col} " + " ".join(f"WHEN '{o}' THEN '{n}'" for o, n in V10_STATUS.items()) + " END"
        n = db.execute(f"SELECT COUNT(*) FROM units WHERE status IN ({marks})", old_codes).fetchone()[0]
        if n:
            db.execute(f"UPDATE units SET status = {case.format(col='status')} WHERE status IN ({marks})", old_codes)
            done.append(f"{n} unit statuses")
        for col in ("old_status", "new_status"):
            n = db.execute(f"SELECT COUNT(*) FROM unit_history WHERE {col} IN ({marks})", old_codes).fetchone()[0]
            if n:
                db.execute(f"UPDATE unit_history SET {col} = {case.format(col=col)} WHERE {col} IN ({marks})", old_codes)
                done.append(f"{n} unit history {col.replace('_', ' ')}es")
        if db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'settings_overrides'").fetchone():
            done += self._v10_overrides(db)
        if done:
            now = self.clock()
            db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, ?, ?, ?, ?)",
                       (now, "", "system", "migration v10", "old unit status codes",
                        "the department's codes (" + ", ".join(f"{o} -> {n}" for o, n in V10_STATUS.items()) + "): "
                        + "; ".join(done)))
        self.migrated.append("schema v10 (the department's own codes: unit statuses "
                             + ", ".join(f"{o} -> {n}" for o, n in V10_STATUS.items()) + ")"
                             + (": " + "; ".join(done) if done else ": nothing to change"))

    def _v10_overrides(self, db) -> list[str]:
        """The Settings edits that name a status (or the old boards), mapped to the new codes."""
        done = []
        st = lambda c: V10_STATUS.get(c, c)

        def put(sec, key, val):
            db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT(section, key) DO UPDATE SET value = excluded.value", (sec, key, json.dumps(val), self.clock(), ""))

        def drop(sec, key):
            db.execute("DELETE FROM settings_overrides WHERE section = ? AND key = ?", (sec, key))
        rows = {(r["section"], r["key"]): json.loads(r["value"])
                for r in db.execute("SELECT section, key, value FROM settings_overrides")}
        for sec in ("unit_statuses", "status_flow"):
            for (s_, key), val in list(rows.items()):
                if s_ != sec:
                    continue
                new_key = st(key)
                new_val = val
                if sec == "status_flow" and isinstance(val, list):
                    new_val = list(dict.fromkeys(st(c) for c in val))
                if sec == "unit_statuses" and key == "_order" and isinstance(val, list):
                    new_val = list(dict.fromkeys(st(c) for c in val))
                if new_key == key and new_val == val:
                    continue
                if new_key != key and (sec, new_key) in rows:
                    done.append(f"{sec} edit {key} left as it is ({new_key} is edited too)")
                    continue
                if new_key != key:
                    drop(sec, key)
                    rows.pop((sec, key))
                put(sec, new_key, new_val)
                rows[(sec, new_key)] = new_val
                done.append(f"Settings {sec} {key}" + (f" -> {new_key}" if new_key != key else " (codes)"))
        L = self.lists
        for code in V10_RETIRED_UNITS:                    # an edited unit the data file now retires: retired too
            v = rows.get(("units", code))
            if isinstance(v, dict) and not v.get("deleted") and not v.get("retired"):
                put("units", code, dict(v, retired=True))
                done.append(f"Settings unit {code} retired (not on the department's sheet)")
        layout = rows.get(("units", "_boards"))
        if isinstance(layout, list):
            new = v10_layout(layout, L.unit_boards, rows)
            base = [{"id": b["id"], "title": b["title"], "units": list(b["units"]), **lists_mod.board_meta(b)}
                    for b in L.unit_boards]
            if new != layout:
                if new == base:
                    drop("units", "_boards")
                else:
                    put("units", "_boards", new)
                done.append("Settings board layout redone on the new boards (its edits kept)")
        return done

    def _v1_to_v2(self, db) -> None:
        """The sheet's vocabulary: numbers 26-000001, TYPE/SUBTYPE, caller fields, dispositions,
        emergency / non-emergency, unit codes and statuses from the data file. Old default run
        cards are replaced by the new defaults (seeded after); an edited one is kept under its new
        key when there is one."""
        L = self.lists
        for col in ("subtype", "type_note", "caller_name", "caller_address", "phone", "disposition", "received_via"):
            db.execute(f"ALTER TABLE incidents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE incidents ADD COLUMN emergency INTEGER NOT NULL DEFAULT 1")
        db.execute("ALTER TABLE incidents ADD COLUMN until_at REAL")
        db.execute("ALTER TABLE incidents RENAME COLUMN narrative TO remarks")
        db.execute("ALTER TABLE dispatches ADD COLUMN kind TEXT NOT NULL DEFAULT 'dispatch'")
        via = "60 CONTROL" if "60 CONTROL" in L.received_codes else ""
        db.execute(SCHEMA[SCHEMA.index("CREATE TABLE unit_history"):SCHEMA.index("CREATE TABLE dispatches")])
        for r in db.execute("SELECT id, number, type, status, source FROM incidents").fetchall():
            number = r["number"]
            if number and (m := re.fullmatch(r"PCFD-(\d{4})-(\d+)", number)):
                number = event_number(int(m.group(1)), int(m.group(2)))
            typ, sub = V1_TYPES.get(r["type"], ("", ""))
            try:
                L.check_type(typ, sub)
            except ValueError:
                typ, sub = "", ""
            note = "" if typ else (f"was {r['type']!r} before the sheet's types: pick the type" if r["type"] else "")
            status = {"pending": "queued"}.get(r["status"], r["status"])
            if status == "active" and not db.execute("SELECT 1 FROM assignments WHERE incident_id = ? AND cleared_at IS NULL",
                                                     (r["id"],)).fetchone():
                status = "pending"
            db.execute("UPDATE incidents SET number = ?, type = ?, subtype = ?, type_note = ?, status = ?, emergency = ?, "
                       "received_via = ? WHERE id = ?",
                       (number, typ, sub, note, status, int(L.is_emergency(typ, sub)),
                        via if r["source"] == "60 Control page" else "", r["id"]))
        for col, decl in (("spoken", "TEXT NOT NULL DEFAULT ''"), ("title", "TEXT NOT NULL DEFAULT ''"),
                          ("board", "TEXT NOT NULL DEFAULT ''"), ("department", "TEXT NOT NULL DEFAULT ''"),
                          ("mutual_aid", "INTEGER NOT NULL DEFAULT 0")):
            db.execute(f"ALTER TABLE units ADD COLUMN {col} {decl}")
        for u in db.execute("SELECT id, name, kind, status FROM units").fetchall():
            status = L.status_for(V1_STATUS_ROLES.get(u["status"], "dispatched"))
            db.execute("UPDATE units SET name = ?, kind = ?, status = ? WHERE id = ?",
                       (v1_unit_code(u["name"]), "marine" if u["kind"] == "boat" else u["kind"], status, u["id"]))
        db.execute("UPDATE run_card_slots SET kind = 'marine' WHERE kind = 'boat'")
        for s in db.execute("SELECT rowid, unit FROM run_card_slots WHERE unit != ''").fetchall():
            db.execute("UPDATE run_card_slots SET unit = ? WHERE rowid = ?", (v1_unit_code(s["unit"]), s["rowid"]))
        db.execute("DELETE FROM run_cards WHERE is_default = 1")
        for c in db.execute("SELECT id, type FROM run_cards").fetchall():
            typ, sub = V1_TYPES.get(c["type"], ("", ""))
            k = lists_mod.key(typ, sub)
            if typ and not db.execute("SELECT 1 FROM run_cards WHERE type = ?", (k,)).fetchone():
                db.execute("UPDATE run_cards SET type = ?, spoken = '' WHERE id = ?", (k, c["id"]))
                self.migrated.append(f"run card {c['type']!r} is now {k}")
            else:
                db.execute("DELETE FROM run_cards WHERE id = ?", (c["id"],))
                self.migrated.append(f"run card {c['type']!r} removed: no matching sheet TYPE (re-enter it)")
        db.execute("DELETE FROM run_card_slots WHERE card_id NOT IN (SELECT id FROM run_cards)")
        self.migrated.insert(0, "schema v1 -> v2 (the sheet's numbers, types, unit codes and statuses)")

    # -- plumbing -----------------------------------------------------------------------------------

    def tx(self):
        """`with store.tx() as db:` one transaction under the lock."""
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                store.db.execute("BEGIN IMMEDIATE")
                return store.db

            def __exit__(self, et, e, tb):
                try:
                    store.db.execute("ROLLBACK" if et else "COMMIT")
                    if not et:
                        store.rev += 1
                finally:
                    store.lock.release()
                return False
        return _Tx()

    def meta_get(self, key: str) -> str | None:
        with self.lock:
            r = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def meta_put(self, key: str, value: str) -> None:
        """A small value (AWS usage, the vocabulary's state): written at once, not a CAD change (no rev)."""
        with self.lock:
            self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (key, value))

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def _event(self, db, iid, kind: str, text: str, operator: str = "", uid=None, at=None, units: str = "",
               places: str = "") -> None:
        db.execute("INSERT INTO events (incident_id, unit_id, at, kind, text, operator, units, places) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (iid, uid, self.clock() if at is None else at, kind, text, operator, units, places))

    def _history(self, db, uid: int, old: str, new: str, iid, operator: str, at: float, note: str = "") -> None:
        db.execute("INSERT INTO unit_history (at, unit_id, old_status, new_status, incident_id, operator, note) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (at, uid, old, new, iid, operator, note))

    # -- units ------------------------------------------------------------------------------------

    def sync_units(self) -> int:
        """The boards from the data file: a unit not in the table is added (available); every
        unit's kind, spoken name, title, spare flag and board position follow the file (there is
        no unit editor: the file is the roster). A unit no longer on the boards keeps its row and
        history, off the boards. A status the file no longer lists becomes the nearest one.
        Returns how many units were added."""
        L = self.lists
        free, dispatched = L.status_for("free"), L.status_for("dispatched")
        now = self.clock()
        added = 0
        with self.tx() as db:
            have = {r["name"]: r for r in db.execute("SELECT * FROM units")}
            on_boards = set()
            for code, board, sort in L.board_units():
                on_boards.add(code)
                u = L.unit(code)
                vals = (u["kind"], u["spoken"], u["title"], int(u["spare"]), u["department"], int(u["mutual_aid"]), board, sort)
                if code in have:
                    db.execute("UPDATE units SET kind = ?, spoken = ?, title = ?, spare = ?, department = ?, mutual_aid = ?, "
                               "board = ?, sort = ? WHERE id = ?", (*vals, have[code]["id"]))
                else:
                    db.execute("INSERT INTO units (name, kind, spoken, title, spare, department, mutual_aid, board, sort, "
                               "status, status_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (code, *vals, free, now))
                    added += 1
            for name, r in have.items():
                if name not in on_boards:
                    db.execute("UPDATE units SET board = '', sort = ? WHERE id = ?", (100000 + r["id"], r["id"]))
                if r["status"] not in L.status_ids:
                    busy = db.execute("SELECT 1 FROM assignments WHERE unit_id = ? AND cleared_at IS NULL", (r["id"],)).fetchone()
                    db.execute("UPDATE units SET status = ? WHERE id = ?", (dispatched if busy else free, r["id"]))
        return added

    def units(self) -> list[dict]:
        rows = self.q("SELECT u.*, a.incident_id, i.number AS incident_number, a.assigned_at FROM units u "
                      "LEFT JOIN assignments a ON a.unit_id = u.id AND a.cleared_at IS NULL "
                      "LEFT JOIN incidents i ON i.id = a.incident_id ORDER BY u.sort, u.id")
        for r in rows:
            r["spare"], r["mutual_aid"] = bool(r["spare"]), bool(r["mutual_aid"])
            r["response"] = self.lists.response(r["name"])       # fire | mutual_aid | agency
            r["station"] = self.lists.station(r["name"])    # home firehouse: the data file, or Settings
        return rows

    def recent_clears(self, since: float) -> dict[int, dict]:
        """unit id -> the latest call it cleared since then, having been on location there
        {incident_id, number, lat, lon, cleared_at} (where a returning rig drives home from:
        sizeup/cad/mapview.py locate()). A call it was cancelled from en route doesn't count."""
        rows = self.q("SELECT a.unit_id, a.incident_id, a.cleared_at, a.onscene_at, i.number, i.lat, i.lon FROM assignments a "
                      "JOIN incidents i ON i.id = a.incident_id WHERE a.cleared_at >= ? ORDER BY a.cleared_at, a.id", (since,))
        latest = {r["unit_id"]: r for r in rows}         # the latest wins
        return {k: r for k, r in latest.items() if r["onscene_at"] is not None}

    def unit(self, uid: int) -> dict:
        u = next((u for u in self.units() if u["id"] == uid), None)
        if u is None:
            raise CadError(f"no unit {uid}")
        return u

    def _take_command(self, db, iid: int, uid: int, name: str, operator: str, now: float) -> tuple[str, list[str]]:
        """uid takes incident command on iid (user, 2026-10-08: "there also can only be one inc commander, so
        the option should minus from others if its set on someone new"): whoever held it there goes back to
        on location, the status command is taken from, in the caller's transaction.
        -> ("Command: E63 (from 2392)", the units relieved)."""
        L = self.lists
        back = next((s["code"] for s in L.unit_statuses if s.get("role") == "onscene" and not s.get("retired")
                     and s["code"] != L.command_status), L.status_for("onscene"))
        held = db.execute("SELECT u.id, u.name, u.status FROM assignments a JOIN units u ON u.id = a.unit_id "
                          "WHERE a.incident_id = ? AND a.cleared_at IS NULL AND u.status = ? AND u.id != ? ORDER BY a.id",
                          (iid, L.command_status, uid)).fetchall()
        for h in held:
            db.execute("UPDATE units SET status = ?, status_at = ? WHERE id = ?", (back, now, h["id"]))
            self._history(db, h["id"], h["status"], back, iid, operator, now, f"command to {name}")
        was = [h["name"] for h in held]
        return f"Command: {name}" + (f" (from {', '.join(was)})" if was else ""), was

    def set_unit_status(self, uid: int, status: str, operator: str = "", note: str = "") -> dict:
        """The status dropdown. A free or out-of-service status clears the unit off its event;
        dispatched, en route and on scene stamp the assignment's times."""
        L = self.lists
        if status not in L.status_ids:
            raise CadError(f"unknown status {status!r}; one of {', '.join(L.status_ids)}")
        role = L.role(status)
        u = self.unit(uid)
        if u["response"] == "agency" and status not in L.agency_statuses():
            raise CadError(f"{u['name']} is another agency's unit: only "
                           + " or ".join(L.name("unit_statuses", s) for s in L.agency_statuses()))
        now = self.clock()
        with self.tx() as db:
            a = db.execute("SELECT * FROM assignments WHERE unit_id = ? AND cleared_at IS NULL", (uid,)).fetchone()
            db.execute("UPDATE units SET status = ?, status_at = ?, out_of_service = ? WHERE id = ?",
                       (status, now, note if role == "oos" else "", uid))
            if a is not None:
                if role in ON_TIMES:
                    db.execute(f"UPDATE assignments SET {ON_TIMES[role]} = COALESCE({ON_TIMES[role]}, ?) "
                               "WHERE id = ?", (now, a["id"]))
                elif role in ("free", "oos"):
                    db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
            text = f"{u['name']} {'cleared' if u['response'] == 'agency' and role == 'free' else L.status_phrase(status)}" \
                + (f" ({note})" if note and role == "oos" else "")
            iid = a["incident_id"] if a is not None else None
            relieved = []
            if iid is not None and status == L.command_status:      # one line: "Command: E63 (from 2392)"
                text, relieved = self._take_command(db, iid, uid, u["name"], operator, now)
            self._event(db, iid, "status", text, operator, uid, now)
            self._history(db, uid, u["status"], status, iid, operator, now, note if role == "oos" else "")
        out = self.unit(uid)
        if relieved:
            out["command_from"] = relieved
        return out

    def set_unit_status_on(self, uid: int, status: str, incident_id: int, operator: str = "", note: str = "") -> dict:
        """The board's working statuses ask which call (user, 2026-10-07: "it should prompt us ... to
        select the call they are now responding to and remove them from the old call if they were
        on one"). incident_id > 0: put the unit on that call first (moved off any other, noted on both
        chronologies); 0: on no call (cleared off the one it was on). Then the status."""
        L = self.lists
        if L.role(status) in ("free", "oos"):
            return self.set_unit_status(uid, status, operator, note)
        u = self.unit(uid)
        now = self.clock()
        if incident_id:
            inc = self.q("SELECT status FROM incidents WHERE id = ?", (incident_id,))
            if not inc:
                raise CadError(f"no incident {incident_id}")
            if inc[0]["status"] not in ("pending", "active"):
                raise CadError("that call isn't open")
            self.assign(incident_id, [uid], operator)
            if status == L.status_for("dispatched"):
                return self.unit(uid)                       # assign() already set and logged it
        else:
            with self.tx() as db:
                a = db.execute("SELECT a.id, a.incident_id FROM assignments a WHERE a.unit_id = ? AND a.cleared_at IS NULL",
                               (uid,)).fetchone()
                if a is not None:
                    db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
                    self._event(db, a["incident_id"], "status", f"{u['name']} off this call ({L.status_phrase(status)} elsewhere)",
                                operator, uid, now)
        return self.set_unit_status(uid, status, operator, note)

    def return_statuses(self) -> tuple[str, ...]:
        """What a cleared unit can go back to a still-open call as: the responding / on-location statuses."""
        L = self.lists
        return tuple(c for c in L.status_ids if L.role(c) in ("enroute", "onscene")
                     and not any(s["code"] == c and s.get("retired") for s in L.unit_statuses))

    def return_to_incident(self, iid: int, uid: int, status: str, operator: str = "") -> dict:
        """A unit that cleared a call that is still open goes back to it (user, 2026-10-07: "after units
        clear an alarm that is still opened, we need a way to send them back en route and then on
        scene"). Its assignment on that call is reopened; the first times it recorded are kept."""
        L = self.lists
        if status not in self.return_statuses():
            raise CadError("a unit goes back to a call as " + " or ".join(L.name("unit_statuses", s)
                                                                           for s in self.return_statuses()))
        u = self.unit(uid)
        now = self.clock()
        with self.tx() as db:
            inc = db.execute("SELECT status FROM incidents WHERE id = ?", (iid,)).fetchone()
            if inc is None:
                raise CadError(f"no incident {iid}")
            if inc["status"] in ("closed", "dismissed"):
                raise CadError("Incident closed")
            busy = db.execute("SELECT a.incident_id, i.number FROM assignments a JOIN incidents i ON i.id = a.incident_id "
                              "WHERE a.unit_id = ? AND a.cleared_at IS NULL", (uid,)).fetchone()
            if busy is not None:
                raise CadError(f"{u['name']} on {busy['number'] or busy['incident_id']}: clear it there")
            if L.role(u["status"]) == "oos":
                raise CadError(f"{u['name']} is {L.status_phrase(u['status'])}")
            a = db.execute("SELECT id FROM assignments WHERE incident_id = ? AND unit_id = ? AND cleared_at IS NOT NULL "
                           "ORDER BY cleared_at DESC, id DESC LIMIT 1", (iid, uid)).fetchone()
            if a is None:
                raise CadError(f"{u['name']} was not on this incident")
            col = ON_TIMES[L.role(status)]
            db.execute(f"UPDATE assignments SET cleared_at = NULL, {col} = COALESCE({col}, ?) WHERE id = ?", (now, a["id"]))
            db.execute("UPDATE units SET status = ?, status_at = ?, out_of_service = '' WHERE id = ?", (status, now, uid))
            self._event(db, iid, "status", f"{u['name']} returned ({L.status_phrase(status)})", operator, uid, now)
            self._history(db, uid, u["status"], status, iid, operator, now)
            if status == L.command_status:
                self._event(db, iid, "status", self._take_command(db, iid, uid, u["name"], operator, now)[0], operator, uid, now)
        return self.unit(uid)

    # -- incidents --------------------------------------------------------------------------------

    def _next_number(self, db, at: float) -> str:
        year = local_year(at)
        db.execute("INSERT INTO seq (year, last) VALUES (?, 1) ON CONFLICT(year) DO UPDATE SET last = last + 1", (year,))
        n = db.execute("SELECT last FROM seq WHERE year = ?", (year,)).fetchone()[0]
        return event_number(year, n)

    def create_incident(self, *, type: str = "", subtype: str = "", type_note: str = "", emergency: bool | None = None,
                        received_via: str = "", until_at: float | None = None, address: str = "",
                        cross_street: str = "", caller_name: str = "", caller_address: str = "", phone: str = "",
                        remarks: str = "", lat: float | None = None, lon: float | None = None, geo_label: str = "",
                        priority: int | None = None, source: str = "manual", source_ref: str | None = None,
                        status: str = "pending", operator: str = "") -> int:
        """ACCEPT EVENT: entered by a person, numbered at once, PENDING until units go (or, a
        non-emergency one, listed under NON-EMERGENCY EVENTS until closed). One from a 60 Control
        page waits as queued, with no number, until someone accepts it. emergency None = from the
        type (cad_lists.json event_class)."""
        if source not in SOURCES:
            raise CadError(f"source: one of {', '.join(SOURCES)}")
        if status not in ("queued", "pending"):
            raise CadError("a new event is queued or pending")
        try:
            self.lists.check_type(type, subtype, required=status == "pending")
        except ValueError as e:
            raise CadError(str(e)) from None
        if received_via and received_via not in self.lists.received_codes:
            raise CadError(f"received_via: one of {', '.join(self.lists.received_codes)}")
        if emergency is None:
            emergency = self.lists.is_emergency(type, subtype)
        now = self.clock()
        with self.tx() as db:
            number = self._next_number(db, now) if status == "pending" else None
            cur = db.execute(
                "INSERT INTO incidents (number, type, subtype, type_note, emergency, received_via, until_at, priority, "
                "address, cross_street, lat, lon, geo_label, caller_name, caller_address, phone, remarks, source, "
                "source_ref, status, created_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)",
                (number, type, subtype, type_note, int(bool(emergency)), received_via, until_at, priority, address,
                 cross_street, lat, lon, geo_label, caller_name, caller_address, phone, remarks, source, source_ref,
                 status, now, now if number else None))
            iid = cur.lastrowid
            L = self.lists
            what = (L.type_label(type, subtype) or "type not set") + (f" at {address}" if address else "")
            if type_note:
                what += f" ({type_note})"
            if received_via:
                what += f"; received via {L.name('received_via', received_via)}"
            head = (f"Event created ({'pending' if emergency else 'non-emergency'})" if number
                    else f"From {source}, waiting to be accepted")
            self._event(db, iid, "created", f"{head}: {what}", operator, at=now)
            if caller_name or caller_address or phone:
                self._event(db, iid, "system", "Caller info: " + " · ".join(x or "—" for x in (caller_name, caller_address, phone)),
                            operator, at=now)
        return iid

    def by_source_ref(self, ref: str) -> dict | None:
        return self.one("SELECT * FROM incidents WHERE source_ref = ?", (ref,))

    def incident(self, iid: int) -> dict:
        inc = self.one("SELECT * FROM incidents WHERE id = ?", (iid,))
        if inc is None:
            raise CadError(f"no event {iid}")
        return inc

    EDITABLE = ("type", "subtype", "type_note", "emergency", "received_via", "until_at", "priority", "address",
                "cross_street", "lat", "lon", "geo_label", "caller_name", "caller_address", "phone", "remarks")
    QUIET = ("lat", "lon", "geo_label", "remarks", "type_note", "until_at")   # changed, not spelled out in the log

    def update_incident(self, iid: int, fields: dict, operator: str = "", log: bool = True) -> dict:
        """Edit an open event. A new TYPE/SUBTYPE re-sorts it emergency / non-emergency from the
        lists unless the same edit sets emergency itself."""
        inc = self.incident(iid)
        if inc["status"] not in OPEN:
            raise CadError(f"event is {inc['status']}")
        fields = dict(fields)
        if "emergency" in fields:
            fields["emergency"] = int(bool(fields["emergency"]))
        changes = {k: v for k, v in fields.items() if k in self.EDITABLE and inc[k] != v}
        if ("type" in changes or "subtype" in changes) and changes.get("type", inc["type"]) and "type_note" not in fields:
            changes["type_note"] = ""                 # a person picked the type: the page's doubt is settled
        t, s = changes.get("type", inc["type"]), changes.get("subtype", inc["subtype"])
        if "type" in changes or "subtype" in changes:
            try:
                self.lists.check_type(t, s, required=inc["status"] != "queued")
            except ValueError as e:
                raise CadError(str(e)) from None
            if "emergency" not in fields and t:
                e = int(self.lists.is_emergency(t, s))
                if e != inc["emergency"]:
                    changes["emergency"] = e
        if changes.get("received_via") and changes["received_via"] not in self.lists.received_codes:
            raise CadError(f"received_via: one of {', '.join(self.lists.received_codes)}")
        if not changes:
            return inc
        with self.tx() as db:
            db.execute(f"UPDATE incidents SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                       (*changes.values(), iid))
            parts = []
            if "type" in changes or "subtype" in changes:
                parts.append(f"type: {self.lists.type_label(t, s) or '(none)'}")
            if "emergency" in changes:
                parts.append("now " + ("an emergency" if changes["emergency"] else "a non-emergency") + " event")
            labels = {"address": "location", "caller_name": "caller", "caller_address": "caller address",
                      "received_via": "received via"}
            if changes.get("received_via"):
                changes_shown = dict(changes, received_via=self.lists.name("received_via", changes["received_via"]))
            else:
                changes_shown = changes
            parts += [f"{labels.get(k, k.replace('_', ' '))}: {changes_shown[k] if changes[k] not in (None, '') else '(none)'}"
                      for k in changes if k not in self.QUIET and k not in ("type", "subtype", "emergency")]
            if "remarks" in changes:
                parts.append("remarks updated")
            if "until_at" in changes:
                parts.append("until: " + (datetime.fromtimestamp(changes["until_at"], TZ).strftime("%m/%d %H:%M")
                                          if changes["until_at"] else "(none)"))
            if log and parts:
                self._event(db, iid, "edit", "Edited " + "; ".join(parts), operator)
        return self.incident(iid)

    def accept(self, iid: int, operator: str = "") -> dict:
        """A queued page accepted: numbered; an emergency one goes to PENDING EVENTS to be
        dispatched, a non-emergency one to NON-EMERGENCY EVENTS."""
        inc = self.incident(iid)
        if inc["status"] != "queued":
            raise CadError(f"Event {inc['status']}: not a page")
        if not inc["type"]:
            raise CadError("Pick a type")
        now = self.clock()
        with self.tx() as db:
            number = self._next_number(db, now)
            db.execute("UPDATE incidents SET status = 'pending', number = ?, accepted_at = ? WHERE id = ?", (number, now, iid))
            self._event(db, iid, "accepted", f"Accepted as {number}: event created "
                        f"({'pending' if inc['emergency'] else 'non-emergency'}): {self.lists.type_label(inc['type'], inc['subtype'])}",
                        operator, at=now)
        return self.incident(iid)

    def dismiss(self, iid: int, operator: str = "", reason: str = "") -> dict:
        inc = self.incident(iid)
        if inc["status"] != "queued":
            raise CadError(f"Event {inc['status']}: not a page")
        now = self.clock()
        with self.tx() as db:
            db.execute("UPDATE incidents SET status = 'dismissed', closed_at = ? WHERE id = ?", (now, iid))
            self._event(db, iid, "dismissed", "Dismissed" + (f": {reason}" if reason else ""), operator, at=now)
        return self.incident(iid)

    def close(self, iid: int, disposition: str, operator: str = "") -> dict:
        """CLOSE EVENT, with a DISPOSITION. Every unit still on it is cleared and back in service."""
        inc = self.incident(iid)
        if inc["status"] not in ("pending", "active"):
            raise CadError(f"Event {inc['status']}" + (": accept or dismiss" if inc["status"] == "queued" else ""))
        if not disposition:
            raise CadError("Pick a disposition")
        if disposition not in self.lists.disposition_codes:
            raise CadError(f"disposition: {disposition!r} is not one of the dispositions")
        free = self.lists.status_for("free")
        now = self.clock()
        with self.tx() as db:
            on = db.execute("SELECT a.id, a.unit_id, u.name, u.status FROM assignments a JOIN units u ON u.id = a.unit_id "
                            "WHERE a.incident_id = ? AND a.cleared_at IS NULL", (iid,)).fetchall()
            for a in on:
                db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
                if self.lists.role(a["status"]) != "oos" and a["status"] != free:
                    db.execute("UPDATE units SET status = ?, status_at = ? WHERE id = ?", (free, now, a["unit_id"]))
                    self._history(db, a["unit_id"], a["status"], free, iid, operator, now)
            db.execute("UPDATE incidents SET status = 'closed', closed_at = ?, disposition = ? WHERE id = ?",
                       (now, disposition, iid))
            names = ", ".join(a["name"] for a in on)
            self._event(db, iid, "closed", f"Event closed: {self.lists.name('dispositions', disposition)}"
                        + (f"; cleared {names}" if names else ""), operator, at=now)
        return self.incident(iid)

    def reopen(self, iid: int, operator: str = "") -> dict:
        """A closed or dismissed event back on the boards, its number kept (#89). Closed: Active if a unit
        was ever on it, else Pending (a non-emergency one: open again); the disposition is cleared and
        kept in the chronology ("Reopened by JD; was Complete at 16:46"). Dismissed: back to the 60
        Control queue, to accept or dismiss. No unit comes back by itself: the dispatcher adds them."""
        inc = self.incident(iid)
        if inc["status"] not in ("closed", "dismissed"):
            raise CadError(f"Event {inc['status']}: not closed")
        now = self.clock()
        if inc["status"] == "dismissed":
            status, was = "queued", "dismissed"
        else:
            had = self.one("SELECT 1 AS x FROM assignments WHERE incident_id = ? LIMIT 1", (iid,)) is not None
            status = "active" if had and inc["emergency"] else "pending"
            was = self.lists.name("dispositions", inc["disposition"]) or inc["disposition"] or "closed"
        if inc["closed_at"]:
            then, today = datetime.fromtimestamp(inc["closed_at"], TZ), datetime.fromtimestamp(now, TZ)
            at = then.strftime("%H:%M" if then.date() == today.date() else "%m/%d %H:%M")
        else:
            at = "?"
        who = re.sub(r"\s*<[^<>]*>\s*$", "", operator).strip()       # "Name <email>" (api.py): the name
        with self.tx() as db:
            db.execute("UPDATE incidents SET status = ?, closed_at = NULL, disposition = '' WHERE id = ?", (status, iid))
            self._event(db, iid, "reopened", f"Reopened{f' by {who}' if who else ''}; was {was} at {at}", operator, at=now)
        return self.incident(iid)

    def note(self, iid: int, text: str, operator: str = "", kind: str = "note", units: list[str] | tuple = (),
             places: list[dict] | tuple = ()) -> None:
        """A chronology entry: an operator's comment (note), or kind 'upgrade'. places: its "@" places
        (Cad.note checks them), kept as JSON with the note; they never move the event."""
        self.incident(iid)
        with self.tx() as db:
            self._event(db, iid, kind, text, operator, units=",".join(units),
                        places=json.dumps(list(places), separators=(",", ":")) if places else "")

    def has_dispatch(self, iid: int) -> bool:
        return self.one("SELECT 1 AS x FROM dispatches WHERE incident_id = ? LIMIT 1", (iid,)) is not None

    def assign(self, iid: int, unit_ids: list[int], operator: str = "", attach: bool = False, verb: str = "",
               quiet: bool = False, status: str = "") -> list[str]:
        """Units dispatched to the event (PENDING -> ACTIVE), or, attach=True, attached with no
        dispatch (a non-emergency event: logged on the chronology, unit status unchanged). A unit
        committed elsewhere is moved (its old assignment cleared, noted on both chronologies);
        one already on this event stays. verb: how the chronology says it ("added to the assignment");
        default the dispatched status ("E63 dispatched").
        status (a responding / on-location code): on the event with no dispatch in that status, its
        time stamped and Disp left empty ("2395 added responding (not dispatched)")."""
        now = self.clock()
        dispatched = self.lists.status_for("dispatched")
        moved, added = [], []
        with self.tx() as db:
            for uid in unit_ids:
                u = db.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone()
                a = db.execute("SELECT a.*, i.number FROM assignments a JOIN incidents i ON i.id = a.incident_id "
                               "WHERE a.unit_id = ? AND a.cleared_at IS NULL", (uid,)).fetchone()
                if a is not None and a["incident_id"] == iid:
                    continue
                if a is not None:
                    db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
                    self._event(db, a["incident_id"], "status", f"{u['name']} reassigned to another event", operator, uid, now)
                    moved.append(f"{u['name']} (from {a['number']})")
                cur = db.execute("INSERT INTO assignments (incident_id, unit_id, dispatched_at, assigned_at) VALUES (?, ?, ?, ?)",
                                 (iid, uid, None if attach or status else now, now))
                if status:
                    col = ON_TIMES[self.lists.role(status)]
                    db.execute(f"UPDATE assignments SET {col} = ? WHERE id = ?", (now, cur.lastrowid))
                    db.execute("UPDATE units SET status = ?, status_at = ?, out_of_service = '' WHERE id = ?", (status, now, uid))
                    self._event(db, iid, "status", f"{u['name']} added {self.lists.status_phrase(status)} (not dispatched)",
                                operator, uid, now)
                    self._history(db, uid, u["status"], status, iid, operator, now)
                    if status == self.lists.command_status:
                        self._event(db, iid, "status", self._take_command(db, iid, uid, u["name"], operator, now)[0],
                                    operator, uid, now)
                elif not attach:
                    db.execute("UPDATE units SET status = ?, status_at = ? WHERE id = ?", (dispatched, now, uid))
                    self._event(db, iid, "status", f"{u['name']} {verb or self.lists.status_phrase(dispatched)}",
                                operator, uid, now)
                    self._history(db, uid, u["status"], dispatched, iid, operator, now)
                added.append(u["name"])
            if db.execute("UPDATE incidents SET status = 'active' WHERE id = ? AND status = 'pending'", (iid,)).rowcount:
                self._event(db, iid, "status", "Event status: active", operator, at=now)
            if attach and added and not quiet:
                self._event(db, iid, "attach", f"Attached {', '.join(added)}", operator, at=now)
        return moved

    DISPATCH_KINDS = {"dispatch": "Dispatch", "add-on": "Add-on dispatch", "upgrade": "Upgrade"}

    def log_dispatch(self, iid: int, *, operator: str, text: str, unit_ids: list[int], audio_path: str,
                     audio_note: str, transmitted: bool, channel: str, tx_note: str, kind: str = "dispatch",
                     remarks: str = "", tones: str = "") -> int:
        """The dispatch row and its chronology entry, then (same time, right after it: the dispatch's,
        see _attach) "Dispatch remarks: ..." and "Tones: Port Chester + Rye Brook ..." when given."""
        now = self.clock()
        with self.tx() as db:
            cur = db.execute("INSERT INTO dispatches (incident_id, at, operator, kind, text, unit_ids, audio_path, "
                             "audio_note, transmitted, channel, tx_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (iid, now, operator, kind, text, json.dumps(unit_ids), audio_path, audio_note,
                              int(transmitted), channel, tx_note))
            how = f"transmitted on {channel}" if transmitted else f"not transmitted ({tx_note})"
            self._event(db, iid, "dispatch", f"{self.DISPATCH_KINDS.get(kind, kind)}, {how}: {text}", operator, at=now)
            for k, label, v in (("remarks", "Dispatch remarks", remarks), ("tones", "Tones", tones)):
                if v:
                    self._event(db, iid, k, f"{label}: {v}", operator, at=now)
            return cur.lastrowid

    def dispatch_row(self, did: int) -> dict | None:
        return self.one("SELECT * FROM dispatches WHERE id = ?", (did,))

    def _attach(self, rows: list[dict], max_events: int = 300) -> list[dict]:
        """Each event with its assignments, chronology and dispatch log."""
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        assigns = self.q(f"SELECT a.*, u.name, u.kind, u.status FROM assignments a JOIN units u ON u.id = a.unit_id "
                         f"WHERE a.incident_id IN ({marks}) ORDER BY a.id", ids)
        events = self.q(f"SELECT * FROM events WHERE incident_id IN ({marks}) ORDER BY at, id", ids)
        disp = self.q(f"SELECT id, incident_id, at, operator, kind, text, unit_ids, audio_path != '' AS has_audio, audio_note, "
                      f"transmitted, channel, tx_note FROM dispatches WHERE incident_id IN ({marks}) ORDER BY at, id", ids)
        by = {r["id"]: dict(r, emergency=bool(r["emergency"]), assignments=[], events=[], dispatches=[]) for r in rows}
        latest = {}                        # (incident, unit) -> its last assignment row
        for a in assigns:
            latest[(a["incident_id"], a["unit_id"])] = a["id"]
        back = list(self.return_statuses())
        for a in assigns:
            a["next"] = [] if a["cleared_at"] else list(self.lists.next_statuses(a["status"]))   # the row's buttons
            # A cleared unit, now free, on a call that is still open: it can go back to it.
            a["return"] = back if (a["cleared_at"] and latest[(a["incident_id"], a["unit_id"])] == a["id"]
                                   and by[a["incident_id"]]["status"] not in ("closed", "dismissed")
                                   and self.lists.role(a["status"]) == "free") else []
            by[a["incident_id"]]["assignments"].append(a)
        for e in events:
            e["places"] = json.loads(e["places"]) if e.get("places") else []     # a note's "@" places (#67)
            by[e["incident_id"]]["events"].append(e)
        for d in disp:
            d["transmitted"], d["has_audio"] = bool(d["transmitted"]), bool(d["has_audio"])
            d["unit_ids"] = json.loads(d["unit_ids"])
            d["remarks"] = d["tones"] = ""
            by[d["incident_id"]]["dispatches"].append(d)
        for r in by.values():
            n = -1                                       # remarks / tones follow their dispatch's entry
            for e in r["events"]:
                n += e["kind"] == "dispatch"
                if e["kind"] in ("remarks", "tones") and 0 <= n < len(r["dispatches"]):
                    r["dispatches"][n][e["kind"]] = e["text"].split(": ", 1)[-1]
        for r in by.values():
            r["events"] = r["events"][-max_events:]
            r["town"] = self.lists.event_town(r)
            r["joint_call"] = self.lists.joint_call(r)      # "Rye Brook": their call (their units first)
        return list(by.values())

    def incidents(self, closed_s: float = 12 * 3600, closed_max: int = 20) -> list[dict]:
        """Every open one (queued, pending, active: however old, a road closure can last days),
        plus those closed or dismissed in the last closed_s."""
        cutoff = self.clock() - closed_s
        rows = self.q("SELECT * FROM incidents WHERE status IN ('queued', 'pending', 'active') ORDER BY created_at DESC")
        rows += self.q("SELECT * FROM incidents WHERE status IN ('closed', 'dismissed') AND closed_at >= ? "
                       "ORDER BY closed_at DESC LIMIT ?", (cutoff, closed_max))
        return self._attach(rows)

    def full(self, iid: int) -> dict:
        """One event with everything (the Incident Database opens old ones this way)."""
        return self._attach([self.incident(iid)])[0]

    def history(self, limit: int = 500) -> list[dict]:
        """The Incident Database: every numbered event, newest first, with the units sent."""
        rows = self.q("SELECT id, number, type, subtype, emergency, received_via, address, status, disposition, source, "
                      "created_at, closed_at FROM incidents WHERE number IS NOT NULL ORDER BY created_at DESC, id DESC "
                      "LIMIT ?", (limit,))
        if rows:
            ids = [r["id"] for r in rows]
            names: dict[int, list[str]] = {}
            for a in self.q(f"SELECT a.incident_id, u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                            f"WHERE a.incident_id IN ({','.join('?' * len(ids))}) ORDER BY a.id", ids):
                lst = names.setdefault(a["incident_id"], [])
                if a["name"] not in lst:
                    lst.append(a["name"])
            for r in rows:
                r["units"], r["emergency"] = names.get(r["id"], []), bool(r["emergency"])
        return rows

    def unit_log(self, limit: int = 50) -> list[dict]:
        """Status changes with no incident (out of service, back in quarters)."""
        return self.q("SELECT * FROM events WHERE incident_id IS NULL AND kind != 'prealert' ORDER BY at DESC, id DESC LIMIT ?",
                      (limit,))
