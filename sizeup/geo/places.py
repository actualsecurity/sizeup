"""Place search for the CAD: what an "@" in a note and the location box suggest (GitHub #67).

One ranked list across every kind of place the gazetteer knows, for typed (not spoken) text:

    kind          from                                            "@..." typed               label
    address       addresses (NYS / CT 9-1-1 points)               189 North Main             189 N Main St
    intersection  intersections (TIGER)                           king & arbor               King St & Arbor Dr
    street        streets                                         westchester av             Westchester Ave
    place         landmarks + places kind "place" (county, OSM)   lyon park, high school     Lyon Park
    milemarker    places (NYSDOT reference markers, Thruway)      287 eastbound mile marker 10   I-287 EB MM 10.0
    exit          places (OSM motorway junctions)                 hutch sb exit 30           Hutch SB exit 30
    place         the department's own names (Settings, cad.sqlite: Cad.search_places passes them in)

Ranking: how well the text matches (every typed word must start a word of the name, in any order;
whole words and the start of the name count more), then our area (Port Chester, Rye Brook, then the
neighbours: gazetteer.TOWN_PRIORITY), then how close it is to the event (``near``). A highway query
("I-95 NB exit 21", "287 eb mm 10") puts that highway's markers first.

``PlaceIndex(gazetteer)`` builds everything in memory once (a fraction of a second); ``search`` is pure.
"""

from __future__ import annotations

import bisect
import math
import re
import sqlite3
from dataclasses import dataclass

from .normalize import DIRECTION_WORDS, STREET_TYPES

# ----------------------------------------------------------------------------------- text

# One spelling per word on both sides (the names and what is typed): street types abbreviated as
# in the address data ("avenue" -> "ave"), directions as letters ("north" -> "n").
WORD_MAP = {**STREET_TYPES, **DIRECTION_WORDS, "saint": "st", "mount": "mt", "fort": "ft",
            "theatre": "theater", "centre": "center", "intl": "international"}
BOUND = {"northbound": "nb", "southbound": "sb", "eastbound": "eb", "westbound": "wb",
         "nb": "nb", "sb": "sb", "eb": "eb", "wb": "wb", "n": "nb", "s": "sb", "e": "eb", "w": "wb"}
MARKER_WORDS = {"mm", "mile", "miles", "marker", "markers", "milemarker", "milepost", "mp", "mi", "rm",
                "ref", "reference", "post", "pole"}
EXIT_WORDS = {"exit", "exits", "ex", "interchange", "int", "x"}
FILLER = {"at", "near", "by", "on", "the", "of", "and", "approaching", "past", "before", "after", "bound"}
STOP = {"the", "of", "a"}
SEPARATORS = {"and", "at"}

_TOKEN = re.compile(r"\d+\.\d+|\d+(?:st|nd|rd|th)\b|\d+[a-z](?![a-z])|\d+|[a-z]+")
NUMBER = re.compile(r"\d+[a-z]?$")


