"""The STT worker routes speak strict JSON (RFC 8259) both ways, as the station's do (#779): a worker's NaN / Infinity /
1e999 reaches the hub as null and never breaks a claim or a complete; the hub never answers a bare NaN; the worker
(tools/stt_worker/worker.py) never sends one. The LLM lane keeps reading its answers strictly: a NaN in a model's
output still makes the answer invalid. Invented ids and texts only."""

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

from sizeup.cloud import client, sttworkers
from tests.test_cloud import HubRun, hub_cfg
from tests.test_stt_pool import FakeWorker, pending_event, row, send
from tests.test_app import wait

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "stt_worker"))
import worker as wk  # noqa: E402


def raw_post(url, path, raw: bytes, token):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url + path, data=raw, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_client_loads_reads_every_non_finite_number_as_none():
    got = client.loads(b'{"a": NaN, "b": Infinity, "c": -Infinity, "d": 1e999, "e": -1e999, "f": [1.5, NaN, {"g": 2e400}]}')
    assert got == {"a": None, "b": None, "c": None, "d": None, "e": None, "f": [1.5, None, {"g": None}]}
    assert client.loads(b'{"x": 0.25, "n": 3}') == {"x": 0.25, "n": 3}


def test_the_hub_never_answers_a_bare_nan_to_a_worker():
    code, body, ctype = sttworkers._json(200, {"x": float("nan"), "y": [float("inf"), {"z": -float("inf")}], "ok": 1.5})
    assert b"NaN" not in body and b"Infinity" not in body
    assert json.loads(body, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c))) == \
        {"x": None, "y": [None, {"z": None}], "ok": 1.5}


def test_a_worker_sending_nan_reaches_the_hub_as_null_and_its_job_goes_through(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, mode="vanish", wait_s=20.0).start()      # (it claims and fetches; we answer by hand)
    try:
        # a hello / state with non-finite numbers (an older or a careless worker): taken, nothing NaN kept
        code, body = raw_post(hub.url, "/api/station/stt/state", b'{"state": "online", "gpu": {"util_pct": NaN, '
                              b'"mem_used_mb": 1e999, "mem_total_mb": 12288}}', token)
        assert code == 200 and b"NaN" not in body
        g = hub.app.sttpool.workers["gpu-1"].gpu
        assert all(not (isinstance(v, float) and not math.isfinite(v)) for v in g.values()) and g["mem_total_mb"] == 12288
        assert wait(lambda: hub.app.sttpool.ready(-2)[0], 5)
        send(hub, pending_event("FG1-31"))
        assert wait(lambda: w.jobs, 10)
        lease = w.jobs[0]["lease"]
        raw = (b'{"text": "Engine 99 on location", "model": "ggml-large-v3-turbo.bin", "timing": {"infer_ms": NaN, '
               b'"fetch_ms": 1e999}, "segments": [{"start": 0, "end": Infinity, "text": "Engine 99 on location", '
               b'"words": [{"word": "Engine", "probability": NaN}, {"word": "99", "probability": 0.75}]}]}')
        code, body = raw_post(hub.url, f"/api/station/stt/jobs/{lease}/complete", raw, token)
        assert code == 200 and json.loads(body) == {"ok": True}
        assert wait(lambda: row(hub, "FG1-31").get("text") == "Engine 99 on location", 10)
        assert wait(lambda: hub.app.stt_runs.of("FG1-31"), 5)
        (run,) = hub.app.stt_runs.of("FG1-31")
        assert run["confidence"] == 0.75 and [x[1] for x in run["words"]] == [None, 0.75]
    finally:
        w.close()
        hub.close()


def test_the_llm_lane_still_reads_a_nan_in_an_answer_as_invalid():
    seen = {}

    class Lane:
        def handle(self, wid, path, d):
            seen[path] = d
            return 200, {"ok": True}
    reg = SimpleNamespace(resolve=lambda b: "gpu-1", touch=lambda w: None, row=lambda w: None)
    api = sttworkers.WorkerApi(reg, SimpleNamespace(seen=lambda *a, **k: None), log=lambda m: None)
    api.infer = Lane()
    body = b'{"output": {"label": "fire", "score": NaN}}'
    headers = {"Authorization": "Bearer sw1.gpu-1." + "A" * 43}
    code, out, _ = api.handle("POST", sttworkers.API + "infer/jobs/0123456789abcdef/complete", headers,
                              lambda n: body)
    got = seen[sttworkers.API + "infer/jobs/0123456789abcdef/complete"]["output"]["score"]
    assert isinstance(got, float) and math.isnan(got)          # left as NaN: the lane's own check makes it invalid
    from sizeup.cloud.infer import Lane as RealLane
    assert RealLane._answer({"output": {"label": "fire", "score": got}}, {"type": "object"}) is None
    assert RealLane._answer({"output": '{"label": "fire", "score": NaN}'}, {"type": "object"}) is None


def test_the_worker_never_sends_a_bare_nan_nested_or_not():
    body = {"text": "x", "timing": {"infer_ms": float("nan"), "fetch_ms": 12}, "gpu": {"util_pct": float("inf")},
            "segments": [{"start": 0.0, "end": float("-inf"), "words": [{"probability": float("nan")}, (1.0, float("nan"))]}]}
    raw = wk.dumps(body)
    assert b"NaN" not in raw and b"Infinity" not in raw
    assert json.loads(raw) == {"text": "x", "timing": {"infer_ms": None, "fetch_ms": 12}, "gpu": {"util_pct": None},
                               "segments": [{"start": 0.0, "end": None, "words": [{"probability": None}, [1.0, None]]}]}
    assert wk.dumps({"ok": True, "n": [1, 2.5, "a"]}) == b'{"ok":true,"n":[1,2.5,"a"]}'
