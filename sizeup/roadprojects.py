"""Road projects & closures for the CAD maps and the incident view (GitHub #185).

User, 2026-10-15: "can we add road projects/etc when roads might be closed etc what dates/times and all that and
include that as a layer?"

One provider (RoadProjects) reads every source we may use, normalises each item to one record and serves them to
the maps (the "Road projects & closures" group in the Layers box, roadprojects.js) and to the incident view ("Road
closed: Westchester Ave between X and Y until 3:00 PM (Port Chester)", near()). Sources: SOURCES_DOC below.

The record (every source, the same shape; "" / None / [] where the source says nothing, never a guess):
    id          "<feed>:<the source's own id>"
    source      the short name shown on the map ("Thruway", "511NY", "NYSDOT", "Port Chester")
    kind        roadwork | closure | detour | event | notice | project (a capital project in construction: its
                footprint and completion date, no closure times: the line and the box say so)
    title       one line; description: the source's text, as given
    road, dir   the road and direction (as the source names them); cross_from, cross_to: "between A and B"
    closure     full | lanes | shoulder | ramp | alternating | detour | "" (not said)
    lanes       the source's lane text ("2 Left lanes closed"); detour: its detour text
    geometry    GeoJSON Point / LineString / MultiLineString / (Multi)Polygon (a project's footprint), or None (an
                unplaced notice); lat, lon: a point on it
    start, end  unix seconds as the source gives them, or None; start_text / end_text the source's own words
                ("Until further notice"); no_end: the source gives no end time (the click box says so); end_day:
                a completion given as a date alone ("2027-10-26": end is the end of that day here)
    hours       recurring daily hours, [{days: [0..6] (Monday 0; [] every day), from: "21:00", to: "05:00"}], only
                when the source gives them as data; hours_text: the source's own words for them
    updated     when the source last updated the item (its terms: show it); link: the source's page for it
    placed      on the map (False: listed in the panel only, unplaced); place_how: how it was placed
    also        other feeds' records of the same work zone, folded into this one (items())

windows(rec, now): the item's spans of time from now on (and the one it is in), [[start | None, end | None], ...]:
its dates, cut into its daily hours when it has them, for the next WINDOW_DAYS. The page decides "active now"
(a window around now), "upcoming" (a later window) and the Today / This week / All filter from these alone, in
the console's timezone; the hub works them out in TZ (the console's, America/New_York).

HTTP (sizeup/web/server.py; any signed-in person, GET only):
    GET /api/cad/roadprojects         {configured, note, sources: [{name, short, label, link, terms, status, on,
                                       at, n, error, why}], tz, at, items: [record + windows], notices: [unplaced]};
                                       ETag (304 when unchanged); asking keeps the sources coming (DEMAND_S)
    GET /api/cad/roadprojects/near?lat=&lon=&address=&from=&to=
                                      the incident view's lines: {items: [{id, text, source, distance_m, how,
                                       active_now}]}: items within NEAR_M of the point, or on the incident's street
                                       within STREET_M, in effect at some time between from and to (unix s; default
                                       now)

Nothing per-second: none of this is in the CAD's state push (#146); the page polls the layer every few minutes
only while a map has it on, and the incident view asks once per event and every few minutes while it is open.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sizeup import USER_AGENT as UA

TZ_NAME = "America/New_York"          # the console's timezone (app.py scene "timezone")
TZ = ZoneInfo(TZ_NAME)
JSON = "application/json"
NEAR_PATH = "/api/cad/roadprojects/near"
WINDOW_DAYS = 8                       # windows worked out this far ahead (the page's "This week" is 7 days)
MAX_WINDOWS = 24
NEAR_M = 300.0                        # an item this close to the incident's point counts
STREET_M = 1500.0                     # ... or on the incident's own street, this close
DEDUPE_M = 60.0                       # the same road, this close, overlapping times, from two sources: one item
DEMAND_S = 3600.0                     # a viewer's ask keeps the fetched sources coming this long
TIMEOUT_S = 20.0
MAX_BYTES = 8 * 1024 * 1024
BACKOFF_S = (120.0, 1800.0)           # a failed read: 2 min doubling to 30
REFUSED_S = 3600.0                    # 401 / 403
RATE_LIMITED_S = 900.0                # 429
DEFAULT_BBOX = (-73.80, 40.92, -73.56, 41.10)


def _s(v, n: int = 400) -> str:
    return " ".join(str(v or "").split())[:n]


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def m_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12_742_000 * math.asin(min(1.0, math.sqrt(a)))


def _lines(g: dict | None) -> list[list[list[float]]]:
    """A geometry as lists of [lon, lat] points (a Point: one list of one)."""
    if not isinstance(g, dict):
        return []
    t, c = g.get("type"), g.get("coordinates")
    try:
        if t == "Point":
            return [[[float(c[0]), float(c[1])]]]
        if t in ("LineString", "MultiPoint"):
            return [[[float(p[0]), float(p[1])] for p in c]]
        if t in ("MultiLineString", "Polygon"):
            return [[[float(p[0]), float(p[1])] for p in ln] for ln in c]
        if t == "MultiPolygon":
            return [[[float(p[0]), float(p[1])] for p in ring] for poly in c for ring in poly]
    except (TypeError, ValueError, IndexError):
        return []
    return []


def distance_m(g: dict | None, lat: float, lon: float) -> float:
    """Metres from the point to the geometry (to a line's nearest segment, 0 inside a polygon; a flat projection: our
    area is small)."""
    if isinstance(g, dict) and g.get("type") in ("Polygon", "MultiPolygon"):
        from sizeup.gis import contains
        if contains(g, lat, lon):
            return 0.0
    best = math.inf
    kx, ky = 111_320.0 * math.cos(math.radians(lat)), 110_540.0
    for ln in _lines(g):
        pts = [((x - lon) * kx, (y - lat) * ky) for x, y in ln]
        if len(pts) == 1:
            best = min(best, math.hypot(*pts[0]))
            continue
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = bx - ax, by - ay
            t = 0.0 if not (dx or dy) else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def representative(g: dict | None) -> tuple[float, float] | None:
    """(lat, lon) of a point on the geometry: a line's middle vertex."""
    ls = [ln for ln in _lines(g) if ln]
    if not ls:
        return None
    ln = max(ls, key=len)
    x, y = ln[len(ln) // 2]
    return y, x


def in_bbox_geom(g: dict | None, bbox) -> bool:
    w, s, e, n = bbox
    return any(w <= x <= e and s <= y <= n for ln in _lines(g) for x, y in ln)


def point(lat, lon) -> dict | None:
    la, lo = _f(lat), _f(lon)
    return {"type": "Point", "coordinates": [round(lo, 6), round(la, 6)]} if la is not None and lo is not None else None


def decode_polyline(s: str, precision: int = 5) -> list[list[float]]:
    """Google's encoded polyline -> [[lon, lat], ...] (511 platforms' EncodedPolyline)."""
    coords, idx, lat, lon, f = [], 0, 0, 0, 10 ** precision
    try:
        while idx < len(s):
            for which in (0, 1):
                shift = result = 0
                while True:
                    b = ord(s[idx]) - 63
                    idx += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                d = ~(result >> 1) if result & 1 else result >> 1
                if which == 0:
                    lat += d
                else:
                    lon += d
            coords.append([round(lon / f, 6), round(lat / f, 6)])
    except IndexError:
        return coords
    return coords


# ---- time ---------------------------------------------------------------------------------------------------


def when(v) -> float | None:
    """A source's time -> unix seconds: epoch (s or ms), ISO 8601 ("2026-10-15T21:00:00-04:00", a date alone: its
    midnight here), RFC 822 (RSS pubDate), "10/13/2026 4:45 PM". Words ("Until further notice"): None."""
    if v in (None, "") or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e11 else 1.0) if v > 0 else None
    s = str(v).strip()
    if re.fullmatch(r"\d{9,13}", s):
        return when(int(s))
    m = re.fullmatch(r"/Date\((\d+)([+-]\d{4})?\)/", s)                  # .NET JSON dates
    if m:
        return int(m.group(1)) / 1000.0
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ).timestamp()
        except ValueError:
            pass
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def day_numbers(v) -> list[int]:
    """["Monday", "Tue", 3, "SUNDAY"] / "Mon,Wed" -> [0, 1, 3, 6]; anything not a day: left out."""
    items = v if isinstance(v, list) else re.split(r"[,;/\s]+", str(v or ""))
    out = set()
    for x in items:
        if isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 6:
            out.add(x)
            continue
        t = str(x).strip().lower()[:3]
        for i, d in enumerate(DAYS):
            if t and d.startswith(t):
                out.add(i)
    return sorted(out)


