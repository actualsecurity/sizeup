"""What the CAD maps draw: rigs at their firehouses or at the incidents, and the overview's summaries.

Pure functions over the CAD state (no database, no clock of their own), so the placement rules are
tested without a browser and the page only draws what it is told:

    locate(unit, ...)     where one rig is now (estimated): the model the maps AND the recommendation use
    place(unit, ...)      one unit -> a marker spec (where, how it looks, a line to its incident) or None
    locations(...)        locate() for every unit (the recommendation's input)
    map_slice(...)        every marker, the firehouses, the incidents' pins, the fire units with no
                          home station (the "Station not set" box): state["map"]
    overview(...)         one summary per open incident (the Overview page's cards): state["overview"]

Placement, by the unit's status role (data/cad_lists.json unit_statuses) and whether it is on an event:

    free (available)                 at its home station ("available")
    free, cleared a call recently    on its way home from that call (place "returning", faded, a thin
                                     line to its station): see locate()
    oos (out of service, unmanned)   at its home station, greyed ("oos")
    dispatched / '' (acknowledged)   at its home station with a dashed line to the incident
    enroute (responding)             at its home station with a dashed line to the incident
    onscene (on location)            at the incident, fanned out around the pin
    on an event, no home station     at the incident in the outer ring (outlined: not there yet)
    no home station, not on a call   not drawn; a fire unit is listed in "Station not set"

Fire units (PCFD's and Rye Brook's apparatus: kinds in FIRE_KINDS, not mutual aid) are drawn by
default; EMS, police and agencies only while on an event, at the incident.

Home station: units[code].station in data/cad_lists.json, with the Settings units override laid over
it (set from the map's picker or Settings > Units; sizeup/cad/settings.py), else not set. The department hasn't said which apparatus is
housed where, so nothing is set by default.

Live positions (future AVL: IamResponding, Red Alert, an iPad app; #358): Cad.positions is the
single input. It returns {unit code: {"lat", "lon", "at", "heading"?, "speed"?, "source"?}}; one no
older than POSITION_MAX_AGE_S overrides where the unit is drawn (place "position"), keeping its look
and its line to the incident. Older than POSITION_STALE_S it is still drawn there, marked stale (the
page draws it hollow and faded, with its age); older than POSITION_MAX_AGE_S it is ignored and the
unit is placed by its status again. No provider feeds it yet (the source isn't chosen).

The ingest shape a future provider hands in, one fix per unit (ingest() checks it):

    {"unit": "E60",              the unit's code (Store.units() name)
     "lat": 41.0, "lon": -73.6,  WGS84 degrees
     "at": 1791400000.0,         epoch seconds when the fix was true (not when it arrived; never milliseconds)
     "heading": 270.0,           optional: degrees true, 0-360 (the page moves it on along it between fixes)
     "speed": 12.5,              optional: metres per second over the ground, 0-100
     "source": "avl-vendor"}     a short name for where it came from (shown in the chip's tooltip)

Cad._feed_position(fix) feeds one in for tests and local dev only.

Where a rig is now (locate(); user, 2026-10-07: "whether they just cleared a call next door should be
taken into account too, if within reasonable time before them ending up back in station"):

    a fresh AVL fix                  there (on the road, unless it is at its own station)
    on location at an open call      at the call
    cleared a call recently          returning: from the call toward its station, at a steady pace over
                                     the estimated drive back + the return margin (sizeup/cad/eta.py);
                                     then at its station. No station: "near" the call for the margin,
                                     then unknown. Only a call it was on location at counts (one cancelled
                                     en route: where it was is unknown, so its station, as before).
                                     Set to the "in quarters" status (Settings > Dispatch): at its station.
    dispatched / responding          where it was when it took the call: its station, or its returning
                                     position at that moment (frozen: it is committed either way)
    otherwise                        its home station; no station: unknown (None)
"""

from __future__ import annotations

import json
from pathlib import Path

from sizeup.cad import eta
from sizeup.cad.lists import ROOT
from sizeup.cad import airport

