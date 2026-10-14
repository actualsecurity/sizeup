"""Weather and water for the CAD (GitHub #132): the National Weather Service's alerts for our zones, the
current conditions at the nearest observation station, NOAA's radar for the maps' Layers, and NOAA's tides
for marine calls. Public data, read by the CAD of record (the hub, or all-in-one; never a station, which has
no maps), cached, and served to the pages from here: the browser never talks to NOAA.

User (#132): "Public, free data: National Weather Service alerts ... as a banner on /cad and /monitor:
warnings/advisories with severity colour; spoken once for warnings. Current conditions ... in the header;
wind arrow on the map near an active fire. A radar layer ... NOAA tides & currents for the nearest stations
... on Marine 40 / water-rescue events and in the marine section of the board. All through the hub (cache,
shared USER_AGENT)."

Sources (docs/cloud.md "Weather and water" has the terms; surveyed 2026-10-14):

  Alerts      api.weather.gov /alerts/active?zone=..., every 2 min. Port Chester (41.0047, -73.6672) is in
              forecast zone NYZ071 (Southern Westchester), county zone NYC119 (Westchester) and fire weather
              zone NYZ211 (a Red Flag Warning is issued there); the harbour opens on marine zone ANZ335 (Long
              Island Sound West of New Haven CT / Port Jefferson NY): /points/41.00,-73.67 names the first three.
              One request for all four. A marine-only alert (a Small Craft Advisory) goes to the marine block,
              not the banner. Kept: event, severity, urgency, certainty, headline ("... issued October 14 at 2:05PM
              EDT until ... by NWS Upton NY") and the NWS headline (its capitals), description, instruction,
              onset, ends / expires, the area, the sender. Only status Actual;
              a Cancel, an expired alert and one another alert replaces (its references) are dropped.
  Conditions  api.weather.gov /stations/KHPN/observations/latest, every 10 min: Westchester County Airport,
              the first of the gridpoint's (OKX 43,57) observation stations and 7 km from Port Chester's centre.
              Temperature, wind speed, gust and direction, humidity, visibility, the sky in words, its time.
  Tides       NOAA CO-OPS (api.tidesandcurrents.noaa.gov). No CO-OPS station is in Port Chester harbour or
              the Byram River; the nearest:
                8518091 Rye Beach NY, 4 km south (Playland): a harmonic prediction station (no gauge). Its
                        high / low and hourly predictions (MLLW, ft) give the level now, rising or falling and
                        the next high and low. Fetched every 6 h (they don't change), 3 days around now.
                8516945 Kings Point NY, 23 km: the nearest live water-level gauge on the Sound. Its latest
                        reading every 10 min, and its own 6-min predictions, give how far the water is above
                        or below the tide table (wind, surge) right now.
              All in GMT (no daylight-saving ambiguity); the pages show local time.
  Radar       NOAA / NWS MRMS base reflectivity (the quality-controlled CONUS composite radar.weather.gov
              draws), from the NWS's OGC service opengeo.ncep.noaa.gov (layer conus/conus_bref_qcd; WMS
              Fees "none", AccessConstraints "none"; time-enabled, a frame every 2 min for 2 h). The hub
              fetches 256 px tiles (GetMap, EPSG:3857) as the maps ask for them, caches them (the latest for
              RADAR_NOW_TTL_S, a past frame for RADAR_FRAME_TTL_S) and serves them; the frame list (for the
              optional animation) from GetCapabilities, at most every RADAR_FRAMES_S.

Every request names us (sizeup.USER_AGENT: api.weather.gov requires a User-Agent with contact details) and
CO-OPS's asks for an application name (application=sizeup_pcfd). Each feed has its own schedule and backoff
(1 min doubling to 15; 30 min after a 403), and one disk cache
(state dir, weather.json) holds what was read, so a restart spends no calls.

Spoken warnings: a Warning (not a watch, advisory or statement) is said once through the dispatch voice,
played by the console's browser where it is open (and the wall board with its Sound on): never keyed on a
radio (transmit stays off). Off unless Settings > Dispatch > Weather warnings turns it on (the CAD's
weather_speak; speak_on() here). The hub renders it with the CAD's own voice (speaker()).

HTTP (sizeup/web/server.py; any signed-in person, viewers too; GET only):
    GET /api/cad/weather                {configured, alerts, marine_alerts, conditions, tides, radar, speak, sources, ...}
    GET /api/cad/weather/speech?id=     {id, text, audio_url, audio_note}: an active Warning said by the dispatch
                                        voice (403 while Settings has it off, 404 for anything else)
    GET /api/cad/weather/radar/frames   {frames: [{id, t}], at, error}: the animation's frames (oldest first)
    GET /api/cad/weather/radar/<frame>/<z>/<x>/<y>.png   a radar tile ("now", or a frame id from the list)
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sizeup import USER_AGENT as UA

TZ = ZoneInfo("America/New_York")
NWS_API = "https://api.weather.gov"
NWS_LINK = "https://www.weather.gov/okx/"
COOPS_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
COOPS_LINK = "https://tidesandcurrents.noaa.gov/stationhome.html?id={id}"
COOPS_APP = "sizeup_pcfd"
RADAR_WMS = "https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows"
RADAR_LAYER = "conus_bref_qcd"
RADAR_LINK = "https://radar.weather.gov/"
RADAR_ATTRIBUTION = '<a href="https://radar.weather.gov/" target="_blank" rel="noopener">NOAA/NWS MRMS radar</a>'

POINT = (41.0047, -73.6672)            # Port Chester, as api.weather.gov /points names it
ZONES = ("NYZ071", "NYC119", "NYZ211")  # Southern Westchester (forecast), Westchester (county), fire weather
MARINE_ZONES = ("ANZ335",)             # Long Island Sound West of New Haven CT / Port Jefferson NY
AREA = "Southern Westchester"          # how the spoken warning names our area
STATION = "KHPN"                       # Westchester County Airport
STATION_NAME = "Westchester County Airport"
TIDE_STATION = ("8518091", "Rye Beach")  # predictions: high / low and hourly
GAUGE_STATION = ("8516945", "Kings Point")  # the nearest live water-level gauge

FEEDS = ("alerts", "conditions", "tides", "predictions")
EVERY_S = {"alerts": 120.0, "conditions": 600.0, "tides": 600.0, "predictions": 6 * 3600.0}
BACKOFF_S = (60.0, 900.0)              # a failed fetch: 1 min doubling to 15 (never sooner than the feed's cadence)
REFUSED_S = 1800.0                     # 403: the service refused us (the User-Agent?): 30 min
TIMEOUT_S = 20.0
MAX_BYTES = 8 * 1024 * 1024
STALE_OBS_S = 2 * 3600.0               # an observation older than this is shown as old
STALE_GAUGE_S = 3600.0
PREDICT_DAYS = (1, 2)                  # predictions from a day before today to two days after (GMT dates)
TEXT_MAX = 8000                        # an alert's description / instruction, at most
LEVELS = ("warning", "watch", "advisory", "statement")
TONE = {"warning": "err", "watch": "warn", "advisory": "warn", "statement": "info"}   # the banner's colour
# NWS marine zone prefixes (coastal and offshore waters, the Great Lakes): an alert only for these is marine
MARINE_PREFIXES = ("AM", "AN", "GM", "LC", "LE", "LH", "LM", "LO", "LS", "PH", "PK", "PM", "PS", "PZ", "SL")
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")

RADAR_NOW_TTL_S = 120.0                # the latest radar tile, served this long (the composite is every 2 min)
RADAR_FRAME_TTL_S = 3600.0             # a past frame's tile never changes
RADAR_FRAMES_S = 300.0                 # the frame list, at most this often
RADAR_KEEP = 600                       # tiles kept in memory (least recently used dropped)
RADAR_PARALLEL = 6
RADAR_MINZOOM, RADAR_MAXZOOM = 3, 10   # the composite is 1 km: the maps overzoom past 10
RADAR_TIMEOUT_S = 15.0
RADAR_MAX_BYTES = 2 * 1024 * 1024
RADAR_FRAMES, RADAR_STEP_S = 7, 600    # the animation: a frame every 10 min over the last hour
FRAME_ID = re.compile(r"\d{8}T\d{6}Z")
RADAR_PATH = re.compile(r"/api/cad/weather/radar/(now|\d{8}T\d{6}Z)/(\d{1,2})/(\d{1,7})/(\d{1,7})\.png")
JSON = "application/json"


# ---- small helpers ------------------------------------------------------------------------------------------


def _iso(v) -> float | None:
    """ISO 8601 (with its offset) -> unix seconds; None for anything else."""
    if not v or not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _gmt(v: str) -> float:
    """CO-OPS's "2026-10-14 00:16" (time_zone=gmt) -> unix seconds."""
    return datetime.strptime(v.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clip(v, n: int) -> str:
    t = str(v or "").strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def compass(deg: float | None) -> str:
    """Degrees (where the wind comes from) -> "NW"; None -> ""."""
    if deg is None:
        return ""
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def _redact(text: str) -> str:
    return re.sub(r"\s+", " ", str(text))[:300]


def http_get(url: str, timeout: float, limit: int) -> tuple[int, dict, bytes]:
    """(status, headers, body): urllib, GET only, as us. Errors as HTTPError / URLError / OSError / ValueError."""
    accept = "image/png" if "GetMap" in url else "application/geo+json, application/json;q=0.9, */*;q=0.5"
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"more than {limit // (1024 * 1024)} MB")
        return r.status, {k.lower(): v for k, v in r.headers.items()}, body


