"""A train on MTA TrainTime moves smoothly on the map (#375): the hub (sizeup/trains.py Trains, its TrainTime poller,
Tracker and along_filter) answers every 5 s as it does live, and the page's own trains.js + motion.js (in node, a
map stand-in, the page's 5 s poll) draw it every 100 ms frame. The feed has the shape measured from the real API on
2026-11-09 (24 polls, 9 New Haven Line trains): a GPS fix every 3 s, each 3 to 16 s old when read, with GPS noise;
the train runs at a steady 52 mph, or brakes to a stop mid-track. Before #375 the page carried a fix on for only 10 s
past its time (TT_HORIZON_S), so the train stood still for seconds between fixes and then lunged on at up to 140 m/s;
then for 30 s at its full speed, so a train that stopped mid-track was drawn hundreds of metres past where it stood.
#375 slowed a fix from 8 s of its age, so a steady train went quick, slow and hollow by turns (#390). Now: on at its
speed, slowed only on evidence (a confirmed fall in speed, its next stop, a fix over 30 s old), its drawn pace within
its consist's limits. The measured cadence (a fix every 3 to 12 s) is here too (cadence="real": synthetic numbers in
its shape).

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
    out.frames.push([now / 1000, w ? w.s : null, w ? !!w.old : null, w ? !!w.snap : null, w ? w.op : null]);
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


# The measured cadence (#375, #390; synthetic numbers in its shape): a new GPS fix every 3 to 12 s (median 5), published
# 0 to 8 s late; with the hub's and the page's 5 s reads, 3 to 16 s old when the page has it (median ~6).
GAPS_S = (5, 3, 8, 4, 12, 5, 6, 3, 10, 5, 4, 7, 5, 6, 12, 4, 5, 8, 3, 6)
PUB_S = (2, 0, 4, 1, 8, 3, 1, 6, 2, 3, 0, 4, 2, 1, 6, 3, 2, 8, 1, 3)


def drive(tmp_path, run=steady, run_s=RUN_S, cadence="every3", start_m=0.0, bad=(), quiet=None):
    """(frames [[t, drawn s, hollow, ringed]], truth(t)): the hub's answers over run_s, drawn by the page. run(dt) ->
    (progress, speed). cadence "every3": a fix every GPS_EVERY_S, LATE_S late; "real": GAPS_S / PUB_S. start_m: that
    much further east (more line before Rye). bad: fix numbers whose speed reads 0 (one bad reading). quiet (from, to):
    no fix taken in that span (s into the run)."""
    tr, clock, fetch = provider(tmp_path)
    model = Model(parse_static(GTFS), tracks=json.loads(T.TRACKS_FILE.read_text()))
    recs = json.loads(LOC)
    mover = next(r for r in recs if r["train_num"] == "9901")          # westbound: s falls
    _, s0 = model.line.project(mover["location"]["latitude"], mover["location"]["longitude"])
    s0 += start_m
    t0, rnd = clock.t, random.Random(375)
    truth = lambda t: s0 - run(t - t0)[0]
    if cadence == "real":
        sched, t, k = [], t0, 0
        while t < t0 + run_s:
            if not (quiet and quiet[0] <= t - t0 < quiet[1]):
                sched.append((t, t + PUB_S[k % len(PUB_S)]))
            t += GAPS_S[k % len(GAPS_S)]
            k += 1
    else:
        sched = [(t0 + GPS_EVERY_S * k, t0 + GPS_EVERY_S * k + LATE_S[k % len(LATE_S)]) for k in range(run_s // GPS_EVERY_S + 1)]
    noise = [rnd.gauss(0, NOISE_M) for _ in sched]

    def feed(now):                                                    # the newest GPS fix published by now
        k = max([i for i, (_, pub) in enumerate(sched) if pub <= now] or [0])
        ts = sched[k][0]
        lat, lon, _ = model.line.at(truth(ts) + noise[k])
        loc = {**mover["location"], "latitude": round(lat, 6), "longitude": round(lon, 6), "timestamp": int(ts),
               "speed": 0.0 if k in bad else round(run(ts - t0)[1] * 3600 / 1609.344, 1)}
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
    fr = [(t, s) for t, s, *_ in frames if s is not None and t - frames[0][0] >= after]
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


def glides_back(frames):
    """Each run of frames moving back (east) more than 0.05 m a frame, faded frames left out: {s (how long), m (how
    far), peak (m/s), ringed}"""
    fr = [(t, s, snap) for t, s, _, snap, op in frames if s is not None and (op is None or op >= 1)]
    runs, cur = [], None
    for a, b in zip(fr, fr[1:]):
        back = b[1] - a[1]
        if back > 0.05 and b[0] - a[0] < 0.15:
            cur = cur or {"t0": a[0], "m": 0.0, "peak": 0.0, "ringed": True}
            cur.update(t1=b[0], m=cur["m"] + back, peak=max(cur["peak"], back / (b[0] - a[0])), ringed=cur.get("last", True),
                       last=cur["ringed"] and b[2])   # (the glide's last frame is already off it)
        elif cur:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    return [{"s": r["t1"] - r["t0"], "m": r["m"], "peak": r["peak"], "ringed": r["ringed"]} for r in runs]


def smooth_back(frames, v=V):
    """#390 ("less surgy"): every move back is a glide, never a jump, at most GLIDE_PEAK_MPS or 1.5x the train's speed
    (it takes as long as that needs, to GLIDE_MAX_S), ringed when it is more than a few metres; a correction too big for
    that is faded out and in instead (op), the only place the drawn train moves more than a glide's pace in a frame"""
    peak = 25.0
    for g in glides_back(frames):
        assert g["s"] <= 25.5 and g["peak"] <= peak * 1.02 + 0.5
        assert g["m"] <= 3.0 or g["ringed"]
    fr = [(t, s, op) for t, s, _, _, op in frames if s is not None]
    for a, b in zip(fr, fr[1:]):
        if not ((a[2] is not None and a[2] < 1) or (b[2] is not None and b[2] < 1)):
            assert abs(b[1] - a[1]) <= max(peak, 2 * v) * 0.1 * 1.05 + 1.0          # no jump outside a fade


