"""Incident sources, ranked (#297): which source may make an incident, which one's fields win, and what each one saw first.
On the CAD of record (the hub, or all-in-one). The station's adapters: sizeup/sources/ (the interface) and
sizeup/sources/runner.py (they come here as "source_event" / "source_health" through the station's outbox).

    tiers      every external CAD feed that reported its health (rank 1 = the primary, 2, 3 ...), then the paging decoder
               (PAGING_RANK: below every feed). A feed is healthy while its last report says "ok" and is under
               REPORT_STALE_S old (older: "stale"); paging is healthy while the station is connected (paging_ok).
               The acting primary is the first healthy tier: when the primary isn't healthy the next one is promoted
               at once, and demoted again when the primary is back (the console's pill and Settings > Health say which)
    create     a new report (a feed's event, a page for us) first ATTACHES to an incident it matches (match()). If
               none: the acting primary, or any tier with no healthy tier above it, creates the incident at once; a
               lower tier's report is HELD for HOLD_S, waiting for the healthy tier above to report it. Held, it is
               a "possible incoming" notice. At HOLD_S (or as soon as no healthy tier is above it any more: the
               primary failed meanwhile) it attaches if it matches by then, else it creates the incident
    match      an incident created within MATCH_WINDOW_S of the report (a feed's dispatched time, else when first
               seen; a page's start), not dismissed, with no report of the same source already on it, and with a
               unit in common (normalised: "Engine 61" = "E61") or the same place (house number and street name);
               the most in common first, then the nearest in time. Never a second incident for one call
    authority  the incident's fields follow its best-ranked report: a higher tier attaching to an incident a lower one
               made takes over its type and location (a timeline entry each); a lower tier's changes are only noted
    timeline   every change a feed reports is an entry on the incident (kind "feed"): the type, location, units, each
               time (dispatched, responding, on location, en route to, out at, in service; per unit when it says),
               each new comment at its own time, closed or reopened in the feed
    measure    each report's first-seen time (source_reports.first_seen); per incident and source the earliest, and
               the lead over paging (paging's first seen less the source's): Settings > Health
    merge      a feed's event naming other ids of the same source in its aliases (the feed merged them into it): each
               alias's report is retired onto this event (merged_into), with a timeline entry on both incidents; the
               alias's own incident, if another one still queued, is dismissed as merged (an accepted one gets the note)
    extra      a feed's adapter-specific fields: their changes on the timeline; jurisdiction, run numbers and each
               group's units shown on the incident (decorate(): incident["feed"])
    trigger    paging tones, a radio pre-alert, the phone ring: poke() tells the adapters to poll now
    incoming   pre-alerts never make an incident: a radio transmission on the 60 Control feed's talkgroups that names
               us and is dispatch opening a call to us or announces one ("incoming additional alarm", "stand by for a
               page") is a "possible incoming" notice for INCOMING_S (with the held reports and the phone pre-alert)
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
HOLD_S = 60.0
MATCH_WINDOW_S = 20 * 60.0
REPORT_STALE_S = 180.0
FORGET_S = 24 * 3600.0                 # a feed silent this long is off the tiers (taken off the station)
INCOMING_S = 300.0
TICK_S = 5.0
PANEL_MAX = 50

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
  incident_id INTEGER,                  -- NULL while held
  first_seen REAL NOT NULL,             -- when this source first had it (the station's clock)
  updated_at REAL NOT NULL,
  hold_until REAL,                      -- held: attached or created by then
  data TEXT NOT NULL DEFAULT '{}',      -- the report as last seen (a feed: FeedEvent.as_dict; paging: the page's fields)
  merged_into TEXT NOT NULL DEFAULT '', -- the feed merged this event into that one (its aliases): retired, ignored after
  PRIMARY KEY (source, ref)
);
CREATE INDEX IF NOT EXISTS source_reports_incident ON source_reports(incident_id);
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
GENERIC = {"st", "ave", "rd", "dr", "ln", "pl", "ct", "blvd", "ter", "pkwy", "hwy", "cir", "n", "s", "e", "w", "the",
           "of", "and", "at", "cross", "corner", "apt", "unit", "fl", "floor"}
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


def _place_key(s: str) -> tuple[str, set[str]]:
    words = [ABBR.get(w, w) for w in re.findall(r"[a-z0-9]+", (s or "").lower().split("@")[0])]
    num = words[0] if words and words[0].isdigit() else ""
    return num, {w for w in words if not w.isdigit() and w not in GENERIC}


def same_place(a: str, b: str) -> bool:
    """The same house number (when both have one) on a street sharing its name words: '12 Example Ave' = '12 EXAMPLE
    AVENUE'; '12 Example Ave' != '14 Example Ave'."""
    (na, sa), (nb, sb) = _place_key(a), _place_key(b)
    if not sa or not sb or not (sa & sb):
        return False
    if na and nb:
        return na == nb
    return sa <= sb or sb <= sa


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


