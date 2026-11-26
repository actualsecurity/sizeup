"""Champion and challenger on the pool and the hub (#869 on #896): a challenger never slows live traffic. It ranks below
everything (background too), runs only on an idle GPU that has its model loaded (on demand: the worker's try list,
never its resident plan), never while LLM work is held or queued, never right after another clip, under its hourly
caps; any other work takes it back at once (the worker's watch); it never cools a worker down, never feeds the pre-alert
speed record or the pool's numbers, never AWS. On a hub: the same audio input and context as the champion, its text a
private run only (role challenger, with its input and correction), scored without the refs that were in the context;
promote only on evidence, shown first; intel told before any champion change; an automatic rollback audited and the
admins notified. Invented clips, units and models' texts."""

from __future__ import annotations

import json

import pytest

from sizeup import sttpool
from sizeup.stt import CATCHUP, LIVE, URGENT
from tests.test_channels import fake_aws  # noqa: F401 (the fixture: test credentials only)
from tests.test_stt_pool import Clock, Jobs, online, pool

Q8, QWEN, Q5 = "large-v3-turbo-q8_0", "qwen3-asr-1.7b-q8_0", "large-v3-turbo-q5_0"


def _pool():
    clk = Clock()
    p, jobs = pool(clk), Jobs()
    w = online(p, "gpu-a")
    w.models, w.caps, w.model = [Q5, Q8], frozenset({"challenger"}), f"ggml-{Q5}.bin"
    clk.t += sttpool.CHALL_QUIET_S + 1
    return clk, p, jobs, w


def chall(jobs, cid="c1", **kw):
    return jobs.job(f"{cid}~ch", LIVE, model=kw.pop("model", Q8), chall=True, bg=True, forced=True, gpu_only=True, **kw)


def test_a_challenger_is_one_greedy_pass_on_its_model_never_a_pre_alert_job():
    _clk, p, jobs, _w = _pool()
    assert p.submit(chall(jobs))
    pub = p.claim("gpu-a", 0).public()
    assert pub["fast"] is False and pub["challenger"] is True and pub["greedy"] is True and pub["model"] == Q8


def test_it_ranks_below_background_and_waits_for_every_other_job():
    _clk, p, jobs, _w = _pool()
    assert p.submit(chall(jobs))
    assert p.submit(jobs.job("bg1", CATCHUP, bg=True))
    assert p.claim("gpu-a", 0).id == "bg1"                        # (the catch-up first: challengers last of all)
    assert sttpool.rank(chall(jobs, "x")) == 3 > sttpool.rank(jobs.job("b", CATCHUP, bg=True))


def test_only_on_a_worker_with_its_model_loaded_quiet_and_no_llm_work():
    clk, p, jobs, w = _pool()
    assert p.submit(chall(jobs, model=QWEN))
    assert p.claim("gpu-a", 0) is None                             # (not loaded there: the worker's try, on demand)
    w.models = [Q5, Q8, QWEN]
    p.llm_busy = lambda wid: True                                  # LLM work held or queued: none
    assert p.claim("gpu-a", 0) is None
    p.llm_busy = lambda wid: False
    assert p.submit(jobs.job("live1", LIVE))                       # another clip came: CHALL_QUIET_S first
    live = p.claim("gpu-a", 0)
    assert live.id == "live1" and p.complete("gpu-a", live.lease, {"text": "x"})
    assert p.claim("gpu-a", 0) is None
    clk.t += sttpool.CHALL_QUIET_S
    assert p.claim("gpu-a", 0).id == "c1~ch"


