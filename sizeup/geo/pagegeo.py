"""Where a 60 Control page sends us (#176): every reading of the location in the page, checked against the
cross streets and the department's own towns, and an honest "unconfirmed" when nothing checks out.

2026-10-15 10:29, Port Chester Fire tones: "135 South Remon Street, Corpus Christi School. Between Clinton Street and
Williams Street, inside order gas ... 135 135 South-Regent-Street, the Corpus Christi School. Between Clinton Street,
William-Street". Only the first "number + street" was read ("135 south remon street"); the geocoder split it into the
bare word "south" (South St) and "remon st" (Tremont St), and since South St with a 135 exists only in Greenwich, CT,
it put 135 South St, Greenwich (0.47) on the map as if it were known. The read-back never counted ("135 135" ran
together into 135135), the cross streets only nudged the score, and nothing said Greenwich is not a Port Chester page.

How a page is read here (``PageLocator.locate``):

1. Every address said: each house number with the words after it up to the street type, the read-back included
   (the repeated number folded, "South-Regent-Street" split at the hyphens).
2. The streets each could be: the gazetteer's fuzzy readings in any town; sound-alikes (edit distance over the
   street's spoken forms, the vocabulary of streets the STT is primed with) among the streets of the page's own
   towns only; and the streets that run between the two cross streets heard.
3. The cross streets ("between A and B", "cross of A"): a reading is checked where they meet its street (the TIGER
   street centrelines and their intersections in data/gazetteer.sqlite): both meet it and the house number lies
   between them = "all"; one does (the other misheard) = "partial"; cross streets said but none meets it = "none".
   A street both cross streets meet whose number was not heard (or not found) is placed on the block between them.
4. Named places said ("Corpus Christi School": the gazetteer's landmarks and named places) are readings of their
   own, checked the same way, and back an address within PLACE_NEAR_M.
5. Jurisdiction: the towns of the page's tones (Port Chester, Rye Brook: DISTRICT) and the towns the page names
   (mutual aid: "... to the Town of Greenwich") are in; a reading anywhere else is halved and never confirmed.

score = 0.45 x name similarity x number quality + cross streets (all 0.35, partial 0.25, none 0, none said
0.25 x name) + town (named right at the location 0.18; the tones' town 0.12 unless another town is named there;
the district or a town named elsewhere 0.08) + 0.04 per extra reading of the same address (up to 0.08) + 0.06
with a named place said nearby.

Confirmed only when the best reading is in an allowed town, scores CONFIRM or more and is checked (all its cross
streets meet it; or one does and the name and number were heard outright; or no cross street was said and the
name and number match outright), and no reading elsewhere comes within RIVAL of it. Anything else is
"unconfirmed": the card offers the readings to pick from and the map is not set.
"""

from __future__ import annotations

import collections
import difflib
import heapq
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from .gazetteer import (CROSS_MARKERS, MUNI_PHRASES, MUNI_PRIOR, NOT_HOUSE_NUMBER_AFTER, STREET_STOP, Gazetteer,
                        Match, _latlon, _xy)
from .normalize import BOUNDARY, TYPE_ABBRS, spaceless, tokenize

DISTRICT = ("Port Chester", "Rye Brook")    # PCFD's district: always in for a page with our tones
CONFIRM = 0.75                # the lowest score a confirmed location has
RIVAL = 0.08                  # another place this close to the best score: two readings fit, unconfirmed
RIVAL_M = 200                 # ... when it is this far from the best one
SOUND_MIN = 0.6               # a sound-alike street (in our towns, a typed street said) at least this similar
BETWEEN_MIN = 0.5             # a street between the cross streets: at least this similar to what was said
NEAR_CROSS_M = 300            # a house number this close to where a cross street meets its street
BETWEEN_PERP_M = 120          # ... or between the two meeting points, at most this far off the line joining them
BLOCK_MAX_M = 1500            # two cross streets meeting a street farther apart than this aren't a block
PLACE_NEAR_M = 200            # a named place backs an address this close
PLACE_CROSS_M = 150           # a named place this close to a cross street's centreline: it meets it
NAME_SURE = 0.85              # a name heard this well needs only one cross street (the other misheard)
NAME_EXACT = 0.95             # ... and with no cross street said, this well
W_NAME, W_ALL, W_PARTIAL = 0.45, 0.35, 0.25
TOWN_NEAR, TOWN_HOME, TOWN_IN = 0.18, 0.12, 0.08
READ_BONUS, PLACE_BONUS = 0.04, 0.06

# Words that don't name a place on their own ("Corpus Christi School": "corpus christi" does).
GENERIC = {
    "the", "and", "of", "at", "a", "an", "school", "schools", "church", "catholic", "chapel", "temple", "synagogue",
    "center", "building", "park", "hall", "house", "home", "high", "middle", "elementary", "public", "library",
    "station", "fire", "department", "police", "village", "town", "city", "hospital", "apartments", "apts", "inc",
    "llc", "co", "company", "club", "plaza", "mall", "store", "shop", "restaurant", "bank", "office", "field",
    "complex", "parish", "academy", "st", "saint", "new", "old", "north", "south", "east", "west", "n", "s", "e", "w",
    "our", "lady", "community", "united", "first", "lot", "parking", "garage", "market", "deli", "pizza", "cafe",
} | set(TYPE_ABBRS)
TOWN_WORDS = {w for p, _m, _n in MUNI_PHRASES for w in p} | {"port", "chester", "brook", "rye", "westchester"}
DEPARTMENT_WORDS = {"fire", "fd", "department", "units", "unit", "engine", "ems", "ambulance", "police", "pd", "volunteer"}
NOT_A_STREET = {"control", "page", "units", "engine", "ladder", "tower", "rescue", "battalion"}


