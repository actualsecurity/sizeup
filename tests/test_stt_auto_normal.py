"""Auto · normal (the admin: "goes to the GPU workers first, but is queued behind the priority channels and then goes to
the stations if it can't hit the GPU, and then AWS"; sizeup/sttroute.py AUTO_NORM): a GPU that can start it within
NORM_GPU_BUDGET_S, queued after every priority clip and before background work (FIFO within its class), else the
stations, else AWS within its budget, else back to its own station. Invented clips and ids; fakes on 127.0.0.1 only."""

from __future__ import annotations

import time

import pytest

from sizeup import channels, sttpool, sttroute
from sizeup.stt import CATCHUP, LIVE, LOW, URGENT
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws, hub_post  # noqa: F401 (fake_aws: a fixture)
from tests.test_cloud import HubRun, hub_cfg, station_cfg
from tests.test_stt_engine_select import _set, st_app  # noqa: F401 (st_app: a fixture)
from tests.test_stt_pool import GPU_TEXT, LONG_POLL_S, Clock, FakeWorker, Jobs, online, pending_event, pool, send


def _row(hub, tid):
    return hub.app.cad.store.one("SELECT text, status, model, stt_route FROM radio_traffic WHERE id = ?", (tid,)) or {}


# -- the engine ------------------------------------------------------------------------------------------------------


def test_auto_normal_is_an_engine_with_no_migration_and_older_stations_get_the_nearest():
    assert channels.clean("tg:325", {"engine": "auto_norm"}) == {"engine": "auto_norm"}
    assert sttroute.mode_label("auto_norm") == "auto·normal" and sttroute.is_new("auto_norm")
    assert channels.effective({"engine": "auto_norm"}, False) == "auto_norm"
    ov = {"tg:325": {"engine": "auto_norm"}}
    assert channels.for_station(ov) == ov
    assert channels.for_station(ov, norm_ok=False) == {"tg:325": {"engine": "auto"}}     # (the selector, not this)
    assert channels.for_station(ov, select_ok=False) == {"tg:325": {"engine": "aws"}}    # (before the selector)
    assert channels.defaults("tg:325", context=lambda k: True)["engine"] == "auto_bg"     # (nothing moves to it)


# -- the pool: behind priority, ahead of background, FIFO ---------------------------------------------------------------


def test_normal_waits_behind_every_priority_clip_and_goes_ahead_of_background_fifo_within_its_class():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    clk.t += 10
    for cid, kw in (("bg", {"bg": True, "forced": True}), ("norm-old", {"norm": True, "forced": True}),
                    ("norm-new", {"norm": True, "forced": True}), ("prio-low", {"forced": True}),
                    ("catchup", {"bg": True})):
        clk.t += 0.1
        assert p.submit(jobs.job(cid, CATCHUP if cid == "catchup" else LOW, **kw)), cid
    clk.t += sttroute.BG_QUIET_S + sttpool.BG_STATION_FIRST_S
    got = []
    while (job := p.claim("gpu-1", 0)) is not None:
        got.append(job.id)
        assert p.complete("gpu-1", job.lease, {"text": "x"})
    # priority (any class) first; then Auto · normal, oldest first; then background and the catch-up
    assert got == ["prio-low", "norm-old", "norm-new", "bg", "catchup"]
    assert sttpool.rank(jobs.job("x", URGENT, norm=True)) == 1 and sttpool.rank(jobs.job("y", LIVE)) == 0


def test_a_gpu_that_cant_start_it_in_time_or_is_away_is_passed_over_with_why():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    assert p.norm_place(3.0) == (False, "no GPU worker online", -1.0)
    p.seen("gpu-1", "gaming")
    assert p.norm_place(3.0)[:2] == (False, "gpu-1 gaming")
    online(p, "gpu-1")
    assert p.norm_place(3.0, avoid={"gpu-1": "latency guard: p50 9.0 s"})[:2] == (False, "gpu-1 latency guard: p50 9.0 s")
    assert p.norm_place(3.0) == (True, "", 0.0)
    for i in range(15):                                   # 15 priority clips ahead x 2 s: 30 s, over its 25 s budget
        assert p.submit(jobs.job(f"prio-{i}", LOW, forced=True)) or True
    ok, why, est = p.norm_place(3.0, budget=sttroute.NORM_GPU_BUDGET_S)
    assert not ok and why.startswith("GPU busy") and est > sttroute.NORM_GPU_BUDGET_S