def test_any_other_work_takes_it_back_at_once_and_the_worker_is_free_for_it():
    clk, p, jobs, w = _pool()
    assert p.submit(chall(jobs))
    j = p.claim("gpu-a", 0)
    w.polling = 0                                                  # (decoding the challenger, not in a claim)
    # while it holds only a challenger the worker is free: a pre-alert clip and background work may go to it
    assert p.ready(URGENT)[0] and p.ready_forced(URGENT)[0]
    with p._cv:
        assert p._free(w) and p._bg_free(w, clk.t + 10)
    assert p.watch("gpu-a", j.lease, 0) is True
    assert p.submit(jobs.job("u1", URGENT, forced=True))           # a clip of ours: the challenger taken back
    assert p.watch("gpu-a", j.lease, 5) is False                   # (at once: the worker stops its decode)
    assert p.extend("gpu-a", j.lease) is None
    assert p.drain() == 1 and j.extra["fell"] == "preempted" and "c1~ch" in jobs.fell
    w.polling = 1
    assert p.claim("gpu-a", 0).id == "u1"
    assert p.challenges["preempted"] == 1 and p.stats()["fallbacks"].get("preempted", 0) == 0


def test_llm_work_queued_takes_it_back_too():
    _clk, p, jobs, _w = _pool()
    assert p.submit(chall(jobs))
    j = p.claim("gpu-a", 0)
    assert p.preempt_challengers("LLM work came") == 1
    assert p.watch("gpu-a", j.lease, 1) is False


def test_it_never_cools_a_worker_never_feeds_the_speed_record_or_the_numbers():
    clk, p, jobs, w = _pool()
    for i in range(sttpool.FAIL_N + 2):                            # handed back again and again, leases run out
        assert p.submit(chall(jobs, f"r{i}"))
        k = p.claim("gpu-a", 0)
        if i % 2:
            assert p.release("gpu-a", k.lease, "model failed")
        else:
            clk.t += 200
            p.tick()
        clk.t += sttpool.CHALL_QUIET_S
    assert w.cool_until <= clk.t and w.fails == 0 and w.lost == 0 and not w.fast
    assert p.submit(chall(jobs, "done"))
    k = p.claim("gpu-a", 0)
    assert p.complete("gpu-a", k.lease, {"text": "x", "model": Q8})
    st = p.stats()
    assert st["done_h"] == 0 and st["p50_s"] is None and w.done == 0 and st["challengers"]["done"] == 1
    assert st["queued"] == 0 and st["leased"] == 0 and st["fallbacks"]["released"] == 0
    assert st["challengers"]["skipped"] >= sttpool.FAIL_N


def test_hourly_caps_per_channel_and_per_worker(monkeypatch):
    monkeypatch.setattr(sttpool, "CHALL_CAP_CHANNEL_H", 2)
    clk, p, jobs, _w = _pool()
    for i in range(2):
        assert p.submit(chall(jobs, f"h{i}"))
        k = p.claim("gpu-a", 0)
        assert p.complete("gpu-a", k.lease, {"text": "x", "model": Q8})
    assert p.chall_ready("FG1", Q8, 3.0) == (False, "its hourly cap")
    assert p.submit(chall(jobs, "h2"))
    assert p.claim("gpu-a", 0) is None                             # (queued before the cap: still not run)
    clk.t += 3601
    assert p.claim("gpu-a", 0).id == "h2~ch"
    assert p.chall_ready("FG1", Q8, sttpool.CHALL_MAX_S + 1) == (False, "too long")


# -- on a hub ---------------------------------------------------------------------------------------------------------

