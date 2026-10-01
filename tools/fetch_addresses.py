#!/usr/bin/env python3
"""Build the offline gazetteer (data/gazetteer.sqlite) for the sizeup geocoder.

Downloads, for the Port Chester / Rye Brook area (plus bordering parts of the
City of Rye and Harrison inside the bounding box):

* NYS Address Points (NYS ITS GIS, CC0) via the NYS GIS REST service
* US Census TIGER/Line 2025 ADDRFEAT + ROADS for Westchester (public domain):
  street centrelines, used for street geometry and intersections
* Westchester County community facilities (county GIS REST): schools,
  hospitals, houses of worship, libraries, parks, police, post offices, ...
* OpenStreetMap named amenities/buildings via Overpass (ODbL) -- optional; if
  every Overpass mirror fails, the OSM landmarks of the previous build are kept
* the five PCFD / Rye Brook firehouses from data/firehouses.json

Run:  .venv/bin/python tools/fetch_addresses.py
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

from sizeup.geo.gazetteer import SCHEMA  # noqa: E402
from sizeup.geo.normalize import BOUNDARY, street_key, tokenize  # noqa: E402
from sizeup.geo.shapefile import iter_shapes, read_dbf  # noqa: E402

DB_PATH = ROOT / "data" / "gazetteer.sqlite"
USER_AGENT = "sizeup-gazetteer/0.1 (Port Chester FD dispatch display; offline geocoder build)"

# south, west, north, east
BBOX = (40.98, -73.72, 41.06, -73.64)
CORE_MUNIS = ("Port Chester", "Rye Brook")

NYS_AP_URL = "https://nysgeohub.ny.gov/arcgis/rest/services/Address/NYS_Address_Points/MapServer/0/query"
TIGER_ADDRFEAT_URL = "https://www2.census.gov/geo/tiger/TIGER2025/ADDRFEAT/tl_2025_36119_addrfeat.zip"
TIGER_ROADS_URL = "https://www2.census.gov/geo/tiger/TIGER2025/ROADS/tl_2025_36119_roads.zip"
COUNTY_URL = "https://giswww.westchestergov.com/arcgis/rest/services/DataHub_CommunityFacilities/MapServer"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

AP_FIELDS = (
    "OBJECTID,AddressNumber,SuffixAddressNumber,PreModifier,PreDirectional,PreType,"
    "StreetName,PostType,PostDirectional,CompleteStreetName,IncorporatedMunicipality,"
    "ZipName,PrimaryPoint,Status"
)

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


def arcgis_query(url: str, params: dict, page: int = 2000) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        p = dict(params, f="json", outSR="4326", resultOffset=offset, resultRecordCount=page)
        d = json.loads(http_get(url, p))
        if "error" in d:
            raise RuntimeError(f"ArcGIS error from {url}: {d['error']}")
        feats = d.get("features", [])
        out.extend(feats)
        if not feats or (not d.get("exceededTransferLimit") and len(feats) < page):
            return out
        offset += len(feats)


def bbox_params(bbox=BBOX) -> dict:
    s, w, n, e = bbox
    return {"geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}


# --------------------------------------------------------------------------- sources

def fetch_address_points() -> list[dict]:
    feats: dict[int, dict] = {}
    queries = [
        {"where": "IncorporatedMunicipality IN ('Port Chester','Rye Brook')",
         "orderByFields": "OBJECTID"},
        dict({"where": "1=1", "orderByFields": "OBJECTID"}, **bbox_params()),
    ]
    for q in queries:
        for f in arcgis_query(NYS_AP_URL, dict(q, outFields=AP_FIELDS)):
            feats[f["attributes"]["OBJECTID"]] = f
    print(f"  address points: {len(feats)} raw", file=sys.stderr)
    return list(feats.values())


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
CT_PLACES = {"Greenwich", "Byram", "Glenville", "Old Greenwich", "Cos Cob", "Riverside"}
# one-word OSM names that are ordinary words in a dispatch transcript
COMMON_SINGLE_WORDS = {"signature", "legend", "aurora", "reclaim", "subway", "target",
                       "courtyard", "verizon", "mastercard", "signature", "sportech"}
OSM_MAN_MADE_OK = {"wastewater_plant", "water_works", "water_tower", "tower", "pier", "lighthouse"}


def fetch_osm_landmarks(bbox) -> tuple[list[dict], str]:
    s, w, n, e = bbox
    q = (f'[out:json][timeout:120][bbox:{s},{w},{n},{e}];'
         f'(nwr["name"][~"^({OSM_KEYS})$"~"."];);out center tags;')
    last = None
    for url in OVERPASS_URLS:
        try:
            raw = http_get(url, data={"data": q}, timeout=180, retries=2)
            d = json.loads(raw)
            used = url
            break
        except Exception as exc:  # noqa: BLE001 -- any failure: try next mirror
            last = exc
            print(f"  overpass {url}: {exc}", file=sys.stderr)
    else:
        raise RuntimeError(f"all Overpass mirrors failed: {last}")
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
        ct = (t.get("addr:state", "").upper() in {"CT", "CONNECTICUT"}
              or t.get("addr:city", "") in CT_PLACES or t.get("addr:postcode", "").startswith("068"))
        out.append({"name": name, "category": kind, "address": addr, "lat": lat, "lon": lon,
                    "source": "osm", "muni": "Greenwich" if ct else ""})
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


def to_xy(lat, lon):
    return ((np.asarray(lon) - BBOX[1]) * M_PER_DEG_LON, (np.asarray(lat) - BBOX[0]) * M_PER_DEG_LAT)


def build_addresses(feats: list[dict]):
    """-> streets {key: display}, rows [(key, number, suffix, muni, lat, lon, units)]"""
    names: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    groups: dict[tuple, list] = collections.defaultdict(list)
    for f in feats:
        a = f["attributes"]
        g = f.get("geometry") or {}
        muni = (a.get("IncorporatedMunicipality") or "").strip()
        if "x" not in g or a.get("AddressNumber") is None or not muni or muni == "Outside County":
            continue
        if (a.get("Status") or "Active") != "Active":
            continue
        disp = display_street(a)
        if not disp:
            continue
        key = street_key(disp)
        names[key][disp] += 1
        suffix = (a.get("SuffixAddressNumber") or "").strip()
        groups[(key, int(a["AddressNumber"]), suffix, muni)].append(
            (a.get("PrimaryPoint") == "Y", g["y"], g["x"]))
    rows = []
    for (key, num, suffix, muni), pts in groups.items():
        prim = [p for p in pts if p[0]] or pts
        lat = sum(p[1] for p in prim) / len(prim)
        lon = sum(p[2] for p in prim) / len(prim)
        rows.append((key, num, suffix, muni, lat, lon, len(pts)))
    streets = {k: c.most_common(1)[0][0] for k, c in names.items()}
    return streets, rows


def clip_runs(pts: list[tuple[float, float]], area) -> list[list[tuple[float, float]]]:
    """Split a polyline (lon, lat) into runs inside the area, one vertex of overhang each side."""
    s, w, n, e = area
    inside = [w <= x <= e and s <= y <= n for x, y in pts]
    runs, cur = [], []
    for i, p in enumerate(pts):
        near = inside[i] or (i > 0 and inside[i - 1]) or (i + 1 < len(pts) and inside[i + 1])
        if near:
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2]


def build_lines(addrfeat, roads, area):
    """-> lines [(key, display, [(lon, lat), ...])], edge_keys {tlid: {keys}}"""
    s, w, n, e = area
    lines = []
    seen = set()
    af_keys = set()
    for rec, bbox, parts in addrfeat:
        name = rec.get("FULLNAME", "")
        if not name or not bbox or bbox[2] < w or bbox[0] > e or bbox[3] < s or bbox[1] > n:
            continue
        key = street_key(name)
        if (rec.get("TLID"), key) in seen:
            continue
        seen.add((rec.get("TLID"), key))
        af_keys.add(key)
        for part in parts:
            for run in clip_runs(part, area):
                lines.append((key, tiger_display(name), run))
    for rec, bbox, parts in roads:
        name = rec.get("FULLNAME", "")
        if not name or not bbox or bbox[2] < w or bbox[0] > e or bbox[3] < s or bbox[1] > n:
            continue
        key = street_key(name)
        is_route = key.startswith(("us rte", "state rte", "state hwy", "co rd", "co hwy"))
        # ROADS fills in un-addressed edges (e.g. the end of a street at a junction);
        # route numbers ("US Rte 1") duplicate the local names of the same edges.
        if is_route and rec.get("MTFCC") != "S1100":
            continue
        for part in parts:
            for run in clip_runs(part, area):
                lines.append((key, tiger_display(name), run))
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
    """-> [(id_a, id_b, lat, lon)] for every place two differently named streets meet."""
    segs = []
    for key, _disp, pts in lines:
        sid = key_ids[key]
        xs, ys = to_xy([p[1] for p in pts], [p[0] for p in pts])
        for i in range(len(pts) - 1):
            segs.append((sid, (float(xs[i]), float(ys[i]), float(xs[i + 1]), float(ys[i + 1]))))
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
                    hits[pair].append(r[1])
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
            out.append((pair[0], pair[1], BBOX[0] + y / M_PER_DEG_LAT, BBOX[1] + x / M_PER_DEG_LON))
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

    print("NYS address points ...", file=sys.stderr)
    ap_feats = fetch_address_points()
    streets, addr_rows = build_addresses(ap_feats)
    sources.append({"name": "NYS Address Points (Westchester)", "url": NYS_AP_URL,
                    "license": "CC0 1.0 (NYS ITS Geospatial Services)", "fetched": today,
                    "records": len(ap_feats)})

    lats = [r[4] for r in addr_rows]
    lons = [r[5] for r in addr_rows]
    margin = 0.004
    area = (min(min(lats), BBOX[0]) - margin, min(min(lons), BBOX[1]) - margin,
            max(max(lats), BBOX[2]) + margin, max(max(lons), BBOX[3]) + margin)

    print("TIGER/Line ADDRFEAT + ROADS ...", file=sys.stderr)
    addrfeat = read_tiger(TIGER_ADDRFEAT_URL)
    roads = read_tiger(TIGER_ROADS_URL)
    lines = build_lines(addrfeat, roads, area)
    for url in (TIGER_ADDRFEAT_URL, TIGER_ROADS_URL):
        sources.append({"name": "US Census TIGER/Line 2025 " + Path(url).stem, "url": url,
                        "license": "Public domain (US Census Bureau)", "fetched": today})
    for key, disp, _ in lines:
        streets.setdefault(key, disp)
    key_ids = {k: i + 1 for i, k in enumerate(sorted(streets))}

    nearest_muni = nearest_muni_fn(addr_rows)

    print("intersections ...", file=sys.stderr)
    inters = compute_intersections(lines, key_ids)

    print("landmarks ...", file=sys.stderr)
    raw_lm = fetch_county_landmarks(area)
    sources.append({"name": "Westchester County GIS DataHub Community Facilities", "url": COUNTY_URL,
                    "license": "Westchester County GIS public REST service "
                               "(no licence stated; attribute the county)",
                    "fetched": today})
    osm_lm: list[dict] = []
    osm_source = None
    if not args.no_osm:
        try:
            osm_lm, osm_url = fetch_osm_landmarks(area)
            osm_source = {"name": "OpenStreetMap via Overpass API", "url": osm_url,
                          "license": "ODbL 1.0, (c) OpenStreetMap contributors", "fetched": today}
        except RuntimeError as exc:
            print(f"  OSM skipped: {exc}", file=sys.stderr)
    if not osm_lm and args.out.exists():
        old = sqlite3.connect(args.out)
        try:
            osm_lm = [dict(zip(("name", "category", "address", "lat", "lon", "source", "muni"), r))
                      for r in old.execute("SELECT name, category, address, lat, lon, source, muni "
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
        landmarks.append((name, "fire_station", addr, nearest_muni(lat, lon, 300.0), lat, lon,
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
        muni = lm.get("muni") or nearest_muni(lm["lat"], lm["lon"], 300.0)
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
    for key, _disp, pts in lines:
        mid = pts[len(pts) // 2]
        coords = json.dumps([[round(lo, 6), round(la, 6)] for lo, la in pts], separators=(",", ":"))
        line_rows.append((key_ids[key], nearest_muni(mid[1], mid[0], 400.0), coords))
    db.executemany("INSERT INTO lines VALUES (?,?,?)", line_rows)
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)",
                   [(a, b, round(la, 6), round(lo, 6), nearest_muni(la, lo, 300.0))
                    for a, b, la, lo in inters])
    db.executemany("INSERT INTO landmarks (name, category, address, muni, lat, lon, source, aliases) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   [(n, c, a, m, round(la, 6), round(lo, 6), s, "|".join(al))
                    for n, c, a, m, la, lo, s, al in landmarks])
    counts = {
        "address_points_raw": len(ap_feats),
        "addresses": len(addr_rows),
        "streets": len(streets),
        "streets_with_addresses": len({r[0] for r in addr_rows}),
        "line_parts": len(line_rows),
        "intersections": len(inters),
        "landmarks": len(landmarks),
        "addresses_by_muni": dict(collections.Counter(r[3] for r in addr_rows)),
    }
    meta = {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "bbox": json.dumps(BBOX),
        "area": json.dumps([round(v, 4) for v in area]),
        "sources": json.dumps(sources),
        "counts": json.dumps(counts),
        "notes": ("Coordinates WGS84 (TIGER NAD83 treated as WGS84, <2 m). Addresses are NYS address "
                  "points collapsed per (street, number, suffix, municipality). OSM-derived landmark "
                  "rows (source='osm') are ODbL: (c) OpenStreetMap contributors."),
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
