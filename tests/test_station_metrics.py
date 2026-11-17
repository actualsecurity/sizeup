"""The station's signal metrics (sizeup/cloud/metrics.py on the station, sizeup/cloud/metricstore.py on the hub,
GET /api/cad/metrics): what is measured, how it goes up without touching the outbox or the radio path, how the hub
keeps it (raw, hourly rollups, retention, the hourly copy instead of Litestream) and who may read it.

Real shapes: the FG1 transmission is a real capture (tests/fixtures/fg1), run through the real squelch; the
trunk-recorder lines are the sample log's (tests/fixtures/trunk-recorder-sample.log) and its decode-rate / uncovered
lines as tests/test_reception.py writes them. Station ids, serials and frequencies of the made-up rows are invented."""

from __future__ import annotations

import gzip
import json
import sqlite3
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup.cad.api import ADMIN_READS, CadApi
from sizeup.cloud import hub as hub_mod
from sizeup.cloud.access import Identity
from sizeup.cloud.client import HubClient, HubError
from sizeup.cloud.metrics import BATCH_MAX, PATH, Collector, MetricsLink, Sampler
from sizeup.cloud.metricstore import (HOURLY_DAYS, RAW_DAYS, SNAPSHOT_NAME, MetricsError, MetricStore)
from sizeup.radio.fm import FRAME, RATE, Channel, NfmReceiver, NoiseSquelch
from sizeup.radio.recorder import ControlChannel
from tests.test_cad import make_cad
from tests.test_cloud import TOKEN, HubRun, hub_cfg
from tests.test_reception import rate_line, uncovered_line
from tests.test_roles import CASEY, JORDAN, people

FIXTURES = Path(__file__).parent / "fixtures"
T0 = 1_800_000_000.0                 # a minute's start (divisible by 60)
rng = np.random.default_rng(7)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def hiss(s):
    return 0.4 * rng.standard_normal(int(RATE * s))


