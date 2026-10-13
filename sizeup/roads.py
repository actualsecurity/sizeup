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
                                           nearest to a point (an event's "Cameras nearby")
    GET /api/cad/cameras/<id>/image       the snapshot (image/jpeg ...), X-Image-Time: when it was taken
                                           (the camera's Last-Modified, else when the hub fetched it)

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
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

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
try:
    from sizeup import __version__ as _VERSION
except Exception:  # pragma: no cover
    _VERSION = "0"
# A plain client that says who it is (no browser impersonation, ever: a host that refuses this is left alone)
UA = f"sizeup/{_VERSION} (Port Chester FD dispatch; contact https://github.com/actualsecurity/sizeup)"
FEEDS = ("cameras", "events", "signs")
# West, south, east, north: Port Chester and Rye Brook, then Rye, Harrison / Purchase, Mamaroneck,
# Rye Neck, White Plains' east edge, Greenwich CT, and I-287 / I-95 / Hutch / Merritt between them.
DEFAULT_BBOX = (-73.80, 40.92, -73.56, 41.10)
EVERY_S = {"cameras": 3600.0, "events": 90.0, "signs": 300.0}
MIN_EVERY_S = {"cameras": 600.0, "events": 60.0, "signs": 60.0}   # 3 feeds at these: 5 calls a minute, under 10
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
NEAR_KM = 1.5                    # "Cameras nearby": this close to the event
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
                    "url": url, "has_video": bool(video), "off": off or not url})
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
                    "lat": round(pt[0], 6), "lon": round(pt[1], 6), "url": url, "has_video": bool(video),
                    "off": off or not url})
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
        return f"{self.short} feeds off"

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
                 thruway: bool = True, thruway_urls: dict | None = None):
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
        self.providers = [p for p in (self.ny, self.thruway) if p is not None]
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
                   thruway=on and cfg.roads_thruway)

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
                        self.refresh(feed, p)
            self._stop.wait(5.0)

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

    @staticmethod
    def _cam_public(c: dict) -> dict:
        return {k: c.get(k, "") for k in ("id", "name", "road", "dir", "mile", "lat", "lon", "has_video", "off", "source")}

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
        cams = [c for c in self.merged("cameras") if not c["off"]]
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

    # -- the snapshot proxy ----------------------------------------------------------------------------

    def image(self, cid: str) -> tuple[int, bytes, str, dict]:
        """(code, body, content type, headers) for GET /api/cad/cameras/<id>/image."""
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
            if hit and now - hit["fetched"] < IMAGE_TTL_S:
                self._images.move_to_end(cid)
                return self._image_out(hit, False)
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
                hit = {"body": body, "ctype": ctype, "taken": taken, "fetched": now}
                self._images[cid] = hit
                self._images.move_to_end(cid)
                while len(self._images) > IMAGE_KEEP:
                    self._images.popitem(last=False)
                return self._image_out(hit, False)
            except (HTTPError, URLError, OSError, ValueError) as e:
                why = _redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", self.ny.key)
                if hit and now - hit["fetched"] < IMAGE_STALE_S:
                    return self._image_out(hit, True)
                return 502, json.dumps({"error": f"the camera didn't answer ({why})"}).encode(), JSON, {}

    @staticmethod
    def _image_out(hit: dict, stale: bool) -> tuple[int, bytes, str, dict]:
        h = {"Cache-Control": f"private, max-age={int(IMAGE_TTL_S)}", "X-Image-Time": str(int(hit["taken"]))}
        if stale:
            h["X-Image-Stale"] = "1"
        return 200, hit["body"], hit["ctype"], h

    # -- HTTP ------------------------------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        return path == "/api/cad/roads" or path == "/api/cad/cameras" or bool(CAMERA_PATH.fullmatch(path))

    def get(self, path: str, query: str) -> tuple[int, bytes, str, dict]:
        q = parse_qs(query or "")
        one = lambda k: (q.get(k) or [""])[0].strip()
        if path == "/api/cad/roads":
            if one("rev") and one("rev") == str(self.rev):
                return 200, json.dumps({"rev": self.rev, "same": True}).encode(), JSON, {}
            return 200, self._public_bytes(), JSON, {}
        if path == "/api/cad/cameras":
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
            return self.image(m.group(1))
        return 404, b"not found\n", "text/plain", {}
