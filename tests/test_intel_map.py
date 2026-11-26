"""The Intel page's map (#937; sizeup/intel/eventlog.py IntelLog.map, GET /api/cad/intel/list?map=1, static/intel-map.js,
static/intel-page.js).

Admin request: "the Intel page should have a map". Every placed event of the list's filters, all pages, the newest
MAP_MAX; the unplaced counted, never pinned; pins by level (colour) and category (glyph), an unconfirmed place hollow
with its radius as a ring; a pin's click selects its row and the other way round; read only when the list's answer
changed, drawn only when the pins differ (the performance session's frame rules: nothing per frame, no polling redraw).

Events come from the engine on the test_intel transcripts where the path is the engine's own, and otherwise in the
engine's exact shape (model.Event, SqlEvents.save). Invented places only (Sample St & Test Rd, 12 Example Road)."""

from __future__ import annotations

import json
import re

import pytest

from sizeup.cad.api import CadApi
from sizeup.intel import eventlog
from tests.test_intel_page import STATIC, T0, get, heard, listing, needs_node, run_node, save, site  # noqa: F401
from tests.test_roles import CASEY, JORDAN, VAL, call


def pins(cad, who=VAL, **q):
    return listing(cad, who, map=1, **q)


# -- the API -------------------------------------------------------------------------------------------------------------

def test_the_map_is_every_placed_event_of_the_filters_on_every_page(site):
    cad, clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)          # the engine's own: an intersection, pushed
    clock.t = T0 + 3600
    placed = [save(cad, first_at=T0 - 60 * (i + 1), precision="address", label_full="12 Example Road, Rye", town="Rye",
                   lat=41.0 + i / 1000, lon=-73.7, radius_m=25.0, confirmed=bool(i % 2)).id for i in range(70)]
    unplaced = [save(cad, first_at=T0 - 30 - i, precision="none").id for i in range(3)]
    m = pins(cad)
    assert m["placed"] == 71 and m["unplaced"] == 3 and m["limit"] == eventlog.MAP_MAX
    got = [p["id"] for p in m["pins"]]
    assert len(got) == 71 and not set(got) & set(unplaced) and set(placed) <= set(got)      # every page, never unplaced
    assert got == sorted(got, key=lambda i: -cad.store.one("SELECT first_at FROM intel_events WHERE id = ?", (i,))["first_at"])
    first = m["pins"][0]
    assert first == {"id": first["id"], "lat": 41.01, "lon": -73.67, "precision": "intersection", "radius_m": 40,
                     "confirmed": True, "level": "push", "category": "weapons", "label": "Weapon / shots", "held": ""}
    p = next(x for x in m["pins"] if x["id"] == placed[0])
    assert (p["confirmed"], p["level"], p["held"], p["radius_m"]) == (False, "log", "rules log", 25)
    assert set(p) == {"id", "lat", "lon", "precision", "radius_m", "confirmed", "level", "category", "label", "held"}
    assert listing(cad)["total"] == 74 and len(listing(cad)["events"]) == eventlog.PAGE_SIZE    # the list: paged


def test_the_map_takes_the_lists_filters(site):
    cad, _clock = site
    a = save(cad, mode="push", pushed_at=T0, precision="address", lat=41.0, lon=-73.7, label_full="12 Example Road, Rye")
    b = save(cad, category="edp", mode="map", precision="block", lat=41.01, lon=-73.71, label_full="Example Road, Rye")
    c = save(cad, mode="log", suppressed="out_of_area", precision="address", lat=41.02, lon=-73.72)
    save(cad, mode="log", suppressed="unplaced", precision="none")
    ids = lambda **q: {p["id"] for p in pins(cad, **q)["pins"]}        # noqa: E731
    assert ids() == {a.id, b.id, c.id}
    assert ids(level="push") == {a.id} and ids(category="edp") == {b.id} and ids(held="out_of_area") == {c.id}
    assert (pins(cad, held="unplaced")["pins"], pins(cad, held="unplaced")["unplaced"]) == ([], 1)
    old = __import__("datetime").datetime.fromtimestamp(T0 - 3 * 86400, __import__("sizeup.cad.store", fromlist=["TZ"]).TZ)
    assert ids(**{"from": old.strftime("%Y-%m-%d"), "to": old.strftime("%Y-%m-%d")}) == set()


