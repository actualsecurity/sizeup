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
    f = {"s": round(s, 1), "d": 1, "train": "9950", "mode": "reported", "t0": NOW, "at": 0, "fixAt": NOW - 30,
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
    f = {"s": round(s, 1), "d": 1, "train": "9950", "mode": "estimated", "t0": NOW, "at": 0, "fixAt": None, "hold": PC,
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
tk.set("9950", { ...IN.stopped, at: at(0) }, at(0));
out.held = []; for (let k = 0; k <= 50; k++) out.held.push(tk.where("9950", at(k * 0.2)).s);
out.busyStopped = tk.busy();
tk.set("9950", { ...IN.left, t0: IN.stopped.t0 + 10, at: at(10) }, at(10));      // the next answer: it left
out.moved = []; for (let k = 50; k <= 100; k++) out.moved.push(tk.where("9950", at(k * 0.2)).s);
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


def test_a_new_fix_ahead_is_caught_up_along_the_track_smoothly(tmp_path):
    """#375, #390: no lunge. A GPS fix 120 m ahead of where the train is drawn: caught up within the band (at most SHARE of
    its speed + EXTRA_SHARE beyond BIG_M), its pace changing no faster than its consist can (the M8's 0.89 m/s^2, less
    at speed), not glided. A GTFS-realtime report (no speed) 150 or 300 m ahead: closed by the same band (#392 final
    check: no fade, no surge). Any fix more than GLIDE_AHEAD_M ahead: glided on to it, never faster than 1.25 v + 2;
    too far for GLIDE_MAX_S of that: faded out and in at the fix."""
    out = node(tmp_path, TRACK + GLIDE + """
const go = (id, f, by) => {
  tk.set(id, { ...f, at: at(0) }, at(0));
  drive(id, 0, 10);
  const from = tk.where(id, at(10)).s;
  const g = { ...f, s: (f.seg ? from : Motion.rail(f, 10).s) + by, t0: f.t0 + 10, fixAt: f.seg ? f.t0 + 10 : f.fixAt + 10, at: at(10) };
  if (f.seg) g.lag = g.fixAt - Motion.profileT(g.s, g.seg);
  tk.set(id, g, at(10));
  const path = watch(id, 10.1, 29);
  return { from, path, v: path.map((p) => Motion.rail(g, p[0] - 10).v), tg: [0, 2, 4, 4.1, 19].map((x) => Motion.rail(g, x).s) };
};
out.gps = go("gps", IN.gps, 120); out.gpsFar = go("gpsFar", IN.gps, 600);
out.est150 = go("est150", IN.est, 150); out.est300 = go("est300", IN.est, 300);
""", {"gps": fix(v=20.0, fixAt=NOW, seg=None, hold=PC + 5000, stop=PC + 5000, until=NOW + 300),
      "est": fix(until=NOW + 300, hold=PC + 3000, stop=PC + 3000)})
    g = out["gps"]
    path = [g["from"]] + [p[1] for p in g["path"]]
    steps = [b - a for a, b in zip(path, path[1:])]
    assert min(steps) >= 0 and not any(p[2] for p in g["path"])                      # forward, no glide
    speeds = [x / 0.1 for x in steps]
    assert max(abs(b - a) for a, b in zip(speeds, speeds[1:])) <= 0.1 * 0.89 + 0.02   # within the M8's limits
    assert max(speeds) <= 1.25 * 20 + 2 + 0.01                                         # never over 1.25 v + 2: no surge
    assert abs(path[-1] - g["tg"][-1]) < 0.35 * 120                                    # catching up, gently
    f = out["gpsFar"]                        # 600 m: a glide no faster than 1.25 v + 2 would take over GLIDE_MAX_S: faded
    assert min(p[3] for p in f["path"][:8]) < 0.2 and f["path"][10][3] == 1 and not f["path"][0][2]
    for name in ("est150", "est300"):        # GTFS-realtime reports 150 / 300 m ahead: the band closes them, no fade,
        e = out[name]                        # no glide, never faster than 1.25 v + 2
        path = [e["from"]] + [p[1] for p in e["path"]]
        assert all(p[3] == 1 and not p[2] for p in e["path"])
        assert all((b - a) / 0.1 <= 1.25 * e["v"][i] + 2 + 0.05 for i, (a, b) in enumerate(zip(path, path[1:])))
        assert all(b >= a for a, b in zip(path, path[1:]))


def test_a_new_fix_behind_slows_the_train_never_back_never_stopped(tmp_path):
    """#375: drawn ahead of a new fix's prediction, the train slows (never under SLOW_SHARE of its speed while it
    runs) until the prediction meets it: no step back, and no standing still while the train runs."""
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 10);
const drawn = tk.where("a", at(10)).s;
const g = { ...IN.f, s: drawn - 150, t0: IN.f.t0 + 10, fixAt: IN.f.t0 + 10, at: at(10), hold: IN.f.hold };
g.lag = g.fixAt - Motion.profileT(g.s, g.seg);
tk.set("a", g, at(10));
out.drawn = drawn;
out.path = drive("a", 10.1, 40);
out.target = []; for (let t = 10.1; t <= 40 + 1e-9; t += 0.1) out.target.push(Motion.rail(g, t - 10));
""", {"f": f})
    path, target, drawn = out["path"], out["target"], out["drawn"]
    steps = [b - a for a, b in zip([drawn] + path, path)]
    assert min(steps) >= 0                                                           # never slid back
    moving = [i for i, q in enumerate(target) if q["moving"] and q["v"] > 1]
    assert all(steps[i] > 0 for i in moving)                                         # never stood still while it runs
    gaps = [p - q["s"] for p, q in zip(path, target)]
    assert all(b <= a + 0.05 for a, b in zip(gaps, gaps[1:]))                       # closing all the time
    meet = next(i for i, g in enumerate(gaps) if abs(g) < 5)                         # (150 m: well past BIG_M, quickly;
    assert meet * 0.1 < 30 and all(abs(g) < 5 for g in gaps[meet:])                 # the last few metres within SHARE)


GLIDE = """
const watch = (id, from, to) => { const r = []; for (let t = from; t <= to + 1e-9; t += 0.1) { const s = tk.where(id, at(t)).s;
  r.push([t, s, Motion.snapped(tk.items.get(id)), Motion.fadeOf(tk.items.get(id), at(t))]); } return r; };
"""


def glide_ok(path, to, d=1, v=0.0):
    """A correction back onto `to` (path [[t, s, marked, opacity]] from the frame the fix came): braked to rest (if it
    was running: at most BRAKE_HARD, eased in), then glided back along the track (a quintic: no jolt at either end) no
    faster than GLIDE_PEAK_MPS, its acceleration at most GLIDE_BACK_ACC, marked meanwhile, as long as that takes (to
    GLIDE_BACK_MAX_S); or, too far for that, faded out and in at the fix. Then at rest there and unmarked. Returns (how
    far, peak speed back or None if faded, how long)."""
    ss = [p[1] for p in path]
    steps = [(b - a) * d for a, b in zip(ss, ss[1:])]
    far = max((s - to) * d for s in ss)
    peak = max(25.0, 1.5 * v)
    faded = [i for i, p in enumerate(path) if p[3] < 1]
    if faded:                                                                        # too far to glide: faded
        assert len(faded) * 0.1 <= 0.9
        assert all(abs(x) <= 2.5 for i, x in enumerate(steps) if i + 1 not in faded and i not in faded)
        assert ss[-1] == to
        return far, None, None
    speeds = [x / 0.1 for x in steps]
    rest = next(i for i, x in enumerate(steps) if x <= 0)
    assert all(x >= -1e-9 for x in steps[:rest])
    dv = [abs(b - a) / 0.1 for a, b in zip(speeds, speeds[1:])]
    assert max(dv[:rest + 1] or [0]) <= 4.0 + 1.3                                  # braking: at most BRAKE_HARD
    assert max(dv[rest + 1:] or [0]) <= 3.0 + 0.05                                 # gliding back: at most GLIDE_BACK_ACC
    assert -min(steps) / 0.1 <= 25.0 + 0.3                                           # back no faster than GLIDE_PEAK_MPS
    back = [i for i, x in enumerate(steps) if x < -1e-9]
    if back:
        assert (back[-1] - back[0] + 1) * 0.1 <= 25.3
        assert all(path[i + 1][2] for i in back[:-1])                                # marked while it glides
    assert ss[-1] == to and not path[-1][2]
    return far, (-min(steps) / 0.1 if back else 0.0), (len(back) * 0.1)


def test_drawn_beyond_a_new_fixs_limit_it_is_braked_then_glided_back(tmp_path):
    """#375, #390 ("I'm fine with an overshoot if we keep it smooth, and then ... we nudge it back like we did for
    planes" ... "less surgy"): drawn running when a new fix says it stopped behind it, a little or a lot: braked hard to
    rest (as little past the fix as smoothly can be), then glided back along the track onto it (at most 3 m/s^2,
    25 m/s, 25 s: about 330 m); 450 m back is further than that: faded out and in."""
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + GLIDE + """
const back = (id, by) => {
  tk.set(id, { ...IN.f, at: at(0) }, at(0));
  drive(id, 0, 20);
  const drawn = tk.where(id, at(20)).s;
  const g = { ...IN.f, s: drawn - by, mode: "stopped", seg: null, hold: drawn - by, stop: drawn + 2000, t0: IN.f.t0 + 20, at: at(20) };
  tk.set(id, g, at(20));
  return { to: g.s, path: [[20, drawn, false, 1], ...watch(id, 20.1, 60)] };
};
out.r = {}; for (const by of [2, 20, 200, 450]) out.r[by] = back("b" + by, by);
""", {"f": f})
    res = {by: glide_ok(r["path"], r["to"]) for by, r in out["r"].items()}
    assert all(res[k][1] is not None for k in ("2", "20", "200")) and res["450"][1] is None   # glided; beyond ~330 m: faded


def test_a_far_jump_the_other_way_or_another_train_is_drawn_at_once(tmp_path):
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + """
const go = (id, g) => { tk.set(id, { ...IN.f, at: at(0) }, at(0)); drive(id, 0, 2); tk.set(id, { ...IN.f, ...g, at: at(2) }, at(2)); return tk.where(id, at(2.1)).s; };
out.far = go("far", { s: IN.f.s + 2500, hold: IN.f.s + 3000, stop: IN.f.s + 3000, seg: null });
out.near = go("near", { s: IN.f.s + 800, hold: IN.f.s + 1600, stop: IN.f.s + 3000, seg: null });
out.back = go("back", { s: IN.f.s + 300, d: -1, hold: IN.f.s + 300, seg: null, stop: null });
out.other = go("other", { s: IN.f.s + 300, train: "9951", hold: IN.f.s + 300, seg: null, stop: null });
""", {"f": f})
    s = f["s"]
    assert out["far"] == s + 2500 and out["back"] == s + 300 and out["other"] == s + 300
    assert out["near"] < s + 800 - 100                  # (drawn ~470 m on) under SNAP_AHEAD_M ahead: caught up, not jumped


def test_the_aircraft_tracker_is_unchanged_without_the_hooks():
    mo = (STATIC / "motion.js").read_text()
    assert "if (this.follow) {" in mo and "this.rest ? this.rest(it.fix)" in mo and "p.moving !== false" in mo
    assert "railTracker = (opts) => new Tracker(rail, { horizonS: Infinity, staleS: 60, ...opts, rest: railRest, follow: railFollow })" in mo
    assert "JUMP_M = 2000, TAU_S = 6, RAMP_S = 1" in mo


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
const control = { map, on: (k) => k === "trains" && on, panel: { hidden: true }, renderPanel() {} };
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


def test_a_gps_fix_runs_on_at_its_own_speed_and_slows_only_on_evidence(tmp_path):
    """MTA TrainTime (#362, #390): a GPS fix with its speed (v, m/s) runs on at that speed, however old (no slowing on
    age alone: #375's soft horizon made a steady train go quick, slow, hollow and quick again), until RAIL_CRUISE_S
    (20 s); a fix older than that eases to a stop at the consist's service brake (b); never past hold_s or the next stop, never
    after predict_until; no speed: held at its fix."""
    f = fix(v=20.0, fixAt=NOW - 4, hold=PC + 3000, stop=PC + 3000, until=NOW + 200, seg=None)
    out = node(tmp_path, SWEEP + "out.r = sweep(IN.f, 200, 0.5); out.still = sweep(IN.still, 4); out.held = sweep(IN.held, 200, 0.5);",
               {"f": f, "still": fix(v=0.0, seg=None), "held": fix(v=20.0, fixAt=NOW, hold=fix()["s"] + 300, until=NOW + 200, seg=None)})
    r, ss = out["r"], [p["s"] for p in out["r"]]
    cruise = 20 - 4                                                                  # RAIL_CRUISE_S of its age
    for k in range(0, 2 * cruise + 1):
        assert ss[k] == pytest.approx(f["s"] + 20.0 * (4 + k * 0.5), abs=1e-6) and r[k]["v"] == 20.0   # steady: 16 s
    b = 1.34                                                                         # the M8's service brake
    k = 2 * cruise + 6                                                               # 3 s into the ease (eased in at
    ramp = b / 1.5                                                                   # RAIL_JERK, 1.5 m/s^3)
    assert r[k]["v"] == pytest.approx(20.0 - b * ramp / 2 - b * (3 - ramp), abs=0.1)
    rest = f["s"] + 20.0 * 20 + 20.0 ** 2 / (2 * b) + 20.0 * ramp / 2
    assert ss[-1] == pytest.approx(rest, abs=3.0) and not r[-1]["moving"] and r[-1]["v"] == 0   # at rest ~165 m on
    assert all(b >= a for a, b in zip(ss, ss[1:]))
    held = [p["s"] for p in out["held"]]
    assert max(held) == fix()["s"] + 300 and not out["held"][-1]["moving"]            # never past hold_s
    assert all(p["s"] == out["still"][0]["s"] and not p["moving"] for p in out["still"])


def test_no_slowing_at_a_steady_speed_while_fixes_come_at_their_normal_ages(tmp_path):
    """#390: TrainTime's fixes are 3 to 16 s old when read (median ~6), a new one every 3 to 12 s. A steady train's
    prediction keeps its full speed to RAIL_CRUISE_S (20 s)."""
    f = fix(v=23.0, fixAt=NOW, hold=PC + 5000, stop=PC + 5000, until=NOW + T.TT_HORIZON_S, seg=None)
    out = node(tmp_path, SWEEP + "out.r = sweep(IN, 60, 0.5); out.cruise = Motion.RAIL.RAIL_CRUISE_S;", f)
    assert out["cruise"] >= 16
    assert all(p["v"] == 23.0 and p["moving"] for p in out["r"][:2 * out["cruise"] + 1])


def test_a_braking_train_is_predicted_to_stop_where_it_will(tmp_path):
    """#375: the hub's filter says its speed is falling (a < -0.2 m/s^2, confirmed: trains.speed_check): predicted
    s0 + v t + a t^2 / 2, stopping where v reaches 0 (v^2 / 2|a| on), and that is its limit (lim) now: an emergency
    brake as hard as the feed shows it (the hub clamps a at 1.5 m/s^2), not carried on at its speed."""
    v, a = 12.0, -0.75
    f = fix(v=v, a=a, fixAt=NOW, hold=PC + 5000, stop=PC + 5000, until=NOW + T.TT_HORIZON_S, seg=None)
    out = node(tmp_path, SWEEP + """out.r = sweep(IN, 60, 0.5); out.gentle = sweep({ ...IN, a: -0.1 }, 60, 0.5);
out.hard = sweep({ ...IN, a: -3 }, 60, 0.5);""", f)
    ss = [p["s"] - f["s"] for p in out["r"]]
    stop = v * v / (2 * -a)                                                          # 96 m on
    assert ss[8] == pytest.approx(v * 4 + 0.5 * a * 16, abs=0.1)                      # 4 s on: braking
    assert max(ss) == pytest.approx(stop, abs=1.0) and ss[-1] == pytest.approx(stop, abs=1.0)
    assert out["r"][0]["lim"] == pytest.approx(f["s"] + stop, abs=1e-6)
    k = next(i for i, x in enumerate(ss) if abs(x - stop) < 1.0)
    assert k * 0.5 <= v / -a + 1 and not out["r"][-1]["moving"] and out["r"][-1]["v"] == 0
    assert out["gentle"][24]["s"] - f["s"] == pytest.approx(v * 12)                  # under 0.2 m/s^2: not braking, on
    assert out["hard"][-1]["s"] - f["s"] == pytest.approx(v * v / (2 * 3), abs=1.0)  # as hard as the feed shows it


def test_a_train_is_braked_into_its_next_stop_as_a_train_stops(tmp_path):
    """#390: a scheduled stop ahead is braked into at an ordinary stop's rate (bs, 0.8 m/s^2): the speed never over
    sqrt(2 bs (distance left)), at rest on the stop's mark; not run at full speed into it and stopped dead."""
    v = 12.0                                                                         # (all within RAIL_CRUISE_S of its fix)
    f = fix(v=v, fixAt=NOW, hold=PC, stop=PC, s=PC - 150, until=NOW + T.TT_HORIZON_S, seg=None)
    out = node(tmp_path, SWEEP + "out.r = sweep(IN, 280, 0.25);", f)["r"]
    for p in out:
        assert p["v"] <= math.sqrt(2 * 0.8 * max(0.0, PC - p["s"])) + 0.05
    assert out[-1]["s"] == PC and out[-1]["v"] == 0 and not out[-1]["moving"]
    dv = [(a["v"] - b["v"]) / 0.25 for a, b in zip(out, out[1:])]
    assert max(dv) <= 0.8 + 0.05                                                     # braked at bs, no harder
    k = next(i for i, p in enumerate(out) if p["v"] < v)
    assert out[k]["s"] == pytest.approx(PC - v * v / 1.6, abs=v * 0.25 + 1)          # braking starts 90 m out


def test_a_consists_limits_bound_the_drawn_pace(tmp_path):
    """#390: the drawn train's speed never changes faster than its consist can (the hub's dyn): speeding up at most
    min(a0, pw / v), slowing at most b, whatever the corrections ask; a diesel (heavier, slower) slower than an M8."""
    from sizeup.trains import consist
    m8, diesel = consist("M8", 8), consist("MNR_DIESEL", 7)
    out = node(tmp_path, TRACK + """
const run = (id, dyn) => {
  const f = { ...IN.f, dyn, v: 5, at: at(0) };
  tk.set(id, f, at(0));
  drive(id, 0, 1);
  tk.set(id, { ...f, s: f.s + 400, v: 30, t0: f.t0 + 1, fixAt: f.fixAt + 1, at: at(1) }, at(1));   // far ahead and fast
  const r = []; for (let t = 1.1; t <= 40; t += 0.1) { tk.where(id, at(t)); r.push(tk.items.get(id).u); }
  return r;
};
out.m8 = run("m8", IN.m8); out.diesel = run("d", IN.diesel);
""", {"f": fix(v=5.0, fixAt=NOW, hold=PC + 5000, stop=PC + 5000, until=NOW + 200, seg=None), "m8": m8, "diesel": diesel})
    for name, dyn in (("m8", m8), ("diesel", diesel)):
        u = out[name]
        for a, b in zip(u, u[1:]):
            assert (b - a) / 0.1 <= min(dyn["a0"], dyn["pw"] / max(1.0, a)) + 1e-6
            assert (a - b) / 0.1 <= dyn["b"] + 1e-6
    assert out["diesel"][100] < out["m8"][100]                                        # 10 s on: the diesel slower


def test_drift_past_a_mid_track_stop(tmp_path):
    """#375 ("if a train stops mid track we want to know where accurately") against #390 (a steady train is drawn
    steadily). A service brake (0.75 m/s^2) right after a moving fix:
      - with evidence (the next fix says its speed is falling, as TrainTime's 3 to 12 s fixes do): predicted to stop where
        it will, drawn close to it;
      - with none (no fix at all until it has stopped: the hard case): drawn on at its speed until the fix is 20 s old,
        then eased at its service brake; the stopped fix glides it back. The trade-off for no slowing on age alone."""
    out = node(tmp_path, TRACK + """
out.r = [];
for (const v of IN.vs) {
  const id = "v" + v, f = { ...IN.f, v, fixAt: IN.f.t0, at: at(0) };
  tk.set(id, f, at(0));
  const stopAt = v / 0.75, stop = IN.f.s + v * v / 1.5;
  // evidence: a fix 4 s into the brake, read 4 s later
  const e = { ...f, id: "e" + v };
  tk.set(e.id, e, at(0));
  drive(e.id, 0, 8);
  const t4 = 4, s4 = IN.f.s + v * t4 - 0.75 * 8;
  tk.set(e.id, { ...f, s: s4, v: v - 3, a: -0.75, t0: IN.f.t0 + t4, fixAt: IN.f.t0 + t4, at: at(8) }, at(8));
  const pe = drive(e.id, 8.1, 80);
  const path = drive(id, 0, 60);
  tk.set(id, { ...f, s: stop, hold: stop, v: 0, mode: "stopped", t0: IN.f.t0 + stopAt + 6, fixAt: IN.f.t0 + stopAt + 6, at: at(60) }, at(60));
  tk.where(id, at(60.1));
  const glided = Motion.snapped(tk.items.get(id)), marked = glided || Motion.fadeOf(tk.items.get(id), at(60.3)) < 1;
  const after = drive(id, 60.2, 90);
  out.r.push({ v, past: Math.max(...path) - stop, end: after[after.length - 1] - stop, marked, glided: glided || !(Math.max(...path) - stop > 0.5),
               ev: Math.max(...pe) - stop, evEnd: pe[pe.length - 1] - stop });
}
""", {"f": fix(fixAt=NOW, hold=PC + 5000, stop=PC + 5000, until=NOW + T.TT_HORIZON_S, seg=None),
      "vs": [4, 8, 12, 16, 20, 23.25, 30, 36]})
    for r in out["r"]:
        assert r["ev"] <= 75 and abs(r["evEnd"]) <= 30          # evidence: on past it, braked smoothly (36 m/s: 61 m)
        assert abs(r["end"]) < 0.01 and r["marked"] == (r["past"] > 0.5)             # the stopped fix: glided there
    hard = {r["v"]: r["past"] for r in out["r"]}
    assert all(r["glided"] for r in out["r"] if r["v"] <= 27)                        # no-evidence stops to 60 mph: glided
    assert 150 < hard[20] < 330                                                      # the trade-off, measured (see #392)


def test_the_lag_cannot_grow_when_every_fix_is_old(tmp_path):
    """#379: fixes 28 s old when they come, every 5 s, at 36 m/s (80 mph). They are past RAIL_CRUISE_S, so each is eased
    for want of news, but the drawn train's catch-up runs off the fix's own speed: the lag stays bounded (it can't run
    faster than its consist's vmax to close it, #390: no surge; #378's drew it further behind with every fix until the
    2 km jump: 1.6 km here)."""
    out = node(tmp_path, TRACK + """
const lag = [];
for (let k = 0; k <= 30; k++) {
  const t = 28 + 5 * k, f = { ...IN.f, s: IN.f.s + 36 * (t - 28), v: 36, fixAt: IN.f.t0 + t - 28, t0: IN.f.t0 + t - 28,
                               until: IN.f.t0 + t - 28 + IN.horizon, at: at(t - 28) };
  tk.set("a", f, at(t));
  for (const p of drive("a", t, t + 4.9)) lag.push(p);
}
out.lag = lag;
""", {"f": fix(fixAt=NOW, hold=PC + 50_000, stop=PC + 50_000, seg=None), "horizon": T.TT_HORIZON_S})
    s0 = fix()["s"]
    n = len(out["lag"]) // 31
    truth = [s0 + 36 * (28 + 5 * k + 0.1 * j) for k in range(31) for j in range(n)]
    lags = [t - p for t, p in zip(truth, out["lag"])]
    peak = max(lags)
    assert peak < 500 and lags[-1] <= peak + 1                                       # bounded, not growing


def test_noisy_gps_fixes_glide_forward_along_the_track_and_a_stop_stays_put(tmp_path):
    """MTA TrainTime (#362): noisy GPS fixes every 5 s, filtered on the hub (trains.along_filter), drawn by the page's
    tracker: forward only, no jumps, at about the feed's speed; stopped: not moved."""
    import random
    rnd, fixes, last, s_true = random.Random(3), [], None, 39000.0
    for k in range(16):
        stopped = k >= 10
        t = NOW + 5 * k
        vf = 0.0 if stopped else 20.0 + rnd.uniform(-1, 1)
        s, v, acc = T.along_filter(last, s_true + rnd.uniform(-40, 40), 1, t, vf)
        last = {"s": s, "v": v, "a": acc, "vf": vf, "fix_at": t}
        fixes.append({"s": round(s, 1), "d": 1, "train": "9902", "mode": "stopped" if v == 0 else "reported", "t0": t, "k": k,
                      "fixAt": t, "hold": s + 1500, "stop": 60000, "until": t + T.TT_HORIZON_S, "seg": None, "lag": None, "v": v,
                      "a": acc})
        s_true += 0 if stopped else 100.0
    out = node(tmp_path, TRACK + """
const path = [];
for (const f of IN.fixes) {
  tk.set("a", { ...f, at: at(f.k * 5) }, at(f.k * 5));
  path.push(...drive("a", f.k * 5, f.k * 5 + 4.9));
}
out.path = path;
""", {"fixes": fixes})
    path = out["path"]
    steps = [b - a for a, b in zip(path, path[1:])]
    # (its speed reads 0 at once, at 20 m/s, with no fall before: braked on past where it stopped, then glided back)
    assert min(steps) >= -8.0 and max(steps) < 8.0                                       # no jumps (20 m/s is 2 m a frame)
    assert min(steps[:450]) >= 0                                                         # never back while it runs
    moving = path[50:450]
    assert (moving[-1] - moving[0]) / 40.0 == pytest.approx(20.0, rel=0.25)              # about the feed's speed
    assert len(set(path[-40:])) == 1                                                     # stopped: put


def test_a_glide_keeps_its_speed_when_a_new_fix_re_aims_it(tmp_path):
    """#390 re-review: a new fix mid-glide re-aims the glide from where it is drawn at the speed it is drawn at: no frame
    where it stops (Tracker.set draws it at the same moment, dt 0), no step in pace."""
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + GLIDE + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 20);
const drawn = tk.where("a", at(20)).s;
const g = { ...IN.f, s: drawn - 30, mode: "stopped", seg: null, hold: drawn - 30, stop: drawn + 2000, t0: IN.f.t0 + 20, at: at(20) };
tk.set("a", g, at(20));
const p1 = watch("a", 20.1, 30);
tk.set("a", { ...g, s: g.s - 5, hold: g.s - 5, t0: g.t0 + 10, at: at(30.05) }, at(30.05));   // re-aimed 5 m further back
out.path = [...p1, ...watch("a", 30.1, 60)];
""", {"f": f})
    ss = [p[1] for p in out["path"]]
    v = [(b - a) / 0.1 for a, b in zip(ss, ss[1:])]
    k = next(i for i, p in enumerate(out["path"]) if p[0] > 30.05)
    assert out["path"][k - 1][2] and v[k - 2] < -0.5                                   # gliding back when the fix came
    assert abs(v[k] - v[k - 2]) <= 2 * 0.1 * 3 + 0.1                                  # its speed carried on
    assert min(abs(x) for x in v[k - 3:k + 3]) > 0.1                                  # never a stop on the way
    assert all(abs(b - a) <= 0.1 * 3 + 0.06 for a, b in zip(v[k - 30:k + 30], v[k - 29:k + 31]))     # no step in pace


def test_no_back_nudge_when_a_fix_goes_quiet(tmp_path):
    """#390 re-review: no fix for a minute: eased to a stop (the ease eased in at RAIL_JERK, as the drawn train's own
    pace), drawn onto its rest without overshooting it and gliding back."""
    out = node(tmp_path, TRACK + """
