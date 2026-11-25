"""A call's building on its maps (sizeup/cad/footprints.py, static/bldg-highlight.js): the county footprint(s) of the
address, the address range or the named place, lit whole.

Everything here is invented: an "Example St" and a "Sample Ave" in an invented "Sampletown", a "Sample Plaza"; the
footprints are rectangles laid out in metres from an origin. The shapes are the data's: the county buildings layer's
footprints (Polygon, no address, no feature id: as the hub caches them), its tax parcels (PARCEL_ADDR) and the
gazetteer's address points (one per street, number, letter and town: _Street.addrs {muni: [(number, letter, lat, lon)]}).

The street (Example St) runs east-west through the origin: the even numbers on its north side, the odd on its south.

    40-44   one building (A) with three address points in it          41   across the street (C)
    46      a house (B) next to it                                    47   a house (E), its point in it
    48      its point in the front yard, 7 m from its house (D)        49   its point near the centreline; across the
                                                                           street a footprint (F) with no point, 11 m
    52      a house (G) with its point in it                               away, faces the even side: never 49's
    60      no address point: the pin in a footprint (K)
    100 Sample Ave   a campus: one lot (parcel "100 SAMPLE AVE"), three large buildings (H1 holds the point), a shed, and a
                     building with an address of its own (104: never the campus's, and the campus never its)
"""
import json
import math
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sizeup.cad import footprints as fpm
from sizeup.gis import DEFAULT_LAYERS, Gis, geometry_bbox
from tests.test_cad import make_cad

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")

LAT0, LON0 = 40.95, -73.75
KX = 111_320.0 * math.cos(math.radians(LAT0))
KY = 110_574.0
TOWN = "Sampletown"


def ll(x, y):
    """Metres east / north of the origin -> (lat, lon)."""
    return round(LAT0 + y / KY, 7), round(LON0 + x / KX, 7)


def rect(cx, cy, w, h):
    pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
    ring = [[ll(x, y)[1], ll(x, y)[0]] for x, y in pts]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def bldg(cx, cy, w, h):
    """A county footprint as the hub keeps it (the configured fields, no feature id)."""
    return {"type": "Feature", "properties": {"ACTUAL_HEIGHT": 30, "PHOTO_DATE": 1364774400000}, "geometry": rect(cx, cy, w, h)}


def parcel(addr, cx, cy, w, h):
    return {"type": "Feature", "properties": {"PARCEL_ADDR": addr, "MUNI_NAME": TOWN}, "geometry": rect(cx, cy, w, h)}


BUILDINGS = {
    "A": bldg(0, 25, 40, 20),          # 40-44
    "B": bldg(40, 25, 12, 12),         # 46
    "C": bldg(0, -25, 14, 14),         # 41
    "D": bldg(70, 25, 12, 12),         # 48's house (its point in the yard)
    "E": bldg(70, -25, 12, 12),        # 47
    "F": bldg(130, 14, 12, 12),        # no address point: across from 49
    "G": bldg(150, 25, 12, 12),        # 52
    "K": bldg(230, 25, 12, 12),        # 60's house (60 has no address point)
    # the campus: 100 Sample Ave (north of the street, x 400 ...)
    "H1": bldg(400, 60, 30, 20), "H2": bldg(440, 60, 20, 20), "H3": bldg(420, 95, 40, 15),
    "S": bldg(460, 90, 6, 6),          # a shed: too small to be the campus's
    "H4": bldg(380, 95, 20, 20),       # 104 Sample Ave: on the lot, but its own address
}
PARCELS_ = [parcel("100 SAMPLE AVE", 420, 75, 120, 70), parcel("40 EXAMPLE ST", 0, 30, 60, 40)]

POINTS = {   # (street, number, letter) -> metres
    ("Example St", 40, ""): (-12, 20), ("Example St", 42, ""): (0, 20), ("Example St", 44, ""): (12, 20),
    ("Example St", 46, ""): (40, 22), ("Example St", 41, ""): (0, -22), ("Example St", 47, ""): (70, -24),
    ("Example St", 48, ""): (70, 12), ("Example St", 49, ""): (130, -3), ("Example St", 52, ""): (150, 22),
    ("Example St", 50, ""): (100, 30),
    ("Sample Ave", 100, ""): (400, 60), ("Sample Ave", 104, ""): (380, 95),
}


