#!/usr/bin/env python3
"""Build the offline gazetteer (data/gazetteer.sqlite) for the sizeup geocoder.

Coverage is defined by municipal boundary polygons (US Census TIGER/Line 2025
PLACE for New York, COUSUB for Connecticut), not by a bounding box. See
MUNICIPALITIES: Port Chester and Rye Brook (PCFD's district) first, then the
City of Rye, Harrison (town = village, incl. Purchase and West Harrison), the
Village of Mamaroneck (incl. Rye Neck, the part in the Town of Rye) and the
Town of Greenwich, CT (Byram, Glenville, Pemberwick, Chickahominy, central
Greenwich, Cos Cob, Riverside, Old Greenwich, back country). Every address,
street segment, intersection and landmark is tagged with the municipality whose
polygon contains it.

Sources:

* NYS Address Points (NYS ITS GIS, CC0) via the NYS GIS REST service. Fetched by
  municipality attribute, then checked against each boundary polygon (object ids
  inside the polygon that the attribute query missed are fetched too).
* USDOT National Address Database (NAD, public domain / CC0) for Greenwich, CT:
  the State of Connecticut 9-1-1 address points (CT DESPP / DSET; also published
  as Public Domain on data.ct.gov, m6xx-nb28), queried from USDOT's NAD feature
  service.
* US Census TIGER/Line 2025 (public domain): ADDRFEAT + ROADS for Westchester
  (36119) and for Connecticut's Western Connecticut Planning Region (09190; CT
  replaced its counties, Fairfield County 09001 included, with planning regions
  in TIGER 2022+): street centrelines, used for geometry and intersections;
  PLACE (36) and COUSUB (09) for the municipal boundaries.
* Westchester County community facilities (county GIS REST): schools,
  hospitals, houses of worship, libraries, parks, police, post offices, ...
* OpenStreetMap named amenities/buildings via Overpass (ODbL) -- optional; if
  every Overpass mirror fails, the OSM landmarks of the previous build are kept
* the five PCFD / Rye Brook firehouses from data/firehouses.json

Run:  .venv/bin/python tools/fetch_addresses.py [--cache DIR]
Only stdlib + numpy. Re-run after the NYS address points refresh (2nd/4th Friday).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import io
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from sizeup.geo.gazetteer import SCHEMA, TOWN_PRIORITY  # noqa: E402
from sizeup.geo.normalize import (  # noqa: E402
    BOUNDARY, DIRECTION_WORDS, STREET_TYPES, street_key, tokenize,
)
from sizeup.geo.shapefile import iter_shapes, read_dbf  # noqa: E402

DB_PATH = ROOT / "data" / "gazetteer.sqlite"
USER_AGENT = "sizeup-gazetteer/0.2 (Port Chester FD dispatch display; offline geocoder build)"

# Covered municipalities: (label used everywhere, TIGER layer, state FIPS, TIGER NAMELSAD).
# Labels must match sizeup.geo.gazetteer.TOWN_PRIORITY (the ranking order).
MUNICIPALITIES = (
    ("Port Chester", "place", "36", "Port Chester village"),
    ("Rye Brook", "place", "36", "Rye Brook village"),
    ("Rye", "place", "36", "Rye city"),
    ("Harrison", "place", "36", "Harrison village"),      # = Town of Harrison (coterminous)
    ("Greenwich", "cousub", "09", "Greenwich town"),     # Byram, Glenville, Pemberwick, ...
    ("Mamaroneck", "place", "36", "Mamaroneck village"),  # incl. Rye Neck (Town of Rye part)
)
NY_LABELS = [m[0] for m in MUNICIPALITIES if m[2] == "36"]
CT_LABELS = [m[0] for m in MUNICIPALITIES if m[2] == "09"]
CORE_MUNIS = ("Port Chester", "Rye Brook")
# An address point whose own municipality attribute names a covered municipality is
# kept with that label when it lies this close outside that municipality's TIGER
# polygon (TIGER boundaries are generalised; the 9-1-1 attribute is authoritative).
ATTR_TOLERANCE_M = 75.0
# Origin of the local metric grid used for geometry (any nearby point will do).
ORIGIN = (40.98, -73.72)

NYS_AP_URL = "https://nysgeohub.ny.gov/arcgis/rest/services/Address/NYS_Address_Points/MapServer/0/query"
NAD_URL = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
           "Address_Points_from_National_Address_Database_view/FeatureServer/0/query")
TIGER = "https://www2.census.gov/geo/tiger/TIGER2025"
TIGER_LINE_URLS = (
    f"{TIGER}/ADDRFEAT/tl_2025_36119_addrfeat.zip", f"{TIGER}/ROADS/tl_2025_36119_roads.zip",
    f"{TIGER}/ADDRFEAT/tl_2025_09190_addrfeat.zip", f"{TIGER}/ROADS/tl_2025_09190_roads.zip",
)
TIGER_BOUNDARY_URLS = {("place", "36"): f"{TIGER}/PLACE/tl_2025_36_place.zip",
                       ("cousub", "09"): f"{TIGER}/COUSUB/tl_2025_09_cousub.zip"}
COUNTY_URL = "https://giswww.westchestergov.com/arcgis/rest/services/DataHub_CommunityFacilities/MapServer"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

AP_FIELDS = (
    "OBJECTID,AddressNumber,SuffixAddressNumber,PreModifier,PreDirectional,PreType,"
    "StreetName,PostType,PostDirectional,CompleteStreetName,IncorporatedMunicipality,"
    "ZipName,PrimaryPoint,Status,MunicipalityType"
)
NAD_FIELDS = ("OBJECTID,Add_Number,AddNum_Suf,St_PreMod,St_PreDir,St_PreTyp,St_PreSep,St_Name,"
              "St_PosTyp,St_PosDir,St_PosMod,Inc_Muni,State,Lifecycle,Expire,DateUpdate")

# County layer id -> (category, name field)
COUNTY_LAYERS = {
    162: ("school", "NAME"),
    151: ("college", "NAME"),
    178: ("hospital", "HOSPNAME"),
    176: ("worship", "ORGANIZATION"),
    175: ("library", "LIBNAME"),
    173: ("municipal", "MUNICIPALI"),
    167: ("police", "NAME"),
    165: ("post_office", "NAME"),
    179: ("historic", "RESNAME"),
    189: ("cemetery", "NAME"),
    1: ("park", "NAME"),
}

# Firehouses come from data/firehouses.json (NERIS / ryebrookny.gov addresses,
# NYS Geocoder coordinates); these are the spoken aliases per street address.
FIREHOUSES_JSON = ROOT / "data" / "firehouses.json"
FIREHOUSE_ALIASES = {
    "209 Westchester Ave": ["port chester fire headquarters", "pcfd headquarters",
                            "port chester firehouse headquarters"],
    "464 Westchester Ave": ["washington park firehouse", "washington park fire station",
                            "washington park hose", "washington park fire house"],
    "51-53 Grace Church St": ["south end firehouse", "south end fire station", "south end engine",
                              "south end fire house", "putnam and mellor", "putnam mellor"],
    "509 Willett Ave": ["brooksville engine", "brooksville firehouse", "brooksville engine and hose",
                        "brooksville fire house", "brooksville"],
    "940 King St": ["rye brook firehouse", "rye brook fire station", "rye brook fire house"],
}


def load_firehouses() -> list[tuple[str, str, float, float, list[str]]]:
    d = json.loads(FIREHOUSES_JSON.read_text())
    out = []
    for f in d["firehouses"]:
        lon, lat = f["ll"]
        name = f["name"]
        aliases = list(FIREHOUSE_ALIASES.get(f["address"], []))
        if any(w in name.lower() for w in ("fire", "headquarters", "engine", "hose")):
            aliases.append(name)
        else:
            name = f"{name} Firehouse"     # "Washington Park" alone is also a neighbourhood
        out.append((name, f["address"], lat, lon, aliases))
    return out


# Extra spoken aliases for named facilities (normalised name -> aliases).
LANDMARK_ALIASES = {
    "port chester senior high school": ["port chester high school", "pchs"],
    "blind brook high school": ["bbhs"],
    "bruno m ponterio ridge st school": ["ridge st school", "ponterio school"],
    "thomas a edison school": ["edison school"],
    "john f kennedy elementary school": ["jfk school", "kennedy school", "jfk elementary school",
                                        "john f kennedy school"],
    "louis m klein middle school": ["klein middle school"],
    "port chester rye brook public library": ["port chester library", "port chester public library"],
    "capitol theater": ["capital theater"],
}

# Words that may appear in a page header / dispatch phrasing: a landmark alias
# made only of these is never matched.
GENERIC_WORDS = {
    "port", "chester", "rye", "brook", "harrison", "purchase", "village", "city", "town", "of",
    "mamaroneck", "neck", "greenwich", "byram", "glenville", "pemberwick", "cos", "cob", "old",
    "riverside", "west", "east", "ct", "connecticut",
    "fire", "department", "fd", "pd", "police", "ems", "ambulance", "engine", "ladder",
    "rescue", "and", "the", "westchester", "county", "ny", "new", "york", "municipal",
    "building", "st", "ave", "rd", "corp", "inc", "llc", "company", "co",
}

M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(41.02))


# --------------------------------------------------------------------------- http

CACHE_DIR: Path | None = None   # --cache: keep raw downloads (for development)


def http_get(url: str, params: dict | None = None, data: dict | None = None,
             timeout: float = 120, retries: int = 3) -> bytes:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data else None
    cache_file = None
    if CACHE_DIR is not None:
        import hashlib
        cache_file = CACHE_DIR / hashlib.sha1(url.encode() + (body or b"")).hexdigest()
        if cache_file.exists():
            return cache_file.read_bytes()
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            if cache_file is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(payload)
            return payload
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"GET {url[:120]} failed: {last}")


def arcgis_post(url: str, params: dict, timeout: float = 300) -> dict:
    """One ArcGIS REST query, POSTed (polygon geometries do not fit in a URL)."""
    d = json.loads(http_get(url, data=dict(params, f="json"), timeout=timeout))
    if "error" in d:
        raise RuntimeError(f"ArcGIS error from {url}: {d['error']}")
    return d


def arcgis_query(url: str, params: dict, page: int = 2000, delay: float = 0.3) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        p = dict(params, outSR="4326", resultOffset=offset, resultRecordCount=page)
        d = arcgis_post(url, p)
        feats = d.get("features", [])
        out.extend(feats)
        if not feats or (not d.get("exceededTransferLimit") and len(feats) < page):
            return out
        offset += len(feats)
        time.sleep(delay)


def arcgis_by_ids(url: str, ids: list[int], fields: str, batch: int = 500) -> list[dict]:
    out: list[dict] = []
    for k in range(0, len(ids), batch):
        d = arcgis_post(url, {"objectIds": ",".join(map(str, ids[k:k + batch])),
                              "outFields": fields, "outSR": "4326"})
        out.extend(d.get("features", []))
        time.sleep(0.3)
    return out


def bbox_params(bbox) -> dict:
    s, w, n, e = bbox
    return {"geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}


# --------------------------------------------------------------------------- boundaries

def to_xy(lat, lon):
    return ((np.asarray(lon, dtype=float) - ORIGIN[1]) * M_PER_DEG_LON,
            (np.asarray(lat, dtype=float) - ORIGIN[0]) * M_PER_DEG_LAT)


def _ring_crossings(ring: np.ndarray, x: np.ndarray, y: np.ndarray, chunk: int = 2000) -> np.ndarray:
    """Even-odd ray casting of points (x, y) against one closed ring (N, 2) of lon/lat."""
    x0, y0, x1, y1 = ring[:-1, 0], ring[:-1, 1], ring[1:, 0], ring[1:, 1]
    dy = np.where(y1 == y0, 1e-300, y1 - y0)
    odd = np.zeros(len(x), dtype=bool)
    for k in range(0, len(x), chunk):
        px, py = x[k:k + chunk, None], y[k:k + chunk, None]
        straddle = (y0 > py) != (y1 > py)
        xint = x0 + (py - y0) * (x1 - x0) / dy
        odd[k:k + chunk] = (np.count_nonzero(straddle & (px < xint), axis=1) % 2) == 1
    return odd


class Boundaries:
    """Municipal boundary polygons (TIGER) -> point-in-polygon labels and distances."""

    def __init__(self, polys: dict[str, list[np.ndarray]]):
        self.polys = polys                       # label -> closed rings, (N, 2) lon/lat
        self.bboxes = {}
        for m, rings in polys.items():
            allp = np.concatenate(rings)
            self.bboxes[m] = (allp[:, 1].min(), allp[:, 0].min(), allp[:, 1].max(), allp[:, 0].max())

    def area(self, labels=None, margin: float = 0.0) -> tuple[float, float, float, float]:
        bbs = [self.bboxes[m] for m in (labels or self.polys)]
        return (min(b[0] for b in bbs) - margin, min(b[1] for b in bbs) - margin,
                max(b[2] for b in bbs) + margin, max(b[3] for b in bbs) + margin)

    def contains(self, label: str, lat, lon) -> np.ndarray:
        lat = np.atleast_1d(np.asarray(lat, dtype=float))
        lon = np.atleast_1d(np.asarray(lon, dtype=float))
        s, w, n, e = self.bboxes[label]
        cand = np.nonzero((lat >= s) & (lat <= n) & (lon >= w) & (lon <= e))[0]
        inside = np.zeros(len(lat), dtype=bool)
        if len(cand):
            odd = np.zeros(len(cand), dtype=bool)
            for ring in self.polys[label]:
                odd ^= _ring_crossings(ring, lon[cand], lat[cand])
            inside[cand] = odd
        return inside

    def locate(self, lat, lon) -> np.ndarray:
        """Label of the polygon containing each point ("" outside all of them)."""
        lat = np.atleast_1d(np.asarray(lat, dtype=float))
        out = np.full(len(lat), "", dtype=object)
        for m in self.polys:
            hit = self.contains(m, lat, lon) & (out == "")
            out[hit] = m
        return out

    def distance_m(self, label: str, lat: float, lon: float) -> float:
        """Distance in metres from a point to the boundary of ``label`` (0 inside)."""
        if self.contains(label, lat, lon)[0]:
            return 0.0
        px, py = to_xy(lat, lon)
        best = math.inf
        for ring in self.polys[label]:
            x, y = to_xy(ring[:, 1], ring[:, 0])
            x0, y0, x1, y1 = x[:-1], y[:-1], x[1:], y[1:]
            dx, dy = x1 - x0, y1 - y0
            ll = np.maximum(dx * dx + dy * dy, 1e-9)
            t = np.clip(((px - x0) * dx + (py - y0) * dy) / ll, 0, 1)
            best = min(best, float(np.min(np.hypot(x0 + t * dx - px, y0 + t * dy - py))))
        return best

    def esri_polygon(self, label: str) -> dict:
        return {"geometry": json.dumps({"rings": [[[round(float(x), 6), round(float(y), 6)]
                                                   for x, y in r] for r in self.polys[label]],
                                        "spatialReference": {"wkid": 4326}}),
                "geometryType": "esriGeometryPolygon", "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects"}


def load_boundaries() -> Boundaries:
    polys: dict[str, list[np.ndarray]] = {}
    for (layer, fips), url in TIGER_BOUNDARY_URLS.items():
        want = {namelsad: label for label, lyr, st, namelsad in MUNICIPALITIES
                if (lyr, st) == (layer, fips)}
        for rec, _bbox, parts in read_tiger(url):
            label = want.get(rec.get("NAMELSAD", ""))
            if label and (layer != "cousub" or rec.get("COUNTYFP") == "190"):
                polys[label] = [np.asarray(p, dtype=float) for p in parts]
    missing = [m[0] for m in MUNICIPALITIES if m[0] not in polys]
    if missing:
        raise RuntimeError(f"no TIGER boundary for {missing}")
    if [m[0] for m in MUNICIPALITIES] != [m for m in TOWN_PRIORITY if m in polys]:
        print("  note: MUNICIPALITIES order differs from gazetteer.TOWN_PRIORITY "
              "(only the latter affects ranking)", file=sys.stderr)
    return Boundaries({m[0]: polys[m[0]] for m in MUNICIPALITIES})


# --------------------------------------------------------------------------- sources

def fetch_ny_address_points(bounds: Boundaries) -> tuple[list[dict], dict]:
    """NYS address points of the covered NY municipalities, checked against the polygons."""
    feats: dict[int, dict] = {}
    names = ",".join(f"'{m}'" for m in NY_LABELS)
    q = {"where": f"CountyName='Westchester' AND IncorporatedMunicipality IN ({names})",
         "orderByFields": "OBJECTID", "outFields": AP_FIELDS}
    for f in arcgis_query(NYS_AP_URL, q):
        feats[f["attributes"]["OBJECTID"]] = f
    verify = {}
    for label in NY_LABELS:
        d = arcgis_post(NYS_AP_URL, dict({"where": "1=1", "returnIdsOnly": "true"},
                                         **bounds.esri_polygon(label)))
        ids = d.get("objectIds") or []
        missing = sorted(set(ids) - feats.keys())
        for f in arcgis_by_ids(NYS_AP_URL, missing, AP_FIELDS):
            feats[f["attributes"]["OBJECTID"]] = f
        verify[label] = {"points_in_polygon": len(ids), "missed_by_attribute_query": len(missing)}
        time.sleep(0.5)
    print(f"  NYS address points: {len(feats)} raw; polygon check {verify}", file=sys.stderr)
    recs = []
    for f in feats.values():
        a, g = f["attributes"], f.get("geometry") or {}
        if "x" not in g:
            continue
        attr = (a.get("IncorporatedMunicipality") or "").strip()
        if attr == "Mamaroneck" and a.get("MunicipalityType") != "3":
            attr = ""                     # the unincorporated Town of Mamaroneck
        recs.append({
            "number": a.get("AddressNumber"), "suffix": (a.get("SuffixAddressNumber") or "").strip(),
            "street": display_street(a), "active": (a.get("Status") or "Active") == "Active",
            "primary": a.get("PrimaryPoint") == "Y", "attr": attr if attr in NY_LABELS else "",
            "lat": g["y"], "lon": g["x"]})
    return recs, verify


def nad_display_street(a: dict) -> str:
    """NAD street parts are spelled out ("North", "Road"); abbreviate like the NYS data."""
    parts = []
    for k in ("St_PreMod", "St_PreDir", "St_PreTyp", "St_PreSep", "St_Name", "St_PosTyp",
              "St_PosDir", "St_PosMod"):
        v = (a.get(k) or "").strip()
        if not v:
            continue
        low = v.lower()
        if k in ("St_PreDir", "St_PosDir") and low in DIRECTION_WORDS:
            v = DIRECTION_WORDS[low].upper()
        elif k == "St_PosTyp" and low in STREET_TYPES and low not in {"saint", "mount", "fort"}:
            v = STREET_TYPES[low].title()
        parts.append(v)
    return " ".join(parts)


def fetch_ct_address_points(bounds: Boundaries) -> tuple[list[dict], dict]:
    """Connecticut 9-1-1 address points of the covered CT towns, from USDOT's NAD."""
    names = ",".join(f"'{m}'" for m in CT_LABELS)
    q = dict({"where": f"State='CT' AND Inc_Muni IN ({names})", "orderByFields": "OBJECTID",
              "outFields": NAD_FIELDS}, **bbox_params(bounds.area(CT_LABELS, 0.003)))
    feats = {f["attributes"]["OBJECTID"]: f for f in arcgis_query(NAD_URL, q)}
    verify = {}
    for label in CT_LABELS:
        try:
            d = arcgis_post(NAD_URL, dict({"where": "State='CT'", "returnIdsOnly": "true"},
                                          **bounds.esri_polygon(label)))
            ids = d.get("objectIds") or []
            missing = sorted(set(ids) - feats.keys())
            for f in arcgis_by_ids(NAD_URL, missing, NAD_FIELDS):
                feats[f["attributes"]["OBJECTID"]] = f
            verify[label] = {"points_in_polygon": len(ids), "missed_by_attribute_query": len(missing)}
        except RuntimeError as exc:
            verify[label] = {"error": str(exc)[:200]}
    print(f"  NAD (CT) address points: {len(feats)} raw; polygon check {verify}", file=sys.stderr)
    now_ms = time.time() * 1000
    recs = []
    for f in feats.values():
        a, g = f["attributes"], f.get("geometry") or {}
        if "x" not in g:
            continue
        expired = a.get("Expire") is not None and a["Expire"] < now_ms
        attr = (a.get("Inc_Muni") or "").strip()
        recs.append({
            "number": a.get("Add_Number"), "suffix": (a.get("AddNum_Suf") or "").strip(),
            "street": nad_display_street(a),
            "active": not expired and (a.get("Lifecycle") or "Current") in {"Current", "Active"},
            "primary": True, "attr": attr if attr in CT_LABELS else "",
            "lat": g["y"], "lon": g["x"]})
    return recs, verify