@dataclass
class Reading:
    """One place the page could mean, and why."""
    match: Match
    score: float
    name_sim: float
    checked: str                 # "all" | "partial" | "none" | "" (no cross street said)
    crosses: list[str]           # the cross streets that meet it, named as in the data
    allowed: bool                # in the page's towns
    how: str                     # heard | sound-alike | between the cross streets | named place | gazetteer
    reads: set = field(default_factory=set)      # the address readings (token positions) behind it
    place: str = ""              # a named place said nearby

    def why(self) -> str:
        parts = []
        if self.checked == "all":
            parts.append("between " + " and ".join(self.crosses) if len(self.crosses) > 1
                         else f"at {self.crosses[0]}")
        elif self.checked == "partial":
            parts.append(f"meets {self.crosses[0]} only")
        elif self.checked == "none":
            parts.append("no cross street meets it")
        if self.how == "sound-alike":
            parts.append("sounds like what was said")
        elif self.how == "between the cross streets":
            parts.append("house number not placed")
        if self.place:
            parts.append(f"near {self.place}")
        if not self.allowed:
            parts.append(f"{self.match.municipality}: not this page's town")
        return "; ".join(parts)

    def as_alt(self) -> dict:
        m = self.match
        return {"label": m.label, "lat": m.lat, "lon": m.lon, "kind": m.kind, "town": m.municipality,
                "confidence": round(self.score, 2), "why": self.why()}


@dataclass
class PageGeo:
    best: Reading | None
    confirmed: bool
    alternatives: list[Reading]
    crosses_heard: list[str]
    why: str = ""                # why it is not confirmed

    def as_geo(self) -> dict | None:
        """The page's location as the monitor incident carries it (sizeup/incidents.py ``geo``): the best reading,
        confirmed or not, with the readings to pick from when it isn't."""
        b = self.best
        if b is None:
            return None
        m = b.match
        others = self.alternatives if self.confirmed else [b] + self.alternatives
        return {"lat": m.lat, "lon": m.lon, "label": m.label, "kind": m.kind,
                "town": m.municipality if self.confirmed else "", "confidence": round(b.score, 2),
                "confirmed": self.confirmed, "checked": b.checked, "crosses": list(b.crosses),
                "crosses_heard": list(self.crosses_heard), "why": self.why or b.why(),
                "alts": [r.as_alt() for r in others[:4]]}


def home_towns(departments) -> tuple[str, ...]:
    """The towns a page's tones name: 'Port Chester · Fire' -> Port Chester, 'Rye Brook FD (est.)' -> Rye Brook."""
    out = []
    for d in departments or ():
        low = " " + re.sub(r"[^a-z]+", " ", str(d).lower()) + " "
        for phrase, muni, _near in MUNI_PHRASES:            # longest first
            if f" {' '.join(phrase)} " in low:
                if muni not in out:
                    out.append(muni)
                break
    return tuple(out)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _word_match(heard: str, name: str) -> bool:
    if heard == name:
        return True
    if min(len(heard), len(name)) >= 5 and (name.startswith(heard) or heard.startswith(name)):
        return True                                   # "christi" / "christian"
    return len(heard) >= 5 and abs(len(heard) - len(name)) <= 2 and _ratio(heard, name) >= 0.85


