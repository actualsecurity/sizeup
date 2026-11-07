"""Highway layers for the CAD maps (GitHub #73, #86): traffic cameras, incidents and closures, roadwork
and message signs from two public sources, and the mile markers and exits from the gazetteer.

User, 2026-10-13, looking at the Thruway's traveler map around Port Chester: "it looks like this has
location data of all the highway things, including cameras, that would be nice to add in for the
ability to @ and then also, click on camera feeds available from the maps (maybe there is a layers
option to turn on these overlays too on the map)"; then (#86): "they exist on that other website so
we should be able to enumerate them without ny511".

Two providers (Provider), read side by side and merged (Roads):

  Thruway   The New York State Thruway Authority's public data host (webdataapi.thruway.ny.gov, what
            its traveler map reads; https://www.thruway.ny.gov/developers): the camera list, events and
            message signs, as XML, no key, no login. I-95 (New England Thruway) and I-287 (Cross
            Westchester) are Thruway roads, so this covers our two highways. On by default ([roads]
            thruway = true). Public data on a public map, used in a fire department's non-commercial
            dispatch tool with attribution ("Data: New York State Thruway Authority" in the Layers box
            and on the map; its disclaimer, https://www.thruway.ny.gov/legal/disclaimer, bars resale and
            redistribution for profit, which this is not). We send a real User-Agent naming sizeup and a
            contact (UA) and poll gently: cameras hourly, events every 2 min, signs every 5. Nothing is
            done to look like a browser: if the host ever refuses plain clients, that feed stays off and
            the log says so. Its milepost and interchange feeds are not read here: the gazetteer's own
            mile markers (#67, #83) are the map's.
  511NY     NYSDOT's 511NY, on Castle Rock's CARS platform since 2026-09-30 (#73, #188). Three feeds, each
            polled once every 5 minutes at most (511NY's recommendation; NY511_MIN_S), conditional GET (ETag /
            If-Modified-Since) and gzip:
              signs    https://nyapi.carsprogram.org/signs.json: public, no login (about 940 KB statewide, 110 KB
                       gzipped; parse_cars_signs reads it one record at a time and keeps our area only). On by
                       default ([roads] ny511 = true). Its lastUpdated runs about 5 h ahead (Eastern wall time
                       written as UTC): put right against the feed's Last-Modified (_cars_shift). The public copy
                       is cached by CloudFront and can be hours old: the Layers box says how old (SIGNS_STALE_S).
              events   the CARS API (https://nysdot.carsprogram.org/carsapi_v1/, Swagger 2.0 "New York State DOT
                       511 API" 1.2.0): GET /api/events, HTTP Basic (the one 511NY username and password: env
                       SIZEUP_511NY_USER / SIZEUP_511NY_PASS, the hub's SSM /sizeup-hub/511ny-user and -pass). No
                       area filter (only eventClassifications and routeDesignator), so the statewide list, read
                       one record at a time (parse_cars_events). Off until the credentials are there: the Layers
                       box and Settings > Data sources say "waiting for credentials (SSM ...)".
              cameras  the CARS-Hub (https://nysdot.carsprogram.org/hub/, [roads] ny511_hub): its CCTV inventory
                       (data/cctv.xml, TMDD's schema, HTTP Basic, the same username and password; hourly, no
                       validators served), read one camera at a time (parse_carshub_cctv). Compared with ours (the
                       Thruway's and the county's, same_camera) and the count of new ones by road is logged and shown
                       in Data sources; the new ones join the map, the camera wall and the overlay (stills through
                       the snapshot proxy, video through the HLS one, under the same stream cap), and one replaces
                       ours only where it has live video and ours hasn't ([roads] carshub_imagery = false: counted
                       only). The hub's FEU (events) and Waze CIFS feeds aren't read: the API's JSON has the events.
            The credentials are only ever in the Authorization header of the request this module makes (BasicAuth):
            never in a URL, a response, the state, the cache, an error or the log (_redact). Terms (511NY's
            Developer Access Agreement, https://511ny.org/help/24): no fee; we may "redistribute, enhance,
            repackage" but not alter a report; we "must display the time at which the data was published or last
            updated" (every item shows its time); acknowledged as "powered by 511NY" with a link.

Both at once: 511NY, when it is on, wins per item: a Thruway event within DEDUPE_M (50 m, the same layer)
of a 511NY one is left out, and a Thruway sign within SIGN_DEDUPE_M (150 m) of a 511NY one that says the same
thing (the same direction too, when both give one) is one sign (_merged). Cameras the other way round: ours
first, a CARS-Hub camera only where we have none. Every item carries its "source" ("511NY" | "Thruway" |
"County"), shown on the map. The feeds are statewide, so only our area is kept (bbox: Port Chester, Rye Brook and
the mutual-aid neighbours with a margin). Each provider has its own schedule and backoff (refused credentials
wait 30 min, a 429 5 min, anything else 1 min doubling to 15, never under the feed's floor), and one disk cache
holds what each kept (state dir, roads511.json: items and validators only, never a credential or a feed
address), so a restart spends no calls.

Live video (#101): the Thruway's cameras each list an HLS stream (512x288, about 205 kbit/s, 6 s
segments, on nysdot.skyvdn.com, answering a plain client with no key; CORS open). The viewer plays
it, muted, through the hub (Roads.stream: the playlist, its chunklist and the segments, nothing the
playlist didn't name and only from the stream's own folder), only while that viewer is open: a
closed viewer stops asking for the playlist and the hub stops serving the segments after
STREAM_IDLE_S (or at once: GET .../stream/close?v=, #125); stream_max (STREAM_MAX, [roads] stream_max) streams at once
hub-wide (a camera watched by several pages is one), the next viewer gets snapshots. The terms are the
same as for the pictures (the Thruway's disclaimer: no resale or redistribution for profit). 511NY's
camera inventory has a VideoUrl too (kept as "video"); the county's cameras are JPEG only. The
viewer always says which it shows: "Live", or "Snapshot, updated every N s" with the source's cadence.
Every segment is checked before it is served (#276, sizeup/hlsseg.py): whole (the bytes the server said, whole
TS packets, every frame complete; else fetched once more, then a 502, never cached), and repaired where the
origin lost reference frames (about one segment in five on the 511NY cameras): those frames are left out, so the
picture holds instead of smearing sideways. A page's first chunklist starts on an undamaged segment.

Camera state (#276): every camera says {state: ok | stale | offline, why, since}. A camera with live video is
judged by its stream (a stale still never takes it off); a still's age by its source (STALE_IMAGE_BY_SOURCE: 511NY's
are re-taken every few minutes); a camera a read leaves out stays listed, stale, for CAMERA_KEEP_S.

Camera images: the browser never talks to a provider or a camera (the key stays here, and nothing is
mixed content or cross-origin behind Cloudflare Access): GET /api/cad/cameras/<id>/image is fetched
by the hub from the camera's own snapshot URL as its provider lists it (the Thruway's redirect once
to nyssnapshot.com, whose Last-Modified is the image's time), kept IMAGE_TTL_S so a room full of
viewers is one request, a few at a time (IMAGE_PARALLEL). Snapshots only: a camera's video stream
(HLS) is not proxied (every segment would come through the hub, and neither source's terms speak to
re-serving video); "has_video" is kept so a later version can decide.

HTTP (sizeup/web/server.py, any signed-in person: viewers too, all GET):
    GET /api/cad/roads[?rev=]             {configured, note, sources: [{name, label, link, configured, at,
                                           status: {cameras, events, signs: {on, at, n, error}}}], rev, cameras,
                                           events, signs, mileposts, exits}; ?rev= the same rev: {rev, same: true}
    GET /api/cad/cameras?q=&near=lat,lon&n=   {cameras: [...]}: the place search for "@...camera", or the
                                           nearest to a point
    GET /api/cad/cameras?for=lat,lon&label=&address=   the incident viewer's cameras (#95, cameras_for):
                                           {highway: {road, dir, mile} | null, cameras: [{..., rel: before | after |
                                           at | side | nearby, miles, side, facing}], down: [...]} (down cameras, #99)
    GET /api/cad/cameras/<id>/image       the snapshot (image/jpeg ...), X-Image-Time: when it was taken
                                           (the camera's Last-Modified, else when the hub fetched it)
    GET /api/cad/cameras/<id>/stream/playlist.m3u8, .../<chunklist>.m3u8, .../<segment>.ts
                                           live video (#101, Roads.stream): the camera's HLS stream through the hub,
                                           only while a viewer keeps asking for the playlist; a pump keeps the newest
                                           segments fetched ahead of the player (pump_once)
    GET /api/cad/cameras/<id>/stream/status   {open, fps (measured from the segments), ahead, target_s, source_down}
    GET /api/cad/cameras/<id>/stream/close?v=  this page stopped watching (close_stream): the stream ends unless another
                                           page is watching it
    GET /api/cad/cameras?area=ours[&for=lat,lon&label=&address=]   the camera wall (#125, area): every camera in our
                                           area (our fire districts plus a margin; Settings "camera_wall"), down ones
                                           flagged, by road then mileage, an event's before / after pinned first; and
                                           the live-video budget {max, per_viewer, open, streaming}
    GET /api/cad/cameras/<id>/image       If-None-Match / If-Modified-Since / ?since=: 304 when the hub's picture
                                           is no newer; ETag, Last-Modified, X-Image-Cadence on every answer

Place search (#67's "@"): Roads.search_places(q, near, limit) answers in the place search's own
shape ({kind: "camera", label, sub, lat, lon, source, camera}); the app hands it to the CAD as
cad.place_providers.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

from sizeup import hlsseg
from sizeup.geo.places import ROUTES          # each highway's positive direction (NB / EB: the mileage rises that way)

SOURCE = "511NY"
LABEL = "511NY (NYSDOT)"
ATTRIBUTION = "powered by 511NY"
TERMS_URL = "https://511ny.org/help/24"
LINK_URL = "https://511ny.org/"
# 511NY on CARS (2026-09-30): the API (HTTP Basic), the public signs feed, the API's WZDx (sizeup/roadprojects.py).
# Tests set NY511_URLS to {} (tests/conftest.py): nothing reaches 511NY from a test.
CARS_API = "https://nysdot.carsprogram.org/carsapi_v1/api"
CARS_HUB = "https://nysdot.carsprogram.org/hub/"           # [roads] ny511_hub: the CARS-Hub (its feeds under data/)
NY511_URLS = {"events": f"{CARS_API}/events", "signs": "https://nyapi.carsprogram.org/signs.json", "wzdx": f"{CARS_API}/wzdx",
              "hub": CARS_HUB}


def hub_cctv(base: str = "") -> str:
    """The CARS-Hub's CCTV feed from its base address ("" = 511NY's, NY511_URLS "hub"; "off" = none)."""
    if base == "off":
        return ""
    base = base or NY511_URLS.get("hub", "")
    return (base.rstrip("/") + "/data/cctv.xml") if base else ""
NY511_EVERY_S = {"cameras": 3600.0, "events": 300.0, "signs": 300.0}
NY511_MIN_S = {"cameras": 300.0, "events": 300.0, "signs": 300.0}     # 511NY: "once every 5 minutes"
SSM_USER, SSM_PASS = "/sizeup-hub/511ny-user", "/sizeup-hub/511ny-pass"
ENV_USER, ENV_PASS = "SIZEUP_511NY_USER", "SIZEUP_511NY_PASS"
WAITING = f"waiting for credentials (SSM {SSM_USER} and {SSM_PASS})"
CARSHUB = "carshub"
CARSHUB_LABEL = "511NY CARS-Hub (NYSDOT)"
SIGN_DEDUPE_M = 150.0            # a Thruway sign this close to a 511NY one, saying the same, is the same sign
CAMERA_MATCH_M = 150.0           # a CARS-Hub camera this close to one of ours is one we have
SIGNS_STALE_S = 1800.0           # the signs feed published longer ago than this: the Layers box says how old
# What each CARS event's access level allows: the public's and every agency's (we are one); the rest is not for us
CARS_ACCESS = {"PUBLIC_DOMAIN", "ALL_AGENCIES"}
THRUWAY = "Thruway"                                   # the short source name on each item
THRUWAY_LABEL = "New York State Thruway Authority"    # "Data: ..." in the Layers box
THRUWAY_LINK = "https://www.thruway.ny.gov/travelers/map"
THRUWAY_TERMS = "https://www.thruway.ny.gov/legal/disclaimer"
THRUWAY_API = "https://webdataapi.thruway.ny.gov/api/data"
THRUWAY_URLS = {"cameras": f"{THRUWAY_API}/trafficcameras/xml", "events": f"{THRUWAY_API}/events/xml",
                "signs": f"{THRUWAY_API}/dmsstatus/xml"}
THRUWAY_EVERY_S = {"cameras": 3600.0, "events": 120.0, "signs": 300.0}
COUNTY = "County"                                     # Westchester County DPW's traffic cameras (#99)
COUNTY_LABEL = "Westchester County DPW"
COUNTY_LINK = "https://www.westchestergov.com/trafficcamera"
COUNTY_URLS = {"cameras": "https://giswww.westchestergov.com/arcgis/rest/services/MappingWestchesterCounty/MapServer/124/query"
                          "?where=1%3D1&outFields=*&outSR=4326&f=json"}            # a public ArcGIS layer, no token
COUNTY_EVERY_S = {"cameras": 86400.0, "events": 86400.0, "signs": 86400.0}          # the list daily (no events or signs)
COUNTY_IMAGE = "https://www.westchestergov.com/cctv/camera{id}.jpg"
CADENCE_S = {SOURCE: 60, THRUWAY: 60, COUNTY: 8}      # how often each source's snapshots change (the viewer says so)
# A camera is "down" (dim with a badge on the map, demoted and folded away in the panels, "Down since" in
# the viewer) when its latest image is older than STALE_IMAGE_S (the county's 2021 placeholders) or its
# snapshot fails DOWN_FAILS times running; a fresh image brings it back. The hub looks at every camera
# every HEALTH_SWEEP_S so the map knows before anyone opens one.
STALE_IMAGE_S = 300.0
# ... per source where its stills are re-taken less often (#276): the CARS-Hub's NYSDOT stills
# (public.carsprogram.org/cameras/NYSDOT/*.flv.png) were 1 to 14 min old when read on 2026-11-07, so at 300 s about
# half of 511NY's cameras went down at one sweep and came back at the next (on the map, off it; live video refused).
STALE_IMAGE_BY_SOURCE = {SOURCE: 1800.0}
DOWN_FAILS = 3
HEALTH_SWEEP_S = 900.0
# A camera with live video is judged by its stream, not its still (#276): an old or missing still never takes it off
# the map or refuses its video; it is offline only when its stream's source refused as well (STREAM_RETRY_S).
# A camera that drops out of its source's list (a partial or odd read) stays listed, stale ("not in the list"),
# this long before it goes: one source's hiccup never makes a camera vanish.
CAMERA_KEEP_S = 6 * 3600.0
# A plain client that says who it is (no browser impersonation, ever: a host that refuses this is left alone)
from sizeup import USER_AGENT as UA
FEEDS = ("cameras", "events", "signs")
# West, south, east, north: Port Chester and Rye Brook, then Rye, Harrison / Purchase, Mamaroneck,
# Rye Neck, White Plains' east edge, Greenwich CT, and I-287 / I-95 / Hutch / Merritt between them.
DEFAULT_BBOX = (-73.80, 40.92, -73.56, 41.10)
EVERY_S = {"cameras": 3600.0, "events": 90.0, "signs": 300.0}
MIN_EVERY_S = {"cameras": 600.0, "events": 60.0, "signs": 60.0}   # 3 feeds at these: 5 calls a minute, under 10
COUNTY_MIN_S = {"cameras": 3600.0, "events": 3600.0, "signs": 3600.0}
DEDUPE_M = 50.0                  # a Thruway item this close to a 511NY one is the same thing
BACKOFF_S = (60.0, 900.0)        # a failed fetch: 1 min doubling to 15 (never sooner than the feed's floor)
REFUSED_S = 1800.0               # 401 / 403: the key is wrong or not yet active; try again in 30 min
RATE_LIMITED_S = 300.0           # 429
TIMEOUT_S = 20.0
MAX_FEED_BYTES = 40 * 1024 * 1024
IMAGE_TTL_S = 15.0               # a snapshot is served this long before the camera is asked again
IMAGE_STALE_S = 600.0            # the camera doesn't answer: the last one, if it is this recent
IMAGE_TIMEOUT_S = 8.0
IMAGE_MAX_BYTES = 3 * 1024 * 1024
IMAGE_KEEP = 200                 # snapshots kept in memory (least recently used dropped)
IMAGE_PARALLEL = 4               # fetches to the cameras at once
CAMERA_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
CAMERA_PATH = re.compile(r"/api/cad/cameras/([A-Za-z0-9_.:-]{1,64})/image")
# Live video (#101): a camera's HLS stream through the hub, only while a viewer is open
STREAM_PATH = re.compile(r"/api/cad/cameras/([A-Za-z0-9_.:-]{1,64})/stream/(status|close|[A-Za-z0-9_-]{1,80}\.(?:m3u8|ts))")
STREAM_IDLE_S = 30.0             # no playlist request this long: the stream's segments stop (a viewer polls every few s)
# Live streams open at once, hub-wide (a camera watched by several pages is one): the next is refused ([roads]
# stream_max). #125 measured (docs/cloud.md "Camera wall"): each stream is about 26 KB/s in from the source and
# 1.4 % of a core on the hub, so 8 is about 45 % of the hub's 0.25 vCPU at most; our area streams 3 (the wall's
# per-page budget, [roads] wall_streams = 0), which leaves 5 for viewer dialogs of other cameras.
STREAM_MAX = 8
STREAM_MAX_LIMIT = 64
VIEWER_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")   # a page's stream token (?v=): its close ends only its own use
STREAM_TIMEOUT_S = 10.0          # a chunklist or segment
STREAM_OPEN_TIMEOUT_S = 6.0      # the first playlist: a stream that doesn't answer this fast is down at the source
STREAM_RETRY_S = 300.0           # ... and is not asked again for this long (the viewer shows snapshots at once)
STREAM_AHEAD = 3                 # segments the pump keeps fetched ahead of the player, from the latest chunklist
STREAM_LIST_FRESH_S = 2.0        # a chunklist younger than this is served from the pump's copy
STREAM_MAX_BYTES = 4 * 1024 * 1024
STREAM_SEG_TTL_S = 60.0          # a segment kept this long (two viewers of one camera: one fetch)
STREAM_SEG_KEEP = 40
STREAM_DAMAGE_WINDOW = 30        # stream_status's "damaged": of this many latest segments
NEAR_KM = 1.5                    # "Cameras nearby" (the map's nearest-camera lookup): this close to the event
# The incident viewer's cameras (#95): along the road, not as the crow flies
ALONG_MI = 2.0                   # the camera just before / just after the event: this far along the road at most
ON_ROAD_M = 150.0                # an event this close to a road's mile markers is on that road
MARKER_M = 300.0                 # a camera (or a labelled event) this close to a marker takes its mileage
FACING_MI = 0.3                  # a camera on the event's side of the road wins over a nearer one this much nearer
AT_MI = 0.05                     # closer than this along the road: "at the location"
NEARBY_KM = 0.3                  # off the highway (or besides the two): cameras this close, as the crow flies
# The camera wall (#125): every camera in our area. The area is our fire districts (the county GIS layer, ours by
# [cad] monitor_departments; the county's 2011 polygons put Rye Brook inside "PORT CHESTER FD"), else our municipal
# boundaries, else AREA_BBOX, plus a margin so the I-95 / I-287 / Hutch cameras at our edges count; Settings > System
# (section "camera_wall": margin_m, include, exclude) adjusts it.
AREA_MARGIN_M = 1000
AREA_MARGIN_MAX_M = 5000
AREA_BBOX = (-73.7067, 40.9866, -73.6552, 41.0739)     # Port Chester and Rye Brook (the county's municipal boundaries)
AREA_PIN = {"at": 0, "before": 1, "after": 2}          # an active highway event's cameras, pinned first in this order
JSON = "application/json"

# Field names, any case, in the order tried: the developer API's (ID, RoadwayName, ...), the camera
# inventory's, and the plainer ones other 511 / CARS feeds use.
FIELDS = {
    "id": ("ID", "Id", "cameraid", "eventid", "device-id", "deviceid", "SourceId"),
    "lat": ("Latitude", "lat"),
    "lon": ("Longitude", "lon", "lng", "long"),
    "name": ("Name", "Location", "cameradesc", "device-name", "Description", "tooltip", "title"),
    "road": ("RoadwayName", "Roadway", "route", "roadName", "route-designator"),
    "dir": ("DirectionOfTravel", "Direction", "directiondesc"),
    "image": ("Url", "ImageUrl", "imageurl", "SnapshotUrl", "image"),
    "video": ("VideoUrl", "videourl", "StreamUrl"),
    "views": ("Views", "views"),
    "disabled": ("Disabled", "Blocked"),
    "status": ("Status", "active"),
    "type": ("EventType", "eventClassification", "category", "eventtype", "type"),
    "subtype": ("EventSubType", "eventSubType", "eventtype"),
    "description": ("Description", "eventdesc", "tooltip", "Comment"),
    "lanes": ("LanesAffected", "lanesaffected"),
    "lanes_status": ("LanesStatus", "lanesstatus"),
    "full_closure": ("IsFullClosure", "isFullClosure"),
    "severity": ("Severity", "severity", "priority"),
    "start": ("StartDate", "Reported", "startTime", "start"),
    "end": ("PlannedEndDate", "EndDate", "expirationdatetime", "endTime"),
    "updated": ("LastUpdated", "updatetime", "lastUpdated", "LastUpdate"),
    "messages": ("Messages", "Message", "activemessage", "messages"),
}

# 511NY's event types (the developer API: "roadwork, closures or accidentsAndIncidents"; also
# specialEvents, ...): roadwork is its own layer; everything else is "Incidents & closures".
ROADWORK_TYPES = {"roadwork", "construction", "maintenance", "workzone"}
CLOSED = re.compile(r"\bclosed\b|\bclosure\b", re.I)


def _redact(text: str, key: str) -> str:
    """Any message that could carry the URL (urllib's errors): the key replaced."""
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(key=)[^&\s'\"]+", r"\1***", text)


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _s(v, n: int = 300) -> str:
    t = " ".join(str(v or "").split())
    return t[:n]


def _pick(r: dict, field: str, default=None):
    """The record's value for FIELDS[field], whatever the case of its name."""
    low = r.get("_lower")
    if low is None:
        low = {str(k).lower(): k for k in r}
        r["_lower"] = low
    for n in FIELDS[field]:
        k = low.get(n.lower())
        if k is not None and r[k] not in (None, "", []):
            return r[k]
    return default


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "y")


def _when(v) -> float | None:
    """The feeds' times -> unix seconds: "13/10/2026 14:03:21" (511NY, day first, Eastern), "10/13/2026 4:45 PM"
    (the Thruway, month first), ISO 8601, or epoch. Words ("Until further notice"): None."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e11 else 1.0)
    s = str(v).strip()
    if re.fullmatch(r"\d{9,13}", s):
        return _when(int(s))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz).timestamp()
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=tz)).timestamp()
    except ValueError:
        return None


def _direction(v) -> str:
    d = _s(v, 40)
    return "" if d.lower() in ("", "unknown", "none", "n/a", "na", "all directions") else d


def _words(text: str) -> list[str]:
    """'I-95 at Exit 21' -> ['i95', 'at', 'exit', '21']: an interstate is one word, however typed."""
    t = re.sub(r"\b(i|interstate)[\s-]*(\d+)", r"i\2", (text or "").lower())
    return re.findall(r"[a-z0-9]+", t)


def in_bbox(lat: float | None, lon: float | None, bbox) -> bool:
    w, s, e, n = bbox
    return lat is not None and lon is not None and s <= lat <= n and w <= lon <= e


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(min(1.0, math.sqrt(a)))


def polygon_m(g: dict, lat: float, lon: float) -> float:
    """Metres from the point to a Polygon / MultiPolygon: 0 inside it, else to its nearest edge (a local flat
    projection: our area is a few km across). inf for anything else."""
    from sizeup.gis import contains
    if contains(g, lat, lon):
        return 0.0
    polys = g.get("coordinates") or []
    if g.get("type") == "Polygon":
        polys = [polys]
    elif g.get("type") != "MultiPolygon":
        return math.inf
    kx, ky = 111320.0 * math.cos(math.radians(lat)), 110540.0
    best = math.inf
    for rings in polys:
        for ring in rings or []:
            for i in range(len(ring) - 1):
                ax, ay = (ring[i][0] - lon) * kx, (ring[i][1] - lat) * ky
                bx, by = (ring[i + 1][0] - lon) * kx, (ring[i + 1][1] - lat) * ky
                dx, dy = bx - ax, by - ay
                t = 0.0 if not (dx or dy) else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
                best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def bbox_polygon(bbox) -> dict:
    w, s, e, n = bbox
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _xml_record(e: ET.Element) -> dict:
    """An XML element as a record: its attributes, its leaf children's text, and each child that has
    children of its own as a list of records (<Views><View>...</View></Views>)."""
    r = {k.split("}")[-1]: v for k, v in e.attrib.items()}
    for c in e:
        tag = c.tag.split("}")[-1]
        if len(c):
            kids = list(c)
            if all(len(g) or g.attrib for g in kids):
                r[tag] = [_xml_record(g) for g in kids]
            elif len({g.tag for g in kids}) == 1:            # <Messages><Message>..</Message>...</Messages>
                r[tag] = [(g.text or "").strip() for g in kids]
            else:
                r[tag] = _xml_record(c)
        elif c.attrib and not (c.text or "").strip():
            r.setdefault(tag + "s", []).append(_xml_record(c))
        else:
            r[tag] = (c.text or "").strip()
    return r


def _has_point(r: dict) -> bool:
    return _pick(r, "lat") is not None or isinstance(r.get("geometry"), dict)


def _xml_rows(root: ET.Element) -> list[dict]:
    """The records in an XML feed: the elements that carry a position (attributes or children)."""
    out = []

    def walk(e: ET.Element) -> None:
        r = _xml_record(e)
        if _has_point(r):
            out.append(r)
            return
        for c in e:
            walk(c)
    walk(root)
    return out


def _flat(r: dict) -> dict:
    """A GeoJSON feature as one record: its properties, and a point from its geometry."""
    if r.get("type") == "Feature" and isinstance(r.get("properties"), dict):
        out = dict(r["properties"])
        g = r.get("geometry") or {}
        pts = g.get("coordinates")
        while isinstance(pts, list) and pts and isinstance(pts[0], list):
            pts = pts[len(pts) // 2] if g.get("type") != "Point" else pts[0]
        if isinstance(pts, list) and len(pts) >= 2 and _pick(out, "lat") is None:
            out["Longitude"], out["Latitude"] = pts[0], pts[1]
        return out
    return r


def _rows(doc) -> list[dict]:
    """The feed's records: a JSON array, {"..": [...]} (GeoJSON features too), or XML."""
    if isinstance(doc, ET.Element):
        return _xml_rows(doc)
    if isinstance(doc, dict):
        doc = next((v for v in doc.values() if isinstance(v, list)), [])
    return [_flat(r) for r in doc if isinstance(r, dict)] if isinstance(doc, list) else []


def decode(body: bytes):
    """A feed's body: JSON, else XML. ValueError for anything else (an HTML page, say)."""
    text = body.decode("utf-8-sig", errors="replace").lstrip()
    if text[:1] in ("[", "{"):
        return json.loads(text)
    if text[:1] == "<" and not re.match(r"<!doctype html|<html", text, re.I):
        try:
            return ET.fromstring(text)
        except ET.ParseError as e:
            raise ValueError(f"bad XML: {e}") from None
    raise ValueError("not JSON or XML (" + ("an HTML page" if text[:1] == "<" else "something else") + ")")


# ---- parsing: 511NY's records -> what we keep (only our area; never a URL with the key) --------------------


def _point(r: dict, bbox) -> tuple[float, float] | None:
    lat, lon = _f(_pick(r, "lat")), _f(_pick(r, "lon"))
    return (lat, lon) if in_bbox(lat, lon, bbox) else None


def _url(v) -> str:
    u = str(v or "").strip()
    return u if urlsplit(u).scheme in ("http", "https") and urlsplit(u).hostname else ""


def parse_cameras(doc, bbox=DEFAULT_BBOX) -> list[dict]:
    """The camera inventory: {ID, Name, RoadwayName, DirectionOfTravel, Latitude, Longitude, Url (the
    snapshot), VideoUrl, Disabled, Blocked}, or with Views [{Url, VideoUrl, Status, Description}]."""
    out, seen = [], set()
    for r in _rows(doc):
        cid, pt = _s(_pick(r, "id"), 64), _point(r, bbox)
        if not CAMERA_ID.fullmatch(cid) or cid in seen or pt is None:
            continue
        views = [v for v in (_pick(r, "views") or []) if isinstance(v, dict)]
        live = [v for v in views if str(_pick(v, "status", "")).lower() not in ("disabled", "blocked", "offline", "false")]
        url = _url(_pick(r, "image")) or next((_url(_pick(v, "image")) for v in live or views if _url(_pick(v, "image"))), "")
        video = _url(_pick(r, "video")) or next((_url(_pick(v, "video")) for v in views if _url(_pick(v, "video"))), "")
        off = (_truthy(_pick(r, "disabled", False)) or str(_pick(r, "status", "")).lower() in ("false", "disabled", "offline")
               or (bool(views) and not live))
        seen.add(cid)
        out.append({"id": cid, "name": _s(_pick(r, "name", cid), 120), "road": _s(_pick(r, "road", ""), 80),
                    "dir": _direction(_pick(r, "dir", "")), "lat": round(pt[0], 6), "lon": round(pt[1], 6),
                    "url": url, "video": video, "has_video": bool(video), "off": off or not url, "cadence": CADENCE_S[SOURCE]})
    out.sort(key=lambda c: (c["road"], c["name"]))
    return out


def parse_events(doc, bbox=DEFAULT_BBOX) -> list[dict]:
    """Traffic events: {ID, EventType (roadwork | closures | accidentsAndIncidents | ...), EventSubType,
    RoadwayName, DirectionOfTravel, Description, LanesAffected, LanesStatus, IsFullClosure, Severity,
    StartDate, PlannedEndDate, LastUpdated (unix seconds), Latitude, Longitude}."""
    out, seen = [], set()
    for r in _rows(doc):
        eid, pt = _s(_pick(r, "id"), 80), _point(r, bbox)
        if not eid or eid in seen or pt is None:
            continue
        seen.add(eid)
        etype, sub = _s(_pick(r, "type", ""), 60), _s(_pick(r, "subtype", ""), 80)
        lanes = " ".join(x for x in (_s(_pick(r, "lanes", ""), 120), _s(_pick(r, "lanes_status", ""), 120)) if x)
        t = etype.lower().replace(" ", "")
        roadwork = t in ROADWORK_TYPES or "roadwork" in t or "construction" in t
        closed = (_truthy(_pick(r, "full_closure", False)) or "closure" in t
                  or bool(CLOSED.search(" ".join((sub, lanes)))))
        out.append({"id": eid, "layer": "roadwork" if roadwork else "incidents", "type": etype, "subtype": sub,
                    "road": _s(_pick(r, "road", ""), 80), "dir": _direction(_pick(r, "dir", "")),
                    "description": _s(_pick(r, "description", ""), 600), "lanes": lanes, "closed": closed,
                    "severity": _s(_pick(r, "severity", ""), 30), "start": _when(_pick(r, "start")),
                    "end": _when(_pick(r, "end")), "updated": _when(_pick(r, "updated")),
                    "lat": round(pt[0], 6), "lon": round(pt[1], 6)} | _schedule(r))
    out.sort(key=lambda e: (e["layer"], -(e["updated"] or e["start"] or 0)))
    return out


def _sign_lines(r: dict) -> list[str]:
    v = _pick(r, "messages", [])
    raw = v if isinstance(v, list) else [v]
    raw += [r[k] for k in sorted(r) if re.fullmatch(r"sign-text-\d+", str(k))]     # <dms sign-text-1=...>
    lines = []
    for m in raw:
        for part in re.split(r"<br\s*/?>|\n|\||~|\[nl\]|\[np\]", str(m or ""), flags=re.I):
            t = _s(re.sub(r"<[^>]*>|\[[a-z]{1,3}[0-9,]*\]", " ", part), 80)
            if t and t.upper() not in ("NO_MESSAGE", "NO MESSAGE", "BLANK", "NONE"):
                lines.append(t)
    return lines[:12]


def parse_signs(doc, bbox=DEFAULT_BBOX) -> list[dict]:
    """Message sign messages: {ID, Name, RoadwayName, DirectionOfTravel, Messages [..], Latitude,
    Longitude, LastUpdated}: what each variable message sign says now (blank: no lines)."""
    out, seen = [], set()
    for r in _rows(doc):
        sid, pt = _s(_pick(r, "id"), 80), _point(r, bbox)
        if not sid or sid in seen or pt is None:
            continue
        seen.add(sid)
        out.append({"id": sid, "name": _s(_pick(r, "name", sid), 120), "road": _s(_pick(r, "road", ""), 80),
                    "dir": _direction(_pick(r, "dir", "")), "lines": _sign_lines(r), "updated": _when(_pick(r, "updated")),
                    "lat": round(pt[0], 6), "lon": round(pt[1], 6)})
    out.sort(key=lambda s: (s["road"], s["name"]))
    return out


def _schedule(r: dict) -> dict:
    """What a 511 event says of when and where, beyond start and end (the road projects layer, sizeup/roadprojects.py,
    #185), only what is there: full_closure (IsFullClosure), hours from RecurrenceSchedules ([{..., DaysOfWeek, Times:
    [{StartTime, EndTime}]}]: any field naming days and start / end times of day), hours_text (Recurrence, as given),
    detour (DetourInstructions), line (EncodedPolyline, decoded), end_text (a planned end that isn't a time)."""
    from sizeup.roadprojects import day_numbers, decode_polyline, hhmm
    out: dict = {}
    if _truthy(_pick(r, "full_closure", False)):
        out["full_closure"] = True
    low = {str(k).lower(): v for k, v in r.items() if k != "_lower"}
    rec = low.get("recurrence")
    if rec not in (None, "", [], {}):
        out["hours_text"] = _s(rec if isinstance(rec, str) else json.dumps(rec, separators=(",", ":")), 200)
    hours = []
    for sch in low.get("recurrenceschedules") or []:
        if not isinstance(sch, dict):
            continue
        sl = {str(k).lower(): v for k, v in sch.items()}
        days = day_numbers(next((v for k, v in sl.items() if "day" in k), []))
        times = next((v for k, v in sl.items() if "time" in k and isinstance(v, list)), None) or [sch]
        for t in times:
            if not isinstance(t, dict):
                continue
            tl = {str(k).lower(): v for k, v in t.items()}
            a = hhmm(next((v for k, v in tl.items() if "start" in k and hhmm(v)), ""))
            b = hhmm(next((v for k, v in tl.items() if "end" in k and hhmm(v)), ""))
            if a and b:
                hours.append({"days": days, "from": a, "to": b})
    if hours:
        out["hours"] = hours[:14]
    detour = _s(low.get("detourinstructions"), 400)
    if detour:
        out["detour"] = detour
    poly = low.get("encodedpolyline")
    if isinstance(poly, str) and poly:
        line = decode_polyline(poly)
        if len(line) >= 2:
            out["line"] = line[:2000]
    end = _pick(r, "end")
    if end not in (None, "") and _when(end) is None:
        out["end_text"] = _s(end, 80)
    return out


# ---- 511NY on CARS (2026-09-30): the API's events, the public signs feed, the CARS-Hub's CCTV ----------------------
#   signs.json  [{id: "nySigns:Region 3:*Region 3-201080", idForDisplay, name: "HRP NB South of Exit 16 [PVMS-170]",
#                 agencyId, agencyName, status: DISPLAYING_MESSAGE | BLANK | ERROR_OR_FAILURE, lastUpdated (ms, about
#                 5 h ahead: _cars_shift), location {routeId: "907W" (NYSDOT's reference route: the Hutch), linearReference,
#                 fips, latitude, longitude, signFacingDirection: N | S | E | W | BOTH_DIRECTIONS, locationDescription,
#                 cityReference}, display {pages: [{lines: [...], justification, hasImage}]} (only while lit),
#                 properties {signType, maxLinesPerPage, ...}}, ...]  (recorded 2026-11-04: tests/fixtures/511ny/
#                 cars_signs_recorded.json)
#   /api/events [CARSEvent: {event-id, headline {category, code}, priority, status, category PLANNED | CURRENT |
#                 FORECAST, update-time, estimated-re-opening-time, details [{element-id, access, start-time, end-time,
#                 duration-was-specified, update-time, lanes [{lanes-type, total-affected, total-original,
#                 link-direction}], descriptions [{description-type: PhraseDescription {kind {category, code}} |
#                 AdditionalTextDescription {description} | DetourDescription {detour-type, locations-on-detour
#                 [LinkLocation]} | ...}], locations [{location-type: LinkLocation {route-designator, link-direction,
#                 primary-location {geo-location {latitude, longitude}, cross-street-name [..], point-name,
#                 linear-reference}, secondary-location} | GeoLocation | Landmark}], scheduleOccurrences [{startTime,
#                 endTime}], recurrent-times, effective-periods}], full-report-texts [{description}], geometry (GeoJSON),
#                 communication-plans {web}, counties}]; every time a ZonedDateTime {time (ms), utcOffset, timeZoneId}.
#                 From the API's Swagger definition (carsapi-definition.json, 1.2.0); the fixtures are constructed
#                 from it (tests/fixtures/511ny/cars_events_constructed.json) until the credentials let us record one.

CARS_ROUTES = {"907W": "Hutch"}              # NYSDOT reference routes that carry a parkway's name in the sign's name
CARS_NAME_ROUTES = (("HRP", "Hutch"), ("BRP", "Bronx River Pkwy"), ("SBP", "Sprain Brook Pkwy"), ("TSP", "Taconic"),
                    ("SMRP", "Saw Mill River Pkwy"))
CARS_DIRS = {"N": "Northbound", "S": "Southbound", "E": "Eastbound", "W": "Westbound", "NORTHBOUND": "Northbound",
             "SOUTHBOUND": "Southbound", "EASTBOUND": "Eastbound", "WESTBOUND": "Westbound"}
CARS_SIGN_STATUS = {"DISPLAYING_MESSAGE": "on", "BLANK": "blank", "ERROR_OR_FAILURE": "fault"}
CARS_ENDED = {"ended", "canceled", "cancelled", "deleted", "delete", "inactive", "archived"}
CARS_ROADWORK = re.compile(r"road\s*work|roadwork|construction|maintenance|work\s*zone|paving|bridge work|utility work", re.I)
CARS_FULL = re.compile(r"\b(road|roadway|highway|bridge|ramp|exit)s?\s+(is\s+|are\s+)?closed\b|\bclosed to (all )?traffic\b"
                       r"|\ball lanes (closed|blocked)\b|\bfull(y)? clos", re.I)
CARS_CLOSED = re.compile(r"\bclosed\b|\bclosure\b|\bblocked\b", re.I)
JSON_GAP = re.compile(r"[\s,]*")


def iter_json_array(text: str):
    """The records of a JSON array one at a time (raw_decode): a big statewide feed is never all dicts at once.
    ValueError for anything that isn't an array of values."""
    i = JSON_GAP.match(text, 0).end()
    if text[i:i + 1] != "[":
        raise ValueError("not a JSON array")
    dec, i, n = json.JSONDecoder(), i + 1, len(text)
    while True:
        i = JSON_GAP.match(text, i).end()
        if i >= n:
            raise ValueError("a JSON array cut short")
        if text[i] == "]":
            return
        obj, i = dec.raw_decode(text, i)
        yield obj


def _body_text(body: bytes) -> str:
    return body.decode("utf-8-sig", errors="replace")


def _words_of(v) -> str:
    """"ROAD_CONSTRUCTION" / "roadConstruction" -> "road construction"."""
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(v or ""))
    return _s(t.replace("_", " ").replace("-", " ").lower(), 120)


