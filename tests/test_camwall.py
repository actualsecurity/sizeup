"""The camera wall (GitHub #125): every camera in our area on the Overview and the wall board.

The area selection (sizeup/roads.py Roads.area: our fire districts plus a margin, the include / exclude lists,
the fallbacks when the district layer isn't loaded), the before / after pin, the order, the live-video budget
and the stream's per-page close; the Settings section ("camera_wall": audited, exported, imported); and the
page's static checks (camwall.js: the toggle, the remembered state, the observer, the pause). Fixtures: the
Thruway's and the county's recorded cameras (tests/test_roads.py), the county GIS's recorded fire districts and
municipal boundaries (tests/fixtures/wcgis), the real mile markers (data/gazetteer.sqlite)."""

import copy
import json
import re
from pathlib import Path

import pytest

from sizeup import roads as roads_mod
from sizeup.cad.store import CadError
from sizeup.config import ConfigError, load
from tests.test_cloud import HubRun, hub_cfg
from tests.test_roads import CHUNKLIST, GAZ, PLAYLIST, Clock, StreamFetch, county, make, place
from tests.test_settings import post

WCGIS = Path(__file__).parent / "fixtures" / "wcgis"
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
# What the area is with the recorded data: inside PORT CHESTER FD (the county's 2011 polygon, Rye Brook inside it),
# or within 1 km of it (the Thruway's I-95 cameras at interchange 21 and the ramp, the county's at Purchase St).
INSIDE = {"1ne01470n", "wc23", "wc24", "wc25", "wc26"}
MARGIN = {"1ne01370s", "1ne01420n", "wc70", "wc71"}


class Districts:
    """The county GIS layers as Roads reads them (sizeup/gis.py Gis.features, .ours), from the fixtures; ours
    marked as Gis.refresh marks them."""

    def __init__(self, keys=("fire_districts", "municipalities")):
        self.ours = ("portchester", "ryebrook")
        self.data = {k: json.loads((WCGIS / f"{k}.geojson").read_text())["features"] for k in keys}
        for f in self.data.get("fire_districts", []):
            if f["properties"].get("FIRENAME") == "PORT CHESTER FD":
                f["properties"]["_ours"] = True

    def features(self, key):
        return copy.deepcopy(self.data.get(key, []))


def wall(gis="both", settings=None, **kw):
    r, f = county(gazetteer=GAZ, **kw)
    r.gis = Districts() if gis == "both" else Districts(gis) if gis else None
    r.ours = ("Port Chester FD", "Rye Brook FD")
    if settings is not None:
        r.wall_settings = lambda: settings
    return r, f


def ids(doc):
    return [c["id"] for c in doc["cameras"]]


# ---- the area ----------------------------------------------------------------------------------------------------


def test_inside_our_fire_districts_plus_the_margin():
    r, _ = wall()
    doc = r.area()
    assert doc["area"] == {"basis": "fire_districts", "names": ["PORT CHESTER FD"], "note": "", "margin_m": 1000,
                           "include": [], "exclude": []}
    assert set(ids(doc)) == INSIDE | MARGIN
    by = {c["id"]: c for c in doc["cameras"]}
    assert all(by[i]["area"] == "inside" and by[i]["dist_m"] == 0 for i in INSIDE)
    assert all(by[i]["area"] == "margin" and 0 < by[i]["dist_m"] <= 1000 for i in MARGIN)
    # the I-287 camera just east of interchange 9 is 1.25 km out: not on the wall at the default margin
    rows = {c["id"]: c for c in r.settings_rows()["cameras"]}
    assert rows["1cw00780w"]["auto"] == "outside" and 1000 < rows["1cw00780w"]["dist_m"] < 1400 and not rows["1cw00780w"]["in"]
    assert r.settings_rows()["n"] == 9
    # each camera keeps what the map has (source, cadence, video, health)
    assert by["1ne01370s"]["source"] == "Thruway" and by["1ne01370s"]["has_video"] and by["wc23"]["cadence"] == 8


