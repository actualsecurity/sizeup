"""Metro-North from the MTA TrainTime backend (GitHub #362): GPS position, speed, heading, consist, per-car load,
milepost, posted track, OTP and branch alerts for the New Haven Line's trains, merged into the GTFS-realtime trains
of sizeup/trains.py (#131). GTFS-realtime stays the fallback: whenever this source fails or changes shape, the map
goes on exactly as before.

UNDOCUMENTED. MTA's public train map (radar.mta.info, no login) reads it; it is keyless, allows any origin and
takes the header "Accept-Version: 3.0". Nothing promises it stays: every record is validated, and a source that
fails or changes shape FALLBACK_AFTER polls in a row is set aside (Data sources and Health say so) until it reads
again. Endpoints (BASE_URL; shapes checked from a radar.mta.info capture and a few single requests, 2026-11-09):

  /locations?geometry=TRACK_TURF&railroad=MNR   every live Metro-North train (about 85; ~30 KB gzip, ~330 KB
        JSON): no polylines, plus details.turf {length, location_mp}. The poll. (geometry=NONE drops turf for
        ~1 KB less; geometry=TRACK, or none given, adds each train's route polyline: ~1 MB gzip, ~5.5 MB.
        railroad=BOTH adds the LIRR.)
  /locations?geometry=TRACK&railroad=MNR        the same with details.polyline (Google encoded polyline,
        precision 6: the train's whole route). Read daily, or when a train runs a route we have no line for;
        kept clipped to our area (state dir, trains/traintime_routes.json).
  /countdown/<code>?include_passed=false&hours=1.0   a station's next hour (code: the GTFS stop_code, e.g.
        2PC Port Chester, 2RY Rye): the same train records. Only while a station's box is open on a page.
  /health                                       {<feed>: {healthy, age}}: not read (a poll that answers is health).

A train record (what we read; anything else is ignored, a record missing a required field is dropped):
  train_num, run_date ("YYYY-MM-DD"), railroad ("MNR")                              required
  location {latitude, longitude, timestamp (unix s), source (GPS | SIGNAL | OPER | SCHED | WMDS),
            speed (mph: 2,900 GPS pairs agree with distance / time to 0.1 %), heading (degrees true; 0.0 also
            means "not given": 41 % of GPS fixes, so 0 is read as none)}             lat / lon / timestamp required
  details {direction (E | W), branch_id (NH, NC, DN, WH, HU...), branch, headsign,
           stops [{code, sched_time, act_time, act_arrive_time, act_depart_time, stop_status (EN_ROUTE |
                   ARRIVING | BERTHED | DEPARTED), stop_type, sign_track, t2s_track, posted, track_change}],
           turf {length, location_mp}: miles along this train's own route from where it starts (not a railroad
                milepost: a westbound train to Grand Central is at MP length - location_mp)}   direction required
  consist {fleet, actual_len, sched_len, occupancy, cars [{number, type, loading (EMPTY | MANY_SEATS |
           FEW_SEATS | SRO | NO_DATA), passengers, locomotive, revenue}]}
  status {otp (seconds; negative = late), held, canceled}
  alerts [{header, text, status, status_level, branches, railroad, is_planned}]      on some trains; header and text
        can be HTML ("<p>The <strong>4:38pm train from ...</strong> is ...</p>"): read as plain text (alert_text), never
        shown as HTML. No trip id or stop: an alert that names a train ("the 4:38pm train from X to Y", "Train 1234")
        is matched to a train by its number, or a stop's scheduled time and name (named_trains, about_train); the rest
        are the line's.

Which trains: Metro-North's New Haven Line (branch_id NH) and any other train inside the map's area (a New Canaan
or Danbury train on the main line). Matched to a GTFS-realtime train by train number and run date.

Schedule (adaptive, like the aircraft layer): FAST_S (5 s; [trains] traintime_every_s, 5 to 60) while a page has
the Trains layer on (it asked within VIEW_S) or an emergency incident (active or pending, changed within HOT_AGE_S)
is within NEAR_LINE_M (1 km) of the line; IDLE_S (30 s) while someone looks otherwise (the GTFS-realtime feed's
demand, trains.DEMAND_S); nothing with nobody looking and no such incident. Only with the small answer: should
the poll ever come back bigger than SMALL_MAX_BYTES (the polylines back in it), never faster than IDLE_S. One request at a time on one thread (the polls,
the route lines and the countdowns all go through it, and a lock covers a manual Refresh now). A failure backs
off (20 s doubling to 10 min; a 429 5 min; 401 / 403 30 min), whatever the rate. Each good poll is pushed to the
pages at once (Trains.on_change: the state stream's trains_rev). The User-Agent (UA) says who we are and why.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SOURCE = "MTA TrainTime"
BASE_URL = "https://backend-unified.mylirr.org"
LOCATIONS = "/locations?geometry=TRACK_TURF&railroad=MNR"
GEOMETRY = "/locations?geometry=TRACK&railroad=MNR"
COUNTDOWN = "/countdown/{code}?include_passed=false&hours=1.0"
UA = "Port Chester Fire Department - sizeup emergency situational awareness application (github.com/actualsecurity/sizeup)"
HEADERS = {"Accept-Version": "3.0", "Accept": "application/json", "Accept-Encoding": "gzip"}
FAST_S = 5.0                    # a page has the layer on, or an active incident near the line ([trains] traintime_every_s)
EVERY_RANGE_S = (5.0, 60.0)
IDLE_S = 30.0                   # otherwise
VIEW_S = 15.0                   # a page that asked for the layer this recently has it on (it asks every 5 s)
NEAR_LINE_M = 1000.0            # an emergency incident this close to the line: fast
HOT_AGE_S = 2 * 3600.0          # ... active or pending, and changed (created, or anything logged) within this
HOT_CHECK_S = 15.0              # the incidents looked at this often
SMALL_MAX_BYTES = 2 * 1024 * 1024   # the poll's answer, unpacked: TRACK_TURF is ~330 KB; the polylines make it ~5.5 MB
EVERY_S = FAST_S
FALLBACK_AFTER = 3              # bad polls in a row (failed, or the shape changed): GTFS-realtime alone
MAX_AGE_S = 90.0                # a good read older than this is not merged
POS_MAX_AGE_S = 40.0            # a train's location older than this: its GTFS-realtime position instead
POS_SOURCES = ("GPS", "SIGNAL") # locations good enough to draw (OPER, SCHED: an operator's or the timetable's)
BACKOFF_S = (20.0, 600.0)
RATE_LIMITED_S = 300.0
REFUSED_S = 1800.0
TIMEOUT_S = 20.0
MAX_BYTES = 8 * 1024 * 1024     # the poll, decompressed (~330 KB today)
MAX_GEOMETRY_BYTES = 48 * 1024 * 1024   # the route lines, decompressed (~5.5 MB today)
GEOMETRY_EVERY_S = 86400.0
GEOMETRY_MISSING_S = 3600.0     # a route we have no line for: read again at most this often
GEOMETRY_MARGIN_DEG = 0.02      # a route line kept this far beyond the area (about 2 km)
SIMPLIFY_M = 3.0
COUNTDOWN_TTL_S = 30.0
COUNTDOWN_DEMAND_S = 10.0       # a station box asked this recently: its countdown read (the page asks every 30 s)
DROP_FRAC = 0.5                 # more than this share of the records invalid: the shape changed
MAX_CARS = 16
LOADS = ("EMPTY", "MANY_SEATS", "FEW_SEATS", "SRO", "NO_DATA")
NH = "NH"
GCT = "0NY"                     # Grand Central's code: the New Haven Line's mileposts count from it
MILE_M = 1609.344
WORD = re.compile(r"[A-Z0-9_]{1,24}")
CODE = re.compile(r"[0-9A-Z]{2,4}")
DATE = re.compile(r"\d{4}-\d\d-\d\d")
TRAIN = re.compile(r"[0-9A-Za-z]{1,8}")


class SchemaError(ValueError):
    """The answer isn't what we know: not a list of train records, or most records fail validation."""


