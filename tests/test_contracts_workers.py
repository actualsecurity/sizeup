"""contracts/ (ADR 0004): a pool worker's hello and state (POST /api/station/stt/hello, /state), as built. The real GPU
worker client (tools/stt_worker/worker.py, its nvidia-smi a fake as in tests/test_stt_worker.py) and a real station's
whisper (sizeup/cloud/stationworker.py) post to a real hub over HTTP; what went over the wire is checked against
contracts/schemas/, and each reader is sent keys it does not know. The other routes of the workers' API (claim, the
job's audio, extend, complete, release, the LLM lane) have no schema yet. Every id and name here is made up."""
from __future__ import annotations

import json

import tools.stt_worker.worker as W
from sizeup import sttpool
from sizeup.cloud import stationworker
from tests import contracts as c
from tests.test_app import wait
from tests.test_contracts_station_state import conforms
from tests.test_stt_worker import FakeSmi

API = "/api/station/stt/"
SCHEMA = {"hello": ("stt-worker-hello", "stt-worker-hello-answer"), "state": ("stt-worker-state", "stt-worker-state-answer")}


def gpu_worker(lv, tmp_path) -> W.Worker:
    """The worker client as on the GPU box, pointed at this hub. Not started: the tests call hello() and heartbeat(),
    what its threads call."""
    cfg = W.Config(hub_url=lv.url, token=lv.worker, whisper_url="http://127.0.0.1:9", state_dir=str(tmp_path / "worker"),
                   log_file="-")
    return W.Worker(cfg, run=FakeSmi())


def seen(lv, what: str) -> list[tuple[dict, dict]]:
    """Every (request, answer) of the workers' hello or state the hub handled, each conforming."""
    out = []
    for path, raw, code, answer in lv.worker_seen:
        if path == API + what:
            assert code == 200, (path, answer)
            out.append((conforms(SCHEMA[what][0], raw), conforms(SCHEMA[what][1], answer)))
    return out


def test_the_gpu_worker_says_hello_and_its_state_as_the_schemas_say(tmp_path):
    lv = c.Live(tmp_path)
    try:
        w = gpu_worker(lv, tmp_path)
        w.hello()                                    # before whisper-server answered and before nvidia-smi was read
        w.heartbeat()
        w.gpu.sample()
        w.whisper_ok = True
        w.hello()
        w.heartbeat()
        hello, state = seen(lv, "hello"), seen(lv, "state")
        assert [(r["state"], "gpu" in r) for r, _ in hello] == [("paused", False), ("online", True)]
        assert [(r["state"], "reason" in r, "gpu" in r) for r, _ in state] == [("paused", True, False),
                                                                               ("online", False, True)]
        assert all(r["worker"] == c.WORKER == a["worker"] for r, a in hello)
        assert hello[0][1]["classes"] == [sttpool.class_key(x) for x in sttpool.BUDGETS] and hello[0][1]["poll_s"] == 25.0
        # the hello with the designed node fields absent is a valid designed hello (docs/nodes.md)
        assert all(c.errors("node-worker-hello", r) == [] for r, _ in hello)
        pool = lv.hub.app.sttpool.workers[c.WORKER]
        assert (pool.state, pool.kind, pool.gpu) == ("online", "gpu", hello[1][0]["gpu"]) and w.pool_on is True
    finally:
        lv.close()


def test_a_stations_whisper_says_its_state_as_the_schema_says(tmp_path, monkeypatch):
    """The other writer of the state post: a station's whisper in the pool, with its load. (It says `paused` here:
    the test's whisper never comes up. Its claims have no schema yet.)"""
    monkeypatch.setattr(stationworker, "OFF_POLL_S", 0.05)
    lv = c.Live(tmp_path)
    lv.hub.app.hub.workers.set_pool(stations={c.STATION: True})
    try:
        st = lv.station()
        assert wait(lambda: any(p == API + "state" for p, *_ in lv.worker_seen), 30)
        req, ans = seen(lv, "state")[0]
        assert req["state"] == "paused" and req["load"]["why"] == "whisper not ready" and req["load"]["max_s"] == 8.0
        assert "version" not in req and ans == {"ok": True, "pool_on": True}
        assert lv.hub.app.sttpool.workers[f"station:{c.STATION}"].kind == "station"
        assert st.link.pool_worker.stats["state"] == "paused"
    finally:
        lv.close()


def test_the_hub_ignores_keys_and_words_it_does_not_know_from_a_worker(tmp_path):
    """The hub as the reader: every valid hello and state, with a key from a newer build in every object, a state it
    has never heard of, and the designed capabilities (a newer worker), is taken like today's."""
    lv = c.Live(tmp_path)
    try:
        for what, name in (("hello", "stt-worker-hello"), ("hello", "node-worker-hello"), ("state", "stt-worker-state")):
            for file, body in c.vectors(name, "valid"):
                body, n = c.with_unknown_keys(name, body)
                assert n >= 1 and c.errors(name, body) == [], (name, file)
                code, ans = lv.call("POST", API + what, body, token="sw1")
                assert code == 200, (name, file, ans)
                conforms(SCHEMA[what][1], ans)
        pool = lv.hub.app.sttpool.workers[c.WORKER]
        lv.call("POST", API + "state", {"state": "online", "model": "m"}, token="sw1")
        code, _ = lv.call("POST", API + "state", {"state": "sleeping", "model": "m2", "from_the_future": 1}, token="sw1")
        assert code == 200 and (pool.state, pool.model) == ("online", "m2")     # the word it does not know: unchanged
        assert "sleeping" not in c.load("stt-worker-state")["properties"]["state"]["x-known"]
        assert c.load("stt-worker-state")["properties"]["state"]["x-known"] == list(sttpool.STATES)
    finally:
        lv.close()


def test_the_worker_ignores_keys_it_does_not_know_in_the_answers(tmp_path):
    """The worker client as the reader: a newer hub's answers (keys it has never heard of, a class it does not know)."""
    lv = c.Live(tmp_path, worker_answer_with={"from_the_future": {"x": 1}, "roles_rev": "5d41402abc4b2a76",
                                              "classes": ["live", "from_the_future"]})
    try:
        w = gpu_worker(lv, tmp_path)
        w.hello()
        w.heartbeat()
        assert w.connected and w.link == "ok" and w.pool_on is True and w.claim_wait == min(w.cfg.claim_wait_s, 25.0)
        assert all(code == 200 and json.loads(a)["from_the_future"] for _, _, code, a in lv.worker_seen)
        assert c.errors("stt-worker-hello-answer", json.loads(lv.worker_seen[0][3])) == []
    finally:
        lv.close()


def test_the_pool_classes_in_the_hello_answer_are_the_codes():
    known = c.load("stt-worker-hello-answer")["properties"]["classes"]["items"]["x-known"]
    assert sorted(known) == sorted(sttpool.class_key(x) for x in sttpool.CLASSES)
