"""The GPU worker pool (#639, sizeup/sttpool.py, sizeup/cloud/sttworkers.py, the hub's routing): a clip goes to a ready
worker first and to the path it would have taken without one (Amazon Transcribe, then the station's whisper; a shed
clip back to the catch-up) whenever no worker gives its text in time. Nothing is ever dropped, the classes keep their
strict order, a gaming or vanished worker costs a clip seconds at most, and with no worker added nothing changes.
Synthetic audio, invented ids and texts, local fakes only: no network beyond 127.0.0.1, never AWS."""

import base64
import json
import random
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from sizeup import sttpool, sttruns
from sizeup.cloud.sttworkers import WorkerRegistry, clean_result
from sizeup.stt import CATCHUP, CONTEXT, HIGH, LIVE, LOW, URGENT
from sizeup.sttpool import BUDGETS, Job, Pool
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import voice, wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws  # noqa: F401 (the fixture: test credentials only)
from tests.test_cloud import TOKEN, HubRun, all_traffic, call, fm_segment, hub_cfg, station, station_cfg

GPU_TEXT = "Engine 99 on location per the GPU"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Jobs:
    """on_done / on_fallback for unit tests: who got what, once each."""

    def __init__(self):
        self.done, self.fell = {}, {}

    def job(self, cid, cls=LIVE, seconds=3.0, **kw):
        return Job(cid, cls, "FG1", "traffic", seconds, audio=lambda: b"RIFF", on_done=self._done,
                   on_fallback=self._fall, **kw)

    def _done(self, job, res):
        assert job.id not in self.done and job.id not in self.fell
        self.done[job.id] = res

    def _fall(self, job, why):
        assert job.id not in self.done and job.id not in self.fell
        self.fell[job.id] = why


def pool(clock=None, on=True):
    return Pool(enabled=lambda cls: on, clock=clock or Clock(), log=lambda s: None)


# -- the pool on its own ---------------------------------------------------------------------------------------------


def test_with_no_worker_or_the_pool_off_nothing_is_taken():
    j = Jobs()
    p = pool()
    assert p.ready(URGENT) == (False, "no worker ready") and not p.submit(j.job("a"))
    p.seen("gpu-1", "online")
    off = pool(on=False)
    off.seen("gpu-1", "online")
    assert off.ready(URGENT) == (False, "off") and not off.submit(j.job("a"))
    assert p.submit(j.job("a")) and not p.submit(j.job("a"))           # one job per clip id
    assert p.submitted == 1


def test_the_classes_keep_their_strict_order_oldest_first_in_a_class(monkeypatch):
    monkeypatch.setattr(sttpool, "SERVICE_DEFAULT_S", 0.1)    # (a fast worker: the queue never looks too long)
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    for cid, cls in (("catchup", CATCHUP), ("low", LOW), ("live-1", LIVE), ("police", CONTEXT), ("page", URGENT),
                     ("air", HIGH), ("live-2", LIVE)):
        clock.t += 0.01
        assert p.submit(j.job(cid, cls, seconds=0.0)), cid
    got = []
    while (job := p.claim("gpu-1", 0)) is not None:
        got.append(job.id)
        assert p.complete("gpu-1", job.lease, {"text": "x"})
    assert got == ["page", "air", "live-1", "live-2", "police", "low", "catchup"]
    assert set(j.done) == set(got) and j.fell == {}


def test_a_job_nobody_claims_in_its_wait_falls_back_and_the_classes_wait_differently():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    p.submit(j.job("page", URGENT, seconds=0.0))
    p.submit(j.job("shed", CATCHUP, seconds=0.0))
    clock.t += BUDGETS[URGENT][0] - 0.1
    p.seen("gpu-1")
    assert p.tick() == 0
    clock.t += 0.2
    p.seen("gpu-1")
    assert p.tick() == 1 and p.drain() == 1
    assert "no GPU worker took it in 2.5 s" in j.fell["page"] and "shed" not in j.fell
    clock.t += BUDGETS[CATCHUP][0]
    p.seen("gpu-1")
    assert p.tick() == 1 and p.drain() == 1 and "shed" in j.fell
    assert p.stats()["fallbacks"]["unclaimed"] == 2


def test_a_lease_that_runs_out_falls_back_and_the_late_answer_is_refused():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    p.submit(j.job("a", URGENT, seconds=4.0))
    job = p.claim("gpu-1", 0)
    lease_s = BUDGETS[URGENT][1] + sttpool.PER_AUDIO_S * 4.0
    assert job.public()["lease_s"] == lease_s and job.public()["audio"].endswith(f"/{job.lease}/audio")
    assert p.job_for("gpu-1", job.lease) is job and p.job_for("gpu-2", job.lease) is None    # its holder's only
    clock.t += lease_s
    assert p.tick() == 1 and p.drain() == 1 and "lease ran out" in j.fell["a"]
    assert p.complete("gpu-1", job.lease, {"text": "late"}) is False and p.refused_late == 1 and "a" not in j.done
    assert p.job_for("gpu-1", job.lease) is None and p.extend("gpu-1", job.lease) is None


