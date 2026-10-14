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
  511NY     NYSDOT's 511NY data feeds (https://511ny.org/help/24, Developer Access Agreement updated
            2026-09-18): events (JSON), the camera inventory (XML), message signs (JSON). Access is by
            request; the approval brings the feed addresses and a key, so the addresses are config
            ([roads] events_url, cameras_url, signs_url, "{key}" where the key goes) and the key a
            secret (env SIZEUP_511NY_KEY; the hub: SSM /sizeup-hub/511ny-key). Records are read by
            field name under any of the names 511NY has used (FIELDS). Terms: no fee; we may
            "redistribute, enhance, repackage" but not alter a report; we "must display the time at
            which the data was published or last updated" (every item and snapshot shows its time);
            acknowledged as "powered by 511NY" with a link. Off until approved: the Layers box says so.

Both at once: 511NY, when it is configured, wins per item: a Thruway camera, sign or event within
DEDUPE_M (50 m) of a 511NY one (same layer, for events) is left out (_merged). Every item carries its
"source" ("511NY" | "Thruway"), shown on the map. The feeds are statewide, so only our area is kept
(bbox: Port Chester, Rye Brook and the mutual-aid neighbours with a margin). Each provider has its
own schedule and backoff (a refused key waits 30 min, a 429 5 min, anything else 1 min doubling to
15, never under the feed's floor), and one disk cache holds what each kept (state dir, roads511.json:
items only, never the key or a feed address), so a restart spends no calls.

The 511NY key is only ever in the request this module makes to 511NY: never in a response, the state,
the cache, an error or the log (_redact).

Live video (#101): the Thruway's cameras each list an HLS stream (512x288, about 205 kbit/s, 6 s
segments, on nysdot.skyvdn.com, answering a plain client with no key; CORS open). The viewer plays
it, muted, through the hub (Roads.stream: the playlist, its chunklist and the segments, nothing the
playlist didn't name and only from the stream's own folder), only while that viewer is open: a
closed viewer stops asking for the playlist and the hub stops serving the segments after
STREAM_IDLE_S; STREAM_MAX streams at once hub-wide, a 5th viewer gets snapshots. The terms are the
same as for the pictures (the Thruway's disclaimer: no resale or redistribution for profit). 511NY's
camera inventory has a VideoUrl too (kept as "video"); the county's cameras are JPEG only. The
viewer always says which it shows: "Live", or "Snapshot, updated every N s" with the source's cadence.

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

from sizeup.geo.places import ROUTES          # each highway's positive direction (NB / EB: the mileage rises that way)

SOURCE = "511NY"
ATTRIBUTION = "powered by 511NY"
TERMS_URL = "https://511ny.org/help/24"
LINK_URL = "https://511ny.org/"
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
DOWN_FAILS = 3
HEALTH_SWEEP_S = 900.0
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
STREAM_PATH = re.compile(r"/api/cad/cameras/([A-Za-z0-9_.:-]{1,64})/stream/(status|[A-Za-z0-9_-]{1,80}\.(?:m3u8|ts))")
STREAM_IDLE_S = 30.0             # no playlist request this long: the stream's segments stop (a viewer polls every few s)
STREAM_MAX = 4                   # live streams open at once, hub-wide (each about 26 KB/s): the 5th is refused
STREAM_TIMEOUT_S = 10.0          # a chunklist or segment
STREAM_OPEN_TIMEOUT_S = 6.0      # the first playlist: a stream that doesn't answer this fast is down at the source
STREAM_RETRY_S = 300.0           # ... and is not asked again for this long (the viewer shows snapshots at once)
STREAM_AHEAD = 3                 # segments the pump keeps fetched ahead of the player, from the latest chunklist
STREAM_LIST_FRESH_S = 2.0        # a chunklist younger than this is served from the pump's copy
STREAM_MAX_BYTES = 4 * 1024 * 1024
STREAM_SEG_TTL_S = 60.0          # a segment kept this long (two viewers of one camera: one fetch)
STREAM_SEG_KEEP = 40
NEAR_KM = 1.5                    # "Cameras nearby" (the map's nearest-camera lookup): this close to the event
# The incident viewer's cameras (#95): along the road, not as the crow flies
ALONG_MI = 2.0                   # the camera just before / just after the event: this far along the road at most
ON_ROAD_M = 150.0                # an event this close to a road's mile markers is on that road
MARKER_M = 300.0                 # a camera (or a labelled event) this close to a marker takes its mileage
FACING_MI = 0.3                  # a camera on the event's side of the road wins over a nearer one this much nearer
AT_MI = 0.05                     # closer than this along the road: "at the location"
NEARBY_KM = 0.3                  # off the highway (or besides the two): cameras this close, as the crow flies
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
                    "lat": round(pt[0], 6), "lon": round(pt[1], 6)})
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