class PageLocator:
    """Built once per gazetteer (``locator(gaz)``); ``locate`` is pure."""

    def __init__(self, gaz: Gazetteer):
        self.gaz = gaz
        self.aliases: dict[int, list[tuple[str, bool, float]]] = collections.defaultdict(list)
        for alias, kind, target, typed, pen in gaz._entries:
            if kind == "street":
                self.aliases[target].append((alias, typed, pen))
        self.adj: dict[int, set[int]] = collections.defaultdict(set)
        for a, b in gaz._inters:
            self.adj[a].add(b)
            self.adj[b].add(a)
        self.towns_of = {sid: set(st.addrs) | set(st.xy_muni) for sid, st in gaz.streets.items()}
        self._meet_cache: dict[tuple[int, int], list[tuple[float, float]]] = {}
        self.places = self._load_places()
        self.place_index: dict[str, list[int]] = collections.defaultdict(list)
        for i, p in enumerate(self.places):
            for w in p["key"]:
                self.place_index[w].append(i)

    # ------------------------------------------------------------------ data

    def _load_places(self) -> list[dict]:
        rows = [(lm["name"], lm["muni"], lm["lat"], lm["lon"], lm["category"]) for lm in self.gaz.landmarks]
        try:
            db = sqlite3.connect(f"file:{self.gaz.path}?mode=ro", uri=True)
            try:
                rows += db.execute("SELECT name, muni, lat, lon, category FROM places WHERE kind = 'place'").fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            pass                                      # an older gazetteer without the places table: landmarks only
        out, seen = [], set()
        for name, muni, lat, lon, cat in rows:
            words = [w for w in tokenize(name) if w != BOUNDARY]
            key = [w for w in words if w not in GENERIC and w not in TOWN_WORDS and not w.isdigit() and len(w) >= 3]
            sig = (name.lower(), round(lat, 4), round(lon, 4))
            if not key or sig in seen:
                continue
            seen.add(sig)
            out.append({"name": name, "muni": muni, "lat": lat, "lon": lon, "category": cat, "words": words, "key": key})
        return out

    def _name_sim(self, heard: str, sid: int, typed: bool) -> float:
        best = 0.0
        sm = difflib.SequenceMatcher(autojunk=False)
        sm.set_seq2(heard)
        for alias, atyped, pen in self.aliases.get(sid, ()):
            if typed and not atyped:
                continue
            sm.set_seq1(alias)
            if sm.real_quick_ratio() * pen <= best:
                continue
            best = max(best, sm.ratio() * pen)
        return best

    def _meets(self, sid: int, csid: int) -> list[tuple[float, float]]:
        """Where street csid meets street sid, in metres (TIGER intersections, else a closest approach <= 40 m)."""
        key = (min(sid, csid), max(sid, csid))
        if key not in self._meet_cache:
            pts = [(lat, lon) for lat, lon, _m in self.gaz._inters.get(key, ())]
            if not pts:
                ca = self.gaz._closest_approach(sid, csid)
                if ca is not None:
                    pts = [(ca[0], ca[1])]
            self._meet_cache[key] = [tuple(float(v) for v in _xy(lat, lon)) for lat, lon in pts]
        return self._meet_cache[key]

    # ------------------------------------------------------------------ reading the text

    @staticmethod
    def _house_number(tokens: list[str], k: int) -> str | None:
        t = tokens[k]
        if not ((t.isdigit() and len(t) <= 5) or (2 <= len(t) <= 6 and t[:-1].isdigit() and t[-1] in "abcdefgh")):
            return None
        # "time out 1432", "engine 4": not house numbers; after a full stop the word before is another clause's
        # ("... activated fire alarm. 209 West Chester Avenue")
        prev = [] if k and tokens[k - 1] == BOUNDARY else [x for x in tokens[max(0, k - 2):k] if x != BOUNDARY]
        if prev and (prev[-1] in NOT_HOUSE_NUMBER_AFTER
                     or (len(prev) > 1 and prev[-2] in {"time", "box", "engine", "ladder"})):
            return None
        if int("".join(c for c in t if c.isdigit())) == 0:
            return None
        return t

    def _addresses(self, tokens: list[str]) -> list[tuple[int, int, str, list[str], bool]]:
        """Every "number + street" said: (start, end, house number, street words, typed)."""
        out = []
        for k in range(len(tokens)):
            num = self._house_number(tokens, k)
            if num is None:
                continue
            words: list[str] = []
            j = k + 1
            while j < len(tokens) and len(words) < 5:
                w = tokens[j]
                if w == BOUNDARY or w.isdigit() or w in STREET_STOP or w in NOT_A_STREET:
                    break
                words.append(w)
                j += 1
                if w in TYPE_ABBRS and len(words) > 1:
                    break
            typed = len(words) > 1 and words[-1] in TYPE_ABBRS
            if not typed:
                words = words[:3]
            if not words or len(spaceless(words)) < 4:
                continue
            out.append((k, k + 1 + len(words), num, words, typed))
        return out

    def _crosses(self, tokens: list[str], mentions) -> list[list[tuple[str, list[tuple[int, float]]]]]:
        """The cross streets said, one group per "between A and B" / "cross of A": [(text, [(street, sim)])]."""
        groups = []
        for i, t in enumerate(tokens):
            if t not in CROSS_MARKERS:
                continue
            group, pos, bounds = [], i + 1, 0
            for m in mentions:
                if m.start < pos:
                    continue
                gap = [x for x in tokens[pos:m.start]]
                bounds += gap.count(BOUNDARY)
                if len([x for x in gap if x != BOUNDARY]) > 3 or bounds > (1 if group else 0) or \
                        any(x not in {"of", "st", "is", "are", "and", "at", "the", "streets", BOUNDARY} for x in gap):
                    break
                group.append((m.text, self._cross_cands(m)))
                pos = m.end
                if len(group) == 2:
                    break
            if group:
                groups.append(group)
        return groups

    def _cross_cands(self, m) -> list[tuple[int, float]]:
        s = m.text.replace(" ", "")
        best: dict[int, float] = dict(m.cands)
        for idx, sim in self.gaz._lookup(s, "street", None, 0.8, limit=12):
            _a, _k, sid, _t, pen = self.gaz._entries[idx]
            best[sid] = max(best.get(sid, 0.0), sim * pen)
        return sorted(((sid, v) for sid, v in best.items() if v >= 0.8), key=lambda kv: -kv[1])[:8]

    def _place_hits(self, tokens: list[str], skip: set[int]) -> list[tuple[int, int, dict, float]]:
        """Named places said: a run of words that all belong to a place's name and cover most of what names it."""
        found = []
        n = len(tokens)
        for i in range(n):
            if i in skip or tokens[i] == BOUNDARY or tokens[i] in GENERIC:
                continue
            first = tokens[i]
            cands = set(self.place_index.get(first, ()))
            if len(first) >= 5:
                cands |= {pi for w, lst in self.place_index.items()
                          if w != first and _word_match(first, w) for pi in lst}
            for pi in cands:
                p = self.places[pi]
                j = i
                while j < n and j - i < 8 and j not in skip and tokens[j] != BOUNDARY and \
                        any(_word_match(tokens[j], w) for w in p["words"]):
                    j += 1
                said = [w for w in tokens[i:j] if w not in GENERIC]
                if j - i < 2 or not said:
                    continue
                cov = sum(1 for w in p["key"] if any(_word_match(h, w) for h in said)) / len(p["key"])
                if cov >= 0.6:
                    found.append((i, j, p, round(cov, 3)))
        return found

    # ------------------------------------------------------------------ checking against the cross streets

    def _check_point(self, sid: int, P: tuple[float, float], groups) -> tuple[str, list[str]]:
        """A point on street sid against the cross streets heard: the best group's verdict."""
        if not groups:
            return "", []
        best = ("none", [])
        rank = {"none": 0, "partial": 1, "all": 2}
        px, py = P
        for g in groups:
            near: list[tuple[float, tuple[float, float], str] | None] = []
            for _text, cands in g:
                hit = None
                for csid, _sim in cands:
                    if csid == sid:
                        continue
                    for x, y in self._meets(sid, csid):
                        d = math.hypot(x - px, y - py)
                        if hit is None or d < hit[0]:
                            hit = (d, (x, y), self.gaz.streets[csid].name)
                near.append(hit)
            got = [h for h in near if h is not None]
            verdict, names = "none", []
            if len(g) == 2 and len(got) == 2:
                (d1, (x1, y1), n1), (d2, (x2, y2), n2) = got
                vx, vy = x2 - x1, y2 - y1
                L = math.hypot(vx, vy)
                if L < 5:
                    between = min(d1, d2) <= NEAR_CROSS_M
                else:
                    t = ((px - x1) * vx + (py - y1) * vy) / (L * L)
                    perp = abs(vx * (py - y1) - vy * (px - x1)) / L
                    between = L <= BLOCK_MAX_M and -0.15 <= t <= 1.15 and perp <= BETWEEN_PERP_M
                if between:
                    verdict, names = "all", [n1, n2]
                elif min(d1, d2) <= NEAR_CROSS_M:
                    verdict, names = "partial", [n1 if d1 <= d2 else n2]
            elif got:
                d, _xy_, nm = min(got, key=lambda h: h[0])
                if d <= NEAR_CROSS_M:
                    verdict, names = ("all" if len(g) == 1 else "partial"), [nm]
            if rank[verdict] > rank[best[0]]:
                best = (verdict, names)
        return best

    def _block(self, sid: int, groups) -> tuple[float, float, str, list[str]] | None:
        """Street sid between two cross streets that both meet it: the point midway along it (lat, lon, town)."""
        st = self.gaz.streets[sid]
        if st.xy is None:
            return None
        best = None
        for g in groups:
            if len(g) != 2:
                continue
            ends = []
            for _text, cands in g:
                pts = [(x, y, self.gaz.streets[c].name) for c, _s in cands if c != sid for x, y in self._meets(sid, c)]
                ends.append(pts)
            for x1, y1, n1 in ends[0]:
                for x2, y2, n2 in ends[1]:
                    L = math.hypot(x2 - x1, y2 - y1)
                    if 5 <= L <= BLOCK_MAX_M and (best is None or L < best[0]):
                        best = (L, (x1 + x2) / 2, (y1 + y2) / 2, [n1, n2])
        if best is None:
            return None
        _L, mx, my, names = best
        k = int(np.argmin(np.hypot(st.xy[:, 0] - mx, st.xy[:, 1] - my)))
        lat, lon = _latlon(float(st.xy[k, 0]), float(st.xy[k, 1]))
        return lat, lon, st.xy_muni[k], names

    def _place_check(self, lat: float, lon: float, groups) -> tuple[str, list[str]]:
        if not groups:
            return "", []
        best = ("none", [])
        for g in groups:
            names = []
            for _text, cands in g:
                for csid, sim in cands:
                    if sim >= 0.85 and self.gaz._dist_to_street(csid, lat, lon) <= PLACE_CROSS_M:
                        names.append(self.gaz.streets[csid].name)
                        break
            if names and len(names) == len(g):
                return "all", names
            if names and best[0] == "none":
                best = ("partial", names)
        return best

    # ------------------------------------------------------------------ the page

    def locate(self, text: str, home=()) -> PageGeo:
        """Every reading of the page's location, best first, and whether the best one is confirmed. home: the towns
        of the page's tones (``home_towns``); none = no jurisdiction to hold it to (the gazetteer's town order)."""
        gaz = self.gaz
        tokens = tokenize(text or "")
        mentions = gaz._street_mentions(tokens)
        # towns said, but not as a department ("Port Chester Fire Department, ..." is who, not where)
        named = [x for x in gaz._muni_mentions(tokens, mentions)
                 if not (x[1] < len(tokens) and tokens[x[1]] in DEPARTMENT_WORDS)]
        addrs = self._addresses(tokens)
        home = tuple(home or ())
        if home:
            allowed = set(home) | set(DISTRICT)
            allowed |= {m for _s, _e, m, near_only in named if not near_only}
            for s, e, m, near_only in named:
                if near_only and any(0 <= s - a_end <= 4 or 0 <= a_start - e <= 3 for a_start, a_end, *_r in addrs):
                    allowed.add(m)
        else:
            allowed = None
        groups = self._crosses(tokens, mentions)
        crosses_heard = list(dict.fromkeys(t for g in groups for t, _c in g))

        def town(muni: str, span: tuple[int, int]) -> float | None:
            if allowed is None:
                return MUNI_PRIOR.get(muni, 0.0)
            said = {m for s, e, m, _n in named if 0 <= s - span[1] <= 4 or 0 <= span[0] - e <= 3}
            if muni in said:
                return TOWN_NEAR
            if muni in home and not said:          # another town named right at the location: mutual aid there
                return TOWN_HOME
            if muni in allowed:
                return TOWN_IN
            return None

        def cross_part(checked: str, name_sim: float) -> float:
            return {"all": W_ALL, "partial": W_PARTIAL, "none": 0.0, "": W_PARTIAL * name_sim}[checked]

        readings: dict[str, Reading] = {}

        def add(r: Reading) -> None:
            old = readings.get(r.match.label)
            if old is None:
                readings[r.match.label] = r
                return
            old.reads |= r.reads
            if r.score > old.score:
                r.reads |= old.reads
                readings[r.match.label] = r

        pool = None
        if allowed is not None:
            pool = [sid for sid, towns in self.towns_of.items() if towns & allowed]
        cross_streets: set[int] = set()
        for g in groups:
            if len(g) == 2:
                a = {c for c, _s in g[0][1]}
                b = {c for c, _s in g[1][1]}
                meet_a = set().union(*(self.adj.get(c, set()) for c in a)) if a else set()
                meet_b = set().union(*(self.adj.get(c, set()) for c in b)) if b else set()
                cross_streets |= meet_a & meet_b

        skip: set[int] = set()
        for start, end, num, words, typed in addrs:
            skip.update(range(start, end))
            heard = spaceless(words)
            cands: dict[int, tuple[float, str]] = {}

            def offer(sid: int, sim: float, how: str) -> None:
                if sid not in cands or sim > cands[sid][0]:
                    cands[sid] = (sim, how)

            for idx, sim in gaz._lookup(heard, "street", None, 0.8 if typed else 0.88, limit=10):
                alias, _k, sid, _t, pen = gaz._entries[idx]
                if not typed and len(alias) <= 5 and sim < 1.0:
                    continue
                s = self._name_sim(heard, sid, typed)
                offer(sid, s, "heard" if s >= NAME_SURE else "sound-alike")
            for m in mentions:                    # the gazetteer's own split of the words ("south" + "remon st")
                if start < m.start < end:
                    for sid, _sim in m.cands:
                        s = self._name_sim(heard, sid, typed)
                        offer(sid, s, "heard" if s >= NAME_SURE else "sound-alike")
            if typed and pool is not None:
                for sid in pool:
                    if sid not in cands:
                        s = self._name_sim(heard, sid, True)
                        if s >= SOUND_MIN:
                            offer(sid, s, "sound-alike")
            for sid in cross_streets:
                if sid not in cands:
                    s = self._name_sim(heard, sid, typed)
                    if s >= BETWEEN_MIN:
                        offer(sid, s, "between the cross streets")
            digits = int("".join(c for c in num if c.isdigit()))
            suffix = "".join(c for c in num if c.isalpha())
            label_num = f"{digits}{suffix.upper()}"
            for sid, (sim, how) in cands.items():
                st = gaz.streets[sid]
                placed_all = False
                for muni, lst in st.addrs.items():
                    res = gaz._locate_number(lst, st.nums[muni], digits, suffix)
                    if res is None:
                        continue
                    lat, lon, q, precision = res
                    if not typed and precision == "approximate":
                        continue
                    checked, names = self._check_point(sid, tuple(float(v) for v in _xy(lat, lon)), groups)
                    placed_all |= checked == "all"
                    tw = town(muni, (start, end))
                    score = W_NAME * sim * q + cross_part(checked, sim) + (tw or 0.0)
                    if tw is None:
                        score *= 0.5
                    add(Reading(Match("address", round(lat, 6), round(lon, 6), f"{label_num} {st.name}, {muni}", muni,
                                      0.0, cross_street=names[0] if names else None, street=st.name,
                                      house_number=label_num, precision=precision, matched_text=f"{num} {' '.join(words)}"),
                                score, sim, checked, names, tw is not None, how, {start}))
                if not placed_all and len(groups) and sim >= BETWEEN_MIN:
                    blk = self._block(sid, groups)
                    if blk is not None:
                        lat, lon, muni, names = blk
                        tw = town(muni, (start, end))
                        score = W_NAME * sim * 0.7 + W_ALL + (tw or 0.0)
                        if tw is None:
                            score *= 0.5
                        add(Reading(Match("street", round(lat, 6), round(lon, 6),
                                          f"{st.name} between {names[0]} and {names[1]}, {muni}", muni, 0.0,
                                          cross_street=names[0], street=st.name, precision="between cross streets",
                                          matched_text=f"{num} {' '.join(words)}"),
                                    score, sim, "all", names, tw is not None, "between the cross streets", {start}))

        cross_tokens = set()                       # the cross streets themselves are no place names
        for i, t in enumerate(tokens):
            if t in CROSS_MARKERS:
                cross_tokens.update(range(i, min(len(tokens), i + 8)))
        street_tokens = {k for m in mentions if m.strong for k in range(m.start, m.end)}
        places = [h for h in self._place_hits(tokens, skip | cross_tokens)
                  if any(k not in street_tokens for k in range(h[0], h[1]))]     # "King Street" is not King Street School
        for i, j, p, cov in places:
            checked, names = self._place_check(p["lat"], p["lon"], groups)
            tw = town(p["muni"], (i, j))
            score = W_NAME * cov * 0.85 + cross_part(checked, cov) + (tw or 0.0)
            if tw is None:
                score *= 0.5
            add(Reading(Match("landmark", p["lat"], p["lon"], p["name"] + (f", {p['muni']}" if p["muni"] else ""),
                              p["muni"], 0.0, precision=p["category"], matched_text=" ".join(tokens[i:j])),
                        score, cov, checked, names, tw is not None, "named place", set()))

        if not addrs:                              # no address said: intersections, landmarks, streets
            for c in gaz.candidates(text or "")[:8]:
                tw = town(c.municipality, (0, 0))
                score = 0.85 * c.confidence + (tw or 0.0)
                if tw is None:
                    score *= 0.5
                checked = "all" if c.kind == "intersection" and c.precision == "intersection" else ""
                add(Reading(c, score, c.confidence, checked, [c.cross_street] if checked and c.cross_street else [],
                            tw is not None, "gazetteer"))

        out = sorted(readings.values(), key=lambda r: -r.score)
        for r in out:
            n = len(r.reads)
            if n > 1:
                r.score += READ_BONUS * min(2, n - 1)
            if r.match.kind != "landmark":
                near = [p for _i, _j, p, _c in places
                        if _dist(r.match.lat, r.match.lon, p["lat"], p["lon"]) <= PLACE_NEAR_M]
                if near:
                    r.score += PLACE_BONUS
                    r.place = near[0]["name"]
            r.score = round(max(0.0, min(0.99, r.score)), 3)
            r.match.confidence = r.score
        out.sort(key=lambda r: (-r.score, {"address": 0, "intersection": 1, "street": 2, "landmark": 3}.get(r.match.kind, 4)))
        if not out:
            return PageGeo(None, False, [], crosses_heard, "no location heard")
        best, rest = out[0], out[1:]
        alts = []
        for r in rest:
            if all(_dist(r.match.lat, r.match.lon, a.match.lat, a.match.lon) > 30 for a in [best] + alts):
                alts.append(r)
            if len(alts) == 4:
                break
        ok, why = self._confirm(best, rest, groups)
        return PageGeo(best, ok, alts, crosses_heard, why)

    @staticmethod
    def _confirm(b: Reading, rest: list[Reading], groups) -> tuple[bool, str]:
        exact = b.match.precision in ("exact", "interpolated")
        if not b.allowed:
            return False, f"{b.match.municipality} is not this page's town"
        if b.how == "gazetteer":
            checked = b.match.kind in ("intersection", "landmark") and b.name_sim >= 0.8
        else:
            checked = (b.checked == "all"
                       or (b.checked == "partial" and b.name_sim >= NAME_SURE and exact)
                       or (b.checked == "" and b.name_sim >= NAME_EXACT and (exact or b.match.kind == "landmark")))
        if not checked:
            unclear = b.name_sim < (NAME_SURE if b.checked == "partial" else NAME_EXACT)
            if b.checked == "none":
                return False, "no cross street said meets it"
            if b.checked == "partial":
                return False, "only one cross street meets it, and " + (
                    "the street name was not clear" if unclear else "the house number is not on that street")
            if unclear:
                return False, "the street name was not clear and no cross street was said"
            return False, "the house number is not on that street and no cross street was said"
        if b.score < CONFIRM:
            return False, f"low confidence ({b.score:.2f})"
        for r in rest:
            if r.score < b.score - RIVAL:
                break
            if _dist(r.match.lat, r.match.lon, b.match.lat, b.match.lon) > RIVAL_M:
                return False, f"two readings fit: {b.match.label} and {r.match.label}"
        return True, ""


