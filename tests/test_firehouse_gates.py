"""Rigs leave and come back into their firehouse by the bay doors, not across the lot.

At the South End firehouse the routes left and came in through the parking lot and the side street to the north: the
router snapped the firehouse's point to the nearest way, a service road in the lot. The rigs really pull out of the
bay doors on the building's west face straight onto Grace Church St, and back in the same way.

  the data     data/firehouses.json egress / ingress {lat, lon, street}: the bay-door apron and the street it joins
  the graph    sizeup/roadgraph.py snap_gate(): the named street, else the nearest public street (never a service
               road while a street is within GATE_SNAP_M), else the nearest road; route() / to_one() from the apron
  the routes   sizeup/cad/routes.py gate_of(): a rig in quarters leaves by its egress, a trip home ends at its
               ingress, the recommendation's estimates start there too; a route already made (or kept) stays

Synthetic street grids (made-up names) for the rules, and a small real extract: OpenStreetMap's drivable ways within
about 450 m of the South End firehouse (tests/fixtures/osm/portchester_south_end.json, clipped from an Overpass answer
on 2026-11-12; © OpenStreetMap contributors, ODbL). Firehouse points and street names are public (data/firehouses.json).
No incident data: every call is a made-up point."""

import json
from pathlib import Path

import pytest

from sizeup import roadgraph as rg
from sizeup.cad import eta, mapview
from sizeup.cad.routes import GATE_M, Routes, street_of
from sizeup.roadgraph import Graph, build

from tests.test_rig_routes import NOW, Ways, at

ROOT = Path(__file__).resolve().parent.parent
SOUTH_END_OSM = ROOT / "tests" / "fixtures" / "osm" / "portchester_south_end.json"
P = eta.Params()
FIREHOUSES = mapview.load_firehouses()
SOUTH = next(f for f in FIREHOUSES if f.get("short") == "South End")
APRON = [SOUTH["egress"]["lon"], SOUTH["egress"]["lat"]]
CALLS = {                                                     # made-up points on the streets around it
    "north": [-73.6654, 40.9975],                            # Grace Church St, north of Midland Ave
    "south": [-73.6651, 40.9935],                            # Grace Church St, south
    "west": [-73.6681, 40.9935],                             # Midland Ave
    "uptown": [-73.6640, 40.9974],                           # Don Bosco Pl
}


def south_end_graph():
    return Graph(build(json.loads(SOUTH_END_OSM.read_text())))


def way_names(g, r):
    return [g.name(e) for e, _ in r["way"]]


def service_edges(g, r):
    return [e for e, _ in r["way"] if g.cls(e) == "service"]


# ---- the data ---------------------------------------------------------------------------------------------------------


def test_the_south_end_egress_is_on_grace_church_st_at_the_west_face():
    eg = SOUTH["egress"]
    assert eg["street"] == "Grace Church St" and "ingress" not in SOUTH           # in the same way it goes out
    assert eg["lon"] < SOUTH["ll"][0] - 0.0002                                     # west of the building's point
    assert abs(eg["lat"] - SOUTH["ll"][1]) < 0.0002                                # level with it, not to the north
    assert eta.haversine_km(APRON, SOUTH["ll"]) * 1000 < GATE_M


def test_load_firehouses_keeps_good_gates_and_leaves_bad_ones_out(tmp_path):
    p = tmp_path / "fh.json"
    p.write_text(json.dumps({"firehouses": [
        {"name": "A", "address": "1 Example St", "ll": [-73.7, 41.0],
         "egress": {"lat": 41.0001, "lon": -73.7001, "street": "Example St"},
         "ingress": {"lat": "x", "lon": -73.7}},
        {"name": "B", "ll": [-73.7, 41.0], "egress": [1, 2]},
        {"name": "C", "ll": [-73.7, 41.0], "egress": {"lat": 41.0, "lon": -73.7}},
    ]}))
    a, b, c = mapview.load_firehouses(p)
    assert a["egress"] == {"lat": 41.0001, "lon": -73.7001, "street": "Example St"} and "ingress" not in a
    assert "egress" not in b and c["egress"]["street"] == ""


def test_the_street_of_a_firehouse_address():
    assert street_of("51-53 Grace Church St") == "Grace Church St"
    assert street_of("209 Westchester Ave") == "Westchester Ave"
    assert street_of("940 King St, Rye Brook") == "King St"
    assert street_of("") == "" and street_of(None) == "" and street_of("12") == ""


