"""The push sources' text read late (sizeup/intel/engine.py late_cause, late_row; counters' late_*): counted once per row
by what made it (the engine, a catch-up, an outage), the ones whose words would have pushed apart, on Settings >
Station > Health, kept across a restart. Invented rows only."""

from __future__ import annotations

import pytest

from sizeup.intel import counters as C
from sizeup.intel.engine import LATE_S, late_cause, late_row
from tests.test_intel import Clock, ctx_row, engine, example_locate, live, row  # noqa: F401 - live: a fixture

QUIET = "car 3 back in service"
KNIFE = "respond to 100 Example Ave, male with a knife"


@pytest.mark.parametrize("r, want", [
    ({"model": "aws-transcribe"}, "aws"),
    ({"model": "large-v3-turbo (GPU worker-1)"}, "gpu"),
    ({"model": "large-v3 (station pool)"}, "station_pool"),
    ({"model": "whisper"}, "station"),
    ({"model": "gpu-worker"}, "other"),                                   # pending
    ({"model": ""}, "other"),
    ({"model": "whisper", "alt_model": "aws-transcribe"}, "station"),    # a late AWS answer kept apart: never this text
    ({"model": "whisper", "rerun_result": "ok; catch-up try 1"}, "catchup"),
    ({"model": "aws-transcribe", "rerun_result": "ok; catch-up try 1", "late": 1}, "outage"),
])
def test_the_cause_from_the_model_label_without_the_925_columns(r, want):
    assert late_cause(r) == want


@pytest.mark.parametrize("r, want", [
    ({"stt_engine": "aws", "stt_catchup": 0, "model": "whisper"}, "aws"),
    ({"stt_engine": "gpu", "stt_catchup": 0, "model": ""}, "gpu"),
    ({"stt_engine": "station_pool", "stt_catchup": 0}, "station_pool"),
    ({"stt_engine": "station", "stt_catchup": 0}, "station"),
    ({"stt_engine": "station", "stt_catchup": 0, "rerun_result": "ok; catch-up try 1"}, "station"),   # the column says
    ({"stt_engine": "aws", "stt_catchup": 1}, "catchup"),
    ({"stt_engine": "aws", "stt_catchup": 1, "late": 1}, "outage"),
    ({"stt_engine": "", "stt_catchup": 0, "model": "aws-transcribe"}, "aws"),      # pending there: the label
])
def test_the_cause_from_the_925_columns(r, want):
    assert late_cause(r) == want


def test_late_push_source_rows_are_counted_by_cause_and_the_lost_ones_apart():
    t = 5000.0
    clock = Clock(t + LATE_S + 60)
    e, sink = engine(clock=clock)
    e.feed(row("a", QUIET, t, model="whisper"))
    e.feed(row("b", KNIFE, t + 5, model="whisper"))                     # would have pushed
    e.feed(row("c", "car 4 en route", t + 9, model="whisper", rerun_result="ok; catch-up try 1"))
    e.feed(row("d", "car 5 on location", clock.t - 10, model="aws-transcribe"))     # in time: not late
    e.feed(row("b", KNIFE + ", units respond", t + 5, model="whisper"))   # a re-text: the same row, once
    e.feed(row("x", KNIFE, t, service="transit", model="whisper"))     # no pack reads it
    c = e.counts(clock.t)
    h = c["all"]["h24"]
    assert (h["late"], h["late_station"], h["late_catchup"], h["late_lost"]) == (3, 2, 1, 1)
    assert c["late"]["text"] == "24 h: 3 (station 2, catch-up 1) · would have pushed: 1" and c["late"]["label"] == "Late PD text"
    assert c["late"]["level"] == "warn" and "AWS budget" in c["late"]["title"] and "Auto · priority" in c["late"]["title"]
    pol = next(p for p in c["packs"] if p["pack"] == "police")
    assert pol["h24"]["late"] == 3


def test_none_late_is_a_quiet_ok_line():
    e, _sink = engine()
    c = e.counts()
    assert c["late"]["text"] == "24 h: 0" and c["late"]["level"] == "ok"


def test_a_pack_that_cannot_push_counts_nothing():
    t = 5000.0
    e, _sink = engine({"intel_packs": {"police": {"mode": "map"}}}, clock=Clock(t + LATE_S + 60))
    e.feed(row("b", KNIFE, t, model="whisper"))
    assert e.counts()["all"]["h24"]["late"] == 0


def test_the_late_counts_are_kept_across_a_restart():
    t = 5000.0
    clock = Clock(t + LATE_S + 60)
    e, _sink = engine(clock=clock)
    e.feed(row("b", KNIFE, t, model="aws-transcribe"))
    kept = e.counters.state(clock.t, ["police"])
    e2, _ = engine(clock=clock)
    e2.counters.restore(kept, clock.t, ["police"])
    h = e2.counts(clock.t)["all"]["h24"]
    assert (h["late"], h["late_aws"], h["late_lost"]) == (1, 1, 1) and set(C.KEYS) <= set(h)


