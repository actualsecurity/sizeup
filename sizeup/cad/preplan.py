"""Pre-plan points (#456): where a building's features are, exactly (the Knox box, the FDC, the shutoffs ...), shown on
the map of every call there.

    preplan_points   one point per row: its building key (sizeup/cad/addrkey.py; '' for a point known only by where it
                     is: then matched within addrkey.GROUP_M of a call, as premise notes are), the address key and label
                     it was keyed from, the county footprint it is in or nearest (building_ref "buildings/<id>"), how it
                     was linked (link), the kind (KINDS), a short label, a note, the point, an optional level ("2",
                     "Roof", "B1"), a priority (1 high, 2 normal, 3 low), active (0: deleted), who made it and when,
                     who changed it last, a review-by date (shown as due once passed); indexed by bldg_key and by a geo
                     cell (cell_of: CELL_DEG squares), so a call's points and a map's viewport are both index reads.

The building (link):
    inside    the point is in a county building footprint (sizeup/gis.py, layer "buildings": the planimetric
              footprints, FEA_CODE 2100): its parcel's address (layer "parcels", PARCEL_ADDR) is keyed with addrkey, so
              the point has the key the calls at that address get; placed from a call and in that call's own footprint
              (or one keyed the same), the call's key
    nearest   outside every footprint (an FDC on the sidewalk, a gate): the nearest footprint within NEAR_M, the same way
    call      the call's (or the typed address's) building, explicitly, or because no footprint is in reach
    point     no building: matched by the point (within addrkey.GROUP_M of a call), as a premise note with no address
The footprint layer is the county's; with none loaded (a dev box, a hub still reading its cache) a point takes the
call's key, or none.

Surfacing: an open incident's points ride on the state push (Cad.state: "preplan" on each open incident: only the
kinds everyone may see, a few small rows), its building's and, when the call or the point has no building key, those
within addrkey.GROUP_M ("nearby"). Restricted kinds (VISIBLE_TO / Settings > System > Address notes, the same setting
as premise notes: RESTRICTABLE) never ride the push: GET /api/cad/preplan?incident=<id> per person. The map's
"Pre-plan" layer reads a viewport (GET ?bbox=, at most BBOX_MAX_DEG a side, ETag on the write generation and the
reader's standing). A list of every point and the CSV are admins'.

Every create, edit, move and delete is in the Settings audit log (section "preplan"); a restricted kind's rows are its id,
its kind and the names of what changed, for readers who may not see that kind (at write and at read). The server log
never gets a point's label, note or coordinates: the API's activity line says "Moved pre-plan point 12".
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs

from sizeup.cad import addrkey
from sizeup.cad.premise import STANDING, may_see, who
from sizeup.cad.settings import Editor
from sizeup.cad.store import TZ, CadError

KINDS = {"knox_box": "Knox box", "fdc": "FDC", "standpipe_riser": "Standpipe riser", "sprinkler_valve": "Sprinkler valve",
         "alarm_panel": "Alarm panel", "gas_shutoff": "Gas shutoff", "electric_shutoff": "Electric shutoff",
         "water_shutoff": "Water shutoff", "elevator": "Elevator", "roof_access": "Roof access", "gate_access": "Gate access",
         "hazmat_storage": "Hazmat storage", "solar_disconnect": "Solar disconnect", "other": "Other"}
# The kinds an admin may keep to some roles (Settings > System > Address notes: the premise_visibility setting, beside
# the note categories): where the keys are, and the gate codes.
RESTRICTABLE = ("knox_box", "gate_access")
VISIBLE_TO: dict[str, str] = {}      # the default before any edit: everyone signed in
PRIORITIES = {1: "High", 2: "Normal", 3: "Low"}
LINKS = ("inside", "nearest", "call", "point")
LABEL_MAX, NOTE_MAX, LEVEL_MAX = 60, 300, 20
NEAR_M = 30.0                        # outside every footprint: the nearest one this close is the building
CELL_DEG = 0.002                     # the geo cell (about 220 m x 170 m here)
BBOX_MAX_DEG = 0.05                  # the layer's viewport at most this a side (zoom 16 on a wide screen, padded)
SECTION = "preplan"                  # the Settings audit log's section
LAYER = "buildings"                  # the county's footprints (sizeup/gis.py DEFAULT_LAYERS)
PARCELS = "parcels"
AUDITED = ("kind", "label", "note", "lat", "lon", "level", "priority", "review_by", "bldg_key", "building_ref", "link", "active")
OPEN = ("queued", "pending", "active")

SCHEMA = """
CREATE TABLE IF NOT EXISTS preplan_points (
  id INTEGER PRIMARY KEY,
  bldg_key TEXT NOT NULL DEFAULT '',    -- the building (addrkey.py); '' = by its point
  addr_key TEXT,                        -- the address it was keyed from, if any
  building_ref TEXT,                    -- the county footprint: "buildings/<id>"; NULL: none in reach
  link TEXT NOT NULL DEFAULT 'point',   -- inside | nearest | call | point
  addr_label TEXT NOT NULL DEFAULT '',  -- the building's address as shown
  kind TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  lat REAL NOT NULL, lon REAL NOT NULL,
  cell TEXT NOT NULL,                   -- cell_of(lat, lon)
  level TEXT,                           -- "2", "Roof", "B1"
  priority INTEGER NOT NULL DEFAULT 2,
  active INTEGER NOT NULL DEFAULT 1,    -- 0: deleted
  created_by TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  review_by REAL
);
CREATE INDEX IF NOT EXISTS preplan_bldg ON preplan_points(bldg_key) WHERE active = 1;
CREATE INDEX IF NOT EXISTS preplan_cell ON preplan_points(cell) WHERE active = 1;
"""


def cell_of(lat: float, lon: float) -> str:
    return f"{math.floor(lat / CELL_DEG)}:{math.floor(lon / CELL_DEG)}"


def cells(w: float, s: float, e: float, n: float) -> list[str]:
    """Every cell a box touches."""
    y0, y1 = math.floor(s / CELL_DEG), math.floor(n / CELL_DEG)
    x0, x1 = math.floor(w / CELL_DEG), math.floor(e / CELL_DEG)
    return [f"{y}:{x}" for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


# ---- the footprints ---------------------------------------------------------------------------------------------------

def _rings(g: dict) -> list[list]:
    c = g.get("coordinates") or []
    if g.get("type") == "Polygon":
        return list(c)
    if g.get("type") == "MultiPolygon":
        return [r for poly in c for r in poly]
    return []


def _seg_m(lat: float, lon: float, a, b) -> float:
    """Metres from the point to the segment a-b ([lon, lat] each), on a local flat plane."""
    ky, kx = 110_574.0, 111_320.0 * math.cos(math.radians(lat))
    ax, ay, bx, by = (a[0] - lon) * kx, (a[1] - lat) * ky, (b[0] - lon) * kx, (b[1] - lat) * ky
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
    return math.hypot(ax + t * dx, ay + t * dy)


def edge_m(g: dict, lat: float, lon: float) -> float:
    """Metres from the point to the polygon's nearest edge."""
    best = math.inf
    for r in _rings(g):
        for i in range(len(r) - 1):
            best = min(best, _seg_m(lat, lon, r[i], r[i + 1]))
    return best


