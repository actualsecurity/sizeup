"""Trains moving on the map between the MTA's reports (web/static/motion.js Motion.rail / railFollow / railTracker,
drawn by trains.js): the run profile ported from sizeup/trains.py (the same numbers on the same runs), the
predictor's bounds (hold_s, the next stop, predict_until, never backwards), a stopped train held until the hub
says it left, the freeze on a stale feed, corrections along the track (caught up, held, brought back, jumped), and
the layer's frames (one setData a source a frame, none when nothing moved, none with the layer off).

The page's own code runs in node (skipped where node isn't installed); what it is fed comes from the Python side:
the runs from sizeup/trains.py and the recorded feed (tests/fixtures/mta/, see test_trains.py)."""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup import trains as T
from sizeup.trains import Model, parse_feed, parse_static
from tests.test_trains import GTFS, NOW, RT, provider

NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

LOAD = r"""
const fs = require("fs");
global.window = global;
eval(fs.readFileSync(process.argv[2], "utf8"));
const IN = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
"""


def node(tmp_path, body: str, data=None) -> dict:
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    h.write_text(LOAD + body + "\nconsole.log(JSON.stringify(out));\n")
    d.write_text(json.dumps(data if data is not None else {}))
    r = subprocess.run([NODE, str(h), str(STATIC / "motion.js"), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def run(s0=0.0, s1=2300.0, t_dep=0.0, t_arr=180.0, **kw):
    return T.segment(s0, s1, t_dep, t_arr) | kw


@pytest.fixture(scope="module")
def model():
    return Model(parse_static(GTFS), tracks=json.loads(T.TRACKS_FILE.read_text()))


@pytest.fixture(scope="module")
def recorded(model):
    return model.trains(parse_feed(RT), NOW)


# ---- profile_s, ported -----------------------------------------------------------------------------------------------


def test_the_pages_run_profile_gives_the_hubs_numbers(tmp_path, recorded):
    segs = [run(), run(2300.0, 0.0), run(s1=1500.0, t_arr=2 * math.sqrt(1500 * (1 / (2 * T.ACCEL) + 1 / (2 * T.DECEL)))),
            run(s1=1500.0, t_arr=40.0), run(s1=60_000.0, t_arr=1500.0), run(t_arr=0.0), run(t_arr=-10.0), run(s1=0.0),
            run(41_000.0, 37_500.0, NOW, NOW + 300), run(19_939.4, 55_499.4, NOW - 600, NOW + 1500)]
    segs += [t["segment"] for t in recorded if t["segment"]]               # the runs the hub sends now
    assert len(segs) > 12
    cases = []
    for seg in segs:
        D, _, _, _, _, ta, tc, tb = T._shape(seg)
        span = max(1.0, ta + tc + tb)
        ts = [seg["t_dep"] + span * k / 40 for k in range(-4, 46)] + [seg["t_dep"] + ta, seg["t_dep"] + ta + tc, seg["t_arr"]]
        ss = [seg["s0"] + (seg["s1"] - seg["s0"]) * k / 20 for k in range(-2, 23)]
        cases.append({"seg": seg, "ts": ts, "ss": ss, "s": [T.profile_s(t, seg) for t in ts], "t": [T.profile_t(s, seg) for s in ss]})
    out = node(tmp_path, """
out.s = IN.map((c) => c.ts.map((t) => Motion.profileS(t, c.seg)));
out.t = IN.map((c) => c.ss.map((s) => Motion.profileT(s, c.seg)));
""", cases)
    for c, js_s, js_t in zip(cases, out["s"], out["t"]):
        for (ps, pv), (js, jv) in zip(c["s"], js_s):
            assert js == pytest.approx(ps, abs=1e-6) and jv == pytest.approx(pv, abs=1e-9)
        for pt, jt in zip(c["t"], js_t):
            assert jt == pytest.approx(pt, abs=1e-6)


def test_the_page_reads_the_profile_settings_from_the_hub(recorded):
    js = (STATIC / "trains.js").read_text()
    assert "seg: t.segment ? { accel: prof.accel, decel: prof.decel, vmax: prof.vmax, ...t.segment } : null" in js
    assert "d.at + (prof.predict_s || 120)" in js and "motion.staleS = (d.profile && d.profile.stale_s) || 60;" in js
    assert all(set(t["segment"]) >= {"accel", "decel", "vmax"} for t in recorded if t["segment"])
    html = (STATIC / "cad.html").read_text()
    assert html.index("/static/maplayers.js") < html.index("/static/motion.js") < html.index("/static/trains.js")
    assert "Motion.railTracker()" in js and "const ANIMATE_MS = 100;" in js and "motion.busy()" in js
    assert 'document.visibilityState !== "hidden"' in js and "if (sig === this.sig) return;" in js


# ---- the predictor's bounds ------------------------------------------------------------------------------------------

PC = 41_000.0                          # a stop's s for the made-up runs below (outbound: s rising)


def fix(**kw):
    """A reported train 1.2 km out of the stop before, 2.3 km run, its fix 10 s after leaving."""
    seg = run(PC - 2300, PC, NOW - 60, NOW + 120)
    s, _ = T.profile_s(NOW - 30, seg)
    f = {"s": round(s, 1), "d": 1, "train": "1350", "mode": "reported", "t0": NOW, "at": 0, "fixAt": NOW - 30,
         "hold": PC - 900, "stop": PC, "until": NOW - 30 + T.PREDICT_S, "seg": seg, "lag": 0.0}
    return f | kw


SWEEP = """
const sweep = (f, n = 400, step = 0.5) => { const r = []; for (let k = 0; k <= n; k++) r.push(Motion.rail(f, k * step)); return r; };
"""


def test_a_reported_train_runs_on_from_its_fix_and_stops_at_hold_s(tmp_path):
    f = fix()
    out = node(tmp_path, SWEEP + "out.r = sweep(IN);", f)["r"]
    ss = [p["s"] for p in out]
    assert ss[0] == pytest.approx(T.profile_s(NOW, f["seg"])[0], abs=1e-6)        # where the run has it now
    assert all(b >= a for a, b in zip(ss, ss[1:]))                                    # never backwards
    assert max(ss) == f["hold"] and all(s <= f["hold"] for s in ss)                  # never past hold_s ...
    held = [p for p in out if p["s"] == f["hold"]]
    assert held and not any(p["moving"] for p in held) and all(p["v"] == 0 for p in held)   # ... and held there
    assert out[0]["moving"] and out[0]["lim"] == f["hold"]


def test_never_past_the_next_stop_never_behind_the_fix_never_after_predict_until(tmp_path):
    out = node(tmp_path, SWEEP + """
out.stop = sweep(IN.stop).map((p) => p.s);
out.behind = sweep(IN.behind).map((p) => p.s);
out.until = sweep(IN.until).map((p) => [p.s, p.moving]);
out.inbound = sweep(IN.inbound).map((p) => p.s);
out.noSeg = sweep(IN.noSeg, 10).map((p) => [p.s, p.moving]);
""", {
        "stop": fix(hold=PC + 1500, until=NOW + 300),                                 # a hold_s past the stop (never sent): the stop
        "behind": fix(lag=40.0),                                       # the run (shifted 40 s late) behind its fix: at the fix
        "until": fix(until=NOW + 5),                                   # 5 s of prediction left
        "inbound": fix(s=PC + 1800.0, d=-1, seg=run(PC + 2300, PC, NOW - 60, NOW + 120), hold=PC + 900, lag=0.0),
        "noSeg": fix(seg=None),
    })
    assert max(out["stop"]) == PC
    assert set(out["behind"][:20]) == {fix()["s"]} and min(out["behind"]) == fix()["s"]
    u = out["until"]
    assert u[10][1] is False and len({s for s, _ in u[10:]}) == 1 and u[9][1] is True    # after 5 s: held, not moving
    assert all(b <= a for a, b in zip(out["inbound"], out["inbound"][1:])) and min(out["inbound"]) == PC + 900
    assert {tuple(x) for x in out["noSeg"]} == {(fix()["s"], False)}                    # no run: at its fix


def test_an_estimated_train_glides_on_the_hubs_run_with_the_same_bounds(tmp_path):
    seg = run(PC - 2300, PC, NOW - 60, NOW + 120)
    s, _ = T.profile_s(NOW, seg)
    f = {"s": round(s, 1), "d": 1, "train": "1350", "mode": "estimated", "t0": NOW, "at": 0, "fixAt": None, "hold": PC,
         "stop": PC, "until": NOW + T.PREDICT_S, "seg": seg, "lag": None}
    out = node(tmp_path, SWEEP + "out.r = sweep(IN).map((p) => p.s);", f)["r"]
    assert out[20] == pytest.approx(T.profile_s(NOW + 10, seg)[0], abs=1e-6)         # the hub's own estimate, in time
    assert max(out) == PC and out[-1] == PC and all(b >= a for a, b in zip(out, out[1:]))


def test_a_stopped_train_is_held_at_the_station_until_the_hub_says_it_left(tmp_path):
    seg = run(PC, PC + 2300, NOW - 30, NOW + 150)                     # by the timetable it left 30 s ago
    stopped = fix(s=PC - 60, mode="stopped", seg=seg, hold=PC - 60, stop=PC + 2300, until=NOW + 90)
    left = fix(s=PC - 60, mode="reported", seg=seg, hold=PC + 900, stop=PC + 2300, fixAt=NOW, until=NOW + 120,
               lag=NOW - T.profile_t(PC - 60, seg))
    out = node(tmp_path, SWEEP + """
out.stopped = sweep(IN.stopped).map((p) => [p.s, p.moving]);
const tk = Motion.railTracker();
const at = (s) => IN.t0ms + s * 1000;
tk.set("1350", { ...IN.stopped, at: at(0) }, at(0));
out.held = []; for (let k = 0; k <= 50; k++) out.held.push(tk.where("1350", at(k * 0.2)).s);
out.busyStopped = tk.busy();
tk.set("1350", { ...IN.left, t0: IN.stopped.t0 + 10, at: at(10) }, at(10));      // the next answer: it left
out.moved = []; for (let k = 50; k <= 100; k++) out.moved.push(tk.where("1350", at(k * 0.2)).s);
out.busyLeft = tk.busy();
""", {"stopped": stopped, "left": left, "t0ms": 1_000_000})
    assert {tuple(x) for x in out["stopped"]} == {(PC - 60, False)}               # the timetable alone moves nothing
    assert set(out["held"]) == {PC - 60} and out["busyStopped"] is False
    assert out["moved"][-1] > PC - 60 and all(b >= a for a, b in zip(out["moved"], out["moved"][1:])) and out["busyLeft"]


# ---- the tracker: stale, corrections along the track, jumps ----------------------------------------------------------

TRACK = """
const tk = Motion.railTracker({ staleS: 60 });
const T0 = 1_000_000;
const at = (s) => T0 + s * 1000;
const drive = (id, from, to, step = 0.1) => { const r = []; for (let t = from; t <= to + 1e-9; t += step) r.push(tk.where(id, at(t)).s); return r; };
"""


def test_a_stale_feed_freezes_the_train_where_it_is(tmp_path):
    out = node(tmp_path, TRACK + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
out.before = drive("a", 0, 59.9, 0.5);
out.after = drive("a", 60.1, 200, 0.5);
out.stale = tk.stale("a"); out.busy = tk.busy();
tk.set("b", { ...IN.f, at: -Infinity }, at(0));                       // the hub says the feed is stale: never moved
out.b = drive("b", 0, 30, 1);
""", {"f": fix(until=NOW + 300, hold=PC)})
    assert out["before"][-1] > out["before"][0]                                        # it ran while the feed was fresh
    assert set(out["after"]) == {out["before"][-1]}                                    # then frozen where it was drawn
    assert out["stale"] is True and out["busy"] is False
    assert set(out["b"]) == {fix()["s"]}                                               # stale from the start: at its fix


def test_a_new_fix_ahead_is_caught_up_along_the_track_in_under_two_seconds(tmp_path):
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 1);
const g = { ...IN.f, s: IN.f.s + 600, t0: IN.f.t0 + 1, fixAt: IN.f.t0 + 1, at: at(1), lag: IN.lag };
const before = tk.where("a", at(1));
tk.set("a", g, at(1));
out.from = before.s;
out.path = drive("a", 1.1, 4);
out.target = []; for (let t = 1.1; t <= 4 + 1e-9; t += 0.1) out.target.push(Motion.rail(g, t - 1).s);
""", {"f": f, "lag": None})
    # (lag None: no prediction from the new fix; it waits at its fix, 600 m on)
    path, target = out["path"], out["target"]
    assert all(b >= a for a, b in zip([out["from"]] + path, path))                      # always forward
    assert path[0] - out["from"] < 600 / 2                                               # not a jump
    caught = next(i for i, (p, q) in enumerate(zip(path, target)) if abs(p - q) < 1e-6)
    assert 1.2 <= (caught + 1) * 0.1 <= 2.0                                             # ~1.75 s
    assert all(abs(p - q) < 1e-6 for p, q in zip(path[caught:], target[caught:]))


def test_a_new_fix_behind_holds_the_train_until_the_prediction_meets_it(tmp_path):
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 10);
const drawn = tk.where("a", at(10)).s;
const g = { ...IN.f, s: drawn - 150, t0: IN.f.t0 + 10, fixAt: IN.f.t0 + 10, at: at(10), hold: IN.f.hold };
g.lag = g.fixAt - Motion.profileT(g.s, g.seg);
tk.set("a", g, at(10));
out.drawn = drawn;
out.path = drive("a", 10.1, 30);
out.target = []; for (let t = 10.1; t <= 30 + 1e-9; t += 0.1) out.target.push(Motion.rail(g, t - 10).s);
""", {"f": f})
    path, target, drawn = out["path"], out["target"], out["drawn"]
    assert all(b >= a for a, b in zip([drawn] + path, path))                             # never slid back
    meet = next(i for i, q in enumerate(target) if q >= drawn)
    assert set(path[:meet]) == {drawn}                                                   # held until the prediction meets it
    assert all(abs(p - q) < 1e-6 for p, q in zip(path[meet + 1:], target[meet + 1:]))    # then on with it


def test_drawn_beyond_a_new_fixs_limit_it_is_brought_back_along_the_track(tmp_path):
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 20);
const drawn = tk.where("a", at(20)).s;
const g = { ...IN.f, s: drawn - 400, mode: "stopped", seg: null, hold: drawn - 400, stop: drawn + 2000, t0: IN.f.t0 + 20, at: at(20) };
tk.set("a", g, at(20));
out.drawn = drawn; out.to = g.s;
out.path = drive("a", 20.1, 24);
""", {"f": f})
    path = out["path"]
    assert all(b <= a for a, b in zip([out["drawn"]] + path, path)) and path[-1] == out["to"]   # back, smoothly, to its fix
    assert out["drawn"] - path[0] < 400 / 2