def test_live_the_cause_from_the_columns_when_the_database_has_them(live, tmp_path):
    from tests.test_cad import make_cad
    cad, n = live
    assert "stt_engine" not in cad.intel._row_sql()                     # absent: the label
    cad.comms.record(ctx_row("c1-t0", KNIFE, cad.clock() - LATE_S - 60))
    cad.intel.drain()
    assert cad.intel.counts()["all"]["h24"]["late_station"] == 1        # ctx_row's model: "whisper"
    cad2 = make_cad(tmp_path / "925")
    cad2.intel.notifier, cad2.intel.locate = n, example_locate
    with cad2.store.lock:
        cad2.store.db.execute("ALTER TABLE radio_traffic ADD COLUMN stt_engine TEXT NOT NULL DEFAULT ''")
        cad2.store.db.execute("ALTER TABLE radio_traffic ADD COLUMN stt_catchup INTEGER NOT NULL DEFAULT 0")
    cad2.comms.record(ctx_row("c2-t0", KNIFE, cad2.clock() - LATE_S - 60))
    cad2.comms.record(ctx_row("c3-t0", QUIET, cad2.clock() - LATE_S - 50))
    with cad2.store.lock:
        cad2.store.db.execute("UPDATE radio_traffic SET stt_engine = 'gpu' WHERE id = 'c2-t0'")
        cad2.store.db.execute("UPDATE radio_traffic SET stt_engine = 'aws', stt_catchup = 1 WHERE id = 'c3-t0'")
    cad2.intel.drain()
    h = cad2.intel.counts()["all"]["h24"]
    assert (h["late"], h["late_gpu"], h["late_catchup"], h["late_lost"]) == (2, 1, 1, 1) and n.sent == []


def test_the_hubs_health_panel_shows_the_late_line(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    app = HubApp(hub_cfg(tmp_path), log=lambda *a: None)
    try:
        [late] = [r for r in app.health.panel()["intel"] if r["pack"] == "late"]
        assert late["text"] == "24 h: 0" and late["level"] == "ok" and late["title"]
    finally:
        app.stop()


# -- #932 review ------------------------------------------------------------------------------------------------------

def test_a_late_follow_up_of_a_call_already_pushed_is_no_lost_pre_alert():
    t = 5000.0
    clock = Clock(t + 5)
    e, sink = engine(clock=clock)
    e.feed(row("k1", KNIFE, t, model="whisper"))                       # in time: pushed
    assert sum(d.would_push for _e, d in sink.pushes) == 1
    clock.t = t + LATE_S + 120
    e.feed(row("k2", "male with a knife still at 100 Example Ave", t + 30, model="whisper"))   # read late
    c = e.counts(clock.t)
    assert c["all"]["h24"]["late"] == 1 and c["all"]["h24"]["late_lost"] == 0 and c["late"]["level"] == "ok"


def test_a_late_row_held_on_time_too_is_no_lost_pre_alert():
    t = 5000.0
    e, sink = engine({"intel": {"push": False}}, clock=Clock(t + LATE_S + 60))     # pushes off: never one on time
    e.feed(row("k", KNIFE, t, model="whisper"))
    assert e.counts()["all"]["h24"]["late_lost"] == 0


def test_a_catch_up_older_than_max_age_is_a_catch_up_never_an_outage():
    t = 50_000.0
    e, sink = engine(clock=Clock(t + 3600))                            # an hour old: past max_age_min
    e.feed(row("c", QUIET, t, model="whisper", rerun_result="ok; catch-up try 1"))
    e.feed(row("s", "car 6 on location", t + 5, model="whisper"))
    h = e.counts()["all"]["h24"]
    assert (h["late"], h["late_catchup"], h["late_station"], h["late_outage"]) == (2, 1, 1, 0)
    e.feed(row("o", "car 8 on location", t + 9, model="whisper", late=1))         # radio_traffic.late: the outage
    assert e.counts()["all"]["h24"]["late_outage"] == 1


def test_a_retext_after_the_window_prunes_and_a_rerecord_after_a_restart_count_once():
    t = 50_000.0
    clock = Clock(t + LATE_S + 60)
    e, sink = engine(clock=clock)
    e.feed(row("a", QUIET, t, model="whisper"))
    e.windows.clear()                                                  # the window let it go
    e.feed(row("a", QUIET + " now", t, model="whisper"))               # a re-text
    assert e.counts()["all"]["h24"]["late"] == 1
    kept = e.counters.state(clock.t, ["police"])
    e2, _ = engine(clock=clock)                                        # a restart
    e2.counters.restore(kept, clock.t, ["police"])
    e2.feed(row("a", QUIET + " now", t, model="whisper"))              # re-recorded
    assert e2.counts()["all"]["h24"]["late"] == 1


def test_the_line_is_hidden_when_nothing_could_push():
    e, _ = engine({"intel": {"enabled": False}})
    assert e.counts()["late"] is None
    off = {"intel_packs": {p: {"mode": "map"} for p in ("police", "county_police", "ems", "fire", "neighbor_police",
                                                        "highway")}}
    e2, _ = engine(off)
    assert e2.counts()["late"] is None


def test_the_hover_names_each_packs_late_s_and_no_issue_numbers():
    e, _ = engine()
    title = e.counts()["late"]["title"]
    assert "PD 5 min" in title and f"{e.pack('highway').label} 10 min" in title and "#" not in title