FIREHOUSES = ROOT / "data" / "firehouses.json"
FIRE_KINDS = ("chief", "engine", "ladder", "tower", "rescue", "utility", "marine")
POSITION_MAX_AGE_S = 300.0      # an AVL fix older than this is ignored (the unit is drawn by status)
POSITION_STALE_S = 60.0         # ... older than this: still drawn at the fix, but stale (hollow, faded, its age)
POSITION_HORIZON_S = 8.0        # the page draws a moving rig on from its fix this long at most (about the AVL interval)
POSITION_FUTURE_S = 60.0        # a fix this far ahead of our clock is wrong (skew is taken up to this)
MAX_SPEED_MS = 100.0            # m/s; faster is no fire apparatus (360 km/h): ingest refuses it, fresh_position drops it
EPOCH_MIN, EPOCH_MAX = 1e9, 1e10   # plausible epoch seconds (2001-2286): milliseconds (1.7e12) are refused
RECENT_S = 6 * 3600.0           # cleared calls looked at for a return trip (the longest window allowed)
AT_HOME_KM = 0.15               # an AVL fix this close to its station: in quarters (turnout counts)
LOOK = {"free": "available", "oos": "oos", "dispatched": "dispatched", "enroute": "responding", "onscene": "onscene"}
# Every other role is committed to a call (role '' in data/cad_lists.json: acknowledged, staging, out at, call back,
# dispatched by other agency; or one not known here): drawn as dispatched, an active rig (#376: chips longest).
ACTIVE_LOOK = "dispatched"


def look_of(role: str | None) -> str:
    """A status role -> the marker's look: available (idle) | oos | dispatched | responding | onscene (active)."""
    return LOOK.get(role or "", ACTIVE_LOOK)
SNIPPET = 160
# Rigs decluttered by zoom (#376; drawn by cadmap.js): below detail_zoom an idle rig is a dot (two or more idle at one
# firehouse: one badge, "HQ · 6"), below active_zoom an active one (on a call) a strong dot. Settings > System > Map
# display; the wall board (/monitor, read from across the room, always showing the whole area) has its own pair. A map
# on its automatic frame (the area or the call, not moved by hand) keeps every active rig a full chip whatever its zoom.
DISPLAY_SECTION = "map_display"
DISPLAY_DEFAULTS = {"detail_zoom": 15.0, "active_zoom": 10.0, "board_detail_zoom": 16.0, "board_active_zoom": 10.0}
DISPLAY_LABELS = {"detail_zoom": "Idle rigs: chips from zoom", "active_zoom": "Active rigs: chips from zoom",
                  "board_detail_zoom": "Wall board, idle: chips from zoom",
                  "board_active_zoom": "Wall board, active: chips from zoom"}
DISPLAY_LIMITS = (8.0, 20.0)
# An active rig's zoom no later than this (the console's and the wall board's): a rig on a call never a dot at the zooms
# the area is watched at (#376: Settings can't hide the responding rigs)
ACTIVE_MAX = 12.0


def display_limits(key: str) -> tuple[float, float]:
    """The zooms one map display setting may take: an active one capped at ACTIVE_MAX."""
    lo, hi = DISPLAY_LIMITS
    return (lo, min(hi, ACTIVE_MAX)) if key.endswith("active_zoom") else (lo, hi)