# ---- validation: one record at a time, a bad one dropped -----------------------------------------------------


def _num(v, lo: float = -math.inf, hi: float = math.inf) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return None
    return float(v)


def _ts(v) -> int | None:
    n = _num(v, 1_000_000_000, 4_000_000_000)
    return int(n) if n is not None else None


def _word(v, pat: re.Pattern = WORD) -> str:
    return v if isinstance(v, str) and pat.fullmatch(v) else ""


def _text(v, n: int = 200) -> str:
    return " ".join(v.split())[:n] if isinstance(v, str) else ""


def _bool(v) -> bool:
    return v is True


def _track(v) -> str:
    return v.strip()[:4] if isinstance(v, str) and re.fullmatch(r"\s*[0-9A-Za-z]{1,4}\s*", v) else ""


def _stop(s) -> dict | None:
    if not isinstance(s, dict) or not _word(s.get("code"), CODE):
        return None
    act = _ts(s.get("act_depart_time")) or _ts(s.get("act_arrive_time")) or _ts(s.get("act_time"))
    sign, t2s = _track(s.get("sign_track")), _track(s.get("t2s_track"))
    return {"code": s["code"], "sched": _ts(s.get("sched_time")), "act": act, "status": _word(s.get("stop_status")),
            "type": _word(s.get("stop_type")), "track": sign or t2s, "posted": _bool(s.get("posted")) and bool(sign),
            "track_change": _bool(s.get("track_change"))}


def _car(c) -> dict | None:
    if not isinstance(c, dict):
        return None
    n = c.get("number")
    load = _word(c.get("loading"))
    pax = _num(c.get("passengers"), 0, 1000)
    return {"n": int(n) if _num(n, 0, 1e6) is not None else None, "type": _word(c.get("type")),
            "load": load if load in LOADS else ("NO_DATA" if not load else load),
            "pax": int(pax) if pax is not None else None, "loco": _bool(c.get("locomotive")),
            "revenue": c.get("revenue") is not False}


# ---- alerts: the MTA's HTML as plain text, the train an alert names, a one-line brief ----------------------------

ALERT_RAW_MAX = 8000            # an alert's header / text read this far (the HTML), before it is made plain text
PARA = " \u00b7 "               # between an alert's paragraphs
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr", "section"}
_SKIP = {"script", "style", "head", "title", "template", "noscript", "iframe", "object", "svg", "math", "select", "textarea"}


