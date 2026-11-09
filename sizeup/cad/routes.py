"""The probable path of each responding rig to its call, kept up as it moves (GitHub #360).

User, 2026-11-09: "can we also show the path from rig to the call, and what path they might be following, and have
it update as they go different directions? just to show the probable path".

Routes sits between the maps (sizeup/cad/mapview.py's markers: where each rig is now, by AVL fix or by status) and
the road graph (sizeup/roadgraph.py). It reads only the map state and Cad.positions' fixes as the markers carry
them; nothing here knows how the page draws or glides a rig.

    for_map(markers, now)     state["map"]["routes"]: one route per rig going to a call (dispatched / responding,
                              its call with a map point): {unit, incident_id, coords, m, s, along_m, remain_m,
                              remain_s, eta_s, from: "fix" | "status", at, why}; and the trails
    to_one(origins, to)       the recommendation's road estimates (sizeup/cad/runcards.py estimates)

Cached per (unit, incident). A route is worked out again only when:

    new          no route yet, the call's point moved, the graph or the closures in effect changed, the settings
    moved        placed by status: its start (station, or returning position) moved more than MOVED_M
    off          a new fix more than reroute_m (Settings > Dispatch, 50 m) from the path (and from where it was
                 routed from: a rig in a yard off the road, its path starting at the road, stays put)
    away         AWAY_FIXES (3) new fixes in a row heading away: the distance left along the path grew by more
                 than AWAY_M, or (a fix with heading and speed) the heading is more than AWAY_DEG off the path's

The closures in effect (penalties()) are worked out off the state build, again only when RoadProjects' list
changes, the graph changes, or an item's time window opens or closes.

A fix that is on the path only moves the rig along it: remain_m / remain_s are what is left from its point on the
path (the time in proportion), eta_s adds turnout for a rig still in quarters. Routing is pure Python and quick
(a few ms a route on the 12 km graph), done in the state build with a budget (BUDGET_S a build; the rest next
second); with no graph (not built yet, routing off) there are no routes and the maps draw the straight line.

Trails (a faint breadcrumb of recent fixes): the last TRAIL_N fixes of each rig with one, at most TRAIL_S old.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Callable

from sizeup.cad import eta

MOVED_M = 30.0
AWAY_M = 15.0
AWAY_DEG = 110.0
AWAY_FIXES = 3
AWAY_MIN_MPS = 2.0            # a heading counts only at this speed or more
BUDGET_S = 0.15               # routing per state build (at least one route is always worked out)
PEN_S = 60.0                  # closures that couldn't be read: tried again after this
PEN_MAX_S = 3600.0            # ... and the penalties looked at again at least this often
RETRY_S = 30.0                # no way found (no road near, closed in): tried again after this, or once it moves                  # the closures in effect looked at again this often
TRAIL_N = 30
TRAIL_S = 900.0
GOING = ("dispatched", "responding")
AT_HOME_KM = 0.15             # mapview.AT_HOME_KM: a fix this close to its station is still in quarters


class Routes:
    def __init__(self, graph: Callable[[], object | None], params: Callable[[], eta.Params],
                 closures: Callable[[], list[dict]] = lambda: [], firehouses: Callable[[], list[dict]] = lambda: [],
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 spawn: Callable[[Callable[[], None]], None] | None = None):
        """spawn(fn): runs the closures' penalties off the state build (a thread; tests: at once)."""
        self.graph, self.params, self.closures, self.firehouses = graph, params, closures, firehouses
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="route-closures", daemon=True).start())
        self.clock, self.log = clock, log
        self.cache: dict[tuple[str, int], dict] = {}
        self.trails: dict[str, deque] = {}
        self.failed: dict[tuple[str, int], tuple] = {}   # no way found: (to, sig, from, when): tried again after RETRY_S
        self._pen: dict | None = None           # {key: (graph, closures list), pen, sig, until}: the closures as penalties
        self._pen_busy = False
        self._pen_lock = threading.Lock()
        self._lock = threading.Lock()
        self.stats = {"routed": 0, "last_ms": None, "why": {}}

    # -- inputs -----------------------------------------------------------------------------------------------

    def penalties(self, g, now: float) -> tuple[dict, object]:
        """The closures in effect now as edge penalties, and their signature (0: none). Worked out again only when
        the closures list changes (RoadProjects.items() hands back a new list when anything in it does), the graph
        changes, or an item's time window opens or closes (`until`); and then off the state build (spawn): until it
        is done the last penalties for this graph stay in use (none for a new graph)."""
        try:
            items = self.closures() or []
        except Exception as e:  # noqa: BLE001 - roadwork's trouble never stops routing
            self.log(f"routes: closures not read ({type(e).__name__}: {e})")
            items = []
        key = (id(g), id(items), len(items))
        got = self._pen                      # (one read: to_one runs on request threads beside the state build)
        with self._pen_lock:                 # (the state build and request threads: one job at a time)
            start = (got is None or got["key"] != key or now >= got["until"]) and not self._pen_busy
            if start:
                self._pen_busy = True
        if start:

            def work(items=items, key=key):
                try:
                    pen = g.penalties(items, now)
                    until = _next_change(items, now)
                except Exception as e:  # noqa: BLE001
                    self.log(f"routes: closures not applied ({type(e).__name__}: {e})")
                    pen, until = {}, now + PEN_S
                sig = tuple(sorted(pen.items()))
                with self._pen_lock:
                    self._pen = {"key": key, "g": id(g), "pen": pen, "sig": hash(sig) if sig else 0, "until": until}
                    self._pen_busy = False
            self.spawn(work)
            got = self._pen
        if got is None or got["g"] != id(g):
            return {}, 0
        return got["pen"], got["sig"]

    def _home(self, station: str):
        for f in self.firehouses() or ():
            if f.get("name") == station:
                return f["ll"]
        return None

    # -- the maps ---------------------------------------------------------------------------------------------

    def for_map(self, markers: list[dict], now: float | None = None) -> dict:
        """{"routes": [...], "trails": {unit: [[lon, lat], ...]}} for state["map"] (the module doc)."""
        now = self.clock() if now is None else now
        with self._lock:
            self._trail(markers, now)
            trails = {u: [[p[0], p[1]] for p in t] for u, t in self.trails.items() if len(t) >= 2}
            g = self.graph()
            going = [m for m in markers if m.get("look") in GOING and m.get("incident_id") and m.get("line_to")
                     and m.get("lon") is not None]
            if g is None or not going:
                self.cache.clear()
                self.failed.clear()
                return {"routes": [], "trails": trails}
            p = self.params()
            pen, pen_sig = self.penalties(g, now)
            sig = (id(g), pen_sig, p.highway_mph, p.arterial_mph, p.local_mph, p.emergency_factor, p.wrongway_m)
            seen, out, n = set(), [], 0
            t0 = time.monotonic()
            for m in going:
                key = (m["unit"], m["incident_id"])
                seen.add(key)
                ent = self.cache.get(key)
                why = self._why(ent, m, sig, p)
                bad = self.failed.get(key)
                if why and bad and bad[0] == m["line_to"] and bad[1] == sig and now - bad[3] < RETRY_S \
                        and _m([m["lon"], m["lat"]], bad[2]) <= MOVED_M:
                    why = ""                                        # no way last time: not again every second
                if why and (n == 0 or time.monotonic() - t0 < BUDGET_S):
                    n += 1
                    ent = self._route(g, m, sig, pen, p, why, now) or (None if why == "new" else ent)
                if ent is None or ent["to"] != m["line_to"]:      # (a call moved, not routed yet: none)
                    continue
                out.append(self._public(ent, m, now, p))
            for k in [k for k in self.cache if k not in seen]:
                del self.cache[k]
            for k in [k for k in self.failed if k not in seen]:
                del self.failed[k]
            return {"routes": out, "trails": trails}

    def _why(self, ent: dict | None, m: dict, sig: tuple, p: eta.Params) -> str:
        """Why the route must be worked out again now ('' = keep it); a new fix moves the rig along it here."""
        if ent is None or ent["to"] != m["line_to"] or ent["sig"] != sig:
            return "new"
        here = [m["lon"], m["lat"]]
        if m.get("place") != "position":
            return "moved" if _m(here, ent["origin"]) > MOVED_M or ent["from"] == "fix" else ""
        at = m.get("at")
        if at == ent.get("fix_at"):
            return ""
        off, along, brg = _project(ent["coords"], ent["cum"], here)
        if off > p.reroute_m:
            if _m(here, ent["origin"]) <= p.reroute_m:
                ent["fix_at"] = at           # still where it was routed from (a rig off the road: the path starts at it)
                return ""
            return "off"                     # (nothing kept: asked again next build until it is routed)
        remain = ent["m"] - along
        hd, sp = m.get("heading"), m.get("speed")
        away = remain > ent["remain"] + AWAY_M
        if isinstance(hd, (int, float)) and isinstance(sp, (int, float)) and sp >= AWAY_MIN_MPS and brg is not None:
            away = away or abs((hd - brg + 180) % 360 - 180) > AWAY_DEG
        n = ent["away"] + 1 if away else 0
        if n >= AWAY_FIXES:
            return "away"
        # on its path: this fix moves it along (a route made from its station too, once fixes come)
        ent.update(fix_at=at, away=n, along=along, remain=remain if away else min(remain, ent["remain"]))
        return ""

    def _route(self, g, m: dict, sig: tuple, pen: dict, p: eta.Params, why: str, now: float) -> dict | None:
        here = [m["lon"], m["lat"]]
        fix = m.get("place") == "position"
        hd = m.get("heading") if fix and isinstance(m.get("speed"), (int, float)) and m["speed"] >= AWAY_MIN_MPS else None
        t0 = time.monotonic()
        try:
            r = g.route(here, m["line_to"], p, pen, heading=hd, wrongway_m=p.wrongway_m)
        except Exception as e:  # noqa: BLE001 - one route's trouble: the straight line, as before
            self.log(f"routes: {m['unit']}: {type(e).__name__}: {e}")
            r = None
        ms = (time.monotonic() - t0) * 1000
        self.stats["routed"] += 1
        self.stats["last_ms"] = round(ms, 1)
        self.stats["why"][why] = self.stats["why"].get(why, 0) + 1
        if r is None or len(r["coords"]) < 2:
            self.failed[(m["unit"], m["incident_id"])] = (list(m["line_to"]), sig, here, now)
            return None
        self.failed.pop((m["unit"], m["incident_id"]), None)
        coords = r["coords"]
        cum = [0.0]
        for a, b in zip(coords, coords[1:]):
            cum.append(cum[-1] + _m(a, b))
        ent = {"to": list(m["line_to"]), "origin": here, "coords": coords, "cum": cum, "m": cum[-1], "s": r["s"],
               "sig": sig, "from": "fix" if fix else "status", "at": now, "why": why, "fix_at": m.get("at"),
               "away": 0, "along": 0.0, "remain": cum[-1]}
        self.cache[(m["unit"], m["incident_id"])] = ent
        return ent

    def _public(self, ent: dict, m: dict, now: float, p: eta.Params) -> dict:
        along = ent["along"] if m.get("place") == "position" else 0.0
        remain = max(0.0, ent["m"] - along)
        remain_s = ent["s"] * remain / ent["m"] if ent["m"] > 0 else 0.0
        quarters = m.get("place") == "station"
        if m.get("place") == "position" and m.get("station"):
            home = self._home(m["station"])
            quarters = home is not None and eta.haversine_km([m["lon"], m["lat"]], home) <= AT_HOME_KM
        eta_s = remain_s + (p.turnout_min * 60.0 if quarters and m.get("look") == "dispatched" else 0.0)
        return {"unit": m["unit"], "incident_id": m["incident_id"], "coords": ent["coords"], "m": round(ent["m"]),
                "s": round(ent["s"]), "along_m": round(along), "remain_m": round(remain), "remain_s": round(remain_s),
                "eta_s": round(eta_s), "text": f"~{max(1, round(eta_s / 60))} min · {eta.miles(remain)}",
                "from": ent["from"], "at": ent["at"], "why": ent["why"]}

    def _trail(self, markers: list[dict], now: float) -> None:
        live = set()
        for m in markers:
            if m.get("place") != "position" or not isinstance(m.get("at"), (int, float)):
                continue
            live.add(m["unit"])
            t = self.trails.setdefault(m["unit"], deque(maxlen=TRAIL_N))
            if not t or t[-1][2] != m["at"]:
                t.append((round(m["lon"], 6), round(m["lat"], 6), m["at"]))
        for u in list(self.trails):
            t = self.trails[u]
            while t and now - t[0][2] > TRAIL_S:
                t.popleft()
            if not t or u not in live:
                del self.trails[u]

    # -- the recommendation -----------------------------------------------------------------------------------

    def to_one(self, origins: dict, to) -> dict:
        """{key: (drive seconds, metres)} by road from each [lon, lat] origin to `to`; {} with no graph."""
        g = self.graph()
        if g is None or not origins or to is None:
            return {}
        p = self.params()
        try:
            pen, _ = self.penalties(g, self.clock())
            return g.to_one(origins, to, p, pen, wrongway_m=p.wrongway_m)
        except Exception as e:  # noqa: BLE001 - the straight-line estimate then
            self.log(f"routes: estimates by road: {type(e).__name__}: {e}")
            return {}