def test_extend_renews_the_lease_up_to_its_cap_never_past_it():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    p.submit(j.job("a", URGENT, seconds=0.0))
    job = p.claim("gpu-1", 0)
    wait_s, lease_s, cap_s = BUDGETS[URGENT]
    for _ in range(10):
        clock.t += lease_s - 1
        if p.extend("gpu-1", job.lease) is None:
            break
        assert job.expires <= job.cap_at
    assert clock.t - job.claimed_at <= cap_s + lease_s
    p.tick()
    p.drain()
    assert "a" in j.fell and "cap" in j.fell["a"]


def test_a_worker_going_gaming_or_paused_takes_nothing_new_and_what_waits_falls_back_at_once():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    p.submit(j.job("a", LIVE))
    p.seen("gpu-1", "gaming")
    assert p.ready(LIVE) == (False, "no worker ready")
    assert p.claim("gpu-1", 0, state="gaming") is None
    assert p.tick() == 1 and p.drain() == 1 and j.fell["a"] == "no GPU worker ready"   # at once, not after its wait
    assert p.stats()["workers"][0]["state"] == "gaming"
    p.seen("gpu-1", "paused")
    assert not p.submit(j.job("b")) and p.stats()["workers"][0]["state"] == "paused"
    p.seen("gpu-1", "online")
    assert p.submit(j.job("b"))


def test_a_worker_that_disappears_mid_job_loses_it_and_goes_offline():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    p.submit(j.job("a", LIVE, seconds=0.0))
    job = p.claim("gpu-1", 0)
    clock.t += BUDGETS[LIVE][1]
    p.tick()
    p.drain()
    assert "a" in j.fell
    clock.t += sttpool.OFFLINE_S
    assert p.stats()["workers"][0]["state"] == "offline" and not p.ready(LIVE)[0]
    assert job.lease not in p._leases and not p.has("a")


def test_handing_back_falls_back_now_and_repeated_failures_cool_the_worker_down():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    for i in range(sttpool.FAIL_N):
        p.submit(j.job(f"c{i}", LIVE, seconds=0.0))
        job = p.claim("gpu-1", 0)
        assert p.release("gpu-1", job.lease, "whisper-server down")
    p.drain()
    assert all("handed it back: whisper-server down" in j.fell[f"c{i}"] for i in range(sttpool.FAIL_N))
    assert p.stats()["workers"][0]["state"] == "cooling" and not p.ready(LIVE)[0]
    clock.t += sttpool.COOLDOWN_S
    p.seen("gpu-1")
    assert p.ready(LIVE)[0]


def test_a_result_that_cannot_be_applied_falls_back_never_dropped():
    clock = Clock()
    fell = []

    def boom(job, res):
        raise RuntimeError("the CAD said no")
    p = pool(clock)
    p.seen("gpu-1", "online")
    p.submit(Job("a", LIVE, "FG1", "traffic", 1.0, audio=lambda: b"", on_done=boom,
                 on_fallback=lambda job, why: fell.append(why)))
    job = p.claim("gpu-1", 0)
    assert p.complete("gpu-1", job.lease, {"text": "x"}) and fell and "couldn't be applied" in fell[0]


def test_a_busy_pool_turns_a_clip_away_rather_than_make_it_wait_past_its_budget():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    for i in range(3):                                        # a backlog of context clips
        assert p.submit(j.job(f"low{i}", LOW, seconds=0.0))
    ok, why = p.ready(LOW)
    assert ok                                                 # (LOW waits 30 s: 3 x 2 s is fine)
    assert p.ready(URGENT)[0]                                 # a page doesn't queue behind them (strict order)
    for i in range(3):
        p.claim("gpu-1", 0)                                   # three leased now: work ahead of anything
    ok, why = p.ready(URGENT)
    assert not ok and why.startswith("busy")


def test_a_revoked_worker_hands_back_what_it_holds():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    p.submit(j.job("a"))
    job = p.claim("gpu-1", 0)
    assert p.forget("gpu-1") == 1 and p.drain() == 1 and "revoked" in j.fell["a"]
    assert p.complete("gpu-1", job.lease, {"text": "x"}) is False and "gpu-1" not in p.workers


