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
from sizeup.stt import CATCHUP, CONTEXT, HIGH, LIVE, LOW, RERUN, URGENT
from sizeup.sttpool import BUDGETS, Job, Pool
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import voice, wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws, hub_post  # noqa: F401 (the fixture: test credentials only)
from tests.test_cloud import TOKEN, HubRun, all_traffic, call, fm_segment, hub_cfg, station, station_cfg

GPU_TEXT = "Engine 99 on location per the GPU"
LONG_POLL_S = 20.0   # an idle worker's claim as the real one makes it: a long poll, so it is in a claim the whole time.
                     # (A 1 s poll leaves a gap each second, and a pre-alert clip that lands in one finds no worker idle
                     # now and goes to AWS: on a loaded CI runner the event's routing came ~1 s after the first claim.)


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


def online(p, wid="gpu-1"):
    """A worker online and in a claim's long poll now (idle: what a pre-alert clip needs)."""
    w = p.seen(wid, "online")
    w.polling = 1
    return w


# -- the pool on its own ---------------------------------------------------------------------------------------------


def test_with_no_worker_or_the_pool_off_nothing_is_taken():
    j = Jobs()
    p = pool()
    assert p.ready(URGENT) == (False, "no worker ready") and not p.submit(j.job("a"))
    online(p)
    off = pool(on=False)
    off.seen("gpu-1", "online")
    assert off.ready(URGENT) == (False, "off") and not off.submit(j.job("a"))
    assert p.submit(j.job("a")) and not p.submit(j.job("a"))           # one job per clip id
    assert p.submitted == 1


def test_the_classes_keep_their_strict_order_oldest_first_in_a_class(monkeypatch):
    monkeypatch.setattr(sttpool, "SERVICE_DEFAULT_S", 0.1)    # (a fast worker: the queue never looks too long)
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    for cid, cls in (("catchup", CATCHUP), ("low", LOW), ("live-1", LIVE), ("rerun", RERUN), ("live-2", LIVE),
                     ("page", URGENT)):                   # (one pre-alert clip at a time: a second goes to AWS)
        clock.t += 0.01
        assert p.submit(j.job(cid, cls, seconds=0.0)), cid
    assert not p.submit(j.job("air", HIGH)) and p.ready(HIGH) == (False, "a pre-alert clip waits already")
    got = []
    while (job := p.claim("gpu-1", 0)) is not None:
        got.append(job.id)
        assert p.complete("gpu-1", job.lease, {"text": "x"})
    assert got == ["page", "live-1", "live-2", "rerun", "low", "catchup"]
    assert set(j.done) == set(got) and j.fell == {}


def test_a_job_nobody_claims_in_its_wait_falls_back_and_the_classes_wait_differently():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    p.submit(j.job("page", URGENT, seconds=0.0))
    p.submit(j.job("shed", CATCHUP, seconds=0.0))
    clock.t += BUDGETS[URGENT][0] - 0.1
    p.seen("gpu-1")
    assert p.tick() == 0
    clock.t += 0.2
    p.seen("gpu-1")
    assert p.tick() == 1 and p.drain() == 1
    assert "no pool worker took it in 2.5 s" in j.fell["page"] and "shed" not in j.fell
    clock.t += BUDGETS[CATCHUP][0]
    p.seen("gpu-1")
    assert p.tick() == 1 and p.drain() == 1 and "shed" in j.fell
    assert p.stats()["fallbacks"]["unclaimed"] == 2


def test_a_lease_that_runs_out_falls_back_and_the_late_answer_is_refused():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    p.submit(j.job("a", URGENT, seconds=4.0))
    job = p.claim("gpu-1", 0)
    lease_s = BUDGETS[URGENT][1]                              # (a pre-alert clip's: never longer for a longer clip)
    assert job.public()["lease_s"] == lease_s and job.public()["audio"].endswith(f"/{job.lease}/audio")
    assert job.public()["fast"] is True                       # a short lease: one greedy pass, no temperature fallback
    p.submit(j.job("b", LOW))
    assert p.claim("gpu-1", 0).public()["fast"] is False
    assert p.job_for("gpu-1", job.lease) is job and p.job_for("gpu-2", job.lease) is None    # its holder's only
    clock.t += lease_s
    assert p.tick() == 1 and p.drain() == 1 and "lease ran out" in j.fell["a"]
    assert p.complete("gpu-1", job.lease, {"text": "late"}) is False and p.refused_late == 1 and "a" not in j.done
    assert p.job_for("gpu-1", job.lease) is None and p.extend("gpu-1", job.lease) is None


def test_extend_renews_the_lease_up_to_its_cap_never_past_it():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
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
    online(p)
    p.submit(j.job("a", LIVE))
    p.seen("gpu-1", "gaming")
    assert p.ready(LIVE) == (False, "no worker ready")
    assert p.claim("gpu-1", 0, state="gaming") is None
    assert p.tick() == 1 and p.drain() == 1 and j.fell["a"] == "no pool worker ready"   # at once, not after its wait
    assert p.stats()["workers"][0]["state"] == "gaming"
    p.seen("gpu-1", "paused")
    assert not p.submit(j.job("b")) and p.stats()["workers"][0]["state"] == "paused"
    online(p)
    assert p.submit(j.job("b"))


def test_a_worker_that_disappears_mid_job_loses_it_and_goes_offline():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    p.submit(j.job("a", LIVE, seconds=0.0))
    job = p.claim("gpu-1", 0)
    clock.t += BUDGETS[LIVE][1]
    p.tick()
    p.drain()
    assert "a" in j.fell
    p.workers["gpu-1"].polling = 0                            # (its long poll gone with it)
    clock.t += sttpool.OFFLINE_S
    assert p.stats()["workers"][0]["state"] == "offline" and not p.ready(LIVE)[0]
    assert job.lease not in p._leases and not p.has("a")


