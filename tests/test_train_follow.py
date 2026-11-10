"""#375: following a train (mapfollow.js, as aircraft and rigs), its box kept up to date while open, the catenary on
one line, the TrainTime route drawn as a route (thin, dashed), and any pan of the viewer's letting go of a follow
(every follower: aircraft, rigs, trains) while a zoom keeps it.

The page's own code runs in node with a map stand-in (tests/test_aircraft_follow.py's DOM); the trains come from the
hub (sizeup/trains.py Trains with tests/test_traintime.py's feeds). No MTA."""

import json

from tests.test_aircraft_follow import DOM, needs_node, run_node
from tests.test_traintime import LOC, provider, view

# ---- MapFollow.Follower: what lets go and what doesn't --------------------------------------------------------------

FOLLOWER = r"""
let T = 1_000_000;
global.performance = { now: () => T };
const winOn = {};
global.addEventListener = (t, f) => { (winOn[t] = winOn[t] || []).push(f); };
const winFire = (t, ev) => { for (const f of winOn[t] || []) f(ev || {}); };
El.prototype.querySelector = function (sel) { return this.find(sel.slice(1))[0] || null; };
const eases = [];
function stand() {
  const on = {};
  const m = {
    on(t, f) { (on[t] = on[t] || []).push(f); }, fire(t, e) { for (const f of on[t] || []) f(e || {}); },
    emit(t, e) { for (const f of on[t] || []) f(e || {}); },
    getCanvasContainer: () => m.box, box: new El("div"),
    getCenter: () => ({ lng: -73.66, lat: 41.0 }), getZoom: () => 14, isMoving: () => false, isZooming: () => false,
    project: (ll) => { const [lng, lat] = Array.isArray(ll) ? ll : [ll.lng, ll.lat]; return { x: lng * 1e5, y: -lat * 1e5 }; },
    easeTo(o) { eases.push(o); }, scrollZoom: { isEnabled: () => true, enable() {}, disable() {} },
    touchZoomRotate: { isEnabled: () => true, enable() {}, disable() {} },
  };
  return m;
}
const fol = () => { const map = stand(), f = new MapFollow.Follower(map, new El("div"), {}); f.start("x", { lon: -73.66, lat: 41.0 }); T += 1000; return [map, f]; };
"""


@needs_node
def test_any_pan_of_the_viewers_lets_go_a_zoom_or_our_own_ease_does_not(tmp_path):
    o = run_node(tmp_path, ["mapfollow.js"], FOLLOWER + r"""
const kept = (fn) => { const [map, f] = fol(); fn(map, f); return f.id === "x"; };
out.mouse = kept((m) => m.emit("dragstart", { originalEvent: { type: "mousemove" } }));
out.touch = kept((m) => m.emit("dragstart", { originalEvent: { type: "touchmove", touches: [{}] } }));
out.pinch = kept((m) => m.emit("dragstart", { originalEvent: { type: "touchmove", touches: [{}, {}] } }));
out.ease = kept((m) => { m.emit("movestart", {}); m.emit("moveend", {}); });                      // our own pans: no originalEvent
out.wheelZoom = kept((m) => { m.emit("movestart", { originalEvent: { type: "wheel" } }); m.emit("zoomstart", { originalEvent: { type: "wheel" } });
                              m.box.fire("wheel", { deltaY: 120, deltaX: 3 }); });
out.ctrlWheel = kept((m) => m.box.fire("wheel", { deltaX: 80, deltaY: 1, ctrlKey: true }));      // a trackpad pinch
out.arrow = kept((m) => m.emit("movestart", { originalEvent: { type: "keydown", key: "ArrowLeft" } }));
out.plus = kept((m) => m.emit("movestart", { originalEvent: { type: "keydown", key: "+" } }));
out.swipe = kept((m) => m.box.fire("wheel", { deltaX: 60, deltaY: 4 }));                       // a sideways trackpad swipe
// pressed and held: no pan eased meanwhile (an ease resets MapLibre's gestures: the drag never started), then on
const [map, f] = fol();
eases.length = 0;
map.box.fire("mousedown", { button: 0 });
T += 1000; out.heldPan = f.pan([-73.65, 41.0]); out.heldEases = eases.length;
winFire("mouseup", {});
T += 1000; out.afterPan = f.pan([-73.65, 41.0]); out.afterEases = eases.length;
map.box.fire("touchstart", { touches: [{}] }); winFire("touchend", { touches: [{}] });             // a finger still down
T += 1000; out.oneUp = f.pan([-73.64, 41.0]);
winFire("touchend", { touches: [] });
T += 1000; out.allUp = f.pan([-73.63, 41.0]);
""")
    assert o["mouse"] is False and o["touch"] is False                       # a drag, mouse or one finger: let go
    assert o["arrow"] is False and o["swipe"] is False                       # the arrow keys, a sideways swipe: a pan too
    assert o["pinch"] and o["ease"] and o["wheelZoom"] and o["ctrlWheel"] and o["plus"]   # zooms and our eases: kept
    assert o["heldPan"] is False and o["heldEases"] == 0                     # nothing eased under a finger or the mouse
    assert o["afterPan"] is True and o["afterEases"] == 1
    assert o["oneUp"] is False and o["allUp"] is True


