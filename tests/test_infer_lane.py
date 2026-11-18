"""The GPU workers' local LLM lane (sizeup/cloud/infer.py): submit() / available() as the intel engine calls them, the
lane after the pre-alert clips, deadlines, gaming, a worker gone, schema checks, and that no prompt or answer is ever
kept. Every prompt and answer here is invented (synthetic radio traffic, "Example Road")."""

from __future__ import annotations

import json
import threading
import time

import pytest

from sizeup import sttpool
from sizeup.cloud import infer
from sizeup.cloud.infer import Lane, supported, valid
from sizeup.stt import LIVE, URGENT
from tests.test_cloud import HubRun, call, hub_cfg
from tests.test_stt_pool import Clock, Jobs, online, pool

SECRET = "Car 4 respond to 12 Example Road, knife involved"        # a prompt that must never be kept anywhere
ANSWER_SECRET = "12 Example Road, apartment 2"

PD = {"type": "object", "additionalProperties": False,
      "required": ["category", "confidence", "in_progress", "weapon", "deescalation", "address", "reason"],
      "properties": {
          "category": {"enum": ["none", "weapons", "assault", "dispute", "edp", "barricade", "fire_report", "fire_call",
                                "mva_injury", "aided", "hazmat", "water_rescue", "pursuit", "alarm", "theft",
                                "suspicious", "other_incident"]},
          "confidence": {"type": "number", "minimum": 0, "maximum": 1},
          "in_progress": {"type": "boolean"},
          "weapon": {"enum": ["none", "knife", "gun", "other", "unknown"]},
          "deescalation": {"type": "boolean"},
          "address": {"type": ["string", "null"], "maxLength": 80},
          "reason": {"type": "string", "maxLength": 120}}}
GOOD = {"category": "weapons", "confidence": 0.85, "in_progress": True, "weapon": "knife", "deescalation": False,
        "address": ANSWER_SECRET, "reason": "a knife reported at a call in progress"}
MSGS = [{"role": "system", "content": "You classify one police radio transmission. Return JSON."},
        {"role": "user", "content": f"Talkgroup: PD dispatch. Trigger: '{SECRET}'. Context: (none)."}]
MODELS = {"stt": "ggml-large-v3-turbo-q5_0.bin", "llm": "qwen2.5-7b-instruct-q4_k_m", "llm_version": "a1b2c3d4e5f6"}


def lane(clock=None, busy=None, logs=None):
    ln = Lane(stt_busy=busy or (lambda wid: False), clock=clock or Clock(), log=(logs.append if logs is not None
                                                                                 else lambda s: None))
    ln._thread = threading.current_thread()          # "started" without its clock thread: the tests tick themselves
    return ln


def llm_online(ln, wid="gpu-1", models=MODELS):
    return ln.seen(wid, "online", models)


# -- the schema subset -----------------------------------------------------------------------------------------------

def test_the_pd_classify_schema_is_supported_and_checked_field_by_field():
    assert supported(PD)
    assert valid(GOOD, PD) and valid(dict(GOOD, address=None), PD)
    for bad in (dict(GOOD, category="robbery"), dict(GOOD, confidence=1.2), dict(GOOD, confidence=-0.1),
                dict(GOOD, confidence="high"), dict(GOOD, in_progress="yes"), dict(GOOD, weapon="bat"),
                dict(GOOD, address="x" * 81), dict(GOOD, address=4), dict(GOOD, reason="r" * 121),
                dict(GOOD, extra=1), {k: v for k, v in GOOD.items() if k != "reason"}, dict(GOOD, deescalation=0),
                dict(GOOD, confidence=float("nan")), [GOOD], "{}"):
        assert not valid(bad, PD), bad
    assert valid(dict(GOOD, confidence=1), PD)                       # an integer is a number
    assert not supported({"type": "object", "pattern": "x"}) and not supported({"type": "date"})
    assert not supported({"type": "object", "additionalProperties": {"type": "string"}})
    assert supported({"type": "array", "items": {"anyOf": [{"type": "integer"}, {"const": "x"}]}, "maxItems": 3})


