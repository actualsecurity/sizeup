"""County-CAD auto-labels for the intel replay (radio intel plan, PR 7: "Evaluation"). A pre-alert is right when the
county's own CAD later had the call: a PD fire / hazmat / MVA / aided event that a county CAD incident matches within
WINDOW_S and NEAR_M is a true positive, which gives its location error in metres and its lead time for free.

A PURE READ of what cad.sqlite already stores. No request of any kind reaches the county CAD application (or anything
else): the reports the station's adapter already made (source_reports, sizeup/cad/sources.py) and the CAD incidents
they are on (incidents) are read with SELECTs only, from a connection the caller opened read-only. Of a report's data
only its times are read (`times.dispatched`, else first_seen); its type and location text are never read, printed or
returned. Nothing here writes.

    county_incidents(db, since, until)   [{t, cls, lat, lon}] one per CAD incident a county feed reported
    match(events, incidents)             each event's label: tp | near | false_place | time_only | none | unlabelled

Which reports count (a positive filter, never "anything but paging"): those of the county feeds, by their adapter's
source id: `feeds` when given (tools/intel_replay.py --feeds), else the feeds that have made incidents themselves
(incidents.source = sources.FEED_SOURCE, their source_ref "feed:<source>:<ref>"). A report merged into another
(merged_into) is not one; an incident dismissed (a duplicate, a merge, nothing) is not either.
One label per INCIDENT, not per report: an incident with several of a feed's reports (a duplicate id, two event ids for
one call) is one, at its earliest time (min of each report's dispatch time, else first_seen).
The class comes from OUR incident's (type, subtype) (data/cad_lists.json codes: TYPES), the point from OUR incident's
location, so error_m is measured against our incident's point (which may be a page's geocode or a person's edit), not
against anything of the county's.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3

from sizeup.cad.sources import FEED_SOURCE

WINDOW_S = 45 * 60                 # an event and the county's incident at most this far apart in time
NEAR_M = 2000.0                    # ... and this far apart in space to be the same call at all
FALSE_PLACE_M = 300.0              # the same call, placed more than this far off: a false place
TP_M = {"address": 150.0, "intersection": 150.0}      # where right: within this by the event's precision ...
TP_M_DEFAULT = 300.0                                   # ... (block, landmark, street: this)

# The department's own (type, subtype) per label class: "TYPE/SUBTYPE" first, else a bare "TYPE" (any subtype). A type
# with only TYPE/SUBTYPE entries (ALARM, HAZARD, MA, AIRPORT) is unlabelled for its other subtypes: a plain fire
# alarm, a wires-down hazard, an EMS standby are no evidence either way. A subtype naming an odour is hazmat.
# Overridable (tools/intel_replay.py --labels FILE: {"types": {cls: ["TYPE", "TYPE/SUBTYPE"]}, "categories": {cat: cls}}).
TYPES = {"fire": ("STRU", "MVF", "BRUSH", "INVEST", "MA/FIRE", "AIRPORT/ACFT-FIRE", "AIRPORT/ACFT-STRU"),
         "hazmat": ("HAZARD/GAS", "HAZARD/GAS-LEAK", "HAZARD/GAS-LINE", "HAZARD/HAZMAT", "HAZARD/FLUID", "ALARM/GAS",
                    "ALARM/CO-COMM", "ALARM/CO-RESD"),
         "mva": ("MVA",),
         "aided": ("ALS", "BLS", "MA/EMS", "MA/EMS-A")}
ODOR_TYPES = ("ALARM", "HAZARD")
CATEGORY_WORDS = (("mva", ("mva", "crash", "accident", "collision", "rollover")),
                  ("fire", ("fire", "smoke", "flames", "burning")),
                  ("hazmat", ("hazmat", "gas", "odor", "spill", "leak", "fumes")),
                  ("aided", ("aided", "medical", "ems", "unconscious", "overdose", "injury", "injured", "cpr")))
PAD_S = 6 * 3600.0                 # reports read this far around the window (an incident's earliest is then picked)

# The reads themselves (SELECT only). FEEDS: the feeds that made incidents of their own. QUERY: their reports, not merged
# away, on an incident not dismissed; grouped per incident in county_incidents().
FEEDS = "SELECT DISTINCT source_ref FROM incidents WHERE source = ? AND source_ref LIKE 'feed:%'"
QUERY = """
SELECT r.incident_id, r.first_seen, r.data, i.type, i.subtype, i.lat, i.lon
FROM source_reports r JOIN incidents i ON i.id = r.incident_id
WHERE r.source IN ({feeds}) AND r.merged_into = '' AND i.status != 'dismissed'
  AND r.first_seen >= ? AND r.first_seen < ?
ORDER BY r.incident_id, r.first_seen
"""


def class_of_category(category: str, overrides: dict | None = None) -> str | None:
    """An intel category's label class by its WHOLE words (split on "_", "-" and spaces: "gas_leak" is hazmat, "gasoline"
    and "fireworks" are not), the first class that has one; overrides first."""
    c = (category or "").lower()
    if overrides and c in overrides:
        return overrides[c] or None
    toks = set(re.split(r"[_\s-]+", c))
    for cls, words in CATEGORY_WORDS:
        if toks & set(words):
            return cls
    return None


def class_of_type(code: str, subtype: str = "", types: dict | None = None) -> str | None:
    """Our incident's (type, subtype) -> a label class, or None (unlabelled)."""
    t, s = (code or "").upper().strip(), (subtype or "").upper().strip()
    table = types or TYPES
    for want in (f"{t}/{s}" if s else None, t):
        if want is None:
            continue
        for cls, codes in table.items():
            if want in codes:
                return cls
    if t in ODOR_TYPES and ("ODOR" in s or "ODOUR" in s):
        return "hazmat"
    return None