def _json(body: bytes):
    doc = json.loads(body.decode("utf-8-sig", errors="replace"))
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):        # CO-OPS answers 200 with {"error": {message}}
        raise ValueError(f"CO-OPS: {doc['error'].get('message') or 'error'}")
    return doc


# ---- alerts --------------------------------------------------------------------------------------------------


def alerts_url(zones=ZONES, marine_zones=MARINE_ZONES) -> str:
    return f"{NWS_API}/alerts/active?" + urlencode({"zone": ",".join([*zones, *marine_zones])})


def level_of(event: str) -> str:
    """'Severe Thunderstorm Warning' -> warning; '... Watch' -> watch; '... Advisory' / 'Air Quality Alert' ->
    advisory; anything else (a statement, an outlook, a message) -> statement."""
    e = (event or "").strip().lower()
    if e.endswith("warning") or e.endswith("emergency"):
        return "warning"
    if e.endswith("watch"):
        return "watch"
    if e.endswith("advisory") or e.endswith(" alert"):
        return "advisory"
    return "statement"


def is_marine_zone(code: str) -> bool:
    return len(code) == 6 and code[2] == "Z" and code[:2] in MARINE_PREFIXES


def _zones_of(p: dict) -> list[str]:
    codes = list((p.get("geocode") or {}).get("UGC") or [])
    codes += [str(z).rstrip("/").rsplit("/", 1)[-1] for z in p.get("affectedZones") or []]
    return sorted({c for c in codes if isinstance(c, str) and re.fullmatch(r"[A-Z]{2}[CZ]\d{3}", c)})


