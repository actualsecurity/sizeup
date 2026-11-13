"""Where a radio pre-alert is (radiogeo): the window's words through the page locator (sizeup/geo/pagegeo.py), held to
the source's home towns, after the police words that would read as house numbers are taken out.

    Locator(gaz)                          over the gazetteer (read only)
    locator.locate(text, scope) -> Place | None
        scope: {source_key, system, service, towns: [...]}

What comes out first (strip()):
    callsigns   the system's own (data/pd_units.json, by system and service: police "266" on MRRS is Rye PD HQ,
                never Rye Brook FD 266 / DPT266 / 2662 / CHIEF266, which this never reads): the number said alone
    car N       "car 12", "unit 4", "sector 2", "post 3", "badge 123", "adam 3": a unit, not a house number
    10-codes    "10-4", "10 13", "ten four"
    plates      "plate ABC1234", "New York plate ..." and a letters-and-digits token
    times       "14:32", "1430 hours"

Then a cheap pre-filter: an address-like phrase (sizeup/cad/radiolog.py addresses_in), an intersection cue ("X and
Y", "corner of"), or a "block of": no cue, no gazetteer work (the hub has half a vCPU).

The reading's precision: address | intersection | block | landmark | street (pagegeo's kinds), confirmed when the page
locator confirms it (cross streets, read-back). Unconfirmed readings are never exact: the map draws them hollow (PR 5),
the alternatives go with them.

Spoken forms beyond what a page says (radiogeo depth):
    "X and Y", "X at Y", "corner of X and Y"   the intersection search (sizeup/geo/intersections.py) as a second
                                               opinion: one meeting point in the source's towns is the place
    "the 100 block of X", "1 hundred block"    the block: that number's point on X, said as "100 block of X", radius
                                               BLOCK_M, never an exact address
    "in front of / rear of / across from / area of / vicinity of <place>"   the place, its radius widened
    "I-95 northbound exit 21", "287 mile marker 10"   the highway exit or marker (the place search, when given)
Per-domain thresholds (DOMAIN_PARAMS): police transcripts are noisier than a page, so a reading needs a higher score to
be confirmed and a sound-alike street must be closer. pagegeo's own values (pages) are unchanged.
HQ narrowing: a headquarters callsign said alone (data/pd_units.json, on its own system and service: police "908" is
Port Chester PD's, "266" Rye PD's) holds the reading to that department's town first.
WHERE score: the locator's, + READBACK_BONUS for each further transmission in the window that repeats the street, -
RIVAL_PENALTY when another reading is close behind; 0..1.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from sizeup.intel.model import Place

PD_UNITS = Path(__file__).resolve().parents[2] / "data" / "pd_units.json"
UNIT_CUES = r"(?:car|unit|units|sector|post|badge|adam|boy|david|charlie|patrol|rmp|squad|sergeant|sgt|lieutenant|lt|officer)"
TEN_CODE = re.compile(r"\b(?:10|ten)[-\s](?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine)\b", re.I)
UNIT_NUM = re.compile(rf"\b{UNIT_CUES}\s+\d{{1,4}}\b", re.I)
MIXED = re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{5,8}\b")      # ABC1234: letters AND digits
TIME = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{3,4}\s+hours\b", re.I)
CROSS_CUE = re.compile(r"\b(?:corner of|intersection of|at the corner|block of)\b|\b\w+\s+(?:and|at|&)\s+\w+\s+"
                       r"(?:st|street|ave|avenue|rd|road|pl|place|ln|lane|dr|drive|blvd|ct|court|pkwy|parkway|ter|terrace|way)\b",
                       re.I)
KIND_PRECISION = {"address": "address", "intersection": "intersection", "landmark": "landmark", "street": "street"}
RADIUS = {"address": 25.0, "intersection": 40.0, "landmark": 75.0, "street": 300.0, "block": 120.0, "town": 0.0, "none": 0.0}
RANK = {"address": 0, "intersection": 1, "block": 2, "landmark": 3, "street": 4, "town": 5, "none": 6}
DOMAIN_PARAMS = {"police": {"confirm": 0.8, "rival": 0.1, "rival_m": 200, "sound_min": 0.7}}
READBACK_BONUS, RIVAL_PENALTY = 0.08, 0.15
SUFFIX = (r"(?:st|street|ave|avenue|av|rd|road|pl|place|ln|lane|dr|drive|blvd|boulevard|ct|court|pkwy|parkway|ter|terrace|"
          r"way|hwy|highway|tpke|turnpike|cir|circle)")
STREET = rf"((?:[a-z]+\s+){{0,3}}?[a-z]+\s+{SUFFIX})"
SPOKEN_CROSS = re.compile(rf"(?:corner of\s+|intersection of\s+)?{STREET}\s+(?:and|at|&|by)\s+{STREET}\b", re.I)
BLOCK = re.compile(rf"\b(?:the\s+)?(\d{{1,2}})\s*(hundred|00)\s+block\s+(?:of\s+)?{STREET}\b", re.I)
RELATIVE = re.compile(r"\b(in front of|rear of|behind|across from|across the street from|area of|vicinity of|near)\b", re.I)
HIGHWAY = re.compile(r"\b((?:i-?|interstate\s+|route\s+|rte\s+)?(?:95|287|684|hutch(?:inson)?(?:\s+river\s+parkway)?|"
                     r"merritt(?:\s+parkway)?|cross\s+county)\b[^.]{0,40}?\b(?:exit\s+\d{1,3}[a-z]?|"
                     r"(?:mile\s+marker|mile\s+post|milepost|mm)\s+\d{1,3}(?:\.\d)?))", re.I)


@lru_cache(maxsize=1)
def _units_file() -> dict:
    try:
        return json.loads(PD_UNITS.read_text())
    except (OSError, ValueError):
        return {}


def callsigns(system: str, service: str) -> list[str]:
    """The callsigns said on this system for this service (data/pd_units.json), as said: namespaced, never global."""
    sysd = ((_units_file().get("systems") or {}).get(system or "") or {})
    out = []
    for c in sysd.get("callsigns") or []:
        if str(c.get("service") or "") == service and c.get("said"):
            out.append(str(c["said"]))
    return out


# Words after which the next number is never a house number: a plate, a radio ID, a unit, a date of birth (#492 review:
# "...New York ABC 1234 heading down <street>" read as "1234 <street>", "ID 1452 <street>" as "1452 <street>").
NUMBER_CUES = {"plate", "plates", "tag", "tags", "registration", "reg", "radio", "id", "ids", "unit", "units", "car", "cars",
               "badge", "dob", "birth", "born", "york", "ny", "connecticut", "ct", "jersey", "nj", "pennsylvania", "pa",
               "sector", "post", "rmp", "number", "plated"}
# ... of which a unit's number may be followed by an address ("K9 unit 120 Example Ave": 120 is the house number), an ID's
# or a plate's never ("ID 1452 Example Ave": fail closed)
UNIT_WORDS = {"unit", "units", "car", "cars", "sector", "post", "rmp", "number"}
ID_WORDS = {"id", "ids", "radio", "badge", "dob", "birth", "born"}          # numbers only: never an address after them
FILLER = {"is", "number", "reads", "of", "was", "say", "says"}
NOT_PLATE = {"at", "on", "to", "the", "in", "and", "by", "for", "a", "an", "from", "near", "is", "of", "was"}
SPACED_PLATE = re.compile(r"\b[A-Z]{2,4}[\s-]\d{3,4}\b")                  # ABC 1234, ABC-1234 (as written, upper case)
STREET_NEXT = re.compile(rf"^\s*(?:[A-Za-z]+\.?\s+){{0,3}}?{SUFFIX}\b", re.I)
TOKEN = re.compile(r"\S+")


def cued_numbers(text: str) -> list[str]:
    """The tokens a NUMBER_CUES word makes not-an-address: up to two short letter groups that lead into a number (a
    plate's letters), then the token(s) with digits right after ("york abc 1234" -> abc, 1234; "id 1452" -> 1452).
    "unit 3 at 100 Example Ave" takes only the 3."""
    toks = [m.group(0).strip(".,;:!?()\"'").lower() for m in TOKEN.finditer(text or "")]
    out: list[str] = []
    i = 0
    while i < len(toks):
        if toks[i] not in NUMBER_CUES:
            i += 1
            continue
        j = i + 1
        while j < len(toks) and toks[j] in FILLER:
            j += 1
        letters = []
        while (j < len(toks) and len(letters) < 2 and re.fullmatch(r"[a-z]{1,4}", toks[j]) and toks[j] not in NUMBER_CUES
               and toks[j] not in NOT_PLATE):
            letters.append(toks[j])
            j += 1
        digits = []
        while j < len(toks) and len(digits) < 2 and re.search(r"\d", toks[j]):
            digits.append(toks[j])
            j += 1
        if digits and toks[i] not in ID_WORDS and not letters and STREET_NEXT.match(" ".join(toks[j:j + 4])):
            i = j                             # "K9 unit 120 Example Ave", "plate is 120 Example Ave": the address
        elif digits:
            out += letters + digits
            i = j
        else:
            i += 1
    return out


def _drop_cued(t: str) -> str:
    """The text without the tokens cued_numbers() names, one occurrence each, in order."""
    bad = cued_numbers(t)
    if not bad:
        return t
    out = []
    for w in t.split():
        k = w.strip(".,;:!?()\"'").lower()
        if bad and k == bad[0]:
            bad.pop(0)
            continue
        out.append(w)
    return " ".join(out)


def strip(text: str, system: str = "", service: str = "") -> str:
    """The text with the police tokens that read as house numbers blanked (see the module's doc). Fails closed: a
    number after a plate / ID / unit / date-of-birth word, and a spaced or hyphenated plate, never reach the gazetteer."""
    t = text or ""
    t = SPACED_PLATE.sub(" ", t)
    t = _drop_cued(t)
    t = TEN_CODE.sub(" ", t)
    t = UNIT_NUM.sub(lambda m: m.group(0) if STREET_NEXT.match(t[m.end():]) else " ", t)
    t = TIME.sub(" ", t)
    t = MIXED.sub(" ", t)
    for said in callsigns(system, service):
        if said.isdigit():                              # the number alone: never inside 2662 / DPT266 / CHIEF266, and
            pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(said)}(?![A-Za-z0-9])")    # never "266 Sample St" (an address)
            t = pat.sub(lambda m: m.group(0) if STREET_NEXT.match(m.string[m.end():]) else " ", t)
    return re.sub(r"\s+", " ", t).strip()


def hq_towns(text: str, system: str, service: str) -> list[str]:
    """The towns of the headquarters whose callsign is said alone (data/pd_units.json, this system and service only)."""
    sysd = ((_units_file().get("systems") or {}).get(system or "") or {})
    out = []
    for c in sysd.get("callsigns") or []:
        said = str(c.get("said") or "")
        if (str(c.get("service") or "") != service or str(c.get("role") or "") != "headquarters" or not said.isdigit()
                or not re.search(rf"(?<![A-Za-z0-9-]){re.escape(said)}(?![A-Za-z0-9-])", text or "")):
            continue
        town = re.sub(r"\s+(PD|Police.*)$", "", str(c.get("department") or "")).strip()
        if town and town not in out:
            out.append(town)
    return out


def has_cue(text: str) -> bool:
    """Worth the gazetteer: an address-like phrase, an intersection, block or highway cue."""
    from sizeup.cad.radiolog import addresses_in
    t = text or ""
    return bool(addresses_in(t)) or bool(CROSS_CUE.search(t) or SPOKEN_CROSS.search(t) or BLOCK.search(t)
                                          or HIGHWAY.search(t) or RELATIVE.search(t))


def _readbacks(text: str, label: str) -> int:
    """How many transmissions of the window (" . " between them) name the reading's street."""
    street = re.sub(r"^\d+[a-z]?\s+", "", (label or "").split(",")[0], flags=re.I).strip().lower()
    words = [w for w in re.findall(r"[a-z]+", street) if len(w) > 2][:2]
    if not words:
        return 0
    return sum(1 for part in (text or "").lower().split(" . ") if all(w in part for w in words))


class Locator:
    """locate(text, scope) over a gazetteer; read only. places: a function returning the place search
    (sizeup/geo/places.py PlaceIndex, the CAD's place_index) for highway exits and markers; None: none."""

    def __init__(self, gaz, places=None):
        self.gaz = gaz
        self.places = places
        self._cross = None

    def __call__(self, text: str, scope: dict) -> Place | None:
        return self.locate(text, scope)

    def cross(self):
        if self._cross is None and self.gaz is not None:
            from sizeup.geo.intersections import IntersectionSearch
            self._cross = IntersectionSearch(self.gaz)
        return self._cross

    def locate(self, text: str, scope: dict) -> Place | None:
        system, service = str(scope.get("system") or ""), str(scope.get("service") or "")
        clean = strip(text, system, service)
        if not clean or not has_cue(clean) or self.gaz is None:
            return None
        towns = list(scope.get("towns") or ())
        hq = [t for t in hq_towns(text, system, service) if not towns or t in towns]
        params = DOMAIN_PARAMS.get(str(scope.get("pack") or service), {})
        cands: list[Place] = []
        for home in ([hq] if hq else []) + [towns]:
            p = self._page(clean, tuple(home), params)
            if p is not None:
                cands.append(p)
                if p.confirmed:
                    break
        for fn in (self._spoken_cross, self._block, self._highway):
            try:
                p = fn(clean, towns, hq)
            except Exception:  # noqa: BLE001 - one form failing never loses the others
                p = None
            if p is not None:
                cands.append(p)
        if not cands:
            return None
        # "120 Example Ave at Sample St": the house number and its street heard, the cross street meeting it confirms the
        # ADDRESS (never the bare corner instead)
        addr = next((p for p in cands if p.precision == "address"), None)
        if addr is not None:
            street = re.sub(r"^\d+[A-Za-z]?\s+", "", addr.label.split(",")[0]).strip().lower()
            for p in cands:
                if p.precision == "intersection" and p.confirmed and street and street in p.label.lower():
                    addr.confirmed = True
                    addr.radius_m = RADIUS["address"]
                    addr.basis = "; ".join(x for x in (addr.basis, "at " + p.label.split(",")[0]) if x)
                    cands = [addr]
                    break
        best = min(cands, key=lambda p: (not p.confirmed, RANK.get(p.precision, 9), -p.where_score))
        rel = RELATIVE.search(clean)
        if rel:
            best.radius_m = max(best.radius_m * 2, 100.0)
            best.basis = "; ".join(x for x in (best.basis, rel.group(1).lower()) if x)
        n = _readbacks(clean, best.label)
        best.where_score = round(max(0.0, min(1.0, best.where_score + READBACK_BONUS * max(0, n - 1)
                                              - (RIVAL_PENALTY if best.alts and not best.confirmed else 0.0))), 3)
        return best

    def _page(self, clean: str, home: tuple, params: dict) -> Place | None:
        from sizeup.geo.pagegeo import locator
        g = locator(self.gaz).locate(clean, home, params)
        b = g.best
        if b is None:
            return None
        m = b.match
        prec = KIND_PRECISION.get(m.kind, "street")
        return Place(precision=prec, lat=m.lat, lon=m.lon, radius_m=RADIUS[prec] if g.confirmed else max(RADIUS[prec], 150.0),
                     label=m.label, town=m.municipality or "", confirmed=bool(g.confirmed), where_score=round(float(b.score), 3),
                     alts=[r.as_alt() for r in g.alternatives[:4]], basis=g.why or b.why())

    def _spoken_cross(self, clean: str, towns: list, hq: list) -> Place | None:
        cx = self.cross()
        if cx is None:
            return None
        for m in SPOKEN_CROSS.finditer(clean):
            res = cx.search(f"{m.group(1)} & {m.group(2)}") or {}
            rows = [r for r in res.get("results") or [] if not towns or r["town"] in towns]
            if hq:
                rows = [r for r in rows if r["town"] in hq] or rows
            if not rows:
                continue
            r = rows[0]
            sure = len(rows) == 1 and r.get("exact", 0) >= 1
            return Place("intersection", r["lat"], r["lon"], RADIUS["intersection"] if sure else 150.0, r["label"],
                         r["town"], sure, round(float(r.get("confidence", 0.8)) * (1.0 if sure else 0.8), 3),
                         [{"label": x["label"], "lat": x["lat"], "lon": x["lon"], "kind": "intersection", "town": x["town"],
                           "confidence": x.get("confidence")} for x in rows[1:5]],
                         "said as two streets" if sure else f"{len(rows)} places where they meet")
        return None

    def _block(self, clean: str, towns: list, hq: list) -> Place | None:
        m = BLOCK.search(clean)
        if not m:
            return None
        number = int(m.group(1)) * 100                 # "1 hundred block" and "100 block" alike
        street = m.group(3)
        hits = [c for c in self.gaz.candidates(f"{number} {street}") if c.kind in ("address", "street")
                and (not towns or c.municipality in towns)]
        if hq:
            hits = [c for c in hits if c.municipality in hq] or hits
        if not hits:
            return None
        c = hits[0]
        name = (c.street or street).strip()
        label = f"{number} block of {name}, {c.municipality}" if c.municipality else f"{number} block of {name}"
        return Place("block", c.lat, c.lon, RADIUS["block"], label, c.municipality or "", False,
                     round(min(0.9, float(c.confidence)), 3), [], "said as a block")

    def _highway(self, clean: str, towns: list, hq: list) -> Place | None:
        m = HIGHWAY.search(clean)
        if not m or self.places is None:
            return None
        ix = self.places() if callable(self.places) else self.places
        if ix is None:
            return None
        for r in ix.search(m.group(1), limit=3):
            if r.get("kind") in ("exit", "milemarker"):
                return Place("landmark", r["lat"], r["lon"], 300.0, r["label"], r.get("sub") or "", False,
                             round(float(r.get("quality", 0.6)), 3), [], "highway " + r["kind"])
        return None
