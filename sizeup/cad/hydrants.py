"""Hydrants (#799): the department's own data laid over the county's hydrant layer (sizeup/gis.py "hydrants",
DataHub_Infrastructure/MapServer/147): status (out of service, restricted, dry), flow and its NFPA 291 class, thread,
outlets, main, owner, the water company's asset id, a nudged position, hydrants the county lacks and county ones that
are gone. Admins edit it (on the map, in Settings > Hydrants, by CSV); everyone signed in sees it on every CAD map and on
an incident (the nearest hydrants, the out-of-service ones called out).

Department data: it lives only in the hub's CAD database (the `hydrants` table, replicated by Litestream like the rest
of the CAD), never in the repo. Every change is in the Settings audit log (section "hydrants", key: the hydrant's key),
which is the hydrant's history on its card.

The key. The county layer has no hydrant id of its own: its fields are OBJECTID, DISTRICT, MUNCIPALIT, HYD_NUMBER and
FIRENAME (surveyed 2026-11-20), and a hydrant number repeats within a district (351 of 3,874 district + number pairs in
our box). So a county hydrant is keyed by its OBJECTID ("c<OBJECTID>": the feature id the service gives, sizeup/gis.py
keeps it), with an anchor kept beside the overlay when it is first edited (the district, the number and the county's
position then). The data is as of 2015 and the MapServer has no edit tracking, so the ids have been the same at every
fetch; but a republished layer could renumber them. Each time the merge is built (sizeup/gis.py serves the layer through
it) an overlay row is bound to:
  its own id     the county feature with that id, when it is still the same hydrant (same district and number within
                 REBIND_M of the anchor, or any number within SAME_M: a corrected number)
  rebound        else the one feature not already bound whose district and number match within REBIND_M of the anchor,
                 or the one within SAME_M of it: the county renumbered its ids (nothing is written: the binding is made
                 again at every build, from the anchor)
  missing        else none: the overlay is kept and drawn where it was, flagged "missing from county" for the admins
A hub hydrant ("h<n>": one the county lacks, a private one) has its own point. Removed: a county hydrant hidden from
every map (reversible); the county row itself is never edited.

The class (NFPA 291, Recommended Practice for Fire Flow Testing and Marking of Hydrants, Table 5.1 (2019 ed.): the
bonnet and nozzle caps by rated capacity at 20 psi residual): AA >= 1500 GPM light blue, A 1000-1499 green, B 500-999
orange, C < 500 red. Checked against the University of Tennessee MTAS's summary of Table 5.1
(https://www.mtas.tennessee.edu/reference/marking-all-fire-hydrants) and the PA DEP's hydrant colour table
(https://www.dep.state.pa.us/dep/deputate/waterops/redesign/TablesNFormulas/Pages/firehydrantcolor.htm). The same
practice recommends a chrome yellow barrel for public hydrants and another colour (most often red) for private ones:
kept as information (body_color), never what the map draws. With a static and a residual pressure the class is taken
from the flow at 20 psi residual (NFPA 291's Q_R = Q_F x ((S - 20) / (S - R)) ^ 0.54), else from the flow as entered
(a water company's rated capacity). No flow: no class (never red: red means under 500 GPM). An admin may set the class.

HTTP (sizeup/cad/api.py): GET /api/cad/hydrants/<key> (anyone signed in: the card and its history),
GET /api/cad/hydrants (?q, status, flag, town: admins: the Settings list), GET /api/cad/hydrants.csv (admins),
POST /api/cad/hydrants (admins: a new one), POST /api/cad/hydrants/<key> (admins: edit, move, remove, restore),
POST /api/cad/hydrants/import {action: preview | apply, csv} (admins).
"""

from __future__ import annotations

import csv
import io
import math
import re
import threading
import time
from datetime import date

from sizeup.cad.premise import who
from sizeup.cad.settings import Editor
from sizeup.cad.store import CadError

LAYER = "hydrants"                    # the county layer (sizeup/gis.py DEFAULT_LAYERS)
TOWNS = "municipalities"              # the town a hydrant is in (the layer's MUNCIPALIT is the fire district's name)
SECTION = "hydrants"                  # the Settings audit log's section
NUMBER, DISTRICT = "HYD_NUMBER", "FIRENAME"
KEY_RE = re.compile(r"[ch][1-9]\d{0,9}(?:-[1-9]\d{0,3})?")    # c<id>-2: a county id whose own key a rebound row has
STATUSES = {"in_service": "In service", "oos": "Out of service", "restricted": "Restricted", "dry": "Dry", "unknown": "Unknown"}
DOWN = ("oos", "dry")                 # drawn with the out-of-service mark, called out near a call
# NFPA 291 Table 5.1 (the module doc): (class, the least rated GPM, the cap colour)
CLASSES = (("AA", 1500, "Light blue"), ("A", 1000, "Green"), ("B", 500, "Orange"), ("C", 0, "Red"))
THREADS = {"nst": "NST (NH)", "nyc": "NYC / NYFD pipe", "storz4": "Storz 4\"", "storz5": "Storz 5\"", "other": "Other"}
OWNERS = {"water_company": "Water company", "municipal": "Municipal", "private": "Private"}
REBIND_M = 150.0                      # the same district and number this close to the anchor: the same hydrant
SAME_M = 3.0                          # this close to the anchor: the same hydrant whatever its number says
SAME_POINT_M = 0.5                    # an imported point this close to where it is: no move
MAX_NUDGE_M = 300.0                   # a county hydrant moved further than this: a mistake (Remove it and Add one)
NEAR_N, NEAR_M = 5, 300.0             # an incident's nearest hydrants: at most this many, this close
CELL_DEG = 0.002                      # the grid the nearest search reads (about 220 m x 170 m)
MAX_IMPORT_ROWS = 20000
LIST_MAX = 500                        # rows a list page at most

