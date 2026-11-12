"""Rigs with no live fix moved along their route, to the call and home, marked estimated (GitHub #453).

The user: "even though we don't yet have their location data, we have a path now for them on their likely response
path to the call (#360/#396 routing). Instead of showing straight lines anymore, can we show them following that path
(until we get real location data in), and on the way back to quarters after being released too."

  the hub     routes.py: a `move` on each route (en-route time, the route's average speed, from rest, held short of the
              call until on location); trips home after a release or a cancel (one-ways the right way); the straight
              line where no road is near; a live fix always wins; placeholders never.
  the page    cadmap.js in node with the stand-in MapLibre from test_unit_follow.py: the rig at alongAt(move, now),
              turned to the road, translucent with "~"; the route ahead of it; a glide on location; home and folded;
              position and rotation only on the frames, at most ~8 Hz.

Synthetic street grids (made-up names and points) and the real downtown extract from test_rig_routes.py (OpenStreetMap
contributors, ODbL); every call, status and fix is made up ("100 Example Ave")."""

import time

import pytest

from sizeup.cad import eta, mapview
from sizeup.cad.routes import (EASE_S, HOLD_FRAC, HOLD_MAX_M, LAST_FIX_S, Routes, _cum, along_at, make_move,
                               point_at)
from sizeup.cad.service import Cad, Settings

from tests.test_rig_routes import LAT0, NOW, at, grid, ladder, osm_graph
from tests.test_unit_follow import STATIC, needs_node, run

P = eta.Params()
OP = {"operator": "Lt. Example"}
HQ = {"name": "HQ", "ll": at(0, 0)}


def routes_on(g, params=P):
    r = Routes(lambda: g, lambda: params, lambda: [], lambda: [HQ], log=lambda m: None, spawn=lambda fn: fn())
    return r


def going(look="responding", status_at=NOW, **kw):
    """E61 from HQ to a made-up call at grid (5, 5), placed by status."""
    return {"unit": "E61", "incident_id": 7, "incident_number": "26-000007", "look": look, "place": "station",
            "group": "station:HQ", "lon": at(0, 0)[0], "lat": at(0, 0)[1], "line_to": at(5, 5), "station": "HQ",
            "status": "RS" if look == "responding" else "DP", "status_at": status_at, "ring": 0, **kw}


def free(status="CE", status_at=NOW, **kw):
    return {"unit": "E61", "incident_id": None, "look": "available", "place": "station", "group": "station:HQ",
            "lon": at(0, 0)[0], "lat": at(0, 0)[1], "line_to": None, "station": "HQ", "status": status,
            "status_at": status_at, "ring": 0, "home_line": False, **kw}


def one(r, m, t, kind="to"):
    rs = [x for x in r.for_map([m], t)["routes"] if x.get("kind") == kind]
    return rs[0] if rs else None


def metres(a, b):
    return eta.haversine_km(a, b) * 1000


# ---- the hub: the move ---------------------------------------------------------------------------------------------


def test_the_position_at_t_along_the_route():
    mv = make_move(NOW, 1000.0, 100.0)                      # 10 m/s average
    assert mv["mps"] == 10.0 and mv["ease_s"] == EASE_S
    assert along_at(mv, NOW - 5) == 0 and along_at(mv, NOW) == 0
    assert 0 < along_at(mv, NOW + 2) < 0.2 * 10 * 2 + 5     # from rest: well short of full speed at first
    assert along_at(mv, NOW + 60) == pytest.approx(10 * (60 - EASE_S), abs=1)   # then at the route's speed
    coords = [at(0, 0), at(3, 0), at(3, 3)]
    cum = _cum(coords)
    ll, brg = point_at(coords, cum, cum[1] / 2)
    assert ll == pytest.approx(at(1.5, 0), abs=1e-7) and brg == pytest.approx(90, abs=0.5)   # along the road, east
    ll, brg = point_at(coords, cum, cum[1] + 10)
    assert brg == pytest.approx(0, abs=0.5) and ll[0] == pytest.approx(at(3, 0)[0])        # round the corner: north
    r = routes_on(grid())
    a = one(r, going(), NOW + 40)
    mv = a["move"]
    assert a["by"] == "road" and mv["t0"] == NOW and mv["mps"] == pytest.approx(a["m"] / a["s"], rel=0.01)
    assert a["along_m"] == pytest.approx(along_at(mv, NOW + 40), abs=1) and a["remain_m"] == pytest.approx(a["m"] - a["along_m"], abs=1)
    assert r.stats["routed"] == 1                            # routed once: the page interpolates
    for t in (60, 80):
        assert one(r, going(), NOW + t)["move"] == mv
    assert r.stats["routed"] == 1


