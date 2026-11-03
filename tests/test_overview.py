"""The Overview after a dispatcher's feedback (GitHub #216, #217, #218).

  #216  the top nav: Overview · Incident · Radio · Settings ("Dispatch" became "Incident"; its id and the view's key,
        "dispatch", stay; it never had a URL of its own). G then I goes there, G then D still does.
  #217  the console's Overview no longer repeats the calls: the queue lists them (the same on every view); the wall
        board, with no queue, keeps its cards. What only the cards had (who is talking on the call, its newest
        transmission and note) is on the queue's cards now.
  #218  coming back from an incident, the Overview showed Long Island: it framed every open call, wherever its point
        was. A map with no call open frames our area (scene home.bounds: Port Chester and Rye Brook) and the open calls
        with a real point within 12 km of home (not Long Island, across the Sound), never a layer's features; it
        frames again when the calls change (as Recenter does), not on a view switch the viewer had moved the map before.

The maps' code runs in node (skipped where node isn't installed); the source checks run everywhere."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

AREA = [-73.7067, 40.9866, -73.6552, 41.0739]          # sizeup/roads.py AREA_BBOX
HOME = {"center": [-73.6640, 41.0130], "zoom": 13.4, "bounds": AREA}
HOME_BOX = [[AREA[0], AREA[1]], [AREA[2], AREA[3]]]


def text(name):
    return (STATIC / name).read_text()


# ---- #216: the nav ------------------------------------------------------------------------------------------------


def test_the_tabs_are_overview_incident_radio_settings():
    html = text("cad.html")
    nav = re.search(r'<nav class="tabs console-only"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r'id="([\w-]+)" class="tab', nav) == ["overview-open", "dispatch-open", "radio-open", "settings-open"]
    assert re.findall(r'<span class="lbl">([^<]+)</span>', nav) == ["Overview", "Incident", "Radio", "Settings"]
    assert ">Dispatch<" not in nav
    cad = text("cad.js")
    assert '[["#overview-open", "overview"], ["#dispatch-open", "dispatch"], ["#radio-open", "radio"], ["#settings-open", "settings"]]' in cad
    # the keyboard: G I, and G D as before; the "?" sheet lists the tabs in their order
    assert 'gi: () => $("#dispatch-open").click(),' in cad and 'gd: () => $("#dispatch-open").click(),' in cad
    ui = text("ui.js")
    assert '[["G", "O"], "Go to Overview"],\n    [["G", "I"], "Go to Incident"],' in ui
    assert "Go to Dispatch" not in ui
    assert "(Overview, Incident, Radio, Settings)" in (ROOT / "docs" / "cloud.md").read_text()


# ---- #217: the calls once -----------------------------------------------------------------------------------------


def test_the_console_overview_has_no_cards_of_its_own_the_board_keeps_them():
    html = text("cad.html")
    side = re.search(r'<div class="ov-side board-only">(.*?)</div>\s*</div>\s*</div>', html, re.S).group(1)
    assert 'id="ov-cards"' in side and 'id="ov-nonemerg"' in side           # the wall board's, hidden on the console
    assert "events" not in re.search(r'<span id="ov-layout".*?</span>', html, re.S).group(0)   # no events column to name
    cadmap = text("cadmap.js")
    assert 'if (BOARD) syncList($("#ov-cards"), emerg, {' in cadmap and 'if (BOARD) syncList($("#ov-nonemerg"), ne, {' in cadmap
    css = text("cadmap.css")
    assert "html:not(.board) .ov-grid, html:not(.board) .ov-grid[data-layout] { grid-template-columns: minmax(0, 1fr); }" in css
    # the layouts and the map stay
    for layout in ("map", "cameras", "side", "stacked"):
        assert f'data-layout="{layout}"' in html
    assert '<div class="ov-mapbox"><div id="ov-map"></div></div>' in html


def test_the_queue_card_carries_what_only_the_overview_cards_had():
    cad = text("cad.js")
    fill = cad[cad.index("function fillQueueItem("):cad.index("// The phone pre-alert")]
    assert "callSummary(inc.id)" in fill and "callLive(o)" in fill
    assert '(live.length ? " live" : "")' in fill                                          # the card lit
    assert 'if (txNow(a.name)) c.classList.add("talking");' in fill                        # the rig's chip ringed
    assert 'el("div", "qs q-live", `TX ${' in fill                                       # who, on which channel
    assert fill.count('el("div", "qs q-last")') == 2 and "o.radio.text" in fill and "o.note.text" in fill
    assert "queueExtra(i)]);" in cad                                                       # redrawn when they change
    css = text("cad.css")
    assert 'body:not([data-view="overview"]) .q-last { display: none; }' in css             # those two: the Overview's
    assert ".q > li.qi.live {" in css and ".qi .chip.talking {" in css


LIVE = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (re) => { const m = src.match(re); if (!m) throw new Error("not found: " + re); return m[0]; };
global.cad = { state: null };
eval(grab(/^function callSummary\(id\) \{[\s\S]*?^\}/m) + "\n" + grab(/^function callLive\(o\) \{[\s\S]*?^\}/m)
     + "\nglobal.callSummary = callSummary; global.callLive = callLive;");
cad.state = {
  overview: [{ id: 1, units: [{ name: "E60" }], channels: ["FG1"] }, { id: 2, units: [{ name: "L31" }], channels: [] }],
  talking: [{ key: "a", rig: "E60", channel: "FG1" }, { key: "b", rig: "", channel: "FG1" },
            { key: "c", rig: "L31", channel: "FG2", ended: true }, { key: "d", rig: "E64", channel: "FG2" }],
};
console.log(JSON.stringify({ one: callLive(callSummary(1)).map((e) => e.key), two: callLive(callSummary(2)).map((e) => e.key),
                             closed: callLive(callSummary(9)) }));
"""


