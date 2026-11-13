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

The reading's precision: address | intersection | landmark | street (pagegeo's kinds), confirmed when the page locator
confirms it (cross streets, read-back). Unconfirmed readings are never exact: the map draws them hollow (PR 5), the
alternatives go with them.
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
SPACED_PLATE = re.compile(r"\b[A-Z]{2,4}[\s-]\d{3,4}\b")                  # ABC 1234, ABC-1234 (as written, upper case)
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
        letters = []
        while j < len(toks) and len(letters) < 2 and re.fullmatch(r"[a-z]{1,4}", toks[j]) and toks[j] not in NUMBER_CUES:
            letters.append(toks[j])
            j += 1
        digits = []
        while j < len(toks) and len(digits) < 2 and re.search(r"\d", toks[j]):
            digits.append(toks[j])
            j += 1
        if digits:
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
    t = UNIT_NUM.sub(" ", t)
    t = TIME.sub(" ", t)
    t = MIXED.sub(" ", t)
    for said in callsigns(system, service):
        if said.isdigit():                              # the number alone: never inside 2662 / DPT266 / CHIEF266
            t = re.sub(rf"(?<![A-Za-z0-9]){re.escape(said)}(?![A-Za-z0-9])", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def has_cue(text: str) -> bool:
    """Worth the gazetteer: an address-like phrase, an intersection or block cue."""
    from sizeup.cad.radiolog import addresses_in
    return bool(addresses_in(text)) or bool(CROSS_CUE.search(text or ""))


class Locator:
    """locate(text, scope) over a gazetteer; read only."""

    def __init__(self, gaz):
        self.gaz = gaz

    def __call__(self, text: str, scope: dict) -> Place | None:
        return self.locate(text, scope)

    def locate(self, text: str, scope: dict) -> Place | None:
        clean = strip(text, str(scope.get("system") or ""), str(scope.get("service") or ""))
        if not clean or not has_cue(clean) or self.gaz is None:
            return None
        from sizeup.geo.pagegeo import locator
        towns = tuple(scope.get("towns") or ())
        g = locator(self.gaz).locate(clean, towns)
        b = g.best
        if b is None:
            return None
        m = b.match
        prec = KIND_PRECISION.get(m.kind, "street")
        return Place(precision=prec, lat=m.lat, lon=m.lon, radius_m=RADIUS[prec] if g.confirmed else max(RADIUS[prec], 150.0),
                     label=m.label, town=m.municipality or "", confirmed=bool(g.confirmed), where_score=round(float(b.score), 3),
                     alts=[r.as_alt() for r in g.alternatives[:4]], basis=g.why or b.why())