def test_the_margin_is_a_setting():
    r, _ = wall(settings={"margin_m": 0})
    assert set(ids(r.area())) == INSIDE
    r, _ = wall(settings={"margin_m": 1400})
    assert set(ids(r.area())) >= INSIDE | MARGIN | {"1cw00780w"}
    r, _ = wall(settings={"margin_m": "lots"})                     # anything else: the default
    assert r.area()["area"]["margin_m"] == 1000


def test_cameras_put_on_or_taken_off_by_hand():
    r, _ = wall(settings={"include": ["1cw00780w", "../x"], "exclude": ["wc70", "wc71"]})
    doc = r.area()
    assert set(ids(doc)) == (INSIDE | MARGIN | {"1cw00780w"}) - {"wc70", "wc71"}
    assert doc["area"]["include"] == ["1cw00780w"]                 # a bad id is dropped
    assert next(c for c in doc["cameras"] if c["id"] == "1cw00780w")["area"] == "include"
    rows = {c["id"]: c for c in r.settings_rows()["cameras"]}
    assert (rows["wc70"]["mode"], rows["wc70"]["auto"], rows["wc70"]["in"]) == ("exclude", "margin", False)


def test_without_the_district_layer_our_municipal_boundaries_then_a_box():
    r, _ = wall(gis=("municipalities",))
    doc = r.area()
    assert doc["area"]["basis"] == "municipalities" and doc["area"]["names"] == ["Port Chester", "Rye Brook"]
    assert "fire-district layer isn't loaded" in doc["area"]["note"]
    assert INSIDE <= set(ids(doc))
    r, _ = wall(gis=None)                                           # no county GIS at all yet
    doc = r.area()
    assert doc["area"]["basis"] == "bbox" and "box around Port Chester and Rye Brook" in doc["area"]["note"]
    assert INSIDE <= set(ids(doc)) and "1ne00694n" not in ids(doc)  # New Rochelle stays off
    r, _ = wall(gis=("fire_districts", "municipalities"))
    r.gis.data["fire_districts"] = []                               # the layer configured but not fetched yet
    assert r.area()["area"]["basis"] == "municipalities"


def test_down_cameras_stay_on_flagged():
    clock = Clock()
    r, f = wall(clock=clock)
    f.county["23"] = "old"                                          # the county's 2021 placeholder: down
    r.image("wc23")
    doc = r.area()
    c = next(c for c in doc["cameras"] if c["id"] == "wc23")
    assert c["down"] and c["down_since"] and c["area"] == "inside"


def test_sorted_by_road_then_mileage_and_a_highway_event_pinned_first():
    r, _ = wall()
    order = ids(r.area())
    assert order[:3] == ["1ne01370s", "1ne01420n", "1ne01470n"]     # I-95 by our mile markers (13.8, 14.2, 14.7)
    assert order[3:] == ["wc23", "wc26", "wc70", "wc71", "wc24", "wc25"]     # then the streets, by road
    lat, lon = place("I-95 SB exit 21")
    doc = r.area(lat, lon, "I-95 SB exit 21")
    assert doc["highway"] == {"road": "I-95", "dir": "SB", "mile": 14.5}
    assert [(c["id"], c["pin"], c["area"]) for c in doc["cameras"][:2]] == [("1ne01470n", "before", "inside"), ("1ne01420n", "after", "margin")]
    assert ids(doc)[2:] == [i for i in order if i not in ("1ne01470n", "1ne01420n")] and doc["cameras"][0]["miles"] is not None
    # an event further down the road: its camera beyond our area is pinned too (on the wall while the event is)
    lat, lon = place("I-95 SB exit 19")
    doc = r.area(lat, lon, "I-95 SB exit 19")
    assert [(c["id"], c["pin"], c["area"]) for c in doc["cameras"][:2]] == [("1ne01370s", "before", "margin"), ("1ne01240n", "after", "outside")]
    # off a highway: nothing pinned
    doc = r.area(41.022, -73.678, "Rye Brook")
    assert doc["highway"] is None and not any(c["pin"] for c in doc["cameras"])


