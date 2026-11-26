"""The station's signal metrics read back (#606, sizeup/cloud/metricsinsight.py): the suggested squelch, the shift
warnings after an event, Health's sparks, GET /api/cad/metrics?view=... (admins only) and the thresholds' Settings.

Shapes: an FG1-like traffic channel idles at 8-10 dB of quieting (q90 of a minute's looks, the squelch closed: as
tests/test_station_metrics.py's real squelch on hiss gives, under its 10 dB Open) and carries traffic at 15-30 dB at
open, with the real FG1 capture's own transmission (~24 dB at open) among it; a weak-traffic channel's weakest real
key-ups sit at 12 dB; a P25 control channel decodes ~40 messages/s (the sample log's rate lines). Everything else
(station ids, serials, times) is invented."""

from __future__ import annotations

import json
import math
import random
import time

import pytest

from sizeup.cad.api import CadApi
from sizeup.cloud import metricsinsight as mi
from sizeup.cloud.access import Identity
from sizeup.cloud.metricstore import MetricStore
from tests.test_cad import make_cad
from tests.test_roles import CASEY, JORDAN, people
from tests.test_station_metrics import fg1_segment

NOW = 1_800_000_000.0
DAY = 86400.0
CFG = dict(mi.DEFAULTS)


def floor_minutes(t0, n, q90=lambda k, r: r.gauss(9.0, 0.45), settings=None, noise=lambda k, r: 0, open_s=0,
                  kind="traffic", every=15, seed=1, am=False):
    """n minute floor records from t0: the idle q90 (and q50 / qmax around it), the noise opens, settings every
    `every` minutes (as the station sends them)."""
    r = random.Random(seed)
    out = []
    for k in range(n):
        q = round(q90(k, r), 1)
        d = {"k": "floor", "at": t0 + 60 * k, "n": 60, "open_s": open_s(k, r) if callable(open_s) else open_s,
             "q50": round(q - 1.8, 1), "q90": q, "qmax": round(q + 1.1, 1), "noise": noise(k, r)}
        if am:
            d.update(floor=round(q90(k, r) - 60, 1))
        else:
            d["hiss"] = 2.4
        if k % every == 0:
            d.update(settings or {"kind": kind, "open": 10.0, "close": 6.0, "min": 0.3, "sdr": "00000102", "gain": 36.4,
                                  "state": "ok", **({"am": 1} if am else {})})
        out.append(d)
    return out


def transmissions(t0, span, qos, kind="voice", seed=2):
    r = random.Random(seed)
    return sorted(({"k": "tx", "at": t0 + r.random() * span, "kind": kind, "dur": 2.4, "qo": round(q, 1),
                    "q": round(q - 0.8, 1), "qc": round(q - 5, 1), "hiss": 2.4} for q in qos), key=lambda d: d["at"])


def real_fg1_qo() -> float:
    return round(fg1_segment().q_open, 1)


# -- the suggested squelch --------------------------------------------------------------------------------------------

def test_fg1_like_channel_gets_ceil_noise_p90_plus_margin_and_no_real_traffic_under_it():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440)
    r = random.Random(3)
    qos = [r.uniform(15, 30) for _ in range(211)] + [real_fg1_qo()]
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, qos), CFG, NOW)
    noise = mi.pct([d["q90"] for d in floor], 90)
    assert 9.3 <= noise <= 9.9                                       # the idle sits at 8-10 dB
    assert out["state"] == "ok" and out["noise_p90"] == round(noise, 1)
    assert out["open"] == math.ceil(noise + 3) == 13
    tails = min(d["qc"] for d in transmissions(NOW - 7 * DAY, 7 * DAY, qos))
    # Close: the running 4 dB of hysteresis would be 9, but never above the real tails' own quieting at close - 1
    assert out["close"] == min(9.0, math.floor((tails - 1) * 2) / 2) and out["cut_tails"] == 0
    assert out["under"] == 0 and out["real"] == 212 and not out["capped"] and out["warn"] == ""
    assert out["apply"] and out["info"] == ""                       # a traffic channel: an admin may apply it
    assert out["why"].startswith(f"noise p90 {noise:.1f} dB, idle max p90 {out['noise_max_p90']:.1f} (10,080 idle min, "
                                 "7 d) + 3 → 13 · 0/212 real under 13 · weakest ")
    assert sum(out["hist"]["idle"]["n"]) == 10080 and sum(out["hist"]["real"]["n"]) == 212


def test_it_is_never_above_the_weak_traffic_the_channel_really_carries():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440)
    r = random.Random(4)
    weak = [r.uniform(12.0, 12.6) for _ in range(30)]               # the weakest real key-ups: 12 dB at open
    qos = weak + [r.uniform(15, 30) for _ in range(270)]
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, qos), CFG, NOW)
    # the weakest real key-ups (12 dB) sit under noise + margin (13): no Open serves both, the running one stays
    assert out["warn"] == "weak traffic overlaps the noise: 30 of 300 real under 13" and not out["apply"]
    assert out["open"] == 10.0 and out["under"] == 0 and out["open"] < min(weak)
    # an admin who allows 10% (Settings): those 30 of 300 are within it, and the count says so
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, qos), {**CFG, "miss_pct": 10.0}, NOW)
    assert out["open"] == 13.0 and not out["capped"] and out["under"] == 30 and "30/300 real under 13" in out["why"]


def test_scenario_a_weak_traffic_in_the_noise_never_pulls_open_under_the_noise():
    # review scenario A: noise p90 ~10.3, real key-ups at 8 dB at open: the old cap suggested 7, under the noise
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(9.7, 0.45))
    qos = [8.0] * 10 + [random.Random(k).uniform(15, 30) for k in range(200)]
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, qos), CFG, NOW)
    assert out["noise_p90"] >= 10.2 and out["open"] == 10.0 and not out["apply"]
    assert out["warn"].startswith("weak traffic overlaps the noise")


def test_scenario_b_a_chattering_channel_never_gets_a_lower_suggestion():
    # review scenario B: chattering on a running 10; a weak key-up at 10.4 used to cap it to 9
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(5.5, 0.4), noise=lambda k, r: 3)
    qos = [10.4] * 3 + [random.Random(k).uniform(15, 30) for k in range(100)]
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, qos), CFG, NOW)
    assert out["open"] >= 10.0 and not out["apply"]