def fetch_zip(url: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(http_get(url, timeout=300)))


def read_tiger(url: str) -> list[tuple[dict, tuple, list]]:
    z = fetch_zip(url)
    stem = Path(url).stem
    recs = read_dbf(z.read(stem + ".dbf"))
    shapes = list(iter_shapes(z.read(stem + ".shp")))
    if len(recs) != len(shapes):
        raise RuntimeError(f"{stem}: {len(recs)} dbf records vs {len(shapes)} shapes")
    return [(r, b, p) for r, (b, p) in zip(recs, shapes)]


def fetch_county_landmarks(bbox) -> list[dict]:
    out = []
    for layer, (category, name_field) in COUNTY_LAYERS.items():
        feats = arcgis_query(f"{COUNTY_URL}/{layer}/query",
                             dict({"where": "1=1", "outFields": "*"}, **bbox_params(bbox)),
                             page=1000)
        for f in feats:
            a = f["attributes"]
            g = f.get("geometry") or {}
            if "x" not in g:
                continue
            name = (a.get(name_field) or "").strip()
            if not name:
                continue
            if category == "historic" and (a.get("FACILITY") or "").strip().lower() == "demolished":
                continue
            addr = (a.get("ADDRESS") or a.get("STREET") or "").strip()
            out.append({"name": name, "category": category, "address": addr,
                        "lat": g["y"], "lon": g["x"], "source": "westchester-county"})
    print(f"  county facilities: {len(out)}", file=sys.stderr)
    return out


