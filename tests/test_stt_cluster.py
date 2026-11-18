"""Whisper capacity across the cluster (#639 phase 2): every station's whisper is a worker in the hub's pool, so a clip
from one station is transcribed on another while it has room; a GPU worker first, then an idle station's whisper, then
AWS; a station's whisper never takes a pre-alert clip nor an AWS channel's; a station hands over the context clips it
would shed and everything still waiting when it stops. Nothing is dropped while any worker is idle. Invented stations,
talkgroups, texts and synthetic audio only; local fakes, never AWS."""

import base64
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import sttpool
from sizeup.cloud.stationworker import StationWorker
from sizeup.stt import CATCHUP, CONTEXT, LIVE, LOW, SHED, URGENT
from sizeup.sttpool import BUDGETS, Job, Pool
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import voice, wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws  # noqa: F401 (the fixture: test credentials only)
from tests.test_cloud import HubRun, all_traffic, call, fm_segment, hub_cfg, station, station_cfg
from tests.test_stt_pool import FakeWorker, pending_event, row, send

CTX = "p25:mrrs:9570"           # an invented context talkgroup of another P25 system (text tier: LOW, shed when behind)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def job(cid, cls=LOW, gpu_only=False, done=None, fell=None):
    done = {} if done is None else done
    fell = {} if fell is None else fell
    return Job(cid, cls, CTX, "traffic", 3.0, audio=lambda: b"RIFF", gpu_only=gpu_only,
               on_done=lambda j, r: done.__setitem__(j.id, r), on_fallback=lambda j, w: fell.__setitem__(j.id, w))


def idle(p, wid, kind):
    w = p.seen(wid, "online", kind=kind)
    w.polling = 1
    return w


# -- the pool's order ---------------------------------------------------------------------------------------------------


def test_a_station_whisper_never_takes_a_pre_alert_nor_an_aws_channels_clip():
    clock = Clock()
    p = Pool(enabled=lambda c: True, clock=clock, log=lambda s: None)
    idle(p, "station:st-b", "station")
    assert p.ready(URGENT) == (False, "no worker ready") and p.ready(CONTEXT)[0] is False
    assert p.ready(LIVE, gpu_only=True)[0] is False           # an AWS channel's: AWS is quicker than a station's whisper
    assert p.submit(job("ctx", LOW))
    assert p.claim("station:st-b", 0).id == "ctx"


def test_a_gpu_worker_first_then_an_idle_station_after_a_moment():
    clock = Clock()
    p = Pool(enabled=lambda c: True, clock=clock, log=lambda s: None)
    idle(p, "gpu-1", "gpu")
    idle(p, "station:st-b", "station")
    assert p.submit(job("a", LOW))
    assert p.claim("station:st-b", 0) is None                  # a GPU worker idle: it first
    assert p.claim("gpu-1", 0).id == "a"
    assert p.submit(job("b", LOW))
    clock.t += sttpool.STATION_DEFER_S                         # the GPU worker busy, the moment over: the station takes it
    assert p.claim("station:st-b", 0).id == "b"
    st = p.stats()
    assert {w["id"]: w["kind"] for w in st["workers"]} == {"gpu-1": "gpu", "station:st-b": "station"}


# -- three stations of different load, over HTTP --------------------------------------------------------------------------


