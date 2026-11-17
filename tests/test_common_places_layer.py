"""The maps' "Common places" layer: the department's located place names (the county's common places, hand-made ones)
as one point per spot (sizeup/cad/commonplaces.py map_points; GET /api/cad/places/map, cached, ETag, read only), drawn by
static/common-places.js as a group in every map's Layers box (dots from zoom 14, names from 16, a box on a click).

Invented data only (this repository is public): the synthetic gazetteer of tests/test_common_places.py (Example Ave,
Sample St) and invented names, imported through the real import (a spreadsheet, as the county's list comes)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sizeup.cad import commonplaces as cp
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.geo.places import PlaceIndex
from tests.test_cad import make_cad
from tests.test_common_places import HEADER, PC, b64, gaz, xlsx  # noqa: F401  (gaz: the fixture)
from tests.test_roles import VAL, people
from tests.test_settings import post

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
CODE = PC.upper().replace(" ", "_")             # the county's town code for the synthetic town
ROWS = [HEADER,
        ["120 EXAMPLE AVE", "SAMPLE FITNESS", CODE],
        ["120 EXAMPLE AVENUE", "EXAMPLE BURGER", CODE],          # two names at one address: one spot
        ["124 EXAMPLE AVE", "SAMPLE HALL", CODE],                # one site: two buildings next door
        ["128 EXAMPLE AVE", "SAMPLE HALL", CODE],
        ["7 NOWHERE RD", "QUIET GROVE", CODE]]                   # not found: no point, not on the map


@pytest.fixture()
def cad(tmp_path, gaz):  # noqa: F811
    c = make_cad(tmp_path, geocoder=lambda q: [])
    c.place_source = lambda: PlaceIndex(gaz)
    post(c, "common_places", action="apply", file=b64(xlsx(ROWS)), filename="list.xlsx")
    return c


def layer(cad, who=None):
    code, body, ctype, h = CadApi(cad).get("/api/cad/places/map", "", who)
    return code, json.loads(body), h


# --------------------------------------------------------------------------- the points

def test_one_point_per_spot_a_site_at_its_primary_with_all_its_addresses(cad):
    code, fc, h = layer(cad)
    assert code == 200 and fc["type"] == "FeatureCollection"
    by = {f["properties"]["label"]: f for f in fc["features"]}
    assert set(by) == {"Example Burger +1", "Sample Hall"}                 # no point: never on the map
    two = by["Example Burger +1"]["properties"]["places"]
    assert [(p["name"], p["addresses"], p["town"], p["hand"]) for p in two] == [
        ("Example Burger", ["120 Example Avenue"], PC, False), ("Sample Fitness", ["120 Example Ave"], PC, False)]
    hall = by["Sample Hall"]
    assert hall["properties"]["places"] == [{"name": "Sample Hall", "addresses": ["124 Example Ave", "128 Example Ave"],
                                             "town": PC, "hand": False}]
    primary = next(r for r in cad.local_places() if r["key"] == "county/sample hall @ 124 example ave")
    assert hall["geometry"]["coordinates"] == [round(primary["lon"], 5), round(primary["lat"], 5)]
    assert [f["id"] for f in fc["features"]] == list(range(len(fc["features"])))       # the page finds a click's by id
    assert h["ETag"].startswith('"cp-') and h["Cache-Control"] == "private, no-cache"


def test_a_hand_made_name_is_on_the_map_with_where_it_was_picked(cad):
    post(cad, "places", action="set", name="the Annex", place={"label": "140 Example Ave", "lat": 41.0002,
                                                                 "lon": -73.679, "sub": PC})
    f = next(f for f in layer(cad)[1]["features"] if f["properties"]["label"] == "the Annex")
    assert f["properties"]["places"] == [{"name": "the Annex", "addresses": ["140 Example Ave"], "town": PC, "hand": True}]


def test_one_name_twice_at_one_spot_is_listed_once():
    at = {"lat": 41.0, "lon": -73.68, "muni": PC}
    fc = cp.map_points([dict(at, key="county/sample deli @ 9 sample st", name="SAMPLE DELI", label="Sample Deli",
                             address="9 SAMPLE ST", source=cp.SOURCE),
                        dict(at, key="sample deli", name="Sample Deli", label="9 Sample St"),
                        dict(at, key="county/x @ 1 nowhere rd", name="X", lat=None, lon=None, address="1 NOWHERE RD")])
    (f,) = fc["features"]
    assert f["properties"]["label"] == "Sample Deli" and len(f["properties"]["places"]) == 1


def test_cached_until_the_names_change_and_the_state_says_when(cad):
    _c, fc, h = layer(cad)
    rev = cad.state()["places_rev"]
    assert layer(cad)[2]["ETag"] == h["ETag"] and cad.state()["places_rev"] == rev
    built = cad._places_map
    layer(cad)
    assert cad._places_map is built                                   # not built again
    post(cad, "places", action="set", name="the Annex", place={"label": "140 Example Ave", "lat": 41.0002,
                                                                 "lon": -73.679, "sub": PC})
    assert layer(cad)[2]["ETag"] != h["ETag"] and cad.state()["places_rev"] != rev
    post(cad, "common_places", action="remove")
    assert [f["properties"]["label"] for f in layer(cad)[1]["features"]] == ["the Annex"]


def test_the_version_is_read_without_copying_the_names(cad, monkeypatch):
    rev = cad.settings.places_rev()
    monkeypatch.setattr(cad.settings, "local_places", lambda: pytest.fail("copied the names"))
    assert cad.settings.places_rev() == rev
    assert cad.state()["places_rev"] == rev


def test_preplan_says_while_a_point_is_being_placed():
    src = (STATIC / "preplan.js").read_text()
    assert "const editing = () => !!st.edit;" in src and "cancelEdit, editing };" in src


def test_read_only_viewers_too_like_the_place_search(cad):
    people(cad)
    assert layer(cad, Identity(email=VAL))[0] == 200
    assert CadApi(cad).post("/api/cad/places/map", b"{}", Identity(email=VAL))[0] in (403, 404)


def test_the_304_round_trip_through_the_server(cad):
    from sizeup.web.server import WebServer
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda *_: None, lambda *_: False, cad=CadApi(cad))
    t = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.httpd.server_address[1]}/api/cad/places/map"
        with urllib.request.urlopen(url, timeout=5) as r:
            etag = r.headers["ETag"]
            assert r.status == 200 and json.loads(r.read())["features"]
        with pytest.raises(HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers={"If-None-Match": etag}), timeout=5)
        assert e.value.code == 304
    finally:
        srv.httpd.shutdown()
        srv.httpd.server_close()


# --------------------------------------------------------------------------- the page (node)

PAGE = r"""
const fs = require("fs");
global.window = global;
const IN = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.on = {}; this.className = "";
                     this._t = ""; this.title = ""; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  append(...xs) { this.children.push(...xs); }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  fire(t) { for (const f of this.on[t] || []) f({}); }
}
global.document = { createElement: (t) => new El(t) };
const listeners = {};
global.addEventListener = (t, f) => { (listeners[t] = listeners[t] || []).push(f); };
const rec = { fetches: 0, sets: [], vis: {}, info: [], closed: 0 };
global.fetch = async (url) => { rec.fetches++; rec.url = url; return { ok: true, json: async () => IN.fc }; };
function fakeMap(glyphs) {
  const on = {}, sources = {}, layers = {}, order = [];
  const m = {
    on(t, a, b) { const k = b ? `${t}:${a}` : t; (on[k] = on[k] || []).push(b || a); },
    once(t, f) { f(); }, emit(t, e) { for (const f of on[t] || []) f(e); },
    getSource: (id) => sources[id], addSource(id, s) { sources[id] = { spec: s, setData: (d) => rec.sets.push(d) }; },
    addLayer(l, before) { layers[l.id] = { ...l, before }; order.push(l.id); }, getLayer: (id) => layers[id],
    setLayoutProperty(id, k, v) { rec.vis[id] = v; },
    getStyle: () => ({ glyphs: glyphs ? "x" : undefined, layers: [{ id: "base-roads" }, { id: "cm-lines" }, { id: "firehouse" }] }),
    isStyleLoaded: () => true, loaded: () => true, getCanvas: () => ({ style: {} }), getZoom: () => 17,
    queryRenderedFeatures: () => m.hits || [], layers, order,
  };
  return m;
}
let group = null;
global.MapLayers = { addGroup: (g) => { group = g; } };
eval(fs.readFileSync(process.argv[2], "utf8"));
const set = {};
const control = (host, glyphs = true) => ({ map: fakeMap(glyphs), host, on: (k, d) => (k in set ? set[k] : d),
  setLayer(k, v) { set[k] = v; }, close() { rec.closed++; }, showInfo: (...a) => rec.info.push(a) });
