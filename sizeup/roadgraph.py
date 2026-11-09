"""A routable road graph for the response area, and routing on it (GitHub #360).

User, 2026-11-09: "can we also show the path from rig to the call, and what path they might be following, and have
it update as they go different directions? just to show the probable path".

Source: OpenStreetMap (© OpenStreetMap contributors, ODbL; the maps already credit it), one Overpass query for the
drivable ways in AREA: the firehouses in data/firehouses.json with RING_KM around them (Port Chester, Rye Brook and
the mutual-aid ring: Rye, Harrison, Greenwich, Mamaroneck, White Plains, Purchase, Stamford). Asked politely: our
User-Agent (sizeup.USER_AGENT names the project and a mailbox), one request, monthly by default (Settings > Data
sources, in the quiet hours), backing off on 429 / 5xx.

Build (build()): the OSM ways become a compact graph: a node at every junction or way end, an edge between two, its
shape points kept for drawing, its length, road class (CLASSES), one-way (normalised a -> b) and name. Kept as JSON
(CACHE_NAME in the state dir; sizeup/cachesync.py keeps a gzip copy in S3 like the GIS layers). Its size and
timings, measured: the comment at the foot of the constants below.

Graph: the cache file read into flat arrays (no per-edge objects), a 200 m grid of edges for snapping, and:

    snap(lon, lat)        the nearest edge point (motorways need to be clearly nearer: a house beside I-95 is on its street)
    route(a, b, ...)      A* from a point to a point: [lon, lat] coords, metres, seconds (pure Python)
    to_one(origins, b)    one reverse Dijkstra from b: seconds and metres from many points (the recommendation)
    penalties(items, now) the edges near road closures in effect now: blocked (a full or ramp closure) or slowed

Rules: one-ways are honoured, except a wrong-way final stretch of at most wrongway_m (Settings > Dispatch) on the
last block, at WRONGWAY_MPH, when it saves a loop around the block. Speeds by road class (highway / arterial / local,
Settings > Dispatch) times the emergency factor; the partial edges at either end are never blocked (the rig is
there, the call is there). A rig with a heading pays UTURN_S to start the other way along its street.

Everything shown from this is an estimate: no traffic, no signals, no turn costs.
"""

from __future__ import annotations

import gzip
import heapq
import json
import math
import re
import threading
import time
from array import array
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sizeup import USER_AGENT

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SOURCE = "OpenStreetMap (Overpass)"
LINK = "https://www.openstreetmap.org/copyright"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
CACHE_NAME = "roadgraph.json"         # in the state dir (sizeup/cachesync.py FILES)
VERSION = 1
RING_KM = 12.0                        # around the firehouses: the mutual-aid ring
TIMEOUT_S = 240.0
MAX_BYTES = 200 * 1024 * 1024
BACKOFF_S = (900.0, 6 * 3600.0)
# OSM highway values kept, in class order (an edge's class is its index here)
CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
           "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "living_street", "service")
HIGHWAY = {0, 1, 7, 8}                # snapped to only when clearly nearer (MOTORWAY_SNAP_M)
GROUP = {"motorway": ("highway", 1.0), "trunk": ("highway", 0.9), "primary": ("arterial", 1.0),
         "secondary": ("arterial", 0.95), "tertiary": ("arterial", 0.85), "unclassified": ("local", 1.0),
         "residential": ("local", 1.0), "motorway_link": ("highway", 0.6), "trunk_link": ("highway", 0.6),
         "primary_link": ("arterial", 0.75), "secondary_link": ("arterial", 0.75), "tertiary_link": ("arterial", 0.75),
         "living_street": ("local", 0.5), "service": ("local", 0.5)}
SERVICE_SKIP = ("parking_aisle", "drive-through", "driveway")
MPS_PER_MPH = 0.44704
WRONGWAY_MPH = 10.0                   # the wrong-way final stretch: slowly
UTURN_S = 20.0                        # starting against the rig's heading
SNAP_M = 300.0                        # a point farther than this from every road: no route
MOTORWAY_SNAP_M = 40.0                # a motorway / trunk must be this much nearer than another road to be snapped to
CELL_M = 200.0
MAX_POPS = 600_000                    # A* gives up (a broken graph never hangs the worker)
CLOSURE_M = 25.0                      # an edge this close to a closure's line is on it
END_BLOCKED = 8.0                     # a closed block the rig or the call is on: this much slower (never shut)
SLOW = {"lanes": 1.5, "alternating": 1.6, "detour": 1.3}   # a partial closure: the edge this much slower
# Measured 2026-11-09 (12 km ring, bbox -73.819 40.888 -73.517 41.144, an M-series Mac): Overpass answered 28 MB in
# 56 s; built into 41,623 nodes and 52,744 edges in 0.5 s, a 5.6 MB file (1.7 MB gzipped in S3); read into a Graph in
# 0.4 s, ~20 MB held (45 MB peak while reading). A route (a firehouse to a point within ~6 km): median 2 ms, p95 6 ms,
# max 50 ms; the closures in effect as penalties: ~5 ms.