SAMPLES = 9                          # a footprint's parcel: a 9 x 9 grid over it, the parcel most of its inside points are in


def samples(g: dict, n: int = SAMPLES) -> list[tuple[float, float]]:
    """Points inside the footprint, on an n x n grid over its box (cell centres): what it covers, whatever the shape (an L,
    a U), the same wherever a point on it was tapped. A footprint too thin for the grid: its vertices' mean."""
    from sizeup.gis import contains, geometry_bbox
    b = geometry_bbox(g)
    if b is None:
        return []
    w, s_, e, n_ = b
    out = [(s_ + (n_ - s_) * (j + 0.5) / n, w + (e - w) * (i + 0.5) / n) for j in range(n) for i in range(n)]
    out = [(y, x) for y, x in out if contains(g, y, x)]
    if not out:
        r = (_rings(g) or [[]])[0][:-1]
        if r:
            out = [(sum(p[1] for p in r) / len(r), sum(p[0] for p in r) / len(r))]
    return out


def footprint(gis, lat: float, lon: float, near_m: float = NEAR_M) -> dict | None:
    """The county footprint the point is in ({ref, how: "inside", dist_m: 0, geometry}), else the nearest within near_m
    (how "nearest"), else None (none in reach, or no footprint layer here)."""
    if gis is None or LAYER not in getattr(gis, "layers", {}):
        return None
    from sizeup.gis import contains
    a, b, c, d = addrkey.bbox(lat, lon, near_m)
    best = None
    for f in gis.features_in(LAYER, (c, a, d, b)):
        g = f.get("geometry") or {}
        if contains(g, lat, lon):
            best = (0.0, f)
            break
        m = edge_m(g, lat, lon)
        if m <= near_m and (best is None or m < best[0]):
            best = (m, f)
    if best is None:
        return None
    m, f = best
    g = f["geometry"]
    if f.get("id") is not None:
        ref = f"{LAYER}/{f['id']}"
    else:                                       # no object id: its first vertex names it
        r = (_rings(g) or [[[0, 0]]])[0][0]
        ref = f"{LAYER}/@{r[1]:.6f},{r[0]:.6f}"
    return {"ref": ref, "how": "inside" if m == 0 else "nearest", "dist_m": round(m, 1), "geometry": g}


