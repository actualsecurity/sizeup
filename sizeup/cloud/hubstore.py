"""The hub keeps everything in SQLite under its state dir, so Litestream can replicate it and a
redeploy (Fargate: the disk is thrown away) restores it. All-in-one keeps JSON files and WAVs
under captures/ instead; the hub has no captures/ at all.

    hub.sqlite     the monitor's state: incidents, transmissions, pages (one row each, written only
                   when it changed: not the whole list on every transmission, which Litestream would
                   ship again each time), and how far the station's events have been applied
    audio.sqlite   received clips (storage.audio_keep_hours, and a size cap: oldest first) and the
                   dispatch audio the station rendered (kept with the dispatch log; previews a day)
    cad.sqlite     the CAD (sizeup/cad/store.py), as everywhere
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from sizeup.incidents import Incident, IncidentStore, Traffic

MONITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, updated REAL NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traffic (id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, start REAL NOT NULL,
                                    end REAL NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS traffic_incident ON traffic (incident_id, start);
CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, start REAL NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

AUDIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio (id TEXT PRIMARY KEY, kind TEXT NOT NULL, at REAL NOT NULL, note TEXT NOT NULL DEFAULT '',
                                  wav BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS audio_kind_at ON audio (kind, at);
"""


def _connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.execute("PRAGMA journal_mode = WAL")          # Litestream needs WAL
    db.execute("PRAGMA busy_timeout = 5000")         # ... and holds a read lock while it copies
    return db


def _dump(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"), sort_keys=True, default=str)


class MonitorDb:
    def __init__(self, path: str | Path):
        self.db = _connect(str(path))
        self.db.executescript(MONITOR_SCHEMA)
        self._lock = threading.Lock()
        self._written: dict[tuple[str, str], str] = {}     # (table, id) -> doc as last written

    # -- meta (the station's events applied so far) ---------------------------------------------------

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            r = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (key, value))

    # -- the monitor's incidents, transmissions and pages ------------------------------------------------

    def load(self, store: IncidentStore) -> list[dict]:
        """Fill the store (incidents with their traffic, loose traffic) and return the page docs,
        newest first. Everything is loaded: the hub's janitor prunes to the history limit, and the
        next sync() deletes what it dropped."""
        with self._lock:
            incs = self.db.execute("SELECT id, doc FROM incidents ORDER BY updated").fetchall()
            traffic = self.db.execute("SELECT id, incident_id, doc FROM traffic ORDER BY start").fetchall()
            pages = self.db.execute("SELECT id, doc FROM pages ORDER BY start DESC").fetchall()
        by_inc: dict[str, list[Traffic]] = {}
        for tid, iid, doc in traffic:
            self._written[("traffic", tid)] = f"{iid}\x00{doc}"
            t = Traffic.from_dict(json.loads(doc))
            if iid:
                by_inc.setdefault(iid, []).append(t)
            else:
                store.loose.append(t)
        for iid, doc in incs:
            self._written[("incidents", iid)] = doc
            inc = Incident.from_json(dict(json.loads(doc), traffic=[]))
            inc.traffic = by_inc.get(iid, [])
            store.incidents.append(inc)
        out = []
        for pid, doc in pages:
            self._written[("pages", pid)] = doc
            out.append(json.loads(doc))
        return out

    def sync(self, store: IncidentStore, page_docs: list[dict]) -> int:
        """Write what changed since the last sync (and delete what the store no longer has). Returns
        the rows written."""
        rows: dict[tuple[str, str], tuple] = {}
        with store._lock:
            incs = list(store.incidents)
            loose = list(store.loose)
            for inc in incs:
                d = inc.to_json()
                d.pop("traffic", None)
                doc = _dump(d)
                rows[("incidents", inc.id)] = (doc, inc.updated)
                for t in inc.traffic:
                    td = _dump(t.as_dict())
                    rows[("traffic", t.id)] = (f"{inc.id}\x00{td}", t.start, t.end, inc.id, td)
            for t in loose:
                td = _dump(t.as_dict())
                rows.setdefault(("traffic", t.id), (f"\x00{td}", t.start, t.end, "", td))
        for p in page_docs:
            rows[("pages", p["id"])] = (_dump(p), p.get("start") or 0.0)
        n = 0
        with self._lock:
            self.db.execute("BEGIN")
            try:
                for (table, key), v in rows.items():
                    if self._written.get((table, key)) == v[0]:
                        continue
                    if table == "incidents":
                        self.db.execute("INSERT INTO incidents (id, updated, doc) VALUES (?, ?, ?) ON CONFLICT(id) DO "
                                        "UPDATE SET updated = excluded.updated, doc = excluded.doc", (key, v[1], v[0]))
                    elif table == "traffic":
                        self.db.execute("INSERT INTO traffic (id, incident_id, start, end, doc) VALUES (?, ?, ?, ?, ?) "
                                        "ON CONFLICT(id) DO UPDATE SET incident_id = excluded.incident_id, "
                                        "start = excluded.start, end = excluded.end, doc = excluded.doc",
                                        (key, v[3], v[1], v[2], v[4]))
                    else:
                        self.db.execute("INSERT INTO pages (id, start, doc) VALUES (?, ?, ?) ON CONFLICT(id) DO "
                                        "UPDATE SET start = excluded.start, doc = excluded.doc", (key, v[1], v[0]))
                    self._written[(table, key)] = v[0]
                    n += 1
                for (table, key) in [k for k in self._written if k not in rows]:
                    self.db.execute(f"DELETE FROM {table} WHERE id = ?", (key,))
                    del self._written[(table, key)]
                    n += 1
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return n