def test_dispatched_has_no_move_and_it_starts_when_responding():
    r = routes_on(grid())
    assert one(r, going("dispatched", NOW - 30), NOW)["move"] is None          # turnout: at its station
    mv = one(r, going(status_at=NOW + 50), NOW + 60)["move"]
    assert mv["t0"] == NOW + 50 and r.stats["routed"] == 1                     # the en-route time, the same route


def test_held_short_of_the_call_until_on_location():
    r = routes_on(grid())
    a = one(r, going(status_at=NOW - 3600), NOW)
    mv = a["move"]
    assert mv["hold_m"] == pytest.approx(max(a["m"] * HOLD_FRAC, a["m"] - HOLD_MAX_M), abs=1)
    assert a["along_m"] == pytest.approx(mv["hold_m"], abs=1) and a["remain_m"] > 0     # never at the call
    long = make_move(NOW, 5000.0, 400.0)
    assert long["hold_m"] == 5000 - HOLD_MAX_M                                    # at most 150 m short
    on = dict(going(), look="onscene", place="incident", group="incident:7", line_to=None)
    assert r.for_map([on], NOW + 10)["routes"] == []                              # on location: no route, no trip


def test_dispatched_again_holds_it_and_responding_again_goes_on_from_there():
    r = routes_on(grid())
    one(r, going(), NOW + 20)
    held = one(r, going("dispatched", NOW + 30), NOW + 30)
    d = held["along_m"]
    assert held["move"]["stop"] == NOW + 30 and one(r, going("dispatched", NOW + 30), NOW + 90)["along_m"] == d
    again = one(r, going(status_at=NOW + 90), NOW + 90)
    assert "stop" not in again["move"] and again["along_m"] == pytest.approx(d, abs=1)   # no jump
    assert one(r, going(status_at=NOW + 90), NOW + 100)["along_m"] > d + 50


def test_the_call_moved_is_routed_again_from_where_the_estimate_has_it():
    r = routes_on(grid())
    one(r, going(), NOW + 40)
    ent = r.cache[("E61", 7)]
    here = point_at(ent["coords"], ent["cum"], along_at(ent["move"], NOW + 41))[0]
    moved = one(r, going(line_to=at(5, 3)), NOW + 41)
    assert moved["coords"][0] == pytest.approx(here, abs=0.0004) and moved["coords"][-1] == pytest.approx(at(5, 3))
    assert moved["move"]["t0"] == NOW + 41 and moved["move"]["ease_s"] == 0       # rolling on, not from rest
    assert moved["along_m"] == 0


def test_moved_to_another_call_goes_on_from_where_it_was():
    r = routes_on(grid())
    one(r, going(), NOW + 40)
    ent = r.cache[("E61", 7)]
    here = point_at(ent["coords"], ent["cum"], along_at(ent["move"], NOW + 41))[0]
    other = one(r, going(incident_id=8, incident_number="26-000008", line_to=at(0, 5)), NOW + 41)
    assert other["incident_id"] == 8 and other["coords"][0] == pytest.approx(here, abs=0.0004)


def test_no_road_near_is_the_straight_line_still_estimated():
    w = ladder()
    r = routes_on(w.graph())
    far = going(line_to=at(1, 40))
    a = one(r, far, NOW + 30)
    assert a["by"] == "line" and a["coords"] == [at(0, 0), at(1, 40)] and a["move"]["t0"] == NOW
    assert a["move"]["mps"] == pytest.approx(a["m"] / eta.drive_s(at(0, 0), at(1, 40), P), rel=0.01)
    assert one(r, far, NOW + 31)["move"] == a["move"]                              # kept, not made again
    none = Routes(lambda: None, lambda: P, firehouses=lambda: [HQ], log=lambda m: None)
    b = one(none, going(), NOW + 30)
    assert b["by"] == "line" and b["move"] is not None                             # no graph yet: the same
    fixed = one(none, dict(going(), place="position", at=NOW + 29), NOW + 30)
    assert fixed is None                                                           # a live fix: nothing to estimate


