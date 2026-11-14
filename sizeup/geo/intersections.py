"""Intersection search for the CAD's location box (GitHub #213): "N Main St/Westchester Ave", "n main & west".

A dispatcher types two streets with a separator between them and gets every place in our area where a street
matching the first meets a street matching the second, as the county CAD does it:

    N Main St/Westchester Ave      n main & westchester      King St and Westchester Av      n main / west
    S Regent at W William          king st @ westchester (the "@" only when what follows isn't a named place)

Each side matches street names by prefix, ignoring case, abbreviations as in the address data (North -> N,
Avenue / Av -> Ave, Street -> St, Road -> Rd, Route -> Rte, First -> 1st, ...: normalize.tokenize). Nothing
special is typed for "starts with": "west" is West St, Westchester Ave, Westview Ave ... A directional typed has to
match ("N Main" is never S Main St); none typed allows any ("Main" is Main St, N Main St, S Main St, W Main St).
"%", "*" and "_" typed from another CAD's habit are taken as spaces: "N Main%/West%" is "N Main / West".

Where they meet: the gazetteer's TIGER intersections (``intersections``, the same meeting points the page check and
the blocks use), one result per distinct point (a curving street can meet another twice), in the towns of
``towns`` only ([geo] search_towns). Order: our towns first (Port Chester, then Rye Brook), then the others by
distance from PCFD headquarters (209 Westchester Ave); within those, whole names typed before beginnings of names.

The names are matched in a small in-memory SQLite table with an index on the name. The typed text only ever reaches
it as a bound parameter: the LIKE pattern is the text with the LIKE characters escaped, then "%" (ESCAPE "\\"), so
whatever was typed is a literal beginning of a name. No SQL is built from what was typed.
"""

from __future__ import annotations

import collections
import math
import re
import sqlite3
import threading
from dataclasses import dataclass

from .normalize import BOUNDARY, DIRECTION_WORDS, DIRECTIONS, EXPAND, EXTRA_ALIASES, TYPE_ABBRS, tokenize

MAX_LEN = 80                 # longer than this is not an intersection typed in a box
MAX_WORDS = 6                # words per street
MIN_LETTERS = 2              # per street, after "%" / "*" / "_" are dropped
LIMIT = 25                   # results shown; the rest are "more"
SAME_POINT_M = 30            # one pair's meeting points closer than this are one (a divided road's two nodes)
TOO_SHORT = "type at least 2 letters per street"

# The towns searched ([geo] search_towns): PCFD's district, then the towns around it that the gazetteer covers
# (Rye, Harrison incl. Purchase / West Harrison, the Village and Town of Mamaroneck incl. Rye Neck, Larchmont,
# Greenwich CT incl. Byram, Glenville, Pemberwick). Labels as in the gazetteer's ``muni`` column.
SEARCH_TOWNS = ("Port Chester", "Rye Brook", "Rye", "Harrison", "Greenwich", "Mamaroneck", "Larchmont",
                "Town of Mamaroneck")
OUR_TOWNS = ("Port Chester", "Rye Brook")
HQ = (41.00253, -73.66692)   # PCFD Headquarters, 209 Westchester Ave (data/firehouses.json)

# "/", "&", "@", " and ", " at ": the separator, kept (an "@" may be a named place instead)
_SPLIT = re.compile(r"\s*(/|&|@)\s*|\s+(and|at)\s+", re.IGNORECASE)
_STRAY = re.compile(r"[%*_]+")
_LETTERS = re.compile(r"[a-z0-9]", re.IGNORECASE)
_DIRS = set(DIRECTIONS) | set(DIRECTION_WORDS)

M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(41.02))


class TooShort(ValueError):
    """A side with fewer than MIN_LETTERS letters ("%/%", "N Main / W")."""


@dataclass(frozen=True)
class Side:
    typed: str               # as typed (stray "%" / "*" / "_" dropped)
    words: tuple[str, ...]   # normalised: ("north", "main"), ("n", "main")
    dir: str                 # the directional typed first, as the data writes it ("n"), "" for none

    @property
    def phrase(self) -> str:
        return " ".join(self.words)

    @property
    def rest(self) -> str:   # the words after the directional
        return " ".join(self.words[1:] if self.dir else self.words)


@dataclass(frozen=True)
class Query:
    a: Side
    b: Side
    sep: str                 # "/", "&", "@", "and", "at"


