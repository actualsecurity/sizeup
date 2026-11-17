"""The station's signal metrics on the hub (#605; sizeup/cloud/metrics.py sends them): kept to graph each channel's
noise floor, levels and squelch over weeks, and each P25 system's control channel, so a squelch or a gain can be tuned
from data (and a shift after an antenna, splitter or dongle change seen at once).

metrics.sqlite, in the hub's state dir beside the others, but its own file: a busy station's numbers never bloat or
slow cad.sqlite / hub.sqlite, their Litestream replication or the leader's handover. It is NOT in Litestream (neither
deploy/hub/litestream.yml nor sizeup/handoff.py DBS know it), so it adds nothing to a drain or a takeover. Instead:

    snapshot   every SNAPSHOT_S the leader writes a consistent copy (SQLite's backup API, from a read connection: the
               ingest isn't held up) to metrics-snapshot.sqlite, and sizeup/cachesync.py keeps a gzip copy of that in
               the backups bucket like the map caches (at most every 3 hours)
    restore    a new task (a deploy) gets the copy back with the map caches: the standby fetches it before it is
               promoted (handoff warm_caches), so nothing waits for it; opened here when metrics.sqlite isn't there yet.
               A copy that comes back after metrics.sqlite was made (the app's own late restore) is merged in once
               (INSERT OR IGNORE)
    resend     a store that started from a copy (or from nothing) answers each station's first post with "resend_from":
               when the copy was made (meta snapshot_at; 0 from nothing). The station keeps what was acked for 6 h and
               sends it again (sizeup/cloud/metrics.py resend): duplicates are ignored, so a deploy loses nothing while
               the station is connected. Without it (a station away for the whole gap, a copy older than 6 h) at most
               the time since the last copy is lost (agreed: metrics only)

Tables (made on first use, never at the hub's start):

    raw      (station, subject, kind, at) -> doc: every floor / tx / cc record as sent (doc: its numbers, JSON),
             RAW_DAYS (30) days. One B-tree (WITHOUT ROWID): a series' range is one seek
    events   (station, subject, at, ev) -> detail: what explains a shift (the station's; the Settings audit's squelch and
             channel changes are joined in when read, from cad.sqlite), EVENT_DAYS (365)
    hourly   (station, subject, metric, hour) -> n, lo, p50, p90, hi: the rollups, HOURLY_DAYS (365)
    series   (station, subject, kind) -> first, last: what there is (the API's index, the retention's list)
    meta     what was rolled up last

Rollups (METRICS below): an hour is rolled again whenever a record of it comes in (a station back after an outage
sends late), and at the first maintain() after a start the last two days are rolled again (in case that was lost).
maintain() runs from the hub's janitor (every 10 min): rollups, then the retention once a day, then the snapshot.
Only the leader writes (may_write: the hub's fence); a standby's maintain() does nothing.

Size (docs/cloud.md "Station metrics"; tests/test_station_metrics.py measures it): a day of 6 channels, 2 P25 systems
and 2,000 transmissions is ~13,500 records, ~1.7 MB (~125 bytes a record with its key); its rollups ~1,100 rows,
~80 KB. At 30 days of raw and a year of rollups: ~50 MB + ~30 MB. The copy gzips to a third or less.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

RAW_DAYS = 30
HOURLY_DAYS = 365
EVENT_DAYS = 365
SNAPSHOT_S = 3 * 3600.0        # (sizeup/cachesync.py uploads it at most this often too)
REROLL_S = 48 * 3600.0          # the first maintain() after a start rolls this far back again
SNAPSHOT_NAME = "metrics-snapshot.sqlite"     # (sizeup/cachesync.py FILES)
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
    v = float(v)
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
            if math.isfinite(float(v)):
                out[k] = v
        elif isinstance(v, str):
            out[k] = _text(v, 40)
        elif k == "unc" and isinstance(v, dict):
            out[k] = {_text(f, 14): int(c) for f, c in list(v.items())[:21]
                      if isinstance(c, int) and not isinstance(c, bool)}
    return out


class MetricStore:
    def __init__(self, path: str | Path, may_write: Callable[[], bool] = lambda: True,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 audit: Callable[[str, float, float], list[dict]] | None = None):
        """audit(subject, t0, t1): the Settings audit's changes to that channel (the hub's cad.sqlite), for the events."""
        self.path = str(path)
        self.may_write, self.log, self.clock, self.audit = may_write, log, clock, audit
        self._db: sqlite3.Connection | None = None
        self._ro: sqlite3.Connection | None = None
        self._lock = threading.Lock()              # the writer
        self._rlock = threading.Lock()             # the reader
        self._dirty: set[tuple[str, str, int]] = set()
        self._rolled_once = False
        self._merged = False
        self._opened_at: float | None = None       # (wall clock) when this process opened the file
        self.resend_from: float | None = None      # started from a copy (its time) or from nothing (0): stations resend
        self._told: set[str] = set()               # ... the stations told so already
        self._snap_at = 0.0
        self._retained_day = -1
        self.ingested = 0

    # -- the file ---------------------------------------------------------------------------------------------------

    @property
    def snapshot_path(self) -> Path | None:
        return None if self.path == ":memory:" else Path(self.path).with_name(SNAPSHOT_NAME)

    def _open(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        snap = self.snapshot_path
        if snap is not None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            if not Path(self.path).exists():
                self.resend_from = 0.0              # nothing here: whatever the stations still keep
            if not Path(self.path).exists() and snap.is_file():
                # a new task: the last copy (sizeup/cachesync.py brought it back) is where this one starts
                shutil.copyfile(snap, self.path)
                self._merged = True
                self.log(f"metrics: {Path(self.path).name} started from its last copy ({snap.stat().st_size / 1e6:.1f} MB)")
                try:
                    with sqlite3.connect(self.path) as c:
                        v = c.execute("SELECT value FROM meta WHERE key = 'snapshot_at'").fetchone()
                    self.resend_from = float(v[0]) if v else 0.0
                except (sqlite3.Error, ValueError):
                    self.resend_from = 0.0
        elif self.path == ":memory:":
            self.resend_from = None
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA busy_timeout = 5000")
        db.executescript(SCHEMA)
        self._db = db
        self._opened_at = time.time()
        if self.path == ":memory:":
            self._ro, self._rlock = db, self._lock
        else:
            ro = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            ro.execute("PRAGMA busy_timeout = 5000")
            ro.execute("PRAGMA query_only = 1")
            self._ro = ro
        return db

    def _read(self, sql: str, params=()) -> list:
        if self._db is None:
            if not (self.path == ":memory:" or Path(self.path).exists()
                    or (self.snapshot_path is not None and self.snapshot_path.is_file())):
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

    # -- in ---------------------------------------------------------------------------------------------------------

    def ingest(self, station: str, records: list, dropped: int = 0) -> dict:
        """POST /api/station/metrics from `station` (the authenticated id). What can't be read is skipped and counted,
        never refused whole: {ok, accepted, skipped}. MetricsError for a body that isn't one."""
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
            raise MetricsError("not the hub's leader")
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
        if self.resend_from is not None and station not in self._told:
            self._told.add(station)
            out["resend_from"] = self.resend_from          # this store started from a copy: what came after, again
        return out

    # -- the rollups, the retention, the snapshot -------------------------------------------------------------------

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

    def maintain(self, now: float | None = None, force_snapshot: bool = False) -> dict:
        """The janitor's turn (every 10 min, the leader's): a late copy merged once, the hours that changed rolled up
        (the last two days too, after a start), the retention once a day, the snapshot once SNAPSHOT_S has passed."""
        now = self.clock() if now is None else now
        did: dict = {}
        if not self.may_write() or not (self._db is not None or self.path == ":memory:" or Path(self.path).exists()
                                        or (self.snapshot_path is not None and self.snapshot_path.is_file())):
            return did
        if not self._merged:
            did["merged"] = self.merge_snapshot()
        keys = set(self._dirty)
        self._dirty -= keys
        if not self._rolled_once:
            self._rolled_once = True
            first = int((now - REROLL_S) // 3600)
            for st, s, a, b in self._read("SELECT station, subject, MIN(first), MAX(last) FROM series GROUP BY station, subject"):
                keys |= {(st, s, h) for h in range(max(first, int(a // 3600)), int(b // 3600) + 1)}
        if keys:
            did["rolled"] = self.roll(keys)
        day = int(now // 86400)
        if day != self._retained_day:
            self._retained_day = day
            did["deleted"] = self.retain(now)
        if force_snapshot or now - self._snap_at >= SNAPSHOT_S:
            did["snapshot"] = self.snapshot(now)
        return did

    def retain(self, now: float | None = None) -> int:
        """raw past RAW_DAYS, hourly past HOURLY_DAYS, events past EVENT_DAYS: deleted, a series at a time (each one
        range of its primary key)."""
        now = self.clock() if now is None else now
        n = 0
        series = self._read("SELECT station, subject, kind FROM series")
        with self._lock:
            db = self._open()
            for st, s, k in series:
                n += db.execute("DELETE FROM raw WHERE station = ? AND subject = ? AND kind = ? AND at < ?",
                                (st, s, k, now - RAW_DAYS * 86400)).rowcount
                db.execute("UPDATE series SET first = MAX(first, ?) WHERE station = ? AND subject = ? AND kind = ?",
                           (now - RAW_DAYS * 86400, st, s, k))
            n += db.execute("DELETE FROM hourly WHERE hour < ?", (int((now - HOURLY_DAYS * 86400) // 3600),)).rowcount
            n += db.execute("DELETE FROM events WHERE at < ?", (now - EVENT_DAYS * 86400,)).rowcount
            # a series with no raw left and no rollup left goes from the index
            db.execute("DELETE FROM series WHERE last < ?", (now - HOURLY_DAYS * 86400,))
        return n

    def snapshot(self, now: float | None = None) -> bool:
        """A consistent copy beside it (SNAPSHOT_NAME), for sizeup/cachesync.py to keep in S3: written through a temp
        file and put in place at once (the mirror never reads half of one). From the read connection: no write waits."""
        snap = self.snapshot_path
        if snap is None or self._db is None or not self.may_write():
            return False
        self._snap_at = self.clock() if now is None else now
        tmp = snap.with_name(f".{snap.name}.{os.getpid()}.tmp")
        try:
            with self._lock:                       # when it was made: a task starting from it asks for what came after
                self._db.execute("INSERT INTO meta (key, value) VALUES ('snapshot_at', ?) ON CONFLICT(key) DO UPDATE "
                                 "SET value = excluded.value", (f"{self._snap_at:.3f}",))
            dst = sqlite3.connect(tmp, isolation_level=None)
            try:
                with self._rlock:
                    self._ro.backup(dst)
                dst.execute("PRAGMA journal_mode = DELETE")     # one file, nothing beside it
            finally:
                dst.close()
            os.replace(tmp, snap)
            self._merged = True                    # ours from now on: never merged back in
        except (OSError, sqlite3.Error) as e:
            self.log(f"metrics: snapshot not written ({type(e).__name__}: {e})")
            return False
        finally:
            Path(tmp).unlink(missing_ok=True)
        return True

    def merge_snapshot(self) -> int:
        """A copy restored after metrics.sqlite was made here (the app's late cache restore): its rows added once
        (INSERT OR IGNORE: what is here wins). The rows added."""
        with self._lock:
            self._open()
        self._merged = True
        snap = self.snapshot_path
        try:
            if snap is None or not snap.is_file() or snap.stat().st_mtime < (self._opened_at or 0.0) - 5.0:
                return 0                           # none, or an older one of ours (a restart): nothing new in it
        except OSError:
            return 0
        n = 0
        with self._lock:
            db = self._open()
            try:
                db.execute("ATTACH DATABASE ? AS snap", (str(snap),))
            except sqlite3.Error as e:
                self.log(f"metrics: last copy not read ({type(e).__name__}: {e})")
                return 0
            try:
                db.execute("BEGIN")
                for t in ("raw", "events", "hourly", "series", ):
                    n += db.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM snap.{t}").rowcount
                db.execute("COMMIT")
            except sqlite3.Error as e:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                self.log(f"metrics: last copy not merged ({type(e).__name__}: {e})")
            finally:
                db.execute("DETACH DATABASE snap")
        if n:
            self._rolled_once = False              # its hours rolled again at this turn
            self.log(f"metrics: {n} row(s) from the last copy merged in")
        return n

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
            if metric is not None and metric not in METRICS:
                raise MetricsError(f"metric: one of {', '.join(METRICS)}")
            ms = [metric] if metric else [m for m, (k, _w, _v) in METRICS.items() if not kind or k == kind]
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
            return {"rows": 0, "bytes": 0}
        n = self._read("SELECT (SELECT COUNT(*) FROM raw), (SELECT COUNT(*) FROM hourly), (SELECT COUNT(*) FROM events)")[0]
        size = Path(self.path).stat().st_size if self.path != ":memory:" and Path(self.path).exists() else 0
        return {"raw": n[0], "hourly": n[1], "events": n[2], "bytes": size}
