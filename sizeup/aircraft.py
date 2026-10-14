"""Aircraft near HPN on the CAD maps (GitHub #155; builds on #88 and the Airport layer, #87).

User (#155): "can we pull in the shortfinal components into this as additional layers? the way we implemented the
data and airplane tracking in there was focused on PCFD so we should keep to that design here, and include both api
data and specific SDR adsb with LNA if / when we get there, we might need to name an SDR specifically for that in
the future as well, but not yet".

What comes from shortfinal (the PCFD firehouse ADS-B + KHPN display; its branch `plan`, ported, not imported):

  the area        what matters to Port Chester / Rye Brook and HPN, not a world flight tracker: the feed is asked
                  for RADIUS_NM around the field, and only our traffic is drawn (below).
  the phase       shortfinal's classify() (common/phase.py) and its memory (core/state.py _phase), unchanged in
                  their rules: arriving (on a published approach, lined up on a runway end, or inbound, descending
                  and low enough to land here: the altitude-for-distance gate that tells HPN arrivals from LGA
                  arrivals passing over at 4,000 ft), departing (close in, low, climbing away; or seen on our
                  ground / departing and still climbing out), ground (on the surface at HPN), low (below the
                  ceiling inside the radius: LGA arrivals over Port Chester, helicopters), overflight, and
                  other_ground (on the surface somewhere else). Overflights and other fields' ground traffic are
                  not drawn; an emergency is drawn whatever its phase.
  the approaches  shortfinal's Procedures.match() on the FAA CIFP paths already in data/airport/
                  khpn_procedures.geojson (finals and transitions, incl. the curved RNAV (RNP) Z lineups over
                  Port Chester): "ILS 34", "RNAV (RNP) Z 34" ...
  the track store the glitch filter (a position jump faster than max(300 kt, 2 x speed + 100 kt) is held back
                  until it repeats 3 times), fade and drop by the feed's own clock, short trails.
  the alerts      squawk 7500 / 7600 / 7700 and the aircraft's own emergency status: always shown, first.
  the airlines    data/airport/airlines.csv: FAA JO 7340.2 designators (public domain, shortfinal's
                  tools/fetch_airlines.py): JBU123 -> "JetBlue Airways".

Left in shortfinal: ATC audio and speech-to-text, callsign attribution, adsb.im routes (community data), the
"window" view, ntfy pushes, the replay/recorder, the readsb supervisor.

Feeds, behind one interface (Feed.read() -> Snapshot), tried in the configured order ([aircraft] feeds); a feed
that fails rests (its Retry-After, else backing off) while the next one carries on:

  adsblol   adsb.lol's point query (https://api.adsb.lol/v2/point/<lat>/<lon>/<nm>): keyless, the data under
            the Open Database License (ODbL 1.0): free to use, also operationally, with attribution (the layer
            names adsb.lol and the licence). Rate-limited by load: never asked more often than every POLL_S.
            The default.
  adsbfi    adsb.fi's open data API, same JSON: its terms are "personal, non-commercial use only" with
            attribution. A fire department's dispatch isn't plainly personal use, so it is OFF unless listed.
  receiver  our own ADS-B receiver, when there is one: readsb / dump1090's aircraft.json (1090 MHz) and
            dump978-fa's (978 MHz UAT), each a file path or an http(s) URL, merged by ICAO address (the fresher
            position wins). Assumes an SDR with an LNA and a 1090 filter in front (run readsb with --gain auto:
            an LNA makes a fixed high gain overload, shortfinal bench 2026-10-02). sizeup only READS those
            files: it never starts a decoder, never opens a dongle and never writes a claim. [aircraft.receiver]
            sdr names the dongle meant for ADS-B (an exact USB serial): sizeup's own radios then never use it
            (it is added to [radio] sdr_ignore), and the layer says whether it is plugged in and who holds it
            (the shared claims, ~/.local/state/rtl-claims, sizeup/radio/claims.py). None is named yet: off.
  (OpenSky) not used: its licence allows non-profit research and education, internal evaluation and "government
            purposes"; live operational use needs a written agreement with OpenSky. Asked of the user (#155).

Types: the feed's ICAO type designator ("t": E75L) when it has one; the make and model from the FAA Releasable
Aircraft Database (public domain, registry.faa.gov/database/ReleasableAircraft.zip) by the aircraft's ICAO
address, for US aircraft: Registry, fetched by the hub weekly once someone has looked at the layer, kept in
SQLite (aircraft/faa_registry.sqlite in the state folder). Only the N-number, the make / model, the year and the
kind are kept: never the owner's name or address. Nothing is guessed: no type, no registration -> none shown.

Nothing fake: positions are the feed's (ADS-B, MLAT or TIS-B as it says); no dead reckoning. Each aircraft
carries the age of its position at the feed's time ("at"); the page adds the time since and draws anything older
than STALE_S hollow, labelled with its age; the hub drops it after DROP_S. The ETA is distance / ground speed,
labelled as such. The phase is our reading of the track (shortfinal's rules), never ATC's word.

Traffic (#146 kept the state push tiny): aircraft never go into the state push. The page asks GET
/api/cad/aircraft every 5 s only while a map has the layer on and the page is visible; the hub reads the feed
only while asked (DEMAND_S), at most every POLL_S; every answer has an ETag (304 when nothing changed).

HTTP (sizeup/web/server.py; any signed-in person, GET only):
    GET /api/cad/aircraft   {configured, note, feed: {key, label, link, licence, terms, credit} | null, feeds,
                            at (the feed's time), fetched, error, stale_s, airport, registry, receiver,
                            aircraft: [{hex, cs, flight, reg, reg_src, type, model, model_src, kind, airline, lat, lon,
                            alt, gs, trk, vr, sq, emerg, phase, rwy, appr, dist, eta, age, pos, band, trail}],
                            counts}
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sizeup import USER_AGENT as UA

ROOT = Path(__file__).resolve().parent.parent
PROCEDURES_FILE = ROOT / "data" / "airport" / "khpn_procedures.geojson"
AIRLINES_FILE = ROOT / "data" / "airport" / "airlines.csv"
ADSBLOL_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}"
ADSBFI_URL = "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{radius}"
FAA_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
FEEDS = {
    "adsblol": {"label": "adsb.lol", "link": "https://adsb.lol", "licence": "ODbL 1.0",
                "terms": "https://opendatacommons.org/licenses/odbl/1-0/", "credit": "adsb.lol (ODbL)"},
    "adsbfi": {"label": "adsb.fi", "link": "https://adsb.fi", "licence": "personal, non-commercial use only",
               "terms": "https://github.com/adsbfi/opendata", "credit": "adsb.fi open data"},
    "receiver": {"label": "our ADS-B receiver", "link": "", "licence": "ours", "terms": "", "credit": "our ADS-B receiver"},
}
REGISTRY_CREDIT = "Types: FAA aircraft registry (public domain)"

# HPN (FAA Chart Supplement via shortfinal's data/khpn.toml, checked 2026-09-30)
ICAO, IATA = "KHPN", "HPN"
ARP = (41.0669531, -73.7075661)
ELEV_FT = 439.0
RUNWAY_ENDS = (("16/34", "16", 41.07555312, -73.71330558), ("16/34", "34", 41.06000083, -73.70140237),
               ("11/29", "11", 41.06705197, -73.71576050), ("11/29", "29", 41.06442797, -73.69999812))

RADIUS_NM = 15.0              # the feed is asked for this much around HPN; "low" traffic inside it
CEILING_FT = 6000.0           # below this (MSL) inside the radius: low traffic (shortfinal's focus.ceiling_ft)
FINAL_NM = 12.0               # lined up on a runway end this close: on final
ARRIVAL_NM = 15.0             # inbound, descending and low enough this close: arriving
POLL_S = 5.0                  # an API feed: at most this often (adsb.lol rate-limits faster polling)
RECEIVER_POLL_S = 2.0         # our receiver's files: cheap, local
DEMAND_S = 60.0               # a viewer's ask keeps the feed coming this long
WAIT_FIRST_S = 5.0            # an ask with nothing fresh waits this long for the read it wakes
STALE_S = 15.0                # a position older than this is drawn hollow, with its age
DROP_S = 60.0                 # ... and gone after this (shortfinal's drop_after_s)
FEED_STALE_S = 30.0           # a receiver file whose own clock is older than this: its decoder has stopped
BACKOFF_S = (5.0, 120.0)
RATE_LIMITED_S = 30.0
TIMEOUT_S = 10.0
MAX_FEED_BYTES = 4 * 1024 * 1024
REGISTRY_EVERY_S = 7 * 86400.0
REGISTRY_BACKOFF_S = (600.0, 86400.0)
MAX_REGISTRY_BYTES = 200 * 1024 * 1024
TRAIL_S = 300.0               # trail history kept (feed seconds)
TRAIL_SPACING_M = 100.0
TRAIL_POINTS = 20             # per aircraft per answer, for arrivals, departures and emergencies
JUMP_MIN_M = 1500.0
JUMP_KT_FLOOR = 300.0
JUMP_ACCEPT_AFTER = 3
JSON_TYPE = "application/json"
PATH = "/api/cad/aircraft"

EMERGENCY_SQUAWKS = {"7500": "squawk 7500 (hijack)", "7600": "squawk 7600 (radio failure)",
                     "7700": "squawk 7700 (emergency)"}
EMERGENCY_STATUS = {"general": "general emergency", "lifeguard": "medical (lifeguard)", "minfuel": "minimum fuel",
                    "nordo": "no radio", "unlawful": "unlawful interference", "downed": "downed aircraft",
                    "reserved": "emergency"}
POSITION_SOURCES = {"adsb_icao": "adsb", "adsb_icao_nt": "adsb", "adsb_other": "adsb", "adsr_icao": "adsr",
                    "adsr_other": "adsr", "tisb_icao": "tisb", "tisb_other": "tisb", "tisb_trackfile": "tisb",
                    "mlat": "mlat", "mode_s": "modes", "adsc": "adsc", "other": "other", "unknown": "other"}
NM_M = 1852.0

# ---- geometry ----------------------------------------------------------------------------------------------------


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return metres(lat1, lon1, lat2, lon2) / NM_M


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    y = math.sin((lon2 - lon1) * p) * math.cos(lat2 * p)
    x = math.cos(lat1 * p) * math.sin(lat2 * p) - math.sin(lat1 * p) * math.cos(lat2 * p) * math.cos((lon2 - lon1) * p)
    return math.degrees(math.atan2(y, x)) % 360


def wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


# ---- one aircraft, as a feed reports it ----------------------------------------------------------------------------


def _num(d: dict, key: str) -> float | None:
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


@dataclass(frozen=True)
class Plane:
    hex: str                          # ICAO address, lower case; "~" first: not an ICAO address (TIS-B / ADS-R)
    flight: str = ""
    reg: str = ""
    type: str = ""                    # ICAO type designator, from the feed ("t")
    desc: str = ""                    # the feed's own type description, when it sends one
    alt_baro: int | str | None = None  # feet, or "ground"
    alt_geom: float | None = None
    gs: float | None = None
    track: float | None = None
    baro_rate: float | None = None
    geom_rate: float | None = None
    squawk: str = ""
    lat: float | None = None
    lon: float | None = None
    seen: float = 0.0
    seen_pos: float | None = None
    emergency: str = ""
    category: str = ""
    sel_heading: float | None = None
    source: str = ""                  # how the position was found: adsb, mlat, tisb, adsr ...
    band: str = ""                    # our receiver: "1090" or "978"

    @classmethod
    def from_json(cls, d: dict, band: str = "") -> Plane:
        alt = d.get("alt_baro")
        if isinstance(alt, (int, float)) and not isinstance(alt, bool):
            alt = int(alt)
        elif alt != "ground":
            alt = None
        lat, lon = _num(d, "lat"), _num(d, "lon")
        if lat is not None and lon is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None
        kind = str(d.get("type") or d.get("addrtype") or "")
        src = POSITION_SOURCES.get(kind, "")
        if isinstance(d.get("mlat"), list) and "lat" in d["mlat"]:
            src = "mlat"
        elif isinstance(d.get("tisb"), list) and "lat" in d["tisb"]:
            src = "tisb"
        return cls(hex=str(d.get("hex", "")).strip().lower(), flight=str(d.get("flight") or "").strip(),
                   reg=str(d.get("r") or "").strip(), type=str(d.get("t") or "").strip().upper(),
                   desc=str(d.get("desc") or "").strip(), alt_baro=alt, alt_geom=_num(d, "alt_geom"), gs=_num(d, "gs"),
                   track=_num(d, "track") if "track" in d else _num(d, "true_heading"),
                   baro_rate=_num(d, "baro_rate"), geom_rate=_num(d, "geom_rate"), squawk=str(d.get("squawk") or "").strip(),
                   lat=lat, lon=lon, seen=_num(d, "seen") or 0.0, seen_pos=_num(d, "seen_pos"),
                   emergency=str(d.get("emergency") or "").strip().lower(), category=str(d.get("category") or "").strip(),
                   sel_heading=_num(d, "nav_heading"), source=src or ("adsb" if band else ""), band=band)

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def vertical_rate(self) -> float | None:
        return self.baro_rate if self.baro_rate is not None else self.geom_rate

    @property
    def altitude_ft(self) -> float | None:
        if self.alt_baro == "ground":
            return 0.0
        if isinstance(self.alt_baro, int):
            return float(self.alt_baro)
        return self.alt_geom

    @property
    def emergency_text(self) -> str:
        if self.squawk in EMERGENCY_SQUAWKS:
            return EMERGENCY_SQUAWKS[self.squawk]
        if self.emergency and self.emergency != "none":
            return EMERGENCY_STATUS.get(self.emergency, self.emergency)
        return ""


@dataclass(frozen=True)
class Snapshot:
    now: float                        # the feed's own time, unix s
    planes: tuple[Plane, ...]


def parse_snapshot(data: dict, band: str = "") -> Snapshot:
    """readsb / dump1090 / dump978 aircraft.json ({now: s, aircraft: []}) or adsb.lol / adsb.fi ({now: ms, ac: []})."""
    if not isinstance(data, dict):
        raise ValueError("not an aircraft list")
    if "ac" in data:
        now, records = float(data.get("now") or 0) / 1000.0, data.get("ac") or []
    elif "aircraft" in data:
        now, records = float(data.get("now") or 0), data.get("aircraft") or []
    else:
        raise ValueError("no aircraft list (ac / aircraft)")
    if not now or not isinstance(records, list):
        raise ValueError("no time (now) or no aircraft list")
    return Snapshot(now, tuple(Plane.from_json(r, band) for r in records if isinstance(r, dict) and r.get("hex")))


def merge(parts: list[Snapshot]) -> Snapshot:
    """1090 and 978 by ICAO address: the fresher position wins; a field only the other band heard is kept from it."""
    if len(parts) == 1:
        return parts[0]
    now = max(p.now for p in parts)
    best: dict[str, tuple[float, Plane]] = {}
    for snap in parts:
        for pl in snap.planes:
            # rebase the ages onto the merged clock
            seen_pos = None if pl.seen_pos is None else pl.seen_pos + (now - snap.now)
            pl = replace(pl, seen=pl.seen + (now - snap.now), seen_pos=seen_pos)
            if not pl.has_position:
                when = -1e18                     # no position: loses to any that has one
            else:
                when = now - (pl.seen_pos if pl.seen_pos is not None else pl.seen)
            have = best.get(pl.hex)
            if have is None:
                best[pl.hex] = (when, pl)
                continue
            keep, other = (pl, have[1]) if when > have[0] else (have[1], pl)
            keep = replace(keep, **{k: getattr(other, k) for k in ("flight", "squawk", "category", "reg", "type")
                                     if not getattr(keep, k) and getattr(other, k)})
            best[pl.hex] = (max(when, have[0]), keep)
    return Snapshot(now, tuple(p for _, p in best.values()))


# ---- the published approaches (shortfinal common/procedures.py) ------------------------------------------------------

NM_PER_DEG_LAT = 60.0
FINAL_LATERAL_NM = 0.6
TRANSITION_LATERAL_NM = 1.0
TRACK_TOLERANCE_DEG = 35.0
GLIDE_FT_PER_NM = 350.0       # ~3.3 deg, a little steeper than a 3 deg ILS to allow for slack
MIN_FT_PER_NM = 150.0


@dataclass(frozen=True)
class OnApproach:
    name: str          # "ILS 16"
    runway: str        # "16"
    to_go_nm: float    # along the path to the threshold
    segment: str       # "final" | "transition"


class Procedures:
    """Which published approach (FAA CIFP) an aircraft is flying: laterally close to the path, tracking along it
    toward the runway, and neither far too high nor far below it for where it is on the path."""

    def __init__(self, path: str | Path | None = PROCEDURES_FILE, lat0: float = ARP[0], lon0: float = ARP[1]):
        self.lat0, self.lon0 = lat0, lon0
        self._k = math.cos(math.radians(lat0)) * NM_PER_DEG_LAT
        self.paths: list[tuple[str, str, str, list[tuple[float, float]], list[float]]] = []
        try:
            gj = json.loads(Path(path).read_text()) if path else {}
        except (OSError, ValueError):
            gj = {}
        finals: dict[str, float] = {}
        raw = [f for f in gj.get("features", []) if f.get("properties", {}).get("kind") == "approach_path"
               and f["properties"].get("segment") in ("final", "transition")]
        for f in sorted(raw, key=lambda f: f["properties"]["segment"] != "final"):   # finals first: their lengths
            p = f["properties"]
            pts = [self._xy(lon, lat) for lon, lat in f["geometry"]["coordinates"]]
            seglen = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
            tail = 0.0 if p["segment"] == "final" else finals.get(p["name"], 0.0)
            to_go = [tail + sum(seglen[i:]) for i in range(len(pts))]
            if p["segment"] == "final":
                finals[p["name"]] = to_go[0]
            self.paths.append((p["name"], p["runway"], p["segment"], pts, to_go))

    def _xy(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self._k, (lat - self.lat0) * NM_PER_DEG_LAT)

    def __len__(self) -> int:
        return len(self.paths)

    def match(self, lat: float, lon: float, track: float | None, agl_ft: float | None, vrate: float | None) -> OnApproach | None:
        if not self.paths or track is None or agl_ft is None or (vrate is not None and vrate > 500):
            return None
        p = self._xy(lon, lat)
        best: tuple[float, OnApproach] | None = None
        for name, runway, segment, pts, to_go_at in self.paths:
            limit = FINAL_LATERAL_NM if segment == "final" else TRANSITION_LATERAL_NM
            for i, (a, b) in enumerate(zip(pts, pts[1:])):
                dx, dy = b[0] - a[0], b[1] - a[1]
                seg2 = dx * dx + dy * dy
                if seg2 == 0:
                    continue
                t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg2))
                lateral = math.dist(p, (a[0] + t * dx, a[1] + t * dy))
                if lateral > limit:
                    continue
                if abs(wrap180(track - math.degrees(math.atan2(dx, dy)) % 360)) > TRACK_TOLERANCE_DEG:
                    continue
                to_go = to_go_at[i] - t * math.sqrt(seg2)
                if agl_ft > GLIDE_FT_PER_NM * to_go + 1500:
                    continue          # far too high for this point: passing over
                if to_go > 3 and agl_ft < MIN_FT_PER_NM * to_go - 300:
                    continue          # far below it: low VFR traffic or a helicopter under it
                score = lateral + (0 if segment == "final" else 0.3)
                if best is None or score < best[0]:
                    best = (score, OnApproach(name, runway, round(to_go, 2), segment))
        return best[1] if best else None


# ---- what each aircraft is doing at HPN (shortfinal common/phase.py) ---------------------------------------------------

ARRIVING, DEPARTING, GROUND, OTHER_GROUND, LOW, OVERFLIGHT = "arriving", "departing", "ground", "other_ground", "low", "overflight"
SHOWN = (ARRIVING, DEPARTING, GROUND, LOW)      # drawn; an emergency is drawn whatever its phase
GROUND_RADIUS_NM = 2.0


@dataclass(frozen=True)
class Phase:
    kind: str
    runway: str | None = None
    dist_nm: float | None = None      # to the threshold (on final / on an approach) or to the field
    eta_s: float | None = None        # distance / ground speed: an estimate
    approach: str | None = None       # "ILS 34", or "maneuvering"


def _eta(d: float, gs: float | None) -> float | None:
    return d / gs * 3600 if gs and gs > 30 else None


def _runway_ends():
    for name, end, lat, lon in RUNWAY_ENDS:
        _, other, olat, olon = next(r for r in RUNWAY_ENDS if r[0] == name and r[1] != end)
        yield end, lat, lon, bearing(lat, lon, olat, olon)      # landing on 16: heading ~150


def classify(ac: Plane, procedures: Procedures | None = None, ceiling_ft: float = CEILING_FT,
             radius_nm: float = RADIUS_NM) -> Phase:
    """One snapshot's reading, shortfinal's rules unchanged."""
    if not ac.has_position:
        return Phase(OVERFLIGHT)
    d = nm(ARP[0], ARP[1], ac.lat, ac.lon)
    alt = ac.altitude_ft
    agl = None if alt is None else alt - ELEV_FT
    gs, trk, vr = ac.gs, ac.track, ac.vertical_rate
    if ac.alt_baro == "ground" or (agl is not None and agl < 150 and (gs or 0) < 60):
        return Phase(GROUND if d <= GROUND_RADIUS_NM else OTHER_GROUND, dist_nm=d)
    if procedures is not None and agl is not None and ac.category != "A7":     # A7: rotorcraft
        m = procedures.match(ac.lat, ac.lon, trk, agl, vr)
        if m is not None:
            return Phase(ARRIVING, runway=m.runway, dist_nm=m.to_go_nm, eta_s=_eta(m.to_go_nm, gs), approach=m.name)
    if agl is not None and trk is not None and (vr is None or vr < 500):
        for end, lat, lon, landing in _runway_ends():
            dt = nm(lat, lon, ac.lat, ac.lon)
            if dt > FINAL_NM or agl > GLIDE_FT_PER_NM * dt + 800:
                continue
            off = abs(wrap180(bearing(lat, lon, ac.lat, ac.lon) - (landing + 180)))   # the approach side
            lateral = dt * math.sin(math.radians(min(off, 90)))
            if off < 90 and lateral < max(0.6, 0.12 * dt) and abs(wrap180(trk - landing)) < 35:
                return Phase(ARRIVING, runway=end, dist_nm=dt, eta_s=_eta(dt, gs))
    if agl is not None and trk is not None and vr is not None:
        to_field = bearing(ac.lat, ac.lon, ARP[0], ARP[1])
        from_field = (to_field + 180) % 360
        # Inbound, descending and low enough to land here, not pass over: pointed here (and set to keep pointing
        # here, when the selected heading is sent: an LGA arrival on a downwind tracks our way for a while), and
        # slow enough (an LGA arrival crossing 3-6 NM east at 250 kt points our way and descends, RPA4550).
        pointed = abs(wrap180(trk - to_field)) < 50 and (ac.sel_heading is None or abs(wrap180(ac.sel_heading - to_field)) < 50)
        slow_enough = gs is None or gs <= 180 + 10 * d
        if d <= ARRIVAL_NM and vr < -300 and agl < GLIDE_FT_PER_NM * d + 1500 and pointed and slow_enough:
            return Phase(ARRIVING, dist_nm=d, eta_s=_eta(d, gs))
        if d <= 8 and vr > 300 and agl < 5000 and abs(wrap180(trk - from_field)) < 60:
            return Phase(DEPARTING, dist_nm=d)
    if alt is not None and alt < ceiling_ft and d <= radius_nm:
        return Phase(LOW, dist_nm=d)
    return Phase(OVERFLIGHT, dist_nm=d)


PHASE_HOLD_S = 20.0           # keep an arriving reading through brief ambiguous updates
DEPARTED_KEEP_S = 15 * 60.0   # an HPN departure stays "departing" this long ...
DEPARTED_MAX_FT = 18000.0     # ... while below this and inside the radius
INBOUND_KEEP_S = 10 * 60.0    # seen arriving within this long and not landed since ...
MANEUVER_NM = 12.0            # ... still this close ...
MANEUVER_MAX_AGL = 5000.0     # ... this low ...
MANEUVER_MAX_KT = 210.0       # ... and this slow: an arrival being vectored, going around, in the pattern


class PhaseMemory:
    """shortfinal core/state.py _phase(), without its route lookups: aircraft seen leaving HPN stay departing
    until they leave the area; an arrival turning or climbing close in is still an arrival (vectors, a go-around,
    the pattern), not a departure."""

    def __init__(self, procedures: Procedures | None, ceiling_ft: float = CEILING_FT, radius_nm: float = RADIUS_NM):
        self.procedures, self.ceiling_ft, self.radius_nm = procedures, ceiling_ft, radius_nm
        self._held: dict[str, tuple[Phase, float]] = {}
        self._departed: dict[str, float] = {}
        self._inbound: dict[str, float] = {}

    def forget(self, live: set[str]) -> None:
        for memory in (self._held, self._departed, self._inbound):
            for h in [h for h in memory if h not in live]:
                del memory[h]

    def phase(self, ac: Plane, now: float) -> Phase:
        p = classify(ac, self.procedures, self.ceiling_ft, self.radius_nm)
        h = ac.hex
        alt = ac.altitude_ft
        d = p.dist_nm if p.dist_nm is not None else 1e9
        if p.kind == GROUND:
            self._inbound.pop(h, None)          # landed: whatever it does next is a departure
            self._departed[h] = now
        if p.kind == ARRIVING:
            self._held[h] = (p, now)
            self._departed.pop(h, None)
            self._inbound[h] = now
            return p
        agl = None if alt is None else alt - ELEV_FT
        maneuvering = (now - self._inbound.get(h, -1e9) < INBOUND_KEEP_S and d <= MANEUVER_NM
                       and (agl is None or agl < MANEUVER_MAX_AGL) and (ac.gs is None or ac.gs <= MANEUVER_MAX_KT))
        if p.kind == DEPARTING:
            if maneuvering:
                return Phase(ARRIVING, dist_nm=p.dist_nm, approach="maneuvering")
            self._departed[h] = now
            return p
        if p.kind not in (LOW, OVERFLIGHT):
            return p
        if not maneuvering and now - self._departed.get(h, -1e9) < DEPARTED_KEEP_S and d <= self.radius_nm \
                and (alt is None or alt < DEPARTED_MAX_FT):
            return Phase(DEPARTING, dist_nm=p.dist_nm)
        held = self._held.get(h)
        if held and now - held[1] < PHASE_HOLD_S:
            return Phase(held[0].kind, held[0].runway, p.dist_nm, held[0].eta_s, held[0].approach)
        self._held.pop(h, None)
        if maneuvering:
            return Phase(ARRIVING, dist_nm=p.dist_nm, approach="maneuvering")
        return p


# ---- the track store (shortfinal common/models.py TrackStore, by the feed's clock) ---------------------------------------


@dataclass
class Track:
    plane: Plane
    pos_time: float | None
    msg_time: float
    trail: deque = field(default_factory=deque)        # (feed time, lat, lon), oldest first
    jumps: int = 0


class Tracks:
    def __init__(self, radius_nm: float = RADIUS_NM, drop_s: float = DROP_S):
        self.radius_nm, self.drop_s = radius_nm, drop_s
        self.tracks: dict[str, Track] = {}
        self.now: float | None = None

    def update(self, snap: Snapshot) -> None:
        if self.now is not None and snap.now - self.now > self.drop_s:
            self.tracks.clear()                         # nobody looked for a while: start afresh
        self.now = max(snap.now, self.now or 0.0)
        for ac in snap.planes:
            if ac.has_position and nm(ARP[0], ARP[1], ac.lat, ac.lon) > self.radius_nm * 1.5:
                self.tracks.pop(ac.hex, None)
                continue
            pos_time = (snap.now - (ac.seen_pos if ac.seen_pos is not None else ac.seen)) if ac.has_position else None
            t = self.tracks.get(ac.hex)
            if t is None:
                t = self.tracks[ac.hex] = Track(ac, pos_time, snap.now - ac.seen)
            else:
                if pos_time is not None and t.pos_time is not None and pos_time < t.pos_time:
                    ac, pos_time = replace(ac, lat=t.plane.lat, lon=t.plane.lon), None   # an older fix than ours
                elif pos_time is not None and self._jumped(t, ac, pos_time):
                    ac, pos_time = replace(ac, lat=t.plane.lat, lon=t.plane.lon), None   # a glitch: keep the last good
                t.plane = ac
                t.msg_time = snap.now - ac.seen
                if pos_time is not None:
                    t.pos_time = pos_time
            if ac.has_position and pos_time is not None:
                self._trail(t, pos_time, ac)
        for h, t in list(self.tracks.items()):
            last = t.pos_time if t.pos_time is not None else t.msg_time
            if self.now - last > self.drop_s:
                del self.tracks[h]

    @staticmethod
    def _jumped(t: Track, ac: Plane, when: float) -> bool:
        prev = t.plane
        if not (prev.has_position and t.pos_time is not None):
            return False
        moved = metres(prev.lat, prev.lon, ac.lat, ac.lon)
        if moved < JUMP_MIN_M:
            t.jumps = 0
            return False
        dt = max(when - t.pos_time, 1.0)
        gs = ac.gs if ac.gs is not None else prev.gs
        limit = max(JUMP_KT_FLOOR, 2 * gs + 100) if gs is not None else 800.0
        if moved / dt <= limit * NM_M / 3600:
            t.jumps = 0
            return False
        t.jumps += 1
        if t.jumps < JUMP_ACCEPT_AFTER:
            return True
        t.jumps = 0
        t.trail.clear()              # it really is there: start its trail over, no spike
        return False

    @staticmethod
    def _trail(t: Track, when: float, ac: Plane) -> None:
        trail = t.trail
        if trail:
            _, lat, lon = trail[-1]
            if metres(lat, lon, ac.lat, ac.lon) < TRAIL_SPACING_M:
                return
        trail.append((when, ac.lat, ac.lon))
        while trail and when - trail[0][0] > TRAIL_S:
            trail.popleft()

    def live(self) -> list[Track]:
        return [t for t in self.tracks.values() if t.plane.has_position and t.pos_time is not None]


# ---- facts ADS-B doesn't carry: the airline (FAA JO 7340.2), the make and model (the FAA registry) ------------------------

AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})(\d[0-9A-Z]{0,3})$")    # DAL2300, RPA5840, EJA14


def load_airlines(path: str | Path | None = AIRLINES_FILE) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("designator") and row.get("company"):
                    out[row["designator"]] = re.sub(r"\s*\([^)]*\)\s*$", "", row["company"]).title()   # "(INDIANAPOLIS, IN)" off
    except (OSError, TypeError):
        pass
    return out


KINDS = {"1": "glider", "2": "balloon", "3": "blimp", "4": "airplane", "5": "airplane", "6": "helicopter",
         "7": "weight-shift", "8": "powered parachute", "9": "gyroplane", "H": "hybrid lift", "O": "other"}
ENGINES = {"0": "", "1": "piston", "2": "turboprop", "3": "turboshaft", "4": "turbojet", "5": "turbofan", "6": "ramjet",
           "7": "2-cycle", "8": "4-cycle", "10": "electric", "11": "rotary"}


def build_registry(blob: bytes, con: sqlite3.Connection) -> int:
    """MASTER.txt and ACFTREF.txt from ReleasableAircraft.zip into `con`: the N-number, model code and year by ICAO
    address; make, model, kind and engines by model code. Never a name or an address. Returns the aircraft read."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = {n.upper(): n for n in z.namelist()}
    if "MASTER.TXT" not in names or "ACFTREF.TXT" not in names:
        raise ValueError("not the FAA registry (no MASTER.txt / ACFTREF.txt)")
    con.executescript("DROP TABLE IF EXISTS aircraft; DROP TABLE IF EXISTS models;"
                      "CREATE TABLE aircraft (hex TEXT PRIMARY KEY, n TEXT, mdl TEXT, year TEXT, kind TEXT, eng TEXT);"
                      "CREATE TABLE models (code TEXT PRIMARY KEY, mfr TEXT, model TEXT, kind TEXT, eng TEXT, engines INTEGER);")

    def rows(name: str):
        with z.open(names[name]) as f:
            r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace", newline=""))
            head = [h.strip().upper() for h in next(r)]
            for row in r:
                yield {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(head) if h}

    n = 0
    batch = []
    for d in rows("MASTER.TXT"):
        hx = d.get("MODE S CODE HEX", "").lower()
        if not re.fullmatch(r"[0-9a-f]{6}", hx) or not d.get("N-NUMBER"):
            continue
        batch.append((hx, "N" + d["N-NUMBER"], d.get("MFR MDL CODE", ""), d.get("YEAR MFR", ""),
                      d.get("TYPE AIRCRAFT", ""), d.get("TYPE ENGINE", "")))
        n += 1
        if len(batch) >= 5000:
            con.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?)", batch)
            batch = []
    con.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?)", batch)
    models = []
    for d in rows("ACFTREF.TXT"):
        if d.get("CODE"):
            try:
                engines = int(d.get("NO-ENG") or 0)
            except ValueError:
                engines = 0
            models.append((d["CODE"], d.get("MFR", ""), d.get("MODEL", ""), d.get("TYPE-ACFT", ""), d.get("TYPE-ENG", ""), engines))
    con.executemany("INSERT OR REPLACE INTO models VALUES (?,?,?,?,?,?)", models)
    con.commit()
    if not n:
        raise ValueError("no aircraft in MASTER.txt")
    return n