def test_a_lower_suggestion_stays_over_the_idle_max_and_moves_two_db_a_step():
    quiet = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(4.0, 0.3),
                          settings={"kind": "traffic", "open": 14.0, "close": 10.0})
    tx = transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 50)
    out = mi.suggest(quiet, tx, CFG, NOW)
    assert out["base"] == 8.0 and out["open"] == 12.0 and out["apply"]            # 14 -> 12, not 8 at once
    spiky = [dict(d, qmax=round(d["q90"] + 7.0, 1)) for d in quiet]               # the hiss spikes to ~11.4
    out = mi.suggest(spiky, tx, CFG, NOW)
    assert out["noise_max_p90"] >= 11.0 and out["open"] == 12.0 > out["noise_max_p90"]   # never into the idle max
    spiky10 = [dict(d, qmax=round(d["q90"] + 7.0, 1)) for d in floor_minutes(NOW - 7 * DAY, 7 * 1440,
                                                                         q90=lambda k, r: r.gauss(4.0, 0.3))]
    out = mi.suggest(spiky10, tx, CFG, NOW)                                          # running 10: not lowered at all
    assert out["open"] == 10.0 and out["same"] and not out["apply"]


def test_a_raised_close_never_cuts_the_real_tails():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440)
    tx = [dict(d, qc=7.5) for d in transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 50)]
    out = mi.suggest(floor, tx, CFG, NOW)
    assert out["open"] == 13.0 and out["close"] == 6.5 and out["cut_tails"] == 0   # held under the tails (7.5 - 1)
    assert "Close held under the tails" in out["why"]


def test_after_a_squelch_change_it_settles_and_counts_the_chatter_only_since():
    sq = NOW - 2 * 3600
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(5.5, 0.4),
                          noise=lambda k, r: 3 if NOW - 7 * DAY + 60 * k < sq else 0)
    tx = transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 50)
    out = mi.suggest(floor, tx, CFG, NOW, squelch_at=sq)
    assert out["warn"].startswith("settling: squelch changed") and not out["apply"] and "chattering" not in out["why"]
    out = mi.suggest(floor, tx, CFG, NOW + 8 * 3600, squelch_at=sq)                  # 10 h on: quiet since the change
    assert out["opens_p50"] == 0 and "chattering" not in out["why"] and out["warn"] == ""


def _rf_store(tmp_path, ev_at, before_q, after_q, before_tx, after_tx):
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(
        before_q if NOW - 7 * DAY + 60 * k < ev_at else after_q, 0.4)))
    put(s, "FG1", transmissions(NOW - 7 * DAY, ev_at - (NOW - 7 * DAY), before_tx)
        + transmissions(ev_at + 600, NOW - ev_at - 600, after_tx, seed=5))
    put(s, "FG1", [ev("FG1", ev_at, "sdr", "SDR 00000102 -> 00000105")])
    return mi.Insight(s, clock=lambda: NOW, log=lambda m: None)


def test_scenario_e_a_rewire_that_raised_the_noise_starts_the_window_again(tmp_path):
    # the noise rose 20 h ago with a dongle change: 6 days of the old, quiet floor would suggest an Open under the hiss
    ins = _rf_store(tmp_path, NOW - 20 * 3600, 4.0, 9.0, [20.0] * 200, [20.0] * 30)
    out = ins.suggestion("pcfd-hq", "FG1")
    assert out["state"] == "short" and out["why"].startswith("not enough data since SDR change ")
    assert out["since"]["ev"] == "sdr" and not out["apply"]


def test_scenario_f_old_strong_traffic_never_masks_new_weak_traffic(tmp_path):
    # strong traffic until a dongle change 3 days ago, weak since: only what came after counts
    ins = _rf_store(tmp_path, NOW - 3 * DAY, 9.0, 9.0, [25.0] * 300, [12.5] * 30)
    ins.section = lambda: {"miss_pct": 10.0}                   # (10% allowed: 30 of 330 would have slipped through)
    out = ins.suggestion("pcfd-hq", "FG1")
    assert out["real"] == 30 and out["days"] == 3.0 and out["warn"].startswith("weak traffic overlaps the noise")
    assert not out["apply"]


def test_under_a_day_of_data_is_not_enough():
    floor = floor_minutes(NOW - DAY, 1000)
    out = mi.suggest(floor, transmissions(NOW - DAY, DAY, [20.0] * 50), CFG, NOW)
    assert out["state"] == "short" and "open" not in out and not out["apply"]
    assert out["why"] == "not enough data: 17 h of 24 h"
    assert mi.suggest([], [], CFG, NOW)["state"] == "short"          # nothing at all: the same, no error


def test_paging_is_information_only_and_never_raised():
    """Without frequent static, paging's advisory never goes above what it runs (static: tests/test_paging_squelch.py)."""
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(9.5, 0.4), kind="paging")
    pages = transmissions(NOW - 7 * DAY, 7 * DAY, [random.Random(k).uniform(18, 26) for k in range(40)], kind="page")
    out = mi.suggest(floor, pages, CFG, NOW)
    assert out["state"] == "ok" and out["info"] == "paging: advisory" and not out["apply"]
    assert out["open"] <= out["cur_open"] == 10.0                    # never above what it runs
    assert "paging: never above the running Open" in out["why"]


def test_a_raise_needs_real_traffic_to_check_it_against():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440)
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, [22.0] * 5), CFG, NOW)
    assert out["open"] == 10.0 and out["same"] and not out["apply"]
    assert "5 real tx: too few to check a raise" in out["why"]


def test_a_quiet_floor_suggests_lower_and_noise_kind_never_counts_as_real():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(4.0, 0.4))
    tx = transmissions(NOW - 7 * DAY, 7 * DAY, [8.0] * 40, kind="noise") + \
        transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 40, seed=5)
    out = mi.suggest(floor, tx, CFG, NOW)
    assert out["open"] == 8.0 and out["close"] == 4.0 and out["real"] == 40 and out["noise_tx"] == 40
    assert out["apply"]


def test_a_chattering_channel_is_never_suggested_lower():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(6.0, 0.4), noise=lambda k, r: 3)
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, [22.0] * 50), CFG, NOW)
    assert out["open"] == 11.0 and "chattering 3/min" in out["why"]


def test_am_gets_the_numbers_for_information():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(3.0, 0.4), kind="", am=True)
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, [15.0] * 40), CFG, NOW)
    assert out["am"] and out["info"] == "AM: no squelch setting here" and not out["apply"]


def test_close_stays_in_its_range_near_the_top():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(27.5, 0.2),
                          settings={"kind": "traffic", "open": 28.0, "close": 27.0})
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, [35.0] * 40), CFG, NOW)
    assert out["open"] == 30.0 and out["close"] == 28.0          # the range's top for both (it took Close 29 before)


def test_settings_out_of_range_fall_back_to_the_defaults():
    assert mi.settings({"margin_db": 50, "miss_pct": True, "shift_db": 4}) == {**mi.DEFAULTS, "shift_db": 4.0}