# -- the hub: each step ------------------------------------------------------------------------------------------------


def _norm_hub(tmp_path, fake, monkeypatch):
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    _set(hub, "tg:325", "auto_norm")
    asked = []
    monkeypatch.setattr(hub.app.hub, "remote", lambda fn, args, *a, **k: asked.append((fn, args)) or 1)
    return hub, asked


def _ev(tid, seq=1):
    ev = pending_event(tid, seq=seq)
    ev["data"].update(source="Neighbor FD", talkgroup=325)
    return ev


def test_gpu_first_then_its_station_then_another_then_aws_then_back(tmp_path, monkeypatch):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub, asked = _norm_hub(tmp_path, fake, monkeypatch)
    w = None
    try:
        token = hub.app.hub.workers.add("gpu-1")
        w = FakeWorker(hub.url, token, wait_s=LONG_POLL_S).start()
        assert wait(lambda: hub.app.sttpool.norm_place(3.0)[0], 5)
        # 1. a GPU that can start it: the GPU
        send(hub, _ev("TG325-1"))
        assert wait(lambda: _row(hub, "TG325-1").get("text") == GPU_TEXT, 30), _row(hub, "TG325-1")
        assert wait(lambda: _row(hub, "TG325-1").get("stt_route") == "auto·normal: gpu-1", 5), _row(hub, "TG325-1")
        # 2. the GPU busy past its budget: its own station's whisper, when it can start it in time
        monkeypatch.setattr(hub.app.sttpool, "norm_place", lambda *a, **k: (False, "GPU busy (about 40 s ahead)", 40.0))
        hub.app.hub.status = {"health": {"stt_queue": {"live": 0, "low": 2, "service_s": 18.0}}}
        monkeypatch.setattr(hub.app.hub, "connected", lambda: True)
        send(hub, _ev("TG325-2", seq=2))
        assert wait(lambda: ("transcribe_now", ["TG325-2"]) in asked, 10), asked
        assert hub.app.stt_chosen("TG325-2") == "station" and _row(hub, "TG325-2")["status"] == "pending"   # (shown)
        # 3. its station behind too: another pooled station that can start it
        hub.app.hub.status = {"health": {"stt_queue": {"live": 3, "low": 10, "service_s": 18.0}}}
        monkeypatch.setattr(hub.app.hub.workers, "stations_on", lambda sid=None: True)
        hub.app.sttpool.allowed = lambda wid: True
        st = hub.app.sttpool.seen("station:west", "online", kind="station",
                                  load={"own": 1, "service_s": 18.0, "max_s": 60.0})
        st.polling = 1
        send(hub, _ev("TG325-3", seq=3))
        assert wait(lambda: hub.app.stt_chosen("TG325-3") == "stations", 10), hub.app.stt_chosen("TG325-3")
        # 4. no station can: AWS within its budget
        st.load["own"] = 20
        send(hub, _ev("TG325-4", seq=4))
        assert wait(lambda: _row(hub, "TG325-4").get("text") == AWS_TEXT + ".", 30), _row(hub, "TG325-4")
        assert wait(lambda: _row(hub, "TG325-4").get("stt_route", "").startswith("auto·normal: GPU → station"), 5), \
            _row(hub, "TG325-4")
        # 5. AWS over its budget: back to its own station's queue (never dropped)
        hub.app.aws.budget = lambda: {"pct": 95.0}
        send(hub, _ev("TG325-5", seq=5))
        assert wait(lambda: ("transcribe_now", ["TG325-5"]) in asked, 10), asked
        h = hub.app.bg_health()
        assert h["norm_h"] == {"gpu": 1, "station": 2, "aws": 1, "back": 1}, h
        assert h["h"] == {}                                   # (Auto · background's own line: untouched)
    finally:
        if w is not None:
            w.close()
        hub.close()
        fake.close()


def test_a_normal_clip_never_holds_up_a_priority_clip_and_the_dropdown_shows_it(tmp_path, monkeypatch):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub, _asked = _norm_hub(tmp_path, fake, monkeypatch)
    try:
        hub.app.hub.workers.add("gpu-1")
        online(hub.app.sttpool, "gpu-1")
        opts = {o["value"]: o for o in hub.app.engine_options()["options"]}
        assert [o["value"] for o in hub.app.engine_options()["options"]][:3] == ["auto", "auto_norm", "auto_bg"]
        assert opts["auto_norm"]["label"] == "Auto · normal" and opts["auto_norm"]["state"] == "gpu-1"
        assert opts["auto_norm"]["title"].startswith("GPU (after priority) → stations → AWS")
        hub.app.sttpool.seen("gpu-1", "gaming")
        opts = {o["value"]: o for o in hub.app.engine_options()["options"]}
        assert "gpu-1 gaming" in opts["auto_norm"]["title"]
    finally:
        hub.close()
        fake.close()