# ---- the graph: snap_gate -----------------------------------------------------------------------------------------------


def lot():
    """Main Street (j=0) and Side Street (j=1.2), joined by Cross Street (i=0); a lot's service road (unnamed, no
    service tag) right beside the firehouse at (2, 0.5), nearer than either street, out onto Side Street."""
    w = Ways()
    w.add([at(i, 0) for i in range(5)], name="Main Street")
    w.add([at(i, 1.2) for i in range(5)], name="Side Street")
    w.add([at(0, 0), at(0, 1.2)], name="Cross Street")
    w.add([at(1, 0.65), at(3, 0.65), at(3, 1.2)], hw="service")                   # ~13 m from the firehouse
    w.add([at(2, 0.4), at(2.2, 0.4)], hw="service", service="parking_aisle")      # never in the graph at all
    return w.graph()


FH = at(2, 0.5)                                                                   # 44 m from Main St, 62 m from Side St


def test_snap_gate_takes_the_named_street_over_a_nearer_lot_or_street():
    g = lot()
    assert g.cls(g.snap(*FH)["edge"]) == "service"                                # the old snap: the lot
    s = g.snap_gate(*FH, "Main St")
    assert (g.name(s["edge"]), s["by"]) == ("Main Street", "street") and s["lat"] == pytest.approx(at(0, 0)[1])
    s = g.snap_gate(*FH, "SIDE STREET")                                           # normalised: Street / St, case
    assert (g.name(s["edge"]), s["by"]) == ("Side Street", "street")


def test_snap_gate_without_its_street_takes_the_nearest_public_street_never_the_lot():
    g = lot()
    for street in ("", "Elsewhere Ave"):
        s = g.snap_gate(*FH, street)
        assert (g.name(s["edge"]), s["by"]) == ("Main Street", "public")
    s = g.snap_gate(FH[0], FH[1] + 0.0003, "")                                    # nearer Side St now
    assert (g.name(s["edge"]), s["by"]) == ("Side Street", "public")


def test_snap_gate_falls_back_to_the_nearest_road_with_no_street_within_reach():
    g = lot()
    s = g.snap_gate(*FH, "Main St", max_m=30.0)                                   # no street within 30 m: the lot
    assert s["by"] == "nearest" and g.cls(s["edge"]) == "service"
    assert g.snap_gate(-73.0, 41.5, "Main St") is None                            # no road near at all


def test_a_route_from_a_gate_starts_at_its_apron_and_counts_the_stretch():
    g = lot()
    to = at(4, 0)
    plain = g.route(FH, to, P)
    gated = g.route(FH, to, P, a_gate="Main St")
    assert plain["coords"][0][1] == pytest.approx(at(0, 0.65)[1])                 # the old snap: out by the lot
    assert any(p[1] > FH[1] for p in plain["coords"]) and not any(p[1] > FH[1] for p in gated["coords"])
    assert not service_edges(g, gated) and gated["m"] < plain["m"]
    assert gated["coords"][0] == pytest.approx(FH) and gated["coords"][1][1] == pytest.approx(at(0, 0)[1])
    assert gated["m"] == pytest.approx(rg._metres(gated["coords"]), rel=1e-6)
    home = g.route(to, FH, P, wrongway_m=0, b_gate="Main St")                     # home: in by the same gate
    assert home["coords"][-1] == pytest.approx(FH) and not service_edges(g, home)
    many = g.to_one({"FH": FH}, to, P, gates={"FH": "Main St"})
    assert many["FH"] == pytest.approx((gated["s"], gated["m"]), rel=1e-6)


# ---- the South End, on the real extract ---------------------------------------------------------------------------------


def test_the_south_end_leaves_by_grace_church_st_never_the_lot():
    g = south_end_graph()
    assert g.cls(g.snap(*SOUTH["ll"])["edge"]) == "service"                       # the old snap: the lot to the north
    s = g.snap_gate(*APRON, SOUTH["egress"]["street"])
    assert s["by"] == "street" and g.name(s["edge"]).startswith("Grace Church Street") and s["d"] < 12
    for name, to in CALLS.items():
        old = g.route(SOUTH["ll"], to, P)
        new = g.route(APRON, to, P, a_gate="Grace Church St")
        assert service_edges(g, old) and not service_edges(g, new), name
        assert new["coords"][0] == pytest.approx(APRON) and new["m"] < old["m"], name
        first = next(n for n in way_names(g, new) if n) if new["way"] else g.name(s["edge"])
        assert first.split(";")[0] in ("Grace Church Street", "Don Bosco Place", "Midland Avenue"), name