def norm_tokens(text: str) -> list[str]:
    """'189 North Main Street' -> ['189', 'n', 'main', 'st']; 'I287 EB MM 10.0' -> ['i', '287', 'eb', 'mm', '10.0']."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = t.replace("&", " and ").replace("/", " and ").replace("@", " ").replace("+", " and ")
    return [WORD_MAP.get(w, w) for w in _TOKEN.findall(t)]


def norm_phrase(text: str) -> str:
    return " ".join(norm_tokens(text))


# ----------------------------------------------------------------------------------- highways

# The highways: display name, (positive direction, negative), the positive direction as (dlat, dlon):
# a marker or exit is on the positive side when its carriageway heads that way.
ROUTES = {
    "I-95": ("NB", "SB", (1.0, 1.0)),          # New England Thruway: north is toward Connecticut
    "I-287": ("EB", "WB", (-0.3, 1.0)),         # Cross Westchester: east is toward Rye / I-95
    "I-684": ("NB", "SB", (1.0, 0.0)),
    "Hutch": ("NB", "SB", (1.0, 0.3)),
    "Merritt Pkwy": ("NB", "SB", (0.3, 1.0)),   # CT 15
    "US-1": ("NB", "SB", (1.0, 1.0)),           # Boston Post Rd
    "NY-120": ("NB", "SB", (1.0, 0.0)),
    "NY-120A": ("NB", "SB", (1.0, 0.0)),
}
# Typed names of each highway (normalised). A bare number ("287") counts only when the rest of the
# text is about a highway (a direction, "exit", "mile marker", a number): "287 King St" is an address.
ROUTE_ALIASES = {
    "I-95": ["i 95", "interstate 95", "new england thruway", "new england twy", "ne thruway", "thruway", "95"],
    "I-287": ["i 287", "interstate 287", "cross westchester", "cross westchester expy", "cwe", "287"],
    "I-684": ["i 684", "interstate 684", "684"],
    "Hutch": ["hutch", "hutchinson", "hutchinson river", "hutchinson river pkwy", "hutchinson pkwy", "hrp"],
    "Merritt Pkwy": ["merritt", "merritt pkwy", "ct 15", "rte 15", "ct rte 15"],
    "US-1": ["us 1", "us rte 1", "rte 1", "boston post rd", "post rd"],
    "NY-120": ["ny 120", "rte 120", "ny rte 120", "sr 120", "120"],
    "NY-120A": ["ny 120a", "rte 120a", "ny rte 120a", "sr 120a", "120a"],
}
_ALIAS_LIST = sorted(((tuple(norm_tokens(a)), r) for r, al in ROUTE_ALIASES.items() for a in al),
                     key=lambda x: -len(x[0]))
DIR_NAMES = {"nb": "NB", "sb": "SB", "eb": "EB", "wb": "WB"}


@dataclass
class Highway:
    route: str
    dir: str            # "NB" ... or ""
    kind: str           # "mm" | "exit" | ""
    value: str          # "10.0", "21a", ""
    bare: bool          # only a bare number named the route


def parse_highway(q: list[str]) -> Highway | None:
    """'287 eb mm 10' -> Highway('I-287', 'EB', 'mm', '10'); None when it isn't about a highway."""
    for i in range(len(q)):
        for alias, route in _ALIAS_LIST:
            n = len(alias)
            seg = tuple(q[i:i + n])
            whole = seg == alias
            # the highway's name still being typed at the end: "hutc", "merri"
            partial = (not whole and i + n == len(q) and seg[:-1] == alias[:-1] and len(seg[-1]) >= 3
                       and alias[-1].startswith(seg[-1]) and not seg[-1].isdigit())
            if not (whole or partial):
                continue
            rest = q[:i] + q[i + n:]
            h = Highway(route, "", "", "", n == 1 and alias[0][0].isdigit())
            ok = True
            for t in rest:
                if t in BOUND and not (t in ("n", "s", "e", "w") and h.dir):
                    h.dir = DIR_NAMES[BOUND[t]]
                elif t in MARKER_WORDS:
                    h.kind = h.kind or "mm"
                elif t in EXIT_WORDS:
                    h.kind = "exit"
                elif t in FILLER:
                    continue
                elif re.fullmatch(r"\d+(?:\.\d+)?[a-z]?", t) and not h.value:
                    h.value = t
                else:
                    ok = False
                    break
            if not ok:
                continue
            if h.bare and not (rest and (h.dir or h.kind or h.value)) and len(q) > 1:
                continue
            return h
    return None


# ----------------------------------------------------------------------------------- name matching

