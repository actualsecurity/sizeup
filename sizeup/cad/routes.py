"""The probable path of each responding rig to its call, kept up as it moves (GitHub #360).

User, 2026-11-09: "can we also show the path from rig to the call, and what path they might be following, and have
it update as they go different directions? just to show the probable path".

Routes sits between the maps (sizeup/cad/mapview.py's markers: where each rig is now, by AVL fix or by status) and
the road graph (sizeup/roadgraph.py). It reads only the map state and Cad.positions' fixes as the markers carry
them; nothing here knows how the page draws or glides a rig.

    for_map(markers, now, incidents)
                              state["map"]["routes"]: one route per rig going to a call (dispatched / responding,
                              its call with a map point): {unit, incident_id, coords, m, s, along_m, remain_m,
                              remain_s, eta_s, from: "fix" | "status", at, why}; and the trails
    to_one(origins, to, inc)  the recommendation's road estimates (sizeup/cad/runcards.py estimates)

Highways (roadgraph's module doc): each call's address, cross streets and type say whether it is on a highway
(roadgraph.highway_call); a call on none keeps the rigs on local streets, a highway call lets them use it, the right
way. A call whose words change is routed again ("new").

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

Estimated motion (#453; the user: "even though we don't yet have their location data, we have a path now for them ...
can we show them following that path (until we get real location data in), and on the way back to quarters after
being released too"). A rig with no live fix is moved along its route by time: the hub sends each route's geometry
and a `move` {t0, mps, hold_m, ease_s, done_at, stop?}; the page (cadmap.js) places it at along_at(move, now) and turns
it to the road. Nothing here runs per frame: a move is made once, and only a status change touches it.

    to the call     responding (its en-route status), placed by status (at its station, or where it was when it took
                    the call): t0 = the en-route time, mps = the route's length / its drive time, from rest (EASE_S),
                    held at HOLD_FRAC of the way (at most HOLD_MAX_M short) until it reports on location; dispatched
                    again (not responding): held where it is (stop), on again from there when it responds
    home            released: cleared from a call it was on location at (mapview's "returning": from the call, t0 the
                    clearing), or cancelled while moving by estimate (from where it was then, t0 the new status):
                    routed to its station (one-ways the right way, no wrong-way stretch, LOCAL), at the road speeds
                    without the emergency factor, all the way; there (done_at), or the in-quarters status reported
                    later than the release (mapview.QUARTERS_GRACE_S): at its station, idle (its marker rewritten)
    re-routed       the call moved, the graph or the closures changed: from where the estimate has it now, rolling
                    (no ease); a road route found later for a straight-line one: the same
    no road near    the straight line (by "line"), at the straight-line model's speed (eta.drive_s); still estimated
    already out     dispatched while on a trip home: from where the trip had it when it took the call (frozen); else
                    from its last live fix if it is no older than LAST_FIX_S and away from its station; a fix older
                    than that (up to OUT_S) away from its station: where it is is unknown, not moved
                    (start_unknown: at its station, dashed, as before). A rig started away from its station is
                    placed "enroute" (est: True), held where it is until it responds
    out of service  (or any status not on a call nor free): its trip is ended, nothing frozen
    a restart       the trips and the moves are kept in the CAD's meta (persist: SAVE_KEY) and read back on the
                    first build, so a restart or a hot/cold handover goes on from the same estimate
    a live fix      always wins: no move, any trip forgotten

Firehouse gates (the bay doors): a route from a rig in quarters (by status: placed at its station; or a live fix within
AT_HOME_KM of its own station) starts at its firehouse's egress, the bay-door apron, and joins the street the rigs pull
out onto; a trip home ends at its ingress the same way (data/firehouses.json egress / ingress: {lat, lon, street}; roadgraph.snap_gate); an
ingress "via": "lot" comes back in through a lot instead, by its drive aisles (Rye Brook: the PD lot behind it). A
firehouse with none: from its point onto the street of its address, else the nearest public street, never a lot's
service road while a street is within roadgraph.GATE_SNAP_M. The recommendation's estimates (to_one) start at the
same gate for the rigs it says are in quarters, so the run card and the map agree; a rig near a firehouse but not in
quarters (on location, driving by) is routed from where it is. A route worked out again in the first metres of its
move (still on the apron) goes out by the same gate, rolling on from where it was. A trip home ends at the apron and
then rolls in to the firehouse's point at BAY_MPS (no jump). A route already made (or kept across a restart) stays as
it was; only a new one uses the gate.

The markers of rigs on a trip home are rewritten here (place "returning" at the estimate, or "station" once there);
The recommendation agrees with the map: Cad.locations lays trip_at() (a rig's place on its trip home, or home) over
mapview.locate's straight-line return (service.py _with_trips).
"""