def _dist(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x1, y1 = _xy(lat1, lon1)
    x2, y2 = _xy(lat2, lon2)
    return float(math.hypot(float(x1) - float(x2), float(y1) - float(y2)))


def locator(gaz: Gazetteer) -> PageLocator:
    """The gazetteer's page locator, built on first use."""
    loc = getattr(gaz, "_page_locator", None)
    if loc is None:
        loc = PageLocator(gaz)
        gaz._page_locator = loc
    return loc


def locate_page(gaz: Gazetteer, text: str, departments=()) -> PageGeo:
    """A page's location, held to the towns of its tones (``departments``: the tone names of ours; one that names no
    town, "PCFD", holds it to the DISTRICT). No departments: no town to hold it to."""
    return locator(gaz).locate(text, home_towns(departments) or (DISTRICT if departments else ()))


# ------------------------------------------------------------------ a typed location's block (#204)
#
# A new call's location, picked in the CAD's location box (or on its map), fills "Cross streets" with the streets at
# each end of its block: the street's TIGER centrelines (data/gazetteer.sqlite ``lines``) walked from the point both
# ways to the first place another street meets it (``intersections``: the same meeting points the page check uses,
# ``PageLocator._meets``). A name that only continues the street ("S Regent St" -> "N Regent St") is no cross street.
# The data can't tell (no centreline near the point, an end that runs out with nothing meeting it, a place set back
# between streets): that side is left out, never guessed.

BLOCK_SNAP_M = 25             # where a cross street meets the street: on its line within this
BLOCK_NEAR_M = 80             # an address point at most this far from its street's line
PLACE_STREET_M = 60           # a place's own point (no address): the nearest street this close ...
PLACE_STREET_GAP_M = 15       # ... and the next street at least this much farther (else: a corner, can't tell)
HERE_ADDRESS_M = 35           # a map click (what_is_here): the address point this close
HERE_INTERSECTION_M = 30      # ... the intersection
HERE_PLACE_M = 40             # ... the named place
HERE_STREET_M = 25            # ... the street centreline (offered as a cross street)
_DIR = {"n", "s", "e", "w", "north", "south", "east", "west"}


def _base(name: str) -> str:
    """'S Regent St' -> 'regent st': a street and its continuation share it."""
    w = name.lower().split()
    while w and w[0] in _DIR:
        w = w[1:]
    return " ".join(w)


def _seg_project(P, A, B):
    """-> (distance, t 0..1, the point) from P to the segment A-B (metres)."""
    ax, ay = A
    vx, vy = B[0] - ax, B[1] - ay
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((P[0] - ax) * vx + (P[1] - ay) * vy) / L2))
    Q = (ax + t * vx, ay + t * vy)
    return math.hypot(P[0] - Q[0], P[1] - Q[1]), t, Q