# ---- the hub: home ---------------------------------------------------------------------------------------------------


def returning(t_clear, now, **kw):
    """E61 cleared from a made-up call at grid (4, 1) (on Oak St, one-way east) at t_clear, as mapview places it."""
    unit = {"name": "E61", "status": "AV", "status_at": t_clear}
    clear = {"incident_id": 5, "number": "26-000005", "lat": at(4, 1)[1], "lon": at(4, 1)[0], "cleared_at": t_clear}
    m = mapview.place(unit, "free", HQ, None, None, now, fire=True, clear=clear, params=P)
    return dict(m, **kw)


def test_the_way_home_by_road_the_right_way_and_folded_there():
    r = routes_on(ladder().graph())
    m = returning(NOW, NOW + 10)
    assert m["place"] == "returning" and m["return_ll"] == at(4, 1)
    back = one(r, m, NOW + 10, "back")
    assert back["incident_id"] is None and back["coords"][0] == pytest.approx(at(4, 1)) and back["coords"][-1] == pytest.approx(at(0, 0))
    # Oak St is one-way east: home (west) is by East St and Main St, never west on Oak St
    mid = point_at(back["coords"], _cum(back["coords"]), back["m"] / 2)[0]
    assert mid[1] == pytest.approx(LAT0, abs=1e-5)                                # half way: on Main St
    mv = back["move"]
    assert mv["t0"] == NOW and mv["hold_m"] == pytest.approx(back["m"], abs=1)   # all the way home
    assert mv["mps"] == pytest.approx(back["m"] / back["s"], rel=0.03)
    assert back["s"] == pytest.approx(one(routes_on(ladder().graph()), going(lon=at(4, 1)[0], lat=at(4, 1)[1], line_to=at(0, 0)), NOW)["s"]
                                      * P.emergency_factor, rel=0.35)           # no emergency factor home
    m2 = returning(NOW, NOW + 30)
    one(r, m2, NOW + 30, "back")
    assert (m2["lon"], m2["lat"]) == pytest.approx(tuple(point_at(back["coords"], _cum(back["coords"]), along_at(mv, NOW + 30))[0]))
    assert m2["group"] == "unit:E61" and m2["home_line"] and m2["line_to"] == at(0, 0)
    done = mv["done_at"] + 1
    m3 = returning(NOW, done)
    assert m3["place"] == "returning"                                            # mapview's window is longer
    assert r.for_map([m3], done)["routes"] == []
    assert (m3["place"], m3["group"], m3["lon"], m3["lat"], m3["line_to"]) == ("station", "station:HQ", *at(0, 0), None)
    assert "returning_from" not in m3 and "E61" in r.trips                         # not started again
    m4 = returning(NOW, NOW + 3600)                                               # mapview: home too
    r.for_map([m4], NOW + 3600)
    assert m4["place"] == "station" and r.trips == {}


def test_in_quarters_with_the_release_still_goes_home_and_reported_later_ends_it():
    clear = {"incident_id": 5, "number": "26-000005", "lat": at(4, 1)[1], "lon": at(4, 1)[0], "cleared_at": NOW}
    with_release = {"name": "E61", "status": "AQ", "status_at": NOW + 2}
    assert mapview.locate(with_release, "free", HQ, None, None, NOW + 30, clear, P)["where"] == "returning"
    later = {"name": "E61", "status": "AQ", "status_at": NOW + 25}
    assert mapview.locate(later, "free", HQ, None, None, NOW + 30, clear, P)["where"] == "station"
    r = routes_on(ladder().graph())
    m = mapview.place(with_release, "free", HQ, None, None, NOW + 10, fire=True, clear=clear, params=P)
    assert one(r, m, NOW + 10, "back") is not None
    m = mapview.place(later, "free", HQ, None, None, NOW + 30, fire=True, clear=clear, params=P)
    assert r.for_map([m], NOW + 30)["routes"] == [] and m["place"] == "station" and r.trips == {}