class NameIndex:
    """Word-prefix matching over names: every typed word starts a different word of one alias."""

    def __init__(self, aliases: list[tuple[tuple[str, ...], int]]):
        self.aliases = aliases                       # (tokens, owner) per alias
        post: dict[str, list[int]] = {}
        for k, (toks, _o) in enumerate(aliases):
            for t in set(toks):
                post.setdefault(t, []).append(k)
        self.vocab = sorted(post)
        self.post = post

    def _prefixed(self, q: str) -> set[int]:
        out: set[int] = set()
        lo = bisect.bisect_left(self.vocab, q)
        hi = bisect.bisect_left(self.vocab, q + "\x7f")
        for t in self.vocab[lo:hi]:
            out.update(self.post[t])
        return out

    def match(self, q: list[str], limit: int = 40) -> list[tuple[int, float]]:
        """-> [(owner, quality 0.55..1)] best first, one per owner."""
        q = [t for t in q if t not in STOP] or q
        if not q:
            return []
        cand: set[int] | None = None
        for t in sorted(set(q), key=len, reverse=True):
            s = self._prefixed(t) if (len(t) >= 2 or t == q[-1] or t.isdigit()) else set(self.post.get(t, ()))
            cand = s if cand is None else cand & s
            if not cand:
                return []
        best: dict[int, float] = {}
        for k in cand or ():
            toks, owner = self.aliases[k]
            quality = score_alias(q, toks)
            if quality and quality > best.get(owner, 0.0):
                best[owner] = quality
        return sorted(best.items(), key=lambda kv: -kv[1])[:limit]


def score_alias(q: list[str], toks: tuple[str, ...]) -> float:
    used = [False] * len(toks)
    exact_all, first, order, last = True, False, True, -1
    for n, t in enumerate(q):
        hit = next((k for k, a in enumerate(toks) if not used[k] and a == t), None)
        if hit is None:
            hit = next((k for k, a in enumerate(toks) if not used[k] and a.startswith(t)), None)
            exact_all = False
        if hit is None:
            return 0.0
        used[hit] = True
        first = first or (n == 0 and hit == 0)
        order = order and hit > last
        last = hit
    total = sum(len(a) for a in toks) or 1
    cover = min(1.0, sum(len(t) for t in q) / total)
    return round(0.55 + 0.25 * cover + 0.08 * first + 0.04 * order + 0.08 * (exact_all and all(used)), 4)


# ----------------------------------------------------------------------------------- the index

M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(41.02))
KIND_ORDER = {"milemarker": 0, "exit": 0, "address": 1, "place": 2, "intersection": 3, "street": 4}
KIND_BONUS = {"address": 0.06, "place": 0.04, "intersection": 0.03, "street": 0.0}
HIGHWAY_BONUS = 0.3
LOCAL_BONUS = 0.08                     # the department's own names (Settings > Places)


def dist_km(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat1 - lat2) * M_PER_DEG_LAT, (lon1 - lon2) * M_PER_DEG_LON) / 1000.0


def area_bonus(muni: str, priority: tuple[str, ...]) -> float:
    """Port Chester and Rye Brook first (0.12), the neighbours after them, anywhere else nothing."""
    if muni not in priority:
        return 0.0
    i = priority.index(muni)
    return 0.12 if i < 2 else max(0.02, 0.07 - 0.01 * (i - 2))


def near_bonus(lat: float, lon: float, near: tuple[float, float] | None) -> float:
    if not near:
        return 0.0
    return 0.1 * max(0.0, 1.0 - dist_km(lat, lon, near[0], near[1]) / 8.0)


def _dedupe(ranked: list[dict]) -> list[dict]:
    """One of each: the same label at the same spot, or two names for one place (a county landmark
    and its OSM twin: "Capitol Theater" / "The Capitol Theatre", one name inside the other) within 400 m;
    one name twice within 1 km (a big park's two points). Best first stays."""
    out: list[dict] = []
    for r in ranked:
        words = set(norm_tokens(r["label"])) - STOP
        dup = False
        for o in out:
            d = dist_km(r["lat"], r["lon"], o["lat"], o["lon"])
            if d > 1.0:
                continue
            if r["label"].lower() == o["label"].lower():
                dup = d <= (1.0 if r["kind"] == o["kind"] == "place" else 0.15)    # a big park: two points
            elif r["kind"] == o["kind"] == "place" and d <= 0.4:
                ow = set(norm_tokens(o["label"])) - STOP
                dup = bool(words) and bool(ow) and (words <= ow or ow <= words)
            if dup:
                break
        if not dup:
            out.append(r)
    return out