def real_fg1() -> np.ndarray:
    with wave.open(str(FIXTURES / "fg1" / "FG1-1791491541708.src.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float64) / 32768


def squelched(audio: np.ndarray, sq: NoiseSquelch, clock: Clock) -> list:
    out = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock.t += FRAME / RATE
        s = sq.feed(audio[i:i + FRAME])
        if s is not None:
            out.append(s)
    return out


def fg1_segment():
    clock = Clock(1000.0)
    sq = NoiseSquelch(clock=clock)
    [seg] = squelched(np.concatenate([hiss(3.0), real_fg1(), hiss(1.5)]), sq, clock)
    return seg


# -- the control channel, from trunk-recorder's own lines --------------------------------------------------------------


def test_the_control_channel_minute_counts_rates_retunes_and_uncovered_grants():
    cc = ControlChannel(clock=Clock())
    for line in (FIXTURES / "trunk-recorder-sample.log").read_text().splitlines():
        cc.feed(line)                       # real call lines ("Freq: 476.075000 MHz" in each): nothing counted
    assert cc.take_minute() == {"n": 0, "sum": 0.0, "lo": None, "hi": None, "retunes": 0, "uncovered": {}}
    for r in (38, 41, 46, 24):
        cc.feed(rate_line(769.90625, r))
    cc.feed("[2026-11-10 09:00:03.000000] (info)   [mrrs]\tRetuning to Control Channel: 770.156250 MHz")
    for tg in (9211, 9212, 9211):
        cc.feed(uncovered_line(tg))
    m = cc.take_minute()
    assert (m["n"], m["sum"], m["lo"], m["hi"], m["retunes"]) == (4, 149.0, 24.0, 46.0, 1)
    assert m["uncovered"] == {"773.681250": 3}
    assert cc.status()["rate"] == 24.0 and cc.retunes == 1      # the health's view unchanged
    assert cc.take_minute()["n"] == 0                            # taken: the next minute starts empty


# -- the station: what is collected ---------------------------------------------------------------------------------


def test_a_real_fg1_transmission_becomes_one_tx_record():
    c = Collector(clock=Clock())
    seg = fg1_segment()
    c.tx("FG1", "voice", seg, ids=True)
    _seq, recs = c.batch()
    assert [r["ev"] for r in recs if r["k"] == "ev"] == ["start"]
    [tx] = [r for r in recs if r["k"] == "tx"]
    assert tx["s"] == "FG1" and tx["kind"] == "voice" and tx["id"] == 1 and tx["why"] == "quiet"
    assert tx["dur"] == pytest.approx(12.0, abs=0.2)
    assert tx["q"] == round(seg.quieting_db, 1) and tx["qo"] == round(seg.q_open, 1) and tx["qc"] == round(seg.q_close, 1)
    assert tx["hiss"] == round(seg.hiss_db, 1) and "floor" not in tx
    assert tx["qo"] > tx["qc"] > 10                              # this capture: ~24 dB at open, ~17 at close
    assert len(json.dumps(tx)) < 200                             # small: numbers only, never audio


def test_the_app_hands_each_transmission_over_and_a_failing_collector_changes_nothing(tmp_path):
    from sizeup.app import App
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    seg = fg1_segment()
    app.metrics = Collector(clock=Clock())
    app._segment(Channel(453.0375, "FG1"), seg)
    tx = [r for r in app.metrics.batch()[1] if r["k"] == "tx"]
    # this capture is a 12 s key-up with nothing said and no ID (its burst was cut off: the .mdc.wav): the app's own
    # low-energy judgement makes it "noise", the kind a squelch tuned too low keeps
    assert [(r["s"], r["kind"]) for r in tx] == [("FG1", "noise")]

    class Broken:
        def tx(self, *a, **k):
            raise RuntimeError("boom")
    app.metrics = Broken()
    seg2 = fg1_segment()
    seg2.start, seg2.end = seg.start + 60, seg.end + 60
    app._segment(Channel(453.0375, "FG1"), seg2)               # no exception: the transmission is kept as before
    assert len(app.clips) == 2


def test_the_collector_is_bounded_dropping_the_oldest_and_acks_by_seq():
    c = Collector(max_records=5, clock=Clock())
    for k in range(10):
        c.event("FG1", "up", str(k))
    assert c.pending() == 5 and c.dropped == 6                  # the start event and 5 more
    upto, recs = c.batch(3)
    assert [r["d"] for r in recs] == ["5", "6", "7"]
    c.event("FG1", "up", "10")                                  # one more drops "5" while the batch is in flight
    c.ack(upto)
    assert [r["d"] for r in c.batch()[1]] == ["8", "9", "10"]


def receiver(label="FG1", serial="00000102"):
    r = NfmReceiver("rtl_fm", SimpleNamespace(serial=serial), 36.4, Channel(453.0375, label), lambda ch, seg: None)
    r.state = "ok"
    return r


class FakeRecorder:
    def __init__(self, clock):
        self.cc = ControlChannel(clock=clock)
        self.state, self.serial = "ok", "00000201"

    def status(self):
        return {"state": self.state, "detail": "", "restarts": 0}

    def on(self):
        return self.serial


def fake_app(receivers, recorder=None):
    return SimpleNamespace(receivers=receivers, _kind={"FG1": "traffic", "South 2": "paging"}, shared=False,
                           assign={}, recorder=recorder, p25=object() if recorder else None, systems={})


def test_the_sampler_writes_a_floor_record_a_minute_from_the_idle_squelch():
    clock = Clock()
    r = receiver()
    app = fake_app([r])
    c = Collector(clock=clock)
    s = Sampler(app, c, clock)
    feed = Clock(1000.0)
    r.squelch.clock = feed
    for sec in range(60):
        squelched(hiss(1.0), r.squelch, feed)                  # the channel idles on hiss
        clock.t = T0 + sec
        s.tick()
    r.shorts.extend([T0 + 10, T0 + 20, T0 + 70])                # two noise opens in this minute, one in the next
    clock.t = T0 + 60
    s.tick()
    [fl] = [x for x in c.batch()[1] if x["k"] == "floor"]
    assert fl["s"] == "FG1" and fl["at"] == T0 and fl["kind"] == "traffic" and fl["n"] == 60
    assert (fl["open"], fl["close"], fl["min"], fl["sdr"], fl["gain"], fl["state"]) == (10.0, 6.0, 0.3, "00000102", 36.4, "ok")
    assert fl["noise"] == 2 and fl["open_s"] == 0 and "hiss" in fl and "am" not in fl
    assert fl["q50"] <= fl["q90"] <= fl["qmax"] < 10            # idle hiss never quiets itself to open_db


def test_the_sampler_says_what_changed():
    clock = Clock()
    r = receiver()
    rec = FakeRecorder(clock)
    app = fake_app([r], rec)
    c = Collector(clock=clock)
    s = Sampler(app, c, clock)
    s.diff(clock())                                             # the first look: nothing is a change
    steps = [lambda: r.set_squelch(14.0, 6.0, 0.3), lambda: setattr(r, "state", "absent"),
             lambda: setattr(r, "state", "ok"), lambda: (setattr(r, "state", "down"), setattr(r, "detail", "rtl_fm stalled: no audio for 5 s")),
             lambda: (setattr(r, "state", "ok"), setattr(r, "detail", "")),
             lambda: setattr(r, "device", SimpleNamespace(serial="00000103")), lambda: setattr(r, "gain_db", 30.0)]
    for step in steps:
        step()
        r.squelch.feed(hiss(FRAME / RATE))                      # (the squelch takes new thresholds at a frame)
        clock.t += 5
        s.diff(clock())
    for rate in [0, 0, 0]:                                      # the control channel lost ...
        clock.t += 3
        rec.cc.feed(rate_line(769.90625, rate))
    s.diff(clock())
    for rate in [40, 41]:                                       # ... and back
        clock.t += 3
        rec.cc.feed(rate_line(769.90625, rate))
    s.diff(clock())
    evs = [(x["s"], x["ev"]) for x in c.batch()[1] if x["k"] == "ev" and x["ev"] != "start"]
    assert evs == [("FG1", "squelch"), ("FG1", "unplug"), ("FG1", "back"), ("FG1", "stall"), ("FG1", "up"),
                   ("FG1", "sdr"), ("FG1", "gain"), ("p25", "cc_lost"), ("p25", "cc_back")]
    sq = next(x for x in c.batch()[1] if x.get("ev") == "squelch")
    assert sq["d"] == "open 14 close 6 min 0.3 (was open 10 close 6 min 0.3)"


def test_the_sampler_writes_a_cc_record_a_minute():
    clock = Clock()
    rec = FakeRecorder(clock)
    c = Collector(clock=clock)
    s = Sampler(fake_app([], rec), c, clock)
    s.tick()
    for r in (40, 38, 46):
        rec.cc.feed(rate_line(476.075, r))
    rec.cc.feed(uncovered_line(9211))
    clock.t = T0 + 61
    s.tick()
    [cc] = [x for x in c.batch()[1] if x["k"] == "cc"]
    assert (cc["s"], cc["at"], cc["n"], cc["rate"], cc["lo"], cc["hi"], cc["sdr"]) == ("p25", T0, 3, 41.3, 38.0, 46.0, "00000201")
    assert cc["unc"] == {"773.681250": 1}


# -- the station: how it goes up ------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.posts, self.fail = [], None

    def request(self, method, path, body=None, timeout=None, gzip_body=False):
        if self.fail is not None:
            raise self.fail
        assert (method, path, gzip_body) == ("POST", PATH, True)
        self.posts.append(body)
        return {"ok": True}


def test_the_sender_keeps_what_the_hub_didnt_take_drops_past_the_bound_and_says_so():
    clock = Clock()
    app = SimpleNamespace(metrics=None)
    link = MetricsLink(app, SimpleNamespace(station_id="st-test"), lambda t: None, log=lambda m: None, clock=clock)
    link.c.max_records = 10
    cl = FakeClient()
    cl.fail = HubError("can't reach the hub", 0)
    for k in range(15):
        link.c.event("FG1", "up", str(k))
    assert link.step(cl) > 0 and link.c.pending() == 10 and link.c.dropped == 6
    cl.fail = None
    clock.t += 400
    link.step(cl)
    assert cl.posts[0]["station"] == "st-test" and len(cl.posts[0]["records"]) == 10 and cl.posts[0]["dropped"] == 6
    [said] = link.c.batch()[1]                                  # once it's back: the gap explained, once
    assert said["ev"] == "dropped" and said["d"].startswith("6 ")
    cl.fail = HubError("hub said 400: bad", 400)
    link.step(cl, now=clock.t + 100)
    assert link.c.pending() == 0 and link.rejected == 1         # a batch the hub will never take: not resent forever


class Answering(FakeClient):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def request(self, *a, **k):
        super().request(*a, **k)
        return self.answer


def test_a_hub_that_started_from_an_older_copy_gets_what_it_lost_again():
    clock = Clock()
    link = MetricsLink(SimpleNamespace(metrics=None), SimpleNamespace(station_id="st-test"), lambda t: None,
                       log=lambda m: None, clock=clock)
    for k in range(3):                                         # three minutes, each acked by the old hub task
        link.c.event("FG1", "up", f"old {k}")
        link.step(FakeClient(), now=clock.t)
        clock.t += 3600
    assert link.c.pending() == 0
    # the new task started from a copy made just after the first post: it asks for what came after
    cl = Answering({"ok": True, "resend_from": T0 + 600})
    link.c.event("FG1", "up", "new")
    link.step(cl, now=clock.t)
    again = [r["d"] for r in link.c.batch()[1]]
    assert again == ["old 1", "old 2", "new"]                 # the start event and "old 0" were in the copy
    link.step(FakeClient(), now=clock.t + 1)
    assert link.c.pending() == 0 and link.c.resend(0) == 5    # all five kept again (6 h), each only once
    clock.t += 7 * 3600
    link.c.ack(0, clock.t)
    assert link.c.resend(0) == 0                               # past RESEND_KEEP_S: gone


def test_a_backlog_goes_up_in_batches():
    clock = Clock()
    link = MetricsLink(SimpleNamespace(metrics=None), SimpleNamespace(station_id="st-test"), lambda t: None,
                       log=lambda m: None, clock=clock)
    for k in range(BATCH_MAX + 10):
        link.c.event("FG1", "up", str(k))
    cl = FakeClient()
    assert link.step(cl) == 1.0                                # more waits: again in a second
    link.step(cl, now=clock.t + 1)
    assert [len(p["records"]) for p in cl.posts] == [BATCH_MAX, 11] and link.c.pending() == 0


# -- the hub: in through /api/station/metrics -------------------------------------------------------------------------


def test_the_hub_takes_metrics_from_its_station_and_another_never_through_the_outbox(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="t"))
    try:
        assert ("POST", "/api/station/metrics") in hub_mod.STATION_ROUTES
        assert not (tmp_path / "hub" / "metrics.sqlite").exists()        # nothing made at the start
        now = time.time()
        recs = [{"k": "floor", "s": "FG1", "at": now - 120, "hiss": 2.4, "q90": 3.1, "open": 10, "close": 6},
                {"k": "tx", "s": "FG1", "at": now - 90.5, "kind": "voice", "q": 18.5, "dur": 2.8},
                {"k": "ev", "s": "FG1", "at": now - 60, "ev": "sdr", "d": "SDR 00000102 -> 00000103"},
                {"k": "nope", "s": "FG1", "at": now}, {"k": "tx", "s": "FG1", "at": now + 10 * 86400}, "x"]
        c = HubClient(hub.url, TOKEN)
        out = c.request("POST", PATH, {"v": 1, "station": "t", "dropped": 0, "records": recs}, gzip_body=True)
        assert out == {"ok": True, "accepted": 3, "skipped": 3, "resend_from": 0.0}   # a new store: all it keeps
        again = c.request("POST", PATH, {"v": 1, "station": "t", "records": recs}, gzip_body=True)
        assert again["accepted"] == 0                                    # a retry stores nothing twice
        assert hub.app.hub.seq == 0                                      # the events' cursor never moved
        other = hub.app.hub.stations.add("st-b")
        out = HubClient(hub.url, other).request("POST", PATH, {"station": "st-b", "records": recs[:1]}, gzip_body=True)
        assert out["accepted"] == 1                                      # taken from any station, under its own id
        with pytest.raises(HubError) as e:                               # but only as itself
            HubClient(hub.url, other).request("POST", PATH, {"station": "t", "records": recs[:1]})
        assert e.value.status == 401
        st = hub.app.metric_store
        assert {(x["station"], x["kind"]) for x in st.index()["series"]} == {("t", "floor"), ("t", "tx"), ("st-b", "floor")}
        q = st.query({"subject": ["FG1"], "station": ["t"], "res": ["raw"]})
        assert [r["k"] for r in q["rows"]] == ["floor", "tx"] and q["events"][0]["ev"] == "sdr"
    finally:
        hub.close()


def test_the_station_link_sends_its_metrics_to_the_hub(tmp_path):
    from tests.test_cloud import station, station_cfg
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="t"))
    st = station(station_cfg(tmp_path, hub.url), {})
    try:
        assert isinstance(st.metrics, Collector) and st.link.metrics_link.c is st.metrics
        queued = st.link.outbox.pending()[0]
        seg = fg1_segment()
        seg.start, seg.end = time.time() - 15, time.time() - 3
        st.metrics.tx("FG1", "voice", seg)
        link = st.link.metrics_link
        link._next = 0.0
        link.step(HubClient(hub.url, TOKEN))
        assert link.c.pending() == 0 and link.error == ""
        kinds = {x["kind"] for x in hub.app.metric_store.index()["series"]}
        assert "tx" in kinds
        assert st.link.outbox.pending()[0] == queued              # the outbox: untouched by metrics
        assert "metrics" in st.link.status(talkgroups=False)
    finally:
        st.link.stop()
        hub.close()