def hhmm(v) -> str:
    """"21:00", "9:00 PM", "21:00:00", "T21:00" -> "21:00"; "" when it isn't a time of day."""
    s = str(v or "").strip().upper()
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?", s)
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            return ""
        h = h % 12 + (12 if m.group(3) == "PM" else 0)
    return f"{h:02d}:{mi:02d}" if h <= 24 and mi < 60 else ""


def _at(day: datetime, t: str) -> datetime:
    """That wall-clock time on that day here ("24:00": the next midnight)."""
    h, m = (int(x) for x in t.split(":"))
    base = datetime(day.year, day.month, day.day, tzinfo=TZ)
    return base.replace(hour=h, minute=m) if h < 24 else datetime(*(day + timedelta(days=1)).timetuple()[:3], tzinfo=TZ)


def windows(rec: dict, now: float, days: int = WINDOW_DAYS) -> list[list[float | None]]:
    """The item's spans of time that end after now (the one it is in first), as [start | None, end | None]: its
    start and end dates, cut into its daily hours when the source gives them. None: not given by the source. An
    item that has ended: []."""
    start, end, hours = rec.get("start"), rec.get("end"), [h for h in rec.get("hours") or [] if h.get("from") and h.get("to")]
    if end is not None and end <= now:
        return []
    if not hours:
        return [[start, end]]
    out: list[list[float | None]] = []
    today = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    for k in range(-1, days + 1):
        d = (today + timedelta(days=k)).replace(tzinfo=None)
        for hr in hours:
            if hr.get("days") and d.weekday() not in hr["days"]:
                continue
            s = _at(d, hr["from"]).timestamp()
            e = _at(d, hr["to"]).timestamp()
            if e <= s:
                e = _at(d + timedelta(days=1), hr["to"]).timestamp()
            if start is not None:
                s = max(s, start)
            if end is not None:
                e = min(e, end)
            if e > s and e > now:
                out.append([s, e])
    out.sort(key=lambda w: w[0])
    return out[:MAX_WINDOWS]


def state(wins: list, now: float) -> str:
    """active (a window around now) | upcoming (a later one) | ended."""
    for s, e in wins:
        if (s is None or s <= now) and (e is None or now < e):
            return "active"
    return "upcoming" if wins else "ended"


def in_effect(rec: dict, t0: float, t1: float) -> list[float | None] | None:
    """The window of the item that overlaps [t0, t1] (the latest such), or None."""
    hit = None
    for s, e in windows(rec, t0):
        if (s is None or s <= t1) and (e is None or e > t0):
            hit = [s, e]
    return hit


def clock(t: float) -> str:
    """3:00 PM (minutes kept: 9:30 PM)."""
    return datetime.fromtimestamp(t, TZ).strftime("%-I:%M %p")


def day_clock(t: float, now: float) -> str:
    """"3:00 PM" today, "Oct 18 3:00 PM" another day (the console's timezone)."""
    d, n = datetime.fromtimestamp(t, TZ), datetime.fromtimestamp(now, TZ)
    return clock(t) if d.date() == n.date() else f"{d.strftime('%b')} {d.day} {clock(t)}"


def arcgis_day(v) -> tuple[float | None, str]:
    """An ArcGIS date field that holds a date (ms at midnight, UTC or here) -> (the end of that day here, "2027-10-26")."""
    t = _f(v)
    if t is None or t <= 0:
        return None, ""
    from datetime import timezone
    u = datetime.fromtimestamp(t / 1000.0, timezone.utc)
    d = u.date() if (u.hour, u.minute) == (0, 0) else datetime.fromtimestamp(t / 1000.0, TZ).date()
    return (datetime(d.year, d.month, d.day, tzinfo=TZ) + timedelta(days=1)).timestamp(), d.isoformat()


def day_text(day: str) -> str:
    """"2027-10-26" -> "Oct 26, 2027"."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return day
    return f"{d.strftime('%b')} {d.day}, {d.year}"


# ---- the incident view's line ----------------------------------------------------------------------------------

HEAD = {"full": "Road closed", "lanes": "Lane closure", "shoulder": "Shoulder closed", "ramp": "Ramp closed",
        "alternating": "Alternating traffic", "detour": "Detour"}
KIND_HEAD = {"roadwork": "Roadwork", "closure": "Closure", "detour": "Detour", "event": "Event", "notice": "Notice",
             "project": "Construction project"}


def where_text(rec: dict) -> str:
    road = rec.get("road") or ""
    a, b = rec.get("cross_from") or "", rec.get("cross_to") or ""
    if road and a and b:
        return f"{road} between {a} and {b}"
    if road and a:
        return f"{road} at {a}"
    if road:
        return " ".join(x for x in (road, rec.get("dir") or "") if x)
    return rec.get("title") or "(no location given)"


def note_line(rec: dict, win: list | None, now: float) -> str:
    """"Road closed: Westchester Ave between X and Y until 3:00 PM (Port Chester)": what, where, until when, whose."""
    head = HEAD.get(rec.get("closure") or "") or KIND_HEAD.get(rec.get("kind") or "", "Roadwork")
    tail = ""
    if rec.get("kind") == "notice":
        posted = f", posted {day_clock(rec['updated'], now)}" if rec.get("updated") else ""
        return f"Notice: {where_text(rec)} ({rec.get('source') or '?'}{posted}): \"{rec.get('title') or ''}\" (dates in the notice)"
    if rec.get("kind") == "project":
        done = (f"completion date {day_text(rec['end_day'])}" if rec.get("end_day")
                else rec.get("end_text") or "no completion date given")
        title = rec.get("title") or where_text(rec)
        title = title if len(title) <= 100 else title[:99].rstrip() + "…"
        return f"{head}: {title} ({rec.get('source') or '?'}: in construction, {done}; no closure times given)"
    if win is not None:
        s, e = win
        hours = rec.get("hours") or []
        if e is not None and e <= now:
            tail = f"ended {day_clock(e, now)}"
        elif s is not None and s > now:
            tail = f"from {day_clock(s, now)}" + (f" until {day_clock(e, now)}" if e is not None else ", no end time given")
        elif e is not None:
            tail = f"until {day_clock(e, now)}"
            if hours and rec.get("end"):
                tail += f" (daily hours; work ends {day_clock(rec['end'], now)})"
        else:
            tail = "no end time given" + (f" ({rec['end_text']})" if rec.get("end_text") else "")
    return f"{head}: {where_text(rec)}" + (f" {tail}" if tail else "") + f" ({rec.get('source') or '?'})"


STREET_TYPES = {"avenue": "ave", "av": "ave", "street": "st", "road": "rd", "drive": "dr", "place": "pl", "lane": "ln",
                "boulevard": "blvd", "parkway": "pkwy", "court": "ct", "terrace": "ter", "highway": "hwy",
                "turnpike": "tpke", "north": "n", "south": "s", "east": "e", "west": "w"}


def street_key(name: str) -> str:
    """"Westchester Avenue" / "WESTCHESTER AVE" -> "westchester ave"; "I-95 - New England Thruway" -> "i95"."""
    t = (name or "").lower()
    m = re.match(r"\s*(i|interstate|us|ny|ct|rt|route|sr)[\s-]*(\d+[a-z]?)\b", t)
    if m:
        p = {"interstate": "i", "rt": "", "route": "", "sr": ""}.get(m.group(1), m.group(1))
        return f"{p}{m.group(2)}"
    if re.search(r"\bhutch(inson)?\b", t):
        return "hutch"
    words = [STREET_TYPES.get(w, w) for w in re.findall(r"[a-z0-9]+", t)]
    return " ".join(words)


def street_of(address: str) -> str:
    """The street of an incident's address ("209 Westchester Ave, Port Chester" -> "westchester ave"); "" for none."""
    a = re.split(r"[,(]", address or "")[0]
    a = re.sub(r"^\s*\d+[a-z]?\s+", "", a, flags=re.I)
    a = re.split(r"\s+(?:&|and|at|x|cross of)\s+", a, flags=re.I)[0]
    return street_key(a) if re.search(r"[a-z]", a, re.I) else ""


# ---- the client -------------------------------------------------------------------------------------------------


