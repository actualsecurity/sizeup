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
import subprocess

from tests.test_aircraft_follow import LOADED, MAP, NODE, STATIC, hub_answer, map_input, needs_node, run_node

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


# ---- #887 follow-up: only the maps a layer is on and on screen time its frames; every layer sleeps between draws -------

# The page's IntersectionObserver (fired by hand here: a map's box coming on or off screen) and its boxes' offsetParent
# reads counted (each one a layout once the page has changed); null: the map is in an Expand shut.
SCREEN = r"""
const seen = [];
global.IntersectionObserver = class { constructor(cb) { this.cb = cb; } observe(el) { seen.push([el, this.cb]); } };
const reshow = (el) => { for (const [e, cb] of seen) if (e === el) cb([]); };
const reads = { n: 0 };
function laidOut(el, on = true) {
  let v = on ? {} : null;
  Object.defineProperty(el, "offsetParent", { configurable: true, get() { reads.n++; return v; }, set(x) { v = x; } });
  return el;
}
// each map's draws: its own source's setData calls
const counted = (c, src) => { c.n = 0; const s = c.map.getSource(src); if (s) { const f = s.setData; s.setData = (d) => { c.n++; f(d); }; } };
"""


@needs_node
def test_aircraft_an_off_or_shut_map_neither_wakes_the_loop_nor_is_drawn(tmp_path):
    """The event's map with the layer off, zoomed in to 16, woke the loop every FRAME_MS (50 ms) for the Overview's draws
    every 250 ms: 61 frames for 12 draws in 3 s. A map in an Expand shut neither; opened, it is drawn at the next vsync."""
    d, ly, clock = hub_answer()
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], VSYNC + r"""
laidOut(control.map.container);
const off = { ...control, map: fakeMap(), host: new El("div"), profile: "incident", on: () => false };
laidOut(off.map.container); off.map.getZoom = () => 16;
const shut = { ...control, map: fakeMap(), host: new El("div") };
laidOut(shut.map.container, false);
""" + LOADED + r"""
const mOff = group.attach(off), mShut = group.attach(shut);
(async () => {
  await AircraftLayer.load();
  await flush();
  virtualTimers();
  for (const c of [control, off, shut]) counted(c, "ac-aircraft");
  out.offFrameMs = mOff.frameMs();
  const f0 = V.frames, r0 = reads.n;
  vsyncs(3000);
  out.frames = V.frames - f0; out.draws = control.n; out.offDraws = off.n; out.shutDraws = shut.n; out.reads = reads.n - r0;
  // a zoom on the off map, the loop asleep: nothing to wake
  let n = control.n; vsyncs(1000, () => control.n > n);
  const f1 = V.frames; off.map.emit("zoom"); vsyncs(100); out.offZoomFrames = V.frames - f1;
  // the Expand opened (its box seen on screen): drawn at the next vsync, not when the next draw is due
  n = control.n; vsyncs(1000, () => control.n > n);
  shut.map.container.offsetParent = {};
  const t = V.t; reshow(shut.map.container);
  vsyncs(1000, () => shut.n > 0); out.showToDraw = V.t - t;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(moving(d), layerOn=True), pre=MAP + SCREEN)
    assert o["offFrameMs"] == 50                             # (the off map, zoomed in: it would have set a 50 ms sleep)
    assert 10 <= o["draws"] <= 13                            # the Overview at z10: every MAX_FRAME_MS
    assert o["frames"] <= 2 * o["draws"] + 2, o              # main: 61 (woken every 50 ms by the off map)
    assert o["offDraws"] == 0 and o["shutDraws"] == 0
    assert o["reads"] == 0                                   # on screen or not: kept, not read on each frame
    assert o["offZoomFrames"] == 0
    assert o["showToDraw"] <= 20                             # at the next vsync


@needs_node
def test_a_picked_aircrafts_ring_goes_when_its_time_is_up(tmp_path):
    """A chip's aircraft is ringed for HIGHLIGHT_MS; with nothing moving no frame runs, so the ring stayed until the next
    answer (up to 2 s later). Its time up, the map is redrawn without it."""
    d, ly, clock = hub_answer()
    d = json.loads(json.dumps(d))
    for a in d["aircraft"]:
        a["age"] = 30.0                                      # stale (past stale_s): drawn where they were, not moving
    hx = next(a["hex"] for a in d["aircraft"] if a["alt"] != "ground")
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], VSYNC + LOADED + r"""
const ring = (hex) => { const s = rec.sets["ac-aircraft"] || []; const f = s.length ? s[s.length - 1].features.find((x) => x.properties.hex === hex) : null; return f ? f.properties.ring || "" : null; };
(async () => {
  await AircraftLayer.load();
  await flush();
  virtualTimers();
  AircraftLayer.focus(IN.hex);
  out.picked = ring(IN.hex);
  const f0 = V.frames; vsyncs(500); out.idleFrames = V.frames - f0;      // (ringed: drawn on, nothing else moving)
  const real = Date.now; Date.now = () => real() + 13000;     // its 12 s up (the page's clock), no answer since
  vsyncs(13000);
  out.after = ring(IN.hex); out.fetches = rec.fetches;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {**map_input(d, layerOn=True), "hex": hx}, pre=MAP)
    assert o["picked"] == "selected"
    assert o["after"] == "" and o["fetches"] == 1            # redrawn when its time was up, not by a poll


@needs_node
def test_a_chip_focuses_its_aircraft_on_a_map_just_opened_at_once(tmp_path):
    """shownMap() reads the map's box now: a chip clicked with the Overview shut opens it and focuses the aircraft at
    once, not ~400 ms later (the kept on-screen flag is only updated when the observer calls back)."""
    d, ly, clock = hub_answer()
    hx = next(a["hex"] for a in d["aircraft"] if a["alt"] != "ground")
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], VSYNC + r"""
laidOut(control.map.container, false);                     // the Overview shut
""" + LOADED + r"""
const ring = (hex) => { const s = rec.sets["ac-aircraft"] || []; const f = s.length ? s[s.length - 1].features.find((x) => x.properties.hex === hex) : null; return f ? f.properties.ring || "" : null; };
(async () => {
  await AircraftLayer.load();
  await flush();
  control.map.container.offsetParent = {};                 // opened (#overview-open): laid out, no observer call yet
  AircraftLayer.focus(IN.hex);
  out.ring = ring(IN.hex); out.eased = rec.eases.length;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {**map_input(d, layerOn=True), "hex": hx}, pre=MAP + SCREEN)
    assert o["ring"] == "selected" and o["eased"] == 1      # centred and ringed now, not on a 400 ms retry


@needs_node
def test_vessels_ask_for_a_frame_only_when_a_draw_is_due(tmp_path):
    """vessels.js asked for a frame on every vsync while a vessel moved, to draw each map every frameMs (100 ms to 1 s).
    Now it sleeps on a timer between draws, as aircraft.js; a zoom, a follow, a map coming on screen and a new answer wake it."""
    from tests.test_vessels_page import answer
    o = run_node(tmp_path, ["motion.js", "mapfollow.js", "vessels.js"], VSYNC + r"""