def test_the_south_end_comes_home_by_grace_church_st_too():
    g = south_end_graph()
    for name, frm in CALLS.items():
        home = g.route(frm, APRON, P, wrongway_m=0, b_gate="Grace Church St")
        assert home["coords"][-1] == pytest.approx(APRON) and not service_edges(g, home), name
        assert "Beech Street" not in way_names(g, home), name


# ---- the routes: the map, the estimates, a restart ---------------------------------------------------------------------


def routes_on(g, firehouses=lambda: FIREHOUSES, persist=None):
    return Routes(lambda: g, lambda: P, lambda: [], firehouses, log=lambda m: None, spawn=lambda fn: fn(),
                  persist=persist)


def going(to, look="responding", iid=7, **kw):
    """E63 at the South End (placed by status) to a made-up call."""
    ll = SOUTH["ll"]
    return {"unit": "E63", "incident_id": iid, "incident_number": f"26-00000{iid}", "look": look, "place": "station",
            "group": f"station:{SOUTH['name']}", "lon": ll[0], "lat": ll[1], "line_to": to, "station": SOUTH["name"],
            "status": "RS" if look == "responding" else "DP", "status_at": NOW, "ring": 0, **kw}


def one(r, m, t, kind="to"):
    rs = [x for x in r.for_map([m], t)["routes"] if x.get("kind") == kind]
    return rs[0] if rs else None


def test_gate_of_finds_the_firehouse_by_its_point_or_its_apron():
    r = routes_on(south_end_graph())
    assert r.gate_of(SOUTH["ll"]) == (APRON, "Grace Church St")
    assert r.gate_of(APRON, "ingress") == (APRON, "Grace Church St")              # ingress: the egress unless given
    assert r.gate_of([SOUTH["ll"][0], SOUTH["ll"][1] + 0.002]) is None            # ~220 m north: not in quarters
    hq = next(f for f in FIREHOUSES if f.get("short") == "HQ")
    assert r.gate_of(hq["ll"]) == (hq["ll"], "Westchester Ave")                   # none given: its address's street


def test_the_map_route_and_the_run_card_estimate_agree_from_the_gate():
    g = south_end_graph()
    r = routes_on(g)
    to = CALLS["south"]
    rt = one(r, going(to), NOW + 1)
    assert rt["by"] == "road" and rt["coords"][0] == pytest.approx(APRON)
    assert not [p for p in rt["coords"] if p[1] > SOUTH["ll"][1] + 0.0002]         # never north into the lot
    est = r.to_one({"E63": SOUTH["ll"]}, to, quarters={"E63": SOUTH["name"]})
    assert est["E63"][0] == pytest.approx(rt["s"], abs=0.6) and est["E63"][1] == pytest.approx(rt["m"], abs=2)
    assert r.to_one({"X": [-73.6681, 40.9935]}, to)["X"] is not None             # away from a firehouse: as before


def test_the_trip_home_ends_at_the_apron_and_the_recommendation_follows():
    g = south_end_graph()
    r = routes_on(g)
    to = CALLS["west"]
    unit = {"name": "E63", "status": "AV", "status_at": NOW}
    clear = {"incident_id": 5, "number": "26-000005", "lat": to[1], "lon": to[0], "cleared_at": NOW}
    m = mapview.place(unit, "free", SOUTH, None, None, NOW + 10, fire=True, clear=clear, params=P)
    back = one(r, m, NOW + 10, "back")
    assert back["coords"][-2] == pytest.approx(APRON) and back["coords"][0] == pytest.approx(to, abs=1e-4)
    assert back["coords"][-1] == pytest.approx(SOUTH["ll"])                        # then into the bays: no jump at the end
    assert not [p for p in back["coords"] if p[1] > SOUTH["ll"][1] + 0.0002]
    done = r.trips["E63"]["move"]["done_at"]
    assert r.trip_at("E63", done + 1) == {"home": True, "ll": SOUTH["ll"]}