def test_the_endpoint():
    r, _ = wall()
    code, body, ctype, headers = r.get("/api/cad/cameras", "area=ours")
    doc = json.loads(body)
    assert code == 200 and ctype == "application/json" and headers["Cache-Control"] == "no-store"
    assert set(ids(doc)) == INSIDE | MARGIN and doc["configured"]
    assert doc["streams"] == {"max": 8, "per_viewer": 3, "open": 0, "streaming": 3}    # the area's 3 Thruway streams
    lat, lon = place("I-95 SB exit 21")
    doc = json.loads(r.get("/api/cad/cameras", f"area=ours&for={lat},{lon}&label=I-95+SB+exit+21")[1])
    assert doc["cameras"][0]["pin"]
    assert r.get("/api/cad/cameras", "area=everywhere")[0] == 400
    assert r.get("/api/cad/cameras", "area=ours&for=nowhere")[0] == 400
    # the image URLs stay at the hub
    assert "westchestergov" not in json.dumps(doc) and "nyssnapshot" not in json.dumps(doc)


def test_the_live_budget_per_page_and_hub_wide():
    r, _ = wall()
    r.stream_max = 2
    assert r.area()["streams"]["per_viewer"] == 2                   # never more than the hub allows
    r, _ = wall()
    r.wall_streams = 1                                              # [roads] wall_streams
    assert r.area()["streams"] == {"max": 8, "per_viewer": 1, "open": 0, "streaming": 3}


# ---- the stream: one origin session per camera, each page's use known, a close that stops it now ------------------


def test_a_page_closing_its_stream_stops_it_unless_another_page_watches():
    clock = Clock()
    f = StreamFetch()
    r = make(key="", urls={}, fetch=f, clock=clock, thruway=True)
    assert r.refresh("cameras")
    cid = "1ne01370s"
    code, body, _, _ = r.get(f"/api/cad/cameras/{cid}/stream/playlist.m3u8", "v=wallA")
    assert code == 200 and b"chunklist_w1849562454.m3u8?v=wallA" in body           # its chunklist asked with its token
    n = sum(1 for u in f.urls if u.endswith("/playlist.m3u8"))
    code, body, _, _ = r.get(f"/api/cad/cameras/{cid}/stream/playlist.m3u8", "v=dlgB")
    assert code == 200 and b"?v=dlgB" in body
    assert sum(1 for u in f.urls if u.endswith("/playlist.m3u8")) == n           # the same origin session: not asked again
    assert r.get(f"/api/cad/cameras/{cid}/stream/chunklist_w1849562454.m3u8", "v=dlgB")[1] == CHUNKLIST
    assert r.streams_open() == 1
    code, body, _, _ = r.get(f"/api/cad/cameras/{cid}/stream/close", "v=wallA")
    assert code == 200 and json.loads(body) == {"open": True} and r.streams_open() == 1    # B still watches
    code, body, _, _ = r.get(f"/api/cad/cameras/{cid}/stream/close", "v=dlgB")
    assert json.loads(body) == {"open": False} and r.streams_open() == 0                  # nobody: closed now, not in 30 s
    assert r.get(f"/api/cad/cameras/{cid}/stream/media_w1849562454_1225.ts", "")[0] == 404
    assert r.get(f"/api/cad/cameras/{cid}/stream/close", "v=wallA")[0] == 200            # closing twice is fine
    # a token that isn't one is ignored (the stream still opens, unnamed)
    code, body, _, _ = r.get(f"/api/cad/cameras/{cid}/stream/playlist.m3u8", "v=../evil")
    assert code == 200 and body == PLAYLIST
    assert r.handles(f"/api/cad/cameras/{cid}/stream/close")


