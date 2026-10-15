"""Aircraft between fixes (web/static/glide.js, drawn by aircraft.js; GitHub #179). The user: "we shouldnt push it
backwards unless there is a real reason to (like its over a certain distance) otherwise we should slow it down
slightly / speed it up slightly if we missed the location guess ... we should also always smooth out the radius on
the trails"; and the trail must run right up to the plane, no gap.

  converge()   a miss ahead is closed by drawing it slower (never under SLOW of its speed, never backwards), one
               behind by drawing it faster (never over FAST), one across by drifting over; only a miss over
               snapM() (0.3 nm, or 4 s of its travel when that is less) or a hard turn jumps.
  Follower     the same over a flight: fixes every 5 s whose estimate runs ahead, behind, or off to the side; the
               horizon (10 s) and the stale limit (15 s) as before; a jump for a new aircraft and between fresh
               and stale.
  line()       the reported positions up to the mark, ending on it (no gap), never running out ahead of it.
  smooth()     a centripetal Catmull-Rom curve: through every reported point, corners rounded, no wild overshoot.

The page's own code runs in node (skipped where node isn't installed; the source checks in test_aircraft.py run
everywhere)."""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

R = 6371008.8
KT = 1852 / 3600
LAT0, LON0 = 41.03, -73.68                     # over Port Chester, on the way to runway 34

LOAD = r"""
const fs = require("fs");
global.window = global;
eval(fs.readFileSync(process.argv[2], "utf8"));
eval(fs.readFileSync(process.argv[3], "utf8"));
const IN = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
const out = {};
// metres east / north of (LON0, LAT0) and back
const R = 6371008.8, DEG = Math.PI / 180;
const ll = (x, y) => [IN.lon0 + x / (R * Math.cos(IN.lat0 * DEG)) / DEG, IN.lat0 + y / R / DEG];
const m = (lon, lat) => [(lon - IN.lon0) * DEG * R * Math.cos(IN.lat0 * DEG), (lat - IN.lat0) * DEG * R];
"""