# The overlay's fields: name -> (kind, arg). enum: the allowed values ("" clears); text: the most characters; int / float:
# (lo, hi); date: YYYY-MM-DD. Their order is the CSV's.
FIELDS: dict[str, tuple[str, object]] = {
    "number": ("text", 20),           # the hydrant's number as shown ("" : the county's)
    "status": ("enum", tuple(STATUSES)),
    "status_reason": ("text", 200),
    "status_since": ("date", None),
    "status_until": ("date", None),   # expected back
    "flow_gpm": ("int", (0, 20000)),
    "flow_date": ("date", None),
    "static_psi": ("float", (0.0, 300.0)),
    "residual_psi": ("float", (0.0, 300.0)),
    "nfpa_class": ("enum", tuple(c for c, _, _ in CLASSES)),   # "" : from the flow
    "thread": ("enum", tuple(THREADS)),
    "outlets": ("text", 60),          # "2x2.5, 1x4.5"
    "main_in": ("float", (0.0, 60.0)),
    "owner": ("enum", tuple(OWNERS)),
    "body_color": ("text", 30),
    "asset_id": ("text", 40),
    "location": ("text", 120),        # street / where it stands
    "inspected": ("date", None),
    "notes": ("text", 500),
}
AUDITED = (*FIELDS, "lat", "lon", "removed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hydrants (
  key TEXT PRIMARY KEY,                 -- "c<county OBJECTID>" | "h<n>" (a hub hydrant)
  county_id INTEGER,                    -- the county feature's id; NULL: a hub hydrant
  anchor_no TEXT NOT NULL DEFAULT '',   -- the county's district, number and point when first edited (the binding)
  anchor_dist TEXT NOT NULL DEFAULT '',
  anchor_lat REAL, anchor_lon REAL,
  lat REAL, lon REAL,                   -- a nudged point (a county one), the point (a hub one); NULL: the county's
  removed INTEGER NOT NULL DEFAULT 0,   -- 1: hidden from every map (a county one that isn't there; a hub one deleted)
  number TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unknown',
  status_reason TEXT NOT NULL DEFAULT '',
  status_since TEXT, status_until TEXT,
  flow_gpm INTEGER, flow_date TEXT, static_psi REAL, residual_psi REAL,
  nfpa_class TEXT,                      -- set by an admin; NULL: from the flow
  thread TEXT, outlets TEXT NOT NULL DEFAULT '', main_in REAL, owner TEXT, body_color TEXT NOT NULL DEFAULT '',
  asset_id TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', inspected TEXT, notes TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
  updated_by TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS hydrants_county ON hydrants(county_id);
"""
TEXT_COLS = {"number", "status_reason", "outlets", "body_color", "asset_id", "location", "notes"}   # NOT NULL '' columns
# a hydrant with no overlay row: what each column would be
DEFAULTS = {k: ("unknown" if k == "status" else "" if k in TEXT_COLS else None) for k in FIELDS} | {
    "lat": None, "lon": None, "removed": 0}


# ---- pure helpers ---------------------------------------------------------------------------------------------------

def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    ky, kx = 110_574.0, 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * ky, (lon2 - lon1) * kx)


def rated_gpm(flow, static, residual) -> int | None:
    """The flow at 20 psi residual (NFPA 291: Q_R = Q_F x ((S - 20) / (S - R)) ^ 0.54) when both pressures make sense,
    else the flow as entered (a rated capacity); None without a flow."""
    if flow is None:
        return None
    if static is not None and residual is not None and static > 20 and static > residual >= 0:
        return int(round(flow * ((static - 20) / (static - residual)) ** 0.54))
    return int(flow)


def class_of(gpm) -> str | None:
    """NFPA 291's class for a rated capacity (GPM); None without one (never C: red means under 500 GPM)."""
    if gpm is None:
        return None
    return next(c for c, lo, _ in CLASSES if gpm >= lo)


def color_of(cls: str | None) -> str:
    return next((name for c, _, name in CLASSES if c == cls), "")


def _norm(v) -> str:
    return " ".join(str(v or "").split()).upper()


def _day(v, name: str) -> str | None:
    s = str(v or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})(?: 0?0:00(?::00)?)?", s)        # as a spreadsheet saves it again
    try:
        if m:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?", s):
            return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass
    raise CadError(f"{name}: a date (2027-06-30)")


def clean(name: str, v):
    """One field's value as stored (CadError when it isn't one): "" / None clears it."""
    kind, arg = FIELDS[name]
    if isinstance(v, str):
        v = v.strip()
        if len(v) >= 2 and v[0] == "'" and v[1] in "=+-@":        # the export's guard against a formula (export._cell)
            v = v[1:]
    if v is not None and not isinstance(v, (str, int, float)):
        raise CadError(f"{name}: a value")
    if kind == "text":
        t = " ".join(str(v or "").split()).replace("×", "x")
        if len(t) > arg:
            raise CadError(f"{name}: at most {arg} characters")
        return t
    if v in (None, ""):
        return "unknown" if name == "status" else None
    if kind == "enum":
        t = str(v).strip()
        low = {k.lower(): k for k in arg}
        if name == "status":                                    # the labels too ("Out of service", "OOS")
            low |= {lab.lower(): k for k, lab in STATUSES.items()} | {"oos": "oos", "in service": "in_service"}
        if name == "thread":
            low |= {lab.lower(): k for k, lab in THREADS.items()}
        if name == "owner":
            low |= {lab.lower(): k for k, lab in OWNERS.items()}
        k = low.get(t.lower())
        if k is None:
            raise CadError(f"{name}: one of {', '.join(arg)}")
        return k
    if kind == "date":
        return _day(v, name)
    lo, hi = arg
    try:
        x = float(str(v).replace(",", "")) if not isinstance(v, (int, float)) or isinstance(v, bool) else float(v)
    except (ValueError, OverflowError):
        raise CadError(f"{name}: a number") from None
    if isinstance(v, bool) or not math.isfinite(x) or not lo <= x <= hi:
        raise CadError(f"{name}: a number from {lo:g} to {hi:g}")
    if kind == "int":
        if x != int(x):
            raise CadError(f"{name}: a whole number")
        return int(x)
    return round(x, 1)


def point(lat, lon) -> tuple[float, float]:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        raise CadError("lat, lon: the point on the map") from None
    if not (math.isfinite(la) and math.isfinite(lo) and -90 <= la <= 90 and -180 <= lo <= 180):
        raise CadError("lat, lon: the point on the map")
    return round(la, 7), round(lo, 7)


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / CELL_DEG), math.floor(lon / CELL_DEG)