OSM_KEYS = "amenity|shop|leisure|tourism|healthcare|office|railway|building|historic|man_made"
OSM_SKIP_VALUES = {
    "parking", "parking_space", "parking_entrance", "bench", "waste_basket", "vending_machine",
    "atm", "bicycle_parking", "charging_station", "post_box", "toilets", "drinking_water",
    "shelter", "fountain", "recycling", "telephone", "bicycle_rental", "motorcycle_parking",
    "pitch", "garden", "playground", "picnic_table", "dog_park", "construction", "bbq",
    "bus_station", "taxi", "car_sharing", "grit_bin", "clock", "letter_box", "boat_rental",
}
OSM_SKIP_KINDS = {"artwork", "information", "yes_no"}
OSM_RAILWAY_OK = {"station"}
# one-word OSM names that are ordinary words in a dispatch transcript
COMMON_SINGLE_WORDS = {"signature", "legend", "aurora", "reclaim", "subway", "target",
                       "courtyard", "verizon", "mastercard", "signature", "sportech"}
OSM_MAN_MADE_OK = {"wastewater_plant", "water_works", "water_tower", "tower", "pier", "lighthouse"}


def _overpass(bbox) -> tuple[dict, str]:
    s, w, n, e = bbox
    q = (f'[out:json][timeout:120][bbox:{s},{w},{n},{e}];'
         f'(nwr["name"][~"^({OSM_KEYS})$"~"."];);out center tags;')
    last = None
    for url in OVERPASS_URLS:
        try:
            return json.loads(http_get(url, data={"data": q}, timeout=180, retries=2)), url
        except Exception as exc:  # noqa: BLE001 -- any failure: try next mirror
            last = exc
            print(f"  overpass {url}: {exc}", file=sys.stderr)
    raise RuntimeError(f"all Overpass mirrors failed: {last}")