class FakeStation(FakeWorker):
    """A station's whisper as the hub sees it: its station token, its load; busy = it never claims (its own clips)."""

    def __init__(self, url, token, busy=False, slow_s=0.0, text="Car 12 clear of the scene"):
        super().__init__(url, token, text=text)
        self.busy, self.slow_s = busy, slow_s

    def start(self):
        self.t.start()
        return self

    def run(self):
        while not self.stop.is_set():
            if self.busy:
                self.post("/api/station/stt/state", {"state": "paused", "model": "small.en-q5_1",
                                                      "load": {"own": 7, "cpu_pct": 96.0, "why": "its own clips first"}})
                self.stop.wait(0.3)
                continue
            code, out = self.post("/api/station/stt/claim", {"wait": 1.0, "state": "online", "model": "small.en-q5_1",
                                                             "load": {"own": 0, "cpu_pct": 20.0, "service_s": 1.0}})
            job = (out or {}).get("job") if code == 200 else None
            if not job:
                continue
            self.jobs.append(job)
            call(self.url + job["audio"], token=self.token)
            time.sleep(self.slow_s)
            self.answers.append(self.post(f"/api/station/stt/jobs/{job['lease']}/complete", {
                "text": self.text, "model": "ggml-small.en-q5_1.bin",
                "segments": [{"start": 0.0, "end": 1.0, "text": self.text,
                              "words": [{"word": w, "probability": 0.7} for w in self.text.split()]}]}))


def ctx_event(tid, seq, now):
    """A context talkgroup's transmission a busy station handed to the pool (transcript_pending "pool")."""
    from sizeup.cad.dispatcher import wav_bytes
    return {"seq": seq, "at": now, "type": "traffic",
            "data": {"id": tid, "source": "MRRS Ops", "talkgroup": 9570, "system": "mrrs", "tier": "text",
                     "start": now - 3, "end": now, "text": "", "status": "remote", "model": "pool",
                     "transcript_pending": "pool", "stt_class": "low"},
            "audio": base64.b64encode(wav_bytes((voice(3.0)[::2] * 32767).astype(np.int16), 8000)).decode()}


def ctx_tier(app):
    app.tg.info[("mrrs", 9570)] = {"name": "Ops", "tier": "text"}


def test_three_stations_of_different_load_lose_nothing_while_any_whisper_is_idle(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="st-a"))
    reg = hub.app.hub.stations
    tokens = {s: reg.add(s) for s in ("st-a", "st-b", "st-c")}
    busy = FakeStation(hub.url, tokens["st-a"], busy=True).start()          # its own capture load: never claims
    quick = FakeStation(hub.url, tokens["st-b"], text="Engine 99 back in quarters").start()
    slow = FakeStation(hub.url, tokens["st-c"], slow_s=0.3, text="Ladder 98 in service").start()
    try:
        app = hub.app
        ctx_tier(app)
        assert wait(lambda: len(app.sttpool.ready_workers()) == 2, 10)
        now = time.time()
        ids = [f"9570-1794-call_{i}" for i in range(24)]
        for i, tid in enumerate(ids):
            send(hub, ctx_event(tid, i + 1, now))
        def texts():
            return {tid: row(hub, tid).get("text") for tid in ids}
        assert wait(lambda: all(texts().values()), 60), {k: row(hub, k) for k, v in texts().items() if not v}
        assert not [tid for tid in ids if row(hub, tid).get("status") != "done"]          # none dropped, none skipped
        assert busy.jobs == [] and quick.jobs and slow.jobs                                # the busy station took none
        models = {row(hub, tid)["model"] for tid in ids}
        assert models <= {"small.en-q5_1 (station st-b)", "small.en-q5_1 (station st-c)"}
        assert app.stt_runs.of(ids[0])[0]["engine"] == "station-whisper"
        health = app.health.model()["stt_pool"]["workers"]
        seen = {w["id"]: w for w in health}
        assert seen["station:st-a"]["state"] == "paused" and seen["station:st-a"]["load"]["own"] == 7
        assert seen["station:st-b"]["kind"] == "station" and seen["station:st-b"]["jobs_h"] > 0
    finally:
        for w in (busy, quick, slow):
            w.close()
        hub.close()