@dataclass
class St:
    id: int
    key: str
    name: str
    addrs: dict = field(default_factory=dict)


class Gaz:
    """The gazetteer as footprints.py reads it: its streets and their address points."""

    def __init__(self, points=POINTS):
        self.streets = {1: St(1, "example st", "Example St"), 2: St(2, "sample ave", "Sample Ave")}
        by = {"Example St": 1, "Sample Ave": 2}
        for (street, num, suf), (x, y) in points.items():
            lat, lon = ll(x, y)
            self.streets[by[street]].addrs.setdefault(TOWN, []).append((num, suf, lat, lon))


def set_layer(g, key, feats):
    g.data[key] = feats
    g._boxes[key] = [geometry_bbox(f["geometry"]) for f in feats]
    g.status[key].update(at=1.8e9, n=len(feats))
    with g._lock:
        g._changed(key)


def fake_gis(buildings=None, parcels=None):
    g = Gis(layers=tuple(l for l in DEFAULT_LAYERS if l.key in ("buildings", "parcels")), load=False)
    g.cache_loaded = True
    set_layer(g, "buildings", [json.loads(json.dumps(f)) for f in (buildings or BUILDINGS).values()])
    set_layer(g, "parcels", [json.loads(json.dumps(f)) for f in (PARCELS_ if parcels is None else parcels)])
    return g


@pytest.fixture
def cad(tmp_path):
    c = make_cad(tmp_path)
    c.gis = fake_gis()
    c.footprints.gaz = Gaz()
    return c


def which(c, b):
    """The fixture's names of the footprints a call's building is ({"A"}, ...)."""
    if b is None:
        return set()
    geo = {json.dumps(f["geometry"], sort_keys=True): k for k, f in BUILDINGS.items()}
    return {geo.get(json.dumps(f["geometry"], sort_keys=True), f["ref"]) for f in b["footprints"]}


def call(c, address, at=None, **kw):
    lat, lon = ll(*at) if at else (None, None)
    iid = c.store.create_incident(type="STRU", address=address, lat=lat, lon=lon, **kw)
    return next(i for i in c.state()["incidents"] if i["id"] == iid)


# -- the house numbers ----------------------------------------------------------------------------------------------------

def test_house_numbers_and_their_short_line(monkeypatch):
    assert fpm.house_numbers("40") == [(40, "")]
    assert fpm.house_numbers("40a") == [(40, "a")]
    assert fpm.house_numbers("12 1/2") == [] and fpm.house_numbers("") == []
    for mod in (fpm._houserange, None):                     # the range reader when it is here, and the seam without it
        monkeypatch.setattr(fpm, "_houserange", mod)
        assert fpm.house_numbers("40-44") == [(40, ""), (42, ""), (44, "")]
        assert fpm.house_numbers("10-200")[:1] == [(10, "")] and len(fpm.house_numbers("10-200")) == 1   # a block: its first
        assert fpm.house_numbers("15-16") == [(15, "")]
    assert fpm.compress(["40", "42", "44"]) == "40-44"
    assert fpm.compress(["41", "40"]) == "40, 41"
    assert fpm.compress(["12", "12A"]) == "12, 12A"


# -- an address ------------------------------------------------------------------------------------------------------------

def test_one_address_of_a_building_with_several_lights_the_whole_building(cad):
    inc = call(cad, "42 Example St", at=(0, 20))
    b = inc["building"]
    assert b["how"] == "address" and which(cad, b) == {"A"}
    assert b["footprints"][0]["addrs"] == "40-44 Example St"
    assert b["footprints"][0]["geometry"] == BUILDINGS["A"]["geometry"]      # drawn from the push at any zoom
    assert b["footprints"][0]["ref"].startswith("buildings/@")
    # any of its numbers: the same building
    assert which(cad, call(cad, "40 Example St", at=(-12, 20))["building"]) == {"A"}
    assert which(cad, call(cad, "44 EXAMPLE ST", at=(12, 20))["building"]) == {"A"}


def test_a_range_is_the_union_of_its_numbers_buildings(cad):
    b = call(cad, "40-46 Example St", at=(0, 20))["building"]
    assert b["how"] == "range" and which(cad, b) == {"A", "B"}
    assert which(cad, call(cad, "40-44 Example St", at=(0, 20))["building"]) == {"A"}