def test_on_a_hub_the_same_input_and_context_a_private_run_promote_on_evidence_and_rollback(tmp_path, fake_aws,
                                                                                           monkeypatch):
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_app import wait
    from tests.test_channels import AWS_TEXT, aws_cfg
    from tests.test_cloud import HubRun, call, hub_cfg
    from tests.test_stt_engine_select import _set
    from tests.test_stt_pool import FakeWorker, pending_event, row, send

    monkeypatch.setattr(sttpool, "CHALL_QUIET_S", 0.0)

    class ModelWorker(FakeWorker):
        def post(self, path, body):
            if path.endswith(("/claim", "/hello", "/state")):
                body = dict(body, models=[Q5, QWEN, Q8] if path.endswith("/claim") else body.get("models"),
                            caps=["challenger"])
            elif path.endswith("/complete") and self.jobs:
                job = self.jobs[-1]
                body = dict(body, model=job.get("model") or Q5)
                if job.get("challenger"):
                    body["text"] = "Engine 60 Ladder 31 on location"
            return call(self.url + path, "POST", body, token=self.token)

    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    app = hub.app
    _set(hub, "FG1", "auto")
    app._pool_class = lambda item, key: LIVE
    app.stt_choices.set("FG1", {"model": QWEN, "challenger": Q8, "pct": 100, "callsigns": ["Example Base"]})
    pushed = []
    monkeypatch.setattr(app.health, "notifier", type("N", (), {"enabled": True, "send": lambda self, *a, **k:
                                                                   pushed.append(a) or True})())
    token = app.hub.workers.add("gpu-1", "Test box")
    w = ModelWorker(hub.url, token, text="Engine 60 Ladder 13 on location").start()
    try:
        app.stt_api.build = {"commit": "c" * 40}
        word = app._stt_models_word()
        assert word["want"] == [QWEN] and word["try"] == [Q8]       # (the challenger on demand, never resident)
        assert wait(lambda: app.sttpool.ready_forced(URGENT)[0], 5)
        send(hub, pending_event("FG1-21"))
        assert wait(lambda: any(r["role"] == "challenger" for r in app.stt_runs.of("FG1-21")), 30)
        champ, ch = w.jobs[0], w.jobs[-1]
        assert not champ.get("challenger") and ch["challenger"] is True and ch["model"] == Q8 and ch["fast"] is False
        assert ch["prompt"] == champ["prompt"] and ch.get("context") == champ.get("context")
        assert "Example Base" in ch["context"]
        assert w.audio[0][1] == w.audio[-1][1]                    # (the very input the champion decoded)
        runs = app.stt_runs.of("FG1-21")
        text_run = next(r for r in runs if r["role"] == "text")
        run = next(r for r in runs if r["role"] == "challenger")
        assert run["input"] == text_run["input"] and run["input"] and run["want"] == Q8 and run["model"] == Q8
        score = json.loads(run["score"])
        assert score["champion"] == QWEN and score["challenger"] == Q8
        assert row(hub, "FG1-21")["text"] != run["text"]          # (never the row: a private run)
        # never the guard, the routes, the latency book
        assert all(e["engine"] != "gpu-1" or e["n"] == 1 for e in app.stt_engines.table())
        board = app.challenge_board("FG1")
        assert board["samples"] == 1 and board["promote_ok"] is False and board["min_n"] == 40
        from sizeup.cad.api import CadApi
        from tests.test_station_identity import CASEY, JORDAN, Identity, people
        people(app.cad)
        api = CadApi(app.cad)

        def promote(confirm):
            return api.post("/api/cad/stt-workers", json.dumps({"action": "promote", "key": "FG1", "confirm": confirm,
                                                                "operator": "Admin Example"}).encode(),
                            Identity(email=CASEY))
        assert api.post("/api/cad/stt-workers", json.dumps({"action": "promote", "key": "FG1"}).encode(),
                        Identity(email=JORDAN))[0] == 403                          # admins only
        code, out, _ = promote(True)
        assert code == 400 and b"not enough evidence" in out                     # n = 1: never, confirm or not
        good = dict(board, n=45, samples=45, verdict="challenger better", champ=0.5, chall=0.62, diff=0.12, ci=0.04)
        real_board = app.challenge_board
        monkeypatch.setattr(app, "challenge_board", lambda key: good)
        code, out, _ = promote(False)
        assert code == 200 and "over 45 clips" in json.loads(out)["confirm"]    # the evidence, before it applies
        assert app.stt_choices.model_for("FG1", "fire") == QWEN
        before = app.stt_champion_log()
        at_hook = []                                                # (intel told BEFORE the change applies)
        app.stt_champion_hooks.append(lambda r: at_hook.append(app.stt_choices.model_for("FG1", "fire")))
        code, out, _ = promote(True)
        assert code == 200, out
        assert app.stt_choices.model_for("FG1", "fire") == Q8 and app.stt_choices.challenger_for("FG1")[0] == QWEN
        assert at_hook == [QWEN]                                    # (still the old champion when intel heard)
        told = app.stt_champion_log()[len(before):]
        assert told[-1]["why"] == "promote" and told[-1]["new"] == Q8 and told[-1]["by"]
        audit = app.cad.store.q("SELECT key, old, new FROM settings_audit WHERE key LIKE 'stt_models/FG1/%'")
        assert {"key": "stt_models/FG1/model", "old": QWEN, "new": Q8} in audit
        code, raw, _ = api.get("/api/cad/stt-workers", "", Identity(email=CASEY))
        assert "Engine 60 Ladder 31" not in raw.decode()                         # (scores only, never a transcript)
        # the model it replaced does better since: rolled back, intel told first, audited, the admins notified
        monkeypatch.setattr(app, "challenge_board", lambda key: {"n": 40, "diff": 0.2, "ci": 0.05, "champ": 0.4,
                                                                 "chall": 0.6, "champion": Q8, "challenger": QWEN})
        app._challenge_watch("FG1")
        assert app.stt_choices.model_for("FG1", "fire") == QWEN and app.stt_choices.challenger_for("FG1") is None
        assert app.stt_champion_log()[-1]["why"] == "rollback"
        assert app.cad.store.one("SELECT operator FROM settings_audit WHERE key = 'stt_models/FG1/model' "
                                 "ORDER BY id DESC LIMIT 1")["operator"] == "system: automatic rollback"
        assert pushed and pushed[-1][0] == "STT rollback: FG1"
        monkeypatch.setattr(app, "challenge_board", real_board)
    finally:
        w.close()
        hub.close()
        fake.close()