def _cars_point(loc: dict) -> tuple[float, float] | None:
    """A CARS location's point: a GeoLocation's latitude / longitude, a LinkLocation's primary-location, a Landmark's
    geo-location (degrees; microdegrees too, as TMDD gives them)."""
    if not isinstance(loc, dict):
        return None
    for g in (loc, loc.get("geo-location"), (loc.get("primary-location") or {}).get("geo-location")
              if isinstance(loc.get("primary-location"), dict) else None):
        if isinstance(g, dict):
            lat, lon = _f(g.get("latitude")), _f(g.get("longitude"))
            if lat is not None and lon is not None:
                if abs(lat) > 90 or abs(lon) > 180:
                    lat, lon = lat / 1e6, lon / 1e6
                return lat, lon
    return None


def _geometry_line(g) -> list[list[float]]:
    """A GeoJSON geometry's points as [[lon, lat], ...]: a LineString's, a MultiLineString's longest, a Point's one."""
    if not isinstance(g, dict):
        return []
    c, t = g.get("coordinates"), g.get("type")
    try:
        if t == "Point":
            return [[float(c[0]), float(c[1])]]
        if t in ("LineString", "MultiPoint"):
            return [[float(p[0]), float(p[1])] for p in c]
        if t == "MultiLineString":
            return [[float(p[0]), float(p[1])] for p in max(c, key=len)]
    except (TypeError, ValueError, IndexError):
        return []
    return []