@dataclass
class _Ctx:
    near: tuple[float, float] | None
    priority: tuple[str, ...]


class PlaceIndex:
    def __init__(self, gaz, priority: tuple[str, ...] | None = None):
        from .gazetteer import TOWN_PRIORITY
        self.gaz = gaz
        self.priority = tuple(priority or TOWN_PRIORITY)
        self.places: list[dict] = []           # landmarks first, then the places table's "place" rows
        for lm in gaz.landmarks:
            self.places.append({"name": lm["name"], "category": lm["category"], "address": lm["address"],
                                "muni": lm["muni"], "lat": lm["lat"], "lon": lm["lon"], "source": lm["source"],
                                "aliases": [a for a in lm["aliases"] if a]})
        self.markers: dict[str, list[dict]] = {r: [] for r in ROUTES}
        for row in self._rows():
            if row["kind"] == "place":
                self.places.append(row)
            elif row["route"] in self.markers:
                self.markers[row["route"]].append(row)
        aliases: list[tuple[tuple[str, ...], int]] = []
        for i, p in enumerate(self.places):
            names = {tuple(norm_tokens(p["name"]))}
            names |= {tuple(norm_tokens(a)) for a in p["aliases"]}
            if p["name"].lower().startswith("the "):
                names.add(tuple(norm_tokens(p["name"][4:])))
            aliases += [(n, i) for n in names if n]
        self.place_ix = NameIndex(aliases)
        self.street_ids = list(gaz.streets)
        self.street_ix = NameIndex([(tuple(norm_tokens(gaz.streets[sid].name)), k)
                                    for k, sid in enumerate(self.street_ids)])
        self._cross: dict[int, list] = {}                 # street id -> its intersections
        for pair, pts in gaz._inters.items():
            for sid in pair:
                self._cross.setdefault(sid, []).append((pair, pts))
        self.counts = {"places": len(self.places), "markers": sum(len(v) for v in self.markers.values()),
                       "streets": len(self.street_ids)}

    def _rows(self) -> list[dict]:
        try:
            db = sqlite3.connect(f"file:{self.gaz.path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            cur = db.execute("SELECT kind, name, category, address, muni, lat, lon, route, dir, value, source, ref, "
                             "aliases FROM places")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur]
        except sqlite3.Error:                # a gazetteer built before the places table
            rows = []
        finally:
            db.close()
        for r in rows:
            r["aliases"] = [a for a in (r["aliases"] or "").split("|") if a]
        return rows

    # -------------------------------------------------------------------------- search

    def search(self, text: str, near: tuple[float, float] | None = None, limit: int = 8,
               extra: list[dict] = (), priority: tuple[str, ...] | None = None) -> list[dict]:
        """Ranked suggestions for typed text. extra: the department's own names
        [{name, aliases?, lat, lon, muni?, label?}] (Settings), matched like the places. priority: the
        towns in order (the CAD's geo_town_order), else gazetteer.TOWN_PRIORITY. Each hit: kind, label,
        sub (its town), lat, lon, source, quality (how well the text matched, 0..1), score (the rank)."""
        near = _Ctx(near, tuple(priority) if priority else self.priority)
        q = norm_tokens(text)
        if not q or (len(q) == 1 and len(q[0]) < 2 and not q[0].isdigit()):
            return []
        out: list[dict] = []
        hw = parse_highway(q)
        if hw is not None:
            out += self._highway(hw, near)
        if NUMBER.match(q[0]) and len(q) > 1:
            out += self._addresses(q[0], q[1:], near)
        cut = next((i for i, t in enumerate(q) if t in SEPARATORS and 0 < i), None)
        if cut is not None:
            out += self._intersections(q[:cut], q[cut + 1:], near)   # "king & arb", "westchester ave &"
        open_cross = len(q) > 1 and q[-1] in SEPARATORS          # "king st &": the cross street comes next
        while len(q) > 1 and q[-1] in SEPARATORS:
            q = q[:-1]
        out += self._places(q, near)
        out += self._local(q, near, extra)
        if not (hw and hw.value) and not open_cross:
            out += self._streets(q, near)
        ranked = _dedupe(sorted(out, key=lambda r: (-r["score"], KIND_ORDER.get(r["kind"], 9), r["label"])))
        for r in ranked:
            r["score"] = round(r["score"], 3)
        return ranked[:limit]

    @staticmethod
    def _item(kind: str, label: str, muni: str, lat: float, lon: float, source: str, quality: float,
              ctx: _Ctx, bonus: float = 0.0, **more) -> dict:
        score = (quality + bonus + KIND_BONUS.get(kind, 0.0) + area_bonus(muni, ctx.priority)
                 + near_bonus(lat, lon, ctx.near))
        return dict({"kind": kind, "label": label, "sub": muni, "lat": round(float(lat), 6),
                     "lon": round(float(lon), 6), "source": source, "quality": round(min(1.0, quality), 3),
                     "score": score}, **more)

    def _highway(self, h: Highway, near, any_side: bool = False) -> list[dict]:
        rows = [r for r in self.markers.get(h.route, ()) if any_side or not h.dir or r["dir"] in (h.dir, "")]
        out = []
        for r in rows:
            is_exit = r["kind"] == "exit"
            if (h.kind == "exit" and not is_exit) or (h.kind == "mm" and is_exit):
                continue
            quality = 0.0
            if not h.value:
                quality = 0.62 if (is_exit or h.kind == "mm") else 0.0
            elif is_exit:
                quality, old = self._exit_quality(h.value, r)
            else:
                quality = self._mile_quality(h.value, r)
            if not quality:
                continue
            d = h.dir if (h.dir and not r["dir"]) else r["dir"]
            label = f"{r['route']}{' ' + d if d else ''} " + (f"exit {r['value']}" if is_exit else f"MM {r['value']}")
            if is_exit and h.value and old:
                label += f" (old {old})"
            detail = r["address"] or (f"Ref marker {r['ref']}" if r["source"] == "nysdot-rm" else "")
            out.append(self._item("exit" if is_exit else "milemarker", label, r["muni"], r["lat"], r["lon"],
                                  r["source"], quality * (0.9 if any_side else 1.0), near, HIGHWAY_BONUS,
                                  route=r["route"], dir=d, value=r["value"], detail=detail))
        if not out and h.dir and h.value and not any_side:
            return self._highway(h, near, any_side=True)     # not on that side (or not mapped): the other side's
        return out

    @staticmethod
    def _exit_quality(v: str, r: dict) -> tuple[float, str]:
        """How well an exit number matches: its number, else its number before a renumbering (the Hutch's
        "exit 30" is 19A now: OSM old_ref, kept as an "old 30s" alias). -> (quality, the old number or "")."""
        def q(have: str) -> float:
            return 1.0 if have == v else 0.86 if have.startswith(v) and len(have) - len(v) <= 1 else 0.0
        v = v.upper()
        best, old = q(r["value"].upper()), ""
        for a in r["aliases"]:
            if a.startswith("old ") and q(a[4:].upper()) * 0.97 > best:
                best, old = q(a[4:].upper()) * 0.97, a[4:].upper()
        return best, old

    @staticmethod
    def _mile_quality(v: str, r: dict) -> float:
        if re.fullmatch(r"\d{4}", v) and r["ref"].replace(" ", "").endswith(v):
            return 0.97                                  # the marker's own number: "5041"
        try:
            want, have = float(v), float(r["value"])
        except ValueError:
            return 0.0
        if abs(want - have) < 0.05:
            return 1.0
        if "." not in v and r["value"].split(".")[0] == v:
            return 0.8                                   # "10": 10.1 ... 10.9 after 10.0
        if abs(want - have) <= 0.15:
            return 0.85
        return 0.0

    def _addresses(self, num: str, rest: list[str], near) -> list[dict]:
        digits = int(re.sub(r"\D", "", num) or 0)
        suffix = re.sub(r"\d", "", num)
        if not digits:
            return []
        out = []
        for k, sq in self.street_ix.match(rest, limit=6):
            st = self.gaz.streets[self.street_ids[k]]
            for muni, lst in st.addrs.items():
                res = self.gaz._locate_number(lst, st.nums[muni], digits, suffix)
                if res is None:
                    continue
                lat, lon, _q, precision = res
                f = {"exact": 1.0, "interpolated": 0.9}.get(precision, 0.82)
                label = f"{digits}{suffix.upper()} {st.name}"
                out.append(self._item("address", label, muni, lat, lon, "address-points", sq * f, near,
                                      precision=precision))
        return out

    def _intersections(self, a: list[str], b: list[str], near) -> list[dict]:
        """'king & arbor': where the two meet; 'king &' (nothing after it yet): its cross streets."""
        sa = self.street_ix.match(a, limit=8)
        out = []
        if not b:
            for ka, qa in sa[:2]:
                ia = self.street_ids[ka]
                for (x, y), pts in self._cross.get(ia, ()):
                    other = y if x == ia else x
                    for lat, lon, muni in pts:
                        label = f"{self.gaz.streets[ia].name} & {self.gaz.streets[other].name}"
                        out.append(self._item("intersection", label, muni, lat, lon, "tiger", qa * 0.85, near))
            return out
        sb = self.street_ix.match(b, limit=8)
        for ka, qa in sa:
            for kb, qb in sb:
                ia, ib = self.street_ids[ka], self.street_ids[kb]
                if ia == ib:
                    continue
                for lat, lon, muni in self.gaz._inters.get((min(ia, ib), max(ia, ib)), ()):
                    label = f"{self.gaz.streets[ia].name} & {self.gaz.streets[ib].name}"
                    out.append(self._item("intersection", label, muni, lat, lon, "tiger", (qa + qb) / 2, near))
        return out

    def _places(self, q: list[str], near) -> list[dict]:
        out = []
        for i, quality in self.place_ix.match(q, limit=30):
            p = self.places[i]
            cat = p["category"].split("=")[-1].replace("_", " ")          # an OSM landmark's "leisure=park": park
            out.append(self._item("place", p["name"], p["muni"], p["lat"], p["lon"], p["source"], quality, near,
                                  category=cat, detail=p["address"]))
        return out

    def _local(self, q: list[str], near, extra) -> list[dict]:
        rows = [r for r in (extra or ()) if r.get("lat") is not None and r.get("lon") is not None]
        if not rows:
            return []
        ix = NameIndex([(tuple(norm_tokens(n)), i) for i, r in enumerate(rows)
                        for n in [r["name"], *(r.get("aliases") or [])] if norm_tokens(n)])
        out = []
        for i, quality in ix.match(q):
            r = rows[i]
            out.append(self._item("place", r.get("label") or r["name"], r.get("muni") or "", r["lat"], r["lon"],
                                  "local", quality, near, LOCAL_BONUS, category="local name",
                                  detail=r.get("note") or ""))
        return out

    def _streets(self, q: list[str], near) -> list[dict]:
        out = []
        for k, quality in self.street_ix.match(q, limit=6):
            sid = self.street_ids[k]
            st = self.gaz.streets[sid]
            for muni in list(st.addrs) or [None]:
                p = self.gaz._street_point(sid, muni)
                if p is None:
                    continue
                out.append(self._item("street", st.name, muni or p[2], p[0], p[1], "tiger", quality, near, -0.08))
        return out