def test_the_map_is_bounded_and_says_so(site, monkeypatch):
    cad, _clock = site
    for i in range(6):
        save(cad, first_at=T0 - i, precision="address", lat=41.0, lon=-73.7)
    monkeypatch.setattr(eventlog, "MAP_MAX", 4)
    m = pins(cad)
    assert len(m["pins"]) == 4 and m["placed"] == 6 and m["limit"] == 4


def test_the_map_reads_the_first_at_index(site, monkeypatch):
    cad, _clock = site
    save(cad, precision="address", lat=41.0, lon=-73.7)
    seen = []
    real = cad.store.q
    real1 = cad.store.one
    monkeypatch.setattr(cad.store, "q", lambda sql, args=(): (seen.append((sql, args)), real(sql, args))[1])
    monkeypatch.setattr(cad.store, "one", lambda sql, args=(): (seen.append((sql, args)), real1(sql, args))[1])
    pins(cad, held="quiet")
    reads = list({sql: (sql, args) for sql, args in seen if "intel_events e" in sql}.values())
    assert len(reads) == 2                                             # the counts, the pins
    for sql, args in reads:
        plan = " ".join(r["detail"] for r in real(f"EXPLAIN QUERY PLAN {sql}", args))
        assert "intel_events_first" in plan and "SCAN e " not in plan + " ", sql


def test_the_map_is_for_whoever_reads_the_list(site):
    cad, _clock = site
    save(cad, precision="address", lat=41.0, lon=-73.7)
    api = CadApi(cad)
    assert get(api, "/api/cad/intel/list", None, "map=1") == (403, {"error": "radio pre-alerts need a sign-in (Cloudflare Access)"})
    for who in (CASEY, JORDAN, VAL):
        code, d = get(api, "/api/cad/intel/list", who, "map=1")
        assert code == 200 and len(d["pins"]) == 1


def test_a_list_row_has_its_place_and_the_list_its_revision(site):
    cad, _clock = site
    save(cad, precision="address", lat=41.0, lon=-73.7, radius_m=25.0, label_full="12 Example Road, Rye")
    save(cad, first_at=T0 - 5, precision="none", lat=None, lon=None)
    d = listing(cad)
    a, b = d["events"]
    assert (a["lat"], a["lon"], a["radius_m"]) == (41.0, -73.7, 25.0) and (b["lat"], b["lon"]) == (None, None)
    rev = d["map_rev"]
    assert call(CadApi(cad), f"/api/cad/intel/{a['id']}/feedback", {"verdict": "useful"}, VAL)[0] == 200
    cad.intel.events.delivered(a["id"], {"ok": True, "at": T0})
    assert listing(cad)["map_rev"] == rev                               # a vote, a delivery: the map as it was
    save(cad, first_at=T0 - 9)
    assert listing(cad)["map_rev"] == rev + 1
    assert call(CadApi(cad), f"/api/cad/intel/{a['id']}/dismiss", {}, JORDAN)[0] == 200
    assert listing(cad)["map_rev"] == rev + 2


# -- the page ------------------------------------------------------------------------------------------------------------

