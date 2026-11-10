"""Airport pre-alerts (GitHub #233): something heard or seen at Westchester County Airport (HPN) that could become a
PCFD response, shown at the top of the CAD's 60 Control column until a dispatcher accepts or dismisses it. One kind
and one component for every source, so each new source only says what it saw:

    atc    HPN ATC on an airband channel (sizeup/atc.py): a transcript that matched the pre-alert rules
           (data/atc/rules.json): a brush fire seen from a departure, a MAYDAY, "roll the trucks" ...
    adsb   an aircraft's own data (#235): a squawk 7500 / 7600 / 7700 or an emergency status near HPN (sizeup/aircraft.py's
           watch, from our receiver or adsb.lol / adsb.fi), through the same raise_alert() with source "adsb", beside the
           emergency alarm (#169) that already pushes (AircraftPreAlerts, below): one pre-alert per emergency, its
           details brought up to date while it lasts (update()), ended when it ends (end())
    radio  60 Control's fire radio (#369, sizeup/airport_watch.py): "alert level 1/2/3" declared for the airport; added to
           the tower's pre-alert for the same aircraft when one is open (add_to), ended by "alert canceled" / "landed"

The model (AirportAlert, what a source hands in):

    source, kind        "atc" / "adsb"; the source's category ("fire_off", "emergency", "squawk" ...)
    subject             the de-duplication key: the same subject again within dedupe_s, while the first is still
                        open, is added to it (its snippets, aircraft, phrases; the higher confidence), never a second
                        pre-alert; group (ATC: the channel) does the same within GROUP_S, so one exchange about one
                        thing ("smoke off the end" ... "say again" ... "roll the trucks") stays one pre-alert
    title, detail       "HPN ATC: possible brush fire reported off runway 16 departure"; one line under it
    confidence          0 to 1, the source's own (ATC: the rules' weights); shown as a word and a percentage
    snippets            [{id, at, channel, text, hits, context}]: the transmissions (id = the traffic id: its audio
                        is /api/audio/<id>.wav while it is kept), context = heard before the one that matched
    aircraft            [{callsign, hex, type, model, reg, lat, lon, alt, gs, trk, phase, rwy, dist, match, spoken, pos_at}]:
                        the aircraft it is about, from the Aircraft layer (ATC: the callsign heard, matched to ADS-B);
                        pos_at: when lat / lon was its fix (the map, aircraft.js, rings each by hex while it is open,
                        and keeps a faded last fix when one is lost)
    location            {lat, lon, approx: true, radius_m, basis} when the words give a place (an approximate point,
                        always said to be one), or {placed: false, basis} / None; never more precise than what was said
    phrases             the location words heard ("off the departure end of runway 16", "rye lake")
    event               {type, subtype}: what Accept puts on the new event
  optional (#235; each off by default, so ATC's behaviour is unchanged):
    lines               [[label, text], ...]: what is known, shown in order (ADS-B: aircraft, operator, emergency,
                        altitude, position, phase, source, time)
    alarm_id            the emergency alarm it goes with (sizeup/cad/alarms.py): the card says whether that is still
                        open or who acknowledged it, so one event shows once; the newest when raised again
    drill               drill mode (Settings > System > Alarms): marked DRILL
    reopen_s            the same subject within this long, open OR ended / expired, is the same pre-alert (open again,
                        never a second); 0: only an open one within dedupe_s, as before
    join_event          with exactly one Airport event open, it is that event's (incident_id; still open); an Airport
                        event made while it is open takes it too (match())
    limit               counted against max_per_hour (False: the source limits itself, as the alarm's 6 an hour)
    push                False: not pushed (the ADS-B alarm already pushed)

Lifecycle for a source that knows when its trigger is over (ADS-B): update(source, subject, ...) replaces an open one's
title, detail, lines and aircraft; end(source, subject, why) closes it as "ended" (reopened by the same subject within
its reopen_s). Every pre-alert is in GET /api/cad/export/airport-alerts.csv (csv()).

What a dispatcher does (POST /api/cad/airport-alert/<id>/accept | dismiss, dispatchers and admins):

    accept     a new PENDING event (Cad.create: numbered, on the Pending board, nobody dispatched): its type from
               the pre-alert, the remarks the transcript, the aircraft and the location words; an approximate point
               only as the map point, labelled approximate. Never a dispatch: a person dispatches from the event.
    dismiss    nothing to it (a reason may be given); kept and logged
    expired    nobody answered within expire_s (kept, logged)

Every step is a line in the CAD event log (kind "prealert", as the phone pre-alerts). A new pre-alert is pushed to
the alerts topic ([alerts] ntfy_topic, sizeup/notify.py ALERTS) once; additions are not pushed again. At most
max_per_hour new pre-alerts an hour per source: past that they are added to the latest open one (logged). Kept 90
days (table airport_alerts in the CAD SQLite: the hub's Litestream keeps it), listed 24 h.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sizeup.cad.store import TZ, CadError, OPERATOR_MAX

SOURCES = {"atc": "HPN ATC", "adsb": "ADS-B", "radio": "60 Control"}
KEEP_S = 90 * 24 * 3600
LIST_S = 24 * 3600
GROUP_S = 90.0                 # the same group (ATC: the channel) this recently: the same exchange, one pre-alert
MAX_SNIPPETS = 12
MAX_AIRCRAFT = 4
MAX_PHRASES = 12
LEVELS = ((0.8, "high"), (0.6, "medium"), (0.0, "low"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS airport_alerts (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,                 -- atc | adsb
  kind TEXT NOT NULL,                   -- the source's category: fire_off, emergency, squawk ...
  subject TEXT NOT NULL,                -- the de-duplication key
  grp TEXT NOT NULL DEFAULT '',         -- ATC: the channel (one exchange, one pre-alert)
  at REAL NOT NULL,                     -- first heard
  updated_at REAL NOT NULL,             -- last added to
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  channel TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}',      -- {snippets, aircraft, location, phrases, kinds, event}
  count INTEGER NOT NULL DEFAULT 1,     -- hits merged into it
  status TEXT NOT NULL,                 -- open | accepted | dismissed | expired
  outcome_at REAL,
  incident_id INTEGER,                  -- accepted: the event it made
  operator TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS airport_alerts_at ON airport_alerts(at);
"""


