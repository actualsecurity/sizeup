"""The Intel page's range, paging and map pins (sizeup/intel/eventlog.py, static/intel-page.js, static/intel-map.js).

Admin, 2026-11-26: "why am I only seeing back to the 25th? We had intel events before then" (the list's 24 h default
was not shown anywhere); "like elsewhere we need to be able to drag this map window up/down to resize"; and a list of
placed events with fewer pins on the map than rows (the map not fitted again when its pins changed, pins at one point
drawn one over the other).

  - the range: ?range=24h | 7d | 30d | all (no from date), echoed with the count of the filters' events before it
    (older); the pick kept per person (me-prefs "intel");
  - paging: the first page's newest id (upto) held for the pages after it, so every row comes back exactly once while
    events keep arriving; a from date before what is kept reads every event kept;
  - the map: every placed event of the filters, on every page and range, with its point; pins at one point badged and
    clicked through in turn; fitted again when the pins change unless the viewer moved the map;
  - the map's height: Panels.separator on its bottom edge (keys, drag, Home), remembered per viewer.

Invented places only (Example Road, Sample St); towns are the public config's."""

from __future__ import annotations

import json

import pytest

from sizeup.cad import prefs as prefs_mod
from sizeup.intel import eventlog
from tests.test_intel_map import MAPSTUB
from tests.test_intel_page import STATIC, T0, listing, needs_node, run_node, save, site  # noqa: F401

DAY = 86400


def ids_of(d):
    return [e["id"] for e in d["events"]]


# -- the range -----------------------------------------------------------------------------------------------------------

def test_the_range_picks_and_the_older_count(site):
    cad, clock = site
    clock.t = T0
    ages = {"hour": 3600, "days3": 3 * DAY, "days20": 20 * DAY, "days60": 60 * DAY}
    ev = {k: save(cad, first_at=T0 - a, last_at=T0 - a).id for k, a in ages.items()}
    want = {"24h": ({ev["hour"]}, 3), "7d": ({ev["hour"], ev["days3"]}, 2),
            "30d": ({ev["hour"], ev["days3"], ev["days20"]}, 1), "all": (set(ev.values()), 0)}
    for r, (got_ids, older) in want.items():
        d = listing(cad, range=r)
        assert set(ids_of(d)) == got_ids and d["total"] == len(got_ids), r
        assert (d["range"], d["older"]) == (r, older), r
    d = listing(cad)                                                       # no range: the last 24 h, said so
    assert (d["range"], d["older"], d["total"]) == ("24h", 3, 1)
    assert listing(cad, range="all", level="push")["older"] == 0            # (the filters apply to older too)
    assert listing(cad, level="log")["older"] == 3
    # a from date: the dates are the range (none echoed); older: what is kept before it
    day = __import__("datetime").datetime.fromtimestamp(T0 - 4 * DAY, __import__("sizeup.cad.store", fromlist=["TZ"]).TZ)
    d = listing(cad, **{"from": day.strftime("%Y-%m-%d"), "range": "30d"})
    assert d["range"] == "" and set(ids_of(d)) == {ev["hour"], ev["days3"]} and d["older"] == 2
    assert listing(cad, review=1)["older"] == 0                             # the review queue: every date already
    from sizeup.cad.api import CadApi
    from tests.test_intel_page import get
    from tests.test_roles import VAL
    code, body = get(CadApi(cad), "/api/cad/intel/list", VAL, "range=1y")
    assert code == 400 and "range" in body["error"]
    assert set(eventlog.RANGES) == set(prefs_mod.INTEL_RANGES)


def test_the_range_pick_is_a_known_shape_kept_per_person(tmp_path):
    assert json.loads(prefs_mod.validate("intel", {"range": "7d"})) == {"range": "7d"}
    assert json.loads(prefs_mod.validate("intel", {})) == {}
    for bad in ({"range": "1y"}, {"range": 7}, {"other": "7d"}, ["7d"], "7d"):
        with pytest.raises(prefs_mod.PrefsError):
            prefs_mod.validate("intel", bad)


# -- paging ---------------------------------------------------------------------------------------------------------------