def test_nothing_is_dropped_whatever_the_workers_do():
    """Hundreds of clips, workers that answer, hand back, vanish, game and pause at random: every clip the pool took
    ends exactly once, in a text or a fallback."""
    rng = random.Random(639)
    clock, j = Clock(), Jobs()
    p = pool(clock)
    workers = ["gpu-1", "gpu-2"]
    for w in workers:
        p.seen(w, "online")
    taken, held = set(), {}
    for n in range(600):
        clock.t += rng.uniform(0.0, 1.5)
        for w in workers:
            p.seen(w, rng.choice(["online"] * 6 + ["gaming", "paused"]))
        cls = rng.choice([URGENT, HIGH, LIVE, CONTEXT, LOW, CATCHUP])
        if p.submit(j.job(f"c{n}", cls, seconds=rng.uniform(0.5, 20))):
            taken.add(f"c{n}")
        for w in workers:
            if w not in held and (job := p.claim(w, 0)) is not None:
                held[w] = job
            elif w in held:
                job, roll = held[w], rng.random()
                if roll < 0.6:
                    p.complete(w, job.lease, {"text": "words"})
                elif roll < 0.7:
                    p.release(w, job.lease, "gaming")
                elif roll < 0.8:
                    held.pop(w)                                # vanished with it: its lease runs out
                    continue
                else:
                    continue                                   # still working on it
                held.pop(w)
        p.tick()
        p.drain()
    clock.t += 1000
    p.tick()
    p.drain()
    assert taken and set(j.done) | set(j.fell) == taken and not set(j.done) & set(j.fell)
    st = p.stats()
    assert st["queued"] == 0 and st["leased"] == 0
    assert sum(st["fallbacks"].values()) == len(j.fell)


def test_the_numbers_queued_to_text_p50_p90_and_jobs_an_hour():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    p.seen("gpu-1", "online")
    for i, took in enumerate([0.4, 0.5, 0.6, 0.7, 2.0]):
        p.submit(j.job(f"c{i}", LIVE, seconds=0.0))
        job = p.claim("gpu-1", 0)
        clock.t += took
        p.complete("gpu-1", job.lease, {"text": "x"})
    st = p.stats()
    assert st["done_h"] == 5 and st["p50_s"] == 0.6 and st["p90_s"] == 2.0
    assert st["workers"][0]["jobs_h"] == 5 and st["classes"]["live"]["n"] == 5
    assert st["service_s"] == 0.6


def test_a_workers_result_is_checked_and_bounded():
    res = clean_result({"text": "  Engine 99   responding ", "model": "ggml-large-v3-turbo-q5_0.bin",
                        "segments": [{"start": 0, "end": 1.5, "text": "Engine 99 responding",
                                      "words": [{"word": "Engine", "probability": 0.9, "start": 0, "end": 0.4},
                                                {"word": "99", "probability": True}, "junk"]}],
                        "timing": {"infer_ms": 210, "fetch_ms": "x"}})
    assert res["text"] == "Engine 99 responding" and res["words"] == [["Engine", 0.9, 0, 0.4], ["99", None, None, None]]
    assert res["timing"] == {"infer_ms": 210}
    with pytest.raises(ValueError):
        clean_result({"text": 5})
    assert sttpool.model_label("ggml-large-v3-turbo-q5_0.bin", "gpu-1") == "large-v3-turbo-q5_0 (GPU gpu-1)"


# -- the hub: a fake worker over HTTP ----------------------------------------------------------------------------------


class FakeWorker:
    """A GPU worker against the hub's API: claim, fetch the audio, "transcribe" (its text by the audio's length),
    complete. mode: "answer" | "vanish" (keeps the job, never answers) | "release" | "gaming"."""

    def __init__(self, url, token, mode="answer", text=GPU_TEXT, wait_s=1.0):
        self.url, self.token, self.mode, self.text, self.wait_s = url, token, mode, text, wait_s
        self.jobs, self.audio, self.answers = [], [], []
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)

    def post(self, path, body):
        return call(self.url + path, "POST", body, token=self.token)

    def start(self):
        code, out = self.post("/api/station/stt/hello", {"version": "test", "model": "ggml-large-v3-turbo-q5_0.bin",
                                                          "state": "gaming" if self.mode == "gaming" else "online"})
        assert code == 200 and out["ok"], out
        self.t.start()
        return self

    def run(self):
        while not self.stop.is_set():
            if self.mode == "gaming":
                self.post("/api/station/stt/state", {"state": "gaming", "gpu": {"util_pct": 97, "others": 1}})
                self.stop.wait(0.2)
                continue
            code, out = self.post("/api/station/stt/claim", {"wait": self.wait_s, "state": "online",
                                                             "model": "ggml-large-v3-turbo-q5_0.bin"})
            job = (out or {}).get("job") if code == 200 else None
            if not job:
                continue
            self.jobs.append(job)
            code, wav = call(self.url + job["audio"], token=self.token)
            self.audio.append((code, wav))
            lease = job["lease"]
            if self.mode == "vanish":
                continue
            if self.mode == "release":
                self.answers.append(self.post(f"/api/station/stt/jobs/{lease}/release", {"why": "gaming started"}))
                continue
            words = [{"word": w, "probability": 0.8, "start": i * 0.3, "end": i * 0.3 + 0.3}
                     for i, w in enumerate(self.text.split())]
            self.answers.append(self.post(f"/api/station/stt/jobs/{lease}/complete", {
                "text": " " + self.text, "model": "ggml-large-v3-turbo-q5_0.bin",
                "segments": [{"start": 0.0, "end": 2.0, "text": self.text, "words": words}],
                "timing": {"fetch_ms": 5, "infer_ms": 180}}))

    def close(self):
        self.stop.set()
        self.t.join(5)


