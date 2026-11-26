"""Whisper capacity across the cluster (#639 phase 2): every station's whisper is a worker in the hub's pool, so a clip
from one station is transcribed on another while it has room; a GPU worker first, then an idle station's whisper, then
AWS; a station's whisper never takes a pre-alert clip nor an AWS channel's; a station hands over the context clips it
would shed and everything still waiting when it stops. Nothing is dropped while any worker is idle. Invented stations,
talkgroups, texts and synthetic audio only; local fakes, never AWS."""

import base64
import threading
import time
from pathlib import Path
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
    hub.app.hub.workers.act({"action": "pool", "stations": {"st-a": True, "st-b": True, "st-c": True}})   # (an admin's)
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
        app.hub.workers.act({"action": "pool", "stations": {"st-b": True}})
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
    hub.app.hub.workers.act({"action": "pool", "stations": {"st-b": True}})
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


def test_a_stations_whisper_is_out_of_the_pool_until_an_admin_puts_that_station_in(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="st-a"))
    try:
        reg = hub.app.hub.workers
        token = hub.app.hub.stations.add("st-b")
        assert reg.enabled(LOW) is False                     # no GPU worker, no station in: nothing changes (#651)
        code, out = call(hub.url + "/api/station/stt/claim", "POST", {"wait": 0}, token=token)
        assert code == 403 and out["reason"] == "stations_off"
        assert hub.app.pool_word("st-b")["worker"] is False and hub.app.pool_word("st-b")["on"] is False
        notes = []
        reg.act({"action": "pool", "stations": {"st-b": True}}, "admin", lambda k, o, n: notes.append((k, o, n)))
        assert notes == [("stt workers/pool/station st-b", "off", "on")] and reg.enabled(LOW)
        assert reg.stations_on("st-b") and not reg.stations_on("st-c")
        assert call(hub.url + "/api/station/stt/claim", "POST", {"wait": 0}, token=token) == (200, {"job": None})
    finally:
        hub.close()


