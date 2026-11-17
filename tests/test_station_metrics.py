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
from sizeup import fence as fence_mod
from sizeup.cloud.metricsbackup import SLOTS, S3Copies, next_slot
from sizeup.cloud.metricstore import (FIRST_UPLOAD_S, HOURLY_DAYS, RAW_DAYS, SNAPSHOT_S, MetricsError, MetricStore)
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
    assert cl.posts[0]["station"] == "st-test" and len(cl.posts[0]["records"]) == 10 and "dropped" not in cl.posts[0]
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
    from sizeup.app import make_app
    from tests.test_cloud import station_cfg
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="t"))
    st = make_app(station_cfg(tmp_path, hub.url))   # the link's threads not started: no long poll outlives the test
    try:
        assert isinstance(st.metrics, Collector) and st.link.metrics_link.c is st.metrics
        queued = st.link.outbox.pending()[0]
        seg = fg1_segment()
        seg.start, seg.end = time.time() - 15, time.time() - 3
        st.metrics.tx("FG1", "voice", seg)
        link = st.link.metrics_link
        link._next = 0.0
        client = HubClient(hub.url, TOKEN)
        link.step(client)
        assert link.c.pending() == 2 and link.error == ""          # a new store: what it acked, again (resend_from 0)
        link.step(client, now=time.time() + 1)
        assert link.c.pending() == 0 and link.error == ""          # ... once: duplicates ignored
        kinds = {x["kind"] for x in hub.app.metric_store.index()["series"]}
        assert "tx" in kinds
        assert st.link.outbox.pending()[0] == queued              # the outbox: untouched by metrics
        assert "metrics" in st.link.status(talkgroups=False)
    finally:
        st.link.stop()
        hub.close()


def post_raw(url, body: bytes, token, gz=True):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + PATH, data=gzip.compress(body) if gz else body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          **({"Content-Encoding": "gzip"} if gz else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_metrics_never_mark_the_station_seen_bad_batches_are_400_and_a_fenced_store_is_503(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="t"))
    try:
        h = hub.app.hub
        other = h.stations.add("st-b")
        rec = {"k": "floor", "s": "FG1", "at": time.time() - 60, "q90": 2.0}
        for tok, sid in ((other, "st-b"), (TOKEN, "t")):
            code, out = post_raw(hub.url, json.dumps({"station": sid, "records": [rec]}).encode(), tok)
            assert code == 200 and out["accepted"] == 1
        assert h.last_seen is None and not h.connected()            # neither a second station nor metrics alone
        for bad in (b"{not json", b"[" * 100_000 + b"]" * 100_000, b'["a list"]',
                    json.dumps({"records": "x"}).encode()):
            code, _ = post_raw(hub.url, bad, TOKEN)
            assert code == 400, bad[:20]                            # the station drops these, never resends them
        huge = json.dumps({"records": [dict(rec, at=10 ** 400), dict(rec, q90=10 ** 400, at=time.time() - 30)]}).encode()
        code, out = post_raw(hub.url, huge, TOKEN)
        assert code == 200 and out["skipped"] == 1 and out["accepted"] == 1
        code, _ = post_raw(hub.url, b"{" + b" " * (5 * 1024 * 1024) + b"}", TOKEN)
        assert code == 413                                          # unpacked over 4 MB
        hub.app.metric_store.may_write = lambda: False              # the lease went between the gate and the write
        code, _ = post_raw(hub.url, json.dumps({"records": [rec]}).encode(), TOKEN)
        assert code == 503                                          # the station sends it again, to the leader
    finally:
        hub.close()