def area_bbox(points: list, ring_km: float = RING_KM) -> tuple[float, float, float, float]:
    """(west, south, east, north) around [lon, lat] points, ring_km on every side."""
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    lat0 = (min(lats) + max(lats)) / 2
    dlat = ring_km / 110.574
    dlon = ring_km / (111.320 * math.cos(math.radians(lat0)))
    return (round(min(lons) - dlon, 4), round(min(lats) - dlat, 4), round(max(lons) + dlon, 4), round(max(lats) + dlat, 4))


def overpass_query(bbox) -> str:
    w, s, e, n = bbox
    main = "|".join(c for c in CLASSES if c != "service")
    return (f'[out:json][timeout:{int(TIMEOUT_S - 30)}][bbox:{s},{w},{n},{e}];'
            f'(way["highway"~"^({main})$"]["area"!="yes"]["access"!="no"];'
            f'way["highway"="service"]["area"!="yes"]["service"!~"^({"|".join(SERVICE_SKIP)})$"]["access"!~"^(no|private)$"];);'
            'out body geom qt;')


def oneway_of(tags: dict, cls: str) -> int:
    """1: the way's direction only; -1: against it; 0: both."""
    ow = str(tags.get("oneway", "")).lower()
    if ow in ("yes", "true", "1"):
        return 1
    if ow == "-1":
        return -1
    if ow in ("no", "false", "0", "reversible", "alternating"):
        return 0
    if cls == "motorway" or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


def _e6(v: float) -> int:
    return int(round(v * 1e6))


def build(doc: dict, bbox=None, at: float | None = None) -> dict:
    """An Overpass answer (out body geom) -> the compact graph (the cache file's shape):

        {v, source, at, osm_base, bbox, classes, names: [...],
         nodes: [lon e6, lat e6, ...]                   one pair per junction / end
         edges: [a, b, class, oneway (1: a -> b only), name index, ...]   five ints per edge
         shape: [[lon e6, lat e6, ...] | [], ...]       each edge's points between a and b}"""
    ways = []
    uses: dict[int, int] = {}
    for el in doc.get("elements") or []:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        cls = tags.get("highway")
        ids, geom = el.get("nodes") or [], el.get("geometry") or []
        if cls not in CLASSES or len(ids) < 2 or len(ids) != len(geom):
            continue
        if cls == "service" and tags.get("service") in SERVICE_SKIP:
            continue
        pts = []
        for g in geom:
            if not isinstance(g, dict) or g.get("lon") is None or g.get("lat") is None:
                pts = []
                break
            pts.append((_e6(g["lon"]), _e6(g["lat"])))
        if not pts:
            continue
        ways.append((ids, pts, CLASSES.index(cls), oneway_of(tags, cls), "; ".join(str(tags[k]) for k in ("name", "ref") if tags.get(k))[:80]))
        for k, nid in enumerate(ids):
            uses[nid] = uses.get(nid, 0) + (2 if k in (0, len(ids) - 1) else 1)
    node_ix: dict[int, int] = {}
    nodes: list[int] = []
    names: list[str] = [""]
    name_ix = {"": 0}
    edges: list[int] = []
    shape: list[list[int]] = []

    def node(nid: int, p) -> int:
        i = node_ix.get(nid)
        if i is None:
            i = node_ix[nid] = len(nodes) // 2
            nodes.extend(p)
        return i
    for ids, pts, cls, ow, name in ways:
        ni = name_ix.get(name)
        if ni is None:
            ni = name_ix[name] = len(names)
            names.append(name)
        start, mid = 0, []
        for k in range(1, len(ids)):
            if k < len(ids) - 1 and uses.get(ids[k], 0) < 2:
                mid.extend(pts[k])
                continue
            if ids[k] == ids[start] and not mid:
                start = k                                    # a repeated node: nothing between
                continue
            a, b = node(ids[start], pts[start]), node(ids[k], pts[k])
            sh = mid
            if ow == -1:                                     # normalised: a -> b is the way it goes
                a, b = b, a
                sh = [v for j in range(len(mid) - 2, -1, -2) for v in mid[j:j + 2]]
            edges.extend((a, b, cls, 1 if ow else 0, ni))
            shape.append(sh)
            start, mid = k, []
    osm = doc.get("osm3s") or {}
    return {"v": VERSION, "source": SOURCE, "at": time.time() if at is None else at,
            "osm_base": str(osm.get("timestamp_osm_base") or ""), "bbox": list(bbox) if bbox else None,
            "classes": list(CLASSES), "names": names, "nodes": nodes, "edges": edges, "shape": shape}


def street_key(name: str) -> str:
    from sizeup.roadprojects import street_key as sk
    return sk(name)