class _Plain(HTMLParser):
    """HTML -> its text nodes: tags, attributes and comments dropped, entities decoded, script / style bodies skipped,
    a block element (p, br, li...) starts a new paragraph."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras: list[list[str]] = [[]]
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.paras.append([])

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self.paras.append([])

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.paras.append([])

    def handle_data(self, data):
        if not self.skip:
            self.paras[-1].append(data)


def alert_text(v, n: int = 600) -> str:
    """An alert's header or text (HTML, or plain) -> plain text: no tags, entities decoded (&amp; &nbsp; &#8211;),
    whitespace collapsed, paragraphs joined by " · "; at most n characters ("…" when cut)."""
    if not isinstance(v, str) or not v.strip():
        return ""
    p = _Plain()
    try:
        p.feed(v[:ALERT_RAW_MAX])
        p.close()
    except Exception:  # noqa: BLE001 - HTMLParser is lenient; anything it can't read is dropped, never shown raw
        return ""
    out = PARA.join(t for t in (" ".join("".join(x).split()) for x in p.paras) if t)
    out = "".join(c for c in out if c.isprintable())
    return out if len(out) <= n else out[:n - 1].rstrip() + "\u2026"


_AMPM = r"(\d{1,2})(?::(\d\d))?\s*([ap])\.?\s*m\b\.?"
_TIME_TRAIN = re.compile(_AMPM + r"\s+(?:[A-Za-z]+\s+){0,2}?trains?\b(.{0,160})", re.I)
_FROM_TO = re.compile(r"^\s*(?:from|out of|departing|leaving)\s+(.+?)(?:\s+(?:to|for|bound for)\s+(.+?))?"
                      r"(?=\s+(?:is|are|has|have|was|were|will|due|that|which|operating|departing|arriving|scheduled|"
                      r"running|now|and|with|at|has been)\b|\s*[.,;:()\u00b7]|$)", re.I)
_TO_ONLY = re.compile(r"^\s*(?:to|for|bound for)\s+(.+?)(?=\s+(?:is|are|has|was|will|due|operating|from|at)\b|\s*[.,;:()\u00b7]|$)", re.I)
_TRAIN_NUM = re.compile(r"\btrain\s*(?:#\s*|no\.?\s*|number\s+)?(\d{3,5})\b(?!\s*[-\u2013]|\s*(?:min|minutes|am|pm|a\.m|p\.m))", re.I)


def named_trains(text: str) -> list[dict]:
    """The trains an alert's plain text names: [{num}] ("Train 1234", "train #1234") and [{hm "16:38", from, to}]
    ("the 4:38pm train from New Haven-State St to Grand Central"; from / to "" when not said)."""
    out = [{"num": m.group(1)} for m in _TRAIN_NUM.finditer(text or "")]
    for m in _TIME_TRAIN.finditer(text or ""):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if not (1 <= h <= 12 and 0 <= mi <= 59):
            continue
        h = h % 12 + (12 if ap == "p" else 0)
        frm = to = ""
        ft = _FROM_TO.match(m.group(4))
        if ft:
            frm, to = ft.group(1).strip(), (ft.group(2) or "").strip()
        else:
            t = _TO_ONLY.match(m.group(4))
            to = t.group(1).strip() if t else ""
        out.append({"hm": f"{h:02d}:{mi:02d}", "from": frm[:60], "to": to[:60]})
    return out


def _norm(name: str) -> str:
    n = " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())
    return re.sub(r"^grand central(?: terminal)?$|^gct$", "grand central", n)


def _same_place(said: str, ours: str, known: set[str] = frozenset()) -> bool:
    """A station name as an alert says it and as the timetable does: equal; or, when what the alert says is no
    station's full name (known), one the other's start ("New Haven Union Station", "New Haven")."""
    a, b = _norm(said), _norm(ours)
    return bool(a and b) and (a == b or (a not in known and (a.startswith(b + " ") or b.startswith(a + " "))))


def _local_hm(t: int | None) -> str:
    if not t:
        return ""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(t, ZoneInfo("America/New_York")).strftime("%H:%M")
    except Exception:  # noqa: BLE001 - no tz database: no time match (the alert stays the line's)
        return ""


def about_train(named: list[dict], rec: dict, names: dict[str, str] | None = None) -> bool:
    """True when an alert names this train: its number, or one of its stops (the first, when the alert says no
    station) at the named scheduled time, the station's name agreeing and the destination too when both are known.
    False when it names none, or only others, or it can't tell (no stops or times): the line's alert then."""
    names = names or {}
    known = {_norm(v) for v in names.values()}
    stops = [s for s in rec.get("stops") or [] if s.get("sched")]
    last = names.get(stops[-1]["code"], "") if stops else ""
    for n in named:
        if n.get("num"):
            if n["num"] == rec.get("train"):
                return True
            continue
        if n.get("to") and last and not _same_place(n["to"], last, known):
            continue
        for s in (stops if n.get("from") else stops[:1]):
            if _local_hm(s["sched"]) == n["hm"] and (not n.get("from") or _same_place(n["from"], names.get(s["code"], ""), known)):
                return True
    return False


_LATE = re.compile(r"(\d{1,3}(?:\s*(?:-|\u2013|to)\s*\d{1,3})?)\s*(?:minutes?|mins?)\s+(late|behind|delayed|later)", re.I)
_DELAYS = re.compile(r"delays?\s+of\s+(?:up\s+to\s+)?(\d{1,3}(?:\s*(?:-|\u2013|to)\s*\d{1,3})?)\s*(?:minutes?|mins?)", re.I)
_CAUSES = (("mechanical", "mechanical"), ("signal", "signal"), ("switch", "switch"), ("police", "police"),
           ("medical", "medical"), ("trespass", "trespasser"), ("struck", "struck"), ("disabled train", "disabled train"),
           ("power", "power"), ("overhead wire", "wires"), ("catenary", "wires"), ("track condition", "track"),
           ("track work", "track work"), ("weather", "weather"), ("crew", "crew"), ("congestion", "congestion"),
           ("fire", "fire"), ("debris", "debris"), ("tree", "tree"), ("flood", "flooding"))


def brief(text: str) -> str:
    """An alert in a few words: "20-25 min late (mechanical)", "delays up to 15 min", "cancelled"; else its start."""
    t = text or ""
    low = t.lower()
    m = _LATE.search(t)
    d = _DELAYS.search(t) if not m else None
    span = lambda g: re.sub(r"\s*(?:-|\u2013|to)\s*", "-", g)
    what = (f"{span(m.group(1))} min late" if m
            else f"delays up to {span(d.group(1))} min" if d
            else "cancelled" if re.search(r"\bcancel+ed\b", low)
            else "held" if re.search(r"\bheld\b", low)
            else "")
    if not what:
        first = t.split(PARA)[0]
        return first if len(first) <= 80 else first[:79].rstrip() + "\u2026"
    cause = next((w for k, w in _CAUSES if k in low), "")
    return f"{what} ({cause})" if cause else what


def _short_place(name: str) -> str:
    return "GCT" if _norm(name) == "grand central" else name[:24]


def train_label(named: list[dict]) -> str:
    """The train an alert is about, for the line's row: "4:38p New Haven-State St\u2013GCT", "Train 1234"; "" when none."""
    for n in named:
        if n.get("hm"):
            h, mi = (int(x) for x in n["hm"].split(":"))
            t = f"{(h % 12) or 12}:{mi:02d}{'a' if h < 12 else 'p'}"
            places = "\u2013".join(_short_place(x) for x in (n["from"], n["to"]) if x)
            return f"{t} {places}".strip()
    return next((f"Train {n['num']}" for n in named if n.get("num")), "")


