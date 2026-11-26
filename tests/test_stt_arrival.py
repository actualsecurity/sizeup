"""#677: the STT input lane's priority (the pre-alert channels' first, dropped last) and each STT input's arrival on the
hub after its event (p50 / p95 per channel and kind, on Health and as a station-metrics series). Synthetic data only."""

from __future__ import annotations

import sqlite3
import threading
import time

import numpy as np
import pytest

from sizeup.cloud import sttarrival, sttinput
from sizeup.cloud.metricstore import MetricStore
from sizeup.cloud.outbox import Outbox
from sizeup.incidents import Traffic
from sizeup.radio.fm import RATE
from tests.test_app import voice, wait
from tests.test_cloud import HubRun, call, hub_cfg
from tests.test_stt_encode import paused, resume
from tests.test_stt_input import FakeHub, _event, _item, _transmission, offline_station, stt_rows

PRE, FIRE, NORMAL, LOW = sttinput.PRE, sttinput.FIRE, sttinput.NORMAL, sttinput.LOW


# -- the station: priority in the lane --------------------------------------------------------------------------------


def test_the_outbox_sends_the_pre_alert_channels_first_and_drops_the_lowest_first(tmp_path):
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=10_000)                 # STT inputs' own budget: 3000
    ob.add_stt({"id": "tg-ctx-1"}, b"l" * 500, {}, prio=LOW)
    ob.add_stt({"id": "FG1-1"}, b"n" * 500, {}, prio=NORMAL)
    ob.add_stt({"id": "South2-1"}, b"p" * 500, {}, prio=PRE)
    ob.add_stt({"id": "FG1-2"}, b"n" * 500, {})                                # (NORMAL by default)
    ob.add_stt({"id": "PD-1"}, b"p" * 500, {}, prio=PRE)
    rows = ob.stt_batch()
    assert [r["data"]["id"] for r in rows] == ["South2-1", "PD-1", "FG1-1", "FG1-2", "tg-ctx-1"]
    assert [r["prio"] for r in rows] == [PRE, PRE, NORMAL, NORMAL, LOW]
    assert [r["data"]["id"] for r in ob.stt_batch(max_bytes=900)] == ["South2-1"]   # a small batch: the pre-alert first
    # over their budget: the lowest first (the context talkgroup), then the oldest of the next
    ob.add_stt({"id": "FG1-3"}, b"n" * 500, {})
    assert ob.dropped_stt == 0
    ob.add_stt({"id": "FG1-4"}, b"n" * 500, {})
    assert [r["data"]["id"] for r in ob.stt_batch()][-1] != "tg-ctx-1" and ob.dropped_stt == 1
    ob.add_stt({"id": "South2-2"}, b"p" * 500, {}, prio=PRE)
    ids = [r["data"]["id"] for r in ob.stt_batch()]
    assert ids == ["South2-1", "PD-1", "South2-2", "FG1-2", "FG1-3", "FG1-4"] and ob.dropped_stt == 2   # FG1-1 went
    assert ob.dropped_stt_pre == 0
    # only once nothing lower is left does a pre-alert one go (the oldest), counted as such
    for k in range(6):
        ob.add_stt({"id": f"PD-x{k}"}, b"p" * 500, {}, prio=PRE)
    assert all(r["prio"] == PRE for r in ob.stt_batch()) and ob.dropped_stt_pre == 3
    assert [r["data"]["id"] for r in ob.stt_batch()][0] == "PD-x0"
    # the events are untouched
    assert ob.pending() == (0, None) and ob.stt_bytes <= 3000


def test_an_stt_lane_from_before_priorities_opens_and_its_rows_are_other_traffic(tmp_path):
    p = tmp_path / "o.sqlite"
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE stt_inputs (n INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, data TEXT NOT NULL,
                        flac BLOB NOT NULL, meta TEXT NOT NULL);
                        INSERT INTO stt_inputs (at, data, flac, meta) VALUES (1, '{"id":"FG1-old"}', x'0102', '{}');""")
    db.commit()
    db.close()
    ob = Outbox(p)
    ob.add_stt({"id": "South2-new"}, b"p", {}, prio=PRE)
    rows = ob.stt_batch()
    assert [(r["data"]["id"], r["prio"]) for r in rows] == [("South2-new", PRE), ("FG1-old", NORMAL)]


def test_the_encoder_makes_the_pre_alert_ones_first_and_a_full_queue_drops_the_lowest():
    made, done = [], threading.Event()

    def sink(d, data, meta, prio):
        made.append((d["id"], prio))
        if len(made) == 3:
            done.set()
    enc = paused(sttinput.Encoder(sink, log=lambda m: None, max_items=3, max_s=100.0, mode="thread"))
    a = voice(0.5)
    tx = lambda tid: Traffic(tid, "FG1", 0.0, 0.5, "")       # noqa: E731
    # (the whole queue laid out before the encoder's threads run: no race with their picks)
    assert enc.submit(tx("ctx-1"), a, LOW) and enc.submit(tx("FG1-1"), a, NORMAL) and enc.submit(tx("ctx-2"), a, LOW)
    assert enc.submit(tx("PD-1"), a, PRE)                       # full: the oldest of the lowest gives its place
    assert enc.dropped["queue"] == 1 and enc.dropped_pre == 0
    assert enc.submit(tx("PD-2"), a, PRE)                       # ... and the next lowest
    assert not enc.submit(tx("ctx-3"), a, LOW)                  # nothing lower than it queued: it is the one dropped
    assert enc.submit(tx("PD-3"), a, PRE)                       # FG1-1 gives its place
    assert not enc.submit(tx("PD-4"), a, PRE)                   # all pre-alert now: the new one is dropped, counted
    assert enc.dropped["queue"] == 5 and enc.dropped_pre == 1 and enc.stats()["queued"] == 3
    resume(enc)
    try:
        assert done.wait(60)
        assert made == [("PD-1", PRE), ("PD-2", PRE), ("PD-3", PRE)]
    finally:
        enc.stop()


def test_a_full_queue_never_drops_lower_ones_for_nothing():
    """A long one that wouldn't fit even with every lower one gone: dropped itself, nothing else."""
    enc = paused(sttinput.Encoder(lambda *a, **k: None, log=lambda m: None, max_items=10, max_s=3.0))
    a = voice(1.0)
    assert enc.submit(Traffic("ctx", "FG1", 0, 1, ""), a, LOW) and enc.submit(Traffic("PD", "FG1", 0, 1, ""), a, PRE)
    assert not enc.submit(Traffic("PD-long", "FG1", 0, 2, ""), voice(2.5), PRE)    # 2.5 s > 3 s - 1 s (PD stays)
    assert enc.stats()["queued"] == 2 and enc.dropped["queue"] == 1
    enc.stop()


