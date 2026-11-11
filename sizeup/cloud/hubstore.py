"""The hub keeps everything in SQLite under its state dir, so Litestream can replicate it and a
redeploy (Fargate: the disk is thrown away) restores it. All-in-one keeps JSON files and WAVs
under captures/ instead; the hub has no captures/ at all.

    hub.sqlite     the monitor's state: incidents, transmissions, pages (one row each, written only
                   when it changed: not the whole list on every transmission, which Litestream would
                   ship again each time), and how far the station's events have been applied
    audio.sqlite   received clips (the retention per channel / talkgroup, and a size cap: oldest first) and the
                   dispatch audio the station rendered (kept with the dispatch log; previews a day): with a store
                   (SIZEUP_AUDIO_BUCKET, #414) their metadata only, the bytes in S3 (sizeup/audiostore.py)
    cad.sqlite     the CAD (sizeup/cad/store.py), as everywhere
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from sizeup.audiostore import object_key
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
# #414: the metadata of audio kept in a store (sizeup/audiostore.py); added to an older file by ALTER TABLE, so the rows
# an older release writes (it names its five columns) still fit, and it reads the new ones as clips without audio.
AUDIO_COLUMNS = (
    ("channel", "TEXT NOT NULL DEFAULT ''"),        # "FG1", "South 2", a talkgroup's name: the retention's key
    ("talkgroup", "INTEGER"),                       # P25: the talkgroup ("tg:<n>" in Settings)
    ("duration", "REAL NOT NULL DEFAULT 0"),        # seconds (the WAV header)
    ("bytes", "INTEGER NOT NULL DEFAULT 0"),        # the WAV's size, wherever it is (0: a row from before #414)
    ("key", "TEXT NOT NULL DEFAULT ''"),            # its key in the store: audio/<kind>/<yyyy>/<mm>/<dd>/<id>.wav
    ("etag", "TEXT NOT NULL DEFAULT ''"),           # the store's ETag (its MD5)
    ("stored", "TEXT NOT NULL DEFAULT 'db'"),       # db: the bytes are in `wav` | store: in the store, `wav` is x''
)
AUDIO_INDEXES = """
CREATE INDEX IF NOT EXISTS audio_pending ON audio (at) WHERE stored = 'db';
"""
RECENT_BYTES = 16 * 1024 * 1024      # the newest clips kept in memory too (the transcription reads them at once)
DIRECT_BACKOFF_S = 60.0              # after a failed PUT on the ingest path, the next clips go to the database this long
UPLOAD_BACKOFF_S = (5.0, 300.0)      # upload_pending after a failure: 5 s doubling to 5 min
DELETE_CHUNK = 1000                  # rows looked at per DeleteObjects call (S3's limit)


def _connect(path: str, incremental: bool = False) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    if incremental and not db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        # a new file: free pages handed back a few at a time, off-peak (AudioDb.incremental_vacuum, #251); before
        # WAL, which writes the header. An older file keeps auto_vacuum off (turning it on is a whole VACUUM, a
        # long lock): its free pages are reused instead.
        db.execute("PRAGMA auto_vacuum = INCREMENTAL")
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
    for this?" for every row, every second.

    With a store (sizeup/audiostore.py, #414) the bytes live there and a row keeps only the metadata (kind, channel,
    talkgroup, at, duration, bytes, key, etag): put() writes the object first, then the row (a crash in between leaves
    an object nobody points at, which the bucket's lifecycle removes, never a row pointing at nothing). When the
    store can't take it (S3 down, not the leader) the row keeps the bytes in `wav` (stored 'db'): Litestream has
    them, so nothing is lost at a crash or a handover, and upload_pending() moves them to the store later, as it
    moves the rows from before #414 (the migration). A row in the store has an empty `wav` (x''), so an older
    release reads it as a clip with no audio (a 404), never a crash. Without a store: as before (`wav` only)."""

    def __init__(self, path: str | Path, max_bytes: int = 500 * 1024 * 1024, store=None, cache=None,
                 may_write: Callable[[], bool] | None = None, log: Callable[[str], None] = print):
        self.db = _connect(str(path), incremental=True)
        self.db.executescript(AUDIO_SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(audio)")}
        for col, decl in AUDIO_COLUMNS:
            if col not in cols:                    # an audio.sqlite from before #414: metadata columns added, rows kept
                self.db.execute(f"ALTER TABLE audio ADD COLUMN {col} {decl}")
        self.db.executescript(AUDIO_INDEXES)
        self.max_bytes = int(max_bytes)
        self.store, self.cache, self.log = store, cache, log
        self.may_write = may_write or (lambda: True)
        self._lock = threading.Lock()
        self._ids = {r[0] for r in self.db.execute("SELECT id FROM audio")}
        self._recent: OrderedDict[str, bytes] = OrderedDict()   # the newest clips' bytes: the STT and Replay at once
        self._recent_bytes = 0
        self._direct_after = 0.0                  # monotonic: no PUT on the ingest path before this (the store failed)
        self._upload_after = 0.0                  # monotonic: upload_pending waits until this (backoff)
        self._upload_fails = 0
        self._said: dict[str, float] = {}

    def _say(self, what: str, msg: str, every_s: float = 600.0) -> None:
        now = time.monotonic()
        if now - self._said.get(what, -1e9) >= every_s:
            self._said[what] = now
            self.log(msg)

    def _remember(self, aid: str, wav: bytes) -> None:
        if len(wav) > RECENT_BYTES // 4:
            return
        with self._lock:
            old = self._recent.pop(aid, None)
            self._recent_bytes -= len(old or b"")
            self._recent[aid] = wav
            self._recent_bytes += len(wav)
            while self._recent_bytes > RECENT_BYTES and self._recent:
                self._recent_bytes -= len(self._recent.popitem(last=False)[1])

    def _forget(self, ids) -> None:
        for aid in ids:
            old = self._recent.pop(aid, None)
            self._recent_bytes -= len(old or b"")

    def put(self, aid: str, wav: bytes, kind: str = "clip", note: str = "", at: float | None = None,
            channel: str = "", talkgroup: int | None = None) -> None:
        """The object first (when there is a store and we may write), then the row; the store failing: the row keeps
        the bytes (upload_pending later). Readable at once either way (the newest clips are kept in memory too)."""
        at = time.time() if at is None else at
        with self._lock:
            r = self.db.execute("SELECT key FROM audio WHERE id = ?", (aid,)).fetchone()
        key = r[0] if r and r[0] else ""
        if not key:
            try:
                key = object_key(kind, at, aid)
            except ValueError:
                key = ""                           # not a name a store takes: kept in the database
        etag, stored = "", "db"
        if self.store is not None and key and self.may_write() and time.monotonic() >= self._direct_after:
            try:
                etag, stored = self.store.put(key, wav), "store"
            except Exception as e:  # noqa: BLE001 (S3 down, no credentials): the database keeps it until it is back
                self._direct_after = time.monotonic() + DIRECT_BACKOFF_S
                self._say("put", f"audio: could not store {kind} {aid} ({type(e).__name__}: {str(e)[:120]}): kept in "
                                 "audio.sqlite, uploaded once the store answers")
        if stored == "store" and self.cache is not None:
            self.cache.put(key, wav)
        tg = int(talkgroup) if isinstance(talkgroup, int) or (isinstance(talkgroup, str) and talkgroup.isdigit()) else None
        with self._lock:
            self.db.execute(
                "INSERT INTO audio (id, kind, at, note, wav, channel, talkgroup, duration, bytes, key, etag, stored) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, "
                "at = excluded.at, note = excluded.note, wav = excluded.wav, channel = excluded.channel, "
                "talkgroup = excluded.talkgroup, duration = excluded.duration, bytes = excluded.bytes, key = excluded.key, "
                "etag = excluded.etag, stored = excluded.stored",
                (aid, kind, at, note, b"" if stored == "store" else wav, str(channel or "")[:80], tg, wav_seconds(wav),
                 len(wav), key, etag, stored))
            self._ids.add(aid)
        self._remember(aid, wav)

    def has(self, aid: str) -> bool:
        return aid in self._ids

    def get(self, aid: str) -> bytes | None:
        """The bytes wherever they are: the newest clips from memory, a row still holding them, the disk cache, the
        store (then cached). None when there are none or the store can't be read now."""
        if aid not in self._ids:
            return None
        with self._lock:
            hit = self._recent.get(aid)
            if hit is not None:
                self._recent.move_to_end(aid)
                return hit
            r = self.db.execute("SELECT wav, key, stored FROM audio WHERE id = ?", (aid,)).fetchone()
        if r is None:
            return None
        wav, key, stored = r
        if wav:
            return bytes(wav)
        if stored != "store" or not key:
            return None
        if self.cache is not None and (b := self.cache.get(key)) is not None:
            return b
        if self.store is None:
            self._say("nostore", f"audio: {aid} is in a store, but none is configured (SIZEUP_AUDIO_BUCKET): no audio")
            return None
        try:
            b = self.store.get(key)
        except Exception as e:  # noqa: BLE001
            self._say("get", f"audio: could not read {aid} from the store: {type(e).__name__}: {str(e)[:120]}", 60.0)
            return None
        if self.cache is not None:
            self.cache.put(key, b)
        return b

    def note(self, aid: str) -> str:
        with self._lock:
            r = self.db.execute("SELECT note FROM audio WHERE id = ?", (aid,)).fetchone()
        return r[0] if r else ""

    # -- into the store: the rows still holding their bytes (a failed PUT, the rows from before #414) ----------------

    def pending(self) -> tuple[int, int]:
        """(rows whose bytes are still in audio.sqlite, their bytes)."""
        with self._lock:
            n, b = self.db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(wav)), 0) FROM audio WHERE stored = 'db' "
                                   "AND LENGTH(wav) > 0").fetchone()
        return int(n), int(b)

    def upload_pending(self, limit: int = 20, pause_s: float = 0.0) -> int:
        """At most `limit` rows' bytes into the store, newest first (a clip the ingest couldn't store, then the
        backlog), the row switched to its key only once the object is there, and only if the row still holds the
        same bytes. Only the leader (may_write), checked before every object and before the switch. A failure stops
        the round and backs off (5 s doubling to 5 min). Safe to stop and run again at any point (a restart, a
        handover): the PUT of the same key is idempotent. The number moved."""
        if self.store is None or time.monotonic() < self._upload_after:
            return 0
        with self._lock:
            rows = self.db.execute("SELECT id, kind, at, key FROM audio WHERE stored = 'db' AND LENGTH(wav) > 0 "
                                   "ORDER BY at DESC LIMIT ?", (int(limit),)).fetchall()
        n = 0
        for aid, kind, at, key in rows:
            if not self.may_write():
                break
            try:
                key = key or object_key(kind if kind in ("clip", "dispatch") else "clip", at, aid)
            except ValueError:
                continue
            with self._lock:
                r = self.db.execute("SELECT wav FROM audio WHERE id = ? AND stored = 'db'", (aid,)).fetchone()
            if not r or not r[0]:
                continue
            wav = bytes(r[0])
            try:
                etag = self.store.put(key, wav)
            except Exception as e:  # noqa: BLE001
                self._upload_fails += 1
                self._upload_after = time.monotonic() + min(UPLOAD_BACKOFF_S[1],
                                                            UPLOAD_BACKOFF_S[0] * 2 ** (self._upload_fails - 1))
                self._say("upload", f"audio: upload to the store failed ({type(e).__name__}: {str(e)[:120]}): "
                                    "kept in audio.sqlite, tried again later")
                break
            self._upload_fails = 0
            if not self.may_write():
                break                              # the lease went while we uploaded: the next leader switches it
            with self._lock:
                done = self.db.execute("UPDATE audio SET wav = x'', key = ?, etag = ?, bytes = ?, duration = ?, "
                                       "stored = 'store' WHERE id = ? AND stored = 'db' AND wav = ?",
                                       (key, etag, len(wav), wav_seconds(wav), aid, wav)).rowcount
            if done:
                n += 1
                if self.cache is not None and time.time() - at < 3600:
                    self.cache.put(key, wav)
            if pause_s:
                time.sleep(pause_s)
        return n

    def backfill_channels(self, lookup: Callable[[list[str]], dict[str, tuple[str, int | None]]],
                          batch: int = 500) -> int:
        """The clips from before #414 have no channel: lookup(ids) -> {id: (channel, talkgroup)} (the hub's
        transmissions and pages, the CAD's radio log), so the per-channel retention applies to them too. The number
        named; the rest keep the default retention."""
        with self._lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM audio WHERE kind = 'clip' AND channel = ''")]
        n = 0
        for k in range(0, len(ids), batch):
            got = lookup(ids[k:k + batch]) or {}
            rows = [(str(ch or "")[:80], tg if isinstance(tg, int) else None, aid) for aid, (ch, tg) in got.items() if ch]
            if rows and self.may_write():
                with self._lock:
                    self.db.executemany("UPDATE audio SET channel = ?, talkgroup = ? WHERE id = ? AND channel = ''", rows)
                n += len(rows)
        return n

    # -- the retention -------------------------------------------------------------------------------------------

    def prune_clips(self, older_than: float, kept: Callable[[list[str]], set[str]] | None = None,
                    kept_older_than: float | None = None, batch: int = 200, pause_s: float = 0.0,
                    max_rows: int | None = None, keep_for: Callable[[str, int | None], float] | None = None,
                    now: float | None = None) -> int:
        """The audio retention (#251, #414, docs/cloud.md "Audio retention"). Clips (kind 'clip'; never the dispatch
        audio) go once older than their own retention: keep_for(channel, talkgroup) seconds (the channel's or
        talkgroup's in Settings, read at each prune, so a change applies to the clips already kept), or older_than
        without keep_for; older_than is also where the scan starts (the shortest retention). The ones kept() says
        are a record (on a CAD event, an alarm, an emergency) go only once also older than kept_older_than (None:
        never). Then, while the bytes still in audio.sqlite are over the size cap, the oldest of those that aren't a
        record. Store objects first (DeleteObjects, 1000 a call), then the rows whose objects are gone, in
        transactions of at most `batch` rows, pause_s apart; at most max_rows this call (None: all). The number
        deleted."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self.db.execute("SELECT id, at, channel, talkgroup FROM audio WHERE kind = 'clip' AND at < ? ORDER BY at",
                                   (older_than,)).fetchall()
        due = [(aid, at) for aid, at, ch, tg in rows if keep_for is None or at < now - keep_for(ch, tg)]
        keep = kept([a for a, _ in due]) if kept is not None and due else set()
        gone = [aid for aid, at in due if aid not in keep or (kept_older_than is not None and at < kept_older_than)]
        if max_rows is not None:
            gone = gone[:max_rows]
        n = self._delete(gone, batch, pause_s)
        if max_rows is not None and n >= max_rows:
            return n
        with self._lock:
            total = self.db.execute("SELECT COALESCE(SUM(LENGTH(wav)), 0) FROM audio").fetchone()[0]
            over = []
            if total > self.max_bytes:
                sizes = self.db.execute("SELECT id, LENGTH(wav) FROM audio WHERE kind = 'clip' AND LENGTH(wav) > 0 "
                                        "ORDER BY at").fetchall()
        if total > self.max_bytes:
            keep = kept([r[0] for r in sizes]) if kept is not None and sizes else set()
            for aid, size in sizes:
                if total <= self.max_bytes:
                    break
                if aid in keep:
                    continue
                over.append(aid)
                total -= size
            n += self._delete(over, batch, pause_s)
            if total > self.max_bytes:
                self.over_cap = total               # the records alone are over the cap: kept (the retention decides)
        return n

    over_cap = 0

    def _delete(self, ids: list[str], batch: int, pause_s: float) -> int:
        """The store's objects first (a row whose object couldn't be deleted stays, tried again next time), then the
        rows. Nothing at all unless we may write."""
        if not ids or not self.may_write():
            return 0
        n = 0
        batch = max(1, int(batch))
        for k in range(0, len(ids), DELETE_CHUNK):
            chunk = ids[k:k + DELETE_CHUNK]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                keyed = dict(self.db.execute(f"SELECT id, key FROM audio WHERE stored = 'store' AND key != '' "
                                             f"AND id IN ({marks})", chunk).fetchall())
            if keyed:
                if self.store is None:
                    self._say("nostore-del", "audio: rows in a store, but none is configured (SIZEUP_AUDIO_BUCKET): the "
                                             "rows go, their objects are left to the bucket's lifecycle")
                else:
                    try:
                        gone_keys = self.store.delete(list(keyed.values()))
                    except Exception as e:  # noqa: BLE001
                        self._say("delete", f"audio: could not delete from the store ({type(e).__name__}: "
                                            f"{str(e)[:120]}): tried again next time")
                        return n
                    if self.cache is not None:
                        self.cache.drop(gone_keys)
                    chunk = [a for a in chunk if a not in keyed or keyed[a] in gone_keys]
            for j in range(0, len(chunk), batch):
                part = chunk[j:j + batch]
                if not self.may_write():
                    return n
                with self._lock:
                    self.db.execute("BEGIN")
                    try:
                        self.db.executemany("DELETE FROM audio WHERE id = ?", [(a,) for a in part])
                        self.db.execute("COMMIT")
                    except Exception:
                        self.db.execute("ROLLBACK")
                        raise
                    self._ids.difference_update(part)
                    self._forget(part)
                n += len(part)
                if pause_s and (j + batch < len(chunk) or k + DELETE_CHUNK < len(ids)):
                    time.sleep(pause_s)
        return n

    def auto_vacuum(self) -> int:
        """0 none, 1 full, 2 incremental."""
        with self._lock:
            return int(self.db.execute("PRAGMA auto_vacuum").fetchone()[0])

    def free_pages(self) -> int:
        with self._lock:
            return int(self.db.execute("PRAGMA freelist_count").fetchone()[0])

    def incremental_vacuum(self, max_pages: int = 4096, step: int = 128, pause_s: float = 0.0) -> int:
        """Free pages handed back to the file system, `step` at a time (a short write each), at most max_pages
        this call: only with auto_vacuum incremental (a new audio.sqlite). The pages freed."""
        if self.auto_vacuum() != 2:
            return 0
        done = 0
        while done < max_pages:
            free = self.free_pages()
            if not free:
                break
            k = min(step, free, max_pages - done)
            with self._lock:
                self.db.execute(f"PRAGMA incremental_vacuum({int(k)})").fetchall()
            done += k
            if pause_s:
                time.sleep(pause_s)
        return done

    def compact(self, max_live_bytes: int = 64 * 1024 * 1024) -> bool:
        """Once the migration is done (no row holds its bytes), an audio.sqlite from before #251 (auto_vacuum off: its
        free pages never go back) is rewritten small, once: auto_vacuum INCREMENTAL and a VACUUM, which copies only the
        live pages (the metadata: a few MB). Only when those are under max_live_bytes, only with a store, only by the
        leader. True when it ran."""
        if self.store is None or not self.may_write() or self.auto_vacuum() == 2 or self.pending()[0]:
            return False
        with self._lock:
            pages, free, size = (int(self.db.execute(f"PRAGMA {p}").fetchone()[0])
                                 for p in ("page_count", "freelist_count", "page_size"))
            if (pages - free) * size > max_live_bytes:
                return False
            self.db.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self.db.execute("VACUUM")
        return True

    def prune_dispatch(self, keep: set[str], older_than: float) -> int:
        """Previews nobody dispatched, after a day (the dispatch log's audio stays)."""
        with self._lock:
            rows = [r[0] for r in self.db.execute("SELECT id FROM audio WHERE kind = 'dispatch' AND at < ?", (older_than,))
                    if r[0] not in keep]
        return self._delete(rows, 200, 0.0)

    def bytes(self) -> int:
        """The audio bytes in audio.sqlite itself (not the store's)."""
        with self._lock:
            return int(self.db.execute("SELECT COALESCE(SUM(LENGTH(wav)), 0) FROM audio").fetchone()[0])

    def channels(self) -> list[dict]:
        """Per channel / talkgroup (Settings > System > Audio retention): clips, their bytes, the oldest. Aggregates."""
        with self._lock:
            rows = self.db.execute(
                "SELECT channel, talkgroup, COUNT(*), COALESCE(SUM(CASE WHEN bytes > 0 THEN bytes ELSE LENGTH(wav) END), 0), "
                "MIN(at) FROM audio WHERE kind = 'clip' GROUP BY channel, talkgroup ORDER BY channel, talkgroup").fetchall()
        return [{"channel": ch, "talkgroup": tg, "clips": int(n), "bytes": int(b), "oldest": oldest}
                for ch, tg, n, b, oldest in rows]


def wav_seconds(wav: bytes) -> float:
    """A WAV's length from its header (its data chunk / byte rate); 0 when it isn't one."""
    try:
        if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return 0.0
        rate = struct.unpack("<I", wav[28:32])[0]
        i = wav.find(b"data", 12)
        if i < 0 or not rate:
            return 0.0
        size = struct.unpack("<I", wav[i + 4:i + 8])[0]
        return round(min(size, len(wav) - i - 8) / rate, 2)
    except (struct.error, ValueError):
        return 0.0