@pytest.mark.parametrize("brake_at", [15, 21, 27, 33])
def test_a_train_braking_to_a_stop_mid_track_is_drawn_where_it_stopped(tmp_path, brake_at):
    """#375: "if a train stops mid track we want to know where accurately". At 52 mph, a service brake (0.75 m/s^2) right
    after a fix, to a stop 360 m on, mid-track; the feed's fixes still 3-16 s old: it ends where the GPS says it stopped.
    #390: until a fix says it is braking it is drawn on at its speed, so by then it may be drawn ahead of the train (the
    user: "fine with an overshoot if we keep it smooth"): braked hard (to 4 m/s^2, eased in) as soon as the data says
    it stops behind, then glided back (at most 25 m/s, 3 m/s^2), faintly ringed."""
    frames, truth = drive(tmp_path, run=braking(brake_at), run_s=brake_at + 65)
    stop = truth(frames[0][0] + 10_000)
    fr = [(t, s) for t, s, *_ in frames if s is not None]
    past = max(stop - s for _, s in fr)                     # westbound: past the stop is a lower s
    assert past <= 250                                      # (#390: on past it, braked hard but smoothly)
    smooth_back(frames)                                     # glided back, never put back at once (#390)
    assert len([g for g in glides_back(frames) if g["m"] > 10]) <= 2
    assert abs(fr[-1][1] - stop) <= 25                      # at rest where it stopped (GPS noise ±10 m)
    assert fr[-1][1] == fr[-20][1]                          # and still


# ---- #390: the measured cadence (a fix every 3-12 s, 3-16 s old when read) -------------------------------------------

V45 = 45 * 1609.344 / 3600


def speeds_1s(frames, t_from, t_to):
    fr = [(t, s) for t, s, *_ in frames if s is not None and t_from <= t <= t_to]
    return [(fr[i][1] - fr[i + 10][1]) / (fr[i + 10][0] - fr[i][0]) for i in range(len(fr) - 10)]   # westbound


