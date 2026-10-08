"""Estimated drive times: how long a rig takes to reach a point (the recommendation's "~3 min est.").

User, 2026-10-07: "can we use the location information of where these apparatus are normally to
recommend certain ones based on proximity to the next call? whether they just cleared a call next
door should be taken into account too, if within reasonable time before them ending up back in station"

A deliberately simple, deterministic model (no road network, no traffic), so it is the same on
every console and tested exactly:

    distance  = straight line (haversine) x road factor           (streets aren't straight)
    drive     = distance / average emergency speed
    ETA       = drive + turnout                                   when it leaves from quarters
              = drive                                             when it is already on the road
                                                                  (returning, or a live AVL fix)

A rig that cleared a call is on its way home for drive(call -> station) + the return margin
(paperwork, picking up hose); its estimated place moves from the call to the station over that
window at a steady pace (sizeup/cad/mapview.py locate()).

The numbers are department settings (Settings > Dispatch, audited); the defaults are guesses for a
small, dense village, NOT measured. Everything shown from this is labelled an estimate.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace

EARTH_KM = 6371.0088
KM_PER_MILE = 1.609344

# key -> (default, low, high, label, unit): what Settings > Dispatch edits (stored under "dispatch").
NUMBERS = {
    "road_factor": (1.35, 1.0, 3.0, "Road factor (road distance ÷ straight line)", "×"),
    "speed_mph": (25.0, 5.0, 75.0, "Average emergency speed", "mph"),   # US units (user, 2026-10-08)
    "turnout_min": (2.0, 0.0, 15.0, "Turnout time from quarters", "min"),
    "return_margin_min": (3.0, 0.0, 60.0, "Return margin after clearing (paperwork, pickup)", "min"),
}
ORDERS = ("nearest", "card")          # recommend: nearest first within a slot | the run card / board order
KEYS = (*NUMBERS, "order", "quarters_status")


@dataclass(frozen=True)
class Params:
    road_factor: float = NUMBERS["road_factor"][0]
    speed_mph: float = NUMBERS["speed_mph"][0]
    turnout_min: float = NUMBERS["turnout_min"][0]
    return_margin_min: float = NUMBERS["return_margin_min"][0]
    order: str = "nearest"
    quarters_status: str = "AQ"      # the free status that means "back in quarters" (ends a return early)

    def with_overrides(self, d: dict) -> Params:
        """These defaults with the stored Settings values (unknown keys ignored). A speed saved before
        the switch to mph (speed_kmh) is taken over, converted."""
        d = dict(d or {})
        if "speed_mph" not in d and isinstance(d.get("speed_kmh"), (int, float)) and not isinstance(d["speed_kmh"], bool):
            d["speed_mph"] = min(max(round(d["speed_kmh"] / KM_PER_MILE, 1), NUMBERS["speed_mph"][1]), NUMBERS["speed_mph"][2])
        return replace(self, **{k: v for k, v in d.items() if k in KEYS})

    def as_dict(self) -> dict:
        return asdict(self)


def check(key: str, v, statuses=()) -> float | str:
    """One setting's value, checked (ValueError says why)."""
    if key in NUMBERS:
        _, lo, hi, label, unit = NUMBERS[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{key}: a number from {lo:g} to {hi:g} {unit}")
        return round(float(v), 2)
    if key == "order":
        if v not in ORDERS:
            raise ValueError(f"order: {' or '.join(ORDERS)}")
        return v
    if key == "quarters_status":
        if not isinstance(v, str) or (v and v not in statuses):
            raise ValueError("quarters_status: a free unit status code (or empty: none)")
        return v
    raise ValueError(f"no setting {key!r}")


def haversine_km(a: list | tuple, b: list | tuple) -> float:
    """Great-circle distance between two [lon, lat] points, km."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def drive_s(a: list | tuple, b: list | tuple, p: Params) -> float:
    """Driving time a -> b, seconds (no turnout)."""
    return haversine_km(a, b) * p.road_factor / (p.speed_mph * KM_PER_MILE) * 3600.0


def eta_s(a: list | tuple, b: list | tuple, p: Params, from_quarters: bool) -> float:
    """Arrival estimate a -> b, seconds: the drive, plus turnout when the crew leaves from quarters."""
    return drive_s(a, b, p) + (p.turnout_min * 60.0 if from_quarters else 0.0)


def return_window_s(call: list | tuple | None, station: list | tuple | None, p: Params) -> float:
    """How long after clearing a call a rig is taken to be on its way home: the drive back plus the
    margin (only the margin when its station or the call's place is unknown)."""
    drive = drive_s(call, station, p) if call and station else 0.0
    return drive + p.return_margin_min * 60.0


def along(a: list | tuple, b: list | tuple, frac: float) -> list[float]:
    """The point frac (0..1) of the way from a to b (straight: an estimate, at village scale)."""
    f = min(1.0, max(0.0, frac))
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]


def text(seconds: float) -> str:
    """'~3 min est.' (whole minutes, at least 1)."""
    return f"~{max(1, round(seconds / 60.0))} min est."
