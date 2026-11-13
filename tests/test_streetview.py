"""Street view on the CAD maps (static/streetview.js; the key: sizeup/config.py [streetview], GET /api/cad/streetview).

Everything here is invented: the key is a made-up string of the right shape, and the points are on the invented
Example Ave grid (tests/test_preplan.py: lat 41.0 + n * 0.00009, lon -73.66)."""
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sizeup import config as config_mod
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad
from sizeup.cloud.access import Identity
from tests.test_access import jwks, keys  # noqa: F401 (fixtures)
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


def test_a_malformed_key_is_off_not_a_crash_and_never_repeated(tmp_path, monkeypatch):
    """A hand-edited SSM value must not stop the hub: street view is off, one line says so, never the value."""
    p = tmp_path / "c.toml"
    p.write_text("")
    bad = "AIza secret with spaces 1234567890"
    monkeypatch.setenv("SIZEUP_GOOGLE_MAPS_KEY", bad)
    c = config_mod.load(p)
    assert c.streetview_google_key == "" and c.streetview_key_bad is True
    monkeypatch.setenv("SIZEUP_GOOGLE_MAPS_KEY", KEY)
    assert config_mod.load(p).streetview_key_bad is False
    p.write_text('[streetview]\nbogus = 1\n')                              # an unknown key in the file: as every section
    monkeypatch.delenv("SIZEUP_GOOGLE_MAPS_KEY")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(p)


def test_the_hub_starts_with_a_malformed_key_and_logs_one_line(tmp_path, monkeypatch, capsys):
    from dataclasses import replace
    from tests.test_cloud import HubRun, hub_cfg
    p = tmp_path / "c.toml"
    p.write_text("")
    bad = "AIza secret with spaces 1234567890"
    monkeypatch.setenv("SIZEUP_GOOGLE_MAPS_KEY", bad)
    loaded = config_mod.load(p)
    h = HubRun(replace(hub_cfg(tmp_path), streetview_google_key=loaded.streetview_google_key,
                       streetview_key_bad=loaded.streetview_key_bad))
    try:
        code, body = _http(h.url + "/api/cad/streetview")
        assert code == 200 and body == {"provider": ""}
    finally:
        h.close()
    out = capsys.readouterr()
    text = out.out + out.err
    assert text.count("street view off") == 1
    assert "secret" not in text and "1234567890" not in text


def _http(url, headers=None):
    r = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


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


# -- the map's gestures in node, on a small fake DOM: pick mode, the long press, Esc ------------------------------------------

