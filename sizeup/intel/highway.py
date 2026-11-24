"""Where a crash on a highway is, as a radio transcript garbles it, and whether that is ours (a category with "highway":
true, data/intel/highway.json; the engine's step 6 for it).

    ref = parse(text)                    Ref(route, dir, kind, value, other, ramp) | None
    corridor = Corridor(markers, area_m, buffer_m)
    corridor.place(ref) -> model.Place | None
    corridor.gate(place, source_towns) -> "" | "out_of_area" | "unplaced"
    corridor.same(a, b, merge_m) -> bool     two places of one crash (one event, one push)
    corridor.at_incident(place, inc, link_m) -> bool     our own call at it (the county's dispatch: linked, no re-push)

What is read (parse): the words as transcribed, number words as digits first ("two eighty seven" and "two eight seven"
are 287, "ninety five" is 95, "exit nine a" is exit 9A, "mile marker ten point four" is 10.4), then

    route   a highway's name (sizeup/geo/places.py ROUTE_ALIASES, the place search's own list, plus the spoken forms in
            SPOKEN: "the Hutch", "Hutchinson", "Cross County", "Thruway"). A bare number ("287", "95") counts only
            beside a highway word: a direction, "exit", a mile marker, a ramp, "the 287", "I-95", "route 287"
    dir     northbound / southbound / eastbound / westbound, "NB", or "north" ... right after the route ("287 west")
    exit    "exit 9", "exit 9A", "exit nine a"
    marker  "mile marker 10.4", "mile post 10", "MM 10", "marker 10"
    ramp    "ramp", "interchange", "split", "connector", and a second route said: the interchange of the two

Where (place): the place search's markers (sizeup/geo/places.py PlaceIndex.markers: NYSDOT milepoints, OSM exits; data
from the gazetteer, never a name in code). An exit or a marker: its point (precision "landmark", never confirmed);
two routes and a ramp word: the meeting point of the two routes' markers (the closest pair, within INTERCHANGE_M); the
route alone: precision "street", no point, labelled "<route> <dir>".

Ours (gate): a point within buffer_m of our area (area_m: metres to our fire district's polygon, the camera wall's own
shapes, sizeup/roads.py area_shapes; 0 inside). The route alone: only from a source whose own towns (its department's
town: "Sample PD" is Sample's) have that route within buffer_m of our area (a neighbour's officer on its own stretch,
next to ours); anyone else's route alone is "unplaced" (held at log). A route we have no markers of: "out_of_area".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sizeup.intel.model import Place

INTERCHANGE_M = 1500.0          # two routes' markers this close: they meet here
EXIT_RADIUS_M = 400.0
SPOKEN = {                      # spoken forms beyond the place search's typed ones (normalised tokens)
    "Hutch": ["the hutch", "hutchinson river parkway", "hutchinson parkway", "hutchison", "hutchinson"],
    "I-95": ["the thruway", "new england thruway", "the 95", "i95"],
    "I-287": ["the 287", "i287", "cross westchester expressway"],
    "I-684": ["the 684", "i684"],
    "Cross County": ["cross county", "cross county parkway", "cross county pkwy"],
    "Saw Mill": ["saw mill", "saw mill parkway", "sawmill", "sawmill parkway"],
    "Sprain": ["sprain", "sprain brook", "sprain brook parkway", "sprain parkway"],
}
BARE = {"95", "287", "684"}     # a number that is a route only beside a highway word
DIRS = {"northbound": "NB", "southbound": "SB", "eastbound": "EB", "westbound": "WB", "nb": "NB", "sb": "SB",
        "eb": "EB", "wb": "WB"}
SHORT_DIRS = {"north": "NB", "south": "SB", "east": "EB", "west": "WB"}
RAMP = {"ramp", "ramps", "interchange", "split", "connector", "merge", "onramp", "offramp"}
HWY_WORDS = {"exit", "mile", "marker", "milepost", "mm", "ramp", "interchange", "northbound", "southbound", "eastbound",
             "westbound", "nb", "sb", "eb", "wb", "north", "south", "east", "west", "bound", "shoulder",
             "median", "guardrail", "split", "connector", "onramp", "offramp"}
# a street's words: "998 North Main Street", "12 Hutchinson Avenue" are addresses, never the highway
STREET_WORDS = {"street", "st", "avenue", "ave", "av", "road", "rd", "lane", "ln", "place", "pl", "drive", "dr", "court",
                "ct", "boulevard", "blvd", "terrace", "ter", "way", "circle", "cir", "square", "sq", "plaza", "alley"}
ONES = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
TOKEN = re.compile(r"\d+(?:\.\d+)?[a-z]?|[a-z]+")


@dataclass
class Ref:
    route: str
    dir: str = ""                  # "NB" ...
    kind: str = ""                 # "exit" | "mm" | ""
    value: str = ""                # "9A", "10.4"
    other: str = ""                # a second route said (an interchange, with ramp)
    ramp: bool = False


def tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("’", "'").replace("-", " ")
    return TOKEN.findall(t)


def _number(ws: list[str], i: int) -> tuple[str, int]:
    """Number words from ws[i] as digits: ("287", words used) or ("", 0). "two eighty seven", "two eight seven",
    "ninety five", "twenty one", "nine", "ten point four" -> "10.4"."""
    def unit(j):
        w = ws[j] if j < len(ws) else ""
        if w in ONES:
            return ONES[w], 1
        if w in TEENS:
            return TEENS[w], 1
        if w in TENS:
            v = TENS[w]
            if j + 1 < len(ws) and ws[j + 1] in ONES and ONES[ws[j + 1]] > 0 and ws[j + 1] not in ("oh", "o"):
                return v + ONES[ws[j + 1]], 2
            return v, 1
        return None, 0
    if i >= len(ws):
        return "", 0
    if ws[i].isdigit() and i + 1 < len(ws) and ws[i] in ("2", "6") and (ws[i + 1] in TENS or ws[i + 1] in ONES):
        # "2 eighty seven" (a number word fixed to a digit before us)
        rest, n = _number(ws, i + 1)
        if rest and len(rest) == 2:
            return ws[i] + rest, n + 1
    first, n = unit(i)
    if first is None:
        return "", 0
    if first < 10 and n == 1 and i + 1 < len(ws):
        nxt, m = unit(i + 1)
        if nxt is not None and 10 <= nxt <= 99:              # "two eighty seven", "six eighty four"
            return f"{first}{nxt}", 1 + m
        if nxt is not None and nxt < 10 and i + 2 < len(ws):  # "two eight seven"
            third, k = unit(i + 2)
            if third is not None and third < 10 and k == 1:
                return f"{first}{nxt}{third}", 3
    s, used = str(first), n
    if i + used + 1 < len(ws) and ws[i + used] == "point":
        dec, k = unit(i + used + 1)
        if dec is not None and dec < 10 and k == 1:
            return f"{s}.{dec}", used + 2
    return s, used


def digits(text: str) -> list[str]:
    """The words, number words as digits ("exit nine a" -> ["exit", "9a"]; "two eighty seven west" -> ["287",
    "west"]); "9 a" -> "9a" after an exit word; "10 point 4" -> "10.4"."""
    ws = tokens(text)
    out: list[str] = []
    i = 0
    while i < len(ws):
        v, n = _number(ws, i)
        if n:
            out.append(v)
            i += n
            continue
        out.append(ws[i])
        i += 1
    fixed: list[str] = []
    for j, w in enumerate(out):
        prev = fixed[-1] if fixed else ""
        if w == "point" and prev.isdigit() and j + 1 < len(out) and out[j + 1].isdigit() and len(out[j + 1]) == 1:
            fixed[-1] = f"{prev}.{out[j + 1]}"
            out[j + 1] = ""
            continue
        if not w:
            continue
        nxt = out[j + 1] if j + 1 < len(out) else ""
        if (len(w) == 1 and w in "abcdens" and prev.isdigit() and len(fixed) >= 2 and fixed[-2] == "exit"
                and not (w == "a" and nxt.isalpha() and len(nxt) > 1 and nxt not in DIRS and nxt not in SHORT_DIRS)):
            fixed[-1] = prev + w                              # "exit 9 a" -> "exit 9a"
            continue
        fixed.append(w)
    return fixed


def _aliases() -> list[tuple[tuple[str, ...], str]]:
    from sizeup.geo.places import ROUTE_ALIASES, norm_tokens
    out = []
    for route, al in ROUTE_ALIASES.items():
        if route.startswith(("US-", "NY-")):
            continue                                          # local roads: not highways for this
        for a in al:
            out.append((tuple(tokens(" ".join(norm_tokens(a)))), route))
    for route, al in SPOKEN.items():
        for a in al:
            out.append((tuple(tokens(a)), route))
    out = [(t, r) for t, r in out if t]
    return sorted(set(out), key=lambda x: -len(x[0]))


_ALIASES: list | None = None
_EXTRA: dict = {}


def _derived(routes) -> list[tuple[tuple[str, ...], str]]:
    """Aliases of routes the place search's list doesn't know, from their names alone ("I-123": "i 123",
    "interstate 123", a bare "123" beside a highway word)."""
    out = []
    from sizeup.geo.places import ROUTE_ALIASES
    for r in routes or ():
        if r in ROUTE_ALIASES or str(r).upper().startswith(("US-", "NY-", "CT-")):
            continue                                          # the place search's own (local roads: never a highway)
        m = re.fullmatch(r"(?:I|US|NY|CT|RT|Route)-?\s?(\d{1,3}[A-Z]?)", str(r), re.I)
        if m:
            n = m.group(1).lower()
            out += [(("i", n), r), (("interstate", n), r), (("route", n), r), ((n,), r)]
    return out


def routes_in(ws: list[str], routes=()) -> list[tuple[int, int, str]]:
    """[(start, end, route)] of every route named in the words, first to last (longest name first at a place).
    routes: the routes markers are known for (their names give aliases of their own: _derived)."""
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = _aliases()
    key = tuple(sorted(str(r) for r in routes or ()))
    if key not in _EXTRA:
        _EXTRA[key] = sorted(set(_ALIASES) | set(_derived(key)), key=lambda x: -len(x[0]))
    aliases = _EXTRA[key]
    bare = BARE | {a[0] for a, _r in aliases if len(a) == 1 and a[0].isdigit()}
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(ws):
        for alias, route in aliases:
            n = len(alias)
            if tuple(ws[i:i + n]) != alias:
                continue
            if n == 1 and alias[0] in bare and not _hwy_beside(ws, i):
                continue
            if any(w in STREET_WORDS for w in ws[i + n:i + n + 3]):
                continue                      # "12 Hutchinson Avenue", "998 North Main Street": a street
            out.append((i, i + n, route))
            i += n
            break
        else:
            i += 1
    return out


def _hwy_beside(ws: list[str], i: int) -> bool:
    """A bare route number with a highway word beside it: "the 287", "i 95", "route 287", "287 west", "95 exit 21"."""
    before = ws[i - 1] if i > 0 else ""
    if before in ("the", "i", "interstate", "route", "rte", "on", "onto", "off"):
        return True
    near = ws[max(0, i - 3):i] + ws[i + 1:i + 4]
    return any(w in HWY_WORDS for w in near)


def parse(text: str, routes=()) -> Ref | None:
    """The highway the words put a crash on (the module's doc), or None. routes: the routes markers are known for."""
    ws = digits(text)
    found = routes_in(ws, routes)
    if not found:
        return None
    s, e, route = found[0]
    ref = Ref(route)
    others = [r for _s, _e, r in found if r != route]
    for j, w in enumerate(ws):
        if w in DIRS and not ref.dir:
            ref.dir = DIRS[w]
        elif w == "bound" and j and ws[j - 1] in SHORT_DIRS and not ref.dir:
            ref.dir = SHORT_DIRS[ws[j - 1]]
    if not ref.dir and e < len(ws) and ws[e] in SHORT_DIRS:
        ref.dir = SHORT_DIRS[ws[e]]                           # "287 west"
    for j, w in enumerate(ws):
        if w == "exit" and j + 1 < len(ws) and re.fullmatch(r"\d{1,3}[a-z]?", ws[j + 1]):
            ref.kind, ref.value = "exit", ws[j + 1].upper()
            break
        if w in ("mm", "milepost", "marker", "post") and j + 1 < len(ws) and re.fullmatch(r"\d{1,3}(?:\.\d)?", ws[j + 1]):
            if w != "post" or (j and ws[j - 1] == "mile"):
                ref.kind, ref.value = "mm", ws[j + 1]
                break
    ref.ramp = any(w in RAMP for w in ws) or "on ramp" in " ".join(ws) or "off ramp" in " ".join(ws)
    if others:
        ref.other = others[0]
    return ref


def _m(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat1 - lat2) * 110_574.0, (lon1 - lon2) * 111_320.0 * math.cos(math.radians(lat1)))