def _when(first_seen: float, data: str) -> float:
    """The county's dispatch time when the report has one, else when the feed first had it."""
    try:
        t = (json.loads(data or "{}").get("times") or {}).get("dispatched")
        return float(t) if t else float(first_seen)
    except (ValueError, TypeError, AttributeError):
        return float(first_seen)


def county_feeds(db: sqlite3.Connection) -> list[str]:
    """The source ids of the feeds that made incidents of their own (incidents.source = FEED_SOURCE)."""
    out = set()
    for (ref,) in db.execute(FEEDS, (FEED_SOURCE,)):
        parts = str(ref or "").split(":", 2)
        if len(parts) == 3 and parts[1]:
            out.add(parts[1])
    return sorted(out)


def county_incidents(db: sqlite3.Connection, since: float, until: float, types: dict | None = None,
                     feeds: list[str] | None = None) -> list[dict]:
    """The county CAD's incidents in [since - WINDOW_S, until + WINDOW_S) of a label class, one per incident:
    {t, cls, lat, lon} (lat / lon None: not geocoded). A pure read (FEEDS, QUERY)."""
    feeds = [f for f in (county_feeds(db) if feeds is None else feeds) if f and f != "paging"]
    if not feeds:
        return []
    sql = QUERY.format(feeds=",".join("?" * len(feeds)))
    lo, hi = since - WINDOW_S, until + WINDOW_S
    per: dict[int, dict] = {}
    for iid, first_seen, data, code, sub, lat, lon in db.execute(sql, (*feeds, lo - PAD_S, hi + PAD_S)):
        t = _when(first_seen, data)
        cur = per.get(iid)
        if cur is None:
            per[iid] = {"t": t, "code": code, "sub": sub, "lat": lat, "lon": lon}
        elif t < cur["t"]:
            cur["t"] = t
    out = []
    for x in sorted(per.values(), key=lambda x: x["t"]):
        cls = class_of_type(x["code"], x["sub"], types)
        if cls and lo <= x["t"] < hi:
            out.append({"t": x["t"], "cls": cls, "lat": x["lat"], "lon": x["lon"]})
    return out


def metres(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, a)))


def match(events: list[dict], incidents: list[dict], categories: dict | None = None) -> list[dict]:
    """Each event ({id, category, first_at, lat?, lon?, precision?}) labelled against the county's incidents:

        unlabelled   its category has no label class (dispute, pursuit ...): not counted either way
        none         no county incident of its class within WINDOW_S and NEAR_M: a false positive (or one the county
                     never had: precision is a lower bound)
        tp           the same call, placed within TP_M of the county's point
        near         the same call, between TP_M and FALSE_PLACE_M off (an address read as the next house)
        false_place  the same call, more than FALSE_PLACE_M off
        time_only    the same call by time and class, but the event or the incident has no point (one candidate only)
        ambiguous    no point to tell, and two or more candidates of its class in the window: not counted either way
        no_time      the event has no first_at (0 or missing): never matched, not counted

    Precision's denominator is tp + near + time_only + false_place + none (replay.LABELLED).
    Returns [{id, label, cls, incident (index) | None, error_m | None, lead_s | None}]; lead_s > 0: the event came first."""
    out = []
    for ev in events:
        cls = class_of_category(ev.get("category", ""), categories)
        res = {"id": ev.get("id"), "label": "unlabelled", "cls": cls, "incident": None, "error_m": None, "lead_s": None}
        out.append(res)
        if cls is None:
            continue
        try:
            t = float(ev.get("first_at") or 0)
        except (TypeError, ValueError):
            t = 0.0
        if t <= 0:
            res["label"] = "no_time"
            continue
        cands = [(k, x) for k, x in enumerate(incidents) if x["cls"] == cls and abs(x["t"] - t) <= WINDOW_S]
        res["label"] = "none"
        lat, lon = ev.get("lat"), ev.get("lon")
        placed = [(metres(lat, lon, x["lat"], x["lon"]), k, x) for k, x in cands
                  if lat is not None and lon is not None and x["lat"] is not None and x["lon"] is not None]
        placed = [p for p in placed if p[0] <= NEAR_M]
        if placed:
            d, k, x = min(placed, key=lambda p: (p[0], abs(p[2]["t"] - t)))
            tp = TP_M.get(ev.get("precision") or "", TP_M_DEFAULT)
            res.update(incident=k, error_m=round(d, 1), lead_s=round(x["t"] - t, 1),
                       label="tp" if d <= tp else "near" if d <= FALSE_PLACE_M else "false_place")
        else:
            blind = [(k, x) for k, x in cands if lat is None or lon is None or x["lat"] is None or x["lon"] is None]
            if len(blind) == 1:
                k, x = blind[0]
                res.update(incident=k, lead_s=round(x["t"] - t, 1), label="time_only")
            elif len(blind) > 1:
                res["label"] = "ambiguous"
    return out