def test_a_steady_train_is_drawn_steadily_at_the_measured_cadence(tmp_path):
    """#390 (the user, at a 45 mph train: "its going quick, decelerating going hollow then picking speed back up and
    repeat"): a steady 45 mph train, fixes at TrainTime's measured cadence with ±10 m of GPS noise, is drawn at a steady
    pace: its speed over each second within ±15 % of the train's (corrections absorbed in that band), a small spread,
    never hollow, near the train. (#378's soft horizon here: 0 to 110 mph, CoV 0.45-0.69, hollow 6-12 % of the time.)"""
    frames, truth = drive(tmp_path, run=lambda dt: (V45 * dt, V45), run_s=105, cadence="real", start_m=1000.0)
    t0 = frames[0][0] - 1
    v = speeds_1s(frames, t0 + 20, t0 + 104)
    mean = sum(v) / len(v)
    cov = (sum((x - mean) ** 2 for x in v) / len(v)) ** 0.5 / mean
    assert cov <= 0.07
    assert min(v) >= 0.85 * V45 - 0.5 and max(v) <= 1.15 * V45 + 0.5
    assert not any(old for t, s, old, *_ in frames if t > t0 + 5)                     # never hollow
    assert max(abs(s - truth(t)) for t, s, *_ in frames if s is not None and t > t0 + 20) <= 30
    assert not [g for g in glides_back(frames) if g["m"] > 3]


def test_one_bad_speed_reading_is_no_stop(tmp_path):
    """#379: one fix whose speed reads 0 while the train runs on at 45 mph (the distance moved says otherwise): set aside
    (trains.speed_check), not braked on; the drawn train keeps its pace."""
    frames, truth = drive(tmp_path, run=lambda dt: (V45 * dt, V45), run_s=90, cadence="real", start_m=1000.0, bad={6})
    t0 = frames[0][0] - 1
    v = speeds_1s(frames, t0 + 20, t0 + 89)
    assert min(v) >= 0.8 * V45 and not [g for g in glides_back(frames) if g["m"] > 3]
    assert max(abs(s - truth(t)) for t, s, *_ in frames if s is not None and t > t0 + 20) <= 30


def test_a_stop_with_no_sign_of_it_before_the_feed_goes_quiet(tmp_path):
    """#390, the trade-off, measured: a 45 mph train brakes (0.75 m/s^2) right after a moving fix and no fix comes until
    it has stopped. Nothing says it is slowing, so it is drawn on at its speed until its fix is RAIL_CRUISE_S (20 s)
    old, then eased to a stop at its service brake (it was drawn slowing on age alone before: within ~60 m, at the cost
    of every steady train); then the stopped fix glides it back where it stopped (at most 25 m/s, 3 m/s^2), not a fade."""
    brake = 40
    frames, truth = drive(tmp_path, run=braking_v(V45, brake),
                          run_s=brake + 80, cadence="real", start_m=1000.0, quiet=(brake - 2, brake + V45 / 0.75 + 1))
    stop = truth(frames[0][0] + 10_000)
    fr = [(t, s) for t, s, *_ in frames if s is not None]
    past = max(stop - s for _, s in fr)
    assert 100 <= past <= 300                                # (45 mph: ~230 m; see #392)
    smooth_back(frames, V45)
    glided = [g for g in glides_back(frames) if g["m"] > 10]
    assert 1 <= len(glided) <= 2 and not any(op is not None and op < 1 for *_, op in frames)   # glided back, not faded
    assert max(g["peak"] for g in glided) <= 25.5
    assert abs(fr[-1][1] - stop) <= 25 and fr[-1][1] == fr[-20][1]


def braking_v(v, at_s, b=0.75):
    def go(dt):
        if dt <= at_s:
            return v * dt, v
        u = min(dt - at_s, v / b)
        return v * at_s + v * u - 0.5 * b * u * u, max(0.0, v - b * u)
    return go


def test_a_feed_gone_quiet_eases_the_train_to_a_stop_and_it_is_drawn_hollow_after_30_s(tmp_path):
    """#390: no fix for 50 s at 45 mph: on at its speed while the last fix is a normal age, eased to a stop from 20 s
    (a little faded), hollow past 30 s; when fixes come again it glides on to them, never jumps."""
    frames, truth = drive(tmp_path, run=lambda dt: (V45 * dt, V45), run_s=125, cadence="real", start_m=1000.0, quiet=(40, 90))
    t0 = frames[0][0] - 1
    hollow = [t - t0 for t, s, old, *_ in frames if old]
    assert hollow and min(hollow) > 40 + 25                  # (its last fix was taken before 40 s; hollow past 30 s)
    v = speeds_1s(frames, t0 + 20, t0 + 124)
    assert min(v[:200]) >= 0.85 * V45 - 0.5                  # 20-40 s: steady
    smooth_back(frames, V45)                                 # no jump outside a fade
    stillest = min(v[300:700])
    assert stillest < 0.5                                    # eased to a stop while nothing came
    at = lambda T_: min(((t, s) for t, s, *_ in frames if s is not None), key=lambda x: abs(x[0] - T_))
    assert abs(at(t0 + 110)[1] - truth(at(t0 + 110)[0])) <= 30            # and back with the train (before it brakes for Rye)