def test_with_no_worker_idle_a_handed_over_clip_waits_for_the_catch_up_never_dropped(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="st-a"))
    try:
        app = hub.app
        ctx_tier(app)
        now = time.time()
        send(hub, ctx_event("9570-1794-call_90", 1, now))
        assert wait(lambda: row(hub, "9570-1794-call_90").get("status") == "skipped", 10), row(hub, "9570-1794-call_90")
        assert SHED in row(hub, "9570-1794-call_90")["model"]                        # the catch-up's to run
        assert [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]] == ["9570-1794-call_90"]
        # a station's whisper free now: the catch-up sends it to the pool and it is done
        token = app.hub.stations.add("st-b")
        w = FakeStation(hub.url, token).start()
        try:
            assert wait(lambda: app.sttpool.ready(CATCHUP)[0], 10)
            app.catchup.pause = lambda: ""
            assert app.catchup.tick() == 1
            assert wait(lambda: row(hub, "9570-1794-call_90").get("status") == "done", 20)
        finally:
            w.close()
    finally:
        hub.close()


def test_an_aws_channels_clip_goes_to_aws_rather_than_wait_on_a_station_whisper(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="st-a", **aws_cfg(fake)))
    token = hub.app.hub.stations.add("st-b")
    w = FakeStation(hub.url, token).start()
    try:
        assert wait(lambda: hub.app.sttpool.ready_workers() == ["station:st-b"], 10)
        send(hub, pending_event("FG1-500"))                                  # Fire/EMS dispatch-class, an AWS channel
        assert wait(lambda: row(hub, "FG1-500").get("text") == AWS_TEXT + ".", 20)
        assert w.jobs == [] and hub.app.sttpool.submitted == 0
    finally:
        w.close()
        hub.close()
        fake.close()


def test_the_stations_whisper_can_be_left_out_of_the_pool(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="st-a"))
    try:
        token = hub.app.hub.stations.add("st-b")
        hub.app.hub.workers.act({"action": "pool", "stations": False})
        code, out = call(hub.url + "/api/station/stt/claim", "POST", {"wait": 0}, token=token)
        assert code == 403 and out["reason"] == "stations_off"
        assert hub.app.pool_word("st-b") == {"on": False, "worker": False, "ready": 0}
    finally:
        hub.close()


# -- a station: hands over what it would shed, and everything waiting when it stops ---------------------------------------


class Hold:
    """A whisper that holds every clip until let go (the station's whisper behind)."""

    def __init__(self, text="whisper heard this"):
        self.go, self.text, self.calls = threading.Event(), text, 0

    def transcribe(self, audio, prompt="", **kw):
        self.calls += 1
        self.go.wait(30)
        return self.text, 0.9


