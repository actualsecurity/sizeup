"""Address history and search over the CAD of record (#403): every incident's address key, History here on the
incident card, and the Search page's query. All of it in cad.sqlite (the CAD of record: hub.sqlite keeps only a short
history of JSON docs), as real columns, indexes and FTS5 tables, made on open (like user_activity: other branches hold
schema versions, and doing it twice changes nothing).

    incidents.addr_key / bldg_key / addr_street / addr_how / addr_label
                    the location's keys (sizeup/cad/addrkey.py). A trigger queues an incident (addr_queue) when it is
                    made or its location or pin changes; key_open() keys the open ones before each state push,
                    catch_up() and the background thread (start()) the rest, oldest last. The first open after this
                    change queues every incident: the backfill
    here_rev        counters bumped by triggers when a call is numbered, typed, closed, reopened, moved or gets a unit: one
                    per building key, one per street ("s:..."), one per ~50 m cell of its pin ("c:..."). History here is
                    cached per building and point, recomputed only when its building's, its street's or the 3 x 3 cells
                    around its pin's counters moved
    fts_incidents   the remarks (external content: incidents)
    fts_events      the comments: the chronology's notes, dispatch remarks and upgrades (external content: events)
    fts_radio       every transcript (radio_traffic: what speech-to-text wrote, the second engine's, and the status words
                    put back by statusrepair.py, so "en route" finds a row heard as "is fine"); a trigger queues a row
                    (fts_radio_queue) on insert and on a change of its text or rig, catch_up() indexes it. A transcript
                    reaches an incident through radio_links
    History here    for an incident with a building key: the other numbered calls with that key (a range and each of
                    its numbers; 12A and 12 when their pins are within SAME_LOT_M), plus the calls keyed only by their
                    point within GROUP_M; for one keyed only by its point: every call within GROUP_M. The last RECENT,
                    the last 12 months by type, and the repeat false alarms (ALARM closed FALSE or UNFOUNDED)
    Search          address / street / place (on the keys, indexed; whole words only when it is no address or street),
                    from / to, type, unit, disposition, and free text over the remarks, comments and transcripts (every
                    hit, newest first, reused HITS_TTL_S while paging; snippets for the page only); newest first, PAGE
                    at a time, on its own read-only connection (never holding the store's lock); audited by the API
    GETs            do at most CATCH_UP rows of the catch-up, and only when the store is free within LOCK_WAIT_S
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from sizeup import sourcekeys
from sizeup.cad import addrkey
from sizeup.cad.store import TZ, CadError

ADDR_COLUMNS = ("addr_key", "bldg_key", "addr_street", "addr_how", "addr_label")
COMMENT_KINDS = ("note", "remarks", "upgrade")      # the chronology's entries that are comments (fts_events)
RECENT = 5
YEAR_S = 365 * 86400
FALSE_DISPOSITIONS = ("FALSE", "UNFOUNDED")
ALARM_TYPES = ("ALARM",)
PAGE = 50
PAGE_MAX = 200
CSV_MAX = 20000

CACHE_N = 256
BATCH = 300               # rows keyed / indexed per lock hold
CATCH_UP = 25             # ... and at most this many before History here / a search is answered (tiny: GETs)
TOKENIZE = "unicode61 remove_diacritics 2"
# History here's counters (here_rev): a building key, "s:<street>|<town>", and "c:<lat cell>,<lon cell>", a ~50 m cell
# (CELL_PER_DEG cells a degree; SQL's CAST and Python's int() both truncate toward zero): a call keyed by its point is
# recomputed when anything in the 3 x 3 cells around it changes
CELL_PER_DEG = 2000
CELL = "'c:' || CAST({r}.lat * " + str(CELL_PER_DEG) + " AS INTEGER) || ',' || CAST({r}.lon * " + str(CELL_PER_DEG) + " AS INTEGER)"
HITS_TTL_S = 30.0          # a search's free-text hits, reused this long (paging through them)
PACE_S = 0.5               # the background backfill: at least this between batches,
CPU_REST = 2.0             # ... and twice the batch's own CPU time (<= ~33% of a core)
LOCK_WAIT_S = 0.25         # a GET's own catch-up waits at most this for the store

SCHEMA = f"""
CREATE INDEX IF NOT EXISTS incidents_bldg ON incidents(bldg_key, created_at);
CREATE INDEX IF NOT EXISTS incidents_street ON incidents(addr_street, created_at);
CREATE INDEX IF NOT EXISTS incidents_point ON incidents(lat, lon) WHERE lat IS NOT NULL;
CREATE INDEX IF NOT EXISTS incidents_created ON incidents(created_at);
CREATE INDEX IF NOT EXISTS assignments_incident ON assignments(incident_id);
CREATE INDEX IF NOT EXISTS assignments_unit ON assignments(unit_id, incident_id);
CREATE TABLE IF NOT EXISTS addr_queue (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS here_rev (key TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS addr_queue_ins AFTER INSERT ON incidents BEGIN
  INSERT OR IGNORE INTO addr_queue (id) VALUES (NEW.id);
END;
CREATE TRIGGER IF NOT EXISTS addr_queue_upd AFTER UPDATE OF address, place_address, lat, lon, geo_label ON incidents BEGIN
  INSERT OR IGNORE INTO addr_queue (id) VALUES (NEW.id);
END;
DROP TRIGGER IF EXISTS here_rev_inc;
DROP TRIGGER IF EXISTS here_rev_asg;
CREATE TRIGGER IF NOT EXISTS here_rev_inc2 AFTER UPDATE OF number, type, subtype, disposition, status, bldg_key, addr_street,
  lat, lon ON incidents BEGIN
  INSERT INTO here_rev (key, n) SELECT k, 1 FROM (
      SELECT NEW.bldg_key AS k UNION SELECT OLD.bldg_key UNION SELECT 's:' || NEW.addr_street UNION SELECT 's:' || OLD.addr_street
      UNION SELECT {CELL.format(r="NEW")} WHERE NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL
      UNION SELECT {CELL.format(r="OLD")} WHERE OLD.lat IS NOT NULL AND OLD.lon IS NOT NULL)
    WHERE k NOT IN ('', 's:') ON CONFLICT(key) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS here_rev_asg2 AFTER INSERT ON assignments BEGIN
  INSERT INTO here_rev (key, n) SELECT k, 1 FROM (
      SELECT i.bldg_key AS k FROM incidents i WHERE i.id = NEW.incident_id
      UNION SELECT 's:' || i.addr_street FROM incidents i WHERE i.id = NEW.incident_id
      UNION SELECT {CELL.format(r="i")} FROM incidents i WHERE i.id = NEW.incident_id AND i.lat IS NOT NULL AND i.lon IS NOT NULL)
    WHERE k NOT IN ('', 's:') ON CONFLICT(key) DO UPDATE SET n = n + 1;
END;
"""

FTS_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS fts_incidents USING fts5(remarks, content='incidents', content_rowid='id',
  tokenize='{TOKENIZE}');
CREATE TRIGGER IF NOT EXISTS fts_inc_ai AFTER INSERT ON incidents BEGIN
  INSERT INTO fts_incidents (rowid, remarks) VALUES (NEW.id, NEW.remarks);
END;
CREATE TRIGGER IF NOT EXISTS fts_inc_au AFTER UPDATE OF remarks ON incidents BEGIN
  INSERT INTO fts_incidents (fts_incidents, rowid, remarks) VALUES ('delete', OLD.id, OLD.remarks);
  INSERT INTO fts_incidents (rowid, remarks) VALUES (NEW.id, NEW.remarks);
END;
CREATE TRIGGER IF NOT EXISTS fts_inc_ad AFTER DELETE ON incidents BEGIN
  INSERT INTO fts_incidents (fts_incidents, rowid, remarks) VALUES ('delete', OLD.id, OLD.remarks);
END;
CREATE VIRTUAL TABLE IF NOT EXISTS fts_events USING fts5(text, content='events', content_rowid='id', tokenize='{TOKENIZE}');
CREATE TRIGGER IF NOT EXISTS fts_ev_ai AFTER INSERT ON events
WHEN NEW.incident_id IS NOT NULL AND NEW.kind IN ({",".join(f"'{k}'" for k in COMMENT_KINDS)}) BEGIN
  INSERT INTO fts_events (rowid, text) VALUES (NEW.id, NEW.text);
END;
CREATE TRIGGER IF NOT EXISTS fts_ev_ad AFTER DELETE ON events
WHEN OLD.incident_id IS NOT NULL AND OLD.kind IN ({",".join(f"'{k}'" for k in COMMENT_KINDS)}) BEGIN
  INSERT INTO fts_events (fts_events, rowid, text) VALUES ('delete', OLD.id, OLD.text);
END;
CREATE TRIGGER IF NOT EXISTS fts_ev_au AFTER UPDATE OF text, kind, incident_id ON events BEGIN
  INSERT INTO fts_events (fts_events, rowid, text) SELECT 'delete', OLD.id, OLD.text
    WHERE OLD.incident_id IS NOT NULL AND OLD.kind IN ({",".join(f"'{k}'" for k in COMMENT_KINDS)});
  INSERT INTO fts_events (rowid, text) SELECT NEW.id, NEW.text
    WHERE NEW.incident_id IS NOT NULL AND NEW.kind IN ({",".join(f"'{k}'" for k in COMMENT_KINDS)});
END;
CREATE VIRTUAL TABLE IF NOT EXISTS fts_radio USING fts5(text, tokenize='{TOKENIZE}');
CREATE TABLE IF NOT EXISTS fts_radio_queue (rid INTEGER PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS fts_rq_ai AFTER INSERT ON radio_traffic BEGIN
  INSERT OR IGNORE INTO fts_radio_queue (rid) VALUES (NEW.rowid);
END;
CREATE TRIGGER IF NOT EXISTS fts_rq_au AFTER UPDATE OF text, alt_text, rig ON radio_traffic BEGIN
  INSERT OR IGNORE INTO fts_radio_queue (rid) VALUES (NEW.rowid);
END;
CREATE TRIGGER IF NOT EXISTS fts_rq_ad AFTER DELETE ON radio_traffic BEGIN
  DELETE FROM fts_radio WHERE rowid = OLD.rowid;
  DELETE FROM fts_radio_queue WHERE rid = OLD.rowid;
END;
"""
FTS_VERSION = "1"         # meta search_fts: the backfill of the FTS tables done (a new version rebuilds them)


def _fts_ok(db) -> bool:
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._fts_probe USING fts5(x)")
        db.execute("DROP TABLE IF EXISTS temp._fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def fts_query(text: str) -> str:
    """What was typed as an FTS5 query: each word a prefix ("eng" finds engine), a "quoted phrase" as said, all of
    them (AND). '' when nothing is left."""
    out = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', text or ""):
        if m.group(1) is not None:
            words = re.findall(r"\w+", m.group(1))
            if words:
                out.append('"' + " ".join(words) + '"')
        else:
            out += [f'"{w}"*' for w in re.findall(r"\w+", m.group(2))]
    return " ".join(out[:12])


def _when(v: float | None) -> str:
    return datetime.fromtimestamp(v, TZ).strftime("%m/%d/%y %H:%M") if v else ""


class AddressHistory:
    """One per Cad (sizeup/cad/service.py)."""

    def __init__(self, cad, clock=None, log=print):
        self.cad, self.store, self.log = cad, cad.store, log
        self.clock = clock or self.store.clock
        self._cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._local = threading.local()        # Search's read-only connection, one per thread (_rq)
        self._hits: dict[str, tuple[float, list[int]]] = {}      # a free-text query -> (when, its incident ids)
        self.fts = False
        self.ensure()

    # -- schema -----------------------------------------------------------------------------------------------------

    def ensure(self) -> None:
        db = self.store.db
        with self.store.lock:
            cols = {r["name"] for r in db.execute("PRAGMA table_info(incidents)")}
            if not cols:
                return
            new = [c for c in ADDR_COLUMNS if c not in cols]
            for c in new:
                db.execute(f"ALTER TABLE incidents ADD COLUMN {c} TEXT NOT NULL DEFAULT ''")
            db.executescript(SCHEMA)
            if new:                                   # the backfill: every incident keyed (catch_up, the thread)
                db.execute("INSERT OR IGNORE INTO addr_queue (id) SELECT id FROM incidents")
                self._migrated("incidents: address keys (#403), backfilled in the background")
            self.fts = _fts_ok(db)
            if not self.fts:
                self.log("CAD: no SQLite FTS5 here: Search's text box looks at remarks and comments the slow way")
                return
            db.executescript(FTS_SCHEMA)
            if self.store.meta_get("search_fts") != FTS_VERSION:
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("INSERT INTO fts_incidents (fts_incidents) VALUES ('rebuild')")
                    db.execute("INSERT INTO fts_events (fts_events) VALUES ('delete-all')")
                    db.execute(f"INSERT INTO fts_events (rowid, text) SELECT id, text FROM events WHERE incident_id IS NOT NULL "
                               f"AND kind IN ({','.join('?' * len(COMMENT_KINDS))})", COMMENT_KINDS)
                    db.execute("DELETE FROM fts_radio")
                    db.execute("INSERT OR IGNORE INTO fts_radio_queue (rid) SELECT rowid FROM radio_traffic "
                               "WHERE text != '' OR alt_text != ''")
                    db.execute("INSERT INTO meta (key, value) VALUES ('search_fts', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (FTS_VERSION,))
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                self._migrated("search index (#403): remarks and comments; transcripts in the background")

    def _migrated(self, what: str) -> None:
        """In the store's list (as its own migrations are) and in the log: Cad.__init__ logged the list before this ran."""
        self.store.migrated.append(what)
        self.log(f"CAD: {what}")

    # -- keying -----------------------------------------------------------------------------------------------------

    def key_of(self, inc: dict) -> addrkey.Key:
        L = self.cad.lists
        return addrkey.key_incident(inc, L.geo_town_aliases, L.town_of, self.cad.geocoder, L.event_town(inc))

    def _key_rows(self, ids: list[int]) -> int:
        """Key these queued incidents (outside the lock: the gazetteer), then write them in one transaction."""
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        rows = self.store.q(f"SELECT id, address, place_address, geo_label, lat, lon FROM incidents WHERE id IN ({marks})", ids)
        keyed = [(self.key_of(r).as_dict(), r) for r in rows]
        with self.store.lock:
            db = self.store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                for k, r in keyed:
                    now = db.execute("SELECT address, place_address, geo_label, lat, lon FROM incidents WHERE id = ?",
                                     (r["id"],)).fetchone()
                    if now is None or tuple(now) != (r["address"], r["place_address"], r["geo_label"], r["lat"], r["lon"]):
                        continue                         # moved meanwhile: queued again by its trigger
                    db.execute(f"UPDATE incidents SET {', '.join(f'{c} = ?' for c in ADDR_COLUMNS)} WHERE id = ?",
                               (*[k[c] for c in ADDR_COLUMNS], r["id"]))
                    db.execute("DELETE FROM addr_queue WHERE id = ?", (r["id"],))
                db.execute(f"DELETE FROM addr_queue WHERE id IN ({marks}) AND id NOT IN (SELECT id FROM incidents)", ids)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return len(keyed)

    def key_open(self) -> int:
        """The open incidents waiting for a key (Cad.state, before each push): a new address shows its notes at once."""
        try:
            queued = [r["id"] for r in self.store.q("SELECT id FROM addr_queue ORDER BY id DESC LIMIT 500")]
            if not queued:                        # nothing waiting: no look at the incidents at all
                return 0
            ids = [r["id"] for r in self.store.q(
                f"SELECT id FROM incidents WHERE id IN ({','.join('?' * len(queued))}) "
                f"AND status IN ('queued', 'pending', 'active') LIMIT 50", queued)]
            return self._key_rows(ids)
        except Exception as e:  # noqa: BLE001 - a read-only standby, a locked file: next time; never the push
            self.log(f"CAD history: keying: {type(e).__name__}: {e}")
            return 0

    def key_one(self, iid: int) -> None:
        if self.store.one("SELECT id FROM addr_queue WHERE id = ?", (iid,)):
            self._key_rows([iid])

    def catch_up(self, max_rows: int = 2000, transcripts: bool = True) -> dict:
        """Key queued incidents (newest first) and index queued transcripts, at most max_rows of each; what is left.
        History here and Search call it with a small max_rows: what was just written is in, the backfill stays the
        background thread's."""
        n = 0
        while n < max_rows:
            ids = [r["id"] for r in self.store.q("SELECT id FROM addr_queue ORDER BY id DESC LIMIT ?", (min(BATCH, max_rows),))]
            if not ids:
                break
            self._key_rows(ids)
            n += len(ids)
        m = 0
        while transcripts and self.fts and m < max_rows:
            k = self._index_radio(min(BATCH, max_rows - m))
            if not k:
                break
            m += k
        return self.left()

    def left(self, read=None) -> dict:
        r = (read or self.store.q)("SELECT (SELECT COUNT(*) FROM addr_queue) AS keys, " +
                                   ("(SELECT COUNT(*) FROM fts_radio_queue)" if self.fts else "0") + " AS transcripts")[0]
        return {"keys": r["keys"], "transcripts": r["transcripts"]}

    def _index_radio(self, limit: int = BATCH) -> int:
        """Index up to limit queued transcripts: read, repair (outside the lock), then one short write."""
        rows = self.store.q("SELECT t.rowid AS rid, t.id, t.text, t.alt_text, t.channel, t.talkgroup, t.rig, t.dispatcher, "
                            "t.system "
                            "FROM fts_radio_queue q JOIN radio_traffic t ON t.rowid = q.rid ORDER BY q.rid DESC LIMIT ?",
                            (limit,))
        gone = [r["rid"] for r in self.store.q("SELECT q.rid FROM fts_radio_queue q LEFT JOIN radio_traffic t "
                                               "ON t.rowid = q.rid WHERE t.rowid IS NULL LIMIT ?", (limit,))]
        if not rows and not gone:
            return 0
        fix = getattr(getattr(self.cad, "hints", None), "decorate", None)
        shown = [dict(r) for r in rows]
        if fix is not None:
            try:
                fix(shown)                              # the status words put back (statusrepair.py): "fixed_text"
            except Exception as e:  # noqa: BLE001 - the heard text is still indexed
                self.log(f"CAD history: status repair: {type(e).__name__}")
        # another P25 system's transcript (#419, context) is never indexed: a search would show its words to anyone
        docs = [(r["rid"], "" if sourcekeys.is_context(r) else
                 "\n".join(x for x in dict.fromkeys((r["text"], r.get("alt_text"), r.get("fixed_text"))) if x))
                for r in shown]
        with self.store.lock:
            db = self.store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                for rid, text in docs:
                    db.execute("DELETE FROM fts_radio WHERE rowid = ?", (rid,))
                    if text:
                        db.execute("INSERT INTO fts_radio (rowid, text) VALUES (?, ?)", (rid, text))
                    db.execute("DELETE FROM fts_radio_queue WHERE rid = ?", (rid,))
                for rid in gone:
                    db.execute("DELETE FROM fts_radio_queue WHERE rid = ?", (rid,))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return len(docs) + len(gone)

    # -- the background backfill / catch-up -------------------------------------------------------------------------

    def start(self, every_s: float = 5.0, delay_s: float = 20.0) -> None:
        """The backfill and catch-up, in the background: from delay_s after start (a deploy's start, its caches and map
        layers, comes first; open calls are keyed by the state push meanwhile), then every every_s; a backlog in
        batches at least PACE_S apart, so it never takes the CPU from the console."""
        if self._thread is not None:
            return
        self._stop.clear()

        def run():
            self._stop.wait(delay_s)
            while not self._stop.is_set():
                t0 = time.thread_time()
                try:
                    left = self.catch_up(BATCH)
                    busy = left["keys"] or left["transcripts"]
                except Exception as e:  # noqa: BLE001
                    self.log(f"CAD history: {type(e).__name__}: {e}")
                    busy = False
                spent = time.thread_time() - t0       # this thread's own CPU for the batch
                self._stop.wait(pace(spent) if busy else every_s)
        self._thread = threading.Thread(target=run, name="cad-history", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)

    def _bounded_catch_up(self, transcripts: bool = False) -> None:
        """A request's own share of the catch-up: a few rows (CATCH_UP of each), only when the store is free within
        LOCK_WAIT_S (a request never waits long for the console's writes; else what is keyed already), never an error.
        The lock is only looked at here, not held: the gazetteer and the status repair run outside it (catch_up takes it
        for each short read and write)."""
        if not self.store.lock.acquire(timeout=LOCK_WAIT_S):
            return
        self.store.lock.release()
        try:
            self.catch_up(CATCH_UP, transcripts=transcripts)
        except Exception as e:  # noqa: BLE001 - the answer from what is keyed already
            self.log(f"CAD history: catch-up: {type(e).__name__}: {e}")

    # -- reads off the store's lock (Search) ------------------------------------------------------------------------

    def _rq(self, sql: str, args=()) -> list[dict]:
        """A Search read on this thread's own read-only connection (WAL: it neither waits for nor holds the store's lock,
        so a long search never stalls the console); an in-memory store (tests) has only its own connection."""
        if self.store.path == ":memory:":
            return self.store.q(sql, args)
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(Path(self.store.path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
            db.row_factory = sqlite3.Row
            self._local.db = db
        return [dict(r) for r in db.execute(sql, args)]

    # -- History here -----------------------------------------------------------------------------------------------

    @staticmethod
    def _cells(lat: float, lon: float) -> list[str]:
        a, b = int(lat * CELL_PER_DEG), int(lon * CELL_PER_DEG)
        return [f"c:{a + i},{b + j}" for i in (-1, 0, 1) for j in (-1, 0, 1)]

    def _revs(self, keys: list[str], point) -> tuple:
        ks = [k for k in keys if k] + (self._cells(*point) if point else [])
        if not ks:
            return ()
        rows = {r["key"]: r["n"] for r in self.store.q(f"SELECT key, n FROM here_rev WHERE key IN ({','.join('?' * len(ks))})", ks)}
        return tuple(rows.get(k, 0) for k in ks)

    def _near(self, lat: float, lon: float, only_unkeyed: bool) -> list[int]:
        a, b, c, d = addrkey.bbox(lat, lon)
        rows = self.store.q("SELECT id, lat, lon, bldg_key FROM incidents WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
                            "AND lat IS NOT NULL AND number IS NOT NULL", (a, b, c, d))
        return [r["id"] for r in rows if (not only_unkeyed or not r["bldg_key"])
                and addrkey.meters(lat, lon, r["lat"], r["lon"]) <= addrkey.GROUP_M]

    def _same_building(self, bldg_key: str, addr_street: str, point) -> list[int]:
        """The other keys that are this building: a range and each of its numbers ("10-12" and 10, 12), and 12A and 12
        when their pins are within SAME_LOT_M of this one's. Only the keys with this house number, by prefix on the
        building index (a range ending with it: on the street index), never the whole street."""
        if not addr_street:
            return []
        num, street, town, b = addrkey.bldg_parts(bldg_key)
        mine = re.fullmatch(r"(\d+)-(\d+)", num)
        lot = re.fullmatch(r"(\d+)([a-z]?)", num)
        if not (mine or lot):
            return []
        rest = _glob(f"|{street}|{town}") + "*"
        ors, args = [], []
        for d in (mine.groups() if mine else (lot.group(1),)):
            ors += ["bldg_key GLOB ?", "bldg_key GLOB ?", "bldg_key GLOB ?",
                    "(addr_street = ? AND bldg_key GLOB ?)"]
            args += [d + rest, d + "[a-z]" + rest, d + "-[0-9]*" + rest, addr_street, "[0-9]*-" + d + rest]
        out = []
        for r in self.store.q(f"SELECT id, bldg_key, lat, lon FROM incidents WHERE ({' OR '.join(ors)}) AND bldg_key != ? "
                              f"AND number IS NOT NULL", (*args, bldg_key)):
            n2, s2, t2, b2 = addrkey.bldg_parts(r["bldg_key"])
            if (s2, t2, b2) != (street, town, b):
                continue
            theirs = re.fullmatch(r"(\d+)-(\d+)", n2)
            lot2 = re.fullmatch(r"(\d+)([a-z]?)", n2)
            if (mine and n2 in mine.groups()) or (theirs and num in theirs.groups()):
                out.append(r["id"])
            elif (lot and lot2 and lot.group(1) == lot2.group(1) and point is not None and r["lat"] is not None
                  and addrkey.meters(point[0], point[1], r["lat"], r["lon"]) <= addrkey.SAME_LOT_M):
                out.append(r["id"])                # 12A and 12 on one pin: one building
        return out

    def building(self, bldg_key: str, lat: float | None, lon: float | None, addr_street: str = "") -> list[dict]:
        """Every numbered call at a building (cached until a call there, on its street or in the cells around its pin
        changes; newest first): {id, number, created_at, type, subtype, disposition, status, units}."""
        point = (lat, lon) if lat is not None and lon is not None else None
        if not bldg_key and point is None:
            return []
        ck = (bldg_key, addr_street, None if point is None else (round(lat, 5), round(lon, 5)))
        revs = self._revs([bldg_key, f"s:{addr_street}" if bldg_key and addr_street else ""], point)
        with self._cache_lock:
            hit = self._cache.get(ck)
            if hit is not None and hit[0] == revs:
                self._cache.move_to_end(ck)
                return hit[1]
        ids: list[int] = []
        if bldg_key:
            ids = [r["id"] for r in self.store.q("SELECT id FROM incidents WHERE bldg_key = ? AND number IS NOT NULL", (bldg_key,))]
            ids += [i for i in self._same_building(bldg_key, addr_street, point) if i not in ids]
        if point is not None:
            ids += [i for i in self._near(lat, lon, only_unkeyed=bool(bldg_key)) if i not in ids]
        rows = self._rows(ids)
        with self._cache_lock:
            self._cache[ck] = (revs, rows)
            while len(self._cache) > CACHE_N:
                self._cache.popitem(last=False)
        return rows

    def _rows(self, ids: list[int], read=None) -> list[dict]:
        if not ids:
            return []
        read = read or self.store.q
        out: list[dict] = []
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            out += read(f"SELECT id, number, created_at, closed_at, type, subtype, disposition, status, address, "
                        f"addr_label, lat, lon FROM incidents WHERE id IN ({marks})", part)
        self._units(out, read)
        out.sort(key=lambda r: (-r["created_at"], -r["id"]))
        return out

    def _units(self, rows: list[dict], read=None) -> None:
        if not rows:
            return
        read = read or self.store.q
        names: dict[int, list[str]] = {}
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for a in read(f"SELECT a.incident_id, u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                          f"WHERE a.incident_id IN ({','.join('?' * len(part))}) ORDER BY a.id", part):
                lst = names.setdefault(a["incident_id"], [])
                if a["name"] not in lst:
                    lst.append(a["name"])
        for r in rows:
            r["units"] = names.get(r["id"], [])

    def here(self, iid: int, may: str = "admin") -> dict:
        """GET /api/cad/incident/<id>/here: History here for the incident card. may: the viewer's standing (admin |
        dispatcher | viewer): the premise notes they may see."""
        try:
            self.key_one(iid)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD history: keying {iid}: {type(e).__name__}: {e}")
        self._bounded_catch_up()                  # calls just made or moved: in before it is read
        inc = self.store.incident(iid)
        rows = [r for r in self.building(inc["bldg_key"], inc["lat"], inc["lon"], inc["addr_street"]) if r["id"] != iid]
        now = self.clock()
        year = [r for r in rows if r["created_at"] >= now - YEAR_S]
        by: dict[str, int] = {}
        for r in year:
            by[r["type"] or "?"] = by.get(r["type"] or "?", 0) + 1
        false = [r for r in year if r["type"] in ALARM_TYPES and r["disposition"] in FALSE_DISPOSITIONS]
        pick = ("id", "number", "created_at", "type", "subtype", "disposition", "status", "units")
        out = {"key": {k: inc.get(k, "") for k in ADDR_COLUMNS}, "total": len(rows),
               "recent": [{k: r[k] for k in pick} for r in rows[:RECENT]],
               "year": {"n": len(year), "by_type": [{"type": t, "n": n} for t, n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))]},
               "false_alarms": {"n": len(false), "calls": [{k: r[k] for k in ("id", "number", "created_at", "subtype")} for r in false]}}
        notes = getattr(self.cad, "premise", None)
        if notes is not None:
            out["notes"] = notes.for_incident(inc, may)
        return out

    # -- Search -----------------------------------------------------------------------------------------------------

    def parse_query(self, q: dict) -> dict:
        """The Search page's filters from a query string: {where, from, to, type, unit, disposition, text, page}."""
        def one(k: str, n: int = 120) -> str:
            return ((q.get(k) or [""])[0] or "").strip()[:n]

        def when(k: str) -> float | None:
            v = one(k, 40)
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                pass
            try:
                d = datetime.fromisoformat(v)
            except ValueError:
                raise CadError(f"{k}: a date (2026-11-01) or unix seconds") from None
            if d.tzinfo is None:
                d = d.replace(tzinfo=TZ)
            if k == "to" and len(v) <= 10:          # a day: to the end of it
                return d.timestamp() + 86400
            return d.timestamp()
        try:
            page = max(0, int(one("page", 6) or 0))
            size = min(PAGE_MAX, max(1, int(one("size", 4) or PAGE)))
        except ValueError:
            raise CadError("page, size: numbers") from None
        f = {"where": one("where"), "from": when("from"), "to": when("to"), "type": one("type", 60), "unit": one("unit", 30),
             "disposition": one("disposition", 40), "text": one("text", 200), "page": page, "size": size}
        return f

    @staticmethod
    def filtered(f: dict) -> bool:
        return any(f.get(k) for k in ("where", "type", "unit", "disposition", "text")) or any(
            f.get(k) is not None for k in ("from", "to"))

    def _where_sql(self, where: str) -> tuple[str, list]:
        """SQL for the address / street / place box, on the keys (indexed): the building (any apartment, a complex's
        buildings, a range's numbers), else the street, else (an intersection, a named place) the words, whole, in the
        location or place."""
        L = self.cad.lists
        k = addrkey.key_text(where, L.geo_town_aliases, L.town_of, self.cad.geocoder)
        if k.bldg_key:
            num, street, town, b = addrkey.bldg_parts(k.bldg_key)
            m = re.fullmatch(r"(\d+)-(\d+)", num)
            nums = [num] + (list(m.groups()) if m else [])
            ors, args = [], []
            for n in nums:
                base = f"{n}|{street}|{town}" + (f"|{b}" if b else "")
                if town:
                    ors.append("bldg_key = ?")
                    args.append(base)
                    if not b:
                        ors.append("bldg_key GLOB ?")      # a complex's buildings: "...|bldg 3"
                        args.append(_glob(base + "|") + "*")
                else:
                    ors.append("bldg_key GLOB ?")
                    args.append(_glob(f"{n}|{street}|") + "*")
            ranges = []                               # a range on the street with this number at one end ("10-12" for 10)
            for r in self._rq("SELECT id, bldg_key FROM incidents WHERE addr_street GLOB ? AND bldg_key GLOB ?",
                              (_glob(f"{street}|") + "*", "[0-9]*-[0-9]*|*")):
                mr = re.match(r"(\d+)-(\d+)\|", r["bldg_key"])
                _, s2, t2, b2 = addrkey.bldg_parts(r["bldg_key"])
                if mr and num in mr.groups() and s2 == street and (not town or t2 == town) and b2 == b:
                    ranges.append(r["id"])
            if ranges:
                ors.append(f"id IN ({','.join('?' * len(ranges))})")
                args += ranges
            return "(" + " OR ".join(ors) + ")", args
        sk = self._street_key(where)
        if sk:
            street, town = sk.split("|")
            if town:
                return "addr_street = ?", [sk]
            return "addr_street GLOB ?", [_glob(f"{street}|") + "*"]
        words = " ".join(re.findall(r"[a-z0-9]+", where.lower()))
        if not words:
            return "0", []
        pat = f"% {words} %"
        col = "(' ' || lower(replace(replace(replace({c}, ',', ' '), '@', ' '), '.', ' ')) || ' ')"
        return f"({col.format(c='address')} LIKE ? OR {col.format(c='place')} LIKE ?)", [pat, pat]

    def _street_key(self, text: str) -> str:
        """'example avenue' / 'EXAMPLE AVE, PORT_CHESTER' -> 'example ave|port chester' (the gazetteer's street when it is
        sure, else the words normalised); '' for an intersection or a place."""
        L = self.cad.lists
        s = addrkey.street_text(text)
        if not s or addrkey.CROSSING.search(f" {s} ") or not re.search(r"[a-z]", s, re.I):
            return ""
        head, _, tail = s.partition(",")
        town = next((L.geo_town_aliases[t] for t in (" ".join(re.findall(r"[a-z0-9]+", p.lower())) for p in tail.split(","))
                     if t in L.geo_town_aliases), "")
        typed = L.towns_typed(head)
        if typed and not town:
            town = typed[0]
            low = head.lower().replace("_", " ")
            for alias in sorted(L.geo_town_aliases, key=len, reverse=True):
                if low.rstrip().endswith(" " + alias):
                    head = head[:len(head.rstrip()) - len(alias)]
                    break
        street = ""
        if self.cad.geocoder is not None:
            try:
                for m in self.cad.geocoder(head) or []:
                    if m.get("kind") == "street" and float(m.get("confidence") or 0) >= addrkey.SNAP_CONFIDENCE and m.get("street"):
                        street, town = m["street"], town or L.town_of(m.get("municipality") or "")
                        break
            except Exception:  # noqa: BLE001
                pass
        if not street:                            # not a street the gazetteer knows: one with a suffix, as typed
            low = re.findall(r"[a-z0-9]+", head.lower())
            if not low or addrkey._suffix_at(low) != len(low) - 1:
                return ""
            street = head
        sk = addrkey.street_key(street)
        return f"{sk}|{town.lower()}" if sk else ""

    def _text_ids(self, text: str) -> list[int]:
        """Every incident the words are in (remarks, comments, transcripts), newest first; reused HITS_TTL_S (paging)."""
        q = fts_query(text)
        if not q:
            return []
        now = time.monotonic()
        with self._cache_lock:
            hit = self._hits.get(q)
            if hit is not None and now - hit[0] < HITS_TTL_S:
                return hit[1]
        ids: set[int] = set()
        try:
            if self.fts:
                for sql in ("SELECT rowid AS iid FROM fts_incidents WHERE fts_incidents MATCH ?",
                            "SELECT e.incident_id AS iid FROM fts_events f JOIN events e ON e.id = f.rowid WHERE fts_events MATCH ?",
                            "SELECT l.incident_id AS iid FROM fts_radio f JOIN radio_traffic t ON t.rowid = f.rowid "
                            "JOIN radio_links l ON l.traffic_id = t.id WHERE fts_radio MATCH ?"):
                    ids.update(r["iid"] for r in self._rq(sql, (q,)) if r["iid"] is not None)
            else:                                   # no FTS5: remarks and comments, the slow way
                like = "%" + text.lower().replace("%", "") + "%"
                ids.update(r["iid"] for r in self._rq("SELECT id AS iid FROM incidents WHERE lower(remarks) LIKE ?", (like,)))
                ids.update(r["iid"] for r in self._rq(
                    f"SELECT incident_id AS iid FROM events WHERE incident_id IS NOT NULL AND kind IN "
                    f"({','.join('?' * len(COMMENT_KINDS))}) AND lower(text) LIKE ?", (*COMMENT_KINDS, like)))
        except sqlite3.OperationalError as e:
            raise CadError(f"text: {e}") from None
        out = sorted(ids, reverse=True)
        with self._cache_lock:
            if len(self._hits) > 32:
                self._hits.clear()
            self._hits[q] = (now, out)
        return out

    def _snippets(self, text: str, ids: list[int]) -> dict[int, str]:
        """Where the words are, for the page's rows only: the remarks, else a comment, else a transcript. The rowids of
        the page's remarks, comments and transcripts are read first (indexed), then each FTS table is matched once with
        them bound (rowid IN (?, ...)): never a subquery the match would run again for every row."""
        q = fts_query(text)
        out: dict[int, str] = {}
        if not q or not ids:
            return out
        if not self.fts:
            like = "%" + text.lower().replace("%", "") + "%"
            for r in self._rq(f"SELECT id, substr(remarks, 1, 120) AS s FROM incidents WHERE id IN ({','.join('?' * len(ids))}) "
                              f"AND lower(remarks) LIKE ?", (*ids, like)):
                out[r["id"]] = r["s"]
            return out
        snip = "snippet({t}, 0, '[', ']', '…', 10)"

        def matched(table: str, owner: dict[int, int]) -> None:
            rids = list(owner)
            for i in range(0, len(rids), 500):
                part = rids[i:i + 500]
                for r in self._rq(f"SELECT rowid AS rid, {snip.format(t=table)} AS s FROM {table} WHERE {table} MATCH ? "
                                  f"AND rowid IN ({','.join('?' * len(part))})", (q, *part)):
                    out.setdefault(owner[r["rid"]], r["s"])
        matched("fts_incidents", {i: i for i in ids})
        left = [i for i in ids if i not in out]
        if left:
            m = ",".join("?" * len(left))
            matched("fts_events", {r["id"]: r["incident_id"] for r in self._rq(
                f"SELECT id, incident_id FROM events WHERE incident_id IN ({m}) AND kind IN "
                f"({','.join('?' * len(COMMENT_KINDS))})", (*left, *COMMENT_KINDS))})
        left = [i for i in ids if i not in out]
        if left:
            m = ",".join("?" * len(left))
            matched("fts_radio", {r["rid"]: r["incident_id"] for r in self._rq(
                f"SELECT t.rowid AS rid, l.incident_id FROM radio_links l JOIN radio_traffic t ON t.id = l.traffic_id "
                f"WHERE l.incident_id IN ({m})", left)})
        return out

    def search(self, f: dict, csv: bool = False) -> dict:
        """The Search page: {rows, total, page, size, pending} newest first. rows: {id, number, created_at, closed_at,
        type, subtype, address, addr_label, disposition, status, lat, lon, units, hit?}. Read on its own connection, off
        the store's lock; the free-text hits are every match (no cap), reused while paging; snippets only for the page."""
        self._bounded_catch_up(transcripts=bool(f.get("text")))     # the newest calls (and transcripts) in first
        conds, args = ["number IS NOT NULL"], []
        if f.get("where"):
            c, a = self._where_sql(f["where"])
            conds.append(c)
            args += a
        if f.get("from") is not None:
            conds.append("created_at >= ?")
            args.append(f["from"])
        if f.get("to") is not None:
            conds.append("created_at < ?")
            args.append(f["to"])
        if f.get("type"):
            t, _, sub = f["type"].partition("/")
            conds.append("type = ?")
            args.append(t)
            if sub:
                conds.append("subtype = ?")
                args.append(sub)
        if f.get("disposition"):
            conds.append("disposition = ?")
            args.append(f["disposition"])
        if f.get("unit"):
            conds.append("id IN (SELECT a.incident_id FROM assignments a JOIN units u ON u.id = a.unit_id WHERE u.name = ?)")
            args.append(f["unit"])
        if f.get("text"):
            ids = self._text_ids(f["text"])
            if not ids:
                return {"rows": [], "total": 0, "page": f["page"], "size": f["size"], "pending": self.left(self._rq)}
            conds.append("id IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(ids))
        where = " AND ".join(conds)
        total = self._rq(f"SELECT COUNT(*) AS n FROM incidents WHERE {where}", args)[0]["n"]
        size = CSV_MAX if csv else f["size"]
        off = 0 if csv else f["page"] * size
        rows = self._rq(f"SELECT id, number, created_at, closed_at, type, subtype, address, addr_label, disposition, "
                        f"status, lat, lon FROM incidents WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                        (*args, size, off))
        self._units(rows, self._rq)
        if f.get("text") and not csv:
            hits = self._snippets(f["text"], [r["id"] for r in rows])
            for r in rows:
                if r["id"] in hits:
                    r["hit"] = hits[r["id"]]
        return {"rows": rows, "total": total, "page": f["page"], "size": f["size"], "pending": self.left(self._rq)}

    @staticmethod
    def summary(f: dict, total: int) -> str:
        """The audit line: 'Searched where=…, type=ALARM, text=…: 12 results' (the filters as typed, cut short)."""
        parts = []
        for k in ("where", "type", "unit", "disposition", "text"):
            if f.get(k):
                v = " ".join(str(f[k]).split())
                parts.append(f"{k}={v[:60]}")
        for k in ("from", "to"):
            if f.get(k) is not None:
                parts.append(f"{k}={_when(f[k])}")
        return f"Searched {', '.join(parts) or 'everything'}: {total} result{'s' if total != 1 else ''}"


def pace(cpu_s: float) -> float:
    """The backfill's rest after a batch that took cpu_s of CPU: at least CPU_REST x that, and PACE_S, so it stays under
    about a third of a core on the hub, whatever the backlog."""
    return max(PACE_S, CPU_REST * cpu_s)


def _glob(s: str) -> str:
    return re.sub(r"[\[\]*?]", "", s)


def csv_rows(rows: list[dict], lists) -> bytes:
    from sizeup.cad.export import _csv, ts
    out = [["Number", "Received", "Closed", "Type", "Subtype", "Location", "Disposition", "Units"]]
    for r in rows:
        out.append([r["number"] or "", ts(r["created_at"]), ts(r["closed_at"]), r["type"], r["subtype"],
                    r["address"], lists.name("dispositions", r["disposition"]) if r["disposition"] else "",
                    ", ".join(r.get("units") or [])])
    return _csv(out[0], out[1:])


def wait_idle(h: AddressHistory, timeout: float = 5.0) -> None:
    """Tests: key and index everything queued."""
    end = time.time() + timeout
    while time.time() < end:
        left = h.catch_up(10_000)
        if not (left["keys"] or left["transcripts"]):
            return
