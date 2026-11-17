"""Trains placed by Metro-North's signals, and trains only MTA TrainTime has (the admin, 2026-11-16: an Amtrak train,
"Train 67 · Metro-North · AM west · ... · Predicted · 42 s ago · MTA TrainTime (signal)", kept disappearing from the
map as it ran down the New Haven Line; "their data feed might be different than the GPS data from MTA lines, since
theirs is via SIGNAL").

  - An Amtrak train is not in Metro-North's GTFS-realtime: TrainTime alone has it. Its location is SIGNAL: the block it
    entered and when, unchanged until the next block a minute or more later. POS_MAX_AGE_S (40 s) dropped it from the
    merge 40 s after each signal (gone from the map until the next one): now kept SIGNAL_MAX_AGE_S.
  - A SIGNAL location is taken as it is (the GPS filter drew it halfway between two signals), its repeats are the
    train still in that block, a jump to the next block is expected, its speed is the block-to-block one (none on its
    first: held at the signal, unpredicted), carried on no further than HOLD_FALLBACK_M and never past TrainTime's own
    next stop; stale only after SIGNAL_STALE_S.
  - Its agency: Amtrak, never "Metro-North".
  - GPS fixes keep #589's handling (regressions).

Synthetic trains on the recorded static feed (tests/fixtures/mta): no real train's data."""

import json

import pytest

from sizeup import traintime as TT
from sizeup import trains as T
from tests.test_traintime import LOC, NOW, gtfs_rt, looking, provider
from tests.test_train_motion import NODE, node

BLOCK_M = 1800.0                  # one signal block (synthetic: Metro-North's are 1 to 3 km)
V = 30.0                          # m/s, about 67 mph
TID = "tt:67:2026-10-14"


def record(model, s, ts, src="SIGNAL", speed=None, num="67", branch="AM", next_code="2NR", d="W"):
    """A TrainTime record for an Amtrak train (branch AM) at s along the line, its location's time ts."""
    lat, lon, _ = model.line.at(s)
    loc = {"latitude": round(lat, 6), "longitude": round(lon, 6), "timestamp": int(ts), "source": src, "heading": 0.0}
    if speed is not None:
        loc["speed"] = speed
    return {"train_id": f"MNR_2026-10-14_{num}", "railroad": "MNR", "run_date": "2026-10-14", "train_num": num,
            "details": {"headsign": "", "branch": "Amtrak" if branch == "AM" else "New Haven", "branch_id": branch, "direction": d,
                        "stops": [{"code": next_code, "sched_time": NOW + 900, "stop_status": "EN_ROUTE", "stop_type": "STOP",
                                   "sign_track": "2", "t2s_track": "", "posted": False}]},
            "location": loc, "consist": {}, "status": {"otp": -1260}}


def poll(tr, clock, fetch, recs):
    """One TrainTime poll (and a GTFS-realtime read, without the train: Metro-North's feed doesn't have Amtrak's)."""
    fetch.loc = json.dumps(recs).encode()
    fetch.rt = gtfs_rt(ts=int(clock.t))
    looking(tr, clock)
    tr.viewed = clock.t
    tr.refresh_rt()
    tr.tt.poll()
    return {x["id"]: x for x in tr.public()["trains"]}