def test_every_follower_is_the_shared_one():
    """Aircraft, rigs and trains all follow through mapfollow.js, so the drag fix holds for each."""
    from tests.test_train_motion import STATIC
    for f in ("aircraft.js", "cadmap.js", "trains.js"):
        assert "new MapFollow.Follower(" in (STATIC / f).read_text(), f


# ---- a train: followed on a click, its box live, its chip -----------------------------------------------------------

TRAINS = r"""
let T = IN.got;
Date.now = () => T;
global.performance = { now: () => T, measure() {}, clearMeasures() {} };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
El.prototype.querySelector = function (sel) { return this.find(sel.slice(1))[0] || null; };
El.prototype.replaceWith = function (n) { const p = this.parent, i = p.children.indexOf(this); p.children[i] = n; n.parent = p; };
let answer = IN.answers[0];
global.fetch = async (u) => ({ ok: true, json: async () => (String(u).includes("/route") ? { runs: [] } : JSON.parse(JSON.stringify(answer))) });
const rec = { eases: [], fired: [] };
const on = {}, sources = {}, layers = {};
const map = {
  on(t, a, b) { const k = b ? `${t}:${a}` : t; (on[k] = on[k] || []).push(b || a); }, once() {},
  fire(t, e) { rec.fired.push(t); for (const f of on[t] || []) f(e || {}); }, emit(t, e) { for (const f of on[t] || []) f(e || {}); },
  getSource: (id) => sources[id], addSource(id) { sources[id] = { setData() {} }; }, addLayer(l) { layers[l.id] = l; },
  getLayer: (id) => layers[id], setLayoutProperty() {}, hasImage: () => true, addImage() {}, getStyle: () => ({ glyphs: "x", layers: [] }),
  isStyleLoaded: () => true, loaded: () => true, getContainer: () => ({ offsetParent: {} }), getCanvas: () => ({ style: {} }),
  getCanvasContainer: () => map.box, box: new El("div"),
  getCenter: () => ({ lng: -73.7, lat: 41.0 }), getZoom: () => 12, isMoving: () => false, isZooming: () => false,
  project: (ll) => { const [lng, lat] = Array.isArray(ll) ? ll : [ll.lng, ll.lat]; return { x: lng * 1e5, y: -lat * 1e5 }; },
  easeTo(o) { rec.eases.push(o); }, scrollZoom: { isEnabled: () => true, enable() {}, disable() {} },
  touchZoomRotate: { isEnabled: () => true, enable() {}, disable() {} },
  queryRenderedFeatures: () => [{ layer: { id: "tr-car" }, properties: { id: IN.id } }],
};
let group = null;
window.MapLayers = { addGroup: (g) => { group = g; } };
const host = new El("div"), info = new El("div");
info.hidden = true;
const control = { map, host, info, on: (k) => k === "trains", panel: { hidden: true }, renderPanel() {}, close() {},
  showInfo(title, sub, lines, foot) {
    const body = new El("div"); body.className = "ml-lines";
    for (const l of lines) body.append(typeof l === "string" ? Object.assign(new El("div"), { textContent: l }) : l);
    const f = new El("div"); f.className = "dim small ml-foot"; f.textContent = foot || "";
    info.replaceChildren(); info.append(new El("div"), body, f); info.hidden = false; info.title_ = title;
  } };
"""