def _side(raw: str) -> Side | None:
    typed = " ".join(_STRAY.sub(" ", raw).split())
    words = tuple(t for t in tokenize(typed) if t != BOUNDARY)
    if (words and words[0].isdigit()) or len(words) > MAX_WORDS:
        return None                          # "135 S Regent St ...", "5 @ ...": an address; a sentence: not a street
    if len(_LETTERS.findall(typed)) < MIN_LETTERS or len("".join(words)) < MIN_LETTERS:
        raise TooShort(TOO_SHORT)
    d = ""
    if len(words) > 1 and words[0] in _DIRS and not (len(words) == 2 and words[1] in TYPE_ABBRS):
        d = DIRECTION_WORDS.get(words[0], words[0])       # "North Main" is N Main; "North St" is a name
    return Side(typed, words, d)


def parse(text: str, is_place=None) -> Query | None:
    """Two streets and a separator -> Query; None when the text isn't that (one street, an address, "@ <place>",
    "<address> @ <place>", a side still to be typed, too long). Raises TooShort for a side under two letters.
    is_place(text) -> bool: what follows an "@" names a place (then it is the place, not a street)."""
    text = (text or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    parts = _SPLIT.split(text)
    # re.split with two groups: [left, sep1, sep2, right]; more than one separator is not a pair of streets
    if len(parts) != 4:
        return None
    left, sep = parts[0], (parts[1] or parts[2] or "").lower()
    right = parts[3]
    if not left.strip() or not right.strip():
        return None                          # "@ Sample Mkt", "King St &" (the cross street still to come)
    if sep == "@" and is_place is not None and is_place(right.strip()):
        return None                          # "... @ Sample Market": a named place (#204)
    a = _side(left)
    b = _side(right)
    if a is None or b is None:
        return None
    return Query(a, b, sep)


def like_prefix(s: str) -> str:
    """The LIKE pattern for names that begin with s: s literal (its "\\", "%" and "_" escaped), then "%"."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# The only statements run: constant text, the typed part only ever a bound parameter.
_SQL_ANY = "SELECT sid, variant, base FROM names WHERE variant LIKE ? ESCAPE '\\'"
_SQL_WHOLE = "SELECT sid, variant, base FROM names WHERE variant LIKE ? ESCAPE '\\' AND nodir = 0"
_SQL_DIR = "SELECT sid, variant, base FROM names WHERE variant LIKE ? ESCAPE '\\' AND nodir = 1 AND dir = ?"


def _variants(key: str) -> list[tuple[str, str, int]]:
    """A street key's searchable forms: (form, its directional when the form leaves it off, 1 if it does)."""
    toks = key.split()
    if not toks:
        return []
    forms: list[tuple[list[str], str, int]] = [(toks, "", 0)]
    if len(toks) > 2 and toks[0] in DIRECTIONS:              # "n main st": also "main st", for "N Main" / "Main"
        forms.append((toks[1:], toks[0], 1))
    if len(toks) == 3 and toks[:2] == ["us", "hwy"]:          # "US Hwy 1": "US 1", "US Rte 1", "Rte 1"
        forms += [(["us", toks[2]], "", 0), (["us", "rte", toks[2]], "", 0), (["rte", toks[2]], "", 0)]
    if len(toks) >= 3 and toks[:2] == ["state", "rte"]:      # "State Rte 120A": "Rte 120A", "NY 120A", "NY Rte 120A"
        forms += [(["rte", *toks[2:]], "", 0), (["ny", *toks[2:]], "", 0), (["ny", "rte", *toks[2:]], "", 0)]
    for alias, target in EXTRA_ALIASES.items():               # "post rd", "hutch", "thruway", "cross westchester"
        if target == key:
            forms.append((alias.split(), "", 0))
    out: list[tuple[str, str, int]] = []
    for ts, d, nodir in forms:
        out.append((" ".join(ts), d, nodir))
        if len(ts) > 1 and ts[-1] in EXPAND:                  # the type spelled out, for one still being typed: "aven"
            out.append((" ".join(ts[:-1] + [EXPAND[ts[-1]]]), d, nodir))
    return list(dict.fromkeys(out))


def _base(form: str) -> str:
    """A form without its street type: "westchester ave" -> "westchester" (typing the name alone is whole too)."""
    w = form.split()
    if len(w) > 1 and (w[-1] in TYPE_ABBRS or w[-1] in EXPAND.values()):
        w = w[:-1]
    return " ".join(w)


class IntersectionSearch:
    """Built once per gazetteer (a fraction of a second); ``search`` may be called from any thread."""

    def __init__(self, gaz, towns=SEARCH_TOWNS, ours=OUR_TOWNS, hq: tuple[float, float] = HQ):
        self.gaz = gaz
        self.towns = tuple(towns or SEARCH_TOWNS)
        self.ours = tuple(t for t in (ours or ()) if t in self.towns)
        self.hq = hq
        self.names = {sid: st.name for sid, st in gaz.streets.items()}
        # street id -> {the street it meets: [(lat, lon, town)]}, our area only
        self.adj: dict[int, dict[int, list[tuple[float, float, str]]]] = collections.defaultdict(dict)
        allowed = set(self.towns)
        for (a, b), pts in gaz._inters.items():
            pts = [p for p in pts if p[2] in allowed]
            if pts and a != b:
                self.adj[a][b] = pts
                self.adj[b][a] = pts
        self._lock = threading.Lock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE TABLE names (variant TEXT NOT NULL COLLATE NOCASE, base TEXT NOT NULL, "
                        "sid INTEGER NOT NULL, dir TEXT NOT NULL, nodir INTEGER NOT NULL)")
        self.db.executemany("INSERT INTO names VALUES (?, ?, ?, ?, ?)",
                            [(v, _base(v), sid, d, nodir) for sid, st in gaz.streets.items() if sid in self.adj
                             for v, d, nodir in _variants(st.key)])
        self.db.execute("CREATE INDEX names_variant ON names (variant COLLATE NOCASE)")
        self.db.commit()

    # -------------------------------------------------------------------------- matching one side

    def streets(self, side: Side) -> dict[int, bool]:
        """The streets one side matches -> {street id: the whole name was typed (else its beginning)}."""
        if side.dir:
            # "N Main": N Main St (the directional matched, then the rest by prefix), or a name that begins with
            # those very words ("W St ..."); never S Main St or Main St
            runs = [(_SQL_DIR, (like_prefix(side.rest), side.dir), side.rest),
                    (_SQL_WHOLE, (like_prefix(side.phrase),), side.phrase)]
        else:
            runs = [(_SQL_ANY, (like_prefix(side.phrase),), side.phrase)]
        out: dict[int, bool] = {}
        with self._lock:
            for sql, params, typed in runs:
                for sid, variant, base in self.db.execute(sql, params):
                    out[sid] = out.get(sid, False) or typed in (variant, base)
        return out

    # -------------------------------------------------------------------------- the search

    def search(self, text: str, limit: int = LIMIT, is_place=None) -> dict | None:
        """-> {"results": [...], "total": n, "more": n - shown} for two streets typed; {"error": TOO_SHORT,
        "results": []} for a side under two letters; None when the text isn't two streets (the box's usual
        suggestions then). Each result: kind "intersection", label "N Main St & Westchester Ave, Port Chester",
        name "N Main St & Westchester Ave", streets [both], street / cross_street, municipality and town, lat,
        lon, precision, confidence (1.0 both names whole, 0.9 one, 0.8 beginnings), exact (0..2), km (from HQ)."""
        try:
            q = parse(text, is_place)
        except TooShort as e:
            return {"error": str(e), "results": [], "total": 0, "more": 0}
        if q is None:
            return None
        sa, sb = self.streets(q.a), self.streets(q.b)
        cands = []
        for ia, ea in sa.items():
            for ib, pts in self.adj.get(ia, {}).items():
                if ib in sb:
                    cands += [self._result(ia, ib, lat, lon, town, int(ea) + int(sb[ib])) for lat, lon, town in pts]
        # one per distinct point of a pair, the best-ranked reading of it: a corner on a town line is ours,
        # "west / west" finds West St & Westchester Ave both ways round (the whole name typed wins)
        rows: list[dict] = []
        kept: dict[frozenset, list[dict]] = collections.defaultdict(list)
        for r in sorted(cands, key=self._rank):
            there = kept[r["pair"]]
            if not any(_m(r["lat"], r["lon"], o["lat"], o["lon"]) < SAME_POINT_M for o in there):
                there.append(r)
                rows.append(r)
        for r in rows:
            del r["pair"]
        shown = rows[:max(1, limit)]
        return {"results": shown, "total": len(rows), "more": len(rows) - len(shown)}

    def _result(self, ia: int, ib: int, lat: float, lon: float, town: str, exact: int) -> dict:
        na, nb = self.names[ia], self.names[ib]
        name = f"{na} & {nb}"
        return {"pair": frozenset((ia, ib)), "kind": "intersection", "label": f"{name}, {town}", "name": name,
                "streets": [na, nb],
                "street": na, "cross_street": nb, "municipality": town, "town": town,
                "lat": round(float(lat), 6), "lon": round(float(lon), 6), "precision": "intersection",
                "confidence": (0.8, 0.9, 1.0)[exact], "exact": exact,
                "km": round(_m(lat, lon, *self.hq) / 1000.0, 2)}

    def _rank(self, r: dict):
        group = self.ours.index(r["town"]) if r["town"] in self.ours else len(self.ours)
        return (group, -r["exact"], r["km"], r["label"])


def _m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return math.hypot((lat1 - lat2) * M_PER_DEG_LAT, (lon1 - lon2) * M_PER_DEG_LON)
