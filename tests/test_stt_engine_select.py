"""The engine selector (#875, sizeup/sttroute.py) and its no-slowdown guards (#878): Auto · priority, Auto ·
background, engines pinned by id, Compare; the migration that changes nothing; a station from before it; the latency
guard; the route each transcript records; the dropdown's live state. Invented clips, ids and texts; fakes on 127.0.0.1
only, never AWS."""

from __future__ import annotations

import json
import time
import pytest

from sizeup import channels, sttpool, sttroute
from sizeup.app import make_app
from sizeup.stt import CATCHUP, HIGH, LIVE, LOW, URGENT
from sizeup.sttroute import EngineBook, Route, Routes
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws, hub_post  # noqa: F401 (fake_aws: a fixture)
from tests.test_cloud import HubRun, hub_cfg, station_cfg
from tests.test_stt_pool import GPU_TEXT, Clock, FakeWorker, Jobs, online, pending_event, pool, quick, send  # noqa: F401


# -- the engines and the migration -----------------------------------------------------------------------------------


def test_the_engines_are_checked_and_the_old_ones_mean_what_they_meant():
    for eng in ("auto", "auto_bg", "pin:gpu-pc", "pin:station:north", "pin:aws", "local", "aws", "both"):
        assert channels.clean("FG1", {"engine": eng}) == {"engine": eng}
    for bad in ("gpu", "pin:", "pin:Bad Id", "pin:station:", "auto ", "pin:gpu-pc; rm"):
        with pytest.raises(ValueError, match="engine: one of"):
            channels.clean("FG1", {"engine": bad})
    # what runs: the old engines exactly as before (AWS off: local), the selector's whatever AWS can do (their own order)
    assert channels.effective({"engine": "aws"}, False) == "local" and channels.effective({"engine": "both"}, False) == "local"
    assert channels.effective({"engine": "aws"}, True) == "aws" and channels.effective({"engine": "local"}, True) == "local"
    for eng in ("auto", "auto_bg", "pin:gpu-pc", "pin:aws"):
        assert channels.effective({"engine": eng}, False) == eng == channels.effective({"engine": eng}, True)
    assert channels.effective({"engine": "auto", "transcribe": False}, True) == "off"


def test_migration_rewrites_nothing_and_an_old_station_gets_the_nearest_old_engine():
    ov = {"FG1": {"engine": "auto"}, "tg:307": {"engine": "aws"}, "p25:mrrs:9211": {"engine": "auto_bg"},
          "FG2": {"engine": "pin:gpu-pc"}, "South 2": {"engine": "pin:aws"}, "tg:300": {"engine": "local"},
          "tg:301": {"engine": "both", "transcribe": True}}
    assert channels.for_station(ov) == {**ov, "tg:307": {"engine": "pin:aws"}}     # as set ("aws": pinned AWS, #896)
    old = channels.for_station(ov, select_ok=False)
    assert old == {"FG1": {"engine": "aws"}, "tg:307": {"engine": "aws"}, "p25:mrrs:9211": {"engine": "local"},
                   "FG2": {"engine": "aws"}, "South 2": {"engine": "aws"}, "tg:300": {"engine": "local"},
                   "tg:301": {"engine": "both", "transcribe": True}}
    channels.check_map(old)                                                 # (what an old station's check takes)
    assert all(sttroute.legacy(e) == e for e in ("local", "aws", "both"))   # the old ones: untouched
    # the [stt] aws_channels default is pinned AWS (#896: "aws" is never the default nor stored again)
    assert channels.defaults("FG1")["engine"] == "pin:aws" and channels.defaults("tg:999")["engine"] == "local"
    assert sttroute.mode_label("aws") == "AWS" and sttroute.mode_label("pin:aws") == "pinned AWS"
    assert sttroute.mode_label("auto") == "auto·priority" and sttroute.mode_label("pin:station:north") == "pinned north"


def test_a_route_says_the_way_the_text_went():
    r = Route("auto·priority")
    r.note("gpu-pc", "went gaming")
    assert r.text("AWS") == "auto·priority: gpu-pc → AWS (gpu-pc: went gaming)"
    r2 = Route("auto·priority")
    r2.note("GPU", "no GPU worker online")
    r2.note("AWS", "the monthly limit reached")
    assert r2.text("station north") == ("auto·priority: GPU → AWS → station north (GPU: no GPU worker online; "
                                        "AWS: the monthly limit reached)")
    assert Route("pinned gpu-pc").text("gpu-pc") == "pinned gpu-pc"
    rs = Routes()
    rs.start("c1", "auto·priority")
    rs.note("c1", "gpu-pc", "gpu-pc busy (background)")
    assert rs.finish("c1", "AWS") == "auto·priority: gpu-pc → AWS (gpu-pc busy (background))"
    assert rs.finish("c1", "AWS") == "" and rs.finish("never", "AWS") == ""


# -- the pool: forced, pinned, background -----------------------------------------------------------------------------


def test_auto_priority_is_taken_whatever_the_class_switch_but_the_speed_gate_and_the_pool_switch_hold():
    clk = Clock()
    p = pool(clk, on=False)                                 # (every class switch off)
    jobs = Jobs()
    w = online(p, "gpu-1")
    assert not p.ready(HIGH, gpu_only=True)[0] and p.ready_forced(HIGH) == (True, "")
    assert p.submit(jobs.job("c1", HIGH, gpu_only=True, forced=True))
    j = p.claim("gpu-1", 0)
    assert j.id == "c1" and j.public()["fast"] is True and j.expires - clk.t <= 4.0    # (the 4 s pre-alert lease)
    assert p.complete("gpu-1", j.lease, {"text": "x"})
    # the pool's pre-alert speed gate holds for a channel's engine too: a worker at 6 s lately gets no page
    w.fast.extend([(clk.t, 6.0)] * 3)
    assert p.ready(URGENT, gpu_only=True)[0] is False
    assert p.ready_forced(URGENT) == (False, "gpu-1 slow on pre-alert clips lately (6.0 s)")
    assert not p.submit(jobs.job("page", URGENT, forced=True))
    # ... except AWS can't take it either (queue_ok): the slow GPU still beats sending it back to the station
    assert p.ready_forced(URGENT, queue_ok=True) == (True, "")
    assert p.submit(jobs.job("page2", URGENT, forced=True, extra={"queue_ok": True}))
    assert p.claim("gpu-1", 0).id == "page2"
    off = sttpool.Pool(enabled=lambda c: True, clock=clk, log=lambda s: None, on=lambda: False)
    online(off, "gpu-1")
    assert off.ready_forced(HIGH) == (False, "pool off")