from __future__ import annotations

import bisect
import json
import math
import re
import threading
import time
from collections import deque
from typing import Callable

from sizeup import roadgraph
from sizeup.cad import eta
from sizeup.cad.mapview import QUARTERS_GRACE_S

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
EASE_S = 6.0                  # an estimated rig sets off from rest: up to the route's speed over about this long (#453)
HOLD_FRAC = 0.95              # ... waits this far along its way to a call until it reports on location
HOLD_MAX_M = 150.0            # ... at most this short of the call
LAST_FIX_S = TRAIL_S          # a last live fix this recent (the maps no longer draw it) starts its next trip
OUT_S = 6 * 3600.0            # older, away from its station: where it is is unknown (not moved)
PLACED = ("station", "returning", "enroute")   # placed by status (no live fix, not at a call); "enroute": started here
GATE_M = 40.0                 # (to_one, no station named) an in-quarters origin this near a firehouse's point: its gate
BAY_MPS = 2.5                 # a trip home: from the apron into the bays, this slowly
SAVE_KEY = "routes.motion"    # the trips and the moves, kept in the CAD's meta (a restart or a handover goes on with them)


class Routes:
    def __init__(self, graph: Callable[[], object | None], params: Callable[[], eta.Params],
                 closures: Callable[[], list[dict]] = lambda: [], firehouses: Callable[[], list[dict]] = lambda: [],
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 spawn: Callable[[Callable[[], None]], None] | None = None,
                 persist: tuple[Callable[[], str | None], Callable[[str], None]] | None = None):
        """spawn(fn): runs the closures' penalties off the state build (a thread; tests: at once). persist: (load,
        save) of the trips and moves as JSON (the CAD's meta: Store.meta_get / meta_put), #453."""
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
        self.trips: dict[str, dict] = {}        # unit -> its trip home (#453)
        self.fixes: dict[str, tuple] = {}       # unit -> ([lon, lat], at): its last live fix (a trip's start)
        self.persist = persist
        self._loaded = persist is None
        self._saved = ""
        self._calls: dict[tuple, roadgraph.Call] = {}   # a call's words -> roadgraph.Call (the last state build's)

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

    @staticmethod
    def gate(f: dict | None, way: str = "egress") -> tuple[list, str] | None:
        """A firehouse's gate, out (way "egress") or in ("ingress", else its egress): ([lon, lat] of the apron, the
        street it joins: roadgraph.LOT for an ingress through a lot, its drive aisles allowed); a firehouse with none
        given: its own point and its address's street."""
        if not f:
            return None
        try:
            g = f.get(way) or f.get("egress")
            if g:
                lot = way == "ingress" and f.get("ingress") is g and g.get("via") == "lot"      # never on the way out
                return [float(g["lon"]), float(g["lat"])], roadgraph.LOT if lot else str(g.get("street") or "")
            return [float(f["ll"][0]), float(f["ll"][1])], street_of(f.get("address"))
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    def firehouse(self, name: str | None) -> dict | None:
        for f in self.firehouses() or ():
            if name and f.get("name") == name:
                return f
        return None

    def gate_of(self, ll, way: str = "egress") -> tuple[list, str] | None:
        """The gate of the firehouse whose point is within GATE_M of ll (the nearest), or None. Only for a rig known to
        be in quarters: nearness alone never says so (a rig on location or driving by)."""
        best = None
        for f in self.firehouses() or ():
            try:
                d = _m(ll, f["ll"])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if d <= GATE_M and (best is None or d < best[0]):
                best = (d, f)
        return self.gate(best[1], way) if best else None

    def _quarters_gate(self, m: dict) -> tuple[list, str] | None:
        """The way out for a rig in quarters by its marker: placed at its station, or a live fix within AT_HOME_KM of
        its own station (either: its marker there); else None."""
        f = self.firehouse(m.get("station"))
        if f is None or m.get("place") not in ("station", "position"):
            return None
        return self.gate(f) if eta.haversine_km([m["lon"], m["lat"]], f["ll"]) <= AT_HOME_KM else None

    def _home(self, station: str):
        for f in self.firehouses() or ():
            if f.get("name") == station:
                return f["ll"]
        return None

    # -- the maps ---------------------------------------------------------------------------------------------

    def call_of(self, inc: dict | None) -> roadgraph.Call:
        """What an incident's words say about being on a highway (roadgraph.highway_call), remembered by its words."""
        if not inc:
            return roadgraph.LOCAL
        words = (inc.get("address") or "", inc.get("cross_street") or "", inc.get("type") or "",
                 inc.get("subtype") or "")
        got = self._calls.get(words)
        if got is None:
            got = roadgraph.highway_call(*words)
            if len(self._calls) > 256:
                self._calls.clear()
            self._calls[words] = got
        return got

    def for_map(self, markers: list[dict], now: float | None = None, incidents: list[dict] | None = None) -> dict:
        """{"routes": [...], "trails": {unit: [[lon, lat], ...]}} for state["map"] (the module doc). incidents: the
        state's (their words: a highway call or not). A rig's marker on a trip home is rewritten (#453)."""
        now = self.clock() if now is None else now
        from sizeup.cad.mapview import is_placeholder
        markers = [m for m in markers if not is_placeholder(m)]     # a department placeholder: never routed (#384)
        with self._lock:
            if not self._loaded:
                self._load()
            out = self._for_map(markers, now, incidents)
            self._save()
            return out

    def _for_map(self, markers: list[dict], now: float, incidents: list[dict] | None) -> dict:
        self._trail(markers, now)
        trails = {u: [[p[0], p[1]] for p in t] for u, t in self.trails.items() if len(t) >= 2}
        g = self.graph()
        p = self.params()
        got: list = []

        def pen():                         # the closures in effect: looked at only when something is routed
            if not got:
                got.append(self.penalties(g, now) if g is not None else ({}, 0))
            return got[0]
        backs = self._trips(markers, now, g, p, pen)
        going = [m for m in markers if m.get("look") in GOING and m.get("incident_id") and m.get("line_to")
                 and m.get("lon") is not None]
        if not going:
            self.cache.clear()
            self.failed.clear()
            return {"routes": backs, "trails": trails}
        sig = None
        if g is not None:
            pens, pen_sig = pen()
            sig = (id(g), pen_sig, p.highway_mph, p.arterial_mph, p.local_mph, p.emergency_factor, p.wrongway_m)
        incs = {i.get("id"): i for i in incidents or ()}
        seen, out, n = set(), [], 0
        t0 = time.monotonic()
        for m in going:
            key = (m["unit"], m["incident_id"])
            seen.add(key)
            try:
                ent = prev = self.cache.get(key)
                if prev is None:                       # moved to another call while moving by estimate: on from there
                    prev = next((e for k, e in self.cache.items() if k[0] == m["unit"] and e.get("move")), None)
                if g is not None:
                    call = self.call_of(incs.get(m["incident_id"]))
                    msig = sig + (call,)
                    why = self._why(ent, m, msig, p)
                    bad = self.failed.get(key)
                    if why and bad and bad[0] == m["line_to"] and bad[1] == msig and now - bad[3] < RETRY_S \
                            and _m([m["lon"], m["lat"]], bad[2]) <= MOVED_M:
                        why = ""                                        # no way last time: not again every second
                    if why and (n == 0 or time.monotonic() - t0 < BUDGET_S):
                        n += 1
                        new = self._route(g, m, msig, pens, p, why, now, call, self._start(prev, m, now))
                        if new is not None:
                            ent = new
                        elif why == "new" and not (ent and ent.get("by") == "line" and ent["to"] == m["line_to"]):
                            ent = None
                if ent is not None and ent.get("by") == "line" and m.get("place") not in PLACED:
                    ent = None                                  # a live fix: no estimate, the straight line as before
                    self.cache.pop(key, None)
                if (ent is None or ent["to"] != m["line_to"]) and self._moving(m):
                    ent = self._line(m, prev, now, p)                   # no road near (no graph): the straight line
                if ent is None or ent["to"] != m["line_to"]:      # (a call moved, not routed yet: none)
                    continue
                self._move(ent, m, now)
                out.append(self._public(ent, m, now, p))
            except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as e:   # one bad route: that one
                self.log(f"routes: {m['unit']}: route dropped ({type(e).__name__}: {e})")   # (routed afresh next build)
                self.cache.pop(key, None)
        for k in [k for k in self.cache if k not in seen]:
            del self.cache[k]
        for k in [k for k in self.failed if k not in seen]:
            del self.failed[k]
        return {"routes": out + backs, "trails": trails}

    # -- estimated motion (#453) ------------------------------------------------------------------------------

    @staticmethod
    def _moving(m: dict) -> bool:
        """Moved along its route by the estimate: responding, placed by status, where it starts from known."""
        return m.get("look") == "responding" and m.get("place") in PLACED and not m.get("start_unknown")

    def _start(self, ent: dict | None, m: dict, now: float) -> tuple[list, bool] | None:
        """Where a route worked out again starts: where the estimate has a rig moving by it now (rolling), else
        None (from its marker)."""
        mv = ent.get("move") if ent else None
        if mv is None or m.get("place") not in PLACED or now <= mv["t0"]:
            return None
        along = along_at(mv, now)                              # (held as far short of the call as before; its gate)
        return point_at(ent["coords"], ent["cum"], along)[0], True, mv.get("short_m"), along, ent.get("gate")

    def _line(self, m: dict, prev: dict | None, now: float, p: eta.Params) -> dict:
        """The straight line to the call (no road near, or no graph), from the marker or the estimate now."""
        if prev is not None and prev.get("by") == "line" and prev["to"] == m["line_to"]:
            return prev
        here = [m["lon"], m["lat"]]
        st = self._start(prev, m, now)
        a, to = (st[0] if st else here), list(m["line_to"])
        d = _m(a, to)
        ent = {"to": to, "origin": a, "anchor": here, "coords": [a, to], "cum": [0.0, d], "m": d,
               "s": eta.drive_s(a, to, p), "sig": None, "from": "status", "at": now, "why": "line", "fix_at": None,
               "away": 0, "along": 0.0, "remain": d, "by": "line", "number": m.get("incident_number") or ""}
        ent["move"] = make_move(now, d, ent["s"], ease=False, short=st[2]) if st else None
        self.cache[(m["unit"], m["incident_id"])] = ent
        return ent

    def _move(self, ent: dict, m: dict, now: float) -> None:
        """Its route's move: made when it responds (from its en-route time), held while it is dispatched again (stop),
        on from there when it responds again; none on a live fix or from an unknown start."""
        if m.get("place") not in PLACED or m.get("start_unknown"):
            ent["move"] = None
            return
        mv = ent.get("move")
        if mv is None and m.get("place") == "enroute" and _num(m.get("start_at")):
            ent["move"] = mv = make_move(m["start_at"], ent["m"], ent["s"])     # away from its station: held there
            mv["stop"] = mv["t0"]
        if m.get("look") == "responding":
            if mv is None:
                t = m.get("status_at")
                t = t if _num(t) and t <= now else now
                if _num(m.get("start_at")):
                    t = max(t, m["start_at"])          # from where it was then (a trip home, its last fix)
                ent["move"] = make_move(t, ent["m"], ent["s"])
            elif "stop" in mv:
                stop, t = mv.pop("stop"), m.get("status_at")
                t = t if _num(t) and stop <= t <= now else now     # on again from its en-route time
                paused = max(0.0, t - stop)
                mv["t0"] += paused
                mv["done_at"] += paused
        elif mv is not None and "stop" not in mv:
            mv["stop"] = now

    def _trips(self, markers: list[dict], now: float, g, p: eta.Params, pen) -> list[dict]:
        """Each rig's trip home (the module doc): started, moved on, ended; its marker rewritten. The trips' routes."""
        out, units = [], set()
        for m in markers:
            units.add(m["unit"])
            try:
                self._trip_one(m, now, g, p, pen, out)
            except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as e:   # one bad trip: that one forgotten
                self.log(f"routes: {m['unit']}: trip dropped ({type(e).__name__}: {e})")
                self.trips.pop(m["unit"], None)
        for u in [u for u in self.trips if u not in units]:
            del self.trips[u]
        for u in [u for u, f in self.fixes.items() if now - f[1] > OUT_S]:
            del self.fixes[u]
        return out

    def _trip_one(self, m: dict, now: float, g, p: eta.Params, pen, out: list) -> None:
        """One rig's trip home this build (_trips)."""
        u = m["unit"]
        place = m.get("place")
        if place == "position":                    # a live fix: always wins
            self.fixes[u] = ([m["lon"], m["lat"]], m["at"] if _num(m.get("at")) else now)
            self.trips.pop(u, None)
            return
        if place not in PLACED:                    # at a call (on location)
            self.trips.pop(u, None)
            return
        home = self._home(m.get("station") or "")
        trip = self.trips.get(u)
        if m.get("look") == "available":
            rel = self._released(m, now, trip)
            if rel is not None:
                trip = self._trip(g, rel, home, now, p, pen) if home is not None else None
                if trip is None:
                    self.trips.pop(u, None)
                else:
                    self.trips[u] = trip
            if trip is None:
                return
            q, at = p.quarters_status, m.get("status_at")
            if q and m.get("status") == q and _num(at) and at > trip["move"]["t0"] + QUARTERS_GRACE_S:
                self.trips.pop(u, None)            # reported back in quarters
                self.fixes.pop(u, None)
                return
            if now >= trip["move"]["done_at"]:     # there: in quarters, idle
                self.fixes.pop(u, None)
                if place == "station":
                    self.trips.pop(u, None)
                else:
                    _at_home(m, trip["home"])
                return
            out.append(self._apply(m, trip, now))
        elif m.get("look") not in GOING:           # out of service (any status neither free nor on a call)
            self.trips.pop(u, None)
            return
        elif trip is not None:                     # on a call while on its way home: from where it was then
            if "froze" not in trip:
                t = m.get("status_at")
                t = t if _num(t) and trip["move"]["t0"] <= t <= now else now
                trip["froze"] = (_est(trip, t)[0], t)
            ll, t = trip["froze"]
            m.update(place="enroute", group=f"unit:{u}", lon=ll[0], lat=ll[1], ring=0, start_at=t, est=True)
        else:
            f = self.fixes.get(u)
            if f is None or home is None or eta.haversine_km(f[0], home) <= AT_HOME_KM:
                return
            if now - f[1] <= LAST_FIX_S:           # from its last live fix
                m.update(place="enroute", group=f"unit:{u}", lon=f[0][0], lat=f[0][1], ring=0, start_at=f[1], est=True)
            elif now - f[1] <= OUT_S:
                m["start_unknown"] = True          # out, where unknown: not moved

    def _released(self, m: dict, now: float, trip: dict | None) -> tuple | None:
        """A release newer than its trip: (key, from [lon, lat], when, the call's number) or None. Cleared from a call
        it was on location at (mapview's returning), or cancelled while moving by estimate (its route's last state)."""
        cands = []
        if m.get("place") == "returning" and m.get("return_ll") and _num(m.get("cleared_at")):
            cands.append((("clear", m["cleared_at"]), list(m["return_ll"]), m["cleared_at"], m.get("returning_from") or ""))
        for k in [k for k in self.cache if k[0] == m["unit"]]:
            ent = self.cache.pop(k)                     # (done with: it is on no call now)
            mv = ent.get("move")
            if mv is None or now <= mv["t0"]:
                continue
            t = m.get("status_at")
            t = t if _num(t) and mv["t0"] <= t <= now else now
            cands.append((("cancel", k[1], ent["at"]), _est(ent, t)[0], t, ent.get("number") or ""))
        if not cands:
            return None
        c = cands[0] if cands[0][0][0] == "clear" else max(cands, key=lambda c: c[2])   # on location there: the clear
        if trip is not None and (c[0] == trip["key"] or c[2] <= trip["move"]["t0"]):
            return None
        return c

    def _trip(self, g, rel: tuple, home: list, now: float, p: eta.Params, pen) -> dict:
        """A trip home: by road (no wrong way, the road speeds without the emergency factor), else the straight line."""
        key, a, t0, number = rel
        r = None
        if g is not None:
            gate = self.gate_of(home, "ingress")                   # in by the bay doors
            try:
                r = g.route(a, gate[0] if gate else home, p, pen()[0], wrongway_m=0.0, call=roadgraph.LOCAL,
                            b_gate=gate[1] if gate else None)
            except Exception as e:  # noqa: BLE001 - the straight line then
                self.log(f"routes: way home: {type(e).__name__}: {e}")
            self.stats["routed"] += 1
            self.stats["why"]["back"] = self.stats["why"].get("back", 0) + 1
        if r is not None and len(r["coords"]) >= 2:
            coords, by = r["coords"], "road"
            secs = r["s"] * max(0.5, p.emergency_factor)
            bay = _m(coords[-1], home)
            if gate and 0.5 <= bay <= 2 * GATE_M:                # from the apron into the bays: no jump at the end
                coords = [*coords, [round(home[0], 6), round(home[1], 6)]]
                secs += bay / BAY_MPS
        else:
            coords, by = [list(a), list(home)], "line"
            secs = eta.drive_s(a, home, p)
        cum = _cum(coords)
        return {"key": key, "home": list(home), "number": number, "coords": coords, "cum": cum, "m": cum[-1],
                "s": secs, "by": by, "at": now, "move": make_move(t0, cum[-1], secs, hold=False)}

    def _apply(self, m: dict, trip: dict, now: float) -> dict:
        """A rig on its way home: its marker at the estimate now (returning), and its route home."""
        along = along_at(trip["move"], now)
        ll = point_at(trip["coords"], trip["cum"], along)[0]
        m.update(place="returning", group=f"unit:{m['unit']}", lon=ll[0], lat=ll[1], ring=0, line_to=list(trip["home"]),
                 home_line=True, returning_from=trip["number"] or m.get("returning_from") or "", est=True)
        remain = max(0.0, trip["m"] - along)
        remain_s = trip["s"] * remain / trip["m"] if trip["m"] > 0 else 0.0
        return {"unit": m["unit"], "incident_id": None, "kind": "back", "coords": trip["coords"], "m": round(trip["m"]),
                "s": round(trip["s"]), "along_m": round(along), "remain_m": round(remain), "remain_s": round(remain_s),
                "eta_s": round(remain_s), "text": f"~{max(1, round(remain_s / 60))} min · {eta.miles(remain)}",
                "from": "status", "at": trip["at"], "why": "back", "by": trip["by"], "move": dict(trip["move"])}

    def _why(self, ent: dict | None, m: dict, sig: tuple, p: eta.Params) -> str:
        """Why the route must be worked out again now ('' = keep it); a new fix moves the rig along it here."""
        if ent is not None and ent.pop("restored", False) and ent["to"] == m["line_to"]:
            ent["sig"] = sig                             # read back after a restart: kept (the same estimate)
        if ent is None or ent["to"] != m["line_to"] or ent["sig"] != sig:
            return "new"
        here = [m["lon"], m["lat"]]
        if m.get("place") != "position":
            return "moved" if _m(here, ent.get("anchor") or ent["origin"]) > MOVED_M or ent["from"] == "fix" else ""
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

    def _route(self, g, m: dict, sig: tuple, pen: dict, p: eta.Params, why: str, now: float,
               call: roadgraph.Call | None = None, start: tuple | None = None) -> dict | None:
        """start: (where, True) to route from where the estimate has it now, rolling on (#453); else its marker."""
        here = [m["lon"], m["lat"]]
        a = list(start[0]) if start else here
        gate, along0 = None, 0.0
        if start is None:
            gate = self._quarters_gate(m)                          # in quarters: out by the bay doors
        elif _gate_ok(start[4]) and start[3] <= start[4][2]:
            gate, along0 = (start[4][0], start[4][1]), start[3]     # still on the apron: out the same way, rolling on
        fix = m.get("place") == "position"
        hd = m.get("heading") if fix and isinstance(m.get("speed"), (int, float)) and m["speed"] >= AWAY_MIN_MPS else None
        t0 = time.monotonic()
        try:
            r = g.route(gate[0] if gate else a, m["line_to"], p, pen, heading=hd, wrongway_m=p.wrongway_m, call=call,
                        a_gate=gate[1] if gate else None)
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
        cum = _cum(coords)
        mv = make_move(now, cum[-1], r["s"], ease=False, short=start[2]) if start and not fix else None
        if mv is not None and along0 > 0 and mv["mps"] > 0:
            back = along0 / mv["mps"]                             # where it was on the apron: no jump back to its start
            mv["t0"], mv["done_at"] = round(mv["t0"] - back, 3), round(mv["done_at"] - back, 3)
        lead = cum[1] if gate and len(coords) > 2 and _m(coords[0], gate[0]) < 0.5 else 0.0
        ent = {"to": list(m["line_to"]), "origin": a, "anchor": here, "coords": coords, "cum": cum, "m": cum[-1],
               "s": r["s"], "sig": sig, "from": "fix" if fix else "status", "at": now, "why": why, "fix_at": m.get("at"),
               "away": 0, "along": 0.0, "remain": cum[-1], "by": "road", "number": m.get("incident_number") or "",
               "move": mv, "gate": [list(gate[0]), gate[1], round(lead, 1)] if gate else None}
        self.cache[(m["unit"], m["incident_id"])] = ent
        return ent

    def _public(self, ent: dict, m: dict, now: float, p: eta.Params) -> dict:
        mv = ent.get("move")
        along = ent["along"] if m.get("place") == "position" else along_at(mv, now) if mv else 0.0
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
                "from": ent["from"], "at": ent["at"], "why": ent["why"], "kind": "to", "by": ent.get("by", "road"),
                "move": dict(mv) if mv else None}

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

    def trip_at(self, unit: str, now: float | None = None) -> dict | None:
        """Where the maps have a rig on its way home (Cad.locations: the recommendation agrees with the map, #453):
        {"ll", "number", "home": False} on its way, {"home": True} there, None (no trip: mapview's)."""
        now = self.clock() if now is None else now
        with self._lock:
            if not self._loaded:
                self._load()
            t = self.trips.get(unit)
            if t is None or "froze" in t:
                return None
            if t.get("done") or now >= t["move"]["done_at"]:
                return {"home": True, "ll": list(t["home"])}
            return {"home": False, "ll": point_at(t["coords"], t["cum"], along_at(t["move"], now))[0],
                    "number": t["number"]}

    # -- kept across a restart (#453) ---------------------------------------------------------------------------

    def _load(self) -> None:
        """The kept trips and moves, each checked (_ok_trip, _ok_ent): a bad one left out; a record that can't be read
        at all: started afresh (nothing half read)."""
        self._loaded = True
        trips, cache, raw = {}, {}, None
        try:
            raw = self.persist[0]() if self.persist else None
            doc = json.loads(raw) if raw else {}
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            for k in ("trips", "moves"):
                if doc.get(k) and not isinstance(doc[k], dict):
                    self.log(f"routes: kept {k} left out (not an object)")
            got = doc.get("trips") or {}
            for u, t in (got.items() if isinstance(got, dict) else ()):
                if isinstance(u, str) and _ok_trip(t):
                    t["key"] = tuple(t["key"])
                    if t.get("froze") is not None:
                        t["froze"] = (list(t["froze"][0]), float(t["froze"][1]))
                    trips[u] = t
                else:
                    self.log(f"routes: a kept trip left out ({str(u)[:20]})")
            got = doc.get("moves") or {}
            for k, ent in (got.items() if isinstance(got, dict) else ()):
                unit, _, iid = str(k).rpartition("|")
                if unit and iid.lstrip("-").isdigit() and _ok_ent(ent):
                    cache[(unit, int(iid))] = dict(ent, sig=None, restored=True, fix_at=None, away=0, along=0.0,
                                                   remain=ent["m"])
                else:
                    self.log(f"routes: a kept move left out ({str(k)[:30]})")
        except Exception as e:  # noqa: BLE001 - a bad record: started afresh, nothing of it kept
            self.log(f"routes: kept trips not read ({type(e).__name__}: {e})")
            return
        self.trips.update(trips)
        self.cache.update(cache)
        self._saved = raw or ""

    def _save(self) -> None:
        if not self.persist:
            return
        keep = ("to", "origin", "anchor", "coords", "cum", "m", "s", "from", "at", "why", "by", "number", "move", "gate")
        doc = {"trips": {u: dict(t, key=list(t["key"])) for u, t in self.trips.items()},
               "moves": {f"{k[0]}|{k[1]}": {x: e.get(x) for x in keep} for k, e in self.cache.items() if e.get("move")}}
        raw = json.dumps(doc, separators=(",", ":"))
        if raw == self._saved:
            return
        try:
            self.persist[1](raw)
            self._saved = raw
        except Exception as e:  # noqa: BLE001 - not kept this time: tried again next build
            self.log(f"routes: trips not kept ({type(e).__name__}: {e})")

    # -- the recommendation -----------------------------------------------------------------------------------

    def to_one(self, origins: dict, to, inc: dict | None = None, quarters: dict | None = None) -> dict:
        """{key: (drive seconds, metres) | None (no way)} by road from each [lon, lat] origin to `to` (inc: the call,
        its words: a highway call or not); {} with no graph. quarters: {key: its station's name ('' unknown)} for the
        origins in quarters (runcards.estimates: no turnout off the road): from their gate, as the map's routes;
        every other origin from where it is."""
        g = self.graph()
        if g is None or not origins or to is None:
            return {}
        p = self.params()
        try:
            pen, _ = self.penalties(g, self.clock())
            pts, gates = {}, {}
            for k, ll in origins.items():
                gate = None
                if quarters and k in quarters:                     # in quarters: from its gate, as the map's route
                    f = self.firehouse(quarters[k])
                    gate = self.gate(f) if f else self.gate_of(ll)
                pts[k] = gate[0] if gate else ll
                if gate:
                    gates[k] = gate[1]
            return g.to_one(pts, to, p, pen, wrongway_m=p.wrongway_m, call=self.call_of(inc), gates=gates)
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