def _alert(a) -> dict | None:
    if not isinstance(a, dict):
        return None
    header = alert_text(a.get("header"), 300)
    if not header:
        return None
    text = alert_text(a.get("text"), 1000)
    if text == header:
        text = ""
    named = named_trains(f"{header}{PARA}{text}")
    br = a.get("branches")
    return {"header": header, "text": text, "brief": brief(f"{header}{PARA}{text}"), "named": named,
            "about": train_label(named), "status": _text(a.get("status"), 60),
            "level": int(_num(a.get("status_level"), 0, 100) or 0), "railroad": _word(a.get("railroad")),
            "branches": [b for b in br if isinstance(b, str) and WORD.fullmatch(b)] if isinstance(br, list) else [],
            "planned": _bool(a.get("is_planned")), "start": _ts(a.get("start_time"))}


def record(x, keep_polyline: bool = False) -> dict | None:
    """One train record -> our shape, or None when a required field is missing or the wrong type."""
    if not isinstance(x, dict):
        return None
    num, date, rr = x.get("train_num"), x.get("run_date"), x.get("railroad")
    det, loc = x.get("details"), x.get("location")
    if not (isinstance(num, str) and TRAIN.fullmatch(num) and isinstance(date, str) and DATE.fullmatch(date)
            and _word(rr) and isinstance(det, dict) and isinstance(loc, dict)):
        return None
    lat, lon, at = _num(loc.get("latitude"), -90, 90), _num(loc.get("longitude"), -180, 180), _ts(loc.get("timestamp"))
    d = det.get("direction")
    if lat is None or lon is None or at is None or d not in ("E", "W") or (lat == 0 and lon == 0):
        return None
    cons = x.get("consist") if isinstance(x.get("consist"), dict) else {}
    st = x.get("status") if isinstance(x.get("status"), dict) else {}
    turf = det.get("turf") if isinstance(det.get("turf"), dict) else {}
    stops = [s for s in map(_stop, det.get("stops") if isinstance(det.get("stops"), list) else []) if s]
    cars = [c for c in map(_car, cons.get("cars") if isinstance(cons.get("cars"), list) else []) if c][:MAX_CARS]
    alerts = [a for a in map(_alert, x.get("alerts") if isinstance(x.get("alerts"), list) else []) if a]
    heading = _num(loc.get("heading"), 0, 360)
    otp = _num(st.get("otp"), -86400, 86400)
    occ = _word(cons.get("occupancy"))
    out = {"train": num, "run_date": date, "railroad": rr, "branch_id": _word(det.get("branch_id")),
           "branch": _text(det.get("branch"), 40), "headsign": _text(det.get("headsign"), 60), "dir": d,
           "lat": round(lat, 6), "lon": round(lon, 6), "loc_at": at, "loc_src": _word(loc.get("source")),
           "speed_mph": _num(loc.get("speed"), 0, 200), "heading": heading if heading else None,
           "route_mp": _num(turf.get("location_mp"), 0, 500), "route_len": _num(turf.get("length"), 0, 500),
           "fleet": _word(cons.get("fleet")), "cars_actual": int(_num(cons.get("actual_len"), 0, 50) or 0) or None,
           "cars_sched": int(_num(cons.get("sched_len"), 0, 50) or 0) or None,
           "occupancy": occ if occ else "NO_DATA", "cars": cars,
           "late_s": int(-otp) if otp is not None else None,          # otp < 0: late
           "held": _bool(st.get("held")), "canceled": _bool(st.get("canceled")), "stops": stops, "alerts": alerts}
    if keep_polyline and isinstance(det.get("polyline"), str):
        out["polyline"] = det["polyline"]
    return out


def parse_locations(body: bytes, keep_polyline: bool = False) -> dict:
    """/locations (or /countdown) -> {records: [...], total, dropped}. Raises SchemaError when it isn't a list of
    train records, or more than DROP_FRAC of them fail validation (the API changed shape: fall back)."""
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as e:
        raise SchemaError(f"not JSON ({type(e).__name__})") from None
    if not isinstance(doc, list):
        raise SchemaError(f"a {type(doc).__name__}, not a list of trains")
    recs = [r for r in (record(x, keep_polyline) for x in doc) if r is not None]
    dropped = len(doc) - len(recs)
    if doc and dropped > DROP_FRAC * len(doc):
        raise SchemaError(f"{dropped} of {len(doc)} train records not in the known shape")
    return {"records": recs, "total": len(doc), "dropped": dropped}


# ---- what a record says ------------------------------------------------------------------------------------------


def in_box(lat: float, lon: float, bbox) -> bool:
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n


def ours(rec: dict, bbox) -> bool:
    """Metro-North's New Haven Line, or any Metro-North train inside the map's area."""
    return rec["railroad"] == "MNR" and (rec["branch_id"] == NH or in_box(rec["lat"], rec["lon"], bbox))


def milepost(rec: dict) -> float | None:
    """The railroad milepost (miles from Grand Central) from turf, when the train's route starts (eastbound) or
    ends (westbound) at Grand Central; None otherwise (turf counts along the train's own route)."""
    mp, ln, stops = rec.get("route_mp"), rec.get("route_len"), rec.get("stops") or []
    if mp is None or not stops:
        return None
    if rec["dir"] == "E" and stops[0]["code"] == GCT:
        return round(mp, 1)
    if rec["dir"] == "W" and stops[-1]["code"] == GCT and ln is not None and ln >= mp:
        return round(ln - mp, 1)
    return None