def node(tmp_path, body: str, data=None) -> dict:
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    h.write_text(LOAD + body + "\nconsole.log(JSON.stringify(out));\n")
    d.write_text(json.dumps({"lat0": LAT0, "lon0": LON0, **(data or {})}))
    r = subprocess.run([NODE, str(h), str(STATIC / "motion.js"), str(STATIC / "glide.js"), str(d)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---- one frame ---------------------------------------------------------------------------------------------------------


def test_the_miss_that_jumps_is_0_3_nm_or_4_s_of_travel(tmp_path):
    out = node(tmp_path, """
out.o = Glide.OPTS;
out.snap = [0, 15, 40, 76.1, 120, 200].map((v) => Glide.snapM(v));
""")
    o = out["o"]
    assert o["SNAP_M"] == 556 and o["SNAP_S"] == 4 and o["SLOW"] == 0.5 and o["FAST"] == 1.5 and o["CONVERGE_S"] == 3
    assert out["snap"][0] == 556                                  # not moving: eased onto its spot from within 0.3 nm
    assert out["snap"][1:] == pytest.approx([60, 160, 304.4, 480, 556])   # 4 s of its travel, at most 0.3 nm


FRAME = r"""
// a frame: the estimate a little on, the mark drawn from where it was. On `rot` at v m/s; the mark `miss` [along,
// across (+ right)] metres off the estimate; n frames of dt s. Each frame: how far the mark moved along the track and
// across it.
function run(v, miss, n, dt, rot = 90) {
  const h = rot * DEG, ux = Math.sin(h), uy = Math.cos(h);
  const est = (t) => { const [lon, lat] = ll(v * t * ux, v * t * uy); return { lon, lat, rot, v }; };
  let [lon, lat] = ll(miss[0] * ux + miss[1] * uy, miss[0] * uy - miss[1] * ux);
  let prev = { lon, lat, rot }, steps = [], snaps = 0, t = 0;
  for (let i = 0; i < n; i++) {
    const p = Glide.converge(prev, est(t), est(t + dt), dt);
    const [x0, y0] = m(prev.lon, prev.lat), [x1, y1] = m(p.lon, p.lat);
    steps.push([(x1 - x0) * ux + (y1 - y0) * uy, (x1 - x0) * uy - (y1 - y0) * ux]);
    if (p.snap) snaps++;
    prev = p; t += dt;
  }
  const [xe, ye] = m(prev.lon, prev.lat), e = est(t), [xt, yt] = m(e.lon, e.lat);
  return { steps, snaps, left: [(xe - xt) * ux + (ye - yt) * uy, (xe - xt) * uy - (ye - yt) * ux] };
}
"""


def test_ahead_of_the_estimate_it_is_drawn_slower_never_backwards(tmp_path):
    v = 148 * KT                                                   # N72LL inbound at 148 kt
    out = node(tmp_path, FRAME + """
out.ahead = run(IN.v, [150, 0], 250, 0.04);          // 150 m ahead (2 s of its travel), 10 s of frames
out.little = run(IN.v, [20, 0], 100, 0.04);
""", {"v": v})
    a = out["ahead"]
    assert a["snaps"] == 0
    speeds = [s[0] / 0.04 for s in a["steps"]]
    assert min(speeds) >= 0.5 * v - 1e-6                           # never under half its speed: never backwards
    assert max(speeds) <= v + 1e-6                                 # and never faster while it is ahead
    assert speeds[0] < 0.75 * v                                    # slowed at once
    assert abs(a["left"][0]) < 10                                  # caught up within 10 s
    third = [s[0] / 0.04 for s in out["little"]["steps"]][0]
    assert 0.9 * v < third < v                                     # a small miss: only slightly slower


def test_behind_the_estimate_it_is_drawn_faster_but_not_much(tmp_path):
    v = 148 * KT
    out = node(tmp_path, FRAME + "out.behind = run(IN.v, [-150, 0], 250, 0.04);", {"v": v})
    b = out["behind"]
    speeds = [s[0] / 0.04 for s in b["steps"]]
    assert b["snaps"] == 0 and min(speeds) >= v - 1e-6 and max(speeds) <= 1.5 * v + 1e-6
    assert abs(b["left"][0]) < 10


def test_off_to_the_side_it_drifts_over_smoothly(tmp_path):
    v = 120 * KT
    out = node(tmp_path, FRAME + "out.side = run(IN.v, [0, 80], 150, 0.04, 330);", {"v": v})
    s = out["side"]
    across = [st[1] for st in s["steps"]]
    assert s["snaps"] == 0
    assert all(abs(c) < 80 * 0.04 / 3 * 1.01 for c in across)     # no faster than the miss / CONVERGE_S
    assert all(st[0] / 0.04 == pytest.approx(v, rel=1e-3) for st in s["steps"])   # its speed along the track kept
    assert abs(s["left"][1]) < 80 * math.exp(-6 / 3) * 1.05         # about e^-2 of it left after 6 s


def test_a_real_reason_jumps(tmp_path):
    v = 148 * KT
    out = node(tmp_path, FRAME + """
out.far = run(IN.v, [320, 0], 1, 0.04);              // over 4 s of its travel (304 m) at 148 kt
out.near = run(IN.v, [290, 0], 1, 0.04);
out.slow = run(25, [120, 0], 1, 0.04);              // 4 s at 25 m/s is 100 m
out.jet = run(150, [570, 0], 1, 0.04);              // over 0.3 nm, though under 4 s of a jet's travel
// a heading off by more than 45 degrees (a hard turn between fixes): jumps
const est = (rot) => ({ lon: IN.lon0, lat: IN.lat0, rot, v: 50 });
out.turn = Glide.converge({ lon: IN.lon0, lat: IN.lat0, rot: 0 }, est(60), est(60), 0.04).snap;
out.bend = Glide.converge({ lon: IN.lon0, lat: IN.lat0, rot: 0 }, est(30), est(30), 0.04).snap;
""", {"v": v})
    assert out["far"]["snaps"] == 1 and out["far"]["left"] == pytest.approx([0, 0], abs=1e-6)
    assert out["near"]["snaps"] == 0
    assert out["slow"]["snaps"] == 1 and out["jet"]["snaps"] == 1
    assert out["turn"] is True and out["bend"] is False


# ---- over a flight: fixes every 5 s ------------------------------------------------------------------------------------

FLIGHT = r"""
// A straight approach, heading `rot`, reporting v m/s but really flying `slow` of that (slowing for the runway).
// Fixes every `every` s, each `late` s old when it arrives; the page thinks each is `bias` s older than it is (so
// its estimate runs bias * v ahead: what the user saw), or by turns each of `biases` (a slow answer, then a quick
// one). Frames every 40 ms. Returns, each frame, the step along / across the track, and how far the mark is from
// the truth.
function flight({ v, rot = 340, every = 5, late = 1.5, bias = 1.2, biases = null, slow = 1, secs = 40, gap = null, side = 0 }) {
  const h = rot * DEG, ux = Math.sin(h), uy = Math.cos(h);
  const truth = (t) => ll(slow * v * t * ux + side * uy, slow * v * t * uy - side * ux);
  const f = Glide.follower(Motion.arc, { horizonS: 10, staleS: 15 });
  const out = { steps: [], miss: [], snaps: [], est: [], stale: [] };
  let last = null, fixT = -1e9;
  for (let ms = 0; ms <= secs * 1000; ms += 40) {
    const t = ms / 1000;
    // a fix taken at k*every, arriving `late` s later (none inside the gap)
    const k = Math.floor((t - late) / every);
    if (k >= 0 && k * every !== fixT && !(gap && k * every >= gap[0] && k * every < gap[1])) {
      fixT = k * every;
      const [lon, lat] = truth(fixT);
      const b = biases ? biases[k % biases.length] : bias;
      f.set("a", { lon, lat, rot, at: (fixT - b) * 1000, gs: v / Glide.KT, trk: rot, tr: 0, ground: false });
    }
    if (fixT < 0) continue;
    out.t = out.t || [];
    out.t.push(t);
    const p = f.where("a", ms);
    const [x, y] = m(p.lon, p.lat);
    if (last) out.steps.push([(x - last[0]) * ux + (y - last[1]) * uy, (x - last[0]) * uy - (y - last[1]) * ux]);
    const [tx, ty] = m(...truth(t));
    out.miss.push(Math.hypot(x - tx, y - ty));
    out.snaps.push(f.items.get("a").snaps);
    out.est.push(f.estimated("a"));
    out.stale.push(f.stale("a"));
    last = [x, y];
  }
  return out;
}
"""


def test_an_estimate_running_ahead_never_pulls_the_plane_back(tmp_path):
    """N72LL on its way in at 148 kt, fixes every 5 s, the page's estimate ahead of each new fix by up to 1.5 s of
    its travel (110 m): the answers' lag varies, and it is slowing for the runway. The mark is slowed onto each fix
    and never steps back (before #179 it was eased back over a second at every fix)."""
    v = 148 * KT
    out = node(tmp_path, FLIGHT + """
out.steady = flight({ v: IN.v });
out.jitter = flight({ v: IN.v, biases: [1.5, 0, 1.2, 0.2, 1.5, 0.3], slow: 0.93, secs: 60 });
""", {"v": v})
    for name in ("steady", "jitter"):
        f = out[name]
        along = [s[0] for s in f["steps"]]
        assert f["snaps"][-1] == 1, name                           # only the first sight of it: no jumps
        assert min(along) >= 0.5 * v * 0.04 - 1e-6, name           # always forward, at least half its speed
        assert max(along) <= 1.5 * v * 0.04 + 1e-6, name           # at most half as fast again
        assert all(abs(s[1]) < 1e-3 for s in f["steps"]), name     # (on its track: nothing across)
    jitter = [s[0] / 0.04 / v for s in out["jitter"]["steps"]]
    assert min(jitter) < 0.8 and max(jitter) > 1.05               # it really was slowed down and sped up
    assert max(out["jitter"]["miss"]) < 2.5 * v                     # and kept close to where it really was
    assert max(out["steady"]["miss"][-50:]) < 1.2 * v * 1.15       # a steady lag: drawn where the estimate says


def test_behind_and_beside_it_catches_up_without_a_jump(tmp_path):
    v = 120 * KT
    out = node(tmp_path, FLIGHT + """
out.behind = flight({ v: IN.v, bias: -1.5 });
out.side = flight({ v: IN.v, bias: 0, side: 60, secs: 20 });
""", {"v": v})
    b = out["behind"]
    assert b["snaps"][-1] == 1 and min(s[0] for s in b["steps"]) >= v * 0.04 * 0.5
    assert max(s[0] for s in b["steps"]) <= 1.5 * v * 0.04 + 1e-6


def test_no_fix_for_a_while_holds_at_the_horizon_then_goes_hollow_at_its_fix(tmp_path):
    """The limits are the old ones: never more than 10 s past a fix, not moved at all once it is 15 s old."""
    v = 148 * KT
    out = node(tmp_path, FLIGHT + "out.f = flight({ v: IN.v, bias: 0, late: 1, gap: [10, 40], secs: 45 });", {"v": v})["f"]
    # last fix taken at 5 s, arriving at 6 s; 10 s past it (15 s) the mark stops; 15 s past it (20 s) it is stale
    i = lambda t: min(range(len(out["t"])), key=lambda j: abs(out["t"][j] - t))
    step = lambda t: out["steps"][i(t) - 1]                       # the step into the frame at t
    assert all(step(t / 10)[0] > 0 for t in range(70, 149))        # moving on, from the fix's own time
    assert all(abs(step(t / 10)[0]) < 1e-3 for t in range(152, 200))   # held at the horizon (10 s past its fix) ...
    assert out["est"][i(19.9)]                                     # ... and still called an estimate there
    assert not out["stale"][i(19.9)] and out["stale"][i(20.1)]
    assert out["snaps"][i(20.1)] == out["snaps"][i(19.9)] + 1      # gone stale: drawn where it really was, at once
    assert not out["est"][i(21)]                                   # ... and not called an estimate there
    assert out["miss"][i(21)] > 14 * v                             # (the truth has flown on)
    # a fresh fix again (taken at 40 s, in at 41 s): drawn there at once, not chased
    assert out["snaps"][i(41.1)] == out["snaps"][i(20.1)] + 1 and out["miss"][i(41.1)] < 2 * v


# ---- the trail ---------------------------------------------------------------------------------------------------------


def test_the_trail_runs_up_to_the_mark_and_never_out_ahead_of_it(tmp_path):
    out = node(tmp_path, """
const P = (x, y) => ll(x, y);
const rep = [P(0, 0), P(0, 60), P(0, 120), P(0, 180)];             // north, a point every 60 m
const mk = (y, x = 0) => { const [lon, lat] = ll(x, y); return { lon, lat, rot: 0 }; };
out.ahead = Glide.line(rep, mk(250)).map(([lon, lat]) => m(lon, lat));      // drawn on past its fix: joined to it
out.behind = Glide.line(rep, mk(100)).map(([lon, lat]) => m(lon, lat));     // still catching up: newest wait
out.beside = Glide.line(rep, mk(200, 30)).map(([lon, lat]) => m(lon, lat));
out.dup = Glide.line([...rep, P(0, 180.3)], mk(180)).map(([lon, lat]) => m(lon, lat));
out.uturn = Glide.line([P(0, 0), P(0, 600), P(300, 600), P(300, 0)], { ...mk(-60, 300), rot: 180 }).length;
""")
    ys = lambda pts: [round(p[1]) for p in pts]
    assert ys(out["ahead"]) == [0, 60, 120, 180, 250]             # ends on the mark: no gap
    assert ys(out["behind"]) == [0, 60, 100]                       # 120 and 180 not reached yet
    assert ys(out["beside"])[-2:] == [180, 200] and round(out["beside"][-1][0]) == 30
    assert ys(out["dup"]) == [0, 60, 120, 180]
    assert out["uturn"] == 5                                       # the older leg ahead of it is kept: only the newest wait


def test_the_trail_is_a_curve_with_its_corners_rounded_inside_never_overshooting(tmp_path):
    out = node(tmp_path, """
const P = (x, y) => ll(x, y);
const corner = [P(0, 0), P(0, 300), P(0, 600), P(300, 600), P(600, 600)];     // a 90 degree turn at (0, 600)
const zig = [P(0, 0), P(60, 60), P(0, 120), P(60, 180), P(0, 240)];             // MLAT-like zigzag
const line = [P(0, 0), P(0, 100), P(0, 200), P(0, 300)];
const turn = [];                                                                 // a standard-rate turn at 148 kt:
for (let k = 0; k <= 8; k++) {                                                   // a fix every 5 s, 15 degrees apart
  const a = k * 15 * DEG, r = 1450;
  turn.push(P(r - r * Math.cos(a), r * Math.sin(a)));
}
const wide = [P(0, 0), P(0, 2000), P(2000, 2000)];                              // long legs: the cut is capped
const xy = (pts) => pts.map(([lon, lat]) => m(lon, lat));
out.corner = xy(Glide.smooth(corner));
out.zig = xy(Glide.smooth(zig));
out.line = xy(Glide.smooth(line));
out.turn = xy(Glide.smooth(turn));
out.wide = xy(Glide.smooth(wide));
out.two = Glide.smooth([P(0, 0), P(0, 50)]).length;
out.inputs = { corner: xy(corner), zig: xy(zig), turn: xy(turn), wide: xy(wide) };
""")

    def seg(p, a, b):
        ax, ay, bx, by = *a, *b
        L = (bx - ax) ** 2 + (by - ay) ** 2
        u = max(0, min(1, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L)) if L else 0
        return math.dist(p, (ax + u * (bx - ax), ay + u * (by - ay)))

    def near(p, curve):                                            # from a point to the curve (a polyline)
        return min(seg(p, curve[i], curve[i + 1]) for i in range(len(curve) - 1))

    ins = out["inputs"]
    for name in ("corner", "zig", "turn", "wide"):
        c, pts = out[name], ins[name]
        assert math.dist(c[0], pts[0]) < 0.01 and math.dist(c[-1], pts[-1]) < 0.01   # its ends exact: up to the mark
        assert len(c) <= 8 * len(pts) + 2
    assert all(abs(x) < 1e-6 for x, _ in out["line"]) and out["two"] == 2  # a straight trail stays straight
    ys = [y for _, y in out["line"]]
    assert ys == sorted(ys)                                         # and never doubles back
    corner = out["corner"]
    assert all(x > -1e-6 and y < 600 + 1e-6 for x, y in corner)   # never out past the corner ...
    assert any(x > 0.5 and y < 599.5 for x, y in corner)           # ... but rounded inside it
    assert 10 < near((0, 600), corner) < 0.25 * 300                # a radius, not a hard angle, not a shortcut
    assert all(0 - 1e-6 <= x <= 60 + 1e-6 for x, _ in out["zig"])  # a zigzag is calmed inside its own swing
    # a real turn (15 degree corners): the curve stays within 13 m of every reported fix, and no further from the
    # turn the aircraft really flew than the straight legs between its fixes were (they cut 12 m inside it)
    assert max(near(p, out["turn"]) for p in ins["turn"]) < 13
    off_arc = lambda pts: max(abs(math.dist(p, (1450, 0)) - 1450) for p in pts)
    legs = [(a[0] + (b[0] - a[0]) * u / 20, a[1] + (b[1] - a[1]) * u / 20) for a, b in zip(ins["turn"], ins["turn"][1:]) for u in range(21)]
    assert off_arc(out["turn"]) <= off_arc(legs)
    # long legs: a corner is cut at most 100 m along each, so the rounding stays close to the fix
    assert near((0, 2000), out["wide"]) < 100