def test_cancelled_en_route_goes_home_from_where_it_was():
    r = routes_on(grid())
    a = one(r, going(), NOW + 40)
    ent = r.cache[("E61", 7)]
    there = point_at(ent["coords"], ent["cum"], along_at(ent["move"], NOW + 45))[0]
    m = free("CE", NOW + 45)
    back = one(r, m, NOW + 46, "back")
    assert back["coords"][0] == pytest.approx(there, abs=1e-6) and back["coords"][-1] == pytest.approx(at(0, 0))
    assert back["move"]["t0"] == NOW + 45 and back["move"]["ease_s"] == EASE_S
    assert m["place"] == "returning" and m["returning_from"] == "26-000007" and m["home_line"]
    assert metres([m["lon"], m["lat"]], there) < 5
    assert ("E61", 7) not in r.cache and a["along_m"] > 0
    later = free("CE", NOW + 45)
    assert one(r, later, NOW + 60, "back")["move"] == back["move"]               # one trip, moved on by time
    # cancelled before it moved (dispatched, turnout): nothing to go home from
    r2 = routes_on(grid())
    one(r2, going("dispatched", NOW), NOW + 20)
    m = free("CE", NOW + 30)
    assert r2.for_map([m], NOW + 30)["routes"] == [] and m["place"] == "station"


def test_dispatched_while_on_its_way_home_starts_from_where_the_trip_had_it():
    r = routes_on(grid())
    one(r, going(), NOW + 40)
    one(r, free("CE", NOW + 45), NOW + 46, "back")
    trip = r.trips["E61"]
    there = point_at(trip["coords"], trip["cum"], along_at(trip["move"], NOW + 50))[0]
    m = going("dispatched", NOW + 50, incident_id=9, line_to=at(5, 0))
    nxt = one(r, m, NOW + 51)
    assert m["place"] == "returning" and (m["lon"], m["lat"]) == pytest.approx(tuple(there))
    assert nxt["coords"][0] == pytest.approx(there, abs=0.0004)
    m = going(status_at=NOW + 70, incident_id=9, line_to=at(5, 0))
    assert one(r, m, NOW + 80)["move"]["t0"] == NOW + 70 and (m["lon"], m["lat"]) == pytest.approx(tuple(there))


def test_a_live_fix_always_wins_and_its_last_fix_starts_the_next_trip():
    r = routes_on(grid())
    m = returning(NOW, NOW + 10)
    r2 = routes_on(ladder().graph())
    assert one(r2, m, NOW + 10, "back") is not None
    fix = dict(returning(NOW, NOW + 20), place="position", lon=at(3, 0)[0], lat=at(3, 0)[1], at=NOW + 19)
    assert r2.for_map([fix], NOW + 20)["routes"] == [] and r2.trips == {}          # AVL: no estimate at all
    live = dict(going(), place="position", at=NOW + 19)
    assert one(r, live, NOW + 20)["move"] is None
    # its fix since dropped by the maps (older than the maps keep), recent: from there
    r3 = routes_on(grid())
    r3.for_map([dict(free("AV"), place="position", lon=at(2, 2)[0], lat=at(2, 2)[1], at=NOW)], NOW)
    m = going(status_at=NOW + 400)
    a = one(r3, m, NOW + 410)
    assert (m["lon"], m["lat"]) == pytest.approx(tuple(at(2, 2))) and a["coords"][0] == pytest.approx(at(2, 2), abs=0.0004)
    assert a["move"]["t0"] == NOW + 400
    # older than LAST_FIX_S, away from home: where it is is unknown: not moved, at its station as before
    r4 = routes_on(grid())
    r4.for_map([dict(free("AV"), place="position", lon=at(2, 2)[0], lat=at(2, 2)[1], at=NOW)], NOW)
    m = going(status_at=NOW + LAST_FIX_S + 100)
    a = one(r4, m, NOW + LAST_FIX_S + 110)
    assert m["start_unknown"] and m["place"] == "station" and a["move"] is None


def test_a_placeholder_is_never_moved_or_sent_home():
    r = routes_on(grid())
    ph = dict(going(), unit="DPT239", placeholder=True)
    assert r.for_map([ph], NOW + 30)["routes"] == []
    assert r.for_map([dict(returning(NOW, NOW + 10), unit="DPT239", placeholder=True)], NOW + 10)["routes"] == []
    assert r.cache == {} and r.trips == {}


