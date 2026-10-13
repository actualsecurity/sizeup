"""Westchester County Airport (HPN) in the CAD (GitHub #87).

Port Chester FD responds to the airport's Alert 2 and above (user, 2026-10-13: "the airport is part of
Port Chester / Rye Brook area, we respond (Port Chester) to their L2+ alerts"). What this module holds:

    layer()            data/khpn_airport.geojson (tools/fetch_airport.py), read once: what the Airport map
                       layer draws (GET /api/cad/airport) and where the field is ("airport": lat, lon, bbox)
    near(lat, lon)     within NEAR_KM of the field: the maps turn the Airport layer on for such an event
    heard(text)        what a 60 Control page or a Fire/EMS dispatch says about the airport:
                       (TYPE, SUBTYPE, note) when it names the airport or an alert level, else None
    default_location   an Airport event with no location gets the terminal's address, 240 Airport Rd

The event type AIRPORT and its subtypes (ALERT-1 .. ALERT-3, FUEL, ACFT-FIRE, OTHER) are in
data/cad_lists.json; their wording is a proposal for the department to confirm (FAA's three alert levels:
1 in-flight difficulty, standby; 2 major difficulty, possible crash; 3 a crash on or near the airport).
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

_ALERT = re.compile(r"\balert\s+(?:(?P<n>[123])|(?P<w>one|two|three))\b")
_WORDS = {"one": "1", "two": "2", "three": "3"}
# The airport named (not just "Airport Road", the address of anything at the terminal).
_AIRPORT = re.compile(r"\b(?:westchester county airport|county airport|the airport|airport incident|airport alert|hpn)\b|"
                      r"\bairport\b(?!\s+(?:road|rd|rd\.)\b)")
_FUEL = re.compile(r"\b(?:fuel spill|jet fuel|fuel leak)\b")
_ACFT_FIRE = re.compile(r"\b(?:aircraft|plane|jet|helicopter)\s+(?:on\s+)?fire\b|\bfire\s+(?:on|in)\s+(?:an?\s+)?(?:aircraft|plane|jet)\b")
_CRASH = re.compile(r"\b(?:aircraft|plane|jet|helicopter)\s+(?:crash|down)\b|\bcrash(?:ed)?\s+(?:aircraft|plane)\b")

_cache: dict | None = None


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower().replace("-", " ")).split())


def heard(text: str) -> tuple[str, str, str] | None:
    """What a page / dispatch says: ("AIRPORT", "ALERT-2", "") for "Alert 2", "Alert two"; the airport named
    without a level: ("AIRPORT", "", "alert level not stated ..."); a fuel spill / aircraft fire / crash at
    the airport by name: the matching subtype. Nothing about the airport: None. An address on Airport Rd
    alone (a fire alarm at the terminal) is not an airport incident."""
    t = _norm(text)
    if not t:
        return None
    m = _ALERT.search(t)
    if m:
        return TYPE, ALERTS[m.group("n") or _WORDS[m.group("w")]], ""
    if _CRASH.search(t):
        return TYPE, "ALERT-3", ""
    if not _AIRPORT.search(t):
        return None
    if _ACFT_FIRE.search(t):
        return TYPE, "ACFT-FIRE", ""
    if _FUEL.search(t):
        return TYPE, "FUEL", ""
    return TYPE, "", "airport named on the page: alert level not stated"


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