# -- the shifts -------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def store(tmp_path, now=NOW, audit=None):
    clock = Clock(now)
    s = MetricStore(tmp_path / "metrics.sqlite", log=lambda m: None, clock=clock, audit=audit)
    return s, clock


def put(s, subject, recs, station="pcfd-hq"):
    recs = [dict(r, s=subject) for r in recs]
    for k in range(0, len(recs), 5000):
        s.ingest(station, recs[k:k + 5000])


def ev(subject, at, kind, detail=""):
    return {"k": "ev", "s": subject, "at": at, "ev": kind, "d": detail}


def stepped(t_step, before_q, after_q, start=None, end=NOW):
    start = t_step - DAY if start is None else start
    return floor_minutes(start, int((end - start) // 60), q90=lambda k, r: r.gauss(
        before_q if start + 60 * k < t_step else after_q, 0.3))


def test_a_step_after_an_sdr_change_is_warned_once_and_a_step_without_an_event_is_not(tmp_path):
    t = NOW - 3 * 3600                                                # the dongle moved three hours ago
    s, _ = store(tmp_path)
    put(s, "FG1", stepped(t, 5.0, 9.2))
    put(s, "FG1", [ev("FG1", t, "sdr", "SDR 00000102 -> 00000105")])
    put(s, "FG2", stepped(t, 5.0, 9.2))                              # the same step, no event
    ins = mi.Insight(s, clock=lambda: NOW, log=lambda m: None)
    found = ins.turn()
    assert [(f["subject"], f["metric"], f["event"]) for f in found] == [("FG1", "idle_q90", "sdr")]
    f = found[0]
    assert 3.8 <= f["after"] - f["before"] <= 4.6
    assert f["text"].startswith("idle quieting +") and f["text"].endswith(" (SDR change)") and " since " in f["text"]
    ins.turn()
    hints = ins.hints()
    assert len(hints) == 1 and hints[0]["kind"] == "metrics_shift" and hints[0]["level"] == "warn"
    assert hints[0]["channel"] == "FG1" and hints[0]["link"] == f"#settings/radios/signal?s=FG1&at={int(t)}"


def test_no_warning_until_half_an_hour_after_and_none_after_two_days(tmp_path):
    s, clock = store(tmp_path)
    t = NOW - 20 * 60
    put(s, "FG1", stepped(t, 5.0, 9.2))
    put(s, "FG1", [ev("FG1", t, "gain", "36.4 -> 42.1")])
    assert mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn() == []      # 15 min of data after it: not yet
    s2, _ = store(tmp_path / "old")
    t = NOW - 50 * 3600
    put(s2, "FG1", stepped(t, 5.0, 9.2))
    put(s2, "FG1", [ev("FG1", t, "gain", "36.4 -> 42.1")])
    assert mi.Insight(s2, clock=lambda: NOW, log=lambda m: None).turn() == []     # past 48 h: not shown any more


def test_a_squelch_change_moves_the_idle_quieting_on_purpose_no_warning(tmp_path):
    s, _ = store(tmp_path)
    t = NOW - 3 * 3600
    put(s, "FG1", stepped(t, 7.0, 11.5))                              # a higher Open: the idle reads higher
    s.ingest("pcfd-hq", [ev("FG1", t, "squelch", "open 14 close 10 min 0.3 (was open 10 close 6 min 0.3)")])
    assert mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn() == []


def test_the_settings_audits_squelch_change_counts_as_the_event_too(tmp_path):
    t = NOW - 3 * 3600
    audit = lambda subject, a, b: [{"at": t, "operator": "Casey", "key": f"squelch/{subject}", "old": "{}",
                                    "new": "{'open_db': 14}"}] if a <= t < b else []
    s, _ = store(tmp_path, audit=audit)
    put(s, "FG1", stepped(t, 7.0, 11.5))
    assert mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn() == []      # a squelch change: on purpose
    rec = lambda subject, a, b: [{"at": t, "operator": "Casey", "key": f"channel/{subject}", "old": "on", "new": "off"}] \
        if a <= t < b else []
    s2, _ = store(tmp_path / "rec", audit=rec)
    put(s2, "FG1", stepped(t, 5.0, 9.2))
    assert mi.Insight(s2, clock=lambda: NOW, log=lambda m: None).turn() == []     # record / transcribe: never the cause


def test_a_dongle_moved_back_or_a_squelch_raise_after_it_is_never_blamed_on_the_first_move(tmp_path):
    # moved at T (+4 dB), moved back at T + 4 h: nothing is shifted now, so no warning about the first move
    t = NOW - 10 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(t - DAY, int((NOW - t + DAY) // 60), q90=lambda k, r: r.gauss(
        9.0 if t <= t - DAY + 60 * k < t + 4 * 3600 else 5.0, 0.3)))
    put(s, "FG1", [ev("FG1", t, "sdr", "SDR 00000102 -> 00000105"), ev("FG1", t + 4 * 3600, "sdr", "SDR 00000105 -> 00000102")])
    assert mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn() == []
    # an SDR change that changed nothing, then a squelch raise two hours later (the idle reads higher on purpose)
    s2, _ = store(tmp_path / "sq")
    t = NOW - 5 * 3600
    put(s2, "FG1", stepped(t + 2 * 3600, 5.0, 9.3))
    put(s2, "FG1", [ev("FG1", t, "sdr", "SDR 00000102 -> 00000105"),
                    ev("FG1", t + 2 * 3600, "squelch", "open 14 close 10 min 0.3 (was open 10 close 6 min 0.3)")])
    assert mi.Insight(s2, clock=lambda: NOW, log=lambda m: None).turn() == []