PARSE = {"cameras": parse_cameras, "events": parse_events, "signs": parse_signs}


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


def http_get(url: str, timeout: float, limit: int) -> tuple[int, dict, bytes]:
    """(status, headers, body): urllib, GET only. Errors as HTTPError / URLError / OSError."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json, image/*;q=0.9, */*;q=0.5"})
    with urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"more than {limit // (1024 * 1024)} MB")
        return r.status, {k.lower(): v for k, v in r.headers.items()}, body


class Provider:
    """One source's feeds: their addresses, parsers and schedule, what each last read and how it went.
    511NY (a key, addresses from its approval) and the Thruway (fixed public addresses, no key)."""

    def __init__(self, name: str, short: str, label: str, link: str, urls: dict | None, parse: dict, every: dict | None,
                 defaults: dict, floors: dict, key: str, bbox, fetch, log, clock, changed: Callable[[], None]):
        self.name, self.short, self.label, self.link = name, short, label, link
        self.urls = {f: str((urls or {}).get(f) or "").strip() for f in FEEDS}
        self.parse, self.key, self.bbox = parse, (key or "").strip(), bbox
        self.every = {f: max(floors[f], float((every or {}).get(f, defaults[f]))) for f in FEEDS}
        self.floors = floors
        self.fetch, self.log, self.clock, self.changed = fetch, log, clock, changed
        self.data: dict[str, list[dict]] = {f: [] for f in FEEDS}
        self.status: dict[str, dict] = {f: {"at": None, "n": 0, "error": "", "next": 0.0, "fails": 0} for f in FEEDS}

    def feed_on(self, feed: str) -> bool:
        u = self.urls.get(feed, "")
        return bool(u) and ("{key}" not in u or bool(self.key))

    @property
    def configured(self) -> bool:
        """At least one feed can be read (an address, and the key when it needs one)."""
        return any(self.feed_on(f) for f in FEEDS)

    def why_not(self) -> str:
        if self.configured:
            return ""
        if self.name == "511ny":
            if not any(self.urls.values()):
                return ("511NY not configured: no feed addresses ([roads] events_url, cameras_url, signs_url, "
                        "from 511NY's access approval)")
            return "511NY not configured: no key (SIZEUP_511NY_KEY)"
        return f"{self.short} cameras off"

    def url(self, feed: str) -> str:
        return self.urls[feed].replace("{key}", quote(self.key, safe=""))

    def updated_at(self) -> float | None:
        ats = [s["at"] for s in self.status.values() if s["at"]]
        return max(ats) if ats else None

    def due(self) -> list[str]:
        now = self.clock()
        return [f for f in FEEDS if self.feed_on(f) and now >= self.status[f]["next"]]

    def refresh(self, feed: str) -> bool:
        """Fetch one feed now; True when it was read. Never raises."""
        if not self.feed_on(feed):
            return False
        now = self.clock()
        try:
            code, _, body = self.fetch(self.url(feed), TIMEOUT_S, MAX_FEED_BYTES)
            if code != 200:
                raise HTTPError("", code, f"HTTP {code}", None, None)
            items = self.parse[feed](decode(body), self.bbox)
        except HTTPError as e:
            return self._failed(feed, now, e.code, f"HTTP {e.code}")
        except (URLError, OSError, ValueError) as e:
            return self._failed(feed, now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}")
        except Exception as e:  # a surprise in the feed must not stop the others
            return self._failed(feed, now, 0, f"{type(e).__name__}: {e}")
        for it in items:
            it["source"] = self.short
        self.data[feed] = items
        self.status[feed].update(at=now, n=len(items), error="", fails=0, next=now + self.every[feed])
        self.changed()
        return True

    def _failed(self, feed: str, now: float, code: int, why: str) -> bool:
        st = self.status[feed]
        why = _redact(why, self.key)
        st["fails"] += 1
        if code in (401, 403):
            wait, why = REFUSED_S, (f"{self.short} refused the key ({why})" if self.key else f"{self.short} refused the request ({why})")
        elif code == 429:
            wait, why = RATE_LIMITED_S, f"{self.short} rate limit ({why})"
        else:
            wait = max(self.floors[feed], min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** (st["fails"] - 1)))
        changed = st["error"] != why
        st.update(error=why, next=now + wait)
        if changed:
            self.changed()
        self.log(f"roads: {self.short} {feed}: {why}; next try in {wait:.0f} s")
        return False

    def public(self) -> dict:
        return {"name": self.name, "short": self.short, "label": self.label, "link": self.link, "configured": self.configured,
                "at": self.updated_at(),
                "status": {f: {"on": self.feed_on(f), "at": s["at"], "n": s["n"], "error": s["error"]} for f, s in self.status.items()}}


class Roads:
    def __init__(self, key: str = "", urls: dict | None = None, bbox=DEFAULT_BBOX, cache: str | Path | None = None,
                 every: dict | None = None, gazetteer: str | Path | None = None,
                 fetch: Callable[[str, float, int], tuple[int, dict, bytes]] = http_get,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 thruway: bool = True, thruway_urls: dict | None = None, county: bool = True, county_urls: dict | None = None):
        self.bbox = tuple(float(x) for x in bbox)
        self.cache = Path(cache) if cache else None
        self.fetch, self.log, self.clock = fetch, log, clock
        self._lock = threading.Lock()
        self.rev = 0
        self._public: tuple[int, bytes] | None = None
        self._merge: tuple[int, dict] | None = None
        # 511NY first: when both are on, it wins a tie (the same camera, sign or event from both)
        self.ny = Provider("511ny", SOURCE, SOURCE, LINK_URL, urls, PARSE, every, EVERY_S, MIN_EVERY_S, key,
                           self.bbox, fetch, log, clock, self._changed)
        self.thruway = (Provider("thruway", THRUWAY, THRUWAY_LABEL, THRUWAY_LINK,
                                 THRUWAY_URLS if thruway_urls is None else thruway_urls, THRUWAY_PARSE, None,
                                 THRUWAY_EVERY_S, MIN_EVERY_S, "", self.bbox, fetch, log, clock, self._changed)
                        if thruway else None)
        self.county = (Provider("county", COUNTY, COUNTY_LABEL, COUNTY_LINK, COUNTY_URLS if county_urls is None else county_urls,
                                COUNTY_PARSE, None, COUNTY_EVERY_S, COUNTY_MIN_S, "", self.bbox, fetch, log, clock, self._changed)
                       if county else None)
        self.providers = [p for p in (self.ny, self.thruway, self.county) if p is not None]
        self._health: dict[str, dict] = {}            # camera id -> {fails, down_since, last_image, checked}
        self._streams: dict[str, dict] = {}           # camera id -> {last (playlist request), base (the stream's folder)}
        self._segments: OrderedDict[str, dict] = OrderedDict()   # "cid/name" -> {body, ctype, fetched}
        self._stream_lock = threading.Lock()
        self._stream_down: dict[str, float] = {}      # camera id -> when its source last refused to stream
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
        return cls(cfg.roads_key if on else "",
                   {"cameras": cfg.roads_cameras_url, "events": cfg.roads_events_url, "signs": cfg.roads_signs_url} if on else {},
                   cfg.roads_bbox, (state / "roads511.json") if state else None,
                   {"cameras": cfg.roads_cameras_s, "events": cfg.roads_events_s, "signs": cfg.roads_signs_s}, log=log,
                   thruway=on and cfg.roads_thruway, county=on and cfg.roads_county)

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
        return n

    def due(self) -> list[tuple[str, str]]:
        """(provider, feed) pairs to fetch now."""
        return [(p.name, f) for p in self.providers for f in p.due()]

    def refresh(self, feed: str, provider: Provider | str | None = None) -> bool:
        """Fetch one feed now, from one provider or all; True when any was read. Never raises."""
        ps = [self.provider(provider) if isinstance(provider, str) else provider] if provider else self.providers
        got = False
        for p in ps:
            if p is not None and p.refresh(feed):
                got = True
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
                    p.data[feed] = items
                    p.status[feed].update(at=at, n=len(items), next=at + p.every[feed])
            self._changed()
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.log(f"roads: cache {self.cache.name} not read ({type(e).__name__}); fetching afresh")

    def _save(self) -> None:
        if not self.cache:
            return
        doc = {"bbox": list(self.bbox),
               "providers": {p.name: {f: {"at": p.status[f]["at"], "items": p.data[f]} for f in FEEDS} for p in self.providers}}
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(self.cache)
        except OSError as e:
            self.log(f"roads: cache not saved ({type(e).__name__}: {e})")

    # -- what the pages get ----------------------------------------------------------------------------

    def merged(self, feed: str) -> list[dict]:
        """Every provider's items, 511NY first; a later provider's item within DEDUPE_M of one already
        there (the same layer, for events), or with the same id, is the same thing and left out."""
        with self._lock:
            cur = self._merge
            if cur is not None and cur[0] == self.rev and feed in cur[1]:
                return cur[1][feed]
        out, ids = [], set()
        for p in self.providers:
            for it in p.data[feed]:
                if it["id"] in ids:
                    continue
                if any(km(it["lat"], it["lon"], o["lat"], o["lon"]) * 1000 <= DEDUPE_M
                       and (feed != "events" or it.get("layer") == o.get("layer")) for o in out if o.get("source") != it.get("source")):
                    continue
                out.append(it)
                ids.add(it["id"])
        with self._lock:
            if self._merge is None or self._merge[0] != self.rev:
                self._merge = (self.rev, {})
            self._merge[1][feed] = out
        return out

    def _cam_public(self, c: dict) -> dict:
        out = {k: c.get(k, "") for k in ("id", "name", "road", "dir", "mile", "lat", "lon", "has_video", "off", "source")}
        out["cadence"] = int(c.get("cadence") or CADENCE_S.get(c.get("source", ""), 60))
        return out | self.health(c["id"])

    # -- camera health: down when its image is stale or it keeps failing (#99) --------------------------

    def health(self, cid: str) -> dict:
        h = self._health.get(cid)
        if not h:
            return {"down": False, "down_since": None, "last_image": None}
        return {"down": h["down_since"] is not None, "down_since": h["down_since"], "last_image": h["last_image"]}

    def _note_image(self, cid: str, ok: bool, taken: float | None, now: float) -> None:
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
            stale = taken is not None and now - taken > STALE_IMAGE_S
            if (h["fails"] >= DOWN_FAILS or stale) and h["down_since"] is None:
                h["down_since"] = now
        if was != (h["down_since"] is not None):
            self._changed()

    def _down_answer(self, cid: str) -> tuple[int, bytes, str, dict]:
        h = self.health(cid)
        return 404, json.dumps({"error": "no current image", **h}).encode(), JSON, {"Cache-Control": "no-store"}

    def public(self) -> dict:
        """The layers for the maps. Camera image URLs stay here (the browser uses our proxy)."""
        return {"configured": self.configured, "note": self.ny.why_not(), "source": SOURCE, "attribution": ATTRIBUTION,
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
        cams = [c for c in self.merged("cameras") if not c["off"] and not self.health(c["id"])["down"]]
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
                if self.health(c["id"])["down"]:                              # demoted: the folded "N cameras down" row
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
            if c["id"] in seen or c["off"] or not self.health(c["id"])["down"]:
                continue
            k = km(lat, lon, c["lat"], c["lon"])
            if k <= NEARBY_KM:
                down.append(self._cam_public(c) | {"rel": "down", "miles": None, "side": "", "facing": False, "km": round(k, 2)})
        down.sort(key=lambda c: c["km"])
        return {"configured": self.configured, "highway": where, "cameras": out, "down": down}

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
                if now - taken > STALE_IMAGE_S:          # an old picture (a dead camera's placeholder): no image
                    self._note_image(cid, False, taken, now)
                    self._images.pop(cid, None)
                    return self._down_answer(cid)
                self._note_image(cid, True, taken, now)
                hit = {"body": body, "ctype": ctype, "taken": taken, "fetched": now}
                self._images[cid] = hit
                self._images.move_to_end(cid)
                while len(self._images) > IMAGE_KEEP:
                    self._images.popitem(last=False)
                return self._image_out(hit, False, since, cam)
            except (HTTPError, URLError, OSError, ValueError) as e:
                why = _redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", self.ny.key)
                self._note_image(cid, False, None, now)
                if self.health(cid)["down"]:              # down: say so, never the old picture
                    self._images.pop(cid, None)
                    return self._down_answer(cid)
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

    def stream(self, cid: str, name: str) -> tuple[int, bytes, str, dict]:
        """GET /api/cad/cameras/<id>/stream/<name>: playlist.m3u8 (the camera's master playlist, its chunklist
        named as the hub serves it), <chunklist>.m3u8 (the segment names likewise), <segment>.ts (the bytes).
        A playlist request opens or keeps the camera's stream (STREAM_MAX at once, hub-wide; a 5th is 429);
        segments are served only while one was asked for within STREAM_IDLE_S: a closed viewer stops the
        flow by itself. Nothing is fetched that a playlist didn't name, and only from the stream's own folder."""
        cam = self.camera(cid)
        if cam is None or not self.configured:
            return 404, b'{"error":"no such camera"}', JSON, {}
        if not cam.get("video"):
            return 404, b'{"error":"this camera has no video stream"}', JSON, {}
        if self.health(cid)["down"]:
            return self._down_answer(cid)
        now = self.clock()
        base = cam["video"].rsplit("/", 1)[0] + "/"
        hdr = {"Cache-Control": "no-store"}
        if name == "status":
            return 200, json.dumps(self.stream_status(cid)).encode(), JSON, hdr
        failed_at = self._stream_down.get(cid)
        if failed_at is not None and now - failed_at < STREAM_RETRY_S:        # its source refused lately: don't wait again
            return 503, json.dumps({"error": "no live video from the source right now", "source_down": True,
                                    "retry_s": int(STREAM_RETRY_S - (now - failed_at))}).encode(), JSON, hdr
        opened = False
        with self._stream_lock:
            st = self._streams.get(cid)
            if name.endswith(".m3u8"):
                if st is None or now - st["last"] > STREAM_IDLE_S:
                    if self.streams_open(now) >= STREAM_MAX:
                        return 429, json.dumps({"error": f"too many live streams open ({STREAM_MAX}); snapshots instead"}).encode(), JSON, hdr
                    st = self._streams[cid] = {"last": now, "base": base, "chunk": "", "list": None, "list_at": 0.0,
                                               "target": 6.0, "frames": 0, "seconds": 0.0, "fps": None, "ahead": 0}
                    opened = True
                st["last"] = now
            elif st is None or now - st["last"] > STREAM_IDLE_S:
                return 404, b'{"error":"the stream is not open: ask for its playlist first"}', JSON, hdr
        src = cam["video"] if name == "playlist.m3u8" else base + name
        if name.endswith(".ts"):
            key = f"{cid}/{name}"
            hit = self._segments.get(key)
            if hit and now - hit["fetched"] < STREAM_SEG_TTL_S:
                return 200, hit["body"], hit["ctype"], {**hdr, "Cache-Control": "private, max-age=60"}
        elif name != "playlist.m3u8" and st.get("list") and name == st.get("chunk") and now - st["list_at"] < STREAM_LIST_FRESH_S:
            return 200, st["list"], "application/vnd.apple.mpegurl", hdr          # the pump's fresh copy
        try:
            with self._parallel:
                code, headers, body = self.fetch(src, STREAM_OPEN_TIMEOUT_S if name == "playlist.m3u8" else STREAM_TIMEOUT_S,
                                                 STREAM_MAX_BYTES)
            if code != 200 or not body:
                raise ValueError(f"HTTP {code}")
        except (HTTPError, URLError, OSError, ValueError) as e:
            why = _redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", self.ny.key)
            if opened:                                           # a stream that never answered holds no slot
                with self._stream_lock:
                    self._streams.pop(cid, None)
                    self._stream_down[cid] = now                 # ... and isn't asked again for a while
                self.log(f"roads: {cid}: no live video from the source ({why}); snapshots for {STREAM_RETRY_S:.0f} s")
                return 503, json.dumps({"error": "no live video from the source right now", "source_down": True,
                                        "retry_s": int(STREAM_RETRY_S)}).encode(), JSON, hdr
            return 502, json.dumps({"error": f"the stream didn't answer ({why})"}).encode(), JSON, hdr
        self._stream_down.pop(cid, None)
        if name.endswith(".ts"):
            ctype = self._keep_segment(cid, name, headers, body, now)
            return 200, body, ctype, {**hdr, "Cache-Control": "private, max-age=60"}
        # a playlist: every line that names a file must be a plain name in the stream's own folder
        clean = self._clean_playlist(body)
        if clean is None:
            return 502, b'{"error":"the stream names files the hub will not fetch"}', JSON, hdr
        with self._stream_lock:
            st = self._streams.get(cid)
            if st is not None:
                if name == "playlist.m3u8":
                    st["chunk"] = next((t for t in clean.decode().splitlines() if t and not t.startswith("#")), "")
                elif name == st.get("chunk"):
                    self._note_list(st, clean, now)
        if opened and st is not None:
            self._start_pump(cid)
        return 200, clean, "application/vnd.apple.mpegurl", hdr

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

    def _keep_segment(self, cid: str, name: str, headers: dict, body: bytes, now: float) -> str:
        """A fetched segment into the cache; its frames into the stream's frame-rate estimate."""
        ctype = (headers.get("content-type") or "video/MP2T").split(";")[0].strip() or "video/MP2T"
        with self._stream_lock:
            self._segments[f"{cid}/{name}"] = {"body": body, "ctype": ctype, "fetched": now}
            self._segments.move_to_end(f"{cid}/{name}")
            while len(self._segments) > STREAM_SEG_KEEP:
                self._segments.popitem(last=False)
            st = self._streams.get(cid)
            if st is not None and st.get("list"):
                dur = _segment_duration(st["list"], name)
                if dur:
                    st["frames"] += ts_frames(body)
                    st["seconds"] += dur
                    if st["seconds"] >= 4:
                        st["fps"] = round(st["frames"] / st["seconds"], 1)
        return ctype

    def stream_status(self, cid: str) -> dict:
        """{open, fps, ahead, target_s, source_down}: for the viewer's notes (a low frame rate at the source)."""
        now = self.clock()
        st = self._streams.get(cid)
        is_open = st is not None and now - st["last"] <= STREAM_IDLE_S
        failed_at = self._stream_down.get(cid)
        return {"open": is_open, "fps": st["fps"] if is_open else None, "ahead": st["ahead"] if is_open else 0,
                "target_s": st["target"] if is_open else None,
                "source_down": failed_at is not None and now - failed_at < STREAM_RETRY_S}

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
            if hit and now - hit["fetched"] < STREAM_SEG_TTL_S:
                continue
            try:
                with self._parallel:
                    code, headers, body = self.fetch(st["base"] + name, STREAM_TIMEOUT_S, STREAM_MAX_BYTES)
                if code != 200 or not body:
                    continue
            except (HTTPError, URLError, OSError, ValueError):
                continue
            self._keep_segment(cid, name, headers, body, now)
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
            return self.stream(m.group(1), m.group(2))
        return 404, b"not found\n", "text/plain", {}