def test_the_console_end_to_end_on_the_real_extract():
    """A made-up call in the real downtown streets: E61 from headquarters responds, is moved by the estimate, goes on
    location, is released and goes home by road, and is in quarters at the end."""
    from tests.test_proximity import Clock
    clock = Clock(NOW)
    c = Cad(Settings(), log=lambda m: None, clock=clock)
    g = osm_graph()
    c.routes = Routes(lambda: g, lambda: c.eta, lambda: [], lambda: c.firehouses, clock=c.clock, log=lambda m: None)
    hq = c.firehouses[0]
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    c.unit_station(uid("E61"), {"station": hq["name"], **OP})
    iid = c.create({"type": "STRU", "address": "100 Example Ave", "lat": 41.0020, "lon": -73.6650, **OP})["id"]
    c.attach(iid, {"unit_ids": [uid("E61")], **OP})
    c.unit_status(uid("E61"), {"status": "RS", **OP})
    clock.t += 30
    rt = next(x for x in c.state()["map"]["routes"] if x["unit"] == "E61")
    assert rt["move"]["t0"] == NOW and 0 < rt["along_m"] < rt["m"] and rt["by"] == "road"
    clock.t += 3600
    rt = next(x for x in c.state()["map"]["routes"] if x["unit"] == "E61")
    assert rt["along_m"] == pytest.approx(rt["move"]["hold_m"], abs=1)
    c.unit_status(uid("E61"), {"status": "OL", **OP})
    st = c.state()["map"]
    assert next(m for m in st["markers"] if m["unit"] == "E61")["place"] == "incident" and st["routes"] == []
    clock.t += 600
    c.unit_status(uid("E61"), {"status": "AV", **OP})
    clock.t += 5
    st = c.state()["map"]
    back = next(x for x in st["routes"] if x["unit"] == "E61")
    m = next(m for m in st["markers"] if m["unit"] == "E61")
    assert back["kind"] == "back" and back["coords"][-1] == pytest.approx(hq["ll"], abs=0.003)
    assert m["place"] == "returning" and m["home_line"]
    clock.t = back["move"]["done_at"] + 1
    st = c.state()["map"]
    m = next(m for m in st["markers"] if m["unit"] == "E61")
    assert m["place"] == "station" and m["lon"] == hq["ll"][0] and not [x for x in st["routes"] if x["unit"] == "E61"]


# ---- the page --------------------------------------------------------------------------------------------------------


def page_state():
    """The hub's state on this run's clock (the page reads its own): E60 and E62 in quarters at HQ, E61 responding from
    HQ to "100 Example Ave" (made up) by road in the real extract, 20 s after its en-route time; L31 on location."""
    from tests.test_proximity import Clock
    clock = Clock(time.time())
    c = Cad(Settings(), log=lambda m: None, clock=clock)
    g = osm_graph()
    c.routes = Routes(lambda: g, lambda: c.eta, lambda: [], lambda: c.firehouses, clock=c.clock, log=lambda m: None)
    hq = c.firehouses[0]
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    for n in ("E60", "E61", "E62", "L31"):
        c.unit_station(uid(n), {"station": hq["name"], **OP})
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0020, "lon": -73.6650, **OP})
    c.attach(inc["id"], {"unit_ids": [uid("L31")], "status": "OL", **OP})
    c.attach(inc["id"], {"unit_ids": [uid("E61")], **OP})
    clock.t -= 20
    c.unit_status(uid("E61"), {"status": "RS", **OP})
    clock.t += 20
    st = c.state()
    return c, clock, uid, {"map": st["map"], "lists": st["lists"], "static": str(STATIC), "hq": hq,
                           "inc": {"id": inc["id"], "lon": -73.6650, "lat": 41.0020}}


PRE = r"""
const zoomTo = (z) => { rm.userMoved = true; rm.map.zoom = z; rm.map.emit("zoom"); rm.map.emit("zoomend"); };
const ll = (u) => { const p = chip(u).marker.getLngLat(); return [p.lng, p.lat]; };
const routeOf = (u) => rm.map.getSource("cm-routes").data.features.find((f) => f.properties.unit === u && !f.properties.trail);
const lineOf = (u, to) => rm.map.getSource("cm-lines").data.features.filter((f) => JSON.stringify(f.geometry.coordinates[1]) === JSON.stringify(to));
const nowS = () => Date.now() / 1000;
"""


