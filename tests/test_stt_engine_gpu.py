"""A channel's engine "gpu" (sizeup/channels.py): the station skips whisper and hands the clip to the hub at once, the
hub gives it to a GPU worker first (the channel's model), then Amazon Transcribe (its budget allowing), then the
station's whisper; never dropped. A station from before "gpu" is sent "aws". Invented clips and texts; fakes on
127.0.0.1 only."""

from __future__ import annotations

import time

import pytest

from sizeup import channels, sttpool
from sizeup.stt import HIGH, LIVE, URGENT
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws, hub_post, traffic  # noqa: F401 (fake_aws: a fixture)
from tests.test_cloud import HubRun, all_traffic, fm_segment, hub_cfg, station, station_cfg
from tests.test_stt_pool import GPU_TEXT, Clock, FakeWorker, Jobs, online, pool


def test_gpu_is_an_engine_whatever_aws_can_do_and_an_old_station_gets_aws():
    assert "gpu" in channels.ENGINES and channels.clean("FG1", {"engine": "gpu"}) == {"engine": "gpu"}
    assert channels.effective({"engine": "gpu"}, False) == "gpu" == channels.effective({"engine": "gpu"}, True)
    assert channels.effective({"engine": "aws"}, False) == "local"                 # (unchanged)
    assert channels.effective({"engine": "gpu", "transcribe": False}, True) == "off"
    ov = {"FG1": {"engine": "gpu"}, "tg:307": {"engine": "aws"}}
    assert channels.for_station(ov, gpu_ok=False) == {"FG1": {"engine": "aws"}, "tg:307": {"engine": "aws"}}
    assert channels.for_station(ov) == ov


def test_a_gpu_channels_clip_is_taken_whatever_the_class_switch_and_its_pre_alert_needs_no_quick_record():
    clk = Clock()
    p = pool(clk, on=False)                                # (every class switched off)
    jobs = Jobs()
    w = online(p, "gpu-1")
    w.fast.extend([(clk.t, 7.0)] * 3)                      # slow on pre-alerts lately: never one without "gpu"
    assert not p.ready(HIGH, gpu_only=True)[0]
    assert p.ready(HIGH, gpu_only=True, forced=True) == (True, "")
    assert p.submit(jobs.job("c1", HIGH, gpu_only=True, forced=True))
    j = p.claim("gpu-1", 0)
    assert j.id == "c1" and j.public()["fast"] is True and j.expires - clk.t <= 4.0       # (the 4 s pre-alert lease)


def test_with_no_gpu_worker_a_gpu_channels_clip_isnt_taken_and_its_wait_still_bounds_it():
    clk = Clock()
    p, jobs = pool(clk), Jobs()
    assert p.ready(URGENT, forced=True) == (False, "no GPU worker ready")
    w = online(p, "gpu-1")
    w.leases = {"x"}                                       # busy: the clip waits its class's claim budget at most
    assert p.submit(jobs.job("c2", URGENT, gpu_only=True, forced=True))
    clk.t += sttpool.BUDGETS[URGENT][0]
    p.tick()
    p.drain()
    assert jobs.fell["c2"].startswith("no pool worker took it in")


def test_fire_traffic_on_another_channel_is_never_starved_by_a_gpu_channels_clip():
    clk = Clock()
    p, jobs = pool(clk), Jobs()
    online(p, "gpu-1")
    assert p.submit(jobs.job("gpu-live", LIVE, gpu_only=True, forced=True))
    assert p.submit(jobs.job("fire-urgent", URGENT))
    assert p.claim("gpu-1", 0).id == "fire-urgent"          # (the classes stay strict)


def _hub_and_station(tmp_path, fake, **kw):
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake, **kw)))
    st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
    assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
    assert hub_post(hub, "channels", {"action": "set", "key": "FG1", "engine": "gpu"})[0] == 200
    assert wait(lambda: st.channel_pref("FG1")["engine"] == "gpu", 10)
    return hub, st


def _row(hub, tid):
    return next((r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid), {})


def test_gpu_first_then_aws_then_the_station_never_dropped(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub, st = _hub_and_station(tmp_path, fake)
    w = None
    try:
        assert st.stt_engine("FG1") == "gpu"
        # 1. a GPU worker online: it transcribes (the pool's class switch for this class off makes no difference)
        token = hub.app.hub.workers.add("gpu-1", "Test box")
        hub.app.hub.workers.set_pool(classes={sttpool.class_key(URGENT): False, sttpool.class_key(HIGH): False,
                                              sttpool.class_key(LIVE): False})
        w = FakeWorker(hub.url, token).start()
        assert wait(lambda: hub.get("/api/state")["channels"].get("gpu", {}).get("online") == ["gpu-1"], 10)
        tid = fm_segment(st)
        assert wait(lambda: _row(hub, tid).get("text") == GPU_TEXT, 30), _row(hub, tid)
        assert "(GPU gpu-1)" in _row(hub, tid)["model"] and traffic(st, tid).status == "remote"
        assert hub.app.aws.health()["done"] == 0
        # 2. the worker gone: AWS
        w.close()
        w = None
        hub.app.sttpool.forget("gpu-1", "test")
        assert wait(lambda: not hub.get("/api/state")["channels"]["gpu"]["online"], 10)
        time.sleep(0.01)
        tid2 = fm_segment(st)
        assert wait(lambda: _row(hub, tid2).get("text") == AWS_TEXT + ".", 30), _row(hub, tid2)
        assert _row(hub, tid2)["model"] == "aws-transcribe"
        # 3. AWS out of budget too: the station still hands the clip over (gpu), and its whisper takes it, never dropped
        assert hub_post(hub, "stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0})[0] == 200
        assert wait(lambda: not st._hub_aws["ok"], 10)
        assert st.stt_engine("FG1") == "gpu"                                 # (whatever AWS can do)
        time.sleep(0.01)
        tid3 = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid3 and t["text"] == "whisper heard this"
                                for t in all_traffic(hub.get("/api/state"))), 30)
    finally:
        if w is not None:
            w.close()
        st.link.stop()
        hub.close()
        fake.close()


def test_a_station_from_before_gpu_is_sent_aws(tmp_path, monkeypatch):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    real = hub.app.channel_desired
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        applied = st.channels_applied

        def old():                                         # a station from before "gpu": no engine_gpu_ok
            d = applied()
            d.pop("engine_gpu_ok", None)
            return d
        st.channels_applied = old
        seen = lambda: ((hub.app.hub.status or {}).get("channels") or {})                 # noqa: E731
        assert wait(lambda: "prefs" in seen() and not seen().get("engine_gpu_ok"), 15)   # (its next state push)
        assert hub_post(hub, "channels", {"action": "set", "key": "FG1", "engine": "gpu"})[0] == 200
        assert wait(lambda: st.channel_pref("FG1")["engine"] == "aws", 15)
        assert wait(lambda: real()["prefs"]["FG1"]["engine"] == "aws", 5)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()
