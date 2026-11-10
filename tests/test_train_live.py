"""A train on MTA TrainTime moves smoothly on the map (#375): the hub (sizeup/trains.py Trains, its TrainTime poller,
Tracker and along_filter) answers every 5 s as it does live, and the page's own trains.js + motion.js (in node, a
map stand-in, the page's 5 s poll) draw it every 100 ms frame. The feed has the shape measured from the real API on
2026-11-09 (24 polls, 9 New Haven Line trains): a GPS fix every 3 s, each 3 to 16 s old when read, with GPS noise;
the train runs at a steady 52 mph, or brakes to a stop mid-track. Before #375 the page carried a fix on for only 10 s
past its time (TT_HORIZON_S), so the train stood still for seconds between fixes and then lunged on at up to 140 m/s;
then for 30 s at its full speed, so a train that stopped mid-track was drawn hundreds of metres past where it stood.
Now: full speed for 8 s of a fix's age, then slowing; braked to a stop when its speed falls.

No MTA: conftest keeps TrainTime's address off, and every feed here is the test's (tests/test_traintime.py Fetch)."""

import json
import random
import subprocess

import pytest

from sizeup import trains as T
from sizeup.trains import Model, parse_static
from tests.test_train_motion import NODE, STATIC
from tests.test_traintime import LOC, provider
from tests.test_trains import GTFS

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

V = 23.25                    # m/s: 52 mph
GPS_EVERY_S = 3              # a new GPS fix this often ...
LATE_S = (4, 6, 9, 3, 7, 12, 5, 8, 6, 4, 10, 6)   # ... published this late (+ up to 5 s to the next poll: 3 to 16 s old)
NOISE_M = 10.0
RUN_S = 62                   # (Rye, its next stop, is about 66 s on: it is held there)

LIVE = r"""
const fs = require("fs");
let now = 0, timers = [], seq = 0;
global.Date = { now: () => now };
global.performance = { now: () => now, measure() {}, clearMeasures() {} };
global.setTimeout = (fn, ms) => { const t = { id: ++seq, fn, at: now + ms, every: 0, live: true }; timers.push(t); return t.id; };
global.setInterval = (fn, ms) => { const t = { id: ++seq, fn, at: now + ms, every: ms, live: true }; timers.push(t); return t.id; };
global.clearTimeout = global.clearInterval = (id) => { const t = timers.find((x) => x.id === id); if (t) t.live = false; };
global.window = global;
const stub = () => new Proxy(function () {}, { get: (t, k) => (k === "getImageData" ? () => ({}) : stub()), apply: () => stub() });
global.document = { visibilityState: "visible", addEventListener() {}, createElement: () => ({ getContext: () => stub() }), documentElement: {} };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
const IN = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
// the hub: its answer as of now (the latest it made), JSON as the page gets it
global.fetch = async () => { let a = null; for (const x of IN.answers) if (x.t * 1000 <= now) a = x.body; return { ok: true, json: async () => JSON.parse(a) }; };
let attach = null;
global.MapLayers = { addGroup: (g) => { attach = g.attach; } };
eval(fs.readFileSync(process.argv[2], "utf8"));
eval(fs.readFileSync(process.argv[3], "utf8"));
const sources = {}, layers = {};
const map = {
  getSource: (id) => sources[id], addSource: (id) => { sources[id] = { setData() {} }; },
  addLayer: (l) => { layers[l.id] = l; }, getLayer: (id) => layers[id], setLayoutProperty() {}, hasImage: () => true, addImage() {},
  getStyle: () => ({ glyphs: "x", layers: [] }), on() {}, once() {}, isStyleLoaded: () => true, loaded: () => true,
  getContainer: () => ({ offsetParent: {} }), getCanvas: () => ({ style: {} }),
};
const control = { map, on: (k) => k === "trains", panel: { hidden: true }, renderPanel() {} };
const flush = () => new Promise((r) => setImmediate(r));
(async () => {
  const out = { frames: [] };
  now = IN.start * 1000;
  const m = attach(control);
  await flush(); await flush();
  while (now < IN.end * 1000) {
    now += 100;
    for (const t of timers.filter((x) => x.live && x.at <= now)) { if (t.every) t.at += t.every; else t.live = false; t.fn(); }
    await flush(); await flush();
    const w = m.drawn && m.drawn.get(IN.id);
    out.frames.push([now / 1000, w ? w.s : null]);
  }
  console.log(JSON.stringify(out));
})();
"""


def steady(dt):
    """(progress m, speed m/s) dt seconds into the run: a steady V."""
    return V * dt, V


def braking(at_s, b=0.75):
    """A train at V that brakes at b m/s^2 (a service brake) from at_s seconds into the run to a stop mid-track."""
    def go(dt):
        if dt <= at_s:
            return V * dt, V
        u = min(dt - at_s, V / b)
        return V * at_s + V * u - 0.5 * b * u * u, max(0.0, V - b * u)
    return go