def test_handing_back_falls_back_now_and_repeated_failures_cool_the_worker_down():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
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
    online(p)
    p.submit(Job("a", LIVE, "FG1", "traffic", 1.0, audio=lambda: b"", on_done=boom,
                 on_fallback=lambda job, why: fell.append(why)))
    job = p.claim("gpu-1", 0)
    assert p.complete("gpu-1", job.lease, {"text": "x"}) and fell and "couldn't be applied" in fell[0]


def test_a_busy_pool_turns_a_clip_away_rather_than_make_it_wait_past_its_budget():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    for i in range(3):                                        # a backlog of context clips
        assert p.submit(j.job(f"low{i}", LOW, seconds=0.0))
    ok, why = p.ready(LOW)
    assert ok                                                 # (LOW waits 30 s: 3 x 2 s is fine)
    assert p.ready(URGENT)[0]                                 # idle now: a page may go to it (ahead of them all)
    assert p.claim("gpu-1", 0) is not None                    # busy on a context clip now ...
    assert p.ready(URGENT) == (False, "no worker idle now")     # ... so a page goes to AWS, never behind it
    p.claim("gpu-1", 0)
    assert p.claim("gpu-1", 0) is None                        # two jobs at once at most
    for i in range(6):
        p.submit(j.job(f"more{i}", LIVE, seconds=0.0))      # (the first two fit; then the work ahead is too long)
    ok, why = p.ready(LIVE)
    assert not ok and why.startswith("busy")


def test_a_pre_alert_clip_has_an_absolute_deadline_and_a_lost_or_gaming_worker_gives_it_back_at_once():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    assert p.submit(j.job("page", URGENT, seconds=40.0))     # a long page: its deadline is the same
    job = p.claim("gpu-1", 0)
    wait_s, lease_s, cap_s = BUDGETS[URGENT]
    assert job.expires - job.claimed_at == lease_s and job.cap_at - job.claimed_at == cap_s
    clock.t += lease_s                                        # a dead worker holds it 4 s, not 16 s
    assert p.tick() == 1 and p.drain() == 1 and "page" in j.fell
    online(p)
    assert p.ready(URGENT)[1].startswith("slow on pre-alert")  # a lost one counts as its whole cap: no more for now
    clock.t += sttpool.PROBE_S                                # ... until its probe is due
    assert p.submit(j.job("long", LOW, seconds=0.0))
    long = p.claim("gpu-1", 0)
    clock.t += sttpool.LOST_S                                 # its worker unheard (no extend, no heartbeat) ...
    assert long.expires > clock.t and p.tick() == 1 and p.drain() == 1 and "no word" in j.fell["long"]   # ... back now
    online(p)
    assert p.submit(j.job("air", HIGH, seconds=0.0))
    air = p.claim("gpu-1", 0)
    assert p.submit(j.job("low", LOW, seconds=0.0))
    low = p.claim("gpu-1", 0)
    p.seen("gpu-1", "gaming")                                 # gaming: its pre-alert job back now, the rest runs on
    p.drain()
    assert "went gaming" in j.fell["air"] and "low" not in j.fell and low.lease in p._leases
    assert p.complete("gpu-1", air.lease, {"text": "late"}) is False


def test_pre_alerts_go_only_to_a_quick_idle_worker(monkeypatch):
    clock, j = Clock(), Jobs()
    p = pool(clock)
    w = online(p)
    assert p.submit(j.job("p0", URGENT, seconds=0.0))
    job = p.claim("gpu-1", 0)
    clock.t += 3.5                                            # slow on pre-alert clips lately (AWS would be as quick)
    assert p.complete("gpu-1", job.lease, {"text": "x"})
    ok, why = p.ready(URGENT)
    assert not ok and why.startswith("slow on pre-alert clips")
    assert p.ready(LIVE)[0]                                   # (other classes go on as before)
    w.polling = 0                                              # not in a claim now: not idle
    assert p.ready(LIVE)[0] and p.ready(URGENT)[0] is False


def test_a_stopped_pool_takes_nothing_and_a_revoked_worker_never_comes_back():
    j = Jobs()
    allowed = {"gpu-1": True}
    p = Pool(enabled=lambda cls: True, clock=Clock(), log=lambda s: None, allowed=lambda wid: allowed.get(wid, False))
    online(p)
    p.forget("gpu-1")
    allowed["gpu-1"] = False
    assert p.seen("gpu-1", "online") is None and p.claim("gpu-1", 0) is None and "gpu-1" not in p.workers
    allowed["gpu-1"] = True                                   # a new token (Rotate): back
    online(p)
    p.stop()
    assert not p.submit(j.job("a")) and p.ready(LIVE) == (False, "stopped")


def test_a_revoked_worker_hands_back_what_it_holds():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
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
    online(p)
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
        self.extra: dict = {}                     # more of each result (the context form it decoded with, ...)
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
                "timing": {"fetch_ms": 5, "infer_ms": 180}, **self.extra}))

    def close(self):
        self.stop.set()
        self.t.join(5)

    def stop_after_hub(self, hub):
        """A long-polling worker's end: the hub stops first (its pool's stop ends the claim at once), then the worker,
        never left in a claim for its long poll."""
        self.stop.set()
        hub.close()
        self.close()


def pending_event(tid, seq=1, source="FG1", seconds=3.0):
    """The station's transmission on an AWS channel: no text, its clip (8 kHz), Amazon Transcribe on the hub."""
    from sizeup.cad.dispatcher import wav_bytes
    now = time.time()
    return {"seq": seq, "at": now, "type": "traffic",
            "data": {"id": tid, "source": source, "start": now - seconds, "end": now, "text": "", "status": "remote",
                     "transcript_pending": "aws", "stt": "aws", "model": "aws-transcribe"},
            "audio": base64.b64encode(wav_bytes((voice(seconds)[::2] * 32767).astype(np.int16), 8000)).decode()}


