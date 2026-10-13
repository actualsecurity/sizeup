#!/usr/bin/env python3
"""Build the ``places`` table of data/gazetteer.sqlite: named places, highway mile markers and exits (#67).

What the CAD's place search (sizeup/geo/places.py: a note's "@", the location box) offers next to the
addresses, intersections and landmarks of tools/fetch_addresses.py. Never used to read page transcripts.

Sources (each row keeps its own: ``source`` + ``ref``; each source with its fetch date in meta 'sources'):

* NYSDOT Roadway Inventory, Reference Marker layer (gisportalny.dot.ny.gov, public NYSDOT GIS service):
  the small green panels every 0.1 mile, with the route's milepoint (MEASURE: the distance along the
  route from where it enters the county, NYSDOT's linear referencing). Two uses (#83):
  - I-287 and I-684 (RM_ROUTES): the panel itself is the marker, its last three digits the posted
    tenths of a mile ("287I 8701 1100" = I-287 MM 10.0): on these two the panels count on through the
    county and 60 Control's I-287 list agrees with them. The route id's last digit is the direction
    (1 primary, 2/3 reverse).
  - The Hutchinson River Pkwy (907W), US-1, NY-120 and NY-120A (LRS_ROUTES): the panel numbers restart
    at every segment (the Hutch's reach 2.4 at I-287 where 60 Control says 15.6-16.0), so the markers are
    made from the milepoints instead: tenth-mile points interpolated between the panels, shifted by the
    offset fitted to 60 Control's own mileage (SIXTY_CONTROL, the HRP NB list) where we have it, and
    otherwise NYSDOT's county mileage as it is, marked unverified. The Hutch's offset is ~4.55 (the Bronx
    part of the parkway: 60 Control counts from the Bruckner interchange, NYSDOT from the county line).
    60 Control's landmark names (bridges, town lines, "I-287 ends") become aliases of the matching marker.
* NYS Thruway Authority mileposts, tenth-mile (services2.arcgis.com, the Thruway's public GIS
  service): I-95, the New England Thruway (posted miles 0-15 from the Bronx line to Connecticut).
* OpenStreetMap via Overpass (ODbL 1.0, (c) OpenStreetMap contributors): motorway junctions (exits)
  of I-95, I-287, I-684, the Hutch and the Merritt Pkwy, with the side of the road; and named places
  (parks, schools, colleges, worship, hospitals, nursing homes, shopping centres and stores, marinas,
  rail stations, hotels, neighbourhoods, ...) in the covered municipalities, skipping any already a
  landmark (same name within 300 m). Hospitals also a little beyond (where patients go).

Not here (yet): Connecticut DOT mile markers for I-95 / the Merritt in Greenwich (no public point layer
found); OSM has no mile markers mapped here.

Run:  .venv/bin/python tools/fetch_places.py [--db data/gazetteer.sqlite] [--cache DIR]
      (tools/fetch_addresses.py runs it too, after a full build). Only this table and its meta change.
If one source fails, the rows of that source from the previous build are kept and the run says so.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import json
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent))

import fetch_addresses as fa  # noqa: E402

from sizeup.geo.gazetteer import PLACES_SCHEMA  # noqa: E402
from sizeup.geo.places import ROUTES, norm_phrase  # noqa: E402

NYSDOT_RM_URL = "https://gisportalny.dot.ny.gov/hostingny/rest/services/Roadways/Roadway/FeatureServer/0/query"
THRUWAY_MP_URL = ("https://services2.arcgis.com/gubH6kG9JCAsMX2M/arcgis/rest/services/"
                  "NY_State_Thruway_Mileposts/FeatureServer/1/query")
# NYSDOT reference route (the panel's first four characters) -> our highway name: the panels are the markers
RM_ROUTES = {"287I": "I-287", "684I": "I-684"}
# ... and the roads whose markers are made from NYSDOT's milepoints (#83: their panel numbers restart)
LRS_ROUTES = {"907W": "Hutch", "1": "US-1", "120": "NY-120", "120A": "NY-120A"}
LRS_MAX_GAP = 0.25                    # miles between two panels beyond which no marker is interpolated
# What the marker's mileage is counted from, for the roads without a 60 Control list (the row's category)
LRS_ORIGIN = {
    "US-1": "NYSDOT mileage from the Bronx line (unverified vs 60 Control)",
    "NY-120": "NYSDOT mileage from US-1 in Rye (unverified vs 60 Control)",
    "NY-120A": "NYSDOT mileage from its south end (unverified vs 60 Control)",
}


class CalPoint(NamedTuple):
    """One line of a 60 Control commonplace list: its mileage, what it says, and where that is when we
    know (lat, lon). fit: part of the offset fit; a point with a position but fit=False is only checked."""
    mile: float
    text: str
    lat: float | None = None
    lon: float | None = None
    fit: bool = True


class Calibration(NamedTuple):
    note: str                   # what the mileage is, for the marker rows (their category)
    points: list[CalPoint]


# 60 Control's own mileage (GitHub #83: Julian's photos of the CAD commonplace lists, 2026-10-13), per
# road and side. The positions: an exit at its OSM motorway_junction node (where the ramp leaves), a
# bridge where the cross street's OSM way crosses the carriageway, the state line where the OSM boundary
# does. For the roads in LRS_ROUTES the offset between NYSDOT's milepoints and these is fitted (the mean
# over the fit points) and every placed point's residual reported; for the others (I-287: the posted
# panels) the points are only checked. The text becomes the marker's detail and, unless it is the exit
# itself (the exit row has that), an alias the "@" search finds ("Rye Brook town line", "bridge Bowman
# Ave", "I-287 ends"). The Hutch SB list is not transcribed yet: the SB side is fitted on the bridges,
# physical points it shares with NB.
SIXTY_CONTROL: dict[tuple[str, str], Calibration] = {
    ("Hutch", "NB"): Calibration("60 Control mileage (HRP NB list)", [
        CalPoint(14.5, "EXIT 14 (North St – Rt 127) – old exit 25", 40.996839, -73.728479),
        CalPoint(15.2, "south of exit 15A"),
        CalPoint(15.6, "EXIT 15A (I-287 EB – West Ave) – old exit 26E", 41.009251, -73.718896),
        CalPoint(15.8, "bridge I-287 / Westchester Ave", 41.014030, -73.716920),
        CalPoint(15.9, "EXIT 15B (Westchester Ave – I-287 WB)", 41.014657, -73.716662),
        CalPoint(16.0, "north of exit 15B"),
        CalPoint(16.1, "south of I-684 (SB only)"),
        CalPoint(16.2, "EXIT 16A (I-684)", 41.017181, -73.715625),
        CalPoint(16.4, "EXIT 16B (Purchase St – Rt 120) – old exit 27", 41.020888, -73.713559),
        CalPoint(16.6, "bridge Purchase St", 41.022290, -73.710090),
        CalPoint(17.0, "south of exit 17"),
        CalPoint(17.2, "EXIT 17 (Lincoln Ave) – old exit 28"),
        CalPoint(17.4, "bridge Lincoln Ave", 41.023360, -73.695340),
        CalPoint(17.9, "ramp to exit 18"),
        CalPoint(18.0, "EXIT 18 (N Ridge St) – old exit 29", 41.027518, -73.688176),
        CalPoint(18.1, "bridge N Ridge St", 41.028190, -73.683540),
        CalPoint(18.6, "south of Rt 120A"),
        CalPoint(18.8, "EXIT 19A (Rt 120A – King St) – old exit 30", 41.035089, -73.676473),
        CalPoint(18.9, "bridge King St"),
        CalPoint(19.0, "ramp from King St – CT"),
        # the OSM state boundary crosses the parkway 0.03 mi past the last NY panel, at 18.9 by this fit:
        # 60 Control's 19.1 is 0.2 mi further on (probably where its Merritt list starts), so not fitted
        CalPoint(19.1, "CT state line", 41.036740, -73.675500, fit=False),
    ]),
    # the bridges of the NB list: physical points both sides share. The HRP SB list is not checked yet.
    ("Hutch", "SB"): Calibration("60 Control mileage (the bridges of the HRP NB list; SB list unverified)", [
        CalPoint(15.8, "bridge I-287 / Westchester Ave", 41.014030, -73.717220),
        CalPoint(16.6, "bridge Purchase St", 41.022360, -73.710120),
        CalPoint(17.4, "bridge Lincoln Ave", 41.023430, -73.695360),
        CalPoint(18.1, "bridge N Ridge St", 41.028310, -73.683430),
    ]),
    ("I-287", "EB"): Calibration("posted panels (60 Control's I287 EB list agrees)", [   # checked, never fitted
        CalPoint(8.4, "EXIT 10 (Westchester Ave EB / Rt 120)", 41.012272, -73.702722),
        CalPoint(8.7, "bridge Westchester Ave"),
        CalPoint(9.0, "bridge Bowman Ave"),
        CalPoint(9.1, "Rye Brook town line"),
        CalPoint(9.9, "bridge Ridge St"),
        CalPoint(10.2, "bridge High St"),
        CalPoint(10.3, "EXIT 11 (to Boston Post Rd)", 40.993788, -73.678388),
        CalPoint(10.4, "bridge Boston Post Rd"),
        CalPoint(10.5, "EXIT 12 (I-95 SB)", 40.991364, -73.677072),
        CalPoint(10.6, "railroad bridge"),
        CalPoint(10.7, "Midland Ave underpass"),
        CalPoint(10.8, "I-287 ends – exit to I-95 NB"),
    ]),
}
OSM_HIGHWAYS = (                      # (OSM ref or name contains, our highway name)
    ("I 95", "I-95"), ("I 287", "I-287"), ("I 684", "I-684"), ("CT 15", "Merritt Pkwy"),
    ("Hutchinson", "Hutch"), ("Merritt", "Merritt Pkwy"), ("New England Thruway", "I-95"),
    ("Cross Westchester", "I-287"),
)
MARKER_MARGIN = 0.02                  # degrees around the covered municipalities, for the highways
HOSPITAL_BBOX = (40.84, -73.86, 41.13, -73.48)   # White Plains, Valhalla, New Rochelle, Stamford
DEDUP_M = 300.0
OVERPASS_URLS = (fa.OVERPASS_URLS[0], "https://overpass.openstreetmap.fr/api/interpreter", *fa.OVERPASS_URLS[1:])

# OSM tags -> our category; first match wins. None: not a place for the search.
AMENITY = {
    "school": "school", "kindergarten": "school", "college": "college", "university": "college",
    "place_of_worship": "worship", "hospital": "hospital", "clinic": "clinic", "doctors": "clinic",
    "dentist": "clinic", "nursing_home": "nursing home", "library": "library", "townhall": "municipal",
    "fire_station": "fire station", "police": "police", "community_centre": "community center",
    "theatre": "theater", "cinema": "theater", "arts_centre": "theater", "ferry_terminal": "ferry",
    "bus_station": "bus station", "post_office": "post office", "courthouse": "municipal",
    "fuel": "gas station", "restaurant": "food", "fast_food": "food", "cafe": "food", "bar": "food",
    "pub": "food", "ice_cream": "food", "bank": "bank", "pharmacy": "pharmacy", "veterinary": "veterinary",
    "childcare": "daycare", "marketplace": "market", "events_venue": "venue", "conference_centre": "venue",
    "car_wash": "car wash", "social_facility": "social facility", "nightclub": "food", "grave_yard": "cemetery",
    "boat_storage": "marina", "recycling": None, "parking": None,
}
LEISURE = {
    "park": "park", "common": "park", "recreation_ground": "park", "nature_reserve": "park", "garden": "park",
    "dog_park": "park", "marina": "marina", "slipway": "marina", "golf_course": "golf course",
    "sports_centre": "sports", "fitness_centre": "sports", "ice_rink": "sports", "swimming_pool": "sports",
    "stadium": "sports", "pitch": "sports", "track": "sports", "playground": "playground",
    "beach_resort": "beach", "sports_hall": "sports", "bandstand": "park", "picnic_table": None,
}
TOURISM = {"hotel": "hotel", "motel": "hotel", "guest_house": "hotel", "museum": "attraction",
           "gallery": "attraction", "attraction": "attraction", "zoo": "attraction", "theme_park": "attraction",
           "viewpoint": "park", "picnic_site": "park", "camp_site": "park"}
PLACE = {"neighbourhood": "neighborhood", "hamlet": "neighborhood", "locality": "neighborhood",
         "suburb": "neighborhood", "quarter": "neighborhood", "square": "neighborhood", "island": "island",
         "islet": "island"}
NATURAL = {"beach": "beach", "bay": "water", "cape": "water", "peninsula": "water", "wetland": "park",
           "wood": "park", "water": "water"}
MAN_MADE = {"pier": "pier", "breakwater": "pier", "lighthouse": "landmark", "water_tower": "utility",
            "wastewater_plant": "utility", "water_works": "utility", "tower": "utility", "bridge": "bridge"}
LANDUSE = {"retail": "shopping center", "commercial": "commercial", "cemetery": "cemetery",
           "religious": "worship", "recreation_ground": "park", "industrial": "commercial"}
BUILDING = {"apartments": "building", "commercial": "building", "retail": "building", "industrial": "building",
            "office": "building", "school": "school", "hospital": "hospital", "church": "worship",
            "civic": "municipal", "public": "municipal", "hotel": "hotel", "warehouse": "building",
            "dormitory": "college", "college": "college", "university": "college", "government": "municipal",
            "fire_station": "fire station", "train_station": "rail station", "residential": "building",
            "yes": "building"}
SHOP_BIG = {"mall": "shopping center", "supermarket": "supermarket", "department_store": "store",
            "doityourself": "store", "hardware": "store", "wholesale": "store", "furniture": "store",
            "electronics": "store", "car": "car dealer", "garden_centre": "store"}
# Names made only of these say what a thing is, not which one: skipped.
GENERIC = {"park", "playground", "parking", "lot", "field", "fields", "court", "courts", "tennis", "basketball",
           "baseball", "softball", "soccer", "pool", "garden", "gardens", "pond", "dock", "pier", "beach",
           "entrance", "exit", "building", "school", "church", "office", "store", "shop", "deli", "pizza",
           "laundromat", "garage", "gazebo", "shed", "barn", "house", "home", "pavilion", "bench", "station",
           "track", "rink", "dog", "run", "community", "a", "b", "c", "d", "1", "2", "3", "4"}


def osm_category(t: dict) -> str | None:
    if t.get("highway") or t.get("boundary") or t.get("route"):
        return None
    if t.get("railway") == "station" or (t.get("public_transport") == "station" and t.get("train") == "yes"):
        return "rail station"
    if t.get("emergency") == "ambulance_station":
        return "ems"
    if t.get("aeroway") in ("helipad", "aerodrome"):
        return "airport"
    a = t.get("amenity")
    if a in fa.OSM_SKIP_VALUES and a not in AMENITY:
        a = None                                   # a bench, a bin, a vending machine: not a place
    if a:
        if a == "social_facility" and t.get("social_facility") in ("nursing_home", "assisted_living"):
            return "nursing home"
        if a in AMENITY:
            return AMENITY[a]
    if t.get("healthcare") == "hospital":
        return "hospital"
    if t.get("healthcare") in ("nursing_home", "assisted_living"):
        return "nursing home"
    if t.get("healthcare"):
        return "clinic"
    for key, table in (("leisure", LEISURE), ("tourism", TOURISM), ("place", PLACE), ("natural", NATURAL),
                       ("man_made", MAN_MADE), ("landuse", LANDUSE)):
        if t.get(key) in table:
            return table[t[key]]
    if t.get("shop"):
        return SHOP_BIG.get(t["shop"], "shop")
    if t.get("office"):
        return "office"
    if t.get("historic"):
        return "historic"
    if a:
        return "amenity"
    if t.get("building") in BUILDING:
        return BUILDING[t["building"]]
    return None


# --------------------------------------------------------------------------- geometry

def dist_m(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat1 - lat2) * fa.M_PER_DEG_LAT, (lon1 - lon2) * fa.M_PER_DEG_LON)


def side(route: str, dlat: float, dlon: float) -> str:
    """The direction a carriageway heading (dlat, dlon) is signed: 'NB', 'WB', ..."""
    pos, neg, (vlat, vlon) = ROUTES[route]
    return pos if dlat * vlat + dlon * vlon * 0.76 >= 0 else neg      # 0.76: a degree of longitude is shorter


def row(kind, name, category, address, muni, lat, lon, route="", dir_="", value="", source="", ref="",
        aliases=()) -> dict:
    return {"kind": kind, "name": name, "category": category, "address": address or "", "muni": muni or "",
            "lat": round(float(lat), 6), "lon": round(float(lon), 6), "route": route, "dir": dir_,
            "value": value, "source": source, "ref": ref,
            "aliases": "|".join(sorted({a for a in aliases if a}))}


# --------------------------------------------------------------------------- highway markers

class Panel(NamedTuple):
    route: str          # our highway name
    rid: str            # NYSDOT route id; its last digit is the direction (1 primary)
    measure: float      # NYSDOT milepoint: miles along the route from where it enters the county
    panel: str          # "907W87025041"
    lat: float
    lon: float

    @property
    def seq(self) -> str:
        return self.panel[-4:]

    @property
    def ref(self) -> str:
        return f"{self.panel[:4].strip()} {self.panel[4:8]} {self.seq}"

    @property
    def primary(self) -> bool:
        return self.rid.endswith("1")


def parse_panels(features: list[dict]) -> list[Panel]:
    """The live reference markers of our highways (RM_ROUTES and LRS_ROUTES) in the layer's features."""
    out = []
    for f in features:
        a, g = f.get("attributes") or {}, f.get("geometry") or {}
        panel = a.get("Panel") or a.get("REFERENCE_MARKER_PANEL") or ""
        if a.get("TO_DATE") or "x" not in g or len(panel) < 12 or not panel[-4:].isdigit():
            continue
        key = panel[:4].strip()
        route = RM_ROUTES.get(key) or LRS_ROUTES.get(key)
        if not route or a.get("MEASURE") is None:
            continue
        out.append(Panel(route, str(a.get("ROUTE_ID") or ""), float(a["MEASURE"]), panel, g["y"], g["x"]))
    return out


