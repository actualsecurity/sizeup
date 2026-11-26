"""The maps' animation costs nothing between its draws (the console felt sluggish, 2026-11-25).

A Performance trace of the console (Overview, the Aircraft layer on, 134 s) showed the page making a frame on every
vsync, 120 a second, the whole time: aircraft.js asked for an animation frame on every frame while an aircraft moved,
to draw it 4 to 20 times a second. About 69 of those frames a second ran nothing but that check, 0.63 ms of the main
thread each (style, the IntersectionObservers, the commit): about a fifth of everything the main thread did. While the
map moved, the camera overlay read the map's size after writing its tiles' positions, so each frame laid the page out
again (0.74 ms).

  aircraft.js    on a 120 Hz screen (vsyncs simulated, in node): a frame only when a draw is due (a timer sleeps
                 between), the draws as often as before (each time the fastest mark moves STEP_PX, FRAME_MS to
                 MAX_FRAME_MS), sooner from a zoom in on; the unlit tower sent to the map once, not on every draw.
  camoverlay.js  placing the tiles on every frame of a pan reads the map's size once, and again only after a resize.

The aircraft are the Aircraft layer's real answer (tests/fixtures/aircraft: fictional tails, recorded positions); the
cameras and the tower's spot are made up."""

import json

from tests.test_aircraft_follow import LOADED, MAP, hub_answer, map_input, needs_node, run_node

# A screen's vsyncs in node: rAF callbacks run on a 120 Hz tick, timers on the same virtual clock. The page's own
# Date.now() (glide.js: where a mark is) stays real; only the frames are counted here.
VSYNC = r"""
const V = { t: 0, rafs: [], timers: [], seq: 0, frames: 0, asked: 0 };
global.requestAnimationFrame = window.requestAnimationFrame = (cb) => { V.asked++; V.rafs.push(cb); return ++V.seq; };
global.cancelAnimationFrame = window.cancelAnimationFrame = () => {};
// from here on every timer is on the virtual clock (after the data is in: load() and flush() use real ones)
function virtualTimers() {
  global.setTimeout = (fn, d) => { const id = ++V.seq; V.timers.push({ id, at: V.t + (d || 0), fn }); return id; };
  global.clearTimeout = (id) => { V.timers = V.timers.filter((x) => x.id !== id); };
}
function vsyncs(ms, until) {
  const end = V.t + ms;
  while (V.t < end && !(until && until())) {
    V.t += 1000 / 120;
    for (const x of V.timers.filter((x) => x.at <= V.t)) { V.timers = V.timers.filter((y) => y !== x); x.fn(); }
    const due = V.rafs; V.rafs = [];
    if (due.length) V.frames++;
    for (const cb of due) cb(V.t);
  }
}
const drawn = () => (rec.sets["ac-aircraft"] || []).length;
"""


def moving(d):
    """The answer with its airborne aircraft fresh (a second old): moving on screen, so the frames run."""
    d = json.loads(json.dumps(d))
    for a in d["aircraft"]:
        a["age"] = 1.0
    return d