LOAD_TRAINS = TRAINS + "".join(f'eval(fs.readFileSync(STATIC + "{s}", "utf8"));\n' for s in ("motion.js", "mapfollow.js", "trains.js"))


def answers(tmp_path):
    """Two hub answers 5 s apart, with 9901 on TrainTime GPS (westbound) the second time further on and faster."""
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    out = []
    doc = json.loads(LOC)
    for k, (dlat, mph) in enumerate(((0.0, 52.3), (-0.0015, 61.0))):
        x = next(r for r in doc if r.get("train_num") == "9901")
        x["location"].update(latitude=round(x["location"]["latitude"] + dlat, 6), longitude=round(x["location"]["longitude"] + dlat * 1.3, 6),
                             timestamp=int(clock.t) - 4, speed=mph)
        fetch.loc = json.dumps(doc).encode()
        view(tr, clock)
        tr.refresh_rt()
        tr.tt.poll()
        out.append({"got": clock.t * 1000, "body": tr.public()})
        clock.t += 5
    return out


@needs_node
def test_a_clicked_train_is_followed_and_its_box_and_chip_stay_live(tmp_path):
    a = answers(tmp_path)
    t0 = next(t for t in a[0]["body"]["trains"] if t["train"] == "9901")
    assert t0["pos_src"] == "traintime"
    from tests.test_train_motion import STATIC
    o = run_node(tmp_path, [], f"const STATIC = {json.dumps(str(STATIC) + '/')};\n" + LOAD_TRAINS + r"""
const flush = () => new Promise((r) => setTimeout(r, 0));
const rows = () => info.find("ml-lines")[0].children.map((c) => [c.textContent, c.title]);
(async () => {
  const m = group.attach(control);
  await TrainLayer.load(); await flush();
  m.draw(true, T);
  map.emit("click", { point: { x: 1, y: 1 }, originalEvent: { stopPropagation() {} } });
  await flush(); await flush();
  out.following = m.following; out.ease = rec.eases[0]; out.fired = [...rec.fired];
  out.chip = host.find("ac-follow-t")[0].textContent;
  out.rows = rows();
  out.route = layers["tr-route"].paint;
  T += 3000; m.tick();                // 3 s on: the ages
  out.rows3 = rows(); out.chip3 = host.find("ac-follow-t")[0].textContent;
  answer = IN.answers[1]; T = IN.answers[1].got;               // the next fix: faster, further on
  await TrainLayer.load(); await flush();
  out.rowsNext = rows(); out.chipNext = host.find("ac-follow-t")[0].textContent;
  map.emit("dragstart", { originalEvent: { type: "mousemove" } });                // a mouse drag lets go
  out.afterDrag = [m.following, host.find("ac-follow")[0].hidden];
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {"answers": [{**x["body"], "got": x["got"]} for x in a], "got": a[0]["got"], "id": t0["id"]})
    assert o["following"] == t0["id"] and o["ease"]["zoom"] >= 15
    assert "sizeup:takemap" in o["fired"] and "sizeup:follow" in o["fired"]                  # any other follower let go
    assert o["chip"].startswith("Following 9901 · ") and " west · 52 mph · " in o["chip"] and o["chip"].endswith(" s ago")
    text = [r[0] for r in o["rows"]]
    pos = next(r for r in text if "mph" in r)
    assert pos.split(" · ")[0] in ("Reported", "Predicted") and "from" not in pos and pos.endswith(" s ago")   # no "from ·"
    cat = next(r for r in o["rows"] if r[0].startswith("⚡"))
    assert cat[0] == "⚡ 12.5 kV catenary + 3rd rail · treat as live" and "Treat all wires as live" in cat[1]
    assert o["route"]["line-dasharray"] and o["route"]["line-width"][-1] <= 2                 # a route, not a track
    age = lambda rs: int(next(r[0] for r in rs if "mph" in r[0]).split(" · ")[-1].split(" ")[0])
    assert age(o["rows3"]) == age(o["rows"]) + 3 and o["chip3"] != o["chip"]                  # the age, live
    nxt = next(r[0] for r in o["rowsNext"] if "mph" in r[0])
    assert "61 mph" in nxt and " 61 mph " in o["chipNext"]                                    # the next fix, without reopening
    assert any(p.startswith("MP\u00a0") for p in o["rowsNext"][0][0].split(" · "))           # (the milepost: where it is drawn)
    assert o["afterDrag"] == ["", True]


# ---- an aircraft's box, live too (MapFollow.LiveInfo, shared with the trains) ----------------------------------------

LIVE_BOX = r"""
let T = Date.now();
Date.now = () => T;
El.prototype.querySelector = function (sel) { return this.find(sel.slice(1))[0] || null; };
El.prototype.replaceWith = function (n) { const p = this.parent, i = p.children.indexOf(this); p.children[i] = n; n.parent = p; };
const info = new El("div");
info.hidden = true;
control.info = info;
control.showInfo = (title, sub, lines, foot) => {
  const body = new El("div"); body.className = "ml-lines";
  for (const l of lines) body.append(typeof l === "string" ? Object.assign(new El("div"), { textContent: l }) : l);
  const f = new El("div"); f.className = "dim small ml-foot";
  f.append({ nodeType: 3, nodeValue: foot || "", get textContent() { return this.nodeValue; } });
  Object.defineProperty(f, "firstChild", { get: () => f.children[0] });
  info.replaceChildren(); info.append(new El("div"), body, f); info.hidden = false;
};
const lines = () => info.find("ml-lines")[0].children.map((c) => (typeof c === "string" ? c : c.textContent));
const foot = () => info.find("ml-foot")[0].textContent;
"""


@needs_node
def test_an_aircrafts_box_updates_from_each_fix_and_its_age_ticks(tmp_path):
    from tests.test_aircraft_follow import LOADED, MAP, hub_answer, map_input
    d, ly, clock = hub_answer()
    a = next(x for x in d["aircraft"] if x["alt"] != "ground" and (x["gs"] or 0) > 60 and x["age"] < 5 and x.get("dist") is not None)
    d2 = json.loads(json.dumps(d))
    b = next(x for x in d2["aircraft"] if x["hex"] == a["hex"])
    b.update(alt=a["alt"] + 700, gs=a["gs"] + 25, vr=1500, sq="4321", trk=(a["trk"] or 0) + 10, dist=round(a["dist"] + 0.4, 2))
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  IN.d.fetched = T / 1000;                                       // read now (this test's clock)
  await AircraftLayer.load(); await flush();
  const hex = IN.hex;
  control.map.queryRenderedFeatures = () => [{ properties: { hex } }];
  m.clicked({ point: [0, 0], originalEvent: { stopPropagation() {} } });
  await flush();
  out.first = lines(); out.foot = foot();
  T += 3000; m.live.refresh();                                   // 3 s on: the age, the glided distance
  out.later = lines(); out.foot3 = foot();
  IN.d = IN.d2; IN.d.fetched = T / 1000;                         // the next fix
  await AircraftLayer.load(); await flush();
  out.next = lines(); out.open = !!m.live;
  info.hidden = true; m.live.refresh();                          // shut: it stops
  out.closed = m.live === null;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {**map_input(d, hex=a["hex"], layerOn=True), "d2": map_input(d2)["d"]}, pre=MAP + LIVE_BOX)
    age = lambda f: int(f.split(" · ")[-1].split(" ")[0])
    assert o["foot"].endswith(" s ago") and age(o["foot3"]) == age(o["foot"]) + 3          # the age, live
    alt = lambda ls: next(x for x in ls if " ft" in x)
    assert alt(o["first"]) != alt(o["next"]) and f"{a['alt'] + 700:,} ft" in alt(o["next"]) and "fpm" in alt(o["next"])
    assert f"{a['gs'] + 25} kt" in alt(o["next"]) and any("Sqk 4321" in x for x in o["next"])
    nm = lambda ls: next(x for x in ls if " nm" in x)
    assert nm(o["first"]) and nm(o["later"])                                                 # (from the glided position)
    assert o["open"] and o["closed"]