# -- the hub: kept ----------------------------------------------------------------------------------------------------


def store(tmp_path, clock=None, leader=True, audit=None):
    clock = clock or Clock(T0 + 3 * 3600)
    s = MetricStore(tmp_path / "metrics.sqlite", may_write=lambda: leader, log=lambda m: None, clock=clock, audit=audit)
    return s, clock


def floors(hour_start, values, subject="FG1"):
    return [{"k": "floor", "s": subject, "at": hour_start + 60 * k, "hiss": 2.0 + v / 10, "q90": v, "noise": k % 2}
            for k, v in enumerate(values)]


def test_rollups_are_n_min_p50_p90_max_and_late_records_roll_their_hour_again(tmp_path):
    s, clock = store(tmp_path)
    h0 = T0
    s.ingest("t", floors(h0, list(range(1, 11))))
    s.ingest("t", [{"k": "tx", "s": "FG1", "at": h0 + 30, "kind": "voice", "q": 20.0, "qo": 24.0},
                   {"k": "tx", "s": "FG1", "at": h0 + 90, "kind": "noise", "q": 11.0}])
    s.maintain()
    rows = {r["metric"]: r for r in s.query({"subject": "FG1", "from": h0, "to": h0 + 3600, "res": "hour"})["rows"]}
    assert (rows["idle_q90"]["n"], rows["idle_q90"]["lo"], rows["idle_q90"]["p50"], rows["idle_q90"]["p90"],
            rows["idle_q90"]["hi"]) == (10, 1.0, 5.0, 9.0, 10.0)
    assert rows["tx_q"]["n"] == 1 and rows["noise_q"]["hi"] == 11.0 and rows["tx_q_open"]["p50"] == 24.0
    s.ingest("t", floors(h0 + 600, [30.0]))                    # the station back after an outage, late
    s.maintain()
    r = [x for x in s.query({"subject": "FG1", "from": h0, "to": h0 + 3600, "res": "hour", "metric": "idle_q90"})["rows"]]
    assert r[0]["n"] == 11 and r[0]["hi"] == 30.0