class AudioDb:
    """Clips and dispatch audio, by id. ids() is kept in memory: the display asks "is there audio
    for this?" for every row, every second."""

    def __init__(self, path: str | Path, max_bytes: int = 500 * 1024 * 1024):
        self.db = _connect(str(path))
        self.db.executescript(AUDIO_SCHEMA)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._ids = {r[0] for r in self.db.execute("SELECT id FROM audio")}

    def put(self, aid: str, wav: bytes, kind: str = "clip", note: str = "", at: float | None = None) -> None:
        with self._lock:
            self.db.execute("INSERT INTO audio (id, kind, at, note, wav) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO "
                            "UPDATE SET kind = excluded.kind, at = excluded.at, note = excluded.note, wav = excluded.wav",
                            (aid, kind, time.time() if at is None else at, note, wav))
            self._ids.add(aid)

    def has(self, aid: str) -> bool:
        return aid in self._ids

    def get(self, aid: str) -> bytes | None:
        if aid not in self._ids:
            return None
        with self._lock:
            r = self.db.execute("SELECT wav FROM audio WHERE id = ?", (aid,)).fetchone()
        return bytes(r[0]) if r else None

    def note(self, aid: str) -> str:
        with self._lock:
            r = self.db.execute("SELECT note FROM audio WHERE id = ?", (aid,)).fetchone()
        return r[0] if r else ""

    def prune_clips(self, older_than: float) -> int:
        """Clips past the audio limit, then the oldest clips while over the size cap."""
        with self._lock:
            gone = [r[0] for r in self.db.execute("SELECT id FROM audio WHERE kind = 'clip' AND at < ?", (older_than,))]
            self.db.execute("DELETE FROM audio WHERE kind = 'clip' AND at < ?", (older_than,))
            total = self.db.execute("SELECT COALESCE(SUM(LENGTH(wav)), 0) FROM audio").fetchone()[0]
            if total > self.max_bytes:
                for aid, n in self.db.execute("SELECT id, LENGTH(wav) FROM audio WHERE kind = 'clip' ORDER BY at").fetchall():
                    if total <= self.max_bytes:
                        break
                    self.db.execute("DELETE FROM audio WHERE id = ?", (aid,))
                    gone.append(aid)
                    total -= n
            self._ids.difference_update(gone)
        return len(gone)

    def prune_dispatch(self, keep: set[str], older_than: float) -> int:
        """Previews nobody dispatched, after a day (the dispatch log's audio stays)."""
        with self._lock:
            rows = [r[0] for r in self.db.execute("SELECT id FROM audio WHERE kind = 'dispatch' AND at < ?", (older_than,))
                    if r[0] not in keep]
            for aid in rows:
                self.db.execute("DELETE FROM audio WHERE id = ?", (aid,))
            self._ids.difference_update(rows)
        return len(rows)

    def bytes(self) -> int:
        with self._lock:
            return int(self.db.execute("SELECT COALESCE(SUM(LENGTH(wav)), 0) FROM audio").fetchone()[0])