@pytest.fixture
def station(tmp_path):
    st = offline_station(tmp_path)
    yield st
    st.link.stop()


def test_the_station_ranks_pre_alerts_then_fire_then_the_airband_and_other_traffic(station, monkeypatch):
    from sizeup import app as app_mod
    from sizeup.radio.page import Page
    st = station
    monkeypatch.setattr(app_mod, "AIRBAND_FIRST", {"HPN ATC"})
    t = lambda src: Traffic(f"{src}-1", src, 0.0, 1.0, "")     # noqa: E731
    page = Page("South2-1790000000000", "South 2", 470.2, 0.0, 6.0, "voice", [], 2.0, None, 20.0)
    assert st._stt_lane(page, "South 2") == PRE                                  # a page
    assert st._stt_lane(t("FG1"), "FG1") == FIRE                                 # whisper's URGENT: fire traffic
    assert st._stt_lane(t("Fire/EMS 15"), "tg:307") == FIRE
    assert st._stt_lane(t("HPN ATC"), "HPN ATC") == NORMAL                       # the airband: never above fire
    assert st._stt_lane(t("Ops 3"), "Ops 3") == NORMAL                           # other live traffic
    # another P25 system's talkgroups (#409): by their tier
    tiers = {1001: "alert", 1002: "record", 1003: "text"}
    monkeypatch.setattr(st.tg, "tier", lambda tg, system="": tiers.get(tg) if system else None)
    assert st._stt_lane(t("PD dispatch"), "p25:mrrs:1001") == PRE                # alert tier: PD dispatch
    assert st._stt_lane(t("PD tac"), "p25:mrrs:1002") == LOW
    assert st._stt_lane(t("PD admin"), "p25:mrrs:1003") == LOW


def test_fire_traffic_goes_up_ahead_of_older_airband(tmp_path, monkeypatch):
    from sizeup import app as app_mod
    monkeypatch.setattr(app_mod, "AIRBAND_FIRST", {"HPN ATC"})
    hub = FakeHub(["p25_systems", "stt_input"])           # (down: the events wait, and the STT inputs behind them)
    st = offline_station(tmp_path, client=hub)
    try:
        _fire_first(st, hub)
    finally:
        st.link.stop()


def _fire_first(st, hub):
    first = _transmission(st, "HPNATC-1790000000100", "HPN ATC", voice(1.0, seed=1), False)
    assert wait(lambda: stt_rows(st, first.id), 10)
    later = _transmission(st, "FG1-1790000000200", "FG1", voice(1.0, seed=2), True)
    assert wait(lambda: stt_rows(st, later.id), 10)
    rows = st.link.outbox.stt_batch(max_bytes=1 << 30)
    assert [(r["data"]["id"], r["prio"]) for r in rows] == [(later.id, FIRE), (first.id, NORMAL)]
    hub.down = False
    assert wait(lambda: hub.stt_posts and not st.link.outbox.stt_pending()[0], 15)
    ids = [it["id"] for body, _gz in hub.stt_posts for it in body["items"]]
    assert ids == [later.id, first.id]
    assert "prio" not in hub.stt_posts[0][0]["items"][0]                          # (the wire is as before)
    assert st.link.stt_stats()["dropped_pre"] == 0


class TimedHub(FakeHub):
    """FakeHub that notes when the events were taken and when each STT post came."""

    def __init__(self, caps):
        super().__init__(caps)
        self.events_at, self.stt_at = [], []

    def request(self, method, path, body=None, timeout=None, gzip_body=False, raw_gzip=None):
        out = super().request(method, path, body, timeout, gzip_body, raw_gzip)
        if path == "/api/station/events":
            self.events_at.append(time.monotonic())
        elif path == "/api/station/stt":
            self.stt_at.append(time.monotonic())
        return out