def next_stop(rec: dict) -> dict | None:
    """The first stop it hasn't left (passengers' stops: not an employee-only one)."""
    return next((s for s in rec.get("stops") or [] if s["status"] != "DEPARTED" and s["type"] != "EMPLOYEE"), None)


def prev_stop(rec: dict) -> dict | None:
    """The last stop it has left (passengers' stops), for the track it left on (#371)."""
    return next((s for s in reversed(rec.get("stops") or []) if s["status"] == "DEPARTED" and s["type"] != "EMPLOYEE"), None)


def route_key(rec: dict) -> str:
    """The route a train runs, by its first and last stop ("2SM>0NY"): its line on the map."""
    st = rec.get("stops") or []
    return f"{st[0]['code']}>{st[-1]['code']}" if len(st) >= 2 else ""


def passengers(rec: dict) -> int | None:
    n = [c["pax"] for c in rec["cars"] if c["pax"] is not None]
    return sum(n) if n else None


def facts(rec: dict, stations: dict[str, str] | None = None) -> dict:
    """What the popup shows of a train from this source (the hub adds these to the train as "tt")."""
    nx, pv = next_stop(rec), prev_stop(rec)
    names = stations or {}
    return {"src": SOURCE, "loc_src": rec["loc_src"], "loc_at": rec["loc_at"], "speed_mph": rec["speed_mph"],
            "heading": rec["heading"], "mp": milepost(rec), "route_mp": rec["route_mp"], "branch": rec["branch_id"],
            "fleet": rec["fleet"], "cars": rec["cars_actual"] or (len(rec["cars"]) or None), "cars_sched": rec["cars_sched"],
            "occupancy": rec["occupancy"], "pax": passengers(rec),
            "car_loads": [{"n": c["n"], "type": c["type"], "load": c["load"], "pax": c["pax"], "loco": c["loco"]} for c in rec["cars"]],
            "late_s": rec["late_s"], "held": rec["held"], "canceled": rec["canceled"],
            "next": {"code": nx["code"], "name": names.get(nx["code"], nx["code"]), "track": nx["track"], "posted": nx["posted"],
                     "track_change": nx["track_change"], "sched": nx["sched"], "status": nx["status"]} if nx else None,
            "prev": ({"code": pv["code"], "name": names.get(pv["code"], pv["code"]), "track": pv["track"], "posted": pv["posted"]}
                     if pv and pv["track"] else None),
            "route": route_key(rec),
            # the alerts riding on this record that name this train (#385): its box says them as "This train"
            "alerts": [{k: a[k] for k in ALERT_KEYS} for a in rec.get("alerts") or []
                       if a["railroad"] in ("MNR", "") and a["named"] and about_train(a["named"], rec, names)]}


ALERT_KEYS = ("header", "text", "brief", "about", "status", "level", "planned", "start")


def branch_alerts(records, branch: str = NH) -> list[dict]:
    """Metro-North's alerts for the branch, once each (they ride along on train records)."""
    seen, out = set(), []
    for r in records:
        for a in r.get("alerts") or []:
            if a["railroad"] in ("MNR", "") and branch in a["branches"] and a["header"] not in seen:
                seen.add(a["header"])
                out.append({k: a[k] for k in ALERT_KEYS})
    return sorted(out, key=lambda a: -a["level"])


# ---- the merge: GTFS-realtime trains, with this source's position and facts ---------------------------------------


def _gdate(s: str) -> str:
    """GTFS start_date "20261109" -> "2026-11-09"."""
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if isinstance(s, str) and re.fullmatch(r"\d{8}", s) else ""


def match(t: dict, by_num: dict[str, list[dict]], misses: list | None = None) -> dict | None:
    """The record for a GTFS-realtime train: its number and run date; with no start date, the only one of that number
    (or the latest run date)."""
    cands = by_num.get(str(t.get("train") or ""), [])
    if not cands:
        return None
    day = _gdate(t.get("start_date") or "")
    if day:
        r = next((r for r in cands if r["run_date"] == day), None)
        if r is None and misses is not None:             # the number, not the date: logged by the hub (once)
            misses.append((str(t.get("train")), day, sorted({c["run_date"] for c in cands})))
        return r
    return max(cands, key=lambda r: r["run_date"])


def usable_position(rec: dict, now: float) -> bool:
    return rec["loc_src"] in POS_SOURCES and now - rec["loc_at"] <= POS_MAX_AGE_S


def merge(feed: dict, records: list[dict], now: float, at: float | None = None, stations: dict[str, str] | None = None,
          gtfs_seen: dict[str, float] | None = None) -> dict:
    """feed (trains.parse_feed's) with each matched train's position replaced by this source's (when usable) and its
    facts added as "tt". The feed's own dicts are not changed. Trains this source has and GTFS-realtime hasn't
    are added (position and facts only). at: when the records were read ("tt_at": a new read for the Tracker).
    gtfs_seen: {train id: when its current GTFS-realtime position first appeared} (the feed's own vehicle timestamp is
    always its header's, so a position's age is ours to keep); a TrainTime position replaces it only when as new."""
    by_num: dict[str, list[dict]] = {}
    for r in records:
        by_num.setdefault(r["train"], []).append(r)
    trains, used, misses = {}, set(), []
    for k, t in feed.get("trains", {}).items():
        r = match(t, by_num, misses)
        if r is None:
            trains[k] = t
            continue
        used.add(id(r))
        trains[k] = _with(t, r, now, stations, (gtfs_seen or {}).get(k))
    for r in records:
        if id(r) in used or not usable_position(r, now):
            continue
        key = f"tt:{r['train']}:{r['run_date']}"
        trains[key] = _with({"id": key, "train": r["train"], "trip_id": "", "route_id": "", "start_date": r["run_date"].replace("-", ""),
                             "stops": [], "delay": None, "pos": None, "vstatus": "", "vstop": ""}, r, now, stations)
    return {"timestamp": feed.get("timestamp"), "tt_at": at, "trains": trains, "tt_misses": misses}


