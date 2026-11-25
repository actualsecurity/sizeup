"""A call's building on its maps: the county footprint(s) of the building the call is at, highlighted whole.

Admin, 2026-11-25: when a call comes in for an address, an address range or a common place name, highlight the
building's GIS object(s), the whole building: one large building carrying several street addresses lights up for any
of them.

What the data has (surveyed 2026-11-25): the county's footprints (sizeup/gis.py layer "buildings", the planimetrics,
FEA_CODE 2100) carry OBJECTID, DESCRIPTION, FEA_CODE, PHOTO_DATE, BASE_ELEVATION, APPROX_ROOF_ELEVATION and
ACTUAL_HEIGHT (feet: the numbers the maps draw on them from zoom 17), and no address. The addresses come from the
gazetteer's address points (NYS / CT 9-1-1: one point per street, number, letter and town, data/gazetteer.sqlite), and
which footprints share a lot from the tax parcels (layer "parcels").

    address    the call's building key (sizeup/cad/addrkey.py: number, street, town): every footprint holding an address
               point of that number on that street (a range "40-44": each of its numbers, house_numbers); a footprint is
               lit whole, so a building with several numbers lights up for any of them
    nearest    an address point in no footprint (a yard, a lot): the nearest footprint within NEAR_M of it that faces
               the same street on our side (the nearest other address point to it is on our street with our parity:
               none near, no), holding no other address's point, with no other address's point nearer to it than ours,
               mostly on the lot the point is on (parcels loaded), and no other such footprint within AMBIG_M of as near;
               each point of a range on its own. No address point for the number (an interpolated pin): the footprint
               the pin is in if it holds no other address, else (the pin in no footprint) the nearest the same way; a
               letter with no point of its own (its pin is the plain number's): nothing
    campus     the tax parcel our address point is on, when the parcel is that address (its PARCEL_ADDR): its other large
               footprints (CAMPUS_MIN_M2) mostly on it and holding no other address's point (a school, a plant: several
               buildings, one address)
    place      a named place with no street address: the place name's polygon (Settings' place names: "geometry") when
               it has one; else the footprints holding its points (its site's other addresses') or the pin, and their
               campus
    none       an intersection, a highway, an exit or a mile marker, a pin with no address or place, a fraction
               ("12 1/2"), a rear building ("12 Example St Rear": not in the data): nothing. Never a building that may be
               the wrong one. A complex's "Bldg 3": the complex, lit whole (which building is 3 is not in the data).

Each open call carries its building on the state push ("building": {how, footprints: [{ref, geometry, addrs}]}: at most
MAX_FOOTPRINTS, the geometry as the county layer has it, so the maps draw it at every zoom, the buildings layer loaded
or not; addrs: the address points it holds, "40-44 Example St"); GET /api/cad/incident/<id> a closed one's too.
Resolved once per location (number, street, town, place, pin) and data version (the layers' revs, the place names'):
an LRU of CACHE_N. The footprints, the parcels and the address points are put on a grid once per layer rev (or
gazetteer), so a call reads a few cells, never the whole layer: at the start in the background (warm()), and a refreshed
layer in the background while the previous grid serves.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from sizeup.cad import addrkey
from sizeup.geo.normalize import street_key

try:                                   # the house-number range reader (sizeup/geo/houserange.py), when it has landed
    from sizeup.geo import houserange as _houserange
except ImportError:
    _houserange = None

LAYER, PARCELS = "buildings", "parcels"     # sizeup/gis.py DEFAULT_LAYERS
CELL = 0.0005                # the grid's cell, degrees (about 55 m x 42 m here)
NEAR_M = 15.0                # an address point (or pin) in no footprint: the nearest footprint this close, ...
AMBIG_M = 3.0                # ... and no other good one within this of as near (else none: never a guess)
FACE_M = 40.0                # a footprint faces the street of the (other) address point nearest it, within this
CAMPUS_MIN_M2 = 200.0        # a parcel's other footprints this large are the same address's (a garage or shed is not)
PIN_NEAR_M = 300.0           # no town to tell one street's stretch from another's: the address points this near the pin
PLACE_NEAR_M = 1000.0        # a place name's entries this near the call's pin are its place
MAX_FOOTPRINTS = 16          # a call's at most (a campus with more is not added: too big to be one building)
SPAN_MAX = 50                # a range's ends at most this far apart (houserange.SPAN_MAX, until it lands)
CACHE_N = 256
ADDRS_MAX = 120              # the hover line's characters at most
PACE_N = 1024                # an index built in the background: a short rest every this many features
MAX_VERTICES = 4000          # a call's footprints' vertices at most on the push (more: the rest left out)
SITE_SPAN_M = 200.0          # a place name with no pin: its entries within this of one another are one place
# A place, or a pin with no address, on a highway: never a building (the call is on the road)
HIGHWAY = re.compile(r"\b(?:I-?\s?\d{2,3}\b|interstate|thruway|hutchinson|merritt|cross\s+county|expressway|expy\b|"
                     r"exit\s*\d+[a-z]?\b|mile\s*marker|milepost|mm\s*\d)", re.I)
M_LAT = 110_574.0


def _m_lon(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


# ---- the house numbers ----------------------------------------------------------------------------------------------

def house_numbers(number: str) -> list[tuple[int, str]]:
    """The house numbers a building key's number stands for, as (number, letter): "40" -> [(40, "")]; "40A" -> [(40, "a")];
    a range "40-44" -> [(40, ""), (42, ""), (44, "")] (sizeup/geo/houserange.py's reading when it is here, else its ends
    on one side of the street, at most SPAN_MAX apart); anything else with a dash: its first number. [] for a fraction
    ("12 1/2": a building of its own the address points don't have) or no number."""
    s = " ".join(str(number or "").lower().split())
    if not s or "1/2" in s or "½" in s:
        return []
    rng = _range(s)
    if rng:
        return rng
    m = re.match(r"(\d{1,6})([a-z])?(?![a-z0-9])", s)
    return [(int(m.group(1)), m.group(2) or "")] if m else []


def _range(s: str) -> list[tuple[int, str]]:
    """A range's numbers as (number, letter): houserange's labels ("14-16a": 14, 16, 16a), else the fallback's."""
    if _houserange is not None:
        r = _houserange.parse_number(s)
        if r is None:
            return []
        out = []
        for lab in sorted(r.labels(), key=lambda x: (int(re.match(r"\d+", x).group(0)), x)):
            m = re.fullmatch(r"(\d+)([a-z]?)", lab)
            if m:
                out.append((int(m.group(1)), m.group(2)))
        return out
    m = re.fullmatch(r"(\d{1,6})\s*-\s*(\d{1,6})([a-z]?)", s)
    if not m:
        return []
    lo, hi = int(m.group(1)), int(m.group(2))
    if not (0 < lo < hi <= lo + SPAN_MAX and (hi - lo) % 2 == 0):
        return []
    return [(n, "") for n in range(lo, hi + 1, 2)] + ([(hi, m.group(3))] if m.group(3) else [])


def compress(nums: list[str]) -> str:
    """House numbers as a short line: ["40", "42", "44"] -> "40-44"; ["40", "41"] -> "40, 41"; letters as they are."""
    plain = sorted({int(n) for n in nums if n.isdigit()})
    other = sorted({n for n in nums if not n.isdigit()}, key=lambda n: (int(re.match(r"\d+", n).group(0)), n))
    runs, out = [], []
    for n in plain:
        for r in runs:
            if r[-1] + 2 == n:
                r.append(n)
                break
        else:
            runs.append([n])
    for r in sorted(runs):
        out.append(f"{r[0]}-{r[-1]}" if len(r) > 1 else str(r[0]))
    return ", ".join(out + other)


# ---- geometry -------------------------------------------------------------------------------------------------------

def _rings(g: dict) -> list[list]:
    c = (g or {}).get("coordinates") or []
    if g.get("type") == "Polygon":
        return [list(c)]
    if g.get("type") == "MultiPolygon":
        return [list(p) for p in c]
    return []


def area_m2(g: dict) -> float:
    """A polygon's area in square metres (on a flat plane at its latitude; holes taken out)."""
    total = 0.0
    for poly in _rings(g):
        for k, ring in enumerate(poly):
            if len(ring) < 3:
                continue
            lat0 = ring[0][1]
            kx, ky = _m_lon(lat0), M_LAT
            a = 0.0
            for i in range(len(ring) - 1):
                x1, y1 = ring[i][0] * kx, ring[i][1] * ky
                x2, y2 = ring[i + 1][0] * kx, ring[i + 1][1] * ky
                a += x1 * y2 - x2 * y1
            total += abs(a) / 2 * (1 if k == 0 else -1)
    return max(0.0, total)


def box_around(lat: float, lon: float, m: float) -> tuple[float, float, float, float]:
    dlat, dlon = m / M_LAT, m / _m_lon(lat)
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def grow(box, m: float) -> tuple[float, float, float, float]:
    w, s, e, n = box
    dlat, dlon = m / M_LAT, m / _m_lon((s + n) / 2)
    return (w - dlon, s - dlat, e + dlon, n + dlat)


class Grid:
    """Items with a box (west, south, east, north) on a CELL-degree grid: what touches a box is a few cells' read."""

    def __init__(self, cell: float = CELL):
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = {}
        self.items: list[tuple[tuple, object]] = []

    def _span(self, box):
        c = self.cell
        return range(math.floor(box[1] / c), math.floor(box[3] / c) + 1), range(math.floor(box[0] / c), math.floor(box[2] / c) + 1)

    def add(self, box, item) -> None:
        i = len(self.items)
        self.items.append((box, item))
        ys, xs = self._span(box)
        for y in ys:
            for x in xs:
                self.cells.setdefault((y, x), []).append(i)

    def query(self, box) -> list:
        ys, xs = self._span(box)
        seen, out = set(), []
        for y in ys:
            for x in xs:
                for i in self.cells.get((y, x), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    b, item = self.items[i]
                    if b[0] <= box[2] and b[2] >= box[0] and b[1] <= box[3] and b[3] >= box[1]:
                        out.append(item)
        return out

    def __len__(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class Fp:
    """A county footprint (or a parcel), as indexed."""
    ref: str                 # "buildings/<OBJECTID>", or "buildings/@lat,lon" of its first vertex (no id: sizeup/cad/preplan.py)
    geometry: dict
    box: tuple
    area: float
    addr: str = ""           # a parcel's address as the county gives it (PARCEL_ADDR: "100 SAMPLE AVE"); "" for a footprint


@dataclass(frozen=True)
class Pt:
    """An address point: where, its street (the gazetteer's id), number, letter (lower case) and town."""
    lat: float
    lon: float
    sid: int
    num: int
    suf: str
    muni: str

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.sid, self.num, self.suf)


@dataclass(frozen=True)
class Loc:
    """What a call's building is resolved from (Footprints.loc_of)."""
    number: str = ""         # the building key's: "40", "40a", "40-44", "12 1/2"
    street: str = ""         # a street_key: "example st"
    town: str = ""           # the CAD's town, lower case ("" unknown)
    place: str = ""          # a named place with no street address
    pin: tuple[float, float] | None = None
    label: str = ""          # the call's address as shown (a footprint with no address point of its own says it)
    rear: bool = False       # "12 Example St Rear": a building behind the one the address point is in (unknown: none)


def _ref(layer: str, f: dict, g: dict) -> str:
    if f.get("id") is not None:
        return f"{layer}/{f['id']}"
    rings = _rings(g) or [[[[0, 0]]]]
    r = rings[0][0][0]
    n = sum(len(ring) for poly in rings for ring in poly)      # (two attached buildings may start at one shared corner)
    return f"{layer}/@{r[1]:.6f},{r[0]:.6f}/{n}"


def _lot_is(addr: str, numbers: set, street: str) -> bool:
    """A parcel's address ("100 SAMPLE AVE", "40-44 EXAMPLE ST") is one of these numbers on this street (a street_key)."""
    p = addrkey.parse(addr, {}) if addr else None
    return p is not None and street_key(p.street) == street and bool({n for n, _ in house_numbers(p.lot)} & numbers)


def _rest() -> None:
    time.sleep(0.005)


def index_layer(layer: str, feats: list[dict], pace=None) -> Grid:
    """A polygon layer's features on the grid (Fp each). pace(): called every PACE_N features (warm(): a background
    build gives way to the requests, as the GIS cache read does: sizeup/gis.py _pace)."""
    from sizeup.gis import geometry_bbox
    grid = Grid()
    for i, f in enumerate(feats):
        if pace is not None and i % PACE_N == PACE_N - 1:
            pace()
        g = f.get("geometry") or {}
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        b = geometry_bbox(g)
        if b is None:
            continue
        addr = str((f.get("properties") or {}).get("PARCEL_ADDR") or "").strip()
        grid.add(b, Fp(_ref(layer, f, g), g, b, area_m2(g), addr))
    return grid


# ---- the service ----------------------------------------------------------------------------------------------------

class Footprints:
    """One per Cad (sizeup/cad/service.py): each call's building. gaz: the gazetteer (the app sets it; None: the
    footprint under the pin only)."""

    def __init__(self, cad):
        self.cad = cad
        self.gaz = None
        self._lock = threading.Lock()
        self._grids: dict[str, tuple[object, Grid]] = {}          # layer -> (its rev, the grid)
        self._tl = threading.local()
        self._building: dict[str, threading.Lock] = {LAYER: threading.Lock(), PARCELS: threading.Lock()}   # one build each
        self._addr: tuple[object, Grid | None, dict, dict] = (None, None, {}, {})   # (gazetteer, grid, by street, by number)
        self._cache: OrderedDict = OrderedDict()

    # -- the data, indexed once per version ---------------------------------------------------------------------------

    def _gis(self):
        g = getattr(self.cad, "gis", None)
        return g if g is not None and LAYER in getattr(g, "layers", {}) else None

    def _rev(self, layer: str):
        g = getattr(self.cad, "gis", None)
        if g is None or layer not in getattr(g, "layers", {}):
            return None
        return g.status[layer]["rev"]

    def warm(self) -> None:
        """Index the layers and the address points now (the app's layer loader, in the background, once the GIS cache is
        read), so the first push after a start doesn't. Never raises."""
        try:
            for layer in (LAYER, PARCELS):
                self._build(layer, pace=_rest)
            self._addresses()
        except Exception as e:  # noqa: BLE001 - the push indexes them itself
            self.cad.log(f"CAD footprints: not indexed ahead ({type(e).__name__})")

    def _build(self, layer: str, pace=None) -> None:
        """Index the layer as it is now, unless that is already done (one build at a time per layer)."""
        with self._building[layer]:
            rev = self._rev(layer)
            got = self._grids.get(layer)
            if rev is None or (got is not None and got[0] == rev):
                return
            grid = index_layer(layer, self.cad.gis.features(layer), pace)
            with self._lock:
                self._grids[layer] = (rev, grid)

    def _grid(self, layer: str) -> tuple[object, Grid | None]:
        """(the rev of the grid used, the grid). The layer's first: built here. A newer rev of a layer already indexed
        (the weekly / monthly refresh): built in the background while the old one serves (its answers are cached under
        the old rev, so they are worked out again once the new one is in)."""
        rev = self._rev(layer)
        if rev is None:
            return None, None
        got = self._grids.get(layer)
        if got is not None and got[0] == rev:
            return got
        if got is None:
            self._build(layer)
            return self._grids.get(layer) or (None, None)
        if not self._building[layer].locked():
            threading.Thread(target=self._build, args=(layer, _rest), name="footprints-index", daemon=True).start()
        return got

    def _addresses(self) -> tuple[Grid | None, dict, dict]:
        """The gazetteer's address points: on the grid, by street key (-> street ids) and by (street id, number)."""
        gaz = self.gaz
        if gaz is None:
            return None, {}, {}
        with self._lock:
            if self._addr[0] is gaz:
                return self._addr[1], self._addr[2], self._addr[3]
        grid, by_street, by_num = Grid(), {}, {}
        for sid, st in getattr(gaz, "streets", {}).items():
            for k in {getattr(st, "key", ""), street_key(getattr(st, "name", ""))} - {""}:
                by_street.setdefault(k, set()).add(sid)
            for muni, rows in (getattr(st, "addrs", None) or {}).items():
                for num, suf, lat, lon in rows:
                    p = Pt(float(lat), float(lon), sid, int(num), str(suf or "").lower(), muni)
                    grid.add((p.lon, p.lat, p.lon, p.lat), p)
                    by_num.setdefault((sid, p.num), []).append(p)
        with self._lock:
            self._addr = (gaz, grid, by_street, by_num)
        return grid, by_street, by_num

    def _munis(self, sid: int) -> list[str]:
        st = getattr(self.gaz, "streets", {}).get(sid) if self.gaz is not None else None
        return list((getattr(st, "addrs", None) or {}).keys()) if st is not None else []

    def _street_name(self, sid: int) -> str:
        st = getattr(self.gaz, "streets", {}).get(sid) if self.gaz is not None else None
        return getattr(st, "name", "") if st is not None else ""

    def _town(self, muni: str) -> str:
        try:
            return (self.cad.lists.town_of(muni) or muni).lower()
        except Exception:  # noqa: BLE001 - no lists: the gazetteer's own name
            return (muni or "").lower()

    # -- a call's location --------------------------------------------------------------------------------------------

    def loc_of(self, inc: dict) -> Loc:
        """The call's building key (number, street, town), else its address parsed (a key the gazetteer didn't confirm,
        or none yet), its place name and its pin."""
        lat, lon = inc.get("lat"), inc.get("lon")
        pin = (float(lat), float(lon)) if lat is not None and lon is not None else None
        number = street = town = ""
        rear = False
        bk = inc.get("bldg_key") or ""
        if bk and not bk.startswith("@"):
            number, street, town, _b = addrkey.bldg_parts(bk)       # (a complex's "bldg 3": the complex, lit whole)
            rear = "rear" in re.split(r"[|\s]+", str(inc.get("addr_key") or "")[len(bk):])
        if not number:
            try:
                aliases = self.cad.lists.geo_town_aliases
            except Exception:  # noqa: BLE001
                aliases = {}
            for t in dict.fromkeys(x for x in (inc.get("place_address"), inc.get("address")) if x):
                p = addrkey.parse(t, aliases)
                if p is not None:
                    number, street, town = p.lot, street_key(p.street), (p.town or "").lower()
                    rear = "rear" in p.unit.split()
                    break
        place = "" if number else str(inc.get("place") or "").strip()
        if not place and not number and str(inc.get("address") or "").lstrip().startswith("@"):
            place = re.sub(r"\s*\(.*$", "", str(inc["address"]).lstrip()[1:]).strip()      # "@SAMPLE SCHOOL": a place only
        label = str(inc.get("addr_label") or inc.get("address") or "").strip()
        if HIGHWAY.search(" ".join(str(inc.get(k) or "") for k in ("address", "place", "geo_label"))) and not number:
            return Loc(pin=None, label=label)                     # on the road: never a building
        if rear:
            return Loc(label=label)                                # the rear building: not in the data, never the front one
        return Loc(number=number, street=street, town=town, place=place, pin=pin, label=label)

    def for_incident(self, inc: dict) -> dict | None:
        """The call's building ({how, footprints: [{ref, geometry, addrs}]}), else None. Cached per location and the data
        it was worked out from (the grids' revs, the gazetteer, the place names)."""
        if self._gis() is None:
            return None
        loc = self.loc_of(inc)
        if not (loc.number or loc.place):
            return None
        frev, fps = self._grid(LAYER)
        prev, parcels = self._grid(PARCELS)
        try:
            places = self.cad.settings.places_rev()
        except Exception:  # noqa: BLE001
            places = ""
        key = (loc, frev, prev, id(self.gaz) if self.gaz is not None else None, places)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        got = self.resolve(loc, fps, parcels)
        with self._lock:
            self._cache[key] = got
            while len(self._cache) > CACHE_N:
                self._cache.popitem(last=False)
        return got

    def decorate(self, incs: list[dict]) -> None:
        """The state push (its calls, open and closed: a call just closed keeps its building on its map) and an incident's
        full read: each call's building ("building")."""
        if self._gis() is None:
            return
        for inc in incs:
            try:
                b = self.for_incident(inc)
                if b is not None:
                    inc["building"] = b
            except Exception as e:  # noqa: BLE001 - never the push (and never the address in the log)
                self.cad.log(f"CAD footprints: {type(e).__name__}")

    # -- resolving ------------------------------------------------------------------------------------------------------

    def resolve(self, loc: Loc, fps: Grid | None = None, parcels: Grid | None = None) -> dict | None:
        if fps is None:
            fps = self._grid(LAYER)[1]
            parcels = self._grid(PARCELS)[1]
        if fps is None or not len(fps):
            return None
        self._tl.parcels = parcels                 # (this thread's resolve: _nearest, _campus)
        if loc.number:
            chosen, how = self._address(loc, fps)
        elif loc.place:
            got = self._place_polygon(loc)
            if got is not None:
                key, poly = got
                return {"how": "place", "footprints": [{"ref": f"place/{key}", "geometry": poly, "addrs": loc.place[:ADDRS_MAX]}]}
            chosen, how = self._place(loc, fps), "place"
        else:
            return None
        if not chosen:
            return None
        out, n = [], 0
        for f in list(chosen.values())[:MAX_FOOTPRINTS]:
            n += sum(len(r) for poly in _rings(f.geometry) for r in poly)
            if out and n > MAX_VERTICES:
                break
            out.append({"ref": f.ref, "geometry": f.geometry, "addrs": self._addrs_of(f) or loc.label[:ADDRS_MAX]})
        return {"how": how, "footprints": out}

    def _points(self, loc: Loc, nums: list[tuple[int, str]]) -> list[Pt]:
        """Our address points: each number's on the street (a letter: that letter's; none: the plain number's, else all
        its letters'), in the call's town (else near the pin; with neither, only when the street has it in one town)."""
        _grid, by_street, by_num = self._addresses()
        sids = by_street.get(loc.street) or set()
        out: list[Pt] = []
        for num, suf in nums:
            rows = [p for sid in sids for p in by_num.get((sid, num), ())]
            if suf:
                rows = [p for p in rows if p.suf == suf]
            elif any(not p.suf for p in rows):
                rows = [p for p in rows if not p.suf]
            out.extend(rows)
        if not out:
            return []
        if loc.town:
            same = [p for p in out if self._town(p.muni) == loc.town]
            if same:
                return same
            if any(self._town(m) == loc.town for sid in sids for m in self._munis(sid)):
                return []                       # the street is in the call's town, the number isn't: never the next town's
        if loc.pin is not None:
            return [p for p in out if addrkey.meters(loc.pin[0], loc.pin[1], p.lat, p.lon) <= PIN_NEAR_M]
        return out if len({p.muni for p in out}) == 1 else []

    def _address(self, loc: Loc, fps: Grid) -> tuple[dict, str]:
        nums = house_numbers(loc.number)
        if not nums:
            return {}, ""
        pts = self._points(loc, nums)
        _grid, by_street, _n = self._addresses()
        sids = by_street.get(loc.street) or set()
        parity = nums[0][0] % 2
        own = {p.key for p in pts}
        chosen: dict[str, Fp] = {}
        how = "range" if len(nums) > 1 else "address"
        if pts:
            near = False
            for p in pts:                             # each point: the footprint it is in, else the nearest good one
                got = self._containing(fps, p.lat, p.lon)
                if not got:
                    f = self._nearest(fps, p.lat, p.lon, sids, parity, own)
                    got, near = ([f], True) if f is not None else ([], near)
                for f in got:
                    chosen.setdefault(f.ref, f)
            if near and how == "address":
                how = "nearest"
            self._campus(fps, [(p.lat, p.lon) for p in pts], own, chosen, ({n for n, _ in nums}, loc.street))
        elif loc.pin is not None and not any(suf for _n, suf in nums):
            # no address point for it (a letter with none of its own: its pin is the plain number's: nothing): the
            # footprint the pin is in when it holds no other address (never its parcel's campus), else the nearest
            got = [f for f in self._containing(fps, *loc.pin) if not self._others_inside(f, own)]
            if got:
                chosen = {f.ref: f for f in got}
                how = "point"
            elif not self._containing(fps, *loc.pin):
                f = self._nearest(fps, loc.pin[0], loc.pin[1], sids, parity, own)
                if f is not None:
                    chosen[f.ref] = f
                    how = "nearest"
        return chosen, how

    def _place(self, loc: Loc, fps: Grid) -> dict:
        pts = self._place_points(loc)
        if loc.pin is not None:
            pts.append(loc.pin)
        chosen: dict[str, Fp] = {}
        for lat, lon in pts:
            for f in self._containing(fps, lat, lon):
                chosen.setdefault(f.ref, f)
        if chosen:
            self._campus(fps, pts, set(), chosen)
        return chosen

    def _entries(self, loc: Loc) -> list[dict]:
        """The department's place names for the call's place (its name or an alias), near its pin, with their site's."""
        from sizeup.cad.commonplaces import name_key
        try:
            rows = self.cad.local_places() or []
        except Exception:  # noqa: BLE001
            return []
        want = name_key(loc.place)
        if not want:
            return []
        hit = [e for e in rows if want in {name_key(e.get("name") or ""), *(name_key(a) for a in e.get("aliases") or ())}]
        if loc.pin is not None:
            hit = [e for e in hit if e.get("lat") is None or addrkey.meters(loc.pin[0], loc.pin[1], e["lat"], e["lon"]) <= PLACE_NEAR_M]
        else:                                   # no pin: one place only (one site, or all close together), else none
            sites = {e.get("site") or e.get("key") for e in hit}
            pts = [(e["lat"], e["lon"]) for e in hit if e.get("lat") is not None]
            if len(sites) > 1 and any(addrkey.meters(a[0], a[1], b[0], b[1]) > SITE_SPAN_M for a in pts for b in pts):
                return []
        sites = {e.get("site") or e.get("key") for e in hit} - {None, ""}
        return hit + [e for e in rows if e not in hit and (e.get("site") in sites or e.get("key") in sites)]

    def _place_polygon(self, loc: Loc) -> tuple[str, dict] | None:
        for e in self._entries(loc):
            g = e.get("geometry")
            if isinstance(g, dict) and g.get("type") in ("Polygon", "MultiPolygon") and _rings(g):
                return str(e.get("key") or e.get("name") or ""), g
        return None

    def _place_points(self, loc: Loc) -> list[tuple[float, float]]:
        return [(float(e["lat"]), float(e["lon"])) for e in self._entries(loc) if e.get("lat") is not None and e.get("lon") is not None]

    def _containing(self, fps: Grid, lat: float, lon: float) -> list[Fp]:
        from sizeup.gis import contains
        return [f for f in fps.query((lon, lat, lon, lat)) if contains(f.geometry, lat, lon)]

    def _inside(self, f: Fp) -> list[Pt]:
        """The address points in a footprint."""
        from sizeup.gis import contains
        grid, _s, _n = self._addresses()
        if grid is None:
            return []
        return [p for p in grid.query(f.box) if contains(f.geometry, p.lat, p.lon)]

    def _others_inside(self, f: Fp, own: set) -> bool:
        return any(p.key not in own for p in self._inside(f))

    def _faces(self, f: Fp, sids: set, parity: int, own: set) -> bool:
        """The footprint faces our street, on our side: the address point nearest it (within FACE_M) other than ours is on
        our street with our parity. None near it: unknown, so no."""
        from sizeup.cad.preplan import edge_m
        from sizeup.gis import contains
        grid, _s, _n = self._addresses()
        if grid is None:
            return False
        best = None
        for p in grid.query(grow(f.box, FACE_M)):
            if p.key in own:
                continue
            d = 0.0 if contains(f.geometry, p.lat, p.lon) else edge_m(f.geometry, p.lat, p.lon)
            if d <= FACE_M and (best is None or (d, p.key) < (best[0], best[1].key)):
                best = (d, p)
        return best is not None and best[1].sid in sids and best[1].num % 2 == parity

    def _nearest(self, fps: Grid, lat: float, lon: float, sids: set, parity: int, own: set) -> Fp | None:
        """The nearest footprint within NEAR_M that faces our street, holds no other address, has no other address point
        nearer to it than ours, and (parcels loaded) is mostly on the lot the point is on; when no other such one is
        within AMBIG_M of as near."""
        from sizeup.cad.preplan import edge_m
        near = sorted(((edge_m(f.geometry, lat, lon), f.ref, f) for f in fps.query(box_around(lat, lon, NEAR_M))),
                      key=lambda t: (t[0], t[1]))
        lot = self._lot_at(lat, lon)
        good = [(d, f) for d, _r, f in near if d <= NEAR_M and not self._others_inside(f, own)
                and not self._other_nearer(f, d, own) and self._faces(f, sids, parity, own)
                and (lot is None or self._mostly_on(f, lot))]
        if not good or (len(good) > 1 and good[1][0] - good[0][0] < AMBIG_M):
            return None
        return good[0][1]

    def _other_nearer(self, f: Fp, d: float, own: set) -> bool:
        """Another address's point is nearer the footprint than ours (d): it is that address's building."""
        from sizeup.cad.preplan import edge_m
        grid, _s, _n = self._addresses()
        if grid is None:
            return False
        return any(p.key not in own and edge_m(f.geometry, p.lat, p.lon) < d for p in grid.query(grow(f.box, d)))

    def _lot_at(self, lat: float, lon: float) -> Fp | None:
        from sizeup.gis import contains
        parcels = getattr(self._tl, "parcels", None)
        if parcels is None:
            return None
        return next((lot for lot in parcels.query((lon, lat, lon, lat)) if contains(lot.geometry, lat, lon)), None)

    @staticmethod
    def _mostly_on(f: Fp, lot: Fp) -> bool:
        from sizeup.cad.preplan import samples
        from sizeup.gis import contains
        ss = samples(f.geometry)
        return bool(ss) and sum(1 for y, x in ss if contains(lot.geometry, y, x)) * 2 >= len(ss)

    def _campus(self, fps: Grid, pts: list[tuple[float, float]], own: set, chosen: dict, ours: tuple | None = None) -> None:
        """The other large footprints on the parcels our points are on (each parcel's added only while the call stays
        within MAX_FOOTPRINTS). ours: an address's (numbers, street key): only a lot that is that address (its PARCEL_ADDR
        one of the numbers on that street: 104 on 100's lot is a building of its own, never 100's campus)."""
        from sizeup.gis import contains
        parcels = getattr(self._tl, "parcels", None)
        if parcels is None or not len(parcels):
            return
        lots: dict[str, Fp] = {}
        for lat, lon in pts:
            for lot in parcels.query((lon, lat, lon, lat)):
                if contains(lot.geometry, lat, lon) and (ours is None or _lot_is(lot.addr, *ours)):
                    lots.setdefault(lot.ref, lot)
        for lot in lots.values():
            add = []
            for f in sorted(fps.query(lot.box), key=lambda f: f.ref):
                if f.ref in chosen or f.area < CAMPUS_MIN_M2:
                    continue
                if not self._mostly_on(f, lot):
                    continue
                if self._others_inside(f, own):
                    continue
                add.append(f)
            if add and len(chosen) + len(add) <= MAX_FOOTPRINTS:
                for f in add:
                    chosen[f.ref] = f

    def _addrs_of(self, f: Fp) -> str:
        """The address points a footprint holds, by street: "40-44 Example St"."""
        by: dict[int, list[str]] = {}
        for p in self._inside(f):
            by.setdefault(p.sid, []).append(f"{p.num}{p.suf.upper()}")
        parts = [f"{compress(nums)} {self._street_name(sid)}".strip() for sid, nums in sorted(by.items(), key=lambda kv: self._street_name(kv[0]))]
        s = "; ".join(parts)
        return s if len(s) <= ADDRS_MAX else s[:ADDRS_MAX - 1] + "…"