def test_one_bad_channel_costs_only_its_own_minute_and_the_sender_survives(monkeypatch):
    import threading as th
    from sizeup.cloud import metrics as metrics_mod
    clock = Clock()
    good, bad = receiver("FG1"), receiver("FG2")

    class Broken:
        open = False
        quieting_db = hiss_db = 1.0

        @property
        def params(self):
            raise RuntimeError("half swapped")
    bad.squelch = Broken()
    logs = []
    app = fake_app([good, bad])
    c = Collector(clock=clock)
    s = Sampler(app, c, clock, log=logs.append)
    s.tick()
    clock.t = T0 + 60
    s.tick()
    clock.t = T0 + 120
    s.tick()
    floors_ = [(x["s"], x["at"]) for x in c.batch()[1] if x["k"] == "floor"]
    assert floors_ == [("FG1", T0), ("FG1", T0 + 60)]               # FG2 skipped, the minutes go on
    assert len([m for m in logs if "FG2" in m]) == 2                # said once for its floor, once for its events
    monkeypatch.setattr(metrics_mod, "SEND_S", 0.01)
    link = MetricsLink(SimpleNamespace(metrics=None), SimpleNamespace(station_id="st-test"), lambda t: None,
                       log=logs.append, clock=clock)
    n = {"steps": 0}

    def step(client, now=None):
        n["steps"] += 1
        if n["steps"] == 1:
            raise KeyError("boom")
        link.stop()
        return 0.01
    link.step = step
    t = th.Thread(target=link._send)
    t.start()
    t.join(5)
    assert not t.is_alive() and n["steps"] == 2 and link.error == "sender: KeyError"


# -- the hub: kept ----------------------------------------------------------------------------------------------------


def store(tmp_path, clock=None, leader=True, audit=None, backup=None):
    clock = clock or Clock(T0 + 3 * 3600)
    s = MetricStore(tmp_path / "metrics.sqlite", may_write=lambda: leader, log=lambda m: None, clock=clock, audit=audit,
                    backup=backup)
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


class FakeCopies:
    """The bucket's copies (sizeup/cloud/metricsbackup.py S3Copies' face), with failures on demand."""

    def __init__(self, clock):
        self.clock, self.slots, self.puts = clock, {}, []
        self.fail_list = self.fail_get = None

    def list(self):
        if self.fail_list:
            raise self.fail_list
        return sorted(({"slot": k, "key": f"copy-{k}", "at": at, "size": len(raw)} for k, (at, raw) in self.slots.items()),
                      key=lambda c: c["at"], reverse=True)

    def get(self, slot):
        if self.fail_get and slot in self.fail_get:
            raise OSError("S3 GET 500")
        return self.slots[slot][1]

    def put(self, slot, raw):
        self.slots[slot] = (self.clock(), raw)
        self.puts.append(slot)
        return len(raw) // 3


def raw_count(s):
    return s._read("SELECT COUNT(*) FROM raw")[0][0]


def test_a_new_task_that_cant_list_the_copies_never_overwrites_them(tmp_path):
    """The review's case: a fresh task whose look at the bucket fails takes posts and runs its janitor, but uploads
    nothing until it has merged the history in (or seen there is none), and merges it once it can."""
    clock = Clock(T0 + 3600)
    bucket = FakeCopies(clock)
    old, _ = store(tmp_path / "old", clock, backup=bucket)
    old.ingest("t", floors(T0, [1.0, 2.0, 3.0]))
    bucket.fail_list = None
    assert old.backup_turn() == "none" and old.resend_from == 0.0       # a listing that worked, and no copies
    clock.t += FIRST_UPLOAD_S
    assert old.backup_turn() == "uploaded" and bucket.puts == [0]
    good = bucket.slots[0][1]
    # a new task: the bucket can't be listed (a 500), then posts come in and the janitor runs, for hours
    new, _ = store(tmp_path / "new", clock, backup=bucket)
    bucket.fail_list = OSError("S3 GET 500")
    first = new.ingest("t", floors(T0 + 1800, [9.0]))
    assert "resend_from" not in first                                     # not settled: nothing to tell yet
    for _ in range(12):
        clock.t += 900
        new.maintain()
        new.backup_turn()
    assert bucket.puts == [0] and bucket.slots[0][1] == good and not new.settled   # history untouched
    assert new.backup_status["error"].startswith("OSError")
    bucket.fail_list = None
    clock.t += 3600
    assert new.backup_turn() == "restored" and raw_count(new) == 4       # its 3 rows and the new one
    assert new.ingest("t", floors(T0 + 1860, [9.0]))["resend_from"] == pytest.approx(T0 + 3600 + FIRST_UPLOAD_S)
    clock.t += FIRST_UPLOAD_S
    assert new.backup_turn() == "uploaded" and bucket.puts == [0, 1]       # a new slot: the old copy stays too


