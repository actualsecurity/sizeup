"""Metro-North trains for the CAD maps (GitHub #131): the New Haven Line through Rye, Port Chester and
Greenwich, live, and "Trains approaching" for an event on or near the tracks.

User (#131): "Metro-North New Haven Line runs through Port Chester and Rye (stations, grade crossings,
catenary). Show approaching trains for calls on or near the tracks."

Source: the MTA's public data feeds (https://www.mta.info/developers, checked 2026-10-14), no key:

  realtime  GTFS-realtime for Metro-North, RT_URL (api-endpoint.mta.info, protobuf, about 150 KB): one
            entity per train with its trip update (every remaining stop's predicted arrival and
            departure, the delay when there is one, and the MTA railroad extension's track and status
            text, field 1005) and, for trains that are running, a vehicle position (lat / lon). Trip ids
            are the MTA's own and don't match the static feed's: a train is its number (the entity id /
            the vehicle label). No key since 2024 (the developer page: real-time subway, LIRR and
            Metro-North feeds need none; bus feeds still do). If the MTA brings a key back, set
            SIZEUP_MTA_KEY (the hub: SSM /sizeup-hub/mta-key) and it goes as the x-api-key header.
  static    GTFS for Metro-North, GTFS_URL (rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip, about 4 MB, updated
            about weekly): stops, routes, trips and shapes. Only those four files are read (never the
            26 MB stop_times): the New Haven Line's longest shape is the line (Grand Central to New Haven),
            every station within MAIN_M of it is on it (its distance along the line, "ch", metres from
            Grand Central), and the routes name the branches. Fetched weekly; what we keep is cached on
            disk (state dir, trains/static.json), so a restart fetches nothing.

Terms (https://www.mta.info/developers/terms-and-conditions, updated 2024-03-13): free; we must keep
the data on our own server and serve our users from it (the hub fetches, the browsers only ever ask
the hub: never the MTA's server directly), not alter it (we only select from it, and our own
estimates are labelled as ours), not imply the MTA endorses or licenses us, show no MTA logo, and,
where the data may lag more than a minute, say that it may not be real time (the layer and the panel
say "times from the MTA, may not be real time" with the feed's time). Provided as is.

What is real and what is estimated:
  real       a train's position when the feed has one (the MTA's vehicle position: "reported"), its
             predicted times at the stations it stops at, its delay, track and status.
  estimated  a train's position when the feed has none: placed on the line between the two stations
             it is between, by time (its departure from one, its arrival at the next), labelled
             "estimated"; and the time an express passes a station it doesn't stop at, or any train
             passes a point between stations (an event on the tracks): interpolated the same way.

Schedule: the realtime feed only while someone looks: a viewer with the Trains layer on (the page asks
GET /api/cad/trains every RT_EVERY_S) or an event panel showing trains (GET /api/cad/trains/near):
every RT_EVERY_S (30 s) while asked within DEMAND_S, otherwise idle (nothing fetched). The static feed
weekly. A failure backs off (30 s doubling to 15 min; a refusal 30 min; a 429 5 min). The shared
User-Agent names sizeup and a contact.

The area: the map shows trains and stations on the line inside bbox (Mamaroneck to Cos Cob: Rye to
Greenwich plus a station each side). "Trains approaching" works anywhere on the line.

HTTP (sizeup/web/server.py; any signed-in person, GET only):
    GET /api/cad/trains                 the layer: {configured, note, source, label, link, terms, at (the feed's
                                        time), fetched, static_at, line: [[lon, lat]...], stations: [...],
                                        trains: [{id, train, line, headsign, dir, dir_label, lat, lon, bearing,
                                        estimated, next_stop, next_time, track, delay_s, status}], catenary}
    GET /api/cad/trains/near?lat=&lon=  the event panel: {near: false} when the point is more than NEAR_M from
                                        the tracks and any station; else {near: true, ref: {kind: station | track,
                                        name, station, m, lat, lon}, at, window_min, inbound: [...], outbound:
                                        [...]}: each {train, headsign, line, eta (epoch s), min, stops, estimated,
                                        delay_s, track, status}, soonest first, at most PER_DIR a direction
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import struct
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

from sizeup import USER_AGENT as UA

SOURCE = "MTA"
LABEL = "MTA Metro-North Railroad"
LINK_URL = "https://www.mta.info/developers"
TERMS_URL = "https://www.mta.info/developers/terms-and-conditions"
RT_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/mnr%2Fgtfs-mnr"
GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip"
DISCLAIMER = "Times from the MTA; may not be real time"
# The New Haven Line and its branches (their trains run the main line through our area).
LINES = ("New Haven", "New Canaan", "Danbury", "Waterbury")
MAIN = "New Haven"
# West, south, east, north: the line from Mamaroneck to Cos Cob (Rye to Greenwich, a station each side).
DEFAULT_BBOX = (-73.745, 40.945, -73.595, 41.035)
RT_EVERY_S = 30.0
DEMAND_S = 120.0                 # a viewer's ask keeps the realtime feed coming this long
STATIC_EVERY_S = 7 * 86400.0
BACKOFF_S = (30.0, 900.0)        # realtime: 30 s doubling to 15 min
STATIC_BACKOFF_S = (300.0, 6 * 3600.0)
REFUSED_S = 1800.0
RATE_LIMITED_S = 300.0
TIMEOUT_S = 20.0
MAX_RT_BYTES = 8 * 1024 * 1024
MAX_STATIC_BYTES = 64 * 1024 * 1024
WAIT_FIRST_S = 5.0               # an ask with nothing fresh waits this long for the fetch it wakes
MAIN_M = 300.0                   # a station this close to the line is on it
NEAR_M = 150.0                   # an event this close to the tracks or a station: "Trains approaching"
WINDOW_S = 15 * 60.0
PER_DIR = 3
GONE_S = 120.0                   # a train whose last stop was this long ago has finished
PASSED_M = 50.0                  # a reported position this far beyond the point: already passed
SIMPLIFY_M = 3.0
FALLBACK_MPS = 20.0             # a pair of stations no train runs between: about 45 mph
TOP_MPS = 40.0                  # no faster than about 90 mph between two stations
STOP_COST_MAX_S = 120.0
JSON = "application/json"
NEAR_PATH = "/api/cad/trains/near"
CATENARY = ("New Haven Line: overhead catenary, 12.5 kV AC, plus the third rail at stations; "
            "treat every wire as live until Metro-North confirms it is de-energized and grounded")
GONE = ("Departed",)             # the MTA railroad extension's status for a stop the train has left
DIRS = {1: ("outbound", "Toward New Haven"), -1: ("inbound", "Toward New York")}


# ---- a small protobuf reader: the few GTFS-realtime fields we need --------------------------------------
# Wire types: 0 varint, 1 fixed64, 2 length-delimited, 5 fixed32 (3 and 4, groups, are not in GTFS-rt).


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = out = 0
    while True:
        if i >= len(b) or shift > 63:
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        out |= (c & 0x7F) << shift
        shift += 7
        if c < 0x80:
            return out, i


def pb_fields(b: bytes) -> Iterator[tuple[int, int, object]]:
    """(field number, wire type, value): an int for a varint, bytes for the rest."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v, i = b[i:i + 8], i + 8
        elif w == 5:
            v, i = b[i:i + 4], i + 4
        elif w == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        else:
            raise ValueError(f"wire type {w}")
        if i > n:
            raise ValueError("truncated field")
        yield f, w, v