def auto(hub, *keys):
    """These channels on Auto · priority (#875): a GPU worker first. Since #896 a channel's engine alone sends its clips
    to the pool (FG1, South 2: pinned AWS by default)."""
    for k in keys:
        assert hub_post(hub, "channels", {"action": "set", "key": k, "engine": "auto"})[0] == 200


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
        hub.app.sttpool.ready = lambda cls, now=None, **k: asked.append(cls) or real(cls, now, **k)
        send(hub, pending_event("FG1-1"))
        assert wait(lambda: row(hub, "FG1-1").get("text") == AWS_TEXT + ".", 20)
        assert row(hub, "FG1-1")["model"] == "aws-transcribe"
        assert hub.app.sttpool.submitted == 0 and not asked and hub.app.hub.workers.any() is False
        db = sqlite3.connect(Path(hub.app.cfg.cad_db).parent / "hub.sqlite")
        assert "stt_workers" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}   # nothing made
        assert wait(lambda: hub.app.stt_runs.of("FG1-1"), 5)   # (written just after the text)
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
    w = FakeWorker(hub.url, token, wait_s=LONG_POLL_S).start()   # (in a claim: a pre-alert clip needs one idle now)
    try:
        auto(hub, "FG1")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
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
        assert wait(lambda: w.answers, 5) and w.answers[0][0] == 200     # (the text lands before the worker hears back)
        assert hub.app.aws.health()["done"] == 0             # AWS never asked
        assert wait(lambda: hub.app.stt_runs.of("FG1-2"), 5)
        (run,) = hub.app.stt_runs.of("FG1-2")
        assert run["engine"] == "gpu-worker" and run["worker"] == "gpu-1" and run["model"] == "ggml-large-v3-turbo-q5_0.bin"
        assert run["text"] == GPU_TEXT and run["confidence"] == 0.8 and run["class"] == "urgent"
        assert [x[0] for x in run["words"]] == GPU_TEXT.split()
        assert run["queued_at"] <= run["engine_start"] <= run["done_at"] and run["clip_end"] <= run["done_at"]
        assert run["input"] == "listening-8k"                 # (no STT input from the station: the listening clip, #678)
        lat = hub.app.stt_latency.table()
        assert lat[0]["channel"] == "FG1" and lat[0]["target"] and lat[0]["engines"] == {"gpu-worker": 1}
        health = hub.app.health.model()["stt_pool"]
        assert health["done_h"] == 1 and health["workers"][0]["id"] == "gpu-1" and health["latency"]
        # the worker's text is the raw transcript: a later engine's never overwrites it (a new run is appended)
        hub.app._aws_done(__import__("sizeup.app", fromlist=["AwsJob"]).AwsJob("traffic", "FG1-2", "FG1", rerun=True),
                          "another engine's words", 0.7)
        runs = hub.app.stt_runs.of("FG1-2")
        assert [x["text"] for x in runs] == [GPU_TEXT, "another engine's words"] and runs[1]["role"] == "rerun"
        assert runs[1]["input"] == "listening-8k"            # (AWS hears the listening clip)
    finally:
        w.stop_after_hub(hub)
        fake.close()


def test_a_jobs_audio_waits_for_the_stt_input_only_its_class_budget_counted_from_when_it_was_queued():
    """#678: a pre-alert clip's audio request waits at most SRC_WAIT_S (0.75 s) from when the job was queued, a LOW one
    longer, a catch-up or re-run clip never (old: its input is here or never comes); half the lease left at most."""
    clock = Clock()
    jobs = Jobs()
    for cls, budget in ((URGENT, 0.75), (HIGH, 0.75), (CONTEXT, 0.75), (LIVE, 1.5), (LOW, 3.0), (CATCHUP, 0.0),
                        (RERUN, 0.0)):
        j = jobs.job(f"FG1-{cls}", cls)
        j.queued_at, j.expires = clock.t, clock.t + 20.0
        assert sttpool.src_wait_s(j, clock.t) == budget, cls
        assert sttpool.src_wait_s(j, clock.t + 0.5) == max(0.0, budget - 0.5)     # the claim's own delay counts
        assert sttpool.src_wait_s(j, clock.t + 5.0) == 0.0
    j = jobs.job("FG1-lease", LOW)
    j.queued_at, j.expires = clock.t, clock.t + 1.0
    assert sttpool.src_wait_s(j, clock.t) == 0.5                                   # half of what is left of the lease


def _stt_item(n, tid, seconds=3.0):
    from sizeup.cloud import flac, sttinput
    pcm = sttinput.stt_pcm(voice(seconds), True)
    meta = {"v": 1, "format": "flac", "input": "stt-src", "kind": "traffic", "rate": 16000, "samples": len(pcm)}
    return {"n": n, "id": tid, "kind": "traffic", "source": "FG1", "talkgroup": None, "system": "",
            "end": time.time(), "stt": sttinput.to_wire(flac.encode(pcm, 16000), meta)}, len(pcm)


def _waits(hub):
    """The wait each pool job's audio request gave HubApp.read_stt_audio (the station's STT input), in order."""
    waits, read = [], hub.app.read_stt_audio

    def spy(cid, *a, wait_s=0.0, **k):
        waits.append(wait_s)
        return read(cid, *a, wait_s=wait_s, **k)
    hub.app.read_stt_audio = spy
    return waits