def test_a_far_jump_the_other_way_or_another_train_is_drawn_at_once(tmp_path):
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + """
const go = (id, g) => { tk.set(id, { ...IN.f, at: at(0) }, at(0)); drive(id, 0, 2); tk.set(id, { ...IN.f, ...g, at: at(2) }, at(2)); return tk.where(id, at(2.1)).s; };
out.far = go("far", { s: IN.f.s + 2500, hold: IN.f.s + 3000, stop: IN.f.s + 3000, seg: null });
out.near = go("near", { s: IN.f.s + 1500, hold: IN.f.s + 1600, stop: IN.f.s + 3000, seg: null });
out.back = go("back", { s: IN.f.s + 300, d: -1, hold: IN.f.s + 300, seg: null, stop: null });
out.other = go("other", { s: IN.f.s + 300, train: "1351", hold: IN.f.s + 300, seg: null, stop: null });
""", {"f": f})
    s = f["s"]
    assert out["far"] == s + 2500 and out["back"] == s + 300 and out["other"] == s + 300
    assert out["near"] < s + 1500 - 100                                                   # under 2 km: caught up, not jumped


def test_the_aircraft_tracker_is_unchanged_without_the_hooks():
    mo = (STATIC / "motion.js").read_text()
    assert "if (this.follow) {" in mo and "this.rest ? this.rest(it.fix)" in mo and "p.moving !== false" in mo
    assert "railTracker = (opts) => new Tracker(rail, { horizonS: Infinity, staleS: 60, ...opts, rest: railRest, follow: railFollow })" in mo
    assert "JUMP_M = 2000, CATCH_S = 1.75" in mo