def test_a_station_hands_auto_normal_over_while_a_gpu_is_ready_else_keeps_it(st_app, monkeypatch):
    app = st_app
    monkeypatch.setattr(app.link, "pool_gpu_ready", lambda: True)
    assert app._stt_way("auto_norm", "tg:325") == "aws"                 # the hub's: a GPU after the priority work
    monkeypatch.setattr(app.link, "pool_gpu_ready", lambda: False)
    assert app._stt_way("auto_norm", "tg:325") == "local_bg"            # its own whisper first, the hub's once behind
    assert app.channels_applied()["engine_norm_ok"] is True


def test_the_ui_lists_auto_normal_with_its_hover_line():
    rc = open("sizeup/web/static/radio-controls.js").read()
    assert '["auto_norm", "Auto · normal"]' in rc and "Auto · normal: GPU (after priority) → stations → AWS" in rc
    assert "Auto · normal" in open("sizeup/web/static/health.js").read()


def test_review_a_pre_alert_class_normal_clip_on_the_stations_is_claimable_and_blocks_no_priority_clip():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    st = p.seen("station:west", "online", kind="station", load={"own": 0, "service_s": 18.0, "max_s": 60.0})
    st.polling = 1
    online(p, "gpu-1")
    j = jobs.job("ctx-norm", sttpool.CONTEXT, forced=True, norm=True,
                 extra={"station_only": True, "claim_wait": sttroute.NORM_STATION_BUDGET_S, "origin": "east"})
    assert p.submit(j)
    assert p.ready_forced(URGENT) == (True, "")                      # (a priority page: never "a pre-alert waits")
    assert p.claim("station:west", 0).id == "ctx-norm"


def test_review_a_gpu_decoding_normal_work_is_busy_for_priority_and_long_clips_stay_off_the_gpu():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    assert p.submit(jobs.job("norm", LOW, seconds=3.0, forced=True, norm=True))
    assert p.claim("gpu-1", 0).id == "norm"
    assert p.ready_forced(LIVE) == (False, "gpu-1 busy (normal)")       # (never queued behind its decode: AWS)
    assert p.submit(jobs.job("prio", LIVE)) is True or True
    assert p.claim("gpu-1", 0) is None                                   # (nothing leased behind the normal decode)
    assert p.norm_place(120.0)[:2] == (False, "too long for the GPU after priority work (120 s)")


def test_review_station_only_work_never_makes_the_gpu_look_busy():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    for i in range(15):
        assert p.submit(jobs.job(f"st-{i}", LOW, forced=True, norm=True,
                                 extra={"station_only": True, "claim_wait": 90.0}))
    assert p.norm_place(3.0) == (True, "", 0.0)


def test_review_normal_clips_never_feed_the_latency_guard_and_a_promoted_one_goes_priority(tmp_path, monkeypatch):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub, _asked = _norm_hub(tmp_path, fake, monkeypatch)
    try:
        hub.app.hub.workers.add("gpu-1")
        now = time.time()
        job = sttpool.Job("TG325-9", LIVE, "tg:325", "traffic", 3.0, audio=lambda: b"", on_done=hub.app._pool_done,
                          on_fallback=hub.app._pool_fallback, clip_end=now - 12.0, norm=True, forced=True,
                          extra={"kind": "gpu"})
        job.worker, job.claimed_at, job.queued_at = "gpu-1", now - 1.0, now - 2.0
        hub.app._pool_done(job, {"text": "words heard"})
        rows = list(hub.app.stt_engines._rows.get("gpu-1", ()))
        assert rows and not any(r[3] for r in rows)                       # (sampled, never as a priority one)
        assert hub.app.stt_engines.tripped("gpu-1") == ""
        monkeypatch.setattr(hub.app, "promoted_keys", lambda: frozenset({"tg:325"}))
        assert hub.app._hub_engine("tg:325") == "auto"                    # (an incident on it: Auto · priority)
    finally:
        hub.close()
        fake.close()
