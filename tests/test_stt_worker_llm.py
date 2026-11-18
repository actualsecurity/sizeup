"""The GPU worker's local LLM (tools/stt_worker/worker.py: llama.cpp's llama-server, the hub's infer lane in
sizeup/cloud/infer.py) against a fake hub and a fake llama-server on 127.0.0.1: the config's loopback rules, the
pinned model, the constrained request, a pre-alert clip first, gaming (abort, hand back, llama-server stopped after a
while), and that no prompt or answer ever reaches a log. Every prompt and answer is invented ("Example Road")."""

from __future__ import annotations

import hashlib
from pathlib import Path
import json
import logging
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

import tools.stt_worker.worker as W
from tests.test_stt_worker import (API, CF_ID, CF_SECRET, TOKEN, FakeHub, FakeProc, FakeSmi, FakeWhisper, _serve,
                                   make_cfg, no_smi, running, tone_wav, wait_for)

PROMPT_SECRET = "Car 4 respond to 12 Example Road, knife involved"
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
        "address": ANSWER_SECRET, "reason": "a knife at a call in progress"}
MSGS = [{"role": "system", "content": "You classify one police radio transmission. Return JSON."},
        {"role": "user", "content": f"Talkgroup: PD dispatch. Trigger: '{PROMPT_SECRET}'. Context: (none)."}]
MODEL = "qwen2.5-7b-instruct-q4_k_m"


class InferHub(FakeHub):
    """FakeHub plus the infer lane's routes as sizeup/cloud/infer.py answers them: claim (a long poll), complete,
    release; 409 for a lease that is gone. infer=False: an older hub (404)."""

    def __init__(self):
        self.infer = True
        self.llm_on = True
        self.infer_pending: list[dict] = []
        self.infer_leases: dict[str, dict] = {}
        super().__init__()

    def add_infer(self, task="pd_classify", messages=MSGS, schema=PD, deadline_s=15.0):
        with self.cv:
            self.infer_pending.append({"task": task, "messages": messages, "schema": schema, "deadline_s": deadline_s})
            self.cv.notify_all()

    def handle(self, req, method, gen):
        if not req.path.startswith(f"{API}/infer/"):
            return super().handle(req, method, gen)
        n = int(req.headers.get("Content-Length") or 0)
        body = json.loads(req.rfile.read(n) or b"{}")
        with self.cv:
            self.requests.append((method, req.path, dict(req.headers), body))
        if req.headers.get("Authorization") != f"Bearer {self.token}":
            return self.send(req, 401, {"error": "unauthorized", "reason": "token_unknown"})
        if not self.infer:
            return self.send(req, 404, {"error": "not found"})
        if req.path == f"{API}/infer/claim":
            if not self.llm_on:
                return self.send(req, 200, {"job": None, "llm_on": False})
            with self.cv:
                self.cv.wait_for(lambda: self.infer_pending or self.gen != gen,
                                 timeout=min(float(body.get("wait", 0)), self.claim_max_wait))
                if not self.infer_pending or self.gen != gen:
                    return self.send(req, 200, {"job": None})
                j = self.infer_pending.pop(0)
                lease = secrets.token_hex(8)
                self.infer_leases[lease] = {"job": j, "closed": False}
            return self.send(req, 200, {"job": {"lease": lease, "id": lease[:8], "task": j["task"],
                                                "messages": j["messages"], "schema": j["schema"], "max_tokens": 256,
                                                "temperature": 0, "deadline_s": j["deadline_s"]}})
        parts = req.path.split("/")
        lease, action = parts[-2], parts[-1]
        with self.cv:
            lv = self.infer_leases.get(lease)
            if lv is None or lv["closed"]:
                return self.send(req, 409, {"error": "lease lost"})
            lv["closed"] = True
            lv[action] = body
        return self.send(req, 200, {"ok": True})

    def infer_done(self, action):
        with self.cv:
            return [lv[action] for lv in self.infer_leases.values() if action in lv]