@needs_node
def test_the_page_moves_it_along_the_route_marked_estimated_with_the_route_ahead(tmp_path):
    *_, data = page_state()
    rt = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    o = run(tmp_path, PRE + r"""
out.t0 = nowS(); out.at0 = ll("E61"); out.cls = cls("E61"); out.st = chip("E61").parts.st.textContent;
out.title = chip("E61").el.title; out.rot0 = chip("E61").el.style["--rot"];
out.route0 = routeOf("E61").geometry.coordinates;
out.lines = lineOf("E61", IN.map.markers.find((m) => m.unit === "E61").line_to).length;
const veh = chip("E61").el.children[0], html = veh.innerHTML, cn = chip("E61").el.className;
let ticks = 0; const t = L.tick.bind(L); L.tick = () => { ticks++; t(); };
run(60, 16);                                    // ~1 s of frames
out.ticks = ticks; out.t1 = nowS(); out.at1 = ll("E61"); out.rot1 = chip("E61").el.style["--rot"];
out.route1 = routeOf("E61").geometry.coordinates[0];
out.same = [chip("E61").el.children[0] === veh, veh.innerHTML === html, chip("E61").el.className === cn];
""", data)
    coords, cum = rt["coords"], _cum(rt["coords"])
    for t_key, ll_key, tol in (("t0", "at0", 1.5), ("t1", "at1", 1 + 0.16 * rt["move"]["mps"])):   # (a tick ~8 Hz)
        want = point_at(coords, cum, along_at(rt["move"], o[t_key]))[0]
        assert metres(o[ll_key], want) < tol                                     # where the hub's model has it
    assert metres(o["at0"], coords[0]) > 30 and metres(o["at1"], o["at0"]) > 3   # set off, and moving
    assert "est" in o["cls"] and "enroute" in o["cls"] and o["st"] == "~RS"
    assert "Estimated from the route and status times, not a real position" in o["title"]
    assert o["route0"][0] == pytest.approx(o["at0"]) and o["route0"][-1] == pytest.approx(coords[-1])   # ahead only
    assert len(o["route0"]) < len(coords) + 1 and o["lines"] == 0                  # no straight line
    assert o["route1"] == pytest.approx(o["at1"], abs=2e-4)
    assert o["rot0"] is not None and o["rot1"] is not None
    assert 2 <= o["ticks"] <= 9                                   # 4-8 Hz (by how far it moves on screen), never 60
    assert o["same"] == [True, True, True]                                       # no rebuild on the frames


@needs_node
def test_the_page_holds_it_short_then_glides_it_in_on_location(tmp_path):
    c, clock, uid, data = page_state()
    rt = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    held = dict(rt, move=dict(rt["move"], t0=rt["move"]["t0"] - 3600, done_at=rt["move"]["done_at"] - 3600))
    data["map"]["routes"] = [held if r is rt else r for r in data["map"]["routes"]]
    c.unit_status(uid("E61"), {"status": "OL", **OP})
    data["ol"] = c.state()["map"]
    o = run(tmp_path, PRE + r"""
out.held = ll("E61"); out.moving = L.moving();
setMap(IN.ol);
run(1, 16); out.just = ll("E61"); out.cls = cls("E61");
run(100, 16); out.there = ll("E61"); out.off = chip("E61").marker.off; out.settle = chip("E61").settle;
""", data)
    coords, cum = rt["coords"], _cum(rt["coords"])
    assert metres(o["held"], point_at(coords, cum, rt["move"]["hold_m"])[0]) < 1 and metres(o["held"], coords[-1]) > 5
    assert o["moving"] is False                                                  # held: no frames asked for
    assert metres(o["just"], o["held"]) < 15                                     # no jump on location: a glide
    assert "est" not in o["cls"] and "at-incident" in o["cls"]
    assert o["there"] == pytest.approx([data["inc"]["lon"], data["inc"]["lat"]]) and o["settle"] is None