GESTURES = r"""
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
class El {
  constructor(tag, cls = "") { this.tag = tag; this.className = cls; this.children = []; this.attrs = {}; this.style = {};
    this.parentNode = null; this.hidden = false; this.ls = {}; this.id = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false }; }
  set textContent(t) { this._t = t; } get textContent() { return this._t || ""; }
  append(...c) { for (const x of c) if (x && typeof x === "object") { x.parentNode = this; this.children.push(x); } }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter((c) => c !== this); this.parentNode = null; } }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  matches(sel) { const [tag, cls] = sel.includes(".") ? sel.split(".") : [sel, ""];
    return (!tag || this.tag === tag) && (!cls || (" " + this.className + " ").includes(" " + cls + " ")); }
  closest(sel) { for (let x = this; x; x = x.parentNode) if (x.matches && x.matches(sel)) return x; return null; }
  querySelector(sel) { for (const c of this.children) { if (c.matches(sel)) return c; const d = c.querySelector(sel); if (d) return d; } return null; }
  focus() {} getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600 }; }
  get offsetWidth() { return 100; } get offsetHeight() { return 40; }
}
const winLs = {};
global.window = global;
global.addEventListener = (t, f, cap) => { (winLs[t] = winLs[t] || []).push(f); };
const docLs = {};
global.document = { body: new El("body"), head: new El("head"), documentElement: { clientWidth: 1000 },
  createElement: (t) => new El(t), addEventListener: (t, f) => { (docLs[t] = docLs[t] || []).push(f); },
  querySelector: (sel) => document.body.querySelector(sel) };
global.innerHeight = 800;
global.fetch = async () => ({ ok: true, json: async () => ({ provider: "google", key: "k" }) });
let VIEW = "overview";
global.cad = { who: { enforced: false }, sel: 7, hooks: [] };
global.canDo = () => true;
global.viewTab = () => VIEW;
const groups = [];
global.MapLayers = { addGroup(g) { groups.push(g); } };

function fakeMap(hostId) {
  const host = new El("div", "cm-host"); host.id = hostId || "";
  const container = new El("div", "cm-canvas"), cc = new El("div", "maplibregl-canvas-container");
  const canvas = new El("canvas", "maplibregl-canvas"); canvas.style.cursor = "grab";
  const marker = new El("div", "maplibregl-marker"), chip = new El("span", "cm-rig");
  marker.append(chip); cc.append(canvas, marker); container.append(cc); host.append(container);
  const ctrls = new El("div", "cm-ctrls"), layers = new El("button", "cm-btn sm"); ctrls.append(layers); host.append(ctrls);
  const ml = {};
  const map = { on(t, f) { (ml[t] = ml[t] || []).push(f); }, getCanvas: () => canvas, getCanvasContainer: () => cc,
    getContainer: () => container, unproject: ([x, y]) => ({ lat: 41 + y * 1e-5, lng: -73.66 + x * 1e-5 }),
    project: () => [0, 0], getStyle: () => ({ layers: [] }), queryRenderedFeatures: () => [] };
  const control = { map, host, btn: layers, focused: null };
  for (const g of groups) g.attach(control);
  return { map, ml, canvas, cc, marker, chip, ctrls, control, others: 0, btn: ctrls.children[1] };
}
// a click as the browser delivers it: the window's capturing listeners first, then the target's (the map's own)
function click(m, target, x = 50, y = 60) {
  const ev = { type: "click", target, clientX: x, clientY: y, stopped: false, dp: false,
    stopPropagation() { this.stopped = true; }, preventDefault() { this.dp = true; } };
  for (const f of winLs.click || []) { f(ev); if (ev.stopped) return ev; }
  m.others++;                                   // newcall-map clicked(), a pre-plan placement, a firehouse's rig list ...
  return ev;
}
function down(x, y) { for (const f of docLs.pointerdown || []) f({ clientX: x, clientY: y, target: document.body }); }
function key(k, prevented = false) {
  const ev = { key: k, defaultPrevented: prevented, preventDefault() { this.defaultPrevented = true; this.dp = true; } };
  for (const f of winLs.keydown || []) f(ev);
  return ev;
}
const touch = (el, t, x = 50, y = 60) => { for (const f of el.ls[t] || []) f({ target: el === undefined ? null : el._t || el,
  touches: t === "touchend" ? [] : [{ clientX: x, clientY: y }] }); };
function touchOn(m, target, t) { for (const f of m.cc.ls[t] || []) f({ target, touches: t === "touchend" ? [] : [{ clientX: 50, clientY: 60 }] }); }
const menus = () => document.body.children.filter((c) => c.matches(".sv-menu"));

(async () => {
  require(process.argv[2]);
  const S = window.StreetView, out = {};
  const m = fakeMap();
  await sleep(20);                                  // the key arrives
  out.btnShown = !m.btn.hidden;
  // pick mode: the armed click is the pick, nobody else's; the cursor as it was before
  m.btn.ls.click[0]({ stopPropagation() {} });
  out.armed = m.btn.attrs["aria-pressed"]; out.cursorArmed = m.canvas.style.cursor;
  down(50, 60); const ev = click(m, m.canvas, 50, 60);
  out.pick = { swallowed: ev.stopped && ev.dp, others: m.others, at: S.lastAt(), pressed: m.btn.attrs["aria-pressed"],
               cursor: m.canvas.style.cursor };
  // a pan while armed (moved between down and up) is not a pick
  m.btn.ls.click[0]({ stopPropagation() {} });
  down(10, 10); const pan = click(m, m.canvas, 100, 100);
  out.pan = { taken: pan.stopped, pressed: m.btn.attrs["aria-pressed"] };
  // a rig chip (a marker) on the armed map is the pick too, not the rig's click
  down(5, 5); const onChip = click(m, m.chip, 5, 5);
  out.chip = { taken: onChip.stopped, pressed: m.btn.attrs["aria-pressed"] };
  // Esc: one already taken (#464's alignment, a dialog) leaves the pick alone; a free one ends it
  m.btn.ls.click[0]({ stopPropagation() {} });
  key("Escape", true); out.escTaken = m.btn.attrs["aria-pressed"];
  const e2 = key("Escape"); out.escFree = [m.btn.attrs["aria-pressed"], !!e2.dp];
  // a change of view (cad.js render hooks) or of the hash ends it
  m.btn.ls.click[0]({ stopPropagation() {} });
  VIEW = "radio"; for (const f of cad.hooks) f(); out.viewChange = m.btn.attrs["aria-pressed"]; VIEW = "overview";
  m.btn.ls.click[0]({ stopPropagation() {} });
  for (const f of winLs.hashchange || []) f({}); out.hashChange = m.btn.attrs["aria-pressed"];
  // never while the pre-plan editor places a point
  const form = new El("form", "pp-form card"); document.body.append(form);
  m.btn.ls.click[0]({ stopPropagation() {} }); out.whileEditing = m.btn.attrs["aria-pressed"];
  form.remove();
  // the new call's map: no button without a call, never armed
  const nc = fakeMap("nc-map"); await sleep(5); for (const f of cad.hooks) f();
  out.newCall = { hidden: nc.btn.hidden };
  nc.btn.ls.click[0]({ stopPropagation() {} }); out.newCall.pressed = nc.btn.attrs["aria-pressed"];
  down(50, 60); const ncClick = click(nc, nc.canvas); out.newCall.mapGotIt = !ncClick.stopped && nc.others === 1;
  // the long press: a menu; the lift's click swallowed, the next one not
  touchOn(m, m.canvas, "touchstart"); await sleep(S.LONG_MS + 100);
  out.lpMenus = menus().length;
  const first = menus()[0];
  // Android's contextmenu for the same press: no second menu
  for (const f of m.ml.contextmenu) f({ originalEvent: { clientX: 50, clientY: 60, preventDefault() {} }, point: { x: 50, y: 60 }, lngLat: { lat: 41, lng: -73.66 } });
  out.lpOnce = menus().length === 1 && menus()[0] === first;
  touchOn(m, m.canvas, "touchend");
  const before = m.others; const lift = click(m, m.canvas);
  out.lift = { swallowed: lift.stopped, others: m.others - before };
  const later = click(m, m.canvas); out.nextClick = !later.stopped;
  first.remove();
  // a long press on a mark (a rig, a dragged pre-plan point): nothing
  await sleep(900);
  touchOn(m, m.chip, "touchstart"); await sleep(S.LONG_MS + 100);
  out.onMarker = menus().length;
  touchOn(m, m.chip, "touchend");
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pick_mode_long_press_and_esc_on_a_fake_page(tmp_path):
    h = tmp_path / "g.js"
    h.write_text(GESTURES)
    r = subprocess.run([NODE, str(h), str(STATIC / "streetview.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["btnShown"] is True
    assert out["armed"] == "true" and out["cursorArmed"] == "crosshair"
    p = out["pick"]
    assert p["swallowed"] and p["others"] == 0                               # nobody else saw the click
    assert p["at"] == {"lat": pytest.approx(41 + 60e-5), "lon": pytest.approx(-73.66 + 50e-5)}
    assert p["pressed"] == "false" and p["cursor"] == "grab"                 # the cursor it had, not ""
    assert out["pan"] == {"taken": False, "pressed": "true"}
    assert out["chip"] == {"taken": True, "pressed": "false"}
    assert out["escTaken"] == "true" and out["escFree"] == ["false", True]
    assert out["viewChange"] == "false" and out["hashChange"] == "false"
    assert out["whileEditing"] == "false"
    assert out["newCall"] == {"hidden": True, "pressed": "false", "mapGotIt": True}
    assert out["lpMenus"] == 1 and out["lpOnce"] is True
    assert out["lift"] == {"swallowed": True, "others": 0} and out["nextClick"] is True
    assert out["onMarker"] == 0


def test_the_pre_plan_editor_is_still_seen_by_its_form():
    """streetview.js knows the editor is on by its form (preplan.js edit(): form.pp-form, gone on cancelEdit)."""
    pp = (STATIC / "preplan.js").read_text()
    assert 'const f = el("form", "pp-form card");' in pp
    assert "if (e.host) { e.host.replaceChildren(); show(e.host, false); }" in pp
    assert 'const ppEditing = () => !!document.querySelector("form.pp-form");' in (STATIC / "streetview.js").read_text()
    assert '$("#nc-map")' in (STATIC / "newcall-map.js").read_text()          # the new call's map, by its host


# -- the endpoint behind Cloudflare Access, and on a station ------------------------------------------------------------------

def test_behind_access_a_viewer_gets_the_key_and_nobody_unsigned_does(tmp_path, keys, jwks):
    from tests.test_access import AUD, TEAM, jwt
    from tests.test_cloud import HubRun, hub_cfg
    from tests.test_roles import VAL, people
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD, streetview_google_key=KEY))
    try:
        h.server.access.certs_url = jwks.url
        h.server.access.log = lambda m: None
        people(h.app.cad)
        code, body = _http(h.url + "/api/cad/streetview")
        assert code == 401 and KEY not in json.dumps(body if isinstance(body, dict) else {"raw": body.decode()})
        code, body = _http(h.url + "/api/cad/streetview", {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=VAL)})
        assert code == 200 and body == {"provider": "google", "key": KEY}      # a viewer looks at the map too
    finally:
        h.close()


def test_a_station_refuses_it_like_the_rest_of_the_cad(tmp_path):
    from tests.test_station_page import raw, serve, station_app
    app = station_app(tmp_path)
    server, port = serve(app)
    try:
        code, _, body = raw(port, "/api/cad/streetview")
        assert code == 409 and "the CAD is on the hub" in json.loads(body)["error"]
    finally:
        server.shutdown()
        app.stop()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_headings_stay_in_0_to_359(tmp_path):
    h = tmp_path / "d.js"
    h.write_text(r"""
global.window = global;
global.document = { addEventListener() {}, createElement() { return {}; } };
global.addEventListener = () => {};
require(process.argv[2]);
const S = window.StreetView;
const near = S.bearing({ lat: 41.0, lon: -73.66 }, { lat: 41.001, lon: -73.66 - 1e-9 });   // a hair west of north
console.log(JSON.stringify({ labels: [359.6, 359.4, 360, -0.4, 0, 720.2, -90].map(S.degLabel), near }));
""")
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "streetview.js")], capture_output=True, text=True, check=True,
                                    timeout=30).stdout)
    assert out["labels"] == ["0°", "359°", "0°", "0°", "0°", "0°", "270°"]
    assert 0 <= out["near"] < 360 and out["near"] in (0, 359.9)
