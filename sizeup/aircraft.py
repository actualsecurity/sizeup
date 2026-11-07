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
                  not drawn; an emergency is drawn whatever its phase. That is one snapshot's reading; what is shown
                  is confirmed over the recent reports (PhaseMemory, #279): the vertical rate, the distance to the
                  field closing or opening, and whether it was on the field lately. Lining up with a runway or an
                  approach never decides arriving vs departing alone (a departure off 34 shares the 34 final's
                  heading), and when the signals disagree it is "near" (near HPN), not a guess.
  the approaches  shortfinal's Procedures.match() on the FAA CIFP paths already in data/airport/
                  khpn_procedures.geojson (finals and transitions, incl. the curved RNAV (RNP) Z lineups over
                  Port Chester): "ILS 34", "RNAV (RNP) Z 34" ...
  the track store the glitch filter (a position jump faster than max(300 kt, 2 x speed + 100 kt) is held back
                  until it repeats 3 times), fade and drop by the feed's own clock, and its trails (a point every
                  60 m or 15 s, a parked one not stacked) for every aircraft drawn, kept short here: TRAIL_S.
  the alerts      squawk 7500 / 7600 / 7700 and the aircraft's own emergency status: always shown, first.
  the airlines    data/airport/airlines.csv: FAA JO 7340.2 designators (public domain, shortfinal's
                  tools/fetch_airlines.py): JBU123 -> "JetBlue Airways".

Left in shortfinal: ATC audio and speech-to-text, callsign attribution, adsb.im routes (community data), the
"window" view, ntfy pushes, the replay/recorder. (Its readsb supervisor came over for the station's decoder in #235:
sizeup/radio/adsbd.py.)

