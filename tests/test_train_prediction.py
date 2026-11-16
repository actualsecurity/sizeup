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


def tt_train(model, s, stops, speed, ts, vstatus="STOPPED_AT", vstop="", n="9960", loc="GPS"):
    """A train (train_at) whose position is a TrainTime GPS fix at s, speed m/s, taken at ts."""
    t = train_at(model, s, stops, vstatus=vstatus, vstop=vstop, n=n)
    lat, lon, _ = model.line.at(s)
    t["pos"] = {"lat": round(lat, 6), "lon": round(lon, 6), "bearing": None, "ts": ts, "src": "traintime", "loc": loc,
                "speed": speed}
    t["tt"] = {"branch": "NH", "fleet": "M8", "cars": 8}     # (what TrainTime adds: the page then has its GPS speed)
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
    # the first, with no fix before it, only up to GTFS-realtime's next stop; the second agrees with it: taken (before:
    # every one refused, "beyond its next stop", while that feed lagged)
    assert tk.rejected == 1 and tk.fix(t["id"])["ok"]
    [x] = model.trains({"timestamp": NOW + 5, "trains": {t["id"]: t}}, NOW + 5, tk)
    assert x["mode"] == "reported"                             # its own speed says: not "Stopped" at 47 mph
    assert x["next"]["name"] == "Rye" and (x["hold_s"] - rye) * -1 <= 0.1    # on to Rye, never past it
    assert x["segment"] is not None and x["segment"]["to"] == st["Rye"]["id"]
    assert x["v_fix"] == pytest.approx(21.0, abs=1.0)


def feed(tk, model, t, at):
    tk.update(model, {"timestamp": at, "tt_at": at, "trains": {t["id"]: t}}, at)
    return tk.fix(t["id"])


def moving(model, st, stops, s, at, speed=21.0):
    return tt_train(model, s, stops, speed, at, vstatus="IN_TRANSIT_TO", vstop=st["Rye"]["id"])


def test_a_gps_jump_is_refused_and_a_wrong_fix_does_not_lock_the_train_out(model):
    """Review of #589: a GPS fix 3.3 km on 5 s after the last (the 5 km rule with the MTA reporting points' 3.5 km
    allowance let it through, and then every right fix was refused "backwards" for minutes): too fast for a GPS fix
    (GPS_JUMP_M, not SPACING_M). And when the fix taken was wrong, two later fixes agreeing with each other win at once."""
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    stops = [stops[0], (stops[1][0], "Departed", stops[1][2])] + stops[2:]
    tk = T.Tracker()
    assert feed(tk, model, moving(model, st, stops, pc - 600, NOW), NOW)["ok"]
    f = feed(tk, model, moving(model, st, stops, pc - 600 - 3300, NOW + 5), NOW + 5)
    assert tk.why == {"too fast": 1} and f["s"] == pytest.approx(pc - 600, abs=1)            # refused: drawn where it was
    f = feed(tk, model, moving(model, st, stops, pc - 600 - 210, NOW + 10), NOW + 10)
    assert f["ok"] and f["s"] == pytest.approx(pc - 810, abs=60)                              # the next right one: taken
    # a wrong fix that looked possible (300 m on in 10 s), then the train where it really is: 1 refused, then back
    tk = T.Tracker()
    feed(tk, model, moving(model, st, stops, pc - 600, NOW), NOW)
    feed(tk, model, moving(model, st, stops, pc - 600 - 300, NOW + 10), NOW + 10)
    f = feed(tk, model, moving(model, st, stops, pc - 600 - 50, NOW + 15), NOW + 15)
    assert tk.why == {"backwards": 1} and not f["ok"]
    f = feed(tk, model, moving(model, st, stops, pc - 600 - 100, NOW + 20), NOW + 20)   # (behind the wrong one: refused,
    assert f["ok"] and f["s"] == pytest.approx(pc - 700, abs=1)                         # but agreeing with the one before: taken)


def test_a_first_gps_fix_is_held_to_the_feeds_next_stop_and_none_runs_past_its_last(model):
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    tk = T.Tracker()
    f = feed(tk, model, tt_train(model, pc - 1200, stops, 21.0, NOW, vstop=st["Port Chester"]["id"]), NOW)
    assert f is None and tk.why == {"beyond its next stop": 1}                                # first: no further
    # past its last stop (Harrison): never taken past it by more than STOP_SLACK_M, however it got there
    hs = st["Harrison"]["ch"]
    last = [(sid, "Departed", t_) for sid, _, t_ in stops[:-1]] + [stops[-1]]
    tk = T.Tracker()
    feed(tk, model, moving(model, st, last, hs + 300, NOW), NOW)
    f = feed(tk, model, moving(model, st, last, hs + 200, NOW + 5), NOW + 5)
    assert f["ok"]
    f = feed(tk, model, moving(model, st, last, hs - 700, NOW + 50), NOW + 50)
    assert not f["ok"] and tk.why == {"beyond its next stop": 2}           # (the first, too: past the feed's next stop)
    [x] = model.trains({"timestamp": NOW + 50, "trains": {"9960": moving(model, st, last, hs - 700, NOW + 50)}}, NOW + 50, tk)
    assert (x["hold_s"] - hs) * -1 <= 0.1                                                      # never predicted past it