def test_a_restarting_station_hands_everything_waiting_to_the_pool_and_nothing_is_lost(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    token = hub.app.hub.workers.add("gpu-1")
    gpu = FakeWorker(hub.url, token, text="the pool heard this").start()
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        hold = Hold()
        st.stt.client = hold
        st._hub_aws = {"ok": False}                         # (FG1 on the station's whisper: AWS can't)
        assert wait(lambda: st.link.pool_has_room(), 15)    # (the hub's word: a worker elsewhere could take one)
        ids = []
        for i in range(3):
            ids.append(fm_segment(st))
            time.sleep(0.01)
        assert wait(lambda: st.stt.busy() == 3 and hold.calls == 1, 10)   # one on whisper, two waiting
        n = st._stt_stop_queue(finish_s=0.2)                # the update stops it: the one on whisper doesn't finish
        assert n["pool"] == 3 and n["saved"] == 0
        st.stt._stopping = False                            # (the station back: its whisper may be asked again)
        hold.go.set()
        assert wait(lambda: all(any(t["id"] == tid and t["text"] for t in all_traffic(hub.get("/api/state")))
                                for tid in ids), 40)
        assert gpu.jobs                                     # the pool took them (what it couldn't: the next way)
    finally:
        gpu.close()
        if st is not None:
            st.link.stop()
        hub.close()


def test_a_station_restarting_with_a_full_queue_and_no_pool_saves_it_and_queues_it_again_at_its_class(tmp_path,
                                                                                                         monkeypatch):
    from sizeup.app import App
    from tests.test_cloud import base
    cfg = base(clips_dir=str(tmp_path / "clips"))
    app = App(cfg)
    hold = Hold()
    app.stt.client = hold
    app.stt.start()
    fire = fm_segment(app)                                      # FG1: Fire/EMS class (urgent)
    time.sleep(0.01)
    with monkeypatch.context() as m:
        m.setattr(app, "_stt_priority", lambda item, key: LOW)
        ctx = [fm_segment(app) for _ in range(3)]               # context clips behind it
    fire2 = fm_segment(app)
    assert wait(lambda: app.stt.busy() == 5 and hold.calls == 1, 10)
    n = app._stt_stop_queue(finish_s=0.1)                       # no hub: saved, every one
    assert n == {"pool": 0, "saved": 5, "reruns": 0}
    assert (tmp_path / "stt-queue.json").is_file()
    hold.go.set()
    app2 = App(cfg)                                             # the next start
    heard = []
    app2._alarm = lambda t, text=None: heard.append(t.id)       # (what acts on live words: never a quiet catch-up)
    assert app2._stt_resume() == 5 and not (tmp_path / "stt-queue.json").exists()
    order = [(e[0], e[2].id) for e in sorted(app2.stt._q._items, key=lambda e: app2.stt._q._key(e, time.time()))]
    assert [cid for _p, cid in order[:2]] == [fire, fire2] and {p for p, _c in order[:2]} == {URGENT}
    assert {p for p, cid in order[2:]} == {LOW} and {cid for _p, cid in order[2:]} == set(ctx)
    app2.stt.client = SimpleNamespace(transcribe=lambda audio, prompt="", **k: ("Engine 99 responding", 0.9))
    app2.stt.start()
    done = lambda: {t.id for t in app2.store.loose if t.text}     # noqa: E731
    assert wait(lambda: done() >= {fire, fire2, *ctx}, 20)
    assert set(heard) >= {fire, fire2}                           # alerts as ever


def test_the_catch_up_takes_the_rows_a_stopped_station_left_first(tmp_path):
    from sizeup.app import App
    from tests.test_cloud import base
    from tests.test_stt_catchup import record
    app = App(base(clips_dir=str(tmp_path / "clips")))
    now = time.time()
    record(app, "alert", now - 100, tg=9211, system="mrrs", tier="alert")
    record(app, "newest", now - 10, tg=383, system="westchester")
    record(app, "stopped", now - 500, tg=383, system="westchester",
           model=f"m.bin ({SHED}: the station stopped (an update or a restart) with 4 clip(s) waiting)")
    assert [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]] == ["stopped", "alert", "newest"]


def test_a_clip_the_station_would_shed_goes_to_the_pool_instead(tmp_path):
    sent = []
    from sizeup.incidents import Traffic
    from sizeup.app import App
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    app.link = SimpleNamespace(pool_takes=lambda: True, pool_has_room=lambda: True)
    app.forwarding = lambda: True
    app._emit = lambda kind, d, clip=None: sent.append((kind, d))
    t = Traffic("MRRSOps-1", "MRRS Ops", 1.0, 4.0, "", status="pending")
    app._stt_dropped(t, f"{SHED}: speech-to-text 15 min behind, a context talkgroup")
    (kind, d), = [x for x in sent if x[0] == "traffic"]
    assert d["transcript_pending"] == "pool" and d["status"] == "remote" and t.status == "remote"
    # behind by more than OFFLOAD_WAIT_S with room elsewhere: a context clip goes at once; a fire one never
    app.stt.stats = lambda: {"queued": 10, "service_s": 16.0}
    assert app._offload_now(LOW) and not app._offload_now(URGENT) and not app._offload_now(LIVE)
    app.stt.stats = lambda: {"queued": 1, "service_s": 16.0}
    assert not app._offload_now(LOW)
    app.link = SimpleNamespace(pool_takes=lambda: False, pool_has_room=lambda: False)
    t2 = Traffic("MRRSOps-2", "MRRS Ops", 1.0, 4.0, "", status="pending")
    assert app._stt_offload(t2, CTX, LOW, "x") is False                  # an older hub: nothing handed over