def test_each_reason_a_gpu_is_passed_over_is_said():
    clk = Clock()
    p = pool(clk)
    assert p.ready_forced(URGENT) == (False, "no GPU worker online")
    assert p.ready_forced(URGENT, pin="gpu-pc") == (False, "gpu-pc not connected")
    p.seen("gpu-pc", "gaming")
    assert p.ready_forced(URGENT) == (False, "gpu-pc gaming")
    online(p, "gpu-pc")
    assert p.ready_forced(URGENT, avoid={"gpu-pc": "latency guard: p50 7.0 s"}) == \
        (False, "gpu-pc latency guard: p50 7.0 s")


def test_background_never_delays_a_priority_clip_on_the_gpu(monkeypatch):
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    # background on an idle GPU, nothing else there and no priority clip lately: taken
    assert p.ready_forced(LOW, bg=True, seconds=3.0) == (True, "")
    assert p.submit(jobs.job("bg-1", LOW, forced=True, bg=True))
    bg = p.claim("gpu-1", 0)
    assert bg.id == "bg-1"
    # a pre-alert clip now: never queued behind it (it would wait): AWS at once, said why
    assert p.ready_forced(URGENT) == (False, "gpu-1 busy (background)")
    assert not p.submit(jobs.job("page", URGENT, forced=True))
    assert p.complete("gpu-1", bg.lease, {"text": "x"})
    # priority and background both waiting: the priority one first, whatever its class or age
    clk.t += 10
    assert p.submit(jobs.job("bg-old", LOW, forced=True, bg=True))
    clk.t += 1
    assert p.submit(jobs.job("live", LIVE, forced=True))
    assert p.claim("gpu-1", 0).id == "live"
    # background stays off the GPU while priority work waits there, and BG_QUIET_S after a priority clip came in
    assert p.claim("gpu-1", 0) is None                      # (the worker still holds "live")
    p._leases.clear()
    p.workers["gpu-1"].leases.clear()
    assert p.claim("gpu-1", 0) is None                      # (quiet: "live" came in under BG_QUIET_S ago)
    assert not p.word("st")["gpu_bg"]
    clk.t += sttroute.BG_QUIET_S
    assert p.word("st")["gpu_bg"] is True                   # (quiet over, only background waits)
    assert p.claim("gpu-1", 0).id == "bg-old"


def test_the_catch_up_on_a_gpu_is_background_work_too():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    assert p.submit(jobs.job("page", URGENT))              # a priority clip came in (the quick gate: an idle worker)
    assert p.submit(jobs.job("shed", CATCHUP, seconds=3.0, bg=True))
    assert p.claim("gpu-1", 0).id == "page"
    p._leases.clear()
    p.workers["gpu-1"].leases.clear()
    assert p.claim("gpu-1", 0) is None                      # quiet after the page
    clk.t += sttroute.BG_QUIET_S
    assert p.claim("gpu-1", 0).id == "shed"


def test_fire_urgent_is_never_starved_by_an_auto_channels_clip():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    assert p.submit(jobs.job("auto-live", LIVE, gpu_only=True, forced=True))
    assert p.submit(jobs.job("fire-urgent", URGENT))
    assert p.claim("gpu-1", 0).id == "fire-urgent"          # (the classes stay strict)


def test_a_pinned_job_goes_to_its_worker_only_and_never_to_one_set_aside():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-a")
    online(p, "gpu-b")
    assert p.submit(jobs.job("pinned", LIVE, forced=True, pin="gpu-b"))
    assert p.claim("gpu-a", 0) is None and p.claim("gpu-b", 0).id == "pinned"
    assert p.submit(jobs.job("guarded", LIVE, forced=True, avoid={"gpu-b": "latency guard"}))
    p.workers["gpu-b"].leases.clear()
    assert p.claim("gpu-b", 0) is None and p.claim("gpu-a", 0).id == "guarded"
    assert p.word("st", avoid={"gpu-b": "x"})["gpu_ready"] == 1 and p.word("st")["gpu_ready"] == 2


# -- the latency guard (#878) -----------------------------------------------------------------------------------------


def test_the_latency_guard_trips_on_slow_or_failing_and_recovers_on_probes_by_itself():
    clk, said = Clock(), []
    b = EngineBook(clock=clk, log=said.append)
    for _ in range(3):
        b.add("gpu-pc", 2.0, gpu=True)
    assert b.tripped("gpu-pc") == ""
    for _ in range(4):
        b.add("gpu-pc", 9.0, gpu=True)                      # median over 5 s now
    assert b.tripped("gpu-pc") == "latency guard: p50 9.0 s" and "its priority clips go to AWS" in said[-1]
    b.add("AWS", 3.0)
    assert {r["engine"]: r["guard"] for r in b.table()} == {"gpu-pc": "tripped: p50 9.0 s", "AWS": ""}
    b.add("gpu-pc", 9.0, gpu=True)                          # (one in flight when it tripped: no probe's answer)
    assert b.guard()["gpu-pc"]["why"] == "p50 9.0 s"
    clk.t += sttroute.GUARD_HOLD_S                          # held, then a probe is due
    assert b.due("gpu-pc") and b.probe("gpu-pc") and b.tripped("gpu-pc").startswith("latency guard")
    assert not b.due("gpu-pc")                              # (the next one GUARD_PROBE_S on)
    b.add("gpu-pc", 1.0, gpu=True)                          # (not the probe's answer: counts for nothing)
    assert b.guard()["gpu-pc"]["good"] == 0
    b.add("gpu-pc", 8.0, gpu=True, probe=True)              # a slow probe: held again
    assert b.guard()["gpu-pc"]["why"] == "probe 8.0 s"
    clk.t += sttroute.GUARD_HOLD_S
    for i in range(sttroute.GUARD_RECOVER_N):
        assert b.due("gpu-pc") and b.probe("gpu-pc")
        b.add("gpu-pc", 1.5, gpu=True, probe=True)
        clk.t += sttroute.GUARD_PROBE_S
    assert b.tripped("gpu-pc") == "" and not b.guard() and "probes in time" in said[-1]
    b.add("gpu-pc", 1.5, gpu=True)                          # the old slow ones no longer count
    assert b.tripped("gpu-pc") == ""
    # failures: leases lost, handed back
    clk.t += 10
    for ok in (False, True, False, True):
        b.add("gpu-2", None if not ok else 1.0, ok=ok, gpu=True)
    assert b.tripped("gpu-2") == "latency guard: 2 of 4 failed"
    # background samples never trip it
    for _ in range(6):
        b.add("gpu-3", 30.0, gpu=True, priority=False)
    assert b.tripped("gpu-3") == ""