def test_stopped_has_hysteresis_and_gtfs_stopped_at_counts_at_the_stop(model):
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    a = model.anchors(train_at(model, pc, stops))
    t = train_at(model, pc - 30, stops, vstatus="STOPPED_AT", vstop=st["Port Chester"]["id"])
    base = {"s": pc - 30, "d": -1, "fix_at": NOW, "src": "traintime", "bearing": None, "first_read": False}
    mode = lambda v, prev=None, vs="STOPPED_AT": model.place(dict(t, vstatus=vs), a, -1, NOW, base | {"v": v, "stopped_prev": prev})["mode"]
    assert mode(0.3) == "stopped" and mode(0.8) == "stopped"                  # at its stop, STOPPED_AT: creeping is stopped
    assert mode(0.8, vs="IN_TRANSIT_TO") == "reported"                         # not at it: 0.8 m/s is moving ...
    assert mode(0.8, prev=True, vs="IN_TRANSIT_TO") == "stopped"               # ... unless it was stopped (MOVE_MPS)
    assert mode(1.2, prev=True) == "reported" and mode(21.0) == "reported"     # never "Stopped · 47 mph"


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
    slow = dict(last, vf=5.0)                                                 # after a long gap, an impossible change still
    chk = T.speed_check(slow, 40_000.0 + 5.0 * 31, 1, NOW + 31, 60.0, True)  # waits for a second reading (A_PHYS)
    assert chk["vf"] == 5.0 and chk["bad"] == "speed"


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


def test_the_tracker_measures_each_new_gps_fix_and_logs_a_summary_line(model, monkeypatch):
    monkeypatch.setattr(T, "PRED_LOG_S", 60.0)
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    lines = []
    tk = T.Tracker(log=lines.append)
    # 20 m/s, a fix every 5 s; the train really runs at 16 m/s: each new fix is behind the prediction (+ ahead)
    for k in range(13):                                      # (to 1 km on: short of braking for Rye)
        now = NOW + 5 * k
        t = tt_train(model, pc - 1000 - 16.0 * 5 * k, stops, 20.0 if k == 0 else 16.0, now, vstatus="IN_TRANSIT_TO",
                     vstop=st["Rye"]["id"])
        t["stops"][1] = dict(t["stops"][1], status="Departed")
        t["stops"][2] = dict(t["stops"][2], arr=now + 600, dep=now + 600)
        tk.update(model, {"timestamp": now, "tt_at": now, "trains": {t["id"]: t}}, now)
    rows = list(tk.residuals.rows)                           # (nobody fetched the list: no viewer needed)
    assert len(rows) == 12 and all(r[2] == 5 for r in rows)
    assert rows[0][1] == pytest.approx((20.0 - 16.0) * 5, abs=2)               # the first: 20 m ahead
    assert all(abs(r[1]) < 10 for r in rows[3:])                                # then the speed is learnt
    pred = [x for x in lines if x.startswith("trains: prediction 1m")]
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
tk.set("a", { ...IN.f, at: at(0) + 7 }, at(14.05));                      // the same fix again, a new answer (its at shifted
                                                                         // by the answer's latency, trains.js fixOf): still held
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


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_only_a_new_gps_fix_snaps(tmp_path):
    """Review of #589: every answer is "fresh"; a snap-back acts only on one carrying a new fix, and only a GPS one."""
    out = node(tmp_path, TRACK + """
tk.set("a", { ...IN.f, at: at(0) }, at(0));
drive("a", 0, 5);
tk.items.get("a").shown = { ...tk.items.get("a").shown, s: tk.items.get("a").shown.s + 300 };   // drawn 300 m off
tk.set("a", { ...IN.f, at: at(0) + 9 }, at(5.05));                      // the same fix re-sent
tk.where("a", at(5.1));
out.resent = !!tk.items.get("a").snapTo;
tk.set("a", { ...IN.f, s: IN.f.s + 100, t0: IN.f.t0 + 5, fixAt: IN.f.fixAt + 5, at: at(5) }, at(5.15));   // a new one
tk.where("a", at(5.2));
out.fresh = !!tk.items.get("a").snapTo;
""", {"f": gps_fix()})
    assert out["resent"] is False and out["fresh"] is True