def test_an_address_point_in_the_yard_takes_the_nearest_footprint_on_its_side(cad):
    b = call(cad, "48 Example St", at=(70, 12))["building"]
    assert b["how"] == "nearest" and which(cad, b) == {"D"}
    # the building has no point of its own: the call's address says what it is
    assert b["footprints"][0]["addrs"] == "48 Example St"


def test_never_the_building_across_the_street(cad):
    # 49's point is 11 m from a footprint across the street (F, the even side's): not 49's
    assert "building" not in call(cad, "49 Example St", at=(130, -3))


def test_no_footprint_in_reach_is_nothing(cad):
    gaz = Gaz({**POINTS, ("Example St", 70, ""): (300, 30)})       # a point in an empty lot
    cad.footprints.gaz = gaz
    assert "building" not in call(cad, "70 Example St", at=(300, 30))


def test_two_footprints_as_near_is_nothing(cad):
    blds = {**BUILDINGS, "D2": bldg(80, 12, 6, 6)}                    # a second one as near 48's point as its house
    cad.gis = fake_gis(blds)
    assert "building" not in call(cad, "48 Example St", at=(70, 12))


def test_no_address_point_the_pin_in_a_footprint(cad):
    b = call(cad, "60 Example St", at=(230, 25))["building"]
    assert b["how"] == "point" and which(cad, b) == {"K"}


def test_no_address_point_and_a_pin_in_the_street_is_nothing(cad):
    assert "building" not in call(cad, "60 Example St", at=(230, 0))


def test_a_campus_lights_its_large_buildings_not_the_shed_nor_another_address(cad):
    b = call(cad, "100 Sample Ave", at=(400, 60))["building"]
    assert which(cad, b) == {"H1", "H2", "H3"}
    assert "104 Sample Ave" in call(cad, "104 Sample Ave", at=(380, 95))["building"]["footprints"][0]["addrs"]
    assert which(cad, call(cad, "104 Sample Ave", at=(380, 95))["building"]) == {"H4"}


def test_no_parcels_no_campus(cad):
    cad.gis = fake_gis(parcels=[])
    assert which(cad, call(cad, "100 Sample Ave", at=(400, 60))["building"]) == {"H1"}


def test_the_address_points_town_and_pin(cad):
    # the same street and number in another town: its points are never ours when the call is in this one
    gaz = Gaz()
    gaz.streets[1].addrs["Elsewhere"] = [(42, "", *ll(0, -2000))]
    cad.footprints.gaz = gaz
    assert which(cad, call(cad, "42 Example St", at=(0, 20))["building"]) == {"A"}
    # no pin and the number in two towns: which one is unknown, so none from the points
    assert "building" not in call(cad, "42 Example St")


def test_a_letter_is_its_own_building(cad):
    gaz = Gaz({**POINTS, ("Example St", 52, "a"): (150, 60)})
    cad.footprints.gaz = gaz
    cad.gis = fake_gis({**BUILDINGS, "G2": bldg(150, 60, 10, 10)})
    assert which(cad, call(cad, "52 Example St", at=(150, 22))["building"]) == {"G"}
    b = call(cad, "52A Example St", at=(150, 60))["building"]
    assert b["footprints"][0]["geometry"] == rect(150, 60, 10, 10)


# -- a place, and what is never a building -------------------------------------------------------------------------------

def test_a_place_with_a_polygon_is_its_polygon(cad):
    poly = rect(400, 75, 110, 60)
    cad.local_places = lambda: [{"key": "sample plaza", "name": "Sample Plaza", "aliases": [], "lat": ll(400, 60)[0],
                                 "lon": ll(400, 60)[1], "geometry": poly}]
    b = call(cad, "@Sample Plaza", at=(400, 60), place="Sample Plaza")["building"]
    assert b == {"how": "place", "footprints": [{"ref": "place/sample plaza", "geometry": poly, "addrs": "Sample Plaza"}]}


