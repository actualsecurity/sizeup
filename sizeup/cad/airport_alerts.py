"""Airport pre-alerts (GitHub #233): something heard or seen at Westchester County Airport (HPN) that could become a
PCFD response, shown at the top of the CAD's 60 Control column until a dispatcher accepts or dismisses it. One kind
and one component for every source, so each new source only says what it saw:

    atc    HPN ATC on an airband channel (sizeup/atc.py): a transcript that matched the pre-alert rules
           (data/atc/rules.json): a brush fire seen from a departure, a MAYDAY, "roll the trucks" ...
    adsb   an aircraft's own data (#235: our ADS-B receiver; a squawk 7700, an emergency status), through the
           same raise_alert() with source "adsb"

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
    aircraft            [{callsign, hex, type, model, reg, lat, lon, alt, gs, trk, phase, rwy, dist, match, spoken}]:
                        the aircraft it is about, from the Aircraft layer (ATC: the callsign heard, matched to ADS-B)
    location            {lat, lon, approx: true, radius_m, basis} when the words give a place (an approximate point,
                        always said to be one), or {placed: false, basis} / None; never more precise than what was said
    phrases             the location words heard ("off the departure end of runway 16", "rye lake")
    event               {type, subtype}: what Accept puts on the new event

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

SOURCES = {"atc": "HPN ATC", "adsb": "ADS-B"}
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


def level(conf: float) -> str:
    return next(w for lo, w in LEVELS if conf >= lo)


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
                                 "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1", (a.source, a.subject, now - self.dedupe_s))
            if row is None and a.group:
                row = self.store.one("SELECT * FROM airport_alerts WHERE status = 'open' AND source = ? AND grp = ? "
                                     "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1", (a.source, a.group, now - GROUP_S))
            if row is None:
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
        if new:
            self._push(out)
        self.on_change()
        return out, new

    def _new(self, a: AirportAlert, at: float, now: float) -> dict:
        data = {"snippets": a.snippets[-MAX_SNIPPETS:], "aircraft": a.aircraft[:MAX_AIRCRAFT], "location": a.location,
                "phrases": list(dict.fromkeys(a.phrases))[:MAX_PHRASES], "kinds": [a.kind], "event": dict(a.event),
                "rank": int(a.rank)}
        conf = round(max(0.0, min(1.0, float(a.confidence))), 2)
        with self.store.tx() as db:
            cur = db.execute("INSERT INTO airport_alerts (source, kind, subject, grp, at, updated_at, title, detail, confidence, "
                             "channel, data, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                             (a.source, a.kind, a.subject[:200], a.group[:80], at, now, a.title[:200], a.detail[:400], conf,
                              a.channel[:40], json.dumps(data)))
            pid = cur.lastrowid
            self._event(db, f"Airport pre-alert ({SOURCES[a.source]}): {a.title} ({level(conf)} confidence)", at=at)
        self.log(f"airport pre-alert #{pid}: {a.title} ({conf:.2f})")
        return self.get(pid)

    def _add(self, row: dict, a: AirportAlert, now: float) -> dict:
        data = json.loads(row["data"] or "{}")
        data["snippets"] = _merge_list(data.get("snippets") or [], a.snippets, lambda s: s.get("id") or s.get("text"), MAX_SNIPPETS)
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
        conf = round(max(float(row["confidence"]), min(1.0, float(a.confidence))), 2)
        title, detail, kind = (a.title, a.detail, a.kind) if stronger else (row["title"], row["detail"], row["kind"])
        with self.store.tx() as db:
            db.execute("UPDATE airport_alerts SET updated_at = ?, data = ?, confidence = ?, title = ?, detail = ?, kind = ?, "
                       "count = count + 1 WHERE id = ?", (now, json.dumps(data), conf, title[:200], detail[:400], kind, row["id"]))
        return self.get(row["id"])

    def _push(self, r: dict) -> None:
        n = self.notifier
        if n is None or not getattr(n, "enabled", False):
            return
        d = r["data"]
        body = []
        for s in (d.get("snippets") or [])[-2:]:
            body.append(f"“{s.get('text', '')[:160]}”")
        for x in (d.get("aircraft") or [])[:1]:
            body.append(aircraft_line(x))
        loc = d.get("location") or {}
        if loc.get("basis"):
            body.append(("approx. location: " if loc.get("lat") is not None else "location: ") + loc["basis"])
        body.append(f"{level(r['confidence'])} confidence; not dispatched: accept or dismiss it in the CAD")
        try:
            n.send(r["title"], " · ".join(b for b in body if b), 4, ("airplane", "warning"),
                   (self.public_url.rstrip("/") + "/cad") if self.public_url else "")
        except Exception as e:  # noqa: BLE001 - a push never stops the pre-alert
            self.log(f"airport pre-alert: push: {type(e).__name__}: {e}")

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
                self._event(db, f"Airport pre-alert {_hms(r['at'])} accepted: {r['title']}: event made, pending (not dispatched)",
                            iid=inc["id"], operator=op, at=now)
        self.on_change()
        return dict(self.get(pid), incident=inc)

    # -- housekeeping and what the screens get -------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> int:
        """Expire the open ones nobody answered in expire_s (from the last addition)."""
        now = self.clock() if now is None else now
        with self._lock:
            old = self.store.q("SELECT id, at, title FROM airport_alerts WHERE status = 'open' AND updated_at < ?",
                               (now - self.expire_s,))
            if not old:
                return 0
            with self.store.tx() as db:
                for r in old:
                    db.execute("UPDATE airport_alerts SET status = 'expired', outcome_at = ? WHERE id = ? AND status = 'open'",
                               (now, r["id"]))
                    self._event(db, f"Airport pre-alert {_hms(r['at'])} expired: nobody accepted it in "
                                    f"{self.expire_s / 60:g} min: {r['title']}", at=now)
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
        return {"items": items, "open": [r for r in items if r["status"] == "open"], "expire_s": self.expire_s}


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
        parts.append("on the ground")
    if x.get("phase"):
        parts.append(f"{x['phase']}" + (f" {x['rwy']}" if x.get("rwy") else ""))
    if x.get("hex") is None and x.get("spoken"):
        parts.append("heard, not on ADS-B")
    elif x.get("match"):
        parts.append(f"ADS-B match {round(float(x['match']) * 100)}%")
    return " · ".join(parts)


def remarks(r: dict) -> str:
    """The new event's remarks: what was heard, who, where (as said), and that it came from a pre-alert."""
    d = r.get("data") or {}
    lines = [f"Airport pre-alert ({r.get('source_name') or SOURCES.get(r.get('source'), '')}, {_hms(r.get('at'))}): "
             f"{r.get('title', '')} ({r.get('level') or level(float(r.get('confidence') or 0))} confidence)"]
    for s in d.get("snippets") or []:
        lines.append(f"{_hms(s.get('at'))} {s.get('channel', '')}: “{s.get('text', '')}”")
    for x in d.get("aircraft") or []:
        lines.append("Aircraft: " + aircraft_line(x))
    loc = d.get("location") or {}
    if loc.get("basis"):
        lines.append(("Approximate location (from the words, not a fix): " if loc.get("lat") is not None
                      else "Location as heard: ") + loc["basis"])
    if d.get("phrases"):
        lines.append("Heard: " + "; ".join(d["phrases"]))
    lines.append("Not dispatched: made from the pre-alert, dispatch from here.")
    return "\n".join(lines)