def street_of(address: str | None) -> str:
    """The street of a firehouse's address ("51-53 Grace Church St" -> "Grace Church St"); '' for none."""
    a = re.split(r"[,(]", address or "")[0]
    a = re.sub(r"^\s*\d+[a-z]?(?:\s*[-\u2013/]\s*\d+[a-z]?)?\s+", "", a, flags=re.I).strip()
    return a if re.search(r"[a-z]", a, re.I) else ""


def _m(a, b) -> float:
    return eta.haversine_km(a, b) * 1000.0


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pt(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v)


def _path_ok(d: dict) -> bool:
    c, cum = d.get("coords"), d.get("cum")
    return (isinstance(c, list) and len(c) >= 2 and all(_pt(x) for x in c) and isinstance(cum, list)
            and len(cum) == len(c) and all(_num(x) for x in cum) and _num(d.get("m")) and _num(d.get("s")))


def _ok_move(mv) -> bool:
    """A kept move: its numbers (stop and short_m when there)."""
    return (isinstance(mv, dict) and all(_num(mv.get(k)) for k in ("t0", "mps", "hold_m", "done_at"))
            and all(k not in mv or _num(mv[k]) for k in ("ease_s", "stop", "short_m")))


def _ok_trip(t) -> bool:
    """A kept trip home: its key, home, path and move (and where it froze, when it did)."""
    if not (isinstance(t, dict) and isinstance(t.get("key"), list) and t["key"] and _pt(t.get("home"))
            and _path_ok(t) and _ok_move(t.get("move")) and _num(t.get("at")) and isinstance(t.get("number"), str)
            and isinstance(t.get("by"), str)):
        return False
    fz = t.get("froze")
    return fz is None or (isinstance(fz, list) and len(fz) == 2 and _pt(fz[0]) and _num(fz[1]))