class _Blocks:
    """Each street's centreline as a graph (vertices in metres; the meeting points marked), built on first use."""

    def __init__(self, loc: PageLocator):
        self.loc, self.gaz = loc, loc.gaz
        self.lines: dict[int, list[np.ndarray]] = collections.defaultdict(list)
        try:
            db = sqlite3.connect(f"file:{self.gaz.path}?mode=ro", uri=True)
            try:
                rows = db.execute("SELECT street_id, coords FROM lines").fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            rows = []
        for sid, coords in rows:
            arr = np.asarray(json.loads(coords), dtype=float)
            if len(arr) >= 2:
                x, y = _xy(arr[:, 1], arr[:, 0])
                self.lines[sid].append(np.column_stack([x, y]))
        self.bbox = {sid: (np.min([a.min(axis=0) for a in ls], axis=0), np.max([a.max(axis=0) for a in ls], axis=0))
                     for sid, ls in self.lines.items()}
        self.graphs: dict[int, tuple] = {}
        self._addr = None
        self._inter = None

    # -- the graph

    def graph(self, sid: int):
        g = self.graphs.get(sid)
        if g is not None:
            return g
        pos: list[tuple[float, float]] = []
        index: dict[tuple[float, float], int] = {}
        adj: list[dict[int, float]] = []

        def vertex(x: float, y: float) -> int:
            k = (round(x, 1), round(y, 1))
            if k not in index:
                index[k] = len(pos)
                pos.append((float(x), float(y)))
                adj.append({})
            return index[k]

        def link(a: int, b: int) -> None:
            if a != b:
                d = math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1])
                adj[a][b] = adj[b][a] = d

        for arr in self.lines.get(sid, ()):
            prev = vertex(*arr[0])
            for x, y in arr[1:]:
                cur = vertex(x, y)
                link(prev, cur)
                prev = cur
        marks: dict[int, set[int]] = collections.defaultdict(set)
        for c in sorted(self.loc.adj.get(sid, ())):
            for M in self.loc._meets(sid, c):
                best = None
                for a in range(len(adj)):
                    for b in adj[a]:
                        if b < a:
                            continue
                        d, t, Q = _seg_project(M, pos[a], pos[b])
                        if best is None or d < best[0]:
                            best = (d, a, b, Q)
                if best is None or best[0] > BLOCK_SNAP_M:
                    continue
                _d, a, b, Q = best
                if math.hypot(Q[0] - pos[a][0], Q[1] - pos[a][1]) < 3:
                    w = a
                elif math.hypot(Q[0] - pos[b][0], Q[1] - pos[b][1]) < 3:
                    w = b
                else:                                   # the meeting point mid-segment: split it there
                    w = vertex(*Q)
                    del adj[a][b], adj[b][a]
                    link(a, w)
                    link(w, b)
                marks[w].add(c)
        g = (pos, adj, dict(marks))
        self.graphs[sid] = g
        return g

    def near_line(self, sid: int, P) -> tuple[float, int, int, tuple[float, float]] | None:
        """The closest point of street sid's centreline to P: (distance, a, b, the point) on its edge a-b."""
        pos, adj, _m = self.graph(sid)
        best = None
        for a in range(len(adj)):
            for b in adj[a]:
                if b < a:
                    continue
                d, _t, Q = _seg_project(P, pos[a], pos[b])
                if best is None or d < best[0]:
                    best = (d, a, b, Q)
        return best

    def _end(self, sid: int, start: int, cost: float, banned: tuple[int, int]):
        """From vertex start (cost metres from the point), along the street away from edge ``banned``: the first
        vertex another street meets it (not one that only continues it) -> (vertex, [street ids], path) or None."""
        pos, adj, marks = self.graph(sid)
        own = _base(self.gaz.streets[sid].name)
        dist = {start: cost}
        prev: dict[int, int] = {}
        heap = [(cost, start)]
        while heap:
            d, v = heapq.heappop(heap)
            if d > dist.get(v, math.inf) or d > BLOCK_MAX_M:
                continue
            cross = [c for c in marks.get(v, ()) if _base(self.gaz.streets[c].name) != own]
            if cross:
                path = [v]
                while path[-1] in prev:
                    path.append(prev[path[-1]])
                return v, cross, path[::-1]
            for w, L in adj[v].items():
                if {v, w} == set(banned):
                    continue
                nd = d + L
                if nd < dist.get(w, math.inf):
                    dist[w], prev[w] = nd, v
                    heapq.heappush(heap, (nd, w))
        return None

    def _number_near(self, sid: int, P) -> int | None:
        st = self.gaz.streets[sid]
        best = None
        for lst in st.addrs.values():
            for num, _suf, lat, lon in lst:
                x, y = _xy(lat, lon)
                d = math.hypot(float(x) - P[0], float(y) - P[1])
                if best is None or d < best[0]:
                    best = (d, num)
        return best[1] if best and best[0] <= 150 else None

    def block(self, sid: int, lat: float, lon: float, max_off: float = BLOCK_NEAR_M) -> dict | None:
        """The block of street sid that (lat, lon) is on: {street, crosses: [one name per end, lower house numbers
        first; "E William St / W William St" when two meet at that end], ends: [{name, lat, lon}], path: [[lon, lat],
        ...] from one end to the other}, or None when the point is off the street or neither end can be told."""
        if sid not in self.lines:
            return None
        P = tuple(float(v) for v in _xy(lat, lon))
        near = self.near_line(sid, P)
        if near is None or near[0] > max_off:
            return None
        _d, a, b, Q = near
        pos, _adj, _m = self.graph(sid)
        sides = []
        for s, o in ((a, b), (b, a)):
            r = self._end(sid, s, math.hypot(Q[0] - pos[s][0], Q[1] - pos[s][1]), (s, o))
            if r is not None:
                sides.append(r)
        if len(sides) == 2 and sides[0][0] == sides[1][0]:
            sides = sides[:1]                          # both ways round to the same corner (a loop)
        if not sides:
            return None
        if len(sides) == 2:
            nums = [self._number_near(sid, pos[v]) for v, _c, _p in sides]
            if None not in nums and nums[1] < nums[0]:
                sides = sides[::-1]
        names = []
        for _v, cross, _p in sides:
            n = " / ".join(sorted({self.gaz.streets[c].name for c in cross}))
            if n not in names:
                names.append(n)
        ends = []
        for v, _c, _p in sides:
            la, lo = _latlon(*pos[v])
            ends.append({"name": " / ".join(sorted({self.gaz.streets[c].name for c in _c})),
                         "lat": round(la, 6), "lon": round(lo, 6)})
        pts = [pos[v] for v in sides[0][2][::-1]] + [Q] + ([pos[v] for v in sides[1][2]] if len(sides) == 2 else [])
        path = []
        for x, y in pts:
            la, lo = _latlon(x, y)
            if not path or path[-1] != [round(lo, 6), round(la, 6)]:
                path.append([round(lo, 6), round(la, 6)])
        return {"street": self.gaz.streets[sid].name, "crosses": names, "ends": ends, "path": path}

    # -- what is near a point

    def streets_near(self, lat: float, lon: float, within: float) -> list[tuple[float, int]]:
        """[(metres to its centreline, street id)] for the streets within ``within``, nearest first."""
        P = tuple(float(v) for v in _xy(lat, lon))
        out = []
        for sid, (lo, hi) in self.bbox.items():
            if not (lo[0] - within <= P[0] <= hi[0] + within and lo[1] - within <= P[1] <= hi[1] + within):
                continue
            n = self.near_line(sid, P)
            if n is not None and n[0] <= within:
                out.append((n[0], sid))
        return sorted(out)

    def addresses(self):
        if self._addr is None:
            rows = [(sid, num, suf, muni, la, lo) for sid, st in self.gaz.streets.items()
                    for muni, lst in st.addrs.items() for num, suf, la, lo in lst]
            xy = np.column_stack(_xy([r[4] for r in rows], [r[5] for r in rows])) if rows else np.zeros((0, 2))
            self._addr = (rows, xy)
        return self._addr

    def intersections(self):
        if self._inter is None:
            rows = [(a, b, la, lo, muni) for (a, b), pts in self.gaz._inters.items() for la, lo, muni in pts]
            xy = np.column_stack(_xy([r[2] for r in rows], [r[3] for r in rows])) if rows else np.zeros((0, 2))
            self._inter = (rows, xy)
        return self._inter