class Corridor:
    """The highway places over the place search's markers ({route: [{kind exit|milemarker, dir, value, lat, lon,
    muni}]}); area_m(lat, lon): metres to our area (0 inside), None: not known (every point is out of the area)."""

    def __init__(self, markers: dict, area_m=None, buffer_m: float = 2000.0):
        self.markers = {r: list(v) for r, v in (markers or {}).items()}
        self.area_m = area_m
        self.buffer_m = float(buffer_m)
        self._near: dict = {}

    def dist(self, lat, lon) -> float:
        if self.area_m is None or lat is None or lon is None:
            return math.inf
        try:
            return float(self.area_m(float(lat), float(lon)))
        except Exception:  # noqa: BLE001 - our area not known now: nothing is near it
            return math.inf

    def parse(self, text: str) -> Ref | None:
        return parse(text, self.markers)

    def near_towns(self, route: str, buffer_m: float | None = None) -> set[str]:
        """The towns (lower case) of the route's markers within the buffer of our area."""
        b = self.buffer_m if buffer_m is None else float(buffer_m)
        k = (route, b)
        if k not in self._near:
            self._near[k] = {str(r.get("muni") or "").lower() for r in self.markers.get(route, ())
                             if r.get("muni") and self.dist(r["lat"], r["lon"]) <= b}
        return self._near[k]

    def place(self, ref: Ref | None) -> Place | None:
        if ref is None:
            return None
        rows = self.markers.get(ref.route)
        d = f" {ref.dir}" if ref.dir else ""
        if not rows:
            return Place("street", None, None, 0.0, f"{ref.route}{d}", "", False, 0.3, [], "highway: no markers")
        if ref.kind == "exit":
            hits = [r for r in rows if r.get("kind") == "exit" and str(r.get("value", "")).upper() == ref.value]
            if not hits:     # "exit 9" for "9A" / "9N-S"
                hits = [r for r in rows if r.get("kind") == "exit"
                        and re.match(rf"{re.escape(ref.value)}(?![0-9])", str(r.get("value", "")).upper())]
            best = sorted(hits, key=lambda r: (bool(ref.dir) and r.get("dir") != ref.dir, str(r.get("value"))))
            if best:
                r = best[0]
                return Place("landmark", r["lat"], r["lon"], EXIT_RADIUS_M, f"{ref.route}{d} exit {r['value']}",
                             str(r.get("muni") or ""), False, 0.7, [], "highway exit")
        if ref.kind == "mm":
            try:
                v = float(ref.value)
            except ValueError:
                v = None
            mms = [r for r in rows if r.get("kind") != "exit" and _f(r.get("value")) is not None
                   and abs(_f(r.get("value")) - v) <= 0.3] if v is not None else []
            best = sorted(mms, key=lambda r: (bool(ref.dir) and r.get("dir") != ref.dir, abs(_f(r.get("value")) - v)))
            if best:
                r = best[0]
                return Place("landmark", r["lat"], r["lon"], EXIT_RADIUS_M, f"{ref.route}{d} MM {ref.value}",
                             str(r.get("muni") or ""), False, 0.7, [], "highway mile marker")
        if ref.other and ref.ramp and self.markers.get(ref.other):
            meet = self._meet(ref.route, ref.other)
            if meet is not None:
                lat, lon, muni = meet
                return Place("landmark", lat, lon, INTERCHANGE_M / 2, f"{ref.route}{d} / {ref.other} interchange",
                             muni, False, 0.6, [], "highway interchange")
        return Place("street", None, None, 0.0, f"{ref.route}{d}", "", False, 0.3, [], "highway: the route only")

    def _meet(self, a: str, b: str):
        best = None
        for r in self.markers.get(a, ()):
            for s in self.markers.get(b, ()):
                dm = _m(r["lat"], r["lon"], s["lat"], s["lon"])
                if dm <= INTERCHANGE_M and (best is None or dm < best[0]):
                    best = (dm, (r["lat"] + s["lat"]) / 2, (r["lon"] + s["lon"]) / 2, str(r.get("muni") or s.get("muni") or ""))
        return None if best is None else best[1:]

    def gate(self, place: Place | None, source_towns=(), buffer_m: float | None = None) -> str:
        """"" ours, "out_of_area" elsewhere, "unplaced" not known where (the module's doc)."""
        b = self.buffer_m if buffer_m is None else float(buffer_m)
        if place is None or place.precision == "none":
            return "unplaced"
        route = route_of(place.label)
        if not self.markers.get(route):
            return "out_of_area"
        if place.lat is not None and place.lon is not None:
            return "" if self.dist(place.lat, place.lon) <= b else "out_of_area"
        towns = {str(t).lower() for t in source_towns or () if t}
        return "" if towns & self.near_towns(route, b) else "unplaced"

    def same(self, a: Place, b: Place, merge_m: float) -> bool:
        """One crash: the same route, and the two points within merge_m (or one of them the route alone)."""
        if route_of(a.label) != route_of(b.label) or not route_of(a.label):
            return False
        da, db = dir_of(a.label), dir_of(b.label)
        if da and db and da != db:
            return False                      # the other carriageway: another crash
        if None in (a.lat, a.lon, b.lat, b.lon):
            return True
        return _m(a.lat, a.lon, b.lat, b.lon) <= merge_m

    def at_incident(self, place: Place, inc: dict, link_m: float, buffer_m: float | None = None) -> bool:
        """Our incident is at this crash: its point within link_m of the crash's; the route alone: the incident on
        that route (its label or address names it) or within EXIT_RADIUS_M of one of its markers, and in or by our
        area."""
        lat, lon = inc.get("lat"), inc.get("lon")
        if place.lat is not None and place.lon is not None and lat is not None and lon is not None:
            return _m(place.lat, place.lon, float(lat), float(lon)) <= link_m
        route = route_of(place.label)
        if not route:
            return False
        words = " ".join(str(inc.get(k) or "") for k in ("label", "address"))
        named = any(r == route for _s, _e, r in routes_in(digits(words), self.markers))
        if lat is None or lon is None:
            return named
        b = self.buffer_m if buffer_m is None else float(buffer_m)
        if self.dist(float(lat), float(lon)) > b:
            return False
        return named or any(_m(float(lat), float(lon), r["lat"], r["lon"]) <= EXIT_RADIUS_M
                            for r in self.markers.get(route, ()))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def route_of(label: str) -> str:
    """The route of a highway place's label ("I-287 WB exit 10" -> "I-287"; "Cross County" -> "Cross County")."""
    lab = str(label or "").strip()
    return re.split(r" (?:NB|SB|EB|WB)\b| exit | MM | / ", lab, maxsplit=1)[0].strip() if lab else ""


