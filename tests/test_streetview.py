"""Street view on the CAD maps (static/streetview.js; the key: sizeup/config.py [streetview], GET /api/cad/streetview).

Everything here is invented: the key is a made-up string of the right shape, and the points are on the invented
Example Ave grid (tests/test_preplan.py: lat 41.0 + n * 0.00009, lon -73.66)."""
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from sizeup import config as config_mod
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
KEY = "AIzaFAKEfakeFAKEfake-FAKEfake_FAKEfake01"          # made up: a browser key's shape, never a real one


def get(api, path="/api/cad/streetview", email=None):
    code, raw, ctype, *h = api.get(path, "", Identity(email=email) if email else None)
    return code, json.loads(raw), (h[0] if h else {})


# -- the key: config / SSM, never the repo or a log ----------------------------------------------------------------------

def test_the_key_comes_from_env_or_the_config_and_none_means_off(tmp_path, monkeypatch):
    monkeypatch.delenv("SIZEUP_GOOGLE_MAPS_KEY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text("")
    assert config_mod.load(p).streetview_google_key == ""
    p.write_text(f'[streetview]\ngoogle_key = "{KEY}"\n')
    assert config_mod.load(p).streetview_google_key == KEY
    monkeypatch.setenv("SIZEUP_GOOGLE_MAPS_KEY", "none")                   # SSM's placeholder before a key exists
    assert config_mod.load(p).streetview_google_key == ""
    p.write_text("")
    monkeypatch.setenv("SIZEUP_GOOGLE_MAPS_KEY", KEY)
    c = config_mod.load(p)
    assert c.streetview_google_key == KEY
    assert Cad.from_config(c).s.streetview_key == KEY
    assert KEY not in repr(c) and KEY not in repr(Cad.from_config(c).s)    # a dumped config never shows it


def test_a_bad_key_is_refused_without_repeating_it(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text("")
    bad = "AIza secret with spaces 1234567890"
    monkeypatch.setenv("SIZEUP_GOOGLE_MAPS_KEY", bad)
    with pytest.raises(config_mod.ConfigError, match="streetview.google_key") as e:
        config_mod.load(p)
    assert "secret" not in str(e.value) and "1234567890" not in str(e.value)
    p.write_text('[streetview]\nbogus = 1\n')
    monkeypatch.delenv("SIZEUP_GOOGLE_MAPS_KEY")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(p)


def test_the_page_gets_the_key_only_when_one_is_set_and_never_cached():
    code, out, h = get(CadApi(make_cad()))
    assert code == 200 and out == {"provider": ""} and h["Cache-Control"] == "no-store"
    code, out, h = get(CadApi(make_cad(streetview_key=KEY)), email="someone@example.org")
    assert code == 200 and out == {"provider": "google", "key": KEY} and h["Cache-Control"] == "no-store"


def test_the_key_is_never_logged(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIZEUP_GOOGLE_MAPS_KEY", KEY)
    p = tmp_path / "c.toml"
    p.write_text("")
    cfg = config_mod.load(p)
    logged = []
    c = Cad.from_config(cfg, log=logged.append)
    api = CadApi(c)
    lines = []
    from sizeup.web.server import WebServer
    ws = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api, access_log=lines.append)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{ws.httpd.server_address[1]}/api/cad/streetview", timeout=5).read()
    finally:
        ws.shutdown()
    assert json.loads(body)["key"] == KEY                                  # it reaches the page ...
    c.state()
    out = capsys.readouterr()
    text = "\n".join(map(str, logged + lines)) + out.out + out.err
    assert lines and "/api/cad/streetview" in text                         # ... the access log has the path ...
    assert KEY not in text and KEY[:12] not in text                        # ... and never the key


def test_the_taskdef_reads_it_from_ssm_and_no_key_is_committed():
    td = json.loads((ROOT / "deploy" / "aws" / "taskdef.json").read_text())
    hub = next(c for c in td["containerDefinitions"] if c["name"] == "hub")
    sec = {s["name"]: s["valueFrom"] for s in hub["secrets"]}
    assert sec["SIZEUP_GOOGLE_MAPS_KEY"].endswith(":parameter/sizeup-hub/google-maps-key")
    assert "SIZEUP_GOOGLE_MAPS_KEY" not in json.dumps(hub.get("environment", []))
    pat = re.compile(r"AIza[0-9A-Za-z_-]{35}")
    for f in [*STATIC.glob("*.js"), *STATIC.glob("*.html"), ROOT / "config.example.toml", ROOT / "deploy" / "hub" / "config.hub.toml"]:
        assert not pat.search(f.read_text()), f


# -- the page: wiring, privacy, the public repo ----------------------------------------------------------------------------

def test_the_page_loads_the_script_after_the_maps_and_cadmap_js_is_untouched():
    html = (STATIC / "cad.html").read_text()
    for before in ("maplayers.js", "cadmap.js", "cad.js", "map-style.js", "ui.js", "icons.js"):
        assert html.index(f'src="/static/{before}"') < html.index('src="/static/streetview.js"'), before
    for f in ("cadmap.js", "maplayers.js", "map-style.js", "cad.js", "preplan.js"):        # its own module; others' files alone
        assert "streetview" not in (STATIC / f).read_text().lower(), f
    assert 'id="i-streetview"' in (STATIC / "icons.svg").read_text()


def test_the_script_wires_the_menu_the_long_press_the_button_and_the_viewer():
    src = (STATIC / "streetview.js").read_text()
    assert "MapLayers.addGroup({ attach })" in src                        # every console map, present and future
    assert 'map.on("contextmenu"' in src                                  # right-click
    assert '"touchstart"' in src and "LONG_MS" in src and '"touchmove"' in src   # long press (iOS sends no contextmenu)
    assert "this.control.focused" in src                                  # the call's map: at the call
    assert "new this.lib.StreetViewPanorama(" in src and src.count("new this.lib.StreetViewPanorama(") == 1
    assert "if (!this.pano)" in src                                       # one panorama per page, reused (billed per one)
    for ev in ('"pano_changed"', '"position_changed"', '"pov_changed"'):
        assert f"this.pano.addListener({ev}" in src, ev                   # the map marker follows each move and turn
    assert "rotationAlignment: \"map\"" in src and "setRotation(" in src
    assert "clickToGo: true" in src and "linksControl: true" in src
    assert "imageDate" in src and "dateLabel(" in src
    assert "typeof BOARD !== \"undefined\" && BOARD" in src                # not on the wall board


def test_nothing_goes_to_google_until_the_viewer_opens_and_then_only_coordinates():
    src = (STATIC / "streetview.js").read_text()
    # the SDK is fetched in one place, sdk(), called only from the viewer's open()
    assert src.count("maps.googleapis.com") == 1 and src.count("sdk()") == 2 and src.count("sdk().then") == 1
    assert re.search(r"open\(at, opts = \{\}\) \{[\s\S]*?sdk\(\)\.then", src)
    assert 's.referrerPolicy = "strict-origin"' in src
    # the only things handed to Google: lat/lng and panorama ids (never a call's number, address, type or notes)
    for field in ("inc.id", "inc.address", "inc.number", "inc.type", "inc.notes", "inc.caller", ".address", ".run_number"):
        assert field not in src, field
    assert "this.control.focused" in src and "{ lat: +inc.lat, lon: +inc.lon }" in src
    # no hand-off to another site: the viewer is in the app
    for ext in ("google.com/maps", "maps.apple.com", "window.open(", "target=\"_blank\""):
        assert ext not in src, ext
    # the key never in the console
    for line in src.splitlines():
        assert not re.search(r"console\.\w+\(", line), line


# -- the helpers in node -----------------------------------------------------------------------------------------------------

NODE_HARNESS = r"""
global.window = global;
global.document = { addEventListener() {}, createElement() { return {}; } };
global.addEventListener = () => {};
require(process.argv[2]);
const S = window.StreetView;
const out = {};
const P = { lat: 41.009, lon: -73.66 };                     // invented: 100 Example Ave
out.north = S.bearing({ lat: 41.0, lon: -73.66 }, { lat: 41.001, lon: -73.66 });
out.east = S.bearing({ lat: 41.0, lon: -73.66 }, { lat: 41.0, lon: -73.659 });
out.south = S.bearing({ lat: 41.001, lon: -73.66 }, { lat: 41.0, lon: -73.66 });
out.west = S.bearing({ lat: 41.0, lon: -73.659 }, { lat: 41.0, lon: -73.66 });
out.ne = S.bearing({ lat: 41.0, lon: -73.66 }, { lat: 41.0005, lon: -73.66 + 0.0005 / Math.cos(41.0 * Math.PI / 180) });
// a footprint whose centre is 15 m east of the address point (the point on its west edge, the front)
const sq = { type: "Polygon", coordinates: [[[-73.66 - 0.00001, 41.00897], [-73.66 + 0.00035, 41.00897],
  [-73.66 + 0.00035, 41.00903], [-73.66 - 0.00001, 41.00903], [-73.66 - 0.00001, 41.00897]]] };
const far = { type: "Polygon", coordinates: [[[-73.70, 41.1], [-73.69, 41.1], [-73.69, 41.11], [-73.70, 41.1]]] };
out.face = S.facePoint(P, [far, sq]);
out.faceNone = S.facePoint(P, [far]);
out.faceEmpty = S.facePoint(P, []);
out.faceMulti = S.facePoint(P, [{ type: "MultiPolygon", coordinates: [sq.coordinates] }]);
// the pano across the street (west), so it faces east at the building's centre, not the address point's edge
const pano = { lat: 41.009, lon: -73.6603 };
out.headPoint = S.bearing(pano, P);
out.headBuilding = S.bearing(pano, out.face);
out.centroid = S.centroid([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]);
out.inRing = [S.inRing([1, 1], [[0, 0], [2, 0], [2, 2], [0, 2]]), S.inRing([3, 1], [[0, 0], [2, 0], [2, 2], [0, 2]])];
out.dates = ["2024-06", "2019-12", "2024-6", "", null, "2024-13", "junk"].map(S.dateLabel);
out.modes = [S.mode({ provider: "google", key: "k" }, false), S.mode({ provider: "google", key: "k" }, true),
             S.mode({ provider: "" }, true), S.mode({ provider: "" }, false), S.mode(null, true)];
out.items = ["on", "notice", "off"].map(S.menuItems);
out.url = S.scriptUrl("AIza-k_1", "__cb");
out.radius = S.RADIUS_M;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_helpers_in_node(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(NODE_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "streetview.js")], capture_output=True, text=True, check=True,
                                    timeout=30).stdout)
    assert out["north"] == 0 and out["east"] == pytest.approx(90, abs=0.1)
    assert out["south"] == 180 and out["west"] == pytest.approx(270, abs=0.1)
    assert out["ne"] == pytest.approx(45, abs=0.2)
    # facing the building: the footprint the call's point is in, by its centroid; none under the point: the point
    assert out["face"]["lon"] == pytest.approx(-73.66 + 0.00017, abs=1e-6) and out["face"]["lat"] == pytest.approx(41.009, abs=1e-6)
    assert out["faceNone"] == {"lat": 41.009, "lon": -73.66} and out["faceEmpty"] == {"lat": 41.009, "lon": -73.66}
    assert out["faceMulti"] == out["face"]
    assert out["headPoint"] == pytest.approx(90, abs=0.2) and out["headBuilding"] == pytest.approx(90, abs=0.5)
    assert out["centroid"] == [1, 1] and out["inRing"] == [True, False]
    assert out["dates"] == ["Imagery Jun 2024", "Imagery Dec 2019", "Imagery Jun 2024", "", "", "", ""]
    # a key: on for everyone; none: a notice for an admin, nothing for anyone else
    assert out["modes"] == ["on", "on", "notice", "off", "off"]
    on, notice, off = out["items"]
    assert [i["label"] for i in on] == ["Street view here"] and not on[0].get("disabled")
    assert notice[0]["disabled"] is True and notice[0]["hint"] == "key not set"
    assert off == []
    assert out["url"] == "https://maps.googleapis.com/maps/api/js?key=AIza-k_1&v=weekly&loading=async&callback=__cb"
    assert out["radius"] == 50


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_offset_heading_faces_the_building_not_the_address_point(tmp_path):
    """A corner lot: the pano is on the side street, south of the address point; the footprint runs east of it. Facing
    the address point would look north up the side street; the centroid turns it towards the building."""
    h = tmp_path / "h.js"
    h.write_text(r"""
global.window = global;
global.document = { addEventListener() {}, createElement() { return {}; } };
global.addEventListener = () => {};
require(process.argv[2]);
const S = window.StreetView;
const P = { lat: 41.009, lon: -73.66 };
const sq = { type: "Polygon", coordinates: [[[-73.66 - 0.00001, 41.00899], [-73.66 + 0.0003, 41.00899],
  [-73.66 + 0.0003, 41.00915], [-73.66 - 0.00001, 41.00915], [-73.66 - 0.00001, 41.00899]]] };
const pano = { lat: 41.0089, lon: -73.66 };
console.log(JSON.stringify([S.bearing(pano, P), S.bearing(pano, S.facePoint(P, [sq]))]));
""")
    to_point, to_building = json.loads(subprocess.run([NODE, str(h), str(STATIC / "streetview.js")], capture_output=True,
                                                      text=True, check=True, timeout=30).stdout)
    assert to_point == 0
    assert 20 < to_building < 70                                              # north-east, into the building