def test_a_place_without_one_is_the_footprints_its_points_are_in(cad):
    lat, lon = ll(400, 60)
    cad.local_places = lambda: [{"key": "sample plaza", "name": "Sample Plaza", "aliases": ["The Plaza"], "lat": lat, "lon": lon}]
    b = call(cad, "@SAMPLE PLAZA", at=(400, 60), place="Sample Plaza")["building"]
    assert b["how"] == "place" and which(cad, b) == {"H1", "H2", "H3"}      # its point's building, and its campus
    # a place the department never named: the footprint its pin is in
    cad.local_places = lambda: []
    assert which(cad, call(cad, "@Sample Hall", at=(0, 25), place="Sample Hall")["building"]) == {"A"}


def test_a_site_of_several_addresses_lights_each(cad):
    a, d = ll(0, 20), ll(70, 12)
    cad.local_places = lambda: [
        {"key": "county/sample plaza @ 42 example st", "name": "Sample Plaza", "aliases": [], "lat": a[0], "lon": a[1]},
        {"key": "county/sample plaza @ 40-44 example st", "name": "Sample Plaza", "aliases": [], "lat": ll(40, 22)[0],
         "lon": ll(40, 22)[1], "site": "county/sample plaza @ 42 example st"},
        {"key": "county/sample plaza @ 9 elsewhere", "name": "Sample Plaza", "aliases": [], "lat": d[0] + 0.2, "lon": d[1]}]
    b = call(cad, "@Sample Plaza", at=(0, 20), place="Sample Plaza")["building"]
    assert which(cad, b) == {"A", "B"}                       # never the same name 20 km away


@pytest.mark.parametrize("address,place", [("I-287 EB MM 5.2", ""), ("I-287 EXIT 9", "I-287 Exit 9"),
                                           ("Example St / Sample Ave", ""), ("", "")])
def test_highways_exits_intersections_and_bare_pins_are_never_a_building(cad, address, place):
    assert "building" not in call(cad, address, at=(0, 25), place=place)        # (a pin right in a footprint)


def test_without_the_county_layer_or_a_gazetteer(tmp_path):
    c = make_cad(tmp_path)
    assert "building" not in call(c, "42 Example St", at=(0, 20))             # no layers: nothing
    c.gis = fake_gis()
    assert which(c, call(c, "42 Example St", at=(0, 20))["building"]) == {"A"}   # no gazetteer: the pin's footprint
    assert "building" not in call(c, "42 Example St", at=(0, 0))


# -- recomputed when the location changes; indexed once per layer rev -----------------------------------------------------

def test_a_new_location_is_a_new_building(cad):
    inc = call(cad, "42 Example St", at=(0, 20))
    assert which(cad, inc["building"]) == {"A"}
    lat, lon = ll(0, -22)
    cad.store.update_incident(inc["id"], {"address": "41 Example St", "lat": lat, "lon": lon})
    now = next(i for i in cad.state()["incidents"] if i["id"] == inc["id"])
    assert which(cad, now["building"]) == {"C"}
    cad.store.update_incident(inc["id"], {"address": "Example St / Sample Ave"})
    assert "building" not in next(i for i in cad.state()["incidents"] if i["id"] == inc["id"])


def test_a_closed_call_carries_it_on_its_full_read(cad):
    inc = call(cad, "42 Example St", at=(0, 20))
    cad.store.db.execute("UPDATE incidents SET status = 'closed' WHERE id = ?", (inc["id"],))
    cad.store.db.commit()
    assert which(cad, cad.full(inc["id"])["building"]) == {"A"}


def test_the_layers_are_indexed_once_per_rev_and_the_answer_cached(cad):
    reads = []
    real = cad.gis.features

    def features(key):
        reads.append(key)
        return real(key)
    cad.gis.features = features
    for _ in range(3):
        call(cad, "42 Example St", at=(0, 20))
        call(cad, "100 Sample Ave", at=(400, 60))
    assert reads.count("buildings") == 1 and reads.count("parcels") == 1
    # a new rev of the layer (A only): indexed again in the background while the old grid serves, then worked out again
    set_layer(cad.gis, "buildings", list(cad.gis.data["buildings"])[:1])
    call(cad, "100 Sample Ave", at=(400, 60))
    for t in [t for t in threading.enumerate() if t.name == "footprints-index"]:
        t.join(10)
    assert reads.count("buildings") == 2
    assert "building" not in call(cad, "100 Sample Ave", at=(400, 60))        # H1 is gone from the layer
    assert which(cad, call(cad, "42 Example St", at=(0, 20))["building"]) == {"A"}
    assert reads.count("buildings") == 2


