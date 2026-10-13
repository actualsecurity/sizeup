"""Highway layers for the CAD maps (GitHub #73): traffic cameras, incidents and closures, roadwork and
message signs from NYSDOT's 511NY data feeds, and the mile markers and exits from the gazetteer.

User, 2026-10-13, looking at the Thruway's traveler map around Port Chester: "it looks like this has
location data of all the highway things, including cameras, that would be nice to add in for the
ability to @ and then also, click on camera feeds available from the maps (maybe there is a layers
option to turn on these overlays too on the map)".

Source: the 511NY data feeds (https://511ny.org/help/24, "Developer Access Agreement", updated
2026-09-18): traffic events (JSON), the camera inventory (XML), message sign messages (JSON) and
winter road conditions. Access is by request (a Google form linked there, reviewed weekly); the
approval comes with the feed addresses and a key. So the addresses are config ([roads] events_url,
cameras_url, signs_url, each with "{key}" where the key goes) and the key is a secret
(env SIZEUP_511NY_KEY; the hub: SSM /sizeup-hub/511ny-key). Each feed is read as JSON or XML,
whichever comes back, and each record is read by field name, case-insensitively, under any of the
names 511NY has used (the developer API's ID / RoadwayName / DirectionOfTravel / Latitude ...,
the camera inventory's Views [{Url, VideoUrl}] ...): see FIELDS.

Terms (that agreement): no fee; we may "redistribute, enhance, repackage, and add value to the
data" but not "alter the substance of a report"; we "must display the time at which the data was
published or last updated wherever [we] present it" (each event, sign and snapshot shows its time);
the source is acknowledged as "powered by 511NY" with a link (the map's attribution). Not used: the
511NY website's own map endpoints and the Thruway map's camera feed (undocumented).

The feeds are statewide, so only our area is kept (bbox: Port Chester, Rye Brook and the mutual-aid
neighbours with a margin). The old developer API allowed 10 calls a minute per key; we stay well
under: events every 90 s, signs every 5 min, cameras hourly; a refused key or a 429 backs off.

The key is only ever in the request this module makes to 511NY: never in a response, the state, the
disk cache, an error or the log (_redact). No key or no feed address: nothing is fetched and the maps
say "not configured"; the mile markers and exits still show (they are ours, from the gazetteer).

Camera images: the browser never talks to 511NY or a camera (the key stays here, and nothing is
mixed content or cross-origin behind Cloudflare Access): GET /api/cad/cameras/<id>/image is fetched
by the hub from the camera's own image URL (as the inventory lists it), kept IMAGE_TTL_S so a room
full of viewers is one request, a few at a time (IMAGE_PARALLEL). Snapshots only: a camera's video
stream (HLS) is not proxied (every segment would come through the hub, and the agreement says
nothing about re-serving video); "has_video" is kept so a later version can decide.

Cached on disk (state dir, roads511.json: what we keep of each feed, never the key or a feed
address), so a restart doesn't spend calls; each feed is fetched again once its interval has passed.

HTTP (sizeup/web/server.py, any signed-in person: viewers too, all GET):
    GET /api/cad/roads[?rev=]             {configured, rev, status, cameras, events, signs, mileposts, exits,
                                           attribution}; ?rev= the same rev: {rev, same: true}
    GET /api/cad/cameras?q=&near=lat,lon&n=   {cameras: [...]}: the place search for "@...camera", or the
                                           nearest to a point (an event's "Cameras nearby")
    GET /api/cad/cameras/<id>/image       the snapshot (image/jpeg ...), X-Image-Time: when it was taken
                                           (the camera's Last-Modified, else when the hub fetched it)

Place search (#67's "@"): Roads.search_places(q, near, limit) answers in the place search's own
shape ({kind: "camera", label, sub, lat, lon, source, camera}); the app hands it to the CAD as
cad.place_providers (until #67's /api/cad/places asks it, GET /api/cad/cameras?q= is the same).
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
UA = "sizeup-cad (Port Chester FD dispatch; 511NY data feeds)"
FEEDS = ("cameras", "events", "signs")
# West, south, east, north: Port Chester and Rye Brook, then Rye, Harrison / Purchase, Mamaroneck,
# Rye Neck, White Plains' east edge, Greenwich CT, and I-287 / I-95 / Hutch / Merritt between them.
DEFAULT_BBOX = (-73.80, 40.92, -73.56, 41.10)
EVERY_S = {"cameras": 3600.0, "events": 90.0, "signs": 300.0}
MIN_EVERY_S = {"cameras": 600.0, "events": 60.0, "signs": 60.0}   # 3 feeds at these: 5 calls a minute, under 10
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
    """511NY's times -> unix seconds: "13/10/2026 14:03:21" (day first, Eastern), ISO 8601, or epoch."""
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
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
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
        for part in re.split(r"<br\s*/?>|\n|\||\[nl\]|\[np\]", str(m or ""), flags=re.I):
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