def test_an_admin_turns_a_stations_whisper_on_from_its_row_in_settings(tmp_path):
    import json as _json
    from sizeup.cad.api import CadApi
    from sizeup.cloud.hubstore import MonitorDb
    from sizeup.cloud.stations import StationRegistry
    from sizeup.cloud.sttworkers import WorkerRegistry
    from tests.test_station_identity import CASEY, Identity, make_cad, people
    cad = make_cad(tmp_path)
    people(cad)
    mdb = MonitorDb(tmp_path / "hub.sqlite")
    cad.stations = StationRegistry(mdb, log=lambda m: None)
    cad.stt_workers = WorkerRegistry(mdb, log=lambda m: None)
    cad.stations.add("st-north")
    api, me = CadApi(cad), Identity(email=CASEY)
    rows = _json.loads(api.get("/api/cad/stations", "", me)[1])["stations"]
    assert rows[0]["pool_whisper"] is False                           # off by default
    body = _json.dumps({"action": "pool_whisper", "id": "st-north", "on": True}).encode()
    assert api.post("/api/cad/stations", body, me)[0] == 200
    assert _json.loads(api.get("/api/cad/stations", "", me)[1])["stations"][0]["pool_whisper"] is True
    assert "stt workers/pool/station st-north" in _json.dumps(cad.settings.audit_log("system"))
    assert api.post("/api/cad/stations", _json.dumps({"action": "pool_whisper", "id": "nope", "on": True}).encode(),
                    me)[0] == 400


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
    # (a long poll: the worker is always in a claim, so the hub's word and the hand-over never fall in the moment
    # between two of its polls, when it isn't "idle" to the pool: that gap, under load, was this test's flake)
    gpu = FakeWorker(hub.url, token, text="the pool heard this", wait_s=20.0).start()
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        hold = Hold()
        st.stt.client = hold
        assert call(hub.url + "/api/cad/settings/channels", "POST", {"action": "set", "key": "FG1", "engine": "local"},
                    token=None)[0] == 200                   # (FG1 on the station's whisper, #896: its engine says so)
        assert wait(lambda: st.channel_pref("FG1")["engine"] == "local", 15)
        st._hub_aws = {"ok": False}                         # (FG1 on the station's whisper: AWS can't)
        # the station has heard the hub's word that one GPU worker is idle and quick (the hand-over's budget is that
        # word, not the room for context clips: waiting for the room alone let it hand over none)
        assert wait(lambda: st.link.pool_has_room() and st.link.pool_gpu_idle_n() == 1, 15)
        ids = []
        for i in range(3):
            ids.append(fm_segment(st))
            time.sleep(0.01)
        assert wait(lambda: st.stt.busy() == 3 and hold.calls == 1, 10)   # one on whisper, two waiting
        n = st._stt_handoff(wait_s=0.2)                     # the update stops it: the one on whisper doesn't finish
        # one idle quick GPU worker: one Fire/EMS clip to it, the other two saved for the next start (never to the
        # stopping station's own whisper)
        assert n["pool"] == 1 and n["saved"] == 2 and n["in_flight"] == 1
        saved = {e["id"] for e in __import__("json").loads(
            (Path(st.cfg.clips_dir).parent / "stt-queue.json").read_text())}
        (handed,) = set(ids) - saved
        hold.go.set()
        assert wait(lambda: any(t["id"] == handed and t["text"] for t in all_traffic(hub.get("/api/state"))), 40)
        assert gpu.jobs                                     # the pool took it
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
    n = app._stt_handoff(wait_s=0.1)                            # no hub: saved, every one
    assert (n["pool"], n["saved"], n["skipped"], n["in_flight"]) == (0, 5, 0, 1)
    assert (tmp_path / "stt-queue.json").is_file()
    hold.go.set()
    app2 = App(cfg)                                             # the next start
    heard = []
    app2._alarm = lambda t, text=None: heard.append(t.id)       # (what acts on live words: never a quiet catch-up)
    # (the one on whisper when it stopped finished meanwhile: it has its text, so it isn't queued again)
    assert app2._stt_resume() == 4 and not (tmp_path / "stt-queue.json").exists()
    order = [(e[0], e[2].id) for e in sorted(app2.stt._q._items, key=lambda e: app2.stt._q._key(e, time.time()))]
    assert [cid for _p, cid in order[:1]] == [fire2] and {p for p, _c in order[:1]} == {URGENT}
    assert {p for p, cid in order[1:]} == {LOW} and {cid for _p, cid in order[1:]} == set(ctx)
    app2.stt.client = SimpleNamespace(transcribe=lambda audio, prompt="", **k: ("Engine 99 responding", 0.9))
    app2.stt.start()
    done = lambda: {t.id for t in app2.store.loose if t.text}     # noqa: E731
    assert wait(lambda: done() >= {fire2, *ctx}, 20)
    assert set(heard) >= {fire2} and heard.count(fire) == 0     # alerts as ever, never twice


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
    room = {"ready": True, "max_s": 30.0}
    app.link = SimpleNamespace(pool_takes=lambda: True, pool_has_room=lambda seconds=None: room["ready"] and (
        seconds is None or seconds <= room["max_s"]))
    app.forwarding = lambda: True
    app._emit = lambda kind, d, clip=None: sent.append((kind, d))
    t = Traffic("MRRSOps-1", "MRRS Ops", 1.0, 4.0, "", status="pending")
    app._stt_dropped(t, f"{SHED}: speech-to-text 15 min behind, a context talkgroup")
    (kind, d), = [x for x in sent if x[0] == "traffic"]
    assert d["transcript_pending"] == "pool" and d["status"] == "remote" and t.status == "remote"
    # behind by more than OFFLOAD_WAIT_S with room elsewhere: a context clip goes at once; a fire one never
    app.stt.stats = lambda: {"queued": 10, "service_s": 16.0}
    assert app._offload_now(LOW) and not app._offload_now(URGENT) and not app._offload_now(LIVE)
    assert not app._offload_now(LOW, 40.0)                       # (longer than any worker elsewhere takes: stays here)
    app.stt.stats = lambda: {"queued": 1, "service_s": 16.0}
    assert not app._offload_now(LOW)
    room["ready"] = False                                        # no room elsewhere: a shed clip is skipped here as ever
    t3 = Traffic("MRRSOps-3", "MRRS Ops", 1.0, 4.0, "", status="pending")
    sent.clear()
    app._stt_dropped(t3, f"{SHED}: speech-to-text 15 min behind, a context talkgroup")
    assert t3.status == "skipped" and not [d for k, d in sent if k == "traffic" and d.get("transcript_pending")]
    app.link = SimpleNamespace(pool_takes=lambda: False, pool_has_room=lambda seconds=None: False)
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