@pytest.mark.parametrize("why", ["model not loaded", "busy: LLM"])
def test_a_challenger_handed_back_is_a_sample_less_never_a_pause(why):
    """A challenger the worker hands back (its model unloaded, the LLM busy) is no failure: never a pause."""
    from types import SimpleNamespace

    from sizeup.cloud.hub import HubApp
    class App:
        CHALLENGE_FAILS, CHALLENGE_PAUSE_S = 1, 60.0
    app = App()
    app.hub = SimpleNamespace(log=print)
    HubApp._challenge_fell(app, SimpleNamespace(channel="FG1", extra={"fell": "released"}), f"GPU worker gpu-1 handed it back: {why}")
    assert not app.__dict__.get("_chall_pause")
    HubApp._challenge_fell(app, SimpleNamespace(channel="FG1", extra={"fell": "lease"}), "no text in time")
    assert app.__dict__["_chall_pause"]["FG1"] > 0                  # (a failure on the worker: paused)


def test_work_only_a_station_takes_never_takes_a_challenger_back():
    _clk, p, jobs, w = _pool()
    st = p.seen("station:north", "online", kind="station")
    st.polling = 1
    assert p.submit(chall(jobs))
    j = p.claim("gpu-a", 0)
    assert p.submit(jobs.job("s1", CATCHUP, bg=True, forced=True, extra={"station_only": True}))
    assert p.watch("gpu-a", j.lease, 0) is True                      # (the GPU isn't wanted for it)


def test_never_on_a_worker_from_before_challengers_nor_on_a_model_live_traffic_decodes_on():
    _clk, p, jobs, w = _pool()
    w.caps = frozenset()                                            # (a worker from before #964: no watch, no greedy)
    assert p.chall_ready("FG1", Q8, 3.0) == (False, "no worker has the model loaded")
    assert p.submit(chall(jobs))
    assert p.claim("gpu-a", 0) is None
    w.caps = frozenset({"challenger"})
    p.resident = frozenset({Q8})                                    # (a champion: its server serves live clips)
    assert p.claim("gpu-a", 0) is None and not p.chall_ready("FG1", Q8, 3.0)[0]
    assert not p.chall_ready("FG1", Q5, 3.0)[0]                     # (the worker's primary: whisper_server's)
    p.resident = frozenset()
    assert p.claim("gpu-a", 0).id == "c1~ch"