def pb(b: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    for f, _, v in pb_fields(b):
        out.setdefault(f, []).append(v)
    return out


def _one(m: dict, f: int, default=None):
    v = m.get(f)
    return v[0] if v else default


def _str(m: dict, f: int) -> str:
    v = _one(m, f)
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


def _int(m: dict, f: int) -> int | None:
    v = _one(m, f)
    return v if isinstance(v, int) else None


def _sint(m: dict, f: int) -> int | None:
    """An int32 / int64 (two's complement in a varint)."""
    v = _int(m, f)
    return v - (1 << 64) if v is not None and v >= 1 << 63 else v


def _float(m: dict, f: int) -> float | None:
    v = _one(m, f)
    if isinstance(v, bytes) and len(v) == 4:
        x = struct.unpack("<f", v)[0]
        return x if math.isfinite(x) else None
    return None


def _event(b) -> tuple[int | None, int | None]:
    """A StopTimeEvent: (time, delay)."""
    if not isinstance(b, bytes):
        return None, None
    m = pb(b)
    t = _sint(m, 2)
    return (t if t and t > 0 else None), _sint(m, 1)


def parse_feed(body: bytes) -> dict:
    """A GTFS-realtime FeedMessage -> {timestamp, trains: {train: {...}}}. Trip updates and vehicle positions
    of one train (the same entity, or the same trip id / vehicle) are merged. Raises ValueError on garbage."""
    msg = pb(body)
    header = pb(_one(msg, 1, b""))
    ts = _int(header, 3)
    trains: dict[str, dict] = {}
    by_trip: dict[str, str] = {}

    def get(key: str, trip_id: str) -> dict:
        k = by_trip.get(trip_id, key) if trip_id else key
        t = trains.get(k)
        if t is None:
            t = trains[k] = {"id": k, "train": "", "trip_id": trip_id, "route_id": "", "start_date": "", "stops": [],
                             "delay": None, "pos": None}
        if trip_id:
            by_trip.setdefault(trip_id, k)
            t["trip_id"] = t["trip_id"] or trip_id
        return t

    for ent_b in msg.get(2, []):
        ent = pb(ent_b)
        if _int(ent, 2):                                 # is_deleted
            continue
        eid = _str(ent, 1)
        tu_b, vp_b = _one(ent, 3), _one(ent, 4)
        if isinstance(tu_b, bytes):
            tu = pb(tu_b)
            trip = pb(_one(tu, 1, b""))
            veh = pb(_one(tu, 3, b""))
            t = get(_str(veh, 2) or _str(veh, 1) or eid, _str(trip, 1))
            t["route_id"] = _str(trip, 5) or t["route_id"]
            t["start_date"] = _str(trip, 3) or t["start_date"]
            t["train"] = t["train"] or _str(veh, 2) or _str(veh, 1) or eid
            t["delay"] = _sint(tu, 5) if _sint(tu, 5) is not None else t["delay"]
            stops = []
            for su_b in tu.get(2, []):
                su = pb(su_b)
                arr, arr_d = _event(_one(su, 2))
                dep, dep_d = _event(_one(su, 3))
                track = status = ""
                for ext_b in su.get(1005, []):          # the MTA railroad extension: track, status
                    if isinstance(ext_b, bytes):
                        ext = pb(ext_b)
                        track = track or _str(ext, 1)
                        status = status or _str(ext, 2)
                if _int(su, 5) == 1:                     # SKIPPED
                    continue
                stops.append({"stop_id": _str(su, 4), "arr": arr, "dep": dep,
                              "delay": arr_d if arr_d is not None else dep_d, "track": track, "status": status})
            if stops:
                t["stops"] = stops
        if isinstance(vp_b, bytes):
            vp = pb(vp_b)
            trip = pb(_one(vp, 1, b""))
            veh = pb(_one(vp, 8, b""))
            t = get(_str(veh, 2) or _str(veh, 1) or eid, _str(trip, 1))
            t["route_id"] = t["route_id"] or _str(trip, 5)
            t["train"] = t["train"] or _str(veh, 2) or _str(veh, 1) or eid
            p = _one(vp, 2)
            if isinstance(p, bytes):
                pm = pb(p)
                lat, lon = _float(pm, 1), _float(pm, 2)
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
                    t["pos"] = {"lat": round(lat, 6), "lon": round(lon, 6), "bearing": _float(pm, 3), "ts": _int(vp, 5)}
    for t in trains.values():
        t["train"] = t["train"] or t["id"]
    return {"timestamp": ts, "trains": trains}


# ---- geometry: the line in local metres, distance along it ------------------------------------------------

LAT0 = 41.0
MX, MY = 111_320.0 * math.cos(math.radians(LAT0)), 110_574.0      # metres a degree of lon / lat, here


def _xy(lon: float, lat: float) -> tuple[float, float]:
    return lon * MX, lat * MY


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12_742_000 * math.asin(min(1.0, math.sqrt(a)))


def simplify(pts: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on [(lon, lat)], iterative."""
    if len(pts) < 3:
        return list(pts)
    xy = [_xy(*p) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        worst, at = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if L2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
                d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > worst:
                worst, at = d, i
        if worst > tol_m:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(pts, keep) if k]


class Line:
    """A polyline [(lon, lat)] with its distance along ("ch", metres from the first point)."""

    def __init__(self, pts: list[tuple[float, float]]):
        self.pts = [(float(a), float(b)) for a, b in pts]
        self.xy = [_xy(*p) for p in self.pts]
        self.ch = [0.0]
        for (x0, y0), (x1, y1) in zip(self.xy, self.xy[1:]):
            self.ch.append(self.ch[-1] + math.hypot(x1 - x0, y1 - y0))

    @property
    def length(self) -> float:
        return self.ch[-1] if self.ch else 0.0

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """(distance from the line in m, ch of the nearest point)."""
        px, py = _xy(lon, lat)
        best, best_ch = math.inf, 0.0
        for i in range(len(self.xy) - 1):
            (ax, ay), (bx, by) = self.xy[i], self.xy[i + 1]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
            d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d < best:
                best, best_ch = d, self.ch[i] + t * math.sqrt(L2)
        return best, best_ch

    def at(self, ch: float) -> tuple[float, float, float]:
        """(lat, lon, bearing in degrees, the way ch grows) of the point ch along."""
        ch = max(0.0, min(self.length, ch))
        i = 0
        lo, hi = 0, len(self.ch) - 1
        while lo < hi:                                   # the last vertex at or before ch
            mid = (lo + hi + 1) // 2
            if self.ch[mid] <= ch:
                lo = mid
            else:
                hi = mid - 1
        i = min(lo, len(self.pts) - 2)
        seg = self.ch[i + 1] - self.ch[i]
        t = 0.0 if seg <= 0 else (ch - self.ch[i]) / seg
        (x0, y0), (x1, y1) = self.xy[i], self.xy[i + 1]
        (lon0, lat0), (lon1, lat1) = self.pts[i], self.pts[i + 1]
        bearing = (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360) % 360
        return lat0 + t * (lat1 - lat0), lon0 + t * (lon1 - lon0), bearing

    def clip(self, lo: float, hi: float) -> list[list[float]]:
        """The line between ch lo and hi as [[lon, lat]]."""
        out = []
        a = self.at(lo)
        out.append([round(a[1], 6), round(a[0], 6)])
        for (lon, lat), c in zip(self.pts, self.ch):
            if lo < c < hi:
                out.append([round(lon, 6), round(lat, 6)])
        b = self.at(hi)
        out.append([round(b[1], 6), round(b[0], 6)])
        return out


# ---- the static feed: the line, its stations, the routes --------------------------------------------------


def _csv(z: zipfile.ZipFile, name: str) -> Iterator[dict]:
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def parse_static(body: bytes) -> dict:
    """gtfsmnr.zip -> {line: [[lon, lat]...] (Grand Central first), stations: {stop_id: {...}}, stops: {stop_id:
    name}, routes: {route_id: name}}. Raises ValueError when the New Haven Line isn't in it."""
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a zip: {e}") from None
    with z:
        routes = {r["route_id"]: (r.get("route_long_name") or r.get("route_short_name") or "").strip() for r in _csv(z, "routes.txt")}
        main_routes = {rid for rid, n in routes.items() if n == MAIN}
        if not main_routes:
            raise ValueError("no New Haven route in routes.txt")
        shape_ids = {t["shape_id"] for t in _csv(z, "trips.txt") if t.get("route_id") in main_routes and t.get("shape_id")}
        shapes: dict[str, list[tuple[int, float, float]]] = {}
        for r in _csv(z, "shapes.txt"):
            if r["shape_id"] in shape_ids:
                shapes.setdefault(r["shape_id"], []).append((int(r["shape_pt_sequence"]), float(r["shape_pt_lon"]), float(r["shape_pt_lat"])))
        stops = {}
        for r in _csv(z, "stops.txt"):
            try:
                stops[r["stop_id"]] = {"name": (r.get("stop_name") or "").strip(), "code": (r.get("stop_code") or "").strip(),
                                       "lat": float(r["stop_lat"]), "lon": float(r["stop_lon"]), "url": (r.get("stop_url") or "").strip()}
            except (KeyError, ValueError):
                continue
    if not shapes:
        raise ValueError("no New Haven Line shape in shapes.txt")

    def length(pts):
        return sum(metres(a[2], a[1], b[2], b[1]) for a, b in zip(pts, pts[1:]))
    best = max((sorted(p) for p in shapes.values()), key=length)
    pts = [(lon, lat) for _, lon, lat in best]
    if pts[0][0] > pts[-1][0]:                           # Grand Central (the west end) first
        pts.reverse()
    line = Line(simplify(pts, SIMPLIFY_M))
    stations = {}
    for sid, s in stops.items():
        d, ch = line.project(s["lat"], s["lon"])
        if d <= MAIN_M:
            stations[sid] = s | {"id": sid, "ch": round(ch, 1)}
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        tc = run_times(z, stations)
    for sid, t in tc.items():
        stations[sid]["tc"] = t
    return {"line": [[round(a, 6), round(b, 6)] for a, b in line.pts], "stations": stations,
            "stops": {sid: s["name"] for sid, s in stops.items()}, "routes": routes}


def _hms(v: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{1,3}):(\d\d):(\d\d)\s*", v or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def run_times(z: zipfile.ZipFile, stations: dict) -> dict[str, float]:
    """Each main-line station's "time coordinate" (tc, seconds from Grand Central): how long a train that
    doesn't stop takes between each pair of neighbouring stations, added up along the line. From the
    schedule: the median run of the trains that stop at both, less what a stop costs (the median, over
    every express run, of how much faster it is than the locals' runs it spans, per station skipped;
    about a minute). An express's time between its stops is shared out by it, so the slow miles (Park
    Avenue, the Bronx) take their share and the fast ones theirs. A pair no train runs: FALLBACK_MPS."""
    order = sorted(stations, key=lambda sid: stations[sid]["ch"])
    idx = {sid: i for i, sid in enumerate(order)}
    trips: dict[str, list[tuple[int, int, int, int]]] = {}
    if "stop_times.txt" in z.namelist():
        for r in _csv(z, "stop_times.txt"):
            i = idx.get(r.get("stop_id", ""))
            if i is None:
                continue
            arr, dep = _hms(r.get("arrival_time", "")), _hms(r.get("departure_time", ""))
            if arr is None and dep is None:
                continue
            try:
                seq = int(r["stop_sequence"])
            except (KeyError, ValueError):
                continue
            trips.setdefault(r["trip_id"], []).append((seq, i, arr if arr is not None else dep, dep if dep is not None else arr))
    runs: dict[int, list[int]] = {}
    express: list[tuple[int, int, int]] = []
    for rows in trips.values():
        rows.sort()
        for (_, i, _, dep), (_, j, arr, _) in zip(rows, rows[1:]):
            if arr <= dep or i == j:
                continue
            if abs(i - j) == 1:
                runs.setdefault(min(i, j), []).append(arr - dep)
            else:
                express.append((min(i, j), max(i, j), arr - dep))
    gaps = [stations[order[k + 1]]["ch"] - stations[order[k]]["ch"] for k in range(len(order) - 1)]
    local = [statistics.median(runs[k]) if runs.get(k) else g / FALLBACK_MPS for k, g in enumerate(gaps)]
    per_stop = [(sum(local[a:b]) - t) / (b - a - 1) for a, b, t in express]
    cost = min(STOP_COST_MAX_S, max(0.0, statistics.median(per_stop))) if per_stop else 0.0
    tc, t = {}, 0.0
    for k, sid in enumerate(order):
        tc[sid] = round(t, 1)
        if k < len(gaps):
            t += max(local[k] - (cost if runs.get(k) else 0.0), gaps[k] / TOP_MPS)
    return tc


# ---- where the trains are, and when they reach a point -------------------------------------------------------


def _t_arr(s: dict) -> int | None:
    return s["arr"] or s["dep"]


def _t_dep(s: dict) -> int | None:
    return s["dep"] or s["arr"]


class Model:
    """The static feed's line and stations, ready to place trains on."""

    def __init__(self, static: dict, bbox=DEFAULT_BBOX):
        self.static = static
        self.line = Line([tuple(p) for p in static["line"]])
        self.stations: dict[str, dict] = static["stations"]
        self.stops: dict[str, str] = static.get("stops", {})
        self.routes: dict[str, str] = static.get("routes", {})
        self.ours = {rid for rid, n in self.routes.items() if n in LINES}
        w, s, e, n = bbox
        inside = [c for (lon, lat), c in zip(self.line.pts, self.line.ch) if w <= lon <= e and s <= lat <= n]
        self.area = (min(inside), max(inside)) if inside else (0.0, 0.0)
        self.area_stations = sorted((st for st in self.stations.values() if w <= st["lon"] <= e and s <= st["lat"] <= n),
                                    key=lambda st: st["ch"])
        # the line's time coordinate (run_times): ch <-> tc at each station, straight between them
        order = sorted(self.stations.values(), key=lambda st: st["ch"])
        if order and all(isinstance(st.get("tc"), (int, float)) for st in order):
            self._chs, self._tcs = [st["ch"] for st in order], [float(st["tc"]) for st in order]
        else:                                            # (no run times: distance alone)
            self._chs, self._tcs = [0.0, self.line.length or 1.0], [0.0, (self.line.length or 1.0) / FALLBACK_MPS]

    @staticmethod
    def _interp(x: float, xs: list[float], ys: list[float]) -> float:
        if x <= xs[0]:
            return ys[0] + (x - xs[0]) * ((ys[1] - ys[0]) / (xs[1] - xs[0]) if len(xs) > 1 and xs[1] > xs[0] else 1.0)
        if x >= xs[-1]:
            return ys[-1] + (x - xs[-1]) * ((ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) if len(xs) > 1 and xs[-1] > xs[-2] else 1.0)
        lo, hi = 0, len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= x:
                lo = mid
            else:
                hi = mid
        span = xs[hi] - xs[lo]
        return ys[lo] if span <= 0 else ys[lo] + (ys[hi] - ys[lo]) * (x - xs[lo]) / span

    def tc(self, ch: float) -> float:
        return self._interp(ch, self._chs, self._tcs)

    def ch(self, tc: float) -> float:
        return self._interp(tc, self._tcs, self._chs)

    def _between(self, ta: float, tb: float, ca: float, cb: float, ch: float) -> float:
        """The time a train leaving ch ca at ta and reaching cb at tb passes ch, by the line's run times."""
        ka, kb = self.tc(ca), self.tc(cb)
        return ta + (tb - ta) * ((self.tc(ch) - ka) / (kb - ka) if kb != ka else 1.0)

    def in_area(self, ch: float, margin: float = 0.0) -> bool:
        return self.area[0] - margin <= ch <= self.area[1] + margin

    def anchors(self, t: dict) -> list[tuple[dict, float]]:
        """The train's stops on the main line, with their ch, in its order; those with a time."""
        return [(s, self.stations[s["stop_id"]]["ch"]) for s in t["stops"]
                if s["stop_id"] in self.stations and _t_arr(s)]

    def direction(self, t: dict, anchors: list) -> int:
        if len(anchors) >= 2 and anchors[-1][1] != anchors[0][1]:
            return 1 if anchors[-1][1] > anchors[0][1] else -1
        try:
            return 1 if int(re.sub(r"\D", "", t["train"]) or "1") % 2 == 0 else -1    # Metro-North: even numbers outbound
        except ValueError:
            return 1

    def position(self, t: dict, anchors: list, d: int, now: float) -> dict | None:
        """{ch, lat, lon, bearing, estimated, off_line}: reported by the MTA when the feed has it, else estimated
        between the two stations the train is between by now; None when it isn't on the line now."""
        p = t.get("pos")
        if p:
            dist, ch = self.line.project(p["lat"], p["lon"])
            _, _, b = self.line.at(ch)
            bearing = p["bearing"] if p.get("bearing") is not None else (b if d > 0 else (b + 180) % 360)
            return {"ch": ch, "lat": p["lat"], "lon": p["lon"], "bearing": round(bearing), "estimated": False,
                    "off_line": dist > MAIN_M}
        if not anchors:
            return None
        ch = None
        first, last = anchors[0], anchors[-1]
        if now < _t_arr(first[0]):
            return None                                  # not on the main line yet (or not yet left)
        if now > _t_dep(last[0]):
            if now - _t_dep(last[0]) > GONE_S:
                return None
            ch = last[1]
        else:
            for (a, ca), (b, cb) in zip(anchors, anchors[1:]):
                if _t_arr(a) <= now <= _t_dep(a):
                    ch = ca
                    break
                ta, tb = _t_dep(a), _t_arr(b)
                if ta <= now <= tb:
                    ka, kb = self.tc(ca), self.tc(cb)
                    ch = self.ch(ka + (kb - ka) * ((now - ta) / (tb - ta) if tb > ta else 1.0))
                    break
            else:
                ch = last[1]
        lat, lon, b = self.line.at(ch)
        return {"ch": ch, "lat": round(lat, 6), "lon": round(lon, 6), "bearing": round(b if d > 0 else (b + 180) % 360),
                "estimated": True, "off_line": False}

    def facts(self, t: dict, now: float) -> dict:
        """What the panel and the click box say of a train: its line, destination, next stop, delay, track."""
        stops = t["stops"]
        nxt = next((s for s in stops if (_t_dep(s) or 0) >= now - 30 and s["status"] != "Departed"), None)
        delay = None
        if nxt is not None:
            delay = nxt["delay"]
            if delay is None:
                delay = next((s["delay"] for s in stops if s is not nxt and s["delay"] is not None and (_t_arr(s) or 0) >= now), None)
        if delay is None:
            delay = t.get("delay")
        if delay is None and nxt is not None and nxt["status"].lower().replace("-", " ") == "on time":
            delay = 0
        dest = self.stops.get(stops[-1]["stop_id"], stops[-1]["stop_id"]) if stops else ""
        return {"train": t["train"], "line": self.routes.get(t["route_id"], ""), "headsign": dest,
                "next_stop": self.stops.get(nxt["stop_id"], nxt["stop_id"]) if nxt else "",
                "next_time": _t_arr(nxt) if nxt else None, "track": nxt["track"] if nxt else "",
                "status": nxt["status"] if nxt else "", "delay_s": delay}

    def ours_train(self, t: dict, now: float) -> bool:
        """On the New Haven Line (or a branch) and not finished."""
        if self.ours and t["route_id"] and t["route_id"] not in self.ours:
            return False
        times = [x for s in t["stops"] for x in (s["arr"], s["dep"]) if x]
        return bool(times) and max(times) >= now - GONE_S or (not times and t.get("pos") is not None)

    def trains(self, feed: dict, now: float) -> list[dict]:
        """The trains on the line in our area now, for the map."""
        out = []
        for t in feed.get("trains", {}).values():
            if not self.ours_train(t, now) or not t["stops"] and not t.get("pos"):
                continue
            anchors = self.anchors(t)
            d = self.direction(t, anchors)
            p = self.position(t, anchors, d, now)
            if p is None or p["off_line"] or not self.in_area(p["ch"], 200.0):
                continue
            key, label = DIRS[d]
            out.append(self.facts(t, now) | {"id": t["id"], "dir": key, "dir_label": label, "lat": p["lat"], "lon": p["lon"],
                                              "bearing": p["bearing"], "estimated": p["estimated"]})
        out.sort(key=lambda x: (x["dir"], x["train"]))
        return out

    def near(self, lat: float, lon: float) -> dict | None:
        """The event's reference point on the line: the station when one is within NEAR_M, else the nearest point
        of the tracks when within NEAR_M; None when neither."""
        dist, ch = self.line.project(lat, lon)
        st = min(self.stations.values(), key=lambda s: metres(lat, lon, s["lat"], s["lon"]), default=None)
        st_m = metres(lat, lon, st["lat"], st["lon"]) if st else math.inf
        if st is not None and st_m <= NEAR_M:
            return {"kind": "station", "name": st["name"], "station": st["id"], "ch": st["ch"], "m": round(st_m),
                    "lat": st["lat"], "lon": st["lon"]}
        if dist > NEAR_M:
            return None
        by_ch = min(self.stations.values(), key=lambda s: abs(s["ch"] - ch))
        p = self.line.at(ch)
        return {"kind": "track", "name": by_ch["name"], "station": by_ch["id"], "ch": round(ch, 1), "m": round(dist),
                "station_m": round(abs(by_ch["ch"] - ch)), "side": "east" if ch > by_ch["ch"] else "west",
                "lat": round(p[0], 6), "lon": round(p[1], 6)}

    def eta(self, t: dict, ref: dict, anchors: list, d: int, now: float, pch: float | None = None) -> dict | None:
        """When the train reaches the reference point: its predicted time there when it stops at that station,
        else between the stations either side by the line's run times (estimated): from where the MTA says
        it is now (pch) when that is between them, else from its departure. None when it doesn't pass it."""
        if ref["kind"] == "station":
            s = next((s for s in t["stops"] if s["stop_id"] == ref["station"] and _t_arr(s)), None)
            if s is not None:
                return None if s["status"] in GONE else {"eta": _t_arr(s), "stops": True, "estimated": False}
        ch = ref["ch"]
        for (a, ca), (b, cb) in zip(anchors, anchors[1:]):
            if (ch - ca) * d >= 0 and (cb - ch) * d >= 0 and ca != cb:
                if b["status"] in GONE or b["status"] == "Arrived":
                    return None                          # already at (or beyond) the station after the point
                ta, tb = _t_dep(a), _t_arr(b)
                if pch is not None and (pch - ca) * d > 0 and (ch - pch) * d >= 0 and now < tb:
                    ta, ca = now, pch
                return {"eta": round(self._between(ta, tb, ca, cb, ch)), "stops": False, "estimated": True}
        return None

    def approaching(self, feed: dict, lat: float, lon: float, now: float) -> dict:
        ref = self.near(lat, lon)
        if ref is None:
            return {"near": False}
        out = {"inbound": [], "outbound": []}
        for t in feed.get("trains", {}).values():
            if not self.ours_train(t, now):
                continue
            anchors = self.anchors(t)
            if not anchors:
                continue
            d = self.direction(t, anchors)
            p, pch = t.get("pos"), None
            if p:
                dist, pch = self.line.project(p["lat"], p["lon"])
                if dist > MAIN_M:
                    pch = None
                elif (pch - ref["ch"]) * d > PASSED_M:  # the MTA says it is already beyond the point
                    continue
            e = self.eta(t, ref, anchors, d, now, pch)
            if e is None or not (now - 60 <= e["eta"] <= now + WINDOW_S):
                continue
            f = self.facts(t, now)
            out[DIRS[d][0]].append({"train": f["train"], "headsign": f["headsign"], "line": f["line"], "eta": e["eta"],
                                    "min": max(0, math.ceil((e["eta"] - now) / 60)), "stops": e["stops"],
                                    "estimated": e["estimated"], "delay_s": f["delay_s"], "track": f["track"] if e["stops"] else "",
                                    "status": f["status"], "reported": bool(p)})
        for k in out:
            out[k] = sorted(out[k], key=lambda x: x["eta"])[:PER_DIR]
        ref = {k: v for k, v in ref.items() if k != "ch"}
        return {"near": True, "ref": ref, "window_min": round(WINDOW_S / 60), **out,
                "dir_labels": {DIRS[1][0]: DIRS[1][1], DIRS[-1][0]: DIRS[-1][1]}}


# ---- the client --------------------------------------------------------------------------------------------


def http_get(url: str, timeout: float, limit: int, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """(status, headers, body): urllib, GET only, the shared User-Agent. Errors as HTTPError / URLError / OSError."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/x-protobuf, application/zip, */*;q=0.5", **(headers or {})})
    with urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"more than {limit // (1024 * 1024)} MB")
        return r.status, {k.lower(): v for k, v in r.headers.items()}, body


class Trains:
    def __init__(self, enabled: bool = True, rt_url: str = "", gtfs_url: str = "", key: str = "", bbox=DEFAULT_BBOX,
                 cache_dir: str | Path | None = None, fetch: Callable | None = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.enabled = bool(enabled)
        self.rt_url = (rt_url or RT_URL).strip() if enabled else ""
        self.gtfs_url = (gtfs_url or GTFS_URL).strip() if enabled else ""
        self.key = (key or "").strip()
        self.bbox = tuple(float(x) for x in bbox)
        self.cache = Path(cache_dir) / "static.json" if cache_dir else None
        self.fetch = fetch or (lambda url, timeout, limit, headers=None: http_get(url, timeout, limit, headers))
        self.log, self.clock = log, clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._fresh = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.model: Model | None = None
        self.feed: dict = {"timestamp": None, "trains": {}}
        self.asked = 0.0                                  # the last time a viewer asked (the realtime feed's demand)
        self.rt = {"at": None, "error": "", "next": 0.0, "fails": 0, "n": 0}
        self.st = {"at": None, "error": "", "next": 0.0, "fails": 0, "modified": ""}
        self._load()

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print) -> Trains:
        state = Path(cfg.cad_db).parent if getattr(cfg, "cad_db", "") else None
        return cls(getattr(cfg, "trains_enabled", True), getattr(cfg, "trains_rt_url", ""), getattr(cfg, "trains_gtfs_url", ""),
                   getattr(cfg, "trains_key", ""), getattr(cfg, "trains_bbox", DEFAULT_BBOX),
                   (state / "trains") if state else None, log=log)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.rt_url) and bool(self.gtfs_url)

    def why_not(self) -> str:
        if not self.enabled:
            return "Trains off ([trains] enabled = false)"
        if not self.configured:
            return "Trains not configured (no MTA feed address)"
        if self.model is None:
            err = self.st["error"]
            return "Metro-North's stations and line not loaded yet" + (f" ({err})" if err else "")
        return ""

    def _headers(self) -> dict:
        return {"x-api-key": self.key} if self.key else {}

    # -- the schedule ----------------------------------------------------------------------------------------

    def start(self) -> Trains:
        if not self.configured:
            self.log(f"trains: {self.why_not()}")
            return self
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="trains", daemon=True)
            self._thread.start()
            self.log(f"trains: Metro-North (MTA GTFS-realtime every {RT_EVERY_S:.0f} s while the layer or an event panel "
                     f"is open; stations and line weekly), area {self.bbox}")
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(2.0)
            self._wake.clear()

    def tick(self) -> None:
        """One turn of the schedule: the static feed when due, the realtime one when due and someone looks."""
        now = self.clock()
        if self.configured and now >= self.st["next"]:
            self.refresh_static()
        if self.rt_due():
            self.refresh_rt()

    def rt_due(self) -> bool:
        now = self.clock()
        return self.configured and now - self.asked <= DEMAND_S and now >= self.rt["next"]

    def ask(self) -> None:
        """A viewer looks (the layer or an event panel): keep the realtime feed coming; wake the fetch now when
        the data is stale, and wait a moment for it (the first look gets trains, not "loading")."""
        now = self.clock()
        stale = self.rt["at"] is None or now - self.rt["at"] > 2 * RT_EVERY_S
        self.asked = now
        if not stale or not self.configured:
            return
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            with self._fresh:
                self._fresh.wait_for(lambda: self.rt["at"] is not None and self.clock() - self.rt["at"] <= 2 * RT_EVERY_S
                                     or bool(self.rt["error"]) or self.model is None and bool(self.st["error"]),
                                     timeout=WAIT_FIRST_S)

    def _failed(self, st: dict, now: float, code: int, why: str, backoff: tuple[float, float], what: str) -> bool:
        st["fails"] += 1
        if code in (401, 403):
            wait, why = REFUSED_S, f"the MTA refused the request ({why}){'' if self.key else '; a key may be needed now (SIZEUP_MTA_KEY)'}"
        elif code == 429:
            wait, why = RATE_LIMITED_S, f"MTA rate limit ({why})"
        else:
            wait = min(backoff[1], backoff[0] * 2 ** (st["fails"] - 1))
        with self._fresh:
            st.update(error=why, next=now + wait)
            self._fresh.notify_all()
        self.log(f"trains: {what}: {why}; next try in {wait:.0f} s")
        return False

    def refresh_rt(self) -> bool:
        """The realtime feed now. True when read. Never raises."""
        if not self.configured:
            return False
        now = self.clock()
        try:
            code, _, body = self.fetch(self.rt_url, TIMEOUT_S, MAX_RT_BYTES, self._headers())
            if code != 200:
                raise HTTPError(self.rt_url, code, f"HTTP {code}", None, None)
            feed = parse_feed(body)
        except HTTPError as e:
            return self._failed(self.rt, now, e.code, f"HTTP {e.code}", BACKOFF_S, "realtime")
        except (URLError, OSError, ValueError) as e:
            return self._failed(self.rt, now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}", BACKOFF_S, "realtime")
        except Exception as e:  # a surprise in the feed must not stop the schedule
            return self._failed(self.rt, now, 0, f"{type(e).__name__}: {e}", BACKOFF_S, "realtime")
        with self._fresh:
            self.feed = feed
            self.rt.update(at=now, error="", fails=0, next=now + RT_EVERY_S, n=len(feed["trains"]))
            self._fresh.notify_all()
        return True

    def refresh_static(self) -> bool:
        """The static feed now (the line, its stations, the routes). True when read. Never raises."""
        if not self.configured:
            return False
        now = self.clock()
        try:
            code, headers, body = self.fetch(self.gtfs_url, 60.0, MAX_STATIC_BYTES, {})
            if code != 200:
                raise HTTPError(self.gtfs_url, code, f"HTTP {code}", None, None)
            static = parse_static(body)
            model = Model(static, self.bbox)
            if not model.area_stations:
                raise ValueError("no New Haven Line station in the area")
        except HTTPError as e:
            return self._failed(self.st, now, e.code, f"HTTP {e.code}", STATIC_BACKOFF_S, "stations and line")
        except (URLError, OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
            return self._failed(self.st, now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}", STATIC_BACKOFF_S, "stations and line")
        except Exception as e:
            return self._failed(self.st, now, 0, f"{type(e).__name__}: {e}", STATIC_BACKOFF_S, "stations and line")
        with self._fresh:
            self.model = model
            self.st.update(at=now, error="", fails=0, next=now + STATIC_EVERY_S, modified=headers.get("last-modified", ""))
            self._fresh.notify_all()
        self.log(f"trains: Metro-North stations and line: {len(static['stations'])} stations on the New Haven Line, "
                 f"{len(model.area_stations)} in our area ({', '.join(s['name'] for s in model.area_stations)})")
        self._save(static)
        return True

    # -- the disk cache (what the static feed gave us) --------------------------------------------------------

    def _load(self) -> None:
        if not self.cache or not self.cache.is_file() or not self.configured:
            return
        try:
            doc = json.loads(self.cache.read_text())
            at = doc.get("at")
            if not isinstance(at, (int, float)) or at > self.clock() + 60:
                return
            model = Model(doc["static"], self.bbox)
            if not model.area_stations:
                return
            self.model = model
            self.st.update(at=at, next=at + STATIC_EVERY_S, modified=str(doc.get("modified", "")))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            self.log(f"trains: cache {self.cache.name} not read ({type(e).__name__}); fetching afresh")

    def _save(self, static: dict) -> None:
        if not self.cache:
            return
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"at": self.st["at"], "modified": self.st["modified"], "static": static}, separators=(",", ":")))
            tmp.replace(self.cache)
        except OSError as e:
            self.log(f"trains: cache not saved ({type(e).__name__}: {e})")

    # -- what the pages get ------------------------------------------------------------------------------------

    def _base(self) -> dict:
        return {"configured": self.configured, "note": self.why_not(), "source": SOURCE, "label": LABEL, "link": LINK_URL,
                "terms": TERMS_URL, "disclaimer": DISCLAIMER, "at": self.feed.get("timestamp"), "fetched": self.rt["at"],
                "error": self.rt["error"], "static_at": self.st["at"]}

    def public(self) -> dict:
        """The layer: the line and stations in our area, the trains on it now."""
        out = self._base() | {"line": [], "stations": [], "trains": [], "catenary": CATENARY, "bbox": list(self.bbox)}
        m = self.model
        if m is None:
            return out
        lo, hi = m.area
        out["line"] = m.line.clip(max(0.0, lo - 1500), min(m.line.length, hi + 1500))
        out["stations"] = [{k: s[k] for k in ("id", "name", "code", "lat", "lon", "url")} for s in m.area_stations]
        out["trains"] = m.trains(self.feed, self.clock())
        return out

    def near(self, lat: float, lon: float) -> dict:
        """The event panel: {near: false}, or the trains approaching the point in the next WINDOW_S."""
        m = self.model
        if m is None or m.near(lat, lon) is None:
            return self._base() | {"near": False}
        self.ask()
        return self._base() | m.approaching(self.feed, lat, lon, self.clock())

    def handles(self, path: str) -> bool:
        return path in ("/api/cad/trains", NEAR_PATH)

    def get(self, path: str, query: str, headers=None) -> tuple[int, bytes, str, dict]:
        q = parse_qs(query or "")
        one = lambda k: (q.get(k) or [""])[0].strip()
        if path == "/api/cad/trains":
            self.ask()
            return 200, json.dumps(self.public(), separators=(",", ":")).encode(), JSON, {}
        if path == NEAR_PATH:
            try:
                lat, lon = float(one("lat")), float(one("lon"))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except ValueError:
                return 400, b'{"error":"lat, lon: numbers"}', JSON, {}
            return 200, json.dumps(self.near(lat, lon), separators=(",", ":")).encode(), JSON, {}
        return 404, b'{"error":"not found"}', JSON, {}
