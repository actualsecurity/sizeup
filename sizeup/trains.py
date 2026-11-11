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
            What a vehicle position carries (decoded 2026-10-14, 310 entities, 30 with a position): the
            trip id, lat / lon, the timestamp, the vehicle's id and label (the train number), and for
            most the current status (STOPPED_AT, INCOMING_AT, IN_TRANSIT_TO) and the stop id it means.
            The position is not a GPS track: it snaps to fixed reporting points on the line, about 1 to
            1.6 km apart (re-read 2026-10-14: ~70 distinct points in 5 minutes), and the vehicle's
            timestamp is always the header's (the MTA rebuilds the feed about every 12 s), so how old a
            position is, the feed never says: we stamp it ourselves (fix_at, the header time it first
            appeared). Some positions are bogus: a point by Stamford given for trains at Rye and Port
            Chester (a 14 km jump and back), a point 3.2 km off the line: see "Fixes" below. No bearing,
            no speed, no odometer, no occupancy, and no
            consist (multi_carriage_details): the feed never says how many cars a train has, nor does the
            static feed (trips.txt has no vehicle or length column). A train's heading on the map is the
            line's own, the way it is going (Line.heading); its mark is a plain cab-led train of a fixed
            length. Should the MTA add multi_carriage_details, its count is passed on as "cars" and the
            map draws that many.
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
  estimated  a train's position when the feed has none we accept: placed on the line between the two
             stations it is between, by its run between them (profile_s: away from the one at its
             departure, up to speed, braking into the next at its arrival), labelled "estimated"; and
             the time an express passes a station it doesn't stop at, or any train passes a point between
             stations (an event on the tracks): interpolated by the line's run times.

Fixes (Tracker): each read, each train's position is judged against what we know before it is shown.
Refused (counted, "rejected", and logged now and then): more than MAIN_M off the line; outside the stretch
between the stop the train last left and the one it is going to (the vehicle's stop id, else the trip's
statuses) by more than STOP_SLACK_M; and, against its last good fix (when that is under RESET_S old),
more than BACK_M backwards or faster than MAX_MPS once one reporting point's spacing (SPACING_M) is
allowed. A refused position is never drawn: the train stays at its last good fix (FIX_MAX_S at most;
then it is estimated), or, with none, is estimated. fix_at: the feed time a good position first
appeared (a position unchanged keeps its fix_at; one already there at our first read is
"fix_age_unknown").

Reporting points (Points): every good fix that appeared while we watched teaches us a point of the
MTA's (fixes within CLUSTER_M along the line are one point; count, first and last seen, each way); a
point seen POINT_KNOWN_N times is "known". Kept on disk (state dir, trains/points.json), seeded from
data/mnr_reporting_points.json (recorded 2026-10-14). hold_s, how far the page may carry a train on past
its last fix before the next one: the next known point ahead of it (within POINT_AHEAD_M), never past the
next stop it stops at; no point known: HOLD_FALLBACK_M on, still not past the stop. The page also stops
predicting PREDICT_S after the fix, and freezes when the feed is older than STALE_S.

Schedule: the realtime feed only while someone looks: a viewer with the Trains layer on (the page asks
GET /api/cad/trains every 5 s) or an event panel showing trains (GET /api/cad/trains/near):
every RT_EVERY_S (5 s; [trains] rt_every_s) while asked within DEMAND_S, otherwise idle (nothing
fetched); a read whose header time hasn't moved changes nothing. About 170 KB a read. The static feed
weekly. A failure backs off (10 s doubling to a minute; a refusal 30 min; a 429 5 min). The shared
User-Agent names sizeup and a contact.

The area: the map shows trains and stations on the line inside bbox ([trains] bbox; the default Mount
Vernon East to Stamford, about 31 km of the line either side of Rye and Port Chester: a train shows some
ten to twenty minutes before it gets here). "Trains approaching" works anywhere on the line.