def test_a_kept_route_from_the_old_snap_goes_on_and_only_a_new_one_uses_the_gate():
    """A route (and its move) kept in the CAD's meta (routes.motion) from before the gates: read back as it was, never
    routed again under a rig on its way; a new call routes from the apron."""
    g = south_end_graph()
    kept = {}
    persist = (lambda: kept.get("v"), lambda v: kept.__setitem__("v", v))
    old = routes_on(g, firehouses=lambda: [], persist=persist)                     # no gates: the old snap, the lot
    to = CALLS["north"]
    before = one(old, going(to), NOW + 20)
    assert before["coords"][0] != pytest.approx(APRON) and before["move"]
    assert any(p[1] > SOUTH["ll"][1] + 0.0002 for p in before["coords"])          # out through the lot to the north
    assert json.loads(kept["v"])["moves"]
    new = routes_on(g, persist=persist)                                           # a restart with the gates
    after = one(new, going(to), NOW + 30)
    assert after["coords"] == before["coords"] and after["move"] == before["move"] and new.stats["routed"] == 0
    assert after["along_m"] > before["along_m"]                                   # goes on along it, no jump
    other = one(new, going(CALLS["south"], iid=8, unit="E64"), NOW + 30)          # a new route: from the apron
    assert other["coords"][0] == pytest.approx(APRON)


def test_the_console_recommendation_and_the_map_agree_from_the_south_end():
    """Through the CAD: a rig in quarters at the South End is estimated from its apron (the run card's "~N min · mi"),
    its route on the map starts there with the same drive, and home again it is in quarters for the next card."""
    from sizeup.cad.service import Cad, Settings
    from tests.test_proximity import Clock
    clock = Clock(NOW)
    c = Cad(Settings(), log=lambda m: None, clock=clock)
    g = south_end_graph()
    c.routes = Routes(lambda: g, lambda: c.eta, lambda: [], lambda: c.firehouses, clock=c.clock, log=lambda m: None)
    op = {"operator": "Lt. Example"}
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    c.unit_station(uid("E61"), {"station": SOUTH["name"], **op})
    to = CALLS["south"]
    iid = c.create({"type": "STRU", "address": "100 Example Ave", "lat": to[1], "lon": to[0], **op})["id"]
    rec = next(u for u in c.recommend(iid, {})["units"] if u["name"] == "E61")
    assert rec["how"] == "road"
    c.attach(iid, {"unit_ids": [uid("E61")], **op})
    c.unit_status(uid("E61"), {"status": "RS", **op})
    rt = next(x for x in c.state()["map"]["routes"] if x["unit"] == "E61")
    assert rt["coords"][0] == pytest.approx(APRON)
    assert rec["eta_s"] == pytest.approx(rt["s"] + c.eta.turnout_min * 60, abs=1)   # the card and the map: one drive
    assert eta.miles(rt["m"]) in rec["reason"]
    clock.t += 3600
    c.unit_status(uid("E61"), {"status": "OL", **op})
    clock.t += 600
    c.unit_status(uid("E61"), {"status": "AV", **op})
    clock.t += 5
    back = next(x for x in c.state()["map"]["routes"] if x["unit"] == "E61")
    assert back["kind"] == "back" and back["coords"][-2] == pytest.approx(APRON)
    assert back["coords"][-1] == pytest.approx(SOUTH["ll"])
    assert c.locations()["E61"]["where"] == "returning"
    clock.t = back["move"]["done_at"] + 1
    loc = c.locations()["E61"]
    assert loc["where"] == "station" and loc["ll"] == SOUTH["ll"]


# ---- only a rig in quarters goes by the gate ---------------------------------------------------------------------------

NEAR = {                                                      # made-up points on streets just off the South End
    "Central Ave": [-73.66453, 40.99565],                    # ~39 m south of its point
    "Don Bosco Pl": [-73.66482, 40.99630],
    "Midland Ave": [-73.66500, 40.99613],
}


def test_a_rig_near_but_not_in_quarters_is_routed_from_where_it_is():
    """On location at a call beside the firehouse, or driving by: its route starts where it is and its estimate is
    the plain one, whatever firehouse is near. Only its own station, by status or a live fix there, is quarters."""
    g = south_end_graph()
    to = CALLS["west"]
    for name, ll in NEAR.items():
        r = routes_on(g)
        assert r.to_one({"X": ll}, to)["X"] == pytest.approx(g.to_one({"X": ll}, to, P)["X"]), name   # on the road
        hq = next(f for f in FIREHOUSES if f.get("short") == "HQ")
        m = going(to, place="position", lon=ll[0], lat=ll[1], at=NOW, station=hq["name"])        # an HQ rig going by
        rt = one(r, m, NOW + 1)
        plain = g.route(ll, to, P)
        assert rt["coords"][0] == pytest.approx(plain["coords"][0]) and rt["coords"][0] != pytest.approx(APRON), name
        assert rt["s"] == round(plain["s"]), name
    own = going(to, place="position", lon=NEAR["Central Ave"][0], lat=NEAR["Central Ave"][1], at=NOW)
    assert one(routes_on(g), own, NOW + 1)["coords"][0] == pytest.approx(APRON)    # its own station, a live fix there