def test_retention_keeps_raw_30_days_and_rollups_and_events_a_year(tmp_path):
    clock = Clock(T0)
    s, _ = store(tmp_path, clock)
    s.ingest("t", floors(T0 - 3600, [1.0, 2.0]) + [{"k": "ev", "s": "FG1", "at": T0 - 3000, "ev": "gain", "d": "x"}])
    s.maintain()
    clock.t = T0 + (RAW_DAYS + 1) * 86400
    s.maintain()
    assert s.query({"subject": "FG1", "from": T0 - 7200, "to": T0, "res": "raw"})["rows"] == []
    q = s.query({"subject": "FG1", "from": T0 - 7200, "to": T0, "res": "hour", "metric": "idle_q90"})
    assert q["rows"][0]["n"] == 2 and [e["ev"] for e in q["events"]] == ["gain"]
    clock.t = T0 + (HOURLY_DAYS + 1) * 86400
    s.maintain()
    q = s.query({"subject": "FG1", "from": T0 - 7200, "to": T0, "res": "hour"})
    assert q["rows"] == [] and q["events"] == []


def test_the_hourly_copy_brings_a_new_task_back_and_a_late_copy_is_merged(tmp_path):
    a, clock = store(tmp_path / "old")
    a.ingest("t", floors(T0, [1.0, 2.0, 3.0]))
    assert a.maintain(force_snapshot=True)["snapshot"] is True
    snap = tmp_path / "old" / SNAPSHOT_NAME
    with sqlite3.connect(snap) as db:                           # one consistent file (no -wal beside it)
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == 3
    # a new task: the copy restored before the store opens (the standby's warm_caches)
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / SNAPSHOT_NAME).write_bytes(snap.read_bytes())
    b, _ = store(tmp_path / "new", clock)
    assert len(b.query({"subject": "FG1", "from": T0, "to": T0 + 600, "res": "raw"})["rows"]) == 3
    first = b.ingest("t", floors(T0 + 1200, [4.0]))
    assert first["resend_from"] == pytest.approx(clock.t)       # when the copy was made: the station sends the rest again
    assert "resend_from" not in b.ingest("t", floors(T0 + 1260, [4.0]))   # once per station
    assert b.ingest("st-b", floors(T0, [4.0]))["resend_from"] == pytest.approx(clock.t)
    a2, _ = store(tmp_path / "old", clock)                      # its own file still there (a restart): nothing to ask
    assert "resend_from" not in a2.ingest("t", floors(T0 + 1200, [4.0]))
    # ... or after it opened (the app's own late restore): merged in once, its hours rolled
    (tmp_path / "late").mkdir()
    c, _ = store(tmp_path / "late", clock)
    c.ingest("t", floors(T0 + 300, [9.0]))
    time.sleep(0.01)
    (tmp_path / "late" / SNAPSHOT_NAME).write_bytes(snap.read_bytes())
    assert c.maintain()["merged"] > 3                           # its rows (raw, rollups, the index), none of ours replaced
    assert len(c.query({"subject": "FG1", "from": T0, "to": T0 + 600, "res": "raw"})["rows"]) == 4
    assert c.query({"subject": "FG1", "from": T0, "to": T0 + 3600, "res": "hour", "metric": "idle_q90"})["rows"][0]["n"] == 4