@needs_node
def test_aircraft_ask_for_a_frame_only_when_a_draw_is_due(tmp_path):
    d, ly, clock = hub_answer()
    assert any(a["alt"] != "ground" and (a["gs"] or 0) > 60 for a in d["aircraft"])
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], VSYNC + LOADED + r"""
(async () => {
  await AircraftLayer.load();
  await flush();
  virtualTimers();
  out.frameMs = m.frameMs();
  const d0 = drawn(), f0 = V.frames;
  vsyncs(3000);
  out.draws = drawn() - d0; out.frames = V.frames - f0;
  // closer in, the marks move faster on screen: drawn more often from the zoom on, not after the sleep it was in
  const d = drawn();
  vsyncs(1000, () => drawn() > d);                         // just drawn: asleep for the next 250 ms
  control.map.getZoom = () => 15;
  const z = drawn(), tz = V.t;
  control.map.emit("zoom");
  vsyncs(1000, () => drawn() > z);
  out.zoomToDraw = V.t - tz;
  out.frameMsIn = m.frameMs();
  const d1 = drawn(), f1 = V.frames;
  vsyncs(3000);
  out.drawsIn = drawn() - d1; out.framesIn = V.frames - f1;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(moving(d), layerOn=True), pre=MAP)
    # at the stand-in map's zoom 10 the marks move slowly: drawn every MAX_FRAME_MS, about 12 times in 3 s
    assert o["frameMs"] == 250
    assert 10 <= o["draws"] <= 13
    # a frame for each draw (and the odd one a vsync early), not 360 (one every vsync)
    assert o["frames"] <= 2 * o["draws"] + 2, o
    # zoomed in: up to 20 draws a second (FRAME_MS), still no frame between them
    assert o["zoomToDraw"] <= 60                             # at the next vsync after the zoom, not ~250 ms later
    assert o["frameMsIn"] < 250
    assert o["drawsIn"] >= 3000 / max(o["frameMsIn"], 50) * 0.7
    assert o["framesIn"] <= 2 * o["drawsIn"] + 2, o


@needs_node
def test_the_unlit_tower_is_sent_to_the_map_once(tmp_path):
    d, ly, clock = hub_answer()
    d = moving(d)
    d["atc"] = {"tower": {"lat": 41.07, "lon": -73.71, "open": True, "until": 0, "tx": []}, "talking": [], "tx": {}}
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  await AircraftLayer.load();
  await flush();
  for (let i = 0; i < 5; i++) m.draw(true);
  out.marks = (rec.sets["ac-aircraft"] || []).length;
  out.tower = (rec.sets["ac-tower"] || []).map((x) => x.features.map((f) => f.properties.name));
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(d, layerOn=True), pre=MAP)
    assert o["marks"] >= 5                                   # the aircraft: every draw
    assert o["tower"] == [["TWR"]]                           # the tower, unlit and unchanged: once


CAMS = r"""
global.el = (tag, cls, text) => { const e = new El(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
document.createElementNS = (ns, tag) => new El(tag);
El.prototype.remove = function () {};
El.prototype.removeAttribute = function () {};
El.prototype.querySelector = function () { return null; };
Object.defineProperty(El.prototype, "parentNode", { get() { return this.parent || null; } });
El.prototype.closest = function (sel) { return sel === "#ov-map" ? this : null; };
const reads = { size: 0 };
const box = new El("div");
Object.defineProperty(box, "clientWidth", { get() { reads.size++; return 1200; } });
Object.defineProperty(box, "clientHeight", { get() { reads.size++; return 800; } });
global.requestAnimationFrame = () => 1;                  // (schedule(): never run here, place() is called)
const on = {};
const map = { on: (t, f) => { (on[t] = on[t] || []).push(f); }, getContainer: () => box, getZoom: () => 13,
              project: ([lon, lat]) => ({ x: (lon + 73.75) * 8000, y: (41.05 - lat) * 8000 }) };
window.cad = { hooks: [] };
window.MapLayers = { addGroup: (g) => { window.group = g; } };
const tiles = [];
window.CamWall = { engine: { OVERLAY: "camoverlay", layout: () => "map", list: () => ({ cameras: IN.cams }), isDown: (c) => !!c.off,
  tile: (c) => { const t = { el: new El("div"), cam: c, id: c.id }; tiles.push(t); return t; }, drop() {}, tick() {}, subscribe() {},
  pinText: () => "" } };
"""


@needs_node
def test_the_camera_overlay_reads_the_maps_size_once_per_resize(tmp_path):
    cams = [{"id": f"cam{i}", "name": f"Example Rd camera {i}", "lat": 41.0 + i * 0.004, "lon": -73.70 + i * 0.003,
             "off": False} for i in range(6)]
    o = run_node(tmp_path, ["camoverlay.js"], r"""
const control = { map, on: () => true };
group.attach(control);
CamOverlay.render();
const r0 = reads.size;
for (let i = 0; i < 60; i++) CamOverlay.place();         // a second of a pan: placed on each frame
out.pan = reads.size - r0;
out.placed = tiles.filter((t) => /translate/.test(t.el.style.transform || "")).length;
for (const f of on.resize || []) f();
CamOverlay.place();
out.resized = reads.size - r0 - out.pan;
""", {"cams": cams}, pre=CAMS)
    assert o["placed"] == 6
    assert o["pan"] == 0                                     # measured by render(), not again on any frame of the pan
    assert o["resized"] == 2                                 # the map's width and height, once, after its resize