def _with(t: dict, r: dict, now: float, stations, gtfs_at: float | None = None) -> dict:
    t = dict(t)
    t["tt"] = facts(r, stations)
    t["dir_hint"] = 1 if r["dir"] == "E" else -1
    gtfs_ts = (gtfs_at if gtfs_at is not None else (t.get("pos") or {}).get("ts") or 0) if t.get("pos") else 0
    if usable_position(r, now) and r["loc_at"] >= gtfs_ts:          # only a position at least as new as GTFS-realtime's
        t["pos"] = {"lat": r["lat"], "lon": r["lon"], "bearing": r["heading"], "ts": r["loc_at"], "src": "traintime",
                    "speed": r["speed_mph"] * 1609.344 / 3600 if r["speed_mph"] is not None else None}
    return t


# ---- the route lines (geometry=TRACK) --------------------------------------------------------------------------


def decode_polyline(s: str, precision: int = 6) -> list[tuple[float, float]]:
    """A Google encoded polyline -> [(lon, lat)]. Raises ValueError on garbage."""
    out, i, lat, lon, f = [], 0, 0, 0, 10.0 ** precision
    n = len(s)
    while i < n:
        vals = []
        for _ in range(2):
            shift = res = 0
            while True:
                if i >= n:
                    raise ValueError("truncated polyline")
                b = ord(s[i]) - 63
                i += 1
                if not 0 <= b < 64:
                    raise ValueError("not a polyline")
                res |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(res >> 1) if res & 1 else res >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append((lon / f, lat / f))
    return out


def clip_runs(pts: list[tuple[float, float]], bbox, margin: float = GEOMETRY_MARGIN_DEG) -> list[list[tuple[float, float]]]:
    """The parts of the line inside the area (plus margin): one or more runs of points."""
    w, s, e, n = bbox
    box = (w - margin, s - margin, e + margin, n + margin)
    runs, cur = [], []
    for lon, lat in pts:
        if box[0] <= lon <= box[2] and box[1] <= lat <= box[3]:
            cur.append((lon, lat))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2]


def routes_from(records: list[dict], bbox) -> dict[str, list[list[list[float]]]]:
    """{route key: [run of [lon, lat]...]}: each route's line, clipped to the area and simplified."""
    from sizeup.trains import simplify
    out = {}
    for r in records:
        key, poly = route_key(r), r.get("polyline")
        if not key or key in out or not poly or not ours(r, bbox):
            continue
        try:
            pts = decode_polyline(poly)
        except ValueError:
            continue
        runs = [[[round(a, 6), round(b, 6)] for a, b in simplify(run, SIMPLIFY_M)] for run in clip_runs(pts, bbox)]
        if runs:
            out[key] = runs
    return out


# ---- the client ------------------------------------------------------------------------------------------------


def http_get(url: str, timeout: float, limit: int) -> tuple[int, bytes]:
    """(status, body decompressed): GET only, our User-Agent, Accept-Version 3.0. Errors as HTTPError / URLError /
    OSError / ValueError (too big)."""
    req = Request(url, headers={"User-Agent": UA, **HEADERS})
    with urlopen(req, timeout=timeout) as r:
        raw = r.read(limit + 1)
        gz = (r.headers.get("Content-Encoding") or "").lower() == "gzip"
        status = r.status
    if len(raw) > limit:
        raise ValueError(f"more than {limit // (1024 * 1024)} MB")
    if gz:
        d = gzip.GzipFile(fileobj=io.BytesIO(raw))
        raw = d.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(f"more than {limit // (1024 * 1024)} MB unpacked")
    return status, raw