def test_paging_returns_every_row_exactly_once_while_events_arrive(site):
    cad, clock = site
    clock.t = T0
    made = [save(cad, first_at=T0 - i * 3600, last_at=T0 - i * 3600).id for i in range(130)]   # 130 h: 7d holds them
    first = listing(cad, range="7d")
    assert first["total"] == 130 and first["upto"] == max(made)
    for i in range(3):                                                    # newer events, saved between the pages
        save(cad, first_at=T0 + 10 + i, last_at=T0 + 10 + i)
    got = ids_of(first)
    for page in (1, 2):
        got += ids_of(listing(cad, range="7d", page=page, upto=first["upto"]))
    assert got == made                                                    # newest first, each exactly once
    # (without the first page's newest id, the arrivals push its last rows onto the next page a second time)
    shifted = ids_of(listing(cad, range="7d", page=1))
    assert shifted[0] in ids_of(first)
    from sizeup.cad.api import CadApi
    from tests.test_intel_page import get
    from tests.test_roles import VAL
    assert get(CadApi(cad), "/api/cad/intel/list", VAL, "upto=abc")[0] == 400


def test_a_from_date_before_what_is_kept_reads_every_event_kept_across_pages(site):
    cad, clock = site
    clock.t = T0
    made = [save(cad, first_at=T0 - i * 0.6 * DAY, last_at=T0 - i * 0.6 * DAY).id for i in range(140)]   # 84 days
    gone = save(cad, first_at=T0 - 120 * DAY, last_at=T0 - 120 * DAY).id      # (pruned by then: past the 92-day span)
    day = __import__("datetime").datetime.fromtimestamp(T0 - 200 * DAY, __import__("sizeup.cad.store", fromlist=["TZ"]).TZ)
    q = {"from": day.strftime("%Y-%m-%d")}
    p0 = listing(cad, **q)
    assert p0["total"] == 140 and p0["since"] >= T0 - eventlog.SPAN_MAX_S - 61
    got = ids_of(p0) + [i for p in (1, 2) for i in ids_of(listing(cad, page=p, upto=p0["upto"], **q))]
    assert got == made and gone not in got and len(set(got)) == len(got)
    assert ids_of(listing(cad, page=3, upto=p0["upto"], **q)) == []
    all_ = listing(cad, range="all")                                       # All: the same events
    assert all_["total"] == 140 and all_["older"] == 0


def test_events_are_kept_90_days_unless_noted():
    from sizeup.intel import events
    assert events.KEEP_EVENTS_S == 90 * DAY and eventlog.SPAN_MAX_S >= events.KEEP_EVENTS_S
    assert eventlog.RANGES["all"] == eventlog.SPAN_MAX_S


# -- the map's pins --------------------------------------------------------------------------------------------------------

def test_every_placed_event_of_the_filter_has_its_pin_with_its_point(site):
    """Two events on one street in one town (the street's one point) and one in another: three pins, two at one point;
    with a longer range, the older placed events too. The map's pins are exactly the list's placed rows, every page."""
    cad, clock = site
    clock.t = T0
    same = dict(precision="street", lat=40.98, lon=-73.68, radius_m=300.0, label_full="Example Road, Rye", town="Rye")
    a = save(cad, first_at=T0 - 60, last_at=T0 - 60, **same).id
    b = save(cad, first_at=T0 - 120, last_at=T0 - 120, **same).id
    c = save(cad, first_at=T0 - 180, last_at=T0 - 180, precision="address", lat=41.0, lon=-73.66, radius_m=25.0,
             label_full="12 Sample St, Port Chester", town="Port Chester").id
    save(cad, first_at=T0 - 240, last_at=T0 - 240, precision="none")
    old = save(cad, first_at=T0 - 5 * DAY, last_at=T0 - 5 * DAY, **same).id
    for q, want in (({}, {a, b, c}), ({"range": "7d"}, {a, b, c, old}), ({"range": "24h", "level": "log"}, {a, b, c})):
        m = listing(cad, map=1, **q)
        pins = {p["id"]: p for p in m["pins"]}
        assert set(pins) == want and m["placed"] == len(want) and m["unplaced"] == 1, q
        assert all(p["lat"] is not None and p["lon"] is not None for p in pins.values())
        rows, page = [], 0
        while True:
            d = listing(cad, page=page, size=2, **q)
            rows += d["events"]
            page += 1
            if page * d["size"] >= d["total"]:
                break
        assert {e["id"] for e in rows if e["lat"] is not None} == set(pins), q
    m = listing(cad, map=1)
    assert (m["pins"][0]["lat"], m["pins"][0]["lon"]) == (m["pins"][1]["lat"], m["pins"][1]["lon"])     # one point


