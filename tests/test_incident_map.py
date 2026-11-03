"""The incident view's map, resized by dragging (GitHub #210, static/incident-map.js). User: "id also like to change the
inc view so you can pull the map out larger and down more if you want ... id like to be able to make it pull down and to
the left/right".

    the handles   the map's left edge (wider, the details give way), its bottom edge (taller), the bottom-left corner
                  (both); the edges are Panels.separator separators (drag, arrow keys, Home / double-click resets)
    limits        height 300 px to 85% of the window; width 288 px to what leaves the details 280 px (clamp(), pure)
    remembered    per viewer, localStorage "sizeup.incidentMap" {w, h}, only inside a try
    the map       resize() once a frame while dragging and once after; then framed again, or its pin kept in view
    narrow        one column, or a window 1000 px or less: the width is the layout's; the height still applies
The drag itself is checked in a browser (the PR has the screenshots)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


def text(name):
    return (STATIC / name).read_text()


# -- the page -----------------------------------------------------------------------------------------------------

def test_the_handles_are_on_the_map_and_the_script_after_the_panels():
    html = text("cad.html")
    box = html[html.index('<div id="d-mapbox" class="mapbox">'):]
    box = box[:box.index('<div id="d-cams"')]
    for hid, cls in (("d-map-w", "dmap-w"), ("d-map-h", "dmap-h"), ("d-map-c", "dmap-c")):
        assert f'<div id="{hid}" class="dmap-handle {cls}" hidden></div>' in box, hid
    assert '<div id="d-grid" class="inc-grid">' in html
    assert html.index('src="/static/cadmap.js"') < html.index('src="/static/panels.js"') < html.index('src="/static/incident-map.js"')
    # the Cameras line and the trains stay under the map, in its column (so at its width)
    grid = html[html.index('<div id="d-grid"'):html.index('<form id="edit-form"')]
    assert grid.index('id="d-mapbox"') < grid.index('id="d-cams"') < grid.index('id="d-trains"')
    assert ".ml-cams { grid-column: 2;" in text("cadmap.css")


def test_the_css_takes_the_size_and_styles_the_handles():
    css = text("cad.css")
    assert ".inc-grid { display: grid; grid-template-columns: minmax(0, 1fr) var(--dmap-w, minmax(18rem, 48%)); gap: var(--s-4); }" in css
    assert "block-size: var(--dmap-h, var(--map-h));" in css
    assert "@container (width < 44rem) { .inc-grid { grid-template-columns: minmax(0, 1fr); } }" in css   # stacked: full width
    for sel in (".dmap-handle {", ".dmap-handle[hidden] { display: none; }", ".dmap-w {", ".dmap-h {", ".dmap-c {",
                "body.dmap-dragging {", "body:is(.col-dragging, .row-dragging, .dmap-dragging) .dmap-handle.on { pointer-events: auto; }"):
        assert sel in css, sel
    assert re.search(r"\.dmap-w \{[^}]*cursor: col-resize", css) and re.search(r"\.dmap-h \{[^}]*cursor: row-resize", css)
    assert re.search(r"\.dmap-c \{[^}]*cursor: nesw-resize", css)


# -- the source ---------------------------------------------------------------------------------------------------

def test_limits_and_the_narrow_layout():
    src = text("incident-map.js")
    assert "const H_MIN = 300, H_MAX_VH = 0.85;" in src and "const W_MIN = 288, DETAILS_MIN = 280;" in src
    assert "const NARROW_PX = 1000, STEP = 16;" in src
    assert "matchMedia(`(max-width: ${NARROW_PX}px)`)" in src
    assert "wide: !NARROW.matches && cols > 1" in src                       # the grid's own columns: stacked = one
    assert "hW.hidden = hC.hidden = !(on && r.wide);" in src and "hH.hidden = !on;" in src
    assert 'if (c.w == null) grid.style.removeProperty("--dmap-w");' in src


def test_remembered_per_viewer_only_inside_a_try():
    src = text("incident-map.js")
    assert 'const KEY = "sizeup.incidentMap";' in src
    assert "localStorage.setItem(KEY, JSON.stringify({ w: st.w, h: st.h }))" in src
    for m in re.finditer(r"localStorage\.\w+Item", src):
        before = src[:m.start()]
        assert before.rfind("try {") > before.rfind("} catch"), m.group(0)
    lines = text("maplayers.js").splitlines()                                # maplayers.js: still its two helper lines
    assert len([ln for ln in lines if "localStorage." in ln]) == 2


def test_separators_keyboard_and_reset():
    src = text("incident-map.js")
    assert src.count("Panels.separator(") == 2
    assert 'orient: "vertical", grow: -1, label: "Resize the map: wider to the left"' in src     # left edge: wider leftwards
    assert 'orient: "horizontal", grow: 1, label: "Resize the map: taller"' in src
    assert 'reset: () => reset("w")' in src and 'reset: () => reset("h")' in src
    assert 'hC.setAttribute("role", "separator")' in src and "hC.tabIndex = 0" in src
    assert 'hC.addEventListener("dblclick", () => reset("both"))' in src and 'e.key === "Home"' in src
    assert "ArrowLeft: [STEP, 0], ArrowRight: [-STEP, 0], ArrowDown: [0, STEP], ArrowUp: [0, -STEP]" in src
    panels = text("panels.js")
    assert 'hd.addEventListener("dblclick", reset)' in panels and "ArrowLeft: -1, ArrowRight: 1" in panels


def test_the_map_follows_and_keeps_its_view():
    src = text("incident-map.js")
    assert "requestAnimationFrame(() => { frame = 0; Panels.resizeMaps(); })" in src          # the corner: once a frame
    assert "setPointerCapture(e.pointerId)" in src and '"pointercancel"' in src
    end = src[src.index("const end = (ev) =>"):src.index('hC.addEventListener("pointermove", move)')]
    assert "set(width(), height(), true);" in end and "Panels.resizeMaps();" in end
    settled = src[src.index("function settled()"):src.index("function set(")]
    assert "remember();" in settled and "rm.map.resize();" in settled
    assert "if (!rm.userMoved) { rm.fit(true); return; }" in settled
    assert "!rm.map.getBounds().contains([inc.lon, inc.lat])" in settled and "rm.map.easeTo({ center: [inc.lon, inc.lat]" in settled
    cm = text("cadmap.js")
    assert "const detailMap = () => views.detail;" in cm and "detail, detailMap, place" in cm
    # Expand is its own overlay: it never touches the inline size
    big = cm[cm.index("function openBig()"):cm.index("// ---- Overview")]
    assert "dmap" not in big


# -- the arithmetic and the behaviour (node) -------------------------------------------------------------------------

CLAMP = r"""
global.window = global;
require(process.argv[2]);
const { clamp } = window.IncidentMap;
const room = { grid: 1000, gap: 16, vh: 900, wide: true };
console.log(JSON.stringify({
  none: clamp({ w: null, h: null }, room),
  mid: clamp({ w: 500, h: 500 }, room),
  small: clamp({ w: 10, h: 10 }, room),
  big: clamp({ w: 5000, h: 5000 }, room),
  narrow: clamp({ w: 500, h: 500 }, { ...room, wide: false }),
  tiny: clamp({ w: 500, h: 500 }, { grid: 400, gap: 16, vh: 200, wide: true }),
  junk: clamp({ w: NaN, h: Infinity }, room),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_clamp(tmp_path):
    h = tmp_path / "clamp.js"
    h.write_text(CLAMP)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "incident-map.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["none"] == {"w": None, "h": None, "wMax": 704, "hMax": 765}       # 1000 - 16 - 280; 85% of 900
    assert out["mid"] == {"w": 500, "h": 500, "wMax": 704, "hMax": 765}
    assert out["small"]["w"] == 288 and out["small"]["h"] == 300
    assert out["big"]["w"] == 704 and out["big"]["h"] == 765
    assert out["narrow"]["w"] is None and out["narrow"]["h"] == 500             # stacked: the height only
    assert out["tiny"] == {"w": 288, "h": 300, "wMax": 288, "hMax": 300}        # never under the least
    assert out["junk"]["w"] is None and out["junk"]["h"] is None


BEHAVIOUR = r"""
global.window = global;
const store = { v: process.argv[3] === "-" ? null : process.argv[3] };
global.localStorage = { getItem: () => store.v, setItem: (k, v) => { if (k !== "sizeup.incidentMap") throw new Error(k); store.v = v; } };
class Style { constructor() { this.p = {}; } setProperty(k, v) { this.p[k] = v; } removeProperty(k) { delete this.p[k]; } }
class El {
  constructor(id) { this.id = id; this.hidden = false; this.style = new Style(); this.attrs = {}; this.on = {};
    this.classList = { add() {}, remove() {} }; this.clientWidth = 1000; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, fn) { this.on[k] = fn; }
  getBoundingClientRect() {
    const g = els["d-grid"].style.p;
    return { width: parseFloat(g["--dmap-w"]) || 480, height: parseFloat(g["--dmap-h"]) || 360 };
  }
}
const els = {};
for (const id of ["d-grid", "d-mapbox", "d-map", "d-map-w", "d-map-h", "d-map-c"]) els[id] = new El(id);
global.document = { getElementById: (id) => els[id], body: new El("body") };
let cols = "500px 480px", narrow = false;
global.getComputedStyle = () => ({ gridTemplateColumns: cols, columnGap: "16px" });
global.matchMedia = () => ({ get matches() { return narrow; }, addEventListener() {} });
global.ResizeObserver = class { observe() {} };
global.addEventListener = () => {};
global.innerHeight = 900;
let frames = [];
global.requestAnimationFrame = (fn) => { frames.push(fn); return 1; };
const seps = [];
let resized = 0;
global.Panels = { separator: (hd, o) => { seps.push(o); return { sync() {} }; }, resizeMaps: () => { resized++; } };
const calls = [];
const rm = { canvas: { clientWidth: 480, clientHeight: 360 }, userMoved: false, focus: { lat: 41, lon: -73.6 },
  map: { resize: () => calls.push("resize"), getBounds: () => ({ contains: () => false }), easeTo: (o) => calls.push("ease") },
  fit: () => calls.push("fit") };
global.CadMap = { detailMap: () => rm };
require(process.argv[2]);
const IM = window.IncidentMap;
const g = els["d-grid"].style.p;
const flush = () => { const f = frames; frames = []; f.forEach((fn) => fn()); };
const out = { start: { ...g }, handles: [els["d-map-w"].hidden, els["d-map-h"].hidden, els["d-map-c"].hidden],
  labels: seps.map((o) => [o.orient, o.grow, o.min(), o.max()]) };
seps[0].set(600, false); out.dragW = { ...g }; out.storedDuring = store.v;
seps[0].set(5000, true); flush(); out.endW = { ...g }; out.stored = JSON.parse(store.v); out.calls = calls.splice(0);
seps[1].set(700, true); flush(); out.endH = { ...g }; calls.splice(0);
rm.userMoved = true;
els["d-map-c"].on.keydown({ key: "ArrowLeft", preventDefault() {} }); flush();
out.keyC = { ...g }; out.movedCalls = calls.splice(0);
els["d-map-c"].on.keydown({ key: "ArrowUp", preventDefault() {} });
out.keyUp = { ...g };
seps[0].reset(); flush(); out.resetW = { ...g }; out.storedW = JSON.parse(store.v);
narrow = true; IM.apply(); out.narrow = { ...g }; out.narrowHandles = [els["d-map-w"].hidden, els["d-map-h"].hidden, els["d-map-c"].hidden];
narrow = false; cols = "980px"; IM.apply(); out.stacked = { ...g };
cols = "500px 480px"; IM.apply();
els["d-map-c"].on.dblclick(); out.resetBoth = { ...g }; out.storedBoth = JSON.parse(store.v);
console.log(JSON.stringify(out));
"""


def behaviour(tmp_path, stored):
    h = tmp_path / "behaviour.js"
    h.write_text(BEHAVIOUR)
    r = subprocess.run([NODE, str(h), str(STATIC / "incident-map.js"), stored], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dragging_keys_reset_and_narrow(tmp_path):
    out = behaviour(tmp_path, "-")
    assert out["start"] == {} and out["handles"] == [False, False, False]
    assert out["labels"] == [["vertical", -1, 288, 704], ["horizontal", 1, 300, 765]]
    assert out["dragW"] == {"--dmap-w": "600px"} and out["storedDuring"] is None        # remembered when it ends
    assert out["endW"] == {"--dmap-w": "704px"} and out["stored"] == {"w": 704, "h": None}
    assert out["calls"] == ["resize", "fit"]                                            # not moved: framed again
    assert out["endH"] == {"--dmap-w": "704px", "--dmap-h": "700px"}
    assert out["keyC"] == {"--dmap-w": "704px", "--dmap-h": "700px"}                      # at the most already
    assert out["movedCalls"] == ["resize", "ease"]                                      # moved: the pin back in view
    assert out["keyUp"]["--dmap-h"] == "684px"
    assert out["resetW"] == {"--dmap-h": "684px"} and out["storedW"] == {"w": None, "h": 684}
    assert out["narrow"] == {"--dmap-h": "684px"} and out["narrowHandles"] == [True, False, True]
    assert out["stacked"] == {"--dmap-h": "684px"}
    assert out["resetBoth"] == {} and out["storedBoth"] == {"w": None, "h": None}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_remembered_size_and_a_bad_one(tmp_path):
    assert behaviour(tmp_path, '{"w": 520, "h": 2000}')["start"] == {"--dmap-w": "520px", "--dmap-h": "765px"}
    assert behaviour(tmp_path, "not json")["start"] == {}
    assert behaviour(tmp_path, '{"w": -4, "h": "x"}')["start"] == {}