def markers_from_rm(features: list[dict], muni_at: Callable[[float, float], str]) -> list[dict]:
    """NYSDOT reference markers of RM_ROUTES -> milemarker rows (the posted tenths), with the side of the
    road where it is divided."""
    by_route: dict[str, list[Panel]] = collections.defaultdict(list)
    for p in parse_panels(features):
        if p.route in RM_ROUTES.values():
            by_route[p.route].append(p)
    out = []
    for route, panels in by_route.items():
        primary = sorted((p for p in panels if p.primary), key=lambda p: p.measure)
        reverse = [p for p in panels if not p.primary]
        pos, neg, _v = ROUTES[route]
        p_dir = side(route, primary[-1].lat - primary[0].lat, primary[-1].lon - primary[0].lon) if len(primary) > 1 else pos
        r_dir = neg if p_dir == pos else pos
        divided = {p.panel for p in reverse}        # panels posted on both carriageways
        seen = set()
        for p in panels:
            d = (p_dir if p.primary else r_dir) if p.panel in divided else ""
            if (route, d, p.panel) in seen:
                continue
            seen.add((route, d, p.panel))
            mile = f"{int(p.seq[1:]) / 10:.1f}"
            name = f"{route}{' ' + d if d else ''} MM {mile}"
            out.append(row("milemarker", name, "reference marker", "", muni_at(p.lat, p.lon), p.lat, p.lon, route, d,
                           mile, "nysdot-rm", p.ref, [p.seq, norm_phrase(p.ref)]))
    return out