def test_a_standby_writes_nothing_and_the_copy_is_in_the_cache_mirror(tmp_path):
    from sizeup.cachesync import gap_for
    s, _ = store(tmp_path, leader=False)
    with pytest.raises(MetricsError):
        s.ingest("t", floors(T0, [1.0]))
    assert s.maintain() == {} and not (tmp_path / "metrics.sqlite").exists()
    assert gap_for(SNAPSHOT_NAME) == 3 * 3600.0 and gap_for("metrics.sqlite") is None    # the copy, never the live file
    assert "metrics" not in (Path(hub_mod.__file__).parent.parent.parent / "deploy" / "hub" / "litestream.yml").read_text()
    from sizeup.handoff import DBS
    assert "metrics" not in DBS                                 # nothing added to a drain or a takeover


def test_queries_check_their_range_and_join_the_settings_audit(tmp_path):
    audit_rows = [{"at": T0 + 120, "operator": "casey@example.org", "key": "squelch/FG1",
                   "old": '{"open_db": 10.0}', "new": '{"open_db": 14.0}'}]
    s, _ = store(tmp_path, audit=lambda subj, t0, t1: [a for a in audit_rows if subj == "FG1" and t0 <= a["at"] < t1])
    s.ingest("t", floors(T0, [1.0, 2.0]) + [{"k": "ev", "s": "station", "at": T0 + 30, "ev": "start", "d": "sizeup started"}])
    s.ingest("st-b", floors(T0, [5.0]))
    with pytest.raises(MetricsError, match="station: one of"):
        s.query({"subject": "FG1", "from": T0, "to": T0 + 600})
    with pytest.raises(MetricsError, match="at most 7 days"):
        s.query({"subject": "FG1", "station": "t", "from": T0, "to": T0 + 8 * 86400, "res": "raw"})
    for bad in ({"res": "x"}, {"from": "a"}, {"from": T0 + 10, "to": T0}, {"kind": "x"}, {"res": "hour", "metric": "x"}):
        with pytest.raises(MetricsError):
            s.query({"subject": "FG1", "station": "t", "from": T0, "to": T0 + 600, **bad})
    q = s.query({"subject": "FG1", "station": "t", "from": T0, "to": T0 + 600})
    assert q["res"] == "raw" and len(q["rows"]) == 2 and q["stations"] == ["st-b", "t"]
    assert [(e["ev"], e["source"]) for e in q["events"]] == [("start", "station"), ("setting", "audit")]
    assert q["events"][1]["detail"].startswith("squelch/FG1: ")
    assert s.query({"subject": "FG1", "station": "t", "from": T0, "to": T0 + 30 * 86400})["res"] == "hour"


