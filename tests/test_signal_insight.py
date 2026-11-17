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
    assert out["close"] == 9.0                                       # the running 4 dB of hysteresis kept
    assert out["under"] == 0 and out["real"] == 212 and not out["capped"]
    assert out["apply"] and out["info"] == ""                       # a traffic channel: an admin may apply it
    assert out["why"].startswith(f"noise p90 {noise:.1f} dB (10,080 idle min, 7 d) + 3 → 13 · 0/212 real under 13")
    assert sum(out["hist"]["idle"]["n"]) == 10080 and sum(out["hist"]["real"]["n"]) == 212


def test_it_is_never_above_the_weak_traffic_the_channel_really_carries():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440)
    r = random.Random(4)
    weak = [r.uniform(12.0, 12.6) for _ in range(30)]               # the weakest real key-ups: 12 dB at open
    qos = weak + [r.uniform(15, 30) for _ in range(270)]
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, qos), CFG, NOW)
    assert out["capped"] and "would miss weak traffic: 30 of 300 under" in out["why"]
    assert out["open"] < min(weak) and out["open"] == math.floor((mi.pct(qos, 2) - 1.0) * 2) / 2 == 11.0
    assert out["under"] == 0                                          # none of the real traffic is under it
    # an admin who allows 10% (Settings): those 30 of 300 are within it, so it isn't capped, and says so
    out = mi.suggest(floor, transmissions(NOW - 7 * DAY, 7 * DAY, qos), {**CFG, "miss_pct": 10.0}, NOW)
    assert out["open"] == 13.0 and not out["capped"] and out["under"] == 30 and "30/300 real under 13" in out["why"]


def test_under_a_day_of_data_is_not_enough():
    floor = floor_minutes(NOW - DAY, 1000)
    out = mi.suggest(floor, transmissions(NOW - DAY, DAY, [20.0] * 50), CFG, NOW)
    assert out["state"] == "short" and "open" not in out and not out["apply"]
    assert out["why"] == "not enough data: 17 h of 24 h"
    assert mi.suggest([], [], CFG, NOW)["state"] == "short"          # nothing at all: the same, no error


def test_paging_is_information_only_and_never_raised():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(9.5, 0.4), kind="paging")
    pages = transmissions(NOW - 7 * DAY, 7 * DAY, [random.Random(k).uniform(18, 26) for k in range(40)], kind="page")
    out = mi.suggest(floor, pages, CFG, NOW)
    assert out["state"] == "ok" and out["info"] == "paging: read-only" and not out["apply"]
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