def _next_change(items: list[dict], now: float) -> float:
    """When the next dated item's time window opens or closes (roadprojects.windows), at most PEN_MAX_S on."""
    from sizeup import roadprojects as rp
    nxt = now + PEN_MAX_S
    for r in items or ():
        if not rp.dated(r):
            continue
        try:
            for a, b in rp.windows(r, now):
                for t in (a, b):
                    if t is not None and t > now:
                        nxt = min(nxt, t)
        except Exception:  # noqa: BLE001 - one record we can't read
            continue
    return nxt


def _m(a, b) -> float:
    return eta.haversine_km(a, b) * 1000.0


def _project(coords: list, cum: list, pt) -> tuple[float, float, float | None]:
    """(metres off the path, metres along it, the path's bearing there) for a [lon, lat] point."""
    kx = 111_320.0 * math.cos(math.radians(pt[1]))
    ky = 110_574.0
    px, py = pt[0] * kx, pt[1] * ky
    best = (math.inf, 0.0, None)
    for i in range(len(coords) - 1):
        x1, y1 = coords[i][0] * kx, coords[i][1] * ky
        x2, y2 = coords[i + 1][0] * kx, coords[i + 1][1] * ky
        dx, dy = x2 - x1, y2 - y1
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ll))
        d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
        if d < best[0]:
            brg = math.degrees(math.atan2(dx, dy)) % 360 if ll else None
            best = (d, cum[i] + t * (cum[i + 1] - cum[i]), brg)
    return best