def _hms(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%H:%M:%S") if at else ""


def _ts(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%m/%d/%Y %H:%M:%S") if at else ""


def _lines(lines) -> list[list[str]]:
    """[[label, text], ...] as shown (at most 16; empty texts left out)."""
    out = []
    for x in (lines or [])[:16]:
        if isinstance(x, (list, tuple)) and len(x) == 2 and str(x[1]).strip():
            out.append([str(x[0])[:40], str(x[1])[:300]])
    return out


def _extras(a) -> dict:
    """The optional fields (#235) a new pre-alert keeps in its data; none for one that sets none (ATC's as before)."""
    out = {}
    if a.lines:
        out["lines"] = _lines(a.lines)
    if a.alarm_id is not None:
        out["alarm_id"] = int(a.alarm_id)
    if a.drill:
        out["drill"] = True
    if a.join_event:
        out["join_event"] = True
    return out


def level(conf: float) -> str:
    return next(w for lo, w in LEVELS if conf >= lo)


def conf_text(conf: float) -> str:
    """The pre-alert's own confidence as the card shows it: 'medium 75%' (never the aircraft match's)."""
    return f"{level(conf)} {round(float(conf) * 100)}%"


@dataclass
class AirportAlert:
    """What a source hands to AirportAlerts.raise_alert (the module doc)."""
    source: str
    kind: str
    subject: str
    title: str
    detail: str = ""
    confidence: float = 0.5
    at: float | None = None
    channel: str = ""
    group: str = ""
    snippets: list = field(default_factory=list)
    aircraft: list = field(default_factory=list)
    location: dict | None = None
    phrases: list = field(default_factory=list)
    event: dict = field(default_factory=dict)
    rank: int = 1                  # how serious: an addition names the pre-alert only when it ranks higher (or the
                                   # same, more confident): "roll the trucks" after a brush fire stays the brush fire
    push: bool = True
    lines: list = field(default_factory=list)      # #235 (the module doc's "optional")
    alarm_id: int | None = None
    drill: bool = False
    reopen_s: float = 0.0
    join_event: bool = False
    limit: bool = True


def _merge_list(old: list, new: list, key, limit: int) -> list:
    out, seen = list(old), {key(x) for x in old}
    for x in new:
        k = key(x)
        if k not in seen:
            out.append(x)
            seen.add(k)
    return out[-limit:]


def _better_location(old: dict | None, new: dict | None) -> dict | None:
    """A placed point beats none; of two, the tighter one."""
    if not new:
        return old
    if not old:
        return new
    po, pn = old.get("lat") is not None, new.get("lat") is not None
    if pn and (not po or float(new.get("radius_m") or 1e9) < float(old.get("radius_m") or 1e9)):
        return new
    return old


class AirportAlerts:
    """The CAD's airport pre-alerts (cad.airport_alerts). Sources call raise_alert(); the console reads state()."""

    def __init__(self, cad, dedupe_s: float = 300.0, expire_s: float = 1800.0, max_per_hour: int = 8,
                 log: Callable[[str], None] = print):
        self.cad, self.store, self.log, self.clock = cad, cad.store, log, cad.clock
        self.dedupe_s, self.expire_s, self.max_per_hour = float(dedupe_s), float(expire_s), int(max_per_hour)
        self.notifier = None                 # sizeup/notify.py KindNotifier (ALERTS), set by the app
        self.public_url = ""                 # the console's address for the push's click
        self.on_change: Callable[[], None] = lambda: None
        self._lock = threading.RLock()
        with self.store.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.store.db.execute(stmt)
        self.prune()

    # -- a source raises one ----------------------------------------------------------------------------------------

    def raise_alert(self, a: AirportAlert) -> tuple[dict | None, bool]:
        """(the pre-alert as it is now, True if it is new). Added to an open one with the same subject (dedupe_s) or
        group (GROUP_S); past max_per_hour, added to the latest open one of the source (None, False: there was none)."""
        if a.source not in SOURCES:
            raise ValueError(f"source: one of {', '.join(SOURCES)}")
        now = self.clock()
        at = float(a.at if a.at is not None else now)
        with self._lock:
            self.tick(now)
            row = self.store.one("SELECT * FROM airport_alerts WHERE status = 'open' AND source = ? AND subject = ? "
                                 "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
                                 (a.source, a.subject, now - max(self.dedupe_s, float(a.reopen_s or 0))))
            if row is None and a.reopen_s:                 # ended (or expired) lately: the same one, open again
                row = self.store.one("SELECT * FROM airport_alerts WHERE source = ? AND subject = ? AND status IN "
                                     "('ended', 'expired') AND outcome_at >= ? ORDER BY id DESC LIMIT 1",
                                     (a.source, a.subject, now - float(a.reopen_s)))
                if row is not None:
                    with self.store.tx() as db:
                        db.execute("UPDATE airport_alerts SET status = 'open', outcome_at = NULL, note = '' WHERE id = ?",
                                   (row["id"],))
                        self._event(db, f"Airport pre-alert {_hms(row['at'])} again: {a.title}", at=now)
                    row = dict(row, status="open")
            if row is None and a.group:
                row = self.store.one("SELECT * FROM airport_alerts WHERE status = 'open' AND source = ? AND grp = ? "
                                     "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1", (a.source, a.group, now - GROUP_S))
            if row is None and a.limit:
                n = self.store.one("SELECT COUNT(*) AS n FROM airport_alerts WHERE source = ? AND at >= ?",
                                   (a.source, now - 3600))["n"]
                if n >= self.max_per_hour:
                    row = self.store.one("SELECT * FROM airport_alerts WHERE status = 'open' AND source = ? "
                                         "ORDER BY updated_at DESC LIMIT 1", (a.source,))
                    self.log(f"airport pre-alert: {n} from {SOURCES[a.source]} in the last hour: "
                             + (f"added to #{row['id']}" if row else "not raised") + f": {a.title}")
                    if row is None:
                        return None, False
            if row is not None:
                out = self._add(row, a, now)
                new = False
            else:
                out = self._new(a, at, now)
                new = True
            if new and a.join_event:
                out = self._join_open_event(out)
        if new and a.push and self._push(out):
            out = self._pushed(out)
        self.on_change()
        return out, new

    def _new(self, a: AirportAlert, at: float, now: float) -> dict:
        data = {"snippets": a.snippets[-MAX_SNIPPETS:], "aircraft": a.aircraft[:MAX_AIRCRAFT], "location": a.location,
                "phrases": list(dict.fromkeys(a.phrases))[:MAX_PHRASES], "kinds": [a.kind], "event": dict(a.event),
                "rank": int(a.rank), **_extras(a)}
        conf = round(max(0.0, min(1.0, float(a.confidence))), 2)
        with self.store.tx() as db:
            cur = db.execute("INSERT INTO airport_alerts (source, kind, subject, grp, at, updated_at, title, detail, confidence, "
                             "channel, data, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                             (a.source, a.kind, a.subject[:200], a.group[:80], at, now, a.title[:200], a.detail[:400], conf,
                              a.channel[:40], json.dumps(data)))
            pid = cur.lastrowid
            self._event(db, f"Airport pre-alert ({SOURCES[a.source]}): {a.title} (alert {conf_text(conf)})", at=at)
        self.log(f"airport pre-alert #{pid}: {a.title} ({conf:.2f})")
        return self.get(pid)

    def _add(self, row: dict, a: AirportAlert, now: float) -> dict:
        data = json.loads(row["data"] or "{}")
        data["snippets"] = _merge_list(data.get("snippets") or [], a.snippets, lambda s: s.get("id") or s.get("text"), MAX_SNIPPETS)
        if a.lines and a.aircraft:                     # a source with live details (#235): the newest of each aircraft
            newer = {x.get("hex") or x.get("callsign") for x in a.aircraft}
            data["aircraft"] = [x for x in data.get("aircraft") or [] if (x.get("hex") or x.get("callsign")) not in newer]
        data["aircraft"] = _merge_list(data.get("aircraft") or [], a.aircraft, lambda x: x.get("hex") or x.get("callsign"),
                                       MAX_AIRCRAFT)
        data["phrases"] = list(dict.fromkeys([*(data.get("phrases") or []), *a.phrases]))[:MAX_PHRASES]
        data["location"] = _better_location(data.get("location"), a.location)
        data["kinds"] = list(dict.fromkeys([*(data.get("kinds") or [row["kind"]]), a.kind]))
        old_rank = int(data.get("rank", 1))
        stronger = int(a.rank) > old_rank or (int(a.rank) == old_rank and float(a.confidence) > float(row["confidence"]))
        if stronger:
            data["rank"] = int(a.rank)
            if a.event:
                data["event"] = dict(a.event)
        if a.lines:                                    # the newest details (#235): replaced, not merged
            data["lines"] = _lines(a.lines)
        if a.alarm_id is not None:
            data["alarm_id"] = int(a.alarm_id)
        for k in ("drill", "join_event"):
            if getattr(a, k):
                data[k] = True
        conf = round(max(float(row["confidence"]), min(1.0, float(a.confidence))), 2)
        title, detail, kind = (a.title, a.detail, a.kind) if stronger else (row["title"], row["detail"], row["kind"])
        with self.store.tx() as db:
            db.execute("UPDATE airport_alerts SET updated_at = ?, data = ?, confidence = ?, title = ?, detail = ?, kind = ?, "
                       "count = count + 1 WHERE id = ?", (now, json.dumps(data), conf, title[:200], detail[:400], kind, row["id"]))
        return self.get(row["id"])

    def _pushed(self, r: dict) -> dict:
        """Keep the confidence the one push said (additions may raise it later; it is not pushed again)."""
        with self._lock:
            row = self.store.one("SELECT data FROM airport_alerts WHERE id = ?", (r["id"],))
            data = json.loads((row or {}).get("data") or "{}")
            data["pushed"] = round(float(r["confidence"]), 2)
            with self.store.tx() as db:
                db.execute("UPDATE airport_alerts SET data = ? WHERE id = ?", (json.dumps(data), r["id"]))
        return self.get(r["id"])

    def _push(self, r: dict) -> bool:
        n = self.notifier
        if n is None or not getattr(n, "enabled", False):
            return False
        d = r["data"]
        body = []
        for s in (d.get("snippets") or [])[-2:]:
            body.append(f"“{s.get('text', '')[:160]}”")
        for x in (d.get("aircraft") or [])[:1]:
            body.append(aircraft_line(x))
        loc = d.get("location") or {}
        if loc.get("basis"):
            body.append(("approx. location: " if loc.get("lat") is not None else "location: ") + loc["basis"])
        body.append(f"alert {conf_text(r['confidence'])} · not dispatched · accept or dismiss in the CAD")
        prio = 5 if d.get("alert_level") else 4          # an alert level declared (#369): the top priority
        try:
            return bool(n.send(r["title"], " · ".join(b for b in body if b), prio, ("airplane", "warning"),
                               (self.public_url.rstrip("/") + "/cad") if self.public_url else ""))
        except Exception as e:  # noqa: BLE001 - a push never stops the pre-alert
            self.log(f"airport pre-alert: push: {type(e).__name__}: {e}")
            return False

    # -- a source that knows when its trigger is over (#235) ----------------------------------------------------------

    def _latest_open(self, source: str, subject: str) -> dict | None:
        return self.store.one("SELECT * FROM airport_alerts WHERE status = 'open' AND source = ? AND subject = ? "
                              "ORDER BY id DESC LIMIT 1", (source, subject))

    def update(self, source: str, subject: str, *, title: str | None = None, detail: str | None = None,
               lines: list | None = None, aircraft: list | None = None) -> dict | None:
        """The open pre-alert's newest details, in place (None: none open). The screens hear of it only when what they
        show changed."""
        with self._lock:
            row = self._latest_open(source, subject)
            if row is None:
                return None
            data = json.loads(row["data"] or "{}")
            before = json.dumps([row["title"], row["detail"], data.get("lines"), data.get("aircraft")])
            if lines is not None:
                data["lines"] = _lines(lines)
            if aircraft is not None:
                data["aircraft"] = aircraft[:MAX_AIRCRAFT]
            title = row["title"] if title is None else title[:200]
            detail = row["detail"] if detail is None else detail[:400]
            changed = before != json.dumps([title, detail, data.get("lines"), data.get("aircraft")])
            with self.store.tx() as db:
                db.execute("UPDATE airport_alerts SET updated_at = ?, title = ?, detail = ?, data = ? WHERE id = ?",
                           (self.clock(), title, detail, json.dumps(data), row["id"]))
        if changed:
            self.on_change()
        return self.get(row["id"])

    def end(self, source: str, subject: str, why: str) -> dict | None:
        """Its trigger is over ("ADS-B: squawk ended (now 1200)"): status "ended" (None: none open)."""
        with self._lock:
            row = self._latest_open(source, subject)
            if row is None:
                return None
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE airport_alerts SET status = 'ended', outcome_at = ?, note = ? WHERE id = ?",
                           (now, why[:200], row["id"]))
                self._event(db, f"Airport pre-alert {_hms(row['at'])} ended: {why}", iid=row["incident_id"], at=now)
        self.on_change()
        return self.get(row["id"])

    # -- by id (#369: sizeup/airport_watch.py follows one pre-alert across sources and channels) -------------------------

    def open_rows(self, within_s: float | None = None) -> list[dict]:
        """The open pre-alerts (updated within within_s), newest first."""
        now = self.clock()
        if within_s is None:
            return self._rows("a.status = 'open'", (), 50)
        return self._rows("a.status = 'open' AND a.updated_at >= ?", (now - float(within_s),), 50)

    def add_to(self, pid: int, a: AirportAlert) -> dict | None:
        """a added to open pre-alert pid (its snippets, aircraft, lines; its title when it ranks higher), whatever its
        source or subject. None: pid isn't open."""
        with self._lock:
            row = self.store.one("SELECT * FROM airport_alerts WHERE id = ? AND status = 'open'", (int(pid),))
            if row is None:
                return None
            out = self._add(row, a, self.clock())
        self.on_change()
        return out

    def update_id(self, pid: int, *, lines: list | None = None, aircraft: list | None = None,
                  data: dict | None = None, title: str | None = None) -> dict | None:
        """Open pre-alert pid's details in place (lines, aircraft, data keys, title); it stays open (updated_at: now, so
        it doesn't expire while what it is about goes on). None: not open."""
        with self._lock:
            row = self.store.one("SELECT * FROM airport_alerts WHERE id = ? AND status = 'open'", (int(pid),))
            if row is None:
                return None
            d = json.loads(row["data"] or "{}")
            before = json.dumps([row["title"], d], sort_keys=True)
            if lines is not None:
                d["lines"] = _lines(lines)
            if aircraft is not None:
                d["aircraft"] = aircraft[:MAX_AIRCRAFT]
            d.update(data or {})
            t = row["title"] if title is None else title[:200]
            changed = before != json.dumps([t, d], sort_keys=True)
            with self.store.tx() as db:
                db.execute("UPDATE airport_alerts SET updated_at = ?, title = ?, data = ? WHERE id = ?",
                           (self.clock(), t, json.dumps(d), row["id"]))
        if changed:
            self.on_change()
        return self.get(row["id"])

    def end_id(self, pid: int, why: str) -> dict | None:
        """Open pre-alert pid is over ("60 Control: alert canceled", "ADS-B: landed at HPN"): status "ended"."""
        with self._lock:
            row = self.store.one("SELECT * FROM airport_alerts WHERE id = ? AND status = 'open'", (int(pid),))
            if row is None:
                return None
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE airport_alerts SET status = 'ended', outcome_at = ?, note = ? WHERE id = ?",
                           (now, why[:200], row["id"]))
                self._event(db, f"Airport pre-alert {_hms(row['at'])} ended: {why}", iid=row["incident_id"], at=now)
        self.log(f"airport pre-alert #{row['id']} ended: {why}")
        self.on_change()
        return self.get(row["id"])

    def repush(self, pid: int) -> bool:
        """Pushed again: it became more serious (#369: an alert level declared for it). Once per call."""
        try:
            r = self.get(int(pid))
        except CadError:
            return False
        return self._push(r)

    def _open_airport_events(self) -> list[dict]:
        return self.store.q("SELECT id, number FROM incidents WHERE status NOT IN ('closed', 'dismissed') AND type = 'AIRPORT' "
                            "ORDER BY created_at DESC")

    def _join_open_event(self, r: dict) -> dict:
        rows = self._open_airport_events()
        if len(rows) != 1:
            return r
        self._link([r["id"]], rows[0]["id"], rows[0]["number"] or "")
        return self.get(r["id"])

    def _link(self, ids: list[int], iid: int, number: str) -> None:
        now = self.clock()
        with self.store.tx() as db:
            for pid in ids:
                p = db.execute("SELECT at, title FROM airport_alerts WHERE id = ?", (pid,)).fetchone()
                db.execute("UPDATE airport_alerts SET incident_id = ? WHERE id = ? AND incident_id IS NULL", (iid, pid))
                self._event(db, f"Airport pre-alert {_hms(p['at'])} on this event: {p['title']}", iid=iid, at=now)
        self.log(f"airport pre-alert: {len(ids)} on {number or f'event {iid}'}")

    def match(self, inc: dict | None) -> list[int]:
        """An Airport event was made (Cad.create): the open pre-alerts that join events (join_event: ADS-B) are its,
        still open (they end with their emergency, or a dispatcher dismisses them). ATC's are left to Accept."""
        if not inc or inc.get("type") != "AIRPORT" or inc.get("status") in ("closed", "dismissed"):
            return []
        with self._lock:
            ids = [r["id"] for r in self.store.q("SELECT id, data FROM airport_alerts WHERE status = 'open' AND "
                                                 "incident_id IS NULL ORDER BY at")
                   if (json.loads(r["data"] or "{}") or {}).get("join_event")]
            if ids:
                self._link(ids, inc["id"], inc.get("number") or "")
        if ids:
            self.on_change()
        return ids

    def csv(self) -> bytes:
        """GET /api/cad/export/airport-alerts.csv: every pre-alert kept (90 days), oldest first, with its outcome."""
        import csv as csvmod
        import io
        rows = list(reversed(self._rows("1 = 1", (), 100000)))
        buf = io.StringIO()
        w = csvmod.writer(buf, lineterminator="\r\n")
        w.writerow(["Raised", "Updated", "Source", "Kind", "Title", "Detail", "Confidence", "Outcome", "Outcome at",
                    "Incident #", "Operator", "Note", "Alarm", "Drill", "Heard", "Details"])
        for r in rows:
            d = r["data"]
            w.writerow([_ts(r["at"]), _ts(r["updated_at"]), r["source_name"], r["kind"], r["title"], r["detail"],
                        r["confidence"], r["status"], _ts(r["outcome_at"]),
                        r["incident_number"] or (f"#{r['incident_id']}" if r["incident_id"] else ""), r["operator"],
                        r["note"], d.get("alarm_id") or "", "yes" if d.get("drill") else "",
                        " | ".join(s.get("text", "") for s in d.get("snippets") or []),
                        " · ".join(f"{k}: {v}" for k, v in d.get("lines") or [])])
        return buf.getvalue().encode("utf-8-sig")

    # -- the dispatcher answers -------------------------------------------------------------------------------------

    def _event(self, db, text: str, iid=None, operator: str = "", at: float | None = None) -> None:
        self.store._event(db, iid, "prealert", text, operator, at=at)

    def _open(self, pid: int) -> dict:
        r = self.store.one("SELECT * FROM airport_alerts WHERE id = ?", (pid,))
        if r is None:
            raise CadError(f"no airport pre-alert {pid}")
        if r["status"] != "open":
            raise CadError(f"airport pre-alert {pid} is already {r['status']}")
        return r

    def dismiss(self, pid: int, body: dict) -> dict:
        """POST /api/cad/airport-alert/<id>/dismiss {operator, reason?}."""
        op, reason = _who(body), _reason(body)
        with self._lock:
            r = self._open(pid)
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE airport_alerts SET status = 'dismissed', outcome_at = ?, operator = ?, note = ? WHERE id = ?",
                           (now, op, reason, pid))
                self._event(db, f"Airport pre-alert {_hms(r['at'])} dismissed: {r['title']}" + (f" ({reason})" if reason else ""),
                            operator=op, at=now)
        self.on_change()
        return self.get(pid)

    def accept(self, pid: int, body: dict) -> dict:
        """POST /api/cad/airport-alert/<id>/accept {operator}: a new PENDING event from it (never a dispatch)."""
        op = _who(body)
        with self._lock:
            r = self.get(self._open(pid)["id"])
            if r["incident_id"] and r["data"].get("alert_level"):    # #369: an alert level made its event: that one
                inc = self.cad.store.incident(r["incident_id"])
                now = self.clock()
                with self.store.tx() as db:
                    db.execute("UPDATE airport_alerts SET status = 'accepted', outcome_at = ?, operator = ? WHERE id = ? "
                               "AND status = 'open'", (now, op, pid))
                    self._event(db, f"Airport pre-alert {_hms(r['at'])} accepted: {r['title']} · its event",
                                iid=inc["id"], operator=op, at=now)
                self.on_change()
                return dict(self.get(pid), incident=inc)
            d = r["data"]
            ev = d.get("event") or {}
            loc = d.get("location") or {}
            f = {"type": str(ev.get("type") or "AIRPORT")[:24], "subtype": str(ev.get("subtype") or "")[:24],
                 "received_via": "RADIO", "emergency": True, "remarks": remarks(r)[:4000], "operator": op}
            if loc.get("lat") is not None and loc.get("lon") is not None:
                f.update(lat=float(loc["lat"]), lon=float(loc["lon"]),
                         geo_label=f"APPROXIMATE (from ATC): {loc.get('basis') or ''}"[:200])
            inc = self.cad.create(f)
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE airport_alerts SET status = 'accepted', outcome_at = ?, operator = ?, incident_id = ? "
                           "WHERE id = ? AND status = 'open'", (now, op, inc["id"], pid))
                self._event(db, f"Airport pre-alert {_hms(r['at'])} accepted: {r['title']} · event pending, not dispatched",
                            iid=inc["id"], operator=op, at=now)
        self.on_change()
        return dict(self.get(pid), incident=inc)

    # -- housekeeping and what the screens get -------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> int:
        """Expire the open ones nobody answered in expire_s (from the last addition)."""
        now = self.clock() if now is None else now
        with self._lock:
            old = self.store.q("SELECT a.id, a.at, a.title, a.data, i.status AS inc FROM airport_alerts a LEFT JOIN incidents i "
                               "ON i.id = a.incident_id WHERE a.status = 'open' AND a.updated_at < ?", (now - self.expire_s,))
            # an alert level whose incident is open stays (#369): it is what the event is about, and never a second one
            old = [r for r in old if not (r["inc"] in ("queued", "pending", "active") and '"alert_level"' in (r["data"] or ""))]
            if not old:
                return 0
            with self.store.tx() as db:
                for r in old:
                    db.execute("UPDATE airport_alerts SET status = 'expired', outcome_at = ? WHERE id = ? AND status = 'open'",
                               (now, r["id"]))
                    self._event(db, f"Airport pre-alert {_hms(r['at'])} expired (unanswered {self.expire_s / 60:g} min): "
                                    f"{r['title']}", at=now)
        return len(old)

    def prune(self) -> int:
        with self.store.lock:
            return self.store.db.execute("DELETE FROM airport_alerts WHERE at < ?", (self.clock() - KEEP_S,)).rowcount

    def get(self, pid: int) -> dict:
        rows = self._rows("a.id = ?", (pid,))
        if not rows:
            raise CadError(f"no airport pre-alert {pid}")
        return rows[0]

    def _rows(self, where: str, args=(), limit: int = 100) -> list[dict]:
        rows = self.store.q("SELECT a.*, i.number AS incident_number FROM airport_alerts a "
                            f"LEFT JOIN incidents i ON i.id = a.incident_id WHERE {where} "
                            "ORDER BY a.at DESC, a.id DESC LIMIT ?", (*args, limit))
        for r in rows:
            try:
                r["data"] = json.loads(r["data"] or "{}")
            except ValueError:
                r["data"] = {}
            r["level"] = level(float(r["confidence"]))
            r["source_name"] = SOURCES.get(r["source"], r["source"])
            r["group"] = r.pop("grp", "")
        return rows

    def state(self, now: float | None = None) -> dict:
        """{items: the last 24 h and every open one, open, expire_s}."""
        now = self.clock() if now is None else now
        self.tick(now)
        items = self._rows("a.at >= ? OR a.status = 'open'", (now - LIST_S,), 50)
        # (no clock in it: the console's stream sends only what changed, and an idle one must stay tiny)
        # an alert level declared on 60 Control first (#369: the highest), then the newest
        opened = sorted((r for r in items if r["status"] == "open"),
                        key=lambda r: -int((r["data"].get("alert_level") or {}).get("level") or 0))
        return {"items": items, "open": opened, "expire_s": self.expire_s}


