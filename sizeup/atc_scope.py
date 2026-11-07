"""Which aircraft can be talking on an airband channel (GitHub #260): HPN Tower, HPN Ground, NY Approach.

The ATC talkers (sizeup/atc_talkers.py) and the pre-alerts' aircraft (sizeup/atc.py) match a callsign heard against the
aircraft the Aircraft layer has around HPN (sizeup/aircraft.py). Who can be on the frequency depends on the facility,
read from the channel's label:

    tower      "HPN Tower" (any label that isn't one of the others): what the layer draws near HPN, as before #260:
               within AREA_NM, arriving, departing, on the ground or low, or an emergency whatever its phase (not
               the wider set below)
    ground     "... Ground" / "GND" (and "Clearance" / "Delivery": on the field too): on the airport only, the layer's
               "ground" phase (on the surface within its GROUND_RADIUS_NM of HPN)
    approach   "... Approach" / "APP" / "Departure" (NY Approach, N90's HPN sector: 126.4 by day, 120.8 overnight,
               124.65): out to APPROACH_NM, HPN's arrivals and departures only: the layer's arriving / departing,
               and its wider set (aircraft.py approach_phase): "inbound" (pointed at HPN and descending) and
               "outbound" (an HPN departure, or close in, climbing away from it)

An aircraft with no phase (the station's own receiver picture, the whisper prompt's fallback) fits tower and approach
within their radius, never ground.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

AREA_NM = 15.0              # the tower's (sizeup/atc_talkers.py AREA_NM, sizeup/aircraft.py RADIUS_NM)
GROUND_NM = 2.0             # on the airport (sizeup/aircraft.py GROUND_RADIUS_NM)
APPROACH_NM = 40.0          # NY Approach works HPN's traffic 10-40 NM out (#260)
INBOUND, OUTBOUND = "inbound", "outbound"   # the Aircraft layer's wider set, beyond what it draws (sizeup/aircraft.py)


@dataclass(frozen=True)
class Scope:
    facility: str                  # tower | ground | approach
    radius_nm: float
    phases: frozenset[str]         # the phases that may be on the frequency ("" = no phase known)

    def fits(self, plane: dict) -> bool:
        d = plane.get("dist")
        if isinstance(d, (int, float)) and d > self.radius_nm:
            return False
        return (plane.get("phase") or "") in self.phases


TOWER = Scope("tower", AREA_NM, frozenset({"arriving", "departing", "near", "ground", "low", "overflight", "other_ground", ""}))
GROUND = Scope("ground", GROUND_NM, frozenset({"ground"}))
APPROACH = Scope("approach", APPROACH_NM, frozenset({"arriving", "departing", "near", INBOUND, OUTBOUND, ""}))


def facility(label: str) -> str:
    """tower | ground | approach, from the channel's label."""
    words = set("".join(ch if ch.isalnum() else " " for ch in (label or "").lower()).split())
    if words & {"approach", "app", "departure", "dep"}:
        return "approach"
    if words & {"ground", "gnd", "clearance", "delivery", "clr"}:
        return "ground"
    return "tower"


def scope(label: str) -> Scope:
    return {"ground": GROUND, "approach": APPROACH}.get(facility(label), TOWER)


def planes_for(label: str, planes: Iterable[dict]) -> list[dict]:
    """The aircraft that may be talking on that channel (the module doc)."""
    s = scope(label)
    return [a for a in planes if isinstance(a, dict) and s.fits(a)]


def widest_nm(labels: Iterable[str]) -> float:
    """How far out the aircraft are needed for these channels (the tower's AREA_NM at least)."""
    return max([AREA_NM, *(scope(x).radius_nm for x in labels)])