def test_the_stt_input_follows_its_event_at_once_not_at_the_lanes_next_look(tmp_path, monkeypatch):
    """The lane waits while an event is in the outbox; the events' ack wakes it. Before, it looked again only every
    2 s, so a clip's STT input reached the hub ~2.0 s after its event (production p50 2.0 s for the pre-alert classes)
    and a GPU worker's audio request waited out the hub's 2 s for it (#678). Its look here is 60 s: only the wake can
    send it within the test's wait."""
    from sizeup.cloud import station as station_mod
    monkeypatch.setattr(station_mod, "STT_LANE_LOOK_S", 60.0)
    hub = TimedHub(["p25_systems", "stt_input"])
    st = offline_station(tmp_path, client=hub)
    try:
        t = _transmission(st, "FG1-1790000000777", "FG1", voice(1.0, seed=3), True, eng="aws")
        assert wait(lambda: stt_rows(st, t.id), 10)
        time.sleep(0.3)                              # the lane is in its wait now (an event in the outbox)
        assert hub.stt_posts == []
        hub.down = False
        assert wait(lambda: hub.stt_at, 20)
        assert hub.events_at and hub.stt_at[0] >= hub.events_at[0]
        assert st.link.status(talkgroups=False)["stt_input"]["wake_on_ack"] is True     # (what the hub's waits go by)
    finally:
        st.link.stop()



def test_a_burst_goes_up_pre_alert_first_and_in_its_own_order_within_each_class(tmp_path):
    """The events' ack wakes the lane (#678) and nothing else about its order changes (#677 / #701): after a backlog,
    the pre-alert channels' inputs first, then fire traffic, then the rest, each class in the order its inputs were
    queued. An input never waits for its own event (a whisper clip's traffic event goes only with its text)."""
    hub = FakeHub(["p25_systems", "stt_input"])           # (down: the events wait, and the STT inputs behind them)
    st = offline_station(tmp_path, client=hub)
    try:
        queued = []
        for k in range(9):
            src, eng = (("FG1", "aws"), ("Ops 3", "aws"), ("Ops 3", "local"))[k % 3]
            t = _transmission(st, f"{src.replace(' ', '')}-17900001{k:05d}", src, voice(0.6, seed=40 + k), True, eng=eng)
            assert wait(lambda: stt_rows(st, t.id), 10)                    # (queued one after the other: n order)
            queued.append((t.id, st._stt_lane(t, src)))
        from sizeup.radio.page import Page
        page = Page("South2-1790000200000", "South 2", 470.2, 0.0, 6.0, "voice", [], 2.0, None, 20.0)
        st.link.stt_input(page, voice(0.6, seed=60), PRE)
        assert wait(lambda: stt_rows(st, page.id), 10)
        queued.append((page.id, PRE))
        assert hub.stt_posts == []
        hub.down = False
        assert wait(lambda: not st.link.outbox.stt_pending()[0] and hub.stt_posts, 20)
        got = [it["id"] for body, _gz in hub.stt_posts for it in body["items"]]
        prio = dict(queued)
        assert sorted(got) == sorted(prio) and got[0] == page.id                    # the pre-alert first
        assert [prio[i] for i in got] == sorted((prio[i] for i in got), reverse=True)  # then fire, then the rest
        for p in (FIRE, NORMAL):                                                       # each class in its own order
            assert [i for i in got if prio[i] == p] == [i for i, q in queued if q == p]
    finally:
        st.link.stop()


def test_the_airband_never_costs_a_fireground_clip(tmp_path):
    """FG1 and HPN ATC alternating over a pressed lane: the airband's go, never the fireground's (#677 review)."""
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=10_000)                 # the lane's budget: 3000 (6 inputs)
    for k in range(30):
        ob.add_stt({"id": f"FG1-{k}"}, b"f" * 500, {}, prio=FIRE)
        ob.add_stt({"id": f"ATC-{k}"}, b"a" * 500, {}, prio=NORMAL)
    kept = [r["data"]["id"] for r in ob.stt_batch(max_bytes=1 << 30)]
    assert kept == [f"FG1-{k}" for k in range(24, 30)] and ob.dropped_stt == 54 and ob.dropped_stt_pre == 0
    assert ob.dropped_stt_fire == 24                                    # (the oldest FG1 ones, the budget being 6)
    # the encoder: an airband clip never takes a fireground clip's place in a full queue
    enc = paused(sttinput.Encoder(lambda *a, **k: None, log=lambda m: None, max_items=2, max_s=100.0))
    a = voice(0.5)
    assert enc.submit(Traffic("FG1-a", "FG1", 0, 1, ""), a, FIRE) and enc.submit(Traffic("FG1-b", "FG1", 0, 1, ""), a, FIRE)
    assert not enc.submit(Traffic("ATC-a", "HPN ATC", 0, 1, ""), a, NORMAL)
    assert enc.stats()["queued"] == 2 and enc.dropped["queue"] == 1 and enc.dropped_fire == 0
    enc.stop()