def _blocks(gaz: Gazetteer) -> _Blocks:
    loc = locator(gaz)
    b = getattr(loc, "_blocks", None)
    if b is None:
        b = loc._blocks = _Blocks(loc)
    return b


def street_named(gaz: Gazetteer, name: str, lat: float, lon: float) -> int | None:
    """The street called ``name`` (as the gazetteer names it) nearest the point."""
    want = (name or "").strip().lower()
    ids = [sid for sid, st in gaz.streets.items() if st.name.lower() == want]
    if not ids:
        return None
    return min(ids, key=lambda sid: gaz._dist_to_street(sid, lat, lon))


def block_of(gaz: Gazetteer, lat: float, lon: float, street: str | None = None) -> dict | None:
    """The block a location is on (see ``_Blocks.block``) and ``cross``: its cross streets as the CAD writes them
    ("Clinton St and William St"). street: the address's street; none (a place's own point): the street nearest the
    point, when it is near (PLACE_STREET_M) and plainly the nearest (PLACE_STREET_GAP_M: not on a corner)."""
    B = _blocks(gaz)
    if street:
        sid = street_named(gaz, street, lat, lon)
        res = B.block(sid, lat, lon) if sid is not None else None
    else:
        near = B.streets_near(lat, lon, PLACE_STREET_M + PLACE_STREET_GAP_M)
        near = [(d, s) for k, (d, s) in enumerate(near) if k == 0 or _base(gaz.streets[s].name)
                != _base(gaz.streets[near[0][1]].name)]
        if not near or near[0][0] > PLACE_STREET_M or (len(near) > 1 and near[1][0] - near[0][0] < PLACE_STREET_GAP_M):
            return None
        res = B.block(near[0][1], lat, lon, max_off=PLACE_STREET_M)
    if res is None:
        return None
    res["cross"] = " and ".join(res["crosses"])
    return res


