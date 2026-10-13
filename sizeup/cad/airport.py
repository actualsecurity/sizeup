"""Westchester County Airport (HPN) in the CAD (GitHub #87, #105).

Port Chester FD responds to the airport's alerts (user, 2026-10-13: "the airport is part of Port Chester / Rye
Brook area, we respond (Port Chester) to their L2+ alerts"; the department, 2026-10-13: "put Engine 64, 62 and
Rescue 40 on the Alert Level 2, and FDR on the 3 and up": the alert, not the souls Level). What this module holds:

    layer()            data/khpn_airport.geojson (tools/fetch_airport.py), read once: what the Airport map
                       layer draws (GET /api/cad/airport) and where the field is ("airport": lat, lon, bbox)
    near(lat, lon)     within NEAR_KM of the field: the maps turn the Airport layer on for such an event
    heard(text)        what a 60 Control page or a Fire/EMS dispatch says about the airport:
                       (TYPE, SUBTYPE, note) when it names the airport, an alert or an aircraft emergency, else None
    fields(text)       the facts the tower passes (FAA AC 150/5200-31C, the ARFF notification): aircraft type,
                       nature of the emergency, minutes out, runway, souls on board, fuel, dangerous goods,
                       as said on the page: {"acft_type": "Boeing 737", "acft_souls": 140, ...}, only what was said
    level(souls)       the Westchester souls-on-board Level 1-5 (1-2 / 3-10 / 11-25 / 26-50 / 51+)
    summary(inc)       one line for the boards and the export: "Alert 2 · Boeing 737 · unsafe gear · 12 min · RWY 16 ·
                       140 souls (Level 5)"
    default_location   an Airport event with no location gets the terminal's address, 240 Airport Rd

The alerts are the FAA's (AC 150/5200-31C, Airport Emergency Plan, para 7-1-4a): Alert I, local standby;
Alert II, full emergency; Alert III, aircraft accident. The Level 1-5 scale is a Westchester overlay on the
souls on board (reported by responders, unofficial). "Alert N" is the alert; "Level N" alone is the souls
Level, never the alert; both can be said ("Alert 2 Level 3"). The event type AIRPORT and its subtypes are in
data/cad_lists.json, a proposal for the department to confirm (the Alert 2 and Alert 3 run cards it has).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from sizeup.cad.lists import ROOT

LAYER_FILE = ROOT / "data" / "khpn_airport.geojson"
TYPE = "AIRPORT"
ALERTS = {"1": "ALERT-1", "2": "ALERT-2", "3": "ALERT-3"}
NAME = "Westchester County Airport"
ADDRESS = "240 Airport Rd"
ADDRESS_TOWN = "Rye Brook"
ADDRESS_LL = (41.068787, -73.704333)      # the NYS address point of 240 Airport Rd (the terminal)
ARP = (41.0669531, -73.7075661)           # airport reference point (FAA Chart Supplement)
NEAR_KM = 1.5
RUNWAYS = ("16", "34", "11", "29")
FIELDS = ("acft_type", "acft_nature", "acft_eta", "acft_runway", "acft_souls", "acft_level", "acft_fuel", "acft_cargo")
LEVELS = ((2, 1), (10, 2), (25, 3), (50, 4))     # souls up to -> Level; more: Level 5

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
        "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
_ROMAN = {"i": "1", "ii": "2", "iii": "3"}
_WORD = r"(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|" \
        r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty)"

# "Alert 2", "alert two", "Alert II", "alert level 2", "alert 2 full emergency"
_ALERT = re.compile(r"\balert\s+(?:level\s+)?(?P<n>[123]|one|two|three|i{1,3})\b")
# "Level 3" (the souls Level) when it isn't "alert level 3": the alert matches are blanked first
_LEVEL = re.compile(r"\blevel\s+(?P<n>[1-5]|one|two|three|four|five)\b")
_ALERT_NAMES = {"full emergency": "2", "local standby": "1", "aircraft accident alert": "3"}
# The airport named (not just "Airport Road", the address of anything at the terminal).
_AIRPORT = re.compile(r"\b(?:westchester county airport|county airport|the airport|airport incident|airport alert|hpn)\b|"
                      r"\bairport\b(?!\s+(?:road|rd|rd\.)\b)")
_RUNWAY_WORDS = {"one six": "16", "three four": "34", "one one": "11", "two nine": "29"}
_RUNWAY = re.compile(r"\b(?:runway|rwy)\s+(?P<n>16|34|11|29|one six|three four|one one|two nine)\b")
_AIRCRAFT = re.compile(r"\b(?:aircraft|airplane|plane|jet|helicopter|chopper|cessna|boeing|airbus|gulfstream|learjet|"
                       r"pilot|tower|inbound|on final|on approach)\b")
_INBOUND = re.compile(r"\b(?:inbound|on final|on approach|minutes? out|miles? out|landing|declared|returning to the field|"
                      r"diverting|emergency landing)\b")
_OFF_AIRPORT = re.compile(r"\b(?:off(?:\s+the)?\s+airport|outside the airport|near the airport|off airport|rye lake|kensico|"
                          r"blind brook|short of the runway|into (?:a|the) (?:house|home|building|woods|trees|road|street|"
                          r"highway|parkway)|(?:i|interstate)\s*684|hutchinson|the hutch|king street|king st|"
                          r"lincoln avenue|lincoln ave|anderson hill|purchase street|purchase st)\b")

_HIJACK = re.compile(r"\bhijack(?:ing|ed)?\b")
_SECURITY = re.compile(r"\bbomb threat\b|\bsuspicious (?:package|device|item|bag)\b|\bbomb\b|\bsecurity (?:threat|incident)\b")
_WATER = re.compile(r"\b(?:aircraft|airplane|plane|jet|helicopter|chopper) (?:is |went |has gone |)(?:down |crashed |)"
                    r"in(?:to)? (?:the )?(?:water|lake|reservoir|brook|sound|river)\b|"
                    r"\b(?:aircraft|plane) in the water\b|\bwater rescue\b")
_ACFT_WORD = re.compile(r"\b(?:aircraft|airplane|plane|jet|helicopter|chopper)\b")
_CRASH = re.compile(r"\b(?:aircraft|airplane|plane|jet|helicopter|chopper) (?:has |is |)(?:crash(?:ed)?|down|went down)\b|"
                    r"\bcrash(?:ed)?\s+(?:aircraft|airplane|plane|jet)\b|\b(?:plane|aircraft|jet|helicopter) crash\b|"
                    r"\baircraft accident\b|\bcrash landing\b|\bcrash landed\b")
_FUEL = re.compile(r"\b(?:fuel spill|jet fuel|fuel leak|fuel tanker|fuel truck|hazmat|haz mat|hazardous materials?)\b")
_ACFT_FIRE = re.compile(r"\b(?:aircraft|airplane|plane|jet|helicopter) (?:is )?(?:on )?fire\b|"
                        r"\bfire\s+(?:on|in)\s+(?:an?\s+)?(?:aircraft|airplane|plane|jet)\b|"
                        r"\b(?:apu|brake|brakes|wheel|ramp|cabin|cockpit) fire\b|\bground emergency\b")
_STRU = re.compile(r"\b(?:structure fire|building fire|hangar fire|terminal fire|fuel farm fire|working fire|10 75|"
                   r"fire in (?:the |a )?(?:hangar|terminal|fuel farm|building|fbo))\b")
_DISABLED = re.compile(r"\b(?:off the runway|off the end of the runway|runway excursion|disabled aircraft|disabled plane|"
                       r"ran off the runway|overran the runway|went off the end|off the taxiway|stuck (?:on|in) the "
                       r"(?:runway|taxiway|grass|mud)|(?:blew|blown) (?:a )?tires?|collapsed gear|gear collapse)\b")

# The nature of the emergency, as the tower passes it (first match wins; the specific ones first).
_NATURES: tuple[tuple[re.Pattern, str, bool], ...] = (          # (pattern, as stored, aircraft-inherent)
    (re.compile(r"\b(?:unsafe gear|gear up|gear problem|gear indication|gear issue|landing gear|nose gear|gear (?:won t|will not|not) "
                r"(?:come )?(?:down|retract|lock)|no gear|gear unsafe|gear warning)\b"), "unsafe gear", True),
    (re.compile(r"\bengine fire\b|\bfire in (?:the )?(?:number )?(?:\d |one |two )?engine\b"), "engine fire", False),
    (re.compile(r"\b(?:engine out|engine failure|engine failed|lost an engine|engine loss|rough engine|engine trouble|engine problem)\b"), "engine out", False),
    (re.compile(r"\bsmoke in the cockpit\b|\bsmoke in cockpit\b"), "smoke in the cockpit", False),
    (re.compile(r"\bsmoke in the (?:cabin|aircraft|plane)\b|\bsmoke in cabin\b|\bsmoke on board\b"), "smoke in the cabin", False),
    (re.compile(r"\bfire on board\b|\bcargo fire\b"), "fire on board", True),
    (re.compile(r"\bhydraulic(?:s)? (?:failure|problem|issue|leak|loss)\b|\blost hydraulics\b|\bno hydraulics\b"), "hydraulic failure", True),
    (re.compile(r"\bbird strike\b|\bstruck a bird\b|\bhit a bird\b"), "bird strike", True),
    (re.compile(r"\b(?:fuel emergency|low fuel|minimum fuel|min fuel)\b"), "low fuel", False),
    (re.compile(r"\b(?:de)?pressurization\b|\bcabin pressure\b"), "pressurization", True),
    (re.compile(r"\bflaps? (?:failure|problem|issue|stuck)\b|\bno flaps\b"), "flap problem", True),
    (re.compile(r"\bmedical (?:emergency )?on board\b|\bsick passenger\b|\bpassenger (?:unresponsive|unconscious)\b"), "medical on board", True),
    (re.compile(r"\belectrical (?:failure|problem|issue|fire)\b|\blost electrical\b"), "electrical problem", False),
    (re.compile(r"\b(?:blew|blown) (?:a )?tires?\b|\bflat tire\b"), "blown tire", False),
    (re.compile(r"\b(?:declared|declaring|declares) an emergency\b|\bemergency declared\b"), "declared an emergency", True),
    (re.compile(r"\bemergency landing\b|\bprecautionary landing\b"), "emergency landing", True),
    (re.compile(r"\binbound with an emergency\b|\baircraft emergency\b|\bairplane emergency\b|\bin flight emergency\b"),
     "inbound with an emergency", True),
)

# Aircraft types as said: a make with its model when said, or a well-known model alone.
_TYPES = re.compile(
    r"\b(?P<t>"
    r"boeing(?: 7[0-9]7(?: \d{3})?)?|b ?7[0-9]7(?: \d{3})?|7[0-9]7(?: \d{3})?|airbus(?: a ?\d{3}(?:neo)?)?|a ?3[0-8]0(?:neo)?|a ?2[12]0(?:neo)?|"
    r"cessna(?: (?:citation(?: (?:x|xls|cj ?\d|sovereign|latitude|longitude|mustang|excel|encore|bravo|ultra|m2|\d{3}|[ivx]+))?|"
    r"caravan|skyhawk|skylane|mustang|cj ?\d|\d{3}))?|citation(?: (?:x|xls|cj ?\d|sovereign|latitude|longitude|mustang|excel|encore|bravo|ultra|m2|\d{3}|[ivx]+))?|"
    r"gulfstream(?: (?:g ?\d{3}|iv|v|g ?iv|g ?v))?|g ?[4-8]\d0|learjet(?: \d{2})?|lear(?: \d{2})?|challenger(?: \d{3})?|"
    r"falcon(?: \d{1,4}[a-z]{0,2})?|king air(?: \d{3})?|pilatus(?: pc ?\d{2})?|pc ?12|pc ?24|embraer(?: \d{3})?|"
    r"phenom(?: \d{3})?|praetor(?: \d{3})?|legacy(?: \d{3})?|global(?: (?:express|\d{4}))?|crj ?\d{3}|erj ?\d{3}|e ?1[79]\d|"
    r"dash 8|q ?400|dhc ?8|piper(?: (?:cherokee|archer|arrow|saratoga|malibu|meridian|seneca|navajo|cheyenne|cub|m\d{3}|pa ?\d{2}))?|"
    r"beechcraft(?: (?:bonanza|baron|king air(?: \d{3})?|premier|\d{2,4}))?|beech(?: (?:bonanza|baron|king air(?: \d{3})?|premier|\d{2,4}))?|bonanza|baron|cirrus(?: sr ?\d{2})?|"
    r"sr ?2[02]|hawker(?: \d{3,4})?|mooney|diamond(?: da ?\d{2})?|da ?\d{2}|skyhawk|skylane|caravan|robinson(?: r ?\d{2})?|r ?[24]4|"
    r"sikorsky(?: s ?\d{2})?|bell(?: \d{3})?|black hawk|helicopter|chopper|light twin|twin engine|single engine|"
    r"small plane|light aircraft|small aircraft|regional jet|business jet|corporate jet|private jet|airliner|"
    r"cargo plane|glider|ultralight)\b")
_MAKES = {"boeing", "airbus", "cessna", "gulfstream", "learjet", "lear", "challenger", "falcon", "pilatus", "embraer", "piper",
          "beechcraft", "beech", "cirrus", "hawker", "mooney", "diamond", "robinson", "sikorsky", "bell", "citation", "phenom",
          "praetor", "legacy", "global"}
_GENERIC = {"helicopter", "chopper", "light twin", "twin engine", "single engine", "small plane", "light aircraft", "small aircraft",
            "regional jet", "business jet", "corporate jet", "private jet", "airliner", "cargo plane", "glider", "ultralight"}
_TAIL = re.compile(r"\bn\d{1,5}[a-z]{0,2}\b")
_ETA = re.compile(rf"\b(?P<n>{_WORD})\s+(?:minutes?|mins?|min)\s+(?:out|away|from (?:landing|touchdown|the field|the airport)|"
                  rf"to (?:landing|touchdown|the field|the airport))\b|\b(?:eta|landing|arriving|touchdown|in)\s+(?:in\s+)?(?P<m>{_WORD})\s+(?:minutes?|mins?|min)\b")
_SOULS = re.compile(rf"\b(?P<n>{_WORD})\s+(?:souls?|sob|pob|persons?|people)(?:\s+on\s*board)?\b|"
                    rf"\b(?:souls?|persons?|people)(?:\s+on\s*board)?[\s:]+(?P<m>{_WORD})\b|\b(?P<k>{_WORD})\s+on\s*board\b")
_PAX = re.compile(rf"\b(?P<n>{_WORD})\s+(?:passengers?|pax)\b|\b(?:passengers?|pax)[\s:]+(?P<m>{_WORD})\b")
_CREW = re.compile(rf"\b(?P<n>{_WORD})\s+crew\b|\bcrew(?: of)?[\s:]+(?P<m>{_WORD})\b")
_FUEL_QTY = re.compile(r"\b(?P<n>\d{1,3}(?: \d{3})*|\d+)\s*(?P<u>pounds|lbs|lb|gallons|gal|kilos|kg|hours|hrs|hour|hr)\s+(?:of\s+)?(?:fuel|jet a|gas)\b|"
                       r"\bfuel(?: on board| remaining)?[\s:]+(?P<m>\d{1,3}(?: \d{3})*|\d+)\s*(?P<v>pounds|lbs|lb|gallons|gal|kilos|kg|hours|hrs|hour|hr)?\b")
_UNITS = {"pounds": "lb", "lbs": "lb", "lb": "lb", "gallons": "gal", "gal": "gal", "kilos": "kg", "kg": "kg", "hours": "h", "hrs": "h",
          "hour": "h", "hr": "h"}
_CARGO_NO = re.compile(r"\bno (?:dangerous|hazardous) (?:goods|cargo|materials?)\b|\bno hazmat (?:on board|aboard)\b|\bno dangerous goods\b|"
                       r"\bnothing hazardous\b|\bno cargo\b")
_CARGO_YES = re.compile(r"\b(?:dangerous|hazardous) (?:goods|cargo|materials?)(?: on board| aboard)?(?:[\s:]+(?P<w>[a-z0-9 ]{3,40}?))?"
                        r"(?=\s+(?:on board|aboard|souls|fuel|runway|minutes|eta|time out|\d)|$)|\bhazmat (?:on board|aboard)\b")

_cache: dict | None = None


def _norm(text: str) -> str:
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", (text or "").lower())     # 12,000 -> 12000
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t.replace("-", " ")).split())


def _int(w: str | None) -> int | None:
    if not w:
        return None
    w = w.strip()
    if w.isdigit():
        return int(w)
    return _NUM.get(w)


def _alert_number(m: re.Match) -> str:
    n = m.group("n")
    return n if n.isdigit() else _ROMAN.get(n) or str(_NUM.get(n, ""))


def level(souls) -> int | None:
    """The Westchester souls-on-board Level: 1 = 1-2 souls, 2 = 3-10, 3 = 11-25, 4 = 26-50, 5 = 51+."""
    try:
        n = int(souls)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    for top, lv in LEVELS:
        if n <= top:
            return lv
    return 5


def nature(t: str, ctx: bool) -> str:
    """The first nature of the emergency the (normalised) text names; the ones that aren't aircraft-only
    ("engine fire", "smoke in the cabin") count only with an aircraft or the airport named (ctx)."""
    for pat, name, inherent in _NATURES:
        if (inherent or ctx) and pat.search(t):
            return name
    return ""


def fields(text: str) -> dict:
    """The tower's facts as the page says them, only those said: {"acft_type": "Boeing 737", "acft_nature":
    "unsafe gear", "acft_eta": 12, "acft_runway": "16", "acft_souls": 140, "acft_level": 5, "acft_fuel":
    "12,000 lb", "acft_cargo": "none"}. acft_level: as said ("Level 3"), else from the souls."""
    t = _norm(text)
    out: dict = {}
    if not t:
        return out
    ctx = bool(_AIRPORT.search(t) or _AIRCRAFT.search(t))
    hits = [m.group("t") for m in _TYPES.finditer(t)]
    typ = next((h for h in hits if h not in _GENERIC), hits[0] if hits else "")    # "single engine Cessna 172": the Cessna
    if typ:
        if typ not in _GENERIC:
            typ = " ".join(w.upper() if (len(w) <= 5 and any(c.isdigit() for c in w)) or w in ("iv", "v", "crj", "erj", "x", "xls")
                           else w.capitalize() for w in typ.split())
            typ = re.sub(r"^(Boeing \d{3}) (\d{3})$", r"\1-\2", typ)
        tail = _TAIL.search(t)
        out["acft_type"] = typ + (f" {tail.group(0).upper()}" if tail else "")
    else:
        tail = _TAIL.search(t)
        if tail and ctx:
            out["acft_type"] = tail.group(0).upper()
    n = nature(t, ctx)
    if n:
        out["acft_nature"] = n
    m = _ETA.search(t)
    if m:
        v = _int(m.group("n") or m.group("m"))
        if v is not None and 0 <= v <= 600:
            out["acft_eta"] = v
    m = _RUNWAY.search(t)
    if m:
        out["acft_runway"] = _RUNWAY_WORDS.get(m.group("n"), m.group("n"))
    rest = _ALERT.sub(" ", t)                        # "alert level 2" is the alert, not a Level
    m = _LEVEL.search(rest)
    if m:
        out["acft_level"] = _int(m.group("n"))
    fuel = _FUEL_QTY.search(t)
    if fuel:
        qty, unit = (fuel.group("n"), fuel.group("u")) if fuel.group("n") else (fuel.group("m"), fuel.group("v"))
        num = int(qty.replace(" ", ""))
        out["acft_fuel"] = f"{num:,}" + (f" {_UNITS[unit]}" if unit else "")
        t2 = t[:fuel.start()] + " " + t[fuel.end():]
    else:
        t2 = t
    souls = None
    m = _SOULS.search(t2)
    if m:
        souls = _int(m.group("n") or m.group("m") or m.group("k"))
    if souls is None:
        p, c = _PAX.search(t2), _CREW.search(t2)
        if p:
            souls = _int(p.group("n") or p.group("m"))
            if souls is not None and c:
                souls += _int(c.group("n") or c.group("m")) or 0
    if souls is not None and 0 <= souls <= 999:
        out["acft_souls"] = souls
        if "acft_level" not in out:
            lv = level(souls)
            if lv:
                out["acft_level"] = lv
    if _CARGO_NO.search(t):
        out["acft_cargo"] = "none"
    else:
        m = _CARGO_YES.search(t)
        if m:
            w = (m.group("w") or "").strip()
            out["acft_cargo"] = w if w and w not in ("on board", "aboard") else "dangerous goods on board"
    return out


def heard(text: str) -> tuple[str, str, str] | None:
    """What a page / dispatch says: ("AIRPORT", "ALERT-2", "") for "Alert 2", "alert two", "Alert II", "alert
    level 2", "full emergency"; a hijack, a bomb threat at the airport, an aircraft in the water, an aircraft
    accident (off the airport: ACFT-OFF; else ALERT-3), a fuel spill or hazmat at the airport, an aircraft fire on
    the ground, a structure fire on the airport, an aircraft off the runway: that subtype. An aircraft emergency
    inbound with no alert said ("declared an emergency, unsafe gear"): ("AIRPORT", "", "unsafe gear: alert level
    not stated"), never an alert guessed. "Level 3" alone is the souls Level, not an alert. The airport named
    without any of this: ("AIRPORT", "", "airport named on the page: alert level not stated"). Nothing about the
    airport: None. An address on Airport Rd alone (a fire alarm at the terminal) is not an airport incident."""
    t = _norm(text)
    if not t:
        return None
    named = bool(_AIRPORT.search(t))
    ctx = named or bool(_AIRCRAFT.search(t))
    m = _ALERT.search(t)
    if m:
        n = _alert_number(m)
        if n in ALERTS:
            return TYPE, ALERTS[n], ""
    if ctx:
        for phrase, n in _ALERT_NAMES.items():
            if re.search(rf"\b{phrase}\b", t):
                return TYPE, ALERTS[n], ""
    lv = _LEVEL.search(_ALERT.sub(" ", t))
    level_note = f"Level {_int(lv.group('n'))} heard: the souls-on-board level, not an alert" if lv else ""
    if _HIJACK.search(t) and ctx:
        return TYPE, "HIJACK", ""
    if _SECURITY.search(t) and named:
        return TYPE, "SECURITY", ""
    if _WATER.search(t) and (_ACFT_WORD.search(t) or named):
        return TYPE, "ACFT-WATER", ""
    if _CRASH.search(t):
        if _OFF_AIRPORT.search(t) and not _RUNWAY.search(t):
            return TYPE, "ACFT-OFF", ""
        return TYPE, "ALERT-3", "" if (named or _RUNWAY.search(t)) else "aircraft accident: on or off the airport not stated"
    if _FUEL.search(t) and named:
        return TYPE, "FUEL", ""
    if _ACFT_FIRE.search(t) and ctx and not _INBOUND.search(t):
        return TYPE, "ACFT-FIRE", ""
    if _STRU.search(t) and named:
        return TYPE, "ACFT-STRU", ""
    if _DISABLED.search(t) and ctx and not _INBOUND.search(t):
        return TYPE, "DISABLED", ""
    n = nature(t, ctx)
    if n:
        return TYPE, "", "; ".join(x for x in (f"{n}: alert level not stated", level_note) if x)
    if named:
        return TYPE, "", "; ".join(x for x in ("airport named on the page: alert level not stated", level_note) if x)
    return None


def summary(inc: dict, subtype_name: str = "") -> str:
    """One line for the wall board and the export: "Alert 2 · Boeing 737 · unsafe gear · 12 min · RWY 16 · 140 souls
    (Level 5) · 12,000 lb fuel · DG: none". Empty when the event carries none of the fields."""
    sub = inc.get("subtype") or ""
    head = ("Alert " + sub[-1]) if sub in ALERTS.values() else subtype_name
    facts = []
    if inc.get("acft_type"):
        facts.append(str(inc["acft_type"]))
    if inc.get("acft_nature"):
        facts.append(str(inc["acft_nature"]))
    if inc.get("acft_eta") is not None:
        facts.append(f"{inc['acft_eta']} min")
    if inc.get("acft_runway"):
        facts.append(f"RWY {inc['acft_runway']}")
    souls, lv = inc.get("acft_souls"), inc.get("acft_level")
    if souls is not None:
        lv = lv or level(souls)
        facts.append(f"{souls} souls" + (f" (Level {lv})" if lv else ""))
    elif lv:
        facts.append(f"Level {lv}")
    if inc.get("acft_fuel"):
        facts.append(f"{inc['acft_fuel']} fuel")
    if inc.get("acft_cargo"):
        facts.append(f"DG: {inc['acft_cargo']}")
    if not facts:
        return ""
    return " · ".join(([head] if head else []) + facts)


def layer(path: str | Path | None = None) -> dict:
    """The layer file as one dict ({} if it isn't there); cached after the first read."""
    global _cache
    if path is None and _cache is not None:
        return _cache
    p = Path(path) if path else LAYER_FILE
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    if path is None:
        _cache = d
    return d


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(min(1.0, math.sqrt(a)))


def near(lat, lon, within_km: float = NEAR_KM) -> bool:
    try:
        return km(float(lat), float(lon), *ARP) <= within_km
    except (TypeError, ValueError):
        return False


def default_location(f: dict) -> bool:
    """An Airport event typed with no location: the terminal's address and its map point (no geocoder
    needed). True when it filled them in."""
    if f.get("type") != TYPE or (f.get("address") or "").strip() or f.get("lat") is not None:
        return False
    f["address"] = ADDRESS
    f["lat"], f["lon"] = ADDRESS_LL
    f["geo_label"] = f"{ADDRESS}, {ADDRESS_TOWN}"
    return True
