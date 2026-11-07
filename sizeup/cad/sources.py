"""Incident sources, ranked (#297): which reports attach to which incident, whose fields win, and what each source saw
first. On the CAD of record (the hub, or all-in-one). The station's adapters: sizeup/sources/ (the interface) and
sizeup/sources/runner.py (they come here as "source_event" / "source_health" through the station's outbox).

    tiers      every external CAD feed that reported its health (rank 1 = the primary, 2, 3 ...), then the paging decoder
               (PAGING_RANK: below every feed). A feed is healthy while its last report says "ok" and is under
               REPORT_STALE_S old (older: "stale"); paging is healthy while the station is connected (paging_ok). The
               acting primary is the first healthy tier, promoted and demoted automatically (the console's pill and
               Settings > Health say which)
    create     never delayed: a page for us makes its incident at once whatever the feeds' health (an occasional
               duplicate is acceptable, a delay is not), and so does a feed's new event, unless it attaches (below).
               A feed event already closed, or dispatched more than MATCH_WINDOW_S ago (a first run, a lost database),
               is recorded and never makes an incident
    match      an OPEN incident (queued, pending, active; never closed or dismissed) created within MATCH_WINDOW_S of the
               report, with no report of the same source on it. When both have a location, the same place is required
               (same_place: house number and street, direction and suffix; an intersection never equals a numbered
               address), whatever units they share; a unit in common (assignments not cleared, and the reports' units)
               is enough only when one side has no location
    attach     a page attaches only to an incident a feed has; a feed event only to a queued one or one a feed made.
               Any other match: the report makes its own incident, noted "possibly the same as #N" on both
    authority  the best-ranked report's type and location are applied while the incident is queued, or for a field no
               person has edited (manual_edits: type, location, pin); otherwise a timeline note, and the incident view
               offers "Use <feed>'s <field>" (feed_use). A pin a person set is never cleared
    timeline   every change a feed reports is an entry on the incident (kind "feed"): type, location, units, each time
               (per unit too), each new comment at its own time, closed or reopened, each extra
    merge      a feed's event naming other ids of the same source in its aliases: each alias's report is retired
               (merged_into) with a note on both incidents and in the log. The alias's incident is dismissed only when
               it is still queued AND that same source made it (its report then moves onto the event); otherwise a note
    measure    each report's first-seen time; per incident and source the earliest, and the lead over paging
    trigger    paging tones, a radio pre-alert, the phone ring: poke() tells the adapters to poll now
    incoming   pre-alerts never make an incident: a transmission on the 60 Control feed's talkgroups that names us and is
               dispatch opening a call to us, or announces one ("incoming additional alarm", "stand by for a page"), is
               a "possible incoming" notice for INCOMING_S (the phone pre-alert has its own list)
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from statistics import median
from typing import Callable

from sizeup.cad.store import OPEN, TZ
from sizeup.sources import EXTRA_SHOWN, HEALTH_STATES, SOURCE_ID, TIME_KEYS, FeedEvent, SourceError

PAGING, PAGING_RANK, PAGING_LABEL = "paging", 1000, "Paging"
FEED_SOURCE = "CAD feed"               # incidents.source of one an external feed made
PAGE_SOURCE = "60 Control page"
MATCH_WINDOW_S = 20 * 60.0
REPORT_STALE_S = 180.0
FORGET_S = 24 * 3600.0                 # a feed silent this long is off the tiers (taken off the station)
INCOMING_S = 300.0
TICK_S = 5.0
PANEL_MAX = 50
MANUAL_FIELDS = ("type", "location", "pin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY,              -- the adapter's source id
  label TEXT NOT NULL,
  rank INTEGER NOT NULL,
  state TEXT NOT NULL,                  -- ok | backing_off | failing | stale (as the station reported it)
  reason TEXT NOT NULL DEFAULT '',
  since REAL,
  at REAL NOT NULL                      -- when the station reported it
);
CREATE TABLE IF NOT EXISTS source_reports (
  source TEXT NOT NULL,                 -- the adapter's source id, or 'paging'
  ref TEXT NOT NULL,                    -- the feed's event id; paging: the monitor incident id
  rank INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  incident_id INTEGER,                  -- NULL: recorded only (closed or old when first seen)
  first_seen REAL NOT NULL,             -- when this source first had it (the station's clock)
  updated_at REAL NOT NULL,
  data TEXT NOT NULL DEFAULT '{}',      -- the report as last seen (a feed: FeedEvent.as_dict; paging: the page's fields)
  merged_into TEXT NOT NULL DEFAULT '', -- the feed merged this event into that one (its aliases): retired, ignored after
  PRIMARY KEY (source, ref)
);
CREATE INDEX IF NOT EXISTS source_reports_incident ON source_reports(incident_id);
CREATE TABLE IF NOT EXISTS manual_edits (     -- a person set this field: a feed only notes its own (#297)
  incident_id INTEGER NOT NULL,
  field TEXT NOT NULL,                  -- type | location | pin
  at REAL NOT NULL,
  operator TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (incident_id, field)
);
CREATE TABLE IF NOT EXISTS incoming (
  id INTEGER PRIMARY KEY,
  ref TEXT NOT NULL UNIQUE,             -- the transmission (radio_traffic.id)
  at REAL NOT NULL,
  kind TEXT NOT NULL,                   -- radio
  source TEXT NOT NULL DEFAULT '',      -- the channel
  text TEXT NOT NULL,
  until REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',  -- open | matched | expired
  incident_id INTEGER
);
"""