@needs_node
def test_the_page_brings_it_home_along_the_route_then_folds_it(tmp_path):
    c, clock, uid, data = page_state()
    c.unit_status(uid("E61"), {"status": "OL", **OP})
    clock.t -= 10
    c.unit_status(uid("E61"), {"status": "AV", **OP})
    clock.t += 10
    data["map"] = c.state()["map"]
    back = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    clock.t = back["move"]["done_at"] + 1
    home = c.state()["map"]
    clock.t = time.time()
    data["home"] = home
    o = run(tmp_path, PRE + r"""
zoomTo(16);
out.t = nowS(); out.at = ll("E61"); out.cls = cls("E61"); out.route = routeOf("E61");
zoomTo(13); out.dotRoute = !!routeOf("E61");
zoomTo(16);
setMap(IN.home); run(100, 16);
zoomTo(13);
out.kind = ["E60", "E61", "E62"].map((u) => cls(u).includes("folded"));
out.badge = L.badges.has(IN.hq.name);
""", data)
    assert back["kind"] == "back"
    want = point_at(back["coords"], _cum(back["coords"]), along_at(back["move"], o["t"]))[0]
    assert metres(o["at"], want) < 1.5 and metres(o["at"], back["coords"][0]) > 20
    assert "est" in o["cls"] and "at-returning" in o["cls"] and "enroute" not in o["cls"]
    assert o["route"]["properties"]["home"] is True and o["route"]["geometry"]["coordinates"][-1] == pytest.approx(back["coords"][-1])
    assert o["dotRoute"] is False                                                # a dot's way home not drawn (#376)
    assert o["kind"] == [True, True, True] and o["badge"]                         # home: folded with the others


@needs_node
def test_a_live_fix_replaces_the_estimate_at_once_and_a_placeholder_is_never_drawn(tmp_path):
    *_, data = page_state()
    e61 = next(m for m in data["map"]["markers"] if m["unit"] == "E61")
    fixed = dict(e61, place="position", lon=-73.6672, lat=41.0030, at=time.time() - 1, heading=90.0, speed=0.0)
    ph = dict(e61, unit="DPT239", placeholder=True, icon="")
    data["fixed"] = dict(data["map"], markers=[fixed if m is e61 else m for m in data["map"]["markers"]] + [ph])
    data["map"]["markers"].append(ph)
    data["map"]["routes"].append(dict(next(r for r in data["map"]["routes"] if r["unit"] == "E61"), unit="DPT239"))
    o = run(tmp_path, PRE + r"""
out.ph = L.chips.has("DPT239");
setMap(IN.fixed);
out.at = ll("E61"); out.cls = cls("E61"); out.st = chip("E61").parts.st.textContent; out.ph2 = L.chips.has("DPT239");
""", data)
    assert o["ph"] is False and o["ph2"] is False
    assert o["at"] == pytest.approx([-73.6672, 41.0030]) and "est" not in o["cls"] and o["st"] == "RS"


@needs_node
def test_the_straight_line_fallback_moves_it_too_and_hidden_tabs_ask_for_no_frames(tmp_path):
    *_, data = page_state()
    rt = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    a, b = rt["coords"][0], rt["coords"][-1]
    d = metres(a, b)
    line = dict(rt, by="line", coords=[a, b], move=make_move(time.time() - 3, d, d / rt["move"]["mps"]))
    data["map"]["routes"] = [line if r is rt else r for r in data["map"]["routes"]]
    o = run(tmp_path, PRE + r"""
out.cls = cls("E61"); out.a = ll("E61"); run(30, 16); out.b = ll("E61"); out.moving = L.moving();
document.visibilityState = "hidden"; frames.splice(0); for (const f of docOn.visibilitychange || []) f();
let n = 0; const t = L.tick.bind(L); L.tick = () => { n++; t(); };
run(30, 16); out.hidden = n;
""", data)
    assert "est" in o["cls"] and o["moving"] is True and metres(o["a"], o["b"]) > 1
    # on the straight line
    k = (o["b"][0] - a[0]) / (b[0] - a[0])
    assert o["b"][1] == pytest.approx(a[1] + (b[1] - a[1]) * k, abs=2e-6)
    assert o["hidden"] == 0