def pending_event(tid, seq=1, source="FG1", seconds=3.0):
    """The station's transmission on an AWS channel: no text, its clip (8 kHz), Amazon Transcribe on the hub."""
    from sizeup.cad.dispatcher import wav_bytes
    now = time.time()
    return {"seq": seq, "at": now, "type": "traffic",
            "data": {"id": tid, "source": source, "start": now - seconds, "end": now, "text": "", "status": "remote",
                     "transcript_pending": "aws", "stt": "aws", "model": "aws-transcribe"},
            "audio": base64.b64encode(wav_bytes((voice(seconds)[::2] * 32767).astype(np.int16), 8000)).decode()}


def row(hub, tid):
    return hub.app.cad.store.one("SELECT text, status, model, alt_text FROM radio_traffic WHERE id = ?", (tid,)) or {}


def send(hub, *events):
    assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": list(events)})[0] == 200


@pytest.fixture
def quick(monkeypatch):
    """Short budgets: a fallback in a second, not ten."""
    for cls, (w, lease, cap) in list(BUDGETS.items()):
        monkeypatch.setitem(BUDGETS, cls, (min(w, 1.0), min(lease, 1.0), min(cap, 2.0)))
    monkeypatch.setattr(sttpool, "PER_AUDIO_S", 0.0)


def test_with_no_worker_added_an_aws_clip_goes_to_aws_as_before_and_the_pool_is_never_asked(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        asked = []
        real = hub.app.sttpool.ready
        hub.app.sttpool.ready = lambda cls, now=None: asked.append(cls) or real(cls, now)
        send(hub, pending_event("FG1-1"))
        assert wait(lambda: row(hub, "FG1-1").get("text") == AWS_TEXT + ".", 20)
        assert row(hub, "FG1-1")["model"] == "aws-transcribe"
        assert hub.app.sttpool.submitted == 0 and asked and hub.app.hub.workers.any() is False
        db = sqlite3.connect(Path(hub.app.cfg.cad_db).parent / "hub.sqlite")
        assert "stt_workers" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}   # nothing made
        runs = hub.app.stt_runs.of("FG1-1")                   # the transcript is versioned all the same
        assert [(r["engine"], r["model"], r["role"], r["text"]) for r in runs] == [
            ("aws", "aws-transcribe", "text", AWS_TEXT + ".")]
        assert runs[0]["queued_at"] and runs[0]["done_at"] >= runs[0]["queued_at"] and runs[0]["class"] == "urgent"
    finally:
        hub.close()
        fake.close()


