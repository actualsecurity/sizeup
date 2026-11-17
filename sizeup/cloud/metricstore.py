"""The station's signal metrics on the hub (#605; sizeup/cloud/metrics.py sends them): kept to graph each channel's
noise floor, levels and squelch over weeks, and each P25 system's control channel, so a squelch or a gain can be tuned
from data (and a shift after an antenna, splitter or dongle change seen at once).

metrics.sqlite, in the hub's state dir beside the others, but its own file: a busy station's numbers never bloat or
slow cad.sqlite / hub.sqlite, their Litestream replication or the leader's handover. It is NOT in Litestream (neither
deploy/hub/litestream.yml nor sizeup/handoff.py DBS know it), so it adds nothing to a drain or a takeover. Instead:

    settle     before anything is uploaded the store must be SETTLED: the copies in the bucket (sizeup/cloud/
               metricsbackup.py, SLOTS of them, round-robin) were listed and either the newest (and the largest, if
               another) merged in (INSERT OR IGNORE: what came in meanwhile stays; series by min / max), or a listing
               that succeeded showed none. Until then the leader's backup thread tries again (BACKOFF_S, doubling): a
               listing or a download that fails never lets a near-empty store overwrite the history. A local file
               that settled before (meta "settled") is settled at once (a restart in place)
    upload     once settled, FIRST_UPLOAD_S later and then every SNAPSHOT_S: a consistent copy (SQLite's backup API, from
               the read connection: the ingest isn't held up), gzipped, into an empty slot or over the OLDEST copy, so
               the last SLOTS copies stay and one bad copy can't take the history with it. The leader only
    resend     once settled, each station's next post is answered with "resend_from": when the merged copy was made
               (meta snapshot_at; 0 with none). The station keeps what was acked for 6 h and sends it again
               (sizeup/cloud/metrics.py resend): duplicates are ignored, so a deploy loses nothing while the station is
               connected and the restore settles within 6 h. What can be lost: what the old task took after its last
               copy, if the station was away for that whole time or the restore took longer than its 6 h; never
               what an earlier copy holds

Tables (made on first use, never at the hub's start):

    raw      (station, subject, kind, at) -> doc: every floor / tx / cc record as sent (doc: its numbers, JSON),
             RAW_DAYS (30) days. One B-tree (WITHOUT ROWID): a series' range is one seek
    events   (station, subject, at, ev) -> detail: what explains a shift (the station's; the Settings audit's squelch and
             channel changes are joined in when read, from cad.sqlite), EVENT_DAYS (365)
    hourly   (station, subject, metric, hour) -> n, lo, p50, p90, hi: the rollups, HOURLY_DAYS (365)
    series   (station, subject, kind) -> first, last: what there is (the API's index, the retention's list)
    meta     snapshot_at (when this copy was made), settled

Rollups (METRICS below): an hour is rolled again whenever a record of it comes in (a station back after an outage
sends late), and at the first maintain() after a start the last two days are rolled again (in case that was lost).
maintain() runs from the hub's janitor (every 10 min): rollups, then the retention once a day; backup_turn() from the
hub's "metrics-backup" thread (every BACKUP_TURN_S): settle, then upload. Only the leader writes (may_write: the hub's
fence): a standby writes nothing, and a read never makes the file.

Size (docs/cloud.md "Station metrics"; tests/test_station_metrics.py measures it): a day of 6 channels, 2 P25 systems
and 2,000 transmissions is ~13,500 records, ~1.7 MB (~125 bytes a record with its key); its rollups ~1,100 rows,
~80 KB. At 30 days of raw and a year of rollups: ~50 MB + ~30 MB. The copy gzips to a third or less.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup import fence as fence_mod

RAW_DAYS = 30
HOURLY_DAYS = 365
EVENT_DAYS = 365
SNAPSHOT_S = 3 * 3600.0         # a copy to the bucket this often, once settled
FIRST_UPLOAD_S = 900.0          # ... the first this long after settling
BACKOFF_S = (30.0, 1800.0)      # settling or an upload failed: again in 30 s, doubling to 30 min
BACKUP_TURN_S = 30.0            # the backup thread's look
RENAME_CHUNK = 20000            # a station renamed: its rows moved this many a transaction
UNSETTLED_WARN_S = 6 * 3600.0   # Health warns: not settled this long (the stations keep 6 h to resend) ...
COPY_WARN_S = 12 * 3600.0       # ... or no good copy this long
REROLL_S = 48 * 3600.0          # the first maintain() after a start rolls this far back again
KINDS = ("floor", "tx", "cc")
MAX_RECORDS = 5000              # a post
MAX_DOC = 2048                  # one record's numbers, as JSON
MAX_FUTURE_S = 86400.0          # a station clock this far ahead: refused
MAX_AGE_S = RAW_DAYS * 86400.0  # older than the retention: refused
RAW_SPAN_S = 7 * 86400.0        # GET raw: at most a week a call
HOUR_SPAN_S = 400 * 86400.0     # ... hourly: a year and a bit
AUTO_RAW_S = 2 * 86400.0        # res=auto: raw up to two days, then hourly
MAX_ROWS = 20000                # a GET's rows (raw: the newest are cut, "truncated")
SETTINGS = ("kind", "am", "mhz", "open", "close", "min", "sdr", "gain", "state")   # (sizeup/cloud/metrics.py SETTINGS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (station TEXT NOT NULL, subject TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL,
                                doc TEXT NOT NULL, PRIMARY KEY (station, subject, kind, at)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (station TEXT NOT NULL, subject TEXT NOT NULL, at REAL NOT NULL, ev TEXT NOT NULL,
                                   detail TEXT NOT NULL DEFAULT '', PRIMARY KEY (station, subject, at, ev)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hourly (station TEXT NOT NULL, subject TEXT NOT NULL, metric TEXT NOT NULL,
                                   hour INTEGER NOT NULL, n INTEGER NOT NULL, lo REAL, p50 REAL, p90 REAL, hi REAL,
                                   PRIMARY KEY (station, subject, metric, hour)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS series (station TEXT NOT NULL, subject TEXT NOT NULL, kind TEXT NOT NULL, first REAL NOT NULL,
                                   last REAL NOT NULL, PRIMARY KEY (station, subject, kind)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# The hourly rollups: metric -> (record kind, which records, the value). The rest of a record stays in raw only.
METRICS: dict[str, tuple[str, Callable[[dict], bool], Callable[[dict], float | None]]] = {
    "floor":        ("floor", lambda d: True, lambda d: d.get("hiss", d.get("floor"))),   # FM hiss (rel dB) / AM dBFS
    "idle_q50":     ("floor", lambda d: True, lambda d: d.get("q50")),
    "idle_q90":     ("floor", lambda d: True, lambda d: d.get("q90")),
    "idle_qmax":    ("floor", lambda d: True, lambda d: d.get("qmax")),
    "noise_opens":  ("floor", lambda d: True, lambda d: d.get("noise")),
    "open_s":       ("floor", lambda d: True, lambda d: d.get("open_s")),
    "sq_open":      ("floor", lambda d: True, lambda d: d.get("open")),      # the squelch as running (sent when it
    "sq_close":     ("floor", lambda d: True, lambda d: d.get("close")),     # changes and every 15 min): #606's steps
    "tx_q":         ("tx", lambda d: d.get("kind") != "noise", lambda d: d.get("q")),
    "tx_q_open":    ("tx", lambda d: d.get("kind") != "noise", lambda d: d.get("qo")),
    "tx_q_close":   ("tx", lambda d: d.get("kind") != "noise", lambda d: d.get("qc")),
    "tx_dbfs":      ("tx", lambda d: d.get("kind") != "noise", lambda d: d.get("cf")),
    "tx_snr":       ("tx", lambda d: d.get("kind") != "noise", lambda d: d.get("snr")),
    "tx_s":         ("tx", lambda d: d.get("kind") != "noise", lambda d: d.get("dur")),
    "noise_q":      ("tx", lambda d: d.get("kind") == "noise", lambda d: d.get("q")),
    "cc_rate":      ("cc", lambda d: True, lambda d: d.get("rate")),
    "cc_lo":        ("cc", lambda d: True, lambda d: d.get("lo")),
    "cc_uncovered": ("cc", lambda d: True, lambda d: sum((d.get("unc") or {}).values()) if d.get("n") or d.get("unc")
                     else None),
}


class MetricsError(ValueError):
    """A request that can't be answered (fixed text, safe to show)."""


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:                         # an int too big for a float
        return None
    return v if math.isfinite(v) else None