@needs_node
def test_who_is_talking_on_a_call(tmp_path):
    h = tmp_path / "live.js"
    h.write_text(LIVE)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True, check=True,
                                    timeout=30).stdout)
    assert out == {"one": ["a", "b"], "two": [], "closed": []}    # its rig, or its channel with no rig; never an ended one


# ---- #218: the area's frame ---------------------------------------------------------------------------------------


HARNESS = r"""
const fs = require("fs");
global.window = global;
global.document = { getElementById: () => null };
eval(fs.readFileSync(process.argv[2], "utf8"));
const IN = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
"""


def node(tmp_path, body: str, data=None):
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    h.write_text(HARNESS + body + "\nconsole.log(JSON.stringify(out));\n")
    d.write_text(json.dumps(data or {}))
    r = subprocess.run([NODE, str(h), str(STATIC / "cadmap.js"), str(d)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# calls across the Sound (Glen Cove, 17 km; Bayville, Long Island's nearest shore, 14 km: inside the new-call form's
# range), one 200 km off, one at 0,0, one with no point, strings, and a fire district's polygon as the county sends it
# (out over the water)
FAR = [{"id": 1, "lat": 40.862, "lon": -73.633}, {"id": 8, "lat": 40.910, "lon": -73.562}, {"id": 2, "lat": 40.7, "lon": -75.9},
       {"id": 3, "lat": 0, "lon": 0},
       {"id": 4, "lat": 41.0, "lon": 0}, {"id": 5, "lat": None, "lon": None}, {"id": 6, "lat": "41.0", "lon": "-73.67"},
       {"id": 7, "lon": -73.67},
       {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[-73.7, 41.07], [-73.6, 40.85], [-73.8, 40.85],
                                                                              [-73.7, 41.07]]]}}]


@needs_node
def test_far_points_bad_points_and_polygons_leave_the_home_bounds(tmp_path):
    out = node(tmp_path, """
out.far = CadMap.areaBounds(IN.home, IN.far);
out.none = CadMap.areaBounds(IN.home, []);
out.nothing = CadMap.areaBounds(IN.home, undefined);
out.nan = CadMap.areaBounds(IN.home, [{ lat: NaN, lon: -73.67 }, { lat: Infinity, lon: -73.67 }, { lat: 95, lon: -73.67 }]);
out.calls = CadMap.areaCalls(IN.home, IN.far);
out.km = CadMap.AREA_KM;
""", {"home": HOME, "far": FAR})
    assert out["km"] == 12                       # Long Island's north shore is 14 km and more from home: never framed
    assert out["far"] == HOME_BOX and out["none"] == HOME_BOX and out["nothing"] == HOME_BOX and out["nan"] == HOME_BOX
    assert out["calls"] == []


@needs_node
def test_calls_in_our_area_widen_the_frame_and_only_they_do(tmp_path):
    out = node(tmp_path, """
out.in = CadMap.areaBounds(IN.home, IN.calls);
out.calls = CadMap.areaCalls(IN.home, IN.calls);
out.inside = CadMap.areaBounds(IN.home, [{ lat: 41.0, lon: -73.67 }]);
""", {"home": HOME, "calls": [{"id": 1, "lat": 40.9706, "lon": -73.6865},       # Rye: a mutual-aid call, 5 km off
                               {"id": 2, "lat": 41.0262, "lon": -73.6282},       # Greenwich, the far side of the line
                               {"id": 4, "lat": 41.053, "lon": -73.539},         # Stamford, 11 km: still ours to frame
                               {"id": 3, "lat": 40.862, "lon": -73.633}]})       # Long Island: left out
    assert out["calls"] == [[-73.6865, 40.9706], [-73.6282, 41.0262], [-73.539, 41.053]]
    assert out["in"] == [[-73.7067, 40.9706], [-73.539, 41.0739]]
    assert out["inside"] == HOME_BOX                                                # a call in the area: the area