def _zoned(v) -> float | None:
    """A CARS ZonedDateTime {time (ms), utcOffset, timeZoneId} (or a plain time) -> unix seconds."""
    if isinstance(v, dict):
        v = v.get("time")
    return _when(v)


def _cars_shift(times: list[float], published: float | None, now: float | None) -> float:
    """What to add to every lastUpdated of one agency's signs in a signs.json. Observed 2026-11-04: "NY Region 11 Signs"
    (New York City) ran up to 5 h after the file's own Last-Modified, to the minute (Eastern wall time written as if it
    were UTC); every other region's newest was 2 min before it. When an agency's newest is later than the file (or now,
    with no Last-Modified) by more than 5 min and our UTC offset puts it back before then: that offset (-5 h / -4 h);
    else 0."""
    ref = published if published is not None else now
    if not times or ref is None or max(times) <= ref + 300:
        return 0.0
    from datetime import datetime
    from zoneinfo import ZoneInfo
    off = datetime.fromtimestamp(max(times), ZoneInfo("America/New_York")).utcoffset()
    shift = off.total_seconds() if off else 0.0
    return shift if max(times) + shift <= ref + 300 else 0.0


def _cars_road(route: str, name: str = "") -> str:
    """The road as we name it: "907W" (NYSDOT's reference route for the Hutch) / a sign named "HRP NB ..." -> "Hutch";
    "I-287", "NY 120" as given."""
    r = _s(route, 60)
    if r in CARS_ROUTES:
        return CARS_ROUTES[r]
    if re.fullmatch(r"\d{3}[A-Z]", r):
        head = (name or "").split(" ", 1)[0].upper()
        return next((road for ab, road in CARS_NAME_ROUTES if head == ab), r)
    return r


def _cars_dir(v) -> str:
    return CARS_DIRS.get(str(v or "").strip().upper(), "")


def sign_text(s: dict) -> str:
    """What a sign says, for "the same sign" (_merged): its lines, upper case, spaces folded."""
    return " ".join(" ".join(s.get("lines") or []).upper().split())


def parse_cars_sign(r: dict, bbox) -> dict | None:
    """One signs.json record -> our sign, or None outside our area."""
    loc = r.get("location") if isinstance(r.get("location"), dict) else {}
    lat, lon = _f(loc.get("latitude")), _f(loc.get("longitude"))
    sid = _s(r.get("id") or r.get("idForDisplay"), 120)
    if not sid or not in_bbox(lat, lon, bbox):
        return None
    status = CARS_SIGN_STATUS.get(str(r.get("status") or "").upper(), _s(r.get("status"), 30).lower())
    pages = []
    if status == "on":
        for pg in ((r.get("display") or {}).get("pages") or []):
            lines = [t for t in (_s(x, 60) for x in (pg.get("lines") or [] if isinstance(pg, dict) else [])) if t]
            if lines:
                pages.append(lines)
    mile = _f(loc.get("linearReference"))
    name = _s(r.get("name") or sid, 120)
    return {"id": sid, "name": name, "road": _cars_road(loc.get("routeId", ""), name),
            "dir": _cars_dir(loc.get("signFacingDirection")), "mile": f"{mile:.1f}" if mile is not None else "",
            "where": _s(loc.get("locationDescription"), 120), "agency": _s(r.get("agencyName"), 60), "status": status,
            "pages": pages[:6], "lines": [t for pg in pages for t in pg][:12],
            "updated": _when(r.get("lastUpdated")), "lat": round(lat, 6), "lon": round(lon, 6)}


def _is_cars_sign(r) -> bool:
    return isinstance(r, dict) and isinstance(r.get("location"), dict) and "status" in r and "routeId" in r["location"]


def parse_cars_signs(body: bytes, bbox=DEFAULT_BBOX, meta: dict | None = None) -> list[dict]:
    """signs.json (raw): one record at a time, our area kept. Any other shape: parse_signs (the older 511 names)."""
    meta = meta if meta is not None else {}
    text = _body_text(body)
    del body
    if JSON_GAP.match(text, 0).end() >= len(text) or text[JSON_GAP.match(text, 0).end()] != "[":
        return parse_signs(decode(text.encode()), bbox)
    out, other, seen, newest = [], [], set(), {}
    for r in iter_json_array(text):
        if not _is_cars_sign(r):
            if isinstance(r, dict):
                other.append(r)
            continue
        t, ag = _when(r.get("lastUpdated")), str(r.get("agencyId") or r.get("agencyName") or "")
        if t is not None:
            newest[ag] = max(newest.get(ag, t), t)
        s = parse_cars_sign(r, bbox)
        if s is not None and s["id"] not in seen:
            seen.add(s["id"])
            s["_agency"] = ag
            out.append(s)
    # each agency's times put right as one (_cars_shift: judged by that agency's newest anywhere in the file, not just
    # in our area); a time still later than the file can't be right: none
    shift = {ag: _cars_shift([t], meta.get("published"), meta.get("now")) for ag, t in newest.items()}
    ref = meta.get("published") if meta.get("published") is not None else meta.get("now")
    for s in out:
        ag = s.pop("_agency", "")
        if s["updated"] is not None:
            s["updated"] += shift.get(ag, 0.0)
            if ref is not None and s["updated"] > ref + 300:
                s["updated"] = None
    out.extend(parse_signs(other, bbox) if other else [])
    out.sort(key=lambda s: (s["road"], _f(s.get("mile")) or 0, s["name"]))
    return out


parse_cars_signs.raw = True


def _phrase(v) -> str:
    """A CARS Phrase {category, code} as words: its code ("road construction")."""
    if isinstance(v, dict):
        return _words_of(v.get("code") or v.get("category"))
    return _words_of(v)


def _detail(r: dict) -> dict:
    """The event's headline detail (headline-element), else its first."""
    ds = [d for d in r.get("details") or [] if isinstance(d, dict)]
    want = r.get("headline-element")
    return next((d for d in ds if want is not None and d.get("element-id") == want), ds[0] if ds else {})


def _lane_text(lanes: list) -> tuple[str, bool, str]:
    """(the lanes in words, all through lanes affected, "lanes" | "ramp" | "")."""
    parts, full, kind = [], False, ""
    for ln in lanes:
        if not isinstance(ln, dict):
            continue
        a, o = ln.get("total-affected"), ln.get("total-original")
        lt = str(ln.get("lanes-type") or "").upper()
        what = _words_of(lt) or "lanes"
        if not isinstance(a, int) or a <= 0:
            continue
        d = _cars_dir(ln.get("link-direction"))
        what = "" if lt in ("THROUGH_LANES", "") else what
        notes = ", ".join(x for x in (what, d.lower()) if x)
        parts.append((f"{a} of {o} lanes" if isinstance(o, int) and o > 0 else f"{a} lane" + ("s" if a > 1 else ""))
                     + " affected" + (f" ({notes})" if notes else ""))
        if "RAMP" in lt:
            kind = kind or "ramp"
        else:
            kind = "lanes"
            if lt == "THROUGH_LANES" and isinstance(o, int) and 0 < o <= a:
                full = True
    return "; ".join(parts)[:200], full, kind


def _period_text(det: dict) -> str:
    """Effective periods in words ("Mon-Fri, night"): only what is given; times of day come as scheduleOccurrences."""
    names = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")       # CARS: Sunday is bit 0
    out = []
    for p in det.get("effective-periods") or []:
        if not isinstance(p, dict):
            continue
        bits = (p.get("days-of-week") or {}).get("bits") if isinstance(p.get("days-of-week"), dict) else None
        days = [names[i] for i in range(7) if isinstance(bits, int) and bits >> i & 1]
        q = _words_of(p.get("effective-period-qualifier"))
        bits_txt = "" if len(days) in (0, 7) else ", ".join(days)
        out.append(", ".join(x for x in (bits_txt, "" if q == "not specified" else q) if x))
    return _s("; ".join(x for x in out if x), 200)