# -- the station's worker ---------------------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, job=None, audio=None, fail_whisper=False):
        self.posts, self.job, self.audio = [], job, audio

    def __call__(self, timeout):
        return self

    def request(self, method, path, body=None, timeout=None):
        self.posts.append((path, body))
        if path.endswith("/claim"):
            j, self.job = self.job, None
            return {"job": j}
        return {"ok": True}

    def get_bytes(self, path, timeout=None):
        return self.audio


def test_the_stations_worker_claims_only_when_its_own_clips_are_done_and_its_cpu_allows():
    from sizeup.stt import wav_bytes
    own = {"n": 2}
    stt = SimpleNamespace(busy=lambda catchup=True: own["n"], stats=lambda: {"queued": own["n"], "service_s": 9.0},
                          client=SimpleNamespace(transcribe_wav=lambda wav, prompt="", fast=False: {
                              "text": " Car 12 clear", "segments": [{"start": 0, "end": 1, "text": "Car 12 clear",
                                                                    "words": [{"word": "Car", "probability": 0.9}]}]}))
    app = SimpleNamespace(stt=stt, whisper=SimpleNamespace(state="ok"), cfg=SimpleNamespace(stt_model="m/ggml-small.bin"))
    link = SimpleNamespace(pool={"worker": True})
    cpu = {"v": 20.0}
    client = FakeClient(job={"lease": "0123456789abcdef", "audio": "/x", "lease_s": 10.0, "prompt": "", "fast": True},
                        audio=wav_bytes(np.zeros(16000, np.float32), 16000))
    w = StationWorker(app, link, client, log=lambda m: None, cpu=lambda: cpu["v"])
    assert w.why_not().startswith("its own clips first")
    own["n"] = 0
    cpu["v"] = 97.0
    assert w.why_not().startswith("the CPU busy")
    cpu["v"] = 20.0
    assert w.why_not() == ""
    link.pool = {"worker": False}
    assert "doesn't take stations" in w.why_not()
    link.pool = {"worker": True}
    assert w.run_job(client.job) == "done"
    done = [b for p, b in client.posts if p.endswith("/complete")][0]
    assert done["text"] == " Car 12 clear" and done["segments"][0]["words"][0]["word"] == "Car"
    stt.client.transcribe_wav = lambda *a, **k: (_ for _ in ()).throw(OSError("whisper down"))
    assert w.run_job({"lease": "fedcba9876543210", "audio": "/x", "lease_s": 10.0}) == "released"
    assert any(p.endswith("fedcba9876543210/release") for p, _b in client.posts)


# -- the public whisper-queue API (the radio session's encoder; restart hand-over) -------------------------------------


def test_behind_live_counts_only_fire_traffic_never_the_context_classes():
    from sizeup.stt import BEHIND_S, CATCHUP, CONTEXT_FALLBACK, HIGH, RERUN, Transcriber
    clock = Clock()
    t = Transcriber(SimpleNamespace(), lambda item: None)
    t._q.clock = clock
    for prio in (LOW, CONTEXT, CONTEXT_FALLBACK, RERUN, CATCHUP):
        t.submit(SimpleNamespace(id=f"x{prio}"), np.zeros(10, np.float32), prio)
    clock.t += BEHIND_S + 60
    assert t._q.behind() is True and t.behind_live() is False        # context backlog: not "live behind"
    t.submit(SimpleNamespace(id="fire"), np.zeros(10, np.float32), URGENT)
    clock.t += BEHIND_S
    assert t.behind_live() is False                                  # (exactly BEHIND_S: not more)
    clock.t += 1
    assert t.behind_live() is True
    for prio in (HIGH, LIVE):
        t2 = Transcriber(SimpleNamespace(), lambda item: None)
        t2._q.clock = clock
        t2.submit(SimpleNamespace(id="x"), np.zeros(10, np.float32), prio)
        clock.t += BEHIND_S + 1
        assert t2.behind_live() is True


