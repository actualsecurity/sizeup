"""The probable path from each responding rig to its call, re-routed as it moves (GitHub #360).

The user: "can we also show the path from rig to the call, and what path they might be following, and have it update
as they go different directions? just to show the probable path".

  the graph     sizeup/roadgraph.py: OSM ways (an Overpass answer) -> a compact graph -> A* and a reverse Dijkstra
  the routes    sizeup/cad/routes.py: per (unit, incident), re-routed off the path or heading away; trails
  the estimate  sizeup/cad/runcards.py estimates(): by road where there is a way, else the straight-line model

Synthetic street grids (made-up names and points) for the rules, and one small real extract: OpenStreetMap's ways in
about a square kilometre of downtown Port Chester (tests/fixtures/osm/portchester_downtown.json, clipped from an
Overpass answer on 2026-11-09; © OpenStreetMap contributors, ODbL). No incident data anywhere: the calls are points."""

import json
import math
import time
from pathlib import Path

import pytest

from sizeup import roadgraph as rg
from sizeup.cad import eta, runcards
from sizeup.cad.routes import AWAY_FIXES, Routes, _project
from sizeup.roadgraph import Graph, build

ROOT = Path(__file__).resolve().parent.parent
OSM = ROOT / "tests" / "fixtures" / "osm" / "portchester_downtown.json"
NOW = 1_791_400_000.0
LON0, LAT0, DX, DY = -73.70, 41.00, 0.001, 0.0008          # a grid step: ~84 m east, ~88 m north
P = eta.Params()


def at(i, j):
    return [round(LON0 + i * DX, 7), round(LAT0 + j * DY, 7)]


class Ways:
    """An Overpass-shaped answer from grid lines (node ids from the points, so crossings join)."""

    def __init__(self):
        self.els, self.ids, self.n = [], {}, 0

    def nid(self, p):
        k = (round(p[0], 7), round(p[1], 7))
        if k not in self.ids:
            self.ids[k] = 1000 + len(self.ids)
        return self.ids[k]

    def add(self, pts, hw="residential", **tags):
        self.n += 1
        self.els.append({"type": "way", "id": self.n, "nodes": [self.nid(p) for p in pts],
                         "geometry": [{"lon": p[0], "lat": p[1]} for p in pts], "tags": {"highway": hw, **tags}})
        return self

    def graph(self):
        return Graph(build({"elements": self.els}))


def ladder():
    """Main St (two-way, j=0) and Oak St (one-way east, j=1), i = 0..4, joined by two-way cross streets at 0 and 4."""
    w = Ways()
    w.add([at(i, 0) for i in range(5)], name="Main Street")
    w.add([at(i, 1) for i in range(5)], name="Oak Street", oneway="yes")
    w.add([at(0, 0), at(0, 1)], name="West Street")
    w.add([at(4, 0), at(4, 1)], name="East Street")
    return w


def names(g, r):
    return [g.name(e) for e, _ in r["way"]]


# ---- the graph --------------------------------------------------------------------------------------------------------


def test_build_splits_at_junctions_and_keeps_one_way_and_class():
    w = ladder()
    w.add([at(2, 0), at(2, -1)], hw="service", service="driveway")         # a driveway: not a road for this
    w.add([at(5, 1), at(5, 0)], name="Back Lane", oneway="-1")             # one-way against its drawing
    doc = build({"elements": w.els})
    g = Graph(doc)
    assert doc["v"] == rg.VERSION and len(doc["nodes"]) // 2 == g.n
    main = [e for e in range(g.m) if g.name(e) == "Main Street"]
    assert len(main) == 1 and g.elen[main[0]] == pytest.approx(4 * 84, rel=0.02)   # no junction inside: one edge
    oak = [e for e in range(g.m) if g.name(e) == "Oak Street"]
    assert [g.eow[e] for e in oak] == [1] and g.cls(oak[0]) == "residential"
    assert not any(g.cls(e) == "service" for e in range(g.m))
    back = next(e for e in range(g.m) if g.name(e) == "Back Lane")
    assert (g.lon[g.ea[back]], g.lat[g.ea[back]]) == pytest.approx(tuple(at(5, 0)))   # normalised: a -> b is the way
    assert rg.oneway_of({"junction": "roundabout"}, "residential") == 1 and rg.oneway_of({}, "motorway") == 1


def test_one_ways_are_honoured():
    g = ladder().graph()
    with_flow = g.route(at(1, 1), at(3, 1), P, wrongway_m=0)
    assert with_flow["m"] == pytest.approx(2 * 84, rel=0.03)
    against = g.route(at(3, 1), at(1, 1), P, wrongway_m=0)                 # never west on Oak St: around by Main St
    assert "Main Street" in names(g, against) and against["m"] > 600
    for e, d in against["way"]:
        assert not (d == 1 and g.eow[e]), g.name(e)


def test_a_short_wrong_way_final_stretch_saves_a_loop():
    g = ladder().graph()
    to = [LON0 + 3.6 * DX, LAT0 + DY]                                       # on Oak St, ~34 m short of East St
    short = g.route(at(4, 0), to, P, wrongway_m=60)
    assert short["m"] == pytest.approx(88 + 34, abs=6) and names(g, short)[-1] == "Oak Street"
    loop = g.route(at(4, 0), to, P, wrongway_m=0)
    assert loop["m"] > 650 and "West Street" in names(g, loop)
    assert short["coords"][0] == pytest.approx(at(4, 0)) and short["coords"][-1] == pytest.approx(to, abs=1e-6)


def two_ways():
    """Main St straight from A to B, a longer way round by Hill Rd."""
    w = Ways()
    w.add([at(0, 0), at(1, 0), at(2, 0), at(3, 0)], name="Main Street", hw="secondary")
    w.add([at(0, 0), at(0, 3), at(3, 3), at(3, 0)], name="Hill Road")
    return w.graph()


def closure(how="full", start=NOW - 3600, end=NOW + 3600, road="Main St"):
    """A roadprojects record (the shape every source is normalised to), made up."""
    return {"id": "test:1", "source": "Port Chester", "kind": "closure", "title": "Main St closed", "road": road,
            "closure": how, "geometry": {"type": "LineString", "coordinates": [at(1.2, 0), at(1.8, 0)]},
            "start": start, "end": end, "hours": [], "lat": LAT0, "lon": LON0 + 1.5 * DX}


def test_a_closure_in_effect_now_is_avoided():
    g = two_ways()
    open_ = g.route(at(0, 0), at(3, 0), P)
    assert names(g, open_) == ["Main Street"] or open_["m"] < 300
    pen = g.penalties([closure()], NOW)
    main = next(e for e in range(g.m) if g.name(e) == "Main Street")
    assert pen == {main: math.inf}
    closed = g.route(at(0, 0), at(3, 0), P, pen)
    assert "Hill Road" in names(g, closed) and closed["m"] > 700
    # not in effect: ended, or not started yet; a lane closure only slows it; another road's closure: nothing here
    assert g.penalties([closure(end=NOW - 60)], NOW) == {}
    assert g.penalties([closure(start=NOW + 3600)], NOW) == {}
    assert g.penalties([closure(how="lanes")], NOW) == {main: 1.5}
    assert g.penalties([closure(road="Hill Road")], NOW) == {}
    assert g.penalties([dict(closure(), start=None)], NOW) == {}            # not dated: nothing says it is now


def test_the_call_and_the_rig_are_reached_even_on_a_closed_block():
    g = two_ways()
    pen = g.penalties([closure()], NOW)
    r = g.route(at(0.5, 0), [LON0 + 1.5 * DX, LAT0], P, pen)                # the call is on the closed block
    assert r is not None and r["coords"][-1] == pytest.approx([LON0 + 1.5 * DX, LAT0], abs=1e-6)