class OneAtATimeWhisper:
    """A whisper-server stand-in on a port: one request at a time (as whisper-server is), decode time = audio seconds x
    factor, and a request whose client goes away stops at once (whisper-server's "client disconnected, aborted")."""

    def __init__(self, factor=0.3):
        import http.server
        import select
        import socket
        import wave as _wave
        import io as _io
        self.factor, self.aborted, self.done = factor, [], []
        lock = threading.Lock()
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                i = raw.find(b"RIFF")
                with _wave.open(_io.BytesIO(raw[i:])) as w:
                    secs = w.getnframes() / w.getframerate()
                with lock:
                    end = time.monotonic() + secs * me.factor
                    while time.monotonic() < end:
                        r, _w, _x = select.select([self.connection], [], [], 0.02)
                        if r and self.connection.recv(1, socket.MSG_PEEK) == b"":
                            me.aborted.append(secs)
                            return
                    me.done.append(secs)
                    body = _json.dumps({"text": f"{secs:.0f} seconds heard", "segments": []}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
        import json as _json
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/inference"

    def close(self):
        self.srv.shutdown()


def station_worker(whisper, client, monkeypatch, cpu=20.0):
    """A station's real Transcriber on the one-at-a-time whisper, and its pool worker."""
    from sizeup.cloud import stationworker
    from sizeup.stt import Transcriber, WhisperClient
    monkeypatch.setattr(stationworker, "QUIET_S", 0.0)
    done = []
    stt = Transcriber(WhisperClient(whisper.url), lambda item: done.append((item.id, time.monotonic(), item.raw)))
    app = SimpleNamespace(stt=stt, whisper=SimpleNamespace(state="ok"), cfg=SimpleNamespace(stt_model="m/ggml-small.bin"))
    link = SimpleNamespace(pool={"worker": True})
    return StationWorker(app, link, client, log=lambda m: None, cpu=lambda: cpu), stt, done, link


def remote_job(seconds, lease="0123456789abcdef"):
    from sizeup.stt import wav_bytes
    return {"lease": lease, "audio": "/x", "lease_s": 10.0, "prompt": ""}, wav_bytes(np.zeros(int(16000 * seconds),
                                                                                              np.float32), 16000)


def test_a_page_arriving_mid_remote_job_cuts_it_and_gets_its_text_at_its_normal_time(monkeypatch):
    whisper = OneAtATimeWhisper(factor=0.3)
    try:
        job, audio = remote_job(12.0)                           # another station's clip: 3.6 s of decode here
        client = FakeClient(job=job, audio=audio)
        w, stt, done, _link = station_worker(whisper, client, monkeypatch)
        stt.start()
        # the page alone first: its normal time
        page0 = SimpleNamespace(id="page-0", fm=False, low_energy=False)
        t0 = time.monotonic()
        stt.submit(page0, np.zeros(16000 * 2, np.float32) + 0.1, URGENT, "South 2")
        assert wait(lambda: done, 5)
        normal = done[0][1] - t0
        done.clear()
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", w.run_job(job)))
        th.start()
        time.sleep(0.6)                                          # mid-way through the remote decode ...
        page = SimpleNamespace(id="page-1", fm=False, low_energy=False)
        t1 = time.monotonic()
        stt.submit(page, np.zeros(16000 * 2, np.float32) + 0.1, URGENT, "South 2")   # ... a page of its own
        assert wait(lambda: done, 5)
        took = done[0][1] - t1
        th.join(10)
        assert out["r"] == "preempted" and whisper.aborted == [pytest.approx(12.0, abs=0.1)]   # the remote decode cut
        assert took < normal + 1.0, (took, normal)                # within ~1 s of its normal time (not 3 s later)
        assert any(p.endswith("/release") for p, _b in client.posts) and w.stats["preempted"] == 1
    finally:
        whisper.close()


def test_the_stations_worker_gates_quiet_spell_own_work_cpu_and_clip_length(monkeypatch):
    from sizeup.cloud import stationworker
    whisper = OneAtATimeWhisper(factor=0.05)
    try:
        job, audio = remote_job(3.0)
        client = FakeClient(job=job, audio=audio)
        w, stt, done, link = station_worker(whisper, client, monkeypatch, cpu=97.0)
        assert w.why_not().startswith("the CPU busy")
        w.cpu = lambda: 20.0
        assert w.why_not() == ""
        monkeypatch.setattr(stationworker, "QUIET_S", 90.0)
        stt.ours_heard()                                         # its own traffic just now: a quiet spell first
        assert w.why_not().startswith("its own traffic")
        monkeypatch.setattr(stationworker, "QUIET_S", 0.0)
        stt.submit(SimpleNamespace(id="own"), np.zeros(1600, np.float32), LOW, "tg:383")
        assert w.why_not().startswith("its own clips first")
        # claimed while its own work came: handed back at once, never decoded
        assert w.run_job(job) == "released" and whisper.done == [] and whisper.aborted == []
        stt.withdraw()
        link.pool = {"worker": False}
        assert "isn't in the pool" in w.why_not()
        link.pool = {"worker": True}
        # its pace: the longest clip it would take is what it decodes in MAX_DECODE_S
        stt.timed.extend([(0, LIVE, 0.1, 6.0, 3.0)] * 5)          # 2 s of decode per audio second
        assert w.rtf() == 2.0 and w.load()["max_s"] == stationworker.MAX_DECODE_S / 2.0
        assert w.run_job(job) == "done" and whisper.done                     # one greedy pass (fast)
        done_body = [b for p, b in client.posts if p.endswith("/complete")][0]
        assert done_body["text"] == "3 seconds heard"
    finally:
        whisper.close()


def test_the_pool_never_hands_a_station_a_clip_longer_than_it_decodes_quickly_nor_its_own():
    clock = Clock()
    p = Pool(enabled=lambda c: True, clock=clock, log=lambda s: None)
    w = idle(p, "station:st-b", "station")
    w.load = {"max_s": 4.0}
    long = Job("long", LOW, CTX, "traffic", 10.0, audio=lambda: b"", on_done=lambda j, r: None,
               on_fallback=lambda j, why: None)
    own = Job("own", LOW, CTX, "traffic", 2.0, audio=lambda: b"", on_done=lambda j, r: None,
              on_fallback=lambda j, why: None, extra={"origin": "st-b"})
    assert p.ready(LOW, seconds=10.0)[0] is False and p.ready(LOW, origin="st-b")[0] is False
    p._queue += [long, own]
    assert p.claim("station:st-b", 0) is None                 # neither: too long, and its own
    assert p.word("st-b")["ready"] == 0 and p.word("st-c")["max_s"] == 4.0


def test_the_stop_path_runs_before_whisper_and_the_link_stop(tmp_path, monkeypatch):
    from sizeup.app import App
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    order = []
    monkeypatch.setattr(app, "_stt_handoff", lambda *a, **k: order.append("stt queue") or {})
    monkeypatch.setattr(app.whisper, "stop", lambda: order.append("whisper"))
    app.stop()
    assert order[:2] == ["stt queue", "whisper"]


def test_a_stopping_station_hands_pre_alert_clips_over_only_to_an_idle_gpu_and_saves_the_rest(tmp_path):
    from sizeup.app import App
    from sizeup.incidents import Traffic
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    sent = []
    word = {"gpu_idle": False, "room": True}
    app.link = SimpleNamespace(pool_takes=lambda: True, pool_gpu_idle=lambda: word["gpu_idle"],
                               pool_has_room=lambda seconds=None: word["room"])
    app.forwarding = lambda: True
    app._emit = lambda kind, d, clip=None: sent.append((kind, d))
    fire = Traffic("FG1-7", "FG1", 1.0, 4.0, "", status="pending")
    ctx = Traffic("MRRSOps-7", "MRRS Ops", 1.0, 4.0, "", status="pending")
    app.stt.submit(fire, np.zeros(1600, np.float32), URGENT, "FG1")
    app.stt.submit(ctx, np.zeros(1600, np.float32), LOW, CTX)
    n = app._stt_handoff(wait_s=0.0)
    assert (n["pool"], n["saved"], n["skipped"]) == (1, 1, 0)   # the context clip to the pool, Fire/EMS saved here
    saved = __import__("json").loads((tmp_path / "stt-queue.json").read_text())
    assert [(e["id"], e["class"]) for e in saved] == [("FG1-7", URGENT)]
    # a saved queue that can't be read is kept aside, said, never crashes the start
    (tmp_path / "stt-queue.json").write_text("{not json")
    assert app._stt_resume() == 0 and (tmp_path / "stt-queue.bad").is_file()


def test_with_nowhere_to_save_the_stop_marks_them_for_the_catch_up_as_before(tmp_path):
    from sizeup.app import App
    from sizeup.incidents import Traffic
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    app._stt_queue_file = lambda: None                            # (no clips dir: nowhere to save)
    t = Traffic("FG1-8", "FG1", 1.0, 4.0, "", status="pending")
    app.stt.submit(t, np.zeros(1600, np.float32), URGENT, "FG1")
    n = app._stt_handoff(wait_s=0.0)
    assert n["skipped"] == 1 and t.status == "skipped" and "the station stopped" in t.model
    assert [r["id"] for r in app.cad.comms.shed_clips(0)[0]] == ["FG1-8"]   # the catch-up's, stopped-station rows first


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
    from sizeup.app import App
    from sizeup.incidents import Traffic
    from sizeup.intel import engine
    from sizeup.stt import HIGH
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    app.tg.info[("mrrs", 9212)] = {"name": "Tac", "tier": "text"}
    t = Traffic("9212-1-call_1", "Tac", 1.0, 3.0, "", status="pending")
    t.source_key = "p25:mrrs:9212"

    def push(keys, rev):
        monkeypatch.setattr(engine, "push_sources", lambda: (frozenset(keys), rev))
    push(set(), 0)
    assert app._stt_priority(t, "p25:mrrs:9212") == LOW                 # a text-tier talkgroup: context
    push({"p25:mrrs:9212"}, 3)
    assert app._stt_priority(t, "p25:mrrs:9212") == HIGH                # its alerts: never shed as context
    # the queue's keys as rows are keyed: the county's "tg:<n>", an FM label
    push({"p25:westchester:383", "fm:MRRS Ops"}, 4)
    ctx = Traffic("383-1-call_1", "WCPD", 1.0, 3.0, "", status="pending", talkgroup=383)
    app.tg.main = lambda tg, system=None: False                          # (a county talkgroup heard as context: LOW)
    assert app._stt_class(ctx, "tg:383") == LOW and app._stt_priority(ctx, "tg:383") == HIGH
    fmx = Traffic("MRRSOps-1", "MRRS Ops", 1.0, 3.0, "", status="pending")
    app._stt_class = lambda item, key: LOW                               # (an FM channel's clip as context)
    assert App._stt_priority(app, fmx, "MRRS Ops") == HIGH
    del app._stt_class
    fire = Traffic("FG1-1", "FG1", 1.0, 3.0, "", status="pending")
    push({"fm:FG1"}, 5)
    assert app._stt_priority(fire, "FG1") == URGENT                     # (never lowered)
    monkeypatch.setattr(engine, "push_sources", lambda: (_ for _ in ()).throw(RuntimeError("no engine")))
    assert app._stt_priority(t, "p25:mrrs:9212") == LOW                 # a failing source raises nothing


def test_the_station_and_the_engine_say_the_same_of_every_key(tmp_path, monkeypatch):
    """The cross-check (#716): a station deciding from the set the hub sent it and the hub deciding with
    sizeup.intel.is_push_source agree on county talkgroups, other systems', FM and airband labels."""
    from sizeup import intel
    from sizeup.app import App
    from sizeup.intel import engine
    from tests.test_cloud import base
    keys = frozenset({"p25:westchester:383", "p25:mrrs:9212", "fm:FG9", "air:Tower"})
    monkeypatch.setattr(engine, "push_sources", lambda: (keys, 7))
    st = App(base(clips_dir=str(tmp_path / "clips")))
    st.forwarding = lambda: True
    st.set_stt_alert_keys(sorted(keys), 7)
    for key, kind in (("tg:383", ""), ("tg:307", ""), ("p25:westchester:383", ""), ("p25:mrrs:9212", ""),
                      ("p25:mrrs:9211", ""), ("FG9", ""), ("FG9", "traffic"), ("FG1", ""), ("Tower", "airband"),
                      ("Tower", ""), ("fm:FG9", ""), ("air:Tower", "")):
        assert st.is_push_source(key, kind) == intel.is_push_source(key, kind), (key, kind)
    assert intel.is_push_source("tg:383") and intel.is_push_source("FG9") and not intel.is_push_source("tg:307")


def test_the_hub_sends_its_stations_the_alert_sources_when_their_revision_differs(tmp_path, monkeypatch):
    from sizeup import intel
    from sizeup.stt import HIGH
    from sizeup.incidents import Traffic
    state = {"keys": frozenset({"p25:mrrs:9212"}), "rev": 7}
    from sizeup.intel import engine
    monkeypatch.setattr(engine, "push_sources", lambda: (state["keys"], state["rev"]))
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: st.stt_alert_rev == 7, 20)
        assert st._push_set() == frozenset({"p25:mrrs:9212"})
        st.tg.info[("mrrs", 9212)] = {"name": "Tac", "tier": "text"}
        t = Traffic("9212-2-call_1", "Tac", 1.0, 3.0, "", status="pending")
        assert st._stt_priority(t, "p25:mrrs:9212") == HIGH                   # the station's queue too
        state.update(keys=frozenset(), rev=8)                                  # the set changes: sent again
        assert wait(lambda: st.stt_alert_rev == 8 and st._push_set() == frozenset(), 20)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