# -- the station: what it does with a clip of each engine -------------------------------------------------------------


@pytest.fixture
def st_app(tmp_path):
    app = make_app(station_cfg(tmp_path, "http://127.0.0.1:9"))
    yield app
    app.link.stop()


def _word(app, monkeypatch, gpu_ready=False, gpu_bg=False, aws_ok=False):
    monkeypatch.setattr(app.link, "pool_gpu_ready", lambda: gpu_ready)
    monkeypatch.setattr(app.link, "pool_gpu_bg", lambda: gpu_bg)
    app._hub_aws = dict(app._hub_aws, ok=aws_ok)


def test_the_station_hands_gpu_first_channels_over_at_once_and_runs_its_whisper_when_nothing_else_can(st_app, monkeypatch):
    app = st_app
    assert app.forwarding()
    _word(app, monkeypatch, gpu_ready=True)
    assert app._stt_way("auto", "FG1") == "aws"                    # a GPU is ready: the hub's, at once
    assert app._stt_way("pin:gpu-pc", "FG1") == "aws" and app._stt_way("pin:aws", "FG1") == "aws"
    _word(app, monkeypatch, gpu_ready=False, aws_ok=True)
    assert app._stt_way("auto", "FG1") == "aws"                    # no GPU, AWS: the hub's still
    _word(app, monkeypatch)
    assert app._stt_way("auto", "FG1") == "local"                  # neither: this whisper, no round trip
    _word(app, monkeypatch, gpu_bg=True)
    assert app._stt_way("auto_bg", "p25:mrrs:9211") == "local_bg"  # background: this station's whisper first (#896)
    app.link.station_id = "north"
    assert app._stt_way("pin:station:north", "FG1") == "local"     # pinned to itself
    assert app._stt_way("pin:station:south", "FG1") == "aws"
    for old in ("local", "aws", "both", "off"):
        assert app._stt_way(old, "FG1") == old                     # the old engines: as before
    assert app.channels_applied()["engine_select_ok"] is True


def test_with_no_hub_the_selector_runs_aws_or_the_local_model(tmp_path):
    from tests.test_cloud import base
    app = make_app(base(clips_dir=str(tmp_path / "clips")))
    assert not app.forwarding()
    assert app._stt_way("auto", "FG1") == "local" and app._stt_way("auto_bg", "FG1") == "local"
    app.aws_ok = lambda key=None: (True, "")
    assert app._stt_way("auto", "FG1") == "aws" and app._stt_way("pin:gpu-pc", "FG1") == "aws"
    assert app._stt_way("auto_bg", "FG1") == "local"               # background never AWS first


def test_a_promoted_channel_on_background_goes_priority(st_app, monkeypatch):
    app = st_app
    app._chan_ov = {"tg:325": {"engine": "auto_bg"}}
    monkeypatch.setattr(app, "promoted_keys", lambda: frozenset({"tg:325"}))
    assert app.stt_engine("tg:325") == "auto"


# -- the hub: each order and each fallback, end to end on its API ----------------------------------------------------


def _row(hub, tid):
    return hub.app.cad.store.one("SELECT text, status, model, stt_route FROM radio_traffic WHERE id = ?", (tid,)) or {}


def _set(hub, key, engine):
    assert hub_post(hub, "channels", {"action": "set", "key": key, "engine": engine})[0] == 200
    assert hub.app.channel_pref(key)["engine"] == engine


def _hub(tmp_path, fake, **kw):
    return HubRun(hub_cfg(tmp_path, **aws_cfg(fake, **kw)))