# -- the lane on its own ---------------------------------------------------------------------------------------------

def test_with_no_llm_worker_submit_is_none_at_once_and_never_raises():
    ln = lane()
    f = ln.submit("pd_classify", MSGS, PD, meta=True)
    assert f.done() and f.result()[0] is None and f.result()[1]["why"] == "no_worker"
    ln.seen("gpu-1", "online", {"stt": "ggml-large-v3-turbo-q5_0.bin"})      # whisper only: still off
    assert not ln.available() and ln.submit("pd_classify", MSGS, PD).result(2) is None
    assert ln.stats()["workers"] == [] and not ln.any_llm()                # (and not on Health's LLM rows)
    for args in (("Bad Task", MSGS, PD), ("t", "text", PD), ("t", [{"role": "tool", "content": "x"}], PD),
                 ("t", MSGS, {"type": "object", "pattern": "x"}), ("t", MSGS, PD, "soon"), (None, None, None)):
        llm_online(ln)
        f = ln.submit(*args, meta=True)
        assert f.done() and f.result() == (None, f.result()[1]) and f.result()[1]["why"] == "invalid", args
    assert ln._queue == []


def test_the_module_api_without_a_hub_answers_none_at_once():
    infer.uninstall(infer._lane) if infer._lane is not None else None
    assert infer.available() is False
    f = infer.submit("pd_classify", MSGS, PD, deadline_s=15, meta=True)
    assert f.done() and f.result()[0] is None and set(f.result()[1]) == {
        "model", "model_version", "worker", "latency_ms", "tokens_in", "tokens_out", "why"}
    assert infer.submit("pd_classify", MSGS, PD).result(2) is None


def test_a_worker_claims_answers_and_the_future_has_the_dict_and_its_meta():
    ln = lane()
    llm_online(ln)
    assert ln.available()
    f = ln.submit("pd_classify", MSGS, PD, deadline_s=15, meta=True)
    assert not f.done()
    job = ln.claim("gpu-1", 0, "online", MODELS)
    pub = ln.public(job, ln.clock())
    assert pub["task"] == "pd_classify" and pub["messages"] == MSGS and pub["schema"] == PD
    assert pub["max_tokens"] == 256 and pub["deadline_s"] == 15.0 and pub["temperature"] == 0
    assert ln.claim("gpu-1", 0) is None                      # one infer job at a time
    assert ln.complete("gpu-1", job.lease, {"output": GOOD, "tokens_in": 640, "tokens_out": 72, "infer_ms": 900})
    out, meta = f.result(2)
    assert out == GOOD
    assert meta["why"] == "ok" and meta["worker"] == "gpu-1" and meta["model"] == "qwen2.5-7b-instruct-q4_k_m"
    assert meta["model_version"] == "a1b2c3d4e5f6" and meta["tokens_in"] == 640 and meta["tokens_out"] == 72
    assert isinstance(meta["latency_ms"], int)
    assert ln.complete("gpu-1", job.lease, {"output": GOOD}) is False       # once
    f2 = ln.submit("pd_classify", MSGS, PD)
    j2 = ln.claim("gpu-1", 0)
    ln.complete("gpu-1", j2.lease, {"output": json.dumps(dict(GOOD, address=None))})   # a JSON string is fine too
    assert f2.result(2) == dict(GOOD, address=None)


def test_an_answer_that_does_not_validate_is_none_invalid_and_three_cool_the_worker_down():
    clock = Clock()
    ln = lane(clock)
    llm_online(ln)
    for i in range(3):
        f = ln.submit("pd_classify", MSGS, PD, meta=True)
        job = ln.claim("gpu-1", 0)
        assert job is not None
        assert ln.complete("gpu-1", job.lease, {"output": dict(GOOD, category="robbery") if i else "not json"})
        assert f.result(2) == (None, f.result(2)[1]) and f.result(2)[1]["why"] == "invalid"
    assert not ln.available() and ln.stats()["workers"][0]["state"] == "cooling"
    clock.t += infer.COOLDOWN_S
    ln.seen("gpu-1")
    assert ln.available()