HTTP (sizeup/web/server.py; any signed-in person, GET only):
    GET /api/cad/trains[?have=static_at] the layer: {configured, note, source, label, link, terms, at (the feed's
                                        time), fetched (= fetched_at), age (now - at), stale, every_s, rejected,
                                        points (known reporting points), profile {accel, decel, vmax, predict_s},
                                        static_at, line: [[lon, lat]...], stations: [...],
                                        trains: [{id, train, line, headsign, dir, dir_label, lat, lon, bearing,
                                        estimated, next_stop, next_time, track, delay_s, status[, cars],
                                        mode (reported | estimated | stopped), s (m along the line), d (+1
                                        toward New Haven, -1 toward New York), fix_at, fix_age_unknown,
                                        vehicle_status, vehicle_stop_id, next {stop_id, name, s, arr, dep,
                                        status, track}, prev_s, hold_s, hold_kind (reporting_point | stop |
                                        fallback), predict_until, segment {from, to, s0, s1, t_dep, t_arr,
                                        accel, decel, vmax} (profile_s's run), lag_s}], catenary}. have: the
                                        static_at the page already holds the line and
                                        stations of: then they are left out ("static_same": true), so a poll
                                        is only the trains (the line is most of the answer)
    GET /api/cad/trains/near?lat=&lon=  the event panel: {near: false} when the point is more than NEAR_M from
                                        the tracks and any station; else {near: true, ref: {kind: station | track,
                                        name, station, m, lat, lon}, at, window_min, inbound: [...], outbound:
                                        [...]}: each {train, headsign, line, eta (epoch s), min, stops, estimated,
                                        delay_s, track, status}, soonest first, at most PER_DIR a direction
    GET /api/cad/trains/route?key=A>B   a train's route line from MTA TrainTime (#362): {key, runs: [[[lon, lat]...]]}
    GET /api/cad/trains/departures?station=2PC   a station's next departures from MTA TrainTime /countdown (#362)
    GET /api/cad/trains/railref         the poles and mileposts layer (#371, sizeup/railassets.py): {source, at, error,
                                        poles: [{id, lon, lat, tick}], mileposts: [{mp, lon, lat, whole}]}; tick: the
                                        structure's line square across the tracks, [[lon, lat] x2] (#374)
  A train also carries lane (the numbered track it is drawn on) and lane_how (said | held | outer: how sure), trk /
  trk_src / trk_at (its track and who said it, posted | planned | mta, at which stop; only within TRACK_NEAR_M of that
  stop), lane_at (held: the stop whose word it is), pole and mp (#371); the layer
  "tracks": {"1": [[[lon, lat, s]...] run...]} beside the lanes and "railref": {poles: [[s, id]], mps: [[s, mp]]};
  the event's ref pole / mp.

MTA TrainTime (sizeup/traintime.py, #362): an undocumented MTA API with GPS positions, speed, consist, per-car load,
milepost, posted track and OTP. While it reads well it is merged into the feed above by train number and run date
(Trains.view; a train then also carries "tt", "mp" and "pos_src"); after FALLBACK_AFTER bad polls in a row the layer
is this module's GTFS-realtime alone, as before.
"""

from __future__ import annotations

import csv
import io
import json
import math
import bisect
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
from sizeup import railassets
from sizeup import traintime

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
# West, south, east, north: the line from Mount Vernon East to Stamford (New Rochelle, Rye, Port Chester,
# Greenwich), about 31 km of it ([trains] bbox).
DEFAULT_BBOX = (-73.845, 40.900, -73.530, 41.060)
RT_EVERY_S = 5.0                 # the realtime feed this often while someone looks ([trains] rt_every_s)
RT_EVERY_RANGE_S = (5.0, 300.0)  # what [trains] rt_every_s may be
DEMAND_S = 120.0                 # a viewer's ask keeps the realtime feed coming this long
STATIC_EVERY_S = 7 * 86400.0
BACKOFF_S = (10.0, 60.0)         # realtime: 10 s doubling to a minute
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
HEADING_M = 150.0                # a train's heading: the line's direction over this much of it (about a train long)
DP = 5                           # decimals of a position the page gets (about 1 m)
DP_TRACK = 7                     # a numbered track's (1 cm: at 1 m, tracks 4 m apart wobble apart and together, #374)
MILE_M = 1609.344
MAX_CARS = 16
FALLBACK_MPS = 20.0             # a pair of stations no train runs between: about 45 mph
TOP_MPS = 40.0                  # no faster than about 90 mph between two stations
STOP_COST_MAX_S = 120.0
JSON = "application/json"
NEAR_PATH = "/api/cad/trains/near"
ROUTE_PATH = "/api/cad/trains/route"             # a train's route line (MTA TrainTime, #362)
DEPARTURES_PATH = "/api/cad/trains/departures"   # a station's next departures (MTA TrainTime /countdown, #362)
CATENARY = ("12.5 kV AC catenary + third rail at stations. "
            "Treat all wires as live until Metro-North confirms de-energized and grounded.")
GONE = ("Departed",)             # the MTA railroad extension's status for a stop the train has left
DIRS = {1: ("outbound", "Toward New Haven"), -1: ("inbound", "Toward New York")}
# GTFS-realtime VehiclePosition.current_status
VEHICLE_STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}
MOVING = ("INCOMING_AT", "IN_TRANSIT_TO")
# Fixes (Tracker): when the feed's position for a train is refused
STOP_SLACK_M = 500.0             # outside the stretch between its last stop and its next by more than this
SPACING_M = 3500.0               # the MTA's reporting points: mostly 1 to 1.6 km apart, 3.3 km at most seen (and a
                                 # point can show up a read late): a jump this long is allowed whatever the time
MAX_MPS = 45.0                   # faster than this (about 100 mph) once one spacing is allowed for
BACK_M = 100.0                   # backwards by more than this
RESET_S = 300.0                  # a last good fix older than this no longer judges speed or backwards
FIX_MAX_S = 180.0                # a last good fix kept this long while the feed's position is refused, or missing
REJECT_LOG_S = 60.0              # a refused fix logged at most once this often (the count always)
# Reporting points (Points) and how far the page may carry a train on (hold_s)
CLUSTER_M = 25.0                 # fixes this close along the line: the same point
POINT_KNOWN_N = 2                # a point seen this many times (by new fixes, not re-reads) is known
POINT_AHEAD_M = 3000.0           # a known point further ahead than this: none (fall back)
HOLD_FALLBACK_M = 1500.0
HOLD_FALLBACK_TT_M = 2500.0      # a GPS fix (TrainTime): carried on at its speed for up to RAIL_CRUISE_S (20 s, motion.js),
                                 # then eased to a stop: at 80 mph about 1.2 km, so the fallback never stops it short
POINTS_SAVE_S = 300.0
PREDICT_S = 120.0                # the page carries a train on at most this long past its fix
STALE_S = 60.0                   # a feed this old: the page freezes the trains where they were
# The run between two stops (profile_s): a Metro-North M8 EMU
ACCEL = 0.5                      # m/s^2 away from a stop
DECEL = 0.75                     # m/s^2 braking into one
VMAX = 36.0                      # m/s, about 80 mph: the New Haven Line's track speed through here, roughly
ROOT = Path(__file__).resolve().parent.parent
SEED_POINTS = ROOT / "data" / "mnr_reporting_points.json"
TRACKS_FILE = ROOT / "data" / "mnr_tracks.json"       # each direction's own track (tools/fetch_train_tracks.py)
LANE_OFF_M = 40.0                # a lane's point further than this from the line: not measured on this line
LANE_SLIP_M = 30.0               # nor one whose s is this far from where it projects
TRACK_NEAR_M = 1500.0            # a stop's track says which track a train is on only this close to the stop (#371)
TRACK_HOLD_M = 4000.0            # a stop's track held no further than this from the stop (an express may cross over beyond)
TRACK_RANK = {"posted": 3, "planned": 2, "mta": 1}  # whose word for a stop's track wins: TrainTime's posted, planned, GTFS-rt
OUTER_TRACK = {1: "4", -1: "3"}  # the direction's outer track: where the MTA's stops put trains here (fetch_train_tracks.py)
TRACK_MEM_S = 3600.0             # a train's remembered track forgotten this long after it was last drawn
RAILREF_PATH = "/api/cad/trains/railref"           # the poles and mileposts layer (#371)


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
    of one train (the same entity, or the same trip id / vehicle) are merged: its stops (each with the MTA
    extension's track and status text: On-Time, Late, Arriving, Arrived, Departed...), its position (pos),
    and the vehicle's current status (vstatus: STOPPED_AT, INCOMING_AT, IN_TRANSIT_TO, "" when not given)
    and the stop that status is about (vstop). Raises ValueError on garbage."""
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
                             "delay": None, "pos": None, "vstatus": "", "vstop": ""}
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
            status = _int(vp, 4)                         # current_status: only when the feed says it
            t["vstatus"] = VEHICLE_STATUS.get(status, "") if status is not None else t["vstatus"]
            t["vstop"] = _str(vp, 7) or t["vstop"]
            p = _one(vp, 2)
            if isinstance(p, bytes):
                pm = pb(p)
                lat, lon = _float(pm, 1), _float(pm, 2)
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
                    t["pos"] = {"lat": round(lat, 6), "lon": round(lon, 6), "bearing": _float(pm, 3), "ts": _int(vp, 5)}
            cars = len(vp.get(11, []))                   # multi_carriage_details: one per car (not in the feed today)
            if cars:
                t["cars"] = min(cars, MAX_CARS)
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

    def heading(self, ch: float, span: float = HEADING_M) -> float:
        """The line's bearing (degrees, the way ch grows) over span of it centred on ch: what a train that long
        lies along, steadier than one short segment's."""
        a, b = self.at(max(0.0, ch - span / 2)), self.at(min(self.length, ch + span / 2))
        if a[:2] == b[:2]:
            return self.at(ch)[2]
        dx, dy = (b[1] - a[1]) * MX, (b[0] - a[0]) * MY
        return (math.degrees(math.atan2(dx, dy)) + 360) % 360

    def clip(self, lo: float, hi: float, dp: int = 6) -> list[list[float]]:
        """The line between ch lo and hi as [[lon, lat]]."""
        out = []
        a = self.at(lo)
        out.append([round(a[1], dp), round(a[0], dp)])
        for (lon, lat), c in zip(self.pts, self.ch):
            if lo < c < hi:
                out.append([round(lon, dp), round(lat, dp)])
        b = self.at(hi)
        out.append([round(b[1], dp), round(b[0], dp)])
        return out


# ---- the run between two stops: away slowly, up to speed, braking into the next ---------------------------------
# One pure function (and its inverse) the page ports line for line (motion.js Motion.profileS / profileT), so where the
# hub estimates a train and where the page carries it on between reports agree (tests/test_train_motion.py).


def _shape(seg: dict) -> tuple[float, int, float, float, float, float, float, float]:
    """The run's shape: (D, sign, accel, decel, v, ta, tc, tb): its length, which way along the line, the
    cruise speed and how long it accelerates, cruises and brakes. v solves D = v T - k v^2 (k = 1/(2 accel) +
    1/(2 decel)), the root with the cruise in the middle, so the run takes exactly T = t_arr - t_dep; a short T
    gives a short cruise, down to none (a triangle). No such v within vmax (T shorter than the train can do
    it, or no T): the quickest run instead (up to vmax, or as fast as the distance allows), which arrives late."""
    D = abs(seg["s1"] - seg["s0"])
    sign = 1 if seg["s1"] >= seg["s0"] else -1
    a, b, vmax = seg["accel"], seg["decel"], seg["vmax"]
    if D <= 0:
        return 0.0, sign, a, b, 0.0, 0.0, 0.0, 0.0
    k = 1 / (2 * a) + 1 / (2 * b)
    T = seg["t_arr"] - seg["t_dep"]
    disc = T * T - 4 * k * D
    v = 2 * D / (T + math.sqrt(disc)) if T > 0 and disc >= 0 else math.inf
    if v > vmax:
        v = min(vmax, math.sqrt(D / k))
    return D, sign, a, b, v, v / a, max(0.0, (D - k * v * v) / v), v / b


def profile_s(t: float, seg: dict) -> tuple[float, float]:
    """Where a train is on its run from one stop to the next at time t: (s, metres along the line; speed, m/s).
    seg: {s0, s1, t_dep, t_arr, accel, decel, vmax}. It leaves s0 from rest at t_dep, accelerates at accel,
    cruises, brakes at decel and stops at s1 at t_arr (_shape). A station between s0 and s1 is one it doesn't
    stop at: it cruises past. Never before s0, never past s1: a run that can't make t_arr reaches s1 late and
    waits there for the next report."""
    D, sign, a, b, v, ta, tc, tb = _shape(seg)
    tau = t - seg["t_dep"]
    if D <= 0 or tau <= 0:
        return float(seg["s0"]), 0.0
    if tau < ta:
        x, sp = 0.5 * a * tau * tau, a * tau
    elif tau < ta + tc:
        x, sp = 0.5 * a * ta * ta + v * (tau - ta), v
    elif tau < ta + tc + tb:
        u = ta + tc + tb - tau
        x, sp = D - 0.5 * b * u * u, b * u
    else:
        x, sp = D, 0.0
    return seg["s0"] + sign * min(D, max(0.0, x)), sp


def profile_t(s: float, seg: dict) -> float:
    """profile_s's inverse: when the run reaches s (s0 at t_dep; s1 when it stops there)."""
    D, sign, a, b, v, ta, tc, tb = _shape(seg)
    if D <= 0:
        return float(seg["t_dep"])
    x = min(D, max(0.0, (s - seg["s0"]) * sign))
    xa = 0.5 * a * ta * ta
    if x <= xa:
        tau = math.sqrt(2 * x / a)
    elif x <= xa + v * tc:
        tau = ta + (x - xa) / v
    else:
        tau = ta + tc + tb - math.sqrt(max(0.0, 2 * (D - x) / b))
    return seg["t_dep"] + tau


def segment(s0: float, s1: float, t_dep: float, t_arr: float) -> dict:
    return {"s0": round(s0, 1), "s1": round(s1, 1), "t_dep": t_dep, "t_arr": t_arr, "accel": ACCEL, "decel": DECEL, "vmax": VMAX}


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


class Lane:
    """One direction's own track through our area (data/mnr_tracks.json, tools/fetch_train_tracks.py): [[lon, lat, s]]
    in order along the line, s the line's own distance (metres from Grand Central). A train going that way is drawn
    on it at its s, so the two directions never share one centre line and the motion code keeps one distance."""

    def __init__(self, coords: list):
        self.coords = sorted(([float(a), float(b), float(c)] for a, b, c in coords), key=lambda x: x[2])
        self.s = [c[2] for c in self.coords]

    def at(self, s: float) -> tuple[float, float] | None:
        """(lat, lon) at s; None outside the lane."""
        if len(self.s) < 2 or not self.s[0] <= s <= self.s[-1]:
            return None
        i = max(1, min(len(self.s) - 1, bisect.bisect_left(self.s, s)))
        (lon0, lat0, s0), (lon1, lat1, s1) = self.coords[i - 1], self.coords[i]
        f = 0.0 if s1 <= s0 else (s - s0) / (s1 - s0)
        return lat0 + f * (lat1 - lat0), lon0 + f * (lon1 - lon0)


class Model:
    """The static feed's line and stations, ready to place trains on (each direction on its own track when tracks,
    data/mnr_tracks.json's, fit this line; each train on its own numbered track when Metro-North's track geometry is
    here, sizeup/railassets.py, #371), and the poles and mileposts along it."""

    def __init__(self, static: dict, bbox=DEFAULT_BBOX, tracks: dict | None = None, assets: dict | None = None):
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
        self.lanes: dict[int, Lane] = {}
        for d, (key, _) in DIRS.items():
            coords = ((tracks or {}).get("lanes", {}).get(key) or {}).get("coords") or []
            lane = Lane(coords) if len(coords) >= 2 else None
            if lane is not None and self._fits(lane):
                self.lanes[d] = lane
        # Metro-North's numbered tracks, poles and mileposts measured along this line (#371); none: as before
        self.ref = railassets.RailRef(assets, self.line)
        self.track_lanes: dict[str, list[Lane]] = {}           # each track's runs (a gap between two: no track there)
        for num, runs in self.ref.tracks.items():
            lanes = [lane for lane in map(Lane, runs) if self._fits(lane)]
            if lanes:
                self.track_lanes[num] = lanes
        self.by_code = {st["code"]: st["ch"] for st in self.stations.values() if st.get("code")}

    def _fits(self, lane: Lane) -> bool:
        """The lane was measured on this line: a few of its points are where their s says, beside it."""
        picks = lane.coords[:: max(1, len(lane.coords) // 8)]
        for lon, lat, s in picks:
            dist, ch = self.line.project(lat, lon)
            if dist > LANE_OFF_M or abs(ch - s) > LANE_SLIP_M:
                return False
        return True

    def where(self, d: int, s: float, trk: str = "") -> tuple[float, float]:
        """(lat, lon) a train going d is drawn at, s along the line: on its numbered track (trk, #371), else on its
        direction's track, else on the line."""
        p = self.track_at(trk, s) if trk else None
        if p is None and d in self.lanes:
            p = self.lanes[d].at(s)
        return p if p is not None else self.line.at(s)[:2]

    def track_at(self, trk: str, s: float) -> tuple[float, float] | None:
        """(lat, lon) of numbered track trk at s; None where we have no line of it (a gap, or beyond its ends)."""
        for lane in self.track_lanes.get(trk, ()):
            p = lane.at(s)
            if p is not None:
                return p
        return None

    def track_evidence(self, t: dict, anchors: list, p: int | None, n: int | None, s: float) -> dict | None:
        """What says which numbered track a train at s is on (#371): the track at the stop it last left or the one it
        is going to, only within TRACK_NEAR_M of that stop (between stations it may have crossed over), the nearer
        stop's; for one stop, MTA TrainTime's posted track (sign_track) over its planned one (t2s_track) over
        GTFS-realtime's ("mta"). {trk, src, ch (the stop's s), name}; None when nothing near says (no GPS guess: the
        tracks are 4 m apart, closer than a phone-grade fix can tell)."""
        if not self.track_lanes:
            return None
        num = lambda v: str(int(v)) if isinstance(v, str) and v.strip().isdigit() else (v or "").strip() if isinstance(v, str) else ""
        cands = []
        tt = t.get("tt") or {}
        for x in (tt.get("prev"), tt.get("next")):
            if x and x.get("track") and x.get("code") in self.by_code:
                cands.append((num(x["track"]), "posted" if x.get("posted") else "planned", self.by_code[x["code"]], x.get("name") or x["code"]))
        for k in (p, n):
            if k is not None and anchors[k][0].get("track"):
                st = anchors[k][0]
                cands.append((num(st["track"]), "mta", anchors[k][1], self.stops.get(st["stop_id"], st["stop_id"])))
        ok = [c for c in cands if abs(c[2] - s) <= TRACK_NEAR_M and self.track_at(c[0], s) is not None]
        if not ok:
            return None
        trk, src, ch, name = min(ok, key=lambda c: (round(abs(c[2] - s)), -TRACK_RANK[c[1]]))
        return {"trk": trk, "src": src, "ch": ch, "name": name}

    def which_track(self, t: dict, anchors: list, p: int | None, n: int | None, s: float, d: int,
                    mem: dict | None = None) -> tuple[str, dict | None, str]:
        """(the numbered track the train is drawn on, the evidence it is drawn by or None, how sure: "said" | "held" |
        "outer") (#371). "said": within TRACK_NEAR_M of the stop that said it; "held": the last stop's word, kept up to
        TRACK_HOLD_M from that stop (beyond, an express may have crossed over: not held); "outer": nothing says, so the
        direction's outer track (3 inbound, 4 outbound), only so it lies on a drawn track (an express may well be on
        an inner one): not known. mem: what we remember of this train (Tracker.trk_mem): its track changes only on
        better stop-track evidence (_better), so a merge flipping between TrainTime and GTFS-realtime doesn't move it.
        ("", None, "") with no numbered track here."""
        if not self.track_lanes:
            return "", None, ""
        ev = self.track_evidence(t, anchors, p, n, s)
        if mem is not None:
            old = mem.get("ev")
            if ev is not None and (old is None or self._better(ev, old, d)):
                mem["ev"] = ev
            ev = mem.get("ev")
        if ev is not None and abs(ev["ch"] - s) <= TRACK_HOLD_M and self.track_at(ev["trk"], s) is not None:
            return ev["trk"], ev, "said" if abs(ev["ch"] - s) <= TRACK_NEAR_M else "held"
        outer = OUTER_TRACK.get(d, "")
        return (outer, None, "outer") if self.track_at(outer, s) is not None else ("", None, "")

    @staticmethod
    def _better(ev: dict, old: dict, d: int) -> bool:
        """New stop-track evidence replaces what a train's track was drawn by (#371): for the same stop, only an equal or
        better word (posted over planned over GTFS-realtime; the same track from a weaker source keeps the stronger
        one); another stop's, only when that stop is ahead of the old one in the direction of travel (a merge
        alternating TrainTime's next stop with GTFS-realtime's last one would flip it back and forth)."""
        if round(ev["ch"]) == round(old["ch"]):
            return TRACK_RANK[ev["src"]] >= TRACK_RANK[old["src"]]
        return (ev["ch"] - old["ch"]) * d > 0 if d in (1, -1) else True

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
        if t.get("dir_hint") in (1, -1):                # TrainTime's direction (E: toward New Haven, #362)
            return t["dir_hint"]
        try:
            return 1 if int(re.sub(r"\D", "", t["train"]) or "1") % 2 == 0 else -1    # Metro-North: even numbers outbound
        except ValueError:
            return 1

    def tracked(self, t: dict) -> bool:
        """The feed follows this train (a vehicle, or a stop it has left or reached): it leaves a stop only when
        the feed says so. A train it doesn't follow has only its predicted times to go by."""
        return bool(t.get("pos") or t.get("vstatus") or any(s["status"] in GONE or s["status"] == "Arrived" for s in t["stops"]))

    def legs(self, t: dict, anchors: list, now: float) -> tuple[int | None, int | None]:
        """(last, next): indexes in anchors of the stop the train last left and the one it is going to or standing
        at. The vehicle's stop when it is one of them; else, for a train the feed follows, the first it hasn't
        left (the MTA's statuses); else the first whose time hasn't passed. next None: beyond its last."""
        vs = t.get("vstop") or ""
        i = next((k for k, (s, _) in enumerate(anchors) if s["stop_id"] == vs), None) if vs else None
        if i is None:
            if self.tracked(t):
                i = next((k for k, (s, _) in enumerate(anchors) if s["status"] not in GONE), None)
            else:
                i = next((k for k, (s, _) in enumerate(anchors) if (_t_dep(s) or 0) >= now), None)
        if i is None:
            return (len(anchors) - 1 if anchors else None), None
        return (i - 1 if i > 0 else None), i

    def judge(self, t: dict, anchors: list, d: int, now: float, dist: float, s: float, last: dict | None, at: float) -> str:
        """Why the feed's position for a train (dist off the line, s along it, in the feed of time at) can't be
        right, or "" when it can: off the line; outside the stretch between the stop it last left and the one it
        is going to; against its last good fix (when that is under RESET_S old): backwards, or faster than a
        train goes once one reporting point's spacing is allowed for."""
        if dist > MAIN_M:
            return "off the line"
        p, n = self.legs(t, anchors, now)
        if n is not None:
            if (s - anchors[n][1]) * d > STOP_SLACK_M:
                return "beyond its next stop"
            if p is not None and (anchors[p][1] - s) * d > STOP_SLACK_M:
                return "behind the stop it left"
        if last is not None and 0 < at - last["fix_at"] <= RESET_S:
            step = (s - last["s"]) * d
            if step < -BACK_M:
                return "backwards"
            if (abs(step) - SPACING_M) / (at - last["fix_at"]) > MAX_MPS:
                return "too fast"
        return ""

    def place(self, t: dict, anchors: list, d: int, now: float, fix: dict | None, points: Points | None = None,
              mem: dict | None = None) -> dict | None:
        """Where the train is drawn, and how far the page may carry it on: {s, lat, lon, bearing, mode, estimated,
        fix_at, fix_age_unknown, next, prev_s, hold_s, hold_kind, predict_until, segment, lag_s}; None when it
        isn't on the line now.
          a good fix   there: "reported", or "stopped" when the vehicle says STOPPED_AT. hold_s: the next known
                       reporting point ahead (points), else HOLD_FALLBACK_M on; never past its next stop.
          none         "estimated" on its run from the stop it left to the next (profile_s), or "stopped" at a
                       stop: waiting to leave, or arrived. hold_s: the next stop.
        segment, the run (profile_s) from the stop it last left to the next, only once the feed says it left
        (that stop's Departed, or the vehicle in transit to the next): never on the timetable alone; a train the
        feed doesn't follow at all (no vehicle, no status) runs on its predicted times. lag_s: how much later
        than that run the train reached its fix (the page shifts the run by it)."""
        p, n = self.legs(t, anchors, now)
        prev, nxt = (anchors[p] if p is not None else None), (anchors[n] if n is not None else None)
        vstatus = t.get("vstatus") or ""
        left = vstatus in MOVING or (prev is not None and prev[0]["status"] in GONE) or not self.tracked(t)
        seg = None
        if prev is not None and nxt is not None and left and vstatus != "STOPPED_AT" and nxt[0]["status"] != "Arrived":
            seg = {"from": prev[0]["stop_id"], "to": nxt[0]["stop_id"]} | segment(prev[1], nxt[1], _t_dep(prev[0]), _t_arr(nxt[0]))
        lag = None
        if fix is not None:
            s = fix["s"]
            mode = "stopped" if vstatus == "STOPPED_AT" or fix.get("v") == 0.0 else "reported"
            if seg is not None:
                lag = fix["fix_at"] - profile_t(s, seg)
        else:
            if not anchors or now < _t_arr(anchors[0][0]):
                return None                              # not on the main line yet
            if nxt is None:                              # beyond its last stop on the line
                if now - _t_dep(anchors[-1][0]) > GONE_S:
                    return None
                s, mode = anchors[-1][1], "stopped"
            elif seg is not None:
                s, v = profile_s(now, seg)
                mode = "estimated" if v > 0 else "stopped"
            elif prev is None or vstatus == "STOPPED_AT" or nxt[0]["status"] == "Arrived":
                s, mode = nxt[1], "stopped"              # at its first stop, or the feed says it is at the next
            else:
                s, mode = prev[1], "stopped"             # the feed hasn't said it left
        lane, ev, how = self.which_track(t, anchors, p, n, s, d, mem)
        lat, lon = self.where(d, s, lane)                # on its own numbered track, else its direction's
        hold_s, kind = s, "stop"
        if mode != "stopped":
            stop = nxt[1] if nxt is not None else None
            if fix is not None:
                # a GPS fix (TrainTime, #362) isn't at one of the MTA's reporting points: the fallback distance
                ahead = points.ahead(s, d) if points is not None and fix.get("src", "gtfs") == "gtfs" else None
                fall = HOLD_FALLBACK_TT_M if fix.get("src") == "traintime" else HOLD_FALLBACK_M
                hold_s, kind = (ahead, "reporting_point") if ahead is not None else (s + d * fall, "fallback")
                if stop is not None and (hold_s - stop) * d > 0:
                    hold_s, kind = stop, "stop"
            elif stop is not None:
                hold_s = stop
            if (hold_s - s) * d < 0:                     # never backwards (a fix just past its stop's mark)
                hold_s, kind = s, "stop"
            hold_s = max(0.0, min(self.line.length, hold_s))
        b = self.line.heading(s)
        bearing = fix["bearing"] if fix is not None and fix.get("bearing") is not None else (b if d > 0 else (b + 180) % 360)
        nx = None
        if nxt is not None:
            st = nxt[0]
            nx = {"stop_id": st["stop_id"], "name": self.stops.get(st["stop_id"], st["stop_id"]), "s": round(nxt[1], 1),
                  "arr": _t_arr(st), "dep": _t_dep(st), "status": st["status"], "track": st["track"]}
        return {"s": round(s, 1), "lat": lat, "lon": lon, "bearing": round(bearing), "mode": mode, "estimated": fix is None,
                "fix_at": fix["fix_at"] if fix is not None else None, "fix_age_unknown": bool(fix is not None and fix["first_read"]),
                "next": nx, "prev_s": round(prev[1], 1) if prev is not None else None, "hold_s": round(hold_s, 1),
                "hold_kind": kind, "predict_until": (fix["fix_at"] + (TT_HORIZON_S if fix.get("src") == "traintime" else PREDICT_S)
                                                     if fix is not None else None),
                "segment": seg, "lag_s": round(lag, 1) if lag is not None else None,
                **({"lane": lane, "lane_how": how} if lane else {}),
                **({"trk": ev["trk"], "trk_src": ev["src"], "trk_at": ev["name"]} if ev and how == "said" else {}),
                **({"lane_at": ev["name"]} if ev and how == "held" else {}),
                **self.ref.at_s(s),
                **({"v_fix": fix["v"], "a_fix": fix.get("a") or 0.0, "v_gps": fix.get("vf_raw"), "v_moved": fix.get("moved"),
                    "v_bad": fix.get("bad") or ""} if fix is not None and fix.get("v") is not None else {})}

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

    def branch(self, t: dict) -> bool:
        """A branch's train (New Canaan, Danbury, Waterbury): off the main line on its own tracks for part of its run."""
        return bool(t["route_id"]) and self.routes.get(t["route_id"], MAIN) != MAIN

    def ours_train(self, t: dict, now: float) -> bool:
        """On the New Haven Line (or a branch) and not finished."""
        if self.ours and t["route_id"] and t["route_id"] not in self.ours:
            return False
        times = [x for s in t["stops"] for x in (s["arr"], s["dep"]) if x]
        return bool(times) and max(times) >= now - GONE_S or (not times and t.get("pos") is not None)

    def trains(self, feed: dict, now: float, track: Tracker | None = None) -> list[dict]:
        """The trains on the line in our area now, for the map (place). track: the fixes across reads (Trains
        keeps one); without, this read alone is judged."""
        if track is None:
            track = Tracker()
            track.update(self, feed, now)
        for k in [k for k, v in track.trk_mem.items() if now - v.get("seen", now) > TRACK_MEM_S]:
            del track.trk_mem[k]
        out: dict[str, dict] = {}
        for t in feed.get("trains", {}).values():
            if not self.ours_train(t, now) or not t["stops"] and not t.get("pos"):
                continue
            anchors = self.anchors(t)
            d = self.direction(t, anchors)
            mem = track.trk_mem.setdefault(t["train"], {})   # its track across reads and sources (#371)
            mem["seen"] = now
            p = self.place(t, anchors, d, now, track.fix(t["id"]), track.points, mem)
            if p is None or not self.in_area(p["s"], 200.0):
                continue
            key, label = DIRS[d]
            x = (self.facts(t, now) | p | {"id": t["id"], "dir": key, "dir_label": label, "d": d, "lat": round(p["lat"], DP),
                                            "lon": round(p["lon"], DP), "vehicle_status": t.get("vstatus") or "",
                                            "vehicle_stop_id": t.get("vstop") or ""}
                 | ({"cars": t["cars"]} if t.get("cars") else {}) | self.tt_facts(t, p, now))
            other = out.get(x["train"])                  # one train drawn once: a reported one over an estimate of it
            if other is None or other["fix_at"] is None and x["fix_at"] is not None:
                out[x["train"]] = x
        return sorted(out.values(), key=lambda x: (x["dir"], x["train"]))

    def tt_facts(self, t: dict, p: dict, now: float | None = None) -> dict:
        """What MTA TrainTime adds to a train (#362): "tt" (sizeup/traintime.py facts), its car count as "cars", and
        "mp": the railroad milepost read off Metro-North's mileposts where we draw it (#371), else from TrainTime's
        turf, else from where we place it on the line (miles from Grand Central); "pos_src": whose position it is
        drawn at."""
        tt = t.get("tt")
        if not tt:
            return {}
        fix = p.get("fix_at") is not None and (t.get("pos") or {}).get("src") == "traintime"
        mp = p.get("mp") if p.get("mp") is not None else tt.get("mp")
        out = {"tt": tt, "pos_src": "traintime" if fix else "gtfs",
               "mp": mp if mp is not None else (round(p["s"] / MILE_M, 1) if fix else None)}
        if tt.get("cars"):
            out["cars"] = min(int(tt["cars"]), MAX_CARS)
        if fix and now is not None:
            out["stale"] = now - p["fix_at"] > STALE_S   # by its own fix, not the GTFS-realtime feed's age
        rec_v = p.get("v_fix")
        if fix and isinstance(rec_v, (int, float)):
            out["v"] = round(rec_v, 2)                   # m/s, filtered (along_filter): the page carries it on at this
            out["a"] = round(p.get("a_fix") or 0.0, 2)   # m/s^2: falling, the page brakes it to a stop (#375)
            # the speed's basis, for the box's hover (#390): the GPS reading and the distance moved over time (m/s)
            out["v_basis"] = {k: (round(p[f], 2) if isinstance(p.get(f), (int, float)) else None)
                              for k, f in (("gps", "v_gps"), ("moved", "v_moved"))} | {"bad": p.get("v_bad") or ""}
        if fix:
            out["dyn"] = consist(tt.get("fleet"), tt.get("cars"))   # what the consist can do: motion.js's limits
        return out

    def near(self, lat: float, lon: float) -> dict | None:
        """The event's reference point on the line: the station when one is within NEAR_M, else the nearest point
        of the tracks when within NEAR_M; None when neither. Either way, when the point is within NEAR_M of the line:
        the nearest catenary structure ("pole") and the milepost ("mp") there (#371), when known."""
        dist, ch = self.line.project(lat, lon)
        loc = self.ref.at_s(ch) if dist <= NEAR_M else {}
        st = min(self.stations.values(), key=lambda s: metres(lat, lon, s["lat"], s["lon"]), default=None)
        st_m = metres(lat, lon, st["lat"], st["lon"]) if st else math.inf
        if st is not None and st_m <= NEAR_M:
            return {"kind": "station", "name": st["name"], "station": st["id"], "ch": st["ch"], "m": round(st_m),
                    "lat": st["lat"], "lon": st["lon"]} | loc
        if dist > NEAR_M:
            return None
        by_ch = min(self.stations.values(), key=lambda s: abs(s["ch"] - ch))
        p = self.line.at(ch)
        return {"kind": "track", "name": by_ch["name"], "station": by_ch["id"], "ch": round(ch, 1), "m": round(dist),
                "station_m": round(abs(by_ch["ch"] - ch)), "side": "east" if ch > by_ch["ch"] else "west",
                "lat": round(p[0], 6), "lon": round(p[1], 6)} | loc

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

    def approaching(self, feed: dict, lat: float, lon: float, now: float, track: Tracker | None = None) -> dict:
        ref = self.near(lat, lon)
        if ref is None:
            return {"near": False}
        if track is None:
            track = Tracker()
            track.update(self, feed, now)
        out = {"inbound": [], "outbound": []}
        for t in feed.get("trains", {}).values():
            if not self.ours_train(t, now):
                continue
            anchors = self.anchors(t)
            if not anchors:
                continue
            d = self.direction(t, anchors)
            p = track.fix(t["id"])                      # its last good fix (never a refused one)
            pch = p["s"] if p is not None else None
            if pch is not None and (pch - ref["ch"]) * d > PASSED_M:  # the MTA says it is already beyond the point
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


# ---- fixes across reads, and the reporting points they teach -----------------------------------------------------


class Points:
    """The MTA's reporting points on the line (its positions snap to them, about 1 to 1.6 km apart), learned from
    good fixes: {lat, lon, s, n (times a train newly reached it), in, out (of those, each way), first, last (feed
    times)}, kept in order along the line. A point seen POINT_KNOWN_N times is known (one sighting could be a
    fluke of a single train). s is the point's distance along the line now loaded (rebase on a new one)."""

    def __init__(self):
        self.pts: list[dict] = []
        self.dirty = False

    def __len__(self) -> int:
        return len(self.pts)

    @property
    def known(self) -> int:
        return sum(1 for p in self.pts if p["n"] >= POINT_KNOWN_N)

    def _nearest(self, s: float) -> dict | None:
        """The point within CLUSTER_M of s along the line, the nearest."""
        best = min(self.pts, key=lambda p: abs(p["s"] - s), default=None)
        return best if best is not None and abs(best["s"] - s) <= CLUSTER_M else None

    def learn(self, lat: float, lon: float, s: float, d: int, at: float) -> None:
        p = self._nearest(s)
        if p is None:
            p = {"lat": lat, "lon": lon, "s": round(s, 1), "n": 0, "in": 0, "out": 0, "first": at, "last": at}
            self.pts.append(p)
            self.pts.sort(key=lambda q: q["s"])
        p["n"] += 1
        p["out" if d > 0 else "in"] += 1
        p["last"] = max(p["last"], at)
        self.dirty = True

    def ahead(self, s: float, d: int, within: float = POINT_AHEAD_M) -> float | None:
        """The nearest known point beyond s (more than CLUSTER_M on) going d, within reach; None when none is."""
        best = None
        for p in self.pts:
            step = (p["s"] - s) * d
            if p["n"] >= POINT_KNOWN_N and CLUSTER_M < step <= within and (best is None or step < (best - s) * d):
                best = p["s"]
        return best

    def merge(self, doc: dict, line: Line | None = None) -> int:
        """Points from a saved doc ({points: [...]}), those we don't have yet (by lat / lon); how many added."""
        have = {(round(p["lat"], 6), round(p["lon"], 6)) for p in self.pts}
        added = 0
        for q in doc.get("points", []) if isinstance(doc, dict) else []:
            try:
                p = {"lat": float(q["lat"]), "lon": float(q["lon"]), "s": float(q.get("s") or 0.0), "n": int(q["n"]),
                     "in": int(q.get("in", 0)), "out": int(q.get("out", 0)), "first": float(q["first"]), "last": float(q["last"])}
            except (KeyError, TypeError, ValueError):
                continue
            key = (round(p["lat"], 6), round(p["lon"], 6))
            if key in have or p["n"] < 1 or not (-90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180):
                continue
            have.add(key)
            self.pts.append(p)
            added += 1
        if line is not None:
            self.rebase(line)
        else:
            self.pts.sort(key=lambda q: q["s"])
        return added

    def rebase(self, line: Line) -> None:
        """Each point's s on this line (a new static feed's)."""
        for p in self.pts:
            p["s"] = round(line.project(p["lat"], p["lon"])[1], 1)
        self.pts.sort(key=lambda q: q["s"])

    def doc(self) -> dict:
        return {"version": 1, "source": "learned from the MTA's Metro-North GTFS-realtime vehicle positions (sizeup/trains.py)",
                "points": [dict(p) for p in self.pts]}


# A GPS fix (MTA TrainTime, #362) is filtered along the track: an alpha-beta filter on the distance along the line
# (u = s * d, the way the train goes) against the fix's own time, the feed's speed blended in. Cross-track error is
# already gone (the fix is projected onto the line, and the train is drawn on its own track at its s).
AB_ALPHA = 0.5                   # how much of a fix's miss moves the position
AB_BETA = 0.2                    # ... and the speed
AB_FEED = 0.5                    # the feed's own speed's share of the speed
STOP_MPS = 0.5                   # slower than this (about 1 mph): stopped, held where it is
STOP_JITTER_M = 150.0            # a train already stopped: a fix less than this ahead (or any behind) is GPS jitter, it
                                 # stays put. The first stopped fix after it moved is taken as it is (forward only).
A_MIN, A_MAX = -1.5, 1.0         # the filter's acceleration (m/s^2), clamped: past a service brake is noise
A_BRAKE = -0.2                   # falling faster than this (m/s^2): braking (the page brakes it too: motion.js RAIL_BRAKE_A)
TT_HORIZON_S = 75.0              # a GPS fix is carried on at most this long past its time (predict_until). The page runs it
                                 # on at its speed (no slowing on age alone) for RAIL_CRUISE_S (20 s: TrainTime's fixes are
                                 # 3 to 16 s old when read, measured #375, read every 5 s), then eases it to a stop at its
                                 # service brake; the hub's own limits come first (POS_MAX_AGE_S 40 s, STALE_S)
# The feed's speed, checked against the distance moved (speed_check): two GPS fixes' s, along the real track geometry,
# over their own times. TrainTime's GPS speed agrees with that to 0.1 % on 2,900 measured pairs (traintime.py), so a
# reading that disagrees with both the distance moved and the last reading is a bad reading, not a change of speed.
A_PHYS = 1.5                     # m/s^2: no train's speed changes faster (an M7's emergency brake is 3.2 mph/s, 1.43)
MOVED_NOISE_M = 14.0             # two GPS fixes' along-track error (about 10 m each, #375), in the distance moved
MOVED_MIN_S = 2.0                # fixes closer in time than this: no speed from the distance moved
SPEED_TOL = 1.5                  # m/s: two readings this close agree (and a speed jump this much over A_PHYS dt is allowed)
A_CONFIRM = -0.447               # m/s^2 (1 mph/s): one falling reading is braked on only this steep (and the distance moved
                                 # agreeing); gentler, two in a row. (±1 mph of reading noise falls past A_BRAKE about 6 % of
                                 # the time over 5 s; past this, almost never)


# What a consist can do (#390): the drawn train never speeds up or slows down faster than its consist can (motion.js
# railFollow). Per fleet (TrainTime's consist "fleet"): a0, the starting acceleration (m/s^2), tapering at speed as
# power / mass (pw, W/kg = m^2/s^3: a(v) = min(a0, pw / v)); b, its full service brake (the drawn train's limit);
# bs, an ordinary stop's braking (the page brakes a prediction into a scheduled stop at this); vmax, its service speed
# (the drawn train's catch-up never runs it faster, unless the train itself is).
#   M8 (Kawasaki EMU, New Haven Line): 2.0 mph/s starting acceleration, 0.89 m/s^2 (Univ. of New Haven field trial of
#        the M8 against its rated 2.0 mph/s); eight 200 kW (270 hp) motors a married pair of ~64 t cars: 12.4 W/kg;
#        80 mph in Metro-North service (en.wikipedia.org/wiki/M8_(railcar)). No published brake rate: the M7's below.
#   M7 / M7A (Bombardier EMU): 2 mph/s acceleration; 3.0 mph/s service, 3.2 mph/s emergency braking; 80 mph in
#        service (en.wikipedia.org/wiki/M7_(railcar)); its power per kg taken as the M8's (no published figure).
#   Diesel push-pull (GE P32AC-DM + coaches): 275.8 kN starting tractive effort, 2.39 MW, 124.5 t
#        (en.wikipedia.org/wiki/GE_Genesis), published; coaches (Shoreliners) about 50 t each and 1.0 m/s^2 braking
#        for a loco-hauled train, assumed (no published figures). A longer train is heavier: slower to accelerate.
# (Published figures as cited; the M8's brake and power per kg for the M7 are assumed from its sister fleet.)
# An EMU's every car is motored, so its length doesn't change what it can do; a diesel's does (one locomotive).
# An ordinary stop is braked at about 0.8 m/s^2 (the run profile's DECEL is 0.75). Unknown fleet: the M8 (the line's
# usual train), which is the quickest here, so the limits never hold the drawn train back from the real one.
MPHS = 0.44704                   # 1 mph/s in m/s^2
_EMU = {"a0": round(2.0 * MPHS, 2), "pw": 12.4, "b": round(3.0 * MPHS, 2), "bs": 0.8, "vmax": round(80 * MPHS, 1)}
CONSISTS = {"M8": _EMU, "M7": _EMU, "M7A": _EMU}
DIESEL_LOCO = {"te": 275_800.0, "p": 2_390_000.0, "kg": 124_466.0}
COACH_KG = 50_000.0
DIESEL_CARS = 6                  # coaches when the feed doesn't say


def consist(fleet: str | None, cars: int | None) -> dict:
    """{a0, pw, b, bs, vmax}: what this consist can do (CONSISTS)."""
    f = (fleet or "").upper()
    if "DIESEL" in f:
        n = cars - 1 if isinstance(cars, int) and cars > 1 else DIESEL_CARS   # the locomotive is one of the "cars"
        m = DIESEL_LOCO["kg"] + n * COACH_KG
        return {"a0": round(DIESEL_LOCO["te"] / m, 3), "pw": round(DIESEL_LOCO["p"] / m, 2), "b": 1.0, "bs": 0.8,
                "vmax": _EMU["vmax"]}
    return dict(CONSISTS.get(f, _EMU))


def speed_check(last: dict | None, s: float, d: int, t: float, vf: float | None, gps: bool) -> dict:
    """The feed's speed reading vf (m/s) at a fix at s (time t) against the train's last fix {s_raw, fix_at, vf, gps,
    vf_held, a}: {"vf": the speed to use, "moved": the speed from the distance moved along the track (GPS fixes both,
    MOVED_MIN_S apart; else None), "bad": "" | "speed" | "position", "brake": a fall in speed may be braked on}.
      - the distance moved says the train ran at about the last reading, not this one: a bad speed reading; the last
        reading stands ("speed").
      - the speed changed faster than any train can (A_PHYS) and nothing moved confirms it: held too, until a second
        reading agrees (#379: one bad reading is no stop).
      - the reading steady but the distance moved disagrees: the position is off ("position"); the speed stands.
    A fall in speed is braked on (brake) only when it is steep (A_CONFIRM, 1 mph/s) and the distance moved agrees with
    it, or the reading before was falling too, or a held reading is agreed with."""
    out = {"vf": vf, "moved": None, "bad": "", "brake": False, "fell": False}
    if last is None or last.get("s_raw") is None or not (t > last.get("fix_at", t)):
        return out
    dt = t - last["fix_at"]
    if gps and last.get("gps") and dt >= MOVED_MIN_S:
        out["moved"] = max(0.0, (s - last["s_raw"]) * d / dt)
    vl = last.get("vf")
    if last.get("repeat") and isinstance(last.get("vf_raw"), (int, float)):
        vl = last["vf_raw"]                              # the last was the same GPS position again (held at 0): its reading
    if vf is None or not isinstance(vl, (int, float)):
        return out
    moved, tol = out["moved"], (max(SPEED_TOL, 2 * MOVED_NOISE_M / dt + 0.1 * max(vf, vl)) if dt else SPEED_TOL)
    agrees = moved is not None and abs(moved - (vf + vl) / 2) <= tol      # a steady change of speed over the interval
    jump = abs(vf - vl) > A_PHYS * dt + SPEED_TOL
    held = last.get("vf_held")                                          # a reading held back last time
    second = isinstance(held, (int, float)) and abs(vf - held) <= SPEED_TOL
    if moved is not None and not agrees and abs(moved - vl) <= tol and abs(vf - vl) > tol and not second:
        out.update(vf=vl, bad="speed")                                  # it ran on at the last reading
    elif jump and not agrees and not second:
        out.update(vf=vl, bad="speed")                                  # faster than any train: wait for a second one
    elif moved is not None and not agrees and abs(vf - vl) <= SPEED_TOL:
        out["bad"] = "position"
    rate = (vf - vl) / dt if dt else 0.0
    out["fell"] = not out["bad"] and rate < A_BRAKE
    out["brake"] = out["fell"] and ((agrees and rate <= A_CONFIRM) or second or bool(last.get("fell")))
    if out["bad"] == "speed":
        out["held"] = vf
    return out


def along_filter(last: dict | None, s: float, d: int, t: float, v_feed: float | None) -> tuple[float, float, float]:
    """(s, v m/s, a m/s^2) for a new GPS fix measured at s (on the line) at time t, from the train's last filtered fix
    {s, v, a, vf, fix_at} (None: none, or too old): predicted on at its speed (braking at a when it was, to a stop at
    most), corrected by AB_ALPHA / AB_BETA of the miss, the feed's speed blended in; never backwards (a miss behind
    is held at the last position: GPS jitter, or a late fix), never a negative speed. a: the change in the feed's
    own speed (vf) since the last fix, else in the filtered one (the page brakes the train on it when it is falling,
    #375). Stopped (the feed's speed under STOP_MPS): a train that was moving is put
    where the fix says (forward only: the first stopped fix is where it stopped); one already stopped stays put
    unless the fix is more than STOP_JITTER_M ahead."""
    vf = v_feed if isinstance(v_feed, (int, float)) and v_feed >= 0 else None
    if last is not None and last.get("v") is not None and t <= last["fix_at"]:
        return last["s"], last["v"], last.get("a") or 0.0          # not newer than the last: no change
    if last is None or last.get("v") is None or t - last["fix_at"] > RESET_S:
        return s, (0.0 if vf is not None and vf < STOP_MPS else (vf or 0.0)), 0.0
    dt = t - last["fix_at"]
    u_last, u_meas = last["s"] * d, s * d
    if vf is not None and vf < STOP_MPS:
        if last["v"] >= STOP_MPS:                        # it was moving: it stopped here (never back)
            return max(u_last, u_meas) * d, 0.0, 0.0
        return (last["s"], 0.0, 0.0) if u_meas - u_last <= STOP_JITTER_M else (s, 0.0, 0.0)   # already stopped: jitter
    a_last = last.get("a") or 0.0
    vf_last = last.get("vf")
    if vf is not None and isinstance(vf_last, (int, float)):
        u_pred = u_last + (vf_last + vf) / 2 * dt        # the feed's two speeds (checked: speed_check), their mean (#390)
    elif a_last < 0:                                     # braking: on at that rate, to a stop at most
        tb = min(dt, last["v"] / -a_last)
        u_pred = u_last + last["v"] * tb + 0.5 * a_last * tb * tb
    else:
        u_pred = u_last + last["v"] * dt
    miss = u_meas - u_pred
    u = u_pred + AB_ALPHA * miss
    v = max(0.0, last["v"] + AB_BETA * miss / dt)
    if vf is not None:
        v = (1 - AB_FEED) * v + AB_FEED * vf
    if u < u_last:                                       # never backwards: hold
        u = u_last
    dv = vf - vf_last if vf is not None and isinstance(vf_last, (int, float)) else v - last["v"]
    a = min(A_MAX, max(A_MIN, dv / dt))
    if vf is not None and a < A_BRAKE:                   # braking: the feed's own speed (TrainTime's agrees with distance
        v = vf                                           # over time to 0.1 %), not the filter's, which lags it
    return u * d, v, a


class Tracker:
    """Each train's last good fix across the feed's reads ({lat, lon, s, d, bearing, fix_at, first_read, raw, ok}),
    the refused ones counted (rejected, why), and the reporting points the good ones teach (points). A read
    whose header time hasn't moved changes nothing."""

    def __init__(self, points: Points | None = None, log: Callable[[str], None] | None = None):
        self.points = points if points is not None else Points()
        self.log = log
        self.fixes: dict[str, dict] = {}
        self.trk_mem: dict[str, dict] = {}               # train number -> {ev, seen}: its numbered track (#371)
        self.at: float | None = None
        self.rejected = 0
        self.why: dict[str, int] = {}
        self._logged: float | None = None
        self._key: tuple | None = None

    def fix(self, tid: str) -> dict | None:
        """The train's last good fix; None when it has none, or the feed has been giving a refused position, or
        none, for more than FIX_MAX_S since."""
        rec = self.fixes.get(tid)
        if rec is None or rec.get("s") is None:
            return None
        if not (rec["ok"] and rec["present"]) and self.at is not None and self.at - rec["fix_at"] > FIX_MAX_S:
            return None
        return rec

    def update(self, model: Model, feed: dict, now: float) -> bool:
        """Judge this read's positions (Model.judge). True when the read was new."""
        at = feed.get("timestamp") or now
        key = (at, feed.get("tt_at"))                    # TrainTime's reads (#362) are new reads too
        if self.at is not None and (at < self.at or key == self._key):
            return False
        self._key = key
        first = self.at is None
        seen = set()
        for t in feed.get("trains", {}).values():
            if not model.ours_train(t, now):
                continue
            tid, p = t["id"], t.get("pos")
            seen.add(tid)
            rec = self.fixes.get(tid)
            if rec is not None:
                rec["present"] = False
            if not p:
                continue                                 # none this read: its last good fix stands a while (fix)
            raw = (p["lat"], p["lon"])
            src = p.get("src") or "gtfs"                 # "traintime": a GPS fix with its own time (#362)
            if (rec is not None and rec["raw"] == raw and rec["ok"] and rec.get("src", "gtfs") == src
                    and not (src == "traintime" and (p.get("ts") or 0) > rec.get("fix_at", 0))):
                rec["present"] = True                    # where it was: its fix_at stays (a GPS fix: unless it is newer)
                continue
            anchors = model.anchors(t)
            d = model.direction(t, anchors)
            dist, s = model.line.project(*raw)
            if dist > MAIN_M and (model.branch(t) or src == "traintime"):
                continue                                 # out on its branch: not on the line we draw, and not bogus
            # a fix from the other source (TrainTime came or went) isn't judged against this one's
            last = rec if rec is not None and rec.get("s") is not None and rec.get("src", "gtfs") == src else None
            fix_at = min(now, p["ts"]) if src == "traintime" and p.get("ts") else at     # a GPS fix: its own time
            if last is not None and fix_at <= last["fix_at"]:
                rec["present"] = True                    # not newer than its last good fix: no change
                continue
            why = model.judge(t, anchors, d, now, dist, s, last, fix_at)
            if why:
                if rec is None or rec["raw"] != raw:     # each refused position counted once, however long it stays
                    self._refused(t, why, raw, s, dist, at)
                if rec is None:
                    self.fixes[tid] = {"raw": raw, "ok": False, "present": False, "s": None}
                else:
                    rec.update(raw=raw, ok=False)
                continue
            if last is not None and abs(s - last["s"]) <= CLUSTER_M and src == "gtfs":
                rec.update(raw=raw, ok=True, present=True)   # back at (or a hair from) its last good fix: the same one
                continue
            v = a = vf = None
            chk: dict = {}
            s_raw, gps = s, p.get("loc", "GPS") == "GPS"
            if src == "traintime":                       # along the track, filtered (along_filter)
                if last is not None and last["raw"] == raw and gps and last.get("gps", True):
                    # the same GPS position again, newer: it hasn't moved, whatever its speed says (#375). (A SIGNAL
                    # location is a fixed point: the same one again says nothing, #379.)
                    vf, chk = 0.0, {"moved": 0.0, "brake": True, "repeat": True}
                else:
                    chk = speed_check(last, s, d, fix_at, p.get("speed"), gps)
                    vf = chk["vf"]
                s, v, a = along_filter(last, s, d, fix_at, vf)
                if a < A_BRAKE and not chk.get("brake"):
                    a = 0.0                              # a fall nothing confirms: not braked on (#379)
            self.fixes[tid] = {"raw": raw, "ok": True, "present": True, "lat": raw[0], "lon": raw[1], "s": s, "d": d,
                               "bearing": p.get("bearing"), "fix_at": fix_at, "first_read": first and src == "gtfs", "src": src,
                               "v": v, "a": a, "vf": vf, "s_raw": s_raw, "gps": gps, "vf_raw": p.get("speed"),
                               "moved": chk.get("moved"), "bad": chk.get("bad", ""), "fell": chk.get("fell", False),
                               "vf_held": chk.get("held"), "repeat": bool(chk.get("repeat"))}
            if not first and src == "gtfs":              # one we saw appear: a point of the MTA's (GPS fixes are anywhere)
                self.points.learn(raw[0], raw[1], s, d, at)
        for tid in [k for k in self.fixes if k not in seen]:
            del self.fixes[tid]
        self.at = at
        return True

    def _refused(self, t: dict, why: str, raw: tuple[float, float], s: float, dist: float, at: float) -> None:
        self.rejected += 1
        self.why[why] = self.why.get(why, 0) + 1
        if self.log is not None and (self._logged is None or at - self._logged >= REJECT_LOG_S):
            self._logged = at
            self.log(f"trains: train {t['train']}'s position {raw[0]:.5f},{raw[1]:.5f} refused ({why}: {s / 1000:.1f} km along "
                     f"the line, {dist:.0f} m off it); {self.rejected} refused since start "
                     f"({', '.join(f'{k} {v}' for k, v in sorted(self.why.items()))})")

    def rebase(self, model: Model) -> None:
        """A new line (the static feed's): each fix and point's s on it."""
        for rec in self.fixes.values():
            if rec.get("s") is not None:
                rec["s"] = model.line.project(rec["lat"], rec["lon"])[1]
        self.points.rebase(model.line)


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
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time, every_s: float = RT_EVERY_S,
                 seed: str | Path | None = SEED_POINTS, tracks: str | Path | None = TRACKS_FILE,
                 traintime: bool = True, tt_url: str = "", tt_every_s: float = 5.0, tt_fetch: Callable | None = None,
                 rail_assets: bool = True, assets_fetch: Callable | None = None):
        from sizeup import traintime as tt
        self.enabled = bool(enabled)
        self.every_s = max(RT_EVERY_RANGE_S[0], min(RT_EVERY_RANGE_S[1], float(every_s)))
        self.rt_url = (rt_url or RT_URL).strip() if enabled else ""
        self.gtfs_url = (gtfs_url or GTFS_URL).strip() if enabled else ""
        self.key = (key or "").strip()
        self.bbox = tuple(float(x) for x in bbox)
        self.cache = Path(cache_dir) / "static.json" if cache_dir else None
        self.points_file = Path(cache_dir) / "points.json" if cache_dir else None
        self.fetch = fetch or (lambda url, timeout, limit, headers=None: http_get(url, timeout, limit, headers))
        self.log, self.clock = log, clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._fresh = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.model: Model | None = None
        self.feed: dict = {"timestamp": None, "trains": {}}
        self.view: dict = self.feed                       # the feed with MTA TrainTime merged in (#362), or the feed
        self.asked = 0.0                                  # the last time a viewer asked (the realtime feed's demand)
        self.viewed = 0.0                                 # the last time a page asked for the layer (TrainTime's fast rate)
        # the emergency incidents' (lat, lon), active or pending and changed within traintime.HOT_AGE_S (the app sets
        # it: sizeup/app.py); one within traintime.NEAR_LINE_M of the line puts TrainTime on its fast rate. (No grade
        # crossing on our stretch of the New Haven Line, and no crossing data: the line itself is the test.)
        self.hot_points: Callable[[], list] = lambda: []
        self._hot = (-1e18, False)
        self.on_change: Callable[[], None] | None = None   # the view changed: push it (the app: the state stream's poke)
        self.rev = 0                                      # bumped with each new view (the state's trains_rev)
        self._tt_missed: set = set()
        self._gtfs_pos: dict[str, tuple] = {}            # train id -> (its GTFS-realtime position, when first seen)
        self._gtfs_pos_init = False
        self.rt = {"at": None, "error": "", "next": 0.0, "fails": 0, "n": 0}
        self.st = {"at": None, "error": "", "next": 0.0, "fails": 0, "modified": ""}
        self.points = Points()
        self.track = Tracker(self.points, log)
        self.tracks = self._read_json(tracks) if tracks and self.enabled else None
        self._points_saved = 0.0
        self.geo_at = 0.0                                 # the model last rebuilt with new tracks, poles, mileposts (#371)
        # Metro-North's numbered tracks, catenary structures and mileposts (sizeup/railassets.py, #371): monthly
        self.assets = railassets.RailAssets(bool(rail_assets) and self.enabled, self.bbox, Path(cache_dir) if cache_dir else None,
                                            fetch=assets_fetch, log=log, clock=clock, on_update=self._assets_updated)
        # MTA TrainTime (sizeup/traintime.py, #362): GPS, consist, load, milepost, track, OTP; GTFS-realtime the fallback
        self.tt = tt.TrainTime(bool(traintime) and self.enabled, tt_url, tt_every_s, self.bbox,
                               Path(cache_dir) if cache_dir else None, fetch=tt_fetch, log=log, clock=clock,
                               demand=lambda: self.clock() - self.asked <= DEMAND_S, on_update=self._remerge,
                               fast=self.tt_fast)
        self._load()
        self._load_points(seed)

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print) -> Trains:
        state = Path(cfg.cad_db).parent if getattr(cfg, "cad_db", "") else None
        return cls(getattr(cfg, "trains_enabled", True), getattr(cfg, "trains_rt_url", ""), getattr(cfg, "trains_gtfs_url", ""),
                   getattr(cfg, "trains_key", ""), getattr(cfg, "trains_bbox", DEFAULT_BBOX),
                   (state / "trains") if state else None, log=log, every_s=getattr(cfg, "trains_rt_every_s", RT_EVERY_S),
                   traintime=getattr(cfg, "trains_traintime", True), tt_url=getattr(cfg, "trains_traintime_url", ""),
                   tt_every_s=getattr(cfg, "trains_traintime_every_s", 5.0))

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.rt_url) and bool(self.gtfs_url)

    def why_not(self) -> str:
        if not self.enabled:
            return "off ([trains] enabled = false)"
        if not self.configured:
            return "not configured (no MTA feed address)"
        if self.model is None:
            err = self.st["error"]
            return "line not loaded yet" + (f" ({err})" if err else "")
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
            self.log(f"trains: Metro-North (MTA GTFS-realtime every {self.every_s:g} s while the layer or an event panel "
                     f"is open; stations and line weekly), area {self.bbox}; {self.points.known} reporting points known")
        self.tt.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.tt.stop()
        self._save_points()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            # up to 2 s, or to the realtime read when that is sooner (every 5 s means 5, not the next 2 s turn)
            now = self.clock()
            self._wake.wait(max(0.1, min(2.0, self.rt["next"] - now)) if now - self.asked <= DEMAND_S else 2.0)
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
        stale = self.rt["at"] is None or now - self.rt["at"] > 2 * self.every_s
        self.asked = now
        if not stale or not self.configured:
            return
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            with self._fresh:
                self._fresh.wait_for(lambda: self.rt["at"] is not None and self.clock() - self.rt["at"] <= 2 * self.every_s
                                     or bool(self.rt["error"]) or self.model is None and bool(self.st["error"]),
                                     timeout=WAIT_FIRST_S)

    def _failed(self, st: dict, now: float, code: int, why: str, backoff: tuple[float, float], what: str) -> bool:
        st["fails"] += 1
        if code in (401, 403):
            wait, why = REFUSED_S, f"the MTA refused the request ({why}){'' if self.key else '; a key may be needed now (SIZEUP_MTA_KEY)'}"
        elif code == 429:
            wait, why = RATE_LIMITED_S, f"MTA rate limit ({why})"
        else:
            wait = min(backoff[1], backoff[0] * 2 ** min(st["fails"] - 1, 30))
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
            self._remerge_locked(now)
            self.rt.update(at=now, error="", fails=0, next=now + self.every_s, n=len(feed["trains"]))
            self._fresh.notify_all()
        if self.points.dirty and now - self._points_saved >= POINTS_SAVE_S:
            self._save_points()
        return True

    def _remerge(self) -> None:
        """MTA TrainTime read (or fell back): the view again (TrainTime's thread), pushed to the pages at once."""
        with self._fresh:
            self._remerge_locked(self.clock())
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception as e:  # noqa: BLE001
                self.log(f"trains: push: {type(e).__name__}: {e}")

    def _gtfs_first_seen(self) -> dict[str, float]:
        """{train id: when its current GTFS-realtime position first appeared (the feed's time)}: how new it is."""
        ts = self.feed.get("timestamp") or self.clock()
        first = not self._gtfs_pos_init                  # at our first read a position's age is unknown: 0 (any newer wins)
        self._gtfs_pos_init = True
        seen = {}
        for k, t in self.feed.get("trains", {}).items():
            p = t.get("pos")
            if not p:
                continue
            raw = (p["lat"], p["lon"])
            old = self._gtfs_pos.get(k)
            seen[k] = old[1] if old is not None and old[0] == raw else (0.0 if first else ts)
        self._gtfs_pos = {k: ((t["pos"]["lat"], t["pos"]["lon"]), seen[k]) for k, t in self.feed.get("trains", {}).items() if k in seen}
        return seen

    def tt_fast(self) -> bool:
        """TrainTime's fast rate: a page has the layer on (asked within VIEW_S), or a recent emergency incident
        (hot_points) is within NEAR_LINE_M of the line (looked at every HOT_CHECK_S)."""
        from sizeup import traintime as tt
        now = self.clock()
        if now - self.viewed <= tt.VIEW_S:
            return True
        if now - self._hot[0] < tt.HOT_CHECK_S:
            return self._hot[1]
        hot = False
        try:
            pts = list(self.hot_points() or [])
        except Exception as e:  # noqa: BLE001 - the CAD's trouble never stops the trains
            self.log(f"trains: incidents for the poll rate: {type(e).__name__}: {e}")
            pts = []
        m = self.model
        for p in pts:
            try:
                lat, lon = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                continue
            if m is not None:
                dist, ch = m.line.project(lat, lon)
                if dist <= tt.NEAR_LINE_M and m.in_area(ch, 5000.0):
                    hot = True
                    break
        self._hot = (now, hot)
        return hot

    def _remerge_locked(self, now: float) -> None:
        """The view: the GTFS-realtime feed with TrainTime's positions and facts merged in by train number and run date
        while TrainTime is good (sizeup/traintime.py merge), else the feed alone; its positions judged (Tracker)."""
        from sizeup import traintime as tt
        recs = self.tt.current()
        if recs:
            codes = {s["code"]: s["name"] for s in self.model.stations.values() if s.get("code")} if self.model is not None else {}
            self.view = tt.merge(self.feed, recs, now, self.tt.st["at"], codes, self._gtfs_first_seen())
            for num, day, days in self.view.get("tt_misses") or []:
                if (num, day) not in self._tt_missed:    # once each: a run date that doesn't line up (midnight, #362)
                    self._tt_missed.add((num, day))
                    self.log(f"trains: train {num}: GTFS-realtime run date {day}, TrainTime has {', '.join(days)}; not merged")
        else:
            self.view = self.feed
        self.rev += 1
        if self.model is not None:
            self.track.update(self.model, self.view, now)

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
            model = Model(static, self.bbox, self.tracks, self.assets.doc)
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
            self.track.rebase(model)
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
            model = Model(doc["static"], self.bbox, self.tracks, self.assets.doc)
            if not model.area_stations:
                return
            self.model = model
            self.st.update(at=at, next=at + STATIC_EVERY_S, modified=str(doc.get("modified", "")))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            self.log(f"trains: cache {self.cache.name} not read ({type(e).__name__}); fetching afresh")

    def refresh_assets(self) -> bool:
        """Metro-North's tracks, poles and mileposts now (the Data sources registry, monthly). Never raises."""
        return self.assets.refresh()

    def _assets_updated(self) -> None:
        """New tracks, poles or mileposts: the model again with them (the line and stations as they are)."""
        m = self.model
        if m is None:
            return
        model = Model(m.static, self.bbox, self.tracks, self.assets.doc)
        with self._fresh:
            self.model = model
            self.track.rebase(model)
            self.geo_at = self.clock()
        self.log(f"trains: {len(model.track_lanes)} numbered tracks on the line ({', '.join(sorted(model.track_lanes, key=int))}), "
                 f"{len(model.ref.poles)} poles, {len(model.ref.mps)} mileposts")

    def static_key(self) -> float | None:
        """What the page's ?have= names: the line and stations it holds, and the tracks with them (#371)."""
        at = self.st["at"]
        return None if at is None else max(at, self.geo_at)

    def _read_json(self, f: str | Path) -> dict | None:
        try:
            return json.loads(Path(f).read_text())
        except (OSError, ValueError) as e:
            self.log(f"trains: {Path(f).name} not read ({type(e).__name__}); trains drawn on the line itself")
            return None

    def _load_points(self, seed: str | Path | None) -> None:
        """The reporting points learned before (state dir, trains/points.json), then the shipped seed's (data/)
        we haven't seen ourselves."""
        if not self.configured:
            return
        line = self.model.line if self.model is not None else None
        for f in (self.points_file, Path(seed) if seed else None):
            if f is None or not f.is_file():
                continue
            try:
                self.points.merge(json.loads(f.read_text()), line)
            except (OSError, ValueError) as e:
                self.log(f"trains: reporting points {f.name} not read ({type(e).__name__})")
        self.points.dirty = False

    def _save_points(self) -> None:
        if not self.points_file or not self.points.dirty:
            return
        with self._lock:
            doc = self.points.doc()
            self.points.dirty = False
        self._points_saved = self.clock()
        try:
            self.points_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.points_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(self.points_file)
        except OSError as e:
            self.log(f"trains: reporting points not saved ({type(e).__name__}: {e})")

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

    def status(self) -> dict:
        """How the realtime feed is doing: its time and age, when we last read it, how often, the positions refused
        (and why), the reporting points known."""
        at, now = self.feed.get("timestamp"), self.clock()
        age = round(now - at, 1) if at else None
        return {"at": at, "fetched": self.rt["at"], "fetched_at": self.rt["at"], "age": age,
                "stale": age is None or age > STALE_S, "every_s": self.every_s, "error": self.rt["error"],
                "rejected": self.track.rejected, "rejected_why": dict(self.track.why), "points": self.points.known,
                "points_seen": len(self.points), "traintime": self.tt.status()}

    def _base(self) -> dict:
        return {"configured": self.configured, "note": self.why_not(), "source": SOURCE, "label": LABEL, "link": LINK_URL,
                "terms": TERMS_URL, "disclaimer": DISCLAIMER, **self.status(), "static_at": self.static_key(),
                "profile": {"accel": ACCEL, "decel": DECEL, "vmax": VMAX, "predict_s": PREDICT_S, "stale_s": STALE_S}}

    def public(self, have: float | None = None) -> dict:
        """The layer: the line and stations in our area, the trains on it now. have: the static_at the page
        already holds the line and stations of: then only the trains (the line doesn't change between them)."""
        out = self._base() | {"line": [], "stations": [], "lanes": {}, "tracks": {}, "railref": {}, "trains": [], "catenary": CATENARY,
                              "bbox": list(self.bbox), "now": self.clock()}
        m = self.model
        if m is None:
            return out
        if have is not None and self.st["at"] is not None and have == self.static_key():
            del out["line"], out["stations"], out["lanes"], out["tracks"], out["railref"]
            out["static_same"] = True
        else:
            lo, hi = m.area
            out["line"] = m.line.clip(max(0.0, lo - 1500), min(m.line.length, hi + 1500), DP)
            out["stations"] = [{k: s[k] for k in ("id", "name", "code", "lat", "lon", "url")} for s in m.area_stations]
            out["lanes"] = {DIRS[d][0]: [[round(a, DP), round(b, DP), round(c, 1)] for a, b, c in lane.coords
                                         if lo - 1500 <= c <= hi + 1500] for d, lane in m.lanes.items()}
            # each numbered track's runs (#371): a train is drawn on its "lane"; the poles' and mileposts' s, so the page
            # reads them at the train's drawn s
            keep = lambda c: lo - 1500 <= c <= hi + 1500
            out["tracks"] = {k: runs for k, lanes in m.track_lanes.items()
                             if (runs := [r for r in ([[round(a, DP_TRACK), round(b, DP_TRACK), round(c, 1)] for a, b, c in lane.coords if keep(c)]
                                                      for lane in lanes) if len(r) >= 2])}
            out["railref"] = {"poles": [[round(x, 1), i] for x, i in m.ref.poles if keep(x)],
                              "mps": [[round(x, 1), v] for x, v in m.ref.mps if keep(x)]}
        with self._lock:
            out["trains"] = m.trains(self.view, self.clock(), self.track)
        if self.tt.active():
            out["alerts"] = list(self.tt.alerts)
        return out

    def stations(self) -> list[dict]:
        """The stations the layer draws ({id, name, lat, lon}, in our area): [] while it draws none (off, or the
        static feed not loaded). The county GIS layer leaves out its own copies of these (sizeup/gis.py, #162)."""
        m = self.model
        if not self.configured or m is None:
            return []
        return [{k: s[k] for k in ("id", "name", "lat", "lon")} for s in m.area_stations]

    def near(self, lat: float, lon: float) -> dict:
        """The event panel: {near: false}, or the trains approaching the point in the next WINDOW_S."""
        m = self.model
        if m is None or m.near(lat, lon) is None:
            return self._base() | {"near": False}
        self.ask()
        with self._lock:
            return self._base() | m.approaching(self.view, lat, lon, self.clock(), self.track)

    def feed_health(self) -> dict:
        """Settings > Health's row for MTA TrainTime (#362): ok, or on GTFS-realtime alone (fallback) and why."""
        s = self.tt.status()
        return {"key": "traintime", "label": "Metro-North: MTA TrainTime", "state": s["state"], "why": s["why"] or s["error"],
                "at": s["at"], "bad": s["bad"], "dropped": s["dropped_total"], "fallback": "GTFS-realtime"}

    def handles(self, path: str) -> bool:
        return path in ("/api/cad/trains", NEAR_PATH, ROUTE_PATH, DEPARTURES_PATH, RAILREF_PATH)

    def area_codes(self) -> dict[str, str]:
        """The stations on the map: {stop code: name} (2PC Port Chester, 2RY Rye...)."""
        m = self.model
        return {s["code"]: s["name"] for s in m.area_stations if s.get("code")} if m is not None else {}

    def get(self, path: str, query: str, headers=None) -> tuple[int, bytes, str, dict]:
        q = parse_qs(query or "")
        one = lambda k: (q.get(k) or [""])[0].strip()
        if path == "/api/cad/trains":
            if self.clock() - self.viewed > traintime.VIEW_S:
                self.tt.wake()                           # the layer just came on: TrainTime's fast rate now
            self.viewed = self.clock()                   # the layer is on in a page: TrainTime fast (#362)
            self.ask()
            try:
                have = float(one("have")) if one("have") else None
            except ValueError:
                have = None
            return 200, json.dumps(self.public(have), separators=(",", ":")).encode(), JSON, {}
        if path == NEAR_PATH:
            try:
                lat, lon = float(one("lat")), float(one("lon"))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except ValueError:
                return 400, b'{"error":"lat, lon: numbers"}', JSON, {}
            return 200, json.dumps(self.near(lat, lon), separators=(",", ":")).encode(), JSON, {}
        if path == RAILREF_PATH:                         # the poles and mileposts layer (#371)
            m = self.model
            return 200, json.dumps(self.assets.layer(m.ref.ticks if m is not None else None), separators=(",", ":")).encode(), JSON, {}
        if path == ROUTE_PATH:                           # {key, runs: [[[lon, lat]...]...]} clipped to the area; runs [] when none
            key = one("key")
            if not re.fullmatch(r"[0-9A-Z]{2,4}>[0-9A-Z]{2,4}", key):
                return 400, b'{"error":"key: FROM>TO stop codes"}', JSON, {}
            return 200, json.dumps({"key": key, "runs": self.tt.route(key) or []}, separators=(",", ":")).encode(), JSON, {}
        if path == DEPARTURES_PATH:                      # a station box open on a page: its next departures
            code, codes = one("station").upper(), self.area_codes()
            if code not in codes:
                return 400, b'{"error":"station: the stop code of a station on the map"}', JSON, {}
            if not self.tt.enabled:
                return 200, json.dumps({"code": code, "name": codes[code], "on": False, "rows": []}).encode(), JSON, {}
            out = self.tt.ask_countdown(code) | {"name": codes[code], "on": True, "source": "MTA TrainTime"}
            names = {s.get("code"): s["name"] for s in self.model.stations.values() if s.get("code")} if self.model else {}
            out["rows"] = [r | {"to_name": names.get(r["to"], r["to"])} for r in out["rows"]]
            return 200, json.dumps(out, separators=(",", ":")).encode(), JSON, {}
        return 404, b'{"error":"not found"}', JSON, {}