class Graph:
    """The compact graph in flat arrays, ready to route on (thread-safe for reads: nothing here changes)."""

    def __init__(self, g: dict):
        if g.get("v") != VERSION or list(g.get("classes") or []) != list(CLASSES):
            raise ValueError("road graph: another version")
        nodes, edges = g["nodes"], g["edges"]
        self.meta = {k: g.get(k) for k in ("source", "at", "osm_base", "bbox")}
        self.names: list[str] = list(g.get("names") or [""])
        n = len(nodes) // 2
        self.lon = array("d", (v / 1e6 for v in nodes[0::2]))
        self.lat = array("d", (v / 1e6 for v in nodes[1::2]))
        self.n = n
        lat0 = (min(self.lat) + max(self.lat)) / 2 if n else 41.0
        lon0 = (min(self.lon) + max(self.lon)) / 2 if n else -73.7
        self.lon0, self.lat0 = lon0, lat0
        self.kx, self.ky = 111_320.0 * math.cos(math.radians(lat0)), 110_574.0
        self.x = array("d", ((v - lon0) * self.kx for v in self.lon))
        self.y = array("d", ((v - lat0) * self.ky for v in self.lat))
        m = len(edges) // 5
        self.m = m
        self.ea, self.eb = array("i", edges[0::5]), array("i", edges[1::5])
        self.ecls, self.eow = bytes(edges[2::5]), bytes(edges[3::5])
        self.ename = array("i", edges[4::5])
        # each edge's points between its ends, flat: sh_off[e] .. sh_off[e + 1]
        self.sh_off = array("i", [0])
        self.sh_lon, self.sh_lat = array("d"), array("d")
        for sh in g.get("shape") or [[]] * m:
            self.sh_lon.extend(v / 1e6 for v in sh[0::2])
            self.sh_lat.extend(v / 1e6 for v in sh[1::2])
            self.sh_off.append(len(self.sh_lon))
        if len(self.sh_off) != m + 1:
            raise ValueError("road graph: shapes and edges differ")
        self.elen = array("d", (self._length(e) for e in range(m)))
        self.adj: list[list[int]] = [[] for _ in range(n)]
        for e in range(m):
            self.adj[self.ea[e]].append(e * 2)                # a -> b, from a
            self.adj[self.eb[e]].append(e * 2 + 1)            # b -> a, from b (legal when two-way)
        self.grid: dict[tuple[int, int], list[int]] = {}
        for e in range(m):
            cells = set()
            pts = self.xy(e)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                for cx in range(int(min(x1, x2) // CELL_M), int(max(x1, x2) // CELL_M) + 1):
                    for cy in range(int(min(y1, y2) // CELL_M), int(max(y1, y2) // CELL_M) + 1):
                        cells.add((cx, cy))
            for c in cells:
                self.grid.setdefault(c, []).append(e)

    # -- geometry ------------------------------------------------------------------------------------------

    def ll(self, e: int) -> list[tuple[float, float]]:
        """The edge's [lon, lat] points, a to b."""
        a, b, s, t = self.ea[e], self.eb[e], self.sh_off[e], self.sh_off[e + 1]
        return [(self.lon[a], self.lat[a]), *zip(self.sh_lon[s:t], self.sh_lat[s:t]), (self.lon[b], self.lat[b])]

    def xy(self, e: int) -> list[tuple[float, float]]:
        lon0, lat0, kx, ky = self.lon0, self.lat0, self.kx, self.ky
        return [((lo - lon0) * kx, (la - lat0) * ky) for lo, la in self.ll(e)]

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def _length(self, e: int) -> float:
        pts = self.xy(e)
        return sum(math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(pts, pts[1:])) or 0.1

    def name(self, e: int) -> str:
        return self.names[self.ename[e]] if 0 <= self.ename[e] < len(self.names) else ""

    def street_keys(self, e: int) -> set[str]:
        """The edge's name and ref as roadprojects.street_key()s ("Westchester Avenue; NY 120" -> {...})."""
        return {street_key(x) for x in self.name(e).split(";") if x.strip()}

    def cls(self, e: int) -> str:
        return CLASSES[self.ecls[e]]

    @staticmethod
    def _onto(pts, px: float, py: float) -> tuple[float, float, int, float]:
        """(distance, metres along, segment, fraction) of the nearest point of a polyline to (px, py)."""
        best = (float("inf"), 0.0, 0, 0.0)
        run = 0.0
        for i in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[i], pts[i + 1]
            dx, dy = x2 - x1, y2 - y1
            ll = dx * dx + dy * dy
            t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ll))
            d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
            seg = math.sqrt(ll)
            if d < best[0]:
                best = (d, run + t * seg, i, t)
            run += seg
        return best

    def near_edges(self, x: float, y: float, r: float) -> set[int]:
        out: set[int] = set()
        for cx in range(int((x - r) // CELL_M), int((x + r) // CELL_M) + 1):
            for cy in range(int((y - r) // CELL_M), int((y + r) // CELL_M) + 1):
                out.update(self.grid.get((cx, cy), ()))
        return out

    def snap(self, lon: float, lat: float, max_m: float = SNAP_M) -> dict | None:
        """The nearest point on a road: {edge, off (m from a), d (m away), len, lon, lat}; None when none within
        max_m. A motorway or trunk (or its ramps) only when it is MOTORWAY_SNAP_M nearer than any other road."""
        x, y = self.to_xy(lon, lat)
        best = None
        for r in (CELL_M / 2, max_m):
            for e in self.near_edges(x, y, r):
                pts = self.xy(e)
                d, off, i, t = self._onto(pts, x, y)
                if d > max_m:
                    continue
                score = d + (MOTORWAY_SNAP_M if self.ecls[e] in HIGHWAY else 0.0)
                if best is None or score < best[0]:
                    best = (score, e, d, off, i, t)
            if best is not None and best[0] <= r:
                break
        if best is None:
            return None
        _, e, d, off, i, t = best
        pts = self.ll(e)
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        return {"edge": e, "off": min(off, self.elen[e]), "d": d, "len": self.elen[e],
                "lon": x1 + (x2 - x1) * t, "lat": y1 + (y2 - y1) * t, "seg": i}

    def bearing(self, e: int, seg: int) -> float:
        """Degrees true of the edge's segment, a -> b."""
        pts = self.ll(e)
        (lo1, la1), (lo2, la2) = pts[seg], pts[min(seg + 1, len(pts) - 1)]
        return math.degrees(math.atan2((lo2 - lo1) * self.kx, (la2 - la1) * self.ky)) % 360

    # -- costs ---------------------------------------------------------------------------------------------

    @staticmethod
    def spm(params) -> list[float]:
        """Seconds per metre by class index: the class's group speed (params.highway_mph / arterial_mph /
        local_mph) times its share (GROUP), times the emergency factor."""
        f = getattr(params, "emergency_factor", 1.0) or 1.0
        out = []
        for c in CLASSES:
            grp, share = GROUP[c]
            mph = getattr(params, f"{grp}_mph", {"highway": 50.0, "arterial": 28.0, "local": 20.0}[grp])
            out.append(1.0 / max(0.5, mph * share * f * MPS_PER_MPH))
        return out

    def penalties(self, items: list[dict], now: float) -> dict[int, float]:
        """edge -> factor (inf: blocked) for the road closures and roadwork in effect now (sizeup/roadprojects.py
        records): a full closure blocks the edges along it (a ramp closure, the ramps); a lane or alternating
        closure, or a detour, slows them (SLOW). Only dated items in effect now (roadprojects.in_effect). An edge
        is along a closure when its road is the closure's road (street_key) and it is within CLOSURE_M of its line,
        or, the closure naming no road, when two of the line's points 15 m apart are that close to it."""
        from sizeup import roadprojects as rp
        out: dict[int, float] = {}
        for r in items or ():
            how = r.get("closure") or ""
            factor = math.inf if how in ("full", "ramp") else SLOW.get(how)
            if factor is None and r.get("kind") == "closure" and not how:
                factor = math.inf
            if factor is None:
                continue
            try:
                if rp.in_effect(r, now, now) is None:
                    continue
            except Exception:  # noqa: BLE001 - a record we can't read is not a closure
                continue
            road = street_key(r.get("road") or "")
            hits: dict[int, int] = {}
            lines = rp._lines(r.get("geometry"))
            need = 1 if all(len(ln) == 1 for ln in lines) else 2     # a line: along it, not only touching its end
            for line in lines:
                for px, py in self._sample([self.to_xy(lo, la) for lo, la in line], 15.0):
                    for e in self.near_edges(px, py, CLOSURE_M):
                        if how == "ramp" and self.ecls[e] not in (7, 8, 9, 10, 11):
                            continue
                        if self._onto(self.xy(e), px, py)[0] <= CLOSURE_M:
                            hits[e] = hits.get(e, 0) + 1
            for e, k in hits.items():
                ok = k >= need and (road in self.street_keys(e) if road else k >= 2)
                if ok:
                    out[e] = max(out.get(e, 1.0), factor)
        return out

    @staticmethod
    def _sample(pts: list, step: float) -> list:
        if len(pts) < 2:
            return list(pts)
        out = [pts[0]]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            d = math.hypot(x2 - x1, y2 - y1)
            k = max(1, int(d // step))
            out.extend((x1 + (x2 - x1) * j / k, y1 + (y2 - y1) * j / k) for j in range(1, k + 1))
        return out

    # -- routing -------------------------------------------------------------------------------------------

    def _starts(self, s: dict, spm: list, pen: dict, heading: float | None) -> list[tuple[int, float, float, int]]:
        """Where a rig at snap s can drive to first: [(node, seconds, metres, direction 0: toward b | 1: toward a)]."""
        e, off, L = s["edge"], s["off"], s["len"]
        k = spm[self.ecls[e]] * _end(pen.get(e, 1.0))
        fwd = rev = 0.0
        if heading is not None:
            diff = abs((heading - self.bearing(e, s["seg"]) + 180) % 360 - 180)
            if diff > 100:
                fwd = UTURN_S
            elif diff < 80:
                rev = UTURN_S
        out = [(self.eb[e], (L - off) * k + fwd, L - off, 0)]
        if not self.eow[e]:
            out.append((self.ea[e], off * k + rev, off, 1))
        return out

    def _goals(self, t: dict, spm: list, pen: dict, wrongway_m: float) -> dict[int, tuple[float, float, list]]:
        """node -> (seconds, metres, the way on to the call [(edge, direction), ...]): its own edge's ends, and the
        nodes from which a wrong-way final stretch of at most wrongway_m reaches it."""
        e, off, L = t["edge"], t["off"], t["len"]
        k = spm[self.ecls[e]] * _end(pen.get(e, 1.0))
        ww = 1.0 / (WRONGWAY_MPH * MPS_PER_MPH)
        goals: dict[int, tuple[float, float, list, float]] = {}

        def put(n, s, m, way, w):
            if n not in goals or s < goals[n][0]:
                goals[n] = (s, m, way, w)
        put(self.ea[e], off * k, off, [(e, 0)], 0.0)                  # a -> along the edge (always legal)
        if not self.eow[e]:
            put(self.eb[e], (L - off) * k, L - off, [(e, 1)], 0.0)
        elif L - off <= wrongway_m:
            put(self.eb[e], (L - off) * ww, L - off, [(e, 1)], L - off)
        # a wrong-way stretch on: nodes whose one-way edge leads into a goal node, driven backwards
        q = [(v[0], n) for n, v in goals.items()]
        heapq.heapify(q)
        while q:
            s, n = heapq.heappop(q)
            if s > goals[n][0]:
                continue
            _, m, way, w = goals[n]
            for code in self.adj[n]:
                f = code >> 1
                if code & 1 or not self.eow[f] or f == e or pen.get(f, 1.0) == math.inf:
                    continue                                          # only one-way edges leaving n (a = n), not closed
                w2 = w + self.elen[f]
                if w2 > wrongway_m:
                    continue
                o = self.eb[f]
                s2 = s + self.elen[f] * ww
                if o not in goals or s2 < goals[o][0]:
                    goals[o] = (s2, m + self.elen[f], [(f, 1)] + way, w2)
                    heapq.heappush(q, (s2, o))
        return {n: (v[0], v[1], v[2]) for n, v in goals.items()}

    def _direct(self, s: dict, t: dict, spm: list, pen: dict, wrongway_m: float) -> tuple[float, float, int] | None:
        """Both on one edge: along it, if that way is legal (or wrong-way, short enough). (s, m, direction)."""
        if s["edge"] != t["edge"]:
            return None
        e = s["edge"]
        d = t["off"] - s["off"]
        k = spm[self.ecls[e]] * _end(pen.get(e, 1.0))
        if d >= 0 or not self.eow[e]:
            return abs(d) * k, abs(d), 0 if d >= 0 else 1
        if -d <= wrongway_m:
            return -d / (WRONGWAY_MPH * MPS_PER_MPH), -d, 1
        return None

    def route(self, a, b, params, pen: dict | None = None, heading: float | None = None,
              wrongway_m: float = 60.0) -> dict | None:
        """The fastest way from [lon, lat] a to [lon, lat] b: {coords: [[lon, lat], ...] (a's road point to b's),
        m (metres), s (seconds), pops}; None: no road near either, or no way (closed in)."""
        sa, sb = self.snap(a[0], a[1]), self.snap(b[0], b[1])
        if sa is None or sb is None:
            return None
        pen = pen or {}
        spm = self.spm(params)
        goals = self._goals(sb, spm, pen, wrongway_m)
        best_s, best = math.inf, None
        direct = self._direct(sa, sb, spm, pen, wrongway_m)
        if direct:
            best_s, best = direct[0], ("direct", direct)
        vmax = 1.0 / min(spm)
        tx, ty = self.to_xy(sb["lon"], sb["lat"])
        X, Y, adj, ea, eb, eow, elen, ecls = self.x, self.y, self.adj, self.ea, self.eb, self.eow, self.elen, self.ecls
        dist: dict[int, float] = {}
        prev: dict[int, tuple[int, int]] = {}
        start_of: dict[int, int] = {}
        heap: list = []
        for n, s, _, d in self._starts(sa, spm, pen, heading):
            if s < dist.get(n, math.inf):
                dist[n] = s
                start_of[n] = d
                heapq.heappush(heap, (s + math.hypot(X[n] - tx, Y[n] - ty) / vmax, s, n))
        pops = 0
        while heap:
            f, g, n = heapq.heappop(heap)
            if f >= best_s:
                break
            if g > dist.get(n, math.inf):
                continue
            pops += 1
            if pops > MAX_POPS:
                break
            gv = goals.get(n)
            if gv is not None and g + gv[0] < best_s:
                best_s, best = g + gv[0], ("node", n)
            for code in adj[n]:
                e = code >> 1
                if code & 1:
                    if eow[e]:
                        continue
                    o = ea[e]
                else:
                    o = eb[e]
                p = pen.get(e, 1.0)
                if p == math.inf:
                    continue
                g2 = g + elen[e] * spm[ecls[e]] * p
                if g2 < dist.get(o, math.inf):
                    dist[o] = g2
                    prev[o] = (n, code)
                    heapq.heappush(heap, (g2 + math.hypot(X[o] - tx, Y[o] - ty) / vmax, g2, o))
        if best is None:
            return None
        coords = self._path(sa, sb, best, prev, start_of, goals)
        return {"coords": coords, "m": _metres(coords), "s": best_s, "pops": pops, "way": self._way(best, prev, goals)}

    @staticmethod
    def _way(best, prev, goals) -> list[tuple[int, int]]:
        """The edges driven between the start's edge and the call's: [(edge, 0: a -> b | 1: b -> a), ...], the
        final (wrong-way) stretch last."""
        if best[0] == "direct":
            return []
        n, chain = best[1], []
        while n in prev:
            p, code = prev[n]
            chain.append((code >> 1, code & 1))
            n = p
        return chain[::-1] + list(goals[best[1]][2])

    def _path(self, sa, sb, best, prev, start_of, goals) -> list[list[float]]:
        start = [sa["lon"], sa["lat"]]
        if best[0] == "direct":
            pts = self._part(sa["edge"], min(sa["off"], sb["off"]), max(sa["off"], sb["off"]))
            if best[1][2] == 1:
                pts.reverse()
            return [start, *pts[1:-1], [sb["lon"], sb["lat"]]]
        n = best[1]
        chain = []
        while n in prev:
            p, code = prev[n]
            chain.append(code)
            n = p
        chain.reverse()
        e0 = sa["edge"]
        out = [start]
        if start_of.get(n) == 0:
            out += self._part(e0, sa["off"], self.elen[e0])[1:]
        else:
            out += list(reversed(self._part(e0, 0.0, sa["off"])))[1:]
        for code in chain:
            pts = [list(p) for p in self.ll(code >> 1)]
            out += (pts[::-1] if code & 1 else pts)[1:]
        way = goals[best[1]][2]
        for k, (e, d) in enumerate(way):
            last = k == len(way) - 1
            if not last:
                pts = [list(p) for p in self.ll(e)]
                out += (pts[::-1] if d else pts)[1:]
            elif d == 0:
                out += self._part(e, 0.0, sb["off"])[1:]
            else:
                out += list(reversed(self._part(e, sb["off"], self.elen[e])))[1:]
        out[-1] = [sb["lon"], sb["lat"]]
        return [[round(x, 6), round(y, 6)] for x, y in _dedupe(out)]

    def _part(self, e: int, o1: float, o2: float) -> list[list[float]]:
        """The edge's points between o1 and o2 metres from a (a -> b order)."""
        pts = self.ll(e)
        xy = self.xy(e)
        out, run = [], 0.0
        for i in range(len(pts) - 1):
            seg = math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1])
            lo, hi = run, run + seg
            if not out and o1 <= hi + 1e-9:
                t = 0.0 if seg == 0 else (o1 - lo) / seg
                out.append(_lerp(pts[i], pts[i + 1], t))
            if out:
                if o2 <= hi + 1e-9:
                    t = 0.0 if seg == 0 else (o2 - lo) / seg
                    out.append(_lerp(pts[i], pts[i + 1], t))
                    return out
                out.append(list(pts[i + 1]))
            run = hi
        if not out:
            out.append(list(pts[-1]))
        out.append(list(pts[-1]))
        return out

    def to_one(self, origins: dict, b, params, pen: dict | None = None, wrongway_m: float = 60.0,
               max_s: float = 1800.0) -> dict:
        """Seconds and metres to [lon, lat] b from each origin, in one reverse Dijkstra: {key: (s, m)} for the
        origins with a way (a key missing: no road near it, or none within max_s). origins: {key: [lon, lat]}."""
        sb = self.snap(b[0], b[1])
        if sb is None:
            return {}
        pen = pen or {}
        spm = self.spm(params)
        goals = self._goals(sb, spm, pen, wrongway_m)
        snaps = {k: self.snap(ll[0], ll[1]) for k, ll in origins.items()}
        want: dict[int, list] = {}
        out: dict = {}
        for k, s in snaps.items():
            if s is None:
                continue
            d = self._direct(s, sb, spm, pen, wrongway_m)
            if d:
                out[k] = (d[0], d[1])
            for n, sec, m, _ in self._starts(s, spm, pen, None):
                want.setdefault(n, []).append((k, sec, m))
        dist: dict[int, float] = {}
        metres: dict[int, float] = {}
        heap = []
        for n, (s, m, _) in goals.items():
            dist[n], metres[n] = s, m
            heap.append((s, n))
        heapq.heapify(heap)
        left = set(want)
        adj, ea, eb, eow, elen, ecls = self.adj, self.ea, self.eb, self.eow, self.elen, self.ecls
        while heap and left:
            g, n = heapq.heappop(heap)
            if g > dist[n]:
                continue
            if g > max_s:
                break
            left.discard(n)
            for code in adj[n]:
                e = code >> 1
                if code & 1:
                    o = ea[e]                                 # n is b: drive a -> b into n (always legal)
                else:
                    if eow[e]:
                        continue
                    o = eb[e]                                 # n is a: drive b -> a into n (two-way only)
                p = pen.get(e, 1.0)
                if p == math.inf:
                    continue
                g2 = g + elen[e] * spm[ecls[e]] * p
                if g2 < dist.get(o, math.inf):
                    dist[o] = g2
                    metres[o] = metres[n] + elen[e]
                    heapq.heappush(heap, (g2, o))
        for n, rows in want.items():
            if n not in dist or dist[n] > max_s:
                continue
            for k, sec, m in rows:
                v = (sec + dist[n], m + metres[n])
                if k not in out or v[0] < out[k][0]:
                    out[k] = v
        return out


def _end(p: float) -> float:
    """A start's or a call's own edge: never blocked (the rig is there, the call is there), but a closed one costs
    END_BLOCKED times as much, so the way round wins wherever there is one."""
    return END_BLOCKED if p == math.inf else p


def _lerp(p, q, t: float) -> list[float]:
    t = max(0.0, min(1.0, t))
    return [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t]


def _dedupe(pts: list) -> list:
    out = []
    for p in pts:
        if not out or abs(out[-1][0] - p[0]) > 1e-7 or abs(out[-1][1] - p[1]) > 1e-7:
            out.append(p)
    return out


def _metres(coords: list) -> float:
    if len(coords) < 2:
        return 0.0
    lat0 = math.radians(coords[0][1])
    kx, ky = 111_320.0 * math.cos(lat0), 110_574.0
    return sum(math.hypot((q[0] - p[0]) * kx, (q[1] - p[1]) * ky) for p, q in zip(coords, coords[1:]))


# ---- the provider: fetched, built, cached, swapped in ----------------------------------------------------------------


def http_post(url: str, body: bytes, timeout: float, limit: int) -> tuple[int, bytes]:
    req = Request(url, data=body, method="POST", headers={
        "User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read(limit + 1)
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw
    except HTTPError as e:
        return e.code, e.read(4096)


class RoadGraph:
    """The road graph for the CAD maps' routes and the recommendation's estimates. Built from the cache file in the
    background (load_cache: the standby's prewarm, or the app's layer loader), fetched afresh by Settings > Data
    sources (refresh(): on its schedule, or Refresh now). The graph in use is swapped whole: a route already being
    worked out finishes on the one it started with."""

    def __init__(self, cache_dir: str | Path | None = None, bbox=None, enabled: bool = True, url: str = OVERPASS_URL,
                 post: Callable[[str, bytes, float, int], tuple[int, bytes]] = http_post,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.cache = (Path(cache_dir) / CACHE_NAME) if cache_dir else None
        self.bbox = tuple(bbox) if bbox else None
        self.enabled, self.url, self.post, self.log, self.clock = enabled, url, post, log, clock
        self.graph: Graph | None = None
        self.status = {"at": None, "error": "", "n": 0, "bytes": 0, "next": 0.0, "fails": 0, "build_s": None,
                       "nodes": 0, "edges": 0, "osm_base": ""}
        self.rev = 0
        self._lock = threading.Lock()
        self.cache_loaded = False

    @classmethod
    def from_config(cls, cfg, firehouses: list[dict] | None = None, log: Callable[[str], None] = print) -> RoadGraph:
        state = Path(cfg.cad_db).parent if cfg.cad_db else None
        if firehouses is None:
            from sizeup.cad.mapview import load_firehouses
            firehouses = load_firehouses()
        ring = float(getattr(cfg, "routing_ring_km", RING_KM))
        bbox = area_bbox([f["ll"] for f in firehouses], ring) if firehouses else None
        on = bool(getattr(cfg, "routing_enabled", False)) and getattr(cfg, "cloud_role", "") != "station" and bbox is not None
        return cls(state, bbox, enabled=on, url=getattr(cfg, "routing_overpass_url", "") or OVERPASS_URL, log=log)

    @property
    def configured(self) -> bool:
        return self.enabled and self.bbox is not None

    def ready(self) -> Graph | None:
        return self.graph

    def load_cache(self) -> None:
        """Read and build the cached graph (once; never raises). Seconds of CPU: never on the request path."""
        if self.cache_loaded or not self.configured:
            self.cache_loaded = True
            return
        try:
            if self.cache and self.cache.is_file():
                t0 = time.monotonic()
                raw = self.cache.read_bytes()
                doc = json.loads(raw)
                if doc.get("bbox") and self.bbox and [round(v, 3) for v in doc["bbox"]] != [round(v, 3) for v in self.bbox]:
                    self.log("roadgraph: the cached graph is for another area: fetched afresh")
                else:
                    self._use(Graph(doc), doc, len(raw), time.monotonic() - t0)
                    self.log(f"roadgraph: {self.status['nodes']} nodes, {self.status['edges']} edges read in "
                             f"{self.status['build_s']:.1f} s")
        except Exception as e:  # a bad cache: fetched afresh on the schedule
            self.log(f"roadgraph: cache not read ({type(e).__name__}: {e})")
        finally:
            self.cache_loaded = True

    def _use(self, g: Graph, doc: dict, size: int, took: float) -> None:
        with self._lock:
            self.graph = g
            self.rev += 1
            self.status.update(at=doc.get("at"), n=g.m, bytes=size, nodes=g.n, edges=g.m, build_s=round(took, 2),
                               osm_base=doc.get("osm_base") or "", error="")

    def refresh(self) -> bool:
        """Fetch the area's roads from Overpass, build, save, swap in. Never raises; a failure keeps the graph."""
        if not self.configured:
            return False
        now = self.clock()
        try:
            from urllib.parse import urlencode
            t0 = time.monotonic()
            code, body = self.post(self.url, urlencode({"data": overpass_query(self.bbox)}).encode(), TIMEOUT_S, MAX_BYTES)
            if code != 200:
                raise RuntimeError(f"HTTP {code}" + (" (rate limited)" if code == 429 else ""))
            if len(body) > MAX_BYTES:
                raise RuntimeError("answer too big")
            fetched = time.monotonic() - t0
            doc = build(json.loads(body), self.bbox, at=now)
            if len(doc["edges"]) < 5 * 100:
                raise RuntimeError(f"only {len(doc['edges']) // 5} edges: not replacing the graph")
            raw = json.dumps(doc, separators=(",", ":")).encode()
            g = Graph(doc)
            took = time.monotonic() - t0
            if self.cache:
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cache.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(self.cache)
            self._use(g, doc, len(raw), took - fetched)
            self.status.update(fails=0, next=0.0)
            self.log(f"roadgraph: {g.n} nodes, {g.m} edges ({len(raw) / 1e6:.1f} MB) fetched in {fetched:.1f} s, "
                     f"built in {took - fetched:.1f} s")
            return True
        except (OSError, URLError, ValueError, RuntimeError, KeyError, TypeError) as e:
            fails = self.status["fails"] + 1
            wait = min(BACKOFF_S[0] * 2 ** (fails - 1), BACKOFF_S[1])
            self.status.update(error=f"{type(e).__name__}: {e}"[:200], fails=fails, next=now + wait)
            self.log(f"roadgraph: not refreshed ({self.status['error']})")
            return False

    def blocked(self) -> str:
        err, nxt = self.status["error"], self.status["next"]
        if err and "rate limited" in err and self.clock() < nxt:
            return "resting after a rate limit"
        return ""


def main(argv: list[str] | None = None) -> int:
    """python -m sizeup.roadgraph [--ring KM] [--out FILE] [--from FILE]: fetch (or read an Overpass answer), build,
    and print the graph's size, build time, memory and a few timed routes (the PR's numbers)."""
    import argparse
    import random
    import tracemalloc
    ap = argparse.ArgumentParser()
    ap.add_argument("--ring", type=float, default=RING_KM)
    ap.add_argument("--out", default="")
    ap.add_argument("--from", dest="src", default="", help="an Overpass answer saved earlier")
    ap.add_argument("--routes", type=int, default=200)
    a = ap.parse_args(argv)
    from sizeup.cad.eta import Params
    from sizeup.cad.mapview import load_firehouses
    fh = load_firehouses()
    bbox = area_bbox([f["ll"] for f in fh], a.ring)
    print(f"bbox {bbox}")
    if a.src:
        body = Path(a.src).read_bytes()
    else:
        from urllib.parse import urlencode
        t0 = time.monotonic()
        code, body = http_post(OVERPASS_URL, urlencode({"data": overpass_query(bbox)}).encode(), TIMEOUT_S, MAX_BYTES)
        print(f"overpass: HTTP {code}, {len(body) / 1e6:.1f} MB in {time.monotonic() - t0:.1f} s")
        if code != 200:
            print(body[:500].decode(errors="replace"))
            return 1
        if a.out:
            Path(a.out + ".overpass.json").write_bytes(body)
    t0 = time.monotonic()
    doc = build(json.loads(body), bbox)
    raw = json.dumps(doc, separators=(",", ":")).encode()
    t1 = time.monotonic()
    print(f"build (OSM -> compact): {t1 - t0:.1f} s; file {len(raw) / 1e6:.1f} MB, gzip {len(gzip.compress(raw)) / 1e6:.1f} MB")
    if a.out:
        Path(a.out).write_bytes(raw)
    tracemalloc.start()
    t0 = time.monotonic()
    g = Graph(json.loads(raw))
    t1 = time.monotonic()
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"load (file -> Graph): {t1 - t0:.1f} s; {g.n} nodes, {g.m} edges; memory {cur / 1e6:.0f} MB (peak {peak / 1e6:.0f} MB)")
    rnd = random.Random(1)
    p = Params()
    times, pops = [], []
    for _ in range(a.routes):
        f = rnd.choice(fh)["ll"]
        to = [f[0] + rnd.uniform(-0.05, 0.05), f[1] + rnd.uniform(-0.035, 0.035)]
        t0 = time.monotonic()
        r = g.route(f, to, p)
        times.append(time.monotonic() - t0)
        if r:
            pops.append(r["pops"])
    times.sort()
    print(f"{a.routes} routes (firehouse -> a point within ~4 km): median {times[len(times) // 2] * 1000:.0f} ms, "
          f"p95 {times[int(len(times) * 0.95)] * 1000:.0f} ms, max {times[-1] * 1000:.0f} ms; {len(pops)} found")
    t0 = time.monotonic()
    g.to_one({f["name"]: f["ll"] for f in fh}, [-73.66, 41.0], p)
    print(f"to_one (5 firehouses -> a point): {(time.monotonic() - t0) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