def test_a_ready_worker_transcribes_an_aws_clip_and_the_run_is_kept_with_its_words_and_timing(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    w = FakeWorker(hub.url, token).start()
    try:
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        send(hub, pending_event("FG1-2"))
        assert wait(lambda: row(hub, "FG1-2").get("text") == GPU_TEXT, 30), row(hub, "FG1-2")
        r = row(hub, "FG1-2")
        assert r["status"] == "done" and r["model"] == "large-v3-turbo-q5_0 (GPU gpu-1)"
        code, wav = w.audio[0]
        assert code == 200 and wav[:4] == b"RIFF"
        import io
        import wave
        with wave.open(io.BytesIO(wav)) as f:                 # what whisper-server takes: 16 kHz, padded 0.5 s each side
            assert f.getframerate() == 16000 and abs(f.getnframes() / 16000 - 4.0) < 0.05
        assert w.answers[0][0] == 200
        assert hub.app.aws.health()["done"] == 0             # AWS never asked
        (run,) = hub.app.stt_runs.of("FG1-2")
        assert run["engine"] == "gpu-worker" and run["worker"] == "gpu-1" and run["model"] == "ggml-large-v3-turbo-q5_0.bin"
        assert run["text"] == GPU_TEXT and run["confidence"] == 0.8 and run["class"] == "urgent"
        assert [x[0] for x in run["words"]] == GPU_TEXT.split()
        assert run["queued_at"] <= run["engine_start"] <= run["done_at"] and run["clip_end"] <= run["done_at"]
        lat = hub.app.stt_latency.table()
        assert lat[0]["channel"] == "FG1" and lat[0]["target"] and lat[0]["engines"] == {"gpu-worker": 1}
        health = hub.app.health.model()["stt_pool"]
        assert health["done_h"] == 1 and health["workers"][0]["id"] == "gpu-1" and health["latency"]
        # the worker's text is the raw transcript: a later engine's never overwrites it (a new run is appended)
        hub.app._aws_done(__import__("sizeup.app", fromlist=["AwsJob"]).AwsJob("traffic", "FG1-2", "FG1", rerun=True),
                          "another engine's words", 0.7)
        runs = hub.app.stt_runs.of("FG1-2")
        assert [x["text"] for x in runs] == [GPU_TEXT, "another engine's words"] and runs[1]["role"] == "rerun"
    finally:
        w.close()
        hub.close()
        fake.close()


def test_a_worker_that_vanishes_mid_job_costs_seconds_and_aws_gives_the_text(tmp_path, quick):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish").start()
    try:
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        send(hub, pending_event("FG1-3"))
        assert wait(lambda: w.jobs, 5)
        assert row(hub, "FG1-3")["status"] == "pending"
        t0 = time.time()
        assert wait(lambda: row(hub, "FG1-3").get("text") == AWS_TEXT + ".", 20)
        assert time.time() - t0 < 15
        assert hub.app.sttpool.stats()["fallbacks"]["lease"] == 1
        late = call(hub.url + f"/api/station/stt/jobs/{w.jobs[0]['lease']}/complete", "POST", {"text": "too late"},
                    token=token)
        assert late[0] == 409 and row(hub, "FG1-3")["text"] == AWS_TEXT + "."     # never over the text
        assert hub.app.sttpool.refused_late == 1
    finally:
        w.close()
        hub.close()
        fake.close()


def test_a_gaming_worker_takes_nothing_and_the_clip_goes_straight_to_aws(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="gaming").start()
    try:
        assert wait(lambda: (hub.app.sttpool.workers.get("gpu-1") or sttpool.Worker("x")).state == "gaming", 5)
        send(hub, pending_event("FG1-4"))
        assert wait(lambda: row(hub, "FG1-4").get("text") == AWS_TEXT + ".", 20)
        assert hub.app.sttpool.submitted == 0 and w.jobs == []
        st = hub.app.health.model()["stt_pool"]["workers"][0]
        assert st["state"] == "gaming" and st["gpu"]["util_pct"] == 97
    finally:
        w.close()
        hub.close()
        fake.close()


def test_a_worker_handing_back_goes_to_aws_at_once(tmp_path, quick):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="release").start()
    try:
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        send(hub, pending_event("FG1-5"))
        assert wait(lambda: row(hub, "FG1-5").get("text") == AWS_TEXT + ".", 20)
        assert w.answers[0][0] == 200 and hub.app.sttpool.stats()["fallbacks"]["released"] == 1
    finally:
        w.close()
        hub.close()
        fake.close()


def test_with_aws_out_too_a_lost_job_goes_to_the_stations_whisper_never_dropped(tmp_path, quick):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish").start()
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        assert wait(lambda: st._hub_aws["ok"], 10)              # the station forwards FG1's clips to the hub
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        hub.app.aws.available = lambda key=None: (False, "test: AWS can't take audio now")
        tid = fm_segment(st)
        assert wait(lambda: w.jobs, 10)                          # a worker took it ... and vanished with it
        assert wait(lambda: any(t["id"] == tid and t["text"] == "whisper heard this"
                                for t in all_traffic(hub.get("/api/state"))), 30)
        assert hub.app.sttpool.stats()["fallbacks"]["lease"] == 1
        assert [r["engine"] for r in hub.app.stt_runs.of(tid)] == ["station-whisper"]
    finally:
        w.close()
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()