Feeds, behind one interface (Feed.read() -> Snapshot). User, 2026-10-14 (#161): "adsb.lol as primary or adsb.fi
whichever gives the most data, and then keep that data, but poll between the two as needed if one loses or is rate
limited, this is personal non commercial use, we are not charging for this"; and "primary local SDR on ADSB,
secondary the above sources". So every feed's latest answer is kept and combined (combine(): the union by ICAO
address, the freshest position per aircraft, our receiver's position first for any aircraft it hears):

  receiver  our own ADS-B receiver (#235): the dongle named for ADS-B ([aircraft.receiver] sdr, or Settings > Radios)
            behind a 1090 MHz filter and an LNA; on the station its own service runs readsb on it
            (sizeup/radio/adsbd.py: auto gain, bias-tee only when set) and the station forwards what it hears to the
            hub (sizeup/cloud/aircraftlink.py -> StationFeed, below). All-in-one, ReceiverFeed reads readsb / dump1090's
            aircraft.json (1090 MHz) and dump978-fa's (978 MHz UAT), each a file path or an http(s) URL, merged by ICAO
            address, every RECEIVER_POLL_S. Its position wins for an aircraft while that position is fresh
            (RECEIVER_LOST_S; user, #235: "default to pull from live ADSB unless we lose the plane and ADSB.lol or
            another source still has the plane"), and while the receiver is healthy (an answer within RECEIVER_KEEP_S);
            stale or down, the APIs carry everything. This module never starts a decoder or opens a dongle; the named
            dongle is kept out of sizeup's radios (sizeup/app.py), and the layer says whether it is plugged in and who
            holds it (the shared claims, ~/.local/state/rtl-claims, sizeup/radio/claims.py).
            Details by ICAO address: the API feeds' registration, type and owner / operator are kept (DETAILS_KEEP_S)
            and attached to a plane only our receiver hears (Plane.details says which feed); the operator is shown
            only for a company or a government by the FAA registry's type of registrant (operator_for()).
  adsblol   adsb.lol's point query (https://api.adsb.lol/v2/point/<lat>/<lon>/<nm>): keyless, the data under the
            Open Database License (ODbL 1.0): attribution (the map corner and the box name it and the licence).
  adsbfi    adsb.fi's open data API, same JSON: "personal, non-commercial use only", attribution. The user decided
            (2026-10-14) that this is personal, non-commercial use: the department charges nothing for it. On.
  The two APIs: the PRIMARY is whichever has been giving more aircraft with a fresh position (a running average,
  SCORE_ALPHA), asked every PRIMARY_S while someone looks; the other every SECONDARY_S (its aircraft join the
  union and its count stays known). A feed that errors, times out or answers 429 rests (its Retry-After when it
  sends one, else 5 s doubling to 5 min) and the other becomes the primary; it comes back on its first good answer.
  Both stay well inside their limits (adsb.fi: 1 request a second; adsb.lol: by load, 5 s was fine for shortfinal).
  (OpenSky) not used: its licence needs a written agreement for live operational use, its credits are far too
            few for a 5 s refresh, and it adds nothing over these.

The emergency watch: a squawk 7500 / 7600 / 7700, or the aircraft's own emergency status, inside RADIUS_NM of HPN
below WATCH_CEILING_FT, from a message no older than STALE_S, raises one alarm and one airport pre-alert in the queue
(sizeup/cad/airport_alerts.py, source "adsb", #235: with everything known about the aircraft; the same item for the same emergency)
(sizeup/cad/alarms.py, its push
to the alerts topic, [alerts] ntfy_topic, when one is set) per aircraft per emergency, cleared when it has not
been seen for CLEAR_HOLD_S (a flicker doesn't re-alarm), not raised again within REALARM_S, at most MAX_ALARMS an hour. So the
alarm works while nobody looks at the map, the primary feed (and the receiver) are read every WATCH_S then.

Types: the feed's ICAO type designator ("t": E75L) when it has one; the make and model from the FAA Releasable
Aircraft Database (public domain, registry.faa.gov/database/ReleasableAircraft.zip) by the aircraft's ICAO
address, for US aircraft: Registry, fetched by the hub weekly once someone has looked at the layer, kept in
SQLite (aircraft/faa_registry.sqlite in the state folder). Only the N-number, the make / model, the year and the
kind are kept: never the owner's name or address. Nothing is guessed: no type, no registration -> none shown.

Nothing fake: positions are the feed's (ADS-B, MLAT or TIS-B as it says), and so are the trails (only the fixes
the track store accepted). Each aircraft carries the age of its position at the feed's time ("at") and how fast it
is turning ("tr", deg/s: the feed's track_rate, else from its last two fixes, clamped to a standard-rate turn).
Between fixes the page moves the mark on along its track and speed, bent by that turn, for at most 10 s after the
fix and never once it is stale (shortfinal's dead reckoning, web/static/app.js positionNow; motion.js), and meets
each new fix by drawing it a little slower or faster, never backwards (glide.js, #179), unless it is far off. Anything older than STALE_S is drawn hollow, at its last real position,
fading until the hub drops it after DROP_S. The ETA is distance / ground speed, labelled as such. The phase is our
reading of the track (shortfinal's rules), never ATC's word.

Traffic (#146 kept the state push tiny): aircraft never go into the state push. The page asks GET
/api/cad/aircraft every 2 s only while a map has the layer on and the page is visible (the feeds are still read at
their own rates: the ask only shortens the wait for a read already made); the hub reads the feeds at the rates above
while asked (DEMAND_S), else only the emergency watch's; every answer has an ETag (304 when unchanged).

HTTP (sizeup/web/server.py; any signed-in person, GET only):
    GET /api/cad/aircraft   {configured, note, feed: the primary {key, label, link, licence, terms, credit} | null,
                            feeds: [{... role, n, fresh, ok, at, shown, resting, healthy}], credit, at (the newest feed
                            time), fetched, error, stale_s, drop_s, airport, registry, receiver,
                            aircraft: [{hex, cs, flight, reg, reg_src, type, model, model_src, kind, cat, airline, lat, lon,
                            alt, gs, trk, tr, vr, sq, emerg, phase, rwy, appr, dist, eta, age, pos, band, src, trail}],
                            counts, atc: who talked on HPN Tower (#252, sizeup/atc_talkers.py map_payload) | null}
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
import traceback
import zipfile
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sizeup import USER_AGENT as UA
from sizeup import tz

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
RECEIVER_POLL_S = 2.0         # our receiver's files: cheap, local (the API feeds' rates: PRIMARY_S, SECONDARY_S below)
DEMAND_S = 60.0               # a viewer's ask keeps the feed coming this long
WAIT_FIRST_S = 5.0            # an ask with nothing fresh waits this long for the read it wakes
STALE_S = 15.0                # a position older than this is drawn hollow, with its age
DROP_S = 60.0                 # ... and gone after this (shortfinal's drop_after_s)
FEED_STALE_S = 30.0           # a receiver file whose own clock is older than this: its decoder has stopped
BACKOFF_S = (5.0, 300.0)      # a feed that fails: 5 s doubling to 5 min (a 429: its Retry-After when it sends one)
TIMEOUT_S = 10.0
MAX_FEED_BYTES = 4 * 1024 * 1024
REGISTRY_EVERY_S = 7 * 86400.0
REGISTRY_BACKOFF_S = (600.0, 86400.0)
MAX_REGISTRY_BYTES = 200 * 1024 * 1024
TRAIL_S = 90.0                # trail kept and sent (feed seconds): short (shortfinal keeps 10 min; this map is busier)
TRAIL_SPACING_M = 60.0        # a trail point after moving this far (shortfinal's TRAIL_SPACING_M) ...
TRAIL_MAX_GAP_S = 15.0        # ... or after this long, so slow movers still get points (shortfinal's TRAIL_MAX_GAP_S)
TRAIL_POINTS = 30             # per aircraft per answer, every one drawn; more are thinned, the newest kept
TURN_GAP_S = (1.0, 20.0)      # a turn rate from two fixes this far apart (closer: noise; further: it may have turned back)
MAX_TURN_DPS = 3.0            # a standard-rate turn; a sharper reading is clamped to it
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


def finite(v) -> float | None:
    """v as a float when it is a finite number, else None. An int too big for a float (JSON allows any number of
    digits: 1e999 written out) is None, never an OverflowError: math.isfinite(int) and float(int) raise one (#269)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _num(d: dict, key: str) -> float | None:
    return finite(d.get(key))


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
    track_rate: float | None = None   # deg/s, + right: the feed's own, when it sends one (readsb "track_rate")
    source: str = ""                  # how the position was found: adsb, mlat, tisb, adsr ...
    band: str = ""                    # our receiver: "1090" or "978"
    feed: str = ""                    # the feed the position came from: receiver, adsblol, adsbfi (combine())
    owner_op: str = ""                # adsb.lol / adsb.fi "ownOp": the registered owner or operator (shown only for a
                                      # company or government, never a private person: operator_for())
    year: str = ""                    # the feeds' "year" (built)
    rssi: float | None = None         # our receiver: the signal, dBFS
    details: str = ""                 # where reg / type / owner came from when not this record: an API feed's key

    @classmethod
    def from_json(cls, d: dict, band: str = "") -> Plane:
        alt = d.get("alt_baro")
        if alt != "ground":
            alt = finite(alt)
            alt = int(alt) if alt is not None and -2000 <= alt <= 200000 else None
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
                   sel_heading=_num(d, "nav_heading"), track_rate=_num(d, "track_rate"), source=src or ("adsb" if band else ""), band=band,
                   owner_op=str(d.get("ownOp") or "").strip()[:80], year=str(d.get("year") or "").strip()[:4],
                   rssi=_num(d, "rssi"))

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
        now, records = (finite(data.get("now")) or 0.0) / 1000.0, data.get("ac") or []
    elif "aircraft" in data:
        now, records = finite(data.get("now")) or 0.0, data.get("aircraft") or []
    else:
        raise ValueError("no aircraft list (ac / aircraft)")
    if not now or not isinstance(records, list):
        raise ValueError("no time (now) or no aircraft list")
    planes = []
    for r in records:
        if not isinstance(r, dict) or not r.get("hex"):
            continue
        try:
            planes.append(Plane.from_json(r, band))
        except (TypeError, ValueError, OverflowError, AttributeError):   # one odd record: that aircraft only (#269)
            continue
    return Snapshot(now, tuple(planes))


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
NEAR = "near"                  # by HPN, arriving or departing not clear from its track (#279: PhaseMemory)
SHOWN = (ARRIVING, DEPARTING, NEAR, GROUND, LOW)      # drawn; an emergency is drawn whatever its phase
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
    """One snapshot's reading, shortfinal's rules unchanged. PhaseMemory.phase() confirms it from the recent reports."""
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


# ---- beyond the map: HPN's traffic on NY Approach (#260) -------------------------------------------------------------
# While an Approach channel is heard (AircraftLayer.widen: sizeup/atc_scope.py APPROACH_NM, 40 NM), the feeds are asked
# for that much and the aircraft the map doesn't draw that may be talking to NY Approach about HPN are kept apart
# (AircraftLayer.wide, never drawn, never in the emergency watch): inbound (pointed at HPN, descending, low and slow
# enough to be landing here) and outbound (seen departing HPN lately, or close in, climbing away from it).
INBOUND, OUTBOUND = "inbound", "outbound"
WIDE_HOLD_S = 30 * 60.0        # the feeds stay widened this long after an Approach transmission
WIDE_CEILING_FT = 17000.0      # N90's airspace (and HPN's arrivals and departures in it): under this
WIDE_POINTED_DEG = 40.0        # inbound: its track within this of the bearing to HPN
WIDE_AWAY_DEG = 60.0           # outbound: its track within this of the bearing away from HPN
WIDE_CLOSE_NM = 12.0           # outbound with no departure seen: this close and climbing away
WIDE_MAX_KT = 320.0            # inbound: slower than this (under 10,000 ft it is 250 kt anyway)


def approach_phase(ac: Plane, departed_at: float | None, now: float, radius_nm: float) -> str:
    """inbound | outbound | '' for an aircraft the map doesn't draw, out to radius_nm (the module's #260 notes)."""
    if not ac.has_position or ac.alt_baro == "ground":
        return ""
    d = nm(ARP[0], ARP[1], ac.lat, ac.lon)
    alt, trk, vr = ac.altitude_ft, ac.track, ac.vertical_rate
    if d > radius_nm or trk is None or (alt is not None and alt >= WIDE_CEILING_FT):
        return ""
    to_field = bearing(ac.lat, ac.lon, ARP[0], ARP[1])
    agl = None if alt is None else alt - ELEV_FT
    if vr is not None and vr < -200 and abs(wrap180(trk - to_field)) < WIDE_POINTED_DEG \
            and (agl is None or agl < GLIDE_FT_PER_NM * d + 4000) and (ac.gs is None or ac.gs < WIDE_MAX_KT):
        return INBOUND
    away = abs(wrap180(trk - (to_field + 180) % 360)) < WIDE_AWAY_DEG
    departed = departed_at is not None and now - departed_at < DEPARTED_KEEP_S
    if away and (vr is None or vr > 0) and departed:
        return OUTBOUND
    if away and vr is not None and vr > 300 and d <= WIDE_CLOSE_NM:
        return OUTBOUND
    return ""


PHASE_HOLD_S = 20.0           # keep an arriving reading through brief ambiguous updates (not while climbing or opening)
DEPARTED_KEEP_S = 15 * 60.0   # an HPN departure stays "departing" this long ...
DEPARTED_MAX_FT = 18000.0     # ... while below this and inside the radius
INBOUND_KEEP_S = 10 * 60.0    # seen arriving within this long and not landed since ...
MANEUVER_NM = 12.0            # ... still this close ...
MANEUVER_MAX_AGL = 5000.0     # ... this low ...
MANEUVER_MAX_KT = 210.0       # ... and this slow: an arrival being vectored or going around (near, unless closing in)
# Which way it is going (#279): a departure off 34 shares the runway's heading with an arrival on the 34 final, so the
# runway or approach a track lines up with never says which on its own. These do:
VS_FPM = 300.0                # climbing past +this, descending past -this; level between (none reported: not known)
TREND_S = (15.0, 90.0)        # the distance to the field now against a report this far back (the latest such) ...
TREND_NM = 0.15               # ... closer by more than this: closing; further: opening; else steady or not known
FIELD_NM = 1.0                # on the field: within this of HPN's reference point and ...
FIELD_AGL_FT = 150.0          # ... this low (or reporting ground): a takeoff roll, a landing, a touch-and-go
FIELD_RECENT_S = 5 * 60.0     # on the field this recently: never inbound (it took off, or it is in the pattern)
HISTORY_S = 120.0             # distances to the field kept per aircraft for the trend


class PhaseMemory:
    """What each aircraft is doing at HPN from its recent reports, not one snapshot (#279; shortfinal core/state.py
    _phase(), without its route lookups, then reworked). classify() gives the snapshot's reading; this confirms it:

      arriving   the reading says arriving (a published approach, lined up on a runway end, or inbound and descending)
                 and it is descending or closing on the field, not climbing, not opening, and not on the field in the
                 last FIELD_RECENT_S. An arrival seen lately keeps arriving while it is level or descending and closing.
      departing  on the field lately (the ground at HPN, or low on it: the takeoff roll, a touch-and-go) and climbing
                 or not closing; or the reading says departing (close in, low, climbing away) and it is not closing and
                 was not arriving lately; then it stays departing while it leaves the area, until it descends or closes.
      near       the signals disagree (an arrival reading while climbing or opening, a departure reading while
                 closing, a go-around, the pattern): "near HPN", never a guess.
    """

    def __init__(self, procedures: Procedures | None, ceiling_ft: float = CEILING_FT, radius_nm: float = RADIUS_NM):
        self.procedures, self.ceiling_ft, self.radius_nm = procedures, ceiling_ft, radius_nm
        self._held: dict[str, tuple[Phase, float]] = {}
        self._departed: dict[str, float] = {}
        self._inbound: dict[str, float] = {}
        self._on_field: dict[str, float] = {}
        self._history: dict[str, deque] = {}

    def forget(self, live: set[str]) -> None:
        for memory in (self._held, self._departed, self._inbound, self._on_field, self._history):
            for h in [h for h in memory if h not in live]:
                del memory[h]

    def _trend(self, h: str, now: float, d: float, trail=()) -> str:
        """closing | opening | '': the distance to the field now against the latest report at least TREND_S[0] and at
        most TREND_S[1] back (this memory's, else the track's trail, which carries only positions)."""
        hist = self._history.setdefault(h, deque())
        if not hist or now > hist[-1][0]:
            hist.append((now, d))
        while hist and now - hist[0][0] > HISTORY_S:
            hist.popleft()
        then = [(t, x) for t, x in hist if TREND_S[0] <= now - t <= TREND_S[1]]
        if not then:
            then = [(t, nm(ARP[0], ARP[1], lat, lon)) for t, lat, lon in trail if TREND_S[0] <= now - t <= TREND_S[1]]
        if not then:
            return ""
        delta = d - max(then)[1]
        return "closing" if delta < -TREND_NM else "opening" if delta > TREND_NM else ""

    def phase(self, ac: Plane, now: float, trail=()) -> Phase:
        p = classify(ac, self.procedures, self.ceiling_ft, self.radius_nm)
        h = ac.hex
        if p.kind == OTHER_GROUND or not ac.has_position:
            return p
        fd = nm(ARP[0], ARP[1], ac.lat, ac.lon)
        trend = self._trend(h, now, fd, trail)
        if p.kind == GROUND:
            self._inbound.pop(h, None)          # landed: whatever it does next is a departure
            self._held.pop(h, None)
            self._on_field[h] = now
            return p
        alt, vr = ac.altitude_ft, ac.vertical_rate
        agl = None if alt is None else alt - ELEV_FT
        climbing = vr is not None and vr > VS_FPM
        descending = vr is not None and vr < -VS_FPM
        closing, opening = trend == "closing", trend == "opening"
        on_field_now = agl is not None and agl < FIELD_AGL_FT and fd <= FIELD_NM
        from_field = now - self._on_field.get(h, -1e9) < FIELD_RECENT_S
        was_inbound = now - self._inbound.get(h, -1e9) < INBOUND_KEEP_S
        near = Phase(NEAR, dist_nm=fd)
        if p.kind == ARRIVING and not climbing and not opening and not from_field and (descending or closing):
            self._held[h] = (p, now)
            self._departed.pop(h, None)
            self._inbound[h] = now
            return p                            # (over the threshold too: on the field once it stops closing)
        if on_field_now:                        # the takeoff roll, a landing roll, a touch-and-go: on the runway
            self._on_field[h] = now
            self._inbound.pop(h, None)
            self._held.pop(h, None)
            return Phase(GROUND, dist_nm=fd)
        in_area = fd <= self.radius_nm and (alt is None or alt < DEPARTED_MAX_FT)
        returning = (closing and not climbing) or (descending and not opening)   # climbing out over the runway closes too
        if from_field and in_area:
            if returning:
                return near                     # back toward the field: the pattern, or coming back
            self._departed[h] = now
            return Phase(DEPARTING, dist_nm=fd)
        close_in = fd <= MANEUVER_NM and (agl is None or agl < MANEUVER_MAX_AGL) and (ac.gs is None or ac.gs <= MANEUVER_MAX_KT)
        if was_inbound:
            if not climbing and not opening and closing:
                self._inbound[h] = now
                return Phase(ARRIVING, dist_nm=fd, eta_s=_eta(fd, ac.gs))          # vectored, level or descending in
            held = self._held.get(h)
            if held and now - held[1] < PHASE_HOLD_S and not climbing and not opening:
                return Phase(held[0].kind, held[0].runway, p.dist_nm, held[0].eta_s, held[0].approach)
            self._held.pop(h, None)
            if close_in or p.kind in (ARRIVING, DEPARTING):
                return near                     # a go-around, a missed approach, vectors away
            return p
        if p.kind == DEPARTING:
            if closing:
                return near
            self._departed[h] = now
            return p
        if p.kind == ARRIVING:
            return near                         # lined up, but level and not closing yet, or climbing, or opening
        if now - self._departed.get(h, -1e9) < DEPARTED_KEEP_S and in_area and p.kind in (LOW, OVERFLIGHT):
            if returning:
                return near
            return Phase(DEPARTING, dist_nm=fd)
        return p


# ---- the track store (shortfinal common/models.py TrackStore, by the feed's clock) ---------------------------------------


@dataclass
class Track:
    plane: Plane
    pos_time: float | None
    msg_time: float
    trail: deque = field(default_factory=deque)        # (feed time, lat, lon), oldest first
    jumps: int = 0
    turn: float | None = None                           # deg/s (+ right): the feed's, else from its last two fixes


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
                t.turn = self._turn(None, None, ac, pos_time)
            else:
                if pos_time is not None and t.pos_time is not None and pos_time < t.pos_time:
                    ac, pos_time = replace(ac, lat=t.plane.lat, lon=t.plane.lon, source=t.plane.source, band=t.plane.band, feed=t.plane.feed), None   # an older fix than ours
                elif pos_time is not None and self._jumped(t, ac, pos_time):
                    ac, pos_time = replace(ac, lat=t.plane.lat, lon=t.plane.lon, source=t.plane.source, band=t.plane.band, feed=t.plane.feed), None   # a glitch: keep the last good
                if pos_time is not None and pos_time != t.pos_time:      # a new fix (none across a jump: its trail is gone)
                    t.turn = self._turn(t.plane, t.pos_time, ac, pos_time) if t.trail else self._turn(None, None, ac, pos_time)
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
    def _turn(prev: Plane | None, prev_time: float | None, ac: Plane, when: float | None) -> float | None:
        """How fast it is turning, deg/s (+ right): the feed's track_rate, else the change of track between its last
        two fixes. None when neither says (no track, the fixes too close or too far apart in time). The page bends
        its short prediction by it (aircraft.js); it is never shown as a number."""
        if ac.track_rate is not None:
            return max(-MAX_TURN_DPS, min(MAX_TURN_DPS, ac.track_rate))
        if prev is None or prev_time is None or when is None or prev.track is None or ac.track is None:
            return None
        dt = when - prev_time
        if not TURN_GAP_S[0] <= dt <= TURN_GAP_S[1]:
            return None
        return max(-MAX_TURN_DPS, min(MAX_TURN_DPS, wrap180(ac.track - prev.track) / dt))

    @staticmethod
    def _trail(t: Track, when: float, ac: Plane) -> None:
        """shortfinal's TrackStore._extend_trail: a point after TRAIL_SPACING_M or TRAIL_MAX_GAP_S, a parked one
        refreshed in place (no stack of points), the oldest let go after TRAIL_S."""
        trail = t.trail
        last = trail[-1] if trail else None
        moved = metres(last[1], last[2], ac.lat, ac.lon) if last else None
        if last is None or (moved >= 1.0 and (moved >= TRAIL_SPACING_M or when - last[0] >= TRAIL_MAX_GAP_S)):
            trail.append((when, ac.lat, ac.lon))
        elif moved < 1.0 and when - last[0] >= TRAIL_MAX_GAP_S:
            trail[-1] = (when, last[1], last[2])
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


# The FAA's TYPE REGISTRANT: kept (never the name) so an operator name from a feed is shown only for a company or a
# government, never for a private person (#161's rule, #235): 1 individual, 2 partnership, 4 co-owned, 9 non-citizen
# co-owned are people; these are not.
COMPANY_REGISTRANTS = {"3": "corporation", "5": "government", "7": "LLC", "8": "non-citizen corporation"}


def build_registry(blob: bytes, con: sqlite3.Connection) -> int:
    """MASTER.txt and ACFTREF.txt from ReleasableAircraft.zip into `con`: the N-number, model code, year and the type of
    registrant (individual, corporation ...: COMPANY_REGISTRANTS) by ICAO address; make, model, kind and engines by model
    code. Never a name or an address. Returns the aircraft read."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = {n.upper(): n for n in z.namelist()}
    if "MASTER.TXT" not in names or "ACFTREF.TXT" not in names:
        raise ValueError("not the FAA registry (no MASTER.txt / ACFTREF.txt)")
    con.executescript("DROP TABLE IF EXISTS aircraft; DROP TABLE IF EXISTS models;"
                      "CREATE TABLE aircraft (hex TEXT PRIMARY KEY, n TEXT, mdl TEXT, year TEXT, kind TEXT, eng TEXT, "
                      "reg_type TEXT);"
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
                      d.get("TYPE AIRCRAFT", ""), d.get("TYPE ENGINE", ""), d.get("TYPE REGISTRANT", "")[:1]))
        n += 1
        if len(batch) >= 5000:
            con.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?)", batch)
            batch = []
    con.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?)", batch)
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
            if "reg_type" not in {r[1] for r in con.execute("PRAGMA table_info(aircraft)")}:
                self.st["next"] = 0.0           # built before #235: kept for the types, fetched afresh when next looked at
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
            wait = min(REGISTRY_BACKOFF_S[1], REGISTRY_BACKOFF_S[0] * 2 ** min(self.st["fails"] - 1, 30))
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
        """{n, make, model, year, kind, engines, company} for a US aircraft, else None. company: what kind of company or
        government the registrant is ("LLC", "corporation" ...), "" for a person (or not known): never a name."""
        if not re.fullmatch(r"[0-9a-f]{6}", hex_ or ""):
            return None
        with self._lock:
            if self.con is None:
                return None
            try:
                row = self.con.execute("SELECT a.n, a.year, a.kind, a.eng, m.mfr, m.model, m.kind, m.engines, a.reg_type "
                                       "FROM aircraft a LEFT JOIN models m ON m.code = a.mdl WHERE a.hex = ?",
                                       (hex_,)).fetchone()
            except sqlite3.OperationalError:          # a cache from before #235 (no reg_type): fetched afresh soon
                try:
                    row = self.con.execute("SELECT a.n, a.year, a.kind, a.eng, m.mfr, m.model, m.kind, m.engines, '' "
                                           "FROM aircraft a LEFT JOIN models m ON m.code = a.mdl WHERE a.hex = ?",
                                           (hex_,)).fetchone()
                except sqlite3.Error:
                    return None
            except sqlite3.Error:
                return None
        if not row:
            return None
        n, year, kind, eng, mfr, model, mkind, engines, reg_type = row
        return {"n": n, "make": _title(mfr or ""), "model": (model or "").strip(), "year": year or "",
                "kind": KINDS.get((kind or mkind or "").strip(), ""), "engine": ENGINES.get((eng or "").strip(), ""),
                "engines": engines or 0, "company": COMPANY_REGISTRANTS.get((reg_type or "").strip(), "")}

    def public(self) -> dict:
        return {"at": self.st["at"], "n": self.st["n"], "error": self.st["error"], "credit": REGISTRY_CREDIT if self.st["at"] else ""}


# ---- the feeds -----------------------------------------------------------------------------------------------------------


class RateLimited(Exception):
    """HTTP 429. retry_after_s: the server's Retry-After, None when it sent none (then we back off exponentially)."""

    def __init__(self, retry_after_s: float | None):
        super().__init__("rate limited (HTTP 429)" + (f", Retry-After {retry_after_s:.0f} s" if retry_after_s else ""))
        self.retry_after_s = retry_after_s


class Feed:
    """One source of aircraft: read() -> Snapshot, or raises. st: its schedule and health (AircraftLayer). local: ours
    (a file here, or the station's forwarded copy): cheap to read, never rested long."""
    key = ""
    st: dict = {}
    local = False

    def read(self) -> Snapshot:
        raise NotImplementedError

    def meta(self) -> dict:
        return {"key": self.key, **FEEDS[self.key]}


class ApiFeed(Feed):
    """adsb.lol or adsb.fi: a point query around HPN (same readsb-like JSON)."""

    def __init__(self, key: str, url_template: str, radius_nm: float, fetch: Callable):
        self.key = key
        self.template, self.radius_nm = url_template, float(radius_nm)   # radius_nm: the layer widens it (#260)
        self.fetch = fetch

    @property
    def url(self) -> str:
        return self.template.format(lat=f"{ARP[0]:.4f}", lon=f"{ARP[1]:.4f}", radius=int(math.ceil(self.radius_nm)))

    def read(self) -> Snapshot:
        try:
            code, headers, body = self.fetch(self.url, TIMEOUT_S, MAX_FEED_BYTES, {"Accept": "application/json"})
        except HTTPError as e:
            if e.code == 429:
                try:
                    retry = float((e.headers or {}).get("Retry-After", "") or 0)
                except (TypeError, ValueError):
                    retry = 0.0
                raise RateLimited(retry if retry > 0 else None) from e
            raise
        if code == 429:
            raise RateLimited(None)
        if code != 200:
            raise HTTPError(self.url, code, f"HTTP {code}", None, None)
        return parse_snapshot(json.loads(body))


class ReceiverFeed(Feed):
    """Our receiver: readsb / dump1090 (1090 MHz) and dump978-fa (978 MHz UAT) aircraft.json, each a path or an
    http(s) URL, merged by ICAO address. Read only: this never starts a decoder or opens a dongle (the station's own
    decoder is its own service: sizeup/radio/adsbd.py)."""
    key = "receiver"
    local = True

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


def _thin(points: tuple, limit: int) -> tuple:
    """At most `limit` points, evenly thinned, always keeping the newest (shortfinal core/state.py _thin)."""
    if len(points) <= limit:
        return points
    step = len(points) / (limit - 1)
    return tuple(points[int(i * step)] for i in range(limit - 1)) + (points[-1],)


def _r(x: float | None, nd: int = 0):
    if x is None:
        return None
    return round(x, nd) if nd else int(round(x))


COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def where_from_hpn(lat: float, lon: float) -> str:
    """"1.4 nm SE of HPN"; "over the field" inside a quarter mile."""
    d = nm(ARP[0], ARP[1], lat, lon)
    if d < 0.25:
        return "over the field at HPN"
    return f"{d:.1f} nm {COMPASS[int((bearing(ARP[0], ARP[1], lat, lon) + 22.5) // 45) % 8]} of HPN"


DETAIL_FIELDS = ("flight", "squawk", "category", "reg", "type", "desc", "owner_op", "year")


def runway_relation(lat: float, lon: float) -> str:
    """"0.6 nm from the runway 34 end": the nearest runway end (FAA's threshold points, RUNWAY_ENDS)."""
    best = min(RUNWAY_ENDS, key=lambda r: nm(r[2], r[3], lat, lon))
    d = nm(best[2], best[3], lat, lon)
    return f"at the runway {best[1]} end" if d < 0.1 else f"{d:.1f} nm from the runway {best[1]} end"


def combine(snaps: list[tuple[str, Snapshot]], prefer: str = "receiver", lost_s: float | None = None) -> Snapshot:
    """The union of every feed's latest snapshot, one record per ICAO address: `prefer`'s (our receiver's) position
    for any aircraft it hears with a position no older than lost_s (RECEIVER_LOST_S; user, #235: "default to pull from
    live ADSB unless we lose the plane and ADSB.lol or another source still has the plane"), else the freshest
    position any feed has; a field only another feed has is kept (its details: the feed it came from). Each record
    says which feed its position came from (Plane.feed). Ages are rebased onto the newest clock."""
    lost_s = RECEIVER_LOST_S if lost_s is None else lost_s
    if not snaps:
        raise ValueError("no snapshot")
    now = max(s.now for _, s in snaps)
    best: dict[str, tuple[tuple, Plane]] = {}
    for key, snap in snaps:
        for pl in snap.planes:
            shift = now - snap.now
            pl = replace(pl, seen=pl.seen + shift, seen_pos=None if pl.seen_pos is None else pl.seen_pos + shift, feed=key)
            if pl.has_position:
                age = pl.seen_pos if pl.seen_pos is not None else pl.seen
                when = now - age
                rank = (1, 1 if key == prefer and age <= lost_s else 0, when)   # positioned; ours while fresh; freshest
            else:
                rank = (0, 1 if key == prefer else 0, now - pl.seen)
            have = best.get(pl.hex)
            if have is None:
                best[pl.hex] = (rank, pl)
                continue
            keep, other = (pl, have[1]) if rank > have[0] else (have[1], pl)
            fill = {k: getattr(other, k) for k in DETAIL_FIELDS if not getattr(keep, k) and getattr(other, k)}
            if fill.keys() & {"reg", "type", "desc", "owner_op"} and not keep.details:
                fill["details"] = other.feed
            keep = replace(keep, **fill)
            best[pl.hex] = (max(rank, have[0]), keep)
    return Snapshot(now, tuple(p for _, p in best.values()))


def operator_for(ac: Plane, reg: dict | None, airlines: dict[str, str]) -> tuple[str, str]:
    """(who operates it, where that came from): the airline its callsign names (FAA JO 7340.2: "airline"), else the
    feeds' owner / operator (ownOp: the feed's key) only when the FAA registry lists the registrant as a company or a
    government (COMPANY_REGISTRANTS); a private person's name is never shown (#161, #235). ("", "") otherwise."""
    m = AIRLINE_CALLSIGN.match(ac.flight or "")
    if m and airlines.get(m.group(1)):
        return airlines[m.group(1)], "airline"
    if ac.owner_op and reg and reg.get("company"):
        return _company(ac.owner_op), ac.details or ac.feed or "feed"
    return "", ""


ABBREVIATIONS = {"LLC", "LP", "LLP", "PLLC", "USA", "US", "NY", "NJ", "CT", "FAA", "NYPD", "FDNY", "NYS", "II", "III", "IV"}


def _company(s: str) -> str:
    """HEAVY METAL AIR LLC -> Heavy Metal Air LLC (the feeds write registrants in capitals)."""
    if s != s.upper():
        return s.strip()
    return " ".join(w if w in ABBREVIATIONS or any(c.isdigit() for c in w) else w.title() for w in s.split())


# ---- our receiver on the station, forwarded to the hub (#235) ------------------------------------------------------------
#
# The station reads its decoder's aircraft.json every second (sizeup/cloud/aircraftlink.py) and POSTs the hub
# /api/station/aircraft (the station token) with what changed: one compact list per aircraft (WIRE, ages in seconds
# before the post's own time, so the two clocks needn't agree), the ICAO addresses gone since, and a full list after
# anything went wrong. The hub keeps them (StationFeed) as the "receiver" feed: the same rules as a local receiver.

WIRE = ("hex", "lat", "lon", "alt", "gs", "trk", "vr", "sq", "em", "fl", "sp", "s", "rssi", "cat", "pos", "band", "tr")
WIRE_MAX = 300                 # aircraft per post (and kept on the hub): the nearest to HPN first
FORWARD_FAST_S = 1.0           # the station posts at most this often while someone looks (or an emergency is on) ...
FORWARD_SLOW_S = 5.0           # ... else this often (the emergency watch; an emergency squawk goes up at once)
FORWARD_AREA = 1.5             # x the radius around HPN: what the hub's track store keeps (Tracks)
FORWARD_CEILING_FT = 20000.0   # above this: overflights the map never draws and the watch never alarms on
HEX = re.compile(r"~?[0-9a-f]{6}")


def _rnd(x, nd):
    return None if x is None else round(x, nd)


def wire(p: Plane) -> list:
    """One aircraft as the station sends it (WIRE's order): position to 5 decimals (~1 m), ages to 0.1 s."""
    return [p.hex, _rnd(p.lat, 5), _rnd(p.lon, 5), p.alt_baro if p.alt_baro is not None else _r(p.alt_geom),
            _rnd(p.gs, 1), _rnd(p.track, 1), _r(p.vertical_rate), p.squawk, p.emergency if p.emergency != "none" else "",
            p.flight, _rnd(p.seen_pos, 1), _rnd(p.seen, 1), _rnd(p.rssi, 1), p.category, p.source, p.band,
            _rnd(p.track_rate, 2)]


def _wnum(v, lo: float, hi: float) -> float | None:
    v = finite(v)
    return v if v is not None and lo <= v <= hi else None


def _wstr(v, n: int) -> str:
    return v.strip()[:n] if isinstance(v, str) else ""


def unwire(rec) -> dict | None:
    """A forwarded aircraft back as fields; None when it isn't one (the hub never trusts the shape)."""
    if not isinstance(rec, list) or len(rec) < 12:
        return None
    rec = rec + [None] * (len(WIRE) - len(rec))
    d = dict(zip(WIRE, rec))
    hx = d["hex"].lower() if isinstance(d["hex"], str) else ""
    if not HEX.fullmatch(hx):
        return None
    alt = d["alt"] if d["alt"] == "ground" else _wnum(d["alt"], -2000, 60000)
    lat, lon = _wnum(d["lat"], -90, 90), _wnum(d["lon"], -180, 180)
    if lat is None or lon is None:
        lat = lon = None
    return {"hex": hx, "lat": lat, "lon": lon, "alt": int(alt) if isinstance(alt, float) else alt,
            "gs": _wnum(d["gs"], 0, 2000), "trk": _wnum(d["trk"], 0, 360), "vr": _wnum(d["vr"], -20000, 20000),
            "sq": _wstr(d["sq"], 4), "em": _wstr(d["em"], 20).lower(), "fl": _wstr(d["fl"], 10),
            "sp": _wnum(d["sp"], 0, 3600), "s": _wnum(d["s"], 0, 3600) or 0.0, "rssi": _wnum(d["rssi"], -100, 10),
            "cat": _wstr(d["cat"], 4), "pos": _wstr(d["pos"], 8), "band": _wstr(d["band"], 4),
            "tr": _wnum(d["tr"], -30, 30)}


class StationFeed(Feed):
    """The hub's copy of our receiver on the station (#235): what the station forwarded, as the "receiver" feed. Its
    aircraft are kept by the hub's own clock (each age taken off the time the post arrived), dropped DROP_S after they
    were last heard; with no post for RECEIVER_KEEP_S it is stale and the APIs carry everything (read() raises)."""
    key = "receiver"
    local = True

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self.recs: dict[str, dict] = {}
        self.epoch, self.seq = "", -1
        self.last: float | None = None             # the last post (hub clock)
        self.posts = self.bytes = self.bad = 0
        self.rx: dict = {}                         # the station's word on its receiver: {sdr, name, state, detail ...}
        self.rate: deque = deque()                 # (when, bytes) of the last minute's posts

    def ingest(self, d: dict, size: int = 0) -> dict:
        """One post: {v, epoch, seq, full, ac: [WIRE lists], gone: [hex], rx?}. {ok, full: send everything next time}."""
        if not isinstance(d, dict) or not isinstance(d.get("ac", []), list) or not isinstance(d.get("gone", []), list):
            raise ValueError("an aircraft post: {epoch, seq, full, ac: [], gone: []}")
        now = self.clock()
        epoch, seq = str(d.get("epoch") or "")[:40], d.get("seq")
        seq = int(seq) if isinstance(seq, int) and not isinstance(seq, bool) else -1
        with self._lock:
            want_full = False
            if d.get("full") is True:
                self.recs = {}
            elif epoch != self.epoch or seq != self.seq + 1:
                want_full = True                   # a post missed (or the hub restarted): apply it, ask for all
            self.epoch, self.seq = epoch, seq
            for raw in d.get("ac", [])[:WIRE_MAX]:
                try:
                    r = unwire(raw)
                except (TypeError, ValueError, OverflowError, AttributeError):   # that aircraft only (#269)
                    r = None
                if r is None:
                    self.bad += 1
                    continue
                r["t_seen"] = now - r["s"]
                r["t_pos"] = None if r["sp"] is None or r["lat"] is None else now - r["sp"]
                self.recs[r["hex"]] = r
            for h in d.get("gone", [])[:WIRE_MAX * 4]:
                if isinstance(h, str):
                    self.recs.pop(h.lower(), None)
            for h in [h for h, r in self.recs.items() if now - r["t_seen"] > DROP_S]:
                del self.recs[h]
            if len(self.recs) > WIRE_MAX:          # never more than a post carries (the nearest kept)
                far = sorted(self.recs, key=lambda h: -nm(ARP[0], ARP[1], self.recs[h]["lat"] or 0, self.recs[h]["lon"] or 0))
                for h in far[:len(self.recs) - WIRE_MAX]:
                    del self.recs[h]
            if isinstance(d.get("rx"), dict):
                self.rx = {k: v for k, v in d["rx"].items() if k in RX_KEYS}
            self.last = now
            self.posts += 1
            self.bytes += size
            self.rate.append((now, size))
            while self.rate and now - self.rate[0][0] > 60:
                self.rate.popleft()
        return {"ok": True, "full": want_full}

    def read(self) -> Snapshot:
        now = self.clock()
        with self._lock:
            if self.last is None:
                raise RuntimeError("receiver: nothing from the station's receiver yet")
            if now - self.last > RECEIVER_KEEP_S:
                raise RuntimeError(f"receiver: no word from the station's receiver for {now - self.last:.0f} s")
            recs = list(self.recs.values())
        planes = []
        for r in recs:
            planes.append(Plane(hex=r["hex"], flight=r["fl"], alt_baro=r["alt"], gs=r["gs"], track=r["trk"],
                                baro_rate=r["vr"], squawk=r["sq"], lat=r["lat"], lon=r["lon"], seen=max(0.0, now - r["t_seen"]),
                                seen_pos=None if r["t_pos"] is None else max(0.0, now - r["t_pos"]),
                                emergency=r["em"], category=r["cat"], track_rate=r["tr"], source=r["pos"] or "adsb",
                                band=r["band"] or "1090", rssi=r["rssi"]))
        return Snapshot(now, tuple(planes))

    def stats(self) -> dict:
        now = self.clock()
        with self._lock:
            per_s = sum(b for t, b in self.rate) / 60.0
            return {"last": self.last, "posts": self.posts, "aircraft": len(self.recs), "bytes": self.bytes,
                    "bytes_per_s": round(per_s, 1), "bad": self.bad, "age_s": None if self.last is None else round(now - self.last, 1)}


RX_KEYS = ("sdr", "name", "state", "detail", "held_by", "present", "messages_per_s", "aircraft", "last_message_at",
           "gain", "bias_tee", "source", "cause", "holder",        # cause, holder: #262 (an older station sends neither)
           "usbfs_mb")                                             # the station's usbfs limit, MB (#269)


# How often each feed is asked (rate limits: adsb.fi's public API allows 1 request a second; adsb.lol limits by load
# and refused 1-second polling in shortfinal's tests). Someone looking at the layer: the primary API feed every
# PRIMARY_S, the other every SECONDARY_S (so its data joins and its count stays known), our receiver every
# RECEIVER_POLL_S. Nobody looking, with the emergency watch on: the primary every WATCH_S, the receiver too.
PRIMARY_S = 5.0
SECONDARY_S = 10.0
WATCH_S = 15.0
SCORE_ALPHA = 0.3                  # the running count of aircraft with a fresh position that picks the primary
RECEIVER_LOG_EVERY_S = 300.0      # our receiver failing the same way: logged once, then at most this often (#262)
ERROR_LOG_EVERY_S = 300.0          # the poll itself failing (a bug): its traceback once per distinct error, then one
                                   # line at most this often while it goes on (#269: one line a second for 25 min)
FEED_KEEP_S = 20.0                 # an API feed's last answer joins the union this long
RECEIVER_KEEP_S = 10.0             # our receiver's: older than this, it is stale and the APIs take over
RECEIVER_LOST_S = 10.0             # an aircraft whose position from our receiver is older than this is "lost" there:
                                   # an API feed's fresher position is used (#235)
RATE_LIMITED_MIN_S = 10.0          # a 429 without Retry-After: at least this, then doubling
DETAILS_KEEP_S = 6 * 3600.0        # an aircraft's registration / type / operator from an API feed, kept by its address
DETAILS_MAX = 5000
PREALERT_UPDATE_S = 15.0           # an open emergency's pre-alert gets its newest altitude / position this often

# The emergency watch (alarms): a squawk 7500 / 7600 / 7700 or the aircraft's own emergency status, inside the
# radius and below WATCH_CEILING_FT (or on the ground), from a message no older than STALE_S.
WATCH_CEILING_FT = 18000.0
CLEAR_HOLD_S = 60.0                # not seen in emergency this long: ended (a flicker in between doesn't re-alarm)
REALARM_S = 30 * 60.0              # the same aircraft and emergency again within this after it ended: no new alarm
MAX_ALARMS = 6                     # at most this many aircraft alarms ...
MAX_ALARMS_S = 3600.0              # ... an hour (more are logged, not raised)


class AircraftLayer:
    def __init__(self, enabled: bool = True, feeds: tuple[str, ...] = ("adsblol", "adsbfi"), radius_nm: float = RADIUS_NM,
                 ceiling_ft: float = CEILING_FT, adsblol_url: str = "", adsbfi_url: str = "", receiver: bool = False,
                 receiver_1090: str = "", receiver_978: str = "", sdr: str = "", sdr_name: str = "", registry: bool = True,
                 registry_url: str = "", cache_dir: str | Path | None = None, fetch: Callable | None = None,
                 devices: Callable[[], list] | None = None, holder: Callable[[str], dict | None] | None = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 airlines: dict[str, str] | None = None, procedures: Procedures | None = None, watch: bool = True,
                 station_feed: bool = False):
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
                elif key == "receiver" and receiver and (receiver_1090 or receiver_978) and not station_feed:
                    self.feeds.append(ReceiverFeed(receiver_1090, receiver_978, self.fetch, clock))
            if station_feed:                     # the hub: our receiver is the station's, forwarded (#235): first
                self.feeds.insert(0, StationFeed(clock))
        for f in self.feeds:
            f.st = {"next": 0.0, "fails": 0, "rest_until": 0.0, "why": "", "ok": None, "at": None, "n": 0, "fresh": 0,
                    "score": None, "snap": None, "reads": 0}
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
        self.view: list[dict] = []
        self.wide: list[dict] = []          # beyond the map, for NY Approach's talkers (#260: approach_phase); never drawn
        self.wide_nm, self.wide_until = 0.0, 0.0
        self.phases: dict[str, Phase] = {}
        self.st = {"at": None, "fetched": None, "error": "", "seq": 0}
        # the emergency watch: on_emergency(event) -> alarm id | None, on_clear(event, alarm id, why) (sizeup/cad/alarms.py)
        self.watch = bool(watch)
        self.on_emergency: Callable[[dict], object] | None = None
        self.on_clear: Callable[[dict, object, str], None] | None = None
        self.emerg: dict[str, dict] = {}            # key (hex:code) -> {event, alarm, since, seen}
        self.ended: dict[str, float] = {}           # key -> when it ended (no re-alarm within REALARM_S)
        self.ended_prealert: dict[str, bool] = {}   # key -> it had a pre-alert (reopened if it comes back in that time)
        self.raised: deque = deque()                # when each aircraft alarm was raised (MAX_ALARMS an hour)
        # its CAD pre-alert (sizeup/cad/airport_alerts.py AircraftPreAlerts, #235; one per emergency, with the alarm): set by the app
        self.prealerts = None
        # this is the CAD of record (not a station forwarding to the hub): only then the emergency watch reads the
        # API feeds while nobody looks (the hub does it; a station doing it too would double the load for nothing)
        self.active: Callable[[], bool] = lambda: True
        # type, registration, operator by ICAO address from the API feeds' records, kept so that an aircraft only our
        # receiver hears (or hears first) still has them (#235): hex -> {reg, type, desc, owner_op, year, feed, at}
        self.details: dict[str, dict] = {}
        # Who is talking on HPN Tower (#252, sizeup/atc_talkers.py), set by the app on the CAD of record: on_view(view, now)
        # gets every new picture (the transmissions are matched against what was near HPN around them), extra() -> {...} |
        # None goes in the answer as "atc" (the "talking" ring, each aircraft's recent transmissions)
        self.on_view: Callable[[list[dict], float], None] | None = None
        self.extra: Callable[[], dict | None] | None = None
        self._errors: dict[tuple, dict] = {}        # _error(): (where, type, file:line) -> {n, at, skipped}

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print, devices: Callable[[], list] | None = None) -> AircraftLayer:
        state = Path(cfg.cad_db).parent if getattr(cfg, "cad_db", "") else None
        from sizeup.radio import claims
        sdr = getattr(cfg, "aircraft_sdr", "")
        hub = getattr(cfg, "cloud_role", "") == "hub"
        return cls(getattr(cfg, "aircraft_enabled", True), tuple(getattr(cfg, "aircraft_feeds", ("adsblol", "adsbfi"))),
                   getattr(cfg, "aircraft_radius_nm", RADIUS_NM), getattr(cfg, "aircraft_ceiling_ft", CEILING_FT),
                   getattr(cfg, "aircraft_adsblol_url", ""), getattr(cfg, "aircraft_adsbfi_url", ""),
                   getattr(cfg, "aircraft_receiver", False), getattr(cfg, "aircraft_receiver_1090", ""),
                   getattr(cfg, "aircraft_receiver_978", ""), sdr, (getattr(cfg, "sdr_names", {}) or {}).get(sdr, sdr),
                   getattr(cfg, "aircraft_registry", True), "", (state / "aircraft") if state else None,
                   devices=None if hub else devices, holder=None if hub else claims.holder, log=log,
                   watch=getattr(cfg, "aircraft_alarms", True), station_feed=hub)

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
            self.log(f"aircraft: {' + '.join(FEEDS[f.key]['label'] for f in self.feeds)}, {self.radius_nm:g} nm around HPN; "
                     f"while the layer is open the primary every {PRIMARY_S:g} s, the other every {SECONDARY_S:g} s"
                     + (f"; emergency watch every {WATCH_S:g} s" if self.watching() else ""))
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # never let one bad answer stop the layer for good
                self._error("poll", e)
            self._wake.wait(1.0)
            self._wake.clear()

    def _error(self, where: str, e: BaseException) -> None:
        """A bug in the poll (not a feed's answer: those rest the feed). The first time an error is seen (its type and
        where it was raised), its traceback; then one line, at most every ERROR_LOG_EVERY_S, with the count (#269)."""
        tb = traceback.extract_tb(e.__traceback__)
        at = f"{Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else "?"
        key = (where, type(e).__name__, at)
        now = self.clock()
        seen = self._errors.get(key)
        if seen is None:
            self._errors[key] = {"n": 1, "at": now, "skipped": 0}
            lines = "".join(traceback.format_exception(type(e), e, e.__traceback__)).rstrip()
            self.log(f"aircraft: {where}: {type(e).__name__}: {e} (at {at}; logged once, then every "
                     f"{ERROR_LOG_EVERY_S / 60:.0f} min)\n{lines}")
            return
        seen["n"] += 1
        if now - seen["at"] >= ERROR_LOG_EVERY_S:
            self.log(f"aircraft: {where}: {type(e).__name__}: {e} (at {at}; {seen['skipped'] + 1} times in the last "
                     f"{(now - seen['at']) / 60:.0f} min, {seen['n']} in all)")
            seen.update(at=now, skipped=0)
        else:
            seen["skipped"] += 1

    def demanded(self) -> bool:
        return self.clock() - self.asked <= DEMAND_S

    def watching(self) -> bool:
        """The emergency watch: reads go on (slower) while nobody looks, so an emergency still raises the alarm. Only on
        the CAD of record (active): a station forwarding to the hub leaves it to the hub."""
        return self.watch and self.on_emergency is not None and self.active()

    def api_feeds(self) -> list[Feed]:
        return [f for f in self.feeds if not f.local]

    def station_feed(self) -> StationFeed | None:
        return next((f for f in self.feeds if isinstance(f, StationFeed)), None)

    def station_ingest(self, d: dict, size: int = 0) -> dict:
        """POST /api/station/aircraft (sizeup/cloud/hub.py): the station's receiver, forwarded. The answer says how
        often to post: FORWARD_FAST_S while someone looks or an emergency is on, else FORWARD_SLOW_S."""
        f = self.station_feed()
        if f is None:
            raise ValueError("this hub has no aircraft layer ([aircraft] enabled = false)")
        out = f.ingest(d, size)
        return dict(out, every=FORWARD_FAST_S if self.demanded() or self.emerg else FORWARD_SLOW_S)

    def primary(self) -> Feed | None:
        """The API feed that has been giving the most aircraft with fresh positions lately (config order on a tie),
        among those not resting."""
        now = self.clock()
        up = [f for f in self.api_feeds() if f.st["rest_until"] <= now]
        if not up:
            return None
        return max(up, key=lambda f: (f.st["score"] if f.st["score"] is not None else -1.0, -self.feeds.index(f)))

    def interval(self, f: Feed, demanded: bool) -> float | None:
        if isinstance(f, StationFeed):          # in memory, kept fresh by the station's posts: cheap
            return None if f.last is None else (1.0 if demanded else 2.0)
        if isinstance(f, ReceiverFeed):
            return RECEIVER_POLL_S if demanded else WATCH_S
        if f is self.primary():
            return PRIMARY_S if demanded else WATCH_S
        return SECONDARY_S if demanded else None

    def tick(self) -> None:
        if not self.configured:
            return
        demanded = self.demanded()
        if not demanded and not self.watching():
            return
        now = self.clock()
        if demanded and self.registry.due():      # its own thread (a large download); not again while it runs
            self.registry.st["next"] = now + REGISTRY_BACKOFF_S[0]
            threading.Thread(target=self.registry.refresh, name="aircraft-registry", daemon=True).start()
        read = False
        for f in list(self.feeds):
            iv = self.interval(f, demanded)
            if iv is None or now < f.st["rest_until"] or now < f.st["next"]:
                continue
            read = self._try_read(f, now, iv) or read
        if read:
            self._combine(now)
        elif self.st["fetched"] is None or now - self.st["fetched"] > FEED_KEEP_S:
            self._no_data(now)

    def refresh(self) -> bool:
        """Read every feed that may be asked now, and combine. True when one answered. Never raises."""
        if not self.configured:
            return False
        now = self.clock()
        read = False
        for f in self.feeds:
            if now >= f.st["rest_until"]:
                read = self._try_read(f, now, self.interval(f, True) or SECONDARY_S) or read
        if read:
            self._combine(now)
        else:
            self._no_data(now)
        return read

    def widen(self, radius_nm: float, now: float | None = None) -> None:
        """An Approach channel was heard (#260): the feeds asked for radius_nm around HPN for WIDE_HOLD_S from now, and
        the aircraft out there that may be HPN's traffic kept in self.wide (approach_phase). The map stays as it is."""
        now = self.clock() if now is None else now
        if radius_nm > self.radius_nm:
            if not self.widened(now):
                self.log(f"aircraft: {radius_nm:g} nm around HPN for NY Approach's traffic (the map stays {self.radius_nm:g} nm)")
            self.wide_nm, self.wide_until = float(radius_nm), now + WIDE_HOLD_S

    def widened(self, now: float | None = None) -> bool:
        return self.wide_nm > self.radius_nm and (self.clock() if now is None else now) < self.wide_until

    def query_nm(self, now: float | None = None) -> float:
        """How far around HPN the feeds are asked: the map's radius, or NY Approach's while widened (#260)."""
        return self.wide_nm if self.widened(now) else self.radius_nm

    def _try_read(self, f: Feed, now: float, iv: float) -> bool:
        """_read(), and a bug in it (not the feed's answer: _read rests the feed for those) rests that feed only: the
        others are still read and combined (#269: one feed's OverflowError stopped every poll)."""
        try:
            return self._read(f, now, iv)
        except Exception as e:  # noqa: BLE001
            f.st.update(rest_until=now + BACKOFF_S[0], next=now + BACKOFF_S[0], why=f"{type(e).__name__}: {e}"[:160])
            self._error(FEEDS[f.key]["label"], e)
            return False

    def _read(self, f: Feed, now: float, iv: float) -> bool:
        st = f.st
        if isinstance(f, ApiFeed):
            f.radius_nm = self.query_nm(now)
        self.tracks.radius_nm = max(self.radius_nm, self.query_nm(now))
        try:
            snap = f.read()
        except Exception as e:  # network, a 429, a file, JSON: this feed rests, the others carry on
            st["fails"] += 1
            # (the exponent capped: 2 ** 1024 times a float is an OverflowError, and ours fails every 5 s, so after
            # 85 min of no posts every poll raised it and no feed was read: #269)
            exp = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(st["fails"] - 1, 30))
            if isinstance(e, RateLimited):
                wait = e.retry_after_s if e.retry_after_s else max(RATE_LIMITED_MIN_S, exp)
            else:
                wait = min(exp, BACKOFF_S[0]) if f.local else exp      # ours: back the moment it is
            why = str(e) if isinstance(e, RateLimited) else f"{type(e).__name__}: {getattr(e, 'reason', e)}"[:160]
            if not f.local:
                self.log(f"aircraft: {FEEDS[f.key]['label']}: {why}; resting {wait:.0f} s")
            else:
                # Ours is retried every few seconds and its words change with the seconds ("for 35 s"): logged when
                # it starts failing or fails another way, then every RECEIVER_LOG_EVERY_S while it goes on (#262).
                kind = re.sub(r"\d+(\.\d+)?", "#", why)
                if kind != st.get("log_kind") or now - st.get("log_at", 0.0) >= RECEIVER_LOG_EVERY_S:
                    since = st.get("down_since") or now
                    self.log(f"aircraft: {FEEDS[f.key]['label']}: {why}; resting {wait:.0f} s"
                             + (f" (failing since {tz.hhmmss(since)}; logged every {RECEIVER_LOG_EVERY_S / 60:.0f} min)"
                                if kind == st.get("log_kind") else ""))
                    st.update(log_kind=kind, log_at=now)
                if not st.get("down_since"):
                    st["down_since"] = now
            st.update(rest_until=now + wait, next=now + wait, why=why)
            return False
        if f.local and st.get("down_since"):
            self.log(f"aircraft: {FEEDS[f.key]['label']} back (after {now - st['down_since']:.0f} s)")
            st.update(down_since=None, log_kind="", log_at=0.0)
        fresh = sum(1 for p in snap.planes if p.has_position and (p.seen_pos if p.seen_pos is not None else p.seen) <= STALE_S
                    and nm(ARP[0], ARP[1], p.lat, p.lon) <= self.radius_nm)
        score = fresh if st["score"] is None else (1 - SCORE_ALPHA) * st["score"] + SCORE_ALPHA * fresh
        st.update(fails=0, rest_until=0.0, why="", ok=now, at=snap.now, n=len(snap.planes), fresh=fresh, score=score,
                  snap=snap, next=now + iv, reads=st["reads"] + 1)
        return True

    def receiver_healthy(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        return any(f.local and f.st["ok"] is not None and now - f.st["ok"] <= RECEIVER_KEEP_S for f in self.feeds)

    def _remember(self, snaps: list[tuple[str, Snapshot]], now: float) -> None:
        """The API feeds' details by ICAO address (DETAILS_KEEP_S; at most DETAILS_MAX, the oldest let go)."""
        for key, snap in snaps:
            if key == "receiver":
                continue
            for p in snap.planes:
                d = {k: getattr(p, k) for k in ("reg", "type", "desc", "owner_op", "year") if getattr(p, k)}
                if d:
                    self.details[p.hex] = dict(self.details.get(p.hex) or {}, **d, feed=key, at=now)
        if len(self.details) > DETAILS_MAX or (self.details and now - min(d["at"] for d in self.details.values()) > DETAILS_KEEP_S):
            keep = sorted(((d["at"], h) for h, d in self.details.items() if now - d["at"] <= DETAILS_KEEP_S), reverse=True)
            self.details = {h: self.details[h] for _at, h in keep[:DETAILS_MAX]}

    def _attach(self, snap: Snapshot) -> Snapshot:
        """Each aircraft's missing registration / type / owner from what an API feed said about it lately (by its ICAO
        address): a plane only our receiver has now keeps them; Plane.details names the feed they came from."""
        out = []
        for p in snap.planes:
            d = self.details.get(p.hex)
            if d:
                fill = {k: d[k] for k in ("reg", "type", "desc", "owner_op", "year") if d.get(k) and not getattr(p, k)}
                if fill:
                    p = replace(p, **fill, details=p.details or d["feed"])
            out.append(p)
        return Snapshot(snap.now, tuple(out))

    def _combine(self, now: float) -> None:
        snaps = []
        for f in self.feeds:
            keep = RECEIVER_KEEP_S if f.local else FEED_KEEP_S
            if f.st["snap"] is not None and f.st["ok"] is not None and now - f.st["ok"] <= keep:
                snaps.append((f.key, f.st["snap"]))
        if not snaps:
            self._no_data(now)
            return
        self._remember(snaps, now)
        snap = self._attach(combine(snaps))
        with self._lock:
            self.tracks.update(snap)
            view = self._view()
            wide = self.wide
        # "fetched" is when the answer came in, not when the tick began: the page reads each aircraft's age as its
        # "age" (at the feed's time) plus the time since "fetched", so stamping it before the reads (an API answer
        # takes 0.3 s to over 10 s) made every position look older than it was and the page drew the aircraft that
        # much further on, then had to bring it back at the next fix (#179)
        done = self.clock()
        with self._fresh:
            self.view = view
            self.st.update(at=snap.now, fetched=done, error="", seq=self.st["seq"] + 1)
            self._fresh.notify_all()
        if self.on_view is not None:
            try:
                self.on_view(view + wide, done)     # ... and NY Approach's wider set (#260), for the talkers only
            except Exception as e:  # noqa: BLE001 - the layer goes on
                self.log(f"aircraft: atc talkers: {type(e).__name__}: {e}")
        self._watch(now)

    def _no_data(self, now: float) -> None:
        why = "; ".join(f"{FEEDS[f.key]['label']}: {f.st['why']}" for f in self.feeds if f.st["why"])
        with self._fresh:
            self.st["error"] = ("no feed answering: " + why if why else "no feed answering")[:300]
            self._fresh.notify_all()

    def ask(self) -> None:
        """A viewer looks: keep the feeds coming; wake the read now when the data is stale and wait a moment for it."""
        now = self.clock()
        self.asked = now
        stale = self.st["fetched"] is None or now - self.st["fetched"] > 2 * PRIMARY_S
        if not stale or not self.configured:
            return
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            with self._fresh:
                self._fresh.wait_for(lambda: (self.st["fetched"] is not None and self.clock() - self.st["fetched"] <= 2 * PRIMARY_S)
                                     or bool(self.st["error"]), timeout=WAIT_FIRST_S)

    # -- the emergency watch (alarm + push: sizeup/cad/alarms.py on_aircraft / aircraft_cleared) -------------------------

    def _event(self, t: Track, code: str, meaning: str) -> dict:
        """What the alarm, its push and the pre-alert say about an aircraft in an emergency."""
        ac = t.plane
        reg = self.registry.lookup(ac.hex) if not ac.hex.startswith("~") else None
        ph = self.phases.get(ac.hex) or Phase(OVERFLIGHT)
        model = " ".join(x for x in ((reg or {}).get("make", ""), (reg or {}).get("model", "")) if x) or ac.desc
        op, op_src = operator_for(ac, reg, self.airlines)
        return {"key": f"{ac.hex}:{code}", "hex": ac.hex, "callsign": ac.flight, "reg": ac.reg or (reg or {}).get("n", ""),
                "type": ac.type, "model": model, "code": code, "meaning": meaning, "squawk": ac.squawk,
                "alt": ac.alt_baro if ac.alt_baro is not None else _r(ac.alt_geom), "vr": _r(ac.vertical_rate), "gs": _r(ac.gs),
                "lat": round(ac.lat, 5), "lon": round(ac.lon, 5), "where": where_from_hpn(ac.lat, ac.lon),
                "runway_end": runway_relation(ac.lat, ac.lon),
                "phase": ph.kind, "runway": ph.runway, "approach": ph.approach,
                "dist_nm": _r(ph.dist_nm, 1), "feed": FEEDS.get(ac.feed, {}).get("label", ac.feed or "ADS-B"),
                "src": ac.feed, "operator": op, "operator_src": op_src,
                "details_from": FEEDS.get(ac.details, {}).get("label", "") if ac.details else "",
                "pos_at": t.pos_time, "at": self.tracks.now}

    def _prealert(self, what: str, ev: dict, aid=None, why: str = "") -> None:
        """The CAD pre-alert beside the alarm (sizeup/cad/airport_alerts.py AircraftPreAlerts): start (with the alarm's id) | again (the
        same emergency within REALARM_S: the same item, back open) | update (newer altitude / position) | end."""
        pa = self.prealerts
        if pa is None or not self.active():
            return
        try:
            if what == "start":
                pa.aircraft(ev, aid)
            elif what == "again":
                pa.aircraft_again(ev)
            elif what == "update":
                pa.aircraft_update(ev)
            else:
                pa.aircraft_ended(ev, why)
        except Exception as e:  # noqa: BLE001 - the watch and the alarm go on
            self.log(f"aircraft: pre-alert ({what}): {type(e).__name__}: {e}")

    def _watch(self, now: float) -> None:
        if self.on_emergency is None:
            return
        seen: dict[str, tuple[Track, str, str]] = {}
        for t in self.tracks.live():
            ac = t.plane
            meaning = ac.emergency_text
            if not meaning or ac.seen > STALE_S:
                continue
            alt = ac.altitude_ft
            if nm(ARP[0], ARP[1], ac.lat, ac.lon) > self.radius_nm or (alt is not None and alt >= WATCH_CEILING_FT):
                continue                         # outside the PCFD / HPN area of interest
            code = ac.squawk if ac.squawk in EMERGENCY_SQUAWKS else ac.emergency
            seen[f"{ac.hex}:{code}"] = (t, code, meaning)
        for key, (t, code, meaning) in seen.items():
            cur = self.emerg.get(key)
            if cur is not None:
                cur["seen"] = now
                cur["event"] = self._event(t, code, meaning)
                if cur.get("prealert") and now - cur.get("updated", 0) >= PREALERT_UPDATE_S:
                    cur["updated"] = now
                    self._prealert("update", cur["event"])
                continue
            ev = self._event(t, code, meaning)
            if now - self.ended.get(key, -1e18) < REALARM_S:
                # the same emergency again soon after it ended: no new alarm (#169), its pre-alert back open (one
                # event shows once in the queue), when it had one
                was = self.ended_prealert.pop(key, False)
                self.emerg[key] = {"event": ev, "alarm": None, "since": now, "seen": now, "prealert": was, "updated": now}
                if was:
                    self._prealert("again", ev)
                self.log(f"aircraft: {key} {meaning} again within {REALARM_S / 60:.0f} min of ending: no new alarm")
                continue
            while self.raised and now - self.raised[0] > MAX_ALARMS_S:
                self.raised.popleft()
            if len(self.raised) >= MAX_ALARMS:
                self.emerg[key] = {"event": ev, "alarm": None, "since": now, "seen": now}
                self.log(f"aircraft: {key} {meaning}: {MAX_ALARMS} aircraft alarms in the last hour already: logged, not raised")
                continue
            self.raised.append(now)
            try:
                aid = self.on_emergency(ev)
            except Exception as e:  # the watch goes on
                aid = None
                self.log(f"aircraft: alarm not raised: {type(e).__name__}: {e}")
            self.emerg[key] = {"event": ev, "alarm": aid, "since": now, "seen": now,
                               "prealert": aid is not None and self.prealerts is not None, "updated": now}
            if aid is not None:
                self._prealert("start", ev, aid)
            self.log(f"aircraft: EMERGENCY {ev['callsign'] or ev['reg'] or ev['hex']} {meaning} {ev['where']} (alarm {aid})")
        for key in [k for k, v in self.emerg.items() if k not in seen and now - v["seen"] > CLEAR_HOLD_S]:
            cur = self.emerg.pop(key)
            self.ended[key] = now
            t = self.tracks.tracks.get(cur["event"]["hex"])
            if t is not None and t.plane.seen <= STALE_S:
                sq = t.plane.squawk
                why = f"squawk ended (now {sq})" if sq else "emergency ended"
            else:
                why = "no longer reported"
            if cur["alarm"] is not None and self.on_clear is not None:
                try:
                    self.on_clear(cur["event"], cur["alarm"], why)
                except Exception as e:  # noqa: BLE001
                    self.log(f"aircraft: alarm not cleared: {type(e).__name__}: {e}")
            if cur.get("prealert"):
                self.ended_prealert[key] = True
                self._prealert("end", cur["event"], why=why)
            self.log(f"aircraft: {key} {why}")
        for key in [k for k, at in self.ended.items() if now - at > REALARM_S]:
            del self.ended[key]
            self.ended_prealert.pop(key, None)

    # -- what the pages get ---------------------------------------------------------------------------------------------

    def _view(self) -> list[dict]:
        now = self.tracks.now or 0.0
        out = []
        live = set()
        phases = {}
        wide = []
        far = self.widened(self.clock())
        for t in self.tracks.live():
            ac = t.plane
            live.add(ac.hex)
            ph = phases[ac.hex] = self.memory.phase(ac, now, t.trail)
            emerg = ac.emergency_text
            d = nm(ARP[0], ARP[1], ac.lat, ac.lon)
            if (ph.kind not in SHOWN and not emerg) or d > self.radius_nm * 1.5:   # the map's area, as before #260
                if far and ph.kind not in SHOWN:
                    wp = approach_phase(ac, self.memory._departed.get(ac.hex), now, self.wide_nm)
                    if wp:
                        wide.append({"hex": ac.hex, "cs": ac.flight or ac.reg, "flight": ac.flight, "reg": ac.reg,
                                     "type": ac.type, "lat": round(ac.lat, 5), "lon": round(ac.lon, 5),
                                     "alt": ac.alt_baro if ac.alt_baro is not None else _r(ac.alt_geom), "gs": _r(ac.gs),
                                     "trk": _r(ac.track), "vr": _r(ac.vertical_rate), "phase": wp, "dist": _r(d, 1),
                                     "wide": True})
                continue
            reg = self.registry.lookup(ac.hex) if not ac.hex.startswith("~") else None
            m = AIRLINE_CALLSIGN.match(ac.flight or "")
            model = " ".join(x for x in ((reg or {}).get("make", ""), (reg or {}).get("model", "")) if x)
            kind = "helicopter" if ac.category == "A7" else (reg or {}).get("kind", "")
            op, op_src = operator_for(ac, reg, self.airlines)
            # to 5 places (~1 m) like the position: 4 (~10 m) put kinks in the page's smoothed curve (#179)
            trail = [[round(lon, 5), round(lat, 5)] for _, lat, lon in _thin(tuple(t.trail), TRAIL_POINTS)]
            out.append({
                "hex": ac.hex, "cs": ac.flight or ac.reg or (reg or {}).get("n", ""), "flight": ac.flight,
                "reg": ac.reg or (reg or {}).get("n", ""), "reg_src": "feed" if ac.reg else ("faa" if reg else ""),
                "type": ac.type, "model": model or ac.desc, "model_src": "faa" if model else ("feed" if ac.desc else ""),
                "kind": kind, "cat": ac.category, "airline": self.airlines.get(m.group(1), "") if m else "",
                "lat": round(ac.lat, 5), "lon": round(ac.lon, 5), "alt": ac.alt_baro if ac.alt_baro is not None else _r(ac.alt_geom),
                "gs": _r(ac.gs), "trk": _r(ac.track), "tr": _r(t.turn, 1), "vr": _r(ac.vertical_rate), "sq": ac.squawk, "emerg": emerg,
                "phase": ph.kind, "rwy": ph.runway, "appr": ph.approach, "dist": _r(ph.dist_nm, 1),
                "eta": _r(ph.eta_s) if now - t.pos_time <= STALE_S else None,      # never from an old position
                "age": round(max(0.0, now - t.pos_time), 1), "pos": ac.source, "band": ac.band, "src": ac.feed,
                "op": op, "op_src": op_src, "det": ac.details, "year": ac.year or (reg or {}).get("year", ""),
                "trail": trail,
            })
        self.memory.forget(live)
        self.phases = phases
        self.wide = sorted(wide, key=lambda a: (a["dist"], a["hex"]))
        order = {ARRIVING: 0, DEPARTING: 1, GROUND: 2, NEAR: 3, LOW: 4}
        out.sort(key=lambda a: (not a["emerg"], order.get(a["phase"], 5), a["dist"] if a["dist"] is not None else 1e9, a["hex"]))
        return out

    def receiver_status(self) -> dict | None:
        """The receiver's sources and the dongle named for ADS-B: plugged in, held by which app. Never opens it. On the
        hub: what the station says about its receiver, and how its forwarding is doing (StationFeed)."""
        sf = self.station_feed()
        if sf is not None:
            if sf.last is None and not sf.rx:
                return None
            rx, st = sf.rx, sf.stats()
            fresh = st["age_s"] is not None and st["age_s"] <= RECEIVER_KEEP_S
            name = rx.get("name") or rx.get("sdr") or ""
            note = ("" if fresh else f"no word from the station's receiver since {tz.hhmmss(sf.last)}"
                    if sf.last else "the station's receiver hasn't sent anything yet")
            if rx.get("state") and rx.get("state") != "ok":
                note = "; ".join(x for x in (note, f"decoder {rx['state']}: {rx.get('detail') or ''}".rstrip(": ")) if x)
            return {"enabled": True, "station": True, "sdr": rx.get("sdr", ""), "name": name, "present": rx.get("present"),
                    "held_by": rx.get("held_by"), "decoder": {k: rx.get(k) for k in ("state", "detail", "messages_per_s",
                                                                                         "aircraft", "last_message_at",
                                                                                         "usbfs_mb")},
                    "forwarding": st, "bands": {}, "note": note}
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

    def feeds_public(self, view: list[dict]) -> list[dict]:
        """Each feed: its role now (receiver / primary / secondary), how many aircraft it gave last time and with a
        fresh position, how many on the map came from it, and why it is resting."""
        now = self.clock()
        prim = self.primary()
        healthy = self.receiver_healthy(now)
        out = []
        for f in self.feeds:
            st = f.st
            if isinstance(f, StationFeed) and f.last is None:
                continue                        # the hub, with no receiver on the station: not a feed to list
            role = "receiver" if f.local else ("primary" if f is prim else "secondary")
            out.append(f.meta() | {"role": role, "n": st["n"], "fresh": st["fresh"], "ok": st["ok"], "at": st["at"],
                                   "shown": sum(1 for a in view if a.get("src") == f.key),
                                   "resting": st["why"] if st["rest_until"] > now else "",
                                   "healthy": healthy if role == "receiver" else (st["ok"] is not None and now - st["ok"] <= FEED_KEEP_S)})
        return out

    def public(self) -> dict:
        with self._lock:
            view = list(self.view)
        counts: dict[str, int] = {}
        for a in view:
            counts[a["phase"]] = counts.get(a["phase"], 0) + 1
        counts["emergency"] = sum(1 for a in view if a["emerg"])
        feeds = self.feeds_public(view)
        used = [f for f in feeds if f["shown"]]
        prim = self.primary()
        return {"configured": self.configured, "note": self.why_not(), "feed": prim.meta() if prim else None,
                "feeds": feeds, "credit": " + ".join(f["credit"] for f in (used or feeds) if f["credit"]),
                "at": self.st["at"], "fetched": self.st["fetched"], "error": self.st["error"], "stale_s": STALE_S,
                "drop_s": DROP_S, "airport": {"icao": ICAO, "lat": ARP[0], "lon": ARP[1]}, "registry": self.registry.public(),
                "receiver": self.receiver_status(), "aircraft": view, "counts": counts, "atc": self._extra()}

    def _extra(self) -> dict | None:
        if self.extra is None:
            return None
        try:
            return self.extra()
        except Exception as e:  # noqa: BLE001
            self.log(f"aircraft: atc talkers: {type(e).__name__}: {e}")
            return None

    def handles(self, path: str) -> bool:
        return path == PATH

    def get(self, path: str, query: str = "", headers=None) -> tuple[int, bytes, str, dict]:
        if path != PATH:
            return 404, b'{"error":"not found"}', JSON_TYPE, {}
        self.ask()
        body = json.dumps(self.public(), separators=(",", ":")).encode()
        # The same data -> the same ETag: the page's next ask (If-None-Match) gets a 304, no body.
        return 200, body, JSON_TYPE, {"ETag": '"' + hashlib.sha1(body).hexdigest()[:16] + '"', "Cache-Control": "no-cache"}