laidOut(control.map.container);
const shut = { ...control, map: fakeMap(), host: new El("div") };
laidOut(shut.map.container, false);
const m = group.attach(control), mShut = group.attach(shut);
const flush = () => new Promise((r) => setTimeout(r, 0));
const vdrawn = () => (rec.sets["vs-vessels"] || []).length;
(async () => {
  await VesselLayer.load(); await flush();
  virtualTimers();
  for (const c of [control, shut]) counted(c, "vs-vessels");
  out.frameMs = m.frameMs();
  const f0 = V.frames, r0 = reads.n;
  vsyncs(5000);
  out.frames = V.frames - f0; out.draws = control.n; out.shutDraws = shut.n; out.reads = reads.n - r0;
  // closer in: drawn from the zoom on, not after the sleep it was in
  let n = control.n; vsyncs(2000, () => control.n > n);
  control.map.getZoom = () => 17;
  n = control.n; let t = V.t; control.map.emit("zoom");
  vsyncs(2000, () => control.n > n); out.zoomToDraw = V.t - t;
  out.frameMsIn = m.frameMs();
  const d1 = control.n, f1 = V.frames;
  vsyncs(3000);
  out.drawsIn = control.n - d1; out.framesIn = V.frames - f1;
  // the Expand opened: drawn at once
  shut.map.container.offsetParent = {};
  t = V.t; reshow(shut.map.container); vsyncs(2000, () => shut.n > 0); out.showToDraw = V.t - t;
  // the layer ticked off everywhere: no frames
  IN.layerOn = false; m.refresh(); mShut.refresh();
  vsyncs(200); const f2 = V.frames; vsyncs(2000); out.offFrames = V.frames - f2;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {"d": answer(), "layerOn": True}, pre=MAP + SCREEN)
    assert o["frameMs"] == 1000                              # z10: the tug moves a pixel in ~14 s: every MAX_FRAME_MS
    assert 4 <= o["draws"] <= 6
    assert o["frames"] <= 2 * o["draws"] + 2, o              # main: 600 (every vsync)
    assert o["shutDraws"] == 0 and o["reads"] == 0
    assert o["frameMsIn"] < 150 and o["zoomToDraw"] <= o["frameMsIn"] + 20   # due by the new zoom, not in up to 1 s
    assert o["frameMsIn"] < 1000 and o["drawsIn"] >= 3000 / o["frameMsIn"] * 0.7
    assert o["framesIn"] <= 2 * o["drawsIn"] + 2, o
    assert o["showToDraw"] <= 20
    assert o["offFrames"] == 0


# trains.js in node: its timers on a fake clock (as tests/test_train_motion.py's layer test), three maps
TRAINS = r"""
const fs = require("fs");
let now = 0, timers = [], seq = 0, wakes = 0;
global.Date = { now: () => now };
global.setTimeout = (fn, ms) => { const t = { id: ++seq, fn, at: now + (ms || 0), every: 0, live: true }; timers.push(t); return t.id; };
global.setInterval = (fn, ms) => { const t = { id: ++seq, fn, at: now + ms, every: ms, live: true }; timers.push(t); return t.id; };
global.clearTimeout = global.clearInterval = (id) => { const t = timers.find((x) => x.id === id); if (t) t.live = false; };
global.window = global;
const stub = () => new Proxy(function () {}, { get: (t, k) => (k === "getImageData" ? () => ({}) : stub()), apply: () => stub() });
global.document = { visibilityState: "visible", addEventListener() {}, createElement: () => ({ getContext: () => stub(), style: {}, classList: { add() {}, toggle() {} }, append() {}, addEventListener() {} }), documentElement: {} };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
const IN = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
global.fetch = async () => ({ ok: true, json: async () => {
  const a = JSON.parse(JSON.stringify(IN.answer)); a.age += (now - IN.got) / 1000; return a; } });
let attach = null;
global.MapLayers = { addGroup: (g) => { attach = g.attach; } };
""" + SCREEN + r"""
for (const f of ["motion.js", "mapfollow.js"]) eval(fs.readFileSync(IN.static + "/" + f, "utf8"));
const mkTracker = Motion.railTracker; let TK = null;
Motion.railTracker = (...a) => (TK = mkTracker(...a));     // the layer's tracker, to start a fade by hand
eval(fs.readFileSync(IN.static + "/trains.js", "utf8"));
function fakeMap(zoom, on, shown = true) {
  const sources = {}, layers = {}, h = {}, box = laidOut({}, shown);
  const map = {
    getSource: (id) => sources[id], addSource: (id) => { sources[id] = { setData() {} }; },
    addLayer: (l) => { layers[l.id] = l; }, getLayer: (id) => layers[id], setLayoutProperty() {}, hasImage: () => true, addImage() {},
    getStyle: () => ({ glyphs: "x", layers: [] }), on(t, f) { (h[t] = h[t] || []).push(f); }, once() {}, isStyleLoaded: () => true, loaded: () => true,
    getContainer: () => box, getCanvas: () => ({ style: {} }), getZoom: () => zoom, getCenter: () => ({ lng: -73.68, lat: 41.0 }),
    emit(t) { for (const f of h[t] || []) f({}); }, zoomTo(z) { zoom = z; this.emit("zoom"); },
  };
  const c = { map, on: (k) => k === "trains" && on(), panel: { hidden: true }, renderPanel() {} };
  return c;
}
const flush = () => new Promise((r) => setImmediate(r));
async function advance(ms, step = 1000 / 120) {
  const end = now + ms;
  while (now < end) {
    now += step;
    for (const t of timers.filter((x) => x.live && x.at <= now)) { if (t.every) t.at += t.every; else { t.live = false; wakes++; } t.fn(); }
    await flush();
  }
}
"""


@needs_node
def test_trains_are_drawn_as_often_as_they_move_a_pixel_on_each_map(tmp_path):
    """trains.js redrew every map (a setData each) 10 times a second while a train moved, at any zoom, on a map the layer
    was off on or one in an Expand shut too. Now each map the layer is on and on screen is drawn as often as the fastest
    train moves STEP_PX on it (ANIMATE_MS to MAX_ANIMATE_MS), and none when nothing moves."""
    from tests.test_trains import provider
    tr, clock, fetch = provider(tmp_path)
    tr.get("/api/cad/trains", "")
    tr.tick()
    clock.t += 3
    answer = json.loads(tr.get("/api/cad/trains", "")[1])
    assert any(t["segment"] and not t["estimated"] for t in answer["trains"])
    o = run_trains(tmp_path, r"""
const out = {};
const far = fakeMap(10, () => true), near = fakeMap(15, () => true), off = fakeMap(15, () => false), shut = fakeMap(15, () => true, false);
(async () => {
  now = IN.got;
  const ms = [far, near, off, shut].map((c) => attach(c));
  await flush(); await flush();
  for (const c of [far, near, off, shut]) counted(c, "tr-cars");
  const r0 = reads.n, w0 = wakes;
  await advance(1000);                                     // (the pace known: the first draws are ANIMATE_MS apart)
  for (const c of [far, near, off, shut]) c.n = 0;
  await advance(5000);
  out.far = far.n; out.near = near.n; out.off = off.n; out.shut = shut.n; out.reads = reads.n - r0;
  out.farMs = ms[0].frameMs ? ms[0].frameMs() : null; out.nearMs = ms[1].frameMs ? ms[1].frameMs() : null;
  // the Expand opened: drawn at once, not after the sleep
  shut.map.getContainer().offsetParent = {};
  const t = now; reshow(shut.map.getContainer()); await advance(500, 1); out.showToDraw = shut.n > 0 ? 0 : -1;
  // zoomed out: drawn less often from then on
  near.map.zoomTo(10); near.n = 0; await advance(3000); out.nearOut = near.n;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {"answer": answer, "got": clock.t * 1000})
    assert o["off"] == 0 and o["shut"] <= 1                  # (the shut one: the new answer's own draw, every 5 s)
    assert o["reads"] == 0                                   # main: each map's box read 10 times a second
    assert o["nearMs"] == 100 and 40 <= o["near"] <= 51      # z15: a train moves a pixel in < 100 ms: 10 a second
    assert o["farMs"] == 500 and 8 <= o["far"] <= 11         # z10: every MAX_ANIMATE_MS (main: 50, like z15)
    assert o["showToDraw"] == 0
    assert 5 <= o["nearOut"] <= 7


@needs_node
def test_a_fading_train_is_drawn_through_its_fade_on_a_zoomed_out_map(tmp_path):
    """A correction too far to glide is faded out where the train is drawn and in at the fix (motion.js, #390: FADE_MS,
    800 ms): its opacity changes, not where it is drawn. At z10 the draws are timed by the motion (every 500 ms): the fade
    got 2 or 3. While a train fades: every ANIMATE_MS."""
    from tests.test_trains import provider
    tr, clock, fetch = provider(tmp_path)
    tr.get("/api/cad/trains", "")
    tr.tick()
    clock.t += 3
    answer = json.loads(tr.get("/api/cad/trains", "")[1])
    o = run_trains(tmp_path, r"""
const out = {};
const far = fakeMap(10, () => true);
const ops = [];
(async () => {
  now = IN.got;
  const m = attach(far);
  await flush(); await flush();
  await advance(1500);
  out.ms = m.frameMs();
  const src = far.map.getSource("tr-trains"), sd = src.setData;
  const id = [...TK.items.keys()].find((k) => TK.items.get(k).shown && !TK.items.get(k).stale);
  src.setData = (d) => { const f = d.features.find((x) => x.properties.id === id); if (f) ops.push(f.properties.op); sd(d); };
  await TrainLayer.load(); await flush();                  // the answer that brought the far fix: drawn now
  const it = TK.items.get(id); it.fade = { at: now, s: it.shown.s };
  await advance(800);
  out.draws = ops.length; out.ops = ops; out.msFading = m.frameMs();
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {"answer": answer, "got": clock.t * 1000})
    assert o["ms"] == 500                                    # z10, nothing fading: timed by the motion
    assert o["draws"] >= 6, o                                # through the fade, ~every ANIMATE_MS (main of this PR: 2-3)
    assert min(o["ops"]) < 0.5                               # faded out and in on the way


def run_trains(tmp_path, body, data):
    h, d = tmp_path / "t.js", tmp_path / "in.json"
    h.write_text(TRAINS + body)
    d.write_text(json.dumps({**data, "static": str(STATIC)}))
    r = subprocess.run([NODE, str(h), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@needs_node
def test_a_moving_live_rig_asks_for_a_frame_only_when_a_tick_is_due(tmp_path):
    """A rig with a live fix (AVL) moving was a frame on every vsync for as long as it drove (#473's smooth path), for
    ticks each time it moved STEP_PX. Now the loop sleeps between ticks (the frames owed after a new state aside), a
    zoom wakes it, and whether the map is on screen is kept, not read per layer per frame."""
    from tests.test_unit_follow import HARNESS, LOAD, page_state, with_now
    o = run_rigs(tmp_path, HARNESS, LOAD, r"""
laidOut(rm.map.getContainer());
let ticks = 0; const tk = L.tick.bind(L); L.tick = () => { ticks++; tk(); };
let fr = 0;
const vs = (n) => { for (let i = 0; i < n; i++) { spin(1000 / 120); const fs_ = frames.splice(0); if (fs_.length) fr++; for (const f of fs_) f(performance.now()); } };
vs(12);                                                     // (the frames owed after the state)
ticks = 0; fr = 0; const r0 = reads.n;
vs(240);                                                    // 2 s at 120 Hz
out.ticks = ticks; out.frames = fr; out.reads = reads.n - r0; out.ms = L.frameMs(); out.moving = L.moving();
// closer in: ticked from the zoom on (FRAME_MS), not after the sleep
let n = ticks; let i = 0; while (ticks === n && i++ < 60) vs(1);
rm.map.zoom = 17; n = ticks; i = 0; rm.map.emit("zoom");
while (ticks === n && i < 60) { vs(1); i++; }
out.zoomToTick = i; out.msIn = L.frameMs();
ticks = 0; fr = 0; vs(120); out.ticksIn = ticks; out.framesIn = fr;
""", with_now(page_state(), age=0.5))
    assert o["moving"] is True and o["ms"] == 250            # z13: 10 m/s is under a pixel a second
    assert 6 <= o["ticks"] <= 9
    assert o["frames"] <= 2 * o["ticks"] + 2, o              # main: 240 (every vsync)
    assert o["reads"] == 0
    assert o["msIn"] == 50 and o["zoomToTick"] <= 7         # due by the new zoom (50 ms), not the 250 ms sleep
    assert o["ticksIn"] >= 15 and o["framesIn"] <= 2 * o["ticksIn"] + 2


def run_rigs(tmp_path, harness, load, body, data):
    h, d = tmp_path / "r.js", tmp_path / "in.json"
    h.write_text(harness + SCREEN + load + body + "\nconsole.log(JSON.stringify(out)); process.exit(0);\n")
    d.write_text(json.dumps(data))
    r = subprocess.run([NODE, str(h), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@needs_node
def test_a_pinned_cameras_label_is_measured_again_once_the_web_font_is_in(tmp_path):
    cams = [{"id": f"cam{i}", "name": f"Example Rd camera {i}", "lat": 41.0 + i * 0.004, "lon": -73.70 + i * 0.003,
             "off": False, "pin": True} for i in range(3)]
    o = run_node(tmp_path, ["camoverlay.js"], r"""
let fontsIn; document.fonts = { ready: new Promise((r) => { fontsIn = r; }) };
let lw = 0;
El.prototype.getAttribute = function (k) { return this.attrs[k]; };
El.prototype.querySelector = function (sel) { return this.children.find((c) => c && c.classList && c.classList.contains(sel.slice(1))) || null; };
Object.defineProperty(El.prototype, "offsetWidth", { get() { lw++; return 60; } });
Object.defineProperty(El.prototype, "offsetHeight", { get() { return 16; } });
const control = { map, on: () => false };
group.attach(control);
(async () => {
  CamOverlay.render();
  const a = lw; for (let i = 0; i < 10; i++) CamOverlay.place(); out.pan = lw - a;
  fontsIn(); await new Promise((r) => setTimeout(r, 0));
  const b = lw; CamOverlay.place(); out.afterFonts = lw - b;
  out.first = a;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {"cams": cams}, pre=CAMS.replace('pinText: () => ""', 'pinText: (c) => c.name'))
    assert o["first"] == 3 and o["pan"] == 0                 # measured once each, not on each frame
    assert o["afterFonts"] == 3                              # the web font in: measured again, once