@needs_node
def test_pins_at_one_point_are_badged_and_clicked_through_in_turn():
    script = f"""
{MAPSTUB}
{(STATIC / "intel-map.js").read_text()}
const map = new maplibregl.Map({{ container: null }});
const picks = [];
const h = IntelMap.attach(map, "t", {{ pick: (id) => {{ picks.push(id); h.select(id); }} }});
const p = (id, lat, lon) => ({{ id, lat, lon, precision: "street", radius_m: 300, confirmed: false, level: "log", category: "weapons" }});
h.set([p(1, 40.98, -73.68), p(2, 40.98, -73.68), p(3, 41.0, -73.66), p(4, 40.98, -73.68)]);
const props = map.data.features.map((f) => [f.properties.id, f.properties.n, f.properties.b]);
// a click where three pins are: the top one, then each in turn, then the first again
const at = {{ features: [4, 2, 1].map((id) => ({{ properties: {{ id }} }})) }};
for (let i = 0; i < 4; i++) map.lh["click:t-pin"][0](at);
const many = IntelMap.features(Array.from({{ length: 12 }}, (_, i) => p(i + 10, 41, -73.7)));
console.log(JSON.stringify({{ props, picks, b: many[11].properties.b, nb: many.filter((f) => f.properties.b).length }}));
"""
    out = run_node(script, {})
    assert out["props"] == [[1, 3, ""], [2, 3, ""], [3, 1, ""], [4, 3, "im-n3"]]   # one badge, on the top one (drawn last)
    assert out["picks"] == [4, 2, 1, 4]
    assert out["b"] == f"im-n{10}" and out["nb"] == 1                               # "9+"


PAGE = r"""
const store = new Map();
global.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
Object.getPrototypeOf($("#il-review")).getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
const mkPick = (r) => { const b = el("button"); b.dataset = { range: r }; return b; };
$("#il-picks").append(...["24h", "7d", "30d", "all"].map(mkPick));
"""


@needs_node
def test_the_map_is_fitted_again_when_its_pins_change_unless_the_viewer_moved_it(site):
    """The screenshot's case: the map fitted to the first pin it had; the next placed events (the same filters) came
    in off the view. Now fitted to them, unless the viewer had moved the map; new filters always fitted."""
    cad, clock = site
    clock.t = T0
    save(cad, first_at=T0 - 60, last_at=T0 - 60, precision="address", lat=41.0, lon=-73.66, label_full="12 Sample St")
    d1, m1 = listing(cad), listing(cad, map=1)
    for i in range(2):
        save(cad, first_at=T0 - 30 + i, last_at=T0 - 30 + i, precision="street", lat=40.98, lon=-73.68,
             label_full="Example Road, Rye")
    d2, m2 = listing(cad), listing(cad, map=1)
    save(cad, first_at=T0 - 10, last_at=T0 - 10, precision="address", lat=40.99, lon=-73.69, label_full="40 Example Road")
    d3, m3 = listing(cad), listing(cad, map=1)
    script = f"""
{MAPSTUB}
{PAGE}
{(STATIC / "map-style.js").read_text()}
{(STATIC / "intel-map.js").read_text()}
{(STATIC / "intel-page.js").read_text()}
global.cad.scene.home = {{ center: [-73.66, 41.0], zoom: 13 }};
let LIST = IN.d1, MAPANS = IN.m1;
global.api = (meth, path) => {{ calls.get.push(path); return Promise.resolve(JSON.parse(JSON.stringify(path.includes("map=1") ? MAPANS : LIST))); }};
(async () => {{
  IntelPage.open("");
  await flush();
  MAP.h.load.forEach((f) => f());
  const f0 = calls.fit;
  LIST = IN.d2; MAPANS = IN.m2;                       // two more placed events, the same filters: fitted to them
  await IntelPage.run(true); await flush();
  const grown = {{ fits: calls.fit - f0, pins: MAP.data.features.length }};
  MAP.h.movestart.forEach((f) => f({{ originalEvent: {{}} }}));     // the viewer pans
  LIST = IN.d3; MAPANS = IN.m3;
  await IntelPage.run(true); await flush();
  const kept = {{ fits: calls.fit - f0, pins: MAP.data.features.length }};
  IntelPage.open("level=log");                         // new filters: fitted whatever the viewer did
  await flush();
  console.log(JSON.stringify({{ first: f0, grown, kept, filt: calls.fit - f0 }}));
}})();
"""
    out = run_node(script, {"d1": d1, "m1": m1, "d2": d2, "m2": m2, "d3": d3, "m3": m3})
    assert out["first"] == 1
    assert out["grown"] == {"fits": 1, "pins": 3}
    assert out["kept"] == {"fits": 1, "pins": 4}
    assert out["filt"] == 2