def test_fresh_pre_alerts_and_fire_go_before_an_outages_backlog(tmp_path):
    now = 1_800_000_000.0
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=10_000_000)
    for k in range(40):                                                         # an hour's backlog (an outage)
        ob.add_stt({"id": f"South2-old{k}"}, b"p" * 100, {}, prio=PRE, at=now - 3600 + k)
        ob.add_stt({"id": f"FG1-old{k}"}, b"f" * 100, {}, prio=FIRE, at=now - 3600 + k)
        ob.add_stt({"id": f"Ops-old{k}"}, b"o" * 100, {}, prio=NORMAL, at=now - 3600 + k)
    ob.add_stt({"id": "Ops-new"}, b"o" * 100, {}, prio=NORMAL, at=now - 5)
    ob.add_stt({"id": "FG1-new"}, b"f" * 100, {}, prio=FIRE, at=now - 10)
    ob.add_stt({"id": "South2-new1"}, b"p" * 100, {}, prio=PRE, at=now - 60)
    ob.add_stt({"id": "South2-new2"}, b"p" * 100, {}, prio=PRE, at=now - 1)
    ids = [r["data"]["id"] for r in ob.stt_batch(max_bytes=1 << 30, max_items=200, now=now)]
    # PRE: fresh (in order), then the stale; FIRE the same; then the rest oldest first
    assert ids[:3] == ["South2-new1", "South2-new2", "South2-old0"]
    assert ids[42:45] == ["FG1-new", "FG1-old0", "FG1-old1"]
    assert ids[83:] == [f"Ops-old{k}" for k in range(40)] + ["Ops-new"]
    assert [r["data"]["id"] for r in ob.stt_batch(max_items=1, now=now)] == ["South2-new1"]   # the first post


def test_the_lane_order_is_an_index_and_never_a_pass_over_the_flac(tmp_path):
    ob = Outbox(tmp_path / "o.sqlite")
    plans = []
    for sql, args in (("SELECT n, prio FROM stt_inputs INDEXED BY stt_inputs_order ORDER BY prio, n LIMIT 20", ()),
                      ("SELECT DISTINCT prio FROM stt_inputs INDEXED BY stt_inputs_order ORDER BY prio DESC", ()),
                      ("SELECT n FROM stt_inputs INDEXED BY stt_inputs_order WHERE prio = ? AND at >= ? ORDER BY n "
                       "LIMIT ?", (3, 0.0, 5)),
                      ("SELECT COUNT(*) FROM stt_inputs INDEXED BY stt_inputs_order", ())):
        plans.append(" ".join(r[-1] for r in ob.db.execute("EXPLAIN QUERY PLAN " + sql, args)))
    assert all("USE TEMP B-TREE FOR ORDER BY" not in p for p in plans), plans
    assert all("COVERING INDEX" in p for p in plans), plans