def test_the_highest_priority_goes_first_then_the_oldest():
    clock = Clock()
    ln = lane(clock)
    llm_online(ln)
    a = ln.submit("t", MSGS, PD)
    clock.t += 0.1
    b = ln.submit("t", MSGS, PD, priority=5)
    clock.t += 0.1
    c = ln.submit("t", MSGS, PD, priority=5)
    order = []
    for _ in range(3):
        j = ln.claim("gpu-1", 0)
        order.append(j.future)
        ln.complete("gpu-1", j.lease, {"output": GOOD})
    assert order == [b, c, a]


def test_no_infer_job_while_the_worker_holds_a_pre_alert_lease_and_infer_never_touches_the_stt_pool():
    clock = Clock()
    p = pool(clock)
    stt = Jobs()
    ln = lane(clock, busy=p.holds_prealert)
    llm_online(ln)
    online(p)
    p.seen("gpu-1", "online")
    assert p.submit(stt.job("FG1-1", URGENT))
    sj = p.claim("gpu-1", 0)
    assert sj is not None and p.holds_prealert("gpu-1")
    f = ln.submit("t", MSGS, PD, deadline_s=5)
    assert ln.claim("gpu-1", 0) is None                      # the pre-alert clip first
    p.complete("gpu-1", sj.lease, {"text": "x"})
    assert not p.holds_prealert("gpu-1")
    j = ln.claim("gpu-1", 0)
    assert j is not None                                      # and now the LLM
    # an infer lease is no STT lease: the worker is idle to the pool, a pre-alert clip still goes to it at once
    online(p)
    assert p.ready(URGENT)[0] and p.submit(stt.job("FG1-2", URGENT)) and p.claim("gpu-1", 0) is not None
    # the worker's client aborts the generation and hands the job back: leased again while time is left
    assert ln.release("gpu-1", j.lease, "preempted") and not f.done() and ln._queue
    # a non-pre-alert STT lease doesn't hold the LLM back
    p2, ln2 = pool(clock), None
    ln2 = lane(clock, busy=p2.holds_prealert)
    llm_online(ln2)
    online(p2)
    assert p2.submit(stt.job("FG1-3", LIVE)) and p2.claim("gpu-1", 0) is not None
    ln2.submit("t", MSGS, PD)
    assert ln2.claim("gpu-1", 0) is not None


def test_a_job_nobody_answers_by_its_deadline_is_none_deadline():
    clock = Clock()
    ln = lane(clock)
    llm_online(ln)
    f = ln.submit("t", MSGS, PD, deadline_s=3, meta=True)
    clock.t += 2.5
    ln.seen("gpu-1")
    ln.tick()
    assert f.result(2)[1]["why"] == "deadline"                # under MIN_LEFT_S left: not leased any more
    g = ln.submit("t", MSGS, PD, deadline_s=3, meta=True)
    job = ln.claim("gpu-1", 0)
    clock.t += 3
    ln.seen("gpu-1")
    ln.tick()
    assert g.result(2)[1]["why"] == "deadline" and not ln._leases
    assert ln.complete("gpu-1", job.lease, {"output": GOOD}) is False        # late: refused


def test_gaming_takes_the_job_back_and_with_no_one_else_it_is_none_gaming():
    clock = Clock()
    ln = lane(clock)
    llm_online(ln)
    f = ln.submit("t", MSGS, PD, deadline_s=10, meta=True)
    job = ln.claim("gpu-1", 0)
    ln.seen("gpu-1", "gaming", dict(MODELS, llm=""))          # gaming: its llama-server stops too
    assert not ln._leases and ln.complete("gpu-1", job.lease, {"output": GOOD}) is False
    ln.tick()
    assert f.result(2)[1]["why"] == "gaming"
    assert ln.submit("t", MSGS, PD, meta=True).result(2)[1]["why"] == "gaming"
    llm_online(ln)
    assert ln.available()