def test_copies_go_round_robin_over_the_oldest_and_an_unreadable_newest_falls_back(tmp_path):
    clock = Clock(T0)
    bucket = FakeCopies(clock)
    s, _ = store(tmp_path, clock, backup=bucket)
    s.ingest("t", floors(T0, [1.0]))
    s.backup_turn()
    for k in range(SLOTS + 2):
        clock.t += SNAPSHOT_S + FIRST_UPLOAD_S
        assert s.backup_turn() == "uploaded"
    assert bucket.puts == list(range(SLOTS)) + [0, 1]                    # the last SLOTS copies are always there
    assert next_slot(bucket.list()) == 2
    bucket.fail_get = {1}                                               # the newest can't be read: the one before
    other, _ = store(tmp_path / "x", clock, backup=bucket)
    assert other.backup_turn() == "restored" and raw_count(other) == 1
    bucket.fail_get = set(range(SLOTS))                                  # none can: not settled, tried again later
    third, _ = store(tmp_path / "y", clock, backup=bucket)
    assert third.backup_turn() == "failed" and not third.settled and third.backup_turn() == ""   # (backing off)


class GzCopies(FakeCopies):
    """As S3Copies keeps them: gzipped, unpacked on the way back (a torn one raises from gzip / zlib)."""

    def get(self, slot):
        return gzip.decompress(super().get(slot))

    def put(self, slot, raw):
        return super().put(slot, gzip.compress(raw))


def test_a_deflate_corrupt_newest_copy_settles_from_an_older_one(tmp_path):
    import zlib
    clock = Clock(T0)
    bucket = GzCopies(clock)
    a, _ = store(tmp_path / "a", clock, backup=bucket)
    a.ingest("t", floors(T0, [1.0, 2.0]))
    a.backup_turn()
    for _ in range(3):
        clock.t += FIRST_UPLOAD_S + SNAPSHOT_S
        a.backup_turn()
    newest = bucket.list()[0]["slot"]
    at, gz = bucket.slots[newest]
    torn = gz[:20] + bytes(b ^ 0x5A for b in gz[20:60]) + gz[60:]          # the deflate stream broken, the header fine
    with pytest.raises(zlib.error):
        gzip.decompress(torn)
    bucket.slots[newest] = (at, torn)
    b, _ = store(tmp_path / "b", clock, backup=bucket)
    assert b.backup_turn() == "restored" and raw_count(b) == 2 and b.settled


def copy_with(tmp_path, clock, sql: list[str]) -> bytes:
    """A copy of a store with two floor records, changed by sql (another release's schema)."""
    s, _ = store(tmp_path, clock)
    s.ingest("t", floors(T0, [1.0, 2.0]) + [{"k": "ev", "s": "FG1", "at": T0 + 5, "ev": "gain", "d": "x"}])
    s.maintain()
    p = tmp_path / "copy.sqlite"
    p.write_bytes(s.snapshot_bytes())
    with sqlite3.connect(p) as db:
        for q in sql:
            db.execute(q)
    return p.read_bytes()