@needs_node
def test_no_bounds_from_an_older_hub_is_a_box_around_home(tmp_path):
    out = node(tmp_path, """
out.box = CadMap.areaBounds({ center: [-73.664, 41.013], zoom: 13.4 }, [{ lat: 40.862, lon: -73.633 }]);
out.bad = CadMap.areaBounds({ center: [-73.664, 41.013], bounds: [0, 0, 0, 0] }, []);
out.nohome = CadMap.areaBounds(null, []);
""")
    box = [-73.684, 40.993, -73.644, 41.033]
    assert sum(out["box"], []) == pytest.approx(box) and sum(out["bad"], []) == pytest.approx(box)
    assert out["nohome"] is None


# RigMap.update and fit on a stand-in map: what the Overview's map does as the calls and the views change
RIGMAP = r"""
const fits = [], moves = [];
const rm = Object.create(CadMap.RigMap.prototype);
Object.assign(rm, {
  canvas: { clientWidth: 900, clientHeight: 700 }, note: {}, opts: { scene: IN.scene }, roads: null,
  size: "", focusId: undefined, fitKey: null, userMoved: false, place: null,
  map: { resize() {}, fitBounds(b, o) { moves.push(["fitBounds", b, o.duration]); },
         jumpTo(o) { moves.push(["jumpTo", o]); }, easeTo(o) { moves.push(["easeTo", o]); } },
  layer: { data: null, focus: null, update(d, f) { this.data = d; this.focus = f || null; }, closePicker() {},
           focusPoints: CadMap.RigLayer.prototype.focusPoints },
});
const realFit = CadMap.RigMap.prototype.fit;
rm.fit = function (animate) { fits.push(animate); return realFit.call(this, animate); };
const data = (incidents) => ({ map: { incidents, markers: [], stations: [] }, lists: {} });
const step = (name, d) => { const n = fits.length; rm.update(d, null, ""); out[name] = fits.slice(n); };
const A = [{ id: 1, lat: 41.0, lon: -73.67 }, { id: 2, lat: 40.862, lon: -73.633 }];   // one here, one across the Sound
step("first", data(A));
out.firstMove = moves[0];
rm.userMoved = true;                                   // the viewer pans; an incident opens and the Overview comes back:
step("back", data(A));                                 // nothing new: left where the viewer put it
rm.canvas.clientWidth = 0; rm.canvas.clientHeight = 0; step("hidden", data(A));
rm.canvas.clientWidth = 900; rm.canvas.clientHeight = 700; step("shown", data(A));    // its box again: still left
step("farCall", data([...A, { id: 3, lat: 0, lon: 0 }]));       // a call with no real point: not a change to the frame
out.movedAfterFar = rm.userMoved;
step("newCall", data([...A, { id: 4, lat: 41.02, lon: -73.68 }]));   // a new call here: all of them again (Recenter)
out.newMove = moves[moves.length - 1];
out.movedAfterNew = rm.userMoved;
"""


@needs_node
def test_the_overview_frames_the_area_and_again_only_when_the_calls_change(tmp_path):
    scene = {"home": HOME}
    out = node(tmp_path, RIGMAP, {"scene": scene})
    assert out["first"] == [False]                                         # framed at once
    assert out["firstMove"][0] == "fitBounds" and out["firstMove"][1] == HOME_BOX   # the area, not Long Island
    assert out["back"] == [] and out["hidden"] == [] and out["shown"] == [] and out["farCall"] == []
    assert out["movedAfterFar"] is True
    assert out["newCall"] == [True] and out["movedAfterNew"] is False      # animated, as the Recenter button does
    assert out["newMove"][0] == "fitBounds" and out["newMove"][1] == HOME_BOX and out["newMove"][2] == 600


def test_the_scene_gives_the_home_bounds_and_the_fit_reads_no_layer():
    app = (ROOT / "sizeup" / "app.py").read_text()
    assert '"home": {"center": [lon, lat], "zoom": zoom, "bounds": list(AREA_BBOX)}' in app
    from sizeup.roads import AREA_BBOX
    assert list(AREA_BBOX) == AREA
    cadmap = text("cadmap.js")
    fit = cadmap[cadmap.index("    fit(animate) {"):cadmap.index("  // ---- the CAD console's maps")]
    assert "areaBounds(sc.home, " in fit
    for layer in ("MapLayers", "GisLayers", "getSource", "querySourceFeatures", "queryRenderedFeatures", "TrainLayer",
                  "AircraftLayer", "CamWall"):
        assert layer not in fit, layer