def _pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


def _text(v, n: int) -> str:
    s = str(v if v is not None else "")
    return "".join(c for c in s if c.isprintable())[:n]


def _clean_doc(rec: dict) -> dict:
    """A record's numbers and short words: scalars only (and cc's {MHz: count}), nothing nested beyond that."""
    out = {}
    for k, v in rec.items():
        if k in ("k", "s", "at") or not isinstance(k, str) or len(k) > 12:
            continue
        if isinstance(v, bool):
            out[k] = int(v)
        elif isinstance(v, (int, float)):
            f = _num(v)
            if f is not None:
                out[k] = v if isinstance(v, float) or abs(v) < 2 ** 53 else f
        elif isinstance(v, str):
            out[k] = _text(v, 40)
        elif k == "unc" and isinstance(v, dict):
            out[k] = {_text(f, 14): int(c) for f, c in list(v.items())[:21]
                      if isinstance(c, int) and not isinstance(c, bool) and 0 <= c < 2 ** 31}
    return out


def _common_columns(db: sqlite3.Connection, table: str) -> list[str] | None:
    """The columns of `table` both this store (main) and an attached copy (snap) have, in this store's order; None when
    the copy hasn't the table, or lacks a key column or one here that must have a value (NOT NULL, no default)."""
    def info(schema: str) -> list[tuple]:
        return db.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    mine, theirs = info("main"), {r[1] for r in info("snap")}
    if not theirs:
        return None
    for _cid, name, _type, notnull, default, pk in mine:
        if name not in theirs and (pk or (notnull and default is None)):
            return None
    return [r[1] for r in mine if r[1] in theirs]