# -- the re-review of 8c9eb90 -----------------------------------------------------------------------------------------------


def test_nothing_goes_to_a_hub_not_heard_lately_and_police_alert_clips_only_to_an_idle_gpu(tmp_path):
    from sizeup.app import App
    from sizeup.cloud.station import StationLink
    from sizeup.incidents import Traffic
    from tests.test_cloud import base
    link = StationLink.__new__(StationLink)                    # its pool word only (no threads)
    link.hub_caps = frozenset({"stt_pool"})
    link.pool = {"on": True, "ready": 2, "max_s": 30.0, "gpu_idle": True}
    link.last_ok, link.error_since = time.time(), None
    link._lock = threading.Lock()
    assert link.pool_has_room(5.0) and link.pool_gpu_idle()
    link.last_ok = time.time() - 600                           # the hub not heard lately (it is deploying): its word is stale
    assert not link.pool_has_room(5.0) and not link.pool_gpu_idle()
    app = App(base(clips_dir=str(tmp_path / "clips")))
    app.link, app.forwarding = link, lambda: True
    app.stt.stats = lambda: {"queued": 10, "service_s": 16.0}
    assert not app._offload_now(LOW, 3.0)                      # unreachable hub: kept here
    t = Traffic("MRRSOps-9", "MRRS Ops", 1.0, 4.0, "", status="pending")
    sent = []
    app._emit = lambda kind, d, clip=None: sent.append((kind, d))
    app._stt_dropped(t, f"{SHED}: speech-to-text 15 min behind, a context talkgroup")
    assert t.status == "skipped" and not [d for k, d in sent if d.get("transcript_pending")]
    link.last_ok = time.time()
    assert app._offload_now(LOW, 3.0) and app._offload_now(CONTEXT, 3.0)
    link.pool["gpu_idle"] = False                              # no GPU worker idle: a police alert clip stays here
    assert app._offload_now(LOW, 3.0) and not app._offload_now(CONTEXT, 3.0)