def _who(body: dict) -> str:
    op = body.get("operator") or ""
    if not isinstance(op, str):
        raise CadError("operator: text")
    return op.strip()[:OPERATOR_MAX]


def _reason(body: dict) -> str:
    r = body.get("reason") or ""
    if not isinstance(r, str):
        raise CadError("reason: text")
    return r.strip()[:200]


def aircraft_line(x: dict) -> str:
    """'JBU1234 (A320) · 1.2 NM · 1,400 ft · departing 16 · matched: exact'."""
    parts = [str(x.get("callsign") or x.get("spoken") or "aircraft")]
    t = x.get("type") or x.get("model")
    if t:
        parts[0] += f" ({t})"
    if x.get("dist") is not None:
        parts.append(f"{x['dist']} NM from HPN")
    if isinstance(x.get("alt"), (int, float)):
        parts.append(f"{int(x['alt']):,} ft")
    elif x.get("alt") == "ground":
        parts.append("GND")
    if x.get("phase"):
        parts.append(("near HPN" if x["phase"] == "near" else f"{x['phase']}") + (f" {x['rwy']}" if x.get("rwy") else ""))
    if x.get("hex") is None and x.get("spoken"):
        parts.append("heard, not on ADS-B")
    elif x.get("match"):
        parts.append(f"ADS-B match {round(float(x['match']) * 100)}%")      # the callsign to aircraft match, not the alert's
    if x.get("adsb"):
        parts.append(str(x["adsb"]))
    return " · ".join(parts)