def dir_of(label: str) -> str:
    """The direction of a highway place's label ("I-287 WB exit 10" -> "WB"), "" for none."""
    m = re.search(r" (NB|SB|EB|WB)\b", str(label or ""))
    return m.group(1) if m else ""


def title_where(label: str) -> str:
    """The place as a title says it: "I-287 WB exit 10" -> "I-287 WB near exit 10"; the rest as it is."""
    return re.sub(r" (exit|MM) ", lambda m: f" near {m.group(1)} ", str(label or ""), count=1)


def towns_of(dept: str, extra=()) -> list[str]:
    """A source's own towns: its department's town ("Sample PD" -> Sample; the county's town list), and extra."""
    out = [str(t) for t in extra or () if t]
    d = re.sub(r"\b(pd|fd|ems|police|fire|department|dept|vac|ambulance)\b", " ", str(dept or ""), flags=re.I)
    d = " ".join(d.split()).lower()
    if d:
        try:
            from sizeup.intel.rules import town_names
            names = town_names()
        except Exception:  # noqa: BLE001
            names = {}
        if d in names and names[d] not in out:
            out.append(names[d])
    return out


class LiveCorridor:
    """The live engine's corridor (engine.highway): the place search's markers (places(): the CAD's place_index) and
    our area's shapes (shapes(): sizeup/roads.py area_shapes, the fire districts marked ours, else our municipal
    boundaries, else a box), built on first use and again every TTL_S (the shapes load after start). The buffer is the
    pack's (engine: gate(..., buffer_m)). A call: the Corridor, or None (no place search here)."""

    TTL_S = 600.0

    def __init__(self, places, shapes, clock=None):
        import time as _t
        self.places, self.shapes, self.clock = places, shapes, clock or _t.monotonic
        self._c: Corridor | None = None
        self._at = -1e18

    def __call__(self) -> Corridor | None:
        now = self.clock()
        if self._c is not None and now - self._at < self.TTL_S:
            return self._c
        ix = self.places() if callable(self.places) else self.places
        if ix is None:
            return self._c
        try:
            got = self.shapes() if callable(self.shapes) else self.shapes
            geoms = list((got or {}).get("shapes") or []) if isinstance(got, dict) else list(got or [])
        except Exception:  # noqa: BLE001 - no shapes now: every point is out of the area (held), tried again later
            geoms = []
        from sizeup.roads import polygon_m

        def area_m(lat: float, lon: float) -> float:
            return min((polygon_m(g, lat, lon) for g in geoms), default=math.inf)
        self._c, self._at = Corridor(getattr(ix, "markers", {}) or {}, area_m if geoms else None), now
        return self._c