# ---- the layer's frames (trains.js in node, a fake map) --------------------------------------------------------------

LAYER = r"""
const fs = require("fs");
let now = 0, timers = [], seq = 0;
global.Date = { now: () => now };
global.setTimeout = (fn, ms) => { const t = { id: ++seq, fn, at: now + ms, every: 0, live: true }; timers.push(t); return t.id; };
global.setInterval = (fn, ms) => { const t = { id: ++seq, fn, at: now + ms, every: ms, live: true }; timers.push(t); return t.id; };
global.clearTimeout = global.clearInterval = (id) => { const t = timers.find((x) => x.id === id); if (t) t.live = false; };
global.window = global;
const stub = () => new Proxy(function () {}, { get: (t, k) => (k === "getImageData" ? () => ({}) : stub()), apply: () => stub() });
global.document = { visibilityState: "visible", addEventListener() {}, createElement: () => ({ getContext: () => stub() }), documentElement: {} };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
const IN = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
global.fetch = async () => ({ ok: true, json: async () => {          // the same feed read again and again: older each time
  const a = JSON.parse(JSON.stringify(IN.answer));
  a.age += (now - IN.got) / 1000;
  a.stale = a.age > a.profile.stale_s;
  return a;
} });
let attach = null;
global.MapLayers = { addGroup: (g) => { attach = g.attach; } };
eval(fs.readFileSync(process.argv[2], "utf8"));
eval(fs.readFileSync(process.argv[3], "utf8"));
const calls = {};
const sources = {}, layers = {};
const map = {
  getSource: (id) => sources[id], addSource: (id) => { sources[id] = { setData: () => { calls[id] = (calls[id] || 0) + 1; } }; },
  addLayer: (l) => { layers[l.id] = l; }, getLayer: (id) => layers[id], setLayoutProperty() {}, hasImage: () => true, addImage() {},
  getStyle: () => ({ glyphs: "x", layers: [] }), on() {}, once() {}, isStyleLoaded: () => true, loaded: () => true,
  getContainer: () => ({ offsetParent: {} }), getCanvas: () => ({ style: {} }),
};
let on = true;
const control = { map, on: () => on, panel: { hidden: true }, renderPanel() {} };
const flush = () => new Promise((r) => setImmediate(r));
async function advance(ms, step = 100) {
  const end = now + ms;
  while (now < end) {
    now += step;
    for (const t of timers.filter((x) => x.live && x.at <= now)) { if (t.every) t.at += t.every; else t.live = false; t.fn(); }
    await flush();
  }
}
const snap = () => ({ ...calls });
const frames = async (n) => { const r = []; for (let i = 0; i < n; i++) { const a = snap(); await advance(100); const b = snap();
  r.push([(b["tr-cars"] || 0) - (a["tr-cars"] || 0), (b["tr-trains"] || 0) - (a["tr-trains"] || 0)]); } return r; };
const anim = () => timers.filter((t) => t.live && t.every).length;
(async () => {
  const out = {};
  now = IN.got;
  const m = attach(control);
  await flush(); await flush();
  out.first = snap(); out.anim = anim();
  out.frames = await frames(20);
  out.drawn = [...m.drawn.entries()];
  on = false; m.refresh();
  out.offFrames = await frames(10); out.offAnim = anim();
  on = true; m.refresh(); await flush();
  await advance(70000, 1000);                      // no answer for 70 s (the poll keeps the old one): stale
  out.staleFrames = await frames(10); out.staleAnim = anim();
  out.staleDrawn = [...m.drawn.entries()];
  console.log(JSON.stringify(out));
})();
"""