const flush = () => new Promise((r) => setTimeout(r, 0));
(async () => {
  const c = control({ id: "d-map" });
  const m = group.attach(c);
  out.keys = m.keys();
  out.offAdded = Object.keys(c.map.layers);                 // off by default: nothing added, nothing asked
  out.offFetches = rec.fetches;
  // the Layers box: one checkbox, "Common places"
  const box = m.panel();
  const cb = box.children[0].children[0];
  out.panel = [box.className, cb.type, cb.dataset.key, cb.checked, box.children[0].children[1].textContent];
  cb.checked = true; cb.fire("change"); m.refresh();
  await flush();
  out.fetches = rec.fetches; out.url = rec.url;
  out.layers = c.map.order.map((id) => [id, c.map.layers[id].type, c.map.layers[id].minzoom, c.map.layers[id].before]);
  const lab = c.map.layers["cp-label"].layout;
  out.label = [lab["text-field"], lab["text-allow-overlap"] || false, lab["text-variable-anchor"]];
  out.vis = { ...rec.vis };
  out.drawn = rec.sets.length && rec.sets[rec.sets.length - 1].features.length;
  // a click on a dot: the box with each name and its addresses, after the map's own click
  c.map.hits = [{ id: 0, layer: { id: "cp-dot" } }];
  c.map.emit("click", { point: [1, 2], originalEvent: { target: { closest: () => null } } });
  await flush();
  out.info = rec.info.map((a) => [a[0], a[1], a[2].map((l) => [l.tagName, l.className, l.textContent]), a[3], a[4]]);
  // anything drawn over the dot keeps its click: a pre-plan point, a firehouse (topmost first); a base map label never
  out.over = {};
  const over = { preplan: { source: "pp-points", layer: { id: "pp-icons", type: "symbol" } },
                 firehouse: { source: "firehouses", layer: { id: "firehouse", type: "symbol" } },
                 camera: { source: "ml-cameras", layer: { id: "ml-cameras", type: "symbol" } },
                 fill: { source: "gis-parcels", layer: { id: "gis-parcels", type: "fill" } },
                 base: { source: "base", layer: { id: "poi-label", type: "symbol" } } };
  for (const [k, f] of Object.entries(over)) {
    rec.info = [];
    c.map.hits = [f, { id: 0, source: "cp-places", layer: { id: "cp-dot", type: "circle" } }];
    c.map.emit("click", { point: [1, 2], originalEvent: { target: { closest: () => null } } });
    await flush();
    out.over[k] = rec.info.length;
  }
  // a pre-plan point being placed: the map is the editor's
  rec.info = [];
  global.Preplan = { editing: () => true };
  c.map.hits = [{ id: 0, source: "cp-places", layer: { id: "cp-dot", type: "circle" } }];
  c.map.emit("click", { point: [1, 2], originalEvent: { target: { closest: () => null } } });
  await flush();
  out.placing = rec.info.length;
  global.Preplan = { editing: () => false };
  c.map.emit("click", { point: [1, 2], originalEvent: { target: { closest: () => null } } });
  await flush();
  out.notPlacing = rec.info.length;
  // a rig's or a pin's mark (a DOM marker) keeps its click
  rec.info = [];
  c.map.emit("click", { point: [1, 2], originalEvent: { target: { closest: (s) => (s === ".maplibregl-marker" ? {} : null) } } });
  await flush();
  out.markerClick = rec.info.length;
  // the new call's map: a click there is "what is here" (newcall-map.js), never this box
  const nc = control({ id: "nc-map" });
  group.attach(nc);
  nc.map.hits = [{ id: 0, layer: { id: "cp-dot" } }];
  nc.map.emit("click", { point: [1, 2], originalEvent: { target: { closest: () => null } } });
  await flush();
  out.callMapClick = rec.info.length;
  // the names change (the state's places_rev: cad.js fires "sizeup:places"): asked again; no glyphs: dots only
  const f0 = rec.fetches;
  for (const f of listeners["sizeup:places"] || []) f({});
  await flush();
  out.again = rec.fetches - f0;
  const g = control({ id: "y" }, false); group.attach(g).sync();
  out.noGlyphs = Object.keys(g.map.layers);
  // off again: hidden
  set.commonplaces = false; m.refresh();
  out.off = { ...rec.vis };
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_layer_in_a_fake_page(tmp_path, cad):
    fc = layer(cad)[1]
    h, d = tmp_path / "page.js", tmp_path / "in.json"
    h.write_text(PAGE)
    d.write_text(json.dumps({"fc": fc}))
    r = subprocess.run([NODE, str(h), str(STATIC / "common-places.js"), str(d)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["keys"] == [["commonplaces", False]]                       # in the event's set, off by default
    assert o["offAdded"] == [] and o["offFetches"] == 0
    assert o["panel"] == ["ml-group cp-group", "checkbox", "commonplaces", False, "Common places"]
    assert o["fetches"] == 1 and o["url"] == "/api/cad/places/map"
    # map layers under the rigs' lines: a dot from 14, the names from 16, collision on (never allow-overlap)
    assert o["layers"] == [["cp-dot", "circle", 14, "cm-lines"], ["cp-label", "symbol", 16, "cm-lines"]]
    assert o["label"] == [["get", "label"], False, ["top", "bottom", "left", "right"]]
    assert o["vis"] == {"cp-dot": "visible", "cp-label": "visible"} and o["drawn"] == len(fc["features"])
    first = fc["features"][0]["properties"]
    assert first["label"] == "Example Burger +1"
    assert o["info"] == [["2 places", PC, [["B", "cp-name", "Example Burger"], ["DIV", "", "120 Example Avenue"],
                                           ["B", "cp-name", "Sample Fitness"], ["DIV", "", "120 Example Ave"]],
                          "Common places", "cp-info"]]
    assert o["over"] == {"preplan": 0, "firehouse": 0, "camera": 0, "fill": 0, "base": 1}
    assert o["placing"] == 0 and o["notPlacing"] == 1
    assert o["markerClick"] == 0 and o["callMapClick"] == 0
    assert o["again"] == 1
    assert o["noGlyphs"] == ["cp-dot"]
    assert o["off"] == {"cp-dot": "none", "cp-label": "none"}


def test_the_pages_load_it_and_the_call_map_looks_through_it():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/maplayers.js"') < html.index('src="/static/common-places.js"') < \
        html.index('src="/static/cad.js"')
    js = (STATIC / "cad.js").read_text()
    assert 'window.dispatchEvent(new CustomEvent("sizeup:places", { detail: data.places_rev }));' in js
    nc = (STATIC / "newcall-map.js").read_text()
    assert re.search(r"!/\^\(nc\|cp\)-/\.test\(String\(f\.layer\.id\)\)", nc)
    src = (STATIC / "common-places.js").read_text()
    assert "maplibregl.Marker" not in src                                # map layers only, never DOM marks