def test_a_pre_alert_job_gets_the_stt_input_that_comes_in_time_and_never_waits_2_s_for_one_that_doesnt(tmp_path):
    """#678: the worker's fetch was ~2.0 s per pre-alert job (the hub's flat 2 s wait for the station's STT input, which
    came ~2 s after its event). Now, from a station whose lane is woken by the events' ack: the input that comes within
    the class's budget is what the worker hears (16 kHz, stamped stt-src); one that doesn't costs the budget (0.75 s at
    most) and the listening clip, stamped listening-8k."""
    import io
    import wave
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    hub.app.hub.status = {"stt_input": {"wake_on_ack": True}}       # (the station's last status, #678)
    waits = _waits(hub)
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    w = FakeWorker(hub.url, token, wait_s=LONG_POLL_S).start()
    try:
        auto(hub, "FG1")                                   # (a GPU worker first: Auto, #896)
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        item, samples = _stt_item(1, "FG1-30")
        threading.Timer(0.3, lambda: call(hub.url + "/api/station/stt", "POST", {"v": 1, "items": [item]})).start()
        send(hub, pending_event("FG1-30"))
        assert wait(lambda: row(hub, "FG1-30").get("text") == GPU_TEXT, 30), row(hub, "FG1-30")
        with wave.open(io.BytesIO(w.audio[0][1])) as f:          # the station's own input, padded 0.5 s each side
            assert f.getframerate() == 16000 and f.getnframes() == samples + 16000
        assert wait(lambda: hub.app.stt_runs.of("FG1-30"), 5)
        assert hub.app.stt_runs.of("FG1-30")[0]["input"] == "stt-src"
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)     # (the worker in its next claim, as before)
        send(hub, pending_event("FG1-31", seq=2))                     # its STT input never comes
        assert wait(lambda: row(hub, "FG1-31").get("text") == GPU_TEXT, 30), row(hub, "FG1-31")
        assert wait(lambda: hub.app.stt_runs.of("FG1-31"), 5)
        assert hub.app.stt_runs.of("FG1-31")[0]["input"] == "listening-8k"
        assert len(waits) == 2 and all(0.0 <= x <= 0.75 for x in waits), waits    # the budget, never the old 2.0 s
    finally:
        w.stop_after_hub(hub)
        fake.close()


def test_an_stt_input_that_comes_before_its_event_is_what_the_worker_hears(tmp_path):
    """The station never holds an input for its own event (a transmission's traffic event goes only with its text): one
    that reaches the hub first is kept as <id>.src and is what the pool's worker gets once the event comes (#678)."""
    import io
    import wave
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    hub.app.hub.status = {"stt_input": {"wake_on_ack": True}}
    waits = _waits(hub)
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    w = FakeWorker(hub.url, token, wait_s=LONG_POLL_S).start()
    try:
        auto(hub, "FG1")                                   # (a GPU worker first: Auto, #896)
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        item, samples = _stt_item(1, "FG1-50")
        assert call(hub.url + "/api/station/stt", "POST", {"v": 1, "items": [item]})[0] == 200    # before its event
        send(hub, pending_event("FG1-50"))
        assert wait(lambda: row(hub, "FG1-50").get("text") == GPU_TEXT, 30), row(hub, "FG1-50")
        with wave.open(io.BytesIO(w.audio[0][1])) as f:
            assert f.getframerate() == 16000 and f.getnframes() == samples + 16000
        assert wait(lambda: hub.app.stt_runs.of("FG1-50"), 5)
        assert hub.app.stt_runs.of("FG1-50")[0]["input"] == "stt-src" and len(waits) == 1
    finally:
        w.stop_after_hub(hub)
        fake.close()


@pytest.mark.parametrize("flag", [None, False, "yes"], ids=["no-status", "false", "not-a-bool"])
def test_a_station_whose_lane_is_not_woken_on_ack_keeps_the_old_2_s_wait(tmp_path, flag):
    """The hub deploys before the stations (#678): a station that doesn't say stt_input.wake_on_ack sends each STT
    input ~2 s after its event, so its pool jobs keep the old flat wait (else every pre-alert job would get the 8 kHz
    clip until the station updates). The input then still comes in time and is what the worker hears."""
    import io
    import wave
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    if flag is not None:
        hub.app.hub.status = {"stt_input": {"wake_on_ack": flag}}
    assert hub.app._station_stt_fast() is False
    waits = _waits(hub)
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    w = FakeWorker(hub.url, token, wait_s=LONG_POLL_S).start()
    try:
        auto(hub, "FG1")                                   # (a GPU worker first: Auto, #896)
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        item, samples = _stt_item(1, "FG1-40")
        threading.Timer(0.3, lambda: call(hub.url + "/api/station/stt", "POST", {"v": 1, "items": [item]})).start()
        send(hub, pending_event("FG1-40"))
        assert wait(lambda: row(hub, "FG1-40").get("text") == GPU_TEXT, 30), row(hub, "FG1-40")
        with wave.open(io.BytesIO(w.audio[0][1])) as f:
            assert f.getframerate() == 16000 and f.getnframes() == samples + 16000
        assert wait(lambda: hub.app.stt_runs.of("FG1-40"), 5)
        assert hub.app.stt_runs.of("FG1-40")[0]["input"] == "stt-src"
        assert waits and waits[0] > 0.75, waits                  # the old wait (2 s, or half the lease left), not 0.75
    finally:
        w.stop_after_hub(hub)
        fake.close()


def test_a_worker_that_vanishes_mid_job_costs_seconds_and_aws_gives_the_text(tmp_path, quick):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish", wait_s=20.0).start()
    try:
        auto(hub, "FG1")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
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
        auto(hub, "FG1")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        send(hub, pending_event("FG1-5"))
        assert wait(lambda: row(hub, "FG1-5").get("text") == AWS_TEXT + ".", 20)
        assert wait(lambda: w.answers, 5)                        # (its release's answer: just after the text)
        fb = hub.app.sttpool.stats()["fallbacks"]
        # (handed back in time: "released"; under a loaded test box its 1 s lease may run out first: "lease", the same
        # way on to AWS)
        assert (w.answers[0][0], fb["released"]) == (200, 1) or (w.answers[0][0], fb["lease"]) == (409, 1)
    finally:
        w.close()
        hub.close()
        fake.close()