def test_a_copy_with_a_column_more_or_less_still_merges(tmp_path):
    clock = Clock(T0 + 3600)
    newer = copy_with(tmp_path / "n", clock, ["ALTER TABLE raw ADD COLUMN extra TEXT", "ALTER TABLE series ADD COLUMN note TEXT"])
    older = copy_with(tmp_path / "o", clock, [
        "CREATE TABLE h2 (station TEXT NOT NULL, subject TEXT NOT NULL, metric TEXT NOT NULL, hour INTEGER NOT NULL, "
        "n INTEGER NOT NULL, lo REAL, p50 REAL, hi REAL, PRIMARY KEY (station, subject, metric, hour)) WITHOUT ROWID",
        "INSERT INTO h2 SELECT station, subject, metric, hour, n, lo, p50, hi FROM hourly", "DROP TABLE hourly",
        "ALTER TABLE h2 RENAME TO hourly", "ALTER TABLE events DROP COLUMN detail"])
    no_key = copy_with(tmp_path / "k", clock, [
        "CREATE TABLE e2 (station TEXT NOT NULL, subject TEXT NOT NULL, at REAL NOT NULL, detail TEXT, "
        "PRIMARY KEY (station, subject, at)) WITHOUT ROWID", "INSERT INTO e2 SELECT station, subject, at, detail FROM events",
        "DROP TABLE events", "ALTER TABLE e2 RENAME TO events"])
    for name, data in (("newer", newer), ("older", older), ("no_key", no_key)):
        s, _ = store(tmp_path / name, clock)
        n, _at = s.merge(data)
        assert raw_count(s) == 2 and s.index()["series"][0]["first"] == T0, name
        events = s._read("SELECT COUNT(*) FROM events")[0][0]
        assert events == (0 if name == "no_key" else 1), name      # a table lacking a key column: left out, the rest in
        if name == "older":
            assert s._read("SELECT p90 FROM hourly LIMIT 1")[0][0] is None and s._read("SELECT detail FROM events")[0][0] == ""


def test_health_warns_when_the_copies_stall(tmp_path):
    clock = Clock(T0)
    bucket = FakeCopies(clock)
    s, _ = store(tmp_path, clock, backup=bucket)
    assert s.health() is None                                    # not led yet
    bucket.fail_list = OSError("S3 GET 500")
    s.backup_turn()
    h = s.health()
    assert h["settled"] is False and h["unsettled_since"] == T0 and h["warn"] == "" and "500" in h["error"]
    clock.t += 7 * 3600
    assert s.health()["warn"].startswith("metrics copies not restored for 7 h: OSError: S3 GET 500")
    bucket.fail_list = None
    s.backup_turn(now=clock.t + 3600)                            # (past its backoff)
    clock.t += 3600
    assert s.health()["settled"] and s.health()["warn"] == ""
    clock.t += 13 * 3600                                         # uploads failing since
    bucket.put = lambda slot, raw: (_ for _ in ()).throw(OSError("S3 PUT 403"))
    s.backup_turn()
    assert s.health()["warn"].startswith("no metrics copy kept for 13 h")