def parse_alerts(doc, now: float) -> list[dict]:
    """api.weather.gov's /alerts/active (GeoJSON) -> the alerts in force at `now`, most serious first (level,
    then severity, then onset). Only status Actual; a Cancel, an expired one and one another replaces are left out."""
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise ValueError("not an alerts collection")
    replaced, out = set(), []
    for f in feats:
        p = (f or {}).get("properties") or {}
        for r in p.get("references") or []:
            if isinstance(r, dict) and r.get("identifier"):
                replaced.add(r["identifier"])
    for f in feats:
        p = (f or {}).get("properties") or {}
        aid = str(p.get("id") or (f or {}).get("id") or "").strip()
        if not aid or p.get("status") != "Actual" or p.get("messageType") == "Cancel" or aid in replaced:
            continue
        ends = _iso(p.get("ends")) or _iso(p.get("expires"))
        if ends is not None and ends <= now:
            continue
        event = _clip(p.get("event"), 80) or "Alert"
        params = p.get("parameters") or {}
        nws_head = (params.get("NWSheadline") or [""])[0] if isinstance(params.get("NWSheadline"), list) else ""
        zones = _zones_of(p)
        out.append({
            "id": aid, "event": event, "level": level_of(event), "severity": p.get("severity") or "Unknown",
            "urgency": p.get("urgency") or "Unknown", "certainty": p.get("certainty") or "Unknown",
            "headline": _clip(p.get("headline") or nws_head or event, 400), "nws_headline": _clip(nws_head, 400),
            "description": _clip(p.get("description"), TEXT_MAX), "instruction": _clip(p.get("instruction"), TEXT_MAX),
            "area": _clip(p.get("areaDesc"), 600), "sender": _clip(p.get("senderName"), 120),
            "sent": _iso(p.get("sent")), "onset": _iso(p.get("onset")) or _iso(p.get("effective")),
            "ends": _iso(p.get("ends")), "expires": _iso(p.get("expires")), "until": ends,
            "message_type": p.get("messageType") or "", "zones": zones,
            "marine": bool(zones) and all(is_marine_zone(z) for z in zones),
        })
    sev = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    out.sort(key=lambda a: (LEVELS.index(a["level"]), sev.get(a["severity"], 4), a["onset"] or 0, a["event"]))
    return out


def active(alerts: list[dict], now: float) -> list[dict]:
    """The alerts still in force at `now` (a cached list ages)."""
    return [a for a in alerts if a.get("until") is None or a["until"] > now]


def banner_state(alerts: list[dict]) -> dict:
    """What the banner shows: {tone (err | warn | info | ""), count, top (the most serious alert) | None,
    levels: {warning: n, ...}}. tone "" = no banner."""
    land = [a for a in alerts if not a.get("marine")]
    levels = {k: sum(1 for a in land if a["level"] == k) for k in LEVELS}
    top = land[0] if land else None
    return {"tone": TONE[top["level"]] if top else "", "count": len(land), "top": top, "levels": levels}


def spoken(alert: dict, area: str = AREA, now: float | None = None) -> str:
    """The warning as the dispatch voice says it: 'Weather alert. The National Weather Service has issued a
    Severe Thunderstorm Warning for Southern Westchester, until 1745.' (numbers the dispatch way, sizeup/cad/speech.py)."""
    text = f"Weather alert. The National Weather Service has issued a {alert['event']} for {area}"
    until = alert.get("until")
    if until:
        d = datetime.fromtimestamp(until, TZ)
        today = datetime.fromtimestamp(now if now is not None else time.time(), TZ).date()
        day = "" if d.date() == today else ("tomorrow " if d.date() == today + timedelta(days=1) else d.strftime("%A "))
        text += f", until {day}{d:%H%M}"
    return text + "."