def test_a_handed_over_clip_carries_its_own_station_and_never_goes_back_to_it(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="st-a"))
    try:
        app = hub.app
        ctx_tier(app)
        reg = app.hub.stations
        tok_b = reg.add("st-b")
        app.hub.workers.act({"action": "pool", "stations": {"st-b": True}})
        w = FakeStation(hub.url, tok_b).start()
        try:
            assert wait(lambda: app.sttpool.ready_workers() == ["station:st-b"], 10)
            now = time.time()
            # st-b's own clip, handed over by st-b: never back to st-b's whisper (the catch-up then)
            assert call(hub.url + "/api/station/events", "POST", {"epoch": "e2", "station": "st-b", "events": [
                ctx_event("9570-1794-call_70", 1, now)]}, token=tok_b)[0] in (200, 409)
            got = next((t for t in app.store.loose if t.id == "9570-1794-call_70"), None)
            if got is not None:                                # (a hub that takes one station refuses st-b's events)
                assert got.origin_station == "st-b" and w.jobs == []
            # the primary station's clip: st-b may take it
            send(hub, ctx_event("9570-1794-call_71", 1, now))
            assert wait(lambda: row(hub, "9570-1794-call_71").get("status") == "done", 20)
            assert next(t for t in app.store.loose if t.id == "9570-1794-call_71").origin_station == "st-a"
        finally:
            w.close()
    finally:
        hub.close()