def test_the_grid_reads_a_few_cells_not_the_layer():
    g = fpm.Grid()
    for i in range(2000):
        g.add((i * 0.001, 0, i * 0.001 + 0.0002, 0.0002), i)
    assert g.query((0.5, 0, 0.5001, 0.0001)) == [500]
    assert sorted(g.query((0.0005, 0, 0.0025, 0.0001))) == [1, 2]


def test_the_push_stays_small(cad):
    for k in range(20):
        call(cad, "42 Example St", at=(0, 20))
    st = cad.state()
    per = [len(json.dumps(i["building"])) for i in st["incidents"] if "building" in i]
    assert per and max(per) < 1500


# -- the page ---------------------------------------------------------------------------------------------------------------

def test_the_script_is_on_the_page_after_the_maps():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/bldg-highlight.js"') > html.index('src="/static/preplan.js"')


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_features_in_node(tmp_path):
    import subprocess
    js = (STATIC / "bldg-highlight.js").read_text()
    script = tmp_path / "t.js"
    script.write_text("globalThis.window = globalThis;\n" + js + """
const B = window.BldgHighlight;
const g = (n) => ({ type: "Polygon", coordinates: [[[0, 0], [n, 0], [n, n], [0, 0]]] });
const incs = [
  { id: 1, status: "active", building: { how: "address", footprints: [{ ref: "buildings/@1", geometry: g(1), addrs: "40-44 Example St" }] } },
  { id: 2, status: "pending", building: { how: "range", footprints: [{ ref: "buildings/@1", geometry: g(1), addrs: "40-44 Example St" },
                                                                    { ref: "buildings/@2", geometry: g(2), addrs: "" }] } },
  { id: 3, status: "closed", building: { how: "address", footprints: [{ ref: "buildings/@3", geometry: g(3), addrs: "1 Sample Ave" }] } },
  { id: 4, status: "active" },
];
const one = B.features([incs[0]]);
const all = B.features(B.shown(incs));
console.log(JSON.stringify({ one: one.features.map((f) => f.properties), n: all.features.length,
                             refs: all.features.map((f) => f.properties.ref), title: B.title(all.features[1].properties) }));
""")
    out = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30,
                         env={"PATH": "/usr/bin:/bin"}, input="")
    assert out.returncode == 0, out.stderr
    d = json.loads(out.stdout)
    assert d["one"] == [{"ref": "buildings/@1", "addrs": "40-44 Example St", "inc": 1}]
    assert d["n"] == 2 and d["refs"] == ["buildings/@1", "buildings/@2"]       # one footprint once; closed calls not on the board
    assert d["title"] == "Call's building"


def test_warm_indexes_ahead_and_once(cad):
    reads = []
    real = cad.gis.features
    cad.gis.features = lambda key: (reads.append(key), real(key))[1]
    cad.footprints.warm()
    assert sorted(reads) == ["buildings", "parcels"]
    call(cad, "100 Sample Ave", at=(400, 60))
    assert sorted(reads) == ["buildings", "parcels"]


# -- in the browser (headless Chromium; skipped without Playwright). SIZEUP_SHOTS=<dir> keeps the screenshots ---------------