def remarks(r: dict) -> str:
    """The new event's remarks: what was heard, who, where (as said), and that it came from a pre-alert."""
    d = r.get("data") or {}
    lines = [f"Airport pre-alert ({r.get('source_name') or SOURCES.get(r.get('source'), '')}, {_hms(r.get('at'))}): "
             f"{r.get('title', '')} (alert {conf_text(float(r.get('confidence') or 0))})"]
    for s in d.get("snippets") or []:
        lines.append(f"{_hms(s.get('at'))} {s.get('channel', '')}: “{s.get('text', '')}”")
    for x in d.get("aircraft") or []:
        lines.append("Aircraft: " + aircraft_line(x))
    loc = d.get("location") or {}
    if loc.get("basis"):
        lines.append(("Approx. location (from the words, not a fix): " if loc.get("lat") is not None
                      else "Location as heard: ") + loc["basis"])
    if d.get("phrases"):
        lines.append("Heard: " + "; ".join(d["phrases"]))
    for k, v in d.get("lines") or []:                 # an aircraft emergency's details (#235)
        lines.append(f"{k}: {v}")
    lines.append("Not dispatched.")
    return "\n".join(lines)


# -- an aircraft emergency (#235): sizeup/aircraft.py's watch, beside the #169 alarm --------------------------------------