def test_a_train_stopping_at_its_station_is_braked_into_it(tmp_path):
    """#390: a 45 mph train braking (0.8 m/s^2) into its next stop (Rye): the prediction brakes into the stop's mark as a
    train does, so it is drawn near the train all the way in and comes to rest at the stop."""
    tr, clock, fetch = provider(tmp_path / "probe")
    tr.get("/api/cad/trains", "")
    tr.tick()
    tr.tt.tick()
    nxt = next(t for t in json.loads(tr.get("/api/cad/trains", "")[1])["trains"] if t["train"] == "9901")["next"]
    model = Model(parse_static(GTFS), tracks=json.loads(T.TRACKS_FILE.read_text()))
    mover = next(r for r in json.loads(LOC) if r["train_num"] == "9901")
    _, s0 = model.line.project(mover["location"]["latitude"], mover["location"]["longitude"])
    dist = s0 + 1000.0 - nxt["s"]
    b = 0.8
    tb = (dist - V45 * V45 / (2 * b)) / V45
    frames, truth = drive(tmp_path, run=braking_v(V45, tb, b), run_s=int(tb + V45 / b + 30), cadence="real", start_m=1000.0)
    t0 = frames[0][0] - 1
    stop = truth(t0 + 10_000)
    fr = [(t, s) for t, s, *_ in frames if s is not None]
    assert max(abs(s - truth(t)) for t, s in fr if t > t0 + 20) <= 30
    assert max(stop - s for _, s in fr) <= 25 and not [g for g in glides_back(frames) if g["m"] > 3]
    assert abs(fr[-1][1] - stop) <= 25 and fr[-1][1] == fr[-20][1]


def test_a_held_train_is_not_moved(tmp_path):
    """A train standing (speed 0, GPS noise): drawn where it stands, never moved by the noise."""
    frames, truth = drive(tmp_path, run=lambda dt: (0.0, 0.0), run_s=90, cadence="real", start_m=1000.0)
    ss = [s for t, s, *_ in frames if s is not None and t - frames[0][0] > 15]
    assert max(ss) - min(ss) <= 1.0


@pytest.mark.parametrize("mph, gap_s", [(45, 20), (60, 20), (60, 35)])
def test_never_drawn_much_faster_than_the_train(tmp_path, mph, gap_s):
    """#390 re-review ("less surgy"): fixes stop for gap_s at a steady speed, then come again; whatever closes the gap
    (the band, a glide, or a fade), the drawn train is never faster than 1.25 v + 2 (1 s windows; a fade's jump aside)."""
    v = mph * 1609.344 / 3600
    frames, truth = drive(tmp_path, run=lambda dt: (v * dt, v), run_s=90, cadence="real", start_m=1000.0,
                          quiet=(30, 30 + gap_s))
    fr = [(t, s) for t, s, _, _, op in frames if s is not None and (op is None or op >= 1)]
    speeds = [(fr[i][1] - fr[i + 10][1]) / (fr[i + 10][0] - fr[i][0]) for i in range(len(fr) - 10) if fr[i + 10][0] - fr[i][0] < 1.05]
    assert max(speeds) <= 1.25 * v + 2 + 0.3


@pytest.mark.parametrize("brake_at, b", [(a, b) for a in (31, 34, 37, 40, 44, 47, 50, 53) for b in (0.75, 1.4)])
def test_a_stop_seen_in_a_later_fix_glides_back_never_fades(tmp_path, brake_at, b):
    """#392 final check: braking at a service or emergency rate after a moving fix, fixes at the measured cadence: drawn
    ahead, braked hard, glided back; a new fix that moves the target mid-glide is taken into it, never a fade or a jump
    (7 of 48 such runs faded on a re-plan)."""
    frames, truth = drive(tmp_path, run=braking_v(V45, brake_at, b), run_s=int(brake_at + V45 / b + 45), cadence="real",
                          start_m=1000.0)
    assert not any(op is not None and op < 1 for *_, op in frames)
    smooth_back(frames, V45)
