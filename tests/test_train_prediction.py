"""Train prediction against what the feed says (the admin, 2026-11-15: a train "barely moving" drawn at 40+ mph with
"Stopped · 47 mph" on its card; "do we log how far ahead/behind the predictions are?"; "we probably need a snap-back").

  - GTFS-realtime's stop statuses lag the train: a TrainTime GPS position past the stop that feed still has it at is
    taken (not refused "beyond its next stop"), its next stop is the one ahead, and its own speed says whether it is
    stopped (never "Stopped" at 47 mph).
  - A speed reading after a long gap stands (not held at the last one).
  - The residuals: the page's prediction (motion.js rail / gpsRun, ported as trains.page_predict / gps_run, checked
    against the page's own code in node) against each new fix, signed (+ ahead), summarised in a periodic log line.
  - The page's snap-back and its Snap (motion.js snapToFix).

Synthetic trains on the recorded static feed (tests/fixtures/mta)."""

import json
import math

import pytest

from sizeup import trains as T
from sizeup.trains import Model, parse_static
from tests.test_train_motion import NODE, node, TRACK
from tests.test_trains import GTFS, NOW, train_at


@pytest.fixture(scope="module")
def model():
    return Model(parse_static(GTFS), tracks=json.loads(T.TRACKS_FILE.read_text()))


def tt_train(model, s, stops, speed, ts, vstatus="STOPPED_AT", vstop="", n="9960"):
    """A train (train_at) whose position is a TrainTime GPS fix at s, speed m/s, taken at ts."""
    t = train_at(model, s, stops, vstatus=vstatus, vstop=vstop, n=n)
    lat, lon, _ = model.line.at(s)
    t["pos"] = {"lat": round(lat, 6), "lon": round(lon, 6), "bearing": None, "ts": ts, "src": "traintime", "loc": "GPS",
                "speed": speed}
    return t


def inbound(model):
    """Westbound (s falling): left Greenwich, GTFS-realtime still says STOPPED_AT Port Chester, Rye next."""
    st = {s["name"]: s for s in model.area_stations}
    stops = [(st["Greenwich"]["id"], "Departed", NOW - 300), (st["Port Chester"]["id"], "On-Time", NOW - 60),
             (st["Rye"]["id"], "On-Time", NOW + 120), (st["Harrison"]["id"], "On-Time", NOW + 300)]
    return st, stops


# ---- the hub: GTFS-realtime lagging a GPS fix ------------------------------------------------------------------------


def test_a_gps_fix_past_the_stop_gtfs_realtime_still_has_it_at_is_taken(model):
    st, stops = inbound(model)
    pc, rye = st["Port Chester"]["ch"], st["Rye"]["ch"]
    tk = T.Tracker()
    for k, ds in enumerate((600.0, 700.0)):                    # 600 then 700 m past Port Chester, 21 m/s (47 mph)
        t = tt_train(model, pc - ds, stops, 21.0, NOW + 5 * k, vstop=st["Port Chester"]["id"])
        tk.update(model, {"timestamp": NOW + 5 * k, "tt_at": NOW + 5 * k, "trains": {t["id"]: t}}, NOW + 5 * k)
    assert tk.rejected == 0 and tk.fix(t["id"])["ok"]          # before: refused, "beyond its next stop"
    [x] = model.trains({"timestamp": NOW + 5, "trains": {t["id"]: t}}, NOW + 5, tk)
    assert x["mode"] == "reported"                             # its own speed says: not "Stopped" at 47 mph
    assert x["next"]["name"] == "Rye" and (x["hold_s"] - rye) * -1 <= 0.1    # on to Rye, never past it
    assert x["segment"] is not None and x["segment"]["to"] == st["Rye"]["id"]
    assert x["v_fix"] == pytest.approx(21.0, abs=1.0)


def test_a_gps_fix_far_past_its_next_stop_is_still_refused(model):
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    t = tt_train(model, pc - T.GPS_PAST_STOP_M - 500, stops, 21.0, NOW, vstop=st["Port Chester"]["id"])
    tk = T.Tracker()
    tk.update(model, {"timestamp": NOW, "tt_at": NOW, "trains": {t["id"]: t}}, NOW)
    assert tk.why == {"beyond its next stop": 1}               # another train's record, not a lagging feed


def test_a_gps_fix_standing_at_its_stop_is_stopped_and_one_running_through_it_is_not(model):
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    for speed, mode in ((0.0, "stopped"), (21.0, "reported")):
        t = tt_train(model, pc - 30, stops, speed, NOW, vstop=st["Port Chester"]["id"], n=f"99{int(speed)}")
        tk = T.Tracker()
        tk.update(model, {"timestamp": NOW, "tt_at": NOW, "trains": {t["id"]: t}}, NOW)
        [x] = model.trains({"timestamp": NOW, "trains": {t["id"]: t}}, NOW, tk)
        assert x["mode"] == mode


