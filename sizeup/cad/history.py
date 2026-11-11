"""Address history and search over the CAD of record (#403): every incident's address key, History here on the
incident card, and the Search page's query. All of it in cad.sqlite (the CAD of record: hub.sqlite keeps only a short
history of JSON docs), as real columns, indexes and FTS5 tables, made on open (like user_activity: other branches hold
schema versions, and doing it twice changes nothing).

    incidents.addr_key / bldg_key / addr_street / addr_how / addr_label
                    the location's keys (sizeup/cad/addrkey.py). A trigger queues an incident (addr_queue) when it is
                    made or its location or pin changes; key_open() keys the open ones before each state push,
                    catch_up() and the background thread (start()) the rest, oldest last. The first open after this
                    change queues every incident: the backfill
    here_rev        a counter per building key, bumped by triggers when a call there is numbered, typed, closed, reopened,
                    moved or gets a unit: History here is cached per building and recomputed only when its counter
                    (or the point-only calls' counter, key '') moved
    fts_incidents   the remarks (external content: incidents)
    fts_events      the comments: the chronology's notes, dispatch remarks and upgrades (external content: events)
    fts_radio       every transcript (radio_traffic: what speech-to-text wrote, the second engine's, and the status words
                    put back by statusrepair.py, so "en route" finds a row heard as "is fine"); a trigger queues a row
                    (fts_radio_queue) on insert and on a change of its text or rig, catch_up() indexes it. A transcript
                    reaches an incident through radio_links
    History here    for an incident with a building key: the other numbered calls with that key, plus the calls keyed
                    only by their point within GROUP_M; for one keyed only by its point: every call within GROUP_M.
                    The last RECENT, the last 12 months by type, and the repeat false alarms (ALARM closed FALSE or
                    UNFOUNDED)
    Search          address / street / place, from / to, type, unit, disposition, and free text over the remarks,
                    comments and transcripts; newest first, PAGE at a time; audited by the API (who, filters, count)
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime

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
TEXT_MAX = 20000          # free-text hits looked at, per source
CACHE_N = 256
BATCH = 300               # rows keyed / indexed per lock hold
CATCH_UP = 100            # ... and at most this many before History here / a search is answered
TOKENIZE = "unicode61 remove_diacritics 2"

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
CREATE TRIGGER IF NOT EXISTS here_rev_inc AFTER UPDATE OF number, type, subtype, disposition, status, bldg_key, lat, lon
ON incidents BEGIN
  INSERT INTO here_rev (key, n) VALUES (NEW.bldg_key, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1;
  INSERT INTO here_rev (key, n) SELECT OLD.bldg_key, 1 WHERE OLD.bldg_key != NEW.bldg_key
    ON CONFLICT(key) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS here_rev_asg AFTER INSERT ON assignments BEGIN
  INSERT INTO here_rev (key, n) SELECT bldg_key, 1 FROM incidents WHERE id = NEW.incident_id
    ON CONFLICT(key) DO UPDATE SET n = n + 1;
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
                self.store.migrated.append("incidents: address keys (#403), backfilled in the background")
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
                self.store.migrated.append("search index (#403): remarks and comments; transcripts in the background")

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
            ids = [r["id"] for r in self.store.q(
                "SELECT q.id FROM addr_queue q JOIN incidents i ON i.id = q.id "
                "WHERE i.status IN ('queued', 'pending', 'active') LIMIT 50")]
            return self._key_rows(ids)
        except sqlite3.Error as e:              # a read-only standby, a locked file: next time
            self.log(f"CAD history: keying: {e}")
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
            k = self._index_radio()
            if not k:
                break
            m += k
        return self.left()

    def left(self) -> dict:
        r = self.store.one("SELECT (SELECT COUNT(*) FROM addr_queue) AS keys, " +
                           ("(SELECT COUNT(*) FROM fts_radio_queue)" if self.fts else "0") + " AS transcripts")
        return {"keys": r["keys"], "transcripts": r["transcripts"]}

    def _index_radio(self) -> int:
        rows = self.store.q("SELECT t.rowid AS rid, t.id, t.text, t.alt_text, t.channel, t.talkgroup, t.rig, t.dispatcher "
                            "FROM fts_radio_queue q JOIN radio_traffic t ON t.rowid = q.rid ORDER BY q.rid DESC LIMIT ?",
                            (BATCH,))
        gone = [r["rid"] for r in self.store.q("SELECT q.rid FROM fts_radio_queue q LEFT JOIN radio_traffic t "
                                               "ON t.rowid = q.rid WHERE t.rowid IS NULL LIMIT ?", (BATCH,))]
        if not rows and not gone:
            return 0
        fix = getattr(getattr(self.cad, "hints", None), "decorate", None)
        shown = [dict(r) for r in rows]
        if fix is not None:
            try:
                fix(shown)                              # the status words put back (statusrepair.py): "fixed_text"
            except Exception as e:  # noqa: BLE001 - the heard text is still indexed
                self.log(f"CAD history: status repair: {type(e).__name__}")
        docs = [(r["rid"], "\n".join(x for x in dict.fromkeys((r["text"], r.get("alt_text"), r.get("fixed_text"))) if x))
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

    def start(self, every_s: float = 5.0) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def run():
            while not self._stop.is_set():
                try:
                    left = self.catch_up(BATCH)
                    busy = left["keys"] or left["transcripts"]
                except Exception as e:  # noqa: BLE001
                    self.log(f"CAD history: {type(e).__name__}: {e}")
                    busy = False
                self._stop.wait(0.05 if busy else every_s)
        self._thread = threading.Thread(target=run, name="cad-history", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=5)

    # -- History here -----------------------------------------------------------------------------------------------

    def _revs(self, key: str) -> tuple:
        rows = {r["key"]: r["n"] for r in self.store.q("SELECT key, n FROM here_rev WHERE key IN (?, '')", (key,))}
        return rows.get(key, 0), rows.get("", 0)

    def _near(self, lat: float, lon: float, only_unkeyed: bool) -> list[int]:
        a, b, c, d = addrkey.bbox(lat, lon)
        rows = self.store.q("SELECT id, lat, lon, bldg_key FROM incidents WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
                            "AND lat IS NOT NULL AND number IS NOT NULL", (a, b, c, d))
        return [r["id"] for r in rows if (not only_unkeyed or not r["bldg_key"])
                and addrkey.meters(lat, lon, r["lat"], r["lon"]) <= addrkey.GROUP_M]

    def building(self, bldg_key: str, lat: float | None, lon: float | None) -> list[dict]:
        """Every numbered call at a building (cached; newest first): {id, number, created_at, type, subtype,
        disposition, status, units}."""
        if not bldg_key and lat is None:
            return []
        point = (round(lat, 4), round(lon, 4)) if lat is not None and lon is not None else None
        ck = (bldg_key, point)
        revs = self._revs(bldg_key)
        with self._cache_lock:
            hit = self._cache.get(ck)
            if hit is not None and hit[0] == revs:
                self._cache.move_to_end(ck)
                return hit[1]
        ids: list[int] = []
        if bldg_key:
            ids = [r["id"] for r in self.store.q("SELECT id FROM incidents WHERE bldg_key = ? AND number IS NOT NULL", (bldg_key,))]
        if point is not None:
            ids += [i for i in self._near(lat, lon, only_unkeyed=bool(bldg_key)) if i not in ids]
        rows = self._rows(ids)
        with self._cache_lock:
            self._cache[ck] = (revs, rows)
            while len(self._cache) > CACHE_N:
                self._cache.popitem(last=False)
        return rows

    def _rows(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        out: list[dict] = []
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            out += self.store.q(f"SELECT id, number, created_at, closed_at, type, subtype, disposition, status, address, "
                                f"addr_label, lat, lon FROM incidents WHERE id IN ({marks})", part)
        self._units(out)
        out.sort(key=lambda r: (-r["created_at"], -r["id"]))
        return out

    def _units(self, rows: list[dict]) -> None:
        if not rows:
            return
        names: dict[int, list[str]] = {}
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for a in self.store.q(f"SELECT a.incident_id, u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                                  f"WHERE a.incident_id IN ({','.join('?' * len(part))}) ORDER BY a.id", part):
                lst = names.setdefault(a["incident_id"], [])
                if a["name"] not in lst:
                    lst.append(a["name"])
        for r in rows:
            r["units"] = names.get(r["id"], [])

    def here(self, iid: int) -> dict:
        """GET /api/cad/incident/<id>/here: History here for the incident card."""
        self.key_one(iid)
        self.catch_up(CATCH_UP, transcripts=False)        # calls just made or moved: in before it is read
        inc = self.store.incident(iid)
        rows = [r for r in self.building(inc["bldg_key"], inc["lat"], inc["lon"]) if r["id"] != iid]
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
            out["notes"] = notes.for_incident(inc)
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

    def _where_ids(self, where: str) -> tuple[str, list]:
        """SQL for the address / street / place box: the building (any apartment), else the street, else the words in
        the location or place (an intersection, a named place, part of a street name)."""
        L = self.cad.lists
        k = addrkey.key_text(where, L.geo_town_aliases, L.town_of, self.cad.geocoder)
        like = "%" + where.lower().replace("\\", "").replace("%", "").replace("_", " ") + "%"
        ors, args = ["lower(address) LIKE ?", "lower(place) LIKE ?", "lower(addr_label) LIKE ?"], [like, like, like]
        if k.bldg_key:
            num, street, town = k.bldg_key.split("|")
            if town:
                ors.append("bldg_key = ?")
                args.append(k.bldg_key)
            else:
                ors.append("bldg_key GLOB ?")
                args.append(_glob(f"{num}|{street}|") + "*")
        else:
            sk = self._street_key(where)
            if sk:
                street, town = sk.split("|")
                if town:
                    ors.append("addr_street = ?")
                    args.append(sk)
                else:
                    ors.append("addr_street GLOB ?")
                    args.append(_glob(f"{street}|") + "*")
        return "(" + " OR ".join(ors) + ")", args

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
        street = head
        if self.cad.geocoder is not None:
            try:
                for m in self.cad.geocoder(head) or []:
                    if m.get("kind") == "street" and float(m.get("confidence") or 0) >= addrkey.SNAP_CONFIDENCE and m.get("street"):
                        street, town = m["street"], town or L.town_of(m.get("municipality") or "")
                        break
            except Exception:  # noqa: BLE001
                pass
        sk = addrkey.street_key(street)
        return f"{sk}|{town.lower()}" if sk else ""

    def _text_hits(self, text: str) -> dict[int, str]:
        """Incident id -> where the words were found (a snippet), over the remarks, comments and transcripts."""
        q = fts_query(text)
        if not q:
            return {}
        hits: dict[int, str] = {}
        if self.fts:
            srcs = (("SELECT rowid AS iid, snippet(fts_incidents, 0, '[', ']', '…', 10) AS s FROM fts_incidents "
                     "WHERE fts_incidents MATCH ? LIMIT ?"),
                    ("SELECT e.incident_id AS iid, snippet(fts_events, 0, '[', ']', '…', 10) AS s FROM fts_events "
                     "JOIN events e ON e.id = fts_events.rowid WHERE fts_events MATCH ? LIMIT ?"),
                    ("SELECT l.incident_id AS iid, snippet(fts_radio, 0, '[', ']', '…', 10) AS s FROM fts_radio "
                     "JOIN radio_traffic t ON t.rowid = fts_radio.rowid JOIN radio_links l ON l.traffic_id = t.id "
                     "WHERE fts_radio MATCH ? LIMIT ?"))
            for sql in srcs:
                try:
                    for r in self.store.q(sql, (q, TEXT_MAX)):
                        if r["iid"] is not None:
                            hits.setdefault(r["iid"], r["s"])
                except sqlite3.OperationalError as e:
                    raise CadError(f"text: {e}") from None
            return hits
        like = "%" + text.lower().replace("%", "") + "%"           # no FTS5: remarks and comments, the slow way
        for r in self.store.q("SELECT id AS iid, remarks AS s FROM incidents WHERE lower(remarks) LIKE ? LIMIT ?", (like, TEXT_MAX)):
            hits.setdefault(r["iid"], r["s"][:120])
        for r in self.store.q(f"SELECT incident_id AS iid, text AS s FROM events WHERE incident_id IS NOT NULL AND kind IN "
                              f"({','.join('?' * len(COMMENT_KINDS))}) AND lower(text) LIKE ? LIMIT ?", (*COMMENT_KINDS, like, TEXT_MAX)):
            hits.setdefault(r["iid"], r["s"][:120])
        return hits

    def search(self, f: dict, csv: bool = False) -> dict:
        """The Search page: {rows, total, page, size, pending} newest first. rows: {id, number, created_at, closed_at,
        type, subtype, address, addr_label, disposition, status, lat, lon, units, hit?}."""
        self.catch_up(CATCH_UP, transcripts=bool(f.get("text")))     # the newest calls (and transcripts) in first
        conds, args = ["number IS NOT NULL"], []
        if f.get("where"):
            c, a = self._where_ids(f["where"])
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
        hits: dict[int, str] = {}
        if f.get("text"):
            hits = self._text_hits(f["text"])
            if not hits:
                return {"rows": [], "total": 0, "page": f["page"], "size": f["size"], "pending": self.left()}
            conds.append("id IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(list(hits)))
        where = " AND ".join(conds)
        total = self.store.one(f"SELECT COUNT(*) AS n FROM incidents WHERE {where}", args)["n"]
        size = CSV_MAX if csv else f["size"]
        off = 0 if csv else f["page"] * size
        rows = self.store.q(f"SELECT id, number, created_at, closed_at, type, subtype, address, addr_label, disposition, "
                            f"status, lat, lon FROM incidents WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                            (*args, size, off))
        self._units(rows)
        for r in rows:
            if r["id"] in hits:
                r["hit"] = hits[r["id"]]
        return {"rows": rows, "total": total, "page": f["page"], "size": f["size"], "pending": self.left()}

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