def panel_check(features: list[dict]) -> dict:
    """Per route id of RM_ROUTES: how far the posted tenths stray from the milepoint (they should differ by
    one constant along the road; a jump means the panel numbers restart, and the road belongs in LRS_ROUTES)."""
    by_rid: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for p in parse_panels(features):
        if p.route in RM_ROUTES.values():
            by_rid[(p.route, p.rid)].append(int(p.seq[1:]) / 10 - p.measure)
    out = {}
    for (route, rid), diffs in sorted(by_rid.items()):
        diffs.sort()
        median = diffs[len(diffs) // 2]
        out[f"{route} {rid}"] = {"panels": len(diffs), "posted_minus_milepoint": round(median, 2),
                                 "max_stray": round(max(abs(d - median) for d in diffs), 2)}
    return out


def measure_at(panels: list[Panel], lat: float, lon: float) -> tuple[float, float]:
    """The milepoint of the point of the panel line (the panels in order, joined) closest to (lat, lon),
    and how far from that line the point is (m)."""
    best = (math.inf, panels[0].measure)
    px, py = lon * fa.M_PER_DEG_LON, lat * fa.M_PER_DEG_LAT
    for a, b in zip(panels, panels[1:]):
        ax, ay, bx, by = a.lon * fa.M_PER_DEG_LON, a.lat * fa.M_PER_DEG_LAT, b.lon * fa.M_PER_DEG_LON, b.lat * fa.M_PER_DEG_LAT
        dx, dy, l2 = bx - ax, by - ay, (bx - ax) ** 2 + (by - ay) ** 2
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
        d = math.hypot(px - ax - t * dx, py - ay - t * dy)
        if d < best[0]:
            best = (d, a.measure + t * (b.measure - a.measure))
    return best[1], best[0]


def fit_offset(panels: list[Panel], points: list[CalPoint]) -> tuple[float, list[dict]]:
    """The offset (miles) to add to NYSDOT's milepoints to read like 60 Control's: the mean of
    (its mileage - the milepoint where it is) over the fit points, and each placed point's residual."""
    placed = []
    for c in points:
        if c.lat is None or c.lon is None:
            continue
        m, off_line = measure_at(panels, c.lat, c.lon)
        placed.append((c, m, off_line))
    fit = [c.mile - m for c, m, _ in placed if c.fit]
    offset = sum(fit) / len(fit) if fit else 0.0
    report = [{"mile": c.mile, "text": c.text, "fitted": round(m + offset, 2), "residual": round(c.mile - m - offset, 3),
               "fit": c.fit, "off_line_m": round(off_line)} for c, m, off_line in placed]
    return offset, report


def tenth_markers(route: str, d: str, panels: list[Panel], offset: float, note: str, muni_at) -> list[dict]:
    """Markers at every tenth of a mile of (milepoint + offset), placed between the two panels around it;
    none where the panels are more than LRS_MAX_GAP apart (a stretch without NY panels: no guessing)."""
    measures = [p.measure for p in panels]
    lo = math.ceil((measures[0] + offset) * 10 - 1e-6)
    hi = math.floor((measures[-1] + offset) * 10 + 1e-6)
    out = []
    for t in range(lo, hi + 1):
        m = t / 10 - offset
        i = min(max(bisect.bisect_right(measures, m + 1e-9) - 1, 0), len(panels) - 2)
        a, b = panels[i], panels[i + 1]
        gap = b.measure - a.measure
        at_panel = abs(m - a.measure) <= 1e-6 or abs(m - b.measure) <= 1e-6
        if gap > LRS_MAX_GAP and not at_panel:
            continue
        f = 0.0 if gap <= 0 else max(0.0, min(1.0, (m - a.measure) / gap))
        lat, lon = a.lat + f * (b.lat - a.lat), a.lon + f * (b.lon - a.lon)
        near = a if f < 0.5 else b                     # the posted panel this marker stands by
        value = f"{t / 10:.1f}"
        name = f"{route}{' ' + d if d else ''} MM {value}"
        out.append(row("milemarker", name, note, "", muni_at(lat, lon), lat, lon,
                       route, d, value, "nysdot-lrs", near.ref, [near.seq, norm_phrase(near.ref)]))
    return out


def markers_from_lrs(features: list[dict], muni_at) -> tuple[list[dict], dict]:
    """NYSDOT milepoints of LRS_ROUTES -> tenth-mile marker rows, offset to 60 Control's mileage where
    SIXTY_CONTROL has that road (else as NYSDOT counts them), one side each where the road is divided
    (both carriageways have panels), else one row for both. -> (rows, the calibration report)."""
    by_route: dict[str, list[Panel]] = collections.defaultdict(list)
    for p in parse_panels(features):
        if p.route in LRS_ROUTES.values():
            by_route[p.route].append(p)
    rows, report = [], {}
    for route, panels in sorted(by_route.items()):
        sides = []
        for want in (True, False):
            by_m = {}
            for p in panels:
                if p.primary == want:
                    by_m.setdefault(round(p.measure, 3), p)
            sides.append([by_m[k] for k in sorted(by_m)])
        primary, reverse = sides
        if len(primary) < 2:
            continue
        pos, neg, _v = ROUTES[route]
        divided = len(reverse) >= 0.8 * len(primary)
        p_dir = side(route, primary[-1].lat - primary[0].lat, primary[-1].lon - primary[0].lon) if divided else ""
        r_dir = (neg if p_dir == pos else pos) if divided else ""
        for d, ps in ((p_dir, primary), (r_dir, reverse)) if divided else ((p_dir, primary),):
            cal = SIXTY_CONTROL.get((route, d))
            offset, residuals = fit_offset(ps, cal.points if cal else [])
            made = tenth_markers(route, d, ps, offset, cal.note if cal else LRS_ORIGIN.get(route, "NYSDOT mileage"), muni_at)
            rows += made
            report[f"{route} {d}".strip()] = {
                "panels": len(ps), "milepoints": [round(ps[0].measure, 2), round(ps[-1].measure, 2)],
                "offset": round(offset, 3), "markers": len(made),
                "values": [made[0]["value"], made[-1]["value"]] if made else [],
                "max_residual": max((abs(r["residual"]) for r in residuals if r["fit"]), default=None),
                "points": residuals}
    return rows, report


def apply_landmarks(rows: list[dict]) -> list[str]:
    """60 Control's texts onto the markers with their mileage: the detail (address), and an alias the search
    finds unless the text is the exit itself. -> the points with no marker to carry them."""
    by_key = {(r["route"], r["dir"], r["value"]): r for r in rows if r["kind"] == "milemarker"}
    missing = []
    for (route, d), cal in SIXTY_CONTROL.items():
        for c in cal.points:
            r = by_key.get((route, d, f"{c.mile:.1f}"))
            if r is None:
                missing.append(f"{route} {d} {c.mile:.1f} {c.text}")
                continue
            r["address"] = " / ".join(x for x in (r["address"], c.text) if x)
            if not c.text.upper().startswith("EXIT"):
                aliases = set(r["aliases"].split("|")) | {norm_phrase(c.text)}
                r["aliases"] = "|".join(sorted(a for a in aliases if a))
    return missing


def markers_from_thruway(features: list[dict], muni_at) -> list[dict]:
    """Thruway tenth-mile posts on the New England Thruway (I-95): one post for both directions."""
    out = []
    for f in features:
        a, g = f.get("attributes") or {}, f.get("geometry") or {}
        if (a.get("RTE_ABBR") or "").strip() != "NE" or "x" not in g or a.get("POSTED_MILEPOST") is None:
            continue
        mile = f"{float(a['POSTED_MILEPOST']):.1f}"
        out.append(row("milemarker", f"I-95 MM {mile}", "milepost", "", muni_at(g["y"], g["x"]), g["y"], g["x"],
                       "I-95", "", mile, "nysta-mileposts", f"NE {a.get('MILEPOST')}", []))
    return out


def osm_route(tags: dict) -> str:
    text = f"{tags.get('ref', '')};{tags.get('name', '')};{tags.get('official_name', '')}"
    return next((r for key, r in OSM_HIGHWAYS if key in text), "")


def exits_from_osm(elements: list[dict], muni_at) -> list[dict]:
    """Motorway junction nodes on our highways' ways -> exit rows ('I-95 NB exit 21')."""
    nodes = {e["id"]: e for e in elements if e["type"] == "node"}
    ways = [e for e in elements if e["type"] == "way"]
    out, seen = [], set()
    for w in ways:
        route = osm_route(w.get("tags") or {})
        geom = w.get("geometry") or []
        ids = w.get("nodes") or []
        if not route or len(geom) != len(ids) or len(ids) < 2:
            continue
        for i, nid in enumerate(ids):
            n = nodes.get(nid)
            if n is None or (n.get("tags") or {}).get("highway") != "motorway_junction":
                continue
            t = n["tags"]
            ref = (t.get("ref") or "").strip().upper().replace(" ", "")
            if not ref:
                continue
            j, k = (i, i + 1) if i + 1 < len(ids) else (i - 1, i)
            d = side(route, geom[k]["lat"] - geom[j]["lat"], geom[k]["lon"] - geom[j]["lon"])
            if (route, d, ref) in seen:
                continue
            seen.add((route, d, ref))
            to = t.get("exit_to") or t.get("destination") or t.get("name") or ""
            # the number before a renumbering (the Hutch, the Merritt): what people still say ("old 30s")
            olds = [f"old {o.strip().lower().replace(' ', '')}" for o in (t.get("old_ref") or "").split(";") if o.strip()]
            name = f"{route} {d} exit {ref}"
            out.append(row("exit", name, "exit", to.replace(";", " / "), muni_at(n["lat"], n["lon"]), n["lat"],
                           n["lon"], route, d, ref, "osm", f"node/{nid}", olds + ([norm_phrase(to)] if to else [])))
    return out


# --------------------------------------------------------------------------- named places

def _names(t: dict) -> list[str]:
    out = []
    for k in ("name", "alt_name", "short_name", "official_name", "old_name", "loc_name"):
        out += [x.strip() for x in (t.get(k) or "").split(";") if x.strip()]
    return out


def places_from_osm(elements: list[dict], muni_at, landmarks: list[dict],
                    keep_outside: set[str] = frozenset()) -> tuple[list[dict], dict]:
    """Named OSM features -> place rows. Dropped: outside the covered municipalities (unless the
    category is in keep_outside), generic names, and anything already a landmark or listed twice
    (same name within 300 m)."""
    stats = collections.Counter()
    lm_by_name: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for lm in landmarks:
        for n in [lm["name"], *lm.get("aliases", [])]:
            lm_by_name[norm_phrase(n)].append((lm["lat"], lm["lon"]))
    kept: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    # ways and relations (the whole park) before nodes (a sign in it); those with an address first
    order = {"relation": 0, "way": 1, "node": 2}
    elements = sorted(elements, key=lambda e: (order.get(e["type"], 3), not (e.get("tags") or {}).get("addr:street")))
    out = []
    for e in elements:
        t = e.get("tags") or {}
        names = _names(t)
        if not names:
            continue
        name = names[0]
        if e["type"] == "node":
            lat, lon = e.get("lat"), e.get("lon")
        else:
            c = e.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        cat = osm_category(t)
        if lat is None or cat is None:
            stats["no category"] += 1
            continue
        key = norm_phrase(name)
        toks = key.split()
        if (len(key.replace(" ", "")) < 3 or all(w in GENERIC for w in toks)
                or any(x in name.lower() for x in ("http", "@", ".com"))):
            stats["generic name"] += 1
            continue
        muni = muni_at(lat, lon)
        if not muni and cat not in keep_outside:
            stats["outside"] += 1
            continue
        if any(dist_m(lat, lon, a, b) < DEDUP_M for a, b in lm_by_name.get(key, ())):
            stats["already a landmark"] += 1
            continue
        if any(dist_m(lat, lon, a, b) < DEDUP_M for a, b in kept.get(key, ())):
            stats["duplicate"] += 1
            continue
        kept[key].append((lat, lon))
        addr = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        if not muni:
            muni = t.get("addr:city") or ""
        aliases = [norm_phrase(n) for n in names[1:]]
        if key.startswith("the "):
            aliases.append(key[4:])
        if cat == "rail station" and "station" not in toks:       # "Port Chester": "port chester station"
            aliases += [f"{key} station", f"{key} train station", f"{key} metro north"]
        out.append(row("place", name, cat, addr, muni, lat, lon, source="osm", ref=f"{e['type']}/{e['id']}",
                       aliases=aliases))
    stats["kept"] = len(out)
    return out, dict(stats)


OSM_PLACE_FILTERS = (
    'nwr["name"]["amenity"]', 'nwr["name"]["shop"]', 'nwr["name"]["leisure"]', 'nwr["name"]["tourism"]',
    'nwr["name"]["healthcare"]', 'nwr["name"]["railway"="station"]', 'nwr["name"]["public_transport"="station"]',
    'nwr["name"]["landuse"~"^(retail|commercial|cemetery|religious|recreation_ground)$"]',
    'nwr["name"]["building"]', 'nwr["name"]["office"]', 'nwr["name"]["historic"]',
    'node["name"]["place"~"^(neighbourhood|hamlet|locality|suburb|quarter|square|island|islet)$"]',
    'nwr["name"]["natural"~"^(beach|bay|cape|peninsula|wetland|wood|water)$"]',
    'nwr["name"]["man_made"~"^(pier|breakwater|lighthouse|water_tower|wastewater_plant|water_works|bridge)$"]',
    'nwr["name"]["emergency"="ambulance_station"]', 'nwr["name"]["aeroway"~"^(helipad|aerodrome)$"]',
)


def overpass(query: str, rounds: int = 3) -> tuple[dict, str]:
    """One Overpass query: each mirror in turn, a few rounds a minute apart (they are often busy)."""
    last = None
    for r in range(rounds):
        for url in OVERPASS_URLS:
            try:
                return json.loads(fa.http_get(url, data={"data": query}, timeout=200, retries=1)), url
            except Exception as exc:  # noqa: BLE001 -- any failure (busy, a timeout, not JSON): the next mirror
                last = exc
                print(f"  overpass {url}: {str(exc)[:160]}", file=sys.stderr)
        if r + 1 < rounds:
            time.sleep(60)
    raise RuntimeError(f"all Overpass mirrors failed: {last}")


def fetch_osm_places(bounds, tile: float = 0.07) -> tuple[list[dict], str]:
    s, w, n, e = bounds.area(margin=0.002)
    elements: dict[tuple, dict] = {}
    used = ""
    ny, nx = max(1, math.ceil((n - s) / tile)), max(1, math.ceil((e - w) / tile))
    for i in range(ny):
        for j in range(nx):
            tb = (s + i * (n - s) / ny, w + j * (e - w) / nx, s + (i + 1) * (n - s) / ny, w + (j + 1) * (e - w) / nx)
            if not any(b[0] <= tb[2] and tb[0] <= b[2] and b[1] <= tb[3] and tb[1] <= b[3] for b in bounds.bboxes.values()):
                continue
            q = (f"[out:json][timeout:180][bbox:{tb[0]},{tb[1]},{tb[2]},{tb[3]}];"
                 f"({';'.join(OSM_PLACE_FILTERS)};);out center tags;")
            d, used = overpass(q)
            for el in d.get("elements", []):
                elements[(el["type"], el["id"])] = el
            time.sleep(2)
    s, w, n, e = HOSPITAL_BBOX
    d, used = overpass(f'[out:json][timeout:120][bbox:{s},{w},{n},{e}];(nwr["name"]["amenity"="hospital"];'
                       f'nwr["name"]["healthcare"="hospital"];);out center tags;')
    for el in d.get("elements", []):
        elements[(el["type"], el["id"])] = el
    print(f"  osm named features: {len(elements)}", file=sys.stderr)
    return list(elements.values()), used


def fetch_osm_exits(bbox) -> tuple[list[dict], str]:
    s, w, n, e = bbox
    q = (f'[out:json][timeout:180][bbox:{s},{w},{n},{e}];node["highway"="motorway_junction"]->.j;'
         f'way(bn.j)["highway"~"^(motorway|trunk)$"]->.m;.m out body geom;.j out body;')
    d, url = overpass(q)
    return d.get("elements", []), url


def fetch_rm(bbox) -> list[dict]:
    where = " OR ".join(f"Panel LIKE '{r}%'" for r in ("287I", "684I", "907W", "  1 ", "120 ", "120A"))
    return fa.arcgis_query(NYSDOT_RM_URL, dict({"where": where,
                                                "outFields": "ROUTE_ID,MEASURE,Panel,TO_DATE"}, **fa.bbox_params(bbox)),
                           page=1000)


def fetch_thruway(bbox) -> list[dict]:
    return fa.arcgis_query(THRUWAY_MP_URL, dict({"where": "RTE_ABBR='NE'", "outFields": "*"}, **fa.bbox_params(bbox)),
                           page=1000)


# --------------------------------------------------------------------------- write

def write_places(db: sqlite3.Connection, rows: list[dict], sources: list[dict], stats: dict,
                 calibration: dict | None = None) -> dict:
    """Replace the places table and its part of meta (sources marked part 'places', counts.places: the counts,
    and the mile markers' calibration report against 60 Control)."""
    db.execute("DROP TABLE IF EXISTS places")
    db.executescript(PLACES_SCHEMA)
    cols = ("kind", "name", "category", "address", "muni", "lat", "lon", "route", "dir", "value", "source", "ref",
            "aliases")
    rows = sorted(rows, key=lambda r: (r["kind"], r["route"], r["name"], r["lat"]))
    db.executemany(f"INSERT INTO places ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [tuple(r[c] for c in cols) for r in rows])
    meta = dict(db.execute("SELECT key, value FROM meta"))
    old = [s for s in json.loads(meta.get("sources") or "[]") if s.get("part") != "places"]
    counts = json.loads(meta.get("counts") or "{}")
    counts["places"] = {
        "by_kind": dict(collections.Counter(r["kind"] for r in rows)),
        "by_source": dict(collections.Counter(r["source"] for r in rows)),
        "by_route": dict(collections.Counter(f"{r['route']} {r['kind']}" for r in rows if r["route"])),
        "by_category": dict(collections.Counter(r["category"] for r in rows if r["kind"] == "place").most_common()),
        "osm_filter": stats,
        "calibration": calibration or {},
    }
    db.execute("INSERT OR REPLACE INTO meta VALUES ('sources', ?)", (json.dumps(old + sources),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('counts', ?)", (json.dumps(counts),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('places_built', ?)",
               (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),))
    db.commit()
    return counts["places"]


def previous(db: sqlite3.Connection, source: str, row_sources: tuple[str, ...] = ()) -> tuple[list[dict], dict | None]:
    """The rows (source in row_sources, default ``source`` itself) and the meta source entry of ``source``
    from the last build (a fetch failed)."""
    row_sources = row_sources or (source,)
    try:
        cur = db.execute(f"SELECT * FROM places WHERE source IN ({', '.join('?' * len(row_sources))})", row_sources)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur]
        srcs = json.loads(dict(db.execute("SELECT key, value FROM meta")).get("sources") or "[]")
    except sqlite3.Error:
        return [], None
    for r in rows:
        r.pop("id", None)
    entry = next((s for s in srcs if s.get("part") == "places" and s.get("code") == source), None)
    return rows, entry


def landmarks_of(db: sqlite3.Connection) -> list[dict]:
    return [{"name": n, "lat": la, "lon": lo, "aliases": [a for a in al.split("|") if a]}
            for n, la, lo, al in db.execute("SELECT name, lat, lon, aliases FROM landmarks")]


def muni_finder(bounds) -> Callable[[float, float], str]:
    """The municipality containing a point, else the nearest one within 1.5 km (a highway on the edge)."""
    def at(lat: float, lon: float) -> str:
        m = bounds.locate(lat, lon)[0]
        if m:
            return m
        best = min(((bounds.distance_m(lab, lat, lon), lab) for lab in bounds.polys), default=(math.inf, ""))
        return best[1] if best[0] <= 1500 else ""
    return at


def build(db_path: Path, bounds, today: str, skip: set[str] = frozenset(), old_db: Path | None = None) -> dict:
    """Fetch every source (keeping the last build's rows of one that fails or is skipped: from old_db,
    else db_path itself) and rewrite the table."""
    db = sqlite3.connect(db_path)
    prev_db = sqlite3.connect(old_db) if old_db and Path(old_db).exists() else db
    muni_at = muni_finder(bounds)
    s, w, n, e = bounds.area(margin=MARKER_MARGIN)
    hw_bbox = (s, w, n, e)
    rows: list[dict] = []
    sources: list[dict] = []
    missing: list[str] = []
    stats: dict = {}
    calibration: dict = {}

    def take(code: str, name: str, url: str, license_: str, fn, row_sources: tuple[str, ...] = ()) -> None:
        """Fetch one source; its rows carry source ``code`` (or one of row_sources)."""
        got, src_url = None, url
        if code not in skip:
            try:
                got = fn()
                if isinstance(got, tuple):
                    got, src_url = got
            except Exception as exc:  # noqa: BLE001 -- a source down: keep the last build's rows
                print(f"  {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        if got:
            rows.extend(got)
            sources.append({"part": "places", "code": code, "name": name, "url": src_url, "license": license_,
                            "fetched": today, "records": len(got)})
            return
        old, entry = previous(prev_db, code, row_sources)
        rows.extend(old)
        if entry:
            sources.append(dict(entry, kept_from_previous_build=True))
            if code == "nysdot-rm":
                calibration.update(entry.get("calibration") or {})
        missing.append(f"{name}: {'kept ' + str(len(old)) + ' rows of the last build' if old else 'nothing'}")

    print("NYSDOT reference markers ...", file=sys.stderr)

    def nysdot():
        feats = fetch_rm(hw_bbox)
        lrs, report = markers_from_lrs(feats, muni_at)
        calibration.update(report)
        calibration["posted_panels"] = panel_check(feats)
        return markers_from_rm(feats, muni_at) + lrs
    take("nysdot-rm", "NYSDOT Roadway Inventory: Reference Markers (the posted panels on I-287 and I-684; tenth-mile "
         "milepoints on the Hutchinson River Pkwy, US-1, NY-120 and NY-120A, the Hutch's offset to 60 Control's "
         "mileage)", NYSDOT_RM_URL, "NYSDOT public GIS service (New York State public data; no licence stated: "
         "attribute NYSDOT)", nysdot, row_sources=("nysdot-rm", "nysdot-lrs"))
    print("Thruway mileposts (I-95) ...", file=sys.stderr)
    take("nysta-mileposts", "NYS Thruway Authority mileposts, tenth-mile (New England Thruway, I-95)", THRUWAY_MP_URL,
         "NYS Thruway Authority public GIS service (listed on data.gis.ny.gov; no licence stated: attribute the "
         "NYS Thruway Authority)", lambda: markers_from_thruway(fetch_thruway(hw_bbox), muni_at))
    print("OSM exits ...", file=sys.stderr)

    def exits():
        els, url = fetch_osm_exits(hw_bbox)
        return exits_from_osm(els, muni_at), url
    osm_license = "ODbL 1.0, (c) OpenStreetMap contributors"
    old_lm = landmarks_of(db)

    # OSM rows share source 'osm': exits and places are fetched (and kept on failure) together
    print("OSM named places ...", file=sys.stderr)

    def osm():
        nonlocal stats
        ex, url = exits()
        time.sleep(2)
        els, url = fetch_osm_places(bounds)
        pl, stats = places_from_osm(els, muni_at, old_lm, keep_outside={"hospital"})
        return ex + pl, url
    take("osm", "OpenStreetMap via Overpass API: motorway junctions (exits) and named places", OVERPASS_URLS[0],
         osm_license, osm)
    calibration["landmarks_without_marker"] = apply_landmarks(rows)
    for src in sources:                    # the report travels with the source entry (kept with its rows)
        if src.get("code") == "nysdot-rm" and not src.get("kept_from_previous_build"):
            src["calibration"] = {k: v for k, v in calibration.items() if k != "landmarks_without_marker"}
    counts = write_places(db, rows, sources, stats, calibration)
    try:                                   # the airport's places (#87, tools/fetch_airport.py): after ours, from checked-in files
        import fetch_airport
        counts["khpn"] = fetch_airport.add_places(db, fetch_airport.place_rows(fetch_airport.read("khpn_surface.geojson"), db), today)
    except Exception as e:                 # noqa: BLE001  (a missing file is reported, the rest of the build stands)
        print(f"airport places skipped: {e}", file=sys.stderr)
    db.execute("VACUUM")
    db.close()
    if prev_db is not db:
        prev_db.close()
    counts["missing"] = missing
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=fa.DB_PATH)
    ap.add_argument("--cache", type=Path, help="cache raw downloads in this directory (development)")
    ap.add_argument("--skip", default="", help="comma-separated sources to keep from the last build: "
                                               "nysdot-rm, nysta-mileposts, osm")
    args = ap.parse_args(argv)
    fa.CACHE_DIR = args.cache
    if not args.db.exists():
        print(f"{args.db} missing: run tools/fetch_addresses.py first", file=sys.stderr)
        return 1
    before = args.db.stat().st_size
    print("TIGER municipal boundaries ...", file=sys.stderr)
    bounds = fa.load_boundaries()
    counts = build(args.db, bounds, dt.date.today().isoformat(), {s.strip() for s in args.skip.split(",") if s.strip()})
    print(json.dumps(counts, indent=1), file=sys.stderr)
    print(f"wrote {args.db}: {before / 1e6:.2f} MB -> {args.db.stat().st_size / 1e6:.2f} MB", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