def test_the_call_map_the_large_map_and_the_overview_light_the_building(tmp_path):
    import os
    import threading
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    from sizeup.cad.api import CadApi
    from sizeup.web.server import WebServer
    c = make_cad(tmp_path)
    g = fake_gis()
    c.gis = g
    c.footprints.gaz = Gaz()
    one = call(c, "42 Example St", at=(0, 20))["id"]
    call(c, "100 Sample Ave", at=(400, 60))
    shots = os.environ.get("SIZEUP_SHOTS", "")
    fonts = "https://tiles.openfreemap.org/fonts/"
    home = ll(200, 40)
    scene = {"timezone": "America/New_York", "home": {"center": [home[1], home[0]], "zoom": 16.2},
             "glyphs": fonts + "{fontstack}/{range}.pbf"}
    ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda iid: False, cad=CadApi(c), gis=g)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    shot = (lambda loc, name: loc.screenshot(path=os.path.join(shots, name))) if shots else (lambda loc, name: None)
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001
        pw.stop()
        ws.shutdown()
        pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
    a_lat, a_lon = ll(0, 25)
    try:
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(base) and not (shots and url.startswith(fonts)), lambda route: route.abort())
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/cad")
        pg.fill("#operator", "JD")
        pg.wait_for_selector("#ov-map canvas.maplibregl-canvas", state="attached")
        pg.wait_for_function("() => window.BldgHighlight && BldgHighlight.maps && BldgHighlight.maps.size > 0")
        # the Overview: every open call's building
        ov = """() => [...BldgHighlight.maps].find((m) => m.map.getContainer().closest('#ov-map'))"""
        pg.wait_for_function(f"() => {{ const m = ({ov})(); return m && m.map.getSource('bh-src') && m.sig.includes('buildings/'); }}")
        refs = pg.evaluate(f"() => JSON.parse(({ov})().sig).map((x) => x[0])")
        assert sorted(set(refs)) == sorted({one, one + 1}) and len(refs) == 4             # A, and the campus's three
        pg.evaluate(f"() => {{ const m = ({ov})(); m.map.jumpTo({{ center: cad.scene.home.center, zoom: 16.2 }}); }}")
        pg.evaluate("() => MapLayers.setLayer('gis:buildings', true)")
        pg.wait_for_timeout(900)
        shot(pg.locator("#ov-map"), "building-overview.png")
        # the call's map: its own building only, drawn at a zoom the buildings layer is not (15+)
        pg.evaluate(f"() => selectIncident({one})")
        dm = """() => [...BldgHighlight.maps].find((m) => m.map.getContainer().closest('#d-map'))"""
        pg.wait_for_function(f"() => {{ const m = ({dm})(); return m && m.sig.includes('buildings/'); }}")
        got = pg.evaluate(f"() => JSON.parse(({dm})().sig).map((x) => [x[0], x[2]])")
        assert got == [[one, "40-44 Example St"]]
        pg.evaluate(f"() => {{ const m = ({dm})(); m.map.jumpTo({{ center: [{a_lon}, {a_lat}], zoom: 18 }}); }}")
        pg.wait_for_function(f"""() => {{ const m = ({dm})(); return m.map.loaded() &&
            m.map.queryRenderedFeatures({{ layers: ['bh-line'] }}).length > 0; }}""")
        # its order: over the county buildings, under the rigs' lines
        order = pg.evaluate(f"() => ({dm})().order()")
        assert order.index("bh-fill") < order.index("bh-line")
        if "gis-buildings-fill" in order:                # right over the county fills, under their outlines and labels
            assert order.index("gis-buildings-fill") < order.index("bh-fill")
            assert order.index("bh-line") < order.index("gis-buildings-line")
        if "cm-lines" in order:
            assert order.index("bh-line") < order.index("cm-lines")
        # hover: the addresses it holds
        box = pg.locator("#d-map canvas.maplibregl-canvas").bounding_box()
        xy = pg.evaluate(f"() => ({dm})().map.project([{a_lon}, {a_lat}])")
        pg.mouse.move(box["x"] + xy["x"], box["y"] + xy["y"])
        pg.wait_for_function("() => document.querySelector('#d-map canvas.maplibregl-canvas').title === '40-44 Example St'")
        pg.wait_for_timeout(500)
        shot(pg.locator("#d-map"), "building-call-map.png")
        # below the buildings layer's zoom (15): still drawn
        pg.evaluate(f"() => {{ const m = ({dm})(); m.map.jumpTo({{ center: [{a_lon}, {a_lat}], zoom: 14.4 }}); }}")
        pg.wait_for_function(f"""() => {{ const m = ({dm})(); return m.map.loaded() && m.map.getZoom() < 15 &&
            m.map.queryRenderedFeatures({{ layers: ['bh-line'] }}).length > 0; }}""")
        if pg.evaluate(f"() => !!({dm})().map.getLayer('gis-buildings-fill')"):
            assert pg.evaluate(f"() => ({dm})().map.queryRenderedFeatures({{ layers: ['gis-buildings-fill'] }}).length") == 0
        shot(pg.locator("#d-map"), "building-call-map-z14.png")
        # the large map: the call's
        pg.evaluate("() => CadMap.openBig()")
        bm = """() => [...BldgHighlight.maps].find((m) => m.map.getContainer().closest('.cm-big'))"""
        pg.wait_for_function(f"() => {{ const m = ({bm})(); return m && m.sig.includes('buildings/'); }}")
        assert pg.evaluate(f"() => JSON.parse(({bm})().sig).map((x) => x[0])") == [one]
        pg.evaluate(f"() => {{ const m = ({bm})(); m.map.jumpTo({{ center: [{a_lon}, {a_lat}], zoom: 17.5 }}); }}")
        pg.wait_for_timeout(900)
        shot(pg.locator(".cm-big"), "building-large-map.png")
        # a new address: the push moves it
        lat, lon = ll(0, -22)
        c.store.update_incident(one, {"address": "41 Example St", "lat": lat, "lon": lon})
        pg.wait_for_function(f"() => {{ const m = ({bm})(); return m && m.sig.includes('41 Example St'); }}")
        assert errors == []
    finally:
        browser.close()
        pw.stop()
        ws.shutdown()