def test_with_aws_out_too_a_lost_job_goes_to_the_stations_whisper_never_dropped(tmp_path, quick):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish", wait_s=20.0).start()
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        assert wait(lambda: st._hub_aws["ok"], 10)              # the station forwards FG1's clips to the hub
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        hub.app.aws.available = lambda key=None: (False, "test: AWS can't take audio now")
        tid = fm_segment(st)
        assert wait(lambda: w.jobs, 30)                          # a worker took it ... and vanished with it
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
                                ("not-a-token", "token_unknown")):
                code, out = call(hub.url + path, method, body, token=tok)
                assert code == 401 and out["reason"] == reason, (path, tok, out)
                assert (tok or "@@") not in json.dumps(out)
        assert call(hub.url + "/api/station/jobs?wait=0", token=good)[0] == 401     # a worker's token: no station route
        # a station's token is its whisper's, as a station worker (#639 phase 2), never a GPU worker's
        own = hub.app.hub.stations.add("st-x")
        assert call(hub.url + "/api/station/stt/hello", "POST", {"state": "online"}, token=own)[1]["reason"] == "stations_off"
        reg.act({"action": "pool", "stations": {"st-x": True}})                  # an admin puts its whisper in
        code, out = call(hub.url + "/api/station/stt/hello", "POST", {"state": "online"}, token=own)
        assert code == 200 and out["worker"] == "station:st-x"
        assert hub.app.sttpool.workers[out["worker"]].kind == "station"
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


def test_the_pool_has_its_on_switch_and_no_class_switches_any_more(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        reg = hub.app.hub.workers
        assert reg.enabled(URGENT) is False                  # no worker, no station's whisper put in (#639 phase 2): off
        reg.add("gpu-1")
        assert reg.enabled(URGENT) and reg.enabled(CATCHUP)
        notes = []
        with pytest.raises(ValueError, match="classes: gone"):   # (#896: a channel's engine decides where its clips go)
            reg.act({"action": "pool", "classes": {"urgent": False}}, "admin", lambda k, o, n: notes.append((k, o, n)))
        assert reg.enabled(URGENT) and notes == []
        reg.act({"action": "pool", "on": False}, "admin", lambda k, o, n: notes.append((k, o, n)))
        assert not reg.enabled(LIVE) and notes == [("stt workers/pool", "on", "off")]
        assert reg.pool_settings() == {"on": False, "stations": {}}
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


def test_a_shed_clip_is_caught_up_on_a_worker_quietly_once_fire_work_is_over(tmp_path):
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
        app.catchup.pause = lambda: "an event is open"          # fire work: nothing goes, to the pool either
        assert app.catchup.tick() == 0 and app.sttpool.submitted == 0
        assert "paused: an event is open" in app.catchup.stats()["why"]
        app.catchup.pause = lambda: ""
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
        assert wait(lambda: app.stt_runs.of(tid), 5)
        (run,) = app.stt_runs.of(tid)
        assert run["role"] == "catchup" and run["class"] == "catchup" and run["engine"] == "gpu-worker"
        assert app.stt_latency.table() == []                    # a catch-up isn't live latency
    finally:
        w.close()
        hub.close()


def test_a_shed_clip_a_worker_loses_is_a_failed_try_and_one_nobody_takes_burns_none(tmp_path, quick):
    from tests.test_stt_catchup import PD, pd_alert, pd_event
    hub = HubRun(hub_cfg(tmp_path))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish", wait_s=20.0).start()
    try:
        app, now, tid = hub.app, time.time(), "9211-1794-call_10"
        pd_alert(app)
        send(hub, pd_event(tid, now))
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]] == [tid], 10)
        assert wait(lambda: app.sttpool.ready(CATCHUP)[0], 5)
        app.catchup.pause = lambda: ""
        assert app.catchup.tick() == 1 and tid in app.catchup._sent
        assert wait(lambda: app.cad.comms.catchup_tries(tid) == 1, 10)    # its lease lost: a failed try (a poison clip ends)
        w.close()
        # one nobody takes (no worker ready by then): back to the catch-up, no try burnt
        tid2 = "9211-1794-call_11"
        send(hub, pd_event(tid2, now, seq=2))
        assert wait(lambda: tid2 in [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]], 10)
        app.sttpool.seen("gpu-1", "online")
        app.sttpool.workers["gpu-1"].polling = 1
        assert app._catchup_pool({"id": tid2, "key": PD, "source": "MRRS PD", "start": now - 3, "seconds": 3.0})[0]
        app.catchup._sent[tid2] = (1, time.time())
        app.sttpool.seen("gpu-1", "gaming")
        app.sttpool.workers["gpu-1"].polling = 0
        assert wait(lambda: tid2 not in app.catchup._sent, 10)  # nobody could take it: forgotten, routed again
        assert app.cad.comms.catchup_tries(tid2) == 0 and tid2 not in app._rerun_note
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


def test_runs_are_pruned_after_their_keep_and_with_their_transcript_and_a_failure_never_stops_one(tmp_path):
    clock = Clock(2_000_000_000.0)
    logs = []
    logged = {"c2", "c3"}                                        # the radio log's rows still there
    runs = sttruns.Runs(tmp_path / "stt.sqlite", clock=clock, log=logs.append, exists=lambda ids: set(ids) & logged)
    assert runs.prune() == 0 and not (tmp_path / "stt.sqlite").exists()   # no file yet: nothing made, no error
    for i, days in enumerate((150, 100, 1, 1)):
        assert runs.add(f"c{i}", "aws", text="x", done_at=clock.t - days * 86400)
    assert runs.prune() == 2 and runs.of("c0") == [] and [r["clip_id"] for r in runs.of("c2")] == ["c2"]
    logged.discard("c3")                                         # its transcript's retention pruned the row
    assert runs.prune() == 1 and runs.of("c3") == [] and runs.of("c2")
    for i in range(1200):                                        # more than a batch: pruned in one pass all the same
        runs.add(f"old{i}", "aws", done_at=clock.t - 100 * 86400)
    assert runs.prune() == 1200
    assert runs.add("c5", "aws", confidence=float("nan"), clip_end=float("inf"))
    assert runs.of("c5")[0]["confidence"] is None and runs.of("c5")[0]["clip_end"] is None

    standby = sttruns.Runs(tmp_path / "standby.sqlite", may_write=lambda: False, clock=clock)
    assert standby.add("c9", "aws") is False and not (tmp_path / "standby.sqlite").exists()
    bad = sttruns.Runs(tmp_path / "no-dir" / "x" / "\0bad", clock=clock, log=logs.append)
    assert bad.add("c9", "aws") is False and bad.add("c9", "aws") is False and len(logs) == 1   # said once a while
    assert sttruns.prompt_version("") == "" and len(sttruns.prompt_version("Engine 99")) == 10
    # a cut transmission's piece (the radio's key-up split): kept with its run, for grouping later
    assert runs.add("FG1-2", "gpu-worker", keyup={"parent": "FG1-1", "prev": "FG1-1", "split": "gap", "x": 1})
    assert runs.of("FG1-2")[0]["keyup"] == {"parent": "FG1-1", "prev": "FG1-1", "split": "gap"}
    # a P25 call split per transmission: its call and turn
    assert runs.add("9211-1794-call_4-t1", "gpu-worker", call_id="9211-1794-call_4", turn=1)
    got = runs.of("9211-1794-call_4-t1")[0]
    assert (got["call_id"], got["turn"]) == ("9211-1794-call_4", 1)


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