def test_a_speed_reading_after_a_long_gap_stands():
    last = {"s_raw": 40_000.0, "fix_at": NOW, "vf": 21.0, "gps": True, "a": 0.0}
    # 60 s on, it moved as if at 21 m/s throughout (it slowed only lately): the new 1.5 m/s reading is the speed now
    chk = T.speed_check(last, 40_000.0 + 21.0 * 60, 1, NOW + 60, 1.5, True)
    assert chk["vf"] == 1.5 and chk["bad"] == ""
    chk = T.speed_check(last, 40_000.0 + 21.0 * 5, 1, NOW + 5, 1.5, True)   # 5 s on, the same: a bad reading, held
    assert chk["vf"] == 21.0 and chk["bad"] == "speed"


# ---- the residuals ---------------------------------------------------------------------------------------------------

CASES = [  # (v, a, tau, room, dyn)
    (20.0, 0.0, 5.0, math.inf, None), (20.0, 0.0, 30.0, math.inf, None), (20.0, -0.75, 12.0, math.inf, None),
    (20.0, 0.0, 15.0, 300.0, None), (30.0, 0.0, 40.0, 120.0, None), (12.0, -0.3, 60.0, 900.0, T.consist("MNR_DIESEL", 7)),
    (5.0, 0.0, 25.0, math.inf, T.consist("M8", 8)),
]


def test_the_hubs_port_of_the_pages_gps_run_gives_its_numbers(tmp_path):
    out = node(tmp_path, "out.r = IN.map(([v, a, tau, room, dyn]) => Motion.gpsRun(v, a, tau, room === null ? Infinity : room, dyn || undefined));",
               [[v, a, tau, None if room == math.inf else room, dyn] for v, a, tau, room, dyn in CASES])
    for (v, a, tau, room, dyn), (jx, ju) in zip(CASES, out["r"]):
        px, pu = T.gps_run(v, a, tau, room, dyn)
        assert px == pytest.approx(jx, abs=1e-6) and pu == pytest.approx(ju, abs=1e-6)


def test_page_predict_is_the_pages_rail_for_a_gps_fix(tmp_path):
    s0 = 41_000.0
    recs = [
        {"s": s0, "d": 1, "v": 20.0, "a": 0.0, "fix_at": NOW, "page": {"hold": s0 + 2500, "stop": None, "until": NOW + 75, "mode": "reported"}},
        {"s": s0, "d": -1, "v": 20.0, "a": 0.0, "fix_at": NOW, "page": {"hold": s0 - 2500, "stop": s0 - 400, "until": NOW + 75, "mode": "reported"}},
        {"s": s0, "d": 1, "v": 18.0, "a": -0.8, "fix_at": NOW, "page": {"hold": s0 + 2500, "stop": s0 + 3000, "until": NOW + 75, "mode": "reported"}},
        {"s": s0, "d": 1, "v": 25.0, "a": 0.0, "fix_at": NOW, "page": {"hold": s0 + 2500, "stop": None, "until": NOW + 15, "mode": "reported"}},
        {"s": s0, "d": 1, "v": 0.0, "a": 0.0, "fix_at": NOW, "page": {"hold": s0, "stop": None, "until": NOW + 75, "mode": "stopped"}},
    ]
    dts = [0, 3, 8, 15, 25, 40, 70]
    fixes = [{"s": r["s"], "d": r["d"], "train": "9960", "mode": r["page"]["mode"], "t0": NOW, "fixAt": NOW, "hold": r["page"]["hold"],
              "stop": r["page"]["stop"], "until": r["page"]["until"], "seg": None, "lag": None, "v": r["v"], "a": r["a"]} for r in recs]
    out = node(tmp_path, "out.r = IN.f.map((f) => IN.dts.map((dt) => Motion.rail(f, dt).s));", {"f": fixes, "dts": dts})
    for rec, js in zip(recs, out["r"]):
        for dt, s in zip(dts, js):
            assert T.page_predict(rec, NOW + dt) == pytest.approx(s, abs=1e-6)


def test_residuals_are_signed_ahead_and_summarised_by_gap_and_train():
    r = T.Residuals()
    for k, (res, gap, g) in enumerate([(30, 5, "NH west"), (-10, 8, "NH west"), (200, 25, "NH east"), (5, 12, "NH east"),
                                        (-160, 50, "NH west")]):
        r.add(NOW + k, res, gap, 20.0, g)
    r.add(NOW - 10_000, 999, 5, 20.0, "NH west")                # outside the window
    s = r.summary(NOW + 10, 600)
    assert s["n"] == 5 and s["median"] == 5 and s["mean"] == pytest.approx(13.0) and s["p90_abs"] == 200 and s["snaps"] == 2
    assert s["by_gap"]["≤10 s"]["n"] == 2 and s["by_gap"]["10-20 s"]["n"] == 1 and s["by_gap"]["20-40 s"]["n"] == 1
    assert s["by_gap"][">40 s"]["median"] == -160
    assert s["by_group"]["NH east"]["n"] == 2 and s["by_group"]["NH west"]["n"] == 3
    assert T.Residuals.text(s).startswith("5 fixes · median +5 m · mean +13 m · p90 |err| 200 m · snaps 2 · by gap ≤10 s 2: ")
    assert T.Residuals().summary(NOW, 600) == {"n": 0, "window_s": 600}