def test_worker_routes_refuse_bad_tokens_and_station_tokens_and_a_worker_token_opens_no_station_route(tmp_path):
    logs = []
    hub = HubRun(hub_cfg(tmp_path))
    try:
        reg = hub.app.hub.workers
        good = reg.add("gpu-1")
        gone = reg.add("gpu-gone")
        reg.revoke("gpu-gone")
        hub.app.stt_api.log = logs.append
        for method, path in (("POST", "/api/station/stt/hello"), ("POST", "/api/station/stt/state"),
                             ("POST", "/api/station/stt/claim"), ("GET", "/api/station/stt/jobs/0123456789abcdef/audio"),
                             ("POST", "/api/station/stt/jobs/0123456789abcdef/complete")):
            body = {} if method == "POST" else None
            for tok, reason in ((None, "no_token"), ("sw1.gpu-1." + "A" * 43, "token_unknown"), (gone, "token_revoked"),
                                (TOKEN, "token_unknown")):                   # (a station's token is no worker's)
                code, out = call(hub.url + path, method, body, token=tok)
                assert code == 401 and out["reason"] == reason, (path, tok, out)
                assert (tok or "@@") not in json.dumps(out)
        assert call(hub.url + "/api/station/jobs?wait=0", token=good)[0] == 401     # a worker's token: no station route
        code, out = call(hub.url + "/api/station/stt/hello", "POST", {"state": "online"}, token=good)
        assert code == 200 and out["worker"] == "gpu-1" and out["pool_on"] is True and "catchup" in out["classes"]
        assert call(hub.url + "/api/station/stt/jobs/0123456789abcdef/complete", "POST", {"text": "x"}, token=good)[0] == 409
        assert call(hub.url + "/api/station/stt/claim", "POST", {"wait": 0}, token=good) == (200, {"job": None})
        assert not any(good in m or gone in m for m in logs) and any("token_revoked" in m for m in logs)
        # revoked: refused from its next request, and what it held falls back
        reg.revoke("gpu-1")
        assert call(hub.url + "/api/station/stt/state", "POST", {}, token=good)[1]["reason"] == "token_revoked"
    finally:
        hub.close()


def test_the_pool_switches_turn_classes_off_and_the_pool_off(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        reg = hub.app.hub.workers
        assert reg.enabled(URGENT) is False                  # no worker: off whatever the switches say
        reg.add("gpu-1")
        assert reg.enabled(URGENT) and reg.enabled(CATCHUP)
        notes = []
        reg.act({"action": "pool", "classes": {"urgent": False}}, "admin", lambda k, o, n: notes.append((k, o, n)))
        assert not reg.enabled(URGENT) and reg.enabled(LIVE)
        reg.act({"action": "pool", "on": False}, "admin", lambda k, o, n: notes.append((k, o, n)))
        assert not reg.enabled(LIVE) and notes == [("stt workers/pool/urgent", "on", "off"), ("stt workers/pool", "on", "off")]
        with pytest.raises(ValueError):
            reg.act({"action": "pool", "classes": {"nope": True}})
    finally:
        hub.close()


def test_admins_add_rotate_and_revoke_workers_audited_without_a_token(tmp_path):
    from sizeup.cad.api import ADMIN_READS, CadApi, needs
    from sizeup.cloud.hubstore import MonitorDb
    from tests.test_station_identity import CASEY, JORDAN, Identity, make_cad, people
    cad = make_cad(tmp_path)
    people(cad)
    reg = WorkerRegistry(MonitorDb(tmp_path / "hub.sqlite"), log=lambda s: None)
    cad.stt_workers = reg
    api = CadApi(cad)
    assert "/api/cad/stt-workers" in ADMIN_READS and needs("/api/cad/stt-workers") == "admin"
    for who in (None, Identity(email=JORDAN)):
        assert api.get("/api/cad/stt-workers", "", who)[0] == 403
        assert api.post("/api/cad/stt-workers", json.dumps({"action": "add", "id": "gpu-x"}).encode(), who)[0] == 403
    me = Identity(email=CASEY)
    code, raw, _ = api.post("/api/cad/stt-workers", json.dumps({"action": "add", "id": "gpu-1", "name": "Box"}).encode(), me)
    out = json.loads(raw)
    assert code == 200 and out["token"].startswith("sw1.gpu-1.")
    tokens = [out["token"]]
    out = json.loads(api.post("/api/cad/stt-workers", json.dumps({"action": "rotate", "id": "gpu-1"}).encode(), me)[1])
    tokens.append(out["token"])
    assert reg.resolve(tokens[0]) == "gpu-1" and reg.resolve(tokens[1]) == "gpu-1"     # (the old one: its grace)
    revoked = []
    cad.stt_pool_forget = revoked.append
    assert api.post("/api/cad/stt-workers", json.dumps({"action": "revoke", "id": "gpu-1"}).encode(), me)[0] == 200
    assert revoked == ["gpu-1"]
    code, raw, _ = api.get("/api/cad/stt-workers", "", me)
    panel = json.loads(raw)
    assert panel["workers"][0]["revoked"] and "hash" not in raw.decode() and not any(t in raw.decode() for t in tokens)
    audit = json.dumps(cad.settings.audit_log("system"))
    assert "stt workers/gpu-1" in audit and not any(t in audit for t in tokens)


def test_the_table_is_made_on_first_use_never_at_the_start(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        hub_db = sqlite3.connect(Path(hub.app.cfg.cad_db).parent / "hub.sqlite")
        cad_db = sqlite3.connect(hub.app.cfg.cad_db)
        assert "stt_workers" not in {r[0] for r in hub_db.execute("SELECT name FROM sqlite_master")}
        assert "stt_runs" not in {r[0] for r in cad_db.execute("SELECT name FROM sqlite_master")}
        assert hub.app.health.model()["stt_pool"] is None     # nothing to show
    finally:
        hub.close()


# -- the catch-up: shed clips to the pool first -------------------------------------------------------------------------


def test_a_shed_clip_is_caught_up_on_a_worker_quietly_even_while_fire_work_pauses_whisper(tmp_path):
    from tests.test_stt_catchup import PD, pd_alert, pd_event
    hub = HubRun(hub_cfg(tmp_path))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token).start()
    try:
        app, now, tid = hub.app, time.time(), "9211-1794-call_9"
        pd_alert(app)
        send(hub, pd_event(tid, now))
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]] == [tid], 10)
        assert wait(lambda: app.sttpool.ready(CATCHUP)[0], 5)
        assert app._catchup_route(PD) == "pool"
        app.catchup.pause = lambda: "an event is open"          # fire work: the station's whisper waits, the pool doesn't
        heard = []
        app.cad.comms.on_text = heard.append
        app.cad.comms.on_context_text = heard.append
        assert app.catchup.tick() == 1
        res = lambda: app.cad.store.one("SELECT status, text, model, rerun_result FROM radio_traffic WHERE id = ?",  # noqa: E731
                                        (tid,)) or {}
        assert wait(lambda: res().get("rerun_result"), 10), res()
        assert res() == {"status": "done", "text": GPU_TEXT, "model": "large-v3-turbo-q5_0 (GPU gpu-1)",
                         "rerun_result": "changed; catch-up"}
        assert heard == []                                       # old words alert nothing
        (run,) = app.stt_runs.of(tid)
        assert run["role"] == "catchup" and run["class"] == "catchup" and run["engine"] == "gpu-worker"
        assert app.stt_latency.table() == []                    # a catch-up isn't live latency
        assert "paused: an event is open" in app.catchup.stats()["why"]
    finally:
        w.close()
        hub.close()