def test_a_released_job_goes_to_another_worker_within_its_deadline():
    clock = Clock()
    ln = lane(clock)
    llm_online(ln, "gpu-1")
    llm_online(ln, "gpu-2")
    f = ln.submit("t", MSGS, PD, deadline_s=10, meta=True)
    j = ln.claim("gpu-1", 0)
    assert ln.release("gpu-1", j.lease, "gaming")
    j2 = ln.claim("gpu-2", 0)
    assert j2 is not None and j2.future is f
    ln.complete("gpu-2", j2.lease, {"output": GOOD, "tokens_in": 10, "tokens_out": 5})
    assert f.result(2)[0] == GOOD and f.result(2)[1]["worker"] == "gpu-2"


def test_a_worker_unheard_loses_its_lease_and_the_job_is_leased_again():
    clock = Clock()
    ln = lane(clock)
    llm_online(ln, "gpu-1")
    f = ln.submit("t", MSGS, PD, deadline_s=20)
    j = ln.claim("gpu-1", 0)
    clock.t += infer.LOST_S
    llm_online(ln, "gpu-2")
    ln.tick()
    assert not ln._leases and ln._queue == [j]
    j2 = ln.claim("gpu-2", 0)
    ln.complete("gpu-2", j2.lease, {"output": GOOD})
    assert f.result(2) == GOOD
    assert ln.complete("gpu-1", j.lease, {"output": GOOD}) is False


def test_a_long_poll_hands_a_job_submitted_meanwhile_and_stop_answers_what_is_open():
    ln = Lane(log=lambda s: None).start()
    try:
        ln.seen("gpu-1", "online", MODELS)
        got = []
        t = threading.Thread(target=lambda: got.append(ln.claim("gpu-1", 5)))
        t.start()
        time.sleep(0.2)
        f = ln.submit("t", MSGS, PD)
        t.join(5)
        assert got and got[0] is not None and got[0].future is f
        g = ln.submit("t", MSGS, PD, meta=True)
    finally:
        assert ln.stop() == 2
    assert f.result(2) is None and g.result(2)[1]["why"] == "no_worker"
    assert not ln.available()


def test_the_numbers_have_tasks_times_and_counts_and_never_a_prompt_or_an_answer():
    logs = []
    clock = Clock()
    ln = lane(clock, logs=logs)
    llm_online(ln)
    for i in range(4):
        ln.submit("pd_classify", MSGS, PD)
        j = ln.claim("gpu-1", 0)
        ln.complete("gpu-1", j.lease, {"output": GOOD if i else dict(GOOD, weapon="bat"), "tokens_in": 600,
                                       "tokens_out": 80})
    ln.submit("other_task", MSGS, PD)
    j = ln.claim("gpu-1", 0)
    ln.release("gpu-1", j.lease, "error")
    clock.t += 30
    ln.seen("gpu-1")
    ln.tick()
    s = ln.stats()
    assert s["jobs_h"] == 5 and s["ok_h"] == 3 and s["why_h"] == {"ok": 3, "invalid": 1, "deadline": 1}
    assert s["tasks"]["pd_classify"]["n"] == 4 and s["tasks"]["pd_classify"]["tokens_in"] == 600
    assert s["tasks"]["pd_classify"]["why"] == {"invalid": 1} and s["tasks"]["other_task"]["why"] == {"deadline": 1}
    w = s["workers"][0]
    assert w["llm"] == MODELS["llm"] and w["stt"] == MODELS["stt"] and w["jobs_h"] == 4 and w["fails_h"] == 1
    kept = json.dumps(s) + json.dumps(list(ln._done)) + "\n".join(logs)
    for secret in (SECRET, ANSWER_SECRET, "Example Road", "knife", MSGS[0]["content"]):
        assert secret not in kept


# -- the hub: the worker API, end to end -------------------------------------------------------------------------------