def test_the_tracker_measures_each_new_gps_fix_and_logs_a_summary_line(model):
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    lines = []
    tk = T.Tracker(log=lines.append)
    # 20 m/s, a fix every 5 s; the train really runs at 16 m/s: each new fix is behind the prediction (+ ahead)
    for k in range(70):
        now = NOW + 5 * k
        t = tt_train(model, pc - 1000 - 16.0 * 5 * k, stops, 20.0 if k == 0 else 16.0, now, vstatus="IN_TRANSIT_TO",
                     vstop=st["Rye"]["id"])
        t["stops"][1] = dict(t["stops"][1], status="Departed")
        t["stops"][2] = dict(t["stops"][2], arr=now + 600, dep=now + 600)
        tk.update(model, {"timestamp": now, "tt_at": now, "trains": {t["id"]: t}}, now)
        model.trains({"timestamp": now, "trains": {t["id"]: t}}, now, tk)      # a page looks: what it is given is kept
    rows = list(tk.residuals.rows)
    assert len(rows) >= 60 and all(r[2] == 5 for r in rows)
    assert rows[0][1] == pytest.approx((20.0 - 16.0) * 5, abs=2)               # the first: 20 m ahead
    assert all(abs(r[1]) < 25 for r in rows[5:])                                # then the speed is learnt
    pred = [x for x in lines if x.startswith("trains: prediction 5m")]
    assert len(pred) == 1 and "fixes · median" in pred[0] and "by train NH west" in pred[0]


# ---- the page: snap-back and Snap ------------------------------------------------------------------------------------


def gps_fix(**kw):
    f = {"s": 41_000.0, "d": 1, "train": "9960", "mode": "reported", "t0": NOW, "fixAt": NOW, "hold": 46_000.0,
         "stop": 46_000.0, "until": NOW + 300, "seg": None, "lag": None, "v": 20.0, "a": 0.0}
    return f | kw


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_small_lead_is_slowed_off_and_a_big_one_snapped_back(tmp_path):
    out = node(tmp_path, TRACK + """
const go = (id, by) => {
  tk.set(id, { ...IN.f, at: at(0) }, at(0));
  const before = drive(id, 0, 10);
  const drawn = tk.where(id, at(10)).s, tg = Motion.rail(IN.f, 10).s;
  tk.set(id, { ...IN.f, s: tg - by, t0: IN.f.t0 + 10, fixAt: IN.f.fixAt + 10, at: at(10) }, at(10));   // the train is by m behind
  return { drawn, after: drive(id, 10.1, 20), snap: Motion.RAIL.SNAP_MS };
};
out.small = go("small", 80); out.big = go("big", 400);
""", {"f": gps_fix()})
    sm, bg = out["small"], out["big"]
    steps = [b - a for a, b in zip([sm["drawn"]] + sm["after"], sm["after"])]
    assert min(steps) >= 0                                                  # 80 m ahead: slowed, never back
    steps = [b - a for a, b in zip([bg["drawn"]] + bg["after"], bg["after"])]
    back = [i for i, x in enumerate(steps) if x < 0]
    assert back and (back[-1] + 1) * 0.1 <= bg["snap"] / 1000 + 0.15        # 400 m: snapped back in SNAP_MS ...
    assert min(steps[back[-1] + 1:]) >= 0                                   # ... then on from there


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_snap_puts_the_train_on_its_last_fix_until_the_next(tmp_path):
    out = node(tmp_path, TRACK + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 10);
out.ok = tk.snapToFix("a", at(10));
out.pinned = tk.pinned("a");
out.held = drive("a", 10.1, 14);
tk.set("a", { ...IN.f, at: at(0) }, at(14.05));                          // the same fix again (a new answer): still held
out.still = drive("a", 14.1, 16);
tk.set("a", { ...IN.f, s: IN.f.s + 320, t0: IN.f.t0 + 16, fixAt: IN.f.fixAt + 16, at: at(16) }, at(16.05));   // a new fix
out.next = drive("a", 16.1, 20);
out.unpinned = !tk.pinned("a");
out.none = tk.snapToFix("nobody");
""", {"f": gps_fix()})
    s0 = gps_fix()["s"]
    assert out["ok"] and out["pinned"] and out["unpinned"] and out["none"] is False
    assert all(b <= a + 1e-9 for a, b in zip(out["held"], out["held"][1:]))   # eased back onto the fix ...
    assert out["held"][-1] == pytest.approx(s0, abs=0.01) and set(out["still"]) == {out["held"][-1]}   # ... and kept there
    assert out["next"][-1] > s0 + 320                                        # the next fix: predicted on again
