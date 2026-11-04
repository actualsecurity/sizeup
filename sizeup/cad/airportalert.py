"""Airport pre-alerts: one kind of CAD pre-alert item ("airport") for anything at or near HPN that could become a PCFD
response before anyone calls it in (GitHub #235; shared with #233's HPN ATC pre-alerts).

Where they come from (each a "trigger"):
    adsb   an emergency squawk (7500 / 7600 / 7700) or an aircraft's own emergency status near HPN (sizeup/aircraft.py's
           watch, #169's rules: inside the area, below 18,000 ft, a fresh message, at most 6 an hour, drill mode). The
           alarm and its push stay as they were; this puts the event in the queue with everything known about the
           aircraft (aircraft(), below). One item per emergency: a flicker, the same emergency again within REOPEN_S,
           or a hub restart while it is still squawking all bring back the SAME item (by its key), never a second.
    atc    (#233) something heard on HPN ATC that could become a call. Its module calls raise_alert() with its own
           trigger, key and lines; nothing here is ADS-B-specific except aircraft*().

The interface (stable: whichever of #233 / #235 lands second adapts to it):

    ap = AirportPreAlerts(cad, log)               built by the app on the CAD of record (hub, all-in-one): cad.airport_prealert
    ap.raise_alert(key, trigger, title, lines, *, at=None, source="", summary="", data=None, alarm_id=None,
                   expire_s=None) -> item
        a new pre-alert, or the open (or recently ended) one with this key brought up to date and open again. lines:
        [[label, text], ...] in the order shown; data: anything else the trigger keeps (JSON); expire_s: an open item
        not updated this long ends by itself ("no update for 10 min": EXPIRE_S).
    ap.update(key, *, title=None, lines=None, summary=None, data=None) -> item | None    an open one's newest details
    ap.end(key, why) -> item | None               its trigger is over ("squawk ended (now 1200)"): status "ended"
    ap.dismiss(id, {operator, reason?}) -> item   POST /api/cad/airport-prealert/<id>/dismiss: nothing to do (dispatchers
                                                  and admins; in the event log and the person's activity)
    ap.match(inc)                                 Cad.create made an Airport event: the open ones are "matched" to it, and
                                                  its chronology lists them
    ap.state(now) -> {kind, time, open, items}    the CAD state's "airport_prealert": open ones and the last 24 h (small:
                                                  it changes only when an item does, updates at most every 15 s; never
                                                  the aircraft themselves, #146)
    ap.get(id), ap.csv()                          GET /api/cad/export/airport-prealerts.csv

An item: {id, kind: "airport", key, trigger, at (first raised), updated_at, title, summary, lines, source, data,
status: open | ended | dismissed | matched | expired, outcome_at, operator, note, alarm_id, incident_id,
incident_number, drill}. The page (static/airport-prealert.js) shows the open ones at the top of the queue, on the
console and the wall board, with the alarm's state when the item has one (one event, shown once).

Table airport_prealerts in the CAD SQLite (the hub's Litestream keeps it): listed LIST_S, kept KEEP_S.
"""

from __future__ import annotations

import csv
import io
import json
import threading
from datetime import datetime
from typing import Callable

from sizeup.cad.store import TZ, CadError, OPERATOR_MAX