# -- the #651 review: a page end to end, a restart with a job out, numbers that aren't, the copies --------------------


def test_a_page_goes_through_a_worker_end_to_end_and_its_dispatch_is_read(tmp_path):
    from tests.test_transcribe_watchdog import _page_event
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, text="Port Chester engine 99 respond to a structure fire",
                   wait_s=LONG_POLL_S).start()
    try:
        auto(hub, "South 2")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        send(hub, _page_event("South_2-900"))
        page = lambda: next((p for p in hub.app.pages if p.id == "South_2-900"), None)   # noqa: E731
        assert wait(lambda: page() is not None and page().status == "done", 20)
        p = page()
        assert p.raw == "Port Chester engine 99 respond to a structure fire" and p.model.endswith("(GPU gpu-1)")
        assert w.jobs[0]["class"] == "urgent" and w.jobs[0]["fast"] is True and w.jobs[0]["kind"] == "page"
        assert hub.app.aws.health()["done"] == 0
        assert wait(lambda: hub.app.stt_runs.of("South_2-900"), 5)
        assert hub.app.stt_runs.of("South_2-900")[0]["engine"] == "gpu-worker"
    finally:
        w.stop_after_hub(hub)
        fake.close()


def test_a_hub_restart_with_a_job_out_sends_it_the_aws_way(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish", wait_s=20.0).start()
    try:
        auto(hub, "FG1")
        assert wait(lambda: hub.app.sttpool.ready_forced(URGENT)[0], 5)
        send(hub, pending_event("FG1-77"))
        assert wait(lambda: w.jobs, 10)                        # a worker has it ... and the hub goes down
        w.close()
        hub.close()
        assert row(hub, "FG1-77")["status"] == "pending"
        hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))     # back: _aws_recover
        assert wait(lambda: row(hub, "FG1-77").get("text") == AWS_TEXT + ".", 20)
        late = call(hub.url + f"/api/station/stt/jobs/{w.jobs[0]['lease']}/complete", "POST", {"text": "x"}, token=token)
        assert late[0] == 409                                   # the old lease means nothing to the new hub
    finally:
        w.close()
        hub.close()
        fake.close()