def parse_cars_event(r: dict, bbox=DEFAULT_BBOX) -> dict | None:
    """One CARS event -> our event (the shape of parse_events plus the road projects' fields), None outside our area,
    ended, or not for us (its access level: CARS_ACCESS)."""
    eid = _s(r.get("event-id"), 80)
    if not eid or str(r.get("status") or "").strip().lower() in CARS_ENDED:
        return None
    det = _detail(r)
    if str(det.get("access") or "PUBLIC_DOMAIN").upper() not in CARS_ACCESS:
        return None
    locs = [x for x in det.get("locations") or [] if isinstance(x, dict)]
    link = next((x for x in locs if x.get("location-type") == "LinkLocation" or "route-designator" in x), {})
    line = _geometry_line(r.get("geometry"))
    pt = next((p for p in (_cars_point(x) for x in locs) if p), None)
    if pt is None and line:
        mid = line[len(line) // 2]
        pt = (mid[1], mid[0])
    if pt is None or not (in_bbox(pt[0], pt[1], bbox) or any(in_bbox(p[1], p[0], bbox) for p in line)):
        return None
    descs = [x for x in det.get("descriptions") or [] if isinstance(x, dict)]
    phrases = [_phrase(x.get("kind")) for x in descs if x.get("description-type") == "PhraseDescription"]
    texts = [_s(x.get("description"), 300) for x in descs if x.get("description-type") == "AdditionalTextDescription"]
    detours = []
    for x in descs:
        if x.get("description-type") != "DetourDescription":
            continue
        via = [_s(y.get("route-designator"), 40) for y in x.get("locations-on-detour") or [] if isinstance(y, dict)]
        detours.append(_words_of(x.get("detour-type")).capitalize() + (" via " + ", ".join(v for v in via if v) if any(via) else ""))
    detours += [t for t in texts if re.search(r"\bdetour", t, re.I)]
    report = next((_s(x.get("description"), 600) for x in r.get("full-report-texts") or []
                   if isinstance(x, dict) and x.get("description")), "")
    head = _phrase(r.get("headline"))
    cat = _words_of((r.get("headline") or {}).get("category") if isinstance(r.get("headline"), dict) else "")
    lanes, all_through, lane_kind = _lane_text(det.get("lanes") or [])
    words = " ".join([head, cat, *phrases, *texts, report])
    planned = str(r.get("category") or "").upper() == "PLANNED"
    roadwork = bool(CARS_ROADWORK.search(words)) or planned
    full = all_through or bool(CARS_FULL.search(words))
    closed = full or bool(CARS_CLOSED.search(" ".join([head, *phrases])))
    prim = link.get("primary-location") if isinstance(link.get("primary-location"), dict) else {}
    sec = link.get("secondary-location") if isinstance(link.get("secondary-location"), dict) else {}
    cross = lambda p: _s(((p.get("cross-street-name") or [None])[0] if isinstance(p.get("cross-street-name"), list) else "")
                         or p.get("point-name"), 80)
    mile = _f(prim.get("linear-reference"))
    start, end = _zoned(det.get("start-time")), _zoned(det.get("end-time"))
    if det.get("duration-was-specified") is False:
        end = None                                            # an end CARS filled in, not one anyone gave
    occ = []
    for o in det.get("scheduleOccurrences") or []:
        if isinstance(o, dict):
            a, b = _zoned(o.get("startTime")), _zoned(o.get("endTime"))
            if a is not None and b is not None and b > a:
                occ.append([a, b])
    occ.sort()
    reopen = _zoned(r.get("estimated-re-opening-time"))
    web = _url((r.get("communication-plans") or {}).get("web") if isinstance(r.get("communication-plans"), dict) else "")
    pr = r.get("priority")
    desc = report or ". ".join(x for x in [head.capitalize(), *[p.capitalize() for p in phrases if p != head], *texts] if x)
    out = {"id": eid, "layer": "roadwork" if roadwork else "incidents", "type": cat or ("planned" if planned else "event"),
           "subtype": head, "road": _s(link.get("route-designator"), 80),
           "dir": _cars_dir(link.get("link-direction")) or _cars_dir(link.get("link-alignment")),
           "description": _s(desc, 600), "lanes": lanes, "closed": closed,
           "severity": f"priority {pr}" if isinstance(pr, int) and not isinstance(pr, bool) else "",
           "start": start if start is not None else (occ[0][0] if occ else None), "end": end,
           "updated": _zoned(r.get("update-time")) or _zoned(det.get("update-time")),
           "lat": round(pt[0], 6), "lon": round(pt[1], 6), "mile": f"{mile:.1f}" if mile is not None else "",
           "cross_from": cross(prim), "cross_to": cross(sec), "category": _words_of(r.get("category")),
           "closure": "full" if full else lane_kind or ("" if not closed else "lanes")}
    if full:
        out["full_closure"] = True
    if occ:
        out["occurrences"] = occ[:40]
    if (ht := _period_text(det)):
        out["hours_text"] = ht
    if detours:
        out["detour"] = _s("; ".join(detours), 400)
    if len(line) >= 2:
        out["line"] = line[:2000]
    if reopen is not None:
        out["reopen"] = reopen
    if web:
        out["link"] = web
    return out


def _is_cars_event(r) -> bool:
    return isinstance(r, dict) and "event-id" in r


def parse_cars_events(body: bytes, bbox=DEFAULT_BBOX, meta: dict | None = None) -> list[dict]:
    """GET /api/events (raw): one record at a time, our area kept. Any other shape: parse_events (the older 511
    names, the CARS-Hub's)."""
    text = _body_text(body)
    del body
    i = JSON_GAP.match(text, 0).end()
    if text[i:i + 1] != "[":
        return parse_events(decode(text.encode()), bbox)
    out, other, seen = [], [], set()
    for r in iter_json_array(text):
        if not _is_cars_event(r):
            if isinstance(r, dict):
                other.append(r)
            continue
        e = parse_cars_event(r, bbox)
        if e is not None and e["id"] not in seen:
            seen.add(e["id"])
            out.append(e)
    out.extend(parse_events(other, bbox) if other else [])
    out.sort(key=lambda e: (e["layer"], -(e["updated"] or e["start"] or 0)))
    return out


parse_cars_events.raw = True


# The CARS-Hub (https://nysdot.carsprogram.org/hub/index.jsf, 2026-11-04): a public page listing the XML feeds and
# their schemas; every feed behind HTTP Basic (realm "Hub": the same username and password as the API). Its feeds:
# FEU-t / -g / -m / -w (the CARS events as TMDD Full Event Updates: the same events as the API's JSON, which we read
# instead; feu-t.xml was 6 MB), their indexed variants, CCTV (data/cctv.xml, 3 MB, no ETag or Last-Modified) and Waze
# CIFS. We read the CCTV inventory, to schemas/CCTV.xsd (TMDD Volume 2, Castle Rock 2010):
#   <cctvInventories><cctvInventory><owner><organization-id/></owner><inventory>
#     <inventory-item><device-id/> (32 chars) <device-updated><date>YYYYMMDD</date><time>HHMMSS</time>
#       <utc-offset>-0500</utc-offset></device-updated> <device-name/> <cctv-image>jpeg</cctv-image> <video-url/>
#       <still-images><still-image><name/><url/></still-image>...</still-images>
#       <location><geo-location><latitude>microdegrees</latitude><longitude/></geo-location><link-ownership/>
#         <route-designator/><linear-reference/><link-direction>0..12 (TMDD: 1 n, 3 e, 5 s, 7 w, ...)</link-direction>
#       </location> <views/> ...</inventory-item>
# Recorded 2026-11-04 (tests/fixtures/511ny/cars_cctv_recorded.xml, trimmed): 2,901 cameras statewide (NYSDOT 1,825, NYC
# DOT 1,076), 53 in our area, 40 with video; no link-direction given (the name's "NB" / "WB" is used); stills on
# public.carsprogram.org (public JPEG with Last-Modified and ETag), video on *.nysdot.skyvdn.com (the HLS the Thruway's
# cameras use: a master playlist naming a chunklist in its folder). The Thruway's cameras are in it too (TA_058 = the
# Thruway's 1ne01370s, 7 m apart): same_camera, ours kept unless the CARS one has live video and ours hasn't.

TMDD_DIRS = {"1": "Northbound", "3": "Eastbound", "5": "Southbound", "7": "Westbound", "n": "Northbound", "e": "Eastbound",
             "s": "Southbound", "w": "Westbound"}


def _tag(e: ET.Element) -> str:
    return e.tag.split("}")[-1]


def _kid(e: ET.Element | None, *path: str) -> ET.Element | None:
    """The first descendant along path by local name (namespaces ignored)."""
    for name in path:
        if e is None:
            return None
        e = next((c for c in e if _tag(c) == name), None)
    return e


def _text(e: ET.Element | None, *path: str) -> str:
    k = _kid(e, *path) if path else e
    return (k.text or "").strip() if k is not None else ""


def tmdd_time(e: ET.Element | None) -> float | None:
    """A TMDD DateTimeZone {date YYYYMMDD, time HHMMSS, utc-offset [+-]HHMM} -> unix seconds (no offset: ours)."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    d, t, off = _text(e, "date"), _text(e, "time"), _text(e, "utc-offset")
    if not re.fullmatch(r"\d{8}", d) or not re.fullmatch(r"\d{4}(\d{2})?", t):
        return None
    try:
        when = datetime.strptime(d + t.ljust(6, "0"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", off)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        tz = timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    else:
        tz = ZoneInfo("America/New_York")
    return when.replace(tzinfo=tz).timestamp()


def _microdeg(v: str) -> float | None:
    x = _f(v)
    if x is None:
        return None
    return x / 1e6 if abs(x) > 180 else x


def parse_carshub_cctv(body: bytes, bbox=DEFAULT_BBOX, meta: dict | None = None) -> list[dict]:
    """The CARS-Hub's CCTV inventory (raw): one <inventory-item> at a time (iterparse, each cleared once read), our
    area kept. Each camera: its first still image (the hub's snapshot proxy) and its video address, when given."""
    import io
    out, seen, owner = [], set(), ""
    try:
        for ev, e in ET.iterparse(io.BytesIO(body), events=("start", "end")):
            name = _tag(e)
            if ev == "start":
                continue
            if name == "organization-name" and not owner:
                owner = (e.text or "").strip()
            if name == "cctvInventory":
                owner = ""
                e.clear()
                continue
            if name != "inventory-item":
                continue
            geo = _kid(e, "location", "geo-location")
            lat, lon = _microdeg(_text(geo, "latitude")), _microdeg(_text(geo, "longitude"))
            cid = re.sub(r"[^A-Za-z0-9_.:-]", "-", _text(e, "device-id"))[:64]
            if cid and cid not in seen and in_bbox(lat, lon, bbox):
                seen.add(cid)
                box = _kid(e, "still-images")
                stills = [_url(_text(x, "url")) for x in (list(box) if box is not None else []) if _tag(x) == "still-image"]
                url = next((u for u in stills if u), "")
                video = _url(_text(e, "video-url"))
                d = _text(e, "location", "link-direction").lower()
                if d not in TMDD_DIRS:                       # (none given, 2026-11-04): the name's "NB", "Westbound"
                    d = cam_dir(_text(e, "device-name"))[:1].lower()
                mile = _f(_text(e, "location", "linear-reference"))
                name_ = _s(_text(e, "device-name") or cid, 120)
                out.append({"id": cid, "name": name_, "road": _cars_road(_text(e, "location", "route-designator"), name_),
                            "dir": TMDD_DIRS.get(d, ""), "mile": f"{mile:.1f}" if mile is not None else "",
                            "lat": round(lat, 6), "lon": round(lon, 6), "url": url, "video": video, "has_video": bool(video),
                            "off": not url, "cadence": CADENCE_S[SOURCE], "owner": _s(owner, 60),
                            "image_type": _s(_text(e, "cctv-image"), 10), "updated": tmdd_time(_kid(e, "device-updated"))})
            e.clear()
    except ET.ParseError as x:
        raise ValueError(f"bad XML: {x}") from None
    out.sort(key=lambda c: (c["road"], _f(c["mile"]) or 0, c["name"]))
    return out


parse_carshub_cctv.raw = True
CARSHUB_PARSE = {"cameras": parse_carshub_cctv, "events": lambda doc, bbox=DEFAULT_BBOX: [],
                 "signs": lambda doc, bbox=DEFAULT_BBOX: []}


def _cam_key(c: dict) -> str:
    """A camera's own name before its location ("SB King Approach - King St/Willett Ave/..." -> "sb king approach")."""
    return " ".join(_words(re.split(r"\s+-\s+", c.get("name") or "")[0]))


def same_camera(a: dict, b: dict, within_m: float = CAMERA_MATCH_M) -> bool:
    """Two sources' records of one camera: within within_m, not facing different ways (when both say), and on a
    highway (by its road or name) or with the same name: an intersection's several cameras (the county's King St /
    Willett Ave approaches) are each their own."""
    if km(a["lat"], a["lon"], b["lat"], b["lon"]) * 1000 > within_m:
        return False
    da, db = cam_dir(a.get("dir") or a.get("name", "")), cam_dir(b.get("dir") or b.get("name", ""))
    if da and db and da != db:
        return False
    hw = lambda c: highway_from_text(f"{c.get('road', '')} {c.get('name', '')}") is not None
    return (hw(a) and hw(b)) or (bool(_cam_key(a)) and _cam_key(a) == _cam_key(b))


def compare_cameras(new: list[dict], ours: list[dict], within_m: float = CAMERA_MATCH_M) -> dict:
    """A feed's cameras against the ones we have (the Thruway's, the county's): {n, known, known_by: {source: n},
    new, new_by_road: {road: n}, new_ids}. A camera that is the same as one of ours (same_camera) is one we have."""
    known_by: dict[str, int] = {}
    fresh = []
    for c in new:
        twin = next((o for o in ours if same_camera(c, o, within_m)), None)
        if twin is None:
            fresh.append(c)
        else:
            src = twin.get("source") or "?"
            known_by[src] = known_by.get(src, 0) + 1
    by_road: dict[str, int] = {}
    for c in fresh:
        road = (highway_from_text(c.get("road", "") or c.get("name", "")) or (c.get("road") or "(no road)",))[0]
        by_road[road] = by_road.get(road, 0) + 1
    return {"n": len(new), "known": len(new) - len(fresh), "known_by": known_by, "new": len(fresh),
            "new_by_road": dict(sorted(by_road.items(), key=lambda x: (-x[1], x[0]))), "new_ids": [c["id"] for c in fresh][:200]}


def compare_text(cmp: dict) -> str:
    """"12 cameras in our area: 9 we have (Thruway 6, County 3), 3 new (Hutch 2, I-287 1)"."""
    known = ", ".join(f"{k} {v}" for k, v in sorted(cmp["known_by"].items()))
    new = ", ".join(f"{k} {v}" for k, v in cmp["new_by_road"].items())
    return (f"{cmp['n']} cameras in our area: {cmp['known']} we have" + (f" ({known})" if known else "")
            + f", {cmp['new']} new" + (f" ({new})" if new else ""))


PARSE = {"cameras": parse_cameras, "events": parse_cars_events, "signs": parse_cars_signs}


# ---- the Thruway's feeds (XML, one element per record, everything in attributes) ------------------------
#   <cameras><lastupdatetime/>
#     <camera cameraid latitude longitude milepost route="I-95 - New England Thruway" cameradesc
#             directiondesc="The traffic closest to the camera is traveling north on I-95" imageurl videourl
#             active="1" cutoff="0" problemstream="0" display groupid cutoffdesc/>
#   <events><event eventid category="incident|roadwork" eventtype="crash|long term road construction|..."
#             eventdesc route direction="northbound|in both directions" milepost latitude longitude
#             updatetime="10/13/2026 3:05 PM" expirationdatetime="Until further notice" orgid region/>
#   <dmsStatus><dms device-id device-name="I-95 11.5 NB" route-designator direction milepost latitude longitude
#             sign-text-1="FROM I-287~TO GMC BRIDGE~41 MIN" sign-text-2 activemessage="1|0" updatetime/>

THRUWAY_CLOSED = re.compile(r"\b(road|roadway|ramp|bridge|exit|all lanes|both directions|highway)\s+(is\s+)?closed\b"
                            r"|\bclosure\b|\bclosed to (all )?traffic\b", re.I)
THRUWAY_LANES = re.compile(r"((?:\w+[ -]){0,3}(?:lanes?|shoulder|ramp)s?\s+(?:closed|blocked))", re.I)


def _thruway_rows(doc, tag: str) -> list[dict]:
    """The <tag .../> records of a Thruway XML feed (attributes; a child text now and then)."""
    if not isinstance(doc, ET.Element):
        return []
    out = []
    for e in doc.iter():
        if e.tag.split("}")[-1] == tag and e.attrib:
            r = {k.split("}")[-1].lower(): (v or "").strip() for k, v in e.attrib.items()}
            for c in e:
                if not len(c):
                    r.setdefault(c.tag.split("}")[-1].lower(), (c.text or "").strip())
            out.append(r)
    return out


def _thruway_point(r: dict, bbox) -> tuple[float, float] | None:
    lat, lon = _f(r.get("latitude")), _f(r.get("longitude"))
    return (lat, lon) if in_bbox(lat, lon, bbox) else None


def _thruway_road(route: str) -> str:
    """"I-95 - New England Thruway" -> "I-95"; "Berkshire Connector" stays."""
    m = re.match(r"\s*((?:I|US|NY|CT|RT|SR)-?\s?\d+[A-Z]?)\s*-\s*\S", route or "")
    return m.group(1).replace(" ", "") if m else _s(route, 80)


def _thruway_dir(text: str) -> str:
    """"...traveling north on I-95" / "northbound" -> "Northbound"; a median camera, "in both directions": ""."""
    t = (text or "").lower()
    if "median" in t or "both direction" in t:
        return ""
    m = re.search(r"traveling\s+(north|south|east|west)\b", t) or re.fullmatch(r"\s*(north|south|east|west)(?:bound)?\s*", t)
    return f"{m.group(1).capitalize()}bound" if m else ""


def parse_thruway_cameras(doc, bbox=DEFAULT_BBOX) -> list[dict]:
    out, seen = [], set()
    for r in _thruway_rows(doc, "camera"):
        cid, pt = _s(r.get("cameraid"), 64), _thruway_point(r, bbox)
        if not CAMERA_ID.fullmatch(cid) or cid in seen or pt is None:
            continue
        seen.add(cid)
        url, video = _url(r.get("imageurl")), _url(r.get("videourl"))
        off = r.get("active", "1") != "1" or r.get("cutoff", "0") == "1" or r.get("problemstream", "0") == "1"
        out.append({"id": cid, "name": _s(r.get("cameradesc") or cid, 120), "road": _thruway_road(r.get("route", "")),
                    "dir": _thruway_dir(r.get("directiondesc", "")), "mile": _s(r.get("milepost"), 10),
                    "lat": round(pt[0], 6), "lon": round(pt[1], 6), "url": url, "video": video, "has_video": bool(video),
                    "off": off or not url, "cadence": CADENCE_S[THRUWAY]})
    out.sort(key=lambda c: (c["road"], _f(c["mile"]) or 0, c["name"]))
    return out


def parse_thruway_events(doc, bbox=DEFAULT_BBOX) -> list[dict]:
    out, seen = [], set()
    for r in _thruway_rows(doc, "event"):
        eid, pt = _s(r.get("eventid"), 80), _thruway_point(r, bbox)
        if not eid or eid in seen or pt is None:
            continue
        seen.add(eid)
        cat, etype, desc = r.get("category", "").lower(), _s(r.get("eventtype"), 80), _s(r.get("eventdesc"), 600)
        roadwork = cat == "roadwork" or any(w in etype.lower() for w in ("construction", "maintenance", "roadwork", "work zone"))
        lanes = THRUWAY_LANES.search(desc)
        out.append({"id": eid, "layer": "roadwork" if roadwork else "incidents", "type": _s(cat or "event", 60),
                    "subtype": etype, "road": _thruway_road(r.get("route", "")), "dir": _thruway_dir(r.get("direction", "")),
                    "description": desc, "lanes": lanes.group(1).strip() if lanes else "",
                    "closed": "clos" in cat or bool(THRUWAY_CLOSED.search(desc)), "severity": "",
                    "mile": _s(r.get("milepost"), 10), "start": None, "end": _when(r.get("expirationdatetime")),
                    "end_text": "" if _when(r.get("expirationdatetime")) else _s(r.get("expirationdatetime"), 80),
                    "updated": _when(r.get("updatetime")), "lat": round(pt[0], 6), "lon": round(pt[1], 6)})
    out.sort(key=lambda e: (e["layer"], -(e["updated"] or 0)))
    return out


def parse_thruway_signs(doc, bbox=DEFAULT_BBOX) -> list[dict]:
    out, seen = [], set()
    for r in _thruway_rows(doc, "dms"):
        sid, pt = _s(r.get("device-id"), 80), _thruway_point(r, bbox)
        if not sid or sid in seen or pt is None:
            continue
        seen.add(sid)
        lit = r.get("activemessage", "1") != "0"
        lines = _sign_lines({"Messages": [r.get("sign-text-1", ""), r.get("sign-text-2", "")]}) if lit else []
        out.append({"id": sid, "name": _s(r.get("device-name") or sid, 120), "road": _thruway_road(r.get("route-designator", "")),
                    "dir": _thruway_dir(r.get("direction", "")), "mile": _s(r.get("milepost"), 10), "lines": lines,
                    "updated": _when(r.get("updatetime")), "lat": round(pt[0], 6), "lon": round(pt[1], 6)})
    out.sort(key=lambda s: (s["road"], _f(s["mile"]) or 0, s["name"]))
    return out


THRUWAY_PARSE = {"cameras": parse_thruway_cameras, "events": parse_thruway_events, "signs": parse_thruway_signs}


# ---- Westchester County DPW's cameras (#99): an ArcGIS feature layer --------------------------------------------
#   {"features": [{"attributes": {NATIVE_CCTV_ID: 23.0, CCTV_NAME: "SB King Approach - King St/Willett Ave/North Pearl St",
#                  DESCRIPTION: "Traffic Approaching the Camera is going South on King St.", URL:
#                  "http://www.westchestergov.com/cctv/camera23.jpg", REFRESH_RATE: "8 seconds", MILE_POINT: 15.5 | null,
#                  OWNER_NAME, LOCATION, NY511_REGION, ...}, "geometry": {"x": -73.66358, "y": 41.00349}}, ...]}
# The images: https://www.westchestergov.com/cctv/camera<id>.jpg, 1280x960, a new one every 8 s with its
# Last-Modified; a dead camera serves a placeholder from 2021 (so: STALE_IMAGE_S) or a 404.

COUNTY_GOING = re.compile(r"going\s+(north|south|east|west)(?:\s+on\s+(?:the\s+)?([^.]+?))?\s*\.?\s*$", re.I)


def parse_county_cameras(doc, bbox=DEFAULT_BBOX) -> list[dict]:
    out, seen = [], set()
    feats = doc.get("features") if isinstance(doc, dict) else None
    for f in feats or []:
        a, g = (f.get("attributes") or {}), (f.get("geometry") or {})
        n = _f(a.get("NATIVE_CCTV_ID"))
        lat, lon = _f(g.get("y")), _f(g.get("x"))
        if n is None or not in_bbox(lat, lon, bbox):
            continue
        cid = f"wc{int(n)}"
        if cid in seen:
            continue
        seen.add(cid)
        url = _url(re.sub(r"^http://(www\.)?westchestergov\.com/", "https://www.westchestergov.com/", str(a.get("URL") or ""))) \
            or COUNTY_IMAGE.format(id=int(n))
        desc = _s(a.get("DESCRIPTION"), 200)
        m = COUNTY_GOING.search(desc)
        road = _s(m.group(2), 80) if m and m.group(2) else ""
        rate = _f(re.sub(r"[^\d.]", "", str(a.get("REFRESH_RATE") or ""))) or CADENCE_S[COUNTY]
        mp = _f(a.get("MILE_POINT"))
        out.append({"id": cid, "name": _s(a.get("CCTV_NAME") or a.get("LOCATION") or cid, 120), "road": road,
                    "dir": f"{m.group(1).capitalize()}bound" if m else "", "mile": f"{mp:g}" if mp is not None else "",
                    "lat": round(lat, 6), "lon": round(lon, 6), "url": url, "video": "", "has_video": False,
                    "off": str(a.get("STATUS") or "").lower() in ("inactive", "disabled", "off"), "cadence": int(rate)})
    out.sort(key=lambda c: int(c["id"][2:]))
    return out


COUNTY_PARSE = {"cameras": parse_county_cameras, "events": lambda doc, bbox=DEFAULT_BBOX: [], "signs": lambda doc, bbox=DEFAULT_BBOX: []}


# The highways the gazetteer has mile markers for (#67, #83), as named in a location label ("I-95 SB
# exit 21", "Hutch NB MM 15.6"), a typed address ("I-287 eastbound at exit 10") or a camera's road.
HIGHWAY_WORDS = (
    (re.compile(r"\bi[- ]?95\b|new england (thruway|twy)", re.I), "I-95"),
    (re.compile(r"\bi[- ]?287\b|cross westchester", re.I), "I-287"),
    (re.compile(r"\bi[- ]?684\b", re.I), "I-684"),
    (re.compile(r"\bhutch(inson)?\b|\bhrp\b", re.I), "Hutch"),
    (re.compile(r"\bus[- ]?1\b|\b(us )?(rte|route) 1\b|boston post", re.I), "US-1"),
    (re.compile(r"\bny[- ]?120a\b|\b(rte|route) 120a\b", re.I), "NY-120A"),
    (re.compile(r"\bny[- ]?120\b|\b(rte|route) 120\b", re.I), "NY-120"),
)
DIR_WORDS = re.compile(r"\b(nb|sb|eb|wb|northbound|southbound|eastbound|westbound)\b", re.I)


def highway_from_text(text: str) -> tuple[str, str] | None:
    """("I-95", "SB") from "I-95 SB exit 21" / "I-95 southbound at exit 21"; the direction "" when none."""
    if not text:
        return None
    for rx, route in HIGHWAY_WORDS:
        if rx.search(text):
            m = DIR_WORDS.search(text)
            return route, (m.group(1)[0].upper() + "B") if m else ""
    return None


def c_dir(c: dict) -> str:
    return c.get("dir", "") or ""


def cam_dir(text: str) -> str:
    """A camera's direction ("Northbound", "NB") as "NB"; "" when it has none."""
    m = DIR_WORDS.search(text or "")
    return (m.group(1)[0].upper() + "B") if m else ""


def _since(headers, since_q: str) -> float | None:
    """The time of the picture the caller already has: If-None-Match ("<unix s>"), If-Modified-Since, ?since=."""
    h = headers or {}
    get = h.get if hasattr(h, "get") else (lambda k, d=None: d)
    for raw in (since_q, (get("If-None-Match") or "").strip('" W/'), get("If-Modified-Since") or ""):
        if not raw:
            continue
        if re.fullmatch(r"\d{9,11}", str(raw)):
            return float(raw)
        try:
            return parsedate_to_datetime(str(raw)).timestamp()
        except (TypeError, ValueError, IndexError):
            continue
    return None


def ts_frames(b: bytes) -> int:
    """Video frames in an MPEG-TS segment: the PES starts on the video PID (from the PAT and PMT)."""
    pmt_pid = video_pid = None
    n = 0
    for i in range(0, len(b) - 187, 188):
        if b[i] != 0x47:
            continue
        pid = ((b[i + 1] & 0x1F) << 8) | b[i + 2]
        pusi = (b[i + 1] >> 6) & 1
        afc = (b[i + 3] >> 4) & 3
        off = 4 + (1 + b[i + 4] if afc & 2 else 0)
        if off >= 188:
            continue
        try:
            if pid == 0 and pmt_pid is None:
                p = off + 1 + b[i + off]
                pmt_pid = ((b[i + p + 10] & 0x1F) << 8) | b[i + p + 11]
            elif pid == pmt_pid and video_pid is None:
                p = off + 1 + b[i + off]
                sl = ((b[i + p + 1] & 0x0F) << 8) | b[i + p + 2]
                pil = ((b[i + p + 10] & 0x0F) << 8) | b[i + p + 11]
                j, end = p + 12 + pil, p + 3 + sl - 4
                while j < end:
                    stype = b[i + j]
                    epid = ((b[i + j + 1] & 0x1F) << 8) | b[i + j + 2]
                    eil = ((b[i + j + 3] & 0x0F) << 8) | b[i + j + 4]
                    if stype in (0x1B, 0x24, 0x02):
                        video_pid = epid
                    j += 5 + eil
            elif pid == video_pid and pusi:
                n += 1
        except IndexError:
            continue
    return n


def _segment_duration(chunklist: bytes, name: str) -> float:
    """The #EXTINF before `name` in a chunklist, or 0."""
    dur = 0.0
    for t in chunklist.decode(errors="replace").splitlines():
        if t.startswith("#EXTINF:"):
            try:
                dur = float(t[8:].split(",")[0])
            except ValueError:
                dur = 0.0
        elif t.strip() == name:
            return dur
    return 0.0


def highway_places(path: str | Path | None, bbox=DEFAULT_BBOX) -> dict:
    """The mile markers and exits in the gazetteer's places table (#67: NYSDOT reference markers, OSM
    motorway junctions), in our area. {} when that table isn't there yet (before #67)."""
    from sizeup.geo.gazetteer import DEFAULT_DB
    p = Path(path) if path else DEFAULT_DB
    if not p.is_file():
        return {}
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            w, s, e, n = bbox
            rows = db.execute("SELECT kind, name, route, dir, value, lat, lon FROM places WHERE kind IN ('milemarker', 'exit') "
                              "AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?", (s, n, w, e)).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    out = {"mileposts": [], "exits": []}
    for kind, name, route, d, value, lat, lon in rows:
        item = {"label": name, "route": route, "dir": d, "value": value, "lat": round(lat, 6), "lon": round(lon, 6)}
        out["mileposts" if kind == "milemarker" else "exits"].append(item)
    return out


# ---- the client ------------------------------------------------------------------------------------------


def _gunzip(body: bytes, limit: int) -> bytes:
    import zlib
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise ValueError(f"more than {limit // (1024 * 1024)} MB unzipped")
    return out


def http_get(url: str, timeout: float, limit: int, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """(status, headers, body): urllib, GET only, gzip asked for and undone here; a 304 comes back as (304, headers,
    b""). Errors as HTTPError / URLError / OSError. headers: the request's own (Authorization, If-None-Match, ...)."""
    hdrs = dict(headers or {})
    auth = hdrs.pop("Authorization", "")
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json, image/*;q=0.9, */*;q=0.5",
                                "Accept-Encoding": "gzip", **hdrs})
    if auth:                    # never carried along a redirect (urllib copies the other headers to wherever it goes)
        req.add_unredirected_header("Authorization", auth)
    try:
        with urlopen(req, timeout=timeout) as r:
            body = r.read(limit + 1)
            if len(body) > limit:
                raise ValueError(f"more than {limit // (1024 * 1024)} MB")
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            # read(n) hands back what came when the server closes early, with no IncompleteRead (#276: a segment cut
            # short must never be served): the body has to be as long as the server said
            want = hdrs.get("content-length", "").strip()
            if want.isdigit() and len(body) < int(want):
                raise ValueError(f"cut short: {len(body)} of {want} bytes")
            if hdrs.get("content-encoding", "").lower() == "gzip":
                body = _gunzip(body, limit)
            return r.status, hdrs, body
    except HTTPError as e:
        if e.code == 304:
            return 304, {k.lower(): v for k, v in (e.headers or {}).items()}, b""
        raise


class BasicAuth:
    """511NY's one username and password (env SIZEUP_511NY_USER / SIZEUP_511NY_PASS; the hub: SSM /sizeup-hub/511ny-user
    and -pass), as the CARS API's Swagger definition asks: HTTP Basic (securityDefinitions basicAuth; the console's
    swagger-oauth.js is Swagger UI's unused stock file: its client id is "your-client-id"). Only ever the Authorization
    header of a request to a feed that needs it; never in repr, a URL, an error or the log (secrets(): what _redact
    takes out)."""

    def __init__(self, user: str = "", password: str = ""):
        self._user, self._password = (user or "").strip(), (password or "")

    def __bool__(self) -> bool:
        return bool(self._user and self._password)

    def __repr__(self) -> str:
        return f"BasicAuth({'set' if self else 'not set'})"

    __str__ = __repr__

    def header(self) -> dict:
        import base64
        if not self:
            return {}
        tok = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def secrets(self) -> tuple[str, ...]:
        if not self:
            return ()
        out = [self._password, self.header()["Authorization"].split(" ", 1)[1]]
        if len(self._user) >= 4:
            out.append(self._user)
        return tuple(out)


def redact(text: str, secrets=()) -> str:
    """_redact for any number of secrets (the key, a password, a Basic token), longest first."""
    text = str(text)
    for x in sorted((x for x in secrets if x), key=len, reverse=True):
        text = text.replace(x, "***")
    return _redact(text, "")


class Provider:
    """One source's feeds: their addresses, parsers and schedule, what each last read and how it went.
    511NY (CARS: the public signs, the API's events with the username and password), its CARS-Hub CCTV, the Thruway
    and the county (fixed public addresses, no login). Feeds that share an address (the CARS-Hub's events and
    cameras) are one request. Each feed remembers its ETag / Last-Modified and asks with them."""

    def __init__(self, name: str, short: str, label: str, link: str, urls: dict | None, parse: dict, every: dict | None,
                 defaults: dict, floors: dict, key: str, bbox, fetch, log, clock, changed: Callable[[], None],
                 auth: BasicAuth | None = None, auth_feeds: tuple[str, ...] = (), why_off: dict | None = None,
                 after: Callable[[Provider, str], None] | None = None):
        self.name, self.short, self.label, self.link = name, short, label, link
        self.urls = {f: str((urls or {}).get(f) or "").strip() for f in FEEDS}
        self.parse, self.key, self.bbox = parse, (key or "").strip(), bbox
        self.auth, self.auth_feeds = auth or BasicAuth(), tuple(auth_feeds)
        self.why_off = dict(why_off or {})
        self.after = after
        self.every = {f: max(floors[f], float((every or {}).get(f, defaults[f]))) for f in FEEDS}
        self.floors = floors
        self.fetch, self.log, self.clock, self.changed = fetch, log, clock, changed
        self.data: dict[str, list[dict]] = {f: [] for f in FEEDS}
        self.status: dict[str, dict] = {f: {"at": None, "n": 0, "error": "", "next": 0.0, "fails": 0, "etag": "",
                                            "modified": "", "published": None, "note": ""} for f in FEEDS}

    def secrets(self) -> tuple[str, ...]:
        return tuple(x for x in (self.key, *self.auth.secrets()) if x)

    def feed_on(self, feed: str) -> bool:
        u = self.urls.get(feed, "")
        return bool(u) and ("{key}" not in u or bool(self.key)) and (feed not in self.auth_feeds or bool(self.auth))

    def feed_why(self, feed: str) -> str:
        """Why a feed is off ("" when on): no address yet, or waiting for the credentials."""
        if self.feed_on(feed):
            return ""
        if not self.urls.get(feed):
            return self.why_off.get(feed, "") or f"{self.short} {feed}: no feed address"
        if feed in self.auth_feeds and not self.auth:
            return WAITING
        return f"{self.short} {feed}: no key"

    @property
    def configured(self) -> bool:
        """At least one feed can be read (an address, and the key when it needs one)."""
        return any(self.feed_on(f) for f in FEEDS)

    def why_not(self) -> str:
        if self.configured:
            return ""
        if self.name == "511ny":
            if not any(self.urls.values()):
                return "511NY off (no feed address: [roads] ny511 = false, or each one \"off\")"
            if any(f in self.auth_feeds and self.urls[f] for f in FEEDS):
                return f"511NY: {WAITING}"
            return "511NY: no key for its feed addresses"
        if self.name == CARSHUB:
            return f"511NY CARS-Hub: {self.feed_why('cameras')}"
        return f"{self.short} cameras off"

    def url(self, feed: str) -> str:
        return self.urls[feed].replace("{key}", quote(self.key, safe=""))

    def updated_at(self) -> float | None:
        ats = [s["at"] for s in self.status.values() if s["at"]]
        return max(ats) if ats else None

    def due(self) -> list[str]:
        now = self.clock()
        return [f for f in FEEDS if self.feed_on(f) and now >= self.status[f]["next"]]

    def _shared(self, feed: str) -> list[str]:
        """The feeds read by this one request: feed, and any other on at the same address."""
        return [feed] + [f for f in FEEDS if f != feed and self.feed_on(f) and self.urls[f] == self.urls[feed]]

    def _ask(self, feed: str, fresh: bool = False) -> tuple[int, dict, bytes]:
        headers = dict(self.auth.header()) if feed in self.auth_feeds else {}
        st = self.status[feed]
        if not fresh and st["at"] is not None:
            if st["etag"]:
                headers["If-None-Match"] = st["etag"]
            if st["modified"]:
                headers["If-Modified-Since"] = st["modified"]
        if headers:
            return self.fetch(self.url(feed), TIMEOUT_S, MAX_FEED_BYTES, headers)
        return self.fetch(self.url(feed), TIMEOUT_S, MAX_FEED_BYTES)

    def refresh(self, feed: str, fresh: bool = False) -> bool:
        """Fetch one feed now (and the feeds at its address); True when it was read (a 304 too). Never raises.
        fresh: without If-None-Match / If-Modified-Since (Refresh now)."""
        if not self.feed_on(feed):
            return False
        now = self.clock()
        feeds = self._shared(feed)
        try:
            code, headers, body = self._ask(feed, fresh)
            if code == 304:
                for f in feeds:
                    self.status[f].update(at=now, error="", fails=0, next=now + self.every[f], checked=now)
                return True
            if code != 200:
                raise HTTPError("", code, f"HTTP {code}", None, None)
            published = None
            if headers.get("last-modified"):
                try:
                    published = parsedate_to_datetime(headers["last-modified"]).timestamp()
                except (TypeError, ValueError, IndexError):
                    published = None
            got = {}
            doc = None
            for f in feeds:
                fn = self.parse[f]
                if getattr(fn, "raw", False):
                    got[f] = fn(body, self.bbox, {"published": published, "now": now})
                else:
                    doc = decode(body) if doc is None else doc
                    got[f] = fn(doc, self.bbox)
            del body, doc
        except HTTPError as e:
            return self._failed(feed, now, e.code, f"HTTP {e.code}")
        except (URLError, OSError, ValueError) as e:
            return self._failed(feed, now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}")
        except Exception as e:  # a surprise in the feed must not stop the others
            return self._failed(feed, now, 0, f"{type(e).__name__}: {e}")
        for f, items in got.items():
            for it in items:
                it["source"], it["provider"] = self.short, self.name
                it.pop("missing_since", None)
            if f == "cameras":
                items = items + self._still_missing(items, now)
            self.data[f] = items
            stale = published is not None and now - published > SIGNS_STALE_S
            self.status[f].update(at=now, n=sum(1 for it in items if not it.get("missing_since")), error="", fails=0, next=now + self.every[f], checked=now,
                                  etag=headers.get("etag", "") or "", modified=headers.get("last-modified", "") or "",
                                  published=published,
                                  note=(f"{self.short}'s copy was published {_ago(now - published)} ago" if stale else ""))
            if self.after is not None:
                try:
                    self.after(self, f)
                except Exception as e:  # pragma: no cover - a comparison's trouble never stops the feed
                    self.log(f"roads: {self.short} {f}: after-read error {type(e).__name__}")
        self.changed()
        return True

    def _still_missing(self, items: list[dict], now: float) -> list[dict]:
        """The cameras of the last list that this read left out, kept (missing_since) for CAMERA_KEEP_S (#276)."""
        ids = {it["id"] for it in items}
        out = []
        for old in self.data["cameras"]:
            if old["id"] in ids:
                continue
            since = old.get("missing_since") or now
            if now - since <= CAMERA_KEEP_S:
                out.append(dict(old, missing_since=since))
        if out:
            self.log(f"roads: {self.short} cameras: {len(out)} not in the latest list, kept (stale) for now")
        return out

    def _failed(self, feed: str, now: float, code: int, why: str) -> bool:
        st = self.status[feed]
        why = redact(why, self.secrets())
        st["fails"] += 1
        if code in (401, 403):
            wait = REFUSED_S
            if feed in self.auth_feeds:
                why = f"{self.short} refused the username and password ({why})"
            else:
                why = f"{self.short} refused the key ({why})" if self.key else f"{self.short} refused the request ({why})"
        elif code == 429:
            wait, why = RATE_LIMITED_S, f"{self.short} rate limit ({why})"
        else:
            wait = max(self.floors[feed], min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(st["fails"] - 1, 30)))
        changed = st["error"] != why
        for f in self._shared(feed):
            self.status[f].update(error=why, next=now + wait)
            if f != feed:
                self.status[f]["fails"] += 1
        if changed:
            self.changed()
        self.log(f"roads: {self.short} {feed}: {why}; next try in {wait:.0f} s")
        return False

    def public(self) -> dict:
        notes = [s["note"] for s in self.status.values() if s.get("note")]
        return {"name": self.name, "short": self.short, "label": self.label, "link": self.link, "configured": self.configured,
                "at": self.updated_at(), "note": "; ".join(dict.fromkeys(notes)),
                "status": {f: {"on": self.feed_on(f), "at": s["at"], "n": s["n"], "error": s["error"],
                               "why": self.feed_why(f) if self.urls.get(f) or f in self.why_off else ""}
                           for f, s in self.status.items()}}


