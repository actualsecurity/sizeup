"""County-CAD auto-labels for the intel replay (radio intel plan, PR 7: "Evaluation"). A pre-alert is right when the
county's own CAD later had the call: a PD fire / hazmat / MVA / aided event that a county CAD incident matches within
WINDOW_S and NEAR_M is a true positive, which gives its location error in metres and its lead time for free.

A PURE READ of what cad.sqlite already stores. No request of any kind reaches the county CAD application (or anything
else): the incidents the station's adapter already reported (source_reports, sizeup/cad/sources.py) and the CAD
incidents they are on (incidents) are read, from a connection the caller opened read-only. Of a report's data only
its times are read (`times.dispatched`, else first_seen); its type and location text are never read, printed or
returned: the class comes from the incident's own department type (data/cad_lists.json codes), the point from the
incident's own geocode. Nothing here writes.

    county_incidents(db, since, until)   [{t, cls, lat, lon}] one per county report on a CAD incident
    match(events, incidents)             each event's label: tp | near | false_place | time_only | none | unlabelled
"""

from __future__ import annotations

import json
import math
import sqlite3

WINDOW_S = 45 * 60                 # an event and the county's incident at most this far apart in time
NEAR_M = 2000.0                    # ... and this far apart in space to be the same call at all
FALSE_PLACE_M = 300.0              # the same call, placed more than this far off: a false place
TP_M = {"address": 150.0, "intersection": 150.0}      # where right: within this by the event's precision ...
TP_M_DEFAULT = 300.0                                   # ... (block, landmark, street: this)

# The department's own types (data/cad_lists.json) per label class; an intel category's class by its words (first
# match). Both overridable (tools/intel_replay.py --labels FILE: {"types": {cls: [codes]}, "categories": {cat: cls}}).
TYPES = {"fire": ("STRU", "MVF", "BRUSH", "INVEST"), "hazmat": ("HAZARD",), "mva": ("MVA", "RESCUE"),
         "aided": ("ALS", "BLS")}
CATEGORY_WORDS = (("mva", ("mva", "crash", "accident", "collision", "rollover")),
                  ("fire", ("fire", "smoke", "flames", "burning")),
                  ("hazmat", ("hazmat", "gas", "odor", "spill", "leak", "fumes")),
                  ("aided", ("aided", "medical", "ems", "unconscious", "overdose", "injury", "injured", "cpr")))

# The read itself: reports by a county CAD feed (never "paging", our own decoder), not merged away, on an incident.
QUERY = """
SELECT r.first_seen, r.data, i.type, i.lat, i.lon
FROM source_reports r JOIN incidents i ON i.id = r.incident_id
WHERE r.source != 'paging' AND r.merged_into = '' AND r.first_seen >= ? AND r.first_seen < ?
ORDER BY r.first_seen
"""


def class_of_category(category: str, overrides: dict | None = None) -> str | None:
    c = (category or "").lower()
    if overrides and c in overrides:
        return overrides[c] or None
    for cls, words in CATEGORY_WORDS:
        if any(w in c for w in words):
            return cls
    return None


def class_of_type(code: str, types: dict | None = None) -> str | None:
    for cls, codes in (types or TYPES).items():
        if (code or "").upper() in codes:
            return cls
    return None


def _when(first_seen: float, data: str) -> float:
    """The county's dispatch time when the report has one, else when the feed first had it."""
    try:
        t = (json.loads(data or "{}").get("times") or {}).get("dispatched")
        return float(t) if t else float(first_seen)
    except (ValueError, TypeError, AttributeError):
        return float(first_seen)


def county_incidents(db: sqlite3.Connection, since: float, until: float, types: dict | None = None) -> list[dict]:
    """The county CAD's incidents in [since - WINDOW_S, until + WINDOW_S) of a label class: {t, cls, lat, lon}
    (lat / lon None: not geocoded). A pure read (QUERY)."""
    out = []
    for first_seen, data, code, lat, lon in db.execute(QUERY, (since - WINDOW_S, until + WINDOW_S)):
        cls = class_of_type(code, types)
        if cls:
            out.append({"t": _when(first_seen, data), "cls": cls, "lat": lat, "lon": lon})
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

    Returns [{id, label, cls, incident (index) | None, error_m | None, lead_s | None}]; lead_s > 0: the event came first."""
    out = []
    for ev in events:
        cls = class_of_category(ev.get("category", ""), categories)
        res = {"id": ev.get("id"), "label": "unlabelled", "cls": cls, "incident": None, "error_m": None, "lead_s": None}
        out.append(res)
        if cls is None:
            continue
        t = float(ev.get("first_at") or 0)
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
        elif len(cands) == 1 and (lat is None or cands[0][1]["lat"] is None):
            k, x = cands[0]
            res.update(incident=k, lead_s=round(x["t"] - t, 1), label="time_only")
    return out
