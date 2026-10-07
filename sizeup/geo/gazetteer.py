"""Offline geocoder for dispatch-page transcripts and typed CAD addresses.

Covers Port Chester and Rye Brook (PCFD's district) and the surrounding towns:
the City of Rye, Harrison (incl. Purchase / West Harrison), the Village of
Mamaroneck (incl. Rye Neck) and Greenwich, CT (Byram, Glenville, Pemberwick, ...).

``Gazetteer`` loads ``data/gazetteer.sqlite`` (built by ``tools/fetch_addresses.py``
from NYS / CT 9-1-1 address points, TIGER street centrelines and municipal
boundaries, county / OSM facilities) and turns speech-to-text such as "respond to
one two three Westchester Avenue cross of Pearl Street" into a ``Match`` with a
lat/lon and a confidence. When the same street and number exist in several
towns, a town named in the text wins; otherwise ``TOWN_PRIORITY`` decides.
"""

from __future__ import annotations

import bisect
import collections
import difflib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .normalize import (
    BOUNDARY, DIRECTIONS, TYPE_ABBRS, spaceless, street_aliases, tokenize,
)

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "gazetteer.sqlite"

# Written by tools/fetch_addresses.py. Coordinates are WGS84 degrees; ``lines.coords``
# is a JSON list of [lon, lat]; ``landmarks.aliases`` is "|"-separated normalised phrases.
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE streets (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
CREATE TABLE addresses (street_id INTEGER NOT NULL, number INTEGER NOT NULL,
    suffix TEXT NOT NULL, muni TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    points INTEGER NOT NULL);
CREATE TABLE lines (street_id INTEGER NOT NULL, muni TEXT NOT NULL, coords TEXT NOT NULL);
CREATE TABLE intersections (a INTEGER NOT NULL, b INTEGER NOT NULL, lat REAL NOT NULL,
    lon REAL NOT NULL, muni TEXT NOT NULL);
CREATE TABLE landmarks (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
    address TEXT NOT NULL, muni TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    source TEXT NOT NULL, aliases TEXT NOT NULL);
"""

LAT0, LON0 = 41.0, -73.68
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(41.02))

# Tokens that can never be part of a street name span.
STREET_STOP = {
    BOUNDARY, "and", "at", "of", "to", "on", "in", "by", "for", "from", "near", "cross",
    "crosses", "crossing", "corner", "between", "respond", "responding", "reported",
    "report", "a", "an", "is", "with", "off", "x", "units", "unit", "time", "out", "box",
    "engine", "ladder", "alarm", "apartment", "apt", "floor", "number", "no", "please",
    "caller", "states", "fire", "department",
}
# A number right after one of these is not a house number ("time out 1432", "engine 4").
NOT_HOUSE_NUMBER_AFTER = {
    "time", "out", "box", "engine", "ladder", "truck", "tower", "rescue", "squad", "car",
    "unit", "units", "apartment", "apt", "floor", "channel", "battalion", "station", "alarm",
    "tone", "tones", "zone", "district", "medic", "ambulance", "code", "signal", "level",
    "room", "suite", "building", "bldg", "fireground", "exit", "i", "rte", "hwy", "marker",
    "mile", "post", "company", "co", "group", "talkgroup", "chief", "captain", "lieutenant",
}
INTERSECTION_GAP = {"and", "at", "corner", "of", "near", "by", "off", "on", "in", "just", "the"}
INTERSECTION_KEY = {"and", "at", "near", "off", "by"}
CROSS_MARKERS = {"cross", "crosses", "crossing", "between", "x"}
CROSS_GAP = CROSS_MARKERS | {"of", "street", "st", "streets", "is", "are", "and", "at", "the"}

# Ranking of the covered municipalities when the text names none and the same
# street / number exists in several of them: PCFD's own district first, then the
# neighbours (mutual aid), then the rest. Edit this one tuple to change the order;
# municipalities not listed get no preference. Labels match the ``muni`` column.
TOWN_PRIORITY = ("Port Chester", "Rye Brook", "Rye", "Harrison", "Greenwich", "Mamaroneck")
PRIOR_TOP, PRIOR_STEP = 0.05, 0.01
MUNI_PRIOR = {m: round(max(0.0, PRIOR_TOP - PRIOR_STEP * i), 3) for i, m in enumerate(TOWN_PRIORITY)}
# A town named in the text beats the priority order: right next to the location ...
SAID_NEAR_BONUS = 0.12
# ... or anywhere in the text (only for phrases that are not near-only).
SAID_FAR_BONUS = 0.06
assert SAID_FAR_BONUS > PRIOR_TOP and SAID_NEAR_BONUS > SAID_FAR_BONUS + PRIOR_TOP

# Spoken / typed town names: (phrase tokens, municipality, counts only right next to
# the location). Hamlets and neighbourhoods map to their municipality. Longest first.
MUNI_PHRASES = [
    (("village", "of", "port", "chester"), "Port Chester", False),
    (("village", "of", "rye", "brook"), "Rye Brook", False),
    (("city", "of", "rye"), "Rye", False),
    (("town", "of", "harrison"), "Harrison", False),
    (("village", "of", "harrison"), "Harrison", False),
    (("village", "of", "mamaroneck"), "Mamaroneck", False),
    (("town", "of", "mamaroneck"), "Mamaroneck", False),
    (("town", "of", "greenwich"), "Greenwich", False),
    (("port", "chester"), "Port Chester", False),
    (("rye", "brook"), "Rye Brook", False),
    (("west", "harrison"), "Harrison", False),
    (("rye", "city"), "Rye", False),
    (("rye", "neck"), "Mamaroneck", False),
    (("old", "greenwich"), "Greenwich", False),
    (("cos", "cob"), "Greenwich", False),
    (("greenwich",), "Greenwich", False),
    (("mamaroneck",), "Mamaroneck", False),
    (("harrison",), "Harrison", True),
    (("purchase",), "Harrison", True),
    (("byram",), "Greenwich", True),
    (("glenville",), "Greenwich", True),
    (("pemberwick",), "Greenwich", True),
    (("chickahominy",), "Greenwich", True),
    (("riverside",), "Greenwich", True),
    (("rye",), "Rye", True),
]
_MUNI_WORDS = {p for p, _m, _n in MUNI_PHRASES}
TOWN_RANK = {m: i for i, m in enumerate(TOWN_PRIORITY)}


@dataclass
class Match:
    kind: str                     # "address" | "intersection" | "landmark" | "street"
    lat: float
    lon: float
    label: str                    # canonical, e.g. "123 Westchester Ave, Port Chester"
    municipality: str
    confidence: float             # 0..1
    cross_street: str | None = None
    street: str | None = None
    house_number: str | None = None
    precision: str = ""           # exact | interpolated | approximate | intersection | ...
    matched_text: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class _Mention:
    start: int
    end: int
    cands: list[tuple[int, float]]   # (street id, similarity incl. alias penalty)
    strong: bool                     # the spoken span carried a street type
    text: str

    @property
    def best(self) -> float:
        return self.cands[0][1]


@dataclass
class _Street:
    id: int
    key: str
    name: str
    addrs: dict[str, list[tuple[int, str, float, float]]] = field(default_factory=dict)
    nums: dict[str, list[int]] = field(default_factory=dict)
    xy: np.ndarray | None = None          # geometry sample points, metres
    xy_muni: list[str] = field(default_factory=list)


def _xy(lat, lon):
    return ((np.asarray(lon, dtype=float) - LON0) * M_PER_DEG_LON,
            (np.asarray(lat, dtype=float) - LAT0) * M_PER_DEG_LAT)


def _latlon(x: float, y: float) -> tuple[float, float]:
    return LAT0 + y / M_PER_DEG_LAT, LON0 + x / M_PER_DEG_LON


def _trigrams(s: str) -> set[str]:
    s = f"^{s}$"
    return {s[i:i + 3] for i in range(len(s) - 2)}


class Gazetteer:
    """In-memory gazetteer + geocoder. Load once; ``geocode`` / ``find_in_text`` are pure."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} missing: run tools/fetch_addresses.py")
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            self.meta = dict(db.execute("SELECT key, value FROM meta"))
            self.streets: dict[int, _Street] = {
                sid: _Street(sid, key, name)
                for sid, key, name in db.execute("SELECT id, key, name FROM streets")}
            for sid, num, suf, muni, lat, lon in db.execute(
                    "SELECT street_id, number, suffix, muni, lat, lon FROM addresses "
                    "ORDER BY street_id, muni, number, suffix"):
                st = self.streets[sid]
                st.addrs.setdefault(muni, []).append((num, suf, lat, lon))
            geo: dict[int, list] = collections.defaultdict(list)
            for sid, muni, coords in db.execute("SELECT street_id, muni, coords FROM lines"):
                geo[sid].append((muni, json.loads(coords)))
            self._inters: dict[tuple[int, int], list[tuple[float, float, str]]] = \
                collections.defaultdict(list)
            for a, b, lat, lon, muni in db.execute("SELECT a, b, lat, lon, muni FROM intersections"):
                self._inters[(a, b)].append((lat, lon, muni))
            self.landmarks = [
                dict(id=i, name=n, category=c, address=a, muni=m, lat=la, lon=lo, source=s,
                     aliases=al.split("|"))
                for i, n, c, a, m, la, lo, s, al in db.execute(
                    "SELECT id, name, category, address, muni, lat, lon, source, aliases FROM landmarks")]
        finally:
            db.close()
        for st in self.streets.values():
            for muni, lst in st.addrs.items():
                st.nums[muni] = [r[0] for r in lst]
            self._build_geometry(st, geo.get(st.id, []))
        keys = [st.key for st in self.streets.values()]
        self._reserved_phrases = [
            p for p, _m, _n in MUNI_PHRASES if len(p) > 1 and "of" not in p
            and (p in {("port", "chester"), ("rye", "brook")}
                 or not any(k == " ".join(p) or k.startswith(" ".join(p) + " ") for k in keys))]
        self._build_index()

    # ------------------------------------------------------------------ loading

    @staticmethod
    def _build_geometry(st: _Street, lines: list) -> None:
        xs: list[np.ndarray] = []
        ys: list[np.ndarray] = []
        munis: list[str] = []
        for muni, coords in lines:
            arr = np.asarray(coords, dtype=float)
            x, y = _xy(arr[:, 1], arr[:, 0])
            for i in range(len(x) - 1):
                seg = math.hypot(x[i + 1] - x[i], y[i + 1] - y[i])
                n = max(1, int(seg // 20.0))
                t = np.arange(n) / n
                xs.append(x[i] + t * (x[i + 1] - x[i]))
                ys.append(y[i] + t * (y[i + 1] - y[i]))
                munis.extend([muni] * n)
            xs.append(x[-1:])
            ys.append(y[-1:])
            munis.append(muni)
        for muni, lst in st.addrs.items():
            x, y = _xy([r[2] for r in lst], [r[3] for r in lst])
            xs.append(np.atleast_1d(x))
            ys.append(np.atleast_1d(y))
            munis.extend([muni] * len(lst))
        if xs:
            st.xy = np.column_stack([np.concatenate(xs), np.concatenate(ys)])
            st.xy_muni = munis

    def _build_index(self) -> None:
        # alias entries: (spaceless alias, kind, target, typed, penalty)
        self._entries: list[tuple[str, str, int, bool, float]] = []
        for st in self.streets.values():
            for alias, typed, pen in street_aliases(st.key):
                self._entries.append((alias, "street", st.id, typed, pen))
        for i, lm in enumerate(self.landmarks):
            for alias in lm["aliases"]:
                if alias:
                    self._entries.append((alias.replace(" ", ""), "landmark", i, True, 1.0))
        self._exact: dict[str, list[int]] = collections.defaultdict(list)
        self._tri: dict[str, list[int]] = collections.defaultdict(list)
        for idx, (alias, *_rest) in enumerate(self._entries):
            self._exact[alias].append(idx)
            for g in _trigrams(alias):
                self._tri[g].append(idx)

    # ------------------------------------------------------------------ lookup

    def _lookup(self, s: str, kind: str, typed: bool | None, min_sim: float,
                limit: int = 6) -> list[tuple[int, float]]:
        """Fuzzy alias lookup -> [(entry index, similarity)] best first."""
        out: dict[int, float] = {}
        for idx in self._exact.get(s, ()):
            e = self._entries[idx]
            if e[1] == kind and (typed is None or e[3] == typed):
                out[idx] = 1.0
        grams = _trigrams(s)
        counts: collections.Counter = collections.Counter()
        for g in grams:
            for idx in self._tri.get(g, ()):
                counts[idx] += 1
        need = max(1, int(len(grams) * 0.35))
        cands = [idx for idx, c in counts.most_common(60) if c >= need]
        sm = difflib.SequenceMatcher(autojunk=False)
        sm.set_seq2(s)
        for idx in cands:
            if idx in out:
                continue
            e = self._entries[idx]
            if e[1] != kind or (typed is not None and e[3] != typed):
                continue
            sm.set_seq1(e[0])
            if sm.real_quick_ratio() < min_sim or sm.quick_ratio() < min_sim:
                continue
            r = sm.ratio()
            if r >= min_sim:
                out[idx] = r
        return sorted(out.items(), key=lambda kv: -kv[1])[:limit]

    # ------------------------------------------------------------------ parsing

    def _reserved(self, tokens: list[str]) -> set[int]:
        """Token positions of multi-word town names, which are never part of a street."""
        res = set()
        for phrase in self._reserved_phrases:
            n = len(phrase)
            for i in range(len(tokens) - n + 1):
                if tuple(tokens[i:i + n]) == phrase:
                    res.update(range(i, i + n))
        return res

    def _street_mentions(self, tokens: list[str]) -> list[_Mention]:
        reserved = self._reserved(tokens)
        found: list[_Mention] = []
        n = len(tokens)
        for i in range(n):
            t0 = tokens[i]
            # "10 Cross Street" is a street; "cross street Pearl" is a marker
            cross_st = (t0 == "cross" and i + 1 < n and tokens[i + 1] == "st"
                        and i > 0 and tokens[i - 1].isdigit())
            if (t0 in STREET_STOP and not cross_st) or i in reserved or t0.isdigit():
                continue
            for j in range(i + 1, min(n, i + 6) + 1):
                t = tokens[j - 1]
                if (t in STREET_STOP and not (cross_st and j - 1 == i)) or (j - 1) in reserved:
                    break
                span = tokens[i:j]
                s = spaceless(span)
                last = span[-1]
                typed = (last in TYPE_ABBRS or span[0] == "i"
                         or (len(span) > 1 and last in DIRECTIONS and span[-2] in TYPE_ABBRS)
                         or (len(span) > 1 and last in {"north", "south", "east", "west"}
                             and span[-2] in TYPE_ABBRS))
                if (typed and len(span) == 1) or len(s) < (3 if typed else 4):
                    continue
                if not typed and tuple(span) in _MUNI_WORDS:
                    continue          # bare "Glenville" is the place, "Glenville Rd" the street
                min_sim = 0.82 if typed else 0.88
                if len(s) <= 6:
                    min_sim = max(min_sim, 0.9)
                hits = self._lookup(s, "street", typed, min_sim)
                if not hits:
                    continue
                best: dict[int, float] = {}
                for idx, sim in hits:
                    alias, _k, sid, _typed, pen = self._entries[idx]
                    if not typed and len(alias) <= 5 and sim < 1.0:
                        continue          # short bare words must match exactly
                    best[sid] = max(best.get(sid, 0.0), sim * pen)
                if not best:
                    continue
                cands = sorted(best.items(), key=lambda kv: -kv[1])
                top = cands[0][1]
                cands = [c for c in cands if c[1] >= top - 0.08][:5]
                found.append(_Mention(i, j, cands, typed, " ".join(span)))
        # Greedy non-overlapping selection, preferring spans that explain more of the
        # text well: "country ridge dr" beats the bare word "ridge" inside it, and
        # "east broaaway" beats "east".
        found.sort(key=lambda m: (-(m.best ** 4) * len(m.text.replace(" ", ""))
                                  * (1.0 if m.strong else 0.9), m.start))
        taken: set[int] = set()
        chosen = []
        for m in found:
            if any(k in taken for k in range(m.start, m.end)):
                continue
            chosen.append(m)
            taken.update(range(m.start, m.end))
        chosen.sort(key=lambda m: m.start)
        return chosen

    def _landmark_hits(self, tokens: list[str]) -> list[tuple[int, int, int, float]]:
        """-> [(start, end, landmark index, similarity)] (non-overlapping, best first)."""
        found = []
        n = len(tokens)
        for i in range(n):
            if tokens[i] == BOUNDARY:
                continue
            for j in range(i + 1, min(n, i + 8) + 1):
                if tokens[j - 1] == BOUNDARY:
                    break
                s = spaceless(tokens[i:j])
                if len(s) < 6:
                    continue
                for idx, sim in self._lookup(s, "landmark", None, 0.88, limit=4):
                    alias = self._entries[idx][0]
                    if (j - i == 1 or len(alias) < 9) and sim < 0.95:
                        continue
                    found.append((i, j, self._entries[idx][2], sim))
        # best first; overlapping hits are dropped unless they are the same name at
        # another place (a chain store, two "Port Chester Fire Station" points)
        found.sort(key=lambda h: (-h[3], -(h[1] - h[0])))
        out: list[tuple[int, int, int, float]] = []
        for i, j, lm, sim in found:
            clash = [o for o in out if o[0] < j and i < o[1]]
            if not clash:
                out.append((i, j, lm, sim))
            elif any((o[0], o[1]) == (i, j) and o[3] == sim
                     and self.landmarks[o[2]]["name"] == self.landmarks[lm]["name"] for o in clash):
                out.append((i, j, lm, sim))
        return out

    @staticmethod
    def _house_number(tokens: list[str], m: _Mention) -> str | None:
        k = m.start - 1
        skipped = 0
        while k >= 0 and tokens[k] in {BOUNDARY, "number", "no"} and skipped < 2:
            k -= 1
            skipped += 1
        if k < 0:
            return None
        t = tokens[k]
        num = None
        if t.isdigit() and len(t) <= 5:
            num = t
        elif len(t) >= 2 and t[:-1].isdigit() and t[-1] in "abcdefgh" and len(t) <= 6:
            num = t
        if num is None:
            return None
        prev = [x for x in tokens[max(0, k - 2):k] if x != BOUNDARY]
        if prev and (prev[-1] in NOT_HOUSE_NUMBER_AFTER
                     or (len(prev) > 1 and prev[-2] in {"time", "box", "engine", "ladder"})):
            return None
        if int("".join(c for c in num if c.isdigit())) == 0:
            return None
        return num

    @staticmethod
    def _muni_mentions(tokens: list[str], mentions: list[_Mention] = ()) -> list[tuple[int, int, str, bool]]:
        """Town names in the text, except inside a typed street name ("Purchase St")."""
        out = []
        streets = [(m.start, m.end) for m in mentions if m.strong]
        i = 0
        while i < len(tokens):
            for phrase, muni, near_only in MUNI_PHRASES:     # longest phrases first
                if tuple(tokens[i:i + len(phrase)]) == phrase:
                    j = i + len(phrase)
                    if not any(s < j and i < e for s, e in streets):
                        out.append((i, j, muni, near_only))
                    i += len(phrase)
                    break
            else:
                i += 1
        return out

    # ------------------------------------------------------------------ geometry helpers

    def _dist_to_street(self, sid: int, lat: float, lon: float) -> float:
        st = self.streets[sid]
        if st.xy is None or not len(st.xy):
            return math.inf
        x, y = _xy(lat, lon)
        return float(np.min(np.hypot(st.xy[:, 0] - x, st.xy[:, 1] - y)))

    def _closest_approach(self, a: int, b: int) -> tuple[float, float, float, str] | None:
        A, B = self.streets[a].xy, self.streets[b].xy
        if A is None or B is None:
            return None
        bx0, by0 = B.min(axis=0) - 60
        bx1, by1 = B.max(axis=0) + 60
        ia = np.nonzero((A[:, 0] >= bx0) & (A[:, 0] <= bx1) & (A[:, 1] >= by0) & (A[:, 1] <= by1))[0]
        if not len(ia):
            return None
        best = (math.inf, 0, 0)
        for start in range(0, len(ia), 400):
            chunk = A[ia[start:start + 400]]
            d = np.hypot(chunk[:, None, 0] - B[None, :, 0], chunk[:, None, 1] - B[None, :, 1])
            k = int(np.argmin(d))
            ii, jj = divmod(k, d.shape[1])
            if d[ii, jj] < best[0]:
                best = (float(d[ii, jj]), int(ia[start + ii]), jj)
        dist, ia_, jb = best
        if dist > 40:
            return None
        x = (A[ia_, 0] + B[jb, 0]) / 2
        y = (A[ia_, 1] + B[jb, 1]) / 2
        lat, lon = _latlon(float(x), float(y))
        return lat, lon, dist, self.streets[a].xy_muni[ia_]

    def _street_point(self, sid: int, muni: str | None) -> tuple[float, float, str] | None:
        st = self.streets[sid]
        if st.xy is None:
            return None
        idx = np.arange(len(st.xy))
        if muni:
            sel = np.array([m == muni for m in st.xy_muni])
            if sel.any():
                idx = idx[sel]
        pts = st.xy[idx]
        c = pts.mean(axis=0)
        k = int(np.argmin(np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1])))
        lat, lon = _latlon(float(pts[k][0]), float(pts[k][1]))
        return lat, lon, st.xy_muni[idx[k]]

    @staticmethod
    def _locate_number(lst: list[tuple[int, str, float, float]], nums: list[int], num: int,
                       suffix: str) -> tuple[float, float, float, str] | None:
        """-> (lat, lon, quality, precision) for a house number on one street in one municipality."""
        lo = bisect.bisect_left(nums, num)
        hi = bisect.bisect_right(nums, num)
        if hi > lo:
            rows = lst[lo:hi]
            pick = next((r for r in rows if r[1].lower() == suffix), None) or \
                next((r for r in rows if r[1] == ""), rows[0])
            return pick[2], pick[3], 0.95, "exact"
        same = [r for r in lst if r[0] % 2 == num % 2]
        quality_cut = 0.0
        if not (same and same[0][0] < num < same[-1][0]):
            same = lst
            quality_cut = 0.05
        below = [r for r in same if r[0] < num]
        above = [r for r in same if r[0] > num]
        if below and above:
            a, b = below[-1], above[0]
            f = (num - a[0]) / (b[0] - a[0])
            gap = b[0] - a[0]
            q = 0.8 if gap <= 12 else 0.72 if gap <= 40 else 0.62
            return (a[2] + f * (b[2] - a[2]), a[3] + f * (b[3] - a[3]), q - quality_cut,
                    "interpolated")
        if not lst:
            return None
        span = max(1, lst[-1][0] - lst[0][0])
        end = lst[0] if num < lst[0][0] else lst[-1]
        if abs(num - end[0]) <= max(20, 0.25 * span):
            return end[2], end[3], 0.5, "approximate"
        return None

    # ------------------------------------------------------------------ candidates

    def _hint_bonus(self, muni: str, mention_span: tuple[int, int],
                    munis: list[tuple[int, int, str]]) -> float:
        said = 0.0
        for s, e, m, near_only in munis:
            if m != muni:
                continue
            near = (0 <= s - mention_span[1] <= 4) or (0 <= mention_span[0] - e <= 3)
            if near:
                said = SAID_NEAR_BONUS
            elif not near_only:
                said = max(said, SAID_FAR_BONUS)
        return MUNI_PRIOR.get(muni, 0.0) + said

    def _address_cands(self, tokens, m: _Mention, num: str, verify: list[_Mention],
                       munis) -> list[Match]:
        digits = int("".join(c for c in num if c.isdigit()))
        suffix = "".join(c for c in num if c.isalpha())
        out = []
        for sid, sim in m.cands:
            st = self.streets[sid]
            for muni, lst in st.addrs.items():
                res = self._locate_number(lst, st.nums[muni], digits, suffix)
                if res is None:
                    continue
                lat, lon, q, precision = res
                if not m.strong and precision == "approximate":
                    continue
                conf = q * sim
                cross = None
                if verify:
                    best_d, best_name = math.inf, None
                    for vm in verify:
                        for vsid, vsim in vm.cands[:3]:
                            if vsid == sid:
                                continue
                            d = self._dist_to_street(vsid, lat, lon)
                            if d - 50 * (1 - vsim) < best_d:
                                best_d, best_name = d, self.streets[vsid].name
                    cross = best_name
                    if best_d <= 150:
                        conf = conf + 0.05
                    elif best_d > 400:
                        conf *= 0.8
                conf += self._hint_bonus(muni, (m.start, m.end), munis)
                label_num = f"{digits}{suffix.upper()}"
                out.append(Match("address", round(lat, 6), round(lon, 6),
                                 f"{label_num} {st.name}, {muni}", muni, conf,
                                 cross_street=cross, street=st.name, house_number=label_num,
                                 precision=precision,
                                 matched_text=f"{num} {m.text}"))
        return out

    def _intersection_cands(self, a: _Mention, b: _Mention, munis, base: float = 0.92) -> list[Match]:
        out = []
        for sa_id, sa in a.cands[:4]:
            for sb_id, sb in b.cands[:4]:
                if sa_id == sb_id:
                    continue
                key = (min(sa_id, sb_id), max(sa_id, sb_id))
                pts = self._inters.get(key)
                precision = "intersection"
                q = base
                if not pts:
                    ca = self._closest_approach(sa_id, sb_id)
                    if ca is None:
                        continue
                    pts = [(ca[0], ca[1], ca[3])]
                    precision = "near-intersection"
                    q = base - 0.15
                na, nb = self.streets[sa_id].name, self.streets[sb_id].name
                for lat, lon, muni in pts:
                    conf = q * sa * sb + self._hint_bonus(muni, (a.start, b.end), munis)
                    label = f"{na} & {nb}" + (f", {muni}" if muni else "")
                    out.append(Match("intersection", round(lat, 6), round(lon, 6), label, muni, conf,
                                     cross_street=nb, street=na, precision=precision,
                                     matched_text=f"{a.text} / {b.text}"))
        return out

    def _street_cands(self, m: _Mention, munis) -> list[Match]:
        out = []
        for sid, sim in m.cands[:3]:
            st = self.streets[sid]
            options = list(st.addrs) or [None]
            for muni in options:
                p = self._street_point(sid, muni)
                if p is None:
                    continue
                lat, lon, pm = p
                muni = muni or pm
                conf = 0.35 * sim + self._hint_bonus(muni, (m.start, m.end), munis)
                out.append(Match("street", round(lat, 6), round(lon, 6),
                                 f"{st.name}" + (f", {muni}" if muni else ""), muni, conf,
                                 street=st.name, precision="street", matched_text=m.text))
        return out

    def _landmark_cands(self, tokens, hits, mentions: list[_Mention], munis) -> list[Match]:
        out = []
        for i, j, li, sim in hits:
            lm = self.landmarks[li]
            conf = 0.85 * sim + self._hint_bonus(lm["muni"], (i, j), munis)
            for m in mentions:
                if m.strong and any(self._dist_to_street(sid, lm["lat"], lm["lon"]) < 250
                                    for sid, _ in m.cands[:2]):
                    conf += 0.05
                    break
            label = lm["name"] + (f", {lm['muni']}" if lm["muni"] else "")
            out.append(Match("landmark", lm["lat"], lm["lon"], label, lm["muni"], conf,
                             street=lm["address"] or None, precision=lm["category"],
                             matched_text=" ".join(tokens[i:j])))
        return out

    @staticmethod
    def _penalize_ambiguity(cands: list[Match]) -> None:
        """Lower confidence when near-equal readings are far apart (same street name and
        number in two municipalities, a chain store twice, ...). Order is preserved."""
        if len(cands) < 2:
            return
        cands.sort(key=lambda c: -c.confidence)
        best = cands[0]
        rivals = []
        for other in cands[1:]:
            if best.confidence - other.confidence > 0.07:
                break
            dx = (best.lon - other.lon) * M_PER_DEG_LON
            dy = (best.lat - other.lat) * M_PER_DEG_LAT
            if math.hypot(dx, dy) > 300:
                rivals.append(other)
        if rivals:
            for c in [best] + rivals:
                c.confidence *= 0.85

    # ------------------------------------------------------------------ public API

    def candidates(self, text: str) -> list[Match]:
        """All location readings of ``text``, best first."""
        tokens = tokenize(text)
        mentions = self._street_mentions(tokens)
        munis = self._muni_mentions(tokens, mentions)
        groups: list[list[Match]] = []

        roles: dict[int, str] = {}
        partner: dict[int, int] = {}
        crosses: dict[int, list[int]] = collections.defaultdict(list)
        primary_of_cross: int | None = None
        for k in range(1, len(mentions)):
            prev, cur = mentions[k - 1], mentions[k]
            gap = [t for t in tokens[prev.end:cur.start] if t != BOUNDARY]
            if gap and set(gap) & CROSS_MARKERS and set(gap) <= CROSS_GAP:
                primary = primary_of_cross if roles.get(k - 1) == "cross" else k - 1
                roles[k] = "cross"
                crosses[primary].append(k)
                primary_of_cross = primary
            elif roles.get(k - 1) == "cross" and gap and set(gap) <= {"and", "at", "the"}:
                roles[k] = "cross"            # "between A and B"
                crosses[primary_of_cross].append(k)
            elif gap and set(gap) <= INTERSECTION_GAP and set(gap) & INTERSECTION_KEY \
                    and len(gap) <= 4:
                partner[k - 1] = k
            elif not gap:
                partner[k - 1] = k            # "King St Arbor Dr"
        # mention preceded by "cross"/"between" with no primary right before it
        for k, m in enumerate(mentions):
            before = [t for t in tokens[max(0, m.start - 3):m.start] if t != BOUNDARY]
            if k not in roles and set(before) & CROSS_MARKERS and k > 0:
                roles[k] = "cross"
                crosses[k - 1].append(k)

        for k, m in enumerate(mentions):
            verify = [mentions[c] for c in crosses.get(k, [])]
            p = partner.get(k)
            if p is not None:
                verify.append(mentions[p])
            if k in partner.values():
                prev = [q for q, v in partner.items() if v == k]
                verify.extend(mentions[q] for q in prev)
            num = self._house_number(tokens, m)
            if num:
                groups.append(self._address_cands(tokens, m, num, verify, munis))
            if p is not None:
                groups.append(self._intersection_cands(m, mentions[p], munis))
            if crosses.get(k) and not num:
                groups.append(self._intersection_cands(m, mentions[crosses[k][0]], munis, base=0.88))
            if m.strong and roles.get(k) != "cross":
                groups.append(self._street_cands(m, munis))
        groups.append(self._landmark_cands(tokens, self._landmark_hits(tokens), mentions, munis))

        out: list[Match] = []
        for g in groups:
            self._penalize_ambiguity(g)
            out.extend(g)
        # Rank on the raw score. Town bonuses can push several exact addresses past 1.0
        # ("15 Beech St, Byram" vs 15 Beech St in Port Chester); scaling by the top
        # score instead of clamping keeps the runner-up visibly below the winner.
        kind_rank = {"address": 0, "intersection": 1, "landmark": 2, "street": 3}
        out.sort(key=lambda c: (-round(c.confidence, 6), kind_rank[c.kind],
                                TOWN_RANK.get(c.municipality, len(TOWN_PRIORITY))))
        scale = max(1.0, out[0].confidence) if out else 1.0
        for c in out:
            c.confidence = round(max(0.0, min(1.0, c.confidence / scale)), 3)
        return out

    def geocode(self, text: str, min_confidence: float = 0.2) -> Match | None:
        """Geocode a location phrase ("123 Westchester Ave", "King St and Arbor Dr")."""
        cands = self.candidates(text)
        if not cands or cands[0].confidence < min_confidence:
            return None
        return cands[0]

    def find_in_text(self, transcript: str, min_confidence: float = 0.2) -> Match | None:
        """Find the incident location anywhere in a full page transcript.

        Department names, unit numbers, "time out 1432", box numbers etc. are
        tolerated: numbers after such words are never taken as house numbers.
        """
        return self.geocode(transcript, min_confidence)

    def street_names(self) -> list[str]:
        return sorted({st.name for st in self.streets.values()})