def test_the_layer_draws_one_setdata_a_source_a_frame_and_none_when_nothing_moves(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.get("/api/cad/trains", "")
    tr.tick()
    clock.t += 3
    answer = json.loads(tr.get("/api/cad/trains", "")[1])
    assert any(t["segment"] and not t["estimated"] for t in answer["trains"])            # something to move
    h, d = tmp_path / "layer.js", tmp_path / "in.json"
    h.write_text(LAYER)
    d.write_text(json.dumps({"answer": answer, "got": clock.t * 1000}))
    # (each poll answers the same feed, older each time: the MTA stopped updating; past stale_s the hub says stale)
    r = subprocess.run([NODE, str(h), str(STATIC / "motion.js"), str(STATIC / "trains.js"), str(d)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["first"] == {"tr-line": 1, "tr-stations": 1, "tr-trains": 1, "tr-cars": 1} and out["anim"] == 1
    assert all(f in ([1, 1], [0, 0]) for f in out["frames"]) and [1, 1] in out["frames"]   # one each, or none
    assert out["offFrames"] == [[0, 0]] * 10 and out["offAnim"] == 0                      # the layer off: no work
    assert out["staleFrames"] == [[0, 0]] * 10 and out["staleAnim"] == 0                  # frozen: nothing moves
    assert all(w["stale"] for _, w in out["staleDrawn"])
    trains = {t["id"]: t for t in answer["trains"]}
    for tid, w in out["drawn"]:
        t = trains[tid]
        if isinstance(t["s"], float):
            assert (w["s"] - t["s"]) * t["d"] >= -0.05                                    # never behind the hub
            if t["mode"] != "stopped":
                assert (w["s"] - t["hold_s"]) * t["d"] <= 0.05                             # never past hold_s
            else:
                assert w["s"] == t["s"]
            assert w["ahead"] == (not t["estimated"] and t["mode"] != "stopped" and (w["s"] - t["s"]) * t["d"] > 5)