def test_a_shed_clip_the_pool_hands_back_burns_no_try_and_goes_the_old_way_next(tmp_path, quick):
    from tests.test_stt_catchup import PD, pd_alert, pd_event
    hub = HubRun(hub_cfg(tmp_path))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish").start()
    try:
        app, now, tid = hub.app, time.time(), "9211-1794-call_10"
        pd_alert(app)
        send(hub, pd_event(tid, now))
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]] == [tid], 10)
        assert wait(lambda: app.sttpool.ready(CATCHUP)[0], 5)
        app.catchup.pause = lambda: ""
        assert app.catchup.tick() == 1 and tid in app.catchup._sent
        assert wait(lambda: tid not in app.catchup._sent, 10)    # handed back: forgotten, no try burnt
        assert app.cad.comms.catchup_tries(tid) == 0 and tid not in app._rerun_note
        w.close()
        app.sttpool.forget("gpu-1")
        assert app._catchup_route(PD) == "whisper"               # no worker now: the station's whisper, as before
    finally:
        w.close()
        hub.close()


# -- every transcript kept: the station's whisper too ----------------------------------------------------------------


def test_the_stations_whisper_text_is_kept_as_a_run_with_its_latency(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        st._hub_aws = {"ok": False}                              # (AWS can't: FG1 runs the station's whisper)
        tid = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid and t["text"] == "whisper heard this"
                                for t in all_traffic(hub.get("/api/state"))), 20)
        assert wait(lambda: hub.app.stt_runs.of(tid), 5)
        (run,) = hub.app.stt_runs.of(tid)
        assert run["engine"] == "station-whisper" and run["model"] == "m.bin" and run["text"] == "whisper heard this"
        assert run["clip_end"] and run["done_at"] >= run["clip_end"] - 5
        assert run["wait_s"] is not None and run["run_s"] is not None and run["class"] == "urgent"   # the station's own
        assert run["keyup"] is None                               # (a whole transmission: not a cut piece)
        assert hub.app.stt_latency.table()[0]["engines"] == {"station-whisper": 1}
        # when its first text landed: kept through a later re-run (updated_at moves, text_at doesn't)
        first = hub.app.cad.store.one("SELECT text_at FROM radio_traffic WHERE id = ?", (tid,))["text_at"]
        assert first and abs(first - run["done_at"]) < 5
        hub.app._retext(tid, "a re-run's words", "done", 0.9, "m.bin")
        assert hub.app.cad.store.one("SELECT text_at FROM radio_traffic WHERE id = ?", (tid,))["text_at"] == first
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_runs_are_pruned_after_their_keep_and_a_failure_to_write_never_stops_a_transcript(tmp_path):
    from tests.test_station_identity import make_cad
    cad = make_cad(tmp_path)
    clock = Clock(2_000_000_000.0)
    logs = []
    runs = sttruns.Runs(cad.store, clock=clock, log=logs.append)
    assert runs.prune() == 0                                     # no table yet: nothing, no error
    for i, days in enumerate((150, 100, 1)):
        assert runs.add(f"c{i}", "aws", text="x", done_at=clock.t - days * 86400)
    assert runs.prune() == 2 and [r["clip_id"] for r in runs.of("c2")] == ["c2"] and runs.of("c0") == []

    class Broken:
        def tx(self):
            raise sqlite3.OperationalError("disk I/O error")
    bad = sttruns.Runs(Broken(), clock=clock, log=logs.append)
    assert bad.add("c9", "aws") is False and bad.add("c9", "aws") is False and len(logs) == 1   # said once a while
    assert sttruns.prompt_version("") == "" and len(sttruns.prompt_version("Engine 99")) == 10
    # a cut transmission's piece (the radio's key-up split): kept with its run, for grouping later
    assert runs.add("FG1-2", "gpu-worker", keyup={"parent": "FG1-1", "prev": "FG1-1", "split": "gap", "x": 1})
    assert runs.of("FG1-2")[0]["keyup"] == {"parent": "FG1-1", "prev": "FG1-1", "split": "gap"}