def test_a_watcher_gone_quiet_does_not_hold_a_stream_open_after_the_others_close():
    clock = Clock()
    f = StreamFetch()
    r = make(key="", urls={}, fetch=f, clock=clock, thruway=True)
    assert r.refresh("cameras")
    cid = "1ne01370s"
    r.get(f"/api/cad/cameras/{cid}/stream/playlist.m3u8", "v=gone")
    clock.t += 40                                                        # that page went away without a close
    r.get(f"/api/cad/cameras/{cid}/stream/playlist.m3u8", "v=here")
    assert json.loads(r.get(f"/api/cad/cameras/{cid}/stream/close", "v=here")[1]) == {"open": False}


# ---- config ----------------------------------------------------------------------------------------------------


def test_the_stream_cap_and_the_wall_budget_are_config(tmp_path):
    def cfg(extra):
        p = tmp_path / "c.toml"
        p.write_text('[station]\nid = "t"\nname = "T"\n[radio]\nsdrs = []\n' + extra)
        return load(p)
    c = cfg("")
    assert (c.roads_stream_max, c.roads_wall_streams) == (8, 0)
    c = cfg("[roads]\nstream_max = 16\nwall_streams = 4\n")
    assert (c.roads_stream_max, c.roads_wall_streams) == (16, 4)
    r = roads_mod.Roads.from_config(c, log=lambda m: None)
    assert (r.stream_max, r.wall_streams) == (16, 4)
    for bad in ("stream_max = 0", "stream_max = 65", "stream_max = true", "wall_streams = -1", "wall_streams = 2.5"):
        with pytest.raises(ConfigError):
            cfg(f"[roads]\n{bad}\n")


# ---- Settings > System > Camera wall: audited, exported, imported ------------------------------------------------


def test_the_settings_are_audited_exported_and_imported(tmp_path):
    h = HubRun(hub_cfg(tmp_path))
    try:
        cad, roads = h.app.cad, h.app.roads
        assert roads.gis is h.app.gis and roads.ours == ("Port Chester FD", "Rye Brook FD")
        page = cad.settings_get()["camera_wall"]
        assert (page["margin_m"], page["default_margin_m"], page["edited"]) == (1000, 1000, False)
        assert page["area"]["basis"] in ("fire_districts", "municipalities", "bbox") and page["cameras"] == []
        post(cad, "camera_wall", action="set", key="margin_m", value=1500)
        post(cad, "camera_wall", action="camera", id="1cw00780w", mode="include")
        post(cad, "camera_wall", action="camera", id="wc70", mode="exclude")
        post(cad, "camera_wall", action="camera", id="wc70", mode="include")          # moved from one list to the other
        assert roads.wall_config() == {"margin_m": 1500.0, "include": ["1cw00780w", "wc70"], "exclude": []}
        page = cad.settings_get()["camera_wall"]
        assert page["edited"] and page["margin_m"] == 1500 and page["include"] == ["1cw00780w", "wc70"]
        log = cad.settings.audit_log("system")
        assert [a["key"] for a in log[:4]] == ["camera_wall/camera/wc70", "camera_wall/camera/wc70",
                                                "camera_wall/camera/1cw00780w", "camera_wall/margin_m"]
        assert (log[-1]["old"], log[-1]["new"]) == ("1000", "1500")
        for bad in ({"action": "set", "key": "margin_m", "value": 9000}, {"action": "set", "key": "margin_m", "value": True},
                    {"action": "camera", "id": "a/b", "mode": "include"}, {"action": "camera", "id": "x", "mode": "always"},
                    {"action": "set", "key": "colour", "value": 1}, {"action": "explode"}):
            with pytest.raises(CadError):
                post(cad, "camera_wall", **bad)
        exported = cad.settings_export()
        assert exported["overrides"]["camera_wall"] == {"margin_m": 1500, "include": ["1cw00780w", "wc70"]}
        post(cad, "camera_wall", action="reset")
        assert roads.wall_config() == {"margin_m": 1000, "include": [], "exclude": []}
        with pytest.raises(CadError):
            post(cad, "camera_wall", action="reset")                                     # already the default
        r = post(cad, "import", action="apply", settings=exported)
        assert any(c["store"] == "camera_wall" for c in r["changes"])
        assert roads.wall_config()["include"] == ["1cw00780w", "wc70"]
        for bad in ({"margin_m": -5}, {"include": "wc70"}, {"include": ["wc70"], "exclude": ["wc70"]}, {"colour": 1}):
            doc = copy.deepcopy(exported)
            doc["overrides"]["camera_wall"] = bad
            with pytest.raises(CadError):
                post(cad, "import", action="dry_run", settings=doc)
        # admins only, like the rest of Settings
        from sizeup.cad.api import needs
        assert needs("/api/cad/settings/camera_wall", {"action": "set"}) == "admin"
    finally:
        h.close()


