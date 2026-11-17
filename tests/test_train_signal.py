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
        assert 0 <= (x["s"] - x["hold_s"]) <= T.HOLD_FALLBACK_M + 0.1  # carried on no further than that
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
