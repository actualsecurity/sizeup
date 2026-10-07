"""CAD store: PCFD's own incidents, units, run cards and dispatch log in one SQLite file.

    state/cad.sqlite        (not under captures/: the janitor there deletes old files)
      units         the roster (seeded once from data/westchester_apparatus.csv + chief callsigns)
      incidents     PCFD-YYYY-NNNN, pending (from a 60 Control page) -> active -> closed
      assignments   unit x incident, with dispatched / en route / on scene / cleared times
      run_cards     call type -> ordered slots ("3 x engine", "2398 Duty Officer")
      events        the incident timeline (and unit status changes with no incident)
      dispatches    what was (or, transmit off, would have been) sent, with its audio

One connection, one lock: the web server is threaded and SQLite writes are short. The schema
version is PRAGMA user_version so later milestones can migrate in place.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1

KINDS = ("chief", "engine", "ladder", "tower", "rescue", "boat")
# "available" = in service (on the air); "available-in-quarters" = back at the firehouse.
STATUSES = ("available", "dispatched", "enroute", "onscene", "transporting", "available-in-quarters",
            "out_of_service")
FREE = ("available", "available-in-quarters")          # can be recommended
ON_TIMES = {"dispatched": "dispatched_at", "enroute": "enroute_at", "onscene": "onscene_at"}
INCIDENT_STATUSES = ("pending", "active", "closed", "dismissed")
SOURCES = ("manual", "60 Control page")

SCHEMA = """
CREATE TABLE units (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,            -- "Engine 60", "2398 Duty Officer"
  kind TEXT NOT NULL,                   -- chief | engine | ladder | tower | rescue | boat
  station TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'available',
  status_at REAL NOT NULL,
  out_of_service TEXT NOT NULL DEFAULT '',   -- why, while out of service
  spare INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',       -- roster notes ("pump/tank 1500/500 gpm/gal")
  sort INTEGER NOT NULL DEFAULT 0       -- roster order: recommendation picks in this order
);
CREATE TABLE incidents (
  id INTEGER PRIMARY KEY,
  number TEXT UNIQUE,                   -- PCFD-2026-0001; NULL until accepted (dismissed pages use none)
  type TEXT NOT NULL DEFAULT '',
  priority INTEGER,
  address TEXT NOT NULL DEFAULT '',
  cross_street TEXT NOT NULL DEFAULT '',
  lat REAL, lon REAL, geo_label TEXT NOT NULL DEFAULT '',
  narrative TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,                 -- manual | 60 Control page
  source_ref TEXT UNIQUE,               -- the monitor incident id (dedupe)
  status TEXT NOT NULL,                 -- pending | active | closed | dismissed
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
  type TEXT NOT NULL UNIQUE,            -- "Structure fire"
  spoken TEXT NOT NULL DEFAULT '',      -- as said in the dispatch: "a structure fire"
  is_default INTEGER NOT NULL DEFAULT 0,   -- seeded stand-in, not yet edited to PCFD policy
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE run_card_slots (
  card_id INTEGER NOT NULL REFERENCES run_cards(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT '',        -- "engine", or "ladder|tower" (either)
  count INTEGER NOT NULL DEFAULT 1,
  unit TEXT NOT NULL DEFAULT ''         -- or one named unit ("2398 Duty Officer")
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER REFERENCES incidents(id),   -- NULL: a unit status change off any incident
  unit_id INTEGER REFERENCES units(id),
  at REAL NOT NULL,
  kind TEXT NOT NULL,                   -- created | accepted | dismissed | note | dispatch | status | closed | edit
  text TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT ''
);
CREATE INDEX events_incident ON events(incident_id);
CREATE TABLE dispatches (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  at REAL NOT NULL,
  operator TEXT NOT NULL,
  text TEXT NOT NULL,
  unit_ids TEXT NOT NULL DEFAULT '[]',
  audio_path TEXT NOT NULL DEFAULT '',
  audio_note TEXT NOT NULL DEFAULT '',  -- why there's no audio (no `say` on this machine, ...)
  transmitted INTEGER NOT NULL DEFAULT 0,
  channel TEXT NOT NULL DEFAULT '',
  tx_note TEXT NOT NULL DEFAULT ''
);
"""


class CadError(ValueError):
    """A request that can't be done as asked (shown to the operator as the 400 reason)."""


def local_year(at: float) -> int:
    return datetime.fromtimestamp(at, TZ).year


class Store:
    def __init__(self, path: str | Path = ":memory:", clock=time.time):
        self.path, self.clock = str(path), clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        self.rev = 0          # bumped on every change: the page refetches nothing, it's pushed
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            v = self.db.execute("PRAGMA user_version").fetchone()[0]
            if v > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path}: schema v{v} is newer than this sizeup (v{SCHEMA_VERSION})")
            if v == 0:
                self.db.executescript("BEGIN;" + SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
            # later: if v == 1: ALTER TABLE ...; user_version = 2

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

    # -- units ------------------------------------------------------------------------------------

    def seed_units(self, roster_csv: str | Path, department: str, callsigns: dict[str, str]) -> int:
        """The department's apparatus from the county roster, plus its chief officers. Once: an
        edited roster is never overwritten. Rows the roster marks 'RESERVED number (no apparatus
        listed)' are numbers, not trucks, and are skipped. Spares are kept, flagged."""
        if self.one("SELECT 1 AS x FROM units LIMIT 1"):
            return 0
        rows, dept_numbers = [], set()
        text = Path(roster_csv).read_text()
        for r in csv.DictReader(ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")):
            if r["department"].strip() != department:
                continue
            dept_numbers.add(r["dept_number"].strip())
            notes = (r.get("notes") or "").strip()
            if "reserved number" in notes.lower():
                continue
            kind = r["unit_type"].strip().lower()
            if kind not in KINDS:
                continue
            rows.append((r["unit"].strip(), kind, (r.get("station") or "").strip(),
                         int("spare" in notes.lower()), notes))
        # Chief officers by callsign: dept number + rank digit (2391 = Port Chester's chief).
        chiefs = [(name, "chief", "", 0, "") for cs, name in sorted(callsigns.items())
                  if cs[:3] in dept_numbers]
        now = self.clock()
        with self.tx() as db:
            for k, (name, kind, station, spare, notes) in enumerate(chiefs + rows):
                db.execute("INSERT INTO units (name, kind, station, status, status_at, spare, notes, sort) "
                           "VALUES (?, ?, ?, 'available', ?, ?, ?, ?)", (name, kind, station, now, spare, notes, k))
        return len(chiefs) + len(rows)

    def units(self) -> list[dict]:
        rows = self.q("SELECT u.*, a.incident_id, i.number AS incident_number FROM units u "
                      "LEFT JOIN assignments a ON a.unit_id = u.id AND a.cleared_at IS NULL "
                      "LEFT JOIN incidents i ON i.id = a.incident_id ORDER BY u.sort, u.id")
        for r in rows:
            r["spare"] = bool(r["spare"])
        return rows

    def unit(self, uid: int) -> dict:
        u = next((u for u in self.units() if u["id"] == uid), None)
        if u is None:
            raise CadError(f"no unit {uid}")
        return u

    def set_unit_status(self, uid: int, status: str, operator: str = "", note: str = "") -> dict:
        """A status button. Free / out of service clears the unit off its incident; dispatched,
        en route and on scene stamp the assignment's times."""
        if status not in STATUSES:
            raise CadError(f"unknown status {status!r}; one of {', '.join(STATUSES)}")
        u = self.unit(uid)
        now = self.clock()
        with self.tx() as db:
            a = db.execute("SELECT * FROM assignments WHERE unit_id = ? AND cleared_at IS NULL", (uid,)).fetchone()
            db.execute("UPDATE units SET status = ?, status_at = ?, out_of_service = ? WHERE id = ?",
                       (status, now, note if status == "out_of_service" else "", uid))
            if a is not None:
                if status in ON_TIMES:
                    db.execute(f"UPDATE assignments SET {ON_TIMES[status]} = COALESCE({ON_TIMES[status]}, ?) "
                               "WHERE id = ?", (now, a["id"]))
                elif status in FREE or status == "out_of_service":
                    db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
            text = f"{u['name']}: {label(status)}" + (f" ({note})" if note and status == "out_of_service" else "")
            db.execute("INSERT INTO events (incident_id, unit_id, at, kind, text, operator) VALUES (?, ?, ?, 'status', ?, ?)",
                       (a["incident_id"] if a is not None else None, uid, now, text, operator))
        return self.unit(uid)

    # -- incidents --------------------------------------------------------------------------------

    def _next_number(self, db, at: float) -> str:
        year = local_year(at)
        db.execute("INSERT INTO seq (year, last) VALUES (?, 1) ON CONFLICT(year) DO UPDATE SET last = last + 1", (year,))
        n = db.execute("SELECT last FROM seq WHERE year = ?", (year,)).fetchone()[0]
        return f"PCFD-{year}-{n:04d}"

    def create_incident(self, *, type: str, address: str = "", cross_street: str = "", narrative: str = "",
                        lat: float | None = None, lon: float | None = None, geo_label: str = "",
                        priority: int | None = None, source: str = "manual", source_ref: str | None = None,
                        status: str = "active", operator: str = "") -> int:
        """A manual incident is active at once (a person entered it) and numbered; one from a
        60 Control page waits as pending with no number until someone accepts it."""
        if source not in SOURCES:
            raise CadError(f"source: one of {', '.join(SOURCES)}")
        if status not in ("pending", "active"):
            raise CadError("a new incident is pending or active")
        now = self.clock()
        with self.tx() as db:
            number = self._next_number(db, now) if status == "active" else None
            cur = db.execute(
                "INSERT INTO incidents (number, type, priority, address, cross_street, lat, lon, geo_label, narrative, "
                "source, source_ref, status, created_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (number, type, priority, address, cross_street, lat, lon, geo_label, narrative, source, source_ref,
                 status, now, now if status == "active" else None))
            iid = cur.lastrowid
            what = f"{type or 'Incident'}" + (f" at {address}" if address else "")
            db.execute("INSERT INTO events (incident_id, at, kind, text, operator) VALUES (?, ?, 'created', ?, ?)",
                       (iid, now, (f"Created {number}: " if number else f"Pending from {source}: ") + what, operator))
        return iid

    def by_source_ref(self, ref: str) -> dict | None:
        return self.one("SELECT * FROM incidents WHERE source_ref = ?", (ref,))

    def incident(self, iid: int) -> dict:
        inc = self.one("SELECT * FROM incidents WHERE id = ?", (iid,))
        if inc is None:
            raise CadError(f"no incident {iid}")
        return inc

    EDITABLE = ("type", "priority", "address", "cross_street", "lat", "lon", "geo_label", "narrative")

    def update_incident(self, iid: int, fields: dict, operator: str = "", log: bool = True) -> dict:
        inc = self.incident(iid)
        if inc["status"] in ("closed", "dismissed"):
            raise CadError(f"incident is {inc['status']}")
        changes = {k: v for k, v in fields.items() if k in self.EDITABLE and inc[k] != v}
        if not changes:
            return inc
        with self.tx() as db:
            db.execute(f"UPDATE incidents SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                       (*changes.values(), iid))
            shown = [k for k in changes if k not in ("lat", "lon", "geo_label", "narrative")]
            parts = [f"{k.replace('_', ' ')}: {changes[k]}" for k in shown]
            if "narrative" in changes:
                parts.append("narrative updated")
            if log and parts:
                db.execute("INSERT INTO events (incident_id, at, kind, text, operator) VALUES (?, ?, 'edit', ?, ?)",
                           (iid, self.clock(), "Edited " + "; ".join(parts), operator))
        return self.incident(iid)

    def accept(self, iid: int, operator: str = "") -> dict:
        inc = self.incident(iid)
        if inc["status"] != "pending":
            raise CadError(f"incident is {inc['status']}, not pending")
        now = self.clock()
        with self.tx() as db:
            number = self._next_number(db, now)
            db.execute("UPDATE incidents SET status = 'active', number = ?, accepted_at = ? WHERE id = ?", (number, now, iid))
            db.execute("INSERT INTO events (incident_id, at, kind, text, operator) VALUES (?, ?, 'accepted', ?, ?)",
                       (iid, now, f"Accepted as {number}", operator))
        return self.incident(iid)

    def dismiss(self, iid: int, operator: str = "", reason: str = "") -> dict:
        inc = self.incident(iid)
        if inc["status"] != "pending":
            raise CadError(f"incident is {inc['status']}, not pending")
        now = self.clock()
        with self.tx() as db:
            db.execute("UPDATE incidents SET status = 'dismissed', closed_at = ? WHERE id = ?", (now, iid))
            db.execute("INSERT INTO events (incident_id, at, kind, text, operator) VALUES (?, ?, 'dismissed', ?, ?)",
                       (iid, now, "Dismissed" + (f": {reason}" if reason else ""), operator))
        return self.incident(iid)

    def close(self, iid: int, operator: str = "") -> dict:
        """Closed: every unit still on it is cleared and back in service ("available")."""
        inc = self.incident(iid)
        if inc["status"] != "active":
            raise CadError(f"incident is {inc['status']}, not active")
        now = self.clock()
        with self.tx() as db:
            on = db.execute("SELECT a.id, a.unit_id, u.name, u.status FROM assignments a JOIN units u ON u.id = a.unit_id "
                            "WHERE a.incident_id = ? AND a.cleared_at IS NULL", (iid,)).fetchall()
            for a in on:
                db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
                if a["status"] != "out_of_service":
                    db.execute("UPDATE units SET status = 'available', status_at = ? WHERE id = ?", (now, a["unit_id"]))
            db.execute("UPDATE incidents SET status = 'closed', closed_at = ? WHERE id = ?", (now, iid))
            names = ", ".join(a["name"] for a in on)
            db.execute("INSERT INTO events (incident_id, at, kind, text, operator) VALUES (?, ?, 'closed', ?, ?)",
                       (iid, now, "Closed" + (f"; cleared {names}" if names else ""), operator))
        return self.incident(iid)

    def note(self, iid: int, text: str, operator: str = "") -> None:
        self.incident(iid)
        with self.tx() as db:
            db.execute("INSERT INTO events (incident_id, at, kind, text, operator) VALUES (?, ?, 'note', ?, ?)",
                       (iid, self.clock(), text, operator))

    def assign(self, iid: int, unit_ids: list[int], operator: str = "") -> list[str]:
        """Units dispatched to the incident. A unit committed elsewhere is moved (its old
        assignment cleared, noted on both timelines); one already on this incident stays."""
        now = self.clock()
        moved = []
        with self.tx() as db:
            for uid in unit_ids:
                u = db.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone()
                a = db.execute("SELECT a.*, i.number FROM assignments a JOIN incidents i ON i.id = a.incident_id "
                               "WHERE a.unit_id = ? AND a.cleared_at IS NULL", (uid,)).fetchone()
                if a is not None and a["incident_id"] == iid:
                    continue
                if a is not None:
                    db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
                    db.execute("INSERT INTO events (incident_id, unit_id, at, kind, text, operator) VALUES (?, ?, ?, 'status', ?, ?)",
                               (a["incident_id"], uid, now, f"{u['name']} reassigned to another incident", operator))
                    moved.append(f"{u['name']} (from {a['number']})")
                db.execute("INSERT INTO assignments (incident_id, unit_id, dispatched_at) VALUES (?, ?, ?)", (iid, uid, now))
                db.execute("UPDATE units SET status = 'dispatched', status_at = ? WHERE id = ?", (now, uid))
        return moved

    def log_dispatch(self, iid: int, *, operator: str, text: str, unit_ids: list[int], audio_path: str,
                     audio_note: str, transmitted: bool, channel: str, tx_note: str) -> int:
        now = self.clock()
        with self.tx() as db:
            cur = db.execute("INSERT INTO dispatches (incident_id, at, operator, text, unit_ids, audio_path, audio_note, "
                             "transmitted, channel, tx_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (iid, now, operator, text, json.dumps(unit_ids), audio_path, audio_note, int(transmitted),
                              channel, tx_note))
            how = f"transmitted on {channel}" if transmitted else f"NOT transmitted ({tx_note})"
            db.execute("INSERT INTO events (incident_id, at, kind, text, operator) VALUES (?, ?, 'dispatch', ?, ?)",
                       (iid, now, f"Dispatch, {how}: {text}", operator))
            return cur.lastrowid

    def dispatch_row(self, did: int) -> dict | None:
        return self.one("SELECT * FROM dispatches WHERE id = ?", (did,))

    def incidents(self, closed_s: float = 12 * 3600, closed_max: int = 20) -> list[dict]:
        """Pending and active ones, plus those closed or dismissed in the last closed_s."""
        cutoff = self.clock() - closed_s
        rows = self.q("SELECT * FROM incidents WHERE status IN ('pending', 'active') ORDER BY created_at DESC")
        rows += self.q("SELECT * FROM incidents WHERE status IN ('closed', 'dismissed') AND closed_at >= ? "
                       "ORDER BY closed_at DESC LIMIT ?", (cutoff, closed_max))
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        assigns = self.q(f"SELECT a.*, u.name, u.kind, u.status FROM assignments a JOIN units u ON u.id = a.unit_id "
                         f"WHERE a.incident_id IN ({marks}) ORDER BY a.id", ids)
        events = self.q(f"SELECT * FROM events WHERE incident_id IN ({marks}) ORDER BY at, id", ids)
        disp = self.q(f"SELECT id, incident_id, at, operator, text, unit_ids, audio_path != '' AS has_audio, audio_note, "
                      f"transmitted, channel, tx_note FROM dispatches WHERE incident_id IN ({marks}) ORDER BY at", ids)
        by = {r["id"]: dict(r, assignments=[], events=[], dispatches=[]) for r in rows}
        for a in assigns:
            by[a["incident_id"]]["assignments"].append(a)
        for e in events:
            by[e["incident_id"]]["events"].append(e)
        for d in disp:
            d["transmitted"], d["has_audio"] = bool(d["transmitted"]), bool(d["has_audio"])
            d["unit_ids"] = json.loads(d["unit_ids"])
            by[d["incident_id"]]["dispatches"].append(d)
        for r in by.values():
            r["events"] = r["events"][-300:]
        return list(by.values())

    def unit_log(self, limit: int = 50) -> list[dict]:
        """Status changes with no incident (out of service, back in quarters)."""
        return self.q("SELECT * FROM events WHERE incident_id IS NULL ORDER BY at DESC, id DESC LIMIT ?", (limit,))


LABELS = {"available": "Available", "dispatched": "Dispatched", "enroute": "En route", "onscene": "On scene",
          "transporting": "Transporting", "available-in-quarters": "In quarters", "out_of_service": "Out of service"}


def label(status: str) -> str:
    return LABELS.get(status, status)
