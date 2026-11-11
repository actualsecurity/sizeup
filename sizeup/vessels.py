"""Vessels on the CAD maps (AIS): ships and boats on western Long Island Sound, the way the Aircraft layer
(sizeup/aircraft.py) and Metro-North's trains (sizeup/trains.py) are drawn. docs/vessels.md has the sources, the setup
and the limits.

The area: one box ([vessels] bbox, west / south / east / north), by default western Long Island Sound from where NY
Harbor's East River meets the Sound at Throgs Neck east to Stamford, and from the Westchester / Connecticut shore
across to Long Island's north shore (Manhasset Bay, Hempstead Harbor, Oyster Bay): BBOX. aisstream.io is asked for
that box, and anything outside it (its boxes are coarse; our own receiver hears further) is dropped here.

Sources (pluggable, in this order of preference; a position is taken from whichever is newer):

  receiver    our own AIS receiver: AIS-catcher on one RTL-SDR at the station, with a marine VHF antenna (not
              installed yet: off by default). The station's sizeup-ais service (sizeup/radio/aisd.py) runs AIS-catcher
              with its JSON output and keeps each vessel's latest messages in a file; the station posts them on to
              the hub (sizeup/cloud/vessellink.py -> POST /api/station/vessels: StationFeed here). All-in-one, the
              file is read directly (ReceiverFile). AIS-catcher's JSON is gpsd-style (type, mmsi, lat, lon, speed,
              course, heading, status, shipname, callsign, shiptype, to_bow ...): from_aiscatcher().
  aisstream   aisstream.io's free websocket (wss://stream.aisstream.io/v0/stream): one connection with a
              subscription for the box, only while someone looks at the layer (and LINGER_S after), or while a
              marine call is open; backoff on failure (5 s doubling to 5 min; a refused key 30 min). Its key:
              [vessels] aisstream_key or env SIZEUP_AISSTREAM_KEY (the hub: SSM /sizeup-hub/aisstream-key). No key:
              this source is off. The key is never logged or shown ("set" / "missing" only). from_aisstream().
  aishub      a hook only (AISHub shares its feed with those who feed it ours): not implemented.

Never MarineTraffic (a paid API whose terms forbid scraping).

The model (Fleet): position reports (types 1, 2, 3; class B 18, 19; long range 27, never over a fresher precise fix)
merged with static data (5; class B 24 parts A and B; the static half of 19) by MMSI: name, callsign, ship type (a
short label: cargo, tanker, passenger, tug, pleasure, sailing, fishing, law enforcement, SAR, other), length and beam,
SOG, COG, heading, navigational status, destination, and how old the position is. A position older than STALE_S
(3 min) is drawn faded; one older than DROP_S (10 min) is gone. Static data is kept by MMSI for STATIC_KEEP_S (a type 5
comes every 6 minutes, so a vessel seen again keeps its name meanwhile).

Served at GET /api/cad/vessels (ETag -> 304), asked by the page only while a map has the layer on, and
GET /api/cad/vessels/near?event=<id>: for a marine call, the vessels its text names (by name or MMSI) and the
NEAREST_N nearest to it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from sizeup.wsclient import WebSocket, WebSocketError

UA = "Port Chester Fire Department - sizeup emergency situational awareness application (github.com/actualsecurity/sizeup)"
AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
# western Long Island Sound: Throgs Neck (East River) to Stamford, the Westchester / CT shore to Long Island's north shore
BBOX = (-73.83, 40.78, -73.50, 41.07)              # west, south, east, north
PATH, NEAR_PATH = "/api/cad/vessels", "/api/cad/vessels/near"
JSON_TYPE = "application/json"
STALE_S = 180.0               # a position older than this: faded on the map
DROP_S = 600.0                # ... and gone after this
LR_HOLD_S = 300.0             # a long-range (type 27) position never replaces a precise one younger than this
STATIC_KEEP_S = 24 * 3600.0   # name / type / size kept by MMSI this long
STATIC_MAX = 5000
MAX_POS = 2000               # positions kept at most (the box holds a few hundred at the busiest)
PRUNE_EVERY_S = 30.0         # old positions and statics dropped this often as messages come, whoever asks
MAX_VESSELS = 500
DEMAND_S = 60.0               # a viewer's ask counts as "looking" this long
LINGER_S = 900.0              # aisstream stays connected this long after the last ask (vessels' names come slowly)
HOT_EVERY_S = 30.0            # an open marine call is looked for this often
RECEIVER_READ_S = 2.0         # our receiver's file (all-in-one), at most this often
RECEIVER_KEEP_S = 60.0        # the station's posts: silent this long, the receiver is "stale"
BACKOFF_S = (5.0, 300.0)
REFUSED_S = 1800.0            # aisstream refused the key: wait this long
IDLE_TIMEOUT_S = 600.0        # nothing from aisstream this long (a quiet box, or a dead link): reconnect
TICK_S = 5.0                  # the connection looks at "still wanted?" this often while nothing comes
CLOSED_FAST_S = 10.0         # a server close this soon after subscribing, nothing sent: treated as a refused key
READ_TIMEOUT_S = 60.0         # a frame begun but not finished within this: a failure
NEAR_KEEP_S = 10.0            # an event's near answer, cached this long
NEAREST_N = 3
NEAR_MAX_NM = 10.0
NM_M = 1852.0

FEEDS = {
    "receiver": {"label": "Our receiver", "credit": "Our AIS receiver"},
    "aisstream": {"label": "aisstream.io", "credit": "aisstream.io", "terms": "https://aisstream.io/"},
    "aishub": {"label": "AISHub", "credit": "AISHub"},          # a hook: not implemented
}

# ITU-R M.1371 ship and cargo types -> (class, short label)
CLASSES = {
    "cargo": "Cargo", "tanker": "Tanker", "passenger": "Passenger", "tug": "Tug", "pleasure": "Pleasure",
    "sailing": "Sailing", "fishing": "Fishing", "law": "Law enforcement", "sar": "SAR", "other": "Other", "unknown": "",
}


def ship_class(code) -> str:
    """The ship type's class (CLASSES' keys): "unknown" for none / 0."""
    if not isinstance(code, int) or isinstance(code, bool) or code <= 0:
        return "unknown"
    if code == 30:
        return "fishing"
    if code in (31, 32, 52):
        return "tug"
    if code == 36:
        return "sailing"
    if code == 37:
        return "pleasure"
    if code == 51:
        return "sar"
    if code == 55:
        return "law"
    if 40 <= code <= 49 or 60 <= code <= 69:
        return "passenger"
    if 70 <= code <= 79:
        return "cargo"
    if 80 <= code <= 89:
        return "tanker"
    return "other"


NAV_STATUS = {0: "Under way", 1: "At anchor", 2: "Not under command", 3: "Restricted manoeuvrability",
              4: "Constrained by draught", 5: "Moored", 6: "Aground", 7: "Fishing", 8: "Under way (sail)",
              14: "AIS-SART active"}


def _num(v, lo: float, hi: float, unavailable=()) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if v in unavailable or not lo <= v <= hi:
        return None
    return float(v)


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _text(v, n: int = 20) -> str:
    if not isinstance(v, str):
        return ""
    return re.sub(r"\s+", " ", v.replace("@", " ")).strip()[:n]


def _mmsi(v) -> int | None:
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    v = _int(v)
    return v if v is not None and 1 <= v <= 999_999_999 else None


def _dims(a, b, c, d) -> tuple[float | None, float | None]:
    """Length (bow + stern) and beam (port + starboard), metres; None when not given."""
    a, b, c, d = (_int(x) or 0 for x in (a, b, c, d))
    ln, bm = a + b, c + d
    return (float(ln) if 0 < ln < 600 else None), (float(bm) if 0 < bm < 80 else None)


def _pos(lat, lon) -> tuple[float | None, float | None]:
    la, lo = _num(lat, -90, 90, (91,)), _num(lon, -180, 180, (181,))
    if la is None or lo is None or (la == 0 and lo == 0):
        return None, None
    return la, lo


# ---- the two message formats -> one normalized message ------------------------------------------------------------
#   {mmsi, type, [lat, lon, sog, cog, hdg, nav], [name, cs, stype, len, beam, dest, imo]}: only what the message had


def from_aisstream(obj) -> dict | None:
    """One aisstream.io message ({MessageType, MetaData, Message: {<type>: {...}}}) -> a normalized message, or None
    (another type, an error, or nothing usable)."""
    if not isinstance(obj, dict):
        return None
    kind = obj.get("MessageType")
    body = (obj.get("Message") or {}).get(kind) if isinstance(obj.get("Message"), dict) else None
    if not isinstance(body, dict):
        return None
    meta = obj.get("MetaData") if isinstance(obj.get("MetaData"), dict) else {}
    mmsi = _mmsi(body.get("UserID")) or _mmsi(meta.get("MMSI"))
    if mmsi is None or body.get("Valid") is False:
        return None
    t = _int(body.get("MessageID"))
    out: dict = {"mmsi": mmsi, "type": t}
    if kind in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                "LongRangeAisBroadcastMessage"):
        lat, lon = _pos(body.get("Latitude"), body.get("Longitude"))
        if lat is None:
            return None
        lr = kind == "LongRangeAisBroadcastMessage"
        out.update(lat=lat, lon=lon,
                   sog=_num(body.get("Sog"), 0, 62 if lr else 102.2, (63,) if lr else (102.3,)),
                   cog=_num(body.get("Cog"), 0, 359.9, (360, 511)),
                   hdg=None if lr else _num(body.get("TrueHeading"), 0, 359, (511,)))
        if kind in ("PositionReport", "LongRangeAisBroadcastMessage"):
            out["nav"] = _int(body.get("NavigationalStatus"))
        if kind == "ExtendedClassBPositionReport":
            ln, bm = _dims(*((body.get("Dimension") or {}).get(k) for k in "ABCD"))
            out.update(name=_text(body.get("Name")), stype=_int(body.get("Type")), len=ln, beam=bm)
        out["type"] = t or {"PositionReport": 1, "StandardClassBPositionReport": 18,
                            "ExtendedClassBPositionReport": 19, "LongRangeAisBroadcastMessage": 27}[kind]
        return out
    if kind == "ShipStaticData":
        ln, bm = _dims(*((body.get("Dimension") or {}).get(k) for k in "ABCD"))
        imo = _int(body.get("ImoNumber"))
        out.update(type=t or 5, name=_text(body.get("Name")), cs=_text(body.get("CallSign"), 7),
                   stype=_int(body.get("Type")), len=ln, beam=bm, dest=_text(body.get("Destination")),
                   imo=imo if imo and 1_000_000 <= imo <= 9_999_999 else None)
        return out
    if kind == "StaticDataReport":
        out["type"] = t or 24
        a, b = body.get("ReportA"), body.get("ReportB")
        if isinstance(a, dict) and a.get("Valid") and _text(a.get("Name")):
            out["name"] = _text(a.get("Name"))
        if isinstance(b, dict) and b.get("Valid"):
            ln, bm = _dims(*((b.get("Dimension") or {}).get(k) for k in "ABCD"))
            out.update(cs=_text(b.get("CallSign"), 7), stype=_int(b.get("ShipType")), len=ln, beam=bm)
        return out if len(out) > 2 else None
    return None


def from_aiscatcher(obj) -> dict | None:
    """One AIS-catcher JSON message (its JSON_FULL / -o 5 output, gpsd-style names; scaled) -> a normalized message."""
    if not isinstance(obj, dict):
        return None
    t, mmsi = _int(obj.get("type")), _mmsi(obj.get("mmsi"))
    if t is None or mmsi is None:
        return None
    out: dict = {"mmsi": mmsi, "type": t}
    if t in (1, 2, 3, 18, 19, 27):
        lat, lon = _pos(obj.get("lat"), obj.get("lon"))
        if lat is None:
            return None
        lr = t == 27
        out.update(lat=lat, lon=lon, sog=_num(obj.get("speed"), 0, 62 if lr else 102.2, (63,) if lr else (102.3,)),
                   cog=_num(obj.get("course"), 0, 359.9, (360, 511)),
                   hdg=None if lr else _num(obj.get("heading"), 0, 359, (511,)))
        if t in (1, 2, 3, 27):
            out["nav"] = _int(obj.get("status"))
        if t == 19:
            ln, bm = _dims(obj.get("to_bow"), obj.get("to_stern"), obj.get("to_port"), obj.get("to_starboard"))
            out.update(name=_text(obj.get("shipname")), stype=_int(obj.get("shiptype")), len=ln, beam=bm)
        return out
    if t == 5:
        ln, bm = _dims(obj.get("to_bow"), obj.get("to_stern"), obj.get("to_port"), obj.get("to_starboard"))
        imo = _int(obj.get("imo"))
        out.update(name=_text(obj.get("shipname")), cs=_text(obj.get("callsign"), 7), stype=_int(obj.get("shiptype")),
                   len=ln, beam=bm, dest=_text(obj.get("destination")),
                   imo=imo if imo and 1_000_000 <= imo <= 9_999_999 else None)
        return out
    if t == 24:
        part = obj.get("partno")
        if part == 0 and _text(obj.get("shipname")):
            out["name"] = _text(obj.get("shipname"))
        elif part == 1:
            ln, bm = _dims(obj.get("to_bow"), obj.get("to_stern"), obj.get("to_port"), obj.get("to_starboard"))
            out.update(cs=_text(obj.get("callsign"), 7), stype=_int(obj.get("shiptype")), len=ln, beam=bm)
        return out if len(out) > 2 else None
    return None


def in_box(lat: float, lon: float, bbox=BBOX) -> bool:
    w, s, e, n = bbox
    return s <= lat <= n and w <= lon <= e


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


STATIC_KEYS = ("name", "cs", "stype", "len", "beam", "dest", "imo")
POS_KEYS = ("lat", "lon", "sog", "cog", "hdg")


class Fleet:
    """Every vessel heard, by MMSI: its last position report and its static data, merged. Thread-safe. version and
    changed_at move only when what is known changes (a message heard again, the same, changes nothing): the API's
    answer and its ETag stay the same in between."""

    def __init__(self, bbox=BBOX):
        self.bbox = tuple(bbox)
        self.pos: dict[int, dict] = {}       # mmsi -> {lat, lon, sog, cog, hdg, nav, t, type, src}
        self.static: OrderedDict[int, dict] = OrderedDict()   # mmsi -> {name, cs, ...t}, least recently updated first
        self._lock = threading.Lock()
        self.counts: dict[int, int] = {}     # messages applied, by AIS type
        self.outside = 0                     # positions dropped outside the box
        self.version = 0
        self.changed_at = 0.0                # when (message or prune time) what is known last changed
        self._pruned = 0.0

    def _changed(self, t: float) -> None:
        self.version += 1
        self.changed_at = max(self.changed_at, t)

    def apply(self, m: dict | None, t: float, src: str) -> bool:
        """One normalized message, heard at t (epoch s) from src. True when it changed what is known."""
        if not m:
            return False
        mmsi, typ = m["mmsi"], m.get("type")
        if t - self._pruned >= PRUNE_EVERY_S:
            self.prune(t)                           # bounded however long nobody asks
        with self._lock:
            self.counts[typ] = self.counts.get(typ, 0) + 1
            changed = False
            st = {k: m[k] for k in STATIC_KEYS if m.get(k) not in (None, "")}
            if st:
                cur = self.static.get(mmsi)
                if cur is None:
                    if len(self.static) >= STATIC_MAX:
                        self.static.popitem(last=False)          # the one least recently updated: O(1)
                    cur = self.static[mmsi] = {}
                if any(cur.get(k) != v for k, v in st.items()):
                    changed = True
                cur.update(st, t=max(t, cur.get("t", t)))
                self.static.move_to_end(mmsi)
            if m.get("lat") is not None:
                if not in_box(m["lat"], m["lon"], self.bbox):
                    self.outside += 1
                    if mmsi in self.pos:            # it left the area
                        del self.pos[mmsi]
                        changed = True
                    if changed:
                        self._changed(t)
                    return changed
                cur = self.pos.get(mmsi)
                if cur is not None and t < cur["t"]:
                    if changed:
                        self._changed(t)
                    return changed                  # an older report than the one we have
                if typ == 27 and cur is not None and cur.get("type") != 27 and t - cur["t"] < LR_HOLD_S:
                    if changed:
                        self._changed(t)
                    return changed                  # a coarse long-range fix never over a fresh precise one
                nav = m.get("nav", cur.get("nav") if cur else None)
                if typ in (18, 19):
                    nav = None                      # class B sends no status
                new = {**{k: m.get(k) for k in POS_KEYS}, "nav": nav, "t": t, "type": typ, "src": src}
                if new != cur:
                    if cur is None and len(self.pos) >= MAX_POS:     # never unbounded: the oldest report goes
                        del self.pos[min(self.pos, key=lambda k: self.pos[k]["t"])]
                    self.pos[mmsi] = new
                    changed = True
            if changed:
                self._changed(t)
            return changed

    def put(self, rec: dict, t_pos: float, t_static: float | None, src: str) -> None:
        """A whole vessel record (what the station posts: record() below), its position heard at t_pos."""
        mmsi = _mmsi(rec.get("id"))
        if mmsi is None:
            return
        lat, lon = _pos(rec.get("lat"), rec.get("lon"))
        if lat is None:
            return
        typ = {"A": 1, "B": 18, "LR": 27}.get(rec.get("c"), 1)
        nav = _int(rec.get("navc"))
        self.apply({"mmsi": mmsi, "type": typ, "lat": lat, "lon": lon, "sog": _num(rec.get("sog"), 0, 102.2),
                    "cog": _num(rec.get("cog"), 0, 359.9), "hdg": _num(rec.get("hdg"), 0, 359),
                    **({"nav": nav} if nav is not None else {})}, t_pos, src)
        st = {"name": _text(rec.get("name")), "cs": _text(rec.get("cs"), 7), "stype": _int(rec.get("type")),
              "len": _num(rec.get("len"), 1, 599), "beam": _num(rec.get("beam"), 1, 79), "dest": _text(rec.get("dest"))}
        imo = _int(rec.get("imo"))
        if imo and 1_000_000 <= imo <= 9_999_999:
            st["imo"] = imo
        if any(v not in (None, "") for v in st.values()):
            self.apply({"mmsi": mmsi, "type": 5 if typ == 1 else 24, **st}, t_static or t_pos, src)

    def prune(self, now: float) -> None:
        with self._lock:
            self._pruned = now
            gone = [k for k, p in self.pos.items() if now - p["t"] > DROP_S]
            for k in gone:
                del self.pos[k]
            while self.static and now - next(iter(self.static.values()))["t"] > STATIC_KEEP_S:
                self.static.popitem(last=False)
            if gone:
                self._changed(now)

    def records(self, now: float, ref: float | None = None) -> list[dict]:
        """The vessels to draw: position within DROP_S (at now) and in the box; the newest MAX_VESSELS; ages as at
        ref (default now)."""
        self.prune(now)
        ref = now if ref is None else ref
        with self._lock:
            items = sorted(self.pos.items(), key=lambda kv: -kv[1]["t"])[:MAX_VESSELS]
            return [record(k, p, self.static.get(k), ref) for k, p in items]


def _r(v, nd: int = 0):
    return None if v is None else (round(v, nd) if nd else int(round(v)))


def record(mmsi: int, p: dict, s: dict | None, now: float) -> dict:
    """What the page (and the station's post) gets for one vessel."""
    s = s or {}
    stype = s.get("stype")
    cls = ship_class(stype)
    nav = p.get("nav")
    return {"id": f"{mmsi:09d}", "name": s.get("name", ""), "cs": s.get("cs", ""), "imo": s.get("imo"),
            "type": stype, "cls": cls, "label": CLASSES[cls], "len": _r(s.get("len")), "beam": _r(s.get("beam")),
            "lat": round(p["lat"], 5), "lon": round(p["lon"], 5), "sog": _r(p.get("sog"), 1), "cog": _r(p.get("cog")),
            "hdg": _r(p.get("hdg")), "navc": nav, "nav": NAV_STATUS.get(nav, "") if isinstance(nav, int) else "",
            "dest": s.get("dest", ""), "age": round(max(0.0, now - p["t"]), 1),
            "sage": None if "t" not in s else round(max(0.0, now - s["t"]), 1),
            "c": "LR" if p.get("type") == 27 else "B" if p.get("type") in (18, 19) else "A", "src": p.get("src", "")}


# ---- incidents: a marine call, the vessels it names, the nearest ---------------------------------------------------

# A marine or water call by its type and subtype (data/cad_lists.json): any boat subtype (MVF/BOAT, SERVICE/BOAT ...),
# a water rescue and an aircraft in the water. Not NOTIF/WATER (a water-department notification) or other */WATER.
MARINE_PAIRS = {("RESCUE", "WATER"), ("AIRPORT", "ACFT-WATER")}
MARINE_ANY_TYPE = {"BOAT"}
OPEN_STATUSES = ("queued", "pending", "open", "active")
STOP_NAMES = {"ENGINE", "RESCUE", "MARINE", "LADDER", "TOWER", "CHIEF", "POLICE", "WATER", "BOAT", "FIRE", "UNIT",
              "PATROL", "HARBOR", "HARBOUR", "VESSEL", "SHIP", "TUG", "FERRY", "PILOT", "UNKNOWN", "NONE", "TEST"}


def placeholder(unit: str) -> bool:
    """A department placeholder unit (DPT239 ...): never located, mapped or followed."""
    return str(unit or "").upper().startswith("DPT")


def marine_type(typ, subtype) -> bool:
    """A boat subtype under any type, or a water rescue / an aircraft in the water (MARINE_PAIRS)."""
    t, s = str(typ or "").upper(), str(subtype or "").upper()
    return s in MARINE_ANY_TYPE or (t, s) in MARINE_PAIRS


def is_marine(inc: dict | None) -> bool:
    """A marine or water call: marine_type() (MVF/BOAT, SERVICE/BOAT, RESCUE/WATER, AIRPORT/ACFT-WATER; never
    NOTIF/WATER), or a marine unit on it, not cleared (a DPT placeholder never counts)."""
    if not inc:
        return False
    if marine_type(inc.get("type"), inc.get("subtype")):
        return True
    return any(a.get("kind") == "marine" and not placeholder(a.get("name")) and not a.get("cleared_at")
               for a in inc.get("assignments") or [] if isinstance(a, dict))


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", str(s or "").upper()).split())


def call_text(inc: dict) -> str:
    """What was said about the call: its type note, remarks and each log entry's text (notes, remarks, the log). Never
    its location words: a vessel called CITY ISLAND is not named by "City Island Ave"."""
    parts = [inc.get(k) for k in ("type_note", "remarks")]
    parts += [e.get("text") for e in inc.get("events") or [] if isinstance(e, dict)]
    return " \n ".join(str(p) for p in parts if p)


def named(text: str, vessels: list[dict]) -> list[str]:
    """The vessels (ids) the text names: its 9-digit MMSI, or its name as whole words (4+ letters, not a dispatch word)."""
    t = f" {_norm(text)} "
    ids = set(re.findall(r"(?<!\d)(\d{9})(?!\d)", text or ""))
    out = []
    for v in vessels:
        n = _norm(v.get("name"))
        if v["id"] in ids or (len(n.replace(" ", "")) >= 4 and n not in STOP_NAMES and f" {n} " in t):
            out.append(v["id"])
    return out


def nearest(vessels: list[dict], lat: float, lon: float, n: int = NEAREST_N, max_nm: float = NEAR_MAX_NM) -> list[dict]:
    """The n vessels nearest (lat, lon) within max_nm, nearest first, each with its distance (nm)."""
    out = []
    for v in vessels:
        d = metres(lat, lon, v["lat"], v["lon"]) / NM_M
        if d <= max_nm:
            out.append((d, v["id"], v))
    out.sort(key=lambda x: (x[0], x[1]))
    return [dict(v, dist=round(d, 2)) for d, _i, v in out[:n]]


# ---- the feeds ------------------------------------------------------------------------------------------------------


class AisStreamFeed:
    """aisstream.io: one websocket, subscribed to the box, while wanted(); backoff when it fails."""
    key = "aisstream"

    def __init__(self, api_key: str, bbox, fleet: Fleet, wanted: Callable[[], bool], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, url: str = AISSTREAM_URL, connect=None):
        self._key = api_key
        self.bbox, self.fleet, self.wanted, self.log, self.clock = tuple(bbox), fleet, wanted, log, clock
        self.url = url
        self.connect = connect or (lambda: WebSocket.connect(self.url, {"User-Agent": UA}, timeout=30.0))
        self.st = {"state": "idle", "at": None, "n": 0, "error": "", "connected_at": None, "fails": 0, "rest_until": 0.0}
        self.ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def subscription(self) -> str:
        """The subscription message (the key in it: never logged). Boxes are [[lat, lon], [lat, lon]] corners."""
        w, s, e, n = self.bbox
        return json.dumps({"APIKey": self._key, "BoundingBoxes": [[[s, w], [n, e]]],
                           "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport",
                                                  "ExtendedClassBPositionReport", "ShipStaticData", "StaticDataReport",
                                                  "LongRangeAisBroadcastMessage"]})

    def handle(self, raw: bytes) -> bool:
        """One message off the socket: applied to the fleet. Raises PermissionError when aisstream says the key is bad."""
        try:
            obj = json.loads(raw)
        except ValueError:
            return False
        if isinstance(obj, dict) and obj.get("error"):
            why = str(obj["error"])[:120]
            if re.search(r"api ?key|apikey|unauthori|forbidden", why, re.I):
                raise PermissionError(why)
            raise WebSocketError(why)
        m = from_aisstream(obj)
        now = self.clock()
        self.st["at"] = now
        self.st["n"] += 1
        return self.fleet.apply(m, now, self.key)

    def start(self) -> AisStreamFeed:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="vessels-aisstream", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()                        # the connection sees it within TICK_S and closes

    def _run(self) -> None:
        while not self._stop.is_set():
            now = self.clock()
            if not self.wanted() or now < self.st["rest_until"]:
                if self.st["state"] == "connected":
                    self.st["state"] = "idle"
                self._stop.wait(2.0)
                continue
            self.session()
            self._stop.wait(BACKOFF_S[0] / 5)        # never a tight loop, whatever session() did

    def session(self) -> None:
        """One connection: subscribe, read until it fails, closes, or is no longer wanted."""
        st = self.st
        try:
            st["state"] = "connecting"
            ws = self.ws = self.connect()
            try:
                ws.sock.settimeout(READ_TIMEOUT_S)          # within a frame
            except (AttributeError, OSError):
                pass
            ws.send_text(self.subscription())
            st.update(state="connected", connected_at=self.clock(), error="")
            got, heard, ours = 0, self.clock(), False
            subscribed = heard
            for raw in ws.messages(idle_s=TICK_S):
                if raw is not None:
                    heard = self.clock()
                    self.handle(raw)
                    got += 1
                    if got == 1 and st["fails"]:
                        self.log(f"vessels: aisstream.io back after {st['fails']} failed tries")
                        st["fails"] = 0
                if self._stop.is_set() or not self.wanted():
                    ours = True
                    break
                if self.clock() - heard > IDLE_TIMEOUT_S:   # a quiet box, or a dead link: a fresh connection
                    break
            ws.close()
            now = self.clock()
            if not got and not ours and now - subscribed < CLOSED_FAST_S:
                # closed by the server right after the subscription, nothing sent: a key or subscription it refuses
                self._fail("closed right after subscribing (key or subscription refused?)", REFUSED_S)
            elif not got and not ours:
                self._fail("closed with nothing received")
            else:
                st.update(state="idle", rest_until=max(st["rest_until"], now + BACKOFF_S[0]))
        except PermissionError as e:
            self._fail(f"key refused ({e})", REFUSED_S)
        except (OSError, WebSocketError, ValueError) as e:
            self._fail(f"{type(e).__name__}: {e}"[:160])
        except Exception as e:  # noqa: BLE001 - the feed must never die
            self._fail(f"{type(e).__name__}: {e}"[:160])
            self.log("vessels: aisstream: " + "".join(traceback.format_exception(type(e), e, e.__traceback__))[-800:])
        finally:
            ws, self.ws = self.ws, None
            if ws is not None:
                ws.close()

    def _fail(self, why: str, wait: float | None = None) -> None:
        st = self.st
        st["fails"] += 1
        if wait is None:
            wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(st["fails"] - 1, 20))
        st.update(state="resting", error=why, rest_until=self.clock() + wait)
        if st["fails"] in (1, 5) or st["fails"] % 50 == 0 or wait >= REFUSED_S:
            self.log(f"vessels: aisstream.io: {why}; next try in {wait:.0f} s")

    def public(self) -> dict:
        st = self.st
        return {"key": self.key, **FEEDS[self.key], "state": st["state"], "error": st["error"]}