# ---- the page (static; tools/ui_consistency.py and the Playwright check render it) ------------------------------------


def in_try(js: str, at: int) -> bool:
    """The code at `at` is inside a try { } block (its braces still open there)."""
    t = js.rfind("try {", 0, at)
    return t >= 0 and js.count("{", t, at) > js.count("}", t, at)


def test_the_layouts_are_on_the_overview_and_the_wall_board():
    html = (STATIC / "cad.html").read_text()
    head = re.search(r'<div class="ov-head view-head">(.*?)</div>\s*<div id="ov-pages"', html, re.S).group(1)
    assert 'id="ov-layout"' in head and "console-only" not in head and "board-only" not in head   # both screens
    grid = re.search(r'<div class="ov-grid" data-layout="map">(.*?)<div class="ov-side">', html, re.S).group(1)
    assert 'class="ov-mapbox"' in grid and 'id="cw-wall"' in grid and 'id="ov-split"' in grid
    assert html.index('src="/static/camwall.js"') > html.index('src="/static/maplayers.js"')


def test_the_wall_is_remembered_per_screen_and_stops_when_hidden():
    js = (STATIC / "camwall.js").read_text()
    assert 'const KEY = "sizeup.overviewLayout", OLD_KEY = "sizeup.cameraWall"' in js and 'BOARD ? "monitor" : "overview"' in js
    for m in re.finditer(r"localStorage\.\w+Item", js):                              # storage may refuse: never throws
        assert in_try(js, m.start()), m.group(0)
    assert "new IntersectionObserver(" in js and "observer.observe(" in js and "observer.unobserve(" in js
    assert 'document.addEventListener("visibilitychange"' in js and 'document.visibilityState !== "hidden"' in js
    assert '$("#overview").hidden' in js                                            # the wall hidden (an incident open): stopped
    assert "stream/close" in js and "keepalive: true" in js                         # a stopped tile stops the hub's stream
    assert '"If-None-Match"' in js and "r.status === 304" in js                     # snapshots only when newer
    assert "per_viewer" in js                                                       # the hub's per-screen live budget
    assert "video.muted = true" in js and 'setAttribute("playsinline"' in js
    ml = (STATIC / "maplayers.js").read_text()
    assert "stream/close?v=" in ml and "loadHls, subscribe" in ml


def test_the_wall_lays_out_on_the_tokens():
    css = (STATIC / "cadmap.css").read_text()
    block = css[css.index(".cw-wall {"):css.index("@container (width < 60rem)")]
    assert "container-type: inline-size" in block
    cols = dict(re.findall(r"@container \(width ([<>]=? \d+rem)\) \{ (?:html\.board )?\.cw-grid \{ --cw-cols: (\d); \}", block))
    assert cols == {"< 30rem": "1", ">= 54rem": "3", ">= 60rem": "4", ">= 80rem": "5", ">= 100rem": "6"}
    assert 'html.board .ov-grid[data-layout="cameras"]' in block
    for v in re.findall(r"(?:font-size|padding|gap|line-height):\s*([^;]+);", block):
        assert "px" not in v and "rem" not in v, v                                  # tokens only
