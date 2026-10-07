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
      unit_history  every unit status change: old -> new, event, operator
      dispatches    what was (or, transmit off, would have been) sent, with its audio

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
SCHEMA_VERSION = 2

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
  mutual_aid INTEGER NOT NULL DEFAULT 0 -- another department's: never picked by kind on a run card
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
  dispatched_at REAL, enroute_at REAL, onscene_at REAL, cleared_at REAL
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
  kind TEXT NOT NULL DEFAULT '',        -- "engine", or "ladder|tower" (either)
  count INTEGER NOT NULL DEFAULT 1,
  unit TEXT NOT NULL DEFAULT ''         -- or one named unit ("2398")
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER REFERENCES incidents(id),   -- NULL: a unit status change off any incident
  unit_id INTEGER REFERENCES units(id),
  at REAL NOT NULL,
  kind TEXT NOT NULL,                   -- created | system | accepted | dismissed | note | dispatch | attach | status | closed | edit
  text TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT ''
);
CREATE INDEX events_incident ON events(incident_id);
CREATE TABLE unit_history (           -- every unit status change (the sheet's Unit History)
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  old_status TEXT NOT NULL,
  new_status TEXT NOT NULL,
  incident_id INTEGER REFERENCES incidents(id),
  operator TEXT NOT NULL DEFAULT ''
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
                self.db.executescript("BEGIN;" + SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
                return
            if v == 1:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    self._v1_to_v2(self.db)
                    self.db.execute("PRAGMA user_version = 2")
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise

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

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def _event(self, db, iid, kind: str, text: str, operator: str = "", uid=None, at=None) -> None:
        db.execute("INSERT INTO events (incident_id, unit_id, at, kind, text, operator) VALUES (?, ?, ?, ?, ?, ?)",
                   (iid, uid, self.clock() if at is None else at, kind, text, operator))

    def _history(self, db, uid: int, old: str, new: str, iid, operator: str, at: float) -> None:
        db.execute("INSERT INTO unit_history (at, unit_id, old_status, new_status, incident_id, operator) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (at, uid, old, new, iid, operator))

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
        rows = self.q("SELECT u.*, a.incident_id, i.number AS incident_number FROM units u "
                      "LEFT JOIN assignments a ON a.unit_id = u.id AND a.cleared_at IS NULL "
                      "LEFT JOIN incidents i ON i.id = a.incident_id ORDER BY u.sort, u.id")
        for r in rows:
            r["spare"], r["mutual_aid"] = bool(r["spare"]), bool(r["mutual_aid"])
        return rows

    def unit(self, uid: int) -> dict:
        u = next((u for u in self.units() if u["id"] == uid), None)
        if u is None:
            raise CadError(f"no unit {uid}")
        return u

    def set_unit_status(self, uid: int, status: str, operator: str = "", note: str = "") -> dict:
        """The status dropdown. A free or out-of-service status clears the unit off its event;
        dispatched, en route and on scene stamp the assignment's times."""
        L = self.lists
        if status not in L.status_ids:
            raise CadError(f"unknown status {status!r}; one of {', '.join(L.status_ids)}")
        role = L.role(status)
        u = self.unit(uid)
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
            text = f"{u['name']} {L.status_phrase(status)}" + (f" ({note})" if note and role == "oos" else "")
            iid = a["incident_id"] if a is not None else None
            self._event(db, iid, "status", text, operator, uid, now)
            self._history(db, uid, u["status"], status, iid, operator, now)
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
            raise CadError(f"event is {inc['status']}, not waiting to be accepted")
        if not inc["type"]:
            raise CadError("pick the type first (the page didn't say)")
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
            raise CadError(f"event is {inc['status']}, not waiting to be accepted")
        now = self.clock()
        with self.tx() as db:
            db.execute("UPDATE incidents SET status = 'dismissed', closed_at = ? WHERE id = ?", (now, iid))
            self._event(db, iid, "dismissed", "Dismissed" + (f": {reason}" if reason else ""), operator, at=now)
        return self.incident(iid)

    def close(self, iid: int, disposition: str, operator: str = "") -> dict:
        """CLOSE EVENT, with a DISPOSITION. Every unit still on it is cleared and back in service."""
        inc = self.incident(iid)
        if inc["status"] not in ("pending", "active"):
            raise CadError(f"event is {inc['status']}" + (": accept or dismiss it" if inc["status"] == "queued" else ""))
        if not disposition:
            raise CadError("disposition: required to close (one of the dispositions)")
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

    def note(self, iid: int, text: str, operator: str = "", kind: str = "note") -> None:
        """A chronology entry: an operator's comment (note), or kind 'upgrade'."""
        self.incident(iid)
        with self.tx() as db:
            self._event(db, iid, kind, text, operator)

    def has_dispatch(self, iid: int) -> bool:
        return self.one("SELECT 1 AS x FROM dispatches WHERE incident_id = ? LIMIT 1", (iid,)) is not None

    def assign(self, iid: int, unit_ids: list[int], operator: str = "", attach: bool = False, verb: str = "") -> list[str]:
        """Units dispatched to the event (PENDING -> ACTIVE), or, attach=True, attached with no
        dispatch (a non-emergency event: logged on the chronology, unit status unchanged). A unit
        committed elsewhere is moved (its old assignment cleared, noted on both chronologies);
        one already on this event stays. verb: how the chronology says it ("added to the assignment");
        default the dispatched status ("E63 dispatched")."""
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
                db.execute("INSERT INTO assignments (incident_id, unit_id, dispatched_at) VALUES (?, ?, ?)",
                           (iid, uid, None if attach else now))
                if not attach:
                    db.execute("UPDATE units SET status = ?, status_at = ? WHERE id = ?", (dispatched, now, uid))
                    self._event(db, iid, "status", f"{u['name']} {verb or self.lists.status_phrase(dispatched)}",
                                operator, uid, now)
                    self._history(db, uid, u["status"], dispatched, iid, operator, now)
                added.append(u["name"])
            if db.execute("UPDATE incidents SET status = 'active' WHERE id = ? AND status = 'pending'", (iid,)).rowcount:
                self._event(db, iid, "status", "Event status: active", operator, at=now)
            if attach and added:
                self._event(db, iid, "attach", f"Attached {', '.join(added)}", operator, at=now)
        return moved

    DISPATCH_KINDS = {"dispatch": "Dispatch", "add-on": "Add-on dispatch", "upgrade": "Upgrade"}

    def log_dispatch(self, iid: int, *, operator: str, text: str, unit_ids: list[int], audio_path: str,
                     audio_note: str, transmitted: bool, channel: str, tx_note: str, kind: str = "dispatch") -> int:
        now = self.clock()
        with self.tx() as db:
            cur = db.execute("INSERT INTO dispatches (incident_id, at, operator, kind, text, unit_ids, audio_path, "
                             "audio_note, transmitted, channel, tx_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (iid, now, operator, kind, text, json.dumps(unit_ids), audio_path, audio_note,
                              int(transmitted), channel, tx_note))
            how = f"transmitted on {channel}" if transmitted else f"not transmitted ({tx_note})"
            self._event(db, iid, "dispatch", f"{self.DISPATCH_KINDS.get(kind, kind)}, {how}: {text}", operator, at=now)
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
                      f"transmitted, channel, tx_note FROM dispatches WHERE incident_id IN ({marks}) ORDER BY at", ids)
        by = {r["id"]: dict(r, emergency=bool(r["emergency"]), assignments=[], events=[], dispatches=[]) for r in rows}
        for a in assigns:
            by[a["incident_id"]]["assignments"].append(a)
        for e in events:
            by[e["incident_id"]]["events"].append(e)
        for d in disp:
            d["transmitted"], d["has_audio"] = bool(d["transmitted"]), bool(d["has_audio"])
            d["unit_ids"] = json.loads(d["unit_ids"])
            by[d["incident_id"]]["dispatches"].append(d)
        for r in by.values():
            r["events"] = r["events"][-max_events:]
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
        return self.q("SELECT * FROM events WHERE incident_id IS NULL ORDER BY at DESC, id DESC LIMIT ?", (limit,))