REOPEN_S = 30 * 60.0          # the same aircraft and emergency again this soon: the same pre-alert (#169's REALARM_S)


class AircraftPreAlerts:
    """What the aircraft layer calls (AircraftLayer.prealerts): aircraft(ev, alarm id) when its watch raises an alarm,
    aircraft_again(ev) when the same emergency comes back within REALARM_S (no new alarm: the same pre-alert, open again),
    aircraft_update(ev) every 15 s while it lasts, aircraft_ended(ev, why) when it ends. The #169 limits stay the layer's
    (raised only with an alarm: 6 an hour; drill mode from the alarms' settings); the alarm already pushes, so this
    doesn't."""

    SOURCE = "adsb"

    def __init__(self, alerts: AirportAlerts, cad=None):
        self.alerts, self.cad = alerts, cad

    @staticmethod
    def subject(ev: dict) -> str:
        return f"{ev.get('hex')}:{ev.get('code')}"

    def _drill(self) -> bool:
        try:
            return bool(self.cad.alarms.config()["drill"])
        except Exception:  # noqa: BLE001 - no alarms here (a test's CAD): not a drill
            return False

    @staticmethod
    def describe(ev: dict) -> tuple[str, str, list[list[str]], dict]:
        """(title, detail, lines, aircraft): everything known about the aircraft, in the alarm's words."""
        from sizeup.cad import alarms as A
        who = A.aircraft_who(ev)
        meaning = ev.get("meaning") or "emergency"
        rx = ev.get("src") == "receiver"
        speed = f"{ev['gs']} kt" if isinstance(ev.get("gs"), (int, float)) else ""
        where = " · ".join(x for x in (ev.get("where"), ev.get("runway_end")) if x)
        op = ev.get("operator") or ""
        op_from = "from its callsign" if ev.get("operator_src") == "airline" else \
            f"from {ev.get('details_from') or ev.get('feed') or 'the feed'}"
        src = "our receiver (station)" if rx else f"{ev.get('feed') or 'a public feed'} (public data)"
        if ev.get("details_from"):
            src += f" · details: {ev['details_from']} (ICAO match)"
        pos_at = ev.get("pos_at")
        when = A._hms(ev.get("at")) + (f" (position {A._hms(pos_at)})" if pos_at and abs((ev.get("at") or 0) - pos_at) >= 1 else "")
        squawk = ev.get("squawk") or ""
        lines = [["Aircraft", f"{who} · {A.aircraft_type(ev)}"],
                 ["Operator", f"{op} ({op_from})" if op else "unknown (private owners never shown)"],
                 ["Emergency", meaning + (f" (squawk {squawk})" if squawk and squawk not in meaning else "")],
                 ["Altitude", " · ".join(x for x in (A.aircraft_alt(ev), speed) if x)],
                 ["Position", where or "unknown"],
                 ["Phase", A.aircraft_doing(ev) or "unknown"],
                 ["Source", src],
                 ["Time", when],
                 ["ICAO address", str(ev.get("hex") or "").lstrip("~").upper()]]
        title = f"AIRCRAFT EMERGENCY · {who} · {meaning}"
        detail = f"ADS-B: {'our receiver' if rx else ev.get('feed') or 'public data'} · {A.ADSB_NOTE}"
        acft = {"callsign": who, "hex": ev.get("hex"), "type": ev.get("type"), "model": ev.get("model"), "reg": ev.get("reg"),
                "lat": ev.get("lat"), "lon": ev.get("lon"), "alt": ev.get("alt"), "gs": ev.get("gs"),
                "phase": ev.get("phase"), "rwy": ev.get("runway"), "dist": ev.get("dist_nm"), "match": 1.0, "pos_at": ev.get("pos_at")}
        return title, detail, lines, acft

    def _alert(self, ev: dict, alarm_id=None) -> AirportAlert:
        title, detail, lines, acft = self.describe(ev)
        return AirportAlert(source=self.SOURCE, kind="squawk" if str(ev.get("code") or "").isdigit() else "emergency",
                            subject=self.subject(ev), title=title, detail=detail, confidence=1.0, at=ev.get("at"),
                            aircraft=[acft], lines=lines, alarm_id=alarm_id if isinstance(alarm_id, int) else None,
                            drill=self._drill(), reopen_s=REOPEN_S, join_event=True, limit=False, push=False, rank=3,
                            event={"type": "AIRPORT", "subtype": ""})

    def aircraft(self, ev: dict, alarm_id=None) -> dict | None:
        return self.alerts.raise_alert(self._alert(ev, alarm_id))[0]

    def aircraft_again(self, ev: dict) -> dict | None:
        return self.alerts.raise_alert(self._alert(ev))[0]

    def aircraft_update(self, ev: dict) -> dict | None:
        title, detail, lines, acft = self.describe(ev)
        return self.alerts.update(self.SOURCE, self.subject(ev), title=title, detail=detail, lines=lines, aircraft=[acft])

    def aircraft_ended(self, ev: dict, why: str) -> dict | None:
        return self.alerts.end(self.SOURCE, self.subject(ev), f"ADS-B: {why}")