# ---- conditions ----------------------------------------------------------------------------------------------

_SPEED = {"wmoUnit:km_h-1": 0.621371, "wmoUnit:m_s-1": 2.236936, "wmoUnit:kn": 1.150779}


def _qv(p: dict, key: str) -> tuple[float | None, str]:
    q = p.get(key) or {}
    if not isinstance(q, dict) or q.get("qualityControl") == "X":        # X: rejected by the NWS's checks
        return None, ""
    return _num(q.get("value")), str(q.get("unitCode") or "")


def parse_observation(doc) -> dict:
    """/stations/<id>/observations/latest -> {at, station, text, temp_f, humidity, wind_mph, gust_mph, wind_deg,
    wind_dir, visibility_mi, short ("54°F · NW 12 G 22 mph")}."""
    p = doc.get("properties") if isinstance(doc, dict) else None
    if not isinstance(p, dict) or not p.get("timestamp"):
        raise ValueError("not an observation")
    t, tu = _qv(p, "temperature")
    temp = None if t is None else (t * 9 / 5 + 32 if tu.endswith("degC") else t)
    speed = []
    for k in ("windSpeed", "windGust"):
        v, u = _qv(p, k)
        speed.append(None if v is None else v * _SPEED.get(u, 0.621371))
    deg, _ = _qv(p, "windDirection")
    hum, _ = _qv(p, "relativeHumidity")
    vis, vu = _qv(p, "visibility")
    out = {"at": _iso(p["timestamp"]), "station": str(p.get("station") or "").rsplit("/", 1)[-1] or STATION,
           "text": _clip(p.get("textDescription"), 80),
           "temp_f": None if temp is None else round(temp),
           "humidity": None if hum is None else round(hum),
           "wind_mph": None if speed[0] is None else round(speed[0]),
           "gust_mph": None if speed[1] is None else round(speed[1]),
           "wind_deg": None if deg is None else round(deg) % 360,
           "visibility_mi": None if vis is None else round(vis / 1609.344 if vu.endswith(":m") else vis, 1)}
    out["wind_dir"] = compass(out["wind_deg"]) if out["wind_mph"] else ""
    out["wind"] = wind_text(out)
    out["short"] = " · ".join(x for x in (f"{out['temp_f']}°F" if out["temp_f"] is not None else "", out["wind"]) if x)
    return out


def wind_text(c: dict) -> str:
    """'NW 12 G 22 mph', 'Calm', 'Var 4 mph', '' (no wind reported)."""
    w, g = c.get("wind_mph"), c.get("gust_mph")
    if w is None:
        return ""
    if w == 0 and not g:
        return "Calm"
    s = f"{c.get('wind_dir') or 'Var'} {w}"
    if g and g > w:
        s += f" G {g}"
    return s + " mph"


# ---- tides ---------------------------------------------------------------------------------------------------


def coops_url(product: str, station: str, **params) -> str:
    q = {"product": product, "station": station, "units": "english", "time_zone": "gmt", "format": "json",
         "application": COOPS_APP, **params}
    if product in ("predictions", "water_level"):
        q.setdefault("datum", "MLLW")
    return f"{COOPS_API}?{urlencode(q)}"


def predict_range(now: float) -> dict:
    d = datetime.fromtimestamp(now, timezone.utc).date()
    return {"begin_date": (d - timedelta(days=PREDICT_DAYS[0])).strftime("%Y%m%d"),
            "end_date": (d + timedelta(days=PREDICT_DAYS[1])).strftime("%Y%m%d")}


def parse_predictions(doc) -> list[list]:
    """{"predictions": [{t, v[, type]}]} -> [[unix, ft, "H" | "L" | ""], ...] in time order."""
    rows = doc.get("predictions") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise ValueError("no predictions")
    out = []
    for r in rows:
        v = _num((r or {}).get("v"))
        if v is None or not r.get("t"):
            continue
        out.append([_gmt(r["t"]), round(v, 2), str(r.get("type") or "")])
    out.sort(key=lambda x: x[0])
    return out


def parse_water_level(doc) -> dict:
    """{"metadata": {...}, "data": [{t, v, ...}]} (date=latest) -> {at, ft}."""
    rows = doc.get("data") if isinstance(doc, dict) else None
    r = next((x for x in reversed(rows or []) if _num((x or {}).get("v")) is not None), None)
    if r is None:
        raise ValueError("no water level")
    return {"at": _gmt(r["t"]), "ft": round(_num(r["v"]), 2)}


def interpolate(series: list[list], t: float) -> float | None:
    """The series' value at t (linear between its points); None outside it."""
    if not series or t < series[0][0] or t > series[-1][0]:
        return None
    for (t0, v0, *_), (t1, v1, *_) in zip(series, series[1:]):
        if t0 <= t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return series[-1][1]