def fetch_osm_landmarks(bbox, bounds: Boundaries | None = None,
                        tile: float = 0.07) -> tuple[list[dict], str]:
    """Named OSM features in ``bbox``, queried in tiles (Overpass times out on the whole area)."""
    s, w, n, e = bbox
    elements: dict[tuple, dict] = {}
    used = ""
    ny, nx = max(1, math.ceil((n - s) / tile)), max(1, math.ceil((e - w) / tile))
    for i in range(ny):
        for j in range(nx):
            tb = (s + i * (n - s) / ny, w + j * (e - w) / nx,
                  s + (i + 1) * (n - s) / ny, w + (j + 1) * (e - w) / nx)
            if bounds is not None and not any(
                    b[0] <= tb[2] and tb[0] <= b[2] and b[1] <= tb[3] and tb[1] <= b[3]
                    for b in bounds.bboxes.values()):
                continue
            d, used = _overpass(tb)
            for el in d.get("elements", []):
                elements[(el["type"], el["id"])] = el
            time.sleep(2)
    d = {"elements": list(elements.values())}
    out = []
    for el in d.get("elements", []):
        t = el.get("tags", {})
        name = t.get("name", "").strip()
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if not name or lat is None:
            continue
        kind = next((f"{k}={t[k]}" for k in OSM_KEYS.split("|") if k in t), "")
        k, _, v = kind.partition("=")
        if v in OSM_SKIP_VALUES or (k == "railway" and v not in OSM_RAILWAY_OK) \
                or (k == "man_made" and v not in OSM_MAN_MADE_OK):
            continue
        if t.get("highway"):
            continue
        if v in OSM_SKIP_KINDS or any(x in name.lower() for x in ("@", "http", ".com", "gmail")):
            continue
        addr = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        out.append({"name": name, "category": kind, "address": addr, "lat": lat, "lon": lon,
                    "source": "osm"})
    print(f"  osm landmarks: {len(out)}", file=sys.stderr)
    return out, used