def test_auto_priority_gpu_first_then_aws_then_the_station_each_said_on_its_route(tmp_path, monkeypatch):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    w = None
    asked = []
    try:
        _set(hub, "FG1", "auto")
        token = hub.app.hub.workers.add("gpu-1", "Test box")
        w = FakeWorker(hub.url, token, wait_s=20.0).start()
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        send(hub, pending_event("FG1-1"))
        assert wait(lambda: _row(hub, "FG1-1").get("text") == GPU_TEXT, 30), _row(hub, "FG1-1")
        assert hub.app.aws.health()["done"] == 0
        assert wait(lambda: _row(hub, "FG1-1").get("stt_route") == "auto·priority: gpu-1", 5), _row(hub, "FG1-1")
        assert hub.app.stt_runs.of("FG1-1")[0]["route"] == "auto·priority: gpu-1"
        # the worker gone: AWS, the route says why
        w.close()
        w = None
        hub.app.sttpool.forget("gpu-1", "test")
        send(hub, pending_event("FG1-2", seq=2))
        assert wait(lambda: _row(hub, "FG1-2").get("text") == AWS_TEXT + ".", 30), _row(hub, "FG1-2")
        assert wait(lambda: _row(hub, "FG1-2").get("stt_route") == "auto·priority: GPU → AWS (GPU: no GPU worker online)", 5)
        # AWS out of budget too: the station's whisper (never dropped), each step on the route
        monkeypatch.setattr(hub.app.hub, "remote", lambda fn, args, *a, **k: asked.append((fn, args)) or 1)
        assert hub_post(hub, "stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0})[0] == 200
        send(hub, pending_event("FG1-3", seq=3))
        assert wait(lambda: ("transcribe_now", ["FG1-3"]) in asked, 10), asked
        hub.app.apply("retext", {"id": "FG1-3", "text": "whisper heard this", "status": "done", "model": "small.en"}, None)
        r = _row(hub, "FG1-3")
        assert r["text"] == "whisper heard this"
        assert r["stt_route"].startswith("auto·priority: GPU → AWS → station") and "AWS: " in r["stt_route"], r
        eng = {x["engine"]: x for x in hub.app.pool_health()["engines"]}
        assert eng["gpu-1"]["n"] == 1 and eng["AWS"]["n"] == 1
    finally:
        if w is not None:
            w.close()
        hub.close()
        fake.close()


def test_pinned_aws_is_aws_and_the_default_is_pinned_aws(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, wait_s=20.0).start()
    try:
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        # FG1's default ([stt] aws_channels): pinned AWS (#896): AWS with a GPU worker idle, no class switch to say else
        assert hub.app.channel_pref("FG1")["engine"] == "pin:aws"
        send(hub, pending_event("FG1-5", seq=2))
        assert wait(lambda: _row(hub, "FG1-5").get("text") == AWS_TEXT + ".", 30), _row(hub, "FG1-5")
        assert not w.jobs
        assert wait(lambda: _row(hub, "FG1-5").get("stt_route") == "pinned AWS", 5), _row(hub, "FG1-5")
        assert hub.app.stt_chosen("FG1-5") == "AWS"
        # pinned AWS with AWS out: the GPU then (never dropped)
        assert hub_post(hub, "stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0})[0] == 200
        time.sleep(0.2)
        send(hub, pending_event("FG1-6", seq=3))
        assert wait(lambda: _row(hub, "FG1-6").get("text") == GPU_TEXT, 30), _row(hub, "FG1-6")
        assert wait(lambda: _row(hub, "FG1-6").get("stt_route", "").startswith("pinned AWS: AWS → gpu-1 (AWS: "), 5), \
            _row(hub, "FG1-6")
        assert hub.app.stt_chosen("FG1-6") == "GPU"
    finally:
        w.close()
        hub.close()
        fake.close()


def test_no_class_switch_decides_where_a_clip_goes_only_its_channels_engine(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, wait_s=20.0).start()
    try:
        reg = hub.app.hub.workers
        with pytest.raises(Exception, match="classes: gone"):
            reg.set_pool(classes={"urgent": False})
        reg.mdb.put("stt_pool", '{"on": true, "classes": {"urgent": false, "high": false, "live": false}}')  # (an old one)
        assert set(reg.pool_settings()) == {"on", "stations"}
        assert reg.enabled(sttpool.URGENT) and reg.enabled(sttpool.CATCHUP)     # (the same for every class)
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        _set(hub, "FG1", "auto")                                 # Auto: the GPU, whatever an old saved switch said
        send(hub, pending_event("FG1-a"))
        assert wait(lambda: _row(hub, "FG1-a").get("text") == GPU_TEXT, 30), _row(hub, "FG1-a")
        _set(hub, "FG1", "pin:aws")                              # pinned AWS: AWS, whatever a switch once said
        time.sleep(0.2)
        send(hub, pending_event("FG1-b", seq=2))
        assert wait(lambda: _row(hub, "FG1-b").get("text") == AWS_TEXT + ".", 30), _row(hub, "FG1-b")
        assert [j["id"] for j in w.jobs] == ["FG1-a"]
        # a Settings write of "aws" is pinned AWS: "aws" is never stored again
        assert hub_post(hub, "channels", {"action": "set", "key": "tg:325", "engine": "aws"})[0] == 200
        assert hub.app.channel_pref("tg:325")["engine"] == "pin:aws"
    finally:
        w.close()
        hub.close()
        fake.close()


def test_the_migration_moves_aws_to_pinned_aws_once_audited(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        ed = hub.app.cad.settings
        first = json.loads(ed.cad.store.meta_get(ed.ENGINE_MIGRATION))       # (the hub's start ran it: the defaults)
        assert first["migrated"] == [] and "FG1" in first["default"]
        before = len([r for r in ed.audit_log("radios") if "#896" in json.dumps(r)])
        ed.cad.store.meta_put(ed.ENGINE_MIGRATION, "")                  # (as on a hub from before it)
        from sizeup.cad.settings import REMOVE  # noqa: F401
        ed._commit([("channels", "tg:325", {"engine": "aws"}), ("channels", "FG2", {"engine": "aws", "transcribe": True}),
                    ("channels", "tg:400", {"engine": "local"}), ("channels", "tg:401", {"engine": "both"})], "test", [])
        got = hub.app.migrate_engines()
        assert got["migrated"] == ["FG2", "tg:325"] and got["default"] == ["FG1", "South 2", "p25:mrrs:9211", "tg:307"]
        ov = ed.channel_overrides()
        assert ov["tg:325"] == {"engine": "pin:aws"} and "FG2" not in ov       # (FG2: pinned AWS is its default now)
        assert ov["tg:400"] == {"engine": "local"} and ov["tg:401"] == {"engine": "both"}     # (already pinned, Compare)
        assert hub.app.channel_pref("FG2")["engine"] == "pin:aws" and hub.app.channel_pref("tg:325")["engine"] == "pin:aws"
        audit = [r for r in ed.audit_log("radios") if "#896" in json.dumps(r)]
        keys = sorted(str(r.get("key") or r.get("target") or "") for r in audit)
        assert len(audit) - before == 6 and any("tg:325" in k for k in keys) and any("FG1" in k for k in keys), audit
        assert hub.app.migrate_engines() is None                              # once
        assert len([r for r in ed.audit_log("radios") if "#896" in json.dumps(r)]) - before == 6
        assert json.loads(ed.cad.store.meta_get(ed.ENGINE_MIGRATION))["migrated"] == ["FG2", "tg:325"]
    finally:
        hub.close()
        fake.close()

def test_a_pinned_gpu_worker_falls_back_through_the_auto_order(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    token = hub.app.hub.workers.add("gpu-b")
    hub.app.hub.workers.add("gpu-pc")
    w = FakeWorker(hub.url, token, wait_s=20.0).start()
    try:
        _set(hub, "FG1", "pin:gpu-pc")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        send(hub, pending_event("FG1-7"))                     # gpu-pc never connected: the next GPU, gpu-b
        assert wait(lambda: _row(hub, "FG1-7").get("text") == GPU_TEXT, 30), _row(hub, "FG1-7")
        assert wait(lambda: _row(hub, "FG1-7").get("stt_route") ==
                    "pinned gpu-pc: gpu-pc → gpu-b (gpu-pc not connected)", 5), _row(hub, "FG1-7")
    finally:
        w.close()
        hub.close()
        fake.close()


def test_the_guard_sends_a_slow_gpus_priority_clips_to_aws_and_says_so(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, wait_s=20.0).start()
    try:
        _set(hub, "FG1", "auto")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        for _ in range(sttroute.GUARD_MIN_N):
            hub.app.stt_engines.add("gpu-1", 12.0, gpu=True)
        assert hub.app.pool_word("")["gpu_ready"] == 0            # (the station hears it: no GPU for priority now)
        send(hub, pending_event("FG1-8"))
        assert wait(lambda: _row(hub, "FG1-8").get("text") == AWS_TEXT + ".", 30), _row(hub, "FG1-8")
        assert not w.jobs
        assert wait(lambda: _row(hub, "FG1-8").get("stt_route") ==
                    "auto·priority: GPU → AWS (GPU: gpu-1 latency guard: p50 12.0 s)", 5), _row(hub, "FG1-8")
        opts = {o["value"]: o for o in hub.app.engine_options()["options"]}
        assert opts["pin:gpu-1"]["state"] == "guard" and opts["pin:gpu-1"]["ok"] is False
        assert [e["guard"] for e in hub.app.pool_health()["engines"] if e["engine"] == "gpu-1"] == ["tripped: p50 12.0 s"]
    finally:
        w.close()
        hub.close()
        fake.close()


def test_auto_backgrounds_catch_up_uses_aws_last_within_each_budget(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        _set(hub, "tg:325", "auto_bg")
        real = hub.app.channel_pref
        hub.app.channel_pref = lambda k: {"record": True, "transcribe": True, "engine": "auto_bg"} if k.startswith("p25:") \
            else real(k)                                         # (no MRRS system on this hub: its tier says nothing)
        assert hub.app._catchup_route("p25:mrrs:9211") == "aws"
        hub.app.aws.context_budget = lambda: {"pct": 95.0}
        assert hub.app._catchup_route("p25:mrrs:9211") == "whisper"
        assert hub.app._catchup_route("tg:325") == "aws"         # (ours: within our month's budget, #896)
        hub.app.aws.budget = lambda: {"pct": 95.0}
        assert hub.app._catchup_route("tg:325") == "whisper"
    finally:
        hub.close()
        fake.close()

def test_an_old_station_is_sent_the_nearest_old_engine(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        _set(hub, "FG1", "auto")
        _set(hub, "tg:325", "auto_bg")
        _set(hub, "FG2", "pin:gpu-pc")
        hub.app.hub.status = {"channels": {"prefs": {}}}                 # a station from before: no engine_select_ok
        prefs = hub.app.channel_desired()["prefs"]
        # (tg:325: Auto · background is its default now (#896), nothing to send; an old station's own default: local)
        assert prefs["FG1"]["engine"] == "aws" and "tg:325" not in prefs and prefs["FG2"]["engine"] == "aws"
        hub.app.hub.status = {"channels": {"prefs": {}, "engine_select_ok": True}}
        prefs = hub.app.channel_desired()["prefs"]
        assert prefs["FG1"]["engine"] == "auto" and "tg:325" not in prefs
        # the hub still routes an old station's "aws" clip by the channel's own choice (Auto · priority's order)
        assert hub.app._hub_engine("FG1") == "auto"
    finally:
        hub.close()
        fake.close()


def test_the_dropdown_shows_each_engine_by_id_with_its_live_state(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake, stt_aws_limit_usd=25.0)
    try:
        hub.app.hub.workers.add("gpu-pc")
        hub.app.sttpool.seen("gpu-pc", "gaming")
        hub.app.hub.workers.add("gpu-2")
        online(hub.app.sttpool, "gpu-2")
        e = hub.app.channels_state()["engines"]
        opts = {o["value"]: o for o in e["options"]}
        assert [o["value"] for o in e["options"]] == ["auto", "auto_bg", "local", "pin:gpu-2", "pin:gpu-pc", "pin:aws",
                                                      "both"]                     # (no legacy "aws": #896)
        assert (opts["pin:gpu-pc"]["label"], opts["pin:gpu-pc"]["state"], opts["pin:gpu-pc"]["ok"]) == ("gpu-pc", "gaming", False)
        assert (opts["pin:gpu-2"]["state"], opts["pin:gpu-2"]["ok"]) == ("online", True)
        assert opts["pin:aws"]["label"] == "AWS" and opts["pin:aws"]["state"] == "$25 left" and opts["pin:aws"]["ok"]
        assert opts["auto"]["state"] == "gpu-2" and opts["local"]["state"] == "offline"
        assert opts["both"]["label"] == "Compare"
        hub.app.hub.status = {"health": {"stt_queue": {"queued": 3}}}
        hub.app.hub.connected = lambda: True
        assert {o["value"]: o for o in hub.app.engine_options()["options"]}["local"]["state"] == "3 waiting"
    finally:
        hub.close()
        fake.close()


def test_the_radio_page_shows_the_route_in_the_transcripts_hover():
    js = open("sizeup/web/static/cad.js").read()
    assert "Route: ${t.stt_route}" in js
    rc = open("sizeup/web/static/radio-controls.js").read()
    assert '["auto", "Auto · priority"]' in rc and '["pin:aws", "AWS"]' in rc and '"Compare"' in rc
    assert 'op.className = o.ok === false ? "off" : ""' in rc        # greyed, still selectable (never disabled)


def test_a_tripped_gpu_is_probed_with_shadow_copies_never_live_clips(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, wait_s=20.0).start()
    try:
        _set(hub, "FG1", "auto")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        for _ in range(sttroute.GUARD_MIN_N):
            hub.app.stt_engines.add("gpu-1", 12.0, gpu=True)
        for i in range(3):                                    # steady Auto traffic while it is held: AWS each time
            send(hub, pending_event(f"FG1-h{i}", seq=i + 1))
            assert wait(lambda i=i: _row(hub, f"FG1-h{i}").get("text") == AWS_TEXT + ".", 30)
        assert not w.jobs
        hub.app.stt_engines._trip["gpu-1"]["at"] -= sttroute.GUARD_HOLD_S     # (the hold over: a probe is due)
        send(hub, pending_event("FG1-live", seq=9))
        # the live clip goes its own way (AWS); a shadow copy of it, pinned to the tripped worker, is the probe
        assert wait(lambda: _row(hub, "FG1-live").get("text") == AWS_TEXT + ".", 30), _row(hub, "FG1-live")
        assert wait(lambda: [j["id"] for j in w.jobs] == ["FG1-live~probe"], 10), w.jobs
        assert wait(lambda: hub.app.stt_engines.guard().get("gpu-1", {}).get("good") == 1, 5), hub.app.stt_engines.guard()
        assert _row(hub, "FG1-live")["text"] == AWS_TEXT + "."                  # (the probe's text never used)
        assert not hub.app.stt_engines.due("gpu-1")                             # (the next one a minute on)
    finally:
        w.close()
        hub.close()
        fake.close()


def test_a_channel_pinned_to_a_tripped_gpu_goes_to_aws_and_says_why(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, wait_s=20.0).start()
    try:
        _set(hub, "FG1", "pin:gpu-1")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        for _ in range(sttroute.GUARD_MIN_N):
            hub.app.stt_engines.add("gpu-1", 12.0, gpu=True)
        send(hub, pending_event("FG1-p1"))
        assert wait(lambda: _row(hub, "FG1-p1").get("text") == AWS_TEXT + ".", 30), _row(hub, "FG1-p1")
        assert not w.jobs
        assert wait(lambda: _row(hub, "FG1-p1").get("stt_route", "").startswith(
            "pinned gpu-1: gpu-1 → GPU → AWS (gpu-1 latency guard: p50 12.0 s"), 5), _row(hub, "FG1-p1")
    finally:
        w.close()
        hub.close()
        fake.close()


def test_the_guard_backs_off_counts_priority_failures_only_and_survives_a_restart(tmp_path):
    clk = Clock()
    b = EngineBook(clock=clk, log=lambda s: None)
    for _ in range(6):
        b.add("gpu-1", None, ok=False, gpu=True, priority=False)    # (RERUN, LOW failures: never the guard's)
    assert b.tripped("gpu-1") == ""
    for _ in range(4):
        b.add("gpu-1", 9.0, gpu=True)
    assert b._trip["gpu-1"]["hold"] == sttroute.GUARD_HOLD_S
    clk.t += sttroute.GUARD_HOLD_S
    for _ in range(sttroute.GUARD_RECOVER_N):
        assert b.due("gpu-1") and b.probe("gpu-1")
        b.add("gpu-1", 1.0, gpu=True, probe=True, priority=False)
        clk.t += sttroute.GUARD_PROBE_S
    assert b.tripped("gpu-1") == ""
    clk.t += 60
    for _ in range(4):
        b.add("gpu-1", 9.0, gpu=True)                               # tripped again within the hour: twice as long
    assert b._trip["gpu-1"]["hold"] == 2 * sttroute.GUARD_HOLD_S
    clk.t += sttroute.GUARD_HOLD_S
    assert not b.due("gpu-1")
    # across a restart: the hub keeps it (hub.sqlite meta) and the next start reads it
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        for _ in range(4):
            hub.app.stt_engines.add("gpu-9", 9.0, gpu=True)
        assert hub.app.stt_engines.tripped("gpu-9")
    finally:
        hub.close()
    hub = _hub(tmp_path, fake)
    try:
        assert hub.app.stt_engines.tripped("gpu-9") == "latency guard: p50 9.0 s"
    finally:
        hub.close()
        fake.close()


def test_a_live_clip_never_queues_behind_background_work_on_a_gpu():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    assert p.submit(jobs.job("bg", LOW, seconds=25.0, forced=True, bg=True))
    assert p.claim("gpu-1", 0).id == "bg"                   # a 25 s context clip decoding (one clip at a time)
    assert p.ready_forced(LIVE) == (False, "gpu-1 busy (background)")      # a Fire/EMS main clip: AWS, never behind it
    assert not p.submit(jobs.job("live", LIVE, forced=True))

def test_resend_after_aws_returns_skips_what_is_in_flight_or_done_on_every_engine(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        sent = []
        hub.app.aws.submit = lambda key, job, pcm: sent.append(job.id)
        _set(hub, "FG2", "pin:gpu-pc")
        from sizeup.incidents import Traffic
        now = time.time()
        for tid, src, status, text in (("FG1-r1", "FG1", "done", "words"), ("FG2-r2", "FG2", "done", "words"),
                                       ("FG2-r3", "FG2", "pending", ""), ("FG1-r4", "FG1", "pending", "")):
            t = Traffic(tid, src, now - 5, now - 2, text, status=status)
            hub.app.store.loose.append(t)
        hub.app.store.loose.append(Traffic("FG2-r5", "FG2", now - 5, now - 2, "", status="error"))
        hub.app._aws_since["FG2-r3"] = now                       # AWS has it now
        hub.app._aws_since["FG1-r4"] = now
        import base64
        wav = base64.b64decode(pending_event("x")["audio"])
        hub.app.audio.get = lambda cid: wav
        assert hub.app._aws_resend() == 1 and sent == ["FG2-r5"]  # (only the failed one nobody has: never twice)
    finally:
        hub.close()
        fake.close()


def _bg_handed(tid, seq=1, cls="low"):
    ev = pending_event(tid, seq=seq)
    ev["data"].update(source="Neighbor FD", talkgroup=325, transcript_pending="pool", stt_class=cls, stt="")
    return ev


def _station_worker(p, sid, queue, service_s=18.0):
    w = p.seen(f"station:{sid}", "online", kind="station", load={"queue": queue, "service_s": service_s, "max_s": 60.0})
    w.polling = 1
    return w


def test_background_goes_stations_first_then_gpu_overflow_then_aws_then_back(tmp_path, monkeypatch):
    """#896, the admin: "stations first, if overflow then the GPU, AWS last". A clip its station couldn't start within
    its budget: another station that can (the soonest), else an idle GPU at once, else AWS within its budget, else back
    to its own station's queue. Never dropped; each step on its route and counted for Health."""
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    asked = []
    try:
        _set(hub, "tg:325", "auto_bg")
        p = hub.app.sttpool
        monkeypatch.setattr(hub.app.hub, "remote", lambda fn, args, *a, **k: asked.append((fn, args)) or 1)
        monkeypatch.setattr(hub.app.hub.workers, "stations_on", lambda sid=None: True)
        monkeypatch.setattr(hub.app.hub.workers, "enabled", lambda cls=0.0: True)
        p.allowed = lambda wid: True
        _station_worker(p, "east", queue=20)                    # 20 x 18 s: 360 s, over the 90 s budget
        _station_worker(p, "west", queue=2)                     # 36 s: can start it in time
        _station_worker(p, "north", queue=3)                    # 54 s
        send(hub, _bg_handed("TG325-1"))
        assert wait(lambda: hub.app.stt_chosen("TG325-1") == "stations", 10), hub.app.stt_chosen("TG325-1")
        assert p.bg_place(sttpool.LOW, 3.0, "", 90.0)[:2] == ("station", "station:west")    # (the soonest)
        assert p.bg_place(sttpool.LOW, 3.0, "west", 90.0)[:2] == ("station", "station:north")  # (never its own)
        # no station can start it in time: an idle GPU at once (overflow)
        for sid in ("west", "north"):
            p.workers[f"station:{sid}"].load["queue"] = 30
        hub.app.hub.workers.add("gpu-1")
        online(p, "gpu-1")
        p._prio_at = 0.0
        send(hub, _bg_handed("TG325-2", seq=2))
        assert wait(lambda: hub.app.stt_chosen("TG325-2") == "gpu-1", 10), hub.app.stt_chosen("TG325-2")
        # no GPU free either: AWS within its budget
        p.workers["gpu-1"].leases.add("busy")
        send(hub, _bg_handed("TG325-3", seq=3))
        assert wait(lambda: _row(hub, "TG325-3").get("text") == AWS_TEXT + ".", 30), _row(hub, "TG325-3")
        assert wait(lambda: "AWS" in _row(hub, "TG325-3").get("stt_route", ""), 5)
        # AWS over its background budget too: back to its own station's queue (never dropped)
        hub.app.aws.budget = lambda: {"pct": 95.0}
        send(hub, _bg_handed("TG325-4", seq=4))
        assert wait(lambda: ("transcribe_now", ["TG325-4"]) in asked, 10), asked
        hub.app.apply("retext", {"id": "TG325-4", "text": "neighbor traffic", "status": "done", "model": "small.en"}, None)
        assert wait(lambda: hub.app.stt_runs.of("TG325-4"), 5)
        assert hub.app.stt_runs.of("TG325-4")[0]["role"] == "text"
        h = hub.app.bg_health()
        assert h["h"] == {"station": 1, "gpu": 1, "aws": 1, "back": 1}, h
        assert h["stations"]["east"] == 360.0 and h["budget_s"] == sttroute.BG_BUDGET_S
    finally:
        hub.close()
        fake.close()


def test_a_station_keeps_its_background_clip_until_it_is_behind_its_budget(st_app, monkeypatch):
    app = st_app
    sent = []
    monkeypatch.setattr(app, "_stt_offload", lambda item, key, prio, why, bg=False: sent.append((item.id, bg)) or True)
    stats = {"queued": 2, "service_s": 18.0}
    monkeypatch.setattr(app.stt, "stats", lambda: dict(stats))
    assert app._bg_over_budget(sttpool.LOW) == 0.0                         # 36 s: it starts here in time
    stats["queued"] = 10
    assert app._bg_over_budget(sttpool.LOW) == 180.0                       # 180 s: over the 90 s budget
    assert app._bg_over_budget(sttpool.CONTEXT) == 180.0 and sttroute.bg_budget(sttpool.CONTEXT) == 60.0


def test_background_on_a_gpu_waits_for_the_stations_first_and_never_blocks_priority():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    st = p.seen("station:west", "online", kind="station", load={"queue": 0, "service_s": 5.0, "max_s": 60.0})
    st.polling = 1
    assert p.submit(jobs.job("shed", CATCHUP, seconds=3.0, bg=True))
    assert p.claim("gpu-1", 0) is None                       # a station could take it: stations first
    assert p.claim("station:west", 0).id == "shed"
    assert p.submit(jobs.job("shed2", CATCHUP, seconds=3.0, bg=True))
    clk.t += sttpool.BG_STATION_FIRST_S                      # nobody took it in time: the GPU as overflow
    assert p.claim("gpu-1", 0).id == "shed2"
    assert p.ready_forced(LIVE) == (False, "gpu-1 busy (background)")      # priority never queues behind it

def test_the_legacy_pool_check_never_counts_background_work_as_a_waiting_pre_alert():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    online(p, "gpu-1")
    online(p, "gpu-2")
    p._queue.append(jobs.job("bg-ctx", sttpool.CONTEXT, forced=True, bg=True))
    assert p.ready(URGENT, gpu_only=True) == (True, "")


def test_two_clips_of_one_channel_on_different_engines_stay_in_air_time_order_and_both_fill_in(tmp_path, monkeypatch):
    """FIFO on the timeline: the older clip goes to the station's whisper (no GPU, AWS out), the newer one to a GPU
    worker that answers first. Each row is in the log at once, at its air time, with its audio and "Transcribing";
    the texts fill in where they are, whichever engine answers first."""
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    asked = []
    w = None
    try:
        _set(hub, "FG1", "auto")
        monkeypatch.setattr(hub.app.hub, "remote", lambda fn, args, *a, **k: asked.append((fn, args)) or 1)
        assert hub_post(hub, "stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0})[0] == 200
        old = pending_event("FG1-old")
        send(hub, old)
        assert wait(lambda: ("transcribe_now", ["FG1-old"]) in asked, 10), asked
        r = hub.app.cad.store.one("SELECT status, text, start FROM radio_traffic WHERE id = 'FG1-old'")
        assert r["status"] == "pending" and r["text"] == "" and hub.app.audio.has("FG1-old")     # (at once, audio too)
        token = hub.app.hub.workers.add("gpu-1")
        w = FakeWorker(hub.url, token, wait_s=20.0).start()
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        time.sleep(0.05)
        send(hub, pending_event("FG1-new", seq=2))
        assert wait(lambda: _row(hub, "FG1-new").get("text") == GPU_TEXT, 30), _row(hub, "FG1-new")
        assert _row(hub, "FG1-old")["text"] == ""                                  # (the newer one answered first)
        hub.app.apply("retext", {"id": "FG1-old", "text": "the older words", "status": "done", "model": "small.en"}, None)
        rows = [x for x in hub.app.cad.comms_query({})["rows"] if x["id"] in ("FG1-old", "FG1-new")]
        assert [x["id"] for x in rows] == ["FG1-new", "FG1-old"]                    # (newest first, by air time)
        assert rows[0]["start"] > rows[1]["start"] and rows[1]["text"] == "the older words" and rows[0]["text"] == GPU_TEXT
    finally:
        if w is not None:
            w.close()
        hub.close()
        fake.close()


def test_a_handed_over_clip_follows_its_channels_engine_and_chosen_follows_each_step(tmp_path):
    """A clip a station hands to the pool (its whisper behind) on a pinned AWS channel goes AWS's way, never the
    generic pool's (#896); stt_chosen says the engine of record at each step: the hand-over, a resend, Compare."""
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        ev = pending_event("FG1-h1")
        ev["data"].update(transcript_pending="pool", stt_class="live", stt="")
        send(hub, ev)                                            # FG1: pinned AWS (its default)
        assert wait(lambda: _row(hub, "FG1-h1").get("text") == AWS_TEXT + ".", 30), _row(hub, "FG1-h1")
        assert hub.app.stt_chosen("FG1-h1") == "AWS" and hub.app.sttpool.submitted == 0
        _set(hub, "tg:325", "local")                              # a Local channel's hand-over: the pool's way
        ev = pending_event("TG325-h2", seq=2)
        ev["data"].update(source="Neighbor FD", talkgroup=325, transcript_pending="pool", stt_class="live", stt="")
        hub.app.hub.workers.add("gpu-1")
        online(hub.app.sttpool, "gpu-1")
        send(hub, ev)
        assert wait(lambda: hub.app.stt_chosen("TG325-h2") == "pool", 10)
        # a resend after AWS came back: AWS is the engine of record
        sent = []
        hub.app.aws.submit = lambda key, job, pcm: sent.append(job.id)
        hub.app.stt_routes.choose("FG1-h1", "station")
        from sizeup.incidents import Traffic
        now = time.time()
        hub.app.store.loose.append(Traffic("FG1-r9", "FG1", now - 5, now - 2, "", status="error"))
        import base64
        wav = base64.b64decode(pending_event("x")["audio"])
        hub.app.audio.get = lambda cid: wav
        assert hub.app._aws_resend() >= 1 and "FG1-r9" in sent and hub.app.stt_chosen("FG1-r9") == "AWS"
    finally:
        hub.close()
        fake.close()


def test_an_import_from_before_never_stores_aws_and_an_old_station_still_gets_it(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        ed = hub.app.cad.settings
        data = ed.export()
        data.setdefault("overrides", {})["channels"] = {"tg:325": {"engine": "aws"}}      # (an export from before)
        got = ed._check_import(data)[0]
        assert got["channels"]["tg:325"]["engine"] == "pin:aws"
        _set(hub, "tg:325", "pin:aws")
        hub.app.hub.status = {"channels": {"prefs": {}}}                 # a station from before the selector
        assert hub.app.channel_desired()["prefs"]["tg:325"]["engine"] == "aws"
        hub.app.hub.status = {"channels": {"prefs": {}, "engine_select_ok": True}}
        assert hub.app.channel_desired()["prefs"]["tg:325"]["engine"] == "pin:aws"
        assert channels.for_station({"tg:9": {"engine": "aws"}}) == {"tg:9": {"engine": "pin:aws"}}
    finally:
        hub.close()
        fake.close()


def test_switched_off_pool_classes_are_said_and_audited_once_when_they_go(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        reg = hub.app.hub.workers
        reg.mdb.put("stt_pool", '{"on": true, "classes": {"catchup": false, "low": false, "urgent": true}}')
        hub.app.migrate_engines()
        audit = [r for r in hub.app.cad.settings.audit_log("system") if "gone (#896)" in json.dumps(r)]
        assert len(audit) == 2
        assert "classes" not in json.loads(reg.mdb.get("stt_pool", ""))
        hub.app.migrate_engines()
        assert len([r for r in hub.app.cad.settings.audit_log("system") if "gone (#896)" in json.dumps(r)]) == 2
    finally:
        hub.close()
        fake.close()


def test_context_talkgroups_on_the_stations_whisper_move_to_auto_background_audited(tmp_path):
    """#896, the admin: every context or neighbour talkgroup on its station's whisper goes to Auto · background (the
    stations first, a GPU as overflow, AWS last): its default now, a stored "local" moved, each one audited, once."""
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        ed = hub.app.cad.settings
        ed.cad.store.meta_put(ed.ENGINE_MIGRATION, "")
        ed._commit([("channels", "tg:500", {"engine": "local", "transcribe": True}),
                    ("channels", "tg:502", {"engine": "pin:aws"})], "test", [])
        got = ed.migrate_engines(context_keys=["tg:500", "tg:501", "tg:502", "p25:metro:12", "FG1", "tg:307"])
        assert got["background"] == ["tg:500"] and got["background_default"] == ["p25:metro:12", "tg:501"]
        ov = ed.channel_overrides()
        assert "tg:500" not in ov or "engine" not in ov["tg:500"]               # (Auto · background: its default now)
        assert ov["tg:502"] == {"engine": "pin:aws"}                            # (an admin's other choice: kept)
        assert hub.app.channel_pref("p25:metro:12")["engine"] == "auto_bg"
        rows = [r for r in ed.audit_log("radios") if "auto_bg" in json.dumps(r)]
        assert len(rows) == 3
        assert ed.migrate_engines(context_keys=["tg:999"]) is None              # once
        # a county talkgroup marked Main is ours: its own station, not background
        assert hub.app.context_tg("p25:metro:12") is True and channels.defaults("tg:1", context=lambda k: False)["engine"] \
            == "local" and channels.defaults("tg:1", context=lambda k: True)["engine"] == "auto_bg"
        assert channels.defaults("FG1", context=lambda k: True)["engine"] == "pin:aws"    # (never an FM channel)
    finally:
        hub.close()
        fake.close()