def _ok_ent(e) -> bool:
    """A kept route with its move (Routes.cache's entry)."""
    return (isinstance(e, dict) and _pt(e.get("to")) and _pt(e.get("origin")) and _path_ok(e) and _num(e.get("at"))
            and _ok_move(e.get("move")) and _pt(e.get("anchor"))
            and all(isinstance(e.get(k), str) for k in ("from", "why", "by")))


def _gate_ok(g) -> bool:
    """A route's gate as kept: [[lon, lat], street, lead metres]."""
    return isinstance(g, list) and len(g) == 3 and _pt(g[0]) and isinstance(g[1], str) and _num(g[2])


def _cum(coords: list) -> list[float]:
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + _m(a, b))
    return cum


def make_move(t0: float, metres: float, secs: float, ease: bool = True, hold: bool = True,
              short: float | None = None) -> dict:
    """A move along a route (#453; cadmap.js alongAt is the same): from t0 at the route's average speed (metres /
    secs), from rest (ease: EASE_S) or rolling; hold: to a call, held HOLD_FRAC of the way (at most HOLD_MAX_M short)
    until on location; else (home) all the way. done_at: when it is there (held, or home). short: routed again from
    where it was, held as far short of the call as the first route said (no creep)."""
    v = metres / secs if secs > 0 and metres > 0 else 0.0
    if not hold:
        hold_m = metres
    elif short is not None:
        hold_m = metres - short
    else:
        hold_m = metres - min(metres * (1 - HOLD_FRAC), HOLD_MAX_M)
    hold_m = max(0.0, hold_m)
    e = EASE_S if ease else 0.0
    return {"t0": round(float(t0), 3), "mps": round(v, 3), "hold_m": round(hold_m, 1), "ease_s": e,
            "done_at": round(float(t0) + (hold_m / v + e if v > 0 else 0.0), 3), "short_m": round(metres - hold_m, 1)}