def test_the_page_params_kept_on_a_fix_are_what_the_list_sends(model):
    """The residuals' page parameters (worked out when a fix is accepted) and the list a page fetches: one builder, the
    same values."""
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    stops = [stops[0], (stops[1][0], "Departed", stops[1][2])] + stops[2:]
    tk = T.Tracker()
    t = moving(model, st, stops, pc - 600, NOW)
    rec = feed(tk, model, t, NOW)
    [x] = model.trains({"timestamp": NOW, "trains": {t["id"]: t}}, NOW, tk)
    assert rec["page"] == T.page_params(x)
    assert set(rec["page"]) == {"s", "hold", "stop", "until", "mode", "v", "a", "dyn"} and rec["page"]["v"] == x["v"]


def test_a_frozen_position_cannot_confirm_itself_and_slow_polls_still_confirm(model):
    """Re-review of #589: the same refused position re-sent with newer times is no second fix (unless the train reads
    stopped there); the confirming window scales with TrainTime's poll (CONFIRM_POLLS)."""
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    first = lambda: tt_train(model, pc - 1200, stops, 21.0, NOW, vstop=st["Port Chester"]["id"])
    tk = T.Tracker()
    feed(tk, model, first(), NOW)
    same = dict(first(), pos=dict(first()["pos"], ts=NOW + 5))
    assert feed(tk, model, same, NOW + 5) is None                         # the same position again: still refused
    on = tt_train(model, pc - 1305, stops, 21.0, NOW + 10, vstop=st["Port Chester"]["id"])
    assert feed(tk, model, on, NOW + 10)["ok"]                             # a different one agreeing: taken
    tk = T.Tracker()                                                       # standing there, reading stopped: it may
    feed(tk, model, tt_train(model, pc - 1200, stops, 0.0, NOW, vstop=st["Port Chester"]["id"]), NOW)
    assert feed(tk, model, tt_train(model, pc - 1200, stops, 0.0, NOW + 5, vstop=st["Port Chester"]["id"]), NOW + 5)["ok"]
    assert T.Tracker().confirm_s == T.CONFIRM_S and T.Tracker(tt_every_s=60.0).confirm_s == 150.0
    tk = T.Tracker(tt_every_s=60.0)                                        # polled every 60 s: 120 s apart still confirm
    feed(tk, model, first(), NOW)
    on = tt_train(model, pc - 1200 - 21 * 120, stops, 21.0, NOW + 120, vstop=st["Port Chester"]["id"])
    assert feed(tk, model, on, NOW + 120)["ok"]


def test_the_gps_noise_allowance_is_for_gps_locations_only(model):
    """A SIGNAL location (a fixed point on the line) keeps the reporting points' allowance (SPACING_M)."""
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    stops = [stops[0], (stops[1][0], "Departed", stops[1][2])] + stops[2:]
    for loc, ok in (("GPS", False), ("SIGNAL", True)):
        tk = T.Tracker()
        feed(tk, model, tt_train(model, pc - 300, stops, 21.0, NOW, vstatus="IN_TRANSIT_TO", vstop=st["Rye"]["id"], loc=loc), NOW)
        f = feed(tk, model, tt_train(model, pc - 1800, stops, 21.0, NOW + 5, vstatus="IN_TRANSIT_TO", vstop=st["Rye"]["id"], loc=loc), NOW + 5)
        assert f["ok"] is ok, loc                                          # 1.5 km in 5 s: GPS too fast; SIGNAL allowed


def test_working_out_the_page_params_leaves_no_track_memory(model):
    """Re-review of #589: the residuals' place() (no viewer) never adds a remembered track (an entry with no "seen"
    would never expire and could carry into the next day's train of that number)."""
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    stops = [stops[0], (stops[1][0], "Departed", stops[1][2])] + stops[2:]
    tk = T.Tracker()
    rec = feed(tk, model, moving(model, st, stops, pc - 600, NOW), NOW)
    assert rec["page"] and tk.trk_mem == {}


def test_stopped_is_kept_over_one_missed_read(model):
    st, stops = inbound(model)
    pc = st["Port Chester"]["ch"]
    stops = [stops[0], (stops[1][0], "Departed", stops[1][2])] + stops[2:]
    tk = T.Tracker()
    feed(tk, model, moving(model, st, stops, pc - 600, NOW, speed=0.0), NOW)
    feed(tk, model, moving(model, st, stops, pc - 600, NOW + 5, speed=0.0), NOW + 5)
    assert tk.fix("9960")["stopped"]
    tk.update(model, {"timestamp": NOW + 10, "tt_at": NOW + 10, "trains": {}}, NOW + 10)    # missing from one read
    t = moving(model, st, stops, pc - 615, NOW + 15, speed=0.8)
    feed(tk, model, t, NOW + 15)
    [x] = model.trains({"timestamp": NOW + 15, "trains": {t["id"]: t}}, NOW + 15, tk)
    assert x["mode"] == "stopped"                                          # 0.8 m/s, but it was stopped: still stopped