class Sources:
    def __init__(self, cad, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.cad, self.store, self.clock, self.log = cad, cad.store, clock, log
        self._lock = threading.RLock()
        self.hold_s = HOLD_S
        # Is the paging decoder up? The hub sets it (the station connected); alone, always.
        self.paging_ok: Callable[[], bool] = lambda: True
        # Cad: create_page(data) -> incident id, for a held page whose time came (Cad._create_from_page)
        self.create_page: Callable[[dict], int | None] | None = None
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
        self._changed(before)

    def _changed(self, before: dict | None) -> None:
        after = self.acting()
        if (before or {}).get("source") != (after or {}).get("source"):
            self.log(f"incident sources: acting primary {(before or {}).get('label', 'none')} -> {(after or {}).get('label', 'none')}")
            self.tick()                       # a promotion: what was held for the tier that failed goes now

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

    def _above(self, rank: int, now: float) -> list[dict]:
        """The healthy tiers ranked above this one."""
        return [t for t in self.tiers(now) if t["healthy"] and t["rank"] < rank]

    # -- reports -------------------------------------------------------------------------------------------------------

    def _report(self, source: str, ref: str) -> dict | None:
        return self.store.one("SELECT * FROM source_reports WHERE source = ? AND ref = ?", (source, ref))

    def _put(self, source: str, ref: str, rank: int, label: str, first_seen: float, data: dict,
             incident_id: int | None = None, hold_until: float | None = None) -> None:
        now = self.clock()
        with self.store.tx() as db:
            db.execute("INSERT INTO source_reports (source, ref, rank, label, incident_id, first_seen, updated_at, hold_until, data) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, ref) DO UPDATE SET rank = excluded.rank, "
                       "label = excluded.label, incident_id = COALESCE(source_reports.incident_id, excluded.incident_id), "
                       "first_seen = MIN(source_reports.first_seen, excluded.first_seen), updated_at = excluded.updated_at, "
                       "hold_until = CASE WHEN COALESCE(source_reports.incident_id, excluded.incident_id) IS NULL "
                       "THEN COALESCE(source_reports.hold_until, excluded.hold_until) END, data = excluded.data",
                       (source, ref, rank, label, incident_id, first_seen, now, hold_until,
                        json.dumps(data, separators=(",", ":"), default=str)))

    def _units_of(self, iid: int) -> list[str]:
        out = [r["name"] for r in self.store.q("SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                                               "WHERE a.incident_id = ?", (iid,))]
        for r in self.store.q("SELECT data FROM source_reports WHERE incident_id = ?", (iid,)):
            out += list(json.loads(r["data"] or "{}").get("units") or [])
        return out

    def match(self, source: str, at: float, location: str, units) -> dict | None:
        """The incident this report is about (see the module's "match"), or None."""
        taken = {r["incident_id"] for r in self.store.q(
            "SELECT incident_id FROM source_reports WHERE source = ? AND incident_id IS NOT NULL", (source,))}
        best, key = None, None
        for inc in self.store.q("SELECT * FROM incidents WHERE created_at BETWEEN ? AND ? AND status != 'dismissed'",
                                (at - MATCH_WINDOW_S, at + MATCH_WINDOW_S)):
            if inc["id"] in taken:
                continue
            common = units_match(units, self._units_of(inc["id"]))
            place = bool(location) and any(same_place(location, x) for x in (inc["address"], inc["place_address"],
                                                                             inc["geo_label"]) if x)
            if not common and not place:
                continue
            k = (-(len(common) + (2 if place else 0)), abs(inc["created_at"] - at))
            if key is None or k < key:
                best, key = inc, k
        return best

    def authority(self, iid: int) -> int | None:
        r = self.store.one("SELECT MIN(rank) AS r FROM source_reports WHERE incident_id = ?", (iid,))
        return r["r"] if r else None

    # -- a feed's event ----------------------------------------------------------------------------------------------

    def on_event(self, d: dict) -> int | None:
        """A "source_event" from the station: attached, created, held or applied as a change. The incident id (None:
        held)."""
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
            inc = self._alias_incident(ev.source, data) or self.match(ev.source, at, data["location"], data["units"])
            if inc is not None:
                self._put(ev.source, ev.event_id, rank, label, seen, data, incident_id=inc["id"])
                self._attached(inc, ev.source, label, rank, seen)
                self._apply(inc["id"], None, data, label, rank, attach=True)
                self._merge(ev.source, ev.event_id, inc["id"], data, label)
                return inc["id"]
            if self._above(rank, now):
                held = rep is None or rep["hold_until"] is None
                self._put(ev.source, ev.event_id, rank, label, seen, data, hold_until=now + self.hold_s)
                if held:
                    self.log(f"incident sources: {label} event {ev.event_id} held {self.hold_s:g} s for "
                             f"{self._above(rank, now)[0]['label']}")
                self.store.rev += 1
                return None
            self._put(ev.source, ev.event_id, rank, label, seen, data)
            iid = self._create_feed(ev.source, ev.event_id, data, label)
            self._merge(ev.source, ev.event_id, iid, data, label)
            self._settle()
            return iid

    def _alias_incident(self, source: str, data: dict) -> dict | None:
        """The incident one of this event's aliases already has (the event it was merged from)."""
        for a in data.get("aliases") or []:
            r = self._report(source, a)
            if r and r["incident_id"]:
                return self.store.incident(r["incident_id"])
        return None

    def _merge(self, source: str, ref: str, iid: int, data: dict, label: str) -> None:
        """The feed merged its aliases into this event: each retired onto it (see the module's "merge")."""
        for a in data.get("aliases") or []:
            r = self._report(source, a)
            if r is not None and r["merged_into"]:
                continue
            old = r["incident_id"] if r else None
            now = self.clock()
            with self.store.tx() as db:
                if r is None:                                # never seen here: on record, so it is ignored if it comes
                    db.execute("INSERT INTO source_reports (source, ref, rank, label, incident_id, first_seen, updated_at, "
                               "data, merged_into) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?)",
                               (source, a, self._rank_of(source, ref), label, iid, now, now, ref))
                else:
                    db.execute("UPDATE source_reports SET merged_into = ?, incident_id = ?, hold_until = NULL "
                               "WHERE source = ? AND ref = ?", (ref, iid, source, a))
                if old and old != iid:
                    self.store._event(db, iid, "feed", f"{label}: event {a} merged into {ref} (event #{old})", at=now)
                    self.store._event(db, old, "feed", f"{label}: event {a} merged into {ref} (event #{iid})", at=now)
            if r is not None:
                self.log(f"incident sources: {label} event {a} merged into {ref}")
            if old and old != iid and self.store.incident(old)["status"] == "queued":
                self.store.dismiss(old, operator=label, reason=f"merged into {label} event {ref}")

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
            db.execute("UPDATE source_reports SET incident_id = ?, hold_until = NULL WHERE source = ? AND ref = ?",
                       (iid, source, ref))
        self.log(f"incident sources: {label} event {ref} -> event {iid}: {data['type'] or 'type not given'} at {loc or '(no location)'}")
        self._apply(iid, None, data, label, self._rank_of(source, ref), created=True)
        self.cad._prealert_match(self.store.incident(iid))      # the phone pre-alert and the notices it answers
        return iid

    def _rank_of(self, source: str, ref: str) -> int:
        r = self._report(source, ref)
        return r["rank"] if r else 1

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
        """A feed's event onto its incident: its fields when it is the authority, and a timeline entry per change."""
        inc = self.store.incident(iid)
        auth = rank <= (self.authority(iid) or rank)
        old = prev or {"type": "", "location": "", "units": [], "times": {}, "unit_times": {}, "comments": [], "closed": False}
        notes: list[tuple[float, str]] = []
        now = self.clock()
        pre = label if auth else f"{label} (not authoritative)"
        fields: dict = {}
        if new["type"] and (new["type"] != old["type"] or attach):
            typ, sub, note = self.cad.map_type(new["type"], label)
            if not created:
                notes.append((now, f"{pre}: type {new['type']}"))
            if auth and typ and (typ, sub) != (inc["type"], inc["subtype"]):
                fields.update(type=typ, subtype=sub, type_note=note)
        loc = new["location"].strip()
        if loc and (loc != old["location"].strip() or attach):
            if not created:
                notes.append((now, f"{pre}: location {loc}"))
            if auth and not created and not same_place(loc, inc["address"]):    # the same place: ours stays (its pin)
                f = {"address": loc[:200], "lat": None, "lon": None, "geo_label": "", "geo_alts": "", "place": "",
                     "place_address": ""}
                self.cad._auto_geo(f)
                fields.update(f)
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

    def page(self, ref: str, at: float, location: str, units, data: dict) -> tuple[str, int | None]:
        """A page for us that made no incident yet: ("attached", id) to the one it matches, ("create", None) when
        paging may make it now (the caller does, then created_by_page()), or ("held", None) for HOLD_S while a healthy
        higher tier may still report it."""
        now = self.clock()
        units = list(units or [])
        rec = dict(data, location=location, units=units, at=at)
        with self._lock:
            rep = self._report(PAGING, ref)
            if rep and rep["incident_id"]:
                return "attached", rep["incident_id"]
            inc = self.match(PAGING, at, location, units)
            if inc is not None:
                self._put(PAGING, ref, PAGING_RANK, PAGING_LABEL, at, rec, incident_id=inc["id"])
                self._page_attached(inc, ref, rec)
                return "attached", inc["id"]
            above = self._above(PAGING_RANK, now)
            if above:
                if rep is None or rep["hold_until"] is None:
                    self.log(f"incident sources: page {ref} held {self.hold_s:g} s for {above[0]['label']}")
                if rep is not None:                          # a repeat of the page: its pages too
                    old = json.loads(rep["data"] or "{}")
                    rec["pages"] = list(dict.fromkeys([*(old.get("pages") or []), *(rec.get("pages") or [])]))
                self._put(PAGING, ref, PAGING_RANK, PAGING_LABEL, at, rec, hold_until=now + self.hold_s)
                self.store.rev += 1
                return "held", None
            self._put(PAGING, ref, PAGING_RANK, PAGING_LABEL, at, rec)
            return "create", None

    def created_by_page(self, ref: str, iid: int) -> None:
        with self.store.tx() as db:
            db.execute("UPDATE source_reports SET incident_id = ?, hold_until = NULL WHERE source = ? AND ref = ?",
                       (iid, PAGING, ref))
        self._settle()

    def _page_attached(self, inc: dict, ref: str, rec: dict) -> None:
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

    # -- held reports --------------------------------------------------------------------------------------------------

    def held(self) -> list[dict]:
        return self.store.q("SELECT * FROM source_reports WHERE incident_id IS NULL AND hold_until IS NOT NULL "
                            "AND merged_into = '' ORDER BY first_seen")

    def _settle(self) -> int:
        """Held reports that now match an incident: attached (after one was created)."""
        n = 0
        for r in self.held():
            r = self._report(r["source"], r["ref"])
            if r is None or r["incident_id"]:
                continue
            d = json.loads(r["data"] or "{}")
            at = d.get("at") or (min(d["times"].values()) if d.get("times") else r["first_seen"])
            inc = self.match(r["source"], at, d.get("location") or "", d.get("units") or [])
            if inc is not None:
                self._attach_held(r, d, inc)
                n += 1
        return n

    def _attach_held(self, r: dict, d: dict, inc: dict) -> None:
        with self.store.tx() as db:
            db.execute("UPDATE source_reports SET incident_id = ?, hold_until = NULL WHERE source = ? AND ref = ?",
                       (inc["id"], r["source"], r["ref"]))
        if r["source"] == PAGING:
            self._page_attached(inc, r["ref"], d)
        else:
            self._attached(inc, r["source"], r["label"], r["rank"], r["first_seen"])
            self._apply(inc["id"], None, d, r["label"], r["rank"], attach=True)

    def tick(self, now: float | None = None) -> int:
        """Held reports: attached if they match by now, else created once HOLD_S is up or no healthy tier is above
        them any more. Open incoming notices past their time: expired. How many held reports were settled."""
        now = self.clock() if now is None else now
        n = 0
        with self._lock:
            for r in self.held():
                r = self._report(r["source"], r["ref"])          # as it is now: one before it may have settled it
                if r is None or r["incident_id"] or r["hold_until"] is None:
                    continue
                d = json.loads(r["data"] or "{}")
                at = d.get("at") or (min(d["times"].values()) if d.get("times") else r["first_seen"])
                inc = self.match(r["source"], at, d.get("location") or "", d.get("units") or [])
                if inc is not None:
                    self._attach_held(r, d, inc)
                    n += 1
                    continue
                if r["hold_until"] > now and self._above(r["rank"], now):
                    continue
                with self.store.tx() as db:
                    db.execute("UPDATE source_reports SET hold_until = NULL WHERE source = ? AND ref = ?", (r["source"], r["ref"]))
                why = "no higher tier reported it" if r["hold_until"] <= now else "promoted: no healthy tier above"
                if r["source"] == PAGING:
                    iid = self.create_page(d) if self.create_page else None
                    if iid:
                        self.log(f"incident sources: held page {r['ref']} -> event {iid} ({why})")
                        self.created_by_page(r["ref"], iid)
                else:
                    self.log(f"incident sources: held {r['label']} event {r['ref']} created ({why})")
                    self._create_feed(r["source"], r["ref"], d, r["label"])
                n += 1
            if self.store.q("SELECT 1 FROM incoming WHERE status = 'open' AND until < ? LIMIT 1", (now,)):
                with self.store.tx() as db:
                    db.execute("UPDATE incoming SET status = 'expired' WHERE status = 'open' AND until < ?", (now,))
        return n

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
        """Each incident a feed has: incident["feed"] = {label, event_id, jurisdiction, run_numbers, group_units} from
        its best-ranked feed (None without one)."""
        ids = [i["id"] for i in incs]
        best: dict[int, dict] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self.store.q(f"SELECT incident_id, source, ref, rank, label, data FROM source_reports WHERE source != ? "
                                  f"AND merged_into = '' AND incident_id IN ({','.join('?' * len(part))}) ORDER BY rank, first_seen",
                                  (PAGING, *part)):
                best.setdefault(r["incident_id"], r)
        for inc in incs:
            r = best.get(inc["id"])
            if r is None:
                inc["feed"] = None
                continue
            x = json.loads(r["data"] or "{}").get("extra") or {}
            inc["feed"] = {"label": r["label"], "event_id": r["ref"], **{k: x[k] for k in EXTRA_SHOWN if k in x}}
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
        """The possible-incoming notices now, newest first: radio pre-alerts and held reports."""
        now = self.clock() if now is None else now
        out = [{"kind": "radio", "id": f"r{r['id']}", "at": r["at"], "source": r["source"], "text": r["text"]}
               for r in self.store.q("SELECT * FROM incoming WHERE status = 'open' AND until >= ? ORDER BY at DESC", (now,))]
        for r in self.held():
            d = json.loads(r["data"] or "{}")
            what = "; ".join(x for x in (d.get("type_heard") or d.get("type") or "", d.get("location") or "",
                                         ", ".join(d.get("units") or [])) if x)
            out.append({"kind": "held", "id": f"h{r['source']}:{r['ref']}", "at": r["first_seen"], "source": r["label"],
                        "text": what or r["ref"], "until": r["hold_until"]})
        return sorted(out, key=lambda x: -x["at"])

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
        return {"tiers": self.tiers(now), "hold_s": self.hold_s, "stale_s": REPORT_STALE_S, "incidents": incs,
                "lead": [{"label": k, "n": len(v), "median_s": round(median(v), 1), "min_s": min(v), "max_s": max(v)}
                         for k, v in sorted(leads.items())]}
