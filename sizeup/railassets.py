"""The New Haven Line's own tracks, catenary structures (pole numbers) and mileposts (GitHub #371), for the Trains layer:
each train drawn on the numbered track it is on, and "near pole 249 · MP 25.3" for a train or an incident by the line
(what Metro-North asks for when a crew is on or near the tracks).

Source: Metro-North's own GIS, published by the MTA as a public ArcGIS Online feature service (no key, no login):

    LINK  .../ROWAccessPoints/FeatureServer (services1.arcgis.com, the MTA's organisation; owner: Metro-North GIS)
      layer 6  "MNR Railroad Track"   polylines: LineCode, TrkName ("1".."4" on the main line through here; "5".."10" at
                                      New Rochelle and Stamford), TrkType ("Main Line", "Switch", "Derail", "Siding"...)
      layer 7  "Catenary Bridge"      polylines across the tracks (a portal): CB_ID ("CB-249", "CB-243A"), CB_ID2
      layer 2  "Milepost"             points: PointName ("25"), the whole miles from Grand Central
      layer 3  "Tenth Milepost"       points: Name ("25.3"), Label (".3")

Checked 2026-11-09 (Rye station to just past Greenwich, about 40.975..41.04 N, 73.70..73.59 W): 4 main tracks, 149
catenary structures (CB-185 to CB-317, some with A / B / X suffixes), 8 whole and 65 tenth mileposts (MP 23 to 30).
Over the layer's area ([trains] bbox, Mount Vernon East to Stamford, plus MARGIN_DEG, as one refresh reads it): 9
numbered tracks, 453 structures and 224 mileposts (whole and tenths). The service was last edited in 2017; the data
has no licence statement (a public service of the MTA's: we say where it is from, keep a copy on our own server and
serve our pages from it). The whole mileposts say LineCode NHV in some of the service's copies, the rest NHL: both
are asked for.
OpenStreetMap has the four tracks too (railway:track_ref 1..4, the same order: 3, 1, 2, 4 from north to south) but no
catenary numbers (no railway=catenary_mast / power=catenary_mast with a ref here) and mileposts only every few miles.

Fetched monthly by the Data sources registry (sizeup/datasources.py, in the quiet hours; Refresh now there), one layer
at a time with our User-Agent, only what lies in the area (a server-side envelope query; the tracks thinned
server-side to about half a metre, maxAllowableOffset). Kept in the state dir (trains/rail_assets.json; a copy in S3,
sizeup/cachesync.py), so a restart fetches nothing. With none (never fetched, or the source down): trains on their
direction's outer track as before (data/mnr_tracks.json), and no pole or milepost anywhere. Never raises.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

SOURCE = "MTA Metro-North GIS"
LINK = "https://services1.arcgis.com/oOUgp466Coyjcu6V/arcgis/rest/services/ROWAccessPoints/FeatureServer"
ATTRIBUTION = "Data: MTA Metro-North Railroad GIS"
CACHE_NAME = "rail_assets.json"         # in the trains cache dir (state dir trains/; sizeup/cachesync.py FILES)
LAYERS = {"tracks": (6, "LineCode='NHL' AND TrkType='Main Line'", "TrkName"),
          "poles": (7, "LineCode='NHL'", "CB_ID"),
          "mileposts": (2, "LineCode IN ('NHL','NHV')", "PointName"),     # the whole miles: NHV in some of the service's copies
          "tenths": (3, "LineCode='NHL'", "Name")}
MARGIN_DEG = 0.01                       # asked for this far beyond the area (about 1 km)
THIN_DEG = 0.000005                     # the tracks thinned server-side to about this (half a metre)
PAGE = 1000                             # the service's maxRecordCount
MAX_PAGES = 10
MAX_BYTES = 24 * 1024 * 1024            # one answer (the four main tracks are the whole line: ~3 MB unthinned)
TIMEOUT_S = 60.0
BACKOFF_S = (300.0, 6 * 3600.0)
TRACK = re.compile(r"\d{1,2}")          # a numbered main track ("1".."10"); "5 (Cos Cob Yard) Siding" is not one
POLE = re.compile(r"(?:CB-?)?0*(\d{1,4}[A-Z]?)")
NEAR_M = 150.0                          # a point this close to the line gets a pole and milepost
POLE_M = 150.0                          # the nearest structure further along the line than this: none
MP_FIT_M = 60.0                         # a milepost further than this from the line isn't on it
MP_SLACK_M = 400.0                      # a point this far beyond the first / last milepost: still read off them
LANE_OFF_M = 40.0                       # a track's point further than this from the line: not this line's
TRACK_GAP_M = 100.0                     # a track's s jumping this much more than the step itself: two runs
RATE_LIMITED_S = 1800.0                 # a 429 / 401 / 403: rest this long


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _pt(c) -> tuple[float, float] | None:
    if isinstance(c, (list, tuple)) and len(c) >= 2 and _num(c[0]) is not None and _num(c[1]) is not None:
        lon, lat = float(c[0]), float(c[1])
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            return round(lon, 7), round(lat, 7)
    return None


def _runs(geom) -> list[list[tuple[float, float]]]:
    """A GeoJSON LineString / MultiLineString -> runs of (lon, lat)."""
    if not isinstance(geom, dict):
        return []
    c = geom.get("coordinates")
    parts = [c] if geom.get("type") == "LineString" else c if geom.get("type") == "MultiLineString" else []
    out = []
    for p in parts if isinstance(parts, list) else []:
        run = [q for q in map(_pt, p if isinstance(p, list) else []) if q is not None]
        if len(run) >= 2:
            out.append(run)
    return out


def _in(lon: float, lat: float, bbox, margin: float = 0.0) -> bool:
    w, s, e, n = bbox
    return w - margin <= lon <= e + margin and s - margin <= lat <= n + margin


def _clip(run, bbox, margin: float):
    out, cur = [], []
    for lon, lat in run:
        if _in(lon, lat, bbox, margin):
            cur.append((lon, lat))
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [r for r in out if len(r) >= 2]


def parse(layers: dict[str, list[dict]], bbox, margin: float = MARGIN_DEG) -> dict:
    """{tracks: [features], poles: [...], mileposts: [...], tenths: [...]} (GeoJSON features) -> our document:
    {tracks: {"1": [[[lon, lat]...] run...]}, poles: [{id, lon, lat, a, b}], mileposts: [{mp, lon, lat}]}, sorted, each
    inside the area (plus margin). A feature in the wrong shape is skipped."""
    layers = {k: [f for f in (v or []) if isinstance(f, dict)] for k, v in layers.items()}
    tracks: dict[str, list] = {}
    for f in layers.get("tracks") or []:
        name = str((f.get("properties") or {}).get("TrkName") or "").strip()
        if not TRACK.fullmatch(name):
            continue
        for run in _runs(f.get("geometry")):
            for part in _clip(run, bbox, margin):
                tracks.setdefault(str(int(name)), []).append([list(p) for p in part])
    poles, seen = [], set()
    for f in layers.get("poles") or []:
        p = f.get("properties") or {}
        m = POLE.fullmatch(str(p.get("CB_ID") or p.get("CB_ID2") or "").strip().upper())
        runs = _runs(f.get("geometry"))
        if not m or not runs:
            continue
        a, b = runs[0][0], runs[-1][-1]
        mid = (round((a[0] + b[0]) / 2, 7), round((a[1] + b[1]) / 2, 7))
        if m.group(1) in seen or not _in(*mid, bbox, margin):
            continue
        seen.add(m.group(1))
        poles.append({"id": m.group(1), "lon": mid[0], "lat": mid[1], "a": list(a), "b": list(b)})
    mps: dict[float, dict] = {}
    for key, field in (("mileposts", "PointName"), ("tenths", "Name")):
        for f in layers.get(key) or []:
            p = f.get("properties") or {}
            g = f.get("geometry") or {}
            q = _pt(g.get("coordinates")) if g.get("type") == "Point" else None
            try:
                mp = round(float(str(p.get(field) or "").strip()), 1)
            except ValueError:
                continue
            if q is None or not 0 <= mp <= 200 or not _in(*q, bbox, margin):
                continue
            mps.setdefault(mp, {"mp": mp, "lon": q[0], "lat": q[1]})
    poles.sort(key=lambda x: (int(re.match(r"\d+", x["id"]).group()), x["id"]))
    return {"tracks": dict(sorted(tracks.items(), key=lambda kv: int(kv[0]))), "poles": poles,
            "mileposts": sorted(mps.values(), key=lambda x: x["mp"])}


def http_get(url: str, timeout: float, limit: int, headers: dict | None = None) -> tuple[int, dict, bytes]:
    from sizeup.trains import http_get as get
    return get(url, timeout, limit, headers)


def query_url(layer: int, where: str, bbox, offset: int = 0, thin: bool = False) -> str:
    w, s, e, n = bbox
    q = {"where": where, "geometry": f"{w - MARGIN_DEG},{s - MARGIN_DEG},{e + MARGIN_DEG},{n + MARGIN_DEG}",
         "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
         "outSR": "4326", "outFields": "*", "resultOffset": str(offset), "resultRecordCount": str(PAGE), "f": "geojson"}
    if thin:
        q["maxAllowableOffset"] = f"{THIN_DEG:g}"
    return f"{LINK}/{layer}/query?{urlencode(q)}"


# ---- reading it off: a point's pole and milepost; the tracks along the line's s ------------------------------------


class _Grid:
    """The line's segments by 200 m cell (a projection of thousands of points without walking the whole line each)."""
    CELL = 200.0

    def __init__(self, line):
        self.line = line
        self.cells: dict[tuple[int, int], list[int]] = {}
        for i in range(len(line.xy) - 1):
            (ax, ay), (bx, by) = line.xy[i], line.xy[i + 1]
            for gx in range(int(min(ax, bx) // self.CELL), int(max(ax, bx) // self.CELL) + 1):
                for gy in range(int(min(ay, by) // self.CELL), int(max(ay, by) // self.CELL) + 1):
                    self.cells.setdefault((gx, gy), []).append(i)

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """(metres from the line, its ch there); (inf, 0) beyond a cell's reach of it (about 200 m)."""
        from sizeup.trains import _xy
        px, py = _xy(lon, lat)
        cx, cy = int(px // self.CELL), int(py // self.CELL)
        best, best_ch = math.inf, 0.0
        xy, ch = self.line.xy, self.line.ch
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for i in self.cells.get((gx, gy), ()):
                    (ax, ay), (bx, by) = xy[i], xy[i + 1]
                    dx, dy = bx - ax, by - ay
                    L2 = dx * dx + dy * dy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
                    d = math.hypot(px - ax - t * dx, py - ay - t * dy)
                    if d < best:
                        best, best_ch = d, ch[i] + t * math.sqrt(L2)
        return best, best_ch


def _increasing(pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The longest subsequence of (s, mp), in s order, whose mp strictly grows."""
    best: list[int] = [1] * len(pairs)
    prev: list[int] = [-1] * len(pairs)
    for i, (si, mi) in enumerate(pairs):
        for j in range(i):
            if pairs[j][1] < mi and pairs[j][0] < si and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    if not pairs:
        return []
    i = max(range(len(pairs)), key=lambda k: best[k])
    out = []
    while i >= 0:
        out.append(pairs[i])
        i = prev[i]
    return out[::-1]


class RailRef:
    """The assets measured along a line (sizeup/trains.py Line: s, metres from Grand Central): each numbered track as
    [[lon, lat, s]] (a trains.Lane's coords), each pole's s, each milepost's s. mp_at(s) reads the milepost off the
    two either side (straight between); pole_at(s) the nearest structure along the line."""

    def __init__(self, doc: dict | None, line):
        doc = doc or {}
        self.line = line
        grid = _Grid(line) if line is not None and len(getattr(line, "xy", [])) >= 2 else None
        self.tracks: dict[str, list[list[list[float]]]] = {}
        self.poles: list[tuple[float, str]] = []
        self.mps: list[tuple[float, float]] = []
        if grid is None:
            return
        # each track a list of runs [[lon, lat, s]...] in s order: each feature's part on its own (two features, or two
        # parts of one, are never joined: a gap between them is a gap, never a chord across it), broken where it
        # leaves the line or its s jumps more than its own length (TRACK_GAP_M slack) from one point to the next
        from sizeup.trains import metres
        for num, runs in (doc.get("tracks") or {}).items():
            out = []
            for run in runs:
                cur: list[list[float]] = []
                for lon, lat in run:
                    d, s = grid.project(lat, lon)
                    jump = cur and abs(s - cur[-1][2]) > metres(lat, lon, cur[-1][1], cur[-1][0]) + TRACK_GAP_M
                    if d > LANE_OFF_M or jump:
                        out.append(cur)
                        cur = []
                    if d <= LANE_OFF_M:
                        cur.append([lon, lat, round(s, 1)])
                out.append(cur)
            out = [sorted(r, key=lambda c: c[2]) for r in out if len(r) >= 2]
            if out:
                self.tracks[num] = sorted(out, key=lambda r: r[0][2])
        for p in doc.get("poles") or []:
            d, s = grid.project(p["lat"], p["lon"])
            if d <= NEAR_M:
                self.poles.append((s, p["id"]))
        self.poles.sort()
        pairs = []
        for m in doc.get("mileposts") or []:
            d, s = grid.project(m["lat"], m["lon"])
            if d <= MP_FIT_M:
                pairs.append((s, m["mp"]))
        pairs.sort()
        # mileposts grow with s along the line (Grand Central's 0): the longest run of them that does; one that
        # doesn't (a typo, a stray) is left out
        self.mps = _increasing(pairs)
        self._pole_s = [s for s, _ in self.poles]
        self._mp_s = [s for s, _ in self.mps]

    def __bool__(self) -> bool:
        return bool(self.tracks or self.poles or self.mps)

    def mp_at(self, s: float) -> float | None:
        """The milepost at s (one decimal); None off the stretch the mileposts cover (MP_SLACK_M beyond its ends)."""
        xs = self._mp_s
        if len(xs) < 2 or not xs[0] - MP_SLACK_M <= s <= xs[-1] + MP_SLACK_M:
            return None
        i = max(1, min(len(xs) - 1, bisect.bisect_left(xs, s)))
        (s0, m0), (s1, m1) = self.mps[i - 1], self.mps[i]
        return round(m0 + (m1 - m0) * (s - s0) / (s1 - s0), 1) if s1 > s0 else round(m0, 1)

    def pole_at(self, s: float) -> tuple[str, float] | None:
        """(the nearest catenary structure's number, metres along the line from s to it); None when none is
        within POLE_M."""
        xs = self._pole_s
        if not xs:
            return None
        i = bisect.bisect_left(xs, s)
        best = min((k for k in (i - 1, i) if 0 <= k < len(xs)), key=lambda k: abs(xs[k] - s))
        return (self.poles[best][1], abs(xs[best] - s)) if abs(xs[best] - s) <= POLE_M else None

    def at_s(self, s: float) -> dict:
        """{pole, mp} at s along the line (each only when known): what a train's box says."""
        out = {}
        p = self.pole_at(s)
        if p is not None:
            out["pole"] = p[0]
        mp = self.mp_at(s)
        if mp is not None:
            out["mp"] = mp
        return out

    def at(self, lat: float, lon: float, within: float = NEAR_M) -> dict:
        """{pole, mp, m (metres off the line)} for a point within `within` of the line, else {}."""
        if not self or self.line is None:
            return {}
        d, s = self.line.project(lat, lon)
        if d > within:
            return {}
        out = self.at_s(s)
        return out | {"m": round(d)} if out else {}


def label(ref: dict) -> str:
    """"near pole 249 · MP 25.3" (what is known of it), "" when nothing is."""
    return " · ".join(x for x in ((f"near pole {ref['pole']}" if ref.get("pole") else ""),
                                  (f"MP {ref['mp']:.1f}" if isinstance(ref.get("mp"), (int, float)) else "")) if x)


# ---- the provider: fetch monthly (the Data sources registry), keep on disk ----------------------------------------


class RailAssets:
    def __init__(self, enabled: bool = True, bbox=(-73.845, 40.900, -73.530, 41.060), cache_dir: str | Path | None = None,
                 fetch: Callable | None = None, log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 on_update: Callable[[], None] | None = None):
        from sizeup.traintime import UA
        self.enabled = bool(enabled)
        self.bbox = tuple(float(x) for x in bbox)
        self.file = Path(cache_dir) / CACHE_NAME if cache_dir else None
        self.fetch = fetch or http_get
        self.headers = {"User-Agent": UA, "Accept": "application/geo+json, application/json"}
        self.log, self.clock = log, clock
        self.on_update = on_update
        self.doc: dict | None = None
        self.st = {"at": None, "error": "", "next": 0.0, "fails": 0, "bytes": 0}
        self._lock = threading.Lock()
        self._load()

    def counts(self) -> dict:
        d = self.doc or {}
        return {"tracks": len(d.get("tracks") or {}), "poles": len(d.get("poles") or []), "mileposts": len(d.get("mileposts") or [])}

    def status(self) -> dict:
        if not self.enabled:
            return {"on": False, "why": "Trains off ([trains])"}
        c = self.counts()
        note = f"{c['tracks']} tracks · {c['poles']} poles · {c['mileposts']} mileposts" if self.doc else ""
        return {"at": self.st["at"], "error": self.st["error"], "n": c["poles"] if self.doc else None, "note": note,
                "bytes": self.st["bytes"] or None}

    def _layer(self, key: str) -> tuple[list[dict], int]:
        layer, where, _ = LAYERS[key]
        feats, size = [], 0
        for page in range(MAX_PAGES):
            url = query_url(layer, where, self.bbox, len(feats), thin=key == "tracks")
            code, _, body = self.fetch(url, TIMEOUT_S, MAX_BYTES, self.headers)
            if code != 200:
                raise HTTPError(url, code, f"HTTP {code}", None, None)
            size += len(body)
            doc = json.loads(body)
            if not isinstance(doc, dict) or "error" in doc:
                err = (doc or {}).get("error") if isinstance(doc, dict) else None
                raise ValueError(f"layer {layer}: {(err or {}).get('message') if isinstance(err, dict) else 'not GeoJSON'}")
            got = doc.get("features")
            if not isinstance(got, list):
                raise ValueError(f"layer {layer}: no features list")
            feats += got
            more = doc.get("exceededTransferLimit") or (doc.get("properties") or {}).get("exceededTransferLimit")
            if not more or not got:
                break
        return feats, size

    def refresh(self) -> bool:
        """All four layers now, one at a time. True when read; on any failure the copy we had stays. Never raises."""
        if not self.enabled:
            return False
        now = self.clock()
        try:
            layers, size = {}, 0
            for key in LAYERS:
                layers[key], n = self._layer(key)
                size += n
            doc = parse(layers, self.bbox)
            if not doc["tracks"] and not doc["poles"] and not doc["mileposts"]:
                raise ValueError("nothing in our area (the layers changed?)")
        except HTTPError as e:
            if e.code == 429:
                return self._failed(now, f"rate limit (HTTP {e.code})", RATE_LIMITED_S)
            if e.code in (401, 403):
                return self._failed(now, f"refused (HTTP {e.code})", RATE_LIMITED_S)
            return self._failed(now, f"HTTP {e.code}")
        except (URLError, OSError, ValueError, KeyError, TypeError) as e:
            return self._failed(now, f"{type(e).__name__}: {getattr(e, 'reason', e)}")
        except Exception as e:  # never the trains' thread
            return self._failed(now, f"{type(e).__name__}: {e}")
        with self._lock:
            self.doc = doc | {"at": now, "source": SOURCE, "link": LINK}
            self.st.update(at=now, error="", fails=0, bytes=size)
        c = self.counts()
        self.log(f"trains: {SOURCE}: {c['tracks']} tracks, {c['poles']} catenary structures, {c['mileposts']} mileposts in our area")
        self._save()
        self._updated()
        return True

    def _failed(self, now: float, why: str, rest: float | None = None) -> bool:
        """A failure: back off (a rate limit or a refusal: rest, which Data sources shows as resting)."""
        self.st["fails"] += 1
        wait = rest if rest is not None else min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(self.st["fails"] - 1, 30))
        self.st.update(error=why, next=now + wait)
        self.log(f"trains: {SOURCE}: {why}; the copy we have stays")
        return False

    def _updated(self) -> None:
        if self.on_update is not None:
            try:
                self.on_update()
            except Exception as e:  # pragma: no cover - the trains' rebuild: logged, never fatal
                self.log(f"trains: rail assets not applied ({type(e).__name__}: {e})")

    def _load(self) -> None:
        f = self.file
        if not self.enabled or f is None or not f.is_file():
            return
        try:
            doc = json.loads(f.read_text())
            at = doc.get("at")
            if not isinstance(at, (int, float)) or not isinstance(doc.get("tracks"), dict) or not isinstance(doc.get("poles"), list):
                raise ValueError("not our shape")
            self.doc = doc
            self.st.update(at=at)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.log(f"trains: {f.name} not read ({type(e).__name__}); fetched again")

    def reload(self) -> None:
        """The cache again (cachesync put a new task's back after we were made)."""
        if self.doc is None:
            self._load()
            if self.doc is not None:
                self._updated()

    def _save(self) -> None:
        if not self.file or self.doc is None:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.doc, separators=(",", ":")))
            tmp.replace(self.file)
        except OSError as e:
            self.log(f"trains: {self.file.name} not saved ({type(e).__name__}: {e})")

    def layer(self) -> dict:
        """GET /api/cad/trains/railref: the poles and mileposts to draw ({poles: [{id, lon, lat}], mileposts: [...]})."""
        d = self.doc or {}
        return {"source": SOURCE, "link": LINK, "at": self.st["at"], "error": self.st["error"],
                "poles": [{"id": p["id"], "lon": p["lon"], "lat": p["lat"]} for p in d.get("poles") or []],
                "mileposts": [{"mp": m["mp"], "lon": m["lon"], "lat": m["lat"], "whole": float(m["mp"]).is_integer()}
                              for m in d.get("mileposts") or []]}