# -- the baseline's gaps: the queue each minute, wait and run per transcript, AWS denied -------------------------------


def test_the_station_sends_its_whisper_queue_each_minute_and_the_hub_keeps_it():
    from types import SimpleNamespace
    from sizeup.cloud.metrics import Collector, Sampler
    from sizeup.cloud.metricstore import KINDS, METRICS
    stats = {"queued": 7, "urgent": 1, "high": 1, "live": 4, "rerun": 0, "context": 1, "low": 2, "catchup": 0,
             "oldest_s": 312.4, "oldest_low_s": 640.0, "shed": 10, "dropped": 0, "skipped": 1}
    t0 = 60 * 29_000_000
    stt = SimpleNamespace(stats=lambda: dict(stats), timed=[(t0 + 5, LIVE, 4.0, 1.5), (t0 + 30, LOW, 300.0, 2.5),
                                                              (t0 + 59, URGENT, 2.0, 1.0), (t0 + 61, LIVE, 1.0, 1.0)])
    c = Collector()
    s = Sampler(SimpleNamespace(stt=stt), c, log=lambda m: None)
    s._stt(t0 // 60)
    stats["shed"] = 14
    s._stt(t0 // 60)
    a, b = [r for r in c.batch()[1] if r["k"] == "stt"]
    assert a["k"] == "stt" and a["s"] == "whisper" and a["queued"] == 7 and a["live"] == 2 and a["low"] == 2
    assert a["done"] == 3 and a["wait50"] == 4.0 and a["wait95"] == 300.0 and a["run50"] == 1.5 and a["oldest"] == 312.4
    assert a["shed"] == 0 and b["shed"] == 4                   # shed in the minute (the count's change)
    assert "stt" in KINDS and METRICS["stt_shed"][0] == "stt"


def test_the_hub_logs_when_a_channel_is_denied_aws_and_when_it_may_again(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    logs = []
    try:
        hub.app.hub.log = logs.append
        app = hub.app
        app._aws_word = None
        app._aws_word_log(True, "", [], [], "")
        app._aws_word_log(False, "the monthly limit is reached", ["tg:307"], [], "")
        app._aws_word_log(False, "the monthly limit is reached", ["tg:307"], [], "")    # (no change: nothing said)
        app._aws_word_log(True, "", [], ["p25:mrrs:9211"], "its own budget is used up")
        assert logs == ["aws transcribe: our channels denied AWS (the monthly limit is reached): the station's whisper; "
                        "reserve kept: tg:307",
                        "aws transcribe: our channels may use AWS again",
                        "aws transcribe: denied AWS on its own budget: p25:mrrs:9211 (its own budget is used up)"]
    finally:
        hub.close()