class ReceiverFile:
    """All-in-one: our receiver's file (sizeup/radio/aisd.py MESSAGES: {at, msgs: [AIS-catcher JSON + "t"]}), read
    at most every RECEIVER_READ_S. Read only: this never starts a decoder or opens a dongle."""
    key = "receiver"

    def __init__(self, path: str, fleet: Fleet, clock: Callable[[], float] = time.time):
        self.path, self.fleet, self.clock = path, fleet, clock
        self.st = {"state": "idle", "at": None, "n": 0, "error": "", "read": 0.0}

    def read(self) -> None:
        now = self.clock()
        if now - self.st["read"] < RECEIVER_READ_S:
            return
        self.st["read"] = now
        try:
            doc = json.loads(Path(self.path).read_bytes())
        except (OSError, ValueError) as e:
            self.st.update(state="error", error=f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"[:120])
            return
        n = ingest_messages(doc, self.fleet, self.key)
        at = doc.get("at") if isinstance(doc, dict) else None
        stale = isinstance(at, (int, float)) and now - at > RECEIVER_KEEP_S
        self.st.update(state="stale" if stale else "ok", at=at, n=n, error="not updated (is sizeup-ais running?)" if stale else "")

    def public(self) -> dict:
        return {"key": self.key, **FEEDS[self.key], **{k: self.st[k] for k in ("state", "error")}}


def ingest_messages(doc, fleet: Fleet, src: str) -> int:
    """aisd's file ({at, msgs: [AIS-catcher JSON message with "t": when heard]}) into a fleet: how many applied."""
    if not isinstance(doc, dict) or not isinstance(doc.get("msgs"), list):
        return 0
    n = 0
    for raw in doc["msgs"]:
        if isinstance(raw, dict) and isinstance(raw.get("t"), (int, float)):
            n += bool(fleet.apply(from_aiscatcher(raw), float(raw["t"]), src))
    return n


class StationFeed:
    """The hub's copy of our receiver on the station (POST /api/station/vessels, sizeup/cloud/vessellink.py): each post
    is the station's whole list of vessel records (record()), ages taken off the time it arrived."""
    key = "receiver"

    def __init__(self, fleet: Fleet, clock: Callable[[], float] = time.time):
        self.fleet, self.clock = fleet, clock
        self.st = {"state": "idle", "at": None, "n": 0, "error": "", "posts": 0, "bad": 0}
        self.rx: dict = {}

    def ingest(self, d: dict) -> dict:
        if not isinstance(d, dict) or not isinstance(d.get("vessels", []), list):
            raise ValueError("a vessels post: {vessels: [...], rx?}")
        now = self.clock()
        n = 0
        for rec in d.get("vessels", [])[:MAX_VESSELS]:
            try:
                age = float(rec.get("age") or 0)
                sage = rec.get("sage")
                self.fleet.put(rec, now - max(0.0, age), None if sage is None else now - max(0.0, float(sage)), self.key)
                n += 1
            except (TypeError, ValueError, AttributeError):
                self.st["bad"] += 1
        if isinstance(d.get("rx"), dict):
            self.rx = {k: v for k, v in d["rx"].items() if k in RX_KEYS}
        self.st.update(state="ok", at=now, n=n, error="")
        self.st["posts"] += 1
        return {"ok": True, "n": n}

    def public(self) -> dict:
        now = self.clock()
        at = self.st["at"]
        state = self.st["state"] if at is None or now - at <= RECEIVER_KEEP_S else "stale"
        return {"key": self.key, **FEEDS[self.key], "state": state,
                "error": "" if state != "stale" else f"no word from the station's receiver for {now - at:.0f} s",
                "station": True, "rx": self.rx}


RX_KEYS = ("sdr", "name", "state", "detail", "held_by", "present", "messages_per_s", "vessels", "last_message_at")


class VesselLayer:
    def __init__(self, enabled: bool = True, aisstream_key: str = "", bbox=BBOX, receiver: bool = False,
                 receiver_json: str = "", station_feed: bool = False, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, connect=None):
        self.enabled = bool(enabled)
        self.bbox = tuple(float(x) for x in bbox)
        self.log, self.clock = log, clock
        self.fleet = Fleet(self.bbox)
        self.asked = 0.0
        self.hot: Callable[[], bool] = lambda: False       # an open marine call keeps aisstream connected (the app)
        # the hub's leader only (hot/cold deploys: the standby, and a task whose lease is gone, never connect; the app)
        self.leader: Callable[[], bool] = lambda: True
        self._hot = (0.0, False)
        self.lookup: Callable[[int], dict] | None = None   # an event by id (the app: the CAD store's full())
        self._near: dict[int, tuple[float, dict]] = {}
        self.aisstream = (AisStreamFeed(aisstream_key, self.bbox, self.fleet, self.wanted, log, clock, connect=connect)
                          if self.enabled and aisstream_key else None)
        self.receiver = None
        if self.enabled and station_feed:
            self.receiver = StationFeed(self.fleet, clock)
        elif self.enabled and receiver and receiver_json:
            self.receiver = ReceiverFile(receiver_json, self.fleet, clock)
        self.key_set = bool(aisstream_key)

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print) -> VesselLayer:
        hub = getattr(cfg, "cloud_role", "") == "hub"
        rx = bool(getattr(cfg, "vessels_receiver", False))
        path = getattr(cfg, "vessels_receiver_json", "") or (aisd_messages() if rx else "")
        return cls(getattr(cfg, "vessels_enabled", True), getattr(cfg, "vessels_aisstream_key", ""),
                   getattr(cfg, "vessels_bbox", BBOX), rx, path, station_feed=hub, log=log)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.feeds())

    def why_not(self) -> str:
        if not self.enabled:
            return "off ([vessels] enabled = false)"
        if not self.configured:
            return "no source (no aisstream.io key, no receiver)"
        return ""

    def start(self) -> VesselLayer:
        if not self.configured:
            self.log(f"vessels: {self.why_not()}")
            return self
        if self.aisstream is not None:
            self.aisstream.start()
        w, s, e, n = self.bbox
        self.log(f"vessels: {' + '.join(f.public()['label'] for f in self.feeds())}; box {s:g},{w:g} to {n:g},{e:g}"
                 + (f"; aisstream.io while the layer is open (and {LINGER_S / 60:.0f} min after) or a marine call is open"
                    if self.aisstream else ""))
        return self

    def stop(self) -> None:
        if self.aisstream is not None:
            self.aisstream.stop()

    def feeds(self) -> list:
        # the hub's copy of the station's receiver counts once the station has posted (most stations have none)
        return [f for f in (self.receiver, self.aisstream)
                if f is not None and not (isinstance(f, StationFeed) and not f.st["posts"])]

    def ask(self) -> None:
        self.asked = self.clock()

    def demanded(self) -> bool:
        return self.clock() - self.asked <= DEMAND_S

    def wanted(self) -> bool:
        """aisstream connected: on the leader only, while someone looked within LINGER_S or a marine call is open."""
        try:
            if not self.leader():
                return False
        except Exception:  # noqa: BLE001 - not known to lead: don't connect
            return False
        now = self.clock()
        if now - self.asked <= LINGER_S:
            return True
        if now - self._hot[0] >= HOT_EVERY_S:
            try:
                self._hot = (now, bool(self.hot()))
            except Exception:  # noqa: BLE001
                self._hot = (now, False)
        return self._hot[1]

    def station_ingest(self, d: dict) -> dict:
        """POST /api/station/vessels: the station's receiver, forwarded. "every": how often to post."""
        if not isinstance(self.receiver, StationFeed):
            raise ValueError("this hub has no vessels receiver feed ([vessels] enabled = false)")
        out = self.receiver.ingest(d)
        return dict(out, every=5.0 if self.demanded() or self.wanted() else 30.0)

    def vessels(self, ref: float | None = None) -> list[dict]:
        if isinstance(self.receiver, ReceiverFile):
            self.receiver.read()
        return self.fleet.records(self.clock(), ref)

    def public(self) -> dict:
        """The answer. Its "at" is when what is known last changed, and each vessel's age is as at then: the answer
        (and its ETag) stays the same until something changes (a 304); the page adds the time since "at" itself."""
        w, s, e, n = self.bbox
        if isinstance(self.receiver, ReceiverFile):
            self.receiver.read()
        self.fleet.prune(self.clock())
        ref = round(self.fleet.changed_at, 3)
        vs = self.fleet.records(self.clock(), ref)
        feeds = [f.public() for f in self.feeds()]
        used = {v["src"] for v in vs}
        credit = " + ".join(f["credit"] for f in feeds if f["key"] in used) or " + ".join(f["credit"] for f in feeds)
        return {"configured": self.configured, "note": self.why_not(), "at": ref, "stale_s": STALE_S,
                "drop_s": DROP_S, "bbox": [w, s, e, n], "feeds": feeds, "credit": credit,
                "key": "set" if self.key_set else "missing", "vessels": vs}

    def near(self, iid: int) -> dict:
        """An event's vessels: {marine, named: [records], nearest: [records with dist]} (cached NEAR_KEEP_S)."""
        now = self.clock()
        hit = self._near.get(iid)
        if hit and now - hit[0] < NEAR_KEEP_S:
            return hit[1]
        if self.lookup is None:
            raise LookupError("no CAD here")
        inc = self.lookup(iid)
        vs = self.vessels()
        marine = is_marine(inc)
        out = {"id": iid, "marine": marine, "configured": self.configured, "note": self.why_not(), "at": now,
               "stale_s": STALE_S, "named": [], "nearest": []}
        if marine:
            ids = set(named(call_text(inc), vs))
            out["named"] = [v for v in vs if v["id"] in ids]
            if isinstance(inc.get("lat"), (int, float)) and isinstance(inc.get("lon"), (int, float)):
                out["nearest"] = nearest(vs, inc["lat"], inc["lon"])
        self._near[iid] = (now, out)
        if len(self._near) > 50:
            self._near.pop(next(iter(self._near)))
        return out

    def handles(self, path: str) -> bool:
        return path in (PATH, NEAR_PATH)

    def get(self, path: str, query: str = "", headers=None) -> tuple[int, bytes, str, dict]:
        if path == NEAR_PATH:
            try:
                iid = int((parse_qs(query).get("event") or [""])[0])
            except ValueError:
                return 400, b'{"error":"event=<id>"}', JSON_TYPE, {}
            self.ask()
            try:
                d = self.near(iid)
            except (LookupError, ValueError) as e:      # (the CAD: no such event)
                return 404, json.dumps({"error": str(e)}).encode(), JSON_TYPE, {}
            return 200, json.dumps(d, separators=(",", ":")).encode(), JSON_TYPE, {"Cache-Control": "no-cache"}
        if path != PATH:
            return 404, b'{"error":"not found"}', JSON_TYPE, {}
        self.ask()
        body = json.dumps(self.public(), separators=(",", ":")).encode()
        return 200, body, JSON_TYPE, {"ETag": '"' + hashlib.sha1(body).hexdigest()[:16] + '"', "Cache-Control": "no-cache"}


def aisd_messages() -> str:
    """Where the station's AIS decoder keeps its messages (sizeup/radio/aisd.py MESSAGES)."""
    try:
        from sizeup.radio import aisd
        return aisd.MESSAGES
    except ImportError:
        return "/run/sizeup-ais/messages.json"