class FakeLlama:
    """llama-server: /health (503 while "loading"), /v1/chat/completions (its content: `answer` as JSON text); stops
    "generating" when the client goes away (aborted), as llama-server does."""

    def __init__(self):
        self.loading = False
        self.delay = 0.0
        self.fail: int | None = None
        self.answer: object = GOOD
        self.prefix = ""
        self.aborted = 0
        self.requests: list[dict] = []
        llama = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, doc):
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    return self._send(503 if llama.loading else 200,
                                      {"status": "loading model" if llama.loading else "ok"})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                llama.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                end = time.monotonic() + llama.delay
                while time.monotonic() < end:
                    if select.select([self.connection], [], [], 0.01)[0]:
                        try:
                            gone = self.connection.recv(1, socket.MSG_PEEK) == b""
                        except OSError:
                            gone = True
                        if gone:
                            llama.aborted += 1
                            return
                if llama.fail:
                    return self._send(llama.fail, {"error": {"message": "failed"}})
                content = llama.prefix + (llama.answer if isinstance(llama.answer, str) else json.dumps(llama.answer))
                self._send(200, {"choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant", "content": content}}],
                                 "usage": {"prompt_tokens": 612, "completion_tokens": 71, "total_tokens": 683},
                                 "timings": {"prompt_per_second": 3000.0, "predicted_per_second": 90.0}})

        self.server = _serve(H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ihub():
    h = InferHub()
    yield h
    h.stop()


@pytest.fixture
def whisper():
    w = FakeWhisper()
    yield w
    w.stop()


@pytest.fixture
def llama():
    s = FakeLlama()
    yield s
    s.stop()


def llm_cfg(tmp_path, hub, whisper, llama, **kw) -> W.Config:
    return make_cfg(tmp_path, hub, whisper, **{"llm_url": llama.url, "llm_model": MODEL, "llm_check_s": 0.05, **kw})


@pytest.fixture(autouse=True)
def no_prompts_in_logs(caplog):
    """Every test: neither a prompt nor an answer (nor a secret) ever reaches the worker's log (DEBUG)."""
    caplog.set_level(logging.DEBUG, logger="sizeup.stt_worker")
    yield
    for s in (PROMPT_SECRET, ANSWER_SECRET, "Example Road", MSGS[0]["content"], TOKEN, CF_SECRET):
        assert s not in caplog.text


# --- config --------------------------------------------------------------------------------------------------------

def base(**kw):
    return W.Config(hub_url="https://hub.example.org", token=TOKEN,
                    **{"llm_url": "http://127.0.0.1:8180", "llm_model": MODEL, **kw})


@pytest.mark.parametrize("kw, msg", [
    ({"llm_url": "http://192.0.2.10:8180"}, "llama-server on this PC only"),
    ({"llm_url": "https://127.0.0.1:8180"}, "llama-server on this PC only"),
    ({"llm_model": ""}, "llm_model"),
    ({"llm_model_sha256": "abc"}, "64 hex"),
    ({"llm_server": ["llama-server", "-m", "m.gguf", "--host", "0.0.0.0", "--port", "8180"]}, "--host must be"),
    ({"llm_server": ["llama-server", "-m", "m.gguf", "--host=0.0.0.0", "--port", "8180"]}, "--host must be"),
    ({"llm_server": ["llama-server", "-m", "m.gguf", "--host", "127.0.0.1,0.0.0.0", "--port", "8180"]}, "--host must be"),
    ({"llm_server": ["llama-server", "-m", "m.gguf", "--port", "8180", "-v"]}, "-v not allowed"),
    ({"llm_server": ["llama-server", "-m", "m.gguf", "--port", "8180", "--slots"]}, "--slots not allowed"),
    ({"llm_server": ["llama-server", "-m", "m.gguf", "--port", "8180", "--api-key", "k"]}, "--api-key not allowed"),
    ({"llm_server": ["llama-server", "-hf", "org/model", "--port", "8180"]}, "-hf not allowed"),
    ({"llm_server": ["llama-server", "-m", "m.gguf", "--port", "8080"]}, "isn't llm_url's port"),
    ({"llm_server": ["llama-server", "-m", "m.gguf"]}, r"8080 \(its default\)"),
    ({"llm_server": ["llama-server", "--port", "8180"]}, "-m <the GGUF file>"),
])
def test_llama_server_stays_on_loopback_with_a_local_pinned_model_and_quiet_logs(kw, msg):
    with pytest.raises(W.ConfigError, match=msg):
        W.validate(base(**kw))


def test_a_good_llm_config_and_none_at_all_validate():
    W.validate(base(llm_server=["llama-server", "-m", "models/m.gguf", "--host", "127.0.0.1", "--port=8180",
                                "-c", "4096", "-ngl", "99", "--jinja", "--no-webui", "--no-slots"],
                    llm_model_sha256="a" * 64))
    W.validate(W.Config(hub_url="https://hub.example.org", token=TOKEN))          # no LLM: nothing to check
    with pytest.raises(W.ConfigError, match="--host must be"):                       # (whisper-server's --host= too)
        W.validate(W.Config(hub_url="https://hub.example.org", token=TOKEN,
                            whisper_server=["whisper-server", "--host=0.0.0.0"]))


def test_llama_server_gets_no_secrets_no_llama_settings_and_its_own_key(monkeypatch):
    monkeypatch.setenv("SIZEUP_STT_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("SIZEUP_STT_CF_CLIENT_SECRET", CF_SECRET)
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")
    monkeypatch.setenv("LLAMA_ARG_ENDPOINT_SLOTS", "1")
    env = W.llm_env("k" * 32)
    assert env["LLAMA_API_KEY"] == "k" * 32 and "PATH" in env
    assert not any(k.startswith("SIZEUP_STT") or (k.startswith("LLAMA_") and k != "LLAMA_API_KEY") for k in env)


def test_the_workers_schema_check_agrees_with_the_examples():
    assert W.schema_valid(GOOD, PD) and W.schema_valid(dict(GOOD, address=None), PD)
    for bad in (dict(GOOD, category="robbery"), dict(GOOD, confidence=1.5), dict(GOOD, address="x" * 81),
                dict(GOOD, weapon=None), dict(GOOD, extra=1), {k: v for k, v in GOOD.items() if k != "reason"},
                dict(GOOD, in_progress=1), dict(GOOD, confidence=True)):
        assert not W.schema_valid(bad, PD), bad


# --- the job path --------------------------------------------------------------------------------------------------

def test_an_infer_job_goes_to_llama_server_constrained_and_back_with_counts_only(tmp_path, ihub, whisper, llama):
    cfg = llm_cfg(tmp_path, ihub, whisper, llama)
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: any((r[3] or {}).get("models", {}).get("llm") == MODEL for r in ihub.calls("/state")),
                 what="the LLM advertised")
        ihub.add_infer()
        wait_for(lambda: ihub.infer_done("complete"), what="the answer")
    (done,) = ihub.infer_done("complete")
    assert done["output"] == GOOD and done["tokens_in"] == 612 and done["tokens_out"] == 71 and done["infer_ms"] >= 0
    (req,) = [r for r in llama.requests if r["body"]["messages"] == MSGS]
    b = req["body"]
    assert req["path"] == "/v1/chat/completions"
    assert b["response_format"] == {"type": "json_schema",
                                    "json_schema": {"name": "pd_classify", "strict": True, "schema": PD}}
    assert b["temperature"] == 0 and b["max_tokens"] == 256 and b["stream"] is False
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    st = [r[3] for r in ihub.calls("/state")][-1]
    assert st["models"] == {"stt": "large-v3-turbo-q5_0", "llm": MODEL, "llm_version": ""}
    assert w.llm_counts["done"] == 1
    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["llm"]["model"] == MODEL and status["llm"]["jobs"]["done"] == 1
    assert PROMPT_SECRET not in json.dumps(status) and ANSWER_SECRET not in json.dumps(status)


def test_an_answer_outside_the_schema_still_goes_back_at_once_and_a_think_block_is_dropped(tmp_path, ihub, whisper,
                                                                                          llama):
    llama.answer = dict(GOOD, category="robbery")
    llama.prefix = "<think>\n\n</think>\n\n"
    w = W.Worker(llm_cfg(tmp_path, ihub, whisper, llama), run=no_smi)
    with running(w):
        wait_for(lambda: w.llm_ok, what="llama-server ok")
        ihub.add_infer()
        wait_for(lambda: ihub.infer_done("complete"), what="the answer")
        llama.answer, llama.prefix = "not json at all", ""
        ihub.add_infer()
        wait_for(lambda: len(ihub.infer_done("complete")) == 2, what="the second answer")
    a, b = ihub.infer_done("complete")
    assert a["output"]["category"] == "robbery" and b["output"] is None        # the hub says invalid
    assert w.llm_counts["invalid"] == 2


def test_a_pre_alert_clip_preempts_the_generation(tmp_path, ihub, whisper, llama):
    llama.delay = 3.0
    w = W.Worker(llm_cfg(tmp_path, ihub, whisper, llama), run=no_smi)
    with running(w):
        wait_for(lambda: w.llm_ok, what="llama-server ok")
        ihub.add_infer()
        wait_for(lambda: w._llm_call is not None, what="generating")
        t0 = time.monotonic()
        ihub.add_job("FG1-1", tone_wav(1.0), cls="URGENT", lease_s=4.0, fast=True)
        wait_for(lambda: ihub.infer_done("release"), what="the infer job handed back")
        assert time.monotonic() - t0 < 1.5
        wait_for(lambda: ihub.accepted(), what="the clip's text")
    assert ihub.infer_done("release")[0]["why"] == "preempted"
    wait_for(lambda: llama.aborted == 1, what="llama-server saw the client go")
    assert w.llm_counts["released"] == 1


def test_a_slow_clip_does_not_preempt(tmp_path, ihub, whisper, llama):
    llama.delay = 0.6
    w = W.Worker(llm_cfg(tmp_path, ihub, whisper, llama), run=no_smi)
    with running(w):
        wait_for(lambda: w.llm_ok, what="llama-server ok")
        ihub.add_infer()
        wait_for(lambda: w._llm_call is not None, what="generating")
        ihub.add_job("FG1-2", tone_wav(1.0), cls="LOW", lease_s=6.0, fast=False)
        wait_for(lambda: ihub.infer_done("complete"), what="the answer")
    assert not ihub.infer_done("release")


def test_gaming_aborts_hands_back_and_stops_llama_server_after_a_while(tmp_path, ihub, whisper, llama):
    llama.delay = 5.0
    procs = []
    cfg = llm_cfg(tmp_path, ihub, whisper, llama, llm_stop_gaming_s=2.0,
                  llm_server=["llama-server", "-m", "m.gguf", "--port", str(llama.port)])
    w = W.Worker(cfg, run=no_smi, popen=lambda argv, **kw: procs.append(FakeProc(argv, **kw)) or procs[-1])
    with running(w):
        wait_for(lambda: w.llm_ok, what="llama-server ok")      # (the fake answers on its port: adopted, not started)
        assert procs == []
        llama.stop()                                            # it goes away: the worker starts its own
        wait_for(lambda: procs, what="llama-server started")
        env = procs[0].kw["env"]
        assert env["LLAMA_API_KEY"] == w.llm.key and len(w.llm.key) >= 32
        assert w.llm.key not in " ".join(procs[0].argv)
        llama.__init__()                                        # (a fake on another port stands in for it)
        w.llm.port = llama.port
        llama.delay = 5.0
        wait_for(lambda: w.llm_ok, what="llama-server ok again")
        ihub.add_infer()
        wait_for(lambda: w._llm_call is not None, what="generating")
        assert llama.requests[-1]["auth"] == f"Bearer {w.llm.key}"
        t_gaming = time.monotonic()
        W.Control(cfg.control_path).set(gaming=True)
        wait_for(lambda: ihub.infer_done("release"), timeout=2, what="handed back")
        assert ihub.infer_done("release")[0]["why"] == "gaming"
        if time.monotonic() - t_gaming < 1.5:                   # (a loaded machine: only when it can tell)
            assert procs[0].code is None                        # not stopped yet: only after llm_stop_gaming_s
        wait_for(lambda: procs[0].code == 1, what="llama-server stopped after the game went on")
        wait_for(lambda: not w.llm_ok, what="no LLM")
        wait_for(lambda: (ihub.calls("/state")[-1][3] or {}).get("models", {}).get("llm") == "", what="told the hub")
        n = len(procs)
        time.sleep(0.3)
        assert len(procs) == n                                  # not started again while gaming
        llama.stop()                                            # (nothing on its port now: the worker starts one)
        w.llm_proc.started = w._llm_started_at = -1e9           # (not within 10 s of its last start: no throttle)
        W.Control(cfg.control_path).set(gaming=False)
        wait_for(lambda: len(procs) == n + 1, what="started again once the game ended")
    assert "Bearer" not in (tmp_path / "state" / "llama-server.log").read_text(errors="replace")


def test_llama_servers_pid_is_ours_not_a_game(tmp_path, ihub, whisper, llama):
    """Its ~5 GB of VRAM must never look like another process's (a game)."""
    procs = []
    cfg = llm_cfg(tmp_path, ihub, whisper, llama, llm_server=["llama-server", "-m", "m.gguf", "--port", "1"],
                  llm_url="http://127.0.0.1:1")
    w = W.Worker(cfg, run=FakeSmi(gpu="20, 6500, 12282", apps="9100, 5400\n9101, 900"),
                 popen=lambda argv, **kw: procs.append(FakeProc(argv, **kw)) or procs[-1])
    w.llm_proc.ensure(port_alive=False)
    procs[0].pid = 9100
    assert 9100 in w._our_pids()
    w.gpu.sample()
    w.gpu.sample()
    assert not w.gpu.gaming                                     # 5.4 GB of ours, 0.9 GB of another: not a game


def test_a_model_whose_hash_is_not_the_pinned_one_is_never_started(tmp_path, ihub, whisper):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF" + b"\0" * 4096)
    good = hashlib.sha256(gguf.read_bytes()).hexdigest()
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    for sha, started in (("0" * 64, False), (good.upper(), True)):
        procs = []
        cfg = make_cfg(tmp_path, ihub, whisper, llm_url=f"http://127.0.0.1:{port}", llm_model=MODEL,
                       llm_model_sha256=sha, llm_server=["llama-server", "-m", str(gguf), "--port", str(port)])
        w = W.Worker(cfg, run=no_smi, popen=lambda argv, **kw: procs.append(FakeProc(argv, **kw)) or procs[-1])
        with running(w):
            wait_for(lambda: w.llm_verified is not None, what="the hash checked")
            time.sleep(0.3)
        assert bool(procs) is started and w.llm_verified is started
        if not started:
            assert w.llm_err == "model hash mismatch"
    cache = json.loads((tmp_path / "state" / "llm-model.json").read_text())
    assert cache["sha256"] == good and cache["size"] == gguf.stat().st_size
    assert W.file_sha256(gguf, tmp_path / "state" / "llm-model.json") == good   # (from the cache)


def test_an_older_hub_without_the_lane_leaves_the_clips_alone(tmp_path, ihub, whisper, llama):
    ihub.infer = False
    w = W.Worker(llm_cfg(tmp_path, ihub, whisper, llama), run=no_smi)
    with running(w):
        wait_for(lambda: ihub.calls("/infer/claim"), what="an infer claim")
        ihub.add_job("FG1-3", tone_wav(1.0))
        wait_for(lambda: ihub.accepted(), what="the clip's text")
        time.sleep(0.2)
        assert len(ihub.calls("/infer/claim")) == 1               # asked again only in 10 min
    assert w._llm_until > w.clock()


def test_llama_server_failing_hands_the_job_back_and_three_in_a_row_stop_the_llm(tmp_path, ihub, whisper, llama):
    llama.fail = 500
    w = W.Worker(llm_cfg(tmp_path, ihub, whisper, llama, llm_check_s=30.0), run=no_smi)
    with running(w):
        wait_for(lambda: w.llm_ok, what="llama-server ok")
        for i in range(3):
            ihub.add_infer()
            wait_for(lambda: len(ihub.infer_done("release")) == i + 1, what="handed back")
        wait_for(lambda: not w.llm_ok, what="no LLM after 3 failures")
    assert [r["why"] for r in ihub.infer_done("release")] == ["error"] * 3 and w.llm_err == "3 failures in a row"


def test_no_llm_without_its_health(tmp_path, ihub, whisper, llama):
    llama.loading = True
    w = W.Worker(llm_cfg(tmp_path, ihub, whisper, llama), run=no_smi)
    with running(w):
        wait_for(lambda: ihub.calls("/state"), what="a heartbeat")
        time.sleep(0.3)
        assert not w.llm_ok and w.llm_err == "loading" and not ihub.calls("/infer/claim")
        assert ihub.calls("/state")[-1][3]["models"]["llm"] == ""
        llama.loading = False
        wait_for(lambda: ihub.calls("/infer/claim"), what="claims once it answers")


def test_check_tries_a_constrained_answer(tmp_path, ihub, whisper, llama, capsys):
    llama.answer = {"ok": True}
    cfg = llm_cfg(tmp_path, ihub, whisper, llama)
    assert W._check(cfg) == 0
    out = capsys.readouterr().out
    assert "llama-server: ok" in out
    assert llama.requests[-1]["body"]["response_format"]["json_schema"]["schema"]["required"] == ["ok"]
    llama.answer = {"ok": "yes"}
    assert W._check(cfg) == 1 and "didn't follow the schema" in capsys.readouterr().out


def test_llm_jobs_switched_off_on_the_hub_the_worker_asks_again_a_minute_later(tmp_path, ihub, whisper, llama):
    ihub.llm_on = False
    w = W.Worker(llm_cfg(tmp_path, ihub, whisper, llama), run=no_smi)
    with running(w):
        wait_for(lambda: ihub.calls("/infer/claim"), what="an infer claim")
        time.sleep(0.3)
        assert len(ihub.calls("/infer/claim")) == 1 and w.llm_on is False
        ihub.add_job("FG1-4", tone_wav(1.0))
        wait_for(lambda: ihub.accepted(), what="the clip's text")       # clips as ever
    assert 55 < w._llm_until - w.clock() <= 60


def test_the_readme_pins_both_model_choices_and_the_example_config_uses_the_default():
    """Switching the model is config only: both GGUFs are pinned (revision, size, SHA256) in the README, and the
    example worker.toml loads Qwen3-8B with its own hash."""
    import re
    readme = (Path(W.__file__).parent / "README.md").read_text()
    pins = {"Qwen3-8B-Q4_K_M.gguf": ("Qwen/Qwen3-8B-GGUF/resolve/7c41481f57cb95916b40956ab2f0b139b296d974",
                                     "5,027,783,488", "D98CDCBD03E17CE47681435B5150E34C1417F50B5C0019DD560E4882C5745785"),
            "google_gemma-3-12b-it-Q4_K_M.gguf": (
                "bartowski/google_gemma-3-12b-it-GGUF/resolve/648e3a36a77c8a9f12d86e741f9dcb9089c769c4",
                "7,300,575,264", "FC57F67EFA46D711C346E587CBEF7D049E95F3DF8DB2EB2271153343EF0ACC7B")}
    for name, (url, size, sha) in pins.items():
        assert f"{url}/{name}" in readme and size in readme
        assert f'(Get-FileHash "$d\\models\\{name}").Hash -eq \'{sha}\'' in readme
    (example,) = re.findall(r"```toml\n(.*?)```", readme, re.S)
    assert 'llm_model_sha256 = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"' in example
    assert "models\\Qwen3-8B-Q4_K_M.gguf" in example and "'--reasoning', 'off'" in example