KIND = "airport"
LIST_S = 24 * 3600.0
KEEP_S = 90 * 24 * 3600.0
REOPEN_S = 30 * 60.0          # the same key this soon after it ended: the same item, open again (#169's REALARM_S)
EXPIRE_S = 10 * 60.0          # an open item nothing has updated for this long: ended by itself
MAX_LINES = 16
TRIGGERS = {"adsb": "ADS-B", "atc": "HPN ATC"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS airport_prealerts (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,                    -- the trigger's own: "adsb:<hex>:<code>" (one item per emergency)
  trigger TEXT NOT NULL,                -- adsb | atc (#233) | ...
  at REAL NOT NULL,                     -- first raised
  updated_at REAL NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  lines TEXT NOT NULL DEFAULT '[]',     -- [[label, text], ...]
  source TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,                 -- open | ended | dismissed | matched | expired
  outcome_at REAL,
  operator TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  alarm_id INTEGER,
  incident_id INTEGER,
  drill INTEGER NOT NULL DEFAULT 0,
  expire_s REAL NOT NULL DEFAULT 600
);
CREATE INDEX IF NOT EXISTS airport_prealerts_key ON airport_prealerts(key);
CREATE INDEX IF NOT EXISTS airport_prealerts_at ON airport_prealerts(at);
"""
CSV_COLUMNS = ["Raised", "Updated", "Trigger", "Title", "Summary", "Source", "Outcome", "Outcome at", "Incident #",
               "Operator", "Note", "Alarm", "Drill", "Details"]


def _hms(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%H:%M:%S") if at else ""


def _ts(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%m/%d/%Y %H:%M:%S") if at else ""


def _lines(lines) -> list[list[str]]:
    out = []
    for x in (lines or [])[:MAX_LINES]:
        if isinstance(x, (list, tuple)) and len(x) == 2 and str(x[1]).strip():
            out.append([str(x[0])[:40], str(x[1])[:300]])
    return out


class AirportPreAlerts:
    def __init__(self, cad, log: Callable[[str], None] = print):
        self.cad, self.store, self.log = cad, cad.store, log
        self.clock = cad.clock
        self._lock = threading.RLock()
        self.on_change: Callable[[], None] = lambda: None       # the app: push the screens now
        with self.store.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.store.db.execute(stmt)
        self.prune()

    def prune(self) -> int:
        with self.store.lock:
            return self.store.db.execute("DELETE FROM airport_prealerts WHERE at < ?", (self.clock() - KEEP_S,)).rowcount

    def _drill(self) -> bool:
        try:
            return bool(self.cad.alarms.config()["drill"])
        except Exception:  # noqa: BLE001 - no alarms here (a test's CAD): not a drill
            return False

    def _log(self, db, text: str, iid=None, operator: str = "", at: float | None = None) -> None:
        self.store._event(db, iid, "prealert", text, operator, at=at)

    def _latest(self, key: str) -> dict | None:
        return self.store.one("SELECT * FROM airport_prealerts WHERE key = ? ORDER BY id DESC LIMIT 1", (key,))

    # -- the interface ------------------------------------------------------------------------------------------------

    def raise_alert(self, key: str, trigger: str, title: str, lines, *, at: float | None = None, source: str = "",
                    summary: str = "", data: dict | None = None, alarm_id: int | None = None,
                    expire_s: float | None = None) -> dict:
        if not key or not trigger or not title:
            raise CadError("an airport pre-alert needs a key, a trigger and a title")
        now = self.clock()
        at = now if at is None else float(at)
        lines_j = json.dumps(_lines(lines))
        data_j = json.dumps(data or {}, separators=(",", ":"), default=str)
        with self._lock:
            cur = self._latest(key)
            reuse = cur is not None and (cur["status"] == "open" or (cur["status"] in ("ended", "expired")
                                                                       and now - (cur["outcome_at"] or now) < REOPEN_S))
            with self.store.tx() as db:
                if reuse:
                    again = cur["status"] != "open"
                    db.execute("UPDATE airport_prealerts SET updated_at = ?, title = ?, summary = ?, lines = ?, source = ?, "
                               "data = ?, status = 'open', outcome_at = NULL, note = '', alarm_id = COALESCE(?, alarm_id) "
                               "WHERE id = ?", (now, title[:200], summary[:600], lines_j, source[:80], data_j, alarm_id,
                                                cur["id"]))
                    pid = cur["id"]
                    if again:
                        self._log(db, f"Airport pre-alert {_hms(cur['at'])} again: {title}", at=now)
                else:
                    pid = db.execute(
                        "INSERT INTO airport_prealerts (key, trigger, at, updated_at, title, summary, lines, source, data, "
                        "status, alarm_id, drill, expire_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                        (key[:120], trigger[:20], at, now, title[:200], summary[:600], lines_j, source[:80], data_j,
                         alarm_id, int(self._drill()), float(expire_s or EXPIRE_S))).lastrowid
                    self._log(db, ("DRILL — " if self._drill() else "") + f"Airport pre-alert "
                                  f"({TRIGGERS.get(trigger, trigger)}): {title}" + (f": {summary}" if summary else ""), at=at)
            if not reuse:
                self.log(f"airport pre-alert: {title}")
                self._match_open_event(pid)
        self.on_change()
        return self.get(pid)

    def update(self, key: str, *, title: str | None = None, lines=None, summary: str | None = None,
               data: dict | None = None) -> dict | None:
        with self._lock:
            cur = self._latest(key)
            if cur is None or cur["status"] != "open":
                return None
            sets, args = ["updated_at = ?"], [self.clock()]
            for col, v in (("title", title), ("summary", summary)):
                if v is not None:
                    sets.append(f"{col} = ?")
                    args.append(v[:600])
            if lines is not None:
                sets.append("lines = ?")
                args.append(json.dumps(_lines(lines)))
            if data is not None:
                sets.append("data = ?")
                args.append(json.dumps(data, separators=(",", ":"), default=str))
            changed = (title is not None and title != cur["title"]) or (summary is not None and summary != cur["summary"]) \
                or (lines is not None and json.dumps(_lines(lines)) != cur["lines"])
            with self.store.tx() as db:
                db.execute(f"UPDATE airport_prealerts SET {', '.join(sets)} WHERE id = ?", (*args, cur["id"]))
        if changed:
            self.on_change()
        return self.get(cur["id"])

    def end(self, key: str, why: str) -> dict | None:
        with self._lock:
            cur = self._latest(key)
            if cur is None or cur["status"] != "open":
                return None
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE airport_prealerts SET status = 'ended', outcome_at = ?, updated_at = ?, note = ? "
                           "WHERE id = ?", (now, now, why[:200], cur["id"]))
                self._log(db, f"Airport pre-alert {_hms(cur['at'])} ended: {why}", iid=cur["incident_id"], at=now)
        self.on_change()
        return self.get(cur["id"])

    def dismiss(self, pid: int, body: dict) -> dict:
        op, reason = body.get("operator") or "", body.get("reason") or ""
        if not isinstance(op, str) or not isinstance(reason, str):
            raise CadError("operator, reason: text")
        op, reason = op.strip()[:OPERATOR_MAX], reason.strip()[:200]
        with self._lock:
            p = self.store.one("SELECT * FROM airport_prealerts WHERE id = ?", (pid,))
            if p is None:
                raise CadError(f"no airport pre-alert {pid}")
            if p["status"] not in ("open", "ended"):
                raise CadError(f"airport pre-alert {pid} is already {p['status']}")
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE airport_prealerts SET status = 'dismissed', outcome_at = ?, operator = ?, note = ? "
                           "WHERE id = ?", (now, op, reason, pid))
                self._log(db, f"Airport pre-alert {_hms(p['at'])} dismissed" + (f": {reason}" if reason else ""),
                          operator=op, at=now)
        self.on_change()
        return self.get(pid)

    def match(self, inc: dict | None) -> list[int]:
        """An Airport event was made: the pre-alerts still open (or ended in the last REOPEN_S) are its."""
        if not inc or inc.get("type") != "AIRPORT" or inc.get("status") in ("closed", "dismissed"):
            return []
        now = self.clock()
        with self._lock:
            ids = [r["id"] for r in self.store.q(
                "SELECT id FROM airport_prealerts WHERE incident_id IS NULL AND (status = 'open' OR (status IN "
                "('ended', 'expired') AND outcome_at >= ?)) ORDER BY at", (now - REOPEN_S,))]
            if ids:
                self._link(ids, inc["id"], inc.get("number") or "")
        if ids:
            self.on_change()
        return ids

    def _match_open_event(self, pid: int) -> None:
        """A pre-alert raised while exactly one Airport event is open is that event's (like the aircraft alarm)."""
        rows = self.store.q("SELECT id, number FROM incidents WHERE status NOT IN ('closed', 'dismissed') AND type = 'AIRPORT' "
                            "ORDER BY created_at DESC")
        if len(rows) == 1:
            self._link([pid], rows[0]["id"], rows[0]["number"] or "", keep_open=True)

    def _link(self, ids: list[int], iid: int, number: str, keep_open: bool = False) -> None:
        now = self.clock()
        with self.store.tx() as db:
            for pid in ids:
                p = db.execute("SELECT at, title, status FROM airport_prealerts WHERE id = ?", (pid,)).fetchone()
                if keep_open or p["status"] != "open":
                    db.execute("UPDATE airport_prealerts SET incident_id = ? WHERE id = ?", (iid, pid))
                else:
                    db.execute("UPDATE airport_prealerts SET incident_id = ?, status = 'matched', outcome_at = ? WHERE id = ?",
                               (iid, now, pid))
                self._log(db, f"Airport pre-alert {_hms(p['at'])} on this event: {p['title']}", iid=iid, at=now)
        self.log(f"airport pre-alert: {len(ids)} on {number or f'event {iid}'}")

    def tick(self, now: float | None = None) -> int:
        """End the open ones nothing has updated for their expire_s. The number ended."""
        now = self.clock() if now is None else now
        with self._lock:
            old = self.store.q("SELECT id, at, expire_s FROM airport_prealerts WHERE status = 'open' AND updated_at < ? - expire_s",
                               (now,))
            if not old:
                return 0
            with self.store.tx() as db:
                for p in old:
                    db.execute("UPDATE airport_prealerts SET status = 'expired', outcome_at = ?, note = ? WHERE id = ?",
                               (now, f"no update for {p['expire_s'] / 60:g} min", p["id"]))
                    self._log(db, f"Airport pre-alert {_hms(p['at'])} expired: no update for {p['expire_s'] / 60:g} min", at=now)
        self.on_change()
        return len(old)

    # -- reading ------------------------------------------------------------------------------------------------------------

    def _rows(self, where: str, args=(), limit: int = 50) -> list[dict]:
        rows = self.store.q("SELECT p.*, i.number AS incident_number FROM airport_prealerts p LEFT JOIN incidents i "
                            f"ON i.id = p.incident_id WHERE {where} ORDER BY p.at DESC, p.id DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["kind"] = KIND
            r["drill"] = bool(r["drill"])
            for k, empty in (("lines", []), ("data", {})):
                try:
                    r[k] = json.loads(r[k] or "null") or empty
                except ValueError:
                    r[k] = empty
        return rows

    def get(self, pid: int) -> dict:
        rows = self._rows("p.id = ?", (pid,), 1)
        if not rows:
            raise CadError(f"no airport pre-alert {pid}")
        return rows[0]

    def state(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        self.tick(now)
        items = self._rows("p.at >= ? OR p.status = 'open'", (now - LIST_S,), 30)
        return {"kind": KIND, "time": now, "open": [p for p in items if p["status"] == "open"], "items": items}

    def csv(self) -> bytes:
        rows = list(reversed(self._rows("1 = 1", (), 100000)))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([_ts(r["at"]), _ts(r["updated_at"]), TRIGGERS.get(r["trigger"], r["trigger"]), r["title"], r["summary"],
                        r["source"], r["status"], _ts(r["outcome_at"]),
                        r["incident_number"] or (f"#{r['incident_id']}" if r["incident_id"] else ""), r["operator"], r["note"],
                        r["alarm_id"] or "", "yes" if r["drill"] else "", " · ".join(f"{a}: {b}" for a, b in r["lines"])])
        return buf.getvalue().encode("utf-8-sig")

    # -- an aircraft emergency (sizeup/aircraft.py's watch, #235) ---------------------------------------------------------

    @staticmethod
    def aircraft_key(ev: dict) -> str:
        return f"adsb:{ev.get('hex')}:{ev.get('code')}"

    @staticmethod
    def aircraft_lines(ev: dict) -> tuple[str, str, list[list[str]], str]:
        """(title, summary, lines, source) for an aircraft emergency: who, what, how high, where, doing what, from where."""
        from sizeup.cad import alarms as A
        who = A.aircraft_who(ev)
        meaning = ev.get("meaning") or "emergency"
        title = f"AIRCRAFT EMERGENCY · {who} · {meaning}"
        rx = ev.get("src") == "receiver"
        source = "ADS-B: our receiver" if rx else f"ADS-B: {ev.get('feed') or 'public data'}"
        speed = f"{ev['gs']} kt" if isinstance(ev.get("gs"), (int, float)) else ""
        where = " · ".join(x for x in (ev.get("where"), ev.get("runway_end")) if x)
        op = ev.get("operator") or ""
        op_from = {"airline": "from its callsign"}.get(ev.get("operator_src") or "", f"from {ev.get('details_from') or ev.get('feed') or 'the feed'}")
        src = ("our receiver on the station" if rx else f"{ev.get('feed') or 'a public feed'} (public data)")
        if ev.get("details_from"):
            src += f"; type, registration and operator from {ev['details_from']} by its ICAO address"
        pos_at = ev.get("pos_at")
        when = A._hms(ev.get("at")) + (f" (position {A._hms(pos_at)})" if pos_at and abs((ev.get('at') or 0) - pos_at) >= 1 else "")
        lines = [["Aircraft", f"{who} · {A.aircraft_type(ev)}"],
                 ["Operator", f"{op} ({op_from})" if op else "not known (a private owner's name is never shown)"],
                 ["Emergency", meaning + (f" (squawk {ev['squawk']})" if ev.get("squawk") and ev.get("squawk") not in meaning else "")],
                 ["Altitude", ", ".join(x for x in (A.aircraft_alt(ev), speed) if x)],
                 ["Position", where or "not known"],
                 ["Phase", A.aircraft_doing(ev) or "not known"],
                 ["Source", src],
                 ["Time", when],
                 ["ICAO address", str(ev.get("hex") or "").lstrip("~").upper()]]
        summary = A.aircraft_text(ev)
        return title, summary, lines, source

    def aircraft(self, ev: dict, alarm_id=None) -> dict:
        title, summary, lines, source = self.aircraft_lines(ev)
        return self.raise_alert(self.aircraft_key(ev), "adsb", title, lines, at=ev.get("at"), source=source,
                                summary=summary, alarm_id=alarm_id if isinstance(alarm_id, int) else None,
                                data={"hex": ev.get("hex"), "code": ev.get("code"), "lat": ev.get("lat"), "lon": ev.get("lon")})

    def aircraft_again(self, ev: dict) -> dict:
        return self.aircraft(ev)

    def aircraft_update(self, ev: dict) -> dict | None:
        title, summary, lines, _source = self.aircraft_lines(ev)
        return self.update(self.aircraft_key(ev), title=title, summary=summary, lines=lines,
                           data={"hex": ev.get("hex"), "code": ev.get("code"), "lat": ev.get("lat"), "lon": ev.get("lon")})

    def aircraft_ended(self, ev: dict, why: str) -> dict | None:
        return self.end(self.aircraft_key(ev), f"ADS-B: {why}")