def load_firehouses(path: str | Path | None = None) -> list[dict]:
    """data/firehouses.json: [{name, address, ll: [lon, lat], short?}]; [] if it can't be read."""
    try:
        fh = json.loads(Path(path or FIREHOUSES).read_text())["firehouses"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [{"name": str(f["name"]), "address": str(f.get("address") or ""), "ll": [float(f["ll"][0]), float(f["ll"][1])],
             **({"short": str(f["short"])[:12]} if f.get("short") else {})}      # the map's badge (#376): "HQ · 6"
            for f in fh if isinstance(f, dict) and f.get("name") and isinstance(f.get("ll"), list) and len(f["ll"]) == 2]


def display_value(key: str, v) -> float:
    """One map display zoom (#376), checked: a number within display_limits(key). Raises ValueError."""
    if key not in DISPLAY_DEFAULTS:
        raise ValueError(f"{', '.join(DISPLAY_DEFAULTS)} only")
    lo, hi = display_limits(key)
    if not _num(v) or not lo <= v <= hi:
        raise ValueError(f"{key}: a zoom from {lo:g} to {hi:g}")
    return round(float(v), 2)


def display_zooms(ov: dict | None = None) -> dict:
    """The map display zooms in effect (state["map"]["display"]): the Settings overrides over DISPLAY_DEFAULTS."""
    out = dict(DISPLAY_DEFAULTS)
    for k, v in (ov or {}).items():
        try:
            out[k] = display_value(k, v)
        except ValueError:
            pass                                    # a bad stored value: the default (import and set refuse them)
    return out


def is_fire(unit: dict) -> bool:
    """Drawn by default: PCFD's and Rye Brook's apparatus (response "fire": not mutual aid, police, EMS or
    other agencies). A unit without a response (older callers) is judged by its kind."""
    if not unit.get("board"):
        return False
    if unit.get("response"):
        return unit["response"] == "fire"
    return unit.get("kind") in FIRE_KINDS and not unit.get("mutual_aid")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def fresh_position(pos, now: float) -> dict | None:
    """A live position {lat, lon, at, heading?, speed?, source?} that can be drawn: numbers in range and
    no older than POSITION_MAX_AGE_S (an `at` up to POSITION_FUTURE_S ahead is clock skew: taken; further ahead
    is a wrong clock or milliseconds: refused). heading, speed and
    source are kept only when they are good (a bad one is dropped, not the fix)."""
    if not isinstance(pos, dict):
        return None
    lat, lon, at = pos.get("lat"), pos.get("lon"), pos.get("at")
    if not all(_num(v) for v in (lat, lon, at)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or now - at > POSITION_MAX_AGE_S or at - now > POSITION_FUTURE_S:
        return None
    out = {"lat": float(lat), "lon": float(lon), "at": float(at)}
    hd, sp, src = pos.get("heading"), pos.get("speed"), pos.get("source")
    if _num(hd) and 0 <= hd <= 360:
        out["heading"] = float(hd) % 360
    if _num(sp) and 0 <= sp <= MAX_SPEED_MS:
        out["speed"] = float(sp)
    if isinstance(src, str) and src.strip():
        out["source"] = src.strip()[:40]
    return out


def stale_position(pos: dict, now: float) -> bool:
    """A fix still drawn (fresh_position) but older than POSITION_STALE_S: hollow, faded, its age shown."""
    return now - pos["at"] > POSITION_STALE_S


def ingest(fix) -> tuple[str, dict]:
    """One fix in the generic ingest shape (the module doc) -> (unit code, the position Cad.positions
    returns for it). Raises ValueError for anything that isn't that shape; an old fix is not an error here
    (fresh_position decides what is drawn)."""
    if not isinstance(fix, dict):
        raise ValueError("a fix is an object")
    unit = fix.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("unit: the unit's code")
    for k in ("lat", "lon", "at"):
        if not _num(fix.get(k)):
            raise ValueError(f"{k}: a number")
    if not (-90 <= fix["lat"] <= 90 and -180 <= fix["lon"] <= 180):
        raise ValueError("lat, lon: out of range")
    if not EPOCH_MIN < fix["at"] < EPOCH_MAX:
        raise ValueError("at: epoch seconds (not milliseconds)")
    for k, lo, hi in (("heading", 0, 360), ("speed", 0, MAX_SPEED_MS)):
        if fix.get(k) is not None and not (_num(fix[k]) and lo <= fix[k] <= hi):
            raise ValueError(f"{k}: {lo}-{hi}")
    src = fix.get("source")
    if not isinstance(src, str) or not src.strip():
        raise ValueError("source: where the fix came from")
    pos = {"lat": float(fix["lat"]), "lon": float(fix["lon"]), "at": float(fix["at"]), "source": src.strip()[:40]}
    for k in ("heading", "speed"):
        if fix.get(k) is not None:
            pos[k] = float(fix[k])
    return unit.strip(), pos


def _ll(x: dict | None) -> list[float] | None:
    return [x["lon"], x["lat"]] if x and x.get("lat") is not None and x.get("lon") is not None else None


def locate(unit: dict, role: str, station: dict | None = None, incident: dict | None = None,
           position: dict | None = None, now: float = 0.0, clear: dict | None = None,
           params: eta.Params | None = None) -> dict | None:
    """Where one rig is now, estimated (the module doc's table).

    unit:     {name, status?, assigned_at? (when it was put on its open call)}
    role, station, incident, position, now: as for place()
    clear:    its latest cleared call it was on location at {incident_id, number, lat, lon, cleared_at} or None
    params:   the ETA settings (eta.Params)

    Returns None (unknown) or {where: position|incident|returning|near|station, ll: [lon, lat],
    on_road (no turnout from here), label ("from Washington Park", "returning from 26-000004"),
    station (its name or ''), home ([lon, lat] or None), and for a return: call_ll, number, until}."""
    p = params or eta.Params()
    home = list(station["ll"]) if station else None
    base = {"station": station["name"] if station else "", "home": home}
    pos = fresh_position(position, now)
    if pos:
        ll = [pos["lon"], pos["lat"]]
        in_quarters = home is not None and eta.haversine_km(ll, home) <= AT_HOME_KM
        return dict(base, where="position", ll=ll, on_road=not in_quarters,
                    label=f"in quarters ({station['name']}, live)" if in_quarters else "live position")
    inc_ll = _ll(incident)
    if incident is not None and role == "onscene" and inc_ll:
        return dict(base, where="incident", ll=inc_ll, on_road=True,
                    label=f"at {incident.get('number') or 'the call'}")
    ret = _returning(unit, role, incident, clear, now, p, home)
    if ret:
        return dict(base, **ret)
    if station:
        return dict(base, where="station", ll=home, on_road=False, label=f"from {station['name']}")
    return None


def _returning(unit: dict, role: str, incident: dict | None, clear: dict | None, now: float,
               p: eta.Params, home: list | None) -> dict | None:
    call = _ll(clear)
    if call is None or role in ("oos", "onscene"):
        return None
    if incident is None:
        if role != "free" or (p.quarters_status and unit.get("status") == p.quarters_status):
            return None                         # reported back in quarters
        t = now
    else:
        if clear.get("incident_id") == incident.get("id"):
            return None
        t = min(now, unit.get("assigned_at") or now)       # committed: where it was when it took the call
    since = t - clear["cleared_at"]
    window = eta.return_window_s(call, home, p)
    if since < 0 or since >= window:
        return None
    num = clear.get("number") or "a call"
    out = {"on_road": True, "call_ll": call, "number": num, "until": clear["cleared_at"] + window}
    if home is None:
        return dict(out, where="near", ll=call, label=f"near {num} (station not set)")
    return dict(out, where="returning", ll=eta.along(call, home, since / window) if window else home,
                label=f"returning from {num}")


def place(unit: dict, role: str, station: dict | None = None, incident: dict | None = None,
          position: dict | None = None, now: float = 0.0, fire: bool | None = None,
          clear: dict | None = None, params: eta.Params | None = None) -> dict | None:
    """Where one unit goes on the map and how it looks.

    unit:     {name, id?, kind?, status?, assigned_at?}
    role:     its status's role: free | oos | dispatched | enroute | onscene | '' (committed)
    station:  its home firehouse {name, ll: [lon, lat]} or None (not set / unknown name)
    incident: the event it is on {id, number?, lat, lon} or None (lat None: no map point)
    position: a live fix {lat, lon, at} or None
    fire:     drawn by default (is_fire); None = from the unit
    clear:    its latest cleared call (locate()): a recent one draws it on its way home

    Returns None (not drawn) or {unit, id, kind, status, status_at, look, place: station|incident|position|returning,
    group, lon, lat, ring: 0 (inner) | 1 (outer, not there yet), incident_id, incident_number,
    line_to: [lon, lat] | None, station, home_line (the line is to its station: thin)}; a "position"
    one also has its fix's at, age (s), stale, and heading / speed / source when the fix has them."""
    fire = is_fire(unit) if fire is None else fire
    look = look_of(role)
    inc_ll = _ll(incident)
    if incident is None and not fire:
        return None                                  # EMS / police / agencies: only while on an event
    spec = {"unit": unit["name"], "id": unit.get("id"), "kind": unit.get("kind", ""), "status": unit.get("status", ""),
            "status_at": unit.get("status_at"), "look": look, "place": "", "group": "", "lon": None, "lat": None, "ring": 0,
            "incident_id": incident["id"] if incident else None,
            "incident_number": (incident or {}).get("number") or "",
            "line_to": None, "station": station["name"] if station else "", "home_line": False}
    going = incident is not None and role != "onscene" and inc_ll is not None   # a line to where it's going
    loc = locate(unit, role, station, incident, position, now, clear, params)
    if loc and loc["where"] == "position":
        fix = fresh_position(position, now)
        extra = {k: fix[k] for k in ("heading", "speed", "source") if k in fix}
        return dict(spec, place="position", group=f"unit:{unit['name']}", lon=loc["ll"][0], lat=loc["ll"][1],
                    line_to=inc_ll if going else None, at=fix["at"], age=round(max(0.0, now - fix["at"]), 1),
                    stale=stale_position(fix, now), **extra)
    if incident is not None and inc_ll is not None and (role == "onscene" or station is None or not fire):
        # on location; or on an event with no home station to start from (EMS, PD, a fire unit not set yet)
        return dict(spec, place="incident", group=f"incident:{incident['id']}", lon=inc_ll[0], lat=inc_ll[1],
                    ring=0 if role == "onscene" else 1)
    if station is None:
        return None                                  # not on a call (or its event has no map point): listed instead
    if loc and loc["where"] == "returning":
        return dict(spec, place="returning", group=f"unit:{unit['name']}", lon=loc["ll"][0], lat=loc["ll"][1],
                    line_to=inc_ll if going else list(station["ll"]), home_line=not going,
                    returning_from=loc["number"])
    return dict(spec, place="station", group=f"station:{station['name']}", lon=station["ll"][0], lat=station["ll"][1],
                line_to=inc_ll if going else None)


def map_slice(units: list[dict], incidents: list[dict], role, firehouses: list[dict],
              positions: dict | None = None, now: float = 0.0, joint=lambda code: "",
              clears: dict | None = None, params: eta.Params | None = None, display: dict | None = None) -> dict:
    """state["map"]: {stations, markers, unset, incidents, avl, display}. units: Store.units(); incidents: the
    state's; role(status) -> role; joint(code) -> 'RB' tag for a joint-response unit; clears: unit id ->
    its latest cleared call (Store.recent_clears); params: the ETA settings; display: the map display zooms
    (display_zooms(), #376)."""
    positions, clears = positions or {}, clears or {}
    by_name = {f["name"]: f for f in firehouses}
    open_incs = {i["id"]: i for i in incidents if i["status"] in ("queued", "pending", "active")}
    markers, unset = [], []
    for u in units:
        if not (u.get("board") or u.get("incident_id")):
            continue
        inc = open_incs.get(u.get("incident_id"))
        st = by_name.get(u.get("station") or "")
        m = place(u, role(u["status"]), st, inc, positions.get(u["name"]), now, clear=clears.get(u["id"]), params=params)
        if m is None:
            if is_fire(u) and st is None:
                unset.append({"id": u["id"], "unit": u["name"], "station": u.get("station") or "",
                              "on": (inc or {}).get("number") or ""})
            continue
        m["tag"] = joint(u["name"])
        m["fire"] = is_fire(u)
        markers.append(m)
    # each group's members, in board order, numbered for fanning out around the anchor
    groups: dict[str, list[dict]] = {}
    for m in markers:
        groups.setdefault(m["group"], []).append(m)
    for g in groups.values():
        for ring in (0, 1):
            members = [m for m in g if m["ring"] == ring]
            for k, m in enumerate(members):
                m["slot"], m["of"] = k, len(members)
    pins = [{"id": i["id"], "number": i.get("number") or "", "status": i["status"], "emergency": bool(i.get("emergency")),
             "type": i.get("type") or "", "subtype": i.get("subtype") or "", "address": i.get("address") or "",
             "lat": i["lat"], "lon": i["lon"]}
            for i in open_incs.values() if i.get("lat") is not None and i.get("lon") is not None]
    return {"stations": firehouses, "markers": markers, "unset": unset, "incidents": pins,
            "avl": {"stale_s": POSITION_STALE_S, "max_age_s": POSITION_MAX_AGE_S, "horizon_s": POSITION_HORIZON_S},
            "display": dict(display) if display else display_zooms()}


def locations(units: list[dict], incidents: list[dict], role, firehouses: list[dict], positions: dict | None = None,
              now: float = 0.0, clears: dict | None = None, params: eta.Params | None = None) -> dict[str, dict | None]:
    """unit code -> locate() for every unit (the recommendation's "where is it now")."""
    positions, clears = positions or {}, clears or {}
    by_name = {f["name"]: f for f in firehouses}
    open_incs = {i["id"]: i for i in incidents if i["status"] in ("queued", "pending", "active")}
    return {u["name"]: locate(u, role(u["status"]), by_name.get(u.get("station") or ""), open_incs.get(u.get("incident_id")),
                              positions.get(u["name"]), now, clears.get(u["id"]), params) for u in units}


def _snippet(text: str, n: int = SNIPPET) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def overview(incidents: list[dict], tactical=("FG1",)) -> list[dict]:
    """One summary per open event (queued, pending, active), oldest first: what the Overview's cards
    show. Each: id, number, status, emergency, type, subtype, address, created_at, joint_call,
    units [{name, status}] (on it now), radio (the newest transmission: rig / talker, channel, start,
    snippet) or None, note (the newest timeline note, else the newest entry) or None, channels (where
    its traffic comes over: for the "talking now" light), lat, lon."""
    out = []
    for i in sorted((i for i in incidents if i["status"] in ("queued", "pending", "active")), key=lambda i: i["created_at"]):
        on = [a for a in i.get("assignments") or [] if not a.get("cleared_at")]
        radio = next((r for r in i.get("radio") or [] if (r.get("text") or "").strip()), None) or \
            next(iter(i.get("radio") or []), None)
        events = i.get("events") or []
        note = next((e for e in reversed(events) if e["kind"] in ("note", "upgrade")), None) or \
            (events[-1] if events else None)
        channels = sorted({r["channel"] for r in (i.get("radio") or [])[:50] if r.get("channel")} |
                          (set(tactical) if on else set()))
        out.append({
            "id": i["id"], "number": i.get("number") or "", "status": i["status"], "emergency": bool(i.get("emergency")),
            "type": i.get("type") or "", "subtype": i.get("subtype") or "", "address": i.get("address") or "",
            "created_at": i["created_at"], "joint_call": i.get("joint_call") or "", "lat": i.get("lat"), "lon": i.get("lon"),
            "units": [{"name": a["name"], "status": a["status"], "kind": a.get("kind", "")} for a in on],
            "radio": {"rig": radio.get("rig") or "", "talker": radio.get("talker") or "", "channel": radio.get("channel") or "",
                      "start": radio.get("start"), "text": _snippet(radio.get("text") or ""),
                      "dispatcher": bool(radio.get("dispatcher"))} if radio else None,
            "note": {"at": note["at"], "kind": note["kind"], "text": _snippet(note["text"], 200), "operator": note.get("operator") or "",
                     "units": [x for x in (note.get("units") or "").split(",") if x]} if note else None,
            "channels": channels,
            **({k: i.get(k) for k in airport.FIELDS} if i.get("type") == airport.TYPE else {}),   # the tower's facts (#105)
        })
    return out