def test_the_wall_board_lights_every_open_call(tmp_path):
    import threading
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    from sizeup.cad.api import CadApi
    from sizeup.web.server import WebServer
    c = make_cad(tmp_path)
    g = fake_gis()
    c.gis = g
    c.footprints.gaz = Gaz()
    call(c, "42 Example St", at=(0, 20))
    call(c, "48 Example St", at=(70, 12))
    home = ll(40, 20)
    scene = {"timezone": "America/New_York", "home": {"center": [home[1], home[0]], "zoom": 17},
             "glyphs": "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf"}     # (asked for, never fetched)
    ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda iid: False, cad=CadApi(c), gis=g)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001
        pw.stop()
        ws.shutdown()
        pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
    try:
        pg = browser.new_page(viewport={"width": 1600, "height": 900})
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(base), lambda route: route.abort())
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/monitor")
        pg.wait_for_function("""() => window.BldgHighlight && [...BldgHighlight.maps].some((m) =>
            m.map.getContainer().closest('#ov-map') && m.sig.includes('48 Example St') && m.sig.includes('40-44 Example St'))""")
        assert errors == []
    finally:
        browser.close()
        pw.stop()
        ws.shutdown()


# -- the review's cases: never a wrong building ------------------------------------------------------------------------------

def test_no_address_point_and_the_pin_in_a_neighbours_house_is_nothing(cad):
    assert "building" not in call(cad, "58 Example St", at=(150, 25))       # the pin in 52's house (G)


def test_a_letter_with_no_point_of_its_own_is_nothing(cad):
    assert "building" not in call(cad, "52A Example St", at=(150, 22))      # its pin is 52's


def test_a_range_takes_each_numbers_building_its_own_way(cad):
    b = call(cad, "40-48 Example St", at=(0, 20))["building"]
    assert b["how"] == "range" and which(cad, b) == {"A", "B", "D"}          # 48's point in its yard: its house too


def test_another_address_nearer_the_footprint_is_its_building(cad):
    n = bldg(100, 42, 10, 10)                                               # 7 m behind 50's point (in no footprint)
    cad.gis = fake_gis({**BUILDINGS, "N": n})
    assert [f["geometry"] for f in call(cad, "50 Example St", at=(100, 30))["building"]["footprints"]] == [n["geometry"]]
    cad.footprints.gaz = Gaz({**POINTS, ("Example St", 54, ""): (100, 50)})  # 54's point 3 m from it: 54's, not 50's
    assert "building" not in call(cad, "50 Example St", at=(100, 30))


def test_the_nearest_is_on_the_points_lot(cad):
    lots = [*PARCELS_, parcel("48 EXAMPLE ST", 70, 6, 20, 20)]              # 48's lot ends short of its house (D)
    cad.gis = fake_gis(parcels=lots)
    assert "building" not in call(cad, "48 Example St", at=(70, 12))


def test_a_rear_building_is_nothing(cad):
    assert "building" not in call(cad, "42 Example St Rear", at=(0, 20))


def test_the_calls_town_with_no_such_number_is_never_the_next_towns(cad):
    gaz = Gaz()
    gaz.streets[1].addrs["Elsewhere"] = [(90, "", *ll(0, 100))]
    gaz.streets[1].addrs["Elsewhere"].append((91, "", *ll(0, 80)))
    cad.footprints.gaz = gaz
    cad.gis = fake_gis({**BUILDINGS, "X": bldg(0, 100, 10, 10)})
    cad.lists.geo_town_aliases["sampletown"] = "Sampletown"
    b = call(cad, "90 Example St", at=(0, 100))["building"]                 # no town said: the points near the pin
    assert [f["geometry"] for f in b["footprints"]] == [rect(0, 100, 10, 10)]
    assert "building" not in call(cad, "90 Example St, Sampletown", at=(0, 100))