TIME_WORDS = {"dispatched": "dispatched", "responding": "responding", "on_location": "on location",
              "en_route_to": "en route to", "out_at": "out at", "in_service": "in service"}
ABBR = {"street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln", "place": "pl",
        "court": "ct", "boulevard": "blvd", "terrace": "ter", "parkway": "pkwy", "highway": "hwy", "route": "rt",
        "north": "n", "south": "s", "east": "e", "west": "w", "saint": "st", "mount": "mt", "circle": "cir"}
SUFFIXES = {"st", "ave", "rd", "dr", "ln", "pl", "ct", "blvd", "ter", "pkwy", "hwy", "cir", "way", "sq", "plz"}
DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}
NOISE = {"the", "of"}
INTERSECTION = re.compile(r"\s(?:&|and|/|at|@)\s|&|/", re.I)
UNIT_TAIL = re.compile(r"(?:\b(?:apt|apartment|unit|ste|suite|fl|floor|rm|room|bldg|building)\b|#).*$", re.I)
# A transmission announcing a page or an alarm to come (a pre-alert: never an incident)
INCOMING = re.compile(r"\b(?:incoming|stand\s*-?\s*by\s+for|standby\s+for)\b[^.?!]{0,40}?\b(?:alarm|page|tones?|call|"
                      r"assignment|box)\b|\b(?:additional|second|2nd|third|3rd|fourth|4th)\s+alarm\b|"
                      r"\b(?:about|going)\s+to\s+(?:be\s+)?(?:page|paged|tone|toned)\b", re.I)


def norm_unit(u: str) -> str:
    """'Engine 61' / 'E-61' / 'e61' -> 'E61'; 'Tower Ladder 2' -> 'TL2'; '23 91' -> '2391'."""
    parts = re.findall(r"[A-Za-z]+|\d+", u or "")
    letters = "".join(p[0].upper() if len(p) > 2 else p.upper() for p in parts if p.isalpha())
    digits = "".join(p for p in parts if p.isdigit())
    return letters + digits


def units_match(a, b) -> set[str]:
    """The units two lists have in common (normalised); a bare number matches a unit with that number."""
    na, nb = {norm_unit(x) for x in a if norm_unit(x)}, {norm_unit(x) for x in b if norm_unit(x)}
    out = na & nb
    for x in na:
        for y in nb:
            dx, dy = re.sub(r"\D", "", x), re.sub(r"\D", "", y)
            if dx and dx == dy and len(dx) >= 2 and (x.isdigit() or y.isdigit()):
                out.add(y)
    return out


def _street(s: str) -> tuple[str, frozenset, str, frozenset]:
    """'12 N Example Ave Apt 3' -> ('12', {'n'}, 'ave', {'example'}): house number, directions, suffix, name words."""
    words = [ABBR.get(w, w) for w in re.findall(r"[a-z0-9]+", s.lower())]
    num = words.pop(0) if words and re.fullmatch(r"\d+[a-z]?", words[0]) else ""
    dirs = frozenset(w for w in words if w in DIRECTIONS)
    suffix = next((w for w in reversed(words) if w in SUFFIXES), "")
    name = frozenset(w for w in words if w not in DIRECTIONS and w not in SUFFIXES and w not in NOISE)
    return num, dirs, suffix, name


def place_key(s: str):
    """('addr', number, directions, suffix, name) for a street address, ('x', {streets}) for an intersection, None for
    nothing usable. The town after a comma, a named place after "@" and an apartment are left out."""
    s = (s or "").split(",")[0]
    s = UNIT_TAIL.sub("", s.split(" @ ")[0] if " @ " in s else s).strip()
    if not s:
        return None
    parts = [p for p in INTERSECTION.split(s) if p.strip()]
    if len(parts) >= 2:
        streets = frozenset((d, x, n) for _, d, x, n in (_street(p) for p in parts) if n)
        return ("x", streets) if len(streets) >= 2 else None
    num, dirs, suffix, name = _street(s)
    return ("addr", num, dirs, suffix, name) if name else None


def _same_street(a, b) -> bool:
    (_, da, sa, na), (_, db, sb, nb) = a, b
    return na == nb and (not da or not db or da == db) and (not sa or not sb or sa == sb)


def same_place(a: str, b: str) -> bool:
    """Strict: the same house number and street ('12 Example Ave' = '12 EXAMPLE AVENUE APT 3'); never '14 Example Ave',
    'S Example Ave' for 'N Example Ave', 'Example Ave' for '12 Example Ave', or an intersection for a numbered address."""
    ka, kb = place_key(a), place_key(b)
    if ka is None or kb is None or ka[0] != kb[0]:
        return False
    if ka[0] == "x":
        return len(ka[1]) == len(kb[1]) and all(any(_same_street((None, *x), (None, *y)) for y in kb[1]) for x in ka[1])
    if ka[1] != kb[1]:
        return False
    return _same_street(ka[1:], kb[1:])