def along_at(mv: dict, t: float) -> float:
    """Metres along its route at t (epoch s): from rest over ease_s up to mps, never past hold_m, held from stop."""
    t = min(t, mv["stop"]) if "stop" in mv else t
    dt = t - mv["t0"]
    if dt <= 0:
        return 0.0
    v, e = mv["mps"], mv.get("ease_s") or 0.0
    d = v * (dt - e * (1 - math.exp(-dt / e))) if e > 0 else v * dt
    return min(mv["hold_m"], max(0.0, d))


def point_at(coords: list, cum: list, d: float) -> tuple[list, float | None]:
    """The [lon, lat] d metres along the path, and the path's bearing there (degrees)."""
    if len(coords) < 2:
        return list(coords[0]), None
    i = max(0, min(len(coords) - 2, bisect.bisect_right(cum, d) - 1))
    while i < len(coords) - 2 and cum[i + 1] - cum[i] <= 0:
        i += 1
    a, b = coords[i], coords[i + 1]
    seg = cum[i + 1] - cum[i]
    k = 0.0 if seg <= 0 else max(0.0, min(1.0, (d - cum[i]) / seg))
    kx = math.cos(math.radians(a[1]))
    brg = math.degrees(math.atan2((b[0] - a[0]) * kx, b[1] - a[1])) % 360 if seg > 0 else None
    return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k], brg


def _est(ent: dict, t: float) -> tuple[list, float | None]:
    """Where a route's move has it at t."""
    return point_at(ent["coords"], ent["cum"], along_at(ent["move"], t))


def _at_home(m: dict, home: list) -> None:
    """Its trip home done: in quarters, idle (#377: folded with the firehouse's others)."""
    m.update(place="station", group=f"station:{m['station']}", lon=home[0], lat=home[1], ring=0, line_to=None,
             home_line=False)
    for k in ("returning_from", "return_ll", "cleared_at", "est"):
        m.pop(k, None)


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