# --------------------------------------------------------------------------- processing

def display_street(a: dict) -> str:
    parts = []
    for k in ("PreModifier", "PreDirectional", "PreType", "StreetName", "PostType", "PostDirectional"):
        v = (a.get(k) or "").strip()
        if v:
            parts.append(v)
    name = " ".join(parts)
    if name.startswith("Interstate "):
        name = "I-" + name[len("Interstate "):]
    return name


def tiger_display(fullname: str) -> str:
    return fullname.replace("I- ", "I-").strip()


def build_addresses(recs: list[dict], bounds: Boundaries):
    """-> streets {key: display}, rows [(key, number, suffix, muni, lat, lon, points)], stats.

    Each point is labelled with the municipality polygon that contains it; a point whose
    own (9-1-1) municipality attribute names a covered municipality keeps that label when
    it is within ATTR_TOLERANCE_M of that polygon. Points outside every polygon are dropped.
    """
    recs = [r for r in recs if r["active"] and r["number"] is not None and r["street"]]
    pip = bounds.locate([r["lat"] for r in recs], [r["lon"] for r in recs])
    stats = collections.Counter()
    names: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    groups: dict[tuple, list] = collections.defaultdict(list)
    for r, poly in zip(recs, pip):
        muni = poly
        attr = r["attr"]
        if attr and attr != poly:
            if bounds.distance_m(attr, r["lat"], r["lon"]) <= ATTR_TOLERANCE_M:
                muni = attr
                stats["attribute_kept_near_boundary"] += 1
            elif poly:
                stats["relabelled_by_polygon"] += 1
        if not muni:
            stats["outside_polygons"] += 1
            continue
        disp = r["street"]
        if disp.startswith("Interstate "):
            disp = "I-" + disp[len("Interstate "):]
        key = street_key(disp)
        names[key][disp] += 1
        groups[(key, int(r["number"]), r["suffix"], muni)].append((r["primary"], r["lat"], r["lon"]))
    rows = []
    for (key, num, suffix, muni), pts in groups.items():
        prim = [p for p in pts if p[0]] or pts
        lat = sum(p[1] for p in prim) / len(prim)
        lon = sum(p[2] for p in prim) / len(prim)
        rows.append((key, num, suffix, muni, lat, lon, len(pts)))
    streets = {k: c.most_common(1)[0][0] for k, c in names.items()}
    return streets, rows, dict(stats)


def labelled_runs(pts: list[tuple[float, float]], labels) -> list[tuple[str, list]]:
    """Split a polyline (lon, lat) into runs per municipality, one vertex of overhang each side.

    A street that only touches a municipality with its end vertex (a side street
    ending on a border road) gets no run there.
    """
    out = []
    last = len(pts) - 1
    for m in {x for x in labels if x}:
        inside = [x == m for x in labels]
        if sum(inside) == 1 and (inside[0] or inside[last]):
            continue
        cur: list = []
        for i, p in enumerate(pts):
            near = inside[i] or (i > 0 and inside[i - 1]) or (i + 1 < len(pts) and inside[i + 1])
            if near:
                cur.append(p)
            elif cur:
                out.append((m, cur))
                cur = []
        if cur:
            out.append((m, cur))
    return [(m, r) for m, r in out if len(r) >= 2]


def build_lines(tiger: list[tuple[str, list]], bounds: Boundaries):
    """-> lines [(key, display, [(lon, lat), ...], muni)] inside the covered municipalities.

    ``tiger`` is [(kind, records)] with kind "addrfeat" or "roads". ROADS fills in
    un-addressed edges (e.g. the end of a street at a junction); its route numbers
    ("US Rte 1") duplicate the local names of the same edges and are skipped.
    """
    s, w, n, e = bounds.area(margin=0.002)
    todo = []                                   # (key, display, part)
    seen = set()
    for kind, recs in tiger:
        for rec, bbox, parts in recs:
            name = rec.get("FULLNAME", "")
            if not name or not bbox or bbox[2] < w or bbox[0] > e or bbox[3] < s or bbox[1] > n:
                continue
            key = street_key(name)
            if kind == "addrfeat":
                if (rec.get("TLID"), key) in seen:
                    continue
                seen.add((rec.get("TLID"), key))
            else:
                is_route = key.startswith(("us rte", "state rte", "state hwy", "co rd", "co hwy"))
                if is_route and rec.get("MTFCC") != "S1100":
                    continue
            for part in parts:
                todo.append((key, tiger_display(name), part))
    flat = [p for _k, _d, part in todo for p in part]
    labels = bounds.locate([p[1] for p in flat], [p[0] for p in flat])
    lines = []
    pos = 0
    for key, disp, part in todo:
        lab = labels[pos:pos + len(part)]
        pos += len(part)
        for muni, run in labelled_runs(part, lab):
            lines.append((key, disp, run, muni))
    return lines