class MetricStore:
    def __init__(self, path: str | Path, may_write: Callable[[], bool] = lambda: True,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 audit: Callable[[str, float, float], list[dict]] | None = None, backup=None):
        """audit(subject, t0, t1): the Settings audit's changes to that channel (the hub's cad.sqlite), for the events.
        backup: where the copies are (sizeup/cloud/metricsbackup.py S3Copies, or one like it); None: no copies, the
        store is settled as it is."""
        self.path = str(path)
        self.may_write, self.log, self.clock, self.audit, self.backup = may_write, log, clock, audit, backup
        self._db: sqlite3.Connection | None = None
        self._ro: sqlite3.Connection | None = None
        self._lock = threading.Lock()              # the writer
        self._rlock = threading.Lock()             # the reader
        self._dirty: set[tuple[str, str, int]] = set()
        self._rolled_once = False
        self._reroll_all = False                   # after a merge: every hour raw still has, rolled again once
        self._fresh = False                        # this process made the file (nothing was here)
        self.settled = False                       # the copies looked at (merged, or none there): uploads may start
        self.resend_from: float | None = None      # once settled: what the stations send again (None: nothing lost)
        self._told: set[str] = set()               # ... the stations told so already
        self._next_try = 0.0                       # (clock) settling or uploading: not before this
        self._fails = 0
        self._next_upload: float | None = None
        self.backup_status = {"state": "unsettled", "error": "", "uploaded": None, "restored": None}
        self._unsettled_since: float | None = None   # (clock) the first leader turn that found it unsettled
        self._copy_at: float | None = None         # (clock) the last good copy: uploaded here, or the one merged in
        self._retained_day = -1
        self.ingested = 0

    # -- the file ---------------------------------------------------------------------------------------------------

    def _exists(self) -> bool:
        return self.path == ":memory:" or Path(self.path).exists()

    def _open(self) -> sqlite3.Connection:
        """The writer (made with the file when it isn't there: callers check may_write first)."""
        if self._db is not None:
            return self._db
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._fresh = not self._exists() or self.path == ":memory:"
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA busy_timeout = 5000")
        db.executescript(SCHEMA)
        self._db = db
        if self.path == ":memory:":
            self._ro, self._rlock = db, self._lock
        else:
            ro = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            ro.execute("PRAGMA busy_timeout = 5000")
            ro.execute("PRAGMA query_only = 1")
            self._ro = ro
        if not self.settled:
            was = db.execute("SELECT value FROM meta WHERE key = 'settled'").fetchone()
            if was is not None:                    # a restart in place: this file has its history already
                self._settle(None, "local")
            elif self.backup is None:              # nowhere to look: as it is
                self._settle(0.0 if self._fresh else None, "no copies")
        return db

    def _read(self, sql: str, params=()) -> list:
        if self._db is None:
            if not self._exists():
                return []                          # nothing kept yet: a read never makes the file
            with self._lock:
                self._open()
        with self._rlock:
            return self._ro.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            if self._ro is not None and self._ro is not self._db:
                self._ro.close()
            if self._db is not None:
                self._db.close()
            self._db = self._ro = None

    def _meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                         "excluded.value", (key, value))

    def _settle(self, resend_from: float | None, how: str) -> None:
        """Settled: uploads may start (FIRST_UPLOAD_S from now), and the stations are told what to send again."""
        self.settled, self.resend_from = True, resend_from
        self._told = set()
        self._next_upload = self.clock() + FIRST_UPLOAD_S
        self.backup_status.update(state="settled", how=how, error="")
        if self._db is not None and self.path != ":memory:" and self.may_write():
            try:
                self._meta("settled", how)
            except sqlite3.Error:
                pass

    # -- in ---------------------------------------------------------------------------------------------------------

    def ingest(self, station: str, records: list) -> dict:
        """POST /api/station/metrics from `station` (the authenticated id). What can't be read is skipped and counted,
        never refused whole: {ok, accepted, skipped, resend_from?}. MetricsError for a body that isn't one (the station
        drops it); fence.FencedWrite when this task isn't the leader (a 503: the station sends it again)."""
        if not isinstance(records, list):
            raise MetricsError("records: a list")
        if len(records) > MAX_RECORDS:
            raise MetricsError(f"records: at most {MAX_RECORDS} a post")
        station = _text(station, 64) or "station"
        now = self.clock()
        raw, events, skipped = [], [], 0
        for r in records:
            if not isinstance(r, dict):
                skipped += 1
                continue
            k, s, at = r.get("k"), _text(r.get("s"), 80), _num(r.get("at"))
            if not s or at is None or at > now + MAX_FUTURE_S or at < now - MAX_AGE_S:
                skipped += 1
                continue
            if k == "ev":
                ev = _text(r.get("ev"), 20)
                if not ev:
                    skipped += 1
                    continue
                events.append((station, s, round(at, 3), ev, _text(r.get("d"), 300)))
            elif k in KINDS:
                doc = json.dumps(_clean_doc(r), separators=(",", ":"), sort_keys=True)
                if len(doc) > MAX_DOC:
                    skipped += 1
                    continue
                raw.append((station, s, k, round(at, 3), doc))
            else:
                skipped += 1
        if not self.may_write():
            fence_mod.refuse("station metrics")
        with self._lock:
            db = self._open()
            db.execute("BEGIN")
            try:
                n = 0
                for row in raw:
                    n += db.execute("INSERT OR IGNORE INTO raw (station, subject, kind, at, doc) VALUES (?, ?, ?, ?, ?)",
                                    row).rowcount
                for row in events:
                    n += db.execute("INSERT OR IGNORE INTO events (station, subject, at, ev, detail) VALUES (?, ?, ?, ?, ?)",
                                    row).rowcount
                spans: dict[tuple[str, str, str], list[float]] = {}
                for st, s, k, at, _d in raw:
                    sp = spans.setdefault((st, s, k), [at, at])
                    sp[0], sp[1] = min(sp[0], at), max(sp[1], at)
                for (st, s, k), (a, b) in spans.items():
                    db.execute("INSERT INTO series (station, subject, kind, first, last) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT(station, subject, kind) DO UPDATE SET first = MIN(first, excluded.first), "
                               "last = MAX(last, excluded.last)", (st, s, k, a, b))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            for st, s, _k, at, _d in raw:
                self._dirty.add((st, s, int(at // 3600)))
        self.ingested += n
        out = {"ok": True, "accepted": n, "skipped": skipped}
        if self.settled and self.resend_from is not None and station not in self._told:
            self._told.add(station)
            out["resend_from"] = self.resend_from          # this store lost what came after: again, please
        return out

    def rename_station(self, old: str, new: str, chunk: int = RENAME_CHUNK, before: float | None = None) -> int:
        """A station renamed on the hub (sizeup/cloud/hub.py HubApp.apply_station_renames, from the renames in hub.sqlite):
        its rows move from `old` to `new`, so its graphs go on with their history under the new id. before: the rename's
        time: only rows from before it move (an hour's rollups: those of the hours that began before it). After a
        rename the hub files the station's own posts under the new id even when they say the old one, so a row under
        the old id from after it is another station's (an own token reusing the id) and stays. In chunks of `chunk`
        rows, each its own transaction (the writer's lock let go between them: the station's posts go on), in
        primary-key order. Rows the new id has already (it posted under both) are kept once (raw and events: the same
        key is the same record); a series spans both; the hours both had, and the rename's own hour, are rolled again
        from raw at the janitor's next turn (HubApp holds it off while a move runs). The rows moved (0: nothing under the
        old id; no file made). fence.FencedWrite when this task isn't the leader."""
        old, new = _text(old, 64), _text(new, 64)
        if not old or not new or old == new or not self._exists():
            return 0
        lim = float("inf") if before is None else float(before)
        when = {"raw": ("at < ?", lim), "events": ("at < ?", lim), "hourly": ("hour * 3600.0 < ?", lim)}
        if not any(self._read(f"SELECT 1 FROM {t} WHERE station = ? AND {when[t][0]} LIMIT 1", (old, lim))
                   for t in ("raw", "events", "hourly")):
            return 0                               # (an index lookup each: cheap at every janitor turn)
        if not self.may_write():
            fence_mod.refuse("station metrics")
        both = {(s_, h) for s_, h in self._read(
            "SELECT DISTINCT o.subject, o.hour FROM hourly o JOIN hourly n ON n.station = ? AND n.subject = o.subject "
            "AND n.hour = o.hour WHERE o.station = ? AND o.hour * 3600.0 < ?", (new, old, lim))}
        spans = self._read("SELECT subject, kind, first, last FROM series WHERE station = ? AND first < ?", (old, lim))
        n = 0
        for table, key, rest in (("raw", ("subject", "kind", "at"), ("doc",)),
                                 ("events", ("subject", "at", "ev"), ("detail",)),
                                 ("hourly", ("subject", "metric", "hour"), ("n", "lo", "p50", "p90", "hi"))):
            k, cols = ", ".join(key), ", ".join(key + rest)
            cond = f"station = ? AND {when[table][0]}"
            while True:
                with self._lock:
                    if not self.may_write():
                        fence_mod.refuse("station metrics")
                    db = self._open()
                    # the chunk: the old id's first `chunk` rows in key order, up to (not past) the next one's key
                    edge = db.execute(f"SELECT {k} FROM {table} WHERE {cond} ORDER BY {k} LIMIT 1 OFFSET ?",
                                      (old, lim, chunk)).fetchone()
                    where = cond + (f" AND ({k}) < ({', '.join('?' * len(key))})" if edge else "")
                    args = (old, lim, *edge) if edge else (old, lim)
                    db.execute("BEGIN")
                    try:
                        n += db.execute(f"INSERT OR IGNORE INTO {table} (station, {cols}) SELECT ?, {cols} FROM {table} "
                                        f"WHERE {where}", (new, *args)).rowcount
                        db.execute(f"DELETE FROM {table} WHERE {where}", args)
                        db.execute("COMMIT")
                    except Exception:
                        if db.in_transaction:
                            db.execute("ROLLBACK")
                        raise
                if not edge:
                    break
        cut = int(lim // 3600) if before is not None else None
        with self._lock:
            db = self._open()
            db.execute("BEGIN")
            try:
                for s_, kind, first, last in spans:       # the new id's span takes what moved; the old one's what stays
                    # (the raw rows say where the split is; past raw's days, the rename's time does)
                    upto = db.execute("SELECT MAX(at) FROM raw WHERE station = ? AND subject = ? AND kind = ? AND at < ?",
                                      (new, s_, kind, lim)).fetchone()[0]
                    db.execute("INSERT INTO series (station, subject, kind, first, last) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT(station, subject, kind) DO UPDATE SET first = MIN(first, excluded.first), "
                               "last = MAX(last, excluded.last)",
                               (new, s_, kind, first, min(last, lim) if upto is None else max(first, upto)))
                    if last < lim:
                        db.execute("DELETE FROM series WHERE station = ? AND subject = ? AND kind = ?", (old, s_, kind))
                    else:
                        since = db.execute("SELECT MIN(at) FROM raw WHERE station = ? AND subject = ? AND kind = ?",
                                           (old, s_, kind)).fetchone()[0]
                        db.execute("UPDATE series SET first = ? WHERE station = ? AND subject = ? AND kind = ?",
                                   (since if since is not None else lim, old, s_, kind))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            subjects = {s_ for s_, _k, _a, _b in spans}
            edge_hours = {(st, s_, cut) for s_ in subjects for st in (old, new)} if cut is not None else set()
            self._dirty = {(new if st == old and (cut is None or h < cut) else st, s_, h) for st, s_, h in self._dirty} \
                | {(new, s_, h) for s_, h in both} | edge_hours
        if old in self._told:
            self._told.add(new)
        return n

    def copy_soon(self) -> None:
        """A copy to the bucket at the backup thread's next turn (after a rename: a takeover then restores it renamed)."""
        if self.settled and self._next_upload is not None:
            self._next_upload = min(self._next_upload, self.clock())

    # -- the rollups, the retention ---------------------------------------------------------------------------------

    def roll(self, keys) -> int:
        """Each (station, subject, hour) rolled up again from raw. The rows written."""
        out = 0
        for st, s, hour in sorted(keys):
            rows = self._read("SELECT kind, doc FROM raw WHERE station = ? AND subject = ? AND kind IN ('floor', 'tx', "
                              "'cc') AND at >= ? AND at < ?", (st, s, hour * 3600.0, hour * 3600.0 + 3600.0))
            docs: dict[str, list[dict]] = {}
            for kind, doc in rows:
                try:
                    docs.setdefault(kind, []).append(json.loads(doc))
                except ValueError:
                    continue
            vals: dict[str, list[float]] = {}
            for metric, (kind, which, value) in METRICS.items():
                for d in docs.get(kind, ()):
                    if which(d):
                        v = _num(value(d))
                        if v is not None:
                            vals.setdefault(metric, []).append(v)
            if not vals:
                continue                           # no raw left for that hour (past RAW_DAYS): its rollups stay
            with self._lock:
                db = self._open()
                db.execute("BEGIN")
                try:
                    db.execute("DELETE FROM hourly WHERE station = ? AND subject = ? AND hour = ?", (st, s, hour))
                    for metric, v in vals.items():
                        db.execute("INSERT INTO hourly (station, subject, metric, hour, n, lo, p50, p90, hi) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (st, s, metric, hour, len(v), min(v), _pct(v, 50), _pct(v, 90), max(v)))
                        out += 1
                    db.execute("COMMIT")
                except Exception:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
        return out

    def maintain(self, now: float | None = None) -> dict:
        """The janitor's turn (every 10 min, the leader's): the hours that changed rolled up (the last two days too,
        after a start; after a merge every hour raw still has), the retention once a day."""
        now = self.clock() if now is None else now
        did: dict = {}
        if not self.may_write() or not self._exists():
            return did
        keys = set(self._dirty)
        self._dirty -= keys
        if not self._rolled_once:
            self._rolled_once = True
            first = int((now - (RAW_DAYS * 86400.0 if self._reroll_all else REROLL_S)) // 3600)
            self._reroll_all = False
            for st, s, a, b in self._read("SELECT station, subject, MIN(first), MAX(last) FROM series GROUP BY station, subject"):
                keys |= {(st, s, h) for h in range(max(first, int(a // 3600)), int(b // 3600) + 1)}
        if keys:
            did["rolled"] = self.roll(keys)
        day = int(now // 86400)
        if day != self._retained_day:
            self._retained_day = day
            did["deleted"] = self.retain(now)
        return did

    def retain(self, now: float | None = None) -> int:
        """raw past RAW_DAYS, hourly past HOURLY_DAYS, events past EVENT_DAYS: deleted, a series at a time (each one
        range of its primary key). A series' first / last stay what it covers (its rollups go back a year); it leaves
        the index once even its rollups are gone."""
        now = self.clock() if now is None else now
        n = 0
        series = self._read("SELECT station, subject, kind FROM series")
        with self._lock:
            db = self._open()
            for st, s, k in series:
                n += db.execute("DELETE FROM raw WHERE station = ? AND subject = ? AND kind = ? AND at < ?",
                                (st, s, k, now - RAW_DAYS * 86400)).rowcount
            n += db.execute("DELETE FROM hourly WHERE hour < ?", (int((now - HOURLY_DAYS * 86400) // 3600),)).rowcount
            n += db.execute("DELETE FROM events WHERE at < ?", (now - EVENT_DAYS * 86400,)).rowcount
            db.execute("DELETE FROM series WHERE last < ?", (now - HOURLY_DAYS * 86400,))
            db.execute("UPDATE series SET first = MAX(first, ?) WHERE first < ?",
                       (now - HOURLY_DAYS * 86400, now - HOURLY_DAYS * 86400))     # (never past last: last is newer)
        return n

    # -- the copies (sizeup/cloud/metricsbackup.py) -----------------------------------------------------------------

    def backup_turn(self, now: float | None = None) -> str:
        """The backup thread's turn (the leader's, every BACKUP_TURN_S): settle (merge the newest copy, or see there is
        none), then a copy every SNAPSHOT_S. What it did, for the log and the tests."""
        now = self.clock() if now is None else now
        if self.backup is None or not self.may_write():
            return ""                              # (no copies: settled as it is when the file opens)
        if not self.settled and self._unsettled_since is None:
            self._unsettled_since = now
        if now < self._next_try:
            return ""
        with self._lock:
            self._open()
        try:
            if not self.settled:
                return self.restore(now)
            if self.backup is not None and self._next_upload is not None and now >= self._next_upload:
                return self.upload(now)
        except Exception as e:  # noqa: BLE001 - tried again later; never taken for "nothing there"
            self._fails += 1
            wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(self._fails - 1, 20))
            self._next_try = now + wait
            why = f"{type(e).__name__}: {e}"[:200]
            if why != self.backup_status.get("error"):
                self.log(f"metrics: {'restore' if not self.settled else 'copy'} failed ({why}); again in {wait:.0f} s")
            self.backup_status["error"] = why
            return "failed"
        return ""

    def restore(self, now: float) -> str:
        """Settle: the newest copy (and the largest, if that is another one) merged in, or none there. Raises when the
        bucket can't be listed or no copy can be read (tried again later; nothing is uploaded meanwhile)."""
        copies = self.backup.list()
        if not copies:
            self._fails = 0
            with self._lock:
                self._settle(0.0, "none")
            self._copy_at = now                    # (none to have: the clock for "no copy since" starts here)
            self.log("metrics: no copies in the bucket: starting from what comes in")
            return "none"
        pick = [copies[0]] + ([c] if (c := max(copies, key=lambda c: c["size"])) is not copies[0] else [])
        merged, snap_at, last_err = 0, None, None
        for c in pick + [c for c in copies if c not in pick]:
            if merged and c not in pick:
                break                              # the newest (and largest) are in: enough
            try:
                n, at = self.merge(self.backup.get(c["slot"]))
            except Exception as e:  # noqa: BLE001 - a torn copy (zlib.error, a bad file, ...): the next one is tried
                last_err = e
                self.log(f"metrics: copy {c['slot']} not read ({type(e).__name__}: {e})")
                continue
            merged += 1
            snap_at = at if snap_at is None else max(snap_at, at or 0.0)
        if not merged:
            raise RuntimeError(f"no copy could be read ({type(last_err).__name__ if last_err else 'none'})")
        self._fails = 0
        self._rolled_once, self._reroll_all = False, True    # its hours (all raw has) rolled at the janitor's next turn
        with self._lock:
            self._settle(snap_at or 0.0, "restored")
        self.backup_status["restored"] = now
        self._copy_at = snap_at or now
        return "restored"

    def merge(self, data: bytes) -> tuple[int, float | None]:
        """A copy (an SQLite file's bytes) merged in: INSERT OR IGNORE (what is here stays), the series by min / max.
        Column by column (the columns both have): a copy from a release with a column more or less still merges, as
        long as it has every key column and every column here that must have a value. (rows added, its snapshot_at)."""
        if data[:16] != b"SQLite format 3\x00":
            raise ValueError("not an SQLite file")
        with self._lock:
            self._open()                           # (its folder made: the copy is written beside it)
        fd, tmp = tempfile.mkstemp(prefix=".metrics-copy.", suffix=".sqlite",
                                   dir=None if self.path == ":memory:" else str(Path(self.path).parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            n = 0
            with self._lock:
                db = self._open()
                db.execute("ATTACH DATABASE ? AS snap", (tmp,))
                try:
                    try:
                        v = db.execute("SELECT value FROM snap.meta WHERE key = 'snapshot_at'").fetchone()
                    except sqlite3.Error:
                        v = None
                    db.execute("BEGIN")
                    for t in ("raw", "events", "hourly", "series"):
                        cols = _common_columns(db, t)
                        if cols is None:
                            self.log(f"metrics: a copy's {t} doesn't fit this release's: left out")
                            continue
                        names = ", ".join(cols)
                        if t == "series":
                            upd = ", ".join(f"{c} = {'MIN' if c == 'first' else 'MAX'}({c}, excluded.{c})"
                                            for c in ("first", "last") if c in cols)
                            n += db.execute(f"INSERT INTO main.series ({names}) SELECT {names} FROM snap.series WHERE true "
                                            "ON CONFLICT(station, subject, kind) DO " + (f"UPDATE SET {upd}" if upd else
                                                                                         "NOTHING")).rowcount
                        else:
                            n += db.execute(f"INSERT OR IGNORE INTO main.{t} ({names}) SELECT {names} FROM snap.{t}").rowcount
                    db.execute("COMMIT")
                except Exception:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
                finally:
                    db.execute("DETACH DATABASE snap")
        finally:
            Path(tmp).unlink(missing_ok=True)
        self.log(f"metrics: {n} row(s) merged in from a copy")
        try:
            return n, float(v[0]) if v else None
        except ValueError:
            return n, None

    def snapshot_bytes(self, now: float | None = None) -> bytes:
        """A consistent copy of the store (SQLite's backup API, from the read connection: no write waits), with when it
        was made (meta snapshot_at), as one file's bytes."""
        at = self.clock() if now is None else now
        with self._lock:
            self._meta("snapshot_at", f"{at:.3f}")
        fd, tmp = tempfile.mkstemp(prefix=".metrics-snap.", suffix=".sqlite",
                                   dir=None if self.path == ":memory:" else str(Path(self.path).parent))
        os.close(fd)
        try:
            dst = sqlite3.connect(tmp, isolation_level=None)
            try:
                with self._rlock:
                    self._ro.backup(dst)
                dst.execute("PRAGMA journal_mode = DELETE")     # one file, nothing beside it
            finally:
                dst.close()
            return Path(tmp).read_bytes()
        finally:
            Path(tmp).unlink(missing_ok=True)

    def upload(self, now: float) -> str:
        """A copy into an empty slot or over the oldest (metricsbackup.next_slot): never before settling."""
        from sizeup.cloud.metricsbackup import next_slot
        if not self.settled:
            return ""
        slot = next_slot(self.backup.list())
        raw = self.snapshot_bytes(now)
        if not self.may_write():
            return ""                              # the lease went meanwhile: the next leader copies
        size = self.backup.put(slot, raw)
        self._fails = 0
        self._next_upload = now + SNAPSHOT_S
        self.backup_status.update(uploaded=now, error="", slot=slot, bytes=size)
        self._copy_at = now
        self.log(f"metrics: copy {slot} kept ({size / 1e6:.2f} MB gzip)")
        return "uploaded"

    def health(self, now: float | None = None) -> dict | None:
        """Settings > Station > Health: the copies' state, and a warning ("warn") when the store hasn't settled for
        UNSETTLED_WARN_S (nothing is uploaded meanwhile) or no copy has been kept for COPY_WARN_S. None without copies
        (a local hub) or before this task has led."""
        now = self.clock() if now is None else now
        if self.backup is None or (self._unsettled_since is None and not self.settled):
            return None
        st = self.backup_status
        out = {"settled": self.settled, "how": st.get("how") or "", "unsettled_since": None if self.settled else
               self._unsettled_since, "copy_at": self._copy_at, "error": st.get("error") or "", "warn": ""}
        why = f": {out['error'][:120]}" if out["error"] else ""
        if not self.settled and now - (self._unsettled_since or now) > UNSETTLED_WARN_S:
            out["warn"] = (f"metrics copies not restored for {(now - self._unsettled_since) / 3600:.0f} h{why} "
                           "(nothing uploaded until they are)")
        elif self.settled and self._copy_at is not None and now - self._copy_at > COPY_WARN_S:
            out["warn"] = f"no metrics copy kept for {(now - self._copy_at) / 3600:.0f} h{why}"
        return out

    # -- out (GET /api/cad/metrics, admins) -------------------------------------------------------------------------

    def index(self) -> dict:
        """What there is: {series: [{station, subject, kind, first, last}]}."""
        return {"series": [{"station": st, "subject": s, "kind": k, "first": a, "last": b} for st, s, k, a, b in
                           self._read("SELECT station, subject, kind, first, last FROM series ORDER BY station, subject, kind")]}

    def query(self, q: dict) -> dict:
        """?subject=FG1 [&station=] [&from=&to= (epoch s; default the last 24 h)] [&res=raw|hour|auto] [&kind=floor|tx|cc]
        [&metric=...]: the subject's series and its events (the station's and the Settings audit's). No subject: the
        index. MetricsError for a bad one."""
        def one(name: str, default=None):
            v = q.get(name)
            return (v[0] if isinstance(v, list) else v) if v else default

        subject = _text(one("subject", ""), 80)
        if not subject:
            return self.index()
        now = self.clock()
        try:
            t1 = float(one("to", now))
            t0 = float(one("from", t1 - 86400.0))
        except (TypeError, ValueError):
            raise MetricsError("from / to: epoch seconds") from None
        if not (math.isfinite(t0) and math.isfinite(t1)) or t0 >= t1:
            raise MetricsError("from: before to")
        res = str(one("res", "auto"))
        if res not in ("raw", "hour", "auto"):
            raise MetricsError("res: raw, hour or auto")
        if res == "auto":
            res = "raw" if t1 - t0 <= AUTO_RAW_S else "hour"
        if t1 - t0 > (RAW_SPAN_S if res == "raw" else HOUR_SPAN_S):
            raise MetricsError(f"from / to: at most {(RAW_SPAN_S if res == 'raw' else HOUR_SPAN_S) / 86400:g} days at res={res}")
        stations = [r[0] for r in self._read("SELECT DISTINCT station FROM series WHERE subject = ?", (subject,))]
        station = _text(one("station", ""), 64) or (stations[0] if len(stations) == 1 else "")
        if not station:
            if not stations:
                return {"subject": subject, "station": None, "from": t0, "to": t1, "res": res, "rows": [], "events": [],
                        "stations": []}
            raise MetricsError(f"station: one of {', '.join(stations)}")
        kind = one("kind")
        if kind is not None and kind not in KINDS:
            raise MetricsError("kind: floor, tx or cc")
        out: dict = {"station": station, "subject": subject, "from": t0, "to": t1, "res": res, "stations": stations}
        if res == "raw":
            kinds = [kind] if kind else list(KINDS)
            rows = []
            carry = self._settings_before(station, subject, t0) if "floor" in kinds else {}
            for k in kinds:
                for at, doc in self._read("SELECT at, doc FROM raw WHERE station = ? AND subject = ? AND kind = ? AND "
                                          "at >= ? AND at < ? ORDER BY at LIMIT ?", (station, subject, k, t0, t1, MAX_ROWS + 1)):
                    try:
                        rows.append({"k": k, "at": at, **json.loads(doc)})
                    except ValueError:
                        continue
            rows.sort(key=lambda r: r["at"])
            for r in rows:                             # a floor record's settings, sent only when they change
                if r["k"] == "floor":
                    if any(f in r for f in SETTINGS):
                        carry = {f: r[f] for f in SETTINGS if f in r}
                    else:
                        r.update(carry)
            out["truncated"] = len(rows) > MAX_ROWS
            out["rows"] = rows[:MAX_ROWS]
        else:
            metric = one("metric")
            picked = [m for m in str(metric).split(",") if m] if metric is not None else []      # one, or "a,b,c"
            if metric is not None and (not picked or len(picked) > len(METRICS) or any(m not in METRICS for m in picked)):
                raise MetricsError(f"metric: one of {', '.join(METRICS)} (or several, comma-separated)")
            ms = list(dict.fromkeys(picked)) if picked else [m for m, (k, _w, _v) in METRICS.items() if not kind or k == kind]
            rows = []
            for m in ms:
                rows += [{"metric": m, "at": h * 3600, "n": n, "lo": lo, "p50": p50, "p90": p90, "hi": hi}
                         for h, n, lo, p50, p90, hi in self._read(
                             "SELECT hour, n, lo, p50, p90, hi FROM hourly WHERE station = ? AND subject = ? AND metric = ? "
                             "AND hour >= ? AND hour < ? ORDER BY hour", (station, subject, m, int(t0 // 3600),
                                                                          int(math.ceil(t1 / 3600))))]
            out["truncated"] = len(rows) > MAX_ROWS
            out["rows"] = rows[:MAX_ROWS]
        out["events"] = self.events(station, subject, t0, t1)
        return out

    def docs(self, station: str, subject: str, kind: str, t0: float, t1: float, limit: int = MAX_ROWS,
             newest: bool = False) -> list[dict]:
        """One series' raw records in [t0, t1), oldest first, at most `limit` (the oldest kept, or the newest with
        newest=True: one seek of the primary key either way): {"at", ...its numbers}. A floor record's settings as
        sent (not carried forward). For sizeup/cloud/metricsinsight.py (the suggestions, the shift checks), never
        the alert path."""
        out = []
        for at, doc in self._read("SELECT at, doc FROM raw WHERE station = ? AND subject = ? AND kind = ? AND at >= ? "
                                  f"AND at < ? ORDER BY at {'DESC' if newest else ''} LIMIT ?",
                                  (station, subject, kind, t0, t1, max(0, int(limit)))):
            try:
                out.append({"at": at, **json.loads(doc)})
            except ValueError:
                continue
        return out[::-1] if newest else out

    def hours(self, station: str, subject: str, metric: str, t0: float, t1: float) -> list[dict]:
        """One metric's hourly rollups in [t0, t1): [{at, n, lo, p50, p90, hi}] (the drift check)."""
        return [{"at": h * 3600, "n": n, "lo": lo, "p50": p50, "p90": p90, "hi": hi} for h, n, lo, p50, p90, hi in self._read(
            "SELECT hour, n, lo, p50, p90, hi FROM hourly WHERE station = ? AND subject = ? AND metric = ? AND hour >= ? "
            "AND hour < ? ORDER BY hour", (station, subject, metric, int(t0 // 3600), int(math.ceil(t1 / 3600))))]

    def _settings_before(self, station: str, subject: str, t0: float) -> dict:
        """The settings of the last floor record before t0 that had them (the station sends them every 15 min)."""
        for (doc,) in self._read("SELECT doc FROM raw WHERE station = ? AND subject = ? AND kind = 'floor' AND at < ? "
                                 "AND at >= ? ORDER BY at DESC LIMIT 30", (station, subject, t0, t0 - 1800.0)):
            try:
                d = json.loads(doc)
            except ValueError:
                continue
            if any(f in d for f in SETTINGS):
                return {f: d[f] for f in SETTINGS if f in d}
        return {}

    def events(self, station: str, subject: str, t0: float, t1: float) -> list[dict]:
        """The subject's events and the station's own (a restart, records dropped), and the Settings audit's changes to
        the subject (squelch, channel), oldest first."""
        out = [{"at": at, "subject": s, "ev": ev, "detail": d, "source": "station"} for s, at, ev, d in self._read(
            "SELECT subject, at, ev, detail FROM events WHERE station = ? AND subject IN (?, 'station') AND at >= ? AND "
            "at < ? ORDER BY at LIMIT 2000", (station, subject, t0, t1))]
        if self.audit is not None:
            try:
                for a in self.audit(subject, t0, t1)[:500]:
                    out.append({"at": a.get("at"), "subject": subject, "ev": "setting", "source": "audit",
                                "detail": f"{a.get('key')}: {a.get('old') or '-'} -> {a.get('new') or '-'}"[:300],
                                "by": a.get("operator") or ""})
            except Exception as e:  # noqa: BLE001 - the graph without them, said
                self.log(f"metrics: settings audit not read: {type(e).__name__}")
        out.sort(key=lambda e: e["at"] or 0)
        return out

    def stats(self) -> dict:
        """For Health / the PR's size check: rows and the file's size."""
        if self._db is None and not (self.path != ":memory:" and Path(self.path).exists()):
            return {"raw": 0, "hourly": 0, "events": 0, "bytes": 0}
        n = self._read("SELECT (SELECT COUNT(*) FROM raw), (SELECT COUNT(*) FROM hourly), (SELECT COUNT(*) FROM events)")[0]
        size = Path(self.path).stat().st_size if self.path != ":memory:" and Path(self.path).exists() else 0
        return {"raw": n[0], "hourly": n[1], "events": n[2], "bytes": size}