def test_the_copies_warning_is_a_health_hint_on_the_hub(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        st = hub.app.metric_store
        st.backup = FakeCopies(st.clock)
        st.backup.fail_list = OSError("S3 GET 500")
        st.backup_turn(now=time.time() - 7 * 3600)
        hints = hub.app.health.model()["hints"]
        assert [h["level"] for h in hints if h["channel"] == "Metrics"] == ["warn"]
        assert hub.app.health.model()["metrics_backup"]["settled"] is False
    finally:
        hub.close()


def test_a_restart_in_place_is_settled_and_asks_for_nothing(tmp_path):
    clock = Clock(T0)
    bucket = FakeCopies(clock)
    a, _ = store(tmp_path, clock, backup=bucket)
    a.ingest("t", floors(T0, [1.0]))
    a.backup_turn()
    a.close()
    b, _ = store(tmp_path, clock, backup=bucket)
    bucket.fail_list = OSError("down")
    assert "resend_from" not in b.ingest("t", floors(T0 + 60, [1.0])) and b.settled


def test_a_merged_copy_keeps_the_widest_series_and_retention_keeps_rollup_coverage(tmp_path):
    clock = Clock(T0 + 3600)
    bucket = FakeCopies(clock)
    a, _ = store(tmp_path / "a", clock, backup=bucket)
    a.ingest("t", floors(T0 - 20 * 86400, [1.0]) + floors(T0, [1.0]))
    a.backup_turn()
    clock.t += FIRST_UPLOAD_S
    a.backup_turn()
    b, _ = store(tmp_path / "b", clock, backup=bucket)
    b.ingest("t", floors(T0 + 1200, [2.0]))
    b.backup_turn()
    [sr] = b.index()["series"]
    assert sr["first"] == T0 - 20 * 86400 and sr["last"] == T0 + 1200       # min / max of both
    b.maintain()
    clock.t = T0 + (RAW_DAYS + 1) * 86400
    b.maintain()
    [sr] = b.index()["series"]
    assert sr["first"] == T0 - 20 * 86400 <= sr["last"]                     # its rollups still cover it
    assert b.query({"subject": "FG1", "from": T0 - 21 * 86400, "to": T0 - 19 * 86400, "res": "hour"})["rows"]


def test_a_standby_writes_nothing_and_nothing_is_in_litestream_or_the_handoff(tmp_path):
    s, _ = store(tmp_path, leader=False, backup=FakeCopies(Clock()))
    with pytest.raises(fence_mod.FencedWrite):                           # a 503: the station sends it again
        s.ingest("t", floors(T0, [1.0]))
    assert s.maintain() == {} and s.backup_turn() == "" and s.query({"subject": "FG1"}) == {
        "subject": "FG1", "station": None, "from": pytest.approx(s.clock() - 86400), "to": s.clock(), "res": "raw",
        "rows": [], "events": [], "stations": []}
    assert s.index() == {"series": []} and not (tmp_path / "metrics.sqlite").exists()    # a read never makes the file
    assert "metrics" not in (Path(hub_mod.__file__).parent.parent.parent / "deploy" / "hub" / "litestream.yml").read_text()
    from sizeup.handoff import DBS
    assert "metrics" not in DBS                                         # nothing added to a drain or a takeover


def test_the_s3_copies_list_put_and_get_and_a_failed_listing_raises():
    calls, objects = [], {}

    def send(method, url, headers, body, timeout):
        calls.append((method, url))
        if "fail" in objects:
            return 500, b"<Error><Code>InternalError</Code></Error>"
        if method == "PUT":
            objects[url.rsplit("/", 1)[1]] = bytes(body)
            return 200, b""
        if method == "GET" and "list-type=2" in url:
            items = "".join(f"<Contents><Key>hub/metrics/{k}</Key><LastModified>2026-11-16T0{n}:00:00.000Z</LastModified>"
                            f"<Size>{len(v)}</Size></Contents>" for n, (k, v) in enumerate(sorted(objects.items())))
            items += "<Contents><Key>hub/metrics/stray.txt</Key><LastModified>2026-11-16T09:00:00Z</LastModified><Size>1</Size></Contents>"
            return 200, f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}</ListBucketResult>'.encode()
        return 200, objects[url.rsplit("/", 1)[1]]

    from sizeup.aws import Credentials
    c = S3Copies("bucket", "hub", "us-east-1", send=send, creds=lambda: Credentials("AKIDEXAMPLE", "secret"))
    assert c.list() == []
    c.put(0, b"SQLite format 3\x00 zero")
    c.put(3, b"SQLite format 3\x00 three")
    got = c.list()
    assert [x["slot"] for x in got] == [3, 0] and c.get(3) == b"SQLite format 3\x00 three"
    assert all(u.startswith("https://bucket.s3.us-east-1.amazonaws.com/") for _m, u in calls)
    objects["fail"] = b""
    with pytest.raises(Exception, match="500"):
        c.list()


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
    gz = len(gzip.compress(s.snapshot_bytes(), 6))
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
