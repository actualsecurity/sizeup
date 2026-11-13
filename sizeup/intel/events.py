"""The intel event store: SqlEvents (cad.sqlite, the live default: Litestream keeps it, the fence guards it) and
MemoryEvents (replay, tests: nothing written anywhere). Both answer the same few calls the engine makes.

Tables (created IF NOT EXISTS here; no SCHEMA_VERSION bump):

    intel_events   one per event (sizeup/intel/model.py Event): no transcript text, ever; traffic_ids are references
    intel_hits     one per (traffic id, rule): every hit, shadow and below-threshold too (recall), 30 days
    intel_feedback a person's verdict on an event (useful | noise | wrong_place | wrong_type | late | missed), a year

Events are kept 90 days (the radio log's own default for context traffic), hits 30.
"""

from __future__ import annotations

import json
import threading
from dataclasses import fields

from sizeup.intel.model import Event

KEEP_EVENTS_S = 90 * 86400
KEEP_HITS_S = 30 * 86400
KEEP_FEEDBACK_S = 366 * 86400
VERDICTS = ("useful", "noise", "wrong_place", "wrong_type", "late", "missed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intel_events (
  id INTEGER PRIMARY KEY,
  domain TEXT NOT NULL, pack TEXT NOT NULL, category TEXT NOT NULL, subject TEXT NOT NULL,
  source_key TEXT NOT NULL DEFAULT '', system TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
  dept TEXT NOT NULL DEFAULT '',
  first_at REAL NOT NULL, last_at REAL NOT NULL, until REAL NOT NULL,
  status TEXT NOT NULL,                              -- open | ended | expired | dismissed
  what_score REAL NOT NULL DEFAULT 0, level TEXT NOT NULL DEFAULT '',
  rules TEXT NOT NULL DEFAULT '[]',                  -- [[rule id, rev, weight]]
  ruleset_hash TEXT NOT NULL DEFAULT '',
  where_score REAL NOT NULL DEFAULT 0, precision TEXT NOT NULL DEFAULT 'none',
  lat REAL, lon REAL, radius_m REAL NOT NULL DEFAULT 0,
  label_full TEXT NOT NULL DEFAULT '', town TEXT NOT NULL DEFAULT '', alts TEXT NOT NULL DEFAULT '[]',
  basis TEXT NOT NULL DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 0,
  traffic_ids TEXT NOT NULL DEFAULT '[]',
  mode TEXT NOT NULL DEFAULT 'log', priority INTEGER NOT NULL DEFAULT 3,
  pushed_at REAL, push_count INTEGER NOT NULL DEFAULT 0, suppressed TEXT NOT NULL DEFAULT '',
  near_incident INTEGER, incident_id INTEGER,
  updated_at REAL NOT NULL,
  dismissed_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS intel_events_status ON intel_events(status, until);
CREATE INDEX IF NOT EXISTS intel_events_source ON intel_events(source_key, last_at);
CREATE INDEX IF NOT EXISTS intel_events_cat ON intel_events(domain, category, first_at);
CREATE TABLE IF NOT EXISTS intel_hits (
  traffic_id TEXT NOT NULL, rule_id TEXT NOT NULL, rule_rev INTEGER NOT NULL, weight REAL NOT NULL,
  category TEXT NOT NULL, pack TEXT NOT NULL, event_id INTEGER, at REAL NOT NULL,
  UNIQUE(traffic_id, rule_id)
);
CREATE INDEX IF NOT EXISTS intel_hits_at ON intel_hits(at);
CREATE INDEX IF NOT EXISTS intel_hits_rule ON intel_hits(rule_id, at);
CREATE TABLE IF NOT EXISTS intel_feedback (
  id INTEGER PRIMARY KEY,
  event_id INTEGER, traffic_id TEXT,
  verdict TEXT NOT NULL,
  corrected TEXT NOT NULL DEFAULT '',                -- {lat, lon, precision} when a marker is dragged (PR 5)
  corrected_category TEXT NOT NULL DEFAULT '',
  rules TEXT NOT NULL DEFAULT '[]', ruleset_hash TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT '', at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS intel_feedback_event ON intel_feedback(event_id);
"""

JSON_COLS = ("rules", "alts", "traffic_ids")
EVENT_COLS = tuple(f.name for f in fields(Event) if f.name != "id")


def row_event(r: dict) -> Event:
    d = {k: r[k] for k in EVENT_COLS if k in r}
    for k in JSON_COLS:
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except ValueError:
            d[k] = []
    d["confirmed"] = bool(d.get("confirmed"))
    return Event(id=r["id"], **d)


class MemoryEvents:
    """Events in memory only (replay, tests)."""

    def __init__(self):
        self.rows: dict[int, Event] = {}
        self._next = 1
        self._lock = threading.Lock()

    def open_events(self) -> list[Event]:
        with self._lock:
            return [e for e in self.rows.values() if e.status == "open"]

    def get(self, eid: int) -> Event | None:
        return self.rows.get(eid)

    def save(self, ev: Event) -> Event:
        with self._lock:
            if ev.id is None:
                ev.id = self._next
                self._next += 1
            self.rows[ev.id] = ev
        return ev

    def all(self) -> list[Event]:
        return sorted(self.rows.values(), key=lambda e: e.id or 0)


class SqlEvents:
    """Events in cad.sqlite (the live store)."""

    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript(SCHEMA)

    def open_events(self) -> list[Event]:
        return [row_event(r) for r in self.store.q("SELECT * FROM intel_events WHERE status = 'open'")]

    def get(self, eid: int) -> Event | None:
        r = self.store.one("SELECT * FROM intel_events WHERE id = ?", (eid,))
        return row_event(r) if r else None

    def save(self, ev: Event) -> Event:
        vals = []
        for k in EVENT_COLS:
            v = getattr(ev, k)
            vals.append(json.dumps(v) if k in JSON_COLS else int(v) if isinstance(v, bool) else v)
        with self.store.tx() as db:
            if ev.id is None:
                cur = db.execute(f"INSERT INTO intel_events ({', '.join(EVENT_COLS)}) VALUES ({', '.join('?' * len(EVENT_COLS))})",
                                 vals)
                ev.id = int(cur.lastrowid)
            else:
                db.execute(f"UPDATE intel_events SET {', '.join(k + ' = ?' for k in EVENT_COLS)} WHERE id = ?", (*vals, ev.id))
        return ev

    def rows(self, where: str = "1", args=(), limit: int = 200) -> list[dict]:
        out = []
        for r in self.store.q(f"SELECT * FROM intel_events WHERE {where} ORDER BY last_at DESC, id DESC LIMIT ?", (*args, limit)):
            e = row_event(r).as_dict()
            e["dismissed_by"] = r.get("dismissed_by") or ""
            out.append(e)
        return out

    def dismiss(self, eid: int, operator: str, now: float) -> bool:
        with self.store.tx() as db:
            return bool(db.execute("UPDATE intel_events SET status = 'dismissed', dismissed_by = ?, updated_at = ? "
                                   "WHERE id = ? AND status = 'open'", (operator[:120], now, eid)).rowcount)

    def feedback(self, eid: int | None, tid: str | None, verdict: str, operator: str, now: float,
                 corrected: dict | None = None, corrected_category: str = "") -> int:
        ev = self.get(eid) if eid is not None else None
        with self.store.tx() as db:
            cur = db.execute("INSERT INTO intel_feedback (event_id, traffic_id, verdict, corrected, corrected_category, rules, "
                             "ruleset_hash, operator, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (eid, tid, verdict, json.dumps(corrected) if corrected else "", corrected_category[:40],
                              json.dumps(ev.rules if ev else []), ev.ruleset_hash if ev else "", operator[:120], now))
            return int(cur.lastrowid)

    def hit(self, h) -> None:
        with self.store.tx() as db:
            db.execute("INSERT INTO intel_hits (traffic_id, rule_id, rule_rev, weight, category, pack, event_id, at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(traffic_id, rule_id) DO UPDATE SET rule_rev = excluded.rule_rev, "
                       "weight = excluded.weight, category = excluded.category, pack = excluded.pack, "
                       "event_id = COALESCE(excluded.event_id, intel_hits.event_id), at = excluded.at",
                       (h.traffic_id, h.rule_id, h.rule_rev, h.weight, h.category, h.pack, h.event_id, h.at))

    def clear_hits(self, tid: str, keep: set[str]) -> None:
        """A re-transcription: the rules that no longer hit this transmission go (idempotent per traffic id)."""
        with self.store.tx() as db:
            if keep:
                db.execute(f"DELETE FROM intel_hits WHERE traffic_id = ? AND rule_id NOT IN ({','.join('?' * len(keep))})",
                           (tid, *sorted(keep)))
            else:
                db.execute("DELETE FROM intel_hits WHERE traffic_id = ?", (tid,))

    def stats(self, since: float) -> dict:
        """{rule id: {hits, events}} since then (Settings > Intel's table)."""
        out: dict = {}
        for r in self.store.q("SELECT rule_id, COUNT(*) AS hits, COUNT(DISTINCT event_id) AS events FROM intel_hits "
                              "WHERE at >= ? GROUP BY rule_id", (since,)):
            out[r["rule_id"]] = {"hits": r["hits"], "events": r["events"]}
        return out

    def counts(self, since: float) -> dict:
        r = self.store.one("SELECT COUNT(*) AS events, SUM(pushed_at IS NOT NULL) AS pushed, "
                           "SUM(suppressed != '') AS suppressed FROM intel_events WHERE first_at >= ?", (since,)) or {}
        return {"events": r.get("events") or 0, "pushed": r.get("pushed") or 0, "suppressed": r.get("suppressed") or 0}

    def prune(self, now: float) -> int:
        with self.store.tx() as db:
            n = db.execute("DELETE FROM intel_events WHERE last_at < ? AND status != 'open'", (now - KEEP_EVENTS_S,)).rowcount
            n += db.execute("DELETE FROM intel_hits WHERE at < ?", (now - KEEP_HITS_S,)).rowcount
            n += db.execute("DELETE FROM intel_feedback WHERE at < ?", (now - KEEP_FEEDBACK_S,)).rowcount
        return n