# -- the final check of 601cb35 ---------------------------------------------------------------------------------------------


def test_a_stopping_station_hands_one_pre_alert_clip_per_idle_gpu_worker_and_saves_the_rest(tmp_path):
    from sizeup.app import App
    from sizeup.incidents import Traffic
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    sent = []
    idle_n = {"n": 1}
    app.link = SimpleNamespace(pool_takes=lambda: True, pool_gpu_idle=lambda: idle_n["n"] > 0,
                               pool_gpu_idle_n=lambda: idle_n["n"], pool_has_room=lambda seconds=None: True)
    app.forwarding = lambda: True
    app._emit = lambda kind, d, clip=None: sent.append((kind, d))
    fires = [Traffic(f"FG1-{i}", "FG1", 1.0 + i, 4.0 + i, "", status="pending") for i in range(3)]
    for t in fires:
        app.stt.submit(t, np.zeros(1600, np.float32), URGENT, "FG1")
    n = app._stt_handoff(wait_s=0.0)
    assert (n["pool"], n["saved"]) == (1, 2)                     # one idle quick GPU worker: one clip to it
    handed = [d["id"] for k, d in sent if k == "traffic" and d.get("transcript_pending") == "pool"]
    saved = __import__("json").loads((tmp_path / "stt-queue.json").read_text())
    assert len(handed) == 1 and sorted([handed[0]] + [e["id"] for e in saved]) == ["FG1-0", "FG1-1", "FG1-2"]
    # the hub's last word caught its GPU worker between two polls (not idle then): none handed over, all saved, none
    # lost (the restart test's flake under load, #715 follow-up: what the test saw was this, not a lost clip)
    idle_n["n"] = 0
    (tmp_path / "stt-queue.json").unlink()
    sent.clear()
    again = [Traffic(f"FG1-{i}", "FG1", 9.0 + i, 12.0 + i, "", status="pending") for i in range(3)]
    for t in again:
        app.stt._stopping = False
        app.stt.submit(t, np.zeros(1600, np.float32), URGENT, "FG1")
    n = app._stt_handoff(wait_s=0.0)
    assert (n["pool"], n["saved"]) == (0, 3) and not [d for k, d in sent if d.get("transcript_pending")]
    assert sorted(e["id"] for e in __import__("json").loads((tmp_path / "stt-queue.json").read_text())) == [
        "FG1-0", "FG1-1", "FG1-2"]