def test_a_place_name_in_two_places_with_no_pin_is_nothing(cad, monkeypatch):
    rev = ["1"]
    monkeypatch.setattr(cad.settings, "places_rev", lambda: rev[0])        # (moves with every edit to the place names)
    a, b = ll(0, 20), ll(400, 60)
    cad.local_places = lambda: [{"key": "sample deli", "name": "Sample Deli", "aliases": [], "lat": a[0], "lon": a[1]},
                                {"key": "sample deli 2", "name": "Sample Deli", "aliases": [], "lat": b[0], "lon": b[1]}]
    assert "building" not in call(cad, "@Sample Deli", place="Sample Deli")
    cad.local_places = lambda: [{"key": "sample deli", "name": "Sample Deli", "aliases": [], "lat": a[0], "lon": a[1]}]
    rev[0] = "2"
    assert which(cad, call(cad, "@Sample Deli", place="Sample Deli")["building"]) == {"A"}


def test_a_new_place_polygon_is_picked_up(cad, monkeypatch):
    rev = ["1"]
    monkeypatch.setattr(cad.settings, "places_rev", lambda: rev[0])
    cad.local_places = lambda: []
    assert which(cad, call(cad, "@Sample Plaza", at=(0, 25), place="Sample Plaza")["building"]) == {"A"}
    poly = rect(0, 25, 60, 30)
    cad.local_places = lambda: [{"key": "sample plaza", "name": "Sample Plaza", "aliases": [], "lat": ll(0, 25)[0],
                                 "lon": ll(0, 25)[1], "geometry": poly}]
    rev[0] = "2"
    b = call(cad, "@Sample Plaza", at=(0, 25), place="Sample Plaza")["building"]
    assert b["footprints"] == [{"ref": "place/sample plaza", "geometry": poly, "addrs": "Sample Plaza"}]


def test_a_lot_addressed_as_a_range_is_its_numbers_campus():
    assert fpm._lot_is("40-44 EXAMPLE ST", {42}, "example st")
    assert fpm._lot_is("100 SAMPLE AVE", {100}, "sample ave") and not fpm._lot_is("100 SAMPLE AVE", {104}, "sample ave")
    assert not fpm._lot_is("", {1}, "example st") and not fpm._lot_is("SAMPLE AVE", {1}, "sample ave")


def test_a_campus_too_big_to_be_one_building_is_not_added(cad):
    many = {f"M{i}": bldg(1000 + (i % 5) * 30, 40 + (i // 5) * 30, 20, 20) for i in range(20)}
    pts = {**POINTS, ("Sample Ave", 200, ""): (1000, 40)}
    cad.gis = fake_gis({**BUILDINGS, **many}, [*PARCELS_, parcel("200 SAMPLE AVE", 1060, 100, 160, 160)])
    cad.footprints.gaz = Gaz(pts)
    assert len(call(cad, "200 Sample Ave", at=(1000, 40))["building"]["footprints"]) == 1


def test_a_footprint_with_a_hole_and_parts():
    outer = rect(0, 0, 40, 40)["coordinates"][0]
    hole = rect(0, 0, 10, 10)["coordinates"][0]
    g = {"type": "MultiPolygon", "coordinates": [[outer, hole], rect(100, 0, 10, 10)["coordinates"]]}
    assert abs(fpm.area_m2(g) - (1600 - 100 + 100)) < 5
    grid = fpm.index_layer("buildings", [{"type": "Feature", "properties": {}, "geometry": g}])
    f = grid.items[0][1]
    assert f.ref.startswith("buildings/@") and f.ref.endswith("/15")
    from sizeup.gis import contains
    assert contains(g, *ll(15, 0)) and not contains(g, *ll(0, 0)) and contains(g, *ll(100, 0))


def test_a_range_with_a_letter_on_its_end(monkeypatch):
    monkeypatch.setattr(fpm, "_houserange", None)
    assert fpm.house_numbers("14-16a") == [(14, ""), (16, ""), (16, "a")]