def test_numbers_that_arent_never_get_in_and_a_body_nested_too_deep_is_refused(tmp_path):
    res = clean_result({"text": "x", "segments": [{"start": float("nan"), "end": float("inf"), "text": "x",
                                                   "words": [{"word": "x", "probability": float("nan")},
                                                             {"word": "y", "probability": 1.7},
                                                             {"word": "z", "probability": -0.1},
                                                             {"word": "ok", "probability": 0.5}]}],
                        "timing": {"infer_ms": float("inf")}})
    assert [w[1] for w in res["words"]] == [None, None, None, 0.5] and res["segments"][0]["start"] is None
    assert res["timing"] == {}
    hub = HubRun(hub_cfg(tmp_path))
    try:
        token = hub.app.hub.workers.add("gpu-1")
        import urllib.request
        body = b'{"text": ' + b"[" * 50000 + b"]" * 50000 + b"}"
        req = urllib.request.Request(hub.url + "/api/station/stt/jobs/0123456789abcdef/complete", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 400
    finally:
        hub.close()


class FakeCopies:
    """The bucket's copies (sizeup/cloud/metricsbackup.py S3Copies' shape), in memory."""

    def __init__(self):
        self.slots, self.puts = {}, 0

    def list(self):
        return [{"slot": k, "at": at, "size": len(raw)} for k, (at, raw) in
                sorted(self.slots.items(), key=lambda kv: -kv[1][0])]

    def get(self, slot):
        return self.slots[slot][1]

    def put(self, slot, raw, level=6):
        self.puts += 1
        self.slots[slot] = (time.time() + self.puts, raw)
        return len(raw)


def test_runs_live_in_their_own_file_with_copies_a_new_task_merges(tmp_path):
    copies = FakeCopies()
    clock = Clock(2_000_000_000.0)
    old = sttruns.Runs(tmp_path / "old" / "stt.sqlite", backup=copies, clock=clock, log=lambda m: None)
    assert old.backup_turn() == "settled"                      # nothing there yet
    old.add("FG1-1", "aws", text="before the deploy", done_at=clock.t - 60)
    clock.t += sttruns.FIRST_UPLOAD_S
    assert old.backup_turn() == "uploaded" and copies.puts == 1     # (a little after settling: the first copy)
    clock.t -= sttruns.FIRST_UPLOAD_S
    new = sttruns.Runs(tmp_path / "new" / "stt.sqlite", backup=copies, clock=clock, log=lambda m: None)
    new.add("FG1-2", "gpu-worker", text="after the deploy", done_at=clock.t)   # written before it settled
    assert new.backup_turn() == "settled"
    assert [r["text"] for r in new.of("FG1-1")] == ["before the deploy"] and new.of("FG1-2")
    assert new.backup_turn() == ""                              # a copy every few hours, not every turn
    clock.t += sttruns.SNAPSHOT_S
    assert new.backup_turn() == "uploaded" and copies.puts == 2
    standby = sttruns.Runs(tmp_path / "standby" / "stt.sqlite", backup=copies, may_write=lambda: False, clock=clock)
    assert standby.backup_turn() == "" and not (tmp_path / "standby").exists()
    # the hub's own: its file next to hub.sqlite, never in cad.sqlite
    hub = HubRun(hub_cfg(tmp_path / "hub"))
    try:
        assert Path(hub.app.stt_runs.path).name == "stt.sqlite"
        assert Path(hub.app.stt_runs.path).parent == Path(hub.app.cfg.cad_db).parent
    finally:
        hub.close()


# -- the #651 re-review: copies that can't all be read, lost pre-alerts, the heartbeat, a busy worker ----------------


def _copy_bytes(tmp_path, name, schema, rows):
    path = tmp_path / name
    db = sqlite3.connect(path)
    db.executescript(schema)
    for r in rows:
        db.execute(f"INSERT INTO stt_runs ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
    db.commit()
    db.close()
    return path.read_bytes()


def test_a_torn_newest_copy_falls_back_to_an_older_one_and_a_copy_a_column_short_still_merges(tmp_path):
    clock = Clock(2_000_000_000.0)
    copies = FakeCopies()
    full = _copy_bytes(tmp_path, "full.sqlite", sttruns.SCHEMA,
                       [{"clip_id": "FG1-1", "engine": "aws", "text": "older copy", "done_at": clock.t - 100}])
    copies.slots[0] = (1.0, full)                                  # an older copy, good
    copies.slots[1] = (2.0, b"SQLite format 3\x00" + b"\x00" * 100)  # the newest, torn
    runs = sttruns.Runs(tmp_path / "a" / "stt.sqlite", backup=copies, clock=clock, log=lambda m: None)
    assert runs.backup_turn() == "settled" and [r["text"] for r in runs.of("FG1-1")] == ["older copy"]
    # a copy from a release without call_id / turn / keyup (and with a column this one hasn't): merged by what both have
    old_schema = sttruns.SCHEMA.replace(", keyup TEXT NOT NULL DEFAULT '', call_id TEXT NOT NULL DEFAULT '',\n  turn INTEGER",
                                        ", extra_old TEXT")
    assert "call_id" not in old_schema
    short = _copy_bytes(tmp_path, "short.sqlite", old_schema,
                        [{"clip_id": "FG1-2", "engine": "aws", "text": "older release", "done_at": clock.t - 50,
                          "extra_old": "x"}])
    c2 = FakeCopies()
    c2.slots[0] = (1.0, short)
    runs2 = sttruns.Runs(tmp_path / "b" / "stt.sqlite", backup=c2, clock=clock, log=lambda m: None)
    assert runs2.backup_turn() == "settled"
    got = runs2.of("FG1-2")[0]
    assert got["text"] == "older release" and got["call_id"] == "" and got["turn"] is None
    # nothing readable at all: unsettled, said on Health, never an upload, tried again after a back-off
    c3 = FakeCopies()
    c3.slots[0] = (1.0, b"not sqlite")
    runs3 = sttruns.Runs(tmp_path / "c" / "stt.sqlite", backup=c3, clock=clock, log=lambda m: None)
    assert runs3.backup_turn() == "failed" and runs3.backup_turn() == "" and c3.puts == 0
    clock.t += sttruns.UNSETTLED_WARN_S + 1
    h = runs3.health()
    assert h["settled"] is False and h["unsettled_since"] and "not restored" in h["warn"]


def test_the_metrics_store_restores_through_the_same_helper(tmp_path):
    from sizeup.cloud import metricsbackup, metricstore
    assert metricstore._common_columns is metricsbackup.common_columns and metricstore.merge_copies is metricsbackup.merge_copies


def test_lost_pre_alert_leases_close_the_quick_gate_and_repeated_cooldowns_grow():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    for i in range(2):                                         # claims and never answers
        assert p.submit(j.job(f"page{i}", URGENT, seconds=0.0)), i
        p.claim("gpu-1", 0)
        clock.t += BUDGETS[URGENT][1]
        p.tick()
        online(p)
        if i == 0:
            assert p.ready(URGENT)[1].startswith("slow on pre-alert")   # one lost: its cap (8 s) > 3 s already
            clock.t += sttpool.PROBE_S                                   # (its probe: the second page)
            online(p)
    p.drain()
    assert len(j.fell) == 2
    w = p.workers["gpu-1"]
    cools = []
    for _round in range(3):                                    # three losses in a row, three times over: 60, 120, 240 s
        for k in range(sttpool.FAIL_N):
            p._failed("gpu-1", clock.t)
        cools.append(round(w.cool_until - clock.t))
        clock.t = w.cool_until
    assert cools[-3:] == [60, 120, 240]
    assert p.submit(j.job("live", LIVE, seconds=0.0))
    job = p.claim("gpu-1", 0)
    p.complete("gpu-1", job.lease, {"text": "x"})
    assert w.cools == 0                                        # a job done: back to the first length


def test_a_heartbeat_every_few_seconds_keeps_a_long_lease_and_silence_loses_it():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    p.submit(j.job("low", LOW, seconds=30.0))
    job = p.claim("gpu-1", 0)
    for _ in range(4):                                         # 20 s of decoding, a heartbeat every 5 s
        clock.t += 5.0
        p.seen("gpu-1", "online")
        assert p.tick() == 0
    assert job.lease in p._leases
    clock.t += sttpool.LOST_S                                  # then silence
    assert p.tick() == 1


def test_a_worker_busy_on_a_job_never_wins_a_pre_alert_clip():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p)
    online(p, "gpu-2")
    p.submit(j.job("low", LOW, seconds=0.0))
    assert p.claim("gpu-1", 0).id == "low"                     # gpu-1 busy now (one lease of two)
    assert p.submit(j.job("page", URGENT, seconds=0.0))        # (gpu-2 is idle: the pool may take it)
    assert p.claim("gpu-1", 0) is None                         # never the busy one ...
    assert p.claim("gpu-2", 0).id == "page"                    # ... the idle one


def test_the_pre_alert_gate_is_per_worker_reopens_by_probe_or_window_and_ignores_gaming_take_backs():
    clock, j = Clock(), Jobs()
    p = pool(clock)
    online(p, "gpu-1")
    assert p.submit(j.job("page", URGENT, seconds=0.0))
    p.claim("gpu-1", 0)
    clock.t += BUDGETS[URGENT][1]
    p.tick()                                                   # lost: gpu-1 slow on pre-alerts now
    online(p, "gpu-1")
    assert p.ready(URGENT)[1].startswith("slow on pre-alert")
    online(p, "gpu-2")                                         # another worker isn't held to gpu-1's record
    assert p.ready(URGENT)[0] and p.submit(j.job("air", HIGH, seconds=0.0))
    assert p.claim("gpu-1", 0) is None and p.claim("gpu-2", 0).id == "air"     # (the slow one never gets it)
    p.forget("gpu-2")
    p.drain()
    # a probe after PROBE_S: one clip, and a quick answer opens the gate again
    clock.t += sttpool.PROBE_S
    online(p, "gpu-1")
    assert p.ready(URGENT)[0] and p.submit(j.job("probe", URGENT, seconds=0.0))
    probe = p.claim("gpu-1", 0)
    assert probe.id == "probe"
    online(p, "gpu-1")
    assert p.submit(j.job("next", URGENT, seconds=0.0)) is False   # (one probe at a time: the next waits for its answer)
    clock.t += 0.5
    assert p.complete("gpu-1", probe.lease, {"text": "x"})
    online(p, "gpu-1")
    assert p.ready(URGENT)[1].startswith("slow on pre-alert")  # (median of [8, 0.5] is still 8: not quick yet)
    for i in range(2):
        clock.t += sttpool.PROBE_S
        online(p, "gpu-1")
        assert p.submit(j.job(f"probe{i}", URGENT, seconds=0.0))
        q = p.claim("gpu-1", 0)
        clock.t += 0.5
        assert p.complete("gpu-1", q.lease, {"text": "x"})
    online(p, "gpu-1")
    assert p.ready(URGENT)[0]                                  # quick again: the gate is open
    # the window: a slow record older than FAST_WINDOW_S is forgotten
    w = p.workers["gpu-1"]
    w.fast.clear()
    w.fast.append((clock.t, 8.0))
    online(p, "gpu-1")
    assert not p.ready(URGENT)[0]
    clock.t += sttpool.FAST_WINDOW_S + 1
    online(p, "gpu-1")
    assert p.ready(URGENT)[0] and not w.fast
    # gaming or a pause takes a pre-alert job back: that isn't slowness
    assert p.submit(j.job("g", URGENT, seconds=0.0))
    p.claim("gpu-1", 0)
    p.seen("gpu-1", "gaming")
    p.drain()
    assert "went gaming" in j.fell["g"] and not w.fast
    online(p, "gpu-1")
    assert p.ready(URGENT)[0]


def test_airband_pool_jobs_go_without_a_whisper_prompt(tmp_path, monkeypatch):
    """An ATC prompt costs words and makes large-v3-turbo loop on a weak AM clip: the airband's pool jobs (_pool_first,
    _pool_clip, _catchup_pool) carry no prompt (and so no vocab_v); every other channel's keeps its own (_prompt_v)."""
    from types import SimpleNamespace

    from sizeup.incidents import Traffic
    from tests.test_cloud import HubRun, hub_cfg
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app = hub.app
        jobs = []
        monkeypatch.setattr(app, "sttpool", SimpleNamespace(ready=lambda cls, **kw: (True, ""), has=lambda cid: False,
                                                            ready_forced=lambda cls, **kw: (True, ""), workers={},
                                                            submit=lambda job: jobs.append(job) or True))
        monkeypatch.setattr(app, "_prompt_v", lambda item: ("the source's own words", "v1"))
        monkeypatch.setitem(app._kind, "HPN Tower", "airband")
        pcm = np.zeros(8000 * 2, np.int16)
        assert app._pool_first(Traffic("HPNTower-1795650700312", "HPN Tower", 1.0, 3.0, ""), pcm, "HPN Tower")
        assert jobs[-1].prompt == "" and jobs[-1].extra["vocab_v"] == ""
        assert app._pool_first(Traffic("FG1-901", "FG1", 1.0, 3.0, ""), pcm, "FG1")
        assert jobs[-1].prompt == "the source's own words" and jobs[-1].extra["vocab_v"] == "v1"
        app._pool_clip(Traffic("HPNTower-1795650700999", "HPN Tower", 1.0, 3.0, ""), "HPN Tower", None)
        assert jobs[-1].id == "HPNTower-1795650700999" and jobs[-1].prompt == ""
        monkeypatch.setattr(app, "_catchup_pcm", lambda *a: pcm)
        t = Traffic("HPNTower-1795650701000", "HPN Tower", 1.0, 3.0, "")
        monkeypatch.setattr(app, "_find", lambda cid: (t, "HPN Tower"))
        sent, _why = app._catchup_pool({"id": t.id, "key": "HPN Tower", "source": "HPN Tower", "start": 1.0,
                                        "seconds": 2.0})
        assert sent and jobs[-1].prompt == "" and jobs[-1].extra["vocab_v"] == ""
    finally:
        monkeypatch.undo()                                                # (the real pool back before the hub stops)
        hub.close()