class Roads:
    def __init__(self, key: str = "", urls: dict | None = None, bbox=DEFAULT_BBOX, cache: str | Path | None = None,
                 every: dict | None = None, gazetteer: str | Path | None = None,
                 fetch: Callable[[str, float, int], tuple[int, dict, bytes]] = http_get,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.key = (key or "").strip()
        self.bbox = tuple(float(x) for x in bbox)
        self.cache = Path(cache) if cache else None
        self.every = {k: max(MIN_EVERY_S[k], float((every or {}).get(k, EVERY_S[k]))) for k in FEEDS}
        # Each feed's address as 511NY gave it, "{key}" where the key goes; a feed with no address (or
        # needing a key we don't have) is off.
        self.urls = {f: str((urls or {}).get(f) or "").strip() for f in FEEDS}
        self.fetch, self.log, self.clock = fetch, log, clock
        self._lock = threading.Lock()
        self.data: dict[str, list[dict]] = {k: [] for k in FEEDS}
        self.status: dict[str, dict] = {k: {"at": None, "n": 0, "error": "", "next": 0.0, "fails": 0} for k in FEEDS}
        self.rev = 0
        self._public: tuple[int, bytes] | None = None
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
                   {"cameras": cfg.roads_cameras_s, "events": cfg.roads_events_s, "signs": cfg.roads_signs_s}, log=log)

    def feed_on(self, feed: str) -> bool:
        u = self.urls.get(feed, "")
        return bool(u) and ("{key}" not in u or bool(self.key))

    @property
    def configured(self) -> bool:
        """At least one feed can be read (an address, and the key when it needs one)."""
        return any(self.feed_on(f) for f in FEEDS)

    def why_not(self) -> str:
        """Why nothing is fetched, for the maps and the log ("" when something is)."""
        if self.configured:
            return ""
        if not any(self.urls.values()):
            return ("511NY not configured: no feed addresses ([roads] events_url, cameras_url, signs_url, "
                    "from 511NY's access approval)")
        return "511NY not configured: no key (SIZEUP_511NY_KEY)"

    def _feed_url(self, feed: str) -> str:
        return self.urls[feed].replace("{key}", quote(self.key, safe=""))

    # -- the schedule ----------------------------------------------------------------------------------

    def start(self) -> Roads:
        if not self.configured:
            self.log(f"roads: {self.why_not()}: cameras, incidents and signs off; mile markers and exits still shown")
            return self
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="roads-511ny", daemon=True)
            self._thread.start()
            on = [f"{f} every {self.every[f]:.0f} s" for f in FEEDS if self.feed_on(f)]
            self.log(f"roads: 511NY on ({', '.join(on)}), area {self.bbox}")
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            for feed in ("events", "signs", "cameras"):
                if self._stop.is_set():
                    break
                if self.feed_on(feed) and self.clock() >= self.status[feed]["next"]:
                    self.refresh(feed)
            self._stop.wait(5.0)

    def due(self) -> list[str]:
        now = self.clock()
        return [f for f in FEEDS if self.feed_on(f) and now >= self.status[f]["next"]]

    def refresh(self, feed: str) -> bool:
        """Fetch one feed now; True when it was read. Never raises."""
        if not self.feed_on(feed):
            return False
        st = self.status[feed]
        now = self.clock()
        try:
            code, _, body = self.fetch(self._feed_url(feed), TIMEOUT_S, MAX_FEED_BYTES)
            if code != 200:
                raise HTTPError("", code, f"HTTP {code}", None, None)
            items = PARSE[feed](decode(body), self.bbox)
        except HTTPError as e:
            return self._failed(feed, now, e.code, f"HTTP {e.code}")
        except (URLError, OSError, ValueError) as e:
            return self._failed(feed, now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}")
        except Exception as e:  # a surprise in the feed must not stop the others
            return self._failed(feed, now, 0, f"{type(e).__name__}: {e}")
        with self._lock:
            self.data[feed] = items
            st.update(at=now, n=len(items), error="", fails=0, next=now + self.every[feed])
            self._changed()
        self._save()
        return True

    def _failed(self, feed: str, now: float, code: int, why: str) -> bool:
        st = self.status[feed]
        why = _redact(why, self.key)
        st["fails"] += 1
        if code in (401, 403):
            wait, why = REFUSED_S, f"511NY refused the key ({why})"
        elif code == 429:
            wait, why = RATE_LIMITED_S, f"511NY rate limit ({why})"
        else:
            wait = max(MIN_EVERY_S[feed], min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** (st["fails"] - 1)))
        with self._lock:
            changed = st["error"] != why
            st.update(error=why, next=now + wait)
            if changed:
                self._changed()
        self.log(f"roads: {feed}: {why}; next try in {wait:.0f} s")
        return False

    def _changed(self) -> None:
        self.rev += 1
        self._public = None

    # -- the disk cache (no key in it) -----------------------------------------------------------------

    def _load(self) -> None:
        if not self.cache or not self.cache.is_file():
            return
        try:
            doc = json.loads(self.cache.read_text())
            if [round(x, 4) for x in doc.get("bbox", [])] != [round(x, 4) for x in self.bbox]:
                return                                    # another area: fetch afresh
            now = self.clock()
            for feed in FEEDS:
                f = doc.get(feed) or {}
                at, items = f.get("at"), f.get("items")
                if not isinstance(items, list) or not isinstance(at, (int, float)) or at > now + 60:
                    continue
                self.data[feed] = items
                self.status[feed].update(at=at, n=len(items), next=at + self.every[feed])
            self._changed()
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.log(f"roads: cache {self.cache.name} not read ({type(e).__name__}); fetching afresh")

    def _save(self) -> None:
        if not self.cache:
            return
        with self._lock:
            doc = {"source": SOURCE, "bbox": list(self.bbox),
                   **{f: {"at": self.status[f]["at"], "items": self.data[f]} for f in FEEDS}}
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(self.cache)
        except OSError as e:
            self.log(f"roads: cache not saved ({type(e).__name__}: {e})")

    # -- what the pages get ----------------------------------------------------------------------------

    @staticmethod
    def _cam_public(c: dict) -> dict:
        return {k: c[k] for k in ("id", "name", "road", "dir", "lat", "lon", "has_video", "off")}

    def public(self) -> dict:
        """The layers for the maps. Camera image URLs stay here (the browser uses our proxy)."""
        with self._lock:
            return {"configured": self.configured, "note": self.why_not(), "source": SOURCE, "attribution": ATTRIBUTION,
                    "link": LINK_URL, "rev": self.rev, "bbox": list(self.bbox),
                    "status": {f: {"on": self.feed_on(f), "at": s["at"], "n": s["n"], "error": s["error"]}
                               for f, s in self.status.items()},
                    "cameras": [self._cam_public(c) for c in self.data["cameras"]],
                    "events": list(self.data["events"]), "signs": list(self.data["signs"]),
                    "mileposts": self.places.get("mileposts", []), "exits": self.places.get("exits", []),
                    "highway_places": bool(self.places)}

    def _public_bytes(self) -> bytes:
        cur = self._public
        if cur is None or cur[0] != self.rev:
            cur = (self.rev, json.dumps(self.public(), separators=(",", ":")).encode())
            self._public = cur
        return cur[1]

    def camera(self, cid: str) -> dict | None:
        with self._lock:
            return next((c for c in self.data["cameras"] if c["id"] == cid), None)

    def nearest(self, lat: float, lon: float, n: int = 3, within_km: float = NEAR_KM) -> list[dict]:
        with self._lock:
            cams = [c for c in self.data["cameras"] if not c["off"]]
        d = sorted(((km(lat, lon, c["lat"], c["lon"]), c) for c in cams), key=lambda x: x[0])
        return [self._cam_public(c) | {"km": round(k, 2)} for k, c in d[:max(0, n)] if k <= within_km]

    def search_places(self, q: str, near: tuple[float, float] | None = None, limit: int = 8) -> list[dict]:
        """Cameras for the place search ("@I-95 exit 21 camera"), in #67's place shape. Every typed
        word (less "camera", "cam", "cctv") must start a word of the camera's name, road or direction;
        "camera" in the text ranks them first."""
        words = _words(q)
        asked = any(w in ("camera", "cameras", "cam", "cams", "cctv") for w in words)
        words = [w for w in words if w not in ("camera", "cameras", "cam", "cams", "cctv", "at", "the", "on", "near")]
        if not words and not asked:
            return []
        with self._lock:
            cams = list(self.data["cameras"])
        out = []
        for c in cams:
            hay = _words(f"{c['name']} {c['road']} {c['dir']}")
            hay += [h[1:] for h in hay if re.fullmatch(r"i\d+", h)]          # "I-287" is also "287"
            if not all(any(h.startswith(w) for h in hay) for w in words):
                continue
            score = (1.0 if asked else 0.6) - (0.2 if c["off"] else 0.0)
            if near:
                score -= min(0.3, km(near[0], near[1], c["lat"], c["lon"]) / 50)
            out.append({"kind": "camera", "label": f"Camera: {c['name']}", "sub": " ".join(x for x in (c["road"], c["dir"]) if x),
                        "lat": c["lat"], "lon": c["lon"], "source": SOURCE, "camera": c["id"], "score": round(score, 3)})
        out.sort(key=lambda p: -p["score"])
        return out[:max(1, min(limit, 20))]

    # -- the snapshot proxy ----------------------------------------------------------------------------

    def image(self, cid: str) -> tuple[int, bytes, str, dict]:
        """(code, body, content type, headers) for GET /api/cad/cameras/<id>/image."""
        if not self.configured:
            return 404, b'{"error":"511NY is not configured"}', JSON, {}
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
                why = _redact(f"{type(e).__name__}: {getattr(e, 'reason', e)}", self.key)
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