_HOUSE = re.compile(r"^\s*(\d+[A-Za-z]?)\s+(.+?)\s*$")


def place_address(gaz: Gazetteer, raw: str, muni: str, lat: float, lon: float) -> dict | None:
    """A named place's street address as the places data gives it ("575 Boston Post Road"), checked against the
    address points: the same number on a street of that name in its town, within 400 m of the place ->
    {address: "575 Boston Post Rd" (the gazetteer's spelling), street, lat, lon} (the address point); else None."""
    m = _HOUSE.match(raw or "")
    if not m:
        return None
    for c in gaz.candidates(f"{raw}, {muni}" if muni else raw):
        if c.kind != "address" or c.precision != "exact" or (muni and c.municipality != muni):
            continue
        if (c.house_number or "").lower() != m.group(1).lower() or _dist(c.lat, c.lon, lat, lon) > 400:
            continue
        return {"address": f"{c.house_number} {c.street}", "street": c.street, "lat": c.lat, "lon": c.lon}
    return None


def location_detail(gaz: Gazetteer, m: dict) -> dict:
    """What the CAD's location box adds to a suggestion (#204), from the gazetteer only:

        place           the place's name (a named place: a landmark or the places table), else ''
        street_address  "135 S Regent St"; a place's own address from the places data (OSM addr:*, the county's
                        facilities), in the gazetteer's spelling when the address points have it (address_checked),
                        else as the data says it; '' when the data has none (never made up)
        cross           the streets at each end of its block ("Clinton St and William St", ``block_of``); '' for an
                        intersection, a street, a mile marker or an exit, or when the data can't tell
        block           {ends: [{name, lat, lon}], path} for the map, or None
    """
    kind = m.get("kind") or ""
    lat, lon = m.get("lat"), m.get("lon")
    out = {"place": "", "street_address": "", "address_checked": False, "cross": "", "block": None}
    if lat is None or lon is None:
        return out
    blk = None
    if kind == "address":
        out["street_address"] = f"{m.get('house_number') or ''} {m.get('street') or ''}".strip()
        out["address_checked"] = True
        if m.get("street"):
            blk = block_of(gaz, lat, lon, m["street"])
    elif kind in ("landmark", "place"):
        out["place"] = m.get("place") or ""
        raw = (m.get("place_address") or "").strip()
        checked = place_address(gaz, raw, m.get("municipality") or "", lat, lon) if raw else None
        if checked:
            out["street_address"], out["address_checked"] = checked["address"], True
            blk = block_of(gaz, checked["lat"], checked["lon"], checked["street"])
        else:
            out["street_address"] = raw
            blk = block_of(gaz, lat, lon)
    if blk:
        out["cross"] = blk["cross"]
        out["block"] = {"ends": blk["ends"], "path": blk["path"]}
    return out