def test_size_per_day_is_what_the_docs_say(tmp_path):
    """A day of a busy station (6 channels, 2 P25 systems, 2,000 transmissions): rows and bytes, against the estimate
    in docs/cloud.md "Station metrics" (~14,000 records, ~1.5 MB a day)."""
    clock = Clock(T0 + 86400)
    s, _ = store(tmp_path, clock)
    recs = []
    for m in range(1440):
        at = T0 + 60 * m
        for ch in ("FG1", "FG2", "South 2", "HPN Tower", "NY App", "HPN Ground"):
            recs.append({"k": "floor", "s": ch, "at": at, "n": 60, "open_s": 2, "hiss": 2.4, "q50": 1.2, "q90": 3.1,
                         "qmax": 5.5, "noise": 1,
                         **({"kind": "traffic", "mhz": 453.0375, "open": 10.0, "close": 6.0, "min": 0.3, "sdr": "00000102",
                             "gain": 36.4, "state": "ok"} if m % 15 == 0 else {})})
        for sysn in ("p25", "p25:mrrs"):
            recs.append({"k": "cc", "s": sysn, "at": at, "n": 20, "rate": 40.1, "lo": 38.0, "hi": 46.0, "state": "ok",
                         "sdr": "00000201"})
    for k in range(2000):
        recs.append({"k": "tx", "s": "FG1", "at": T0 + 43.2 * k, "dur": 2.8, "kind": "voice", "q": 18.5, "qo": 22.1,
                     "qc": 17.0, "hiss": 2.4, "why": "quiet"})
    for k in range(0, len(recs), 5000):
        s.ingest("pcfd-hq", recs[k:k + 5000])
    s.maintain()
    with s._lock:
        s._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = (tmp_path / "metrics.sqlite").stat().st_size
    assert len(recs) < 14_000 and size < 2.5e6, (len(recs), size)
    s.maintain(force_snapshot=True)
    gz = len(gzip.compress((tmp_path / SNAPSHOT_NAME).read_bytes(), 6))
    assert gz < size / 3, (gz, size)


# -- who may read it ---------------------------------------------------------------------------------------------------


def test_only_a_signed_in_admin_reads_metrics_and_it_is_closed_without_a_store(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    assert "/api/cad/metrics" in ADMIN_READS
    assert api.get("/api/cad/metrics", "", Identity(email=CASEY))[0] == 404      # not the hub: nothing here
    s, _ = store(tmp_path)
    s.ingest("t", floors(T0, [1.0]))
    cad.station_metrics = s
    for who in (None, Identity(email=""), Identity(email=JORDAN)):
        code, raw, _ = api.get("/api/cad/metrics", "subject=FG1", who)
        assert code == 403 and b"FG1" not in raw
    code, raw, _ = api.get("/api/cad/metrics", f"subject=FG1&from={T0}&to={T0 + 600}", Identity(email=CASEY))
    assert code == 200 and json.loads(raw)["rows"][0]["q90"] == 1.0
    code, raw, _ = api.get("/api/cad/metrics", "subject=FG1&res=nope", Identity(email=CASEY))
    assert code == 400
    code, raw, _ = api.get("/api/cad/metrics", "", Identity(email=CASEY))
    assert code == 200 and json.loads(raw)["series"][0]["subject"] == "FG1"