def http_get(url: str, timeout: float, limit: int, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """(status, headers, body): urllib, GET only; a 304 comes back as (304, headers, b"")."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/xml;q=0.9, */*;q=0.5",
                                **(headers or {})})
    try:
        with urlopen(req, timeout=timeout) as r:
            body = r.read(limit + 1)
            if len(body) > limit:
                raise ValueError(f"more than {limit // (1024 * 1024)} MB")
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body
    except HTTPError as e:
        if e.code == 304:
            return 304, {k.lower(): v for k, v in (e.headers or {}).items()}, b""
        raise


# ---- the Thruway's and 511NY's roadwork (read by sizeup/roads.py: no second fetch) ------------------------------

BETWEEN = re.compile(r"\bbetween\s+(.+?)\s+and\s+(.+?)\s*$", re.I)
AT = re.compile(r"\bat\s+((?:exit|interchange|mile|mp)\b.+?)\s*$", re.I)
FULL = re.compile(r"\b(road|roadway|highway|bridge|all lanes|both directions)\s+(is\s+|are\s+)?closed\b|\bclosed to (all )?traffic\b"
                  r"|\bfull(y)? clos", re.I)
RAMP = re.compile(r"\b(ramp|exit|entrance)\s+(is\s+)?closed\b|\bramp closure\b", re.I)
SHOULDER = re.compile(r"\bshoulder\s+(is\s+)?(closed|blocked)\b|\bshoulder closure\b", re.I)
LANES = re.compile(r"\blanes?\s+(is\s+|are\s+)?(closed|blocked)\b|\blane closure", re.I)
ALTERNATING = re.compile(r"\balternating\b|\bflagg(er|ing)\b|\bone[- ]lane\b", re.I)
DETOUR = re.compile(r"\bdetour", re.I)


def closure_of(*texts: str, full: bool = False) -> str:
    """full | ramp | lanes | shoulder | alternating | detour | "" from the source's words (and its full-closure flag)."""
    t = " ".join(x for x in texts if x)
    if full or FULL.search(t):
        return "full"
    for rx, kind in ((RAMP, "ramp"), (LANES, "lanes"), (SHOULDER, "shoulder"), (ALTERNATING, "alternating"), (DETOUR, "detour")):
        if rx.search(t):
            return kind
    return ""


def from_roads_event(e: dict) -> dict | None:
    """A sizeup/roads.py event (the Thruway's, 511NY's) of the roadwork layer as a record."""
    if e.get("layer") != "roadwork":
        return None
    desc = _s(e.get("description"), 600)
    m = BETWEEN.search(desc)
    a, b = (m.group(1), m.group(2)) if m else ("", "")
    if not m and (m2 := AT.search(desc)):
        a = m2.group(1)
    g = None
    if isinstance(e.get("line"), list) and len(e["line"]) >= 2:
        g = {"type": "LineString", "coordinates": e["line"]}
    g = g or point(e.get("lat"), e.get("lon"))
    src = e.get("source") or "?"
    return {"id": f"{src.lower()}:{e.get('id')}", "source": src, "kind": "roadwork",
            "title": _s(e.get("subtype") or e.get("type") or "Roadwork", 120).capitalize(), "description": desc,
            "road": _s(e.get("road"), 80), "dir": _s(e.get("dir"), 40), "cross_from": _s(a, 80), "cross_to": _s(b, 80),
            "mile": _s(e.get("mile"), 10),
            "closure": closure_of(desc, e.get("lanes") or "", full=bool(e.get("full_closure"))),
            "lanes": _s(e.get("lanes"), 160), "detour": _s(e.get("detour"), 400), "geometry": g,
            "start": e.get("start"), "end": e.get("end"), "start_text": "", "end_text": _s(e.get("end_text"), 80),
            "no_end": e.get("end") is None, "hours": list(e.get("hours") or []), "hours_text": _s(e.get("hours_text"), 200),
            "updated": e.get("updated"), "link": e.get("link") or "", "placed": g is not None, "place_how": ""}


# ---- WZDx (the Work Zone Data Exchange, USDOT's open spec, v4.x): a GeoJSON FeatureCollection ---------------------
#   features[].properties: core_details {event_type: work-zone | detour, data_source_id, road_names [], direction,
#   name, description, update_date, related_road_events}, start_date, end_date, is_start_date_verified,
#   is_end_date_verified, beginning_cross_street, ending_cross_street, vehicle_impact (all-lanes-closed,
#   some-lanes-closed, all-lanes-open, alternating-one-way, some-lanes-closed-merge-left, ...), lanes [{order, type,
#   status: open | closed | shift-left ...}], types_of_work, worker_presence, restrictions; geometry LineString /
#   MultiPoint. Feeds of v3 put the same under properties directly (road_name, event_type, ...).

WZDX_IMPACT = {"all-lanes-closed": "full", "some-lanes-closed": "lanes", "some-lanes-closed-merge-left": "lanes",
               "some-lanes-closed-merge-right": "lanes", "some-lanes-closed-split": "lanes", "alternating-one-way": "alternating",
               "flagging": "alternating", "all-lanes-open": "", "unknown": ""}


def parse_wzdx(doc, short: str, bbox=DEFAULT_BBOX, prefix: str = "wzdx") -> list[dict]:
    out = []
    feats = doc.get("features") if isinstance(doc, dict) else None
    for f in feats or []:
        if not isinstance(f, dict):
            continue
        p = f.get("properties") or {}
        core = p.get("core_details") if isinstance(p.get("core_details"), dict) else p
        g = f.get("geometry") if isinstance(f.get("geometry"), dict) else None
        if g and g.get("type") == "MultiPoint":
            g = {"type": "LineString", "coordinates": g.get("coordinates") or []}
        if not g or not in_bbox_geom(g, bbox):
            continue
        fid = _s(f.get("id") or p.get("road_event_id") or core.get("road_event_id"), 120)
        if not fid:
            continue
        roads = core.get("road_names") or ([core.get("road_name")] if core.get("road_name") else [])
        lanes = [ln for ln in p.get("lanes") or [] if isinstance(ln, dict)]
        closed = [ln for ln in lanes if str(ln.get("status", "")).lower() == "closed"]
        lane_text = (f"{len(closed)} of {len(lanes)} lanes closed" if closed and lanes else "")
        impact = str(p.get("vehicle_impact") or "").lower()
        etype = str(core.get("event_type") or p.get("event_type") or "work-zone").lower()
        closure = WZDX_IMPACT.get(impact, "") or ("detour" if etype == "detour" else "")
        if closure == "" and closed and any(str(ln.get("type", "")).lower() == "shoulder" for ln in closed):
            closure = "shoulder"
        rp = representative(g)
        start, end = when(p.get("start_date")), when(p.get("end_date"))
        work = ", ".join(_s(w.get("type_name") if isinstance(w, dict) else w, 40) for w in p.get("types_of_work") or [])
        out.append({"id": f"{prefix}:{fid}", "source": short, "kind": "detour" if etype == "detour" else "roadwork",
                    "title": _s(core.get("name") or work or ("Detour" if etype == "detour" else "Work zone"), 120),
                    "description": _s(core.get("description") or p.get("description"), 600),
                    "road": _s(", ".join(str(r) for r in roads), 80), "dir": _s(core.get("direction") or p.get("direction"), 40),
                    "cross_from": _s(p.get("beginning_cross_street"), 80), "cross_to": _s(p.get("ending_cross_street"), 80),
                    "mile": "", "closure": closure, "lanes": lane_text, "detour": "",
                    "geometry": g, "lat": rp[0] if rp else None, "lon": rp[1] if rp else None,
                    "start": start, "end": end, "start_text": "" if start else _s(p.get("start_date"), 60),
                    "end_text": "" if end else _s(p.get("end_date"), 60), "no_end": end is None,
                    "verified": {"start": p.get("is_start_date_verified"), "end": p.get("is_end_date_verified")},
                    "hours": [], "hours_text": "", "updated": when(core.get("update_date") or p.get("update_date")),
                    "link": "", "placed": True, "place_how": ""})
    return out


# ---- capital construction projects: where a project is being built and when it is due to finish (no daily hours,
#      lanes or detours in these sources: the records say so, nothing is made up) ----------------------------------------


def _first_sentence(text: str, n: int = 160) -> str:
    t = _s(text, 2000)
    m = re.match(r"(.+?[.;])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def parse_nysdot_capital(doc, short: str = "NYSDOT", bbox=DEFAULT_BBOX) -> list[dict]:
    """NYSDOT's Capital Program Projects, In Construction (gis.dot.ny.gov ArcGIS, GeoJSON): a project's pieces (one
    feature each) as one record per PIN: {PIN, ContractNumber, PublicFriendlyDescription, ConstructionCompleteDate (ms,
    a date), WEPIURL, WORK_CATEGORY, ResponsibleOrganization}."""
    by: dict[str, dict] = {}
    for f in (doc.get("features") if isinstance(doc, dict) else None) or []:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        pin = _s(p.get("PIN"), 20)
        if not pin or g.get("type") not in ("Polygon", "MultiPolygon") or not in_bbox_geom(g, bbox):
            continue
        polys = [g["coordinates"]] if g["type"] == "Polygon" else list(g["coordinates"])
        if pin in by:
            by[pin]["geometry"]["coordinates"].extend(polys)
            continue
        end, day = arcgis_day(p.get("ConstructionCompleteDate"))
        desc = _s(p.get("PublicFriendlyDescription"), 1200)
        link = _s(p.get("WEPIURL"), 300)
        by[pin] = {"id": f"nysdot:{pin}", "source": short, "kind": "project",
                   "title": _first_sentence(desc) or f"Project {pin}", "description": desc,
                   "road": "", "dir": "", "cross_from": "", "cross_to": "", "mile": "", "closure": "", "lanes": "",
                   "detour": "", "geometry": {"type": "MultiPolygon", "coordinates": polys},
                   "start": None, "end": end, "end_day": day, "start_text": "", "no_end": end is None,
                   "end_text": "" if end else "no completion date given",
                   "hours": [], "hours_text": "", "updated": None,
                   "link": link if link.startswith("https://") else "", "placed": True,
                   "place_how": "the project's footprint (NYSDOT)",
                   "facts": [x for x in (f"PIN {pin}", f"contract {_s(p.get('ContractNumber'), 20)}" if p.get("ContractNumber") else "",
                                         _s(p.get("WORK_CATEGORY"), 60).lower(), "in construction") if x]}
    out = list(by.values())
    for r in out:
        rp = representative(r["geometry"])
        r["lat"], r["lon"] = rp if rp else (None, None)
    return sorted(out, key=lambda r: r["id"])


CT_MAX_TOWNS = 4
CT_ROUTES = {"1": "US-1", "95": "I-95", "15": "CT-15 (Merritt Pkwy)", "684": "I-684", "7": "US-7"}


def ct_route(r: str) -> str:
    """CTDOT's "95-N-17" / "1-S" / "FERRIS DR NO 1" -> "I-95" / "US-1" / "Ferris Dr No 1"."""
    r = r.strip()
    m = re.fullmatch(r"(\d+[A-Z]?)(-[NSEW](-\w+)?)?", r)
    if m:
        return CT_ROUTES.get(m.group(1), f"CT-{m.group(1)}")
    return r.title()


CT_RAIL = re.compile(r"\b(power prog\w*|nhl|catenary|rail\w*)\b", re.I)


def parse_ctdot_projects(doc, short: str = "CTDOT", bbox=DEFAULT_BBOX, towns: tuple[str, ...] = ("Greenwich",)) -> list[dict]:
    """CTDOT's Project Work Areas (services1.arcgis.com ArcGIS, GeoJSON), in construction, in our towns: {ProjectNumber,
    Title, ProjectDescription, CurrentSchedulePhase, EstConstrCompletionDate / ConstructionCompletionDate (ms, a date),
    StateRoutes, Towns, ConstructionDistrict, PrimaryDesigner, last_edited_date}. Rail projects (the New Haven Line's
    power and bridges), studies and designs are not road work and are left out; so is a project with its completion
    date passed (CTDOT's "Missing Dates" rows)."""
    out = []
    for f in (doc.get("features") if isinstance(doc, dict) else None) or []:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        num = _s(p.get("ProjectNumber"), 20)
        phase = _s(p.get("CurrentSchedulePhase"), 40)
        title = _s(p.get("Title"), 120)
        if not num or g.get("type") not in ("Polygon", "MultiPolygon") or not in_bbox_geom(g, bbox):
            continue
        if "construction" not in phase.lower():
            continue
        place = {t.strip() for t in str(p.get("Towns") or "").split(",") if t.strip()}
        if (towns and not place & set(towns)) or len(place) > CT_MAX_TOWNS:
            continue                                   # not ours, or a district-wide program (its "work area" is the district)
        if (str(p.get("ConstructionDistrict") or "").strip().upper() == "RAILS" or "rail" in str(p.get("PrimaryDesigner") or "").lower()
                or CT_RAIL.search(title) or re.search(r"\b(study|design)\b", title, re.I)):
            continue
        done, day = arcgis_day(p.get("ConstructionCompletionDate"))
        est = False
        if done is None:
            done, day = arcgis_day(p.get("EstConstrCompletionDate"))
            est = done is not None
        desc = _s(p.get("ProjectDescription"), 1200)
        routes = list(dict.fromkeys(ct_route(r) for r in str(p.get("StateRoutes") or "").split(",") if r.strip()))
        out.append({"id": f"ctdot:{num}", "source": short, "kind": "project", "title": title or f"Project {num}",
                    "description": desc, "road": _s(", ".join(routes[:4]), 80), "dir": "", "cross_from": "", "cross_to": "",
                    "mile": "", "closure": "", "lanes": "", "detour": "", "geometry": g, "start": None, "end": done,
                    "end_day": day, "end_estimated": est, "start_text": "", "no_end": done is None,
                    "end_text": "" if done else f"no completion date given ({phase})", "hours": [], "hours_text": "",
                    "updated": (_f(p.get("last_edited_date")) or 0) / 1000.0 or None,
                    "link": "", "placed": True,
                    "place_how": "the project's work area (CTDOT)",
                    "facts": [x for x in (f"project {num}", phase.replace("05_", "").lower(),
                                          "estimated completion" if est else "") if x]})
    for r in out:
        rp = representative(r["geometry"])
        r["lat"], r["lon"] = rp if rp else (None, None)
    return sorted(out, key=lambda r: r["id"])


def parse_thruway_capital(body: bytes, short: str = "Thruway", bbox=DEFAULT_BBOX) -> list[dict]:
    """The Thruway's capital program (webdataapi.thruway.ny.gov capitalprogram/xml): its active projects in our area,
    <activecapprojects> {id, description, locationdescription, latitude, longitude, lettingdate, estimatedcompletiondate
    (a year), contractno}. A point each; the completion only as the year the Thruway gives."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(body.decode("utf-8-sig", errors="replace").lstrip())
    except ET.ParseError as e:
        raise ValueError(f"bad XML: {e}") from None
    out = []
    for e in root.iter():
        if e.tag.split("}")[-1].lower() != "activecapprojects":
            continue
        get = lambda k: (e.findtext(k) or "").strip()
        lat, lon = _f(get("latitude")), _f(get("longitude"))
        pid = _s(get("id"), 20)
        w, s_, ea, n = bbox
        if not pid or lat is None or lon is None or not (s_ <= lat <= n and w <= lon <= ea):
            continue
        year = _s(get("estimatedcompletiondate"), 20)
        loc = _s(get("locationdescription"), 120)
        out.append({"id": f"thruwaycap:{pid}", "source": short, "kind": "project",
                    "title": _first_sentence(get("description")) or f"Project {pid}", "description": _s(get("description"), 600),
                    "road": "I-95" if loc.startswith("NE ") else "I-287" if loc.startswith("CW ") else "", "dir": "",
                    "cross_from": "", "cross_to": "", "mile": "", "closure": "", "lanes": "", "detour": "",
                    "geometry": point(lat, lon), "lat": round(lat, 6), "lon": round(lon, 6), "start": None, "end": None,
                    "end_day": "", "start_text": "", "no_end": True,
                    "end_text": f"estimated completion {year}" if year else "no completion date given",
                    "hours": [], "hours_text": "", "updated": None, "link": "",
                    "placed": True, "place_how": f"the Thruway's point for the project ({loc})" if loc else "the Thruway's point",
                    "facts": [x for x in (f"project {pid}", f"let {get('lettingdate')}" if get("lettingdate") else "",
                                          f"contract {_s(get('contractno'), 30)}" if get("contractno") else "") if x]})
    return out


# ---- CivicPlus public RSS feeds (a village's Alert Center and News Flash): road notices, placed only by the text ----

ROAD_WORDS = re.compile(r"\b(clos(ed|ure|ures|ing)|detours?|paving|milling|resurfac\w*|utilit(y|ies)|construction|road ?work|"
                        r"street ?work|excavat\w*|water main|gas main|sewer|drainage|parade|festival|festa|street fair|"
                        r"block party|lane closures?|traffic (stoppages?|delays?|pattern)|no left turns?)\b", re.I)
NOT_ROAD = re.compile(r"\b(leaf|leaves|pickup|pick-up|recycl\w*|garbage|sanitation|bulk|brochure|permitting)\b", re.I)
NOTICE_DAYS = 45              # a notice posted longer ago than this is old news: not shown


def _strip_html(s: str) -> str:
    import html
    return _s(html.unescape(re.sub(r"<[^>]+>", " ", s or "")), 2000)


def parse_rss(body: bytes) -> list[dict]:
    """RSS 2.0 items: {guid, title, link, description (text), published (unix), category, enclosure}."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(body.decode("utf-8-sig", errors="replace").lstrip())
    except ET.ParseError as e:
        raise ValueError(f"bad RSS: {e}") from None
    out = []
    for it in root.iter("item"):
        get = lambda tag: (it.findtext(tag) or "").strip()
        enc = it.find("enclosure")
        out.append({"guid": get("guid") or get("link"), "title": _s(get("title"), 200), "link": get("link"),
                    "description": _strip_html(get("description")), "published": when(get("pubDate")),
                    "category": _s(get("category"), 80), "enclosure": enc.get("url", "") if enc is not None else ""})
    return out


def road_related(item: dict) -> bool:
    """A notice about the roads: work, a closure, a detour, a parade or festival; not leaf pickup or sanitation (unless
    it closes a road)."""
    t = " ".join((item.get("title", ""), item.get("description", "")))
    return bool(ROAD_WORDS.search(t)) and not (NOT_ROAD.search(item.get("title", "")) and not CLOSED_WORDS.search(t))


CLOSED_WORDS = re.compile(r"\bclos(ed|ure|ures)\b|\bdetour\b", re.I)


# ---- placing a notice from its own words (the gazetteer; #176's cross-street check) -------------------------------

STREET_TYPE = (r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Place|Pl|Lane|Ln|Boulevard|Blvd|Parkway|Pkwy|Court|Ct|Terrace|"
               r"Ter|Highway|Hwy|Turnpike|Tpke|Way|Plaza|Circle|Cir)\b\.?")
NAME = r"(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?(?:[A-Z0-9][\w'.-]*\s+){1,3}?" + STREET_TYPE
LINK_WORDS = r"(?:(?:is|are|will|be|being|closed|closing|remain|remains|temporarily|fully|partially|to|traffic|work|paving|" \
             r"milling|on|in|both|directions|(?:north|south|east|west)bound),?\s+){0,6}"
SEGMENT = re.compile(rf"({NAME})\s*,?\s+{LINK_WORDS}(?:between|from)\s+({NAME}|[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)?)\s+(?:and|to|&)\s+({NAME})")
ADDRESS = re.compile(rf"\b(\d{{1,5}}[A-Z]?)\s+({NAME})")
CORNER = re.compile(rf"({NAME})\s+(?:and|&|at|/)\s+({NAME})")
SEGMENT_PERP_M = 60.0          # a street's sample this far off the line between the two meeting points: not on that block
SEGMENT_MAX_M = 2500.0


class Placer:
    """Where a notice's text says, only when it says it plainly and the gazetteer agrees: a street between two cross
    streets that both meet it (a line along the street), an address the gazetteer knows outright, or two streets that
    meet. Only in the notice's own towns. Anything less: not placed (listed in the panel), never guessed."""

    def __init__(self, gaz, min_confidence: float = 0.8):
        self.gaz = gaz
        self.min = min_confidence
        self._loc = None

    def loc(self):
        if self._loc is None:
            from sizeup.geo.pagegeo import locator
            self._loc = locator(self.gaz)
        return self._loc

    def street(self, name: str, towns: tuple[str, ...]) -> int | None:
        """The one street of that name in these towns (an exact name first), or None. A word or two before the name
        ("The", "On") is let go: the longest ending of the words that names a street."""
        words = name.split()
        for k in range(0, max(1, len(words) - 1)):
            sid = self._street(" ".join(words[k:]), towns)
            if sid is not None:
                return sid
        return None

    def _street(self, name: str, towns: tuple[str, ...]) -> int | None:
        from sizeup.geo.normalize import BOUNDARY, spaceless, tokenize
        toks = [t for t in tokenize(name) if t != BOUNDARY]
        if not toks:
            return None
        loc = self.loc()
        hits = []
        for idx, sim in self.gaz._lookup(spaceless(toks), "street", True, 0.9):
            _a, _k, sid, _typed, pen = self.gaz._entries[idx]
            if sim * pen >= 0.9 and (not towns or loc.towns_of.get(sid, set()) & set(towns)):
                hits.append((sim * pen, sid))
        if not hits:
            return None
        hits.sort(reverse=True)
        best = [sid for s, sid in hits if s >= hits[0][0] - 1e-9]
        exact = [sid for sid in best if street_key(self.gaz.streets[sid].name) == street_key(name)]
        pick = exact or best
        return pick[0] if len(set(pick)) == 1 else None

    def centreline(self, sid: int) -> list[tuple[float, float]]:
        """The street's centreline vertices (the gazetteer's TIGER lines), in metres: never its address points."""
        from sizeup.geo.gazetteer import _xy
        import sqlite3
        if not hasattr(self, "_lines"):
            self._lines: dict[int, list[tuple[float, float]]] = {}
        if sid not in self._lines:
            pts: list[tuple[float, float]] = []
            try:
                db = sqlite3.connect(f"file:{self.gaz.path}?mode=ro", uri=True)
                try:
                    for (coords,) in db.execute("SELECT coords FROM lines WHERE street_id = ?", (sid,)):
                        for lon, lat in json.loads(coords):
                            x, y = _xy(lat, lon)
                            pts.append((float(x), float(y)))
                finally:
                    db.close()
            except (sqlite3.Error, ValueError, TypeError):
                pts = []
            self._lines[sid] = pts
        return self._lines[sid]

    def segment(self, sid: int, a: int, b: int) -> list[list[float]] | None:
        """Street sid between where a and b meet it: [[lon, lat], ...] along its centreline, or None."""
        from sizeup.geo.gazetteer import _latlon
        loc = self.loc()
        best = None
        for x1, y1 in loc._meets(sid, a):
            for x2, y2 in loc._meets(sid, b):
                L = math.hypot(x2 - x1, y2 - y1)
                if 5 <= L <= SEGMENT_MAX_M and (best is None or L < best[0]):
                    best = (L, x1, y1, x2, y2)
        if best is None:
            return None
        L, x1, y1, x2, y2 = best
        vx, vy = (x2 - x1) / L, (y2 - y1) / L
        pts = [(0.0, x1, y1), (L, x2, y2)]
        for x, y in self.centreline(sid):
            t = (x - x1) * vx + (y - y1) * vy
            if 0 < t < L and abs((x - x1) * vy - (y - y1) * vx) <= SEGMENT_PERP_M:
                pts.append((float(t), float(x), float(y)))
        pts.sort()
        out = []
        for _t, x, y in pts:
            lat, lon = _latlon(x, y)
            c = [round(lon, 6), round(lat, 6)]
            if not out or c != out[-1]:
                out.append(c)
        return out if len(out) >= 2 else None

    def place(self, text: str, towns: tuple[str, ...]) -> dict | None:
        """{geometry, road, cross_from, cross_to, how} from the first plain location in the text, or None."""
        if not text or self.gaz is None:
            return None
        for m in SEGMENT.finditer(text):
            sid, a, b = (self.street(m.group(i), towns) for i in (1, 2, 3))
            if sid is None or a is None or b is None or len({sid, a, b}) < 3:
                continue
            line = self.segment(sid, a, b)
            if line:
                names = [self.gaz.streets[x].name for x in (sid, a, b)]
                return {"geometry": {"type": "LineString", "coordinates": line}, "road": names[0], "cross_from": names[1],
                        "cross_to": names[2], "how": f"the notice names {names[0]} between {names[1]} and {names[2]}"}
        for rx, kind in ((ADDRESS, "address"), (CORNER, "intersection")):
            for m in rx.finditer(text):
                if kind == "intersection" and re.search(r"\b(between|from)\s*$", text[:m.start()]):
                    continue                          # "between A and B": the ends of a block, not a corner
                for c in self.gaz.candidates(m.group(0))[:3]:
                    if c.kind == kind and c.confidence >= self.min and (not towns or c.municipality in towns) \
                            and c.precision in ("exact", "interpolated", "intersection"):
                        return {"geometry": point(c.lat, c.lon), "road": c.street or "", "cross_from": c.cross_street or "",
                                "cross_to": "", "how": f"the notice names {c.label}"}
        return None


# ---- the sources ---------------------------------------------------------------------------------------------------
# Each read only while someone looks (DEMAND_S), with If-None-Match / If-Modified-Since. "{bbox}": [roads] bbox (west,
# south, east, north). Terms: SOURCES_DOC.

SOURCES_DOC = """
  Thruway roadwork   sizeup/roads.py's Thruway events (webdataapi.thruway.ny.gov events/xml), roadwork only; no start
                     time, the end as the Thruway gives it ("Until further notice"). Disclaimer: no resale or
                     redistribution for profit (https://www.thruway.ny.gov/legal/disclaimer).
  511NY roadwork     sizeup/roads.py's 511NY events once the key arrives ([roads] events_url, SIZEUP_511NY_KEY): start,
                     planned end, lanes, full closure, recurring schedules, detour, the encoded line, when given.
  Thruway projects   the Thruway's capital program (webdataapi.thruway.ny.gov capitalprogram/xml, public, no key):
                     active projects, a point and the estimated completion year. Same disclaimer.
  NYSDOT projects    NYSDOT's Capital Program Projects, In Construction (gis.dot.ny.gov ArcGIS, layer 4, public, no
                     key): footprints and the construction completion date. NYSDOT's web-service terms: free to use,
                     at the user's own risk.
  CTDOT projects     CTDOT's Project Work Areas (services1.arcgis.com, public, no key), in construction in Greenwich:
                     work areas and the (estimated) completion date; informational, at the user's own risk
                     (https://portal.ct.gov/Policies/Disclaimer). Staff names and e-mails are never asked for.
  Village notices    the public RSS feeds of Port Chester (CivicPlus Alert Center, News Flash), Greenwich (CivicPlus
                     Public Works news, Alert Center) and Rye Brook (WordPress): published for subscribers to read;
                     only their road notices, each linked to its page, placed only when its words name a block, an
                     address or a corner the gazetteer confirms.
  WZDx               any Work Zone Data Exchange feed ([roadwork] wzdx_url, "{key}" for a key: 511NY's, when its
                     post-2026-09-30 address is known). Off until set.
"""
THRUWAY_CAPITAL_URL = "https://webdataapi.thruway.ny.gov/api/data/capitalprogram/xml"
NYSDOT_URL = ("https://gis.dot.ny.gov/hostingny/rest/services/Projects/CapitalProgramProjects/FeatureServer/4/query"
              "?where=1%3D1&geometry={bbox}&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects"
              "&outFields=PIN,ContractNumber,PublicFriendlyDescription,ConstructionCompleteDate,WEPIURL,WORK_CATEGORY,"
              "ResponsibleOrganization&outSR=4326&f=geojson")
CTDOT_URL = ("https://services1.arcgis.com/FCaUeJ5SOVtImake/arcgis/rest/services/CTDOT_Project_Work_Areas/FeatureServer/0/query"
             "?where=CurrentSchedulePhase+LIKE+%27%25Construction%25%27&geometry={bbox}&geometryType=esriGeometryEnvelope"
             "&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=ProjectNumber,Title,ProjectDescription,"
             "CurrentSchedulePhase,EstConstrCompletionDate,ConstructionCompletionDate,StateRoutes,Towns,ConstructionDistrict,"
             "PrimaryDesigner,last_edited_date&outSR=4326&f=geojson")
# name, short, label, link, address, towns
NOTICE_FEEDS = (
    ("pc_alerts", "Port Chester", "Village of Port Chester: Alert Center", "https://www.portchesterny.gov/AlertCenter.aspx",
     "https://www.portchesterny.gov/RSSFeed.aspx?ModID=63&CID=All-0", ("Port Chester",)),
    ("pc_news", "Port Chester", "Village of Port Chester: News Flash", "https://www.portchesterny.gov/CivicAlerts.aspx",
     "https://www.portchesterny.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml", ("Port Chester",)),
    ("rb_news", "Rye Brook", "Village of Rye Brook: news", "https://ryebrookny.gov/",
     "https://ryebrookny.gov/feed/", ("Rye Brook",)),
    ("gw_dpw", "Greenwich", "Town of Greenwich: Public Works news", "https://www.greenwichct.gov/CivicAlerts.aspx",
     "https://www.greenwichct.gov/RSSFeed.aspx?ModID=1&CID=Public-Works-News-24", ("Greenwich",)),
    ("gw_alerts", "Greenwich", "Town of Greenwich: Alert Center", "https://www.greenwichct.gov/AlertCenter.aspx",
     "https://www.greenwichct.gov/RSSFeed.aspx?ModID=63&CID=All-0", ("Greenwich",)),
)
NOTICE_EVERY_S = 1200.0                 # 20 min
PROJECTS_EVERY_S = 86400.0              # the capital programs change slowly: daily
WZDX_EVERY_S = 300.0


def feeds_from_config(cfg) -> list[Feed]:
    bbox = ",".join(f"{x:g}" for x in getattr(cfg, "roads_bbox", DEFAULT_BBOX))
    out = []
    if getattr(cfg, "roadwork_projects", True):
        if THRUWAY_CAPITAL_URL and getattr(cfg, "roads_thruway", True):
            out.append(Feed("thruway_capital", "Thruway", "NYS Thruway Authority: capital program", "https://www.thruway.ny.gov/developers",
                            "https://www.thruway.ny.gov/legal/disclaimer", THRUWAY_CAPITAL_URL, "thruway_capital", PROJECTS_EVERY_S))
        if NYSDOT_URL:
            out.append(Feed("nysdot", "NYSDOT", "NYSDOT: capital projects in construction",
                            "https://gis.dot.ny.gov/hostingny/rest/services/Projects/CapitalProgramProjects/FeatureServer",
                            "https://gis.dot.ny.gov/hostingny/rest/services/Projects/CapitalProgramProjects/FeatureServer",
                            NYSDOT_URL.replace("{bbox}", bbox),
                            "nysdot_capital", PROJECTS_EVERY_S))
        if CTDOT_URL:
            out.append(Feed("ctdot", "CTDOT", "CTDOT: project work areas (Greenwich)",
                            "https://services1.arcgis.com/FCaUeJ5SOVtImake/arcgis/rest/services/CTDOT_Project_Work_Areas/FeatureServer/0",
                            "https://portal.ct.gov/Policies/Disclaimer", CTDOT_URL.replace("{bbox}", bbox), "ctdot_projects",
                            PROJECTS_EVERY_S, towns=("Greenwich",)))
    if getattr(cfg, "roadwork_notices", True):
        for name, short, label, link, url, towns in NOTICE_FEEDS:
            if url:
                out.append(Feed(name, short, label, link, link, url, "rss", NOTICE_EVERY_S, towns=towns))
    out.append(Feed("wzdx", "511NY", "511NY work zones (WZDx)", "https://511ny.org/", "https://511ny.org/help/24",
                    getattr(cfg, "roadwork_wzdx_url", ""), "wzdx", WZDX_EVERY_S, key=getattr(cfg, "roads_key", ""),
                    why_off="waiting for 511NY's key and WZDx address ([roadwork] wzdx_url, SIZEUP_511NY_KEY)"))
    return out


# ---- the provider ------------------------------------------------------------------------------------------------


class Feed:
    """A source this module reads itself (the Thruway's and 511NY's come through sizeup/roads.py): its address,
    its kind (wzdx | rss), its schedule and how the last read went. A 304 (ETag / Last-Modified) changes nothing."""

    def __init__(self, name: str, short: str, label: str, link: str, terms: str, url: str, kind: str, every_s: float,
                 key: str = "", towns: tuple[str, ...] = (), why_off: str = ""):
        self.name, self.short, self.label, self.link, self.terms = name, short, label, link, terms
        self.url, self.kind, self.every_s, self.key, self.towns = url, kind, float(every_s), (key or "").strip(), tuple(towns)
        self.why_off = why_off
        self.items: list[dict] = []
        self.notices: list[dict] = []
        self.status = {"at": None, "n": 0, "error": "", "next": 0.0, "fails": 0, "etag": "", "modified": ""}

    @property
    def on(self) -> bool:
        return bool(self.url) and ("{key}" not in self.url or bool(self.key))

    def why(self) -> str:
        if self.on:
            return ""
        if self.why_off:
            return self.why_off
        return "no key yet" if self.url and "{key}" in self.url else "no feed address"

    def address(self) -> str:
        from urllib.parse import quote
        return self.url.replace("{key}", quote(self.key, safe=""))

    def redact(self, text: str) -> str:
        text = str(text)
        if self.key:
            text = text.replace(self.key, "***")
        return re.sub(r"((?:key|api_key|apikey|token)=)[^&\s'\"]+", r"\1***", text, flags=re.I)

    def public(self) -> dict:
        st = self.status
        return {"name": self.name, "short": self.short, "label": self.label, "link": self.link, "terms": self.terms,
                "on": self.on, "why": self.why(), "at": st["at"], "n": len(self.items) + len(self.notices), "error": st["error"]}


SOURCE_RANK = {"511NY": 0, "Thruway": 2}         # a tie: the lower wins (WZDx feeds 1, notices 3)


class RoadProjects:
    def __init__(self, feeds: list[Feed] | None = None, roads=None, gaz=None, bbox=DEFAULT_BBOX,
                 cache: str | Path | None = None, enabled: bool = True,
                 fetch: Callable[..., tuple[int, dict, bytes]] = http_get, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time):
        self.enabled = enabled
        self.feeds = list(feeds or []) if enabled else []
        self.roads = roads if enabled else None
        self.gaz = gaz
        self.placer = Placer(gaz) if gaz is not None else None
        self.bbox = tuple(float(x) for x in bbox)
        self.cache = Path(cache) if cache else None
        self.fetch, self.log, self.clock = fetch, log, clock
        self.asked = 0.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._out: tuple | None = None
        self._load()

    @classmethod
    def from_config(cls, cfg, roads=None, gaz=None, log: Callable[[str], None] = print) -> RoadProjects:
        state = Path(cfg.cad_db).parent if getattr(cfg, "cad_db", "") else None
        return cls(feeds_from_config(cfg), roads=roads, gaz=gaz, bbox=getattr(cfg, "roads_bbox", DEFAULT_BBOX),
                   cache=(state / "roadprojects.json") if state else None,
                   enabled=getattr(cfg, "roadwork_enabled", True), log=log)

    @property
    def configured(self) -> bool:
        return self.enabled and (any(f.on for f in self.feeds) or self.roads is not None)

    def why_not(self) -> str:
        if not self.enabled:
            return "Road projects off ([roadwork] enabled = false)"
        return "" if self.configured else "No road project source is on"

    # -- the schedule ---------------------------------------------------------------------------------------------

    def start(self) -> RoadProjects:
        for f in self.feeds:
            if not f.on:
                self.log(f"roadwork: {f.short} off ({f.why()})")
        if not any(f.on for f in self.feeds) or (self._thread is not None and self._thread.is_alive()):
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="roadwork", daemon=True)
        self._thread.start()
        self.log("roadwork: " + ", ".join(f"{f.short} every {f.every_s / 60:g} min" for f in self.feeds if f.on)
                 + f" while someone looks (within {DEMAND_S / 60:g} min of an ask)")
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(30.0)
            self._wake.clear()

    def due(self) -> list[Feed]:
        now = self.clock()
        if now - self.asked > DEMAND_S:
            return []
        return [f for f in self.feeds if f.on and now >= f.status["next"]]

    def tick(self) -> int:
        n = 0
        for f in self.due():
            if self._stop.is_set():
                break
            n += self.refresh(f)
        return n

    def ask(self) -> None:
        """Someone looks (the layer, an incident view): keep the sources coming; a source never read is read now."""
        self.asked = self.clock()
        if any(f.on and f.status["at"] is None and self.clock() >= f.status["next"] for f in self.feeds):
            self._wake.set()

    def refresh(self, f: Feed) -> bool:
        """Read one source now; True when it was read (a 304 too). Never raises."""
        if not f.on:
            return False
        now = self.clock()
        st = f.status
        cond = {}
        if st["etag"]:
            cond["If-None-Match"] = st["etag"]
        if st["modified"]:
            cond["If-Modified-Since"] = st["modified"]
        try:
            code, headers, body = self.fetch(f.address(), TIMEOUT_S, MAX_BYTES, cond)
            if code == 304:
                st.update(at=now, error="", fails=0, next=now + f.every_s)
                return True
            if code != 200:
                raise HTTPError("", code, f"HTTP {code}", None, None)
            items, notices = self.parse(f, body)
        except HTTPError as e:
            return self._failed(f, now, e.code, f"HTTP {e.code}")
        except (URLError, OSError, ValueError) as e:
            return self._failed(f, now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}")
        except Exception as e:  # a surprise in one source must not stop the others
            return self._failed(f, now, 0, f"{type(e).__name__}: {e}")
        with self._lock:
            f.items, f.notices = items, notices
            st.update(at=now, n=len(items) + len(notices), error="", fails=0, next=now + f.every_s,
                      etag=headers.get("etag", "") or "", modified=headers.get("last-modified", "") or "")
            self._out = None
        self._save()
        return True

    def _failed(self, f: Feed, now: float, code: int, why: str) -> bool:
        st = f.status
        why = f.redact(why)
        st["fails"] += 1
        if code in (401, 403):
            wait, why = REFUSED_S, f"{f.short} refused the request ({why})"
        elif code == 429:
            wait, why = RATE_LIMITED_S, f"{f.short} rate limit ({why})"
        else:
            wait = min(BACKOFF_S[1], max(f.every_s / 4, BACKOFF_S[0]) * 2 ** (st["fails"] - 1))
        st.update(error=why, next=now + wait)
        self.log(f"roadwork: {f.short}: {why}; next try in {wait:.0f} s")
        return False

    def parse(self, f: Feed, body: bytes) -> tuple[list[dict], list[dict]]:
        if f.kind == "wzdx":
            text = body.decode("utf-8-sig", errors="replace").lstrip()
            if text[:1] not in "[{":
                raise ValueError("not JSON")
            return parse_wzdx(json.loads(text), f.short, self.bbox, f.name), []
        if f.kind == "rss":
            return self.notices_from(f, parse_rss(body))
        if f.kind == "thruway_capital":
            return parse_thruway_capital(body, f.short, self.bbox), []
        if f.kind in ("nysdot_capital", "ctdot_projects"):
            text = body.decode("utf-8-sig", errors="replace").lstrip()
            if text[:1] != "{":
                raise ValueError("not JSON")
            doc = json.loads(text)
            if isinstance(doc.get("error"), dict):              # ArcGIS answers its errors with a 200
                raise ValueError(f"ArcGIS error {doc['error'].get('code', '')}: {_s(doc['error'].get('message'), 120)}")
            if f.kind == "nysdot_capital":
                return parse_nysdot_capital(doc, f.short, self.bbox), []
            return parse_ctdot_projects(doc, f.short, self.bbox, f.towns), []
        raise ValueError(f"unknown kind {f.kind}")

    def notices_from(self, f: Feed, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        """A village's RSS items about the roads: placed by their own words (Placer), else listed unplaced."""
        placed, unplaced = [], []
        old = self.clock() - NOTICE_DAYS * 86400
        for r in rows:
            if not road_related(r) or (r["published"] is not None and r["published"] < old):
                continue
            text = f"{r['title']}. {r['description']}"
            where = self.placer.place(text, f.towns) if self.placer else None
            gid = hashlib.sha1((r["guid"] or r["title"]).encode()).hexdigest()[:12]
            if where is None:
                how = ("details in the village's flyer (linked; pictures aren't read)" if r.get("enclosure") or "flyer" in text.lower()
                       else "the notice names no street we can place")
            rec = {"id": f"{f.name}:{gid}", "source": f.short, "kind": "notice", "title": r["title"],
                   "description": r["description"], "road": (where or {}).get("road", ""), "dir": "",
                   "cross_from": (where or {}).get("cross_from", ""), "cross_to": (where or {}).get("cross_to", ""),
                   "mile": "", "closure": closure_of(text) if where else "", "lanes": "", "detour": "",
                   "geometry": (where or {}).get("geometry"), "start": None, "end": None, "start_text": "",
                   "end_text": "", "no_end": True, "hours": [], "hours_text": "", "updated": r["published"],
                   "link": r["link"], "flyer": r.get("enclosure", ""), "placed": where is not None,
                   "place_how": where["how"] if where else how}
            rp = representative(rec["geometry"])
            rec["lat"], rec["lon"] = (rp if rp else (None, None))
            (placed if where else unplaced).append(rec)
        return placed, unplaced

    # -- the disk cache (items only, never a key or a feed address) -------------------------------------------------

    def _load(self) -> None:
        if not self.cache or not self.cache.is_file():
            return
        try:
            doc = json.loads(self.cache.read_text())
            now = self.clock()
            for f in self.feeds:
                c = (doc.get("feeds") or {}).get(f.name) or {}
                at = c.get("at")
                if not isinstance(at, (int, float)) or at > now + 60 or not isinstance(c.get("items"), list):
                    continue
                f.items, f.notices = c["items"], list(c.get("notices") or [])
                f.status.update(at=at, n=len(f.items) + len(f.notices), next=at + f.every_s,
                                etag=str(c.get("etag") or ""), modified=str(c.get("modified") or ""))
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.log(f"roadwork: cache {self.cache.name} not read ({type(e).__name__}); reading afresh")

    def _save(self) -> None:
        if not self.cache:
            return
        doc = {"feeds": {f.name: {"at": f.status["at"], "items": f.items, "notices": f.notices, "etag": f.status["etag"],
                                  "modified": f.status["modified"]} for f in self.feeds}}
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(self.cache)
        except OSError as e:
            self.log(f"roadwork: cache not saved ({type(e).__name__}: {e})")

    # -- what the pages get ------------------------------------------------------------------------------------------

    def _roads_items(self) -> list[dict]:
        if self.roads is None:
            return []
        out = []
        for e in self.roads.merged("events"):
            r = from_roads_event(e)
            if r is not None:
                rp = representative(r["geometry"])
                r["lat"], r["lon"] = (rp if rp else (None, None))
                out.append(r)
        return out

    def _roads_sources(self) -> list[dict]:
        if self.roads is None:
            return []
        out = []
        for p in self.roads.providers:
            if p.name not in ("thruway", "511ny"):
                continue
            st = p.status["events"]
            on = p.feed_on("events")
            why = "" if on else ("waiting for 511NY's key ([roads] events_url, SIZEUP_511NY_KEY)" if p.name == "511ny"
                                 else "off ([roads] thruway = false)")
            n = sum(1 for e in p.data["events"] if e.get("layer") == "roadwork")
            out.append({"name": p.name, "short": p.short, "label": "511NY (NYSDOT)" if p.name == "511ny" else p.label,
                        "link": p.link, "terms": "https://511ny.org/help/24" if p.name == "511ny" else "https://www.thruway.ny.gov/legal/disclaimer",
                        "on": on, "why": why, "at": st["at"], "n": n, "error": st["error"]})
        return out

    def items(self) -> list[dict]:
        """Every source's records, one per thing: 511NY first, then the WZDx feeds and projects, the Thruway, the
        notices; a later one from another feed on the same road within DEDUPE_M of one already kept, at overlapping
        times, is the same thing (its source is named on the kept one, "also"). A notice is never folded (its dates
        are in its words, so nothing says it is the same closure) and never folds another."""
        roads_rev = getattr(self.roads, "rev", 0) if self.roads is not None else 0
        sig = (roads_rev, tuple((f.name, f.status["at"], id(f.items)) for f in self.feeds))
        with self._lock:
            if self._out is not None and self._out[0] == sig:
                return self._out[1]
            cands = self._roads_items() + [dict(r) for f in self.feeds for r in f.items]
        cands = [r for r in cands if r.get("geometry") is not None]
        rank = lambda r: SOURCE_RANK.get(r["source"], 3 if r["kind"] == "notice" else 1)
        cands.sort(key=lambda r: (rank(r), r["id"]))
        kept: list[dict] = []
        for r in cands:
            twin = None if r["kind"] == "notice" else \
                next((k for k in kept if k["kind"] != "notice" and feed_of(k) != feed_of(r) and same_thing(k, r)), None)
            if twin is not None:
                tag = r["source"] if r["source"] != twin["source"] else f"{r['source']} ({feed_of(r)})"
                twin.setdefault("also", [])
                if tag not in twin["also"]:
                    twin["also"].append(tag)
                continue
            kept.append(r)
        with self._lock:
            self._out = (sig, kept)
        return kept

    def notices(self) -> list[dict]:
        return [n for f in self.feeds for n in f.notices]

    def sources(self) -> list[dict]:
        return self._roads_sources() + [f.public() for f in self.feeds]

    def public(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        items = []
        for r in self.items():
            w = windows(r, now)
            if not w:
                continue                                          # ended
            items.append(r | {"windows": w, "state": state(w, now)})
        return {"configured": self.configured, "note": self.why_not(), "tz": TZ_NAME, "at": now,
                "sources": self.sources(), "items": items,
                "notices": [n | {"windows": windows(n, now)} for n in self.notices()]}

    def near(self, lat: float, lon: float, address: str = "", t0: float | None = None, t1: float | None = None) -> dict:
        """The incident view's lines: items in effect at some time in [t0, t1] (default now) within NEAR_M of the
        point, or on the incident's own street within STREET_M; nearest first."""
        now = self.clock()
        t1 = now if t1 is None else t1
        t0 = min(t1, now if t0 is None else t0)
        street = street_of(address)
        out = []
        for r in self.items():
            d = distance_m(r["geometry"], lat, lon)
            on_street = bool(street) and street_key(r.get("road") or "") == street
            if not (d <= NEAR_M or (on_street and d <= STREET_M)):
                continue
            win = in_effect(r, t0, t1)
            if win is None:
                continue
            active = state([win], now) == "active"
            how = f"{round(d / 10) * 10:.0f} m away" if d > 15 else "at the location"
            if d > NEAR_M:
                how = f"on {r['road']}, {how}"
            out.append({"id": r["id"], "text": note_line(r, win, now), "source": r["source"], "distance_m": round(d),
                        "how": how, "active_now": active, "closure": r.get("closure") or "", "kind": r["kind"]})
        # closures in effect now (a full closure first), then the rest of the closures and roadwork, the notices, the projects
        rank = lambda x: {"notice": 2, "project": 3}.get(x["kind"], 0 if x["active_now"] else 1)
        out.sort(key=lambda x: (rank(x), x["closure"] != "full", x["distance_m"]))
        return {"configured": self.configured, "items": out[:6]}

    # -- HTTP ------------------------------------------------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        return path in ("/api/cad/roadprojects", NEAR_PATH)

    def get(self, path: str, query: str, headers=None) -> tuple[int, bytes, str, dict]:
        q = parse_qs(query or "")
        one = lambda k: (q.get(k) or [""])[0].strip()
        self.ask()
        if path == "/api/cad/roadprojects":
            body = json.dumps(self.public(), separators=(",", ":"), default=str).encode()
            # the same items in the same minute -> the same ETag: the page's next ask gets a 304
            sig = json.dumps(json.loads(body) | {"at": None}, separators=(",", ":"), sort_keys=True).encode()
            return 200, body, JSON, {"ETag": '"' + hashlib.sha1(sig).hexdigest()[:16] + '"', "Cache-Control": "no-cache"}
        if path == NEAR_PATH:
            try:
                lat, lon = float(one("lat")), float(one("lon"))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
                t0 = float(one("from")) if one("from") else None
                t1 = float(one("to")) if one("to") else None
            except ValueError:
                return 400, b'{"error":"lat, lon: numbers; from, to: unix seconds"}', JSON, {}
            return 200, json.dumps(self.near(lat, lon, one("address")[:200], t0, t1), separators=(",", ":")).encode(), JSON, \
                {"Cache-Control": "no-store"}
        return 404, b'{"error":"not found"}', JSON, {}


def feed_of(r: dict) -> str:
    return str(r.get("id", "")).split(":", 1)[0]


def same_thing(a: dict, b: dict) -> bool:
    """Two sources' records of one work zone: the same road, within DEDUPE_M, at overlapping times."""
    ka, kb = street_key(a.get("road") or ""), street_key(b.get("road") or "")
    if not ka or ka != kb:
        return False
    pa, pb = representative(a["geometry"]), representative(b["geometry"])
    if pa is None or pb is None:
        return False
    if min(distance_m(a["geometry"], *pb), distance_m(b["geometry"], *pa)) > DEDUPE_M:
        return False
    sa, ea, sb, eb = a.get("start"), a.get("end"), b.get("start"), b.get("end")
    return (sa is None or eb is None or sa < eb) and (sb is None or ea is None or sb < ea)
