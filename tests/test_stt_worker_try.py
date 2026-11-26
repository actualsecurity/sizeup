"""The GPU worker's try (challenger) models and challenger jobs (deploy/stt-worker/worker.py: stt_models try, job
challenger/greedy, POST jobs/{lease}/watch): a try model loads on demand only beside every planned model and the LLM's
share, never takes the LLM's room from it nor stops a planned model, and is unloaded when not wanted, idle, or its room
is needed; a challenger clip gives way to the LLM, decodes greedily on its own model only, and stops at once when the
hub preempts it. Fakes only: a fake Popen for the servers, a fake hub and whisper-server on 127.0.0.1."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from tests.stt_worker_module import W
from tests.test_stt_worker import (API, FakeHub, make_cfg, no_smi, running, tone_wav, wait_for,  # noqa: F401
                                   hub, no_secrets_in_logs, whisper)
from tests.test_stt_worker_models import _claim_extra, entry, ready_models, sha, two_models, HF


# --- try models (Models, fakes) --------------------------------------------------------------------------------------

def three_models() -> dict:
    cat = two_models()
    cat["stt_models"]["try-test"] = entry(files={"model": {"file": "ggml-try.bin", "url": f"{HF}/ggml-try.bin",
                                                           "bytes": 4, "sha256": sha(b"abcd")}}, vram_mb=1400)
    cat["stt_models"]["try-two"] = entry(files={"model": {"file": "ggml-try2.bin", "url": f"{HF}/ggml-try2.bin",
                                                          "bytes": 4, "sha256": sha(b"abcd")}}, vram_mb=1000)
    return cat


def rig(tmp_path, monkeypatch, llm_mb: float = 2000.0, budget: float = 10752.0):
    """10,752 MB (a 12 GB card), the primary 1000, Qwen3-ASR-like 3800 and ATC-like 2300 planned, an LLM of llm_mb;
    a try model of 1400 (and one of 1000)."""
    m, procs, clock = ready_models(tmp_path, monkeypatch, three_models())
    m.cfg.vram_budget_mb = budget
    m.cfg.llm_server, m.cfg.llm_vram_mb = ["llama-server", "-m", "llm.gguf"], llm_mb
    m.want = ["asr-test", "atc-test"]
    return m, procs, clock


def go(m, idle=True, held=False, llm_running=False):
    m.step(idle=lambda: idle, held=held, gpu=None, llm_running=llm_running, stop=lambda: held)


def answer(m, mid):
    s = m.servers[mid]
    s.ok, s.alive = True, (lambda: True)
    s.last_used = m.clock()


def champions(m):
    """Both planned models started and answering."""
    for mid in ("asr-test", "atc-test"):
        go(m)
        answer(m, mid)


def test_a_try_model_loads_only_after_the_plan_and_beside_the_llm(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch, llm_mb=2000)       # 1000 + 3800 + 2300 + 2000 = 9100: 1652 left
    m.try_ = ["try-test"]
    go(m)
    assert list(m.servers) == ["asr-test"] and not m.trial          # a planned model first
    answer(m, "asr-test")
    go(m, idle=False)
    assert list(m.servers) == ["asr-test"]                          # a clip in hand: nothing
    go(m)
    assert list(m.servers) == ["asr-test", "atc-test"] and not m.trial
    answer(m, "atc-test")
    go(m, held=True)
    assert not m.trial                                              # gaming: never
    go(m)
    assert m.trial == "try-test" and m.servers["try-test"].trial and not m.servers["try-test"].in_llm_room
    assert m.status["try-test"] == "try: loading" and "try-test" not in m.ready()
    answer(m, "try-test")
    go(m)
    assert m.ready() == ["asr-test", "atc-test", "try-test"] and m.llm_room_ok and m.plan == ["asr-test", "atc-test"]
    assert "try-test" in m.report()["stt_loaded"]
    m.make_plan(None)
    assert m.llm_room_ok                                            # never flipped by the try model


def test_no_room_beside_the_llm_no_try_model(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch, llm_mb=2500)       # 1000 + 6100 + 2500: 1152 left (1400 no)
    m.try_ = ["try-test"]
    champions(m)
    go(m)
    assert not m.trial and m.status["try-test"] == "try: no room" and m.llm_room_ok
    m.try_ = ["try-test", "try-two"]                                # one of 1000 fits
    go(m)
    assert m.trial == "try-two"
    answer(m, "try-two")
    go(m)
    assert m.trial == "try-two" and "try-test" not in m.servers     # at most one at a time


def test_into_the_llms_room_only_once_it_gave_way_and_out_before_it_comes_back(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch, llm_mb=6000)       # 7100 + 6000 > 10752: the LLM gave way (yield)
    m.try_ = ["try-test"]
    champions(m)
    go(m, llm_running=True)
    assert not m.llm_room_ok and not m.trial                        # llama-server not down yet: never into its room
    go(m)
    assert m.trial == "try-test" and m.servers["try-test"].in_llm_room and m.try_in_llm_room()
    answer(m, "try-test")
    m.want = ["atc-test"]                                           # the plan shrinks: the LLM may come back
    go(m, idle=False)
    s = m.servers["try-test"]
    assert m.llm_room_ok and s.draining and m.try_in_llm_room()    # (llama-server waits for it) ...
    assert "try-test" not in m.ready() and m.acquire("try-test") is None
    go(m)
    assert "try-test" not in m.servers and not m.trial and not m.try_in_llm_room()   # ... unloaded once idle
    assert m.status["try-test"] == "try: no room"


def test_keep_never_lends_the_llms_room(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch, llm_mb=2000)
    m.cfg.llm_priority = "keep"
    m.try_ = ["try-test"]
    champions(m)
    go(m)
    assert m.trial == "try-test" and not m.servers["try-test"].in_llm_room   # 10752 - 7100 - 2000: 1652 left
    m.stop("try-test", "test")
    m.trial = ""
    m.cfg.llm_vram_mb = 3000                                        # 652 left beside it: no room
    go(m)
    assert not m.trial and m.status["try-test"] == "try: no room" and m.llm_room_ok


def test_unused_for_try_idle_s_it_is_unloaded_never_with_a_clip_on_it(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch)
    m.try_ = ["try-test"]
    champions(m)
    go(m)
    answer(m, "try-test")
    s = m.acquire("try-test")                                       # a clip on it
    clock[0] += W.TRY_IDLE_S + 1
    go(m)
    assert m.servers["try-test"] is s                               # (in use: not idle)
    m.release(s)
    go(m)
    assert m.servers["try-test"] is s                               # just used
    clock[0] += W.TRY_IDLE_S - 1
    go(m)
    assert "try-test" in m.servers
    clock[0] += 2
    go(m, idle=False)
    assert "try-test" in m.servers                                  # a clip on another model: not now
    go(m)
    assert "try-test" not in m.servers and m.status["try-test"] == "try: unloaded (idle)" and not m.trial
    go(m)
    assert m.trial == "try-test"                                    # loaded again on demand (still wanted)


def test_the_plan_needs_its_room_unloaded_before_the_planned_model_starts(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch, llm_mb=0.0)
    m.cfg.llm_server = []
    m.want = ["asr-test"]                                           # 1000 + 3800: 5952 left
    m.try_ = ["try-test"]
    go(m)
    answer(m, "asr-test")
    go(m)
    answer(m, "try-test")
    m.cfg.vram_budget_mb = 7200                                     # the budget shrinks: 1000 + 3800 + 2300 = 7100
    m.want = ["asr-test", "atc-test"]
    go(m)
    assert "try-test" not in m.servers and "atc-test" not in m.servers   # the try model out first ...
    go(m)
    assert "atc-test" in m.servers                                  # ... then the planned one
    assert m.status["try-test"] == "try: no room"


def test_not_wanted_any_more_it_is_unloaded_and_said_no_more(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch)
    m.try_ = ["try-test"]
    champions(m)
    go(m)
    answer(m, "try-test")
    m.offer({"stt_models": {"want": ["asr-test", "atc-test"], "try": []}})
    go(m)
    assert "try-test" not in m.servers and "try-test" not in m.status and not m.trial
    assert "asr-test" in m.servers and "atc-test" in m.servers      # never a planned model


def test_a_try_id_the_hub_now_wants_is_a_planned_model(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch, llm_mb=0.0)
    m.cfg.llm_server = []
    m.try_ = ["try-test"]
    champions(m)
    go(m)
    answer(m, "try-test")
    s = m.servers["try-test"]
    m.offer({"stt_models": {"want": ["asr-test", "atc-test", "try-test"], "try": ["try-test"]}})
    assert m.try_ == []                                             # (want wins)
    go(m)
    assert m.servers["try-test"] is s and not s.trial and not m.trial and "try-test" in m.plan
    assert m.status["try-test"] == "ready"


def test_gaming_stops_it_with_the_others(tmp_path, monkeypatch):
    m, procs, clock = rig(tmp_path, monkeypatch)
    m.try_ = ["try-test"]
    champions(m)
    go(m)
    m.stop_all("gaming")
    go(m, held=True)
    assert not m.trial and m.servers == {}


# --- challenger jobs (a Worker against a fake hub) -------------------------------------------------------------------

class WatchHub(FakeHub):
    """FakeHub plus POST jobs/{lease}/watch as the hub answers it: {"live": true} after the wait, at once
    {"live": false} once the lease is preempted (taken back); 409 for a lease that is gone. watch=False: an older hub
    (404)."""

    def __init__(self):
        self.watch = True
        self.preempted: set = set()
        self.watch_max = 0.3
        super().__init__()

    def preempt_all(self) -> None:
        with self.cv:
            for lease, lv in self.leases.items():
                if not lv["closed"]:
                    lv["closed"] = True
                    self.preempted.add(lease)
            self.cv.notify_all()

    def handle(self, req, method, gen):
        if not req.path.endswith("/watch"):
            return super().handle(req, method, gen)
        n = int(req.headers.get("Content-Length") or 0)
        body = json.loads(req.rfile.read(n) or b"{}")
        with self.cv:
            self.requests.append((method, req.path, dict(req.headers), body))
        if not self.watch:
            return self.send(req, 404, {"error": "not found"})
        lease = req.path.split("/")[-2]
        with self.cv:
            if lease not in self.leases:
                return self.send(req, 409, {"error": "lease lost"})
            self.cv.wait_for(lambda: lease in self.preempted or self.gen != gen,
                             timeout=min(float(body.get("wait", 0)), self.watch_max))
            live = lease not in self.preempted
        return self.send(req, 200, {"live": live})


@pytest.fixture
def whub():
    h = WatchHub()
    yield h
    h.stop()


def challenger(hub, clip="CH-1", model="", seconds=1.5, lease_s=6.0, **extra):
    hub.add_job(clip, tone_wav(seconds), cls="LOW", lease_s=lease_s, fast=False)
    hub.pending[-1]["extra"] = {"challenger": True, "greedy": True, "fast": False, **({"model": model} if model else {}),
                                **extra}


def worker(tmp_path, hub, whisper, **kw):
    _claim_extra(hub)
    return W.Worker(make_cfg(tmp_path, hub, whisper, **kw), run=no_smi)


def released(hub) -> list:
    return [r[3]["why"] for r in hub.calls("/release")]


def test_a_challenger_decodes_greedily_on_the_primary_and_watches(tmp_path, whub, whisper):
    w = worker(tmp_path, whub, whisper)
    challenger(whub, model=w.cfg.model)
    with running(w):
        wait_for(lambda: whub.accepted(), what="the result")
    (req,) = whisper.jobs()
    assert req["fields"]["temperature_inc"] == b"0.0" and req["fields"]["temperature"] == b"0.0"
    assert whub.accepted()[0]["model"] == w.cfg.model


def test_a_challenger_gives_way_to_the_llm_and_never_preempts_it(tmp_path, whub, whisper):
    w = worker(tmp_path, whub, whisper)
    w.llm_job = {"lease": "a" * 16}                                  # an LLM job held
    w._llm_call = type("Gen", (), {"abort": lambda self: None})()  # (a generation in flight)
    challenger(whub, model=w.cfg.model, fast=True)                  # (even if the hub said fast)
    with running(w):
        wait_for(lambda: whub.calls("/release"), what="handed back")
    assert released(whub) == ["busy: LLM"] and not w._preempt.is_set()
    assert whisper.jobs() == [] and not whub.calls("/audio")


def test_an_llm_job_that_comes_mid_decode_takes_the_gpu_back(tmp_path, whub, whisper):
    whisper.delay = 2.0
    w = worker(tmp_path, whub, whisper)
    whisper.before = lambda f: setattr(w, "llm_job", {"lease": "b" * 16}) if len(f.get("file", b"")) > 40000 else None
    challenger(whub, model=w.cfg.model)
    with running(w):
        wait_for(lambda: whub.calls("/release"), what="handed back")
        wait_for(lambda: whisper.aborted == 1, what="the decode aborted")
    assert released(whub) == ["busy: LLM"] and not whub.accepted() and not w._preempt.is_set()


def test_a_challenger_runs_only_on_its_own_model(tmp_path, whub, whisper):
    w = worker(tmp_path, whub, whisper)
    challenger(whub, model="atc-test")                              # not loaded here
    with running(w):
        wait_for(lambda: whub.calls("/release"), what="handed back")
    assert released(whub) == ["model not loaded"] and whisper.jobs() == []       # never the primary instead


def test_a_challenger_on_a_try_model(tmp_path, whub, whisper):
    w = worker(tmp_path, whub, whisper)
    e = W.check_catalog(two_models())["atc-test"]
    port = int(whisper.url.split(":")[2].split("/")[0])
    s = W.ModelServer(e, {}, port, proc=None, timeout_s=10)
    s.ok, s.trial = True, True
    w.stt_models.servers["atc-test"] = s
    challenger(whub, model="atc-test")
    with running(w):
        wait_for(lambda: whub.accepted(), what="the result")
    assert whub.accepted()[0]["model"] == "atc-test" and s.inuse == 0
    assert whisper.jobs()[0]["fields"]["temperature_inc"] == b"0.0"


def test_a_try_model_unloading_ends_its_challenger_at_once(tmp_path, whub, whisper):
    whisper.delay = 2.0
    w = worker(tmp_path, whub, whisper)
    e = W.check_catalog(two_models())["atc-test"]
    port = int(whisper.url.split(":")[2].split("/")[0])
    s = W.ModelServer(e, {}, port, proc=None, timeout_s=10)
    s.ok, s.trial = True, True
    w.stt_models.servers["atc-test"] = s
    whisper.before = lambda f: setattr(s, "draining", True) if len(f.get("file", b"")) > 40000 else None
    challenger(whub, model="atc-test")
    with running(w):
        wait_for(lambda: whub.calls("/release"), what="handed back")
        wait_for(lambda: whisper.aborted == 1, what="the decode aborted")
    assert released(whub) == ["model unloading"] and s.inuse == 0


def test_preempted_by_the_hub_it_stops_at_once_and_says_nothing_more(tmp_path, whub, whisper, caplog):
    caplog.set_level(logging.INFO, logger="sizeup.stt_worker")
    whisper.delay = 3.0
    whub.watch_max = 5.0
    w = worker(tmp_path, whub, whisper)
    t: dict = {}

    def before(f):
        if len(f.get("file", b"")) > 40000 and "at" not in t:
            t["at"] = time.monotonic()
            threading.Timer(0.2, whub.preempt_all).start()
    whisper.before = before
    challenger(whub, model=w.cfg.model)
    with running(w):
        wait_for(lambda: w.counts.get("preempted") == 1, what="preempted")
        gone = time.monotonic()
        wait_for(lambda: whisper.aborted == 1, what="the decode aborted")
        whisper.before, whisper.delay = None, 0.0
        whub.add_job("LIVE-1", tone_wav(1.0), cls="URGENT", lease_s=4.0, fast=True)   # then a live clip at once
        wait_for(lambda: whub.accepted(), what="the live clip")
    assert gone - t["at"] < 1.5
    assert not whub.calls("/complete")[:-1] and not whub.calls("/release")        # no complete, no release for it
    assert len(whub.calls("/complete")) == 1 and whub.accepted()[0]["text"]
    assert any(r.endswith("/watch") for _, r, _, _ in whub.requests)
    assert "preempted" in caplog.text and "Radio check" not in caplog.text


def test_an_older_hub_without_the_watch_extends_every_half_second(tmp_path, whub, whisper):
    whub.watch = False
    whisper.delay = 1.3
    w = worker(tmp_path, whub, whisper)
    challenger(whub, model=w.cfg.model, lease_s=30.0)
    with running(w):
        wait_for(lambda: whub.accepted(), what="the result")
    assert len(whub.calls("/watch")) == 1 and len(whub.calls("/extend")) >= 2   # (a lease of 30 s: not its half)


def test_an_older_hub_that_takes_the_lease_back_preempts_it(tmp_path, whub, whisper):
    whub.watch = False
    whisper.delay = 3.0
    w = worker(tmp_path, whub, whisper)
    whisper.before = lambda f: threading.Timer(0.3, whub.expire_all).start() if len(f.get("file", b"")) > 40000 else None
    challenger(whub, model=w.cfg.model, lease_s=30.0)
    with running(w):
        wait_for(lambda: w.counts.get("preempted") == 1, what="preempted")
        wait_for(lambda: whisper.aborted == 1, what="the decode aborted")
    assert not whub.calls("/complete") and not whub.calls("/release")


def test_gaming_ends_a_challenger_at_once(tmp_path, whub, whisper):
    whisper.delay = 3.0
    w = worker(tmp_path, whub, whisper)
    whisper.before = (lambda f: W.Control(w.cfg.control_path).set(gaming=True)
                      if len(f.get("file", b"")) > 40000 else None)
    challenger(whub, model=w.cfg.model, lease_s=30.0)
    with running(w):
        wait_for(lambda: whub.calls("/release"), what="handed back")
        wait_for(lambda: whisper.aborted == 1, what="the decode aborted")
    assert released(whub)[0].startswith("gaming") and not whub.accepted()


def test_the_worker_says_it_takes_challenger_jobs_in_hello_state_and_claim(tmp_path):
    """caps ["challenger"]: this worker.py takes challenger / greedy jobs, their watch preemption and stt_models.try.
    The hub sends challenger jobs only to a worker whose latest hello, state or claim said so."""
    from tests.test_stt_worker import FakeWhisper
    hub, whisper = FakeHub(), FakeWhisper()
    try:
        w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
        with running(w):
            wait_for(lambda: hub.calls("/claim") and hub.calls("/state"), what="a claim and a heartbeat")
        for route in ("/hello", "/state", "/claim"):
            assert all(r[3]["caps"] == ["challenger"] for r in hub.calls(route)), route
        assert W.CAPS == ("challenger",)
    finally:
        hub.stop()
        whisper.stop()