def test_a_quiet_night_after_a_busy_evening_is_not_a_shift(tmp_path):
    # traffic 07:00-23:00 every day, none at night; the station restarts at 23:00: the same hours the day before were
    # quiet too
    t = NOW - 6 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(t - 2 * DAY, int((NOW - t + 2 * DAY) // 60)))
    tx = []
    for d in (2, 1, 0):
        day0 = t - d * DAY - 16 * 3600                                  # 07:00 that day (t is 23:00)
        tx += transmissions(day0, 16 * 3600, [20.0] * 160, seed=d)
    put(s, "FG1", [x for x in tx if x["at"] < NOW])
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    assert mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn() == []


def test_noise_opens_up_three_times_after_a_rewire(tmp_path):
    s, _ = store(tmp_path)
    t = NOW - 4 * 3600
    recs = floor_minutes(t - DAY, int((NOW - t + DAY) // 60),
                         noise=lambda k, r: (1 if r.random() < 0.1 else 0) if k < 1440 else (1 if r.random() < 0.8 else 0))
    put(s, "FG1", recs)
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    assert [(f["metric"], f["event"]) for f in found] == [("noise_opens", "start")]
    assert found[0]["text"].startswith("noise opens 0.8/min (was 0.1)") and found[0]["text"].endswith("(station restart)")


def test_traffic_that_stops_after_an_event_is_warned_and_a_busy_hour_before_is_not(tmp_path):
    t = NOW - 6 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(t - DAY, int((NOW - t + DAY) // 60)))
    put(s, "FG1", transmissions(t - DAY, DAY, [20.0] * 240) + transmissions(t + 600, 6 * 3600 - 600, [20.0] * 6, seed=9))
    put(s, "FG1", [ev("FG1", t, "back", "SDR 00000102")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    assert [(f["metric"], f["event"]) for f in found] == [("tx_rate", "back")]
    assert 9.0 <= found[0]["before"] <= 13.0 and found[0]["after"] <= 2.0       # the same hours the day before
    s2, _ = store(tmp_path / "fire")
    put(s2, "FG1", floor_minutes(t - DAY, int((NOW - t + DAY) // 60)))
    put(s2, "FG1", transmissions(t - 3 * 3600, 3600, [20.0] * 300))   # a working fire's hour, the day before: median 0
    put(s2, "FG1", [ev("FG1", t, "back", "SDR 00000102")])
    assert mi.Insight(s2, clock=lambda: NOW, log=lambda m: None).turn() == []


def test_a_p25_control_channel_that_decodes_less_after_an_event_is_warned(tmp_path):
    t = NOW - 2 * 3600
    s, _ = store(tmp_path)
    r = random.Random(6)
    cc = [{"k": "cc", "at": a, "n": 20, "state": "ok", "rate": round(r.gauss(40 if a < t else 25, 1.0), 1),
           "lo": 36.0, "hi": 44.0, "sdr": "00000201"} for a in range(int(t - DAY), int(NOW), 60)]
    put(s, "p25:sys2", cc)
    put(s, "p25:sys2", [ev("p25:sys2", t, "sdr", "SDR 00000201 -> 00000203")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    assert [(f["subject"], f["metric"]) for f in found] == [("p25:sys2", "cc_rate")]
    assert found[0]["text"].startswith("decode 25/s (was 40/s) since ") and found[0]["text"].endswith("(SDR change)")
    # a 20% dip is under the default 30%
    s2, _ = store(tmp_path / "dip")
    put(s2, "p25:sys2", [dict(d, rate=d["rate"] if d["at"] < t else round(d["rate"] * 0.8 / 0.625, 1)) for d in cc])
    put(s2, "p25:sys2", [ev("p25:sys2", t, "sdr", "SDR 00000201 -> 00000203")])
    assert mi.Insight(s2, clock=lambda: NOW, log=lambda m: None).turn() == []


def test_pages_that_open_weaker_after_a_splitter_change_are_warned(tmp_path):
    t = NOW - 30 * 3600
    s, _ = store(tmp_path)
    put(s, "South 2", floor_minutes(t - DAY, int((NOW - t + DAY) // 60), kind="paging"))
    put(s, "South 2", transmissions(t - 6 * DAY, 6 * DAY, [24.0, 23.5, 25.1, 24.4, 23.9], kind="page")
        + transmissions(t + 600, 29 * 3600, [18.2, 19.0, 18.8, 17.9], kind="page", seed=8))
    put(s, "South 2", [ev("South 2", t, "sdr", "SDR 00000102 -> 00000104")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    assert [(f["metric"]) for f in found] == ["tx_q_open"]
    assert found[0]["text"].startswith("pages open at 18.5 dB (was 24.0, 4 pages)")


def test_a_slow_drift_over_a_week_is_warned_once_a_day_without_an_event(tmp_path):
    s, clock = store(tmp_path)
    hours = []
    for h in range(28 * 24):
        at = NOW - 28 * DAY + h * 3600
        hours.append({"k": "floor", "at": at, "q90": 5.0 if at < NOW - 7 * DAY else 9.0, "open_s": 0})
    put(s, "FG1", hours)
    s.maintain(NOW)
    ins = mi.Insight(s, clock=lambda: NOW, log=lambda m: None)
    found = ins.turn()
    assert [(f["metric"], f["event"]) for f in found] == [("idle_q90", "drift")]
    assert found[0]["text"] == "idle quieting +4.0 dB over 7 d (no event)"


# -- Health, the API, Settings ----------------------------------------------------------------------------------------

def test_the_warning_is_a_health_hint_and_muted_it_is_not_shown(tmp_path):
    from tests.test_cloud import HubRun, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app = hub.app
        now = time.time()
        t = now - 3 * 3600
        put(app.metric_store, "FG1", stepped(t, 5.0, 9.2, end=now))
        put(app.metric_store, "FG1", [ev("FG1", t, "sdr", "SDR 00000102 -> 00000105")])
        app.metric_insight.turn(now)
        hints = [h for h in app.health.model()["hints"] if h.get("kind") == "metrics_shift"]
        assert len(hints) == 1 and hints[0]["channel"] == "FG1" and "(SDR change)" in hints[0]["text"]
        app.cad.settings_post("alert_mutes", {"action": "mute", "key": "metrics_shift", "hours": 24, "operator": "KM"})
        assert not [h for h in app.health.model()["hints"] if h.get("kind") == "metrics_shift"]
    finally:
        hub.close()


def test_views_are_for_signed_in_admins_and_bounded(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    assert api.get("/api/cad/metrics", "view=squelch", Identity(email=CASEY))[0] == 404       # not the hub
    s, _ = store(tmp_path, now=time.time())
    now = time.time()
    put(s, "FG1", floor_minutes(now - 2 * DAY, 2 * 1440))
    cad.station_metrics = s
    cad.station_metrics_insight = mi.Insight(s, log=lambda m: None)
    for who in (None, Identity(email=""), Identity(email=JORDAN)):
        code, raw, _ = api.get("/api/cad/metrics", "view=squelch", who)
        assert code == 403 and b"FG1" not in raw and json.loads(raw)["error"].startswith("Admins only")
    code, raw, _ = api.get("/api/cad/metrics", "view=squelch", Identity(email=CASEY))
    out = json.loads(raw)
    assert code == 200 and [c["subject"] for c in out["channels"]] == ["FG1"] and out["channels"][0]["open"] == 10.0
    assert "0 real tx: too few to check a raise" in out["channels"][0]["why"]
    code, raw, _ = api.get("/api/cad/metrics", "view=spark", Identity(email=CASEY))
    sp = json.loads(raw)
    assert code == 200 and len(sp["subjects"][0]["level"]) == 48 and sp["bin_s"] == 1800
    assert api.get("/api/cad/metrics", "view=shifts", Identity(email=CASEY))[0] == 200
    assert api.get("/api/cad/metrics", "view=nope", Identity(email=CASEY))[0] == 400
    s.maintain()
    code, raw, _ = api.get("/api/cad/metrics", f"subject=FG1&res=hour&from={now - DAY}&to={now}&metric=idle_q90,sq_open",
                           Identity(email=CASEY))
    rows = json.loads(raw)["rows"]
    assert code == 200 and {r["metric"] for r in rows} == {"idle_q90", "sq_open"}
    assert {r["p50"] for r in rows if r["metric"] == "sq_open"} == {10.0}      # the Open as running, an hour at a time
    assert api.get("/api/cad/metrics", "subject=FG1&res=hour&metric=idle_q90,nope", Identity(email=CASEY))[0] == 400


def test_the_thresholds_are_a_settings_section_audited_as_radios(tmp_path):
    cad = make_cad(tmp_path)
    page = cad.settings.page()["signal_metrics"]
    assert page["margin_db"] == {"label": mi.LABELS["margin_db"], "value": 3.0, "default": 3.0, "edited": False,
                                 "min": 1.0, "max": 10.0}
    out = cad.settings_post("signal_metrics", {"action": "set", "key": "shift_db", "value": 4, "operator": "KM"})
    assert out["signal_metrics"]["shift_db"]["value"] == 4.0 and out["signal_metrics"]["shift_db"]["edited"]
    assert mi.settings(cad.settings.section("signal_metrics"))["shift_db"] == 4.0
    rows = cad.store.q("SELECT section, key, old, new FROM settings_audit WHERE key LIKE 'signal_metrics/%'")
    assert [(r["section"], r["key"], r["old"], r["new"]) for r in rows] == [("radios", "signal_metrics/shift_db", "3", "4")]
    with pytest.raises(Exception, match="shift_db: 1 to 15"):
        cad.settings_post("signal_metrics", {"action": "set", "key": "shift_db", "value": 40})
    with pytest.raises(Exception, match="key: one of"):
        cad.settings_post("signal_metrics", {"action": "set", "key": "nope", "value": 4})
    cad.settings_post("signal_metrics", {"action": "reset", "key": "shift_db", "operator": "KM"})
    assert cad.settings.section("signal_metrics") == {}
    n = len(cad.store.q("SELECT 1 FROM settings_audit"))
    cad.settings_post("signal_metrics", {"action": "reset", "key": "shift_db", "operator": "KM"})
    assert len(cad.store.q("SELECT 1 FROM settings_audit")) == n                  # nothing changed: nothing audited


# -- the review's shift scenarios (#685) ------------------------------------------------------------------------------

def test_one_burst_of_static_after_a_restart_is_not_a_noise_opens_shift(tmp_path):
    t = NOW - 4 * 3600
    s, _ = store(tmp_path)
    burst = lambda k, r: 10 if t + 600 <= t - DAY + 60 * k < t + 1800 else (1 if r.random() < 0.1 else 0)
    put(s, "FG1", floor_minutes(t - DAY, int((NOW - t + DAY) // 60), noise=burst))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    assert mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn() == []


def test_pages_are_never_blamed_on_a_later_event_than_the_one_that_moved_them(tmp_path):
    t1, t2 = NOW - 40 * 3600, NOW - 20 * 3600              # the splitter (sdr) at t1; a gain change that did nothing at t2
    s, _ = store(tmp_path)
    put(s, "South 2", floor_minutes(NOW - 7 * DAY, 7 * 1440, kind="paging"))
    put(s, "South 2", transmissions(NOW - 7 * DAY, 5 * DAY, [24.0, 23.5, 25.1, 24.4, 23.9, 24.2], kind="page")
        + transmissions(t1 + 600, t2 - t1 - 1200, [19.0, 18.5, 18.8, 19.2], kind="page", seed=8)
        + transmissions(t2 + 600, NOW - t2 - 600, [18.9, 19.1, 18.7, 19.0], kind="page", seed=9))
    put(s, "South 2", [ev("South 2", t1, "sdr", "SDR 00000102 -> 00000104"), ev("South 2", t2, "gain", "36.4 -> 38.6")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    assert [(f["metric"], f["event"]) for f in found] == [("tx_q_open", "sdr")]


def test_an_sdr_change_with_a_squelch_edit_minutes_later_is_still_checked(tmp_path):
    t = NOW - 3 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", stepped(t, 5.0, 9.2))
    put(s, "FG1", [ev("FG1", t, "sdr", "SDR 00000102 -> 00000105"),
                   ev("FG1", t + 300, "squelch", "open 11 close 7 min 0.3 (was open 10 close 6 min 0.3)")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    assert [f["metric"] for f in found] == ["idle_q90"] and found[0]["text"].endswith("(SDR change, squelch change)")


def test_a_traffic_channel_whose_transmissions_come_in_weaker_is_warned(tmp_path):
    t = NOW - 30 * 3600                                    # a cable swapped: every carrier ~7 dB weaker since
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(t - DAY, int((NOW - t + DAY) // 60)))
    put(s, "FG1", transmissions(t - DAY, DAY, [22.0 + (k % 5) * 0.4 for k in range(40)])
        + transmissions(t + 600, NOW - t - 600, [15.0 + (k % 5) * 0.4 for k in range(40)], seed=7))
    put(s, "FG1", [ev("FG1", t, "back", "SDR 00000102")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    assert [f["metric"] for f in found] == ["tx_q"]
    assert found[0]["text"].startswith("transmissions at 15.") and "(was 22." in found[0]["text"]


def test_since_says_the_day_for_an_event_before_today():
    from sizeup import tz
    assert mi._when(NOW - 60, NOW) == tz.hhmm(NOW - 60)
    old = NOW - 2 * DAY
    assert mi._when(old, NOW) == f"{tz.clock(old, '%b')} {int(tz.clock(old, '%d'))} {tz.hhmm(old)}"
    assert mi._when(1798967100.0, NOW) == "Jan 3 04:05"                # the day without its 0, the hour with it


def test_apply_is_refused_unless_the_squelch_it_was_for_still_runs_and_a_reset_forgets_it(tmp_path):
    from sizeup.app import App
    from sizeup.cad.store import CadError
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    forgot = []
    app.cad.station_metrics_insight = type("I", (), {"forget": lambda self, k: forgot.append(k)})()
    body = {"action": "set", "key": "FG1", "open_db": 13, "close_db": 9, "operator": "admin@example.org"}
    with pytest.raises(CadError, match="FG1 now runs Open 10.0 / Close 6.0"):
        app.cad.settings.post("squelch", {**body, "expect": {"open_db": 12, "close_db": 8}})
    assert app._receiver("FG1").open_db == 10.0
    app.cad.settings.post("squelch", {**body, "expect": {"open_db": 10, "close_db": 6}})
    assert app._receiver("FG1").open_db == 13.0 and forgot == ["FG1"]
    app.cad.settings.post("squelch", {"action": "reset", "key": "FG1", "operator": "admin@example.org"})
    assert forgot == ["FG1", "FG1"]


def test_a_renamed_station_keys_its_suggestions_and_shifts_under_its_new_id(tmp_path):
    t = NOW - 3 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", stepped(t, 5.0, 9.2), station="old-id")
    put(s, "FG1", [ev("FG1", t, "sdr", "SDR 00000102 -> 00000105")], station="old-id")
    ins = mi.Insight(s, clock=lambda: NOW, log=lambda m: None)
    assert [f["station"] for f in ins.turn()] == ["old-id"]
    assert ins.squelch(NOW)["channels"][0]["station"] == "old-id"
    assert s.rename_station("old-id", "new-id") > 0
    ins.renamed("old-id", "new-id")
    assert [f["station"] for f in ins.found] == ["new-id"]                          # the hint follows at once
    assert [c["station"] for c in ins.squelch(NOW)["channels"]] == ["new-id"]       # looked up again under the new id
    assert [f["station"] for f in ins.turn()] == ["new-id"]


# -- the re-review's follow-ups (#686) --------------------------------------------------------------------------------

def _handed_over(tmp_path, evs):
    """FG1 on a shared dongle: 7 days of floor and traffic, with `evs` (each [at, ev, detail])."""
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212))
    put(s, "FG1", [ev("FG1", t, e, d) for t, e, d in evs])
    return s, mi.Insight(s, clock=lambda: NOW, log=lambda m: None)


def test_a_channel_handed_over_every_few_hours_on_the_same_dongle_still_gets_a_suggestion(tmp_path):
    # shared dongles: Off and back on every 8 h (the station sends "on" each time), and an unplug / replug of the same
    # dongle: what it hears never changed, so the window is the whole 7 days
    evs = []
    for k in range(1, 21):
        t = NOW - k * 8 * 3600
        evs += [[t - 1800, "off", ""], [t, "on", "SDR 00000102"]]
    evs += [[NOW - 5 * 3600, "unplug", "usb gone"], [NOW - 5 * 3600 + 30, "back", "SDR 00000102"]]
    _s, ins = _handed_over(tmp_path, evs)
    out = ins.suggestion("pcfd-hq", "FG1")
    assert out["state"] == "ok" and out["since"] is None and out["days"] == 7 and out["apply"]


def test_back_on_another_dongle_starts_the_window_again_even_without_its_sdr_event(tmp_path):
    t = NOW - 20 * 3600
    _s, ins = _handed_over(tmp_path, [[NOW - 3 * DAY, "on", "SDR 00000102"], [t, "on", "SDR 00000105"]])
    out = ins.suggestion("pcfd-hq", "FG1")
    assert out["state"] == "short" and out["since"] == {"at": t, "ev": "on"}
    assert out["why"].startswith("not enough data since dongle assigned ")
    # the station's own "sdr" for it (and the same dongle handed back after): from the SDR change
    evs = [{"ev": "sdr", "detail": "SDR 00000102 -> 00000105", "at": 1.0}, {"ev": "on", "detail": "SDR 00000105", "at": 2.0},
           {"ev": "back", "detail": "SDR 00000105", "at": 3.0}, {"ev": "unplug", "detail": "", "at": 4.0},
           {"ev": "gain", "detail": "36.4 -> 42.1", "at": 5.0}, {"ev": "back", "detail": "SDR 00000107", "at": 6.0}]
    assert [e["at"] for e in mi.rf_events(evs)] == [1.0, 5.0, 6.0]


def _portables(t, quiet_after=False):
    """FG1's kept voice: ~22 dB the week before t; after it a working fire's hour of portables at ~12 dB (100 of
    them), and (unless quiet_after) the usual traffic at ~22 dB every 20 min for a day."""
    before = transmissions(t - 7 * DAY + 600, 7 * DAY - 1200, [22.0 + (k % 5) * 0.4 for k in range(504)])
    fire = transmissions(t + 900, 3600, [12.0 + (k % 5) * 0.4 for k in range(100)], seed=4)
    usual = [] if quiet_after else transmissions(t + 600, DAY, [22.0 + (k % 5) * 0.4 for k in range(72)], seed=6)
    return before, fire + usual


def test_a_burst_of_portables_after_an_event_is_not_a_weaker_level(tmp_path):
    t = NOW - 2 * DAY
    before, after = _portables(t)
    # the median of every transmission after would be the fire's portables (100 of 172): a cable's loss, wrongly
    assert mi.median([d["q"] for d in after]) < 14.0
    assert mi.check_levels(before, after, CFG, False) == []                # half-hour medians: the fire is 2 of ~50
    weaker = [dict(d, q=d["q"] - 7) for d in after]                        # a real loss on every carrier still shows
    assert [h["metric"] for h in mi.check_levels(before, weaker, CFG, False)] == ["tx_q"]


def test_a_restart_alone_with_every_carrier_weaker_is_warned_on_traffic_and_paging(tmp_path):
    # powered down, a splitter added (every carrier ~7 dB weaker), booted: the station's first look after a restart is
    # never an event, so the restart is all there is to blame, and the level check runs after it
    t = NOW - 30 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(t - DAY, int((NOW - t + DAY) // 60)))
    put(s, "FG1", transmissions(t - DAY, DAY, [22.0 + (k % 5) * 0.4 for k in range(40)])
        + transmissions(t + 600, NOW - t - 600, [15.0 + (k % 5) * 0.4 for k in range(40)], seed=7))
    put(s, "South 2", floor_minutes(t - 7 * DAY, int((NOW - t + 7 * DAY) // 60), kind="paging"))
    put(s, "South 2", transmissions(t - 5 * DAY, 4 * DAY, [24.0, 23.5, 25.1, 24.4, 23.9, 24.2], kind="page")
        + transmissions(t + 600, NOW - t - 600, [17.0, 16.5, 16.8, 17.2], kind="page", seed=8))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    found = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).turn()
    got = {(f["subject"], f["metric"], f["event"]) for f in found}
    assert ("FG1", "tx_q", "start") in got and ("South 2", "tx_q_open", "start") in got
    assert next(f for f in found if f["metric"] == "tx_q")["text"].endswith("(station restart)")


def test_too_little_traffic_after_an_event_says_the_level_was_not_checked(tmp_path):
    # a restart, then only one fire's hour of portables (100 tx, but in two half hours, never 3): neither
    # a warning nor silence, an info hint with why on hover
    t = NOW - 6 * 3600
    before, _ = _portables(t)
    after = transmissions(t + 1800, 3500, [12.0 + (k % 5) * 0.4 for k in range(100)], seed=4)   # two half hours
    assert mi.median([d["q"] for d in after]) < 14.0                     # the plain median would have warned
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(t - DAY, int((NOW - t + DAY) // 60)))
    put(s, "FG1", before + after)
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    ins = mi.Insight(s, clock=lambda: NOW, log=lambda m: None)
    found = ins.turn()
    assert [f["metric"] for f in found] == ["tx_q_unchecked"]
    h = ins.hints()[0]
    assert h["level"] == "info" and h["text"].startswith("level not checked: too little traffic since ")
    assert h["title"].startswith("kept voice in ") and "needs 3 each" in h["title"]


def test_apply_is_checked_against_the_suggestion_itself_on_the_hub(tmp_path):
    s, ins = _handed_over(tmp_path, [])
    assert ins.suggestion("pcfd-hq", "FG1")["apply"]
    was, to = {"open_db": 10.0, "close_db": 6.0}, {"open_db": 13.0, "close_db": 9.0}
    assert ins.applicable("FG1", was, to) == ""
    assert ins.applicable("FG1", {"open_db": 11.0, "close_db": 7.0}, to).startswith(
        "FG1: the suggestion was worked out for Open 10.0 / Close 6.0")
    # a stale target: the confirm left open while the suggestion moved (here: 13 now, the dialog still says 12)
    assert ins.applicable("FG1", was, {"open_db": 12.0, "close_db": 8.0}) == \
        "FG1: the suggestion is now Open 13.0 / Close 9.0, look again"
    assert ins.applicable("FG1", was, None).startswith("FG1: the suggestion is now Open 13.0")
    assert ins.applicable("FG9", was, to) == "FG9: no suggestion now, look again"
    put(s, "FG1", [ev("FG1", NOW - 3600, "squelch", "open 13 close 9 min 0.3 (was open 10 close 6 min 0.3)")])
    assert ins.applicable("FG1", was, to).startswith(
        "FG1: the suggestion isn't offered now (settling: squelch changed ")


def test_the_settings_edit_is_refused_when_the_hub_says_the_suggestion_no_longer_applies(tmp_path):
    from sizeup.app import App
    from sizeup.cad.store import CadError
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    asked = []
    app.cad.station_metrics_insight = type("I", (), {
        "forget": lambda self, k: None,
        "applicable": lambda self, k, exp, to: asked.append((k, exp, to)) or "FG1: the suggestion isn't offered now"})()
    body = {"action": "set", "key": "FG1", "open_db": 13, "close_db": 9, "operator": "admin@example.org",
            "expect": {"open_db": 10, "close_db": 6}}
    with pytest.raises(CadError, match="the suggestion isn't offered now"):
        app.cad.settings.post("squelch", body)
    assert app._receiver("FG1").open_db == 10.0
    assert asked == [("FG1", {"open_db": 10, "close_db": 6}, {"open_db": 13, "close_db": 9})]
    app.cad.settings.post("squelch", {k: v for k, v in body.items() if k != "expect"})   # a typed edit: never asked
    assert app._receiver("FG1").open_db == 13.0 and len(asked) == 1


def test_a_station_with_no_floor_data_in_the_window_never_stands_beside_the_one_that_reports_it(tmp_path):
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440))
    put(s, "FG1", floor_minutes(NOW - 20 * DAY, 1440), station="old-id")         # a station id from 3 weeks ago
    ins = mi.Insight(s, clock=lambda: NOW, log=lambda m: None)
    assert [(c["station"], c["subject"]) for c in ins.squelch(NOW)["channels"]] == [("pcfd-hq", "FG1")]


def test_a_squelch_put_back_at_a_restart_is_no_squelch_change(tmp_path):
    # the station ran 13 / 9 (the hub's) before a restart; one that came up on 10 / 6 (its store lost the hub's) and
    # took the hub's push 20 s later logs "squelch" -> 13 / 9: not an edit (no 6 h of settling, no shift excuse)
    t = NOW - 2 * 3600
    s, _ = store(tmp_path)
    run = {"kind": "traffic", "open": 13.0, "close": 9.0, "min": 0.3, "sdr": "00000102", "gain": 36.4, "state": "ok"}
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440, settings=run))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [24.0] * 212))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    put(s, "FG1", [ev("FG1", t + 20, "squelch", "open 13 close 9 min 0.3 (was open 10 close 6 min 0.3)")])
    ins = mi.Insight(s, clock=lambda: NOW, log=lambda m: None)
    out = ins.suggestion("pcfd-hq", "FG1")
    assert "settling" not in out["warn"] and out["state"] == "ok"
    assert [e["ev"] for e in ins._events("pcfd-hq", "FG1", t - 60, NOW)] == ["start"]
    # a squelch that really changed at the restart (the hub's edit while the station was down) stays one
    s2, _ = store(tmp_path / "edit")
    put(s2, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440, settings=dict(run, open=10.0, close=6.0)))
    put(s2, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [24.0] * 212))
    put(s2, "station", [ev("station", t, "start", "sizeup started")])
    put(s2, "FG1", [ev("FG1", t + 20, "squelch", "open 13 close 9 min 0.3 (was open 10 close 6 min 0.3)")])
    out = mi.Insight(s2, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")
    assert out["warn"].startswith("settling: squelch changed")


def test_the_station_logs_no_squelch_change_at_a_restart_when_it_runs_the_hubs_from_its_first_look():
    # the station keeps the hub's squelch in its own Settings and runs it before the sampler's first look: the hub's
    # push after a restart changes nothing, and the first look itself is never an event
    from sizeup.cloud.metrics import Collector, Sampler
    c = Collector(clock=lambda: NOW)
    smp = Sampler(type("A", (), {"receivers": []})(), c, clock=lambda: NOW, log=lambda m: None)
    cur = {"state": "ok", "detail": "", "restarts": 0, "sdr": "00000102", "gain": 36.4, "squelch": (13.0, 9.0, 0.3)}
    smp._changed("FG1", dict(cur), NOW)                 # the first look after the start: 13 / 9 already
    smp._changed("FG1", dict(cur), NOW + 5)             # the hub's push of the same 13 / 9: nothing moved
    assert [r["ev"] for _s, r in c._q if r["k"] == "ev"] == ["start"]


def test_a_dongle_or_gain_changed_while_the_station_was_down_starts_the_window_at_its_restart(tmp_path):
    t = NOW - 20 * 3600
    for k, (key, after) in enumerate((("sdr", "00000105"), ("gain", 42.1))):
        s, _ = store(tmp_path / str(k))
        base = {"kind": "traffic", "open": 10.0, "close": 6.0, "min": 0.3, "sdr": "00000102", "gain": 36.4, "state": "ok"}
        put(s, "FG1", floor_minutes(NOW - 7 * DAY, int((t - 1800 - NOW + 7 * DAY) // 60), settings=base))
        put(s, "FG1", floor_minutes(t + 60, int((NOW - t - 60) // 60), settings=dict(base, **{key: after}), seed=4))
        put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212))
        put(s, "station", [ev("station", t, "start", "sizeup started")])
        out = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")
        assert out["state"] == "short" and out["since"] == {"at": t, "ev": "start"}, key
        assert out["why"].startswith("not enough data since station restart ")
    # a restart that changed neither (a deploy): the whole week
    s, _ = store(tmp_path / "same")
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    out = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")
    assert out["state"] == "ok" and out["since"] is None


def test_on_or_back_with_no_serial_to_tell_dongles_apart_still_starts_the_window(tmp_path):
    t = NOW - 20 * 3600
    for detail in ("", "SDR 0"):                       # (no serial: the station names the configured "" or index)
        _s, ins = _handed_over(tmp_path / (detail or "none"), [[t, "on", detail]])
        out = ins.suggestion("pcfd-hq", "FG1")
        assert out["state"] == "short" and out["since"] == {"at": t, "ev": "on"}, detail
    # the first "on" in the window: compared with the dongle the floor records name before it (00000102)
    _s, ins = _handed_over(tmp_path / "first", [[t, "on", "SDR 00000105"]])
    assert ins.suggestion("pcfd-hq", "FG1")["since"] == {"at": t, "ev": "on"}
    _s, ins = _handed_over(tmp_path / "same", [[t, "on", "SDR 00000102"]])
    assert ins.suggestion("pcfd-hq", "FG1")["since"] is None
    # with nothing to compare it with (no floor records name one): counted
    assert [e["at"] for e in mi.rf_events([{"ev": "on", "detail": "SDR 00000105", "at": 2.0}])] == [2.0]


def test_a_hub_edit_made_while_the_station_was_down_still_settles(tmp_path):
    # the Settings audit's squelch edit at 13 / 9 during the downtime, the station's echo of it after the restart: an
    # edit (the audit row is never an echo), so 6 h of settling
    t = NOW - 2 * 3600
    audit = lambda subject, a, b: [{"at": t - 1200, "operator": "Casey", "key": f"squelch/{subject}", "old": "{}",
                                    "new": "{'open_db': 13}"}] if a <= t - 1200 < b else []
    s, _ = store(tmp_path, audit=audit)
    run = {"kind": "traffic", "open": 10.0, "close": 6.0, "min": 0.3, "sdr": "00000102", "gain": 36.4, "state": "ok"}
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440, settings=run))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [24.0] * 212))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    put(s, "FG1", [ev("FG1", t + 20, "squelch", "open 13 close 9 min 0.3 (was open 10 close 6 min 0.3)")])
    out = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")
    assert out["warn"].startswith("settling: squelch changed")


def test_a_squelch_event_more_than_five_minutes_after_the_restart_is_kept(tmp_path):
    t = NOW - 2 * 3600
    s, _ = store(tmp_path)
    run = {"kind": "traffic", "open": 13.0, "close": 9.0, "min": 0.3, "sdr": "00000102", "gain": 36.4, "state": "ok"}
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, 7 * 1440, settings=run))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [24.0] * 212))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    put(s, "FG1", [ev("FG1", t + mi.SETTLE_S + 60, "squelch", "open 13 close 9 min 0.3 (was open 10 close 6 min 0.3)")])
    ins = mi.Insight(s, clock=lambda: NOW, log=lambda m: None)
    assert [e["ev"] for e in ins._events("pcfd-hq", "FG1", t - 60, NOW)] == ["start", "squelch"]
    assert ins.suggestion("pcfd-hq", "FG1")["warn"].startswith("settling: squelch changed")


BASE = {"kind": "traffic", "open": 10.0, "close": 6.0, "min": 0.3, "sdr": "00000102", "gain": 36.4, "state": "ok"}


def test_a_restart_whose_channel_runs_hours_later_is_still_compared_with_what_ran_before(tmp_path):
    # the gain changed in the config, the station restarted, and FG1 came up 2 h later (its shared dongle held
    # elsewhere, absent, auto-assign): its first floor settings after the restart are what it is compared with
    t = NOW - 20 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, int((t - 1800 - NOW + 7 * DAY) // 60), settings=BASE))
    put(s, "FG1", floor_minutes(t + 2 * 3600, int((NOW - t - 2 * 3600) // 60), settings=dict(BASE, gain=42.1), seed=4))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    out = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")
    assert out["since"] == {"at": t, "ev": "start"} and out["state"] == "short"


def test_back_from_off_on_the_same_dongle_with_another_gain_starts_the_window(tmp_path):
    t = NOW - 20 * 3600
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, int((t - 1800 - NOW + 7 * DAY) // 60), settings=BASE))
    put(s, "FG1", floor_minutes(t + 60, int((NOW - t - 60) // 60), settings=dict(BASE, gain=42.1), seed=4))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212))
    put(s, "FG1", [ev("FG1", t - 1800, "off", ""), ev("FG1", t, "on", "SDR 00000102")])
    out = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")
    assert out["since"] == {"at": t, "ev": "on"}


def test_a_restart_after_a_long_outage_on_a_new_dongle_never_mixes_in_the_old_dongles_days(tmp_path):
    t = NOW - 30 * 3600                                  # down 3 days, back on another dongle
    s, _ = store(tmp_path)
    put(s, "FG1", floor_minutes(NOW - 7 * DAY, int((t - 3 * DAY - NOW + 7 * DAY) // 60), settings=BASE))
    put(s, "FG1", floor_minutes(t + 60, int((NOW - t - 60) // 60), settings=dict(BASE, sdr="00000105"), seed=4))
    put(s, "FG1", transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212))
    put(s, "station", [ev("station", t, "start", "sizeup started")])
    out = mi.Insight(s, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")
    assert out["since"] == {"at": t, "ev": "start"} and out["days"] == 1.2
    # nothing known before it at all (down longer than the window), settings after: counted too
    s2, _ = store(tmp_path / "none")
    put(s2, "FG1", floor_minutes(t + 60, int((NOW - t - 60) // 60), settings=dict(BASE, sdr="00000105"), seed=4))
    put(s2, "station", [ev("station", t, "start", "sizeup started")])
    assert mi.Insight(s2, clock=lambda: NOW, log=lambda m: None).suggestion("pcfd-hq", "FG1")["since"] == \
        {"at": t, "ev": "start"}