def test_withdraw_current_and_stop_hand_back_everything_in_order():
    from sizeup.stt import Transcriber
    hold = Hold()
    t = Transcriber(SimpleNamespace(transcribe=hold.transcribe), lambda item: None)
    t.start()
    items = [SimpleNamespace(id=f"c{i}", fm=True) for i in range(4)]
    t.submit(items[0], np.zeros(16000, np.float32), URGENT, "FG1")
    assert wait(lambda: t.current() is not None, 5)
    t.submit(items[1], np.zeros(16000, np.float32), LOW, "tg:383")
    t.submit(items[2], np.zeros(16000, np.float32), URGENT, "FG1")
    t.submit(items[3], np.zeros(16000, np.float32), LIVE, "South 2")
    cur = t.stop(0.2)                                   # the one on whisper doesn't finish: handed back
    assert cur[1] is items[0] and cur[0] == URGENT and cur[3] == "FG1"
    got = t.withdraw()
    assert [(g[0], g[1].id, g[3]) for g in got] == [(URGENT, "c2", "FG1"), (LIVE, "c3", "South 2"), (LOW, "c1", "tg:383")]
    assert all(isinstance(g[4], float) for g in got) and t.busy(catchup=False) == 1   # (the one still on whisper)
    hold.go.set()
    assert wait(lambda: t.current() is None, 5)
    assert t.withdraw() == []                            # stopped: nothing more was taken


def test_a_source_the_pre_alerts_push_from_is_high_at_least_whatever_its_tier(tmp_path, monkeypatch):
    from sizeup import intel
    from sizeup.app import App
    from sizeup.incidents import Traffic
    from sizeup.stt import HIGH
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    app.tg.info[("mrrs", 9212)] = {"name": "Tac", "tier": "text"}
    t = Traffic("9212-1-call_1", "Tac", 1.0, 3.0, "", status="pending")
    t.source_key = "p25:mrrs:9212"
    assert app._stt_priority(t, "p25:mrrs:9212") == LOW                       # a text-tier talkgroup: context
    monkeypatch.setattr(intel, "push_sources", lambda: (frozenset({"p25:mrrs:9212"}), 3), raising=False)
    assert app._stt_priority(t, "p25:mrrs:9212") == HIGH                      # its alerts: never shed as context
    fire = Traffic("FG1-1", "FG1", 1.0, 3.0, "", status="pending")
    monkeypatch.setattr(intel, "push_sources", lambda: (frozenset({"FG1"}), 4), raising=False)
    assert app._stt_priority(fire, "FG1") == URGENT                           # (never lowered)
    monkeypatch.setattr(intel, "push_sources", lambda: (_ for _ in ()).throw(RuntimeError("no engine")), raising=False)
    assert app._stt_priority(t, "p25:mrrs:9212") == LOW                       # a failing source raises nothing


def test_the_hub_sends_its_stations_the_alert_sources_when_their_revision_differs(tmp_path, monkeypatch):
    from sizeup import intel
    from sizeup.stt import HIGH
    from sizeup.incidents import Traffic
    state = {"keys": frozenset({"p25:mrrs:9212"}), "rev": 7}
    monkeypatch.setattr(intel, "push_sources", lambda: (state["keys"], state["rev"]), raising=False)
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: st.stt_alert_rev == 7, 20)
        assert st.stt_alert_keys() == frozenset({"p25:mrrs:9212"})
        st.tg.info[("mrrs", 9212)] = {"name": "Tac", "tier": "text"}
        t = Traffic("9212-2-call_1", "Tac", 1.0, 3.0, "", status="pending")
        assert st._stt_priority(t, "p25:mrrs:9212") == HIGH                   # the station's queue too
        state.update(keys=frozenset(), rev=8)                                  # the set changes: sent again
        assert wait(lambda: st.stt_alert_rev == 8 and st.stt_alert_keys() == frozenset(), 20)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