def test_the_map_is_on_the_page_with_its_toggle_and_searchs_map():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/map-style.js"') < html.index('src="/static/intel-map.js"') < html.index('src="/static/intel-page.js"')
    view = html[html.index('<div id="intel-view"'):html.index('<div id="prealert-view"')]
    assert view.index('id="il-map-btn"') < view.index('id="il-mapbox"') < view.index('id="il-rows"')
    assert re.search(r'<div id="il-mapbox" class="il-mapwrap" hidden>', view)
    page = (STATIC / "intel-page.js").read_text()
    assert "plainMap(" in page and "plainMap(" in (STATIC / "search-view.js").read_text()     # Search's map, one maker
    assert (STATIC / "map-style.js").read_text().count("function plainMap(") == 1
    assert 'matchMedia("(max-width: 640px)")' in page                 # a phone: the map starts hidden
    for m in re.finditer(r"localStorage\.\w+Item", page):
        before = page[:m.start()]
        assert before.rfind("try {") > before.rfind("} catch"), m.group(0)
    for src in (page, (STATIC / "intel-map.js").read_text()):         # nothing per frame, no timer of its own
        assert "requestAnimationFrame" not in src
    assert page.count("setInterval(") == 1                            # (the list's poll, #906)
    assert "setInterval" not in (STATIC / "intel-map.js").read_text()


MAPSTUB = r"""
const calls = { get: [], setData: 0, ease: [], fit: 0, sel: [], scrolled: [], images: 0 };
global.matchMedia = () => ({ matches: false });
global.localStorage = { getItem: () => null, setItem() {} };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
document.documentElement = {};
const mk = document.createElement;
document.createElement = (t) => {
  if (t !== "canvas") return mk(t);
  return { width: 0, height: 0, getContext: () => new Proxy({}, { get: (o, k) => (k === "getImageData" ? () => ({ w: 32 }) : k in o ? o[k] : () => {}),
                                                                 set: (o, k, v) => { o[k] = v; return true; } }) };
};
class FakeMap {
  constructor(o) { this.h = {}; this.lh = {}; this.z = 12; this.src = null; global.MAP = this; this.container = o.container; }
  addControl() {}
  on(t, a, b) { if (b) (this.lh[t + ":" + a] = this.lh[t + ":" + a] || []).push(b); else (this.h[t] = this.h[t] || []).push(a); }
  hasImage() { return false; }
  addImage() { calls.images++; }
  addSource(id) { this.src = { setData: (d) => { calls.setData++; this.data = d; } }; }
  getSource() { return this.src; }
  addLayer() {}
  setFilter(id, f) { calls.sel.push(f[2]); }
  easeTo(o) { calls.ease.push(o.center); }
  fitBounds() { calls.fit++; }
  getZoom() { return this.z; }
  getCanvas() { return { style: {} }; }
  resize() {}
}
global.maplibregl = { Map: FakeMap, NavigationControl: class {}, LngLatBounds: class { extend() {} } };
const flush = () => new Promise((r) => setTimeout(r, 5));
"""