# ---- the service ----------------------------------------------------------------------------------------------------

class Hydrants:
    """One per Cad (sizeup/cad/service.py); the gis's overlay for its "hydrants" layer (sizeup/app.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        # bumped on every write: the merge, the layer's rev and ETag, the pages' copies. From this process's start in ms,
        # so a new task never repeats an earlier one's (as preplan.py)
        self.gen = int(time.time() * 1000)
        self._lock = threading.Lock()
        self._idx: tuple | None = None          # (signature, index)
        self._towns: dict = {"rev": None, "at": {}}
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'incidents'").fetchone():
                self.store.db.executescript(SCHEMA)

    # -- the county layer and the merge -------------------------------------------------------------------------------

    def _gis(self):
        g = getattr(self.cad, "gis", None)
        return g if g is not None and LAYER in getattr(g, "layers", {}) else None

    def _county(self) -> tuple[object, list[dict]]:
        g = self._gis()
        if g is None:
            return None, []
        return g.status[LAYER]["rev"], g.features(LAYER)

    def index(self) -> dict:
        """The merge (the module doc), rebuilt when the county layer or the overlay changes: {items: [item], by_key,
        grid, missing}. An item: {key, fid, lat, lon, props (the county's), row (the overlay or None), missing, rebound,
        removed, county_lat, county_lon}."""
        rev, feats = self._county()
        sig = (rev, len(feats), self.gen)
        with self._lock:
            if self._idx is not None and self._idx[0] == sig:
                return self._idx[1]
        rows = self.store.q("SELECT * FROM hydrants")     # (never under our lock: a caller may hold the store's)
        idx = self._build(feats, rows)
        with self._lock:
            if self.gen == sig[2]:                         # a write since: built again by the next reader
                self._idx = (sig, idx)
        return idx

    @staticmethod
    def _same(f: dict, r: dict, lat: float, lon: float) -> bool:
        if r["anchor_lat"] is None:
            return True
        d = metres(lat, lon, r["anchor_lat"], r["anchor_lon"])
        p = f.get("properties") or {}
        return d <= SAME_M or (d <= REBIND_M and _norm(p.get(NUMBER)) == r["anchor_no"] and _norm(p.get(DISTRICT)) == r["anchor_dist"])

    def _build(self, feats: list[dict], rows: list[dict]) -> dict:
        pts: dict = {}
        plain = []                                   # a county feature with no id: served as it is (drawn, not editable)
        for f in feats:
            fid = f.get("id")
            c = (f.get("geometry") or {}).get("coordinates")
            if not isinstance(c, list) or len(c) < 2:
                continue
            if fid is None:
                plain.append(f)
                continue
            pts[fid] = (f, float(c[1]), float(c[0]))
        county_rows = [r for r in rows if r["county_id"] is not None]
        bound: dict = {}                             # county feature id -> (row, rebound)
        loose = []
        for r in county_rows:
            hit = pts.get(r["county_id"])
            if hit is not None and self._same(hit[0], r, hit[1], hit[2]):
                bound[r["county_id"]] = (r, False)
            else:
                loose.append(r)
        missing = []
        if loose:
            # the candidates: every feature no row is bound to directly (an id another row once had included: swapped
            # or reused ids follow their hydrants); by district and number, and by point (the grid: never a full scan)
            by_fp: dict = {}
            near: dict = {}
            for fid, (f, la, lo) in pts.items():
                if fid in bound:
                    continue
                p = f.get("properties") or {}
                by_fp.setdefault((_norm(p.get(DISTRICT)), _norm(p.get(NUMBER))), []).append((fid, la, lo))
                near.setdefault(_cell(la, lo), []).append((fid, la, lo))
            for r in loose:
                cands = []
                if r["anchor_lat"] is not None:
                    alat, alon = r["anchor_lat"], r["anchor_lon"]
                    cands = [x for x in by_fp.get((r["anchor_dist"], r["anchor_no"]), [])
                             if x[0] not in bound and metres(x[1], x[2], alat, alon) <= REBIND_M]
                    if len(cands) != 1:
                        cy, cx = _cell(alat, alon)
                        cands = [x for y in (cy - 1, cy, cy + 1) for xx in (cx - 1, cx, cx + 1) for x in near.get((y, xx), ())
                                 if x[0] not in bound and metres(x[1], x[2], alat, alon) <= SAME_M]
                if len(cands) == 1:
                    bound[cands[0][0]] = (r, True)
                else:
                    missing.append(r)
        taken = {r["key"] for r in rows}
        items = []
        for fid, (f, la, lo) in pts.items():
            r, rebound = bound.get(fid, (None, False))
            if r is not None:
                key = r["key"]
            else:                                    # its own key, unless a row bound elsewhere (or missing) has it
                key, n = f"c{fid}", 1
                while key in taken:
                    n += 1
                    key = f"c{fid}-{n}"
            items.append(self._item(key, fid, la, lo, f.get("properties") or {}, r, False, rebound))
        for r in missing:                            # kept, where it was: flagged for the admins
            la = r["lat"] if r["lat"] is not None else r["anchor_lat"]
            lo = r["lon"] if r["lon"] is not None else r["anchor_lon"]
            if la is None:
                continue
            props = {NUMBER: r["anchor_no"], DISTRICT: r["anchor_dist"]}
            items.append(self._item(r["key"], r["county_id"], r["anchor_lat"] if r["anchor_lat"] is not None else la,
                                    r["anchor_lon"] if r["anchor_lon"] is not None else lo, props, r, True, False))
        for r in rows:
            if r["county_id"] is None and r["lat"] is not None:
                items.append(self._item(r["key"], None, None, None, {}, r, False, False))
        by_key = {it["key"]: it for it in items}
        grid: dict = {}
        for it in items:
            if not it["removed"]:
                grid.setdefault(_cell(it["lat"], it["lon"]), []).append(it)
        return {"items": items, "by_key": by_key, "grid": grid, "missing": sum(1 for it in items if it["missing"]),
                "plain": plain}

    @staticmethod
    def _item(key, fid, clat, clon, props, r, missing, rebound) -> dict:
        lat = r["lat"] if r is not None and r["lat"] is not None else clat
        lon = r["lon"] if r is not None and r["lon"] is not None else clon
        st = (r["status"] if r is not None else "") or "unknown"
        gpm = rated_gpm(r["flow_gpm"], r["static_psi"], r["residual_psi"]) if r is not None else None
        cls = (r["nfpa_class"] if r is not None else None) or class_of(gpm)
        number = (r["number"] if r is not None and r["number"] else "") or str(props.get(NUMBER) or "").strip()
        return {"key": key, "fid": fid, "lat": lat, "lon": lon, "county_lat": clat, "county_lon": clon, "props": props,
                "row": r, "missing": missing, "rebound": rebound, "removed": bool(r and r["removed"]), "status": st,
                "gpm": gpm, "cls": cls, "number": number, "hub": fid is None}

    def apply(self, key: str, feats: list[dict]) -> list[dict]:
        """sizeup/gis.py: the layer as the pages get it: the county's with the overlay (moved, removed, missing, hub
        hydrants added), each with _k (its key), _no (the number shown), _cls, _gpm, _st (when known), _miss."""
        if key != LAYER:
            return feats
        out = []
        idx = self.index()
        for it in idx["items"]:
            if it["removed"]:
                continue
            p = dict(it["props"])
            p["_k"] = it["key"]
            if it["number"]:
                p["_no"] = it["number"]
            if it["cls"]:
                p["_cls"] = it["cls"]
            if it["gpm"] is not None:
                p["_gpm"] = it["gpm"]
            if it["status"] != "unknown":
                p["_st"] = it["status"]
            if it["missing"]:
                p["_miss"] = True
            if it["hub"]:
                p["_hub"] = True
            f = {"type": "Feature", "properties": p,
                 "geometry": {"type": "Point", "coordinates": [round(it["lon"], 7), round(it["lat"], 7)]}}
            if it["fid"] is not None and not it["missing"]:   # (a missing one's id may be another's now)
                f["id"] = it["fid"]
            out.append(f)
        # (a copy of the layer without ids, until it is fetched again: never a layer with nothing on it, #831)
        out.extend(idx["plain"])
        return out

    # -- the town (the layer's MUNCIPALIT is the fire district's name: Rye Brook's say PORT_CHESTER) --------------------

    def town(self, it: dict) -> str:
        g = getattr(self.cad, "gis", None)
        if g is None or TOWNS not in getattr(g, "layers", {}):
            return ""
        rev = g.status[TOWNS]["rev"]
        with self._lock:
            if self._towns["rev"] != rev:
                self._towns = {"rev": rev, "at": {}}
            at = self._towns["at"]
        k = (round(it["lat"], 6), round(it["lon"], 6))
        if k not in at:
            p = g.feature_at(TOWNS, it["lat"], it["lon"])
            at[k] = str((p or {}).get("NAME") or "").strip()
        return at[k]

    # -- reading ------------------------------------------------------------------------------------------------------

    def shown(self, it: dict, full: bool = True) -> dict:
        r = it["row"] or {}
        out = {"key": it["key"], "number": it["number"], "lat": it["lat"], "lon": it["lon"], "status": it["status"],
               "cls": it["cls"], "gpm": it["gpm"], "removed": it["removed"], "missing": it["missing"], "hub": it["hub"]}
        if not full:
            return out
        p = it["props"]
        nudge = None
        if not it["hub"] and r.get("lat") is not None and it["county_lat"] is not None:
            nudge = round(metres(it["county_lat"], it["county_lon"], it["lat"], it["lon"]), 1)
        out.update({
            "county_id": it["fid"], "county_number": str(p.get(NUMBER) or "").strip(), "district": str(p.get(DISTRICT) or "").strip(),
            "town": self.town(it), "county_lat": it["county_lat"], "county_lon": it["county_lon"], "nudge_m": nudge,
            "rebound": it["rebound"], "status_label": STATUSES.get(it["status"], it["status"]),
            "class_color": color_of(it["cls"]), "class_set": bool(r.get("nfpa_class")),
            "class_flow": class_of(it["gpm"]), "edited": bool(r),
            "by": who(r.get("updated_by") or r.get("created_by") or ""), "updated_at": r.get("updated_at"),
        })
        for k in FIELDS:
            if k not in out or k == "number":
                out[k] = r.get(k, "" if k in TEXT_COLS else None)
        out["number"] = it["number"]
        out["status"] = it["status"]
        return out

    def _item_or_404(self, key: str) -> dict:
        if not KEY_RE.fullmatch(key or ""):
            raise CadError("no such hydrant")
        it = self.index()["by_key"].get(key)
        if it is None:
            raise CadError(f"no hydrant {key}")
        return it

    def one(self, key: str, may: str = "viewer") -> dict:
        """GET /api/cad/hydrants/<key>: the card and its history (the Settings audit log's rows: anyone signed in reads
        that log in Settings too; hydrant data is no secret, the list and the CSV are admins' as the editors' tools)."""
        out = {"hydrant": self.shown(self._item_or_404(key)), "gen": self.gen}
        out["history"] = self.store.q("SELECT at, operator, old, new FROM settings_audit WHERE section = ? AND key = ? "
                                      "ORDER BY at DESC, id DESC LIMIT 100", (SECTION, key))
        for h in out["history"]:
            h["by"] = who(h.pop("operator") or "")
        return out

    def near(self, lat: float, lon: float, n: int = NEAR_N, within: float = NEAR_M) -> list[dict]:
        """The hydrants within `within` metres, nearest first, at most n (removed ones never)."""
        idx = self.index()
        cy, cx = _cell(lat, lon)
        ry = int(within / 110_574.0 / CELL_DEG) + 1
        rx = int(within / (111_320.0 * max(0.2, math.cos(math.radians(lat)))) / CELL_DEG) + 1
        found = []
        for y in range(cy - ry, cy + ry + 1):
            for x in range(cx - rx, cx + rx + 1):
                for it in idx["grid"].get((y, x), ()):
                    d = metres(lat, lon, it["lat"], it["lon"])
                    if d <= within:
                        found.append((d, it))
        found.sort(key=lambda t: (t[0], t[1]["key"]))
        return [self.shown(it, full=False) | {"dist_m": round(d)} for d, it in found[:n]]

    def for_incident(self, inc: dict) -> dict | None:
        """An incident's hydrants: the nearest (near) and every one out of service within NEAR_M (down)."""
        if inc.get("lat") is None or inc.get("lon") is None:
            return None
        allnear = self.near(inc["lat"], inc["lon"], n=200)
        return {"near": allnear[:NEAR_N], "down": [h for h in allnear if h["status"] in DOWN]}

    def decorate(self, incs: list[dict]) -> None:
        """The state push: each open incident's hydrants ("hydrants")."""
        if self._gis() is None and not self.store.one("SELECT 1 AS x FROM hydrants LIMIT 1"):
            return
        for inc in incs:
            if inc.get("status") in ("queued", "pending", "active") and inc.get("lat") is not None:
                try:
                    h = self.for_incident(inc)
                    if h is not None:
                        inc["hydrants"] = h
                except Exception as e:  # noqa: BLE001 - never the push
                    self.cad.log(f"CAD hydrants: {type(e).__name__}: {e}")

    def state(self) -> dict:
        return {"gen": self.gen}

    FLAGS = {"oos": "Out of service, dry", "restricted": "Restricted", "no_flow": "No flow", "moved": "Moved",
             "removed": "Removed", "missing": "Missing from county", "hub": "Added here", "edited": "Edited"}

    def _match(self, it: dict, f: dict) -> bool:
        flag, st, q, town = f["flag"], f["status"], f["q"], f["town"]
        r = it["row"] or {}
        if not f["all"] and it["removed"] and flag != "removed":
            return False
        if st and it["status"] != st:
            return False
        if flag == "oos" and it["status"] not in DOWN:
            return False
        if flag == "restricted" and it["status"] != "restricted":
            return False
        if flag == "no_flow" and r.get("flow_gpm") is not None:
            return False
        if flag == "moved" and not (not it["hub"] and r.get("lat") is not None):
            return False
        if flag == "removed" and not it["removed"]:
            return False
        if flag == "missing" and not it["missing"]:
            return False
        if flag == "hub" and not it["hub"]:
            return False
        if flag == "edited" and not r:
            return False
        if town and self.town(it).lower() != town.lower():
            return False
        if q:
            hay = " ".join(str(x or "") for x in (it["key"], it["number"], it["props"].get(DISTRICT), r.get("location"),
                                                  r.get("asset_id"), r.get("notes"), r.get("status_reason"))).lower()
            if not all(w in hay for w in q.lower().split()):
                return False
        return True

    @staticmethod
    def parse_query(q: dict) -> dict:
        def one(k: str, n: int = 80) -> str:
            return ((q.get(k) or [""])[0] or "").strip()[:n]
        flag, st = one("flag"), one("status")
        if flag and flag not in Hydrants.FLAGS:
            raise CadError(f"flag: one of {', '.join(Hydrants.FLAGS)}")
        if st and st not in STATUSES:
            raise CadError(f"status: one of {', '.join(STATUSES)}")
        try:
            offset = max(0, int(one("offset") or 0))
            limit = min(LIST_MAX, max(1, int(one("limit") or 100)))
        except ValueError:
            raise CadError("offset, limit: numbers") from None
        return {"flag": flag, "status": st, "q": one("q"), "town": one("town"), "offset": offset, "limit": limit,
                "all": one("all") == "1"}

    def _sorted(self, f: dict) -> list[dict]:
        items = [it for it in self.index()["items"] if self._match(it, f)]
        return sorted(items, key=lambda it: (self.town(it), _num_key(it["number"]), it["key"]))

    def listing(self, q: dict) -> dict:
        """GET /api/cad/hydrants (admins): the Settings list, filtered; the counts for its chips; the towns."""
        f = self.parse_query(q)
        items = self._sorted(f)
        idx = self.index()
        every = idx["items"]
        counts = {"all": sum(1 for it in every if not it["removed"]),
                  "oos": sum(1 for it in every if it["status"] in DOWN and not it["removed"]),
                  "missing": idx["missing"], "removed": sum(1 for it in every if it["removed"]), "unkeyed": len(idx["plain"]),
                  "hub": sum(1 for it in every if it["hub"] and not it["removed"]),
                  "edited": sum(1 for it in every if it["row"])}
        towns = sorted({self.town(it) for it in every} - {""})
        page = items[f["offset"]:f["offset"] + f["limit"]]
        return {"rows": [self.shown(it) for it in page], "total": len(items), "counts": counts, "towns": towns,
                "gen": self.gen, "county": self._gis() is not None}

    # -- writing ------------------------------------------------------------------------------------------------------

    def _bump(self) -> None:
        with self._lock:
            self.gen += 1
            self._idx = None
        with self.store.lock:
            self.store.rev += 1
        g = getattr(self.cad, "gis", None)
        if g is not None and hasattr(g, "overlay_changed"):
            g.overlay_changed(LAYER)

    def _fields(self, body: dict) -> dict:
        return {k: clean(k, body.get(k)) for k in FIELDS if k in body}

    def _bounds(self) -> tuple | None:
        g = getattr(self.cad, "gis", None)
        return tuple(g.bbox) if g is not None and getattr(g, "bbox", None) else None

    def _check_point(self, it: dict | None, lat: float, lon: float) -> None:
        b = self._bounds()
        if b and not (b[0] <= lon <= b[2] and b[1] <= lat <= b[3]):
            raise CadError("lat, lon: outside the map's area")
        if it is not None and not it["hub"] and it["county_lat"] is not None:
            d = metres(it["county_lat"], it["county_lon"], lat, lon)
            if d > MAX_NUDGE_M:
                raise CadError(f"moved {d:.0f} m from the county's point: at most {MAX_NUDGE_M:.0f} m (Remove it and Add one)")

    def _new_row(self, db, it: dict, op: str, now: float) -> dict:
        p = it["props"]
        db.execute("INSERT INTO hydrants (key, county_id, anchor_no, anchor_dist, anchor_lat, anchor_lon, created_by, created_at, "
                   "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (it["key"], it["fid"], _norm(p.get(NUMBER)), _norm(p.get(DISTRICT)), it["county_lat"], it["county_lon"],
                    op, now, op, now))
        return dict(db.execute("SELECT * FROM hydrants WHERE key = ?", (it["key"],)).fetchone())

    def _write(self, db, it: dict, changes: dict, op: str, now: float) -> list[str]:
        """One hydrant's changes in the transaction, audited (the row as it is now, read in the transaction: never the
        index's copy; made first only when something changes). The fields changed."""
        cur = db.execute("SELECT * FROM hydrants WHERE key = ?", (it["key"],)).fetchone()
        r = dict(cur) if cur is not None else dict(DEFAULTS)
        diff = {k: v for k, v in changes.items() if r.get(k) != v}
        if not diff:
            return []
        if cur is None:
            r = self._new_row(db, it, op, now)
        db.execute(f"UPDATE hydrants SET {', '.join(f'{k} = ?' for k in diff)}, updated_by = ?, updated_at = ? WHERE key = ?",
                   (*diff.values(), op, now, it["key"]))
        Editor._audit(db, now, op, SECTION, it["key"], {k: r.get(k) for k in diff}, diff)
        return sorted(diff)

    def _stored(self, f: dict) -> dict:
        return {k: ("" if v is None and k in TEXT_COLS else v) for k, v in f.items()}

    def edit(self, key: str, body: dict) -> dict:
        """POST /api/cad/hydrants/<key> {fields ..., lat?, lon? (a move), action?: remove | restore | reset_position}."""
        it = self._item_or_404(key)
        op = str(body.get("operator") or "")[:320]
        f = self._stored(self._fields(body))
        act = str(body.get("action") or "")
        if act == "remove":
            f["removed"] = 1
        elif act == "restore":
            f["removed"] = 0
        elif act == "reset_position":
            if it["hub"]:
                raise CadError("reset_position: a county hydrant's")
            f.update(lat=None, lon=None)
        elif act:
            raise CadError("action: remove, restore or reset_position")
        if "lat" in body or "lon" in body:
            lat, lon = point(body.get("lat"), body.get("lon"))
            self._check_point(it, lat, lon)
            f.update(lat=lat, lon=lon)
        if it["missing"] and "lat" in f and f["lat"] is None:
            raise CadError("reset_position: the county no longer has this hydrant")
        now = self.cad.clock()
        with self.store.tx() as db:
            changed = self._write(db, it, f, op, now)
        if changed:
            self._bump()
        it = self.index()["by_key"][key]
        return {"ok": True, "hydrant": self.shown(it), "changed": changed}

    def create(self, body: dict) -> dict:
        """POST /api/cad/hydrants {lat, lon, number?, fields ...}: a hydrant the county lacks (a private one, a new one)."""
        if self._gis() is None:
            raise CadError("no hydrant layer here")
        lat, lon = point(body.get("lat"), body.get("lon"))
        self._check_point(None, lat, lon)
        f = self._stored(self._fields(body))
        op = str(body.get("operator") or "")[:320]
        now = self.cad.clock()
        with self.store.tx() as db:
            n = db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(key, 2) AS INTEGER)), 0) + 1 FROM hydrants WHERE key LIKE 'h%'").fetchone()[0]
            key = f"h{n}"
            db.execute("INSERT INTO hydrants (key, lat, lon, created_by, created_at, updated_by, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, lat, lon, op, now, op, now))
            if f:
                db.execute(f"UPDATE hydrants SET {', '.join(f'{k} = ?' for k in f)} WHERE key = ?", (*f.values(), key))
            r = dict(db.execute("SELECT * FROM hydrants WHERE key = ?", (key,)).fetchone())
            Editor._audit(db, now, op, SECTION, key, None, {k: r[k] for k in AUDITED if r.get(k) not in (None, "", 0)} | {
                "lat": lat, "lon": lon})
        self._bump()
        return {"ok": True, "hydrant": self.shown(self.index()["by_key"][key]), "key": key}

    # -- CSV ------------------------------------------------------------------------------------------------------------

    COLUMNS = ("key", "county_id", "county_number", "district", "town", "county_lat", "county_lon", "lat", "lon",
               "removed", *FIELDS, "class", "rated_gpm", "missing", "updated_by", "updated_at")
    READ_ONLY = ("county_id", "county_number", "district", "town", "county_lat", "county_lon", "class", "rated_gpm",
                 "missing", "updated_by", "updated_at")

    def csv(self, q: dict) -> tuple[bytes, int]:
        """GET /api/cad/hydrants.csv (admins): the list's rows (its filter), every overlay column, keyed by the key."""
        from sizeup.cad.export import _csv
        f = self.parse_query(q)
        f["all"] = True if f["flag"] in ("", "removed") and (q.get("all") or [""])[0] == "1" else f["all"]
        items = self._sorted(f)
        out = []
        for it in items:
            r = it["row"] or {}
            p = it["props"]
            vals = {"key": it["key"], "county_id": it["fid"] if it["fid"] is not None else "",
                    "county_number": str(p.get(NUMBER) or "").strip(), "district": str(p.get(DISTRICT) or "").strip(),
                    "town": self.town(it), "county_lat": _f6(it["county_lat"]), "county_lon": _f6(it["county_lon"]),
                    "lat": _f6(r.get("lat")), "lon": _f6(r.get("lon")), "removed": "yes" if it["removed"] else "",
                    "class": it["cls"] or "", "rated_gpm": it["gpm"] if it["gpm"] is not None else "",
                    "missing": "yes" if it["missing"] else "", "updated_by": who(r.get("updated_by") or ""),
                    "updated_at": _iso(r.get("updated_at"))}
            for k in FIELDS:
                v = r.get(k)
                vals[k] = "" if v is None or (k == "status" and v == "unknown") else v
            vals["number"] = r.get("number") or ""
            out.append([vals[c] for c in self.COLUMNS])
        return _csv(list(self.COLUMNS), out), len(out)

    def import_csv(self, body: dict) -> dict:
        """POST /api/cad/hydrants/import {action: preview | apply, csv}: rows keyed by the key (a blank key with lat and
        lon: a new hydrant). Only the columns the file has are changed (an empty cell clears that field); the read-only
        ones (county_*, town, class ...) and unknown ones are ignored. Any row in error: nothing is applied."""
        act = str(body.get("action") or "preview")
        if act not in ("preview", "apply"):
            raise CadError("action: preview or apply")
        text = body.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise CadError("csv: the file's text")
        try:
            return self._import(act, text, body)
        except csv.Error as e:
            raise CadError(f"csv: {e}") from None

    def _import(self, act: str, text: str, body: dict) -> dict:
        rd = csv.reader(io.StringIO(text.lstrip("\ufeff")))
        try:
            head = next(rd)
        except StopIteration:
            raise CadError("csv: empty") from None
        cols = [h.strip().lower() for h in head]
        if "key" not in cols:
            raise CadError("csv: needs a key column (export the list first)")
        if len(set(c for c in cols if c)) != len([c for c in cols if c]):
            raise CadError("csv: a column twice")
        writable = {c for c in cols if c in FIELDS or c in ("lat", "lon", "removed")}
        ignored = [h for h, c in zip(head, cols) if c and c not in writable and c != "key" and c not in self.READ_ONLY]
        idx = self.index()
        changes, errors, seen, new = [], [], set(), []
        new_at: list = []
        unchanged = rows = 0
        for line, row in enumerate(rd, start=2):
            if line - 1 > MAX_IMPORT_ROWS:
                raise CadError(f"csv: at most {MAX_IMPORT_ROWS} rows")
            if not any(c.strip() for c in row):
                continue
            rows += 1
            cells = {c: (row[i] if i < len(row) else "") for i, c in enumerate(cols) if c}
            key = cells.get("key", "").strip()
            try:
                f = {k: clean(k, cells[k]) for k in writable if k in FIELDS}
                f = self._stored(f)
                if "removed" in writable:
                    f["removed"] = 1 if cells["removed"].strip().lower() in ("yes", "y", "1", "true", "removed") else 0
                if "lat" in writable or "lon" in writable:
                    la, lo = cells.get("lat", "").strip(), cells.get("lon", "").strip()
                    if la or lo:
                        f["lat"], f["lon"] = point(la, lo)
                    elif key:
                        f["lat"] = f["lon"] = None
                if not key:
                    if f.get("lat") is None:
                        raise CadError("a new hydrant (no key) needs lat and lon")
                    self._check_point(None, f["lat"], f["lon"])
                    there = self.near(f["lat"], f["lon"], n=1, within=SAME_M)
                    if there or any(metres(a, b, f["lat"], f["lon"]) <= SAME_M for a, b in new_at):
                        raise CadError(f"a hydrant within {SAME_M:g} m already ({there[0]['key'] if there else 'this file'})")
                    new_at.append((f["lat"], f["lon"]))
                    new.append((line, f))
                    continue
                if key in seen:
                    raise CadError(f"{key} twice")
                seen.add(key)
                it = idx["by_key"].get(key) if KEY_RE.fullmatch(key) else None
                if it is None:
                    raise CadError(f"no hydrant {key}")
                if f.get("lat") is not None:
                    self._check_point(it, f["lat"], f["lon"])
                    if metres(it["lat"], it["lon"], f["lat"], f["lon"]) < SAME_POINT_M:
                        f.pop("lat")                   # where it is already (a file with every point): no move
                        f.pop("lon")
                    elif (not it["hub"] and it["county_lat"] is not None
                          and metres(it["county_lat"], it["county_lon"], f["lat"], f["lon"]) < SAME_POINT_M):
                        f["lat"] = f["lon"] = None     # the county's point: no nudge
                elif "lat" in f and (it["hub"] or it["missing"]):
                    f.pop("lat")
                    f.pop("lon", None)                 # a hub hydrant's (or a missing one's) point stays: never cleared
                r = it["row"] or DEFAULTS
                base = {k: r.get(k, DEFAULTS[k]) for k in f}
                diff = {k: (base[k], v) for k, v in f.items() if base[k] != v}
                if diff:
                    changes.append({"key": key, "number": it["number"], "line": line, "changes": diff, "_it": it,
                                    "_f": {k: v for k, (_, v) in diff.items()}})
                else:
                    unchanged += 1
            except CadError as e:
                errors.append({"line": line, "key": key, "error": str(e)})
        out = {"rows": rows,
               "changed": len(changes), "unchanged": unchanged, "new": len(new), "errors": errors[:200],
               "error_count": len(errors), "ignored": ignored, "columns": sorted(writable),
               "read_only": [h for h, c in zip(head, cols) if c in self.READ_ONLY]}
        out["preview"] = [{"key": c["key"], "number": c["number"], "line": c["line"],
                           "changes": {k: list(v) for k, v in c["changes"].items()}} for c in changes[:500]]
        out["preview"] += [{"key": "(new)", "number": f.get("number") or "", "line": ln,
                            "changes": {k: [None, v] for k, v in f.items() if v not in (None, "", 0)}} for ln, f in new[:100]]
        if act == "preview":
            return out
        if errors:
            raise CadError(f"{len(errors)} row(s) in error: nothing applied")
        op = str(body.get("operator") or "")[:320]
        now = self.cad.clock()
        if self.index() is not idx:
            raise CadError("the hydrants changed while importing: try again")
        with self.store.tx() as db:
            for c in changes:
                self._write(db, c["_it"], c["_f"], op, now)
            n0 = db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(key, 2) AS INTEGER)), 0) FROM hydrants WHERE key LIKE 'h%'").fetchone()[0]
            for i, (_, f) in enumerate(new, start=1):
                key = f"h{n0 + i}"
                db.execute("INSERT INTO hydrants (key, lat, lon, created_by, created_at, updated_by, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, f["lat"], f["lon"], op, now, op, now))
                rest = {k: v for k, v in f.items() if k not in ("lat", "lon")}
                if rest:
                    db.execute(f"UPDATE hydrants SET {', '.join(f'{k} = ?' for k in rest)} WHERE key = ?", (*rest.values(), key))
                Editor._audit(db, now, op, SECTION, key, None, {k: v for k, v in f.items() if v not in (None, "", 0)})
        if changes or new:
            self._bump()
        out["applied"] = True
        return out


def _num_key(n: str) -> tuple:
    m = re.match(r"(\d+)(.*)", n or "")
    return (0, int(m.group(1)), m.group(2)) if m else (1, 0, n or "")


def _f6(v) -> str:
    return f"{v:.6f}" if isinstance(v, (int, float)) else ""


def _iso(t) -> str:
    if not t:
        return ""
    from datetime import datetime

    from sizeup.cad.store import TZ
    return datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d %H:%M")