def _ago(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60} min" if s < 5400 else f"{s / 3600:.1f} h"


class Roads:
    def __init__(self, key: str = "", urls: dict | None = None, bbox=DEFAULT_BBOX, cache: str | Path | None = None,
                 every: dict | None = None, gazetteer: str | Path | None = None,
                 fetch: Callable[[str, float, int], tuple[int, dict, bytes]] = http_get,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 thruway: bool = True, thruway_urls: dict | None = None, county: bool = True, county_urls: dict | None = None,
                 stream_max: int = STREAM_MAX, wall_streams: int = 0, auth: BasicAuth | None = None,
                 auth_feeds: tuple[str, ...] = (), carshub_url: str = "", carshub: bool = False,
                 carshub_imagery: bool = True):
        self.bbox = tuple(float(x) for x in bbox)
        self.stream_max = max(1, min(int(stream_max), STREAM_MAX_LIMIT))   # live streams at once, hub-wide
        self.wall_streams = max(0, int(wall_streams))   # a camera wall's live tiles at once per page (0: the area's streams)
        # The camera wall's area (#125): the app hands in the county GIS layers (the fire districts), the
        # Settings edits ("camera_wall": margin_m, include, exclude) and our departments' names.
        self.gis = None
        self.wall_settings: Callable[[], dict] = dict
        self.ours: tuple[str, ...] = ()
        self.cache = Path(cache) if cache else None
        self.fetch, self.log, self.clock = fetch, log, clock
        self._lock = threading.Lock()
        # Each process starts its rev from its clock (#276): a page holding an older hub's rev (a deploy, a hot/cold
        # handover) never hears "same" from a new hub whose own counter happens to be there.
        self.rev = int(self.clock() * 1000)
        self._public: tuple[int, bytes] | None = None
        self._merge: tuple[int, dict] | None = None
        # 511NY first: when both are on, it wins a tie (the same sign or event from both). urls: its feeds' addresses
        # (from_config: NY511_URLS, [roads] events_url / signs_url); auth_feeds: those that need the username and
        # password (the API's events).
        self.auth = auth or BasicAuth()
        self.carshub_imagery = bool(carshub_imagery)
        self.camera_compare: dict | None = None
        self.ny = Provider("511ny", SOURCE, LABEL, LINK_URL, urls, PARSE, every, NY511_EVERY_S, NY511_MIN_S, key,
                           self.bbox, fetch, log, clock, self._changed, auth=self.auth, auth_feeds=auth_feeds,
                           why_off={"events": WAITING} if "events" in auth_feeds else {})
        # The CARS-Hub's CCTV inventory (data/cctv.xml, HTTP Basic, the same username and password): its cameras,
        # counted against ours; on the map only with carshub_imagery
        self.carshub = (Provider(CARSHUB, SOURCE, CARSHUB_LABEL, f"{CARS_HUB}index.jsf", {"cameras": carshub_url},
                                 CARSHUB_PARSE, every, NY511_EVERY_S, NY511_MIN_S, "", self.bbox, fetch, log, clock,
                                 self._changed, auth=self.auth, auth_feeds=("cameras",),
                                 why_off={"cameras": "off ([roads] ny511_hub = \"off\")"}, after=self._carshub_read)
                        if carshub else None)
        self.thruway = (Provider("thruway", THRUWAY, THRUWAY_LABEL, THRUWAY_LINK,
                                 THRUWAY_URLS if thruway_urls is None else thruway_urls, THRUWAY_PARSE, None,
                                 THRUWAY_EVERY_S, MIN_EVERY_S, "", self.bbox, fetch, log, clock, self._changed)
                        if thruway else None)
        self.county = (Provider("county", COUNTY, COUNTY_LABEL, COUNTY_LINK, COUNTY_URLS if county_urls is None else county_urls,
                                COUNTY_PARSE, None, COUNTY_EVERY_S, COUNTY_MIN_S, "", self.bbox, fetch, log, clock, self._changed)
                       if county else None)
        self.providers = [p for p in (self.ny, self.carshub, self.thruway, self.county) if p is not None]
        self._health: dict[str, dict] = {}            # camera id -> {fails, down_since, last_image, checked}
        self._streams: dict[str, dict] = {}           # camera id -> {last (playlist request), base (the stream's folder)}
        self._segments: OrderedDict[str, dict] = OrderedDict()   # "cid/name" -> {body, ctype, fetched, kept, ...}
        self._segment_locks: dict[str, list] = {}               # "cid/name" -> [lock, users]: one fetch at a time
        self._stream_lock = threading.Lock()
        self._stream_down: dict[str, float] = {}      # camera id -> when its source last refused to stream
        self._probe: dict[str, dict] = {}             # camera id -> {at, ok}: its stream looked at by the sweep (#276)
        self._state_sig: tuple | None = None          # the cameras' states as the pages last heard them
        self._pumps: dict[str, threading.Thread] = {}
        self._swept = 0.0
        self._images: OrderedDict[str, dict] = OrderedDict()
        self._image_locks: dict[str, threading.Lock] = {}
        self._parallel = threading.BoundedSemaphore(IMAGE_PARALLEL)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.places = highway_places(gazetteer, self.bbox)
        self._load()

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print) -> Roads:
        state = Path(cfg.cad_db).parent if cfg.cad_db else None
        on = cfg.roads_enabled
        ny = on and getattr(cfg, "roads_ny511", True)
        pick = lambda v, feed: "" if v == "off" else (v or NY511_URLS.get(feed, ""))      # "": 511NY's own address
        urls = {"events": pick(getattr(cfg, "roads_events_url", ""), "events"),
                "signs": pick(getattr(cfg, "roads_signs_url", ""), "signs")} if ny else {}
        return cls("", urls, cfg.roads_bbox, (state / "roads511.json") if state else None,
                   {"cameras": cfg.roads_cameras_s, "events": cfg.roads_events_s, "signs": cfg.roads_signs_s}, log=log,
                   thruway=on and cfg.roads_thruway, county=on and cfg.roads_county,
                   stream_max=getattr(cfg, "roads_stream_max", STREAM_MAX), wall_streams=getattr(cfg, "roads_wall_streams", 0),
                   auth=BasicAuth(getattr(cfg, "roads_511ny_user", ""), getattr(cfg, "roads_511ny_pass", "")),
                   auth_feeds=("events",) if ny else (), carshub=ny,
                   carshub_url=hub_cctv(getattr(cfg, "roads_ny511_hub", "")) if ny else "",
                   carshub_imagery=getattr(cfg, "roads_carshub_imagery", True))

    @property
    def configured(self) -> bool:
        """At least one provider can be read."""
        return any(p.configured for p in self.providers)

    def why_not(self) -> str:
        """Why nothing is fetched at all ("" when something is)."""
        if self.configured:
            return ""
        return "; ".join(p.why_not() for p in self.providers) or "highway layers off ([roads] enabled = false)"

    def provider(self, name: str) -> Provider | None:
        return next((p for p in self.providers if p.name == name), None)

    # -- the schedule ----------------------------------------------------------------------------------

    def start(self) -> Roads:
        for p in self.providers:
            if not p.configured:
                self.log(f"roads: {p.why_not()}")
            else:
                for f in p.auth_feeds:
                    if not p.feed_on(f):
                        self.log(f"roads: {p.short} {f}: {p.feed_why(f)}")
        if not self.configured:
            self.log("roads: no camera, incident or sign source: those layers off; mile markers and exits still shown")
            return self
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="roads", daemon=True)
            self._thread.start()
            for p in self.providers:
                if p.configured:
                    on = [f"{f} every {p.every[f]:.0f} s" for f in FEEDS if p.feed_on(f)]
                    self.log(f"roads: {p.short} on ({', '.join(on)}), area {self.bbox}")
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            for p in self.providers:
                for feed in ("events", "signs", "cameras"):
                    if self._stop.is_set():
                        break
                    if p.feed_on(feed) and self.clock() >= p.status[feed]["next"]:
                        if self.refresh(feed, p) and feed == "cameras":
                            self._swept = 0.0                   # a new camera list: look at them now
            if self.clock() >= self._swept + HEALTH_SWEEP_S:
                self.sweep()
            self.state_tick()
            self._stop.wait(5.0)

    def sweep(self) -> int:
        """Look at every camera's snapshot not looked at within HEALTH_SWEEP_S, so the map knows which are
        down before anyone opens one. The number fetched."""
        now = self.clock()
        self._swept = now
        n = 0
        for c in list(self.merged("cameras")):
            if self._stop.is_set():
                break
            h = self._health.get(c["id"])
            if c["off"] or (h and now - h["checked"] < HEALTH_SWEEP_S):
                continue
            self.image(c["id"])
            n += 1
            # (#276) a live-video camera whose still is down: is its stream there? (two small GETs, as a player's first)
            if c.get("video") and (self._health.get(c["id"]) or {}).get("down_since") is not None:
                self.probe_stream(c["id"])
        return n

    def probe_stream(self, cid: str) -> bool:
        """A live-video camera's stream looked at: its master playlist and its chunklist, as a player's first two
        requests (an open stream with a fresh chunklist counts without asking). Kept in _probe; a refusal also in
        _stream_down (the viewers get snapshots at once). True when it streams."""
        cam = self.camera(cid)
        if cam is None or not cam.get("video"):
            return False
        now = self.clock()
        st = self._streams.get(cid)
        ok = bool(st and st.get("list") and now - st.get("list_at", 0) < 30)
        if not ok:
            base = cam["video"].rsplit("/", 1)[0] + "/"
            try:
                code, _, body = self.fetch(cam["video"], STREAM_OPEN_TIMEOUT_S, 1 << 20)
                clean = self._clean_playlist(body) if code == 200 and body else None
                chunk = next((t for t in (clean or b"").decode().splitlines() if t and not t.startswith("#")), "")
                if chunk:
                    code, _, body = self.fetch(base + chunk, STREAM_TIMEOUT_S, 1 << 20)
                    ok = code == 200 and b".ts" in (body or b"")
            except (HTTPError, URLError, OSError, ValueError):
                ok = False
        was = self._probe.get(cid, {}).get("ok")
        self._probe[cid] = {"at": now, "ok": ok}
        if not ok:
            self._stream_down[cid] = now
            self.log(f"roads: {cid}: still down and no live video either: offline")
        if was != ok:
            self._changed()
        return ok

    def state_tick(self) -> bool:
        """The cameras' states change with time too (a refusal or a list's age running out, a probe gone old): the rev
        moves when any did, so the pages hear of it. True when it moved."""
        sig = tuple((c["id"], self.health(c["id"], c)["state"]) for c in self.merged("cameras"))
        if sig == self._state_sig:
            return False
        moved = self._state_sig is not None
        self._state_sig = sig
        if moved:
            self._changed()
        return moved

    def due(self) -> list[tuple[str, str]]:
        """(provider, feed) pairs to fetch now."""
        return [(p.name, f) for p in self.providers for f in p.due()]

    def refresh(self, feed: str, provider: Provider | str | None = None, fresh: bool = False) -> bool:
        """Fetch one feed now, from one provider or all; True when any was read. Never raises. fresh: without
        If-None-Match / If-Modified-Since (Settings > Data sources' Refresh now)."""
        ps = [self.provider(provider) if isinstance(provider, str) else provider] if provider else self.providers
        got = False
        for p in ps:
            if p is not None and p.refresh(feed, fresh):
                got = True
                if feed == "cameras" and p is not self.carshub and self.carshub is not None and self.carshub.status["cameras"]["at"]:
                    self._carshub_read(self.carshub, "cameras")      # ours changed: count the CARS-Hub's against them again
        if got:
            self._save()
        return got

    def _changed(self) -> None:
        with self._lock:
            self.rev += 1
            self._public = None
            self._merge = None

    # -- the disk cache (items only: no key, no feed address) ------------------------------------------

    def _load(self) -> None:
        if not self.cache or not self.cache.is_file():
            return
        try:
            doc = json.loads(self.cache.read_text())
            if [round(x, 4) for x in doc.get("bbox", [])] != [round(x, 4) for x in self.bbox]:
                return                                    # another area: fetch afresh
            now = self.clock()
            saved = doc.get("providers") or {"511ny": {f: doc.get(f) for f in FEEDS}}   # (an older cache: 511NY's)
            for p in self.providers:
                for feed in FEEDS:
                    f = (saved.get(p.name) or {}).get(feed) or {}
                    at, items = f.get("at"), f.get("items")
                    if not isinstance(items, list) or not isinstance(at, (int, float)) or at > now + 60:
                        continue
                    for it in items:
                        it.setdefault("source", p.short)
                        it.setdefault("provider", p.name)
                    p.data[feed] = items
                    p.status[feed].update(at=at, n=len(items), next=at + p.every[feed], etag=str(f.get("etag") or ""),
                                          modified=str(f.get("modified") or ""),
                                          published=f.get("published") if isinstance(f.get("published"), (int, float)) else None)
            self._changed()
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.log(f"roads: cache {self.cache.name} not read ({type(e).__name__}); fetching afresh")

    def _save(self) -> None:
        if not self.cache:
            return
        doc = {"bbox": list(self.bbox),
               "providers": {p.name: {f: {"at": p.status[f]["at"], "items": p.data[f], "etag": p.status[f]["etag"],
                                          "modified": p.status[f]["modified"], "published": p.status[f]["published"]}
                                      for f in FEEDS} for p in self.providers}}
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(self.cache)
        except OSError as e:
            self.log(f"roads: cache not saved ({type(e).__name__}: {e})")

    # -- what the pages get ----------------------------------------------------------------------------

    def _order(self, feed: str) -> list[Provider]:
        """Who wins a tie: 511NY first for events and signs; for cameras ours first (the Thruway's, the county's) and a
        CARS-Hub camera where we have none, or in place of ours when only it has live video (merged); with
        carshub_imagery off ([roads]), its cameras are only counted (camera_compare), not shown."""
        if feed != "cameras":
            return self.providers
        out = [p for p in self.providers if p.name != CARSHUB]
        if self.carshub is not None and self.carshub_imagery:
            out.append(self.carshub)
        return out

    def merged(self, feed: str) -> list[dict]:
        """Every provider's items, in _order; a later provider's item that is already there is left out: the same id,
        or within DEDUPE_M of another provider's (the same layer, for events), or for signs within SIGN_DEDUPE_M saying
        the same thing (and facing the same way, when both say)."""
        with self._lock:
            cur = self._merge
            if cur is not None and cur[0] == self.rev and feed in cur[1]:
                return cur[1][feed]
        out, ids, whose = [], set(), []

        def same(it: dict, o: dict, mine: str, theirs: str) -> bool:
            if feed == "cameras" and CARSHUB in (mine, theirs):
                return same_camera(it, o)
            d = km(it["lat"], it["lon"], o["lat"], o["lon"]) * 1000
            if feed == "signs":
                return (d <= SIGN_DEDUPE_M and sign_text(it) == sign_text(o)
                        and not (it.get("dir") and o.get("dir") and it["dir"] != o["dir"]))
            return d <= DEDUPE_M and (feed != "events" or it.get("layer") == o.get("layer"))
        for p in self._order(feed):
            for it in p.data[feed]:
                if it["id"] in ids:
                    continue
                twin = next((i for i, (o, w) in enumerate(zip(out, whose)) if w != p.name and same(it, o, p.name, w)), None)
                if twin is not None:
                    o = out[twin]
                    # the same camera: the one with live video (a CARS-Hub HLS camera over the county's JPEG one)
                    if feed == "cameras" and it.get("has_video") and not o.get("has_video") and not it.get("off"):
                        ids.discard(o["id"])
                        out[twin], whose[twin] = it, p.name
                        ids.add(it["id"])
                    continue
                out.append(it)
                whose.append(p.name)
                ids.add(it["id"])
        with self._lock:
            if self._merge is None or self._merge[0] != self.rev:
                self._merge = (self.rev, {})
            self._merge[1][feed] = out
        return out

    def _carshub_read(self, p: Provider, feed: str) -> None:
        """The CARS-Hub's cameras read: how many in our area are new against ours (the Thruway's, the county's,
        within CAMERA_MATCH_M), by road; logged when it changes and shown in Settings > Data sources."""
        if feed != "cameras":
            return
        ours = [c for q in (self.thruway, self.county) if q is not None for c in q.data["cameras"]]
        cmp = compare_cameras(p.data["cameras"], ours)
        text = compare_text(cmp) + ("" if self.carshub_imagery else "; not shown ([roads] carshub_imagery = false)")
        if self.camera_compare is None or self.camera_compare.get("text") != text:
            self.log(f"roads: 511NY CARS-Hub: {text}")
        self.camera_compare = cmp | {"text": text}
        p.status["cameras"]["note"] = text

    def note(self) -> str:
        """What the Layers box says of 511NY beyond "updated hh:mm": a feed still waiting (the credentials), a stale
        copy of the signs."""
        ny, out = self.ny, []
        if not ny.configured:
            out.append(ny.why_not())
        elif "events" in ny.auth_feeds and not ny.feed_on("events"):
            out.append(f"511NY incidents and closures: {ny.feed_why('events')}")
        if ny.feed_on("signs") and ny.status["signs"].get("note"):
            out.append(f"511NY signs: {ny.status['signs']['note']}")
        return "; ".join(x for x in out if x)

    def _cam_public(self, c: dict) -> dict:
        out = {k: c.get(k, "") for k in ("id", "name", "road", "dir", "mile", "lat", "lon", "has_video", "off", "source")}
        out["cadence"] = int(c.get("cadence") or CADENCE_S.get(c.get("source", ""), 60))
        return out | self.health(c["id"], c)

    # -- camera health: down when its image is stale or it keeps failing (#99); a video camera by its stream (#276) --

    @staticmethod
    def stale_s(cam: dict | None) -> float:
        """How old a camera's still may be before it counts as no current image (STALE_IMAGE_BY_SOURCE)."""
        return STALE_IMAGE_BY_SOURCE.get((cam or {}).get("source", ""), STALE_IMAGE_S)

    def health(self, cid: str, cam: dict | None = None) -> dict:
        """{down, down_since, last_image, state: ok | stale | offline, why: a short reason ("" when ok), since}.
        down (offline): a snapshot camera whose still is stale or failing; a live-video camera when its still is and
        its stream's source refused too (a viewer's open, or the sweep's probe_stream). stale: still listed and offered, but its source's list left it out or hasn't been read
        lately, or (video) its stream refused while its still is fine."""
        cam = cam if cam is not None else self.camera(cid)
        h = self._health.get(cid) or {"fails": 0, "down_since": None, "last_image": None}
        now = self.clock()
        video = bool(cam and cam.get("video") and not cam.get("off"))
        refused = self._stream_down.get(cid)
        refused = refused if refused is not None and now - refused < STREAM_RETRY_S else None
        still_down = h["down_since"] is not None
        probe = self._probe.get(cid)
        probe = probe if probe is not None and now - probe["at"] < 2 * HEALTH_SWEEP_S else None
        if video and still_down and refused is None and probe is not None and not probe["ok"]:
            refused = probe["at"]                     # the sweep found no stream either: offline until it finds one
        down = still_down and (not video or refused is not None)
        out = {"down": down, "down_since": (max(h["down_since"], refused) if video else h["down_since"]) if down else None,
               "last_image": h["last_image"], "state": "ok", "why": "", "since": None}
        if down:
            what = "No video or image" if video else "Not answering" if h["fails"] >= DOWN_FAILS else "Image stale"
            return out | {"state": "offline", "why": what, "since": out["down_since"]}
        if cam is None:
            return out
        p = self.provider(cam.get("provider", "")) if cam.get("provider") else None
        st = p.status["cameras"] if p is not None else None
        if cam.get("missing_since"):
            return out | {"state": "stale", "why": f"Not in {cam.get('source') or 'its source'}'s list",
                          "since": cam["missing_since"]}
        if st and st["error"] and st["at"] and now - st["at"] > 2 * p.every["cameras"]:
            return out | {"state": "stale", "why": f"{cam.get('source') or 'Source'} list not updated", "since": st["at"]}
        if video and refused is not None:
            return out | {"state": "stale", "why": "No live video", "since": refused}
        if video and still_down and probe is None:   # the still is dead and its stream not looked at yet
            return out | {"state": "stale", "why": "Still old · video not checked", "since": h["down_since"]}
        return out

    def _offerable(self, c: dict) -> bool:
        """Offered by the pickers (nearest, cameras_for): not offline, and not a video camera whose still is dead and
        whose stream nobody has looked at yet (#276: it may be dead too)."""
        h = self.health(c["id"], c)
        return not h["down"] and h["why"] != "Still old · video not checked"

    def _note_image(self, cid: str, ok: bool, taken: float | None, now: float, stale_s: float = STALE_IMAGE_S) -> None:
        """A snapshot fetch's outcome: fresh (ok), or stale / failed. Transitions bump the rev."""
        h = self._health.setdefault(cid, {"fails": 0, "down_since": None, "last_image": None, "checked": 0.0})
        h["checked"] = now
        if taken is not None:
            h["last_image"] = max(h["last_image"] or 0, taken)
        was = h["down_since"] is not None
        if ok:
            h["fails"] = 0
            h["down_since"] = None
        else:
            h["fails"] += 1
            stale = taken is not None and now - taken > stale_s
            if (h["fails"] >= DOWN_FAILS or stale) and h["down_since"] is None:
                h["down_since"] = now
        if was != (h["down_since"] is not None):
            self._changed()

    def _down_answer(self, cid: str, cam: dict | None = None) -> tuple[int, bytes, str, dict]:
        h = self.health(cid, cam)
        return 404, json.dumps({"error": "no current image", **h}).encode(), JSON, {"Cache-Control": "no-store"}

    def public(self) -> dict:
        """The layers for the maps. Camera image URLs stay here (the browser uses our proxy)."""
        return {"configured": self.configured, "note": self.note(), "source": SOURCE, "attribution": ATTRIBUTION,
                "link": LINK_URL, "sources": [p.public() for p in self.providers], "rev": self.rev, "bbox": list(self.bbox),
                "cameras": [self._cam_public(c) for c in self.merged("cameras")],
                "events": list(self.merged("events")), "signs": list(self.merged("signs")),
                "mileposts": self.places.get("mileposts", []), "exits": self.places.get("exits", []),
                "highway_places": bool(self.places)}

    def _public_bytes(self) -> bytes:
        cur = self._public
        if cur is None or cur[0] != self.rev:
            cur = (self.rev, json.dumps(self.public(), separators=(",", ":")).encode())
            self._public = cur
        return cur[1]

    def camera(self, cid: str) -> dict | None:
        return next((c for c in self.merged("cameras") if c["id"] == cid), None)

    def nearest(self, lat: float, lon: float, n: int = 3, within_km: float = NEAR_KM) -> list[dict]:
        cams = [c for c in self.merged("cameras") if not c["off"] and self._offerable(c)]
        d = sorted(((km(lat, lon, c["lat"], c["lon"]), c) for c in cams), key=lambda x: x[0])
        return [self._cam_public(c) | {"km": round(k, 2)} for k, c in d[:max(0, n)] if k <= within_km]

    def search_places(self, q: str, near: tuple[float, float] | None = None, limit: int = 8) -> list[dict]:
        """Cameras for the place search ("@I-95 exit 21 camera"), in #67's place shape. Every typed
        word (less "camera", "cam", "cctv") must start a word of the camera's name, road, direction or
        mile; "camera" in the text ranks them first."""
        words = _words(q)
        asked = any(w in ("camera", "cameras", "cam", "cams", "cctv") for w in words)
        words = [w for w in words if w not in ("camera", "cameras", "cam", "cams", "cctv", "at", "the", "on", "near")]
        if not words and not asked:
            return []
        out = []
        for c in self.merged("cameras"):
            hay = _words(f"{c['name']} {c['road']} {c['dir']} {c.get('mile', '')}")
            hay += [h[1:] for h in hay if re.fullmatch(r"i\d+", h)]          # "I-287" is also "287"
            if not all(any(h.startswith(w) for h in hay) for w in words):
                continue
            score = (1.0 if asked else 0.6) - (0.2 if c["off"] else 0.0)
            if near:
                score -= min(0.3, km(near[0], near[1], c["lat"], c["lon"]) / 50)
            out.append({"kind": "camera", "label": f"Camera: {c['name']}", "sub": " ".join(x for x in (c["road"], c["dir"]) if x),
                        "lat": c["lat"], "lon": c["lon"], "source": c.get("source", SOURCE), "camera": c["id"],
                        "score": round(score, 3)})
        out.sort(key=lambda p: -p["score"])
        return out[:max(1, min(limit, 20))]

    # -- the incident viewer's cameras: just before and just after, along the road (#95) -------------------

    def _routes(self) -> dict:
        """{route: [(mileage, lat, lon), ...]} from the gazetteer's mile markers (both carriageways' rows)."""
        cached = getattr(self, "_route_markers", None)
        if cached is not None:
            return cached
        out: dict[str, list] = {}
        for m in self.places.get("mileposts", []):
            v = _f(m.get("value"))
            if v is not None and m.get("route"):
                out.setdefault(m["route"], []).append((v, m["lat"], m["lon"]))
        self._route_markers = out
        return out

    def _mileage(self, route: str, lat: float, lon: float) -> tuple[float, float] | None:
        """(mileage, metres from the nearest marker) on route, or None without markers."""
        best = None
        for v, mlat, mlon in self._routes().get(route, ()):
            d = km(lat, lon, mlat, mlon) * 1000
            if best is None or d < best[1]:
                best = (v, d)
        return best

    def locate_on_road(self, lat: float, lon: float, label: str = "", address: str = "") -> dict | None:
        """{road, dir, mile} for an event on a highway we have markers for: the road and direction named
        in its location ("I-95 SB exit 21", "I-287 eastbound at exit 10") when the point is within
        MARKER_M of that road's markers, else the road whose markers are within ON_ROAD_M. None otherwise."""
        if lat is None or lon is None or not self._routes():
            return None
        named = highway_from_text(label) or highway_from_text(address)
        if named and named[0] in self._routes():
            at = self._mileage(named[0], lat, lon)
            if at and at[1] <= MARKER_M:
                return {"road": named[0], "dir": named[1], "mile": at[0]}
        best = None
        for route in self._routes():
            at = self._mileage(route, lat, lon)
            if at and at[1] <= ON_ROAD_M and (best is None or at[1] < best[1][1]):
                best = (route, at)
        if best is None:
            return None
        d = named[1] if named and named[0] == best[0] else ""
        return {"road": best[0], "dir": d, "mile": best[1][0]}

    def cameras_for(self, lat: float, lon: float, label: str = "", address: str = "") -> dict:
        """The incident viewer's cameras (#95). On a highway: the camera just before and just after the
        event along the road (by our mile markers), in its direction of travel when the location says one
        (rel "before" / "after", with the miles; a camera on that carriageway wins over one up to FACING_MI
        nearer: "facing"), else the nearest on each side labelled by compass (rel "side"); within ALONG_MI.
        Then (and off a highway, only) the cameras within NEARBY_KM as the crow flies (rel "nearby")."""
        where = self.locate_on_road(lat, lon, label, address)
        out, taken, down = [], set(), []
        if where:
            route, d, mile = where["road"], where["dir"], where["mile"]
            pos, neg = ROUTES.get(route, ("NB", "SB", None))[:2]
            compass = {"NB": ("south", "north"), "EB": ("west", "east")}[pos]       # the lower side, the higher side
            cands = []
            for c in self.merged("cameras"):
                if c["off"]:
                    continue
                on = highway_from_text(c.get("road", ""))
                if on and on[0] != route:                                     # another road's camera at the interchange
                    continue
                at = self._mileage(route, c["lat"], c["lon"])
                if not at or at[1] > MARKER_M:
                    continue
                if not self._offerable(c):                                     # demoted: the folded "N cameras down" row
                    if abs(at[0] - mile) <= ALONG_MI:
                        down.append(self._cam_public(c) | {"rel": "down", "miles": round(abs(at[0] - mile), 1),
                                                           "side": "", "facing": False, "km": round(km(lat, lon, c["lat"], c["lon"]), 2)})
                    continue
                cands.append((at[0] - mile, c))
            def side(lower: bool) -> list:
                return sorted(((abs(dm), dm, c) for dm, c in cands if (dm < 0) == lower and abs(dm) <= ALONG_MI), key=lambda x: x[0])
            def pick(cs: list):
                if not cs:
                    return None
                best = cs[0]
                if d:
                    facing = next((x for x in cs if cam_dir(c_dir(x[2])) == d), None)
                    if facing and facing[0] <= best[0] + FACING_MI:
                        best = facing
                return best
            sides = [(True, "side"), (False, "side")]
            if d:                                                              # travelling with the mileage, or against it
                sides = [(True, "before"), (False, "after")] if d == pos else [(False, "before"), (True, "after")]
            def add(rel: str, dist: float, c: dict, lower: bool) -> None:
                out.append(self._cam_public(c) | {
                    "rel": rel, "miles": round(dist, 1), "side": compass[0] if lower else compass[1],
                    "facing": bool(d) and cam_dir(c_dir(c)) == d, "km": round(km(lat, lon, c["lat"], c["lon"]), 2)})
                taken.add(c["id"])
            for lower, rel in sides:
                cs = [x for x in side(lower) if x[2]["id"] not in taken]
                while cs and cs[0][0] < AT_MI:                                 # right at the spot: those, then the next along
                    add("at", cs[0][0], cs[0][2], lower)
                    cs = cs[1:]
                p = pick(cs)
                if p is not None:
                    add(rel, p[0], p[2], lower)
            out.sort(key=lambda c: {"at": 0, "before": 1, "after": 2, "side": 3}[c["rel"]])
        for c in self.nearest(lat, lon, n=3, within_km=NEARBY_KM):
            if c["id"] not in taken:
                out.append(c | {"rel": "nearby", "miles": None, "side": "", "facing": False})
        seen = {c["id"] for c in down}
        for c in self.merged("cameras"):                                       # down cameras close by, too
            if c["id"] in seen or c["off"] or self._offerable(c):
                continue
            k = km(lat, lon, c["lat"], c["lon"])
            if k <= NEARBY_KM:
                down.append(self._cam_public(c) | {"rel": "down", "miles": None, "side": "", "facing": False, "km": round(k, 2)})
        down.sort(key=lambda c: c["km"])
        return {"configured": self.configured, "highway": where, "cameras": out, "down": down}

    # -- the camera wall (#125): every camera in our area ------------------------------------------------------

    def wall_config(self) -> dict:
        """The Settings edits ("camera_wall") as the area uses them: {margin_m, include, exclude}."""
        try:
            raw = self.wall_settings() or {}
        except Exception as e:  # a settings store that can't be read: the defaults
            self.log(f"roads: camera wall settings not read ({type(e).__name__}); the defaults")
            raw = {}
        m = raw.get("margin_m")
        margin = float(m) if isinstance(m, (int, float)) and not isinstance(m, bool) and 0 <= m <= AREA_MARGIN_MAX_M else AREA_MARGIN_M
        ids = lambda k: [str(x) for x in (raw.get(k) or []) if isinstance(x, str) and CAMERA_ID.fullmatch(x)]
        return {"margin_m": margin, "include": ids("include"), "exclude": ids("exclude")}

    def area_shapes(self) -> dict:
        """Our area: {basis: fire_districts | municipalities | bbox, names, note, shapes: [geometry]}. The fire
        districts marked ours (gis.py _ours); before that layer is loaded, our municipal boundaries; before
        those, AREA_BBOX. The note says which when it isn't the districts."""
        from sizeup.gis import _dept_key
        g = self.gis
        feats = g.features("fire_districts") if g is not None else []
        ours = [f for f in feats if (f.get("properties") or {}).get("_ours") and f.get("geometry")]
        if ours:
            names = sorted({str(f["properties"].get("FIRENAME") or "") for f in ours} - {""})
            return {"basis": "fire_districts", "names": names, "note": "", "shapes": [f["geometry"] for f in ours]}
        keys = tuple(k for k in (getattr(g, "ours", ()) or tuple(_dept_key(d) for d in self.ours)) if k)
        munis = g.features("municipalities") if g is not None else []
        mine = [f for f in munis if f.get("geometry") and keys
                and any(_dept_key((f.get("properties") or {}).get("NAME", "")).startswith(k) for k in keys)]
        if mine:
            names = sorted({str(f["properties"].get("NAME") or "") for f in mine} - {""})
            return {"basis": "municipalities", "names": names, "shapes": [f["geometry"] for f in mine],
                    "note": "The fire-district layer isn't loaded yet: our municipal boundaries instead"}
        return {"basis": "bbox", "names": ["Port Chester", "Rye Brook"], "shapes": [bbox_polygon(AREA_BBOX)],
                "note": "Neither the fire districts nor the municipal boundaries are loaded yet: a box around Port Chester and Rye Brook"}

    def _road_order(self, c: dict) -> tuple:
        """Road, then mileage (our mile markers, as the before / after picker reads them; else the camera's own
        milepost), then name: the highways first, in HIGHWAY_WORDS's order."""
        on = highway_from_text(c.get("road", ""))
        mile = None
        if on:
            at = self._mileage(on[0], c["lat"], c["lon"])
            if at and at[1] <= MARKER_M:
                mile = at[0]
        if mile is None:
            mile = _f(c.get("mile"))
        rank = next((i for i, (_, r) in enumerate(HIGHWAY_WORDS) if on and r == on[0]), len(HIGHWAY_WORDS))
        road = on[0] if on else (c.get("road") or "").lower() or "~"
        return (rank, road, mile if mile is not None else math.inf, c.get("name", ""))

    def area_rows(self) -> tuple[dict, list[dict]]:
        """(the area, every camera with where it falls): {camera, dist_m, auto: inside | margin | outside, mode:
        auto | include | exclude, in: on the wall}. For Settings and for area()."""
        cfg, shape = self.wall_config(), self.area_shapes()
        inc, exc = set(cfg["include"]), set(cfg["exclude"])
        rows = []
        for c in self.merged("cameras"):
            d = min((polygon_m(g, c["lat"], c["lon"]) for g in shape["shapes"]), default=math.inf)
            auto = "inside" if d == 0 else "margin" if d <= cfg["margin_m"] else "outside"
            mode = "exclude" if c["id"] in exc else "include" if c["id"] in inc else "auto"
            rows.append({"camera": c, "dist_m": None if math.isinf(d) else round(d), "auto": auto, "mode": mode,
                         "in": mode == "include" or (mode == "auto" and auto != "outside")})
        area = {k: shape[k] for k in ("basis", "names", "note")} | cfg
        return area, rows

    def settings_rows(self) -> dict:
        """Settings > System's camera wall: the area, its settings, and every camera we have with where it falls."""
        area, rows = self.area_rows()
        rows.sort(key=lambda r: (0, self._road_order(r["camera"])) if r["in"]           # the wall's first, in its order
                  else (1, (r["dist_m"] if r["dist_m"] is not None else math.inf, r["camera"].get("name", ""))))   # then the nearest
        return {"area": area, "default_margin_m": AREA_MARGIN_M, "max_margin_m": AREA_MARGIN_MAX_M,
                "cameras": [{k: r["camera"].get(k, "") for k in ("id", "name", "road", "dir", "source", "has_video")}
                            | {"auto": r["auto"], "dist_m": r["dist_m"], "mode": r["mode"], "in": r["in"]} for r in rows],
                "n": sum(1 for r in rows if r["in"]),
                "streams": {"max": self.stream_max, "per_viewer": self.wall_streams,
                            "streaming": sum(1 for r in rows if r["in"] and r["camera"].get("has_video") and not r["camera"]["off"])}}

    def area(self, lat: float | None = None, lon: float | None = None, label: str = "", address: str = "") -> dict:
        """GET /api/cad/cameras?area=ours[&for=lat,lon&label=&address=]: every camera in our area (down ones too,
        flagged), sorted by road then mileage; with an event's location, its cameras just before / at / just
        after along the road (cameras_for) pinned first ("pin": the rel), even from outside the area. And the
        live-video budget: {max (hub-wide), per_viewer (a page's live tiles at once), open, streaming (the area's)}."""
        area, rows = self.area_rows()
        pins: dict[str, dict] = {}
        where = None
        if lat is not None and lon is not None:
            near = self.cameras_for(lat, lon, label, address)
            where = near["highway"]
            if where:
                for c in near["cameras"]:
                    if c["rel"] in AREA_PIN and c["id"] not in pins:
                        pins[c["id"]] = {"pin": c["rel"], "miles": c["miles"]}
        out = []
        for r in rows:
            c = r["camera"]
            if not (r["in"] or c["id"] in pins):
                continue
            out.append(self._cam_public(c) | {"area": r["auto"] if r["mode"] == "auto" else r["mode"], "dist_m": r["dist_m"],
                                              "pin": None, "miles": None} | pins.get(c["id"], {}))
        orders = {r["camera"]["id"]: self._road_order(r["camera"]) for r in rows}
        out.sort(key=lambda c: (AREA_PIN.get(c["pin"], 9), orders[c["id"]]))
        streaming = sum(1 for c in out if c["has_video"] and not c["off"])
        return {"configured": self.configured, "area": area, "highway": where, "cameras": out,
                "streams": {"max": self.stream_max, "per_viewer": min(self.stream_max, self.wall_streams or streaming),
                            "open": self.streams_open(), "streaming": streaming}}

    # -- the snapshot proxy ----------------------------------------------------------------------------

    @staticmethod
    def image_ttl(cam: dict) -> float:
        """How long a snapshot is served before the camera is asked again: half its cadence (the county's
        8 s pictures every 4 s), never over IMAGE_TTL_S."""
        return min(IMAGE_TTL_S, max(2.0, float(cam.get("cadence") or 60) / 2))

    def image(self, cid: str, since: float | None = None) -> tuple[int, bytes, str, dict]:
        """(code, body, content type, headers) for GET /api/cad/cameras/<id>/image. since: the time of the
        picture the caller has (If-None-Match / If-Modified-Since / ?since=): 304 when the hub's is no newer."""
        if not self.configured:
            return 404, b'{"error":"no camera source is configured"}', JSON, {}
        cam = self.camera(cid)
        if cam is None:
            return 404, b'{"error":"no such camera"}', JSON, {}
        if not cam["url"]:
            return 404, b'{"error":"this camera has no snapshot"}', JSON, {}
        lock = self._image_locks.setdefault(cid, threading.Lock())
        with lock:                                   # many viewers, one fetch
            hit = self._images.get(cid)
            now = self.clock()
            if hit and now - hit["fetched"] < self.image_ttl(cam):
                self._images.move_to_end(cid)
                return self._image_out(hit, False, since, cam)
            try:
                with self._parallel:
                    code, headers, body = self.fetch(cam["url"], IMAGE_TIMEOUT_S, IMAGE_MAX_BYTES)
                ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
                if code != 200 or not ctype.startswith("image/") or not body:
                    raise ValueError(f"HTTP {code} {ctype or 'no type'}")
                taken = now
                if headers.get("last-modified"):
                    try:
                        taken = min(now, parsedate_to_datetime(headers["last-modified"]).timestamp())
                    except (TypeError, ValueError, IndexError):
                        pass
                if now - taken > self.stale_s(cam):     # an old picture (a dead camera's placeholder): no image
                    self._note_image(cid, False, taken, now, self.stale_s(cam))
                    self._images.pop(cid, None)
                    return self._down_answer(cid, cam)
                self._note_image(cid, True, taken, now)
                hit = {"body": body, "ctype": ctype, "taken": taken, "fetched": now}
                self._images[cid] = hit
                self._images.move_to_end(cid)
                while len(self._images) > IMAGE_KEEP:
                    self._images.popitem(last=False)
                return self._image_out(hit, False, since, cam)
            except (HTTPError, URLError, OSError, ValueError) as e:
                why = redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", self.ny.secrets())
                self._note_image(cid, False, None, now)
                if self._health[cid]["down_since"] is not None:      # no current still: say so, never the old picture
                    self._images.pop(cid, None)
                    return self._down_answer(cid, cam)
                if hit and now - hit["fetched"] < IMAGE_STALE_S:
                    return self._image_out(hit, True, since, cam)
                return 502, json.dumps({"error": f"the camera didn't answer ({why})"}).encode(), JSON, {}

    def _image_out(self, hit: dict, stale: bool, since: float | None = None, cam: dict | None = None) -> tuple[int, bytes, str, dict]:
        taken = int(hit["taken"])
        h = {"Cache-Control": f"private, max-age={int(self.image_ttl(cam) if cam else IMAGE_TTL_S)}",
             "X-Image-Time": str(taken), "ETag": f'"{taken}"', "Last-Modified": formatdate(taken, usegmt=True),
             "X-Image-Cadence": str(int((cam or {}).get("cadence") or 60))}
        if stale:
            h["X-Image-Stale"] = "1"
        if since is not None and taken <= int(since):      # the caller has this picture already
            return 304, b"", hit["ctype"], h
        return 200, hit["body"], hit["ctype"], h

    # -- live video (#101): the camera's HLS stream through the hub, only while a viewer is open --------------

    def streams_open(self, now: float | None = None) -> int:
        now = self.clock() if now is None else now
        return sum(1 for st in self._streams.values() if now - st["last"] <= STREAM_IDLE_S)

    def stream(self, cid: str, name: str, viewer: str = "") -> tuple[int, bytes, str, dict]:
        """GET /api/cad/cameras/<id>/stream/<name>: playlist.m3u8 (the camera's master playlist, its chunklist
        named as the hub serves it), <chunklist>.m3u8 (the segment names likewise), <segment>.ts (the bytes).
        A playlist request opens or keeps the camera's stream (stream_max at once, hub-wide; the next is 429);
        segments are served only while one was asked for within STREAM_IDLE_S: a closed viewer stops the
        flow by itself. Nothing is fetched that a playlist didn't name, and only from the stream's own folder.
        viewer (?v=, a page's token, #125): the master playlist names its chunklist with it, so each page's use
        is known; close (GET .../stream/close?v=) ends that page's use at once, and the stream (its pump too)
        when nobody else is watching, instead of after STREAM_IDLE_S."""
        cam = self.camera(cid)
        if cam is None or not self.configured:
            return 404, b'{"error":"no such camera"}', JSON, {}
        if not cam.get("video"):
            return 404, b'{"error":"this camera has no video stream"}', JSON, {}
        # (#276) the still's health never refuses the video (the 511NY cameras' stills are often minutes old)
        now = self.clock()
        base = cam["video"].rsplit("/", 1)[0] + "/"
        hdr = {"Cache-Control": "no-store"}
        if name == "status":
            return 200, json.dumps(self.stream_status(cid)).encode(), JSON, hdr
        if name == "close":
            return 200, json.dumps({"open": self.close_stream(cid, viewer)}).encode(), JSON, hdr
        failed_at = self._stream_down.get(cid)
        if failed_at is not None and now - failed_at < STREAM_RETRY_S:        # its source refused lately: don't wait again
            return 503, json.dumps({"error": "no live video from the source right now", "source_down": True,
                                    "retry_s": int(STREAM_RETRY_S - (now - failed_at))}).encode(), JSON, hdr
        opened = False
        with self._stream_lock:
            st = self._streams.get(cid)
            if name.endswith(".m3u8"):
                if st is None or now - st["last"] > STREAM_IDLE_S:
                    if self.streams_open(now) >= self.stream_max:
                        return 429, json.dumps({"error": f"too many live streams open ({self.stream_max}); snapshots instead"}).encode(), JSON, hdr
                    st = self._streams[cid] = {"last": now, "base": base, "chunk": "", "list": None, "list_at": 0.0,
                                               "target": 6.0, "frames": 0, "seconds": 0.0, "fps": None, "ahead": 0, "viewers": {}}
                    opened = True
                st["last"] = now
                st.setdefault("viewers", {})[viewer] = now
            elif st is None or now - st["last"] > STREAM_IDLE_S:
                return 404, b'{"error":"the stream is not open: ask for its playlist first"}', JSON, hdr
        src = cam["video"] if name == "playlist.m3u8" else base + name
        if name == "playlist.m3u8" and not opened and st.get("master"):
            # Another page already watches it: the same origin session (its chunklist name), so every page's
            # chunklist is the pump's one copy and the origin sees one viewer (#125: many tiles, many pages).
            return 200, self._for_viewer(st["master"], viewer), "application/vnd.apple.mpegurl", hdr
        if name.endswith(".ts"):
            # (#276) checked whole and repaired before anyone gets it (hlsseg): never a partial segment
            try:
                hit = self._segment(cid, st["base"], name)
            except hlsseg.Bad as e:
                return 502, json.dumps({"error": f"the source sent a broken segment ({e})"}).encode(), JSON, hdr
            except (HTTPError, URLError, OSError, ValueError) as e:
                why = redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", self.ny.secrets())
                return 502, json.dumps({"error": f"the stream didn't answer ({why})"}).encode(), JSON, hdr
            if self._stream_down.pop(cid, None) is not None:
                self._changed()
            return 200, hit["body"], hit["ctype"], {**hdr, "Cache-Control": "private, max-age=60"}
        if name != "playlist.m3u8" and st.get("list") and name == st.get("chunk") and now - st["list_at"] < STREAM_LIST_FRESH_S:
            return 200, self._for_player(cid, st["list"], self._first_list(st, viewer)), "application/vnd.apple.mpegurl", hdr
        try:
            with self._parallel:
                code, headers, body = self.fetch(src, STREAM_OPEN_TIMEOUT_S if name == "playlist.m3u8" else STREAM_TIMEOUT_S,
                                                 STREAM_MAX_BYTES)
            if code != 200 or not body:
                raise ValueError(f"HTTP {code}")
        except (HTTPError, URLError, OSError, ValueError) as e:
            why = redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", self.ny.secrets())
            if opened:                                           # a stream that never answered holds no slot
                with self._stream_lock:
                    self._streams.pop(cid, None)
                    self._stream_down[cid] = now                 # ... and isn't asked again for a while
                self._changed()                                  # its state on the map: "No live video"
                self.log(f"roads: {cid}: no live video from the source ({why}); snapshots for {STREAM_RETRY_S:.0f} s")
                return 503, json.dumps({"error": "no live video from the source right now", "source_down": True,
                                        "retry_s": int(STREAM_RETRY_S)}).encode(), JSON, hdr
            return 502, json.dumps({"error": f"the stream didn't answer ({why})"}).encode(), JSON, hdr
        if self._stream_down.pop(cid, None) is not None:
            self._changed()
        # a playlist: every line that names a file must be a plain name in the stream's own folder
        clean = self._clean_playlist(body)
        if clean is None:
            return 502, b'{"error":"the stream names files the hub will not fetch"}', JSON, hdr
        with self._stream_lock:
            st = self._streams.get(cid)
            if st is not None:
                if name == "playlist.m3u8":
                    st["chunk"] = next((t for t in clean.decode().splitlines() if t and not t.startswith("#")), "")
                    st["master"] = clean
                elif name == st.get("chunk"):
                    self._note_list(st, clean, now)
        if opened and st is not None:
            self._start_pump(cid)
        if name == "playlist.m3u8":
            clean = self._for_viewer(clean, viewer)
        elif st is not None and name == st.get("chunk"):
            clean = self._for_player(cid, clean, self._first_list(st, viewer))
        return 200, clean, "application/vnd.apple.mpegurl", hdr

    @staticmethod
    def _for_viewer(master: bytes, viewer: str) -> bytes:
        """The master playlist with its chunklist asked for with the page's token too (?v=)."""
        if not viewer:
            return master
        return re.sub(rb"(?m)^([A-Za-z0-9_-]{1,80}\.m3u8)$", lambda m: m.group(1) + b"?v=" + viewer.encode(), master)

    def close_stream(self, cid: str, viewer: str) -> bool:
        """A page stopped watching (its tile scrolled away, the wall or the page hidden, the viewer closed): its use
        ends now; nobody else watching within STREAM_IDLE_S, the stream closes (its slot free, its pump stops).
        True while the stream stays open for someone else."""
        now = self.clock()
        with self._stream_lock:
            st = self._streams.get(cid)
            if st is None:
                return False
            views = st.setdefault("viewers", {})
            views.pop(viewer, None)
            for v in [v for v, t in views.items() if now - t > STREAM_IDLE_S]:
                views.pop(v)
            if views:
                st["last"] = max(views.values())
                return True
            self._streams.pop(cid, None)
            return False

    @staticmethod
    def _clean_playlist(body: bytes) -> bytes | None:
        lines = []
        for ln in body.decode("utf-8", errors="replace").splitlines():
            t = ln.strip()
            if not t or t.startswith("#"):
                lines.append(t)
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.(?:m3u8|ts)", t):
                return None
            lines.append(t)
        return ("\n".join(lines) + "\n").encode()

    @staticmethod
    def _note_list(st: dict, clean: bytes, now: float) -> None:
        st["list"], st["list_at"] = clean, now
        m = re.search(rb"#EXT-X-TARGETDURATION:(\d+(?:\.\d+)?)", clean)
        if m:
            st["target"] = max(1.0, float(m.group(1)))

    def _segment(self, cid: str, base: str, name: str) -> dict:
        """A segment from the cache, else from the origin: one fetch at a time per segment (the player and the pump
        asking at once wait for the one), checked (hlsseg.check) and repaired (frames whose reference chain is broken
        at the source left out). A body that isn't MPEG-TS with video, or came short of its Content-Length, is fetched
        once more; failing again, hlsseg.Bad / ValueError (never served, never cached). Fetch errors are raised."""
        key = f"{cid}/{name}"
        with self._stream_lock:                       # [lock, users]: dropped once nobody holds or waits for it
            ent = self._segment_locks.setdefault(key, [threading.Lock(), 0])
            ent[1] += 1
        try:
            with ent[0]:
                hit = self._segments.get(key)
                if hit and self.clock() - hit["fetched"] < STREAM_SEG_TTL_S:
                    return hit
                for attempt in (1, 2):
                    try:
                        with self._parallel:
                            code, headers, body = self.fetch(base + name, STREAM_TIMEOUT_S, STREAM_MAX_BYTES)
                        if code != 200 or not body:
                            raise ValueError(f"HTTP {code}")
                        return self._keep_segment(cid, name, headers, body, self.clock())
                    except hlsseg.Bad as e:
                        self._note_bad(cid, name, str(e))
                        if attempt == 2:
                            raise
                    except ValueError as e:               # cut short (http_get), an HTTP error: once more
                        if attempt == 2:
                            self._note_bad(cid, name, str(e))
                            raise
        finally:
            with self._stream_lock:
                ent[1] -= 1
                if ent[1] <= 0 and self._segment_locks.get(key) is ent:
                    self._segment_locks.pop(key, None)
        raise AssertionError("unreachable")  # pragma: no cover

    def _note_bad(self, cid: str, name: str, why: str) -> None:
        with self._stream_lock:
            st = self._streams.get(cid)
            if st is not None:
                st.setdefault("bad", {})[name] = why
                while len(st["bad"]) > 50:
                    st["bad"].pop(next(iter(st["bad"])))
        self.log(f"roads: stream {cid}: {name} refused ({why})")

    def _keep_segment(self, cid: str, name: str, headers: dict, body: bytes, now: float) -> dict:
        """A fetched segment, checked (hlsseg.Bad when it isn't MPEG-TS with video) and repaired, into the cache; its
        frames into the stream's frame-rate estimate and its damage into the stream's record (stream_status). The
        check carries on from the segment before when this one follows it (its last reference frame, a chain broken
        across the cut). A segment with no picture left after the repair is kept as it came (noplay: a GAP in the
        players' lists; served as it came if asked for)."""
        ctype = (headers.get("content-type") or "video/MP2T").split(";")[0].strip() or "video/MP2T"
        st = self._streams.get(cid)
        m = re.search(r"^(.*?)(\d{1,12})\.ts$", name)
        session, seq = (m.group(1), int(m.group(2))) if m else ("", None)
        prev = (st or {}).get("hstate")
        # carried only from this session's segment just before (not another session's, a gap, or one out of order);
        # else judged by itself (its leading frames before an IDR are the only ones that can't be)
        follows = prev is not None and seq is not None and prev[0] == session and prev[1] == seq - 1
        state = prev[2] if follows else {"sps": (st or {}).get("sps")}
        info = hlsseg.check(body, state)
        try:
            fixed, noplay = hlsseg.repair(body, info), False
        except hlsseg.Bad:
            fixed, noplay = body, True
        hit = {"body": fixed, "ctype": ctype, "fetched": now, "frames": info["frames"], "dropped": len(info["broken"]),
               "lost": info["lost"], "kept": 0 if noplay else info["playable"], "noplay": noplay, "size": info["size"]}
        with self._stream_lock:
            self._segments[f"{cid}/{name}"] = hit
            self._segments.move_to_end(f"{cid}/{name}")
            while len(self._segments) > STREAM_SEG_KEEP:
                self._segments.popitem(last=False)
            if st is not None:
                if info["sps"]:
                    st["sps"] = info["sps"]
                if seq is not None and (not st.get("hstate") or st["hstate"][0] != session or seq > st["hstate"][1]):
                    st["hstate"] = (session, seq, info["state"])
                if name not in st.setdefault("seen", {}):
                    st["seen"][name] = hit["dropped"]
                    while len(st["seen"]) > STREAM_DAMAGE_WINDOW:
                        st["seen"].pop(next(iter(st["seen"])))
                if st.get("list"):
                    dur = _segment_duration(st["list"], name)
                    if dur:
                        st["frames"] += info["frames"]
                        st["seconds"] += dur
                        if st["seconds"] >= 4:
                            st["fps"] = round(st["frames"] / st["seconds"], 1)
        if hit["dropped"]:
            self.log(f"roads: stream {cid}: {name}: {info['lost']} reference frames lost at the source; "
                     f"{hit['dropped']} of {info['frames']} frames left out" + ("; nothing to show: a gap" if noplay else ""))
        return hit

    def _first_list(self, st: dict, viewer: str) -> bool:
        """Is this the first chunklist this page's player gets (where its playback starts)?"""
        with self._stream_lock:
            started = st.setdefault("started", set())
            first = bool(viewer) and viewer not in started
            started.add(viewer)
            if len(started) > 200:
                started.clear()
        return first

    def _for_player(self, cid: str, chunklist: bytes, start: bool = False) -> bytes:
        """The chunklist as a player gets it (#276). Every list: a segment the hub knows has nothing to play (refused
        twice, or no picture left after the repair) is marked EXT-X-GAP (the player skips it; its place and the
        media sequence stay). start (a page's first chunklist: where its playback begins): the head trimmed of those
        and of the ones that came damaged, so the first picture is a whole live one; only the head, so the media
        sequence only ever moves on, and the discontinuity sequence counts any EXT-X-DISCONTINUITY taken off."""
        st = self._streams.get(cid) or {}
        bad = st.get("bad") or {}
        lines = chunklist.decode(errors="replace").splitlines()
        segs, tags = [], []
        head = []
        for t in lines:
            if not t or t.startswith("#"):
                per_segment = t == "#EXT-X-DISCONTINUITY" or t.startswith(("#EXTINF", "#EXT-X-PROGRAM-DATE-TIME",
                                                                                     "#EXT-X-BYTERANGE", "#EXT-X-GAP"))
                if not segs and not tags and not per_segment:
                    head.append(t)
                else:
                    tags.append(t)
            else:
                segs.append((tags, t))
                tags = []

        def gap(name: str) -> bool:
            hit = self._segments.get(f"{cid}/{name}")
            return name in bad or (hit is not None and hit.get("noplay", False))
        drop = 0
        if start:
            for _, name in segs[:-1]:                   # never the last: something to play
                hit = self._segments.get(f"{cid}/{name}")
                if gap(name) or (hit is not None and hit["dropped"]):
                    drop += 1
                else:
                    break
        gaps = [name for _, name in segs[drop:] if gap(name)]
        if not drop and not gaps:
            return chunklist
        disc = sum(1 for t, _ in segs[:drop] for x in t if x == "#EXT-X-DISCONTINUITY")
        out = []
        for t in head:
            m = re.fullmatch(r"#EXT-X-(MEDIA|DISCONTINUITY)-SEQUENCE:(\d+)", t)
            if m:
                t = f"#EXT-X-{m.group(1)}-SEQUENCE:{int(m.group(2)) + (drop if m.group(1) == 'MEDIA' else disc)}"
            out.append(t)
        if disc and not any(t.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:") for t in head):
            out.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc}")
        for t, name in segs[drop:]:
            out += t + (["#EXT-X-GAP"] if name in gaps and "#EXT-X-GAP" not in t else []) + [name]
        out += tags
        return ("\n".join(x for x in out if x) + "\n").encode()

    def stream_status(self, cid: str) -> dict:
        """{open, fps, ahead, target_s, source_down, damaged, checked}: for the viewer's notes (a low frame rate at the
        source; damaged: of the last `checked` segments, how many arrived with frames lost at the source, #276)."""
        now = self.clock()
        st = self._streams.get(cid)
        is_open = st is not None and now - st["last"] <= STREAM_IDLE_S
        failed_at = self._stream_down.get(cid)
        seen = (st or {}).get("seen") or {}
        return {"open": is_open, "fps": st["fps"] if is_open else None, "ahead": st["ahead"] if is_open else 0,
                "target_s": st["target"] if is_open else None,
                "source_down": failed_at is not None and now - failed_at < STREAM_RETRY_S,
                "damaged": sum(1 for v in seen.values() if v) if is_open else 0, "checked": len(seen) if is_open else 0}

    # The pump: while a stream is open, keep its chunklist fresh and the newest segments fetched ahead of
    # the player, so the player never waits on the origin per segment (the origin's latency per 6 s
    # segment, two hops away, is what drains a buffer).
    pump_threads = True                     # tests: False, and they call pump_once themselves

    def _start_pump(self, cid: str) -> None:
        if not self.pump_threads:
            return
        t = self._pumps.get(cid)
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self._pump, args=(cid,), name=f"stream-{cid}", daemon=True)
        self._pumps[cid] = t
        t.start()

    def _pump(self, cid: str) -> None:
        while not self._stop.is_set():
            st = self._streams.get(cid)
            if st is None or self.clock() - st["last"] > STREAM_IDLE_S:
                return
            try:
                self.pump_once(cid)
            except Exception as e:  # pragma: no cover - never lets the thread die on a surprise
                self.log(f"roads: stream {cid}: pump error {type(e).__name__}: {e}")
            st = self._streams.get(cid)
            self._stop.wait(max(1.0, (st["target"] if st else 6.0) / 3))

    def pump_once(self, cid: str) -> int:
        """One round: the chunklist (unless fresh), then up to STREAM_AHEAD of its newest segments not yet
        cached, oldest first. The number of segments fetched."""
        st = self._streams.get(cid)
        if st is None or not st.get("chunk"):
            return 0
        now = self.clock()
        if not st.get("list") or now - st["list_at"] >= STREAM_LIST_FRESH_S:
            try:
                code, _, body = self.fetch(st["base"] + st["chunk"], STREAM_TIMEOUT_S, STREAM_MAX_BYTES)
                clean = self._clean_playlist(body) if code == 200 and body else None
            except (HTTPError, URLError, OSError, ValueError):
                clean = None
            if clean is not None:
                with self._stream_lock:
                    self._note_list(st, clean, now)
        if not st.get("list"):
            return 0
        names = [t for t in st["list"].decode().splitlines() if t and not t.startswith("#") and t.endswith(".ts")]
        wanted = names[-STREAM_AHEAD:]
        n = 0
        for name in wanted:
            key = f"{cid}/{name}"
            hit = self._segments.get(key)
            if hit and now - hit["fetched"] < STREAM_SEG_TTL_S or name in (st.get("bad") or {}):
                continue
            try:
                self._segment(cid, st["base"], name)
            except (HTTPError, URLError, OSError, ValueError):     # (hlsseg.Bad is a ValueError)
                continue
            n += 1
        st["ahead"] = sum(1 for name in wanted if f"{cid}/{name}" in self._segments)
        return n

    # -- HTTP ------------------------------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        return (path == "/api/cad/roads" or path == "/api/cad/cameras" or bool(CAMERA_PATH.fullmatch(path))
                or bool(STREAM_PATH.fullmatch(path)))

    def get(self, path: str, query: str, headers=None) -> tuple[int, bytes, str, dict]:
        q = parse_qs(query or "")
        one = lambda k: (q.get(k) or [""])[0].strip()
        if path == "/api/cad/roads":
            if one("rev") and one("rev") == str(self.rev):
                return 200, json.dumps({"rev": self.rev, "same": True}).encode(), JSON, {}
            return 200, self._public_bytes(), JSON, {}
        if path == "/api/cad/cameras":
            if one("area"):                                  # the camera wall (#125): every camera in our area
                if one("area") != "ours":
                    return 400, b'{"error":"area: ours"}', JSON, {}
                lat = lon = None
                if one("for"):
                    try:
                        lat, lon = (float(x) for x in one("for").split(","))
                    except ValueError:
                        return 400, b'{"error":"for: lat,lon"}', JSON, {}
                return 200, json.dumps(self.area(lat, lon, one("label")[:200], one("address")[:200])).encode(), JSON, \
                    {"Cache-Control": "no-store"}
            if one("for"):                                   # the incident viewer's cameras (#95)
                try:
                    lat, lon = (float(x) for x in one("for").split(","))
                except ValueError:
                    return 400, b'{"error":"for: lat,lon"}', JSON, {}
                return 200, json.dumps(self.cameras_for(lat, lon, one("label")[:200], one("address")[:200])).encode(), JSON, {}
            near = None
            if one("near"):
                try:
                    lat, lon = (float(x) for x in one("near").split(","))
                    near = (lat, lon)
                except ValueError:
                    return 400, b'{"error":"near: lat,lon"}', JSON, {}
            try:
                n = max(1, min(int(one("n") or 3), 20))
            except ValueError:
                return 400, b'{"error":"n: a number"}', JSON, {}
            if one("q"):
                cams = self.search_places(one("q")[:120], near, n)
            elif near:
                cams = self.nearest(near[0], near[1], n)
            else:
                return 400, b'{"error":"q or near"}', JSON, {}
            return 200, json.dumps({"configured": self.configured, "cameras": cams}).encode(), JSON, {}
        if m := CAMERA_PATH.fullmatch(path):
            return self.image(m.group(1), _since(headers, one("since")))
        if m := STREAM_PATH.fullmatch(path):
            v = one("v")
            return self.stream(m.group(1), m.group(2), v if VIEWER_RE.fullmatch(v) else "")
        return 404, b"not found\n", "text/plain", {}