def tide_summary(hilo: list[list], hourly: list[list], gauge: dict | None, gauge_pred: list[list], now: float,
                 station=TIDE_STATION, gauge_station=GAUGE_STATION) -> dict | None:
    """What the marine block shows: {station, name, ft (predicted now), trend (rising | falling), next: [{type
    (high | low), t, ft}] (the next two), last, hours: [[t, ft]] (the next 24 h), gauge: {station, name, ft, at,
    predicted_ft, offset_ft, stale} | None}. None when the predictions don't cover now."""
    nxt = [x for x in hilo if x[0] > now and x[2] in ("H", "L")]
    if not nxt:
        return None
    prev = [x for x in hilo if x[0] <= now and x[2] in ("H", "L")]
    ft = interpolate(hourly, now)
    ext = lambda x: {"type": "high" if x[2] == "H" else "low", "t": x[0], "ft": x[1]}
    out = {"station": station[0], "name": station[1], "link": COOPS_LINK.format(id=station[0]),
           "ft": None if ft is None else round(ft, 1), "trend": "rising" if nxt[0][2] == "H" else "falling",
           "next": [ext(x) for x in nxt[:2]], "last": ext(prev[-1]) if prev else None,
           "hours": [[t, round(v, 1)] for t, v, *_ in hourly if now - 3600 <= t <= now + 24 * 3600],
           "datum": "MLLW", "gauge": None}
    if gauge and gauge.get("at"):
        pred = interpolate(gauge_pred, gauge["at"])
        out["gauge"] = {"station": gauge_station[0], "name": gauge_station[1], "link": COOPS_LINK.format(id=gauge_station[0]),
                        "ft": round(gauge["ft"], 1), "at": gauge["at"],
                        "predicted_ft": None if pred is None else round(pred, 1),
                        "offset_ft": None if pred is None else round(gauge["ft"] - pred, 1),
                        "stale": now - gauge["at"] > STALE_GAUGE_S}
    return out


# ---- radar ---------------------------------------------------------------------------------------------------


def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """A web-mercator tile's bounds in EPSG:3857 metres (west, south, east, north)."""
    r = 6378137.0 * math.pi
    s = 2 * r / (1 << z)
    return (-r + x * s, r - (y + 1) * s, -r + (x + 1) * s, r - y * s)