@needs_node
def test_a_pin_selects_its_row_and_a_row_its_pin_and_nothing_is_read_again_without_a_change(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    save(cad, first_at=T0 - 60, precision="address", lat=41.0, lon=-73.7, radius_m=25.0, confirmed=False,
         label_full="12 Example Road, Rye", town="Rye")
    save(cad, first_at=T0 - 120, precision="none")
    d, m = listing(cad), pins(cad)
    m["pins"].append({**m["pins"][1], "id": 999999})                  # a pin of another page
    assert call(CadApi(cad), f"/api/cad/intel/{d['events'][1]['id']}/feedback", {"verdict": "useful"}, VAL)[0] == 200
    voted = listing(cad)                                                # the hub's answer after a vote
    save(cad, first_at=T0 - 180, precision="none")
    more = listing(cad)                                                 # ... after a new (unplaced) event
    filtered, mapf = listing(cad, level="push"), pins(cad, level="push")
    script = f"""
{MAPSTUB}
{(STATIC / "map-style.js").read_text()}
{(STATIC / "intel-map.js").read_text()}
{(STATIC / "intel-page.js").read_text()}
global.cad.scene.home = {{ center: [-73.66, 41.0], zoom: 13 }};
let LIST = IN.list, MAPANS = IN.map, FAIL = false;
global.api = (meth, path) => {{
  calls.get.push(path);
  if (FAIL && path.includes("map=1")) return Promise.reject(new Error("boom"));
  return Promise.resolve(JSON.parse(JSON.stringify(path.includes("map=1") ? MAPANS : LIST)));
}};
const maps = () => calls.get.filter((p) => p.includes("map=1")).length;
(async () => {{
  IntelPage.open("");
  await flush();
  const before = {{ maps: calls.get.filter((p) => p.includes("map=1")).length, made: !!global.MAP }};
  MAP.h.load.forEach((f) => f());
  const feats = MAP.data.features.map((f) => f.properties);
  const body = $("#il-rows");
  for (const tr of body.children) {{ tr.scrollIntoView = () => calls.scrolled.push(tr.dataset.id); tr.focus = () => {{}}; }}
  const [pushed, unconf, unplaced] = body.children;
  // a pin's click: its row selected and scrolled to
  MAP.lh["click:il-pin"][0]({{ features: [{{ properties: {{ id: Number(unconf.dataset.id) }} }}] }});
  const pin = {{ sel: unconf.classList.contains("il-sel"), scrolled: calls.scrolled.slice(), filter: calls.sel.slice(-1)[0] }};
  // a row's click: its pin selected and centred; again: its More info
  pushed.on.click[0]();
  const row = {{ sel: pushed.classList.contains("il-sel"), was: unconf.classList.contains("il-sel"), ease: calls.ease.slice(),
                filter: calls.sel.slice(-1)[0], hash: location.hash }};
  pushed.on.click[0]();
  const again = location.hash;
  location.hash = "";
  unplaced.on.click[0]();                                              // unplaced: no pin, its More info at once
  const nopin = location.hash;
  // a pin of another page: selected, its More info a link away
  MAP.lh["click:il-pin"][0]({{ features: [{{ properties: {{ id: 999999 }} }}] }});
  const off = {{ hidden: $("#il-map-sel").hidden, href: $("#il-map-sel").href }};
  // the page's own reads: a poll with the same answer, then the hub's answer after a vote: no map read, nothing drawn
  const g0 = maps(), s0 = calls.setData, f0 = calls.fit;
  await IntelPage.run(true);
  LIST = IN.voted;
  await IntelPage.run(true);
  // More info and back: the list read, the map not
  IntelPage.close();
  IntelPage.open("");
  await flush();
  const quiet = {{ gets: maps() - g0, sets: calls.setData - s0, voted: body.children[1].find("il-n").map((x) => x.textContent) }};
  LIST = IN.more;                                                      // a new event: read again
  await IntelPage.run(true);
  await flush();
  const moved = {{ gets: maps() - g0, sets: calls.setData - s0, fits: calls.fit - f0 }};
  // other filters: one map read, fitted to them, the selection gone
  LIST = IN.filtered; MAPANS = IN.mapf;
  const g1 = maps();
  IntelPage.open("level=push");
  await flush();
  const filt = {{ gets: maps() - g1, fits: calls.fit - f0, link: $("#il-map-sel").hidden, sel: calls.sel.slice(-1)[0],
                 pins: MAP.data.features.length }};
  // a failed read: no pins under the error; Refresh reads it again
  FAIL = true; LIST = IN.list; MAPANS = IN.map;
  IntelPage.open("");
  await flush();
  const failed = {{ n: $("#il-map-n").textContent, pins: MAP.data.features.length }};
  FAIL = false;
  const g2 = maps();
  $("#il-refresh").on.click[0]();
  await flush();
  const retried = {{ gets: maps() - g2, pins: MAP.data.features.length }};
  console.log(JSON.stringify({{ before, feats, pin, row, again, nopin, off, quiet, moved, filt, failed, retried, n: $("#il-map-n").textContent,
                                ids: body.children.map((r) => Number(r.dataset.id)), images: calls.images }}));
}})();
"""
    out = run_node(script, {"list": d, "map": m, "voted": voted, "more": more, "filtered": filtered, "mapf": mapf})
    ids = out["ids"]
    assert out["before"] == {"maps": 1, "made": True}
    assert out["images"] == 2 * 10                                     # each glyph, light and dark, once
    f = {p["id"]: p for p in out["feats"]}
    assert set(f) == {ids[0], ids[1], 999999}                          # placed only
    assert (f[ids[0]]["h"], f[ids[0]]["r"], f[ids[0]]["k"]) == (0, 0, "im-police-d")         # confirmed: filled
    assert (f[ids[1]]["h"], f[ids[1]]["r"], f[ids[1]]["k"]) == (1, 25, "im-police")          # unconfirmed: hollow, ring
    assert out["pin"] == {"sel": True, "scrolled": [str(ids[1])], "filter": ids[1]}
    assert out["row"]["sel"] is True and out["row"]["was"] is False and out["row"]["filter"] == ids[0]
    assert out["row"]["ease"] == [[-73.67, 41.01]] and out["row"]["hash"] == ""
    assert out["again"] == f"#prealert/intel/{ids[0]}" and out["nopin"] == f"#prealert/intel/{ids[2]}"
    assert out["off"] == {"hidden": False, "href": "#prealert/intel/999999"}
    assert out["quiet"] == {"gets": 0, "sets": 0, "voted": ["1"]}       # (the vote on its row)
    assert out["moved"] == {"gets": 1, "sets": 0, "fits": 0}           # read again; the same pins: not drawn, view kept
    assert out["filt"] == {"gets": 1, "fits": 1, "link": True, "sel": -1, "pins": 1}
    assert out["failed"] == {"n": "Map: boom", "pins": 0}
    assert out["retried"] == {"gets": 1, "pins": 3}
    assert out["n"] == "1 unplaced"


@needs_node
def test_the_pins_are_drawn_only_when_they_change():
    script = f"""
{MAPSTUB}
{(STATIC / "intel-map.js").read_text()}
const map = new maplibregl.Map({{ container: null }});
const h = IntelMap.attach(map, "t", {{}});
const a = [{{ id: 1, lat: 41, lon: -73.7, precision: "address", radius_m: 0, confirmed: true, level: "push", category: "fire_report" }},
           {{ id: 2, lat: 41.1, lon: -73.6, precision: "street", radius_m: 300, confirmed: false, level: "held", category: "zzz" }}];
const r = [h.set(a), h.set(JSON.parse(JSON.stringify(a))), h.set([a[0]]), h.set([a[0]])];
console.log(JSON.stringify({{ r, sets: calls.setData, k: IntelMap.feature(a[1]).properties.k, kf: IntelMap.feature(a[0]).properties.k }}));
"""
    out = run_node(script, {})
    assert out == {"r": [True, False, True, False], "sets": 2, "k": "im-other", "kf": "im-fire-d"}


def test_every_category_the_packs_name_has_a_glyph():
    src = (STATIC / "intel-map.js").read_text()
    kinds = dict(re.findall(r"(\w+): \"(\w+)\"", src[src.index("const KIND = {"):src.index("};", src.index("const KIND = {"))]))
    cats = set()
    for f in (STATIC.parent.parent.parent / "data" / "intel").glob("*.json"):
        cats |= set((json.loads(f.read_text()).get("categories") or {}))
    assert cats and cats <= set(kinds), sorted(cats - set(kinds))
    listed = re.search(r"const KINDS = \[([^\]]*)\]", src).group(1)
    assert set(kinds.values()) | {"other"} == set(re.findall(r'"(\w+)"', listed))


# -- in a browser (skipped where Playwright isn't installed) ----------------------------------------------------------------

def test_in_a_browser_a_pin_click_selects_its_row_and_a_row_its_pin(tmp_path, monkeypatch):
    """Real MapLibre (its layers' expressions checked by it), no tiles (nothing off this machine)."""
    import threading

    import numpy as np

    from sizeup import sourcekeys
    from sizeup.cad import dispatcher
    from sizeup.web.server import WebServer
    from tests.test_cad import make_cad
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    monkeypatch.setattr(sourcekeys, "may_see_context", lambda broadcast_ok=False: True)
    import time
    cad = make_cad(tmp_path)
    now = time.time()
    from sizeup.intel.model import Event
    for i, (lat, lon, conf) in enumerate(((41.0, -73.66, True), (41.004, -73.672, False))):
        cad.intel.events.save(Event(
            pack="police", category="weapons", subject=f"police:weapons:{i}", source_key="p25:mrrs:9211", system="mrrs",
            service="police", first_at=now - 60 * (i + 1), last_at=now - 60 * (i + 1), until=now + 3600, status="open",
            what_score=0.7, level="medium", rules=[["pd.shots", 1, 0.7]], mode="map", updated_at=now, traffic_ids=["c1"],
            precision="address", lat=lat, lon=lon, radius_m=25.0, confirmed=conf, label_full=f"{12 + i} Example Road, Rye",
            town="Rye"))
    scene = {"timezone": "America/New_York", "home": {"center": [-73.666, 41.002], "zoom": 14, "bounds": [-73.7, 40.98, -73.63, 41.03]},
             "glyphs": "http://127.0.0.1:9/{fontstack}/{range}.pbf", "firehouses": [], "categories": {}}     # (never fetched)
    ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda iid: False, cad=CadApi(cad))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])     # WebGL
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
            pg = browser.new_page(viewport={"width": 1440, "height": 1000})
            pg.set_default_timeout(20000)
            pg.route(lambda url: not url.startswith(base), lambda route: route.abort())
            for path in ("roads", "weather"):           # the Overview map's layers: not this test's (and slow here)
                pg.route(f"**/api/cad/{path}*", lambda route: route.fulfill(status=200, body="{}", content_type="application/json"))
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load" not in m.text else None)
            pg.goto(base + "/cad#intel")
            if not pg.evaluate("() => !!document.createElement('canvas').getContext('webgl2')"):
                pytest.skip("no WebGL in this Chromium")
            pg.wait_for_function("() => IntelPage._mp.ready && IntelPage._mp.m.isSourceLoaded('il-pins') "
                                 "&& IntelPage._mp.m.queryRenderedFeatures({ layers: ['il-pin'] }).length === 2")
            ids = pg.eval_on_selector_all("#il-rows tr.il-row", "rs => rs.map((r) => Number(r.dataset.id))")
            # the second row's pin, clicked where the map draws it
            box = pg.locator("#il-map").bounding_box()
            pt = pg.evaluate("() => IntelPage._mp.m.project([-73.672, 41.004])")
            pg.mouse.click(box["x"] + pt["x"], box["y"] + pt["y"])
            pg.wait_for_function(f"() => document.querySelector('#il-rows tr[data-id=\"{ids[1]}\"]').classList.contains('il-sel')")
            # the first row's click: its pin selected and centred, the second row no longer
            pg.locator(f'#il-rows tr[data-id="{ids[0]}"]').click()
            pg.wait_for_function("() => { const c = IntelPage._mp.m.getCenter(); return Math.abs(c.lng + 73.66) < 1e-4 "
                                 "&& Math.abs(c.lat - 41.0) < 1e-4; }")
            sel = pg.eval_on_selector_all("#il-rows tr.il-sel", "rs => rs.map((r) => Number(r.dataset.id))")
            assert sel == [ids[0]]
            assert pg.evaluate("() => JSON.stringify(IntelPage._mp.m.getFilter('il-sel'))") == json.dumps(["==", ["get", "id"], ids[0]], separators=(",", ":"))
            assert pg.locator("#il-map-n").text_content() == ""
            assert not [e for e in errors if "maplibre" in e.lower() or "layer" in e.lower() or "expression" in e.lower()], errors
            browser.close()
    finally:
        ws.shutdown()