for (const v of IN.vs) {
  tk.set("q" + v, { ...IN.f, v, fixAt: IN.f.t0, at: at(0) }, at(0));
  out["q" + v] = drive("q" + v, 0, 70);
}
""", {"f": fix(fixAt=NOW, hold=PC + 5000, stop=PC + 5000, until=NOW + T.TT_HORIZON_S, seg=None), "vs": [10, 20, 30, 36]})
    for k, path in out.items():
        assert min(b - a for a, b in zip(path, path[1:])) >= -1e-6, k                    # never back
        assert path[-1] == path[-50]                                                   # and at rest



@pytest.mark.parametrize("by, move", [(60, -3), (120, -6), (200, -10), (300, -15), (250, 15)])
def test_a_re_aim_mid_glide_never_fades_or_jumps(tmp_path, by, move):
    """#392 final check: gliding back (here 8-17 m/s with 7-65 m left when it comes), a new fix that moves the target is
    taken into the glide as a smooth shift: never a fade, never a jump, never a hitch, at most 4.5 m/s^2."""
    f = fix(until=NOW + 300, hold=PC)
    out = node(tmp_path, TRACK + GLIDE + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 20);
const drawn = tk.where("a", at(20)).s;
const g = { ...IN.f, s: drawn - IN.by, mode: "stopped", seg: null, hold: drawn - IN.by, stop: drawn + 2000, t0: IN.f.t0 + 20, at: at(20) };
tk.set("a", g, at(20));
const p1 = watch("a", 20.1, 60);
// the fix comes late in the glide: still 8 m/s or more back, 7 m or more left, but too little room to stop gently
let k = -1;
for (let i = 1; i < p1.length && k < 0; i++) {
  const sp = (p1[i - 1][1] - p1[i][1]) / 0.1, left = p1[i][1] - g.s;
  if (sp >= 8 && left >= 7 && left <= (sp * sp) / 2.5) k = i;
}
if (k < 0) { let best = 0; for (let i = 1; i < p1.length; i++) { const sp = (p1[i - 1][1] - p1[i][1]) / 0.1; if (sp > best) { best = sp; k = i; } } }
out.at = p1[k][0]; out.left = p1[k][1] - g.s; out.speed = (p1[k - 1][1] - p1[k][1]) / 0.1;
""", {"f": f, "by": by})
    t_fix = out["at"]
    assert out["speed"] >= 8 and out["left"] >= 7                                   # (the case the review found)
    out2 = node(tmp_path, TRACK + GLIDE + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 20);
const drawn = tk.where("a", at(20)).s;
const g = { ...IN.f, s: drawn - IN.by, mode: "stopped", seg: null, hold: drawn - IN.by, stop: drawn + 2000, t0: IN.f.t0 + 20, at: at(20) };
tk.set("a", g, at(20));
const p1 = watch("a", 20.1, IN.t);
const t2 = IN.t + 0.05;
tk.set("a", { ...g, s: g.s - IN.move, hold: g.s - IN.move, t0: g.t0 + 1, at: at(t2) }, at(t2));
out.path = [...p1, ...watch("a", IN.t + 0.1, IN.t + 40)]; out.to = g.s - IN.move;
""", {"f": f, "by": by, "move": move, "t": round(t_fix, 1)})
    path = out2["path"]
    assert all(p[3] == 1 for p in path)                                              # never a fade
    ss = [p[1] for p in path]
    v = [(b - a) / 0.1 for a, b in zip(ss, ss[1:])]
    assert max(abs(x) for x in v) <= 26                                              # never a jump
    assert max(abs(b - a) for a, b in zip(v, v[1:])) <= 0.1 * 4.5 + 0.35            # no hitch (the 0.05 s fix frame aside)
    assert ss[-1] == out2["to"]