class TrainTime:
    """The poller: the latest good read (records), how the source is doing, the route lines, the countdowns."""

    def __init__(self, enabled: bool = True, base_url: str = "", every_s: float = EVERY_S, bbox=(-73.845, 40.900, -73.530, 41.060),
                 cache_dir: str | Path | None = None, fetch: Callable | None = None, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, demand: Callable[[], bool] = lambda: True,
                 fast: Callable[[], bool] = lambda: False,
                 on_update: Callable[[], None] | None = None, fallback_after: int = FALLBACK_AFTER):
        self.base = (base_url or BASE_URL).strip().rstrip("/") if enabled else ""
        self.enabled = bool(enabled) and bool(self.base)
        self.every_s = max(EVERY_RANGE_S[0], min(EVERY_RANGE_S[1], float(every_s)))
        self.bbox = tuple(float(x) for x in bbox)
        self.routes_file = Path(cache_dir) / "traintime_routes.json" if cache_dir else None
        self.fetch = fetch or http_get
        self.log, self.clock, self.demand, self.on_update = log, clock, demand, on_update
        self.fast = fast                                 # a page has the layer on, or an incident is near the line
        self.fallback_after = max(1, int(fallback_after))
        self.lock = threading.Lock()
        self._io = threading.Lock()                      # one request at a time (a manual Refresh now included)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.records: list[dict] = []
        self.alerts: list[dict] = []
        self.st = {"at": None, "error": "", "next": 0.0, "last": None, "fails": 0, "bad": 0, "n": 0, "total": 0, "dropped": 0,
                   "dropped_total": 0, "bytes": 0, "since": None}
        self.geo = {"at": None, "error": "", "next": 0.0, "fails": 0, "missing_at": None}
        self.routes: dict[str, list] = {}
        self.countdowns: dict[str, dict] = {}
        self._load_routes()

    # -- what the hub reads -------------------------------------------------------------------------------------

    def active(self) -> bool:
        """Merge this source now: its last read good, not too old, and not FALLBACK_AFTER bad polls in a row."""
        at = self.st["at"]
        return self.enabled and at is not None and self.st["bad"] < self.fallback_after and self.clock() - at <= MAX_AGE_S

    def state(self) -> tuple[str, str]:
        """(state, why): off | idle (nobody looking, no emergency near the line) | starting | ok | fallback."""
        if not self.enabled:
            return "off", "off ([trains] traintime = false)"
        if self.active():
            return "ok", ""
        if self.st["bad"] >= self.fallback_after:
            return "fallback", f"GTFS-realtime only: {self.st['bad']} bad polls ({self.st['error'] or 'no answer'})"
        if not self.wanted():
            return "idle", "read only while someone looks, or an emergency is near the line"
        if self.st["at"] is not None and self.clock() - self.st["at"] > MAX_AGE_S:
            return "fallback", f"GTFS-realtime only: last good read {round(self.clock() - self.st['at'])} s ago"
        return "starting", self.st["error"] or "first read"

    def interval(self) -> float:
        """Seconds between polls now: fast (a page has the layer on, or an incident near the line) or idle; fast only
        with the small answer (the size guard)."""
        try:
            hot = bool(self.fast())
        except Exception:  # noqa: BLE001 - a fault in the check never stops the polls
            hot = False
        if not hot or self.big():
            return max(IDLE_S, self.every_s)
        return self.every_s

    def big(self) -> bool:
        """The last answer was over SMALL_MAX_BYTES (the polylines back in it): never faster than IDLE_S."""
        return self.st["bytes"] > SMALL_MAX_BYTES

    def wanted(self) -> bool:
        """Poll at all: someone looks (the GTFS-realtime feed's demand), or the fast rate's reasons hold."""
        try:
            return bool(self.demand()) or bool(self.fast())
        except Exception:  # noqa: BLE001
            return False

    def due_at(self) -> float:
        """The next poll: the rate's turn, never before a back-off ends."""
        last = self.st["last"]
        return max(self.st["next"], (last + self.interval()) if last is not None else 0.0)

    def current(self) -> list[dict]:
        with self.lock:
            return list(self.records) if self.active() else []

    def status(self) -> dict:
        state, why = self.state()
        return {"state": state, "why": why, "at": self.st["at"], "error": self.st["error"], "bad": self.st["bad"],
                "fallback_after": self.fallback_after, "n": self.st["n"], "dropped": self.st["dropped"],
                "dropped_total": self.st["dropped_total"], "bytes": self.st["bytes"], "every_s": self.every_s,
                "routes": len(self.routes), "routes_at": self.geo["at"], "routes_error": self.geo["error"],
                "rate": "off" if not self.enabled else "none" if not self.wanted() else "fast" if self.interval() < IDLE_S else "idle",
                "big": self.big(),
                "interval_s": self.interval(), "next": self.due_at() if self.enabled else None}

    # -- the schedule ----------------------------------------------------------------------------------------------

    def start(self) -> TrainTime:
        if self.enabled and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="traintime", daemon=True)
            self._thread.start()
            self.log(f"trains: MTA TrainTime every {self.every_s:g} s while the layer is on or an incident is near the line, "
                     f"else {IDLE_S:g} s (GTFS-realtime the fallback); "
                     f"{len(self.routes)} route lines cached")
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - never stop the schedule
                self.log(f"trains: TrainTime: {type(e).__name__}: {e}")
            self._wake.wait(max(0.1, min(1.0, self.due_at() - self.clock())))
            self._wake.clear()

    def tick(self) -> None:
        """One turn: the poll when due (fast or idle); a countdown a station box asked for; the route lines when due
        (daily, or a route missing) while someone looks. One request at a time: this thread makes them all."""
        if not self.enabled:
            return
        now = self.clock()
        looking = self.demand()
        if self.wanted() and now >= self.due_at():
            self.poll()
        resting = self.clock() < self.st["next"]        # the poll's back-off holds the other requests too
        for code in [c for c, v in list(self.countdowns.items()) if self.clock() - v["asked"] <= COUNTDOWN_DEMAND_S
                     and self.clock() >= v["next"]] if not resting else []:
            self.fetch_countdown(code)
        now = self.clock()
        if looking and not resting and now >= self.geo["next"] and (self.geo["at"] is None or now - self.geo["at"] >= GEOMETRY_EVERY_S
                                                    or self._missing(now)):
            self.refresh_routes()

    def _missing(self, now: float) -> bool:
        if self.geo["at"] is not None and now - self.geo["at"] < GEOMETRY_MISSING_S:
            return False
        with self.lock:
            return any(route_key(r) and route_key(r) not in self.routes for r in self.records if in_box(r["lat"], r["lon"], self.bbox))

    def _backoff(self, st: dict, code: int, why: str, what: str) -> None:
        st["fails"] += 1
        if code in (401, 403):
            wait, why = REFUSED_S, f"refused ({why})"
        elif code == 429:
            wait, why = RATE_LIMITED_S, f"rate limit ({why})"
        else:
            wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(st["fails"] - 1, 20))
        st.update(error=why, next=self.clock() + wait)
        self.log(f"trains: TrainTime {what}: {why}; next try in {wait:.0f} s")

    def _get(self, path: str, limit: int) -> bytes:
        with self._io:
            code, body = self.fetch(self.base + path, TIMEOUT_S, limit)
        if code != 200:
            raise HTTPError(self.base + path, code, f"HTTP {code}", None, None)
        return body

    def poll(self) -> bool:
        """/locations now. True when read and in the known shape. Never raises."""
        if not self.enabled:
            return False
        was = self.active()
        try:
            body = self._get(LOCATIONS, MAX_BYTES)
            doc = parse_locations(body)
        except HTTPError as e:
            return self._bad(e.code, f"HTTP {e.code}", was)
        except (URLError, OSError, ValueError) as e:
            return self._bad(0, f"{type(e).__name__}: {getattr(e, 'reason', e)}", was)
        except Exception as e:  # noqa: BLE001 - a surprise in the answer must not stop the schedule
            return self._bad(0, f"{type(e).__name__}: {e}", was)
        finally:
            self.st["last"] = self.clock()
        now = self.clock()
        keep = [r for r in doc["records"] if ours(r, self.bbox)]
        with self.lock:
            self.records = keep
            self.alerts = branch_alerts(doc["records"])
            if self.st["bad"] >= self.fallback_after:
                self.log(f"trains: TrainTime back after {self.st['bad']} bad polls; merging again")
            self.st.update(at=now, error="", fails=0, bad=0, next=0.0, n=len(keep), total=doc["total"],
                           dropped=doc["dropped"], dropped_total=self.st["dropped_total"] + doc["dropped"], bytes=len(body),
                           since=self.st["since"] or now)
        if doc["dropped"]:
            self.log(f"trains: TrainTime: {doc['dropped']} of {doc['total']} records not in the known shape, dropped")
        self._changed()
        return True

    def _bad(self, code: int, why: str, was: bool) -> bool:
        self.st["bad"] += 1
        self._backoff(self.st, code, why, "poll")
        if self.st["bad"] == self.fallback_after:
            self.log(f"trains: TrainTime: {self.st['bad']} bad polls in a row; GTFS-realtime alone until it reads again")
        if was != self.active():
            self._changed()
        return False

    def _changed(self) -> None:
        if self.on_update is not None:
            try:
                self.on_update()
            except Exception as e:  # noqa: BLE001
                self.log(f"trains: TrainTime merge: {type(e).__name__}: {e}")

    def refresh_routes(self) -> bool:
        """The route lines (geometry=TRACK, ~1 MB gzip): each route in our area, clipped and simplified, cached."""
        if not self.enabled:
            return False
        try:
            doc = parse_locations(self._get(GEOMETRY, MAX_GEOMETRY_BYTES), keep_polyline=True)
            routes = routes_from(doc["records"], self.bbox)
        except HTTPError as e:
            self._backoff(self.geo, e.code, f"HTTP {e.code}", "route lines")
            return False
        except Exception as e:  # noqa: BLE001
            self._backoff(self.geo, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}", "route lines")
            return False
        now = self.clock()
        with self.lock:
            self.routes.update(routes)            # a route not running today keeps its line
            self.geo.update(at=now, error="", fails=0, next=now + GEOMETRY_MISSING_S)
        self._save_routes()
        return True

    def _load_routes(self) -> None:
        f = self.routes_file
        if not self.enabled or f is None or not f.is_file():
            return
        try:
            doc = json.loads(f.read_text())
            routes = doc["routes"]
            if not isinstance(routes, dict):
                raise ValueError("routes")
            ok = {k: v for k, v in routes.items() if isinstance(k, str) and isinstance(v, list)
                  and all(isinstance(run, list) and all(isinstance(p, list) and len(p) == 2 for p in run) for run in v)}
            at = doc.get("at")
            self.routes = ok
            if isinstance(at, (int, float)) and at <= self.clock() + 60:
                self.geo.update(at=float(at), next=float(at) + GEOMETRY_MISSING_S)
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.log(f"trains: {f.name} not read ({type(e).__name__}); route lines fetched afresh")

    def _save_routes(self) -> None:
        f = self.routes_file
        if f is None:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            with self.lock:
                doc = {"at": self.geo["at"], "source": f"{BASE_URL}{GEOMETRY} (clipped to the area)", "routes": dict(self.routes)}
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(f)
        except OSError as e:
            self.log(f"trains: route lines not saved ({type(e).__name__}: {e})")

    def route(self, key: str) -> list | None:
        with self.lock:
            return self.routes.get(key)

    # -- a station's departures (/countdown), only while a station box asks ---------------------------------------

    def ask_countdown(self, code: str) -> dict:
        """A page's station box asks: its departures as last read (the read itself on this thread)."""
        now = self.clock()
        with self.lock:
            c = self.countdowns.setdefault(code, {"asked": now, "at": None, "next": 0.0, "fails": 0, "error": "", "rows": []})
            c["asked"] = now
            fresh = c["at"] is not None and now - c["at"] <= 2 * COUNTDOWN_TTL_S
            out = {"code": code, "at": c["at"], "error": c["error"], "rows": list(c["rows"]) if fresh else []}
        if c["at"] is None or now >= c["next"]:
            self._wake.set()
        return out

    def fetch_countdown(self, code: str) -> bool:
        c = self.countdowns.get(code)
        if c is None or not CODE.fullmatch(code):
            return False
        try:
            doc = parse_locations(self._get(COUNTDOWN.format(code=code), MAX_BYTES))
        except HTTPError as e:
            self._backoff(c, e.code, f"HTTP {e.code}", f"countdown {code}")
            return False
        except Exception as e:  # noqa: BLE001
            self._backoff(c, 0, f"{type(e).__name__}: {getattr(e, 'reason', e)}", f"countdown {code}")
            return False
        now = self.clock()
        rows = departures(doc["records"], code, now)
        with self.lock:
            c.update(at=now, error="", fails=0, next=now + COUNTDOWN_TTL_S, rows=rows)
        return True


def departures(records: list[dict], code: str, now: float, limit: int = 8) -> list[dict]:
    """A station's next departures (Metro-North only), soonest first: {train, dir, to, sched, est, late_s, track,
    posted, cars, fleet, occupancy, held, canceled}."""
    out = []
    for r in records:
        if r["railroad"] != "MNR":
            continue
        s = next((s for s in r["stops"] if s["code"] == code), None)
        if s is None or s["status"] == "DEPARTED" or s["sched"] is None:
            continue
        late = max(0, r["late_s"] or 0)
        est = s["act"] if s["act"] and s["status"] in ("BERTHED", "ARRIVING") else s["sched"] + late
        if est < now - 60:
            continue
        last = r["stops"][-1]["code"] if r["stops"] else ""
        out.append({"train": r["train"], "dir": r["dir"], "to": last, "sched": s["sched"], "est": est, "late_s": r["late_s"],
                    "track": s["track"], "posted": s["posted"], "status": s["status"],
                    "cars": r["cars_actual"] or (len(r["cars"]) or None), "fleet": r["fleet"], "occupancy": r["occupancy"],
                    "held": r["held"], "canceled": r["canceled"]})
    return sorted(out, key=lambda x: x["est"])[:limit]