def test_the_card_gates_only_the_rigs_it_says_are_in_quarters():
    from sizeup.cad import runcards
    g = south_end_graph()
    r = routes_on(g)
    to = CALLS["south"]
    where = {"E63": {"ll": SOUTH["ll"], "on_road": False, "label": "from South End", "station": SOUTH["name"]},
             "E64": {"ll": NEAR["Central Ave"], "on_road": True, "label": "at 26-000001", "station": SOUTH["name"]}}
    units = [{"id": 1, "name": "E63"}, {"id": 2, "name": "E64"}]
    est = runcards.estimates(units, where, {"lat": to[1], "lon": to[0]}, P, r.to_one)
    gated = g.route(APRON, to, P, a_gate="Grace Church St")
    plain = g.route(NEAR["Central Ave"], to, P)
    assert est[1]["eta_s"] == round(gated["s"] + P.turnout_min * 60)
    assert est[2]["eta_s"] == round(g.to_one({"E64": NEAR["Central Ave"]}, to, P)["E64"][0])
    assert est[2]["eta_s"] == pytest.approx(plain["s"], abs=1)


def test_a_route_worked_out_again_on_the_apron_goes_out_the_same_gate_rolling_on():
    """The call moves in the first seconds of the move, while the rig is still on the apron: routed again from the
    gate (never a plain snap from the apron, which could find a lot), from where it was on it (no jump back)."""
    g = south_end_graph()
    r = routes_on(g)
    first = one(r, going(CALLS["south"]), NOW + 1)
    lead = r.cache[("E63", 7)]["gate"][2]
    assert first["coords"][0] == pytest.approx(APRON) and lead > 5
    t = NOW + 1.5                                                                  # a metre or two out, still on the apron
    was = one(r, going(CALLS["south"]), t)["along_m"]
    assert 0 < was < lead
    again = one(r, going(CALLS["north"]), t)
    assert r.stats["why"].get("new", 0) >= 2
    assert again["coords"][0] == pytest.approx(APRON) and again["along_m"] == pytest.approx(was, abs=1)
    assert again["move"]["ease_s"] == 0                                            # rolling on
    late = one(r, going(CALLS["west"]), NOW + 30)                                  # well out on the street: from there
    assert late["coords"][0] != pytest.approx(APRON) and late["along_m"] == 0


def test_a_rig_on_a_parkway_by_a_firehouse_keeps_the_parkway_rule():
    """A rig back from a call, on the Hutch, 30 m from a firehouse that is not its own: not in quarters, so it drives
    on along the parkway to an exit as before (#396), on the map and in the estimate."""
    from tests.test_rig_routes import DX, DY, LAT0, LON0, freeway, limited_in
    g = freeway(parkway=True)
    fhs = [{"name": "HQ", "address": "1 Long Road", "ll": at(0, 0)},
           {"name": "Parkside", "address": "5 Long Road", "ll": at(5, -0.65)}]
    r = routes_on(g, firehouses=lambda: fhs)
    on_hutch = at(5, -1)
    to = [LON0 + 20 * DX, LAT0 + 0.5 * DY]
    m = going(to, place="position", lon=on_hutch[0], lat=on_hutch[1], at=NOW, station="HQ")
    rt = one(r, m, NOW + 1)
    plain = g.route(on_hutch, to, P)
    assert limited_in(g, plain) == {2} and rt["coords"] == plain["coords"]
    assert r.to_one({"E60": on_hutch}, to, quarters={})["E60"] == pytest.approx(g.to_one({"E60": on_hutch}, to, P)["E60"])


# ---- Rye Brook: out onto King St, back in through the PD lot ------------------------------------------------------------
# The user: the rigs leave onto King St and come back through the PD parking lot, pulling in straight (drive-through
# bays). Its ingress is "via": "lot": the trip home joins the lot's drive aisles (OSM service ways, entered from King
# St between the firehouse and Village Hall), not a street. Real extract: tests/fixtures/osm/ryebrook_firehouse.json.