def nearest_muni_fn(addr_rows):
    lat = np.array([r[4] for r in addr_rows])
    lon = np.array([r[5] for r in addr_rows])
    muni = [r[3] for r in addr_rows]
    ax, ay = to_xy(lat, lon)
    cell = 300.0
    grid: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    for i, (x, y) in enumerate(zip(ax, ay)):
        grid[(int(x // cell), int(y // cell))].append(i)

    def nearest(la: float, lo: float, max_m: float = 300.0) -> str:
        x, y = to_xy(la, lo)
        cx, cy = int(x // cell), int(y // cell)
        best, bd = "", max_m
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in grid.get((cx + dx, cy + dy), ()):
                    d = math.hypot(ax[i] - x, ay[i] - y)
                    if d < bd:
                        best, bd = muni[i], d
        return best
    return nearest


def _seg_hit(a, b, tol):
    """Intersection point of segments a, b (each (x1, y1, x2, y2)) in metres, or None.

    Returns ('overlap', None) when the segments lie on top of each other.
    """
    x1, y1, x2, y2 = a
    x3, y3, x4, y4 = b
    dx1, dy1, dx2, dy2 = x2 - x1, y2 - y1, x4 - x3, y4 - y3
    den = dx1 * dy2 - dy1 * dx2
    l1 = math.hypot(dx1, dy1)
    l2 = math.hypot(dx2, dy2)
    if l1 < 1e-6 or l2 < 1e-6:
        return None

    def pdist(px, py, sx, sy, ex, ey, ll):
        t = max(0.0, min(1.0, ((px - sx) * (ex - sx) + (py - sy) * (ey - sy)) / (ll * ll)))
        qx, qy = sx + t * (ex - sx), sy + t * (ey - sy)
        return math.hypot(px - qx, py - qy)

    if abs(den) > 1e-9 * l1 * l2 * 1e3:
        t = ((x3 - x1) * dy2 - (y3 - y1) * dx2) / den
        u = ((x3 - x1) * dy1 - (y3 - y1) * dx1) / den
        if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            # nearly parallel and sharing geometry -> overlap
            if abs(den) / (l1 * l2) < 0.05 and pdist(x3, y3, x1, y1, x2, y2, l1) < tol \
                    and pdist(x4, y4, x1, y1, x2, y2, l1) < tol:
                return ("overlap", None)
            return ("hit", (x1 + t * dx1, y1 + t * dy1))
    # T-junction / near miss: an endpoint within tol of the other segment
    d3 = pdist(x3, y3, x1, y1, x2, y2, l1)
    d4 = pdist(x4, y4, x1, y1, x2, y2, l1)
    d1 = pdist(x1, y1, x3, y3, x4, y4, l2)
    d2 = pdist(x2, y2, x3, y3, x4, y4, l2)
    if (d3 < tol and d4 < tol) or (d1 < tol and d2 < tol):
        return ("overlap", None)
    for d, (px, py) in ((d3, (x3, y3)), (d4, (x4, y4)), (d1, (x1, y1)), (d2, (x2, y2))):
        if d < tol:
            return ("hit", (px, py))
    return None


def compute_intersections(lines, key_ids, tol=5.0, cluster_m=60.0):
    """-> [(id_a, id_b, lat, lon, muni)] for every place two differently named streets meet.

    ``muni`` is every municipality in which both streets have line parts at that point,
    one row each; when they share none (a side street ending on a border road such as
    King St) one row for each street's municipality.
    """
    segs = []
    seg_muni = []
    for key, _disp, pts, muni in lines:
        sid = key_ids[key]
        xs, ys = to_xy([p[1] for p in pts], [p[0] for p in pts])
        for i in range(len(pts) - 1):
            segs.append((sid, (float(xs[i]), float(ys[i]), float(xs[i + 1]), float(ys[i + 1]))))
            seg_muni.append(muni)
    cell = 150.0
    grid: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    for idx, (_sid, (x1, y1, x2, y2)) in enumerate(segs):
        for cx in range(int((min(x1, x2) - tol) // cell), int((max(x1, x2) + tol) // cell) + 1):
            for cy in range(int((min(y1, y2) - tol) // cell), int((max(y1, y2) + tol) // cell) + 1):
                grid[(cx, cy)].append(idx)
    checked = set()
    hits: dict[tuple[int, int], list] = collections.defaultdict(list)
    overlaps: collections.Counter = collections.Counter()
    for idxs in grid.values():
        for ii in range(len(idxs)):
            i = idxs[ii]
            si, ai = segs[i]
            for jj in range(ii + 1, len(idxs)):
                j = idxs[jj]
                sj, aj = segs[j]
                if si == sj or (i, j) in checked:
                    continue
                checked.add((i, j))
                r = _seg_hit(ai, aj, tol)
                if r is None:
                    continue
                pair = (min(si, sj), max(si, sj))
                if r[0] == "overlap":
                    overlaps[pair] += 1
                else:
                    ma, mb = (seg_muni[i], seg_muni[j]) if si < sj else (seg_muni[j], seg_muni[i])
                    hits[pair].append((r[1][0], r[1][1], ma, mb))
    out = []
    for pair, pts in hits.items():
        if overlaps[pair] >= 2:
            continue                       # two names for one road
        clusters: list[list] = []
        for p in pts:
            for c in clusters:
                cx = sum(q[0] for q in c) / len(c)
                cy = sum(q[1] for q in c) / len(c)
                if math.hypot(p[0] - cx, p[1] - cy) < cluster_m:
                    c.append(p)
                    break
            else:
                clusters.append([p])
        if len(clusters) > 8:
            continue                       # parallel roads, not real intersections
        for c in clusters:
            x = sum(q[0] for q in c) / len(c)
            y = sum(q[1] for q in c) / len(c)
            lat, lon = ORIGIN[0] + y / M_PER_DEG_LAT, ORIGIN[1] + x / M_PER_DEG_LON
            ma, mb = {q[2] for q in c} - {""}, {q[3] for q in c} - {""}
            # on a border road (King St is the NY/CT line) the two streets' parts can
            # carry different labels: the junction then belongs to both towns
            shared = (ma & mb) or (ma | mb)
            for m in sorted(shared, key=lambda m: TOWN_PRIORITY.index(m)
                            if m in TOWN_PRIORITY else len(TOWN_PRIORITY)) or [""]:
                out.append((pair[0], pair[1], lat, lon, m))
    return out


def norm_phrase(text: str) -> str:
    return " ".join(t for t in tokenize(text) if t != BOUNDARY)


def clean_landmark_name(name: str, category: str) -> tuple[str, list[str]]:
    """Display name and spoken aliases (normalised phrases) for a facility."""
    import re
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    if name.isupper():
        name = name.title().replace("'S ", "'s ").replace(" Of ", " of ")
    aliases: list[str] = []
    if category == "municipal":
        muni = name.title()
        name = f"{muni} Municipal Building"
        aliases = [f"{muni} municipal building"]
        if muni == "Rye Brook":
            aliases += ["rye brook village hall"]
    elif category == "police":
        muni = re.sub(r"\s*(PD|Police.*)$", "", name).strip()
        if muni.lower().startswith("rye city"):
            muni = "Rye"
        name = f"{muni} Police Department"
        aliases = [f"{muni} police headquarters", f"{muni} police station"]
    elif category == "post_office":
        name = re.sub(r"\bP\.?O\.?$", "Post Office", name).strip()
        aliases = [name]
    else:
        aliases = [name]
    norm = norm_phrase(name)
    aliases = [norm_phrase(a) for a in aliases]
    aliases += [norm_phrase(a) for a in LANDMARK_ALIASES.get(norm, [])]
    if "senior high" in norm:
        aliases.append(norm.replace("senior high", "high"))
    if category == "historic" and norm.endswith(" theater"):
        aliases.append(norm)
    return name, aliases


def usable_alias(alias: str, street_aliases: set[str]) -> bool:
    toks = alias.split()
    if not toks or len("".join(toks)) < 6:
        return False
    if all(t in GENERIC_WORDS or t.isdigit() for t in toks):
        return False
    if "".join(toks) in street_aliases:
        return False
    return True


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=DB_PATH)
    ap.add_argument("--no-osm", action="store_true", help="skip Overpass (keep previous OSM landmarks)")
    ap.add_argument("--cache", type=Path, help="cache raw downloads in this directory (development)")
    args = ap.parse_args(argv)
    global CACHE_DIR
    CACHE_DIR = args.cache
    today = dt.date.today().isoformat()
    sources = []

    print("TIGER municipal boundaries ...", file=sys.stderr)
    bounds = load_boundaries()
    for url in TIGER_BOUNDARY_URLS.values():
        sources.append({"name": "US Census TIGER/Line 2025 " + Path(url).stem, "url": url,
                        "license": "Public domain (US Census Bureau)", "fetched": today,
                        "use": "municipal boundaries: " + ", ".join(
                            f"{m[0]} = {m[3]}" for m in MUNICIPALITIES
                            if TIGER_BOUNDARY_URLS[(m[1], m[2])] == url)})

    print("NYS address points ...", file=sys.stderr)
    ny_recs, ny_verify = fetch_ny_address_points(bounds)
    sources.append({"name": "NYS Address Points (Westchester)", "url": NYS_AP_URL,
                    "license": "CC0 1.0 (NYS ITS Geospatial Services)", "fetched": today,
                    "records": len(ny_recs), "municipalities": NY_LABELS})
    print("NAD address points (Connecticut) ...", file=sys.stderr)
    ct_recs, ct_verify = fetch_ct_address_points(bounds)
    sources.append({"name": "USDOT National Address Database (State of Connecticut 9-1-1 address points)",
                    "url": NAD_URL,
                    "license": "Public domain / CC0 (USDOT NAD); CT source data: CT DESPP Division of "
                               "Statewide Emergency Telecommunications, Public Domain on data.ct.gov "
                               "(dataset m6xx-nb28)",
                    "fetched": today, "records": len(ct_recs), "municipalities": CT_LABELS})
    streets, addr_rows, tag_stats = build_addresses(ny_recs + ct_recs, bounds)
    print(f"  tagging: {tag_stats}", file=sys.stderr)

    print("TIGER/Line ADDRFEAT + ROADS ...", file=sys.stderr)
    tiger = []
    for url in TIGER_LINE_URLS:
        tiger.append(("addrfeat" if "addrfeat" in url else "roads", read_tiger(url)))
        sources.append({"name": "US Census TIGER/Line 2025 " + Path(url).stem, "url": url,
                        "license": "Public domain (US Census Bureau)", "fetched": today})
    lines = build_lines(tiger, bounds)
    del tiger
    for key, disp, _pts, _m in lines:
        streets.setdefault(key, disp)
    key_ids = {k: i + 1 for i, k in enumerate(sorted(streets))}

    nearest_muni = nearest_muni_fn(addr_rows)

    def muni_at(lat: float, lon: float, max_m: float = 300.0) -> str:
        return bounds.locate(lat, lon)[0] or nearest_muni(lat, lon, max_m)

    print("intersections ...", file=sys.stderr)
    inters = compute_intersections(lines, key_ids)

    print("landmarks ...", file=sys.stderr)
    area = bounds.area(margin=0.002)
    raw_lm = fetch_county_landmarks(bounds.area(NY_LABELS, 0.002))
    sources.append({"name": "Westchester County GIS DataHub Community Facilities", "url": COUNTY_URL,
                    "license": "Westchester County GIS public REST service "
                               "(no licence stated; attribute the county)",
                    "fetched": today})
    osm_lm: list[dict] = []
    osm_source = None
    if not args.no_osm:
        try:
            osm_lm, osm_url = fetch_osm_landmarks(area, bounds)
            osm_source = {"name": "OpenStreetMap via Overpass API", "url": osm_url,
                          "license": "ODbL 1.0, (c) OpenStreetMap contributors", "fetched": today}
        except RuntimeError as exc:
            print(f"  OSM skipped: {exc}", file=sys.stderr)
    if not osm_lm and args.out.exists():
        old = sqlite3.connect(args.out)
        try:
            osm_lm = [dict(zip(("name", "category", "address", "lat", "lon", "source"), r))
                      for r in old.execute("SELECT name, category, address, lat, lon, source "
                                           "FROM landmarks WHERE source = 'osm'")]
            prev = json.loads(old.execute("SELECT value FROM meta WHERE key='sources'").fetchone()[0])
            osm_source = next((s for s in prev if s["name"].startswith("OpenStreetMap")), None)
            if osm_lm:
                print(f"  kept {len(osm_lm)} OSM landmarks from previous build", file=sys.stderr)
        except sqlite3.Error:
            osm_lm = []
        finally:
            old.close()
    if osm_source and osm_lm:
        sources.append(osm_source)
    sources.append({"name": "PCFD / Rye Brook firehouses", "url": "data/firehouses.json",
                    "license": json.loads(FIREHOUSES_JSON.read_text()).get("_source", ""),
                    "fetched": dt.date.fromtimestamp(FIREHOUSES_JSON.stat().st_mtime).isoformat()})

    from sizeup.geo.normalize import street_aliases
    all_street_aliases = {a for k in streets for a, _t, _p in street_aliases(k)}
    landmarks = []
    seen_lm = set()
    for name, addr, lat, lon, aliases in load_firehouses():
        aliases = [norm_phrase(a) for a in aliases]
        aliases = sorted({a for a in aliases if usable_alias(a, all_street_aliases)})
        landmarks.append((name, "fire_station", addr, muni_at(lat, lon), lat, lon,
                          "firehouses.json", aliases))
    for lm in raw_lm + osm_lm:
        name, aliases = clean_landmark_name(lm["name"], lm["category"])
        aliases = sorted({a for a in aliases if usable_alias(a, all_street_aliases)
                          and not (" " not in a and a in COMMON_SINGLE_WORDS)})
        if not aliases:
            continue
        dedup = (norm_phrase(name), round(lm["lat"], 3), round(lm["lon"], 3))
        if dedup in seen_lm:
            continue
        seen_lm.add(dedup)
        muni = muni_at(lm["lat"], lm["lon"], 150.0)
        if not muni:
            continue                       # outside the covered municipalities
        landmarks.append((name, lm["category"], lm["address"], muni, lm["lat"], lm["lon"],
                          lm["source"], aliases))

    # ------------------------------------------------------------------ write
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO streets VALUES (?,?,?)",
                   [(key_ids[k], k, streets[k]) for k in sorted(streets)])
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)",
                   [(key_ids[k], n, s, m, round(la, 6), round(lo, 6), c)
                    for k, n, s, m, la, lo, c in sorted(addr_rows, key=lambda r: (r[0], r[3], r[1], r[2]))])
    line_rows = []
    for key, _disp, pts, muni in lines:
        coords = json.dumps([[round(lo, 6), round(la, 6)] for lo, la in pts], separators=(",", ":"))
        line_rows.append((key_ids[key], muni, coords))
    db.executemany("INSERT INTO lines VALUES (?,?,?)", line_rows)
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)",
                   [(a, b, round(la, 6), round(lo, 6), m or muni_at(la, lo))
                    for a, b, la, lo, m in inters])
    db.executemany("INSERT INTO landmarks (name, category, address, muni, lat, lon, source, aliases) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   [(n, c, a, m, round(la, 6), round(lo, 6), s, "|".join(al))
                    for n, c, a, m, la, lo, s, al in landmarks])
    counts = {
        "address_points_raw": {"NYS": len(ny_recs), "NAD (CT)": len(ct_recs)},
        "address_points_by_polygon_check": dict(ny_verify, **ct_verify),
        "address_tagging": tag_stats,
        "addresses": len(addr_rows),
        "streets": len(streets),
        "streets_with_addresses": len({r[0] for r in addr_rows}),
        "line_parts": len(line_rows),
        "intersections": len(inters),
        "landmarks": len(landmarks),
        "addresses_by_muni": {m: c for m, c in sorted(
            collections.Counter(r[3] for r in addr_rows).items(),
            key=lambda kv: [x[0] for x in MUNICIPALITIES].index(kv[0]))},
        "streets_by_muni": {m: len({r[0] for r in addr_rows if r[3] == m}) for m, *_ in MUNICIPALITIES},
    }
    meta = {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "area": json.dumps([round(float(v), 4) for v in area]),
        "municipalities": json.dumps([
            {"label": m, "tiger": name, "state": "NY" if st == "36" else "CT",
             "bbox": [round(float(v), 4) for v in bounds.bboxes[m]]}
            for m, _lyr, st, name in MUNICIPALITIES]),
        "sources": json.dumps(sources),
        "counts": json.dumps(counts),
        "notes": ("Coordinates WGS84 (TIGER/NAD NAD83 treated as WGS84, <2 m). Coverage = the TIGER "
                  "polygons of the municipalities listed under 'municipalities'; every address, line "
                  "part, intersection and landmark carries the label of the polygon containing it "
                  f"(an address keeps its own 9-1-1 municipality when within {ATTR_TOLERANCE_M:.0f} m "
                  "of that polygon). Addresses are NYS (NY) and NAD / CT 9-1-1 (CT) address points "
                  "collapsed per (street, number, suffix, municipality); CT points are mostly parcel "
                  "centroids. Line parts are split at municipal boundaries. OSM-derived landmark rows "
                  "(source='osm') are ODbL: (c) OpenStreetMap contributors."),
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(args.out)
    print(json.dumps(counts, indent=1), file=sys.stderr)
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.2f} MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