def test_the_hub_routes_a_workers_infer_claims_and_the_module_api_answers(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    try:
        assert infer._lane is hub.app.infer_lane and infer.available() is False
        hello = {"version": "test", "model": MODELS["stt"], "state": "online", "models": MODELS}
        assert call(hub.url + "/api/station/stt/hello", "POST", hello, token=token)[0] == 200
        assert infer.available() is True
        f = infer.submit("pd_classify", MSGS, PD, deadline_s=15, meta=True)
        code, out = call(hub.url + "/api/station/stt/infer/claim", "POST",
                         {"wait": 2, "state": "online", "models": MODELS}, token=token)
        assert code == 200 and out["job"]["messages"] == MSGS and out["job"]["schema"] == PD
        lease = out["job"]["lease"]
        assert call(hub.url + "/api/station/stt/infer/claim", "POST", {"wait": 0}, token="sw1.gpu-1." + "x" * 43)[0] == 401
        code, out = call(hub.url + f"/api/station/stt/infer/{lease}/complete", "POST",
                         {"output": GOOD, "tokens_in": 620, "tokens_out": 70, "infer_ms": 800}, token=token)
        assert code == 200 and out == {"ok": True}
        res, meta = f.result(5)
        assert res == GOOD and meta["why"] == "ok" and meta["worker"] == "gpu-1"
        assert call(hub.url + f"/api/station/stt/infer/{lease}/complete", "POST", {"output": GOOD}, token=token)[0] == 409
        # gaming (its LLM stopped): off at once, and what waits is None
        assert call(hub.url + "/api/station/stt/state", "POST",
                    {"state": "gaming", "models": dict(MODELS, llm="")}, token=token)[0] == 200
        assert infer.available() is False
        assert infer.submit("pd_classify", MSGS, PD, meta=True).result(1)[1]["why"] == "gaming"
        health = hub.app.health.model()["stt_pool"]["infer"]
        assert health["tasks"]["pd_classify"]["why"] == {"gaming": 1} and health["tasks"]["pd_classify"]["ok"] == 1 and health["workers"][0]["llm"] == ""
        assert SECRET not in json.dumps(hub.app.health.model())
        # the STT pool's own numbers never count an infer job
        assert hub.app.sttpool.stats()["leased"] == 0 and hub.app.sttpool.submitted == 0
    finally:
        hub.close()
    assert infer._lane is None


def test_a_revoked_workers_job_goes_back_to_the_queue(tmp_path):
    ln = lane()
    llm_online(ln)
    f = ln.submit("t", MSGS, PD, deadline_s=10)
    ln.claim("gpu-1", 0)
    ln.forget("gpu-1")
    assert not ln._leases and len(ln._queue) == 1 and not ln.available()
    ln.tick()
    assert f.result(2) is None


@pytest.mark.parametrize("why", ["preempted", "error", "stopping"])
def test_a_job_handed_back_with_no_time_left_is_none_deadline(why):
    clock = Clock()
    ln = lane(clock)
    llm_online(ln)
    f = ln.submit("t", MSGS, PD, deadline_s=2, meta=True)
    j = ln.claim("gpu-1", 0)
    clock.t += 1.5
    ln.release("gpu-1", j.lease, why)
    assert f.result(2)[1]["why"] == "deadline"


def test_sttpool_holds_prealert_only_for_the_pre_alert_classes():
    clock = Clock()
    p = pool(clock)
    stt = Jobs()
    online(p)
    assert not p.holds_prealert("gpu-1") and not p.holds_prealert("nobody")
    assert p.submit(stt.job("A", LIVE)) and p.claim("gpu-1", 0)
    assert not p.holds_prealert("gpu-1")
    online(p, "gpu-2")
    assert p.submit(stt.job("B", URGENT)) and p.claim("gpu-2", 0)
    assert p.holds_prealert("gpu-2") and sttpool.FAST_CLASSES


# -- review fixes (#733) ---------------------------------------------------------------------------------------------

def api(tmp_path, ln):
    from sizeup.cloud.hubstore import MonitorDb
    from sizeup.cloud.sttworkers import WorkerApi, WorkerRegistry
    reg = WorkerRegistry(MonitorDb(tmp_path / "hub.sqlite"), log=lambda s: None)
    wa = WorkerApi(reg, pool(Clock()), log=lambda s: None)
    wa.infer = ln
    return reg, wa


def post(wa, path, body, token):
    raw = json.dumps(body).encode()
    code, data, _ctype = wa.handle("POST", path, {"Authorization": f"Bearer {token}"}, lambda n: raw)
    return code, json.loads(data)


def test_only_a_gpu_workers_own_token_reaches_the_lane(tmp_path):
    """A station's whisper in the pool (#715: "station:<id>", its station token) never claims a prompt, and its
    requests never register it in the lane."""
    ln = lane()
    reg, wa = api(tmp_path, ln)
    token = reg.add("gpu-1")
    station_token = "st1.made-up-station-token"
    real = reg.resolve
    reg.resolve = lambda bearer: "station:st-north" if bearer == station_token else real(bearer)
    llm_online(ln, "gpu-1")
    ln.submit("pd_classify", MSGS, PD)
    for path, body in (("/api/station/stt/infer/claim", {"wait": 0, "state": "online", "models": MODELS}),
                       ("/api/station/stt/infer/0123456789abcdef/complete", {"output": GOOD})):
        code, out = post(wa, path, body, station_token)
        assert code == 403 and out["reason"] == "not_a_gpu_worker"
    post(wa, "/api/station/stt/state", {"state": "online", "models": MODELS}, station_token)
    assert "station:st-north" not in ln.workers and len(ln._queue) == 1
    code, out = post(wa, "/api/station/stt/infer/claim", {"wait": 0, "state": "online", "models": MODELS}, token)
    assert code == 200 and out["job"]["messages"] == MSGS and out["llm_on"] is True


@pytest.mark.parametrize("output", [
    "deep", "nan-string", "inf-string", "nan-float", "big-string", "big-dict", 7, None, ["x"]])
def test_whatever_a_worker_sends_back_the_future_is_resolved_once(output):
    ln = lane()
    llm_online(ln)
    f = ln.submit("pd_classify", MSGS, PD, meta=True)
    j = ln.claim("gpu-1", 0)
    if output == "deep":
        d = {}
        for _ in range(100000):
            d = {"a": d}
        output = d
    elif output == "nan-string":
        output = json.dumps(GOOD).replace("0.85", "NaN")
    elif output == "inf-string":
        output = json.dumps(GOOD).replace("0.85", "Infinity")
    elif output == "nan-float":
        output = dict(GOOD, confidence=float("nan"))
    elif output == "big-string":
        output = json.dumps(dict(GOOD, reason="r" * 20000))
    elif output == "big-dict":
        output = dict(GOOD, pad="p" * 20000)
    assert ln.complete("gpu-1", j.lease, {"output": output})
    res, meta = f.result(2)
    assert res is None and meta["why"] == "invalid"
    assert ln.complete("gpu-1", j.lease, {"output": GOOD}) is False and len(ln._done) == 1


def test_the_pool_switch_stops_the_lane_and_a_workers_llm_switch_keeps_prompts_off_it(tmp_path):
    on = {"pool": True, "gpu-1": True}
    clock = Clock()
    ln = Lane(clock=clock, log=lambda s: None, enabled=lambda: on["pool"], llm_on=lambda wid: on.get(wid, True))
    ln._thread = threading.current_thread()
    llm_online(ln, "gpu-1")
    assert ln.available()
    on["pool"] = False
    ln.switched()
    assert not ln.available() and ln.submit("t", MSGS, PD).result(2) is None
    assert ln.handle("gpu-1", "/api/station/stt/infer/claim", {"wait": 0})[1] == {"job": None, "llm_on": False}
    on["pool"] = True
    ln.switched()
    llm_online(ln, "gpu-2")
    f = ln.submit("t", MSGS, PD)
    j = ln.claim("gpu-1", 0)
    on["gpu-1"] = False                                      # switched off while it holds a job: back to the queue
    ln.switched()
    ln.tick()
    assert not ln._leases and ln._queue == [j]
    assert ln.claim("gpu-1", 0) is None
    j2 = ln.claim("gpu-2", 0)
    ln.complete("gpu-2", j2.lease, {"output": GOOD})
    assert f.result(2) == GOOD
    assert {w["id"]: w["llm_on"] for w in ln.stats()["workers"]} == {"gpu-1": False, "gpu-2": True}
    # the registry: on by default, the switch audited, shown in the panel
    from sizeup.cloud.hubstore import MonitorDb
    from sizeup.cloud.sttworkers import WorkerError, WorkerRegistry
    reg = WorkerRegistry(MonitorDb(tmp_path / "hub.sqlite"), log=lambda s: None)
    reg.add("gpu-1")
    assert reg.llm_on("gpu-1") and reg.llm_on("never-added")
    notes = []
    assert reg.act({"action": "llm", "id": "gpu-1", "on": False}, "casey", lambda k, o, n: notes.append((k, o, n))) \
        == {"ok": True, "id": "gpu-1", "llm_on": False}
    assert notes == [("stt workers/gpu-1/llm jobs", "on", "off")] and not reg.llm_on("gpu-1")
    assert reg.panel()["workers"][0]["llm_on"] is False
    with pytest.raises(WorkerError):
        reg.act({"action": "llm", "id": "gpu-9", "on": True})
    with pytest.raises(WorkerError):
        reg.act({"action": "llm", "id": "gpu-1", "on": "yes"})


def test_a_submit_racing_stop_is_answered_never_left_waiting():
    ln = lane()
    llm_online(ln)
    ln.available = lambda: True                              # (it was available a moment ago ...)
    ln._stop.set()                                           # (... and stop() ran in between)
    f = ln.submit("t", MSGS, PD, meta=True)
    assert f.done() and f.result()[1]["why"] == "no_worker" and ln._queue == []


def test_futures_resolve_on_the_lanes_result_thread_never_a_request_thread_or_under_a_lock():
    ln = lane()
    llm_online(ln)
    f = ln.submit("t", MSGS, PD)
    seen = []

    def cb(_f):
        free = ln._cv.acquire(blocking=False)                # (an RLock: only another thread holding it says no)
        if free:
            ln._cv.release()
        seen.append((threading.current_thread().name, free))

    f.add_done_callback(cb)
    j = ln.claim("gpu-1", 0)
    ln.complete("gpu-1", j.lease, {"output": GOOD})          # (this thread: a worker's request)
    assert f.result(2) == GOOD
    wait_until(lambda: seen)
    name, got_lock = seen[0]
    assert name.startswith("infer-results") and got_lock


def test_models_without_a_key_leave_it_as_it_was():
    ln = lane()
    llm_online(ln)
    ln.seen("gpu-1", "online", {"stt": "ggml-small.bin"})
    w = ln.workers["gpu-1"]
    assert w.llm == MODELS["llm"] and w.llm_version == MODELS["llm_version"] and w.stt == "ggml-small.bin"
    ln.seen("gpu-1", "online", {"llm": ""})                  # said: no LLM now
    assert w.llm == "" and w.llm_version == "" and not ln.available()


def test_a_job_is_none_at_its_deadline_not_a_tick_later():
    ln = Lane(log=lambda s: None).start()
    try:
        ln.seen("gpu-1", "online", MODELS)
        f = ln.submit("t", MSGS, PD, deadline_s=1.5, meta=True)
        t0 = time.monotonic()
        assert ln.claim("gpu-1", 0) is not None              # leased, never answered
        res, meta = f.result(5)
        took = time.monotonic() - t0
        assert res is None and meta["why"] == "deadline" and 1.45 <= took < 1.6, took
    finally:
        ln.stop()


def wait_until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        time.sleep(0.01)
    assert pred()


# -- re-review follow-ups (#733) -------------------------------------------------------------------------------------

def test_a_slow_hub_sqlite_never_holds_submit_or_a_claim():
    """The switches are read outside the lane's lock (tick, a new worker, an admin's change): with hub.sqlite stuck for
    3 s, submit() and available() answer from what was read last, at once."""
    gate = threading.Event()
    gate.set()

    def slow_on():
        gate.wait()
        return True

    ln = Lane(log=lambda s: None, enabled=slow_on, llm_on=lambda wid: True)
    ln._thread = threading.current_thread()
    llm_online(ln)
    gate.clear()                                             # hub.sqlite's lock held from here
    ticker = threading.Thread(target=lambda: ln.tick(now=ln.clock()), daemon=True)
    ln._switch_at = -1e9
    ticker.start()                                           # the tick waits on it, outside the lane's lock
    time.sleep(0.1)
    t0 = time.monotonic()
    f = ln.submit("t", MSGS, PD)
    assert ln.available() and not f.done()
    assert ln.claim("gpu-1", 0) is not None
    assert time.monotonic() - t0 < 0.2
    gate.set()
    ticker.join(5)


def test_an_admins_switch_holds_at_once_through_the_registry(tmp_path):
    from sizeup.cloud.hubstore import MonitorDb
    from sizeup.cloud.sttworkers import WorkerRegistry
    reg = WorkerRegistry(MonitorDb(tmp_path / "hub.sqlite"), log=lambda s: None)
    reg.add("gpu-1")
    ln = Lane(log=lambda s: None, enabled=lambda: bool(reg.pool_settings()["on"]), llm_on=reg.llm_on)
    ln._thread = threading.current_thread()
    reg.listeners.append(ln.switched)
    llm_online(ln)
    assert ln.available()
    reg.act({"action": "llm", "id": "gpu-1", "on": False})
    assert not ln.available()                                # no second's wait
    reg.act({"action": "llm", "id": "gpu-1", "on": True})
    assert ln.available()
    reg.act({"action": "pool", "on": False})
    assert not ln.available()


def test_an_oversized_answer_is_refused_by_its_size_without_a_look_inside():
    ln = lane()
    llm_online(ln)
    f = ln.submit("t", MSGS, PD, meta=True)
    j = ln.claim("gpu-1", 0)
    looked = []
    real = infer.valid
    try:
        infer.valid = lambda *a: looked.append(1) or real(*a)
        assert ln.handle("gpu-1", f"/api/station/stt/infer/{j.lease}/complete", {"output": GOOD},
                         size=infer.OUTPUT_MAX + 5000) == (200, {"ok": True})
    finally:
        infer.valid = real
    assert f.result(2)[1]["why"] == "invalid" and looked == []


def test_the_answer_is_checked_outside_the_lanes_lock():
    ln = lane()
    llm_online(ln)
    f = ln.submit("t", MSGS, PD)
    j = ln.claim("gpu-1", 0)
    free = []
    real = infer.valid

    def check(*a):
        t = threading.Thread(target=lambda: free.append(ln._cv.acquire(timeout=1) and (ln._cv.release() or True)))
        t.start()
        t.join(2)
        return real(*a)

    try:
        infer.valid = check
        ln.complete("gpu-1", j.lease, {"output": GOOD})
    finally:
        infer.valid = real
    assert free and all(free) and f.result(2) == GOOD


def test_available_and_submit_never_wait_on_the_pools_lock_or_hub_sqlite():
    """The intel engine's calls: with the STT pool's lock and hub.sqlite each held for 3 s (a claim and a tick in
    flight meanwhile), available() and submit() answer in under 50 ms."""
    p = pool(Clock())
    gate = threading.Event()
    gate.set()

    def slow_db():
        gate.wait()
        return True

    ln = Lane(stt_busy=p.holds_prealert, log=lambda s: None, enabled=slow_db, llm_on=lambda wid: slow_db())
    ln.start()
    try:
        ln.seen("gpu-1", "online", MODELS)
        assert ln.available()
        gate.clear()                                         # hub.sqlite held ...
        held = threading.Event()

        def hold_pool():
            with p._cv:                                      # ... and the pool's lock, 3 s each
                held.set()
                time.sleep(3.0)

        threading.Thread(target=hold_pool, daemon=True).start()
        held.wait(2)
        ln._switch_at = -1e9                                 # (the tick now waits on hub.sqlite, outside the lane's lock)
        claimer = threading.Thread(target=lambda: ln.claim("gpu-1", 2), daemon=True)
        claimer.start()                                      # (its STT check waits on the pool's lock)
        time.sleep(0.2)
        for _ in range(5):
            t0 = time.monotonic()
            ok = ln.available()
            f = ln.submit("pd_classify", MSGS, PD, deadline_s=15)
            took = time.monotonic() - t0
            assert ok and not f.done() and took < 0.05, took
        gate.set()
        claimer.join(5)
    finally:
        gate.set()
        ln.stop()
