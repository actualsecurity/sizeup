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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sizeup.audiostore import PREFIX, StoreError, object_key
from sizeup.fence import GuardedConnection
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
CREATE INDEX IF NOT EXISTS audio_key ON audio (key);
"""
RECENT_BYTES = 16 * 1024 * 1024      # the newest clips kept in memory too (the transcription reads them at once)
KNOWN_IDS = 50_000                   # has(): the ids recently seen with audio, in memory (the rest: one indexed read)
UPLOAD_BACKOFF_S = (5.0, 300.0)      # upload_pending after a failure: 5 s doubling to 5 min
DELETE_CHUNK = 1000                  # rows looked at per DeleteObjects call (S3's limit)
SCAN_PAGE = 5000                     # the prune looks at the clips this many at a time (a short read each, no lock held)
ORPHAN_AGE_S = 86400.0               # the orphan sweep: an object with no row this old (never one a row names)
ORPHAN_MAX_N = 200                   # ... and its circuit breaker: more orphans than this in one sweep, nothing deleted
ORPHAN_SHARE = 0.02                  # ... or more than this share of the clip objects looked at
ORPHAN_MIN_ALLOW = 20                # ... (a small bucket: up to this many whatever the share)
SWEEP_MARGIN_S = 3600.0              # ... and never an object uploaded within this of the newest clip audio.sqlite knows


def _connect(path: str, incremental: bool = False) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # the writer's connection: no write gets through while its may_write() says False (the hub's fence, #501)
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, factory=GuardedConnection)
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

    @property
    def may_write(self) -> Callable[[], bool]:
        """Only the leader writes hub.sqlite (the fence, set by HubApp; #501): the connection skips every write else."""
        return getattr(self, "_may_write", None) or self.db.may_write

    @may_write.setter
    def may_write(self, fn: Callable[[], bool]) -> None:
        self._may_write = self.db.may_write = fn

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
    """Clips and dispatch audio, by id.

    With a store (sizeup/audiostore.py, #414) the bytes end up there and a row keeps only the metadata (kind, channel,
    talkgroup, at, duration, bytes, key, etag). put() never waits on the store: it writes the row with the bytes
    (stored 'db': audio.sqlite is the spool, replicated by Litestream, so a crash or a handover loses nothing) and
    wakes the uploader; upload_pending() then puts the object and only after that switches the row to it (wav x'',
    stored 'store'), so a row never points at nothing. The rows from before #414 are the same backlog (the migration).
    A row in the store has an empty `wav`, so an older release reads it as a clip with no audio (a 404), never a
    crash. Without a store: as before (`wav` only).

    Reads go through a second, read-only connection (WAL: they never wait for a write), so the prune's scan, the
    Settings page's counts and the players don't hold the writer's lock. has() keeps only the ids recently seen in
    memory (KNOWN_IDS), not every row's."""

    def __init__(self, path: str | Path, max_bytes: int = 500 * 1024 * 1024, store=None, cache=None,
                 may_write: Callable[[], bool] | None = None, log: Callable[[str], None] = print,
                 may_delete: Callable[[], bool] | None = None):
        self.path = str(path)
        self.db = _connect(self.path, incremental=True)
        self.db.executescript(AUDIO_SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(audio)")}
        for col, decl in AUDIO_COLUMNS:
            if col not in cols:                    # an audio.sqlite from before #414: metadata columns added, rows kept
                self.db.execute(f"ALTER TABLE audio ADD COLUMN {col} {decl}")
        self.db.executescript(AUDIO_INDEXES)
        if self.path == ":memory:":
            self.ro = self.scan = self.db          # (tests: one connection, the same lock)
        else:
            self.ro, self.scan = self._reader(), self._reader()
        self.max_bytes = int(max_bytes)
        self.store, self.cache, self.log = store, cache, log
        self.may_write = may_write or (lambda: True)      # the writer connection's own guard too (#501)
        # deletes from the store (the retention's, the orphan sweep's): only with a real lease that is ours and fresh
        # (the hub's fence); without one (a local hub, a test) nothing is ever deleted from a store
        self.may_delete = may_delete or (lambda: False)
        self._lock = threading.Lock()              # the writer
        self._rlock = self._lock if self.ro is self.db else threading.Lock()       # the hot reads: has, get, put's key
        self._slock = self._lock if self.ro is self.db else threading.Lock()       # the long ones: prune, Settings, sweep
        self._channels: tuple[float, list[dict]] = (-1e9, [])
        self._mem = threading.Lock()               # the in-memory caches below
        self._known: OrderedDict[str, None] = OrderedDict()     # ids seen with audio lately (has())
        self._recent: OrderedDict[str, bytes] = OrderedDict()   # the newest clips' bytes: the STT and Replay at once
        self._recent_bytes = 0
        self._upload_mu = threading.Lock()         # one uploader (or orphan sweep) at a time
        self._upload_after = 0.0                   # monotonic: upload_pending waits until this (backoff)
        self._upload_fails = 0
        self._said: dict[str, float] = {}
        self._missing = 0
        self.wake = threading.Event()              # put() -> the uploader, at once

    @property
    def may_write(self) -> Callable[[], bool]:
        return getattr(self, "_may_write", None) or self.db.may_write

    @may_write.setter
    def may_write(self, fn: Callable[[], bool]) -> None:
        self._may_write = self.db.may_write = fn

    def _say(self, what: str, msg: str, every_s: float = 600.0) -> None:
        now = time.monotonic()
        if now - self._said.get(what, -1e9) >= every_s:
            self._said[what] = now
            self.log(msg)

    def _reader(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("PRAGMA query_only = 1")
        return c

    def _read(self, sql: str, params=()) -> list:
        with self._rlock:
            return self.ro.execute(sql, params).fetchall()

    def _scan(self, sql: str, params=()) -> list:
        with self._slock:
            return self.scan.execute(sql, params).fetchall()

    def _seen(self, aid: str) -> None:
        with self._mem:
            self._known[aid] = None
            self._known.move_to_end(aid)
            while len(self._known) > KNOWN_IDS:
                self._known.popitem(last=False)

    def _remember(self, aid: str, wav: bytes) -> None:
        if len(wav) > RECENT_BYTES // 4:
            return
        with self._mem:
            old = self._recent.pop(aid, None)
            self._recent_bytes -= len(old or b"")
            self._recent[aid] = wav
            self._recent_bytes += len(wav)
            while self._recent_bytes > RECENT_BYTES and self._recent:
                self._recent_bytes -= len(self._recent.popitem(last=False)[1])

    def _forget(self, ids) -> None:
        with self._mem:
            for aid in ids:
                old = self._recent.pop(aid, None)
                self._recent_bytes -= len(old or b"")
                self._known.pop(aid, None)

    def put(self, aid: str, wav: bytes, kind: str = "clip", note: str = "", at: float | None = None,
            channel: str = "", talkgroup: int | None = None) -> None:
        """The row with its bytes, at once (never waits on the store: a page or a transmission is never held up by
        S3); the uploader is woken to move the bytes into the store. Readable at once (memory, then the row)."""
        at = time.time() if at is None else at
        r = self._read("SELECT key FROM audio WHERE id = ?", (aid,))
        key = r[0][0] if r and r[0][0] else ""
        if not key:
            try:
                key = object_key(kind, at, aid)
            except ValueError:
                key = ""                           # not a name a store takes: kept in the database
        tg = int(talkgroup) if isinstance(talkgroup, int) or (isinstance(talkgroup, str) and talkgroup.isdigit()) else None
        with self._lock:
            self.db.execute(
                "INSERT INTO audio (id, kind, at, note, wav, channel, talkgroup, duration, bytes, key, etag, stored) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'db') ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, "
                "at = excluded.at, note = excluded.note, wav = excluded.wav, channel = excluded.channel, "
                "talkgroup = excluded.talkgroup, duration = excluded.duration, bytes = excluded.bytes, key = excluded.key, "
                "etag = '', stored = 'db'",
                (aid, kind, at, note, wav, str(channel or "")[:80], tg, wav_seconds(wav), len(wav), key))
        self._seen(aid)
        self._remember(aid, wav)
        if self.store is not None:
            self.wake.set()

    def has(self, aid: str) -> bool:
        with self._mem:
            if aid in self._known:
                return True
        if self._read("SELECT 1 FROM audio WHERE id = ?", (aid,)):
            self._seen(aid)
            return True
        return False

    def get(self, aid: str, strict: bool = False) -> bytes | None:
        """The bytes wherever they are: the newest clips from memory, a row still holding them, the disk cache, the
        store (then cached). None when there is no such clip. When the clip exists but the store can't be read now:
        None, or with strict (the attachments copy) a StoreError, so the caller tries again instead of taking
        "no audio" for an answer."""
        with self._mem:
            hit = self._recent.get(aid)
            if hit is not None:
                self._recent.move_to_end(aid)
                return hit
        r = self._read("SELECT wav, key, stored FROM audio WHERE id = ?", (aid,))
        if not r:
            return None
        wav, key, stored = r[0]
        if wav:
            return bytes(wav)
        if stored != "store" or not key:
            return None                            # a row with no audio at all (nothing to try again)
        if self.cache is not None and (b := self.cache.get(key)) is not None:
            return b
        if self.store is None:
            self._say("nostore", f"audio: {aid} is in a store, but none is configured (SIZEUP_AUDIO_BUCKET): no audio")
            if strict:
                raise StoreError(f"audio {aid}: in a store, none configured")
            return None
        try:
            b = self.store.get(key)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "missing", False):           # really gone (NoSuchKey): no audio, for good (a copy: transcript only)
                self._missing += 1
                self._say("missing", f"audio: {self._missing} clip(s) so far whose object is not in the store "
                                     f"(NoSuchKey, the latest {aid}): no audio for them")
                return None
            if getattr(e, "config", False):            # the bucket gone, access denied: the store's setup, said loudly
                self._say("config", f"audio: THE AUDIO STORE REFUSES READS ({str(e)[:160]}): check SIZEUP_AUDIO_BUCKET "
                                    "and the task role (sizeup-aws 18-audio-storage.sh)", 300.0)
            self._say("get", f"audio: could not read {aid} from the store: {type(e).__name__}: {str(e)[:120]}", 60.0)
            if strict:
                raise StoreError(f"audio {aid}: {type(e).__name__}: {str(e)[:120]}") from None
            return None
        if self.cache is not None:
            self.cache.put(key, b)
        return b

    def note(self, aid: str) -> str:
        r = self._read("SELECT note FROM audio WHERE id = ?", (aid,))
        return r[0][0] if r else ""

    # -- into the store: the rows still holding their bytes (every new clip, the rows from before #414) ----------------

    def pending(self) -> tuple[int, int]:
        """(rows whose bytes are still in audio.sqlite, their bytes)."""
        n, b = self._read("SELECT COUNT(*), COALESCE(SUM(LENGTH(wav)), 0) FROM audio WHERE stored = 'db' "
                          "AND LENGTH(wav) > 0")[0]
        return int(n), int(b)

    def upload_pending(self, limit: int = 20, pause_s: float = 0.0) -> int:
        """At most `limit` rows' bytes into the store, newest first (the clips just in, then the backlog). A row is
        switched to its key only once the object is there, and only if it still holds the very bytes uploaded (a
        compare-and-set on the bytes: a put() of new bytes meanwhile keeps the row 'db', and they go next round).
        Only the leader (may_write), checked before every object and before the switch; one uploader at a time. A
        failure stops the round and backs off (5 s doubling to 5 min). Safe to stop and run again at any point (a
        restart, a handover): the PUT of the same key is idempotent. The number moved."""
        if self.store is None or time.monotonic() < self._upload_after:
            return 0
        with self._upload_mu:
            rows = self._read("SELECT id, kind, at, key FROM audio WHERE stored = 'db' AND LENGTH(wav) > 0 "
                              "ORDER BY at DESC LIMIT ?", (int(limit),))
            n = 0
            for aid, kind, at, key in rows:
                if not self.may_write():
                    break
                try:
                    key = key or object_key(kind if kind in ("clip", "dispatch") else "clip", at, aid)
                except ValueError:
                    continue
                r = self._read("SELECT wav FROM audio WHERE id = ? AND stored = 'db'", (aid,))
                if not r or not r[0][0]:
                    continue
                wav = bytes(r[0][0])
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
                    break                          # the lease went while we uploaded: the next leader switches it
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
        ids = [r[0] for r in self._scan("SELECT id FROM audio WHERE kind = 'clip' AND channel = ''")]
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
                    max_rows: int | None = None, policy=None, now: float | None = None,
                    context: Callable[[list[str]], dict[str, tuple[int, bool]]] | None = None,
                    context_max_bytes: int | None = None) -> int:
        """The audio retention (#251, #414, docs/cloud.md "Audio retention"). Clips (kind 'clip'; never the dispatch
        audio) go once older than their own retention: the policy's (sizeup/audiostore.py Retention: the channel's or
        talkgroup's days in Settings, worked out in SQL at every prune, so a change applies to the clips already
        kept), or older_than without one; older_than is also where the scan stops (the shortest retention). The
        ones kept() says are a record (on a CAD event, an alarm, an emergency) go only once also older than
        kept_older_than (None: never). Then, while the bytes still in audio.sqlite are over the size cap, the
        oldest of those that aren't a record. The scan reads SCAN_PAGE rows at a time on the read connection (no
        lock held between pages); store objects go first (DeleteObjects, 1000 a call), then the rows whose objects
        are gone, in transactions of at most `batch` rows, pause_s apart; at most max_rows this call (None: all).
        The number deleted.

        Context audio (#409: another P25 system's clips; context(ids) -> {id: (rank, extended)}: rank 0 goes first,
        extended: its tier still keeps it past its retention) never costs our own audio anything. A tier only extends
        a clip past its retention; context clips have their own budget (context_max_bytes, over their bytes wherever
        kept: over it the lowest rank, then the oldest, go); and over the cap every context clip goes before any clip
        of ours."""
        now = time.time() if now is None else now
        expr, params = policy.sql(now) if policy is not None else ("?", [older_than])
        n, cur_at, cur_rowid = 0, float("-inf"), -1
        after = "kind = 'clip' AND at < ? AND at >= ? AND (at > ? OR rowid > ?)"     # (at >= : the index's range)
        while True:
            # where this page ends: the index alone (kind, at, rowid), SCAN_PAGE rows on; then only that page's due
            # clips come back to Python (the channel / talkgroup cutoffs worked out in SQL)
            end = self._scan(f"SELECT at, rowid FROM audio WHERE {after} ORDER BY at, rowid LIMIT 1 OFFSET ?",
                             (older_than, cur_at, cur_at, cur_rowid, SCAN_PAGE - 1))
            last = end[0] if end else (older_than, 1 << 62)
            due = self._scan(f"SELECT id, at FROM audio WHERE {after} AND at <= ? AND (at < ? OR rowid <= ?) "
                             f"AND at < ({expr}) ORDER BY at, rowid",
                             (older_than, cur_at, cur_at, cur_rowid, last[0], last[0], last[1], *params))
            cur_at, cur_rowid = last
            if due:
                ids = [a for a, _ in due]
                keep = kept(ids) if kept is not None else set()
                ext = context(ids) if context is not None else {}
                gone = [aid for aid, at in due
                        if (aid not in keep and not ext.get(aid, (0, False))[1])
                        or (aid in keep and kept_older_than is not None and at < kept_older_than)]
                if max_rows is not None:
                    gone = gone[:max(0, max_rows - n)]
                n += self._delete(gone, batch, pause_s)
                if max_rows is not None and n >= max_rows:
                    return n
            if not end:
                break
        total = self.bytes()
        over: list[str] = []
        ctx: dict[str, tuple[int, bool]] = {}
        if context is not None and (context_max_bytes is not None or total > self.max_bytes):
            # every clip with its bytes wherever kept (audio.sqlite or the store), oldest first
            sizes = self._scan("SELECT id, CASE WHEN LENGTH(wav) > 0 THEN LENGTH(wav) ELSE bytes END, stored, "
                               "LENGTH(wav) FROM audio WHERE kind = 'clip' ORDER BY at")
            ctx = context([r[0] for r in sizes]) if sizes else {}
            if ctx:
                # context clips: the lowest tier first, then the oldest (sizes is oldest first: a stable sort keeps that)
                mine = sorted((r for r in sizes if r[0] in ctx), key=lambda r: ctx[r[0]][0])
                used = sum(r[1] or 0 for r in mine)
                budget = context_max_bytes if context_max_bytes is not None else used
                for aid, size, stored, in_db in mine:
                    if used <= budget and total <= self.max_bytes:
                        break
                    # inside the budget, only a clip still in audio.sqlite helps the cap (the cap counts those bytes
                    # alone): one already in the store stays
                    if used <= budget and not (stored == 'db' and in_db):
                        continue
                    over.append(aid)
                    used -= size or 0
                    if stored == 'db':
                        total -= in_db or 0
        if total > self.max_bytes:
            sizes = self._scan("SELECT id, LENGTH(wav) FROM audio WHERE stored = 'db' AND kind = 'clip' "
                               "AND LENGTH(wav) > 0 ORDER BY at")
            keep = kept([r[0] for r in sizes]) if kept is not None and sizes else set()
            for aid, size in sizes:
                if total <= self.max_bytes:
                    break
                if aid in keep or aid in ctx:
                    continue
                over.append(aid)
                total -= size
        if over:
            n += self._delete(over, batch, pause_s)
        if total > self.max_bytes:
            self.over_cap = total                   # the records alone are over the cap: kept (the retention decides)
        return n

    over_cap = 0

    def _delete(self, ids: list[str], batch: int, pause_s: float) -> int:
        """The store's objects first: every key a row names, whatever its state (a 'db' row's bytes may have been
        uploaded before a crash); a row whose object couldn't be deleted stays, tried again next time. Then the rows.
        Nothing at all unless we may write."""
        if not ids or not self.may_write():
            return 0
        n = 0
        batch = max(1, int(batch))
        for k in range(0, len(ids), DELETE_CHUNK):
            chunk = ids[k:k + DELETE_CHUNK]
            marks = ",".join("?" * len(chunk))
            keyed = {aid: (key, stored) for aid, key, stored in
                     self._read(f"SELECT id, key, stored FROM audio WHERE key != '' AND id IN ({marks})", chunk)}
            if keyed:
                if self.store is None:
                    if any(s == "store" for _, s in keyed.values()):
                        self._say("nostore-del", "audio: rows in a store, but none is configured (SIZEUP_AUDIO_BUCKET): "
                                                 "the rows go, their objects are left to the bucket's lifecycle")
                elif not self.may_delete():
                    self._say("nodelete", "audio: no lease (fence) to delete from the store: clips past their retention "
                                          "are kept")
                    chunk = [a for a in chunk if a not in keyed]
                else:
                    try:
                        gone_keys = self.store.delete(sorted({key for key, _ in keyed.values()}))
                    except Exception as e:  # noqa: BLE001
                        self._say("delete", f"audio: could not delete from the store ({type(e).__name__}: "
                                            f"{str(e)[:120]}): tried again next time")
                        return n
                    if self.cache is not None:
                        self.cache.drop(gone_keys)
                    chunk = [a for a in chunk if a not in keyed or keyed[a][0] in gone_keys]
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
                self._forget(part)
                n += len(part)
                if pause_s and (j + batch < len(chunk) or k + DELETE_CHUNK < len(ids)):
                    time.sleep(pause_s)
        return n

    def sweep_orphans(self, min_age_s: float = ORPHAN_AGE_S, pause_s: float = 0.0, now: float | None = None,
                      known: Callable[[list[str]], dict[str, tuple[str, int | None, float]]] | None = None) -> int:
        """Clip objects in the store that no row names (a release before #414 pruned the row, a crash between a
        delete's two halves), older than min_age_s: deleted. Never dispatch audio (only audio/clip/), never a date
        folder older than the oldest clip audio.sqlite knows (the bucket's lifecycle has those), never an object
        uploaded within SWEEP_MARGIN_S of the newest clip it knows (a restore a little behind), never a key a row
        names, and only with a real lease (may_delete). An object whose clip id the rest of the hub still knows
        (known(ids) -> {id: (channel, talkgroup, at)}: the CAD's radio log, the monitor) is not an orphan: its row is
        made again from its key and what is known (sweep_restored), never deleted; without known() nothing is. A look
        first, over the whole listing; then a circuit breaker: more orphans than ORPHAN_MAX_N, or than ORPHAN_SHARE of
        what was looked at (at least ORPHAN_MIN_ALLOW), or no clip rows at all, and nothing is deleted (an empty, an
        old or a wrong audio.sqlite would otherwise empty the bucket): sweep_status says why. Then the deletes, each
        batch looked at again under the uploader's lock. The number deleted."""
        self.sweep_status, self.sweep_restored = "", 0
        if self.store is None or not self.may_delete():
            return 0
        if known is None:
            self.sweep_status = "nothing to check the clip ids against: not swept"
            return 0
        now = time.time() if now is None else now
        oldest, newest, rows = self._scan("SELECT MIN(at), MAX(at), COUNT(*) FROM audio WHERE kind = 'clip' AND key != ''")[0]
        if not rows:
            self.sweep_status = "no clips in audio.sqlite: not swept"
            return 0
        first = object_key("clip", oldest, "x").rsplit("/", 1)[0] + "/"     # audio/clip/<yyyy>/<mm>/<dd>/
        before = min(now - min_age_s, newest - SWEEP_MARGIN_S)
        looked, orphans = 0, []
        for page in self.store.pages(f"{PREFIX}/clip/"):
            old = [k for k, t in page if t < before and k >= first]
            if not old:
                continue
            looked += len(old)
            marks = ",".join("?" * len(old))
            named = {r[0] for r in self._scan(f"SELECT key FROM audio WHERE key IN ({marks})", old)}
            unnamed = {k: k.rsplit("/", 1)[-1].removesuffix(".wav") for k in old if k not in named}
            still = known(list(unnamed.values())) if unnamed else {}
            for k, aid in unnamed.items():
                if aid in still:
                    self._restore_row(aid, k, *still[aid])
                else:
                    orphans.append(k)
            if len(orphans) > ORPHAN_MAX_N:
                break
            if pause_s:
                time.sleep(pause_s)
        if len(orphans) > ORPHAN_MAX_N or len(orphans) > max(ORPHAN_MIN_ALLOW, ORPHAN_SHARE * looked):
            self.sweep_status = (f"{len(orphans)}{'+' if len(orphans) > ORPHAN_MAX_N else ''} of {looked} clip objects "
                                 f"named by no row (audio.sqlite has {rows}): too many, nothing deleted")
            return 0
        n = 0
        for k in range(0, len(orphans), DELETE_CHUNK):
            chunk = orphans[k:k + DELETE_CHUNK]
            with self._upload_mu:
                if not self.may_delete():
                    return n
                marks = ",".join("?" * len(chunk))
                named = {r[0] for r in self._scan(f"SELECT key FROM audio WHERE key IN ({marks})", chunk)}
                chunk = [x for x in chunk if x not in named]
                if chunk:
                    gone = self.store.delete(chunk)
                    n += len(gone)
                    if self.cache is not None:
                        self.cache.drop(gone)
        return n

    sweep_status = ""
    sweep_restored = 0

    def _restore_row(self, aid: str, key: str, channel: str, talkgroup: int | None, at: float | None) -> None:
        """A clip the hub still knows whose row audio.sqlite lost (a restore a little behind): made again, pointing at
        its object (sizes unknown: 0), so it plays and its retention applies, instead of the object being deleted."""
        if not self.may_write():
            return
        if not at:
            try:
                y, m, d = (int(x) for x in key.split("/")[2:5])
                at = datetime(y, m, d, 12, tzinfo=timezone.utc).timestamp()
            except (ValueError, IndexError):
                return
        with self._lock:
            done = self.db.execute(
                "INSERT OR IGNORE INTO audio (id, kind, at, note, wav, channel, talkgroup, duration, bytes, key, etag, "
                "stored) VALUES (?, 'clip', ?, '', x'', ?, ?, 0, 0, ?, '', 'store')",
                (aid, float(at), str(channel or "")[:80], talkgroup if isinstance(talkgroup, int) else None, key)).rowcount
        if done:
            self.sweep_restored += 1
            self._seen(aid)

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

    def compact(self, min_free_bytes: int = 32 * 1024 * 1024) -> bool:
        """Once the migration is done (no row holds its bytes), an audio.sqlite from before #251 (auto_vacuum off: its
        free pages never go back) is rewritten once, when that is worth it: at least min_free_bytes free and at least
        as much free as live (the metadata: ~260 bytes a clip, so a few MB at 12 h, ~260 MB at a million clips), with
        auto_vacuum INCREMENTAL, so from then on its free pages go back a few at a time. Only with a store, only by the
        leader. True when it ran."""
        if self.store is None or not self.may_write() or self.auto_vacuum() == 2 or self.pending()[0]:
            return False
        with self._lock:
            pages, free, size = (int(self.db.execute(f"PRAGMA {p}").fetchone()[0])
                                 for p in ("page_count", "freelist_count", "page_size"))
            if free * size < max(min_free_bytes, (pages - free) * size):
                return False
            self.db.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self.db.execute("VACUUM")
        return True

    def prune_dispatch(self, keep: set[str], older_than: float) -> int:
        """Previews nobody dispatched, after a day (the dispatch log's audio stays)."""
        rows = [r[0] for r in self._scan("SELECT id FROM audio WHERE kind = 'dispatch' AND at < ?", (older_than,))
                if r[0] not in keep]
        return self._delete(rows, 200, 0.0)

    def bytes(self) -> int:
        """The audio bytes in audio.sqlite itself (not the store's)."""
        return int(self._read("SELECT COALESCE(SUM(LENGTH(wav)), 0) FROM audio WHERE stored = 'db'")[0][0])

    def channels(self, max_age_s: float = 300.0) -> list[dict]:
        """Per channel / talkgroup (Settings > System > Audio retention): clips, their bytes, the oldest. Aggregates,
        worked out at most every max_age_s (a whole-table read: ~1.4 s at a million clips) on the scan connection."""
        at, got = self._channels
        if time.monotonic() - at < max_age_s:
            return got
        rows = self._scan(
            "SELECT channel, talkgroup, COUNT(*), COALESCE(SUM(CASE WHEN bytes > 0 THEN bytes ELSE LENGTH(wav) END), 0), "
            "MIN(at) FROM audio WHERE kind = 'clip' GROUP BY channel, talkgroup ORDER BY channel, talkgroup")
        got = [{"channel": ch, "talkgroup": tg, "clips": int(n), "bytes": int(b), "oldest": oldest}
               for ch, tg, n, b, oldest in rows]
        self._channels = (time.monotonic(), got)
        return got

    def close(self) -> None:
        if self.ro is not self.db:
            self.ro.close()
            self.scan.close()
        self.db.close()


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