RB_OSM = ROOT / "tests" / "fixtures" / "osm" / "ryebrook_firehouse.json"
RB = next(f for f in FIREHOUSES if f.get("short") == "Rye Brook")
RB_OUT = [RB["egress"]["lon"], RB["egress"]["lat"]]
RB_IN = [RB["ingress"]["lon"], RB["ingress"]["lat"]]
RB_CALLS = {"north": [-73.6760, 41.0370], "south": [-73.6722, 41.0330], "west": [-73.6780, 41.0335],
            "east": [-73.6710, 41.0370]}                         # made-up points on the streets around it


def rb_graph():
    return Graph(build(json.loads(RB_OSM.read_text())))


def rb_going(to, **kw):
    ll = RB["ll"]
    return dict(going(to, **kw), unit="E70", group=f"station:{RB['name']}", lon=ll[0], lat=ll[1], station=RB["name"])


def test_rye_brook_has_an_egress_onto_king_st_and_an_ingress_through_the_lot():
    assert RB["egress"]["street"] == "King St" and "via" not in RB["egress"]
    assert RB["ingress"]["via"] == "lot"
    r = routes_on(rb_graph())
    assert r.gate(RB) == (RB_OUT, "King St") and r.gate(RB, "ingress") == (RB_IN, rg.LOT)
    assert r.gate(SOUTH, "ingress") == r.gate(SOUTH) == (APRON, "Grace Church St")   # no ingress: the egress


def test_rye_brook_trip_home_goes_through_the_lot_and_ends_at_the_bays():
    g = rb_graph()
    for name, to in RB_CALLS.items():
        r = routes_on(g)
        unit = {"name": "E70", "status": "AV", "status_at": NOW}
        clear = {"incident_id": 5, "number": "26-000005", "lat": to[1], "lon": to[0], "cleared_at": NOW}
        m = mapview.place(unit, "free", RB, None, None, NOW + 10, fire=True, clear=clear, params=P)
        back = one(r, m, NOW + 10, "back")
        assert back["coords"][-1] == pytest.approx(RB["ll"]) and back["coords"][-2] == pytest.approx(RB_IN), name
        way = [e for e, _ in g.route(to, RB_IN, P, wrongway_m=0, b_gate=rg.LOT)["way"]]
        assert g.cls(way[-1]) == "service" and any("King Street" in g.name(e) for e in way), name   # King St, the lot
        # in from the lot's King St entrance, between the firehouse and Village Hall, never by the King St apron
        assert any(eta.haversine_km(p, [-73.674113, 41.035167]) * 1000 < 3 for p in back["coords"]), name
        assert not any(eta.haversine_km(p, RB_OUT) * 1000 < 3 for p in back["coords"]), name
    plain = routes_on(g, firehouses=lambda: [dict(RB, ingress=None)])                    # before: in the way it goes out
    unit = {"name": "E70", "status": "AV", "status_at": NOW}
    clear = {"incident_id": 5, "number": "26-000005", "lat": RB_CALLS["north"][1], "lon": RB_CALLS["north"][0],
             "cleared_at": NOW}
    m = mapview.place(unit, "free", RB, None, None, NOW + 10, fire=True, clear=clear, params=P)
    assert one(plain, m, NOW + 10, "back")["coords"][-2] == pytest.approx(RB_OUT)


def test_rye_brook_leaves_onto_king_st_and_its_run_card_ignores_the_ingress():
    g = rb_graph()
    to = RB_CALLS["south"]
    r = routes_on(g)
    rt = one(r, rb_going(to), NOW + 1)
    assert rt["coords"][0] == pytest.approx(RB_OUT)
    first = g.route(RB_OUT, to, P, a_gate="King St")["way"]
    assert not [e for e, _ in first if g.cls(e) == "service"]
    est = r.to_one({"E70": RB["ll"]}, to, quarters={"E70": RB["name"]})
    assert est["E70"][0] == pytest.approx(rt["s"], abs=0.6)
    no_in = routes_on(g, firehouses=lambda: [dict(RB, ingress=None)])
    assert no_in.to_one({"E70": RB["ll"]}, to, quarters={"E70": RB["name"]}) == est     # the ingress: home only


def test_a_lot_ingress_is_never_used_on_the_way_out():
    f = {"name": "X", "address": "1 Example St", "ll": [-73.7, 41.0],
         "egress": {"lat": 41.0, "lon": -73.7, "street": "Example St", "via": "lot"}}
    assert Routes.gate(f) == ([-73.7, 41.0], "Example St")
    assert Routes.gate(f, "ingress") == ([-73.7, 41.0], "Example St")                # an egress's "via": ignored