@needs_node
def test_the_range_chip_the_picks_and_the_older_row(site):
    cad, clock = site
    clock.t = T0
    save(cad, first_at=T0 - 60, last_at=T0 - 60)
    for i in range(3):
        save(cad, first_at=T0 - (3 + i) * DAY, last_at=T0 - (3 + i) * DAY)
    d24, d7, dall = listing(cad), listing(cad, range="7d"), listing(cad, range="all")
    script = f"""
{MAPSTUB}
{PAGE}
{(STATIC / "intel-page.js").read_text()}
const reqs = [];
let ANS = IN.d24;
global.api = (meth, path) => {{ reqs.push(path); return Promise.resolve(JSON.parse(JSON.stringify(ANS))); }};
const lastRow = () => {{ const rs = $("#il-rows").children; return rs[rs.length - 1]; }};
const pressed = () => $("#il-picks").children.filter((b) => b.attrs["aria-pressed"] === "true").map((b) => b.dataset.range);
(async () => {{
  IntelPage._mp.on = false;
  IntelPage.open("");
  await flush();
  const day = {{ chip: $("#il-range").textContent, picks: pressed(), older: lastRow().textContent, req: reqs[0] }};
  // the older row's All: the person's pick, every event kept
  ANS = IN.dall;
  location.hash = "#intel";
  lastRow().children[0].children.find((c) => c.tagName === "BUTTON").on.click[0]();
  await flush();
  const all = {{ chip: $("#il-range").textContent, picks: pressed(), stored: localStorage.getItem("{'sizeup.intelRange'}"),
                rows: $("#il-rows").children.length, req: reqs[reqs.length - 1], hash: location.hash }};
  // a later open with no range in the hash: the person's pick; the hash's own wins
  ANS = IN.dall;
  IntelPage.open("");
  await flush();
  const again = reqs[reqs.length - 1];
  ANS = IN.d7;
  IntelPage.open("range=7d");
  await flush();
  console.log(JSON.stringify({{ day, all, again, seven: {{ chip: $("#il-range").textContent, picks: pressed(), req: reqs[reqs.length - 1] }} }}));
}})();
"""
    out = run_node(script, {"d24": d24, "d7": d7, "dall": dall})
    assert out["day"]["chip"] == "Last 24 h" and out["day"]["picks"] == ["24h"]
    assert out["day"]["older"].startswith("Older: 3") and "range=24h" in out["day"]["req"]
    assert out["all"] == {"chip": "All", "picks": ["all"], "stored": "all", "rows": 4, "req": out["all"]["req"], "hash": "#intel"}
    assert "range=all" in out["all"]["req"]
    assert "range=all" in out["again"]
    assert out["seven"]["chip"] == "Last 7 d" and out["seven"]["picks"] == ["7d"] and "range=7d" in out["seven"]["req"]


@needs_node
def test_a_later_page_holds_the_first_pages_newest_id(site):
    cad, clock = site
    clock.t = T0
    for i in range(60):
        save(cad, first_at=T0 - i * 60, last_at=T0 - i * 60)
    p0 = listing(cad)
    script = f"""
{MAPSTUB}
{PAGE}
{(STATIC / "intel-page.js").read_text()}
const reqs = [];
global.api = (meth, path) => {{ reqs.push(path); return Promise.resolve(JSON.parse(JSON.stringify(IN.p0))); }};
(async () => {{
  IntelPage._mp.on = false;
  IntelPage.open("");
  await flush();
  IntelPage.open("page=1");
  await flush();
  IntelPage.open("level=log&page=1");                  // other filters: not the other list's id
  await flush();
  console.log(JSON.stringify({{ reqs }}));
}})();
"""
    reqs = run_node(script, {"p0": p0})["reqs"]
    assert "upto" not in reqs[0] and f"upto={p0['upto']}" in reqs[1] and "page=1" in reqs[1]
    assert "level=log" in reqs[2] and "upto" not in reqs[2]


# -- the map's height (a browser; skipped where Playwright isn't installed) ---------------------------------------------------