def frame_id(iso: str) -> str:
    """'2026-10-14T15:36:13.000Z' -> '20261014T153613Z'."""
    return datetime.fromtimestamp(_iso(iso), timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def frame_iso(fid: str) -> str:
    return datetime.strptime(fid, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%S.000Z")


def radar_url(z: int, x: int, y: int, frame: str = "now") -> str:
    w, s, e, n = tile_bbox(z, x, y)
    q = {"service": "WMS", "version": "1.3.0", "request": "GetMap", "layers": RADAR_LAYER, "styles": "",
         "crs": "EPSG:3857", "bbox": f"{w:.2f},{s:.2f},{e:.2f},{n:.2f}", "width": "256", "height": "256",
         "format": "image/png", "transparent": "true"}
    if frame != "now":
        q["time"] = frame_iso(frame)
    return f"{RADAR_WMS}?{urlencode(q)}"


def parse_frames(xml: bytes, n: int = RADAR_FRAMES, step_s: float = RADAR_STEP_S) -> list[dict]:
    """GetCapabilities -> the animation's frames, oldest first: the latest and one about every step_s before
    it (the nearest the service has, within a third of a step), n in all. [{id, t}]."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ValueError(f"bad capabilities: {e}") from None
    times: list[float] = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "Dimension" and el.get("name", "").lower() == "time" and el.text:
            times = sorted(t for t in (_iso(x.strip()) for x in el.text.split(",")) if t is not None)
    if not times:
        raise ValueError("no radar times in the capabilities")
    picked = [times[-1]]
    for k in range(1, n):
        want = times[-1] - k * step_s
        best = min(times, key=lambda t: abs(t - want))
        if abs(best - want) <= step_s / 3 and best not in picked:
            picked.append(best)
    picked.sort()
    return [{"id": datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%SZ"), "t": t} for t in picked]


# ---- the provider --------------------------------------------------------------------------------------------


class Weather:
    def __init__(self, zones=ZONES, marine_zones=MARINE_ZONES, station: str = STATION, tide_station=TIDE_STATION,
                 gauge_station=GAUGE_STATION, area: str = AREA, cache: str | Path | None = None, enabled: bool = True,
                 radar: bool = True, fetch: Callable[[str, float, int], tuple[int, dict, bytes]] = http_get,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.zones, self.marine_zones = tuple(zones), tuple(marine_zones)
        self.station, self.area = station, area
        self.tide_station, self.gauge_station = tuple(tide_station), tuple(gauge_station)
        self.cache = Path(cache) if cache else None
        self.enabled, self.radar_on = enabled, radar
        self.fetch, self.log, self.clock = fetch, log, clock
        self.status = {f: {"at": None, "error": "", "next": 0.0, "fails": 0} for f in FEEDS}
        self.alerts: list[dict] = []
        self.conditions: dict | None = None
        self.hilo: list[list] = []
        self.hourly: list[list] = []
        self.gauge: dict | None = None
        self.gauge_pred: list[list] = []
        self.frames: list[dict] = []
        self.frames_status = {"at": None, "error": "", "next": 0.0, "fails": 0}
        self.speak_on: Callable[[], bool] = lambda: False        # the CAD's Settings > Dispatch > Weather warnings
        self.speaker: Callable[[str], tuple[str | None, str]] | None = None   # text -> (audio url, note)
        self._spoken: dict[str, dict] = {}
        self._speak_lock = threading.Lock()
        self._tiles: OrderedDict[tuple, dict] = OrderedDict()
        self._tile_lock = threading.Lock()
        self._frames_lock = threading.Lock()
        self._parallel = threading.BoundedSemaphore(RADAR_PARALLEL)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rev = 0
        self._load()

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print) -> Weather:
        state = Path(cfg.cad_db).parent if cfg.cad_db else None
        return cls(cfg.weather_zones, cfg.weather_marine_zones, cfg.weather_station, cfg.weather_tide_station,
                   cfg.weather_gauge_station, cfg.weather_area, (state / "weather.json") if state else None,
                   enabled=cfg.weather_enabled, radar=cfg.weather_radar, log=log)

    @property
    def configured(self) -> bool:
        return self.enabled

    # -- the schedule ------------------------------------------------------------------------------------------

    def start(self) -> Weather:
        if not self.enabled:
            self.log("weather: off ([weather] enabled = false): no alerts, conditions, radar or tides")
            return self
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="weather", daemon=True)
            self._thread.start()
            self.log(f"weather: NWS alerts for {','.join(self.zones + self.marine_zones)} every {EVERY_S['alerts']:.0f} s, "
                     f"{self.station} every {EVERY_S['conditions']:.0f} s, tides {self.tide_station[1]} / "
                     f"{self.gauge_station[1]} every {EVERY_S['tides']:.0f} s" + (", radar on request" if self.radar_on else ""))
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            for feed in self.due():
                if self._stop.is_set():
                    break
                self.refresh(feed)
            self._stop.wait(5.0)

    def due(self) -> list[str]:
        if not self.enabled:
            return []
        now = self.clock()
        return [f for f in FEEDS if now >= self.status[f]["next"]]

    def refresh(self, feed: str) -> bool:
        """Fetch one feed now; True when it was read. Never raises."""
        if not self.enabled or feed not in FEEDS:
            return False
        now = self.clock()
        try:
            getattr(self, f"_read_{feed}")(now)
        except HTTPError as e:
            return self._failed(feed, now, e.code, f"HTTP {e.code}")
        except (URLError, OSError, ValueError, KeyError, TypeError) as e:
            return self._failed(feed, now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}")
        except Exception as e:  # noqa: BLE001 - a surprise in one feed must not stop the others
            return self._failed(feed, now, 0, f"{type(e).__name__}: {e}")
        st = self.status[feed]
        st.update(at=now, error="", fails=0, next=now + EVERY_S[feed])
        self._changed()
        self._save()
        return True

    def _get(self, url: str):
        code, _, body = self.fetch(url, TIMEOUT_S, MAX_BYTES)
        if code != 200:
            raise HTTPError(url, code, f"HTTP {code}", None, None)
        return _json(body)

    def _read_alerts(self, now: float) -> None:
        self.alerts = parse_alerts(self._get(alerts_url(self.zones, self.marine_zones)), now)

    def _read_conditions(self, now: float) -> None:
        self.conditions = parse_observation(self._get(f"{NWS_API}/stations/{self.station}/observations/latest"))

    def _read_tides(self, now: float) -> None:
        self.gauge = parse_water_level(self._get(coops_url("water_level", self.gauge_station[0], date="latest")))

    def _read_predictions(self, now: float) -> None:
        rng = predict_range(now)
        hilo = parse_predictions(self._get(coops_url("predictions", self.tide_station[0], interval="hilo", **rng)))
        hourly = parse_predictions(self._get(coops_url("predictions", self.tide_station[0], interval="h", **rng)))
        gp = parse_predictions(self._get(coops_url("predictions", self.gauge_station[0], interval="6", **rng)))
        self.hilo, self.hourly, self.gauge_pred = hilo, hourly, gp

    def _failed(self, feed: str, now: float, code: int, why: str, st: dict | None = None) -> bool:
        st = st if st is not None else self.status[feed]
        st["fails"] += 1
        why = _redact(why)
        if code == 403:
            wait, why = REFUSED_S, f"refused ({why})"
        else:
            wait = max(min(EVERY_S.get(feed, RADAR_FRAMES_S), BACKOFF_S[0]),
                       min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** (st["fails"] - 1)))
        changed = st["error"] != why
        st.update(error=why, next=now + wait)
        if changed:
            self._changed()
        self.log(f"weather: {feed}: {why}; next try in {wait:.0f} s")
        return False

    def _changed(self) -> None:
        self.rev += 1

    # -- the disk cache ----------------------------------------------------------------------------------------

    def _signature(self) -> list:
        return [list(self.zones), list(self.marine_zones), self.station, list(self.tide_station), list(self.gauge_station)]

    def _load(self) -> None:
        if not self.cache or not self.cache.is_file():
            return
        try:
            doc = json.loads(self.cache.read_text())
            if doc.get("signature") != self._signature():
                return                                    # other zones or stations: fetch afresh
            now = self.clock()
            for f in FEEDS:
                at = (doc.get("at") or {}).get(f)
                if isinstance(at, (int, float)) and at <= now + 60:
                    self.status[f].update(at=at, next=at + EVERY_S[f])
            self.alerts = [a for a in doc.get("alerts") or [] if isinstance(a, dict) and a.get("id")]
            self.conditions = doc.get("conditions") if isinstance(doc.get("conditions"), dict) else None
            for k in ("hilo", "hourly", "gauge_pred"):
                setattr(self, k, [list(x) for x in doc.get(k) or [] if isinstance(x, list) and len(x) >= 2])
            self.gauge = doc.get("gauge") if isinstance(doc.get("gauge"), dict) else None
            self._changed()
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.log(f"weather: cache {self.cache.name} not read ({type(e).__name__}); fetching afresh")

    def _save(self) -> None:
        if not self.cache:
            return
        doc = {"signature": self._signature(), "at": {f: self.status[f]["at"] for f in FEEDS}, "alerts": self.alerts,
               "conditions": self.conditions, "hilo": self.hilo, "hourly": self.hourly, "gauge": self.gauge,
               "gauge_pred": self.gauge_pred}
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(self.cache)
        except OSError as e:
            self.log(f"weather: cache {self.cache.name} not saved ({type(e).__name__}: {e})")

    # -- what the pages get ------------------------------------------------------------------------------------

    def _speak(self) -> bool:
        try:
            return bool(self.speak_on())
        except Exception:  # noqa: BLE001 - a setting that can't be read is off
            return False

    def current(self, now: float | None = None) -> dict | None:
        c = self.conditions
        if not c:
            return None
        now = self.clock() if now is None else now
        return dict(c, stale=bool(c.get("at")) and now - c["at"] > STALE_OBS_S, name=STATION_NAME if c.get("station") == STATION else c.get("station"))

    def public(self) -> dict:
        now = self.clock()
        alerts = active(self.alerts, now)
        land = [a for a in alerts if not a["marine"]]
        src = lambda key, label, link, feeds: {"key": key, "label": label, "link": link,
                                               "at": max((self.status[f]["at"] or 0) for f in feeds) or None,
                                               "error": next((self.status[f]["error"] for f in feeds if self.status[f]["error"]), "")}
        return {"configured": self.configured, "rev": self.rev, "time": now, "zones": list(self.zones),
                "marine_zones": list(self.marine_zones), "area": self.area,
                "alerts": land, "marine_alerts": [a for a in alerts if a["marine"]], "banner": banner_state(land),
                "conditions": self.current(now),
                "tides": tide_summary(self.hilo, self.hourly, self.gauge, self.gauge_pred, now, self.tide_station, self.gauge_station),
                "radar": {"on": self.radar_on and self.enabled, "tiles": "/api/cad/weather/radar/{frame}/{z}/{x}/{y}.png",
                          "minzoom": RADAR_MINZOOM, "maxzoom": RADAR_MAXZOOM, "attribution": RADAR_ATTRIBUTION,
                          "link": RADAR_LINK, "label": "NOAA/NWS MRMS base reflectivity"},
                "speak": self._speak(),
                "link": f"https://forecast.weather.gov/MapClick.php?lat={POINT[0]}&lon={POINT[1]}",
                "sources": [src("nws", "National Weather Service", NWS_LINK, ("alerts", "conditions")),
                            src("coops", "NOAA Tides & Currents", COOPS_LINK.format(id=self.tide_station[0]), ("tides", "predictions"))]}

    def speech(self, aid: str) -> tuple[int, dict]:
        """An active Warning as the dispatch voice says it (rendered once, kept): (200, {id, text, audio_url,
        audio_note}); 403 while Settings has it off; 404 for anything that isn't an active warning."""
        if not self._speak():
            return 403, {"error": "spoken weather warnings are off (Settings > Dispatch > Weather warnings)"}
        a = next((x for x in active(self.alerts, self.clock()) if x["id"] == aid), None)
        if a is None or a["level"] != "warning" or a["marine"]:
            return 404, {"error": "no such active warning"}
        with self._speak_lock:
            hit = self._spoken.get(aid)
            if hit is None:
                text = spoken(a, self.area, self.clock())
                url, note = (None, "no dispatch voice here")
                if self.speaker is not None:
                    try:
                        url, note = self.speaker(text)
                    except Exception as e:  # noqa: BLE001 - the text stands without its audio
                        url, note = None, f"{type(e).__name__}: {e}"
                hit = {"id": aid, "text": text, "audio_url": url, "audio_note": note}
                if url:
                    self._spoken[aid] = hit
                    for k in list(self._spoken)[:-50]:
                        self._spoken.pop(k, None)
        return 200, hit

    # -- radar -------------------------------------------------------------------------------------------------

    def radar_frames(self) -> dict:
        """The animation's frames (GetCapabilities at most every RADAR_FRAMES_S; a failure backs off)."""
        if not (self.radar_on and self.enabled):
            return {"frames": [], "at": None, "error": "radar off"}
        st = self.frames_status
        with self._frames_lock:
            now = self.clock()
            if now >= st["next"]:
                try:
                    code, _, body = self.fetch(f"{RADAR_WMS}?" + urlencode({"service": "WMS", "version": "1.3.0",
                                                                           "request": "GetCapabilities"}), TIMEOUT_S, MAX_BYTES)
                    if code != 200:
                        raise HTTPError(RADAR_WMS, code, f"HTTP {code}", None, None)
                    self.frames = parse_frames(body)
                    st.update(at=now, error="", fails=0, next=now + RADAR_FRAMES_S)
                except HTTPError as e:
                    self._failed("radar", now, e.code, f"HTTP {e.code}", st)
                except (URLError, OSError, ValueError) as e:
                    self._failed("radar", now, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}", st)
        return {"frames": list(self.frames), "at": st["at"], "error": st["error"]}

    def radar_tile(self, frame: str, z: int, x: int, y: int) -> tuple[int, bytes, str, dict]:
        if not (self.radar_on and self.enabled):
            return 404, b'{"error":"radar off"}', JSON, {}
        if not (RADAR_MINZOOM <= z <= RADAR_MAXZOOM and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            return 404, b'{"error":"no radar tile at that zoom"}', JSON, {}
        if frame != "now" and frame not in {f["id"] for f in self.frames}:
            return 404, b'{"error":"no such radar frame (GET /api/cad/weather/radar/frames)"}', JSON, {}
        ttl = RADAR_NOW_TTL_S if frame == "now" else RADAR_FRAME_TTL_S
        key, now = (frame, z, x, y), self.clock()
        with self._tile_lock:
            hit = self._tiles.get(key)
            if hit and now - hit["at"] < ttl:
                self._tiles.move_to_end(key)
                return 200, hit["body"], "image/png", {"Cache-Control": f"private, max-age={int(ttl - (now - hit['at']))}"}
        try:
            with self._parallel:
                code, headers, body = self.fetch(radar_url(z, x, y, frame), RADAR_TIMEOUT_S, RADAR_MAX_BYTES)
            ctype = (headers.get("content-type") or "").split(";")[0].strip()
            if code != 200 or ctype != "image/png" or not body.startswith(b"\x89PNG"):
                raise ValueError(f"HTTP {code} {ctype or 'no type'}")      # a WMS error answers 200 with XML
        except (HTTPError, URLError, OSError, ValueError) as e:
            if hit:                                           # the last one we had, rather than a hole in the map
                return 200, hit["body"], "image/png", {"Cache-Control": "private, max-age=30", "X-Radar-Stale": "1"}
            return 502, json.dumps({"error": f"radar: {_redact(getattr(e, 'reason', e))}"}).encode(), JSON, {}
        with self._tile_lock:
            self._tiles[key] = {"body": body, "at": now}
            self._tiles.move_to_end(key)
            while len(self._tiles) > RADAR_KEEP:
                self._tiles.popitem(last=False)
        return 200, body, "image/png", {"Cache-Control": f"private, max-age={int(ttl)}"}

    # -- HTTP --------------------------------------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        return path.startswith("/api/cad/weather") and (
            path in ("/api/cad/weather", "/api/cad/weather/speech", "/api/cad/weather/radar/frames") or bool(RADAR_PATH.fullmatch(path)))

    def get(self, path: str, query: str, headers=None) -> tuple[int, bytes, str, dict]:
        q = parse_qs(query or "")
        one = lambda k: (q.get(k) or [""])[0].strip()
        nocache = {"Cache-Control": "no-store"}
        if path == "/api/cad/weather":
            return 200, json.dumps(self.public(), separators=(",", ":")).encode(), JSON, nocache
        if path == "/api/cad/weather/speech":
            code, body = self.speech(one("id")[:300])
            return code, json.dumps(body).encode(), JSON, nocache
        if path == "/api/cad/weather/radar/frames":
            return 200, json.dumps(self.radar_frames()).encode(), JSON, nocache
        if m := RADAR_PATH.fullmatch(path):
            return self.radar_tile(m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        return 404, b"not found\n", "text/plain", {}