@pytest.fixture
def hub(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    return tr, clock, fetch


def stations(tr):
    return {s["name"]: s for s in tr.model.area_stations}


# ---- the bug: an Amtrak train on SIGNAL locations ----------------------------------------------------------------------


def test_an_amtrak_train_on_signal_locations_stays_on_the_map_and_moves_block_to_block(hub):
    """Westbound from Greenwich, a signal block every 60 s, TrainTime read every 5 s for 6 minutes. Before: drawn 40 s of
    every 60 (gone from the answer from 45 s after each signal), and drawn halfway between signals."""
    tr, clock, fetch = hub
    m = tr.model
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    signals = [s0 - BLOCK_M * k for k in range(6)]
    drawn = []
    for step in range(72):
        clock.t = t0 + 5 * step
        k = min(int(5 * step * V // BLOCK_M), len(signals) - 1)
        out = poll(tr, clock, fetch, [record(m, signals[k], t0 + k * BLOCK_M / V)])
        assert TID in out, f"gone at t+{5 * step} s"                 # never off the map
        x = out[TID]
        assert x["s"] == pytest.approx(signals[k], abs=1.0)            # at the signal, not between it and the last one
        assert x["agency"] == "Amtrak" and x["pos_src"] == "traintime" and not x["stale"] and not x["estimated"]
        assert x["mode"] == "reported" and x["fix_at"] == t0 + k * BLOCK_M / V    # its time: when it got to that signal
        assert x["next"]["name"] == "New Rochelle" and (x["hold_s"] - x["next"]["s"]) * -1 <= 0.1   # never past it
        # carried on to the end of its block, as long as the last, and SIGNAL_LATE_S of its travel (the next fix's
        # lateness); HOLD_FALLBACK_M before a block is known
        assert 0 <= (x["s"] - x["hold_s"]) <= (BLOCK_M + T.SIGNAL_LATE_S * V if k else T.HOLD_FALLBACK_M) + 0.1
        if k == 0:
            assert "v" not in x                                        # no speed yet: held at its signal, unpredicted
        else:
            assert x["v"] == pytest.approx(V, abs=0.1) and x["v_basis"]["moved"] == pytest.approx(V, abs=0.1)
        drawn.append(x["s"])
    assert all(b <= a for a, b in zip(drawn, drawn[1:]))               # only ever on, block to block
    assert len(set(round(s) for s in drawn)) == len(signals) and tr.track.rejected == 0


def test_the_same_signal_again_however_new_is_the_train_still_in_that_block(hub):
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    poll(tr, clock, fetch, [record(tr.model, s0, t0)])
    poll(tr, clock, fetch, [record(tr.model, s0 - BLOCK_M, t0 + 60)])
    for k in range(1, 6):                                              # TrainTime re-stamping the same signal
        clock.t = t0 + 60 + 5 * k
        x = poll(tr, clock, fetch, [record(tr.model, s0 - BLOCK_M, clock.t)])[TID]
        assert x["fix_at"] == t0 + 60 and x["v"] == pytest.approx(V, abs=0.1) and x["mode"] == "reported"


def test_a_signal_location_is_stale_only_after_minutes_and_gone_after_signal_max_age(hub):
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    rec = record(tr.model, s0, t0)
    assert poll(tr, clock, fetch, [rec])[TID]["stale_s"] == T.SIGNAL_STALE_S
    clock.t = t0 + 120                                                 # two minutes in one block (a red signal)
    x = poll(tr, clock, fetch, [rec])[TID]
    assert not x["stale"] and x["s"] == pytest.approx(s0, abs=1.0)
    clock.t = t0 + T.SIGNAL_STALE_S + 5
    assert poll(tr, clock, fetch, [rec])[TID]["stale"]                 # faded, held where it was: still there
    clock.t = t0 + TT.SIGNAL_MAX_AGE_S + 5
    assert TID not in poll(tr, clock, fetch, [rec])                    # nothing new for 10 min: off the map


def test_a_signal_jump_further_than_a_train_goes_or_backwards_is_refused(hub):
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    poll(tr, clock, fetch, [record(tr.model, s0, t0)])
    clock.t += 10
    x = poll(tr, clock, fetch, [record(tr.model, s0 - 9000, clock.t)])[TID]   # 9 km in 10 s
    assert x["s"] == pytest.approx(s0, abs=1.0) and tr.track.why == {"too fast": 1}
    clock.t += 10
    x = poll(tr, clock, fetch, [record(tr.model, s0 + 600, clock.t)])[TID]    # 600 m back
    assert x["s"] == pytest.approx(s0, abs=1.0) and tr.track.why.get("backwards") == 1


def test_a_signal_speed_from_the_feed_is_taken_as_it_is():
    last = {"signal": True, "s": 5000.0, "fix_at": NOW}
    assert T.signal_speed(last, 5000.0 - BLOCK_M, -1, NOW + 60, None) == (pytest.approx(V), pytest.approx(V))
    assert T.signal_speed(last, 5000.0 - BLOCK_M, -1, NOW + 60, 25.0) == (25.0, pytest.approx(V))
    assert T.signal_speed(last, 5000.0 - BLOCK_M, -1, NOW + 10, None)[0] == T.SIGNAL_MAX_MPS      # capped
    assert T.signal_speed(last, 5000.0, -1, NOW + 60, None) == (None, None)                       # not moved on
    assert T.signal_speed(None, 5000.0, -1, NOW + 60, None) == (None, None)                       # its first
    assert T.signal_speed(last, 5000.0 - BLOCK_M, -1, NOW + T.RESET_S + 1, None) == (None, None)  # too long ago


# ---- whose train ---------------------------------------------------------------------------------------------------------


def test_the_agency_is_amtrak_for_amtraks_trains():
    assert TT.agency({"branch_id": "AM", "branch": ""}) == "Amtrak"
    assert TT.agency({"branch_id": "", "branch": "Amtrak Northeast Regional"}) == "Amtrak"
    assert TT.agency({"branch_id": "NH", "branch": "New Haven"}) == "Metro-North"
    js = (T.ROOT / "sizeup" / "web" / "static" / "trains.js").read_text()
    assert 'amtrak(t) ? "Amtrak" : t.line ?' in js                    # the box: "Amtrak", never "Metro-North"


def test_a_metro_north_train_keeps_its_agency(hub):
    tr, clock, fetch = hub
    out = poll(tr, clock, fetch, json.loads(LOC))
    assert {x["agency"] for x in out.values()} == {"Metro-North"}


# ---- the merge ---------------------------------------------------------------------------------------------------------


def test_the_merge_keeps_a_train_only_traintime_has_by_its_signal_location_longer(hub):
    tr, _, _ = hub
    s0 = stations(tr)["Greenwich"]["ch"]
    feed = T.parse_feed(gtfs_rt())
    sig = TT.record(record(tr.model, s0, NOW))
    gps = TT.record(record(tr.model, s0, NOW, src="GPS", speed=60.0, num="69"))
    late = NOW + TT.POS_MAX_AGE_S + 30
    m = TT.merge(feed, [sig, gps], late)
    assert "tt:67:2026-10-14" in m["trains"] and m["trains"]["tt:67:2026-10-14"]["pos"]["loc"] == "SIGNAL"
    assert "tt:69:2026-10-14" not in m["trains"]                       # a GPS location: as before, 40 s
    assert "tt:67:2026-10-14" not in TT.merge(feed, [sig], NOW + TT.SIGNAL_MAX_AGE_S + 1)["trains"]
    assert TT.usable_position(sig, late, alone=True) and not TT.usable_position(sig, late)   # a GTFS train: its own position


# ---- GPS: #589 as it was (regressions) ----------------------------------------------------------------------------------


def test_a_gps_train_is_still_filtered_judged_and_its_residuals_kept(hub):
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    for k in range(4):                                                 # a GPS fix every 5 s at 30 m/s
        clock.t = t0 + 5 * k
        x = poll(tr, clock, fetch, [record(tr.model, s0 - V * 5 * k, clock.t, src="GPS", speed=V / T.MILE_M * 3600)])[TID]
    rec = tr.track.fixes[TID]
    assert rec["gps"] and not rec["signal"] and rec["page"] and tr.track.residuals.total == 3
    assert "stale_s" not in x and x["v"] == pytest.approx(V, abs=0.5)
    clock.t += 5                                                       # a GPS jump 1.5 km in 5 s: refused
    poll(tr, clock, fetch, [record(tr.model, s0 - V * 15 - 1500, clock.t, src="GPS", speed=67.0)])
    assert tr.track.why.get("too fast") == 1
    clock.t += TT.POS_MAX_AGE_S + 5                                    # a GPS location of a train only TrainTime has
    assert TID not in poll(tr, clock, fetch, [record(tr.model, s0 - V * 15, t0 + 15, src="GPS", speed=67.0)])


def test_signal_fixes_teach_no_residuals(hub):
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    for k in range(4):
        clock.t = t0 + 60 * k
        poll(tr, clock, fetch, [record(tr.model, s0 - BLOCK_M * k, clock.t)])
    assert tr.track.residuals.total == 0 and "page" not in tr.track.fixes[TID]


# ---- the page: a signal's own stale age ---------------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_holds_a_signal_fix_fresh_to_its_own_stale_age(tmp_path):
    out = node(tmp_path, """
const tr = Motion.railTracker({ staleS: 60 });
const fix = (staleS) => ({ s: 5000, d: -1, train: "67", mode: "reported", t0: 0, at: 0, fixAt: 0, hold: 3500, stop: null,
                          until: 75, seg: null, lag: null, v: null, a: 0, dyn: null, staleS });
tr.set("sig", fix(180), 0); tr.set("gps", fix(null), 0);
tr.where("sig", 0); tr.where("gps", 0);
tr.where("sig", 90000); tr.where("gps", 90000);
out.at90 = [tr.stale("sig"), tr.stale("gps")];
tr.where("sig", 181000);
out.at181 = tr.stale("sig");
""")
    assert out["at90"] == [False, True] and out["at181"] is True


def page_fix(x):
    """What trains.js fixOf makes of a TrainTime train in the hub's answer (its own fix time on the page's clock)."""
    return {"s": x["s"], "d": x["d"], "train": x["train"], "mode": x["mode"], "t0": x["fix_at"], "at": x["fix_at"] * 1000,
            "fixAt": x["fix_at"], "hold": x["hold_s"], "stop": (x.get("next") or {}).get("s"), "until": x["predict_until"],
            "seg": None, "lag": x.get("lag_s"), "v": x.get("v"), "a": x.get("a", 0.0), "dyn": x.get("dyn"),
            "staleS": x.get("stale_s"), "cruiseS": x.get("cruise_s"), "signal": True}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_steady_signal_train_is_drawn_on_smoothly_through_each_block(hub, tmp_path):
    """Review of #633: with a GPS fix's 20 s cruise the drawn train eased to a stop about 950 m into each 1.8 km block, then
    snapped 850 m on when the next signal came. Now: on at its block speed to the end of the block (block_m, cruise_s),
    no snap-back: never stalled mid-block, never more than a few metres a frame."""
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    answers = []
    for step in range(60):                                             # 5 min: a block every 60 s
        clock.t = t0 + 5 * step
        k = int(5 * step * V // BLOCK_M)
        x = poll(tr, clock, fetch, [record(tr.model, s0 - BLOCK_M * k, t0 + k * BLOCK_M / V)])[TID]
        answers.append({"t": clock.t, "fix": page_fix(x)})
    assert answers[-1]["fix"]["cruiseS"] == T.TT_HORIZON_S and tr.track.fixes[TID]["block_m"] == pytest.approx(BLOCK_M, abs=1)
    out = node(tmp_path, """
const tk = Motion.railTracker({ staleS: 60 });
const A = IN.answers, frames = [];
let i = 0;
for (let t = A[0].t; t <= A[A.length - 1].t + 4.9; t = Math.round((t + 0.1) * 10) / 10) {
  while (i < A.length && A[i].t <= t + 1e-9) { tk.set("a", A[i].fix, A[i].t * 1000); i++; }
  const p = tk.where("a", t * 1000);
  frames.push([t, p.s]);
}
out.frames = frames;
""", {"answers": answers})
    frames = [(t - t0, (s0 - s)) for t, s in out["frames"]]             # (s, metres on from Greenwich, westbound)
    on = [(t, x) for t, x in frames if t >= 65]                        # from the second block's speed on
    steps = [b[1] - a[1] for a, b in zip(on, on[1:])]
    assert max(steps) < 10 and min(steps) > -0.01                      # never a jump, never back (30 m/s: 3 m a frame)
    for t, x in on:
        assert abs(x - V * t) <= 150, (t, x)                           # with the train all the way, block to block
    for a in range(0, len(on) - 50):                                   # never stalled: on at least 100 m every 5 s
        assert on[a + 50][1] - on[a][1] >= 100, on[a]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_signal_train_held_at_a_red_signal_overshoots_a_little_and_is_glided_back(hub, tmp_path):
    """The trade-off of not braking for the end of the block: a train that stops there (a red signal, no new fix) is
    drawn on through it by about SIGNAL_LATE_S of its travel plus a hard stop, then glided back, never a jump."""
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    answers = []
    for step in range(50):                                             # two blocks, then stuck at the third signal
        clock.t = t0 + 5 * step
        k = min(2, int(5 * step * V // BLOCK_M))
        x = poll(tr, clock, fetch, [record(tr.model, s0 - BLOCK_M * k, t0 + k * BLOCK_M / V)])[TID]
        answers.append({"t": clock.t, "fix": page_fix(x)})
    out = node(tmp_path, """
const tk = Motion.railTracker({ staleS: 60 });
const A = IN.answers, frames = [];
let i = 0;
for (let t = A[0].t; t <= A[A.length - 1].t + 4.9; t = Math.round((t + 0.1) * 10) / 10) {
  while (i < A.length && A[i].t <= t + 1e-9) { tk.set("a", A[i].fix, A[i].t * 1000); i++; }
  frames.push([t, tk.where("a", t * 1000).s]);
}
out.frames = frames;
""", {"answers": answers})
    on = [(t - t0, s0 - s) for t, s in out["frames"] if t - t0 >= 125]
    end = 2 * BLOCK_M + BLOCK_M + T.SIGNAL_LATE_S * V                  # its hold: the end of the third block, and the slack
    assert max(x for _, x in on) <= end + 200                          # on past it at most a hard stop's way
    assert all(abs(b[1] - a[1]) < 10 for a, b in zip(on, on[1:]))     # glided, never a jump
    assert on[-1][1] <= end + 1                                         # back at the end of its block


def test_the_page_says_at_signal_not_stale_for_an_old_signal_fix():
    js = (T.ROOT / "sizeup" / "web" / "static" / "trains.js").read_text()
    assert "const sigOld = held && signalOf(t), stale = held && !sigOld;" in js     # dimmed, no "Stale" row
    assert "sigOld || (!aged && fixOlder(t, d, now, DIM_FIX_S))" in js
    assert 'sig ? "At signal"' in js and "signal: signalOf(t)" in js and "cruiseS:" in js


def test_a_signal_location_ages_from_when_the_train_got_there_not_its_newest_time(hub):
    """Review of #633: a record re-stamped at the same signal (a train that never moves on, or a dead record) leaves the
    map SIGNAL_MAX_AGE_S after it first had that signal, not never."""
    tr, clock, fetch = hub
    s0, t0 = stations(tr)["Greenwich"]["ch"], clock.t
    for k in range(0, int(TT.SIGNAL_MAX_AGE_S) + 60, 30):              # the same signal, its time re-stamped every read
        clock.t = t0 + k
        out = poll(tr, clock, fetch, [record(tr.model, s0, clock.t)])
        assert (TID in out) == (k <= TT.SIGNAL_MAX_AGE_S), k
        assert tr.tt.records[0]["loc_since"] == t0
    clock.t += 30                                                      # it moves on: a new signal, a new time
    assert TID in poll(tr, clock, fetch, [record(tr.model, s0 - BLOCK_M, clock.t)])
    assert tr.tt.records[0]["loc_since"] == clock.t