def test_in_a_browser_the_map_is_resized_by_its_edge_and_its_pins_at_one_point_badged(tmp_path, monkeypatch):
    import threading
    import time

    import numpy as np

    from sizeup import sourcekeys
    from sizeup.cad import dispatcher
    from sizeup.cad.api import CadApi
    from sizeup.intel.model import Event
    from sizeup.web.server import WebServer
    from tests.test_cad import make_cad
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    monkeypatch.setattr(sourcekeys, "may_see_context", lambda broadcast_ok=False: True)
    cad = make_cad(tmp_path)
    now = time.time()
    for i, (lat, lon) in enumerate(((40.985, -73.68), (40.985, -73.68), (41.0, -73.66))):
        cad.intel.events.save(Event(
            pack="police", category="weapons", subject=f"police:weapons:{i}", source_key="p25:mrrs:7701", system="mrrs",
            service="police", first_at=now - 60 * (i + 1), last_at=now - 60 * (i + 1), until=now + 3600, status="open",
            what_score=0.7, level="medium", rules=[["pd.shots", 1, 0.7]], mode="map", updated_at=now, traffic_ids=["c1"],
            precision="street", lat=lat, lon=lon, radius_m=0.0, confirmed=True, label_full="Example Road", town="Rye"))
    scene = {"timezone": "America/New_York", "home": {"center": [-73.666, 41.002], "zoom": 14, "bounds": [-73.7, 40.98, -73.63, 41.03]},
             "glyphs": "http://127.0.0.1:9/{fontstack}/{range}.pbf", "firehouses": [], "categories": {}}
    ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda iid: False, cad=CadApi(cad))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
            pg = browser.new_page(viewport={"width": 1440, "height": 1000})
            pg.set_default_timeout(20000)
            pg.route(lambda url: not url.startswith(base), lambda route: route.abort())
            for path in ("roads", "weather"):
                pg.route(f"**/api/cad/{path}*", lambda route: route.fulfill(status=200, body="{}", content_type="application/json"))
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "/cad#intel")
            if not pg.evaluate("() => !!document.createElement('canvas').getContext('webgl2')"):
                pytest.skip("no WebGL in this Chromium")
            pg.wait_for_function("() => IntelPage._mp.ready && IntelPage._mp.m.isSourceLoaded('il-pins') "
                                 "&& IntelPage._mp.m.queryRenderedFeatures({ layers: ['il-pin'] }).length === 3")
            # every pin in view (fitted), the point with two badged once
            assert pg.evaluate("() => { const m = IntelPage._mp.m, b = m.getBounds(); return IntelPage._mp.data.pins"
                               ".every((p) => b.contains([p.lon, p.lat])); }")
            assert pg.evaluate("() => IntelPage._mp.m.queryRenderedFeatures({ layers: ['il-n'] }).length") == 1
            assert pg.locator("#il-range").text_content() == "Last 24 h"
            hd = pg.locator("#il-map-h")
            h0 = pg.evaluate("() => document.getElementById('il-mapbox-in').getBoundingClientRect().height")
            assert hd.get_attribute("role") == "separator" and hd.get_attribute("aria-orientation") == "horizontal"
            # the keys: 16 px a press, remembered; the canvas follows
            hd.focus()
            for _ in range(3):
                pg.keyboard.press("ArrowDown")
            h1 = pg.evaluate("() => document.getElementById('il-mapbox-in').getBoundingClientRect().height")
            assert abs(h1 - (h0 + 48)) <= 1
            assert json.loads(pg.evaluate("() => localStorage.getItem('sizeup.intelMapH')"))["h"] == round(h1)
            pg.wait_for_function(f"() => Math.abs(IntelPage._mp.m.getCanvas().clientHeight - {h1 - 2}) <= 2")
            # a drag up past the least: held at H_MIN
            b = hd.bounding_box()
            x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
            pg.mouse.move(x, y)
            pg.mouse.down()
            pg.mouse.move(x, 5, steps=8)
            pg.mouse.up()
            h2 = pg.evaluate("() => document.getElementById('il-mapbox-in').getBoundingClientRect().height")
            assert abs(h2 - pg.evaluate("() => IntelPage.H_MIN")) <= 1
            # kept over a reload; Home puts the layout's own back
            pg.reload()
            pg.wait_for_function("() => IntelPage._mp.ready")
            assert abs(pg.evaluate("() => document.getElementById('il-mapbox-in').getBoundingClientRect().height") - h2) <= 1
            pg.locator("#il-map-h").focus()
            pg.keyboard.press("Home")
            assert abs(pg.evaluate("() => document.getElementById('il-mapbox-in').getBoundingClientRect().height") - h0) <= 1
            assert pg.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            browser.close()
    finally:
        ws.shutdown()


def test_the_map_handle_is_the_consoles_separator():
    html = (STATIC / "cad.html").read_text()
    view = html[html.index('<div id="intel-view"'):html.index('<div id="prealert-view"')]
    assert '<div id="il-map-h" class="dmap-handle dmap-h"></div>' in view
    assert view.index('id="il-count"') < view.index('id="il-range"') < view.index('id="il-picks"')
    page = (STATIC / "intel-page.js").read_text()
    assert "Panels.separator(" in page and 'orient: "horizontal"' in page
    assert "var(--il-map-h," in (STATIC / "cad.css").read_text()
