"""The Search view's map, made taller or shorter by dragging its bottom edge (static/search-view.js). User: "in the search
page, we should be able to drag the map down/up like in other places".

    the handle    the incident map's bottom-edge handle (.dmap-handle .dmap-h), a Panels.separator (drag, arrow keys,
                  Home / double-click resets)
    limits        H_MIN px to 85% of the window, again when the window changes (clampH(), pure)
    remembered    per viewer, localStorage "sizeup.searchMap" {h}, only inside a try
    the map       MapLibre's resize() once a frame while dragging and once after
Synthetic only: no calls, addresses or units here."""
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


def test_the_handle_is_on_the_map_and_the_css_takes_the_height():
    html = text("cad.html")
    box = html[html.index('<div id="sv-mapbox" class="sv-mapbox">'):html.index('<div class="sv-results">')]
    assert '<div id="sv-map-h" class="dmap-handle dmap-h"></div>' in box
    css = text("cad.css")
    assert "block-size: var(--sv-map-h, calc(var(--map-h) * 0.75));" in css
    rule = re.search(r"\.sv-mapbox \{([^}]*)\}", css).group(1)
    assert "overflow: hidden" not in rule                                   # the handle reaches out of the box
    assert ".sv-mapbox > :is(.sv-map, .nomap) { border-radius: calc(var(--radius) - var(--bw)); overflow: hidden; }" in css
    assert "body.row-dragging .sv-mapbox > :not(.dmap-handle) { pointer-events: none; }" in css
    assert re.search(r"\.dmap-handle \{[^}]*touch-action: none", css)        # no page scroll while dragging


def test_the_shared_separator_and_storage_in_try():
    src = text("search-view.js")
    assert src.count("Panels.separator(") == 1
    assert 'orient: "horizontal", grow: 1, label: "Resize the map", controls: "sv-mapbox"' in src
    assert 'const H_KEY = "sizeup.searchMap", H_MIN = 160, H_MAX_VH = 0.85;' in src
    for m in re.finditer(r"localStorage\.\w+Item", src):
        before = src[:m.start()]
        assert before.rfind("try {") > before.rfind("} catch"), m.group(0)
    assert 'addEventListener("resize", sizeMap)' in src
    assert "requestAnimationFrame(() => { sz.frame = 0; mapResize(); })" in src
    panels = text("panels.js")                                               # the pointer, the keys, the reset: shared
    assert "setPointerCapture(e.pointerId)" in panels and '"pointercancel"' in panels
    html = text("cad.html")
    assert html.index('src="/static/search-view.js"') < html.index('src="/static/panels.js"')   # hence made lazily


CLAMP = r"""
global.window = global;
require(process.argv[2]);
const { clampH } = window.SearchView;
console.log(JSON.stringify([clampH(null, 900), clampH(400, 900), clampH(10, 900), clampH(5000, 900),
  clampH(400, 100), clampH(NaN, 900), clampH(-5, 900), clampH(400.4, 900)]));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_clamp(tmp_path):
    h = tmp_path / "clamp.js"
    h.write_text(CLAMP)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "search-view.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == [None, 400, 160, 765, 160, None, None, 400]                 # 85% of 900 = 765; never under 160


BEHAVIOUR = r"""
global.window = global;
const store = { v: process.argv[3] === "-" ? null : process.argv[3] };
global.localStorage = { getItem: () => { if (store.v === "throw") throw new Error("no"); return store.v; },
  setItem: (k, v) => { if (k !== "sizeup.searchMap") throw new Error(k); if (store.v === "throw") throw new Error("no"); store.v = v; } };
class Style { constructor() { this.p = {}; } setProperty(k, v) { this.p[k] = v; } removeProperty(k) { delete this.p[k]; } }
const els = {};
class El {
  constructor(id) { this.id = id; this.style = new Style(); this.on = {}; this.options = []; this.value = ""; this.tagName = "DIV"; }
  setAttribute() {}
  addEventListener(k, fn) { this.on[k] = fn; }
  getBoundingClientRect() { return { height: parseFloat(this.style.p["--sv-map-h"]) || 270 }; }
}
global.$ = (sel) => (els[sel] = els[sel] || new El(sel));
const docOn = {}, winOn = {};
global.document = { querySelectorAll: () => [], addEventListener: (k, fn) => { docOn[k] = fn; } };
global.addEventListener = (k, fn) => { winOn[k] = fn; };
global.innerHeight = 900;
global.cad = { hooks: [] };
let frames = [];
global.requestAnimationFrame = (fn) => { frames.push(fn); return 1; };
require(process.argv[2]);
const box = () => $("#sv-mapbox").style.p;
const out = { start: { ...box() } };
const seps = [];
global.Panels = { separator: (hd, o) => { seps.push([hd.id, o]); return { sync() {} }; } };
docOn.DOMContentLoaded(); docOn.DOMContentLoaded();
const o = seps[0][1];
out.seps = seps.map(([id, s]) => [id, s.orient, s.grow, s.min(), s.max(), s.controls]);
o.set(500, false); o.set(520, false); out.drag = { ...box() }; out.frames = frames.length; out.storedDuring = store.v;
console.log(JSON.stringify(out));
"""

BEHAVIOUR_MAP = r"""
o.set(5000, true); out.end = { ...box() }; out.stored = store.v;
innerHeight = 400; winOn.resize(); out.small = { ...box() };
innerHeight = 900; winOn.resize(); out.back = { ...box() };
o.reset(); out.reset = { ...box() }; out.storedReset = store.v;
console.log(JSON.stringify(out));
"""


def behaviour(tmp_path, stored, more=""):
    src = BEHAVIOUR if not more else BEHAVIOUR.replace("console.log(JSON.stringify(out));\n", "") + more
    h = tmp_path / "behaviour.js"
    h.write_text(src)
    r = subprocess.run([NODE, str(h), str(STATIC / "search-view.js"), stored], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dragging_window_and_reset(tmp_path):
    out = behaviour(tmp_path, "-", BEHAVIOUR_MAP)
    assert out["start"] == {}
    assert out["seps"] == [["#sv-map-h", "horizontal", 1, 160, 765, "sv-mapbox"]]   # made once
    assert out["drag"] == {"--sv-map-h": "520px"} and out["frames"] == 1 and out["storedDuring"] is None
    assert out["end"] == {"--sv-map-h": "765px"} and json.loads(out["stored"]) == {"h": 765}
    assert out["small"] == {"--sv-map-h": "340px"}                           # the window shrank: re-clamped
    assert out["back"] == {"--sv-map-h": "765px"}                            # what was asked for is kept
    assert out["reset"] == {} and json.loads(out["storedReset"]) == {"h": None}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_remembered_height_a_bad_one_and_no_storage(tmp_path):
    assert behaviour(tmp_path, '{"h": 420}')["start"] == {"--sv-map-h": "420px"}
    assert behaviour(tmp_path, '{"h": 9000}')["start"] == {"--sv-map-h": "765px"}
    for bad in ("not json", '{"h": "x"}', '{"h": -4}', "null", "5"):
        assert behaviour(tmp_path, bad)["start"] == {}, bad
    out = behaviour(tmp_path, "throw", BEHAVIOUR_MAP)                         # storage refuses: this page view only
    assert out["start"] == {} and out["end"] == {"--sv-map-h": "765px"}