def drive(tmp_path, run=steady, run_s=RUN_S):
    """(frames [[t, drawn s]], truth(t)): the hub's answers over run_s, drawn by the page. run(dt) -> (progress, speed)."""
    tr, clock, fetch = provider(tmp_path)
    model = Model(parse_static(GTFS), tracks=json.loads(T.TRACKS_FILE.read_text()))
    recs = json.loads(LOC)
    mover = next(r for r in recs if r["train_num"] == "9901")          # westbound: s falls
    _, s0 = model.line.project(mover["location"]["latitude"], mover["location"]["longitude"])
    t0, rnd = clock.t, random.Random(375)
    noise = [rnd.gauss(0, NOISE_M) for _ in range(run_s)]
    truth = lambda t: s0 - run(t - t0)[0]

    def feed(now):                                                    # the newest GPS fix published by now
        k = int((now - t0) // GPS_EVERY_S)
        while k > 0 and t0 + GPS_EVERY_S * k + LATE_S[k % len(LATE_S)] > now:
            k -= 1
        ts = t0 + GPS_EVERY_S * k
        lat, lon, _ = model.line.at(truth(ts) + noise[k])
        loc = {**mover["location"], "latitude": round(lat, 6), "longitude": round(lon, 6), "timestamp": int(ts),
               "speed": round(run(ts - t0)[1] * 3600 / 1609.344, 1)}
        return json.dumps([{**r, "location": loc} if r is mover else r for r in recs]).encode()

    answers = []
    tr.get("/api/cad/trains", "")                                     # a page has the layer on: TrainTime every 5 s
    for step in range(run_s):
        clock.t = t0 + step
        fetch.loc = feed(clock.t)
        tr.tick()
        tr.tt.tick()
        body = tr.get("/api/cad/trains", "")[1].decode()
        answers.append({"t": clock.t, "body": body})
    d = json.loads(answers[-1]["body"])
    tid = next(t["id"] for t in d["trains"] if t["train"] == "9901")
    assert next(t for t in d["trains"] if t["train"] == "9901")["pos_src"] == "traintime"
    h, f = tmp_path / "live.js", tmp_path / "in.json"
    h.write_text(LIVE)
    f.write_text(json.dumps({"answers": answers, "start": t0 + 1, "end": t0 + run_s, "id": tid}))
    r = subprocess.run([NODE, str(h), str(STATIC / "motion.js"), str(STATIC / "trains.js"), str(f)], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["frames"], truth


def measure(frames, truth, after=15.0):
    """Steps, stalls and error once settled (westbound: progress is -ds)."""
    fr = [(t, s) for t, s in frames if s is not None and t - frames[0][0] >= after]
    steps = [a[1] - b[1] for a, b in zip(fr, fr[1:])]
    speeds = [x / 0.1 for x in steps]
    run, stall = 0, 0
    for x in steps:
        run = run + 1 if abs(x) < 0.01 else 0
        stall = max(stall, run)
    return {"back": min(steps), "max_step": max(steps), "stall_s": stall * 0.1,
            "dv": max(abs(b - a) for a, b in zip(speeds, speeds[1:])),
            "err": max(abs(s - truth(t)) for t, s in fr)}


def test_a_train_on_traintime_glides_at_its_speed_with_no_steps_stalls_or_backward_moves(tmp_path):
    frames, truth = drive(tmp_path)
    m = measure(frames, truth)
    assert m["back"] >= 0                                   # never a step back
    assert m["stall_s"] <= 0.1                              # never stands still while it runs (one frame of rounding)
    assert m["max_step"] <= 2 * V * 0.1                     # no lunge: at most twice its speed in a frame
    assert m["dv"] <= 4.5                                   # its pace changes gently (m/s a frame; the drawn s is rounded to
                                                            # 0.1 m, ±1 m/s): no step at a fix (the old catch-up: 20)
    assert m["err"] <= 75                                   # and it is near where the train is (GPS noise ±10 m, fixes 3-16
                                                            # s old: past 8 s of age it is drawn slowing, so a little behind)


@pytest.mark.parametrize("brake_at", [15, 21, 27, 33])
def test_a_train_braking_to_a_stop_mid_track_is_drawn_where_it_stopped(tmp_path, brake_at):
    """#375: "if a train stops mid track we want to know where accurately". At 52 mph, a service brake (0.75 m/s^2) right
    after a fix, to a stop 360 m on, mid-track; the feed's fixes still 3-16 s old: drawn at most 60 m past where it
    stopped (measured: under 10 m; the old 30 s horizon at its full speed: over 300 m), no long slide back, and it
    ends where the GPS says it stopped."""
    frames, truth = drive(tmp_path, run=braking(brake_at), run_s=brake_at + 65)
    stop = truth(frames[0][0] + 10_000)
    fr = [(t, s) for t, s in frames if s is not None]
    past = max(stop - s for _, s in fr)                     # westbound: past the stop is a lower s
    back = max(b[1] - a[1] for a, b in zip(fr, fr[1:]))     # a step back (east) in one frame
    assert past <= 60
    assert back <= 3.0                                      # no slide or jump back (BACK_MAX_MPS 20: 2 m a frame)
    assert abs(fr[-1][1] - stop) <= 25                      # at rest where it stopped (GPS noise ±10 m)
    assert fr[-1][1] == fr[-20][1]                          # and still