# ---- the service ------------------------------------------------------------------------------------------------------

class Preplan:
    """One per Cad (sizeup/cad/service.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        # bumped on every write: the caches, the layer's ETag, the pages' copies. Starts at this process's start in ms, so a
        # new task (a deploy) never repeats an earlier one's generation (that would take more than a write a ms)
        self.gen = int(time.time() * 1000)
        self._cache: dict = {}
        self._lock = threading.Lock()
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'incidents'").fetchone():
                self.store.db.executescript(SCHEMA)

    # -- who sees what ------------------------------------------------------------------------------------------------

    def visible(self) -> dict:
        """kind -> the least standing that sees it (only RESTRICTABLE kinds; the premise_visibility setting)."""
        v = getattr(self.cad, "premise_visible", None)
        v = VISIBLE_TO if v is None else v
        return {k: v[k] for k in RESTRICTABLE if k in v}

    def can_see(self, p: dict, may: str) -> bool:
        return may_see(p["kind"], may, self.visible())

    def restricted(self) -> list[str]:
        return sorted(self.visible())

    # -- reading --------------------------------------------------------------------------------------------------------

    def shown(self, p: dict, now: float) -> dict:
        out = {k: p[k] for k in ("id", "kind", "label", "note", "lat", "lon", "level", "priority", "bldg_key", "addr_label",
                                 "building_ref", "link", "created_at", "updated_at", "review_by")}
        out["active"] = bool(p["active"])
        out["review_due"] = p["review_by"] is not None and p["review_by"] <= now
        out["by"] = who(p["updated_by"] or p["created_by"])
        out["restricted"] = p["kind"] in self.visible()
        return out

    def _at(self, bldg_key: str, lat, lon) -> list[dict]:
        """The active points of a building, and (the call or the point with no building key) within GROUP_M of the
        call's point. Cached until the next write. Two keyed buildings never share points."""
        if not bldg_key and lat is None:
            return []
        ck = (bldg_key, round(lat, 5) if lat is not None else None, round(lon, 5) if lon is not None else None)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None and hit[0] == self.gen:
                return hit[1]
            gen = self.gen
        rows = self.store.q("SELECT * FROM preplan_points WHERE bldg_key = ? AND active = 1", (bldg_key,)) if bldg_key else []
        if lat is not None and lon is not None:
            a, b, c, d = addrkey.bbox(lat, lon)
            cs = cells(c, a, d, b)
            seen = {r["id"] for r in rows}
            rows += [r for r in self.store.q(f"SELECT * FROM preplan_points WHERE active = 1 AND cell IN ({','.join('?' * len(cs))})", cs)
                     if r["id"] not in seen and (not bldg_key or not r["bldg_key"])
                     and addrkey.meters(lat, lon, r["lat"], r["lon"]) <= addrkey.GROUP_M]
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[ck] = (gen, rows)
        return rows

    @staticmethod
    def order(points: list[dict]) -> list[dict]:
        kinds = list(KINDS)
        return sorted(points, key=lambda p: (p["priority"], kinds.index(p["kind"]) if p["kind"] in kinds else 99, p["id"]))

    def for_incident(self, inc: dict, may: str = "admin") -> list[dict]:
        """An incident's points the viewer may see (may: their standing; the push: "viewer"), the building's first, each
        with nearby (shown by its point, not this building)."""
        now = self.cad.clock()
        out = []
        for p in self.order([p for p in self._at(inc.get("bldg_key") or "", inc.get("lat"), inc.get("lon")) if self.can_see(p, may)]):
            s = self.shown(p, now)
            s["nearby"] = not (inc.get("bldg_key") and p["bldg_key"] == inc.get("bldg_key"))
            out.append(s)
        return out

    def decorate(self, incs: list[dict]) -> None:
        """The state push: each open incident's points ("preplan": what everyone may see)."""
        for inc in incs:
            if inc.get("status") in OPEN and (inc.get("bldg_key") or inc.get("lat") is not None):
                try:
                    inc["preplan"] = self.for_incident(inc, "viewer")
                except Exception as e:  # noqa: BLE001 - never the push (and never a point's text in the log)
                    self.cad.log(f"CAD preplan: {type(e).__name__}")

    def state(self) -> dict:
        """The state's "preplan": the write generation and the kinds kept to some roles (the page asks for those)."""
        return {"gen": self.gen, "restricted": self.restricted(), "levels": self.visible()}

    def in_box(self, box: tuple[float, float, float, float], may: str) -> list[dict]:
        w, s, e, n = box
        cs = cells(w, s, e, n)
        rows = self.store.q(f"SELECT * FROM preplan_points WHERE active = 1 AND cell IN ({','.join('?' * len(cs))})", cs)
        now = self.cad.clock()
        return [self.shown(r, now) for r in self.order(rows)
                if w <= r["lon"] <= e and s <= r["lat"] <= n and self.can_see(r, may)]

    def etag(self, query: str, may: str) -> str | None:
        """The layer's (bbox) and a building's (where) answer: the generation, the reader's standing, the setting and the
        query. None for anything else: a call's points follow the call (its address, pin, status), so never a 304."""
        q = parse_qs(query or "")
        if not (q.get("bbox") or q.get("where")) or q.get("incident"):
            return None
        vis = json.dumps(self.visible(), sort_keys=True)
        h = hashlib.sha1(f"{query}|{may}|{vis}".encode()).hexdigest()[:12]
        return f'W/"pp-{self.gen}-{h}"'

    def get(self, q: dict, may: str) -> dict:
        """GET /api/cad/preplan: ?bbox=w,s,e,n (the layer) | ?incident=<id> | ?where=<address> (one building) | all
        (admins: every point; all=1 the deleted too)."""
        def one(k: str, n: int = 120) -> str:
            return ((q.get(k) or [""])[0] or "").strip()[:n]
        now = self.cad.clock()
        if one("bbox"):
            from sizeup.gis import parse_bbox
            try:
                box = parse_bbox(one("bbox"), BBOX_MAX_DEG)
            except ValueError as e:
                raise CadError(str(e)) from None
            return {"points": self.in_box(box, may), "gen": self.gen}
        if one("incident"):
            try:
                inc = self.store.incident(int(one("incident")))
            except (TypeError, ValueError):
                raise CadError("incident: a number") from None
            return {"points": self.for_incident(inc, may), "gen": self.gen}
        if one("where"):
            k = self.cad.premise._key_text(one("where"))
            if not k.bldg_key:
                raise CadError("Not an address the map knows")
            rows = self.store.q("SELECT * FROM preplan_points WHERE bldg_key = ? AND active = 1", (k.bldg_key,))
            return {"points": [self.shown(r, now) for r in self.order(rows) if self.can_see(r, may)], "gen": self.gen,
                    "key": {"bldg_key": k.bldg_key, "label": k.label}, "point": self._point_of(one("where"))}
        if may != "admin":
            raise PermissionError("every pre-plan point at once is for admins")
        rows = self.store.q("SELECT * FROM preplan_points" + ("" if one("all") == "1" else " WHERE active = 1")
                            + " ORDER BY addr_label, id LIMIT 5000")
        return {"points": [self.shown(r, now) for r in rows], "gen": self.gen}

    def _point_of(self, text: str) -> list[float] | None:
        """A typed address's map point (the Search page's building map), when the gazetteer is sure of it."""
        try:
            w = self.cad.premise._where({"where": text})
        except CadError:
            return None
        return [w["lat"], w["lon"]] if w["lat"] is not None else None

    def csv(self) -> tuple[bytes, int]:
        """GET /api/cad/preplan.csv (admins): every active point."""
        from sizeup.cad.export import _csv, ts
        rows = self.store.q("SELECT * FROM preplan_points WHERE active = 1 ORDER BY addr_label, id")
        out = [[r["addr_label"], KINDS.get(r["kind"], r["kind"]), r["label"], r["note"], r["level"] or "",
                PRIORITIES.get(r["priority"], ""), f"{r['lat']:.6f}", f"{r['lon']:.6f}", r["link"], r["building_ref"] or "",
                ts(r["review_by"]) if r["review_by"] else "", who(r["updated_by"]), ts(r["updated_at"])] for r in rows]
        return _csv(["Building", "Kind", "Label", "Note", "Level", "Priority", "Lat", "Lon", "Linked", "Footprint",
                     "Review by", "By", "Changed"], out), len(rows)

    # -- the building a point belongs to ----------------------------------------------------------------------------------

    def _parcel_key(self, fp: dict) -> addrkey.Key | None:
        """The footprint's parcel's address, keyed as calls are (only when the gazetteer is sure of it). The parcel is the
        one most of the footprint is on (samples(): one answer per footprint, wherever it was tapped; a tie: the first
        address in order), never the one under the tap."""
        from sizeup.gis import contains, geometry_bbox
        gis = getattr(self.cad, "gis", None)
        if gis is None or PARCELS not in getattr(gis, "layers", {}):
            return None
        g = fp["geometry"]
        b = geometry_bbox(g)
        if b is None:
            return None
        lots = gis.features_in(PARCELS, b)
        votes: dict[tuple, int] = {}
        for y, x in samples(g):
            for f in lots:
                if contains(f.get("geometry") or {}, y, x):
                    pr = f.get("properties") or {}
                    t = (str(pr.get("PARCEL_ADDR") or "").strip(), str(pr.get("MUNI_NAME") or "").strip())
                    votes[t] = votes.get(t, 0) + 1
                    break
        if not votes:
            return None
        (addr, muni), _ = min(votes.items(), key=lambda kv: (-kv[1], kv[0]))
        if not addr:
            return None
        k = self.cad.premise._key_text(f"{addr}, {muni}" if muni else addr)
        sure = k.how == "gazetteer" or (self.cad.geocoder is None and k.how == "text")
        return k if sure and k.bldg_key else None

    def associate(self, lat: float, lon: float, ctx: dict | None, link: str = "auto") -> dict:
        """Where a point at lat / lon belongs: {bldg_key, addr_key, addr_label, building_ref, link, dist_m, choices,
        geometry}. ctx: the call's (or typed address's, or the point's own) building {bldg_key, addr_key, label, lat,
        lon}. link: auto | building (the footprint's address) | call (ctx) | point (none). choices: what each link would
        give, for the editor's picker."""
        gis = getattr(self.cad, "gis", None)
        fp = footprint(gis, lat, lon)
        k = self._parcel_key(fp) if fp else None
        ctx = ctx or {}
        ck = ctx.get("bldg_key") or ""
        same = elsewhere = False          # in the call's own footprint | in another one the call's pin is known to be in
        if fp and ctx and (ck or ctx.get("lat") is not None):
            if k is not None and ck and k.bldg_key == ck:
                same = True
            elif ctx.get("lat") is not None:
                cf = footprint(gis, ctx["lat"], ctx["lon"])
                same = cf is not None and cf["ref"] == fp["ref"]
                elsewhere = cf is not None and not same       # no footprint of its own (no pin, or none near it): unknown
        fp_how = fp["how"] if fp else "call"
        own = k is not None and (not same or not ck)          # the footprint's key: another building's, or the call has none
        choices = {}
        if own:
            choices["building"] = {"bldg_key": k.bldg_key, "addr_key": k.addr_key, "addr_label": k.label, "link": fp_how}
        if ctx and (ck or ctx.get("lat") is not None):
            choices["call"] = {"bldg_key": ck, "addr_key": ctx.get("addr_key") or None, "addr_label": ctx.get("label") or "",
                               "link": fp_how if same else "call"}
        choices["point"] = {"bldg_key": "", "addr_key": None, "addr_label": "", "link": "point"}
        if link == "auto":
            if own:
                pick = choices["building"]
            elif elsewhere and "call" in choices:             # another building's footprint with no address: not the call's
                pick = choices["point"]
            else:
                pick = choices.get("call") or choices["point"]
        elif link in choices:
            pick = choices[link]
        elif link == "building" and same and k is not None and "call" in choices:
            pick = choices["call"]          # the footprint's key is the call's (the preview saw the call before it was keyed)
        else:
            raise CadError("link: " + " | ".join(("auto", *choices)))
        return {**pick, "building_ref": fp["ref"] if fp else None,
                "dist_m": fp["dist_m"] if fp else None, "geometry": fp["geometry"] if fp else None,
                "choices": {c: v["addr_label"] or ("Map point only" if c == "point" else "") for c, v in choices.items()},
                "footprints": gis is not None and LAYER in getattr(gis, "layers", {}) and bool(gis.status.get(LAYER, {}).get("at"))}

    def _ctx(self, body: dict, write: bool = True) -> dict | None:
        """The building a new point is placed for: a call's (incident_id), or a typed address's (where). write=False (a
        GET: the editor's preview): the call's keys as they are, never keyed here."""
        iid = body.get("incident_id")
        if iid not in (None, ""):
            try:
                inc = self.store.incident(int(iid))
            except (TypeError, ValueError):
                raise CadError("incident_id: a number") from None
            if write:
                self.cad.addrs.key_one(inc["id"])
                inc = self.store.incident(inc["id"])
            return {"bldg_key": inc["bldg_key"], "addr_key": inc["addr_key"] or None, "lat": inc["lat"], "lon": inc["lon"],
                    "label": (inc["addr_label"] or inc["address"] or inc["geo_label"] or "")[:200]}
        if str(body.get("where") or "").strip():
            w = self.cad.premise._where({"where": body.get("where")})
            return {"bldg_key": w["bldg_key"], "addr_key": w["addr_key"] or None, "lat": w["lat"], "lon": w["lon"],
                    "label": w["label"]}
        return None

    def preview(self, q: dict, may: str = "admin") -> dict:
        """GET /api/cad/preplan/where?lat=&lon=&incident_id= | where= | point=<id>&link=: where a point placed there
        would belong (dispatchers and admins: the editor). A point the person may not see, or a deleted one, is no point;
        nothing is written."""
        def one(k: str) -> str:
            return ((q.get(k) or [""])[0] or "").strip()[:200]
        lat, lon = self._point({"lat": one("lat"), "lon": one("lon")})
        pid = one("point")
        if pid:
            p = self.store.one("SELECT * FROM preplan_points WHERE id = ?", (int(pid) if pid.isdigit() else -1,))
            if p is None or not p["active"] or not self.can_see(p, may):
                raise CadError(f"no point {pid}")
            ctx = self._own(p)
        else:
            ctx = self._ctx({"incident_id": one("incident_id") or None, "where": one("where")}, write=False)
        return self.associate(lat, lon, ctx, one("link") or "auto")

    @staticmethod
    def _own(p: dict) -> dict:
        return {"bldg_key": p["bldg_key"], "addr_key": p["addr_key"], "label": p["addr_label"], "lat": p["lat"], "lon": p["lon"]}

    # -- writing --------------------------------------------------------------------------------------------------------

    @staticmethod
    def _point(body: dict) -> tuple[float, float]:
        try:
            lat, lon = float(body.get("lat")), float(body.get("lon"))
        except (TypeError, ValueError):
            raise CadError("lat, lon: the point on the map") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or math.isnan(lat) or math.isnan(lon):
            raise CadError("lat, lon: the point on the map")
        return round(lat, 7), round(lon, 7)

    @staticmethod
    def _when(v) -> float | None:
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        try:
            d = datetime.fromisoformat(str(v)[:40])
        except ValueError:
            raise CadError("review_by: a date (2027-06-30)") from None
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()

    def _fields(self, body: dict, partial: bool) -> dict:
        f: dict = {}
        if not partial or "kind" in body:
            k = str(body.get("kind") or "")
            if k not in KINDS:
                raise CadError(f"kind: one of {', '.join(KINDS)}")
            f["kind"] = k
        for k, n in (("label", LABEL_MAX), ("note", NOTE_MAX)):
            if not partial or k in body:
                t = " ".join(str(body.get(k) or "").split())
                if len(t) > n:
                    raise CadError(f"{k}: at most {n} characters")
                f[k] = t
        if not partial or "level" in body:
            t = " ".join(str(body.get("level") or "").split())
            if len(t) > LEVEL_MAX:
                raise CadError(f"level: at most {LEVEL_MAX} characters")
            f["level"] = t or None
        if not partial or "priority" in body:
            try:
                p = int(body.get("priority") or 2)
            except (TypeError, ValueError):
                p = 0
            if p not in PRIORITIES:
                raise CadError("priority: 1 (high), 2 (normal) or 3 (low)")
            f["priority"] = p
        if not partial or "review_by" in body:
            f["review_by"] = self._when(body.get("review_by"))
        return f

    def _audit(self, db, op: str, p: dict, old: dict | None, new: dict | None) -> None:
        """A restricted kind's point (or one moved in or out of one) is only its id, its kind and the names of what
        changed: never its label, note or point."""
        vis = self.visible()
        kinds = {p["kind"], (old or {}).get("kind"), (new or {}).get("kind")}
        if any(k in vis for k in kinds if isinstance(k, str)):
            shape = lambda d, kind: None if d is None else {"kind": kind, "changed": sorted(d)}   # noqa: E731
            Editor._audit(db, self.cad.clock(), op, SECTION, str(p["id"]),
                          shape(old, (old or {}).get("kind", p["kind"])), shape(new, (new or {}).get("kind", p["kind"])))
            return
        Editor._audit(db, self.cad.clock(), op, SECTION, f"{p['id']} {KINDS.get(p['kind'], p['kind'])}", old, new)

    def redact_audit(self, rows: list[dict], may: str) -> list[dict]:
        """The Settings audit log as this reader may see it: a point of a restricted kind (any row written before it was
        restricted too) reduced to its id, its kind now and the names of what changed."""
        ids = {int(m.group(1)) for r in rows if r.get("section") == SECTION and (m := re.match(r"(\d+)", r.get("key") or ""))}
        if not ids:
            return rows
        kinds = {r["id"]: r["kind"] for r in self.store.q(
            f"SELECT id, kind FROM preplan_points WHERE id IN ({','.join('?' * len(ids))})", list(ids))}

        def names(v):
            try:
                d = json.loads(v) if v else None
            except ValueError:
                return None
            return sorted(d) if isinstance(d, dict) else None
        def row_kinds(r) -> list[str]:
            found = []
            for v in (r.get("old"), r.get("new")):
                try:
                    d = json.loads(v) if v else None
                except ValueError:
                    d = None
                if isinstance(d, dict) and isinstance(d.get("kind"), str):
                    found.append(d["kind"])
            return found
        vis = self.visible()
        out = []
        for r in rows:
            m = re.match(r"(\d+)", r.get("key") or "") if r.get("section") == SECTION else None
            kind = kinds.get(int(m.group(1))) if m else None
            hidden = [k for k in row_kinds(r) if not may_see(k, may, vis)] if m else []
            if hidden and kind is not None and may_see(kind, may, vis):
                kind = hidden[0]                  # a row from when it was a kind they may not see: that kind's rule
            if m and (kind is None or not may_see(kind, may, vis)):
                def shape(v, kind=kind):
                    n = names(v)
                    return "" if n is None else json.dumps({"kind": kind, "changed": n}, separators=(",", ":"))
                r = dict(r, key=m.group(1), old=shape(r.get("old")), new=shape(r.get("new")))
            out.append(r)
        return out

    def _bump(self) -> None:
        with self._lock:
            self.gen += 1

    def create(self, body: dict) -> dict:
        """POST /api/cad/preplan {kind, lat, lon, label?, note?, level?, priority?, review_by?, incident_id | where?,
        link?: auto | building | call | point}."""
        f = self._fields(body, partial=False)
        may = str(body.get("_may") or "admin")
        if not may_see(f["kind"], may, self.visible()):
            raise CadError(f"kind: one of {', '.join(k for k in KINDS if may_see(k, may, self.visible()))}")
        lat, lon = self._point(body)
        a = self.associate(lat, lon, self._ctx(body), str(body.get("link") or "auto"))
        op = str(body.get("operator") or "")[:320]
        now = self.cad.clock()
        with self.store.tx() as db:
            cur = db.execute(
                "INSERT INTO preplan_points (bldg_key, addr_key, building_ref, link, addr_label, kind, label, note, lat, lon, "
                "cell, level, priority, active, created_by, created_at, updated_by, updated_at, review_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (a["bldg_key"], a["addr_key"], a["building_ref"], a["link"], a["addr_label"][:200], f["kind"], f["label"],
                 f["note"], lat, lon, cell_of(lat, lon), f["level"], f["priority"], op, now, op, now, f["review_by"]))
            p = dict(db.execute("SELECT * FROM preplan_points WHERE id = ?", (cur.lastrowid,)).fetchone())
            self._audit(db, op, p, None, {k: p[k] for k in AUDITED})
        self._bump()
        return {"ok": True, "point": self.shown(p, now)}

    def edit(self, pid: int, body: dict) -> dict:
        """POST /api/cad/preplan/<id> {kind?, label?, note?, level?, priority?, review_by?, lat? lon? (a move: its
        building looked at again, link?), action?: delete | restore}. A point of a kind the person may not see is, for
        them, no point (body["_may"]: their standing, the API's)."""
        may = str(body.get("_may") or "admin")
        p = self.store.one("SELECT * FROM preplan_points WHERE id = ?", (pid,))
        if p is None or not self.can_see(p, may):
            raise CadError(f"no point {pid}")
        f = self._fields(body, partial=True)
        if "kind" in f and not may_see(f["kind"], may, self.visible()):
            raise CadError(f"kind: one of {', '.join(k for k in KINDS if may_see(k, may, self.visible()))}")
        act = str(body.get("action") or "")
        if act == "delete":
            f["active"] = 0
        elif act == "restore":
            if STANDING.get(may, 0) < STANDING["admin"]:
                raise CadError("restore: admins")
            f["active"] = 1
        elif act:
            raise CadError("action: delete or restore")
        if "lat" in body or "lon" in body or "link" in body:
            lat, lon = self._point({"lat": body.get("lat", p["lat"]), "lon": body.get("lon", p["lon"])})
            a = self.associate(lat, lon, self._own(p), str(body.get("link") or "auto"))
            f.update(lat=lat, lon=lon, cell=cell_of(lat, lon), bldg_key=a["bldg_key"], addr_key=a["addr_key"],
                     building_ref=a["building_ref"], link=a["link"], addr_label=a["addr_label"][:200])
        changes = {k: v for k, v in f.items() if p[k] != v}
        now = self.cad.clock()
        if not changes:
            return {"ok": True, "point": self.shown(p, now), "changed": []}
        with self.store.tx() as db:
            db.execute(f"UPDATE preplan_points SET {', '.join(f'{k} = ?' for k in changes)}, updated_at = ?, updated_by = ? "
                       f"WHERE id = ?", (*changes.values(), now, str(body.get("operator") or "")[:320], pid))
            aud = {k: v for k, v in changes.items() if k in AUDITED}
            self._audit(db, str(body.get("operator") or "")[:320], p, {k: p[k] for k in aud}, aud)
            p = dict(db.execute("SELECT * FROM preplan_points WHERE id = ?", (pid,)).fetchone())
        self._bump()
        changed = sorted(k for k in changes if k in AUDITED and k not in ("cell",))
        if not self.can_see(p, may):
            return {"ok": True, "point": {"id": pid, "kind": p["kind"], "restricted": True}, "changed": changed}
        return {"ok": True, "point": self.shown(p, now), "changed": changed}