def test_a_saved_clip_whisper_finished_during_the_stop_is_never_transcribed_twice(tmp_path):
    from sizeup.app import App
    from tests.test_cloud import base
    cfg = base(clips_dir=str(tmp_path / "clips"))
    app = App(cfg)
    hold = Hold()
    app.stt.client = hold
    app.stt.start()
    first = fm_segment(app)                                      # on whisper when the station stops
    time.sleep(0.01)
    second = fm_segment(app)
    assert wait(lambda: app.stt.busy() == 2 and hold.calls == 1, 10)
    assert app._stt_handoff(wait_s=0.0)["saved"] == 2            # (both saved: the first was still on whisper)
    hold.go.set()                                                # ... and whisper finishes it before the exit
    assert wait(lambda: (app.cad.store.one("SELECT status FROM radio_traffic WHERE id = ?", (first,)) or {}).get(
        "status") == "done", 10)
    app2 = App(cfg)
    assert app2._stt_resume() == 1                               # the second only: the first has its text
    assert [e[2].id for e in app2.stt._q._items] == [second]


def test_a_pooled_stations_identity_reaches_only_the_transcription_routes(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="st-a"))
    try:
        tok = hub.app.hub.stations.add("st-b")
        hub.app.hub.workers.act({"action": "pool", "stations": {"st-b": True}})
        assert call(hub.url + "/api/station/stt/hello", "POST", {"state": "online"}, token=tok)[0] == 200
        assert call(hub.url + "/api/station/stt/claim", "POST", {"wait": 0}, token=tok) == (200, {"job": None})
        assert call(hub.url + "/api/station/stt/jobs/0123456789abcdef/complete", "POST", {"text": "x"},
                    token=tok)[0] == 409                         # (a lease route: listed, the lease just isn't its)
        for path in ("/api/station/stt/infer/claim", "/api/station/stt/anything-new"):
            code, out = call(hub.url + path, "POST", {}, token=tok)
            assert code == 403 and out["reason"] == "gpu_only", path   # not listed: a GPU worker's only
        gpu = hub.app.hub.workers.add("gpu-1")
        assert call(hub.url + "/api/station/stt/anything-new", "POST", {}, token=gpu)[0] == 404   # (a GPU worker: no such)
    finally:
        hub.close()