def test_no_road_near_a_point_is_no_route():
    g = ladder().graph()
    assert g.route(at(1, 0), at(1, 40), P) is None                          # ~3 km off the grid
    assert g.to_one({"E60": at(1, 0), "far": at(1, 40)}, at(3, 1), P).keys() == {"E60"}


def test_to_one_agrees_with_a_star():
    w = Ways()
    for j in range(6):
        w.add([at(i, j) for i in range(6)], name=f"Row {j}", **({"oneway": "yes"} if j % 2 else {}))
    for i in range(6):
        w.add([at(i, j) for j in range(6)], name=f"Col {i}", hw="tertiary" if i == 2 else "residential")
    g = w.graph()
    to = [LON0 + 4.5 * DX, LAT0 + 3 * DY]
    origins = {f"U{k}": [LON0 + (k % 6) * DX + 0.0002, LAT0 + (k // 6) * DY] for k in range(30)}
    many = g.to_one(origins, to, P)
    for k, ll in origins.items():
        one = g.route(ll, to, P)
        assert many[k][0] == pytest.approx(one["s"], rel=1e-6, abs=0.01), k
        assert many[k][1] == pytest.approx(one["m"], rel=0.02, abs=2), k


def test_speeds_by_road_class_and_the_emergency_factor():
    g = two_ways()
    slow = eta.Params(arterial_mph=20.0, emergency_factor=1.0)
    fast = eta.Params(arterial_mph=20.0, emergency_factor=2.0)
    a, b = g.route(at(0, 0), at(3, 0), slow), g.route(at(0, 0), at(3, 0), fast)
    assert a["s"] == pytest.approx(a["m"] / (20 * 0.95 * eta.KM_PER_MILE * 1000 / 3600), rel=0.02)   # secondary: 0.95
    assert b["s"] == pytest.approx(a["s"] / 2, rel=1e-6)


# ---- the live routes ----------------------------------------------------------------------------------------------------


def marker(lon, lat, place="position", look="responding", at_=NOW, **kw):
    return {"unit": "E61", "incident_id": 7, "look": look, "place": place, "lon": lon, "lat": lat,
            "line_to": [LON0 + 5 * DX, LAT0 + 5 * DY], "at": at_, "station": "", **kw}


def grid():
    w = Ways()
    for j in range(6):
        w.add([at(i, j) for i in range(6)], name=f"Row {j}")
    for i in range(6):
        w.add([at(i, j) for j in range(6)], name=f"Col {i}")
    return w.graph()


def routes_on(g, params=P, items=()):
    log = []
    return Routes(lambda: g, lambda: params, lambda: list(items), log=log.append, spawn=lambda fn: fn()), log


def test_a_route_is_cached_and_moved_along_by_fixes_on_it():
    r, _ = routes_on(grid())
    one = r.for_map([marker(*at(0, 0))], NOW)["routes"][0]
    assert (one["unit"], one["incident_id"], one["from"], one["why"]) == ("E61", 7, "fix", "new")
    assert one["coords"][0] == pytest.approx(at(0, 0)) and one["coords"][-1] == pytest.approx(at(5, 5))
    assert one["m"] == pytest.approx(5 * 84 + 5 * 88, rel=0.03) and one["text"].endswith(" mi")
    path = r.cache[("E61", 7)]["coords"]
    for k in range(1, 4):                       # along its path: the same route, less left
        p = _point_along(path, 60.0 * k)
        nxt = r.for_map([marker(*p, at_=NOW + 5 * k)], NOW + 5 * k)["routes"][0]
        assert nxt["at"] == one["at"] and nxt["why"] == "new"
        assert nxt["remain_m"] == pytest.approx(one["m"] - 60 * k, abs=3)
    assert r.stats["routed"] == 1


def test_off_the_path_by_more_than_the_setting_re_routes():
    r, _ = routes_on(grid())
    r.for_map([marker(*at(0, 0))], NOW)
    path = r.cache[("E61", 7)]["coords"]
    near = r.for_map([marker(*_offset(path[0], 30), at_=NOW + 5)], NOW + 5)["routes"][0]   # 30 m: kept
    assert near["why"] == "new"
    far = r.for_map([marker(*_offset(path[0], 120), at_=NOW + 10)], NOW + 10)["routes"][0]
    assert far["why"] == "off" and far["at"] == NOW + 10 and r.stats["routed"] == 2
    tight, _ = routes_on(grid(), eta.Params(reroute_m=20.0))
    tight.for_map([marker(*at(0, 0))], NOW)
    assert tight.for_map([marker(*_offset(path[0], 30), at_=NOW + 5)], NOW + 5)["routes"][0]["why"] == "off"


def test_heading_away_for_three_fixes_re_routes():
    r, _ = routes_on(grid())
    r.for_map([marker(*at(0, 0))], NOW)
    path = r.cache[("E61", 7)]["coords"]
    assert r.for_map([marker(*_point_along(path, 200), at_=NOW + 1)], NOW + 1)["routes"][0]["why"] == "new"
    whys = []
    for k in range(1, AWAY_FIXES + 1):                    # turned round: back along its own path, 20 m a fix
        whys.append(r.for_map([marker(*_point_along(path, 200 - 20 * k), at_=NOW + 1 + k)], NOW + 1 + k)["routes"][0]["why"])
    assert whys == ["new"] * (AWAY_FIXES - 1) + ["away"]
    assert r.stats["why"] == {"new": 1, "away": 1}
    # a jitter back of a few metres is not heading away
    j, _ = routes_on(grid())
    j.for_map([marker(*at(0, 0))], NOW)
    got = [j.for_map([marker(*_point_along(path, 100 + (5 if k % 2 else 0)), at_=NOW + k)], NOW + k)["routes"][0]["why"]
           for k in range(1, 8)]
    assert set(got) == {"new"}
    # a heading the other way at speed counts the same, on the path itself
    h, _ = routes_on(grid())
    h.for_map([marker(*at(0, 0))], NOW)
    p0 = h.cache[("E61", 7)]["coords"]
    brg = math.degrees(math.atan2((p0[1][0] - p0[0][0]) * 84000, (p0[1][1] - p0[0][1]) * 110000)) % 360
    got = [h.for_map([marker(*_point_along(p0, 5.0 + k), at_=NOW + k, heading=(brg + 180) % 360, speed=8.0)],
                     NOW + k)["routes"][0]["why"] for k in range(1, AWAY_FIXES + 1)]
    assert got == ["new"] * (AWAY_FIXES - 1) + ["away"]


def test_placed_by_status_is_routed_once_from_its_station():
    r, _ = routes_on(grid())
    m = marker(*at(0, 0), place="station", look="dispatched", at_=None, station="HQ")
    r.firehouses = lambda: [{"name": "HQ", "ll": at(0, 0)}]
    a = r.for_map([m], NOW)["routes"][0]
    b = r.for_map([m], NOW + 30)["routes"][0]
    assert a["from"] == "status" and b["at"] == NOW and r.stats["routed"] == 1
    assert a["eta_s"] == pytest.approx(a["remain_s"] + P.turnout_min * 60, abs=1)       # turnout from quarters
    assert r.for_map([], NOW + 40) == {"routes": [], "trails": {}} and r.cache == {}    # cleared: forgotten


def test_on_scene_or_no_graph_is_no_route_and_the_line_stays():
    r, _ = routes_on(grid())
    assert r.for_map([marker(*at(0, 0), look="onscene")], NOW)["routes"] == []
    none = Routes(lambda: None, lambda: P)
    assert none.for_map([marker(*at(0, 0))], NOW)["routes"] == [] and none.to_one({"E61": at(0, 0)}, at(1, 1)) == {}


def test_trails_keep_the_recent_fixes():
    r, _ = routes_on(grid())
    for k in range(5):
        r.for_map([marker(LON0 + k * 0.0002, LAT0, at_=NOW + k)], NOW + k)
    t = r.for_map([marker(LON0 + 4 * 0.0002, LAT0, at_=NOW + 4)], NOW + 5)["trails"]["E61"]
    assert len(t) == 5 and t[0] == [LON0, LAT0]
    assert "E61" not in r.for_map([marker(*at(0, 0), at_=NOW + 4)], NOW + 2000)["trails"]


def test_a_failed_route_is_not_tried_every_second():
    w = ladder()
    r, _ = routes_on(w.graph())
    m = dict(marker(*at(1, 0)), line_to=at(1, 40))       # no road near the call
    for k in range(5):
        assert r.for_map([m], NOW + k)["routes"] == []
    assert r.stats["routed"] == 1


def test_a_few_hundred_routes_are_quick():
    w = Ways()
    for j in range(40):
        w.add([at(i, j) for i in range(40)], name=f"Row {j}", **({"oneway": "yes"} if j % 3 == 1 else {}))
    for i in range(40):
        w.add([at(i, j) for j in range(40)], name=f"Col {i}", hw="tertiary" if i % 10 == 0 else "residential")
    t0 = time.monotonic()
    g = w.graph()
    built = time.monotonic() - t0
    pts = [[LON0 + ((k * 7) % 40) * DX + 0.0003, LAT0 + ((k * 11) % 40) * DY] for k in range(300)]
    t0 = time.monotonic()
    got = [g.route(pts[k], pts[(k + 150) % 300], P) for k in range(300)]
    took = time.monotonic() - t0
    assert all(got) and built < 1.0
    # 0.3 s here (~1 ms a route, every node settled at worst); a bound with room for a slow CI runner, still well
    # under one state build's routing budget per route
    assert took < 2.0, f"300 routes on a {g.n}-node grid took {took:.2f} s"
    assert max(r["pops"] for r in got) <= g.max_pops


# ---- the estimate: by road, else the straight line ------------------------------------------------------------------


def test_estimates_by_road_with_turnout_else_the_straight_line():
    units = [{"id": 1, "name": "E60"}, {"id": 2, "name": "E61"}]
    where = {"E60": {"ll": at(0, 0), "on_road": False, "label": "from HQ"},
             "E61": {"ll": at(1, 1), "on_road": True, "label": "live position"}}
    inc = {"lat": at(3, 3)[1], "lon": at(3, 3)[0]}
    road = lambda origins, to, inc: {"E60": (120.0, 1609.0), "E61": (60.0, 800.0)}
    est = runcards.estimates(units, where, inc, P, road)
    assert est[1] == {"eta_s": 240, "eta": "~4 min est.", "how": "road", "reason": "~4 min est. · 1.0 mi · from HQ"}
    assert est[2]["how"] == "road" and est[2]["eta_s"] == 60                  # on the road: no turnout
    # one without a way by road: the straight line for ALL of them, never a mix
    mixed = runcards.estimates(units, where, inc, P, lambda origins, to, inc: {"E60": (120.0, 1609.0)})
    straight = eta.eta_s(at(1, 1), at(3, 3), P, from_quarters=False)
    assert {e["how"] for e in mixed.values()} == {"line"} and mixed[2]["eta_s"] == round(straight)
    assert runcards.estimates(units, where, inc, P)[1]["how"] == "line"     # no graph: the model, as before
    broken = runcards.estimates(units, where, inc, P, lambda o, t, i: 1 / 0)
    assert broken[1]["how"] == "line"


def test_the_settings_numbers_are_editable_and_checked():
    for k in ("highway_mph", "arterial_mph", "local_mph", "emergency_factor", "wrongway_m", "reroute_m"):
        assert k in eta.NUMBERS and k in eta.KEYS
        lo, hi = eta.NUMBERS[k][1], eta.NUMBERS[k][2]
        with pytest.raises(ValueError):
            eta.check(k, hi + 1)
        assert eta.check(k, lo) == lo
    assert eta.Params().with_overrides({"reroute_m": 80.0}).reroute_m == 80.0
    assert eta.miles(1609.344) == "1.0 mi" and eta.miles(10) == "0.1 mi"


# ---- the provider, the config, the cache, the data source -----------------------------------------------------------


BOX = (-73.68, 40.99, -73.65, 41.01)


def in_process(answer: bytes, code: int = 200, calls: list | None = None):
    """A builder running fetch_build here, with a stand-in Overpass."""
    def post(url, data, timeout, limit):
        if calls is not None:
            calls.append(data.decode())
        return code, answer
    return lambda bbox, url, out: rg.fetch_build(bbox, url, out, post=post)


def test_refresh_builds_saves_and_a_bad_answer_keeps_the_graph(tmp_path):
    calls = []
    p = rg.RoadGraph(tmp_path, bbox=BOX, builder=in_process(OSM.read_bytes(), calls=calls), log=lambda m: None)
    assert p.refresh() and p.graph is not None and (tmp_path / rg.CACHE_NAME).is_file()
    assert "highway" in calls[0] and "bbox" in calls[0] and not list(tmp_path.glob("*.new"))
    g = p.graph
    p.builder = in_process(b"slow down", code=429)
    assert not p.refresh() and p.graph is g and "rate limited" in p.status["error"] and p.blocked()
    p.builder = in_process(b'{"elements": []}')
    assert not p.refresh() and p.graph is g                                  # an empty answer never replaces it
    q = rg.RoadGraph(tmp_path, bbox=BOX, log=lambda m: None)
    q.load_cache()
    assert q.graph is not None and q.graph.m == g.m and q.status["nodes"] == g.n


def test_an_overpass_error_remark_a_shrunken_graph_or_any_exception_keeps_the_graph(tmp_path):
    doc = json.loads(OSM.read_text())
    p = rg.RoadGraph(tmp_path, bbox=BOX, builder=in_process(OSM.read_bytes()), log=lambda m: None)
    assert p.refresh()
    g = p.graph
    bad = dict(doc, remark="runtime error: Query timed out in \"query\" at line 1 after 210 seconds.")
    p.builder = in_process(json.dumps(bad).encode())
    assert not p.refresh() and p.graph is g and "Overpass: runtime error" in p.status["error"]
    half = dict(doc, elements=doc["elements"][: len(doc["elements"]) * 6 // 10])     # 60 % of the roads
    p.builder = in_process(json.dumps(half).encode())
    assert not p.refresh() and p.graph is g and "not replacing" in p.status["error"]
    p.builder = lambda *a: {}["boom"]                                                 # anything at all
    assert not p.refresh() and p.graph is g and p.status["error"].startswith("KeyError")
    assert rg.MAX_BYTES <= 64 * 2 ** 20


def test_the_build_runs_in_its_own_process(tmp_path):
    """The hub's refresh: `python -m sizeup.roadgraph build` (here from the saved extract, no network); only the
    compact file comes back, and a failure comes back as its error."""
    out = tmp_path / "g.json"
    said = rg.build_in_child(BOX, "", out, src=OSM)
    assert said["edges"] > 50 and said["peak_mb"] > 0 and out.is_file()
    assert Graph(json.loads(out.read_text())).m == said["edges"]
    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        rg.build_in_child(BOX, "", tmp_path / "x.json", src=tmp_path / "missing.json")
    with pytest.raises(RuntimeError, match="over"):
        rg.build_in_child(BOX, "", tmp_path / "y.json", src=OSM, timeout=0.01)
    p = rg.RoadGraph(tmp_path, bbox=BOX, builder=lambda b, u, o: rg.build_in_child(b, u, o, src=OSM), log=lambda m: None)
    assert p.refresh() and p.graph.m == said["edges"]


def test_the_answer_is_trimmed_to_the_tags_used():
    t = rg.trim({"elements": [{"type": "way", "id": 1, "nodes": [1, 2], "geometry": [], "tags": {
        "highway": "residential", "name": "Example Street", "surface": "asphalt", "lit": "yes", "oneway": "yes"}},
        {"type": "node", "id": 5}], "remark": ""})
    assert t["elements"] == [{"type": "way", "nodes": [1, 2], "geometry": [], "tags": {"highway": "residential",
                                                                                         "name": "Example Street", "oneway": "yes"}}]


def test_the_user_agent_names_the_project():
    seen = {}

    class R:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"{}"
    orig = rg.urlopen
    try:
        rg.urlopen = lambda req, timeout: seen.update(h=dict(req.header_items()), url=req.full_url) or R()
        rg.http_post(rg.OVERPASS_URL, b"data=x", 5, 100)
    finally:
        rg.urlopen = orig
    assert seen["h"]["User-agent"].startswith("sizeup/") and "Port Chester FD" in seen["h"]["User-agent"]


def test_config_routing_section(tmp_path):
    import dataclasses

    from sizeup.config import Config, ConfigError, load
    assert next(f for f in dataclasses.fields(Config) if f.name == "routing_enabled").default is False   # built directly: off
    base = '[radio]\ntones_file = ""\n'
    p = tmp_path / "c.toml"
    p.write_text(base)
    c = load(p)
    assert c.routing_enabled is True and c.routing_ring_km == 12.0
    p.write_text(base + "\n[routing]\nenabled = false\nring_km = 8\n")
    c = load(p)
    assert (c.routing_enabled, c.routing_ring_km) == (False, 8.0)
    for bad in ("ring_km = 100", "enabled = 1", 'overpass_url = "http://x"', "colour = 1"):
        p.write_text(base + f"\n[routing]\n{bad}\n")
        with pytest.raises(ConfigError):
            load(p)


def test_from_config_area_and_never_on_a_station():
    class C:
        cad_db, routing_enabled, routing_ring_km, cloud_role = "", True, 12.0, "hub"
    fh = [{"name": "A", "ll": [-73.67, 41.0]}]
    g = rg.RoadGraph.from_config(C, fh)
    w, s, e, n = g.bbox
    assert g.configured and e - w == pytest.approx(2 * 12 / (111.32 * math.cos(math.radians(41))), abs=1e-3)
    C.cloud_role = "station"
    assert not rg.RoadGraph.from_config(C, fh).configured


def test_cached_and_listed_like_the_gis_layers():
    from sizeup import cachesync, datasources
    assert cachesync.gap_for("roadgraph.json") == 0.0

    class App:
        roadgraph = rg.RoadGraph(None, bbox=(-73.7, 40.9, -73.6, 41.0), log=lambda m: None)
    src = datasources.roadgraph_source(App.roadgraph)
    assert (src.key, src.group, src.every, src.quiet, src.managed) == ("roadgraph", "Map layers", "monthly", True, True)
    assert "OpenStreetMap" in src.attribution
    assert src.status()["note"] == "not built yet"


# ---- a small real extract: downtown Port Chester ------------------------------------------------------------------------


def osm_graph():
    return Graph(build(json.loads(OSM.read_text())))


def test_real_extract_routes_honour_its_one_ways():
    g = osm_graph()
    assert 50 < g.m < 2000 and OSM.stat().st_size < 100_000
    hq = [-73.66692, 41.00253]                                               # PCFD Headquarters (data/firehouses.json)
    pts = [[-73.6705 + 0.0011 * i, 40.9990 + 0.0011 * j] for i in range(10) for j in range(7)]
    found = 0
    for p in pts:
        for a, b in ((hq, p), (p, hq)):
            r = g.route(a, b, P, wrongway_m=0)
            if r is None:
                continue
            found += 1
            for e, d in r["way"]:
                assert not (d == 1 and g.eow[e]), (g.name(e), a, b)
            assert r["m"] >= eta.haversine_km(r["coords"][0], r["coords"][-1]) * 1000 - 1
    assert found > 100


def test_real_extract_routes_the_same_from_to_one():
    g = osm_graph()
    to = [-73.6650, 41.0020]
    origins = {f"P{i}": [-73.6705 + 0.0013 * i, 41.0045 - 0.0006 * i] for i in range(8)}
    many = g.to_one(origins, to, P)
    assert len(many) >= 6
    for k, (s, m) in many.items():
        assert s == pytest.approx(g.route(origins[k], to, P)["s"], rel=1e-6, abs=0.01)


# ---- the state ----------------------------------------------------------------------------------------------------------


def test_routes_trails_and_road_estimates_reach_the_console():
    """A made-up call in the real extract's streets: E61 attached from headquarters gets a route from there, then from
    its fixes; the recommendation's estimates are by road."""
    from tests.test_cad import make_cad, uid
    c = make_cad()
    g = osm_graph()
    c.routes = Routes(lambda: g, lambda: c.eta, lambda: [], lambda: c.firehouses, clock=c.clock, log=lambda m: None)
    hq = c.firehouses[0]
    e61 = uid(c, "E61")
    c.unit_station(e61, {"station": hq["name"], "operator": "Lt. Example"})
    iid = c.create({"type": "STRU", "address": "1 Example Street", "lat": 41.0020, "lon": -73.6650, "operator": "Lt. Example"})["id"]
    rec = c.recommend(iid, {})
    hows = {u["name"]: u["how"] for u in rec["units"]}
    assert hows.get("E61") == "road" and " mi · " in next(u["reason"] for u in rec["units"] if u["name"] == "E61")
    c.attach(iid, {"unit_ids": [e61], "operator": "Lt. Example"})
    c.unit_status(e61, {"status": "RS", "operator": "Lt. Example"})          # responding
    st = c.state()
    json.dumps(st)
    r = next(x for x in st["map"]["routes"] if x["unit"] == "E61")
    assert (r["incident_id"], r["from"]) == (iid, "status")
    assert r["coords"][0] == pytest.approx(hq["ll"], abs=0.003) and r["coords"][-1] == pytest.approx([-73.6650, 41.0020], abs=0.001)
    assert r["eta_s"] >= r["remain_s"] and r["text"].startswith("~")
    now = c.clock()
    c._feed_position({"unit": "E61", "lat": 41.0040, "lon": -73.6690, "at": now - 1, "source": "test"})
    r2 = next(x for x in c.state()["map"]["routes"] if x["unit"] == "E61")
    assert r2["from"] == "fix" and r2["at"] > r["at"]                         # its first fix: routed from there


# ---- helpers ------------------------------------------------------------------------------------------------------------


def _point_along(coords, metres):
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + eta.haversine_km(a, b) * 1000)
    for i in range(len(coords) - 1):
        if cum[i + 1] >= metres:
            t = (metres - cum[i]) / max(1e-9, cum[i + 1] - cum[i])
            return [coords[i][0] + (coords[i + 1][0] - coords[i][0]) * t, coords[i][1] + (coords[i + 1][1] - coords[i][1]) * t]
    return list(coords[-1])


def _offset(p, metres):
    """p moved `metres` north-west (off an east/north-running path)."""
    k = metres / math.sqrt(2)
    return [p[0] - k / (111_320 * math.cos(math.radians(p[1]))), p[1] + k / 110_574]


def test_project_helper():
    coords = [at(0, 0), at(2, 0)]
    off, along, brg = _project(coords, [0.0, eta.haversine_km(*coords) * 1000], [LON0 + DX, LAT0 + 0.0001])
    assert off == pytest.approx(11, abs=1) and along == pytest.approx(84, abs=2) and brg == pytest.approx(90, abs=1)


# ---- the page, in node (the follow tests' stand-in MapLibre) ----------------------------------------------------------


def _page_with_route():
    from tests.test_unit_follow import page_state, with_now
    data = with_now(page_state())
    e61 = next(m for m in data["map"]["markers"] if m["unit"] == "E61")
    e61.update(incident_id=7, incident_number="26-000007", line_to=[e61["lon"] + 0.01, e61["lat"] - 0.01])   # a made-up call
    to = e61["line_to"]
    mid = [e61["lon"] + 0.004, e61["lat"]]                                  # on east, the way it heads
    data["map"]["routes"] = [{"unit": "E61", "incident_id": e61["incident_id"], "coords": [[e61["lon"] - 0.002, e61["lat"]], [e61["lon"], e61["lat"]], mid, to],
                              "m": 1900, "s": 170, "along_m": 160, "remain_m": 1750, "remain_s": 160, "eta_s": 160,
                              "text": "~3 min · 1.1 mi", "from": "fix", "at": e61["at"], "why": "new"}]
    data["map"]["trails"] = {"E61": [[e61["lon"] - 0.003, e61["lat"]], [e61["lon"] - 0.0015, e61["lat"]]]}
    return data, e61, mid, to


@pytest.mark.skipif(__import__("shutil").which("node") is None and not Path("/opt/homebrew/bin/node").exists(), reason="node not installed")
def test_the_page_draws_the_route_from_the_mark_and_the_chip_says_it(tmp_path):
    from tests.test_unit_follow import run
    data, e61, mid, to = _page_with_route()
    o = run(tmp_path, r"""
const routes = () => rm.map.getSource("cm-routes").data.features;
const lines = () => rm.map.getSource("cm-lines").data.features;
out.routes = routes().map((f) => [f.properties.unit, !!f.properties.trail, f.geometry.coordinates]);
out.lines = lines().map((f) => f.geometry.coordinates);
out.mark = chip("E61").marker.getLngLat();
out.layers = Object.keys(rm.map.layers);
CadMap.followUnit("E61");
out.text = followText();
out.title = follow().title;
out.followed = routes().filter((f) => f.properties.trail).map((f) => f.properties.unit);
rm.update({ map: { ...IN.map, routes: [] }, lists: IN.lists }, null, "");
out.after = [routes().length, lines().length];
""", data)
    e = [r for r in o["routes"] if r[0] == "E61" and not r[1]]
    assert len(e) == 1 and not any(r[1] for r in o["routes"])                  # no trail until it is followed
    path = e[0][2]
    assert path[0] == [o["mark"]["lng"], o["mark"]["lat"]] and path[1:] == [mid, to]   # from the mark, the passed part gone
    assert not any(c[-1] == to and len(c) == 2 and c[0] == [e61["lon"], e61["lat"]] for c in o["lines"])   # no straight line too
    assert "cm-routes" in o["layers"] and "cm-trail" in o["layers"]
    assert o["text"].startswith("Following E61 · ") and "~3 min · 1.1 mi" in o["text"] and o["text"].endswith("ago")
    assert "by road (est.)" in o["title"] and o["followed"] == ["E61"]
    assert o["after"][0] == 1 and o["after"][1] >= 1                           # no route: the straight line back (the trail stays)


def test_the_estimate_tooltip_names_the_road_model():
    js = (ROOT / "sizeup" / "web" / "static" / "cad.js").read_text()
    assert 'u.how === "road"' in js and "by road (OSM)" in js


def test_a_route_from_quarters_is_moved_along_by_the_fixes_that_follow():
    r, _ = routes_on(grid())
    a = r.for_map([marker(*at(0, 0), place="station", look="responding", at_=None)], NOW)["routes"][0]
    path = r.cache[("E61", 7)]["coords"]
    for k in (1, 2, 3):
        b = r.for_map([marker(*_point_along(path, 100.0 * k), at_=NOW + k)], NOW + k)["routes"][0]
        assert b["remain_m"] == pytest.approx(a["m"] - 100 * k, abs=3) and b["along_m"] == pytest.approx(100 * k, abs=3)
    assert r.stats["routed"] == 1


def test_an_off_path_fix_not_routed_at_once_is_still_re_routed_later(monkeypatch):
    import sizeup.cad.routes as routes_mod
    r, _ = routes_on(grid())
    r.for_map([marker(*at(0, 0))], NOW)
    path = r.cache[("E61", 7)]["coords"]
    away = marker(*_offset(path[0], 150), at_=NOW + 5)
    real = r._route
    monkeypatch.setattr(r, "_route", lambda *a: None)                       # (a build that couldn't route it)
    r.for_map([away], NOW + 5)
    monkeypatch.setattr(r, "_route", real)
    assert r.for_map([away], NOW + 6)["routes"][0]["why"] == "off"           # the same fix, the next build
    assert routes_mod.BUDGET_S > 0


def test_the_wrong_way_stretch_never_uses_a_closed_block():
    """Oak St split at 3.5 by a stub: the call at 3.3 is reached wrong-way from East St over both blocks (59 m) when
    open; with the block 3.5-4 closed, the right way round."""
    w = ladder()
    w.els[1]["nodes"].insert(4, 999)
    w.els[1]["geometry"].insert(4, {"lon": LON0 + 3.5 * DX, "lat": LAT0 + DY})
    w.add([[LON0 + 3.5 * DX, LAT0 + DY], [LON0 + 3.5 * DX, LAT0 + 2 * DY]], name="Stub Lane")
    w.els[-1]["nodes"][0] = 999
    g = w.graph()
    to = [LON0 + 3.3 * DX, LAT0 + DY]
    assert names(g, g.route(at(4, 0), to, P, wrongway_m=60))[-2:] == ["Oak Street", "Oak Street"]
    item = {"id": "t:2", "source": "Port Chester", "kind": "closure", "closure": "full", "road": "Oak St",
            "geometry": {"type": "LineString", "coordinates": [[LON0 + 3.75 * DX, LAT0 + DY], [LON0 + 3.95 * DX, LAT0 + DY]]},
            "start": NOW - 60, "end": NOW + 600, "hours": []}
    pen = g.penalties([item], NOW)
    assert len(pen) == 1 and g.name(next(iter(pen))) == "Oak Street"          # the closed block only, not its neighbour
    r = g.route(at(4, 0), to, P, pen, wrongway_m=60)
    assert r is not None and "West Street" in names(g, r)


@pytest.mark.skipif(__import__("shutil").which("node") is None and not Path("/opt/homebrew/bin/node").exists(), reason="node not installed")
def test_the_route_moves_with_the_glided_mark_and_keeps_no_frames_of_its_own(tmp_path):
    from tests.test_unit_follow import run
    data, e61, mid, to = _page_with_route()
    o = run(tmp_path, r"""
const m0 = IN.map.markers.find((m) => m.unit === "E61");
const next = { ...m0, lon: m0.lon + 0.0004, at: m0.at + 5 };
rm.update({ map: { ...IN.map, markers: IN.map.markers.map((m) => (m.unit === "E61" ? next : m)) }, lists: IN.lists }, null, "");
out.pairs = [];
for (let i = 0; i < 6; i++) {
  spin(260); L.tick();
  const f = rm.map.getSource("cm-routes").data.features.find((x) => x.properties.unit === "E61" && !x.properties.trail);
  const ll = chip("E61").marker.getLngLat();
  out.pairs.push([f.geometry.coordinates[0], [ll.lng, ll.lat]]);
}
// no live fix anywhere and nothing followed: the routes alone never ask for frames
const still = IN.map.markers.map((m) => (m.unit === "E61" ? { ...m, place: "station", at: undefined } : m));
rm.update({ map: { ...IN.map, markers: still }, lists: IN.lists }, null, "");
for (let i = 0; i < 4; i++) { spin(20); L.tick(); }
out.moving = L.moving();
out.routes = rm.map.getSource("cm-routes").data.features.length;
""", data)
    for start, mark in o["pairs"]:
        assert start == pytest.approx(mark, abs=1e-9)                            # the route starts at the mark, every frame
    assert len({tuple(p[1]) for p in o["pairs"]}) > 1                            # (and the mark did move)
    assert o["moving"] is False and o["routes"] >= 1


@pytest.mark.skipif(__import__("shutil").which("node") is None and not Path("/opt/homebrew/bin/node").exists(), reason="node not installed")
def test_moving_lines_are_set_at_most_about_4_hz_and_the_age_timer_stops_itself(tmp_path):
    from tests.test_unit_follow import run
    data, *_ = _page_with_route()
    o = run(tmp_path, r"""
const m0 = IN.map.markers.find((m) => m.unit === "E61");
const next = { ...m0, lon: m0.lon + 0.004, at: m0.at + 10, speed: 15, heading: 90 };
rm.update({ map: { ...IN.map, markers: IN.map.markers.map((m) => (m.unit === "E61" ? next : m)) }, lists: IN.lists }, null, "");
const sets = { "cm-lines": 0, "cm-routes": 0 };
for (const id of Object.keys(sets)) { const s = rm.map.getSource(id), f = s.setData; s.setData = (d) => { sets[id] += 1; f(d); }; }
const marks = new Set();
const t0 = Date.now();
while (Date.now() - t0 < 1000) { spin(16); L.tick(); const ll = chip("E61").marker.getLngLat(); marks.add(ll.lng); }
out.sets = sets; out.marks = marks.size;
// the age timer: made while a fix is on screen; the map hidden (another view), its next beat clears it
const timers = [], cleared = [];
global.setInterval = (f, ms) => { timers.push([f, ms]); return 4242; };
global.clearInterval = (id) => { cleared.push(id); };
rm.canvas.offsetParent = null; L.draw();               // hidden: the real one (from loading) is cleared
rm.canvas.offsetParent = {}; L.draw();                 // shown again: a new one
out.made = timers.map((t) => t[1]);
rm.canvas.offsetParent = null;                         // gone from screen, no redraw: only the timer's own beat
timers[timers.length - 1][0]();
out.cleared = cleared.map((x) => (typeof x === "number" ? x : "real"));
""", data)
    assert o["marks"] > 20                                                    # the mark moved on most frames
    assert 2 <= o["sets"]["cm-routes"] <= 6 and o["sets"]["cm-lines"] <= 6, o["sets"]   # ~4 Hz over the second, not 60
    assert o["made"] == [1000] and o["cleared"][-1] == 4242


@pytest.mark.skipif(__import__("shutil").which("node") is None and not Path("/opt/homebrew/bin/node").exists(), reason="node not installed")
def test_the_call_line_from_a_moving_mark_is_set_at_most_about_4_hz(tmp_path):
    from tests.test_unit_follow import run
    data, *_ = _page_with_route()
    data["map"]["routes"] = []                                               # no graph: the straight call line
    o = run(tmp_path, r"""
const m0 = IN.map.markers.find((m) => m.unit === "E61");
const next = { ...m0, lon: m0.lon + 0.004, at: m0.at + 10, speed: 15, heading: 90 };
rm.update({ map: { ...IN.map, markers: IN.map.markers.map((m) => (m.unit === "E61" ? next : m)) }, lists: IN.lists }, null, "");
const s = rm.map.getSource("cm-lines"), f = s.setData;
let n = 0; s.setData = (d) => { n += 1; f(d); };
const t0 = Date.now();
while (Date.now() - t0 < 1000) { spin(16); L.tick(); }
out.n = n;
""", data)
    assert 2 <= o["n"] <= 6



# ---- the review's items (#361) --------------------------------------------------------------------------------------


def island_grid():
    """The 6x6 grid, and a separate parking-lot loop ~170 m east of it with no way onto it."""
    w = Ways()
    for j in range(6):
        w.add([at(i, j) for i in range(6)], name=f"Row {j}")
    for i in range(6):
        w.add([at(i, j) for j in range(6)], name=f"Col {i}")
    w.add([at(7, 2), at(8, 2), at(8, 3), at(7, 3), at(7, 2)], hw="service", name="Lot")
    return w.graph()


def test_islands_are_never_starts_or_calls_and_no_way_is_quick():
    g = island_grid()
    assert g.components == 2 and sum(g.main) == 36
    lot = [LON0 + 7.5 * DX, LAT0 + 2.5 * DY]
    s = g.snap(*lot)
    assert s is not None and g.name(s["edge"]) == "Col 5"                     # onto the grid (~210 m), never the lot
    far = [LON0 + 9.5 * DX, LAT0 + 2.5 * DY]                                   # beside the lot: no grid road within 300 m
    t0 = time.monotonic()
    assert g.route(at(0, 0), far, P) is None and g.to_one({"E61": at(0, 0)}, far, P) == {}
    assert time.monotonic() - t0 < 0.05
    # walled in by closures: the search ends after at most every node once
    pen = {e: math.inf for e in range(g.m) if g.name(e) in ("Row 1", "Col 1") or g.name(e) == "Row 0"}
    r = g.route(at(0, 0), at(5, 5), P, pen)
    assert g.max_pops <= g.n + 16 and (r is None or r["pops"] <= g.max_pops)


def test_no_wrong_way_on_highways_or_roundabouts():
    w = Ways()
    w.add([at(i, 0) for i in range(5)], name="Main Street")
    w.add([at(i, 1) for i in range(5)], hw="motorway_link", name="Exit 2")    # one-way by its class
    w.add([at(0, 0), at(0, 1)], name="West Street")
    w.add([at(4, 0), at(4, 1)], name="East Street")
    g = w.graph()
    to = [LON0 + 3.3 * DX, LAT0 + DY]                                          # 59 m short of East St, on the ramp
    r = g.route(at(4, 0), to, P, wrongway_m=60)
    assert g.cls(g.snap(*to)["edge"]) == "motorway_link" and r["m"] > 300     # round, never backwards up a ramp
    w2 = ladder()
    w2.els[1]["tags"] = {"highway": "residential", "name": "Oak Street", "junction": "roundabout"}
    g2 = w2.graph()
    assert g2.route(at(4, 0), to, P, wrongway_m=60)["m"] > 300
    assert ladder().graph().route(at(4, 0), to, P, wrongway_m=60)["m"] < 150   # a plain one-way street: allowed


def test_a_closure_naming_no_road_only_slows_and_only_along_its_line():
    g = two_ways()
    main = next(e for e in range(g.m) if g.name(e) == "Main Street")
    assert g.penalties([closure(road="")], NOW) == {main: rg.UNNAMED_SLOW}
    across = dict(closure(road=""), geometry={"type": "LineString", "coordinates": [
        [LON0 + 1.5 * DX, LAT0 - 0.0003], [LON0 + 1.5 * DX, LAT0 + 0.0003]]})        # a line across Main St
    assert g.penalties([across], NOW) == {}


def test_a_full_closure_on_a_highway_edge_slows_it_rather_than_shutting_it():
    w = Ways()
    w.add([at(0, 0), at(3, 0)], hw="motorway", name="New England Thruway", ref="I 95")
    w.add([at(3, 0), at(0, 0)], hw="motorway", name="New England Thruway", ref="I 95")
    g = w.graph()
    pen = g.penalties([closure(road="I-95")], NOW)
    assert pen and set(pen.values()) == {rg.HIGHWAY_CLOSED}


def test_the_closures_are_worked_out_again_only_on_a_change_and_off_the_state_build():
    calls, jobs = [], []
    items = [closure(start=NOW - 60, end=NOW + 600)]
    g = two_ways()
    real = g.penalties
    g.penalties = lambda its, now: calls.append(now) or real(its, now)
    r = Routes(lambda: g, lambda: P, lambda: items, log=lambda m: None, spawn=lambda fn: fn())
    for t in (0, 60, 120, 590):
        r.penalties(g, NOW + t)
    assert calls == [NOW]                                                     # the minute no longer counts
    r.penalties(g, NOW + 601)                                                 # the closure's window closed
    assert calls[-1] == NOW + 601 and r.penalties(g, NOW + 602)[0] == {}
    items = [closure(start=NOW + 900, end=NOW + 1800)]                        # a new list: again
    r.penalties(g, NOW + 610)
    assert len(calls) == 3
    lazy = Routes(lambda: g, lambda: P, lambda: items, log=lambda m: None, spawn=jobs.append)
    assert lazy.penalties(g, NOW) == ({}, 0) and len(jobs) == 1 and len(calls) == 3   # not on the state build
    jobs[0]()
    assert len(calls) == 4 and lazy.penalties(g, NOW)[1] == 0                 # (none in effect yet)


def test_a_rig_off_the_road_is_not_re_routed_on_every_fix():
    r, _ = routes_on(grid())
    yard = [LON0 + 2.5 * DX, LAT0 - 0.0009]                                   # ~100 m south of Row 0, in a yard
    r.for_map([marker(*yard)], NOW)
    for k in range(1, 6):
        assert r.for_map([marker(yard[0] + 0.00001 * k, yard[1], at_=NOW + k)], NOW + k)["routes"][0]["why"] == "new"
    assert r.stats["routed"] == 1
    gone = r.for_map([marker(LON0 + 5 * DX, LAT0 - 0.0012, at_=NOW + 9)], NOW + 9)["routes"][0]
    assert gone["why"] == "off"


def test_the_routing_budget_spreads_many_units_over_builds(monkeypatch):
    import sizeup.cad.routes as routes_mod
    monkeypatch.setattr(routes_mod, "BUDGET_S", 0.0)                          # one route a build, at least
    r, _ = routes_on(grid())
    ms = [dict(marker(*at(k % 6, k // 6)), unit=f"U{k}") for k in range(30)]
    counts = [len(r.for_map(ms, NOW + k)["routes"]) for k in range(30)]
    assert counts[0] == 1 and counts == list(range(1, 31))
    monkeypatch.setattr(routes_mod, "BUDGET_S", 10.0)
    r2, _ = routes_on(grid())
    assert len(r2.for_map(ms, NOW)["routes"]) == 30


@pytest.mark.skipif(__import__("shutil").which("node") is None and not Path("/opt/homebrew/bin/node").exists(), reason="node not installed")
def test_a_route_that_doubles_back_keeps_its_first_leg(tmp_path):
    from tests.test_unit_follow import page_state, run
    o = run(tmp_path, r"""
const x = -73.67, y = 41.0;
const coords = [[x, y], [x + 0.003, y], [x + 0.0005, y], [x + 0.0005, y + 0.002]];   // out east, back west, then north
const mark = [x + 0.001, y];                                                          // on the street both legs use
out.first = CadMap.routeFrom(coords, mark, 80);          // the hub had it ~80 m along: the first leg
out.none = CadMap.routeFrom(coords, mark);               // no along: a tie goes to the earlier segment
out.back = CadMap.routeFrom(coords, mark, 380);          // ~380 m along: on its way back
""", {**page_state(), "map": {"markers": [], "routes": []}})
    x, y = -73.67, 41.0
    assert o["first"] == [[x + 0.001, y], [x + 0.003, y], [x + 0.0005, y], [x + 0.0005, y + 0.002]]
    assert o["none"] == o["first"]
    assert o["back"] == [[x + 0.001, y], [x + 0.0005, y], [x + 0.0005, y + 0.002]]



def test_a_closed_ramp_stays_shut_while_the_mainline_is_only_slowed():
    w = Ways()
    w.add([at(0, 0), at(3, 0)], hw="motorway", name="New England Thruway", ref="I 95")
    w.add([at(1, 0), at(1.6, -1)], hw="motorway_link", name="Exit 21", ref="I 95")       # an I-95-style off-ramp
    g = w.graph()
    ramp = next(e for e in range(g.m) if g.cls(e) == "motorway_link")
    main = [e for e in range(g.m) if g.cls(e) == "motorway"]
    on_ramp = dict(closure(how="ramp", road="I-95"), geometry={"type": "LineString", "coordinates": [at(1.1, -0.15), at(1.5, -0.8)]})
    assert g.penalties([on_ramp], NOW) == {ramp: math.inf}
    full_ramp = dict(on_ramp, closure="full")
    assert g.penalties([full_ramp], NOW) == {ramp: math.inf}
    on_main = dict(closure(road="I-95"), geometry={"type": "LineString", "coordinates": [at(1.8, 0), at(2.6, 0)]})
    pen = g.penalties([on_main], NOW)
    assert pen and set(pen) <= set(main) and set(pen.values()) == {rg.HIGHWAY_CLOSED}


def test_an_older_cache_is_not_read_and_a_leftover_temp_file_is_cleared(tmp_path):
    old = build(json.loads(OSM.read_text()))
    old["v"] = 1
    with pytest.raises(ValueError):
        Graph(old)
    (tmp_path / rg.CACHE_NAME).write_text(json.dumps(old))
    p = rg.RoadGraph(tmp_path, bbox=BOX, log=lambda m: None)
    p.load_cache()
    assert p.graph is None and rg.VERSION == 2
    out = tmp_path / "roadgraph.new"
    stale = out.with_suffix(".tmp")
    stale.write_text("half a graph from a build that was killed")
    calls = []

    def post(url, data, timeout, limit):
        calls.append(stale.exists())
        return 200, OSM.read_bytes()
    rg.fetch_build(BOX, "", out, post=post)
    assert calls == [False] and out.is_file() and not stale.exists()


# ---- highways: local streets for a local call, the highway (its way) for a highway call -----------------------------
# The user, 2026-11-10: "for the paths, unless it's a highway call it's unlikely a rig would ever take a highway to get
# to a normal call". Made-up streets; the highway names are the area's public ones.


def freeway(parkway=False):
    """A rig's street (Long Road) loops 28 blocks round from (0, 0) to (20, 0); a highway beside it is the short way:
    an eastbound carriageway at j=-1 and a westbound one at j=-1.3, an on-ramp from (0, 0) eastbound and one from
    (20, 0) westbound, and the matching off-ramps."""
    w = Ways()
    w.add([at(0, 0), at(0, 4), at(20, 4), at(20, 0)], name="Long Road")
    w.add([at(-2, 0), at(0, 0)], name="Short Street")
    if parkway:
        name, ref = "Hutchinson River Parkway", None
    else:
        name, ref = "New England Thruway", "I 95"
    tags = {"name": name, **({"ref": ref} if ref else {})}
    w.add([at(i, -1) for i in range(1, 20)], hw="motorway", **tags)                 # eastbound
    w.add([at(i, -1.3) for i in range(19, 0, -1)], hw="motorway", **tags)           # westbound
    w.add([at(0, 0), at(1, -1)], hw="motorway_link")                                 # on, eastbound
    w.add([at(19, -1), at(20, 0)], hw="motorway_link")                               # off, eastbound
    w.add([at(20, 0), at(19, -1.3)], hw="motorway_link")                             # on, westbound
    w.add([at(1, -1.3), at(0, 0)], hw="motorway_link")                               # off, westbound
    return w.graph()


def limited_in(g, r):
    return {g.limited(e) for e, _ in r["way"]} - {0}


def test_a_highway_call_is_read_from_the_calls_words():
    hc = rg.highway_call
    assert hc("100 Example Ave", "Example Pl", typ="ALS") == rg.LOCAL
    assert hc("I-95 NB", "Exit 21") == rg.Call({"i95", "*"}, "N")
    assert hc("Hutch SB @ Exit 25").keys == {"hutch", "*"} and hc("Hutch SB @ Exit 25").dir == "S"
    assert hc("Cross Westchester Expwy EB").keys == {"i287", "*"} and hc("I287 EB").dir == "E"
    assert hc("Merritt Pkwy westbound").keys == {"merritt", "*"} and hc("Merritt Pkwy westbound").dir == "W"
    assert hc("Thruway", typ="MVA").keys == {"i95", "i287", "i87", "*"} and hc("Thruway", typ="MVA").vehicle
    assert hc("684 SB").keys == {"i684", "*"}
    assert hc("Exit 9 ramp").keys == {"*"} and hc("MM 12.4").keys == {"*"}
    assert hc("95 North Main St") == rg.LOCAL and hc("287 Example Ave") == rg.LOCAL     # a house number, not a road
    assert hc("100 Example Ave", "Example Pl NB").dir is None                          # a direction on no highway
    assert rg.hw_keys("New England Thruway; I 95") == {"i95"} and rg.hw_keys("Hutchinson River Parkway; HR") == {"hutch"}


def test_the_graph_marks_motorways_parkways_and_their_ramps():
    g = freeway()
    kinds = {(g.cls(e), g.limited(e)) for e in range(g.m)}
    assert kinds == {("residential", 0), ("motorway", 1), ("motorway_link", 1)}
    assert all(g.kinds[g.ekind[e]][1] == {"i95"} for e in range(g.m) if g.limited(e))    # the ramps take I-95's keys
    p = freeway(parkway=True)
    assert {p.limited(e) for e in range(p.m) if p.cls(e) != "residential"} == {2}
    w = Ways()
    w.add([at(0, 0), at(5, 0)], hw="trunk", name="Example Circle")                       # a trunk, not a parkway
    w.add([at(0, 1), at(5, 1)], hw="trunk", name="Example River Parkway")
    t = w.graph()
    assert {t.name(e): t.limited(e) for e in range(t.m)} == {"Example Circle": 0, "Example River Parkway": 2}


def test_a_local_call_keeps_off_the_motorway_even_when_it_is_shorter():
    g = freeway()
    to = [LON0 + 20 * DX, LAT0 + 0.5 * DY]                              # on Long Road, beside the off-ramp
    free = g.route(at(0, 0), to, P, call=rg.highway_call("I-95 EB"))   # (as if it were a highway call)
    r = g.route(at(0, 0), to, P)
    assert free["m"] < 2000 and limited_in(g, free) == {1}
    assert limited_in(g, r) == set() and r["m"] > 2200 and not r["highway"]
    assert {g.name(e) for e, _ in r["way"]} <= {"Long Road"}
    # the estimate agrees: by Long Road, and its seconds are driving time at street speeds
    one = g.to_one({"E60": at(0, 0)}, to, P)["E60"]
    assert one[0] == pytest.approx(r["s"], rel=1e-6) and one[1] == pytest.approx(r["m"], rel=0.02)
    assert r["s"] == pytest.approx(r["m"] * g.spm(P)[rg.CLASSES.index("residential")], rel=0.02)


def test_a_motorway_is_a_last_resort_with_its_seconds_as_driven():
    w = Ways()
    w.add([at(-2, 0), at(0, 0)], name="Short Street")
    w.add([at(0, 0), at(1, -1)], hw="motorway_link")
    w.add([at(i, -1) for i in range(1, 20)], hw="motorway", name="Cross Westchester Expressway", ref="I 287")
    w.add([at(19, -1), at(20, 0)], hw="motorway_link")
    w.add([at(20, 0), at(22, 0)], name="Far Street")
    g = w.graph()
    r = g.route(at(-2, 0), at(22, 0), P)                                 # no other way: still a route
    assert r and limited_in(g, r) == {1}
    spm = g.spm(P)
    driven = sum(g.elen[e] * spm[g.ecls[e]] for e, _ in r["way"]) + g.elen[0] * spm[rg.CLASSES.index("residential")]
    assert r["s"] == pytest.approx(driven, rel=0.05)                     # not LIMITED_SLOW times it
    assert g.to_one({"E60": at(-2, 0)}, at(22, 0), P)["E60"][0] == pytest.approx(r["s"], rel=1e-6)


def test_a_highway_call_uses_the_highway_by_the_on_ramp_for_its_direction():
    g = freeway()
    to = at(10, -1.15)                                                   # between the carriageways
    eb = g.route(at(-2, 0), to, P, call=rg.highway_call("I-95 EB", "Exit 21"))
    wb = g.route(at(-2, 0), to, P, call=rg.highway_call("I-95 WB", "Exit 21"))
    sb_e, sb_w = g.target(*to, rg.highway_call("I-95 EB"))[0], g.target(*to, rg.highway_call("I-95 WB"))[0]
    assert 45 < g.bearing(sb_e["edge"], sb_e["seg"]) < 135 and 225 < g.bearing(sb_w["edge"], sb_w["seg"]) < 315
    assert eb["highway"] and limited_in(g, eb) == {1} and eb["m"] < 1200            # on at (0, 0), east 9 blocks
    assert wb["highway"] and limited_in(g, wb) == {1} and wb["m"] > eb["m"] + 1000      # east, off, and back on westbound
    last = wb["way"][-1][0]
    assert 225 < g.bearing(last, 0) < 315 and wb["way"][-2][0] in {e for e in range(g.m) if g.cls(e) == "motorway_link"} | {last}
    assert eb["coords"][-1] == pytest.approx([sb_e["lon"], sb_e["lat"]]) and wb["coords"][-1][1] < eb["coords"][-1][1]
    # no direction said: the nearest carriageway
    near = g.target(LON0 + 10 * DX, LAT0 - 1.25 * DY, rg.highway_call("I-95"))[0]
    assert 225 < g.bearing(near["edge"], near["seg"]) < 315
    # a call that lands on the motorway with no words for it (an MVA's point on I-95) is a highway call too
    on = g.route(at(-2, 0), at(10, -1), P, call=rg.Call(vehicle=True))
    assert on["highway"] and limited_in(g, on) == {1}


def test_parkways_are_shut_to_rigs_unless_the_call_is_on_that_parkway():
    g = freeway(parkway=True)
    to = [LON0 + 20 * DX, LAT0 + 0.5 * DY]
    r = g.route(at(0, 0), to, P)
    assert limited_in(g, r) == set()
    assert limited_in(g, g.route(at(0, 0), to, P, call=rg.highway_call("I-95 EB"))) == set()    # another highway's call
    on = g.route(at(-2, 0), at(10, -1.15), P, call=rg.highway_call("Hutch EB"))
    assert on["highway"] and limited_in(g, on) == {2} and on["m"] < 1200
    # a parkway as the only way: no route at all for a local call
    w = Ways()
    w.add([at(-2, 0), at(0, 0)], name="Short Street")
    w.add([at(0, 0), at(1, -1)], hw="motorway_link")
    w.add([at(i, -1) for i in range(1, 20)], hw="motorway", name="Merritt Parkway", ref="CT 15")
    w.add([at(19, -1), at(20, 0)], hw="motorway_link")
    w.add([at(20, 0), at(22, 0)], name="Far Street")
    only = w.graph()
    assert only.route(at(-2, 0), at(22, 0), P) is None and only.to_one({"E60": at(-2, 0)}, at(22, 0), P) == {}
    assert only.route(at(-2, 0), at(10, -1), P, call=rg.highway_call("Merritt Pkwy EB")) is not None


def test_the_live_routes_read_each_calls_words_and_route_again_when_they_change():
    g = freeway()
    r, _ = routes_on(g)
    to = at(20, 0.5)                                                     # on Long Road
    m = dict(marker(*at(-2, 0)), line_to=to)
    inc = {"id": 7, "address": "100 Example Ave", "cross_street": "", "type": "ALS"}
    local = r.for_map([m], NOW, [inc])["routes"][0]
    assert local["m"] > 2300                                             # round by Long Road
    hw = r.for_map([dict(m, at=NOW + 1)], NOW + 1, [dict(inc, address="I-95 EB", cross_street="Exit 21")])["routes"][0]
    assert hw["why"] == "new" and hw["m"] < local["m"] - 400                          # by I-95 eastbound
    est = r.to_one({"E60": at(-2, 0)}, to, dict(inc, address="I-95 EB"))["E60"]
    assert est[1] == pytest.approx(hw["m"], rel=0.05)