def _title(s: str) -> str:
    """BOMBARDIER INC -> Bombardier Inc; model codes (CL-600-2B16, ERJ 170-200 LR) as they are."""
    return " ".join(w if any(c.isdigit() for c in w) else w.title() for w in s.split())


class Registry:
    """The FAA Releasable Aircraft Database, by ICAO address. Fetched by the hub weekly once someone has looked."""

    def __init__(self, url: str = "", cache_dir: str | Path | None = None, fetch: Callable | None = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.url = url
        self.path = Path(cache_dir) / "faa_registry.sqlite" if cache_dir else None
        self.fetch, self.log, self.clock = fetch, log, clock
        self._lock = threading.Lock()
        self.con: sqlite3.Connection | None = None
        self.st = {"at": None, "error": "", "next": 0.0, "fails": 0, "n": 0}
        self._busy = False
        self._open()

    def _open(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            con = sqlite3.connect(str(self.path), check_same_thread=False)
            at, n = con.execute("SELECT at, n FROM meta").fetchone()
            self.con = con
            self.st.update(at=float(at), n=int(n), next=float(at) + REGISTRY_EVERY_S)
        except (sqlite3.Error, TypeError, ValueError) as e:
            self.log(f"aircraft: FAA registry cache not read ({type(e).__name__}); fetching afresh")

    def due(self) -> bool:
        return bool(self.url) and self.fetch is not None and not self._busy and self.clock() >= self.st["next"]

    def refresh(self) -> bool:
        """Fetch and build now. True when read. Never raises."""
        if not self.url or self.fetch is None or self._busy:
            return False
        self._busy = True
        now = self.clock()
        try:
            code, _, blob = self.fetch(self.url, 300.0, MAX_REGISTRY_BYTES, {})
            if code != 200:
                raise HTTPError(self.url, code, f"HTTP {code}", None, None)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.unlink(missing_ok=True)
                con = sqlite3.connect(str(tmp), check_same_thread=False)
            else:
                con = sqlite3.connect(":memory:", check_same_thread=False)
            n = build_registry(blob, con)
            con.executescript("DROP TABLE IF EXISTS meta; CREATE TABLE meta (at REAL, n INTEGER);")
            con.execute("INSERT INTO meta VALUES (?, ?)", (now, n))
            con.commit()
            if self.path:
                con.close()
                tmp.replace(self.path)
                con = sqlite3.connect(str(self.path), check_same_thread=False)
        except Exception as e:  # network, a bad zip, a full disk: try again later, the layer works without it
            self.st["fails"] += 1
            wait = min(REGISTRY_BACKOFF_S[1], REGISTRY_BACKOFF_S[0] * 2 ** (self.st["fails"] - 1))
            self.st.update(error=f"{type(e).__name__}: {getattr(e, 'reason', e)}"[:160], next=now + wait)
            self.log(f"aircraft: FAA registry not read: {self.st['error']}; next try in {wait:.0f} s")
            return False
        finally:
            self._busy = False
        with self._lock:
            old, self.con = self.con, con
        if old is not None:
            try:
                old.close()
            except sqlite3.Error:
                pass
        self.st.update(at=now, n=n, error="", fails=0, next=now + REGISTRY_EVERY_S)
        self.log(f"aircraft: FAA registry: {n} US aircraft")
        return True

    def lookup(self, hex_: str) -> dict | None:
        """{n, make, model, year, kind, engines} for a US aircraft, else None."""
        if not re.fullmatch(r"[0-9a-f]{6}", hex_ or ""):
            return None
        with self._lock:
            if self.con is None:
                return None
            try:
                row = self.con.execute("SELECT a.n, a.year, a.kind, a.eng, m.mfr, m.model, m.kind, m.engines FROM aircraft a "
                                       "LEFT JOIN models m ON m.code = a.mdl WHERE a.hex = ?", (hex_,)).fetchone()
            except sqlite3.Error:
                return None
        if not row:
            return None
        n, year, kind, eng, mfr, model, mkind, engines = row
        return {"n": n, "make": _title(mfr or ""), "model": (model or "").strip(), "year": year or "",
                "kind": KINDS.get((kind or mkind or "").strip(), ""), "engine": ENGINES.get((eng or "").strip(), ""),
                "engines": engines or 0}

    def public(self) -> dict:
        return {"at": self.st["at"], "n": self.st["n"], "error": self.st["error"], "credit": REGISTRY_CREDIT if self.st["at"] else ""}


# ---- the feeds -----------------------------------------------------------------------------------------------------------


class RateLimited(Exception):
    def __init__(self, retry_after_s: float):
        super().__init__(f"rate limited, retry in {retry_after_s:.0f} s")
        self.retry_after_s = retry_after_s


class Feed:
    """One source of aircraft: read() -> Snapshot, or raises."""
    key = ""
    poll_s = POLL_S

    def read(self) -> Snapshot:
        raise NotImplementedError

    def meta(self) -> dict:
        return {"key": self.key, **FEEDS[self.key]}


class ApiFeed(Feed):
    """adsb.lol or adsb.fi: a point query around HPN (same readsb-like JSON)."""

    def __init__(self, key: str, url_template: str, radius_nm: float, fetch: Callable):
        self.key = key
        self.url = url_template.format(lat=f"{ARP[0]:.4f}", lon=f"{ARP[1]:.4f}", radius=int(math.ceil(radius_nm)))
        self.fetch = fetch

    def read(self) -> Snapshot:
        try:
            code, headers, body = self.fetch(self.url, TIMEOUT_S, MAX_FEED_BYTES, {"Accept": "application/json"})
        except HTTPError as e:
            if e.code == 429:
                try:
                    retry = float((e.headers or {}).get("Retry-After", "") or 0)
                except (TypeError, ValueError):
                    retry = 0.0
                raise RateLimited(max(retry, RATE_LIMITED_S)) from e
            raise
        if code == 429:
            raise RateLimited(RATE_LIMITED_S)
        if code != 200:
            raise HTTPError(self.url, code, f"HTTP {code}", None, None)
        return parse_snapshot(json.loads(body))


class ReceiverFeed(Feed):
    """Our receiver: readsb / dump1090 (1090 MHz) and dump978-fa (978 MHz UAT) aircraft.json, each a path or an
    http(s) URL, merged by ICAO address. Read only: never a decoder started, never a dongle opened."""
    key = "receiver"
    poll_s = RECEIVER_POLL_S

    def __init__(self, src_1090: str, src_978: str, fetch: Callable, clock: Callable[[], float] = time.time):
        self.srcs = {b: s for b, s in (("1090", src_1090), ("978", src_978)) if s}
        self.fetch, self.clock = fetch, clock
        self.bands = {b: {"src": s, "at": None, "n": 0, "error": ""} for b, s in self.srcs.items()}

    def _get(self, src: str) -> bytes:
        if urlsplit(src).scheme in ("http", "https"):
            code, _, body = self.fetch(src, TIMEOUT_S, MAX_FEED_BYTES, {"Accept": "application/json"})
            if code != 200:
                raise HTTPError(src, code, f"HTTP {code}", None, None)
            return body
        p = Path(src)
        if p.stat().st_size > MAX_FEED_BYTES:
            raise ValueError("aircraft.json larger than 4 MB")
        return p.read_bytes()

    def read(self) -> Snapshot:
        parts, why = [], []
        now = self.clock()
        for band, src in self.srcs.items():
            st = self.bands[band]
            try:
                snap = parse_snapshot(json.loads(self._get(src)), band)
            except (OSError, ValueError, HTTPError, URLError) as e:
                st["error"] = f"{type(e).__name__}: {getattr(e, 'reason', e)}"[:120]
                why.append(f"{band}: {st['error']}")
                continue
            st.update(at=snap.now, n=len(snap.planes))
            if now - snap.now > FEED_STALE_S:
                st["error"] = f"not updated for {now - snap.now:.0f} s (is the decoder running?)"
                why.append(f"{band}: {st['error']}")
                continue
            st["error"] = ""
            parts.append(snap)
        if not parts:
            raise RuntimeError("receiver: " + ("; ".join(why) or "no source"))
        return merge(parts)


def http_get(url: str, timeout: float, limit: int, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """(status, headers, body): urllib, GET only, the shared User-Agent. Errors as HTTPError / URLError / OSError."""
    req = Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"more than {limit // (1024 * 1024)} MB")
        return r.status, {k.lower(): v for k, v in r.headers.items()}, body


# ---- the provider -----------------------------------------------------------------------------------------------------


def _r(x: float | None, nd: int = 0):
    if x is None:
        return None
    return round(x, nd) if nd else int(round(x))


class AircraftLayer:
    def __init__(self, enabled: bool = True, feeds: tuple[str, ...] = ("adsblol",), radius_nm: float = RADIUS_NM,
                 ceiling_ft: float = CEILING_FT, adsblol_url: str = "", adsbfi_url: str = "", receiver: bool = False,
                 receiver_1090: str = "", receiver_978: str = "", sdr: str = "", sdr_name: str = "", registry: bool = True,
                 registry_url: str = "", cache_dir: str | Path | None = None, fetch: Callable | None = None,
                 devices: Callable[[], list] | None = None, holder: Callable[[str], dict | None] | None = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 airlines: dict[str, str] | None = None, procedures: Procedures | None = None):
        self.enabled = bool(enabled)
        self.radius_nm, self.ceiling_ft = float(radius_nm), float(ceiling_ft)
        self.fetch = fetch or http_get
        self.log, self.clock = log, clock
        self.sdr, self.sdr_name = sdr, sdr_name or sdr
        self.devices, self.holder = devices, holder
        self.receiver_on = bool(receiver)
        self.feeds: list[Feed] = []
        if self.enabled:
            for key in feeds:
                if key == "adsblol" and (adsblol_url or ADSBLOL_URL):
                    self.feeds.append(ApiFeed("adsblol", adsblol_url or ADSBLOL_URL, self.radius_nm, self.fetch))
                elif key == "adsbfi" and (adsbfi_url or ADSBFI_URL):
                    self.feeds.append(ApiFeed("adsbfi", adsbfi_url or ADSBFI_URL, self.radius_nm, self.fetch))
                elif key == "receiver" and receiver and (receiver_1090 or receiver_978):
                    self.feeds.append(ReceiverFeed(receiver_1090, receiver_978, self.fetch, clock))
        url = (registry_url or FAA_URL) if registry and self.enabled else ""
        self.registry = Registry(url, cache_dir, self.fetch if url else None, log, clock)
        self.airlines = load_airlines() if airlines is None else airlines
        self.procedures = Procedures() if procedures is None else procedures
        self.memory = PhaseMemory(self.procedures, self.ceiling_ft, self.radius_nm)
        self.tracks = Tracks(self.radius_nm)
        self._lock = threading.Lock()
        self._fresh = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.asked = 0.0
        self.used: Feed | None = None
        self.view: list[dict] = []
        self.st = {"at": None, "fetched": None, "error": "", "next": 0.0, "fails": 0, "seq": 0}
        self.resting: dict[str, tuple[float, str]] = {}      # feed key -> (until, why)

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print, devices: Callable[[], list] | None = None) -> AircraftLayer:
        state = Path(cfg.cad_db).parent if getattr(cfg, "cad_db", "") else None
        from sizeup.radio import claims
        sdr = getattr(cfg, "aircraft_sdr", "")
        hub = getattr(cfg, "cloud_role", "") == "hub"
        return cls(getattr(cfg, "aircraft_enabled", True), tuple(getattr(cfg, "aircraft_feeds", ("adsblol",))),
                   getattr(cfg, "aircraft_radius_nm", RADIUS_NM), getattr(cfg, "aircraft_ceiling_ft", CEILING_FT),
                   getattr(cfg, "aircraft_adsblol_url", ""), getattr(cfg, "aircraft_adsbfi_url", ""),
                   getattr(cfg, "aircraft_receiver", False), getattr(cfg, "aircraft_receiver_1090", ""),
                   getattr(cfg, "aircraft_receiver_978", ""), sdr, (getattr(cfg, "sdr_names", {}) or {}).get(sdr, sdr),
                   getattr(cfg, "aircraft_registry", True), "", (state / "aircraft") if state else None,
                   devices=None if hub else devices, holder=None if hub else claims.holder, log=log)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.feeds)

    def why_not(self) -> str:
        if not self.enabled:
            return "Aircraft off ([aircraft] enabled = false)"
        if not self.feeds:
            return "Aircraft not configured (no feed)"
        return ""

    # -- the schedule ------------------------------------------------------------------------------------------------

    def start(self) -> AircraftLayer:
        if not self.configured:
            self.log(f"aircraft: {self.why_not()}")
            return self
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="aircraft", daemon=True)
            self._thread.start()
            self.log(f"aircraft: {' then '.join(FEEDS[f.key]['label'] for f in self.feeds)}, {self.radius_nm:g} nm around HPN, "
                     f"while the layer is open (every {self.feeds[0].poll_s:g} s at most)")
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(1.0)
            self._wake.clear()

    def demanded(self) -> bool:
        return self.clock() - self.asked <= DEMAND_S

    def tick(self) -> None:
        if not self.configured or not self.demanded():
            return
        if self.registry.due():          # its own thread (a large download); not again while it runs
            self.registry.st["next"] = self.clock() + REGISTRY_BACKOFF_S[0]
            threading.Thread(target=self.registry.refresh, name="aircraft-registry", daemon=True).start()
        if self.clock() >= self.st["next"]:
            self.refresh()

    def ask(self) -> None:
        """A viewer looks: keep the feed coming; wake the read now when the data is stale and wait a moment for it."""
        now = self.clock()
        self.asked = now
        stale = self.st["fetched"] is None or now - self.st["fetched"] > 2 * POLL_S
        if not stale or not self.configured:
            return
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            with self._fresh:
                self._fresh.wait_for(lambda: (self.st["fetched"] is not None and self.clock() - self.st["fetched"] <= 2 * POLL_S)
                                     or bool(self.st["error"]), timeout=WAIT_FIRST_S)

    def refresh(self) -> bool:
        """Read the first feed that answers, in order. True when one did. Never raises."""
        if not self.configured:
            return False
        now = self.clock()
        why = []
        for feed in self.feeds:
            until, err = self.resting.get(feed.key, (0.0, ""))
            if until > now:
                why.append(f"{FEEDS[feed.key]['label']}: {err}")
                continue
            try:
                snap = feed.read()
            except RateLimited as e:
                self.resting[feed.key] = (now + e.retry_after_s, str(e))
                why.append(f"{FEEDS[feed.key]['label']}: {e}")
                continue
            except Exception as e:  # network, a file, JSON: the next feed carries on
                err = f"{type(e).__name__}: {getattr(e, 'reason', e)}"[:160]
                self.resting[feed.key] = (now + BACKOFF_S[0], err)
                why.append(f"{FEEDS[feed.key]['label']}: {err}")
                continue
            self.resting.pop(feed.key, None)
            self._take(feed, snap, now)
            return True
        self.st["fails"] += 1
        wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** (self.st["fails"] - 1))
        with self._fresh:
            self.st.update(error="; ".join(why)[:300], next=now + wait)
            self._fresh.notify_all()
        self.log(f"aircraft: no feed answered ({self.st['error']}); next try in {wait:.0f} s")
        return False

    def _take(self, feed: Feed, snap: Snapshot, now: float) -> None:
        with self._lock:
            if self.used is not None and self.used is not feed:
                self.tracks.tracks.clear()        # another feed: its own ages and glitches, no trail spikes
            self.tracks.update(snap)
            view = self._view()
        with self._fresh:
            self.used = feed
            self.view = view
            self.st.update(at=snap.now, fetched=now, error="", fails=0, next=now + feed.poll_s, seq=self.st["seq"] + 1)
            self._fresh.notify_all()

    # -- what the pages get ---------------------------------------------------------------------------------------------

    def _view(self) -> list[dict]:
        now = self.tracks.now or 0.0
        out = []
        live = set()
        for t in self.tracks.live():
            ac = t.plane
            live.add(ac.hex)
            ph = self.memory.phase(ac, now)
            emerg = ac.emergency_text
            if ph.kind not in SHOWN and not emerg:
                continue
            reg = self.registry.lookup(ac.hex) if not ac.hex.startswith("~") else None
            m = AIRLINE_CALLSIGN.match(ac.flight or "")
            model = " ".join(x for x in ((reg or {}).get("make", ""), (reg or {}).get("model", "")) if x)
            kind = "helicopter" if ac.category == "A7" else (reg or {}).get("kind", "")
            trail = []
            if ph.kind in (ARRIVING, DEPARTING) or emerg:
                pts = list(t.trail)[-TRAIL_POINTS:]
                trail = [[round(lon, 4), round(lat, 4)] for _, lat, lon in pts]
            out.append({
                "hex": ac.hex, "cs": ac.flight or ac.reg or (reg or {}).get("n", ""), "flight": ac.flight,
                "reg": ac.reg or (reg or {}).get("n", ""), "reg_src": "feed" if ac.reg else ("faa" if reg else ""),
                "type": ac.type, "model": model or ac.desc, "model_src": "faa" if model else ("feed" if ac.desc else ""),
                "kind": kind, "airline": self.airlines.get(m.group(1), "") if m else "",
                "lat": round(ac.lat, 5), "lon": round(ac.lon, 5), "alt": ac.alt_baro if ac.alt_baro is not None else _r(ac.alt_geom),
                "gs": _r(ac.gs), "trk": _r(ac.track), "vr": _r(ac.vertical_rate), "sq": ac.squawk, "emerg": emerg,
                "phase": ph.kind, "rwy": ph.runway, "appr": ph.approach, "dist": _r(ph.dist_nm, 1),
                "eta": _r(ph.eta_s) if now - t.pos_time <= STALE_S else None,      # never from an old position
                "age": round(max(0.0, now - t.pos_time), 1), "pos": ac.source, "band": ac.band, "trail": trail,
            })
        self.memory.forget(live)
        order = {ARRIVING: 0, DEPARTING: 1, GROUND: 2, LOW: 3}
        out.sort(key=lambda a: (not a["emerg"], order.get(a["phase"], 4), a["dist"] if a["dist"] is not None else 1e9, a["hex"]))
        return out

    def receiver_status(self) -> dict | None:
        """The receiver's sources and the dongle named for ADS-B: plugged in, held by which app. Never opens it."""
        if not self.receiver_on and not self.sdr:
            return None
        out = {"enabled": self.receiver_on, "sdr": self.sdr, "name": self.sdr_name, "present": None, "held_by": None,
               "bands": {}, "note": ""}
        rf = next((f for f in self.feeds if isinstance(f, ReceiverFeed)), None)
        if rf is not None:
            out["bands"] = {b: dict(st) for b, st in rf.bands.items()}
        if self.sdr:
            if self.devices is not None:
                try:
                    out["present"] = any(s == self.sdr for _i, _p, s in self.devices())
                except Exception:
                    out["present"] = None
            if self.holder is not None:
                try:
                    c = self.holder(self.sdr)
                except Exception:
                    c = None
                if c:
                    out["held_by"] = {"app": c.get("app"), "mode": c.get("mode")}
            if out["present"] is False:
                out["note"] = f"The ADS-B dongle {self.sdr_name} is not plugged in"
            elif not out["held_by"] and out["present"]:
                out["note"] = f"The ADS-B dongle {self.sdr_name} is plugged in but no app holds it (nothing decoding)"
        elif self.receiver_on:
            out["note"] = "No dongle named for ADS-B yet ([aircraft.receiver] sdr)"
        return out

    def public(self) -> dict:
        with self._lock:
            view = list(self.view)
            used = self.used
        counts: dict[str, int] = {}
        for a in view:
            counts[a["phase"]] = counts.get(a["phase"], 0) + 1
        counts["emergency"] = sum(1 for a in view if a["emerg"])
        return {"configured": self.configured, "note": self.why_not(), "feed": used.meta() if used else None,
                "feeds": [f.meta() | {"resting": self.resting.get(f.key, (0, ""))[1]} for f in self.feeds],
                "at": self.st["at"], "fetched": self.st["fetched"], "error": self.st["error"], "stale_s": STALE_S,
                "airport": {"icao": ICAO, "lat": ARP[0], "lon": ARP[1]}, "registry": self.registry.public(),
                "receiver": self.receiver_status(), "aircraft": view, "counts": counts}

    def handles(self, path: str) -> bool:
        return path == PATH

    def get(self, path: str, query: str = "", headers=None) -> tuple[int, bytes, str, dict]:
        if path != PATH:
            return 404, b'{"error":"not found"}', JSON_TYPE, {}
        self.ask()
        body = json.dumps(self.public(), separators=(",", ":")).encode()
        # The same data -> the same ETag: the page's next ask (If-None-Match) gets a 304, no body.
        return 200, body, JSON_TYPE, {"ETag": '"' + hashlib.sha1(body).hexdigest()[:16] + '"', "Cache-Control": "no-cache"}