def what_is_here(gaz: Gazetteer, lat: float, lon: float, places: list[dict] = ()) -> dict:
    """A click on the new call's map (#204): the nearest address point, intersection and named place to it, each as a
    location box suggestion (kind, label, lat, lon, municipality, ...), and the street centreline under it (to offer
    as a cross street). Each only when close (HERE_*_M); places: the named places to look through
    [{name, address, muni, lat, lon}] (the place search's)."""
    B = _blocks(gaz)
    P = np.array([float(v) for v in _xy(lat, lon)])
    out: dict = {"address": None, "intersection": None, "place": None, "street": None}
    rows, xy = B.addresses()
    if len(rows):
        d = np.hypot(xy[:, 0] - P[0], xy[:, 1] - P[1])
        k = int(np.argmin(d))
        if d[k] <= HERE_ADDRESS_M:
            sid, num, suf, muni, la, lo = rows[k]
            st = gaz.streets[sid]
            hn = f"{num}{suf.upper()}"
            out["address"] = {"kind": "address", "label": f"{hn} {st.name}, {muni}", "lat": la, "lon": lo,
                              "municipality": muni, "street": st.name, "house_number": hn, "precision": "exact",
                              "confidence": 1.0, "metres": round(float(d[k]))}
    rows, xy = B.intersections()
    if len(rows):
        d = np.hypot(xy[:, 0] - P[0], xy[:, 1] - P[1])
        k = int(np.argmin(d))
        if d[k] <= HERE_INTERSECTION_M:
            a, b, la, lo, muni = rows[k]
            na, nb = gaz.streets[a].name, gaz.streets[b].name
            out["intersection"] = {"kind": "intersection", "label": f"{na} & {nb}" + (f", {muni}" if muni else ""),
                                   "lat": la, "lon": lo, "municipality": muni, "precision": "intersection",
                                   "confidence": 1.0, "metres": round(float(d[k]))}
    best = None
    for p in places:
        dd = _dist(lat, lon, p["lat"], p["lon"])
        if dd <= HERE_PLACE_M and (best is None or dd < best[0]):
            best = (dd, p)
    if best:
        p = best[1]
        out["place"] = {"kind": "place", "label": p["name"], "place": p["name"], "place_address": p.get("address") or "",
                        "lat": p["lat"], "lon": p["lon"], "municipality": p.get("muni") or "", "precision": "place",
                        "confidence": 1.0, "metres": round(best[0])}
    near = B.streets_near(lat, lon, HERE_STREET_M)
    if near:
        out["street"] = {"name": gaz.streets[near[0][1]].name, "metres": round(near[0][0])}
    return out