def _shown(v) -> str:
    """An extra's value for people: {"Fire": "26-0012"} -> "Fire 26-0012"; [a, b] -> "a, b"; None -> "(none)"."""
    if v is None or v == "" or v == [] or v == {}:
        return "(none)"
    if isinstance(v, dict):
        return "; ".join(f"{k} {_shown(x)}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(_shown(x) for x in v)
    return str(v)


def _hm(t: float | None) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S") if t else "—"


def _dur(s: float) -> str:
    s = abs(int(round(s)))
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


def _ref(inc: dict) -> str:
    return f"#{inc['number'] or inc['id']}"


class Sources:
    def __init__(self, cad, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.cad, self.store, self.clock, self.log = cad, cad.store, clock, log
        self._lock = threading.RLock()
        # Is the paging decoder up? The hub sets it (the station connected); alone, always.
        self.paging_ok: Callable[[], bool] = lambda: True
        # trigger(reason): tell the station's adapters "poll now" (the app sets it: the runner here, or a job to the
        # station from the hub); None: nothing to tell
        self.trigger: Callable[[str], None] | None = None
        self._stop = threading.Event()
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    # -- health and tiers ----------------------------------------------------------------------------------------------

    def on_health(self, d: dict) -> None:
        """A "source_health" from the station: kept when newer than the one we have."""
        src = str(d.get("source") or "")
        state = str(d.get("state") or "")
        at = d.get("at")
        if not SOURCE_ID.fullmatch(src) or src == PAGING or state not in HEALTH_STATES or not isinstance(at, (int, float)):
            raise SourceError(f"bad source_health {src!r} {state!r}")
        rank = d.get("rank") if isinstance(d.get("rank"), int) and 1 <= d.get("rank") <= 99 else 1
        label = str(d.get("label") or src)[:60]
        before = self.acting()
        with self.store.tx() as db:
            db.execute("INSERT INTO source_health (source, label, rank, state, reason, since, at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(source) DO UPDATE SET label = excluded.label, rank = excluded.rank, state = excluded.state, "
                       "reason = excluded.reason, since = excluded.since, at = excluded.at WHERE excluded.at >= source_health.at",
                       (src, label, rank, state, str(d.get("reason") or "")[:200],
                        d.get("since") if isinstance(d.get("since"), (int, float)) else None, float(at)))
        after = self.acting()
        if (before or {}).get("source") != (after or {}).get("source"):
            self.log(f"incident sources: acting primary {(before or {}).get('label', 'none')} -> {(after or {}).get('label', 'none')}")

    def tiers(self, now: float | None = None) -> list[dict]:
        """Every tier, best first: {source, label, rank, state, reason, at, healthy, acting}."""
        now = self.clock() if now is None else now
        out = []
        for r in self.store.q("SELECT * FROM source_health WHERE at >= ? ORDER BY rank, source", (now - FORGET_S,)):
            state, reason = r["state"], r["reason"]
            if now - r["at"] > REPORT_STALE_S:
                state, reason = "stale", f"no report since {_hm(r['at'])}"
            out.append({"source": r["source"], "label": r["label"], "rank": r["rank"], "state": state, "reason": reason,
                        "at": r["at"], "healthy": state == "ok"})
        try:
            pok = bool(self.paging_ok())
        except Exception:  # noqa: BLE001
            pok = False
        out.append({"source": PAGING, "label": PAGING_LABEL, "rank": PAGING_RANK, "state": "ok" if pok else "failing",
                    "reason": "" if pok else "station offline", "at": now, "healthy": pok})
        acting = next((t for t in out if t["healthy"]), None)
        for t in out:
            t["acting"] = t is acting
        return out

    def acting(self, now: float | None = None) -> dict | None:
        return next((t for t in self.tiers(now) if t["acting"]), None)

    # -- reports -------------------------------------------------------------------------------------------------------

    def _report(self, source: str, ref: str) -> dict | None:
        return self.store.one("SELECT * FROM source_reports WHERE source = ? AND ref = ?", (source, ref))

    def _put(self, source: str, ref: str, rank: int, label: str, first_seen: float, data: dict,
             incident_id: int | None = None) -> None:
        now = self.clock()
        with self.store.tx() as db:
            db.execute("INSERT INTO source_reports (source, ref, rank, label, incident_id, first_seen, updated_at, data) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, ref) DO UPDATE SET rank = excluded.rank, "
                       "label = excluded.label, incident_id = COALESCE(source_reports.incident_id, excluded.incident_id), "
                       "first_seen = MIN(source_reports.first_seen, excluded.first_seen), updated_at = excluded.updated_at, "
                       "data = excluded.data",
                       (source, ref, rank, label, incident_id, first_seen, now,
                        json.dumps(data, separators=(",", ":"), default=str)))

    def _units_of(self, iid: int) -> list[str]:
        """The units on an incident now: assignments not cleared, and the units its reports name."""
        out = [r["name"] for r in self.store.q("SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                                               "WHERE a.incident_id = ? AND a.cleared_at IS NULL", (iid,))]
        for r in self.store.q("SELECT data FROM source_reports WHERE incident_id = ? AND merged_into = ''", (iid,)):
            out += list(json.loads(r["data"] or "{}").get("units") or [])
        return out

    def _has_feed(self, iid: int) -> bool:
        return self.store.one("SELECT 1 AS x FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = '' "
                              "LIMIT 1", (iid, PAGING)) is not None

    def match(self, source: str, at: float, location: str, units) -> dict | None:
        """The open incident this report is most likely about (see the module's "match"), or None. Whether it may
        attach there is the caller's (see "attach")."""
        taken = {r["incident_id"] for r in self.store.q(
            "SELECT incident_id FROM source_reports WHERE source = ? AND incident_id IS NOT NULL", (source,))}
        here = place_key(location) is not None
        best, key = None, None
        for inc in self.store.q(f"SELECT * FROM incidents WHERE created_at BETWEEN ? AND ? AND status IN "
                                f"({','.join('?' * len(OPEN))})", (at - MATCH_WINDOW_S, at + MATCH_WINDOW_S, *OPEN)):
            if inc["id"] in taken:
                continue
            common = units_match(units, self._units_of(inc["id"]))
            theirs = [x for x in (inc["address"], inc["place_address"], inc["geo_label"]) if place_key(x) is not None]
            if here and theirs:
                if not any(same_place(location, x) for x in theirs):
                    continue                                 # two places: never the same call, whatever the units
                score = 10 + len(common)
            elif common:
                score = len(common)                          # one side has no location: the units decide
            else:
                continue
            k = (-score, abs(inc["created_at"] - at))
            if key is None or k < key:
                best, key = inc, k
        return best

    def authority(self, iid: int) -> int | None:
        r = self.store.one("SELECT MIN(rank) AS r FROM source_reports WHERE incident_id = ? AND merged_into = ''", (iid,))
        return r["r"] if r else None

    def _possibly(self, new_id: int, other: dict) -> None:
        """Not attached, but it may be the same call: a note on both."""
        new = self.store.incident(new_id)
        now = self.clock()
        with self.store.tx() as db:
            self.store._event(db, new_id, "feed", f"Possibly the same as {_ref(other)}: {other['address'] or '(no location)'}",
                              at=now)
            self.store._event(db, other["id"], "feed", f"Possibly the same as {_ref(new)}: {new['address'] or '(no location)'}",
                              at=now)
        self.log(f"incident sources: event {new_id} possibly the same as {other['id']}")

    # -- manual edits ---------------------------------------------------------------------------------------------------

    def mark_manual(self, iid: int, fields, operator: str = "") -> None:
        """A person set these (type, location, pin): a feed only notes its own from now on."""
        fields = [f for f in dict.fromkeys(fields) if f in MANUAL_FIELDS]
        if not fields:
            return
        now = self.clock()
        with self.store.tx() as db:
            for f in fields:
                db.execute("INSERT INTO manual_edits (incident_id, field, at, operator) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(incident_id, field) DO UPDATE SET at = excluded.at, operator = excluded.operator",
                           (iid, f, now, operator))

    def manual(self, iid: int) -> set[str]:
        return {r["field"] for r in self.store.q("SELECT field FROM manual_edits WHERE incident_id = ?", (iid,))}

    def _best_feed(self, iid: int) -> dict | None:
        return self.store.one("SELECT * FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = '' "
                              "ORDER BY rank, first_seen LIMIT 1", (iid, PAGING))

    def _location_fields(self, iid: int, loc: str) -> dict:
        """The fields for a feed's location: the address, and a new pin only when no person set one."""
        f = {"address": loc[:200], "place": "", "place_address": "", "geo_alts": ""}
        if "pin" not in self.manual(iid):
            g = {"address": loc[:200], "lat": None, "lon": None, "geo_label": ""}
            self.cad._auto_geo(g)
            f.update(lat=g["lat"], lon=g["lon"], geo_label=g["geo_label"])
        return f

    def feed_use(self, iid: int, field: str, operator: str = "") -> dict:
        """'Use <feed>'s type / location': the best feed's value over a person's, logged as theirs."""
        from sizeup.cad.store import CadError
        if field not in ("type", "location"):
            raise CadError("field: type or location")
        r = self._best_feed(iid)
        if r is None:
            raise CadError("no CAD feed on this event")
        d = json.loads(r["data"] or "{}")
        if field == "type":
            typ, sub, note = self.cad.map_type(d.get("type") or "", r["label"])
            if not typ:
                raise CadError(f"{r['label']}'s type isn't one of ours: {d.get('type') or '(none)'}")
            f = {"type": typ, "subtype": sub, "type_note": note}
        else:
            loc = (d.get("location") or "").strip()
            if not loc:
                raise CadError(f"{r['label']} has no location")
            f = self._location_fields(iid, loc)
        inc = self.store.update_incident(iid, f, operator=operator)
        with self.store.tx() as db:
            db.execute("DELETE FROM manual_edits WHERE incident_id = ? AND field = ?", (iid, field))
            self.store._event(db, iid, "feed", f"Used {r['label']}'s {field}", operator, at=self.clock())
        return inc

    # -- a feed's event ----------------------------------------------------------------------------------------------

    def on_event(self, d: dict) -> int | None:
        """A "source_event" from the station: applied as a change, attached, created, or (closed or old) recorded only.
        The incident id (None: recorded only)."""
        ev = FeedEvent.from_dict(d.get("event") or {})
        rank = d.get("rank") if isinstance(d.get("rank"), int) and 1 <= d.get("rank") <= 99 else 1
        label = str(d.get("label") or ev.source)[:60]
        now = self.clock()
        seen = float(d.get("first_seen") or d.get("seen_at") or now)
        data = ev.as_dict()
        with self._lock:
            rep = self._report(ev.source, ev.event_id)
            if rep and rep["merged_into"]:                   # retired: the feed merged it into another event
                into = self._report(ev.source, rep["merged_into"])
                return into["incident_id"] if into else None
            prev = json.loads(rep["data"]) if rep else None
            if rep and rep["incident_id"]:
                self._put(ev.source, ev.event_id, rank, label, seen, data)
                self._apply(rep["incident_id"], prev, data, label, rank)
                self._merge(ev.source, ev.event_id, rep["incident_id"], data, label)
                return rep["incident_id"]
            at = min(data["times"].values()) if data["times"] else seen
            inc = self._alias_incident(ev.source, data)
            similar = None
            if inc is None:
                inc = self.match(ev.source, at, data["location"], data["units"])
                if inc is not None and not (inc["status"] == "queued" or inc["source"] == FEED_SOURCE):
                    inc, similar = None, inc                 # accepted and not a feed's: its own incident, noted
            if inc is not None:
                self._put(ev.source, ev.event_id, rank, label, seen, data, incident_id=inc["id"])
                self._attached(inc, ev.source, label, rank, seen)
                self._apply(inc["id"], None, data, label, rank, attach=True)
                self._merge(ev.source, ev.event_id, inc["id"], data, label)
                return inc["id"]
            self._put(ev.source, ev.event_id, rank, label, seen, data)
            disp = data["times"].get("dispatched") or (min(data["times"].values()) if data["times"] else None)
            if data["closed"] or (disp is not None and now - disp > MATCH_WINDOW_S):
                if rep is None:
                    self.log(f"incident sources: {label} event {ev.event_id} recorded only "
                             f"({'closed' if data['closed'] else 'dispatched ' + _hm(disp)})")
                return None
            iid = self._create_feed(ev.source, ev.event_id, data, label)
            if similar is not None:
                self._possibly(iid, similar)
            self._merge(ev.source, ev.event_id, iid, data, label)
            return iid

    def _create_feed(self, source: str, ref: str, data: dict, label: str) -> int:
        typ, sub, note = self.cad.map_type(data["type"], label)
        loc = data["location"].strip()
        f = {"type": typ, "subtype": sub, "type_note": note, "address": loc[:200],
             "remarks": (f"{label} event {ref}: {data['type'] or 'type not given'} at {loc or '(no location)'}"
                         + (f"; units {', '.join(data['units'])}" if data["units"] else ""))[:4000]}
        self.cad._auto_geo(f)
        via = "60 CONTROL" if "60 CONTROL" in self.cad.lists.received_codes else ""
        iid = self.store.create_incident(**f, received_via=via, source=FEED_SOURCE, source_ref=f"feed:{source}:{ref}",
                                         status="queued")
        with self.store.tx() as db:
            db.execute("UPDATE source_reports SET incident_id = ? WHERE source = ? AND ref = ?", (iid, source, ref))
        self.log(f"incident sources: {label} event {ref} -> event {iid}: {data['type'] or 'type not given'} at {loc or '(no location)'}")
        rep = self._report(source, ref)
        self._apply(iid, None, data, label, rep["rank"] if rep else 1, created=True)
        self.cad._prealert_match(self.store.incident(iid))      # the phone pre-alert and the notices it answers
        return iid

    def _alias_incident(self, source: str, data: dict) -> dict | None:
        """The open incident one of this event's aliases already has (the event it was merged from)."""
        for a in data.get("aliases") or []:
            r = self._report(source, a)
            if r and r["incident_id"]:
                inc = self.store.incident(r["incident_id"])
                if inc["status"] in OPEN:
                    return inc
        return None

    def _merge(self, source: str, ref: str, iid: int, data: dict, label: str) -> None:
        """The feed merged its aliases into this event (see the module's "merge")."""
        for a in data.get("aliases") or []:
            r = self._report(source, a)
            if r is not None and r["merged_into"]:
                continue
            old = r["incident_id"] if r else None
            now = self.clock()
            dismiss = False
            if old and old != iid:
                o = self.store.incident(old)
                dismiss = o["status"] == "queued" and o["source"] == FEED_SOURCE and o["source_ref"] == f"feed:{source}:{a}"
            with self.store.tx() as db:
                if r is None:                                # never seen here: on record, so it is ignored if it comes
                    db.execute("INSERT INTO source_reports (source, ref, rank, label, incident_id, first_seen, updated_at, "
                               "data, merged_into) VALUES (?, ?, ?, ?, NULL, ?, ?, '{}', ?)",
                               (source, a, self._report(source, ref)["rank"], label, now, now, ref))
                else:                                        # its report moves only with a dismissed (or no) incident
                    move = old is None or dismiss
                    db.execute("UPDATE source_reports SET merged_into = ?, incident_id = ? WHERE source = ? AND ref = ?",
                               (ref, iid if move else old, source, a))
                if old and old != iid:
                    self.store._event(db, iid, "feed", f"{label}: event {a} merged into {ref} (event #{old})", at=now)
                    self.store._event(db, old, "feed", f"{label}: event {a} merged into {ref} (event #{iid})"
                                      + ("" if dismiss else ": kept, check it"), at=now)
            self.log(f"incident sources: MERGE {label} event {a} -> {ref} (event {old or 'none'} -> {iid})"
                     + (", dismissed" if dismiss else ""))
            if dismiss:
                self.store.dismiss(old, operator=label, reason=f"merged into {label} event {ref}")

    def _attached(self, inc: dict, source: str, label: str, rank: int, seen: float) -> None:
        """A note on the incident: this source has it too, how long after (or before) the one that made it."""
        first = self.store.one("SELECT source, label, MIN(first_seen) AS t FROM source_reports WHERE incident_id = ? "
                               "AND source != ?", (inc["id"], source))
        rel = ""
        if first and first["t"] is not None:
            gap = seen - first["t"]
            rel = f" ({_dur(gap)} {'after' if gap >= 0 else 'before'} {first['label'] or first['source']})"
        auth = self.authority(inc["id"])
        lead = " · now authoritative" if auth == rank and rank != PAGING_RANK else ""
        with self.store.tx() as db:
            self.store._event(db, inc["id"], "feed", f"{label}: attached{rel}{lead}", at=self.clock())

    def _apply(self, iid: int, prev: dict | None, new: dict, label: str, rank: int, attach: bool = False,
               created: bool = False) -> None:
        """A feed's event onto its incident: its fields where it is the authority and no person set them (see
        "authority"), and a timeline entry per change."""
        inc = self.store.incident(iid)
        auth = rank <= (self.authority(iid) or rank)
        hand = set() if inc["status"] == "queued" else self.manual(iid)
        old = prev or {"type": "", "location": "", "units": [], "times": {}, "unit_times": {}, "comments": [], "closed": False}
        notes: list[tuple[float, str]] = []
        now = self.clock()
        pre = label if auth else f"{label} (not authoritative)"
        fields: dict = {}
        if new["type"] and (new["type"] != old["type"] or attach):
            typ, sub, note = self.cad.map_type(new["type"], label)
            differs = bool(typ) and (typ, sub) != (inc["type"], inc["subtype"])
            if auth and differs and "type" in hand:
                notes.append((now, f"{label}: type {new['type']} (the type set by hand kept: Use {label}'s type)"))
            elif not created:
                notes.append((now, f"{pre}: type {new['type']}"))
            if auth and differs and "type" not in hand:
                fields.update(type=typ, subtype=sub, type_note=note)
        loc = new["location"].strip()
        if loc and (loc != old["location"].strip() or attach):
            differs = not created and not same_place(loc, inc["address"])
            if auth and differs and "location" in hand:
                notes.append((now, f"{label}: location {loc} (the location set by hand kept: Use {label}'s location)"))
            elif not created:
                notes.append((now, f"{pre}: location {loc}"))
            if auth and differs and "location" not in hand:
                fields.update(self._location_fields(iid, loc))
        if new["units"] != list(old["units"]):
            added = [u for u in new["units"] if u not in old["units"]]
            gone = [u for u in old["units"] if u not in new["units"]]
            notes.append((now, f"{pre}: units {', '.join(new['units']) or '(none)'}"
                          + (f" (+{', '.join(added)})" if added and old["units"] else "")
                          + (f" (-{', '.join(gone)})" if gone else "")))
        for k in TIME_KEYS:
            t = new["times"].get(k)
            if t is not None and t != old["times"].get(k):
                notes.append((t, f"{pre}: {TIME_WORDS[k]} {_hm(t)}"))
        for u, ts in new["unit_times"].items():
            was = old["unit_times"].get(u) or {}
            for k in TIME_KEYS:
                t = ts.get(k)
                if t is not None and t != was.get(k):
                    notes.append((t, f"{pre}: {u} {TIME_WORDS[k]} {_hm(t)}"))
        had = {(float(a), b) for a, b in old["comments"]}
        for a, text in new["comments"]:
            if (float(a), text) not in had:
                notes.append((float(a), f"{label} comment: {text}"))
        was_x, now_x = old.get("extra") or {}, new.get("extra") or {}
        for k in sorted(set(was_x) | set(now_x)):
            if was_x.get(k) != now_x.get(k):
                notes.append((now, f"{pre}: {k.replace('_', ' ')} {_shown(now_x.get(k))}"))
        if bool(new["closed"]) != bool(old["closed"]):
            notes.append((now, f"{pre}: {'closed' if new['closed'] else 'reopened'} in the feed"))
        if fields and inc["status"] in OPEN:
            try:
                self.store.update_incident(iid, fields, operator=label, log=False)
            except Exception as e:  # noqa: BLE001 - a field the lists refuse: the timeline still says it
                notes.append((now, f"{label}: not applied ({type(e).__name__}: {e})"))
        if notes:
            with self.store.tx() as db:
                for at, text in notes:
                    self.store._event(db, iid, "feed", text[:2000], at=at)

    # -- the paging decoder -------------------------------------------------------------------------------------------

    def paging_incident(self, ref: str) -> int | None:
        r = self._report(PAGING, ref)
        return r["incident_id"] if r else None

    def page(self, ref: str, at: float, location: str, units, data: dict) -> tuple[str, dict | int | None]:
        """A page for us that made no incident yet: ("attached", id) to the incident a feed has for it, else
        ("create", the incident it may be the same as, or None): the caller makes it now, then created_by_page()."""
        units = list(units or [])
        rec = dict(data, location=location, units=units, at=at)
        with self._lock:
            rep = self._report(PAGING, ref)
            if rep and rep["incident_id"]:
                return "attached", rep["incident_id"]
            inc = self.match(PAGING, at, location, units)
            if inc is not None and self._has_feed(inc["id"]):
                self._put(PAGING, ref, PAGING_RANK, PAGING_LABEL, at, rec, incident_id=inc["id"])
                self._page_attached(inc, rec)
                return "attached", inc["id"]
            self._put(PAGING, ref, PAGING_RANK, PAGING_LABEL, at, rec)
            return "create", inc

    def created_by_page(self, ref: str, iid: int, similar: dict | None = None) -> None:
        with self.store.tx() as db:
            db.execute("UPDATE source_reports SET incident_id = ? WHERE source = ? AND ref = ?", (iid, PAGING, ref))
        if similar is not None:
            self._possibly(iid, similar)

    def _page_attached(self, inc: dict, rec: dict) -> None:
        self.cad.comms.link_pages(inc["id"], rec.get("pages") or [])
        first = self.store.one("SELECT label, MIN(first_seen) AS t FROM source_reports WHERE incident_id = ? AND source != ?",
                               (inc["id"], PAGING))
        rel = ""
        if first and first["t"] is not None:
            gap = rec["at"] - first["t"]
            rel = f" ({_dur(gap)} {'after' if gap >= 0 else 'before'} {first['label']})"
        with self.store.tx() as db:
            self.store._event(db, inc["id"], "feed", f"Page {_hm(rec['at'])} attached{rel}: "
                              + "; ".join(x for x in (rec.get("type_heard", ""), rec.get("location", "")) if x), at=self.clock())

    # -- the ticker ----------------------------------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> int:
        """Open incoming notices past their time: expired. How many."""
        now = self.clock() if now is None else now
        with self._lock:
            if not self.store.q("SELECT 1 FROM incoming WHERE status = 'open' AND until < ? LIMIT 1", (now,)):
                return 0
            with self.store.tx() as db:
                return db.execute("UPDATE incoming SET status = 'expired' WHERE status = 'open' AND until < ?", (now,)).rowcount

    def run(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the ticker never dies
                self.log(f"incident sources: {type(e).__name__}: {e}")

    def start(self) -> Sources:
        threading.Thread(target=self.run, name="incident-sources", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def poke(self, reason: str) -> None:
        """Paging tones, a radio pre-alert, the phone ring: the adapters poll now (never raises)."""
        if self.trigger is None:
            return
        try:
            self.trigger(reason)
        except Exception as e:  # noqa: BLE001
            self.log(f"incident sources: poll now ({reason}): {type(e).__name__}: {e}")

    def decorate(self, incs: list[dict]) -> list[dict]:
        """Each incident a feed has: incident["feed"] = {label, event_id, jurisdiction, run_numbers, group_units, use}
        from its best-ranked feed (None without one); use: the fields a person set that the feed says otherwise
        ({"type": "...", "location": "..."}: the incident view's "Use <feed>'s <field>")."""
        ids = [i["id"] for i in incs]
        best: dict[int, dict] = {}
        hand: dict[int, set] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            for r in self.store.q(f"SELECT incident_id, source, ref, rank, label, data FROM source_reports WHERE source != ? "
                                  f"AND merged_into = '' AND incident_id IN ({marks}) ORDER BY rank, first_seen", (PAGING, *part)):
                best.setdefault(r["incident_id"], r)
            for r in self.store.q(f"SELECT incident_id, field FROM manual_edits WHERE incident_id IN ({marks})", part):
                hand.setdefault(r["incident_id"], set()).add(r["field"])
        for inc in incs:
            r = best.get(inc["id"])
            if r is None:
                inc["feed"] = None
                continue
            d = json.loads(r["data"] or "{}")
            x = d.get("extra") or {}
            use = {}
            h = hand.get(inc["id"], set())
            if "type" in h and d.get("type"):
                typ, sub, _ = self.cad.map_type(d["type"], r["label"])
                if typ and (typ, sub) != (inc["type"], inc["subtype"]):
                    use["type"] = d["type"]
            loc = (d.get("location") or "").strip()
            if "location" in h and loc and not same_place(loc, inc["address"]):
                use["location"] = loc
            inc["feed"] = {"label": r["label"], "event_id": r["ref"], **{k: x[k] for k in EXTRA_SHOWN if k in x}, "use": use}
        return incs

    # -- pre-alerts: possible incoming ------------------------------------------------------------------------------------

    def consider_radio(self, tid: str) -> dict | None:
        """A transcript on the 60 Control feed's talkgroups (or a paging channel's voice): a possible-incoming notice
        when it names us and is dispatch opening a call to us, or announces a page or alarm to come."""
        t = self.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        if not t or not (t["text"] or "").strip():
            return None
        feed = self.cad.feed
        feed._refresh()
        if t["talkgroup"] not in feed.cfg.talkgroups and not (t["talkgroup"] is None and t["channel"] in feed.paging):
            return None
        why = feed.matcher.match(t["text"])
        if not why:
            return None
        ann = INCOMING.search(t["text"])
        if not ann:
            if not t["dispatcher"]:
                return None
            if t["call_id"] and t["turn"] is not None and self.store.one(
                    "SELECT 1 AS x FROM radio_traffic WHERE call_id = ? AND turn < ? LIMIT 1", (t["call_id"], t["turn"])):
                return None                                  # dispatch answering, not opening the call
            busy = {r["name"] for r in self.store.q("SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                                                    "JOIN incidents i ON i.id = a.incident_id WHERE a.cleared_at IS NULL "
                                                    "AND i.status IN ('pending', 'active')")}
            if why in busy:
                return None                                  # a unit already on a call: traffic about it
        said = t["text"].strip()
        text = ("" if ann else f"Calling {why}: ") + (said if len(said) <= 140 else said[:139] + "…")
        with self.store.tx() as db:
            cur = db.execute("INSERT OR IGNORE INTO incoming (ref, at, kind, source, text, until) VALUES (?, ?, 'radio', ?, ?, ?)",
                             (tid, t["start"], t["channel"], text, t["start"] + INCOMING_S))
        if cur.rowcount:
            self.log(f"incident sources: possible incoming ({t['channel']}): {text}")
            self.poke("radio pre-alert")
        return self.store.one("SELECT * FROM incoming WHERE ref = ?", (tid,))

    def created(self, inc: dict) -> None:
        """An incident was made (any way): the open notices before it are answered."""
        with self.store.tx() as db:
            db.execute("UPDATE incoming SET status = 'matched', incident_id = ? WHERE status = 'open' AND at >= ? AND at <= ?",
                       (inc["id"], inc["created_at"] - INCOMING_S, inc["created_at"] + 5))

    def incoming(self, now: float | None = None) -> list[dict]:
        """The possible-incoming notices now, newest first (radio pre-alerts)."""
        now = self.clock() if now is None else now
        return [{"kind": "radio", "id": f"r{r['id']}", "at": r["at"], "source": r["source"], "text": r["text"]}
                for r in self.store.q("SELECT * FROM incoming WHERE status = 'open' AND until >= ? ORDER BY at DESC", (now,))]

    # -- what the console and Settings > Health show --------------------------------------------------------------------

    def state(self, now: float | None = None) -> dict:
        """The CAD state push: the tiers in short, the acting primary, the possible-incoming notices."""
        now = self.clock() if now is None else now
        tiers = self.tiers(now)
        acting = next((t for t in tiers if t["acting"]), None)
        return {"tiers": [{k: t[k] for k in ("source", "label", "rank", "state", "reason", "acting")} for t in tiers],
                "acting": acting["label"] if acting else "", "feeds": sum(1 for t in tiers if t["source"] != PAGING),
                "incoming": self.incoming(now)}

    def panel(self, now: float | None = None) -> dict:
        """Settings > Health: the tiers, and per incident each source's first-seen time and its lead over paging."""
        now = self.clock() if now is None else now
        rows = self.store.q("SELECT r.incident_id, r.source, r.label, MIN(r.first_seen) AS first, i.number, i.address, "
                            "i.created_at FROM source_reports r JOIN incidents i ON i.id = r.incident_id "
                            "GROUP BY r.incident_id, r.source ORDER BY i.created_at DESC")
        by: dict[int, dict] = {}
        for r in rows:
            e = by.setdefault(r["incident_id"], {"id": r["incident_id"], "number": r["number"], "address": r["address"],
                                                 "created_at": r["created_at"], "seen": []})
            e["seen"].append({"source": r["source"], "label": r["label"], "first_seen": r["first"]})
        leads: dict[str, list[float]] = {}
        for e in by.values():
            e["seen"].sort(key=lambda s: s["first_seen"])
            page = next((s["first_seen"] for s in e["seen"] if s["source"] == PAGING), None)
            for s in e["seen"]:
                s["lead_s"] = None if page is None or s["source"] == PAGING else round(page - s["first_seen"], 1)
                if s["lead_s"] is not None:
                    leads.setdefault(s["label"], []).append(s["lead_s"])
        incs = list(by.values())[:PANEL_MAX]
        return {"tiers": self.tiers(now), "stale_s": REPORT_STALE_S, "incidents": incs,
                "lead": [{"label": k, "n": len(v), "median_s": round(median(v), 1), "min_s": min(v), "max_s": max(v)}
                         for k, v in sorted(leads.items())]}