def test_an_event_never_waits_on_a_full_stt_lane(tmp_path):
    """The emergency button's add() while the lane's sender picks its batch over ~86 MB of FLAC: the outbox's lock is
    only ever held for one small read. Medians (robust under a loaded CI): the old sort of the whole lane under the
    lock held it ~20 ms each time here (p95 add() ~21 ms; 105 ms on the station's i5)."""
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=300 * 1024 * 1024)
    blob = np.random.default_rng(1).integers(0, 256, 48_000, dtype=np.uint8).tobytes()
    for k in range(1800):                                                       # every priority, fresh and stale
        ob.add_stt({"id": f"X-{k}"}, blob, {}, prio=k % 4, at=time.time() - 3600 + k)

    class Timed:
        def __init__(self, lock):
            self.lock, self.holds, self.t0 = lock, [], 0.0

        def __enter__(self):
            self.lock.acquire()
            self.t0 = time.perf_counter()

        def __exit__(self, *exc):
            if threading.current_thread().name == "lane":
                self.holds.append(time.perf_counter() - self.t0)
            self.lock.release()
    ob._lock = timed = Timed(ob._lock)
    stop, batches = threading.Event(), [0]

    def sender():
        while not stop.is_set():
            assert len(ob.stt_batch()) >= 1
            ob.stt_pending()
            batches[0] += 1
    t = threading.Thread(target=sender, name="lane", daemon=True)
    t.start()
    lat = []
    try:
        for k in range(200):
            t0 = time.perf_counter()
            ob.add("alarm", {"id": f"FG1-{k}", "emergency": True})
            lat.append(time.perf_counter() - t0)
            time.sleep(0.002)
    finally:
        stop.set()
        t.join(10)
    holds, lat = sorted(timed.holds), sorted(lat)
    assert batches[0] >= 5 and len(holds) > 50
    assert holds[len(holds) // 2] < 0.003, holds[len(holds) // 2]
    assert lat[len(lat) // 2] < 0.005, lat[len(lat) // 2]


def test_too_long_or_failed_pre_alerts_arent_pressure_drops():
    def boom(*a, **k):
        raise OSError("disk full")
    enc = sttinput.Encoder(boom, log=lambda m: None)
    assert not enc.submit(Traffic("South2-long", "South 2", 0, 1, ""), np.zeros(sttinput.MAX_SECONDS * RATE + 1,
                                                                                np.float32), PRE)
    assert enc.submit(Traffic("South2-1", "South 2", 0, 1, ""), voice(0.5), PRE)
    assert wait(lambda: enc.dropped["failed"] == 1, 5)
    assert enc.dropped["too_big"] == 1 and enc.dropped_pre == 0
    enc.stop()


def test_a_renamed_station_keeps_its_hourly_p95(tmp_path):
    now = 1_800_000_000.0
    store = MetricStore(tmp_path / "metrics.sqlite", log=lambda m: None, clock=lambda: now)
    store.ingest("st-old", [{"k": "stt_in", "s": "FG1", "at": now - 7200 + k, "delay": k / 10, "kind": "traffic"}
                            for k in range(1, 21)], tell=False)
    store.maintain(now)
    before = store.hours("st-old", "FG1", "stt_delay", now - 86400, now)
    assert before and before[0]["p95"] is not None
    assert store.rename_station("st-old", "st-new") > 0
    assert store.hours("st-new", "FG1", "stt_delay", now - 86400, now) == before


# -- the hub: the arrival delay -------------------------------------------------------------------------------------


def test_arrival_after_the_event_before_it_once_each_and_forgotten_in_time():
    clock = [1000.0]
    arr = sttarrival.Arrival(clock=lambda: clock[0])
    arr.event("FG1-1")
    clock[0] += 1.25
    arr.blob("FG1-1", "st-hq", "traffic", "FG1")
    arr.blob("FG1-1", "st-hq", "traffic", "FG1")              # a resend: never twice
    arr.event("FG1-1")                                         # a replayed event: neither
    arr.blob("South2-1", "st-hq", "page", "South 2")           # the STT input first ...
    clock[0] += 0.5
    arr.event("South2-1")                                      # ... its event 0.5 s later: -0.5
    arr.event("FG1-2")                                         # never an STT input (a channel not transcribed)
    arr.blob("FG1-3", "st-2", "traffic", "FG1")                # never an event
    got = arr.take()
    assert got == {"st-hq": [{"k": "stt_in", "s": "FG1", "at": 1001.25, "delay": 1.25, "kind": "traffic"},
                             {"k": "stt_in", "s": "South 2", "at": 1001.75, "delay": -0.5, "kind": "page"}]}
    assert arr.take() == {} and arr.pending() == {"events": 1, "blobs": 1, "untaken": 0}
    clock[0] += sttarrival.PENDING_S + 1
    arr.event("FG1-4")                                         # (the old ones forgotten as new ones come)
    arr.blob("FG1-5", "st-hq", "traffic", "FG1")
    assert arr.pending() == {"events": 1, "blobs": 1, "untaken": 0}
    arr.blob("FG1-2", "st-hq", "traffic", "FG1")              # its event forgotten: no sample
    assert arr.take() == {}


def test_arrival_stats_p50_p95_per_kind_and_channel_over_the_hour_and_day(tmp_path):
    now = 1_800_000_000.0
    store = MetricStore(tmp_path / "metrics.sqlite", log=lambda m: None, clock=lambda: now)
    recs = [{"k": "stt_in", "s": "FG1", "at": now - 7200 + k, "delay": float(k), "kind": "traffic"} for k in range(1, 21)]
    recs += [{"k": "stt_in", "s": "FG1", "at": now - 60, "delay": 0.4, "kind": "traffic"}]
    recs += [{"k": "stt_in", "s": "South 2", "at": now - 30 - k, "delay": d, "kind": "page"}
             for k, d in enumerate([-0.2, 0.3, 0.5, 0.6, 2.0])]
    recs += [{"k": "stt_in", "s": "FG2", "at": now - 3 * 86400, "delay": 9.0, "kind": "traffic"}]     # older than a day
    store.ingest("st-hq", recs, tell=False)
    out = sttarrival.stats(store, now)
    assert out["kinds"]["page"]["h1"] == {"n": 5, "p50": 0.5, "p95": 2.0, "early": 1}
    assert out["kinds"]["traffic"]["h1"] == {"n": 1, "p50": 0.4, "p95": 0.4, "early": 0}
    assert out["kinds"]["traffic"]["d1"] == {"n": 21, "p50": 10.0, "p95": 19.0, "early": 0}
    assert [(c["channel"], c["kind"]) for c in out["channels"]] == [("South 2", "page"), ("FG1", "traffic")]
    assert out["channels"][1]["d1"]["p95"] == 19.0 and out["more"] == 0
    assert sttarrival.stats(MetricStore(":memory:", log=lambda m: None), now) is None


def test_the_stt_delay_series_rolls_up_hourly_with_its_p95(tmp_path):
    now = 1_800_000_000.0
    store = MetricStore(tmp_path / "metrics.sqlite", log=lambda m: None, clock=lambda: now)
    hour = int(now // 3600) - 1
    store.ingest("st-hq", [{"k": "stt_in", "s": "FG1", "at": hour * 3600 + k * 30, "delay": k / 10, "kind": "traffic"}
                           for k in range(1, 101)], tell=False)
    store.maintain(now)
    [row] = store.hours("st-hq", "FG1", "stt_delay", now - 86400, now)
    assert row["n"] == 100 and row["p50"] == 5.0 and row["p95"] == 9.5 and row["p90"] == 9.0 and row["hi"] == 10.0
    q = store.query({"subject": "FG1", "from": now - 86400, "to": now, "res": "hour", "metric": "stt_delay"})
    assert q["rows"][0]["p95"] == 9.5
    raw = store.query({"subject": "FG1", "from": now - 86400, "to": now, "res": "raw", "kind": "stt_in"})
    assert len(raw["rows"]) == 100 and raw["rows"][0] == {"k": "stt_in", "at": hour * 3600 + 30, "delay": 0.1, "kind": "traffic"}
    assert {"station": "st-hq", "subject": "FG1", "kind": "stt_in"} in [
        {k: s[k] for k in ("station", "subject", "kind")} for s in store.index()["series"]]


def test_a_metrics_file_from_before_p95_opens_and_rolls_it_up(tmp_path):
    p = tmp_path / "metrics.sqlite"
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE hourly (station TEXT NOT NULL, subject TEXT NOT NULL, metric TEXT NOT NULL,
                        hour INTEGER NOT NULL, n INTEGER NOT NULL, lo REAL, p50 REAL, p90 REAL, hi REAL,
                        PRIMARY KEY (station, subject, metric, hour)) WITHOUT ROWID;
                        INSERT INTO hourly VALUES ('st-hq', 'FG1', 'floor', 499995, 3, 1, 2, 3, 4);""")
    db.commit()
    db.close()
    now = 1_800_000_000.0
    store = MetricStore(p, log=lambda m: None, clock=lambda: now)
    store.ingest("st-hq", [{"k": "stt_in", "s": "FG1", "at": now - 100, "delay": 1.0, "kind": "traffic"}], tell=False)
    store.maintain(now)
    assert store.hours("st-hq", "FG1", "floor", 499995 * 3600, 499996 * 3600)[0]["p95"] is None     # (an older row)
    assert store.hours("st-hq", "FG1", "stt_delay", now - 3600, now)[0]["p95"] == 1.0


def test_the_hubs_own_records_never_take_a_stations_resend_answer(tmp_path):
    store = MetricStore(":memory:", log=lambda m: None)
    store._settle(123.0, "test")
    assert "resend_from" not in store.ingest("st-hq", [{"k": "stt_in", "s": "FG1", "at": time.time(), "delay": 1.0}], tell=False)
    assert store.ingest("st-hq", [])["resend_from"] == 123.0                     # still the station's to hear


def _page_event(seq, pid, end):
    return {"seq": seq, "at": end, "type": "page",
            "data": {"id": pid, "label": "South 2", "mhz": 470.2, "start": end - 6, "end": end, "kind": "voice",
                     "status": "done", "departments": [], "quieting_db": 25.0, "tones": [], "raw": "Engine 60",
                     "confidence": 0.9, "voice_offset_s": 2.0, "tone_seq": [], "model": "m.bin"}}


def test_the_hub_measures_each_stt_inputs_arrival_and_keeps_it_as_a_series(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        assert call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": {"id": "t"}})[0] == 200
        a, b = voice(1.0, seed=1), voice(1.0, seed=2)
        now = time.time()
        # a transmission: its event first, its STT input a moment later
        assert call(hub.url + "/api/station/events", "POST",
                    {"epoch": "e1", "events": [_event(1, "FG1-1790000000001", a, end=now)]})[1]["acked"] == 1
        time.sleep(0.3)
        assert call(hub.url + "/api/station/stt", "POST",
                    {"station": "t", "items": [_item(1, "FG1-1790000000001", a)]})[1]["stored"] == [1]
        # a page: its STT input here first (negative), its event after
        assert call(hub.url + "/api/station/stt", "POST",
                    {"station": "t", "items": [_item(2, "South2-1790000000002", b, kind="page", voice_offset_s=2.0)]}
                    )[1]["stored"] == [2]
        time.sleep(0.2)
        assert call(hub.url + "/api/station/events", "POST",
                    {"epoch": "e1", "events": [_page_event(2, "South2-1790000000002", now)]})[1]["acked"] == 2
        # resent: never measured twice
        call(hub.url + "/api/station/stt", "POST", {"station": "t", "items": [_item(3, "FG1-1790000000001", a)]})
        assert hub.app.stt_arrival_flush() == 2
        docs = {d["kind"]: d for st, s in (("t", "FG1"), ("t", "South 2"))
                for d in hub.app.metric_store.docs(st, s, "stt_in", now - 60, now + 60)}
        assert 0.25 <= docs["traffic"]["delay"] < 120.0                     # (at least the 0.3 s between the two)
        assert -120.0 < docs["page"]["delay"] <= -0.15
        # on Health: per kind and channel
        sa = hub.app.hub.health_.model()["stt_arrival"]
        assert sa["kinds"]["page"]["h1"]["n"] == 1 and sa["kinds"]["page"]["h1"]["early"] == 1
        assert [(c["channel"], c["kind"]) for c in sa["channels"]] == [("South 2", "page"), ("FG1", "traffic")]
        # and in the metrics API (GET /api/cad/metrics: the store's query), for the Signal tab
        assert hub.app.cad.station_metrics is hub.app.metric_store
        q = hub.app.metric_store.query({"subject": "South 2", "res": "raw", "kind": "stt_in"})
        assert [(r["k"], r["kind"]) for r in q["rows"]] == [("stt_in", "page")]
        hub.app.metric_store.maintain()
        q = hub.app.metric_store.query({"subject": "FG1", "res": "hour", "metric": "stt_delay"})
        assert q["rows"][0]["n"] == 1 and q["rows"][0]["p95"] == docs["traffic"]["delay"]
    finally:
        hub.close()


def test_a_standby_hub_drops_its_measurements_and_a_post_never_fails_for_them(tmp_path, monkeypatch):
    from sizeup import fence as fence_mod
    hub = HubRun(hub_cfg(tmp_path))
    try:
        a = voice(1.0, seed=3)
        assert call(hub.url + "/api/station/events", "POST",
                    {"epoch": "e1", "events": [_event(1, "FG1-1790000000011", a)]})[1]["acked"] == 1
        assert call(hub.url + "/api/station/stt", "POST", {"items": [_item(1, "FG1-1790000000011", a)]})[1]["stored"] == [1]

        def fenced(*a, **k):
            fence_mod.refuse("station metrics")
        monkeypatch.setattr(hub.app.metric_store, "ingest", fenced)
        assert hub.app.stt_arrival_flush() == 0 and hub.app.stt_arrival.pending()["untaken"] == 0
    finally:
        hub.close()


# -- the pages: Health and the Signal tab ---------------------------------------------------------------------------


def test_health_shows_the_arrival_table_and_the_pre_alert_drops(tmp_path):
    from tests.test_signal_ui import NODE, run
    if NODE is None:
        pytest.skip("node not installed")
    w = lambda n, p50, p95, early=0: {"n": n, "p50": p50, "p95": p95, "early": early}     # noqa: E731
    sa = {"kinds": {"page": {"h1": w(3, 0.4, 0.9, 1), "d1": w(40, 0.5, 1.2, 2)}, "traffic": {"h1": None, "d1": w(9, 1.1, 3.0)}},
          "channels": [{"channel": "South 2", "kind": "page", "h1": w(3, 0.4, 0.9, 1), "d1": w(40, 0.5, 1.2, 2)},
                       {"channel": "FG1", "kind": "traffic", "h1": None, "d1": w(9, 1.1, 3.0)}], "more": 0}
    data = {"now": 1_800_000_000, "fetch": {
        "/api/cad/metrics?view=spark": {"status": 200, "body": {"subjects": []}},
        "/api/cad/health": {"status": 200, "body": {
            "health": {"reported": True, "health_reported": True, "channels": [], "stt_arrival": sa,
                       "outbox": {"pending": 0, "dropped_stt": 4, "dropped_stt_why": {"outbox": 4}, "dropped_stt_pre": 1,
                                  "dropped_stt_fire": 2,
                                  "stt_worker": {"where": "service", "state": "unhealthy", "held": False,
                                                 "detail": "the worker is unhealthy (no answer for 600 s)"}}},
            "alerts": [], "notify": {}, "thresholds": {}, "history": []}},
        "/api/cad/perf": {"status": 403, "body": {}}}}
    out = run(tmp_path, r"""
load("signal-chart.js");
global.fmtDur = (s) => String(s); global.sHead = (t) => [el("h2", "", t)];
global.Help = { info: () => el("span") };
load("health.js");
const box = new N("div");
StationHealth.mount(box);
for (let i = 0; i < 20; i++) await flush();
const subs = cls(box, "hl-sub");
const i = subs.findIndex((h) => h.textContent === "STT input arrival (hub)");
const parent = all(box, (n) => n.children.includes(subs[i]))[0];
const tbl = parent.children[parent.children.indexOf(subs[i]) + 1];
const rows = all(tbl, (n) => n.tag === "tr").map((r) => r.children.map((c) => c.textContent));
const dts = all(box, (n) => n.tag === "dt" && n.textContent === "STT inputs dropped");
const dd = all(box, (n) => n.tag === "div" && n.children.includes(dts[0]))[0].children[1];
const ets = all(box, (n) => n.tag === "dt" && n.textContent === "STT encoder");
const ed = all(box, (n) => n.tag === "div" && n.children.includes(ets[0]))[0].children[1];
console.log(JSON.stringify({ title: subs[i].title, rows, dropped: dd.textContent, cls: dd.className,
                             enc: [ed.textContent, ed.className, ed.title] }));
""", data)
    assert out["title"].startswith("Event applied → its STT input stored")
    assert out["rows"] == [["Channel", "Kind", "1 h", "24 h", "n 24 h"],
                           ["All", "page", "0.4 / 0.9 s", "0.5 / 1.2 s", "40"],
                           ["All", "traffic", "—", "1.1 / 3 s", "9"],
                           ["South 2", "page", "0.4 / 0.9 s", "0.5 / 1.2 s", "40"],
                           ["FG1", "traffic", "—", "1.1 / 3 s", "9"]]
    assert out["dropped"] == "4 (outbox 4) · 1 pre-alert · 2 fire" and out["cls"] == "hl-warn"
    # the encoder service's state: terse, the detail on hover (a wedged-but-alive service is seen)
    assert out["enc"] == ["service · unhealthy", "hl-warn", "service\nthe worker is unhealthy (no answer for 600 s)"]


def test_health_carries_the_encoder_workers_state_from_the_station(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        status = {"id": "t", "name": "Test", "radios": [], "outbox": {"pending": 0, "oldest": None, "dropped_audio": 0},
                  "stt_input": {"dropped": {}, "encoder": "service", "held": True,
                                "worker": {"where": "service", "state": "slow", "detail": "the worker is slow"}}}
        assert call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": status})[0] == 200
        ob = hub.app.hub.health_.model()["outbox"]
        assert ob["stt_worker"] == {"where": "service", "state": "slow", "detail": "the worker is slow", "held": True}
    finally:
        hub.close()


def test_the_whisper_queues_stt_records_and_the_arrivals_stt_in_records_never_mix(tmp_path):
    """#639's whisper queue is kind "stt" (subject "whisper"); #677's arrival delays are "stt_in": both in one store."""
    from tests.test_signal_ui import NODE, PANEL, run
    now = 1_800_000_000.0
    store = MetricStore(tmp_path / "metrics.sqlite", log=lambda m: None, clock=lambda: now)
    store.ingest("st-hq", [{"k": "stt", "s": "whisper", "at": now - 600 + 60 * k, "queued": k, "oldest": 2.0 * k,
                            "done": 3} for k in range(10)])
    store.ingest("st-hq", [{"k": "stt_in", "s": "FG1", "at": now - 300 + k, "delay": 0.5 + k / 10, "kind": "traffic"}
                           for k in range(5)], tell=False)
    store.maintain(now)
    st = sttarrival.stats(store, now)                                           # arrivals only
    assert [c["channel"] for c in st["channels"]] == ["FG1"] and st["kinds"]["traffic"]["d1"]["n"] == 5
    w = store.query({"subject": "whisper", "res": "raw", "kind": "stt", "from": now - 3600, "to": now})
    assert len(w["rows"]) == 10 and all(r["k"] == "stt" and "delay" not in r for r in w["rows"])   # ?kind=stt: #639's
    assert store.query({"subject": "FG1", "res": "raw", "kind": "stt", "from": now - 3600, "to": now})["rows"] == []
    assert len(store.query({"subject": "FG1", "res": "raw", "kind": "stt_in", "from": now - 3600, "to": now})["rows"]) == 5
    assert store.hours("st-hq", "whisper", "stt_delay", now - 86400, now) == []
    assert store.hours("st-hq", "FG1", "stt_delay", now - 86400, now)[0]["n"] == 5
    assert store.hours("st-hq", "whisper", "stt_queued", now - 86400, now)[0]["n"] == 10
    if NODE is None:
        return
    q = {"from": now - 86400, "to": now, "res": "raw"}
    data = {"now": now, "fetch": {
        "/api/cad/metrics?view=squelch": {"status": 200, "body": {"channels": []}},
        "/api/cad/metrics?subject=FG1": {"status": 200, "body": store.query({**q, "subject": "FG1"})},
        "/api/cad/metrics?subject=whisper": {"status": 200, "body": store.query({**q, "subject": "whisper"})},
        "/api/cad/metrics": {"status": 200, "body": store.index()}}}
    out = run(tmp_path, PANEL, data)
    assert [c["subject"] for c in out["cards"]] == ["FG1"]                     # never a "whisper" arrival card
    assert [f[0] for f in out["cards"][0]["figs"]] == ["FG1: STT input arrival after its event"]


def test_the_signal_tab_graphs_the_arrival_and_a_talkgroup_with_only_that(tmp_path):
    from tests.test_signal_ui import NODE, PANEL, run
    from tests.test_signal_insight import NOW, floor_minutes
    if NODE is None:
        pytest.skip("node not installed")
    s = MetricStore(tmp_path / "metrics.sqlite", log=lambda m: None, clock=lambda: NOW)
    s.ingest("st-hq", [dict(r, s="FG1") for r in floor_minutes(NOW - 3600, 60)])
    s.ingest("st-hq", [{"k": "stt_in", "s": "FG1", "at": NOW - 600 + k, "delay": 0.5 + k / 100, "kind": "traffic"}
                       for k in range(10)] + [{"k": "stt_in", "s": "PD dispatch", "at": NOW - 300, "delay": -0.2,
                                                "kind": "traffic"}], tell=False)
    q = {"from": NOW - 86400, "to": NOW, "res": "raw"}
    data = {"now": NOW, "fetch": {
        "/api/cad/metrics?view=squelch": {"status": 200, "body": {"channels": []}},
        "/api/cad/metrics?subject=FG1": {"status": 200, "body": s.query({**q, "subject": "FG1"})},
        "/api/cad/metrics?subject=PD": {"status": 200, "body": s.query({**q, "subject": "PD dispatch"})},
        "/api/cad/metrics": {"status": 200, "body": s.index()}}}
    out = run(tmp_path, PANEL, data)
    by = {c["subject"]: c for c in out["cards"]}
    assert list(by) == ["FG1", "PD dispatch"]                                   # the arrival-only one last
    fg1 = [f[0] for f in by["FG1"]["figs"]]
    assert fg1[0] == "FG1: idle quieting, squelch, transmissions" and fg1[-1] == "FG1: STT input arrival after its event"
    assert by["FG1"]["figs"][-1][3] == 10                                       # a dot each
    pd = by["PD dispatch"]
    assert pd["head"].startswith("PD dispatchSTT input arrival")
    assert [f[0] for f in pd["figs"]] == ["PD dispatch: STT input arrival after its event"]
