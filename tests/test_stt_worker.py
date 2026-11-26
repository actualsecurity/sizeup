"""The GPU STT worker (deploy/stt-worker/worker.py, #639) against a fake hub and a fake whisper-server, both on
127.0.0.1. The hub side follows the worker API in #639: hello / state / claim (long poll) / audio / extend /
complete / release, leases that expire, 401 with a reason, 503 with Retry-After. The whisper-server answer has the
shape of whisper.cpp's server.cpp verbose_json (task, language, duration, text, segments with tokens, words with
probability and t_dtw, temperature, avg_logprob, no_speech_prob). Audio is a generated tone; ids, tokens and the hub
URL are made up. Waits are short: intervals are configured down to tens of ms and backoff sleeps are recorded, not
slept."""

from __future__ import annotations

import ast
import os
import contextlib
import email.parser
import hashlib
import email.policy
import io
import json
import logging
import math
import secrets
import select
import socket
import socketserver
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.stt_worker_module import W

WORKER_PY = Path(W.__file__)
SECRET = secrets.token_urlsafe(32)                 # a fresh one per run: the leak checks look for it
TOKEN = f"sw1.gpu-test.{SECRET}"
CF_ID = "made-up-client-id.access"
CF_SECRET = secrets.token_hex(32)
API = "/api/station/stt"

# whisper.cpp server.cpp's verbose_json, field for field (the text is made up)
VERBOSE = {
    "task": "transcribe", "language": "en", "duration": 1.5, "text": " Radio check,  test one.",
    "detected_language": "english", "detected_language_probability": 0.98, "language_probabilities": {"en": 0.98},
    "segments": [{
        "id": 0, "text": " Radio check, test one.", "start": 0.0, "end": 1.5,
        "tokens": [50365, 17206, 1520, 11, 1500, 472, 13],
        "words": [
            {"word": " Radio", "start": 0.0, "end": 0.42, "t_dtw": -1, "probability": 0.93},
            {"word": " check", "start": 0.42, "end": 0.8, "t_dtw": -1, "probability": 0.88},
            {"word": ",", "start": 0.8, "end": 0.8, "t_dtw": -1, "probability": 0.61},
            {"word": " test", "start": 0.8, "end": 1.1, "t_dtw": -1, "probability": 0.9},
            {"word": " one", "start": 1.1, "end": 1.38, "t_dtw": -1, "probability": 0.95},
            {"word": ".", "start": 1.38, "end": 1.5, "t_dtw": -1, "probability": 0.7}],
        "temperature": 0.0, "avg_logprob": -0.21, "no_speech_prob": 0.01}],
}


def tone_wav(seconds: float = 1.0, hz: float = 440.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate)))
                               for i in range(int(rate * seconds))))
    return buf.getvalue()


def wait_for(pred, timeout: float = 5.0, what: str = "") -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what or pred}")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):                       # HTTPServer's getfqdn() can take a second on a Mac
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def _serve(handler_cls, port: int = 0):
    srv = _Server(("127.0.0.1", port), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


class FakeHub:
    """The hub's worker API (#639) with in-memory jobs and leases, answering as sizeup/cloud/sttworkers.py WorkerApi
    does: extend gives the lease's remaining time (never past the cap) and cap_at; release, complete and extend on a
    lease that is gone are 409; the audio is 409 for a lost lease and 410 when the hub has no audio for the job."""

    def __init__(self, token: str = TOKEN):
        self.token = token
        self.cv = threading.Condition()
        self.pending: list[dict] = []
        self.leases: dict[str, dict] = {}
        self.requests: list[tuple] = []          # (method, path, headers, body)
        self.auth_reason = ""                    # set: every request gets 401 with it
        self.fail_503, self.retry_after = 0, "7"
        self.claim_max_wait = 0.2
        self.cap_s = 30.0
        self.fail_complete: list[int] = []      # statuses the next completes get
        self.claim_answer = None                 # a claim answer to send once instead (a broken hub)
        self.takeback_on_hold = False            # as #651: a worker gone gaming / paused loses its pre-alert jobs
        self.conns: set = set()
        self.gen = 0                             # bumped by stop(): a long poll of the old server takes no job
        self.port = 0
        self.server = None
        self.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        hub, gen = self, self.gen               # this server's generation: once stopped, its handlers take no job

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                hub.conns.add(self.connection)

            def log_message(self, *a):
                pass

            def do_GET(self):
                hub.handle(self, "GET", gen)

            def do_POST(self):
                hub.handle(self, "POST", gen)

        self.server = _serve(H, self.port)
        self.port = self.server.server_address[1]

    def stop(self) -> None:
        with self.cv:
            self.gen += 1
            self.cv.notify_all()
        self.server.shutdown()
        self.server.server_close()
        for c in list(self.conns):           # kept-alive connections: gone too, as when the hub goes away
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
                c.close()
        self.conns.clear()

    def add_job(self, clip_id: str, wav: bytes | None, cls: str = "URGENT", lease_s: float = 6.0, prompt: str = "",
                fast: bool | None = None, audio: str | None = None) -> None:
        with self.cv:
            self.pending.append({"id": clip_id, "wav": wav, "class": cls, "lease_s": lease_s, "prompt": prompt,
                                 "fast": fast, "audio": audio})
            self.cv.notify_all()

    def calls(self, route: str) -> list:
        with self.cv:
            return [r for r in self.requests if r[1].endswith(route)]

    def expire_all(self) -> None:
        with self.cv:
            for lease in self.leases.values():
                lease["expires"] = 0.0

    def _live(self, lease: str) -> dict | None:
        lv = self.leases.get(lease)
        return lv if lv and not lv["closed"] and time.monotonic() < lv["expires"] else None

    def send(self, req, status: int, doc, headers: dict | None = None) -> None:
        data, ctype = (doc, "audio/wav") if isinstance(doc, bytes) else (json.dumps(doc).encode(), "application/json")
        try:
            req.send_response(status)
            req.send_header("Content-Type", ctype)
            req.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                req.send_header(k, v)
            req.end_headers()
            req.wfile.write(data)
        except OSError:
            pass

    def handle(self, req, method: str, gen: int) -> None:
        n = int(req.headers.get("Content-Length") or 0)
        raw = req.rfile.read(n) if n else b""
        body = json.loads(raw) if raw else None
        path = req.path
        with self.cv:
            self.requests.append((method, path, dict(req.headers), body))
        if self.fail_503 > 0:
            self.fail_503 -= 1
            return self.send(req, 503, {"error": "standby"}, {"Retry-After": self.retry_after})
        if req.headers.get("Authorization") != f"Bearer {self.token}" or self.auth_reason:
            return self.send(req, 401, {"error": "unauthorized", "reason": self.auth_reason or "token_unknown"})
        if path == f"{API}/hello":
            return self.send(req, 200, {"ok": True, "worker": "gpu-test", "poll_s": 25,
                                        "classes": ["URGENT", "HIGH", "LIVE", "CATCHUP"]})
        if path == f"{API}/state":
            if self.takeback_on_hold and body.get("state") != "online":
                with self.cv:
                    for lv in self.leases.values():
                        lv["closed"] = True
            return self.send(req, 200, {"ok": True, "pool_on": True})
        if path == f"{API}/claim":
            if self.claim_answer is not None:
                answer, self.claim_answer = self.claim_answer, None
                return self.send(req, 200, answer)
            if body.get("state") != "online":
                return self.send(req, 200, {"job": None})
            with self.cv:
                self.cv.wait_for(lambda: self.pending or self.gen != gen,
                                 timeout=min(float(body.get("wait", 0)), self.claim_max_wait))
                if not self.pending or self.gen != gen:
                    job = None
                else:
                    j = self.pending.pop(0)
                    lease = secrets.token_hex(8)
                    now = time.monotonic()
                    self.leases[lease] = {"job": j, "expires": now + j["lease_s"], "cap": now + self.cap_s,
                                          "closed": False}
                    job = {"lease": lease, "id": j["id"], "kind": "traffic", "channel": "test-channel",
                           "class": j["class"], "prompt": j["prompt"], "seconds": round(len(j["wav"] or b"") / 32000, 2),
                           "lease_s": j["lease_s"], "cap_at": round(time.time() + self.cap_s, 3),
                           "audio": j["audio"] or f"{API}/jobs/{lease}/audio"}
                    if j["fast"] is not None:
                        job["fast"] = j["fast"]
            return self.send(req, 200, {"job": job})
        parts = path.split("/")
        if len(parts) == 7 and path.startswith(f"{API}/jobs/"):
            lease, action = parts[5], parts[6]
            with self.cv:
                live = self._live(lease)
                if action == "audio" and method == "GET":
                    if not live:
                        return self.send(req, 409, {"error": "lease lost"})
                    if not live["job"]["wav"]:
                        live["closed"] = True      # the hub gives it to the next engine
                        return self.send(req, 410, {"error": "no audio for this job: it went to another engine"})
                    return self.send(req, 200, live["job"]["wav"])
                if action == "extend":
                    now = time.monotonic()
                    if not live:
                        return self.send(req, 409, {"error": "lease lost"})
                    live["expires"] = min(now + live["job"]["lease_s"], live["cap"])
                    return self.send(req, 200, {"ok": True, "lease_s": round(max(0.0, live["expires"] - now), 1),
                                                "cap_at": round(time.time() + live["cap"] - now, 3)})
                if action == "complete":
                    if self.fail_complete:
                        status = self.fail_complete.pop(0)
                        return self.send(req, status, {"error": "nope"}, {"Retry-After": self.retry_after})
                    if not live:
                        return self.send(req, 409, {"error": "lease lost: the job went to another engine"})
                    live["closed"] = True
                    live["result"] = body
                    return self.send(req, 200, {"ok": True})
                if action == "release":
                    if not live:
                        return self.send(req, 409, {"error": "lease lost"})
                    live["closed"] = True
                    return self.send(req, 200, {"ok": True})
        return self.send(req, 404, {"error": "not found"})

    def accepted(self) -> list[dict]:
        with self.cv:
            return [lv["result"] for lv in self.leases.values() if "result" in lv]


class FakeWhisper:
    """whisper-server's /inference."""

    def __init__(self):
        self.delay = 0.0
        self.fail: int | None = None
        self.before = None                       # called before answering (a test's hook)
        self.aborted = 0                         # requests whose client went away while "decoding"
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        whisper = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                    f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + raw)
                fields = {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    fields[name] = part.get_payload(decode=True)
                    if name == "file":
                        fields["filename"] = part.get_filename()
                with whisper.lock:
                    whisper.requests.append({"path": self.path, "fields": fields})
                if whisper.before:
                    whisper.before(fields)
                health = len(fields.get("file", b"")) == len(W.silence_wav(1.0))
                end = time.monotonic() + (0.0 if health else whisper.delay)   # the delay: a job's decoding
                while time.monotonic() < end:    # whisper-server stops decoding when the client goes away
                    if select.select([self.connection], [], [], 0.01)[0]:
                        try:
                            gone = self.connection.recv(1, socket.MSG_PEEK) == b""
                        except OSError:
                            gone = True
                        if gone:
                            with whisper.lock:
                                whisper.aborted += 1
                            return
                status, doc = (whisper.fail, {"error": "failed"}) if whisper.fail else (200, VERBOSE)
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = _serve(H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/inference"

    def jobs(self) -> list[dict]:
        """The requests that carried a job's audio (not the 1 s silence health checks)."""
        with self.lock:
            return [r for r in self.requests if len(r["fields"].get("file", b"")) != len(W.silence_wav(1.0))]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeSmi:
    """nvidia-smi --format=csv,noheader,nounits; any other command (pgrep, tasklist) isn't installed."""

    def __init__(self, gpu: str = "3, 1210, 12282", apps: str = ""):
        self.gpu, self.apps = gpu, apps

    def __call__(self, argv: list) -> str:
        if argv[0] != "nvidia-smi":
            raise FileNotFoundError(argv[0])
        return (self.gpu if argv[1].startswith("--query-gpu") else self.apps) + "\n"


def no_smi(argv: list) -> str:
    raise FileNotFoundError(argv[0])


@pytest.fixture(autouse=True)
def no_secrets_in_logs(caplog):
    """Every test: the worker logs at DEBUG and neither the token nor the Cloudflare secret ever shows up."""
    caplog.set_level(logging.DEBUG, logger="sizeup.stt_worker")
    yield
    for s in (TOKEN, SECRET, CF_SECRET):
        assert s not in caplog.text


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    with contextlib.suppress(Exception):
        h.stop()


@pytest.fixture
def whisper():
    w = FakeWhisper()
    yield w
    with contextlib.suppress(Exception):
        w.stop()


def make_cfg(tmp_path, hub, whisper, **kw) -> W.Config:
    cfg = W.Config(hub_url=hub.url, token=TOKEN, cf_client_id=CF_ID, cf_client_secret=CF_SECRET,
                   whisper_url=whisper.url, state_dir=str(tmp_path / "state"), log_file="-", heartbeat_s=0.05,
                   claim_wait_s=0.2, tick_s=0.02, whisper_check_s=0.1, gpu_poll_s=0.05, gaming_clear_s=0.3,
                   busy_grace_s=0.05)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class Sleeps(list):
    """The worker's backoff / refused waits: recorded, then only a moment really slept."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)
        time.sleep(0.01)


@contextlib.contextmanager
def running(worker: W.Worker):
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    try:
        yield worker
    finally:
        worker.stop()
        t.join(5)
        assert not t.is_alive(), "the worker didn't stop"


def states(hub: FakeHub) -> list[str]:
    return [r[3]["state"] for r in hub.calls("/state")]


# --- the job path --------------------------------------------------------------------------------------------------

def test_claim_fetch_whisper_complete(tmp_path, hub, whisper):
    wav = tone_wav(1.5)
    hub.add_job("clip-1", wav, prompt="radio check")
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: hub.accepted(), what="a completed job")
        wait_for(lambda: len(hub.calls("/state")) >= 3, what="heartbeats")
    result = hub.accepted()[0]
    assert result["text"] == "Radio check, test one."          # whitespace folded as the station's WhisperClient does
    assert result["model"] == "large-v3-turbo-q5_0"
    seg = result["segments"][0]
    assert (seg["start"], seg["end"], seg["text"]) == (0.0, 1.5, " Radio check, test one.")
    assert seg["words"][0] == {"word": " Radio", "start": 0.0, "end": 0.42, "probability": 0.93}
    assert len(seg["words"]) == 6
    assert set(result["timing"]) == {"fetch_ms", "infer_ms"} and all(isinstance(v, int) for v in result["timing"].values())
    # whisper-server got the hub's WAV unchanged, with the station's fields
    (req,) = whisper.jobs()
    assert req["path"] == "/inference"
    f = req["fields"]
    assert f["file"] == wav and f["filename"] == "tx.wav"
    assert (f["response_format"], f["temperature"], f["prompt"]) == (b"verbose_json", b"0.0", b"radio check")
    # every hub request: the worker token and the Cloudflare Access service token
    for method, path, headers, body in hub.requests:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["CF-Access-Client-Id"] == CF_ID and headers["CF-Access-Client-Secret"] == CF_SECRET
        assert method == "GET" or headers["Content-Type"] == "application/json"
    hello = hub.calls("/hello")[0][3]
    assert hello["worker"] == "gpu-test" and hello["model"] == "large-v3-turbo-q5_0" and hello["version"] == W.VERSION
    assert all(c[3]["state"] == "online" and c[3]["wait"] <= 25 for c in hub.calls("/claim"))
    assert "online" in states(hub)
    assert w.counts["done"] == 1
    st = W.read_json(Path(w.cfg.status_path))
    assert st["worker"] == "gpu-test" and st["jobs"]["done"] == 1
    assert SECRET not in Path(w.cfg.status_path).read_text()


def test_extend_while_inference_is_slow(tmp_path, hub, whisper):
    whisper.delay = 0.6
    hub.add_job("clip-slow", tone_wav(0.5), lease_s=0.2)
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: hub.accepted(), what="the slow job completed")
    assert len(hub.calls("/extend")) >= 2                  # every lease_s/2 while whisper ran
    assert not hub.calls("/release")


def test_complete_after_the_lease_is_lost_is_dropped(tmp_path, hub, whisper, caplog):
    whisper.before = lambda fields: hub.expire_all() if len(fields.get("file", b"")) > 40000 else None
    hub.add_job("clip-late", tone_wav(1.5))
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: w.counts["lost"] == 1, what="the late result dropped")
        whisper.before = None
        hub.add_job("clip-next", tone_wav(1.0))
        wait_for(lambda: hub.accepted(), what="the next job")
    assert len(hub.calls("/complete")) == 2                # the late one was refused (409), the next one taken
    assert [r["text"] for r in hub.accepted()] == ["Radio check, test one."]
    assert "lease expired" in caplog.text
    assert not hub.calls("/release")


def test_whisper_failure_releases_then_pauses_and_resumes(tmp_path, hub, whisper):
    w = W.Worker(make_cfg(tmp_path, hub, whisper, whisper_check_s=0.2), run=no_smi)
    with running(w):
        wait_for(lambda: "online" in states(hub), what="online")
        whisper.fail = 500
        for i in range(3):
            hub.add_job(f"clip-f{i}", tone_wav(1.0))
        wait_for(lambda: len(hub.calls("/release")) == 3, what="three releases")
        assert all("500" in r[3]["why"] for r in hub.calls("/release"))
        wait_for(lambda: states(hub)[-1] == "paused", what="paused")
        assert "whisper-server" in hub.calls("/state")[-1][3]["reason"]
        claims, beats = len(hub.calls("/claim")), len(hub.calls("/state"))
        time.sleep(0.4)
        assert len(hub.calls("/claim")) == claims                # no new jobs while whisper-server fails ...
        assert len(hub.calls("/state")) > beats                  # ... and the heartbeat goes on
        whisper.fail = None                                      # the health check sees it answer again
        wait_for(lambda: states(hub)[-1] == "online", what="online again")
        hub.add_job("clip-ok", tone_wav(1.0))
        wait_for(lambda: hub.accepted(), what="a job after the recovery")
    assert w.counts["failed"] == 3


def test_whisper_server_gone_pauses(tmp_path, hub, whisper):
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: "online" in states(hub), what="online")
        whisper.stop()
        wait_for(lambda: w.state()[0] == "paused", what="paused: whisper-server not answering")
        wait_for(lambda: states(hub)[-1] == "paused", what="the hub told")


# --- gaming mode -----------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def monitor(smi: FakeSmi, clock: Clock, busy=lambda: False, ours=frozenset()) -> W.GpuMonitor:
    return W.GpuMonitor(W.Config(), run=smi, clock=clock, busy=busy, ours=lambda: set(ours))


def test_gaming_from_another_process_using_gpu_memory():
    smi, clock = FakeSmi(apps="4321, 600\n5555, 2048"), Clock()
    m = monitor(smi, clock, ours={4321})
    m.sample()
    assert m.available and not m.gaming                      # one sample isn't enough
    clock.t += 5
    m.sample()
    assert m.gaming and "2048 MB" in m.why
    assert m.info == {"util_pct": 3, "mem_used_mb": 1210, "mem_total_mb": 12282, "others": 1}


def test_our_whisper_server_is_not_a_game():
    smi, clock = FakeSmi(apps="4321, 1600"), Clock()      # f16 large-v3-turbo: over 1 GB, but it's ours
    m = monitor(smi, clock, ours={4321})
    for _ in range(3):
        m.sample()
        clock.t += 5
    assert not m.gaming and m.info["others"] == 0


def test_gaming_from_gpu_load_only_while_we_are_idle():
    smi, clock = FakeSmi(gpu="64, 3000, 12282"), Clock()
    busy = [True]
    m = monitor(smi, clock, busy=lambda: busy[0])
    for _ in range(3):
        m.sample()
        clock.t += 5
    assert not m.gaming                                      # that load is our own inference
    busy[0] = False
    m.sample()
    clock.t += 5
    m.sample()
    assert m.gaming and "64%" in m.why


def test_windows_wddm_memory_not_available_uses_the_load():
    smi, clock = FakeSmi(gpu="12, 5000, 12282", apps="7000, [N/A]\n7001, [N/A]"), Clock()
    m = monitor(smi, clock)
    m.sample()
    m.sample()
    assert not m.gaming and m.info["others"] == 2
    smi.gpu = "97, 9000, 12282"
    m.sample()
    m.sample()
    assert m.gaming


def test_gaming_clears_after_60_s_quiet():
    smi, clock = FakeSmi(gpu="90, 8000, 12282"), Clock()
    m = monitor(smi, clock)
    m.sample()
    clock.t += 5
    m.sample()
    assert m.gaming
    smi.gpu = "2, 1200, 12282"
    m.sample()                                               # quiet from here
    clock.t += 30
    smi.gpu = "88, 8000, 12282"
    m.sample()                                               # busy again: the 60 s start over
    smi.gpu = "2, 1200, 12282"
    clock.t += 5
    m.sample()
    clock.t += 59
    m.sample()
    assert m.gaming
    clock.t += 1
    m.sample()
    assert not m.gaming and m.why == ""


def test_one_busy_sample_is_not_gaming():
    smi, clock = FakeSmi(gpu="90, 8000, 12282"), Clock()
    m = monitor(smi, clock)
    m.sample()
    smi.gpu = "1, 1200, 12282"
    m.sample()
    smi.gpu = "90, 8000, 12282"
    m.sample()
    assert not m.gaming


def test_no_nvidia_smi_is_manual_only():
    m = W.GpuMonitor(W.Config(), run=no_smi, clock=Clock())
    m.sample()
    assert m.available is False and not m.gaming and m.info is None


def test_auto_gaming_stops_claims_and_keeps_the_heartbeat(tmp_path, hub, whisper):
    smi = FakeSmi()
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=smi)
    with running(w):
        wait_for(lambda: "online" in states(hub), what="online")
        smi.gpu = "95, 9000, 12282"
        wait_for(lambda: states(hub)[-1] == "gaming", what="gaming")
        last = hub.calls("/state")[-1][3]
        assert "95%" in last["reason"] and last["gpu"]["util_pct"] == 95
        time.sleep(0.25)                                     # a claim in flight when it started comes back
        claims, beats = len(hub.calls("/claim")), len(hub.calls("/state"))
        hub.add_job("clip-g", tone_wav(1.0))
        time.sleep(0.2)
        assert len(hub.calls("/claim")) == claims
        assert len(hub.calls("/state")) > beats
        smi.gpu = "1, 1200, 12282"                           # quiet for gaming_clear_s (0.3 s here)
        wait_for(lambda: hub.accepted(), what="the job once gaming ended")
    assert states(hub)[-1] in ("online", "paused")


def test_manual_gaming_and_pause_from_the_cli(tmp_path, hub, whisper, capsys):
    cfg = make_cfg(tmp_path, hub, whisper)
    conf = tmp_path / "worker.json"
    conf.write_text(json.dumps({"hub_url": hub.url, "state_dir": cfg.state_dir, "whisper_url": whisper.url}))
    cli = lambda *a: W.main([*a, "-c", str(conf)])           # noqa: E731
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: "online" in states(hub), what="online")
        assert cli("gaming", "on") == 0
        wait_for(lambda: states(hub)[-1] == "gaming", what="gaming")
        assert hub.calls("/state")[-1][3]["reason"] == "manual"
        time.sleep(0.25)
        claims, beats = len(hub.calls("/claim")), len(hub.calls("/state"))
        hub.add_job("clip-m", tone_wav(1.0))
        assert cli("pause") == 0
        wait_for(lambda: states(hub)[-1] == "paused", what="paused")
        time.sleep(0.2)
        assert len(hub.calls("/claim")) == claims and len(hub.calls("/state")) > beats
        capsys.readouterr()
        assert cli("status") == 0
        out = capsys.readouterr().out
        assert "gaming on, paused" in out and "paused (manual)" in out and SECRET not in out
        assert cli("resume") == 0
        wait_for(lambda: hub.accepted(), what="the job after resume")
    assert W.read_json(cfg.control_path) | {"changed": ""} == {"gaming": False, "paused": False, "changed": ""}
    # the switch outlives a restart
    W.Control(cfg.control_path).set(gaming=True)
    assert W.Worker(cfg, run=no_smi).state() == ("gaming", "manual")


def test_gaming_mid_job_finishes_within_the_lease(tmp_path, hub, whisper):
    cfg = make_cfg(tmp_path, hub, whisper)
    whisper.delay = 0.3
    whisper.before = lambda f: W.Control(cfg.control_path).set(gaming=True) if len(f["file"]) > 40000 else None
    hub.add_job("clip-mid", tone_wav(1.5), lease_s=2.0)
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: hub.accepted(), what="finished inside its lease")
        wait_for(lambda: states(hub)[-1] == "gaming", what="gaming")
    assert not hub.calls("/release") and not hub.calls("/extend")


def test_gaming_mid_job_releases_when_the_lease_runs_short(tmp_path, hub, whisper):
    cfg = make_cfg(tmp_path, hub, whisper, lease_margin_s=0.1)
    whisper.delay = 0.7
    whisper.before = lambda f: W.Control(cfg.control_path).set(gaming=True) if len(f["file"]) > 40000 else None
    hub.add_job("clip-mid", tone_wav(1.5), lease_s=0.4)
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: hub.calls("/release"), what="released")
    assert "gaming" in hub.calls("/release")[0][3]["why"]
    assert not hub.calls("/extend") and not hub.calls("/complete")
    wait_for(lambda: whisper.aborted == 1, what="the inference aborted")


# --- the link: refused, standby, gone ------------------------------------------------------------------------------

def test_refused_token_retries_slowly(tmp_path, hub, whisper, caplog):
    hub.auth_reason = "token_revoked"
    sleeps = Sleeps()
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi, sleep=sleeps)
    with running(w):
        wait_for(lambda: len(hub.calls("/hello")) >= 3, what="a few hellos")
        assert set(sleeps) == {300.0}                         # every retry after a 401 waits 5 min
        assert len(hub.calls("/hello")) <= len(sleeps) + 1     # never a hello without that wait: no spin
        assert not hub.calls("/claim") and not hub.calls("/state")
        assert w.link == "refused: token_revoked"
        hub.auth_reason = ""
        hub.add_job("clip-after", tone_wav(1.0))
        wait_for(lambda: hub.accepted(), what="a job once the token works")
    assert "token_revoked" in caplog.text


def test_cloudflare_access_redirect_is_refused_not_followed(tmp_path, whisper):
    class Login(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Location", "https://example.org/cdn-cgi/access/login")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = _serve(Login)
    try:
        hub = W.Hub(f"http://127.0.0.1:{srv.server_address[1]}", TOKEN, CF_ID, CF_SECRET)
        with pytest.raises(W.HubError) as e:
            hub.request("POST", f"{API}/hello", {})
        assert e.value.refused and e.value.reason == "cloudflare_access"
    finally:
        srv.shutdown()
        srv.server_close()


def test_503_retry_after_is_respected(tmp_path, hub, whisper):
    hub.fail_503, hub.retry_after = 2, "7"
    sleeps = Sleeps()
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi, sleep=sleeps)
    hub.add_job("clip-503", tone_wav(1.0))
    with running(w):
        wait_for(lambda: hub.accepted(), what="a job after the standby")
    assert sleeps[:2] == [7.0, 7.0]


def test_reconnects_after_the_hub_goes_away(tmp_path, hub, whisper):
    sleeps = Sleeps()
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi, sleep=sleeps)
    with running(w):
        wait_for(lambda: hub.calls("/claim"), what="claiming")
        hub.stop()
        wait_for(lambda: len(sleeps) >= 7, what="backing off")
        assert sleeps[:7] == [1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0]
        hellos = len(hub.calls("/hello"))
        hub.start()                                          # the same port: the hub is back
        hub.add_job("clip-back", tone_wav(1.0))
        wait_for(lambda: hub.accepted(), what="a job after the reconnect")
        assert len(hub.calls("/hello")) == hellos + 1
    assert w.backoff.n == 0


def test_shutdown_releases_the_job_and_says_paused(tmp_path, hub, whisper):
    whisper.delay = 1.0
    hub.add_job("clip-stop", tone_wav(1.5), lease_s=5.0)
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: whisper.jobs(), what="inference running")
    assert hub.calls("/release")[0][3]["why"] == "shutdown"
    wait_for(lambda: whisper.aborted == 1, what="whisper-server saw the client go")
    assert w.counts["failed"] == 0
    last = dict(hub.calls("/state")[-1][3])
    assert last.pop("self_update")["sha256"] == hashlib.sha256(WORKER_PY.read_bytes()).hexdigest()
    assert last | {"gpu": None} == {"state": "paused", "reason": "shutdown", "gpu": None,
                                                         "model": "large-v3-turbo-q5_0", "version": W.VERSION,
                                                         "models": {"stt": "large-v3-turbo-q5_0", "llm": "", "llm_version": ""}}


def test_audio_only_from_the_hub():
    hub = W.Hub("https://hub.example.org", TOKEN)
    assert hub.path_of("/api/station/stt/jobs/abcd/audio") == "/api/station/stt/jobs/abcd/audio"
    assert hub.path_of("https://hub.example.org/api/station/stt/jobs/abcd/audio") == "/api/station/stt/jobs/abcd/audio"
    for other in ("https://elsewhere.example.net/a.wav", "//elsewhere.example.net/a.wav", "http://hub.example.org/x"):
        with pytest.raises(W.HubError):
            hub.path_of(other)


# --- config, files, process --------------------------------------------------------------------------------------

def test_config_json_and_env(tmp_path):
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"worker": {"hub_url": "https://hub.example.org", "model": "large-v3-turbo",
                                        "gaming_util_pct": "55", "auto_gaming": "no", "heartbeat_s": 30,
                                        "whisper_server": "C:/w/whisper-server.exe -m m.bin --port 8179"}}))
    cfg = W.load_config(p, env={"SIZEUP_STT_WORKER_TOKEN": TOKEN, "SIZEUP_STT_CF_CLIENT_ID": CF_ID,
                                "SIZEUP_STT_CF_CLIENT_SECRET": CF_SECRET})
    W.validate(cfg)
    assert cfg.worker_id == "gpu-test" and cfg.model == "large-v3-turbo"
    assert cfg.gaming_util_pct == 55.0 and cfg.auto_gaming is False and cfg.heartbeat_s == 5
    assert cfg.whisper_server == ["C:/w/whisper-server.exe", "-m", "m.bin", "--port", "8179"]
    assert cfg.whisper_url == "http://127.0.0.1:8179/inference"
    assert SECRET not in repr(cfg) and CF_SECRET not in repr(cfg)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_config_toml_and_token_file(tmp_path):
    (tmp_path / "token").write_text(TOKEN + "\n")
    p = tmp_path / "worker.toml"
    p.write_text(f'hub_url = "https://hub.example.org"\ntoken_file = "{(tmp_path / "token").as_posix()}"\n'
                 'whisper_server = ["whisper-server", "-m", "ggml-large-v3-turbo-q5_0.bin"]\n')
    cfg = W.load_config(p, env={})
    W.validate(cfg)
    assert cfg.token == TOKEN and cfg.whisper_server[0] == "whisper-server"


@pytest.mark.parametrize("doc, env, msg", [
    ({"hub_url": "https://hub.example.org", "llm_priority": "first"}, {"SIZEUP_STT_WORKER_TOKEN": TOKEN},
     "llm_priority: first: one of keep, yield"),
    ({"hub_url": "http://hub.example.org"}, {"SIZEUP_STT_WORKER_TOKEN": TOKEN}, "https only"),
    ({"hub_url": "https://hub.example.org"}, {}, "no token"),
    ({"hub_url": "https://hub.example.org"}, {"SIZEUP_STT_WORKER_TOKEN": "sz1.station." + SECRET}, "isn't a worker token"),
    ({"hub_url": "https://hub.example.org"}, {"SIZEUP_STT_WORKER_TOKEN": TOKEN, "SIZEUP_STT_CF_CLIENT_ID": CF_ID},
     "set both"),
])
def test_config_errors_never_quote_the_token(tmp_path, doc, env, msg):
    p = tmp_path / "worker.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(W.ConfigError) as e:
        W.validate(W.load_config(p, env=env))
    assert msg in str(e.value) and SECRET not in str(e.value)


def test_an_unknown_key_is_warned_of_and_ignored_never_refused(tmp_path, capsys):
    """A worker.py rolled back (or not yet updated) starts with a worker.toml a newer one's key was added to: the key
    is ignored and named in worker.log and `check`, never a refusal (a known key's bad value still is)."""
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"hub_url": "https://hub.example.org", "gamin_util": 1, "from_the_future": "x",
                             "gaming_util_pct": 55}))
    cfg = W.load_config(p, env={"SIZEUP_STT_WORKER_TOKEN": TOKEN})
    W.validate(cfg)
    assert cfg.ignored_keys == ["from_the_future", "gamin_util"] and cfg.gaming_util_pct == 55.0
    assert not hasattr(cfg, "gamin_util") and "ignored_keys" not in repr(cfg)
    cfg.state_dir, cfg.log_file = str(tmp_path / "state"), ""
    W.setup_logging(cfg)
    try:
        for h in W.log.handlers:
            h.flush()
        text = (tmp_path / "state" / "worker.log").read_text()
    finally:
        for h in list(W.log.handlers):
            W.log.removeHandler(h)
            h.close()
    assert "ignoring keys this worker.py doesn't know: from_the_future, gamin_util" in text
    assert W.load_config(p, env={}).llm_priority == "yield"                  # (the default: speech first)
    with pytest.raises(W.ConfigError, match="gaming_util_pct: expected float"):
        p.write_text(json.dumps({"hub_url": "https://hub.example.org", "gaming_util_pct": "lots"}))
        W.load_config(p, env={})


def test_cli_config_error_exits_2_without_the_token(tmp_path, capsys, monkeypatch):
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"hub_url": "ftp://x"}))
    monkeypatch.setenv("SIZEUP_STT_WORKER_TOKEN", TOKEN)
    assert W.main(["run", "-c", str(p)]) == 2
    out = capsys.readouterr()
    assert "hub_url" in out.err and SECRET not in out.err + out.out


def test_redact_filter_in_the_log_file(tmp_path):
    cfg = W.Config(token=TOKEN, cf_client_secret=CF_SECRET, state_dir=str(tmp_path))
    W.setup_logging(cfg)
    try:
        W.log.warning("a mistake that would print %s and %s", TOKEN, CF_SECRET)
        for h in W.log.handlers:
            h.flush()
        text = (tmp_path / "worker.log").read_text()
        assert "a mistake" in text and SECRET not in text and CF_SECRET not in text and "***" in text
    finally:
        for h in list(W.log.handlers):
            W.log.removeHandler(h)
            h.close()
        for f in list(W.log.filters):
            W.log.removeFilter(f)


def test_whisper_process_started_restarted_or_adopted(tmp_path):
    class Proc:
        n = 0

        def __init__(self, argv, **kw):
            Proc.n += 1
            self.pid, self.code, self.kw = 9000 + Proc.n, None, kw

        def poll(self):
            return self.code

    clock = Clock()
    wp = W.WhisperProcess(["whisper-server", "-m", "m.bin"], tmp_path / "whisper-server.log", popen=Proc, clock=clock)
    wp.ensure(port_alive=True)
    assert wp.proc is None                                   # something already serves the port: used as it is
    wp.ensure(port_alive=False)
    assert wp.pid == 9001
    wp.proc.code = 1                                         # it exited
    clock.t += 3
    wp.ensure(port_alive=False)
    assert wp.pid == 0                                       # not restarted within 10 s of the last start
    clock.t += 10
    wp.ensure(port_alive=False)
    assert wp.pid == 9002


def test_find_pids_never_fails():
    assert W.find_pids("whisper-server", run=no_smi) == set()
    assert W.find_pids("", run=no_smi) == set()


def test_worker_is_standard_library_only_and_runs_on_python_3_10():
    """It runs from a plain python.org install on the gaming PC: no sizeup, no numpy, nothing newer than 3.10."""
    src = WORKER_PY.read_text()
    tree = ast.parse(src, feature_version=(3, 10))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods.add(node.module.split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)
    assert "tomllib" in mods                                 # imported only inside load_config, for a .toml (3.11+)
    assert "import tomllib" not in src.split("def load_config")[0]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_the_readme_example_config_loads(tmp_path):
    """The worker.toml in the Windows setup docs is a config the worker takes as it is."""
    import re
    readme = (WORKER_PY.parent / "README.md").read_text()
    (example,) = re.findall(r"```toml\n(.*?)```", readme, re.S)
    p = tmp_path / "worker.toml"
    p.write_text(example)
    cfg = W.load_config(p, env={"SIZEUP_STT_WORKER_TOKEN": TOKEN})
    W.validate(cfg)
    assert cfg.whisper_server[0] == r"whisper\Release\whisper-server.exe"
    assert cfg.whisper_server[cfg.whisper_server.index("--host") + 1] == "127.0.0.1"


# --- review fixes (#649) --------------------------------------------------------------------------------------------

def test_busy_samples_neither_count_nor_reset_the_load():
    """1: steady work. 90 % with [N/A] memory (Windows): samples taken while we infer say nothing, so a game that
    keeps the GPU busy between our jobs is still seen, and a quiet spell isn't restarted by our own load."""
    smi, clock = FakeSmi(gpu="90, 7000, 12282", apps="7000, [N/A]"), Clock()
    busy = [False]
    m = monitor(smi, clock, busy=lambda: busy[0])
    for b in (False, True, True, False):                     # idle (hot), busy, busy, idle (hot): 2 in a row
        busy[0] = b
        m.sample()
        clock.t += 5
    assert m.gaming and "90%" in m.why
    smi.gpu = "5, 1200, 12282"                               # the game ends; our jobs come back
    busy[0] = False
    m.sample()                                               # quiet from here
    for b in (True, False, True):
        clock.t += 20
        busy[0] = b
        smi.gpu = "92, 7000, 12282" if b else "5, 1200, 12282"   # our own inference: busy and loud
        m.sample()
    assert m.gaming
    clock.t += 20
    busy[0] = False
    smi.gpu = "5, 1200, 12282"
    m.sample()
    assert not m.gaming                                      # 75 s of quiet: the busy samples didn't reset it


def test_windows_fullscreen_is_gaming_even_while_busy():
    smi, clock = FakeSmi(gpu="20, 3000, 12282", apps="7000, [N/A]"), Clock()
    full = ["a full-screen Direct3D game"]
    m = W.GpuMonitor(W.Config(), run=smi, clock=clock, busy=lambda: True, fullscreen=lambda: full[0])
    m.sample()
    m.sample()
    assert m.gaming and m.why == "a full-screen Direct3D game"
    full[0] = ""
    m.sample()
    clock.t += 60
    m.sample()
    assert not m.gaming
    # without nvidia-smi (or on a Mac) the full-screen check alone still works
    m2 = W.GpuMonitor(W.Config(), run=no_smi, clock=clock, fullscreen=lambda: "a full-screen app")
    m2.sample()
    m2.sample()
    assert m2.gaming and m2.available is False
    m3 = W.GpuMonitor(W.Config(fullscreen_gaming=False), run=no_smi, clock=clock, fullscreen=lambda: "a full-screen app")
    m3.sample()
    m3.sample()
    assert not m3.gaming


def test_windows_fullscreen_is_quiet_elsewhere():
    assert W.windows_fullscreen() == "" or sys.platform == "win32"
    assert W.QUNS == {2: "a full-screen app", 3: "a full-screen Direct3D game"}


def test_gaming_mid_job_hands_back_at_once_when_much_lease_is_left(tmp_path, hub, whisper):
    """2: gaming with seconds of lease left: released now, and whisper-server stops decoding (the client went)."""
    cfg = make_cfg(tmp_path, hub, whisper)
    whisper.delay = 2.0
    started = []

    def gaming_now(f):
        if len(f["file"]) > 40000:
            started.append(time.monotonic())
            W.Control(cfg.control_path).set(gaming=True)

    whisper.before = gaming_now
    hub.add_job("clip-long", tone_wav(1.5), lease_s=6.0)
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: hub.calls("/release"), what="released")
        released = time.monotonic()
        wait_for(lambda: whisper.aborted == 1, what="the inference aborted")
    assert released - started[0] < 0.5
    assert "gaming" in hub.calls("/release")[0][3]["why"] and not hub.calls("/complete")
    assert w.counts == {"done": 0, "released": 1, "lost": 0, "failed": 0}


def test_gaming_mid_job_hands_back_a_long_class_at_once(tmp_path, hub, whisper):
    cfg = make_cfg(tmp_path, hub, whisper)
    whisper.delay = 1.0
    whisper.before = lambda f: W.Control(cfg.control_path).set(gaming=True) if len(f["file"]) > 40000 else None
    hub.add_job("clip-catchup", tone_wav(1.5), cls="CATCHUP", lease_s=2.0)      # 2 s left: short, but a long class
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: hub.calls("/release"), timeout=0.8, what="released at once")
        wait_for(lambda: whisper.aborted == 1, what="the inference aborted")


def test_pause_mid_job_also_hands_back(tmp_path, hub, whisper):
    cfg = make_cfg(tmp_path, hub, whisper)
    whisper.delay = 1.0
    whisper.before = lambda f: W.Control(cfg.control_path).set(paused=True) if len(f["file"]) > 40000 else None
    hub.add_job("clip-p", tone_wav(1.5), lease_s=6.0)
    with running(W.Worker(cfg, run=no_smi)):
        wait_for(lambda: hub.calls("/release"), timeout=0.8, what="released")
    assert hub.calls("/release")[0][3]["why"] == "paused: manual"


def test_stop_interrupts_a_long_poll_at_once(tmp_path, hub, whisper):
    """8: Ctrl-C / stop doesn't wait out a 25 s claim."""
    hub.claim_max_wait = 10.0
    w = W.Worker(make_cfg(tmp_path, hub, whisper, claim_wait_s=25), run=no_smi)
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    wait_for(lambda: hub.calls("/claim"), what="a long poll")
    time.sleep(0.1)
    t0 = time.monotonic()
    w.stop()
    t.join(5)
    assert not t.is_alive() and time.monotonic() - t0 < 1.0


def test_ctrl_c_stops_the_worker(tmp_path, hub, whisper):
    import _thread
    import signal
    hub.claim_max_wait = 10.0
    w = W.Worker(make_cfg(tmp_path, hub, whisper, claim_wait_s=25), run=no_smi)
    # interrupt_main() raises KeyboardInterrupt only through Python's own SIGINT handler: pytest started in the
    # background from a non-interactive shell has SIGINT ignored, and run() would wait forever. So the handler is put
    # in for the test (and put back), and a deadline stops the worker its own way: never a hang, a failure instead.
    old = signal.signal(signal.SIGINT, signal.default_int_handler)
    timer = threading.Timer(0.5, _thread.interrupt_main)
    deadline = threading.Timer(10.0, w.stop)
    t0 = time.monotonic()
    try:                                                     # (the timers inside: the interrupt never lands outside)
        timer.start()
        deadline.start()
        w.run()                                              # in this (the main) thread, as `worker.py run` does
    finally:
        timer.cancel()
        deadline.cancel()
        signal.signal(signal.SIGINT, old)
    assert time.monotonic() - t0 < 2.0, "not stopped by Ctrl-C: the deadline did it"
    assert hub.calls("/state")[-1][3]["reason"] == "shutdown"
    assert W.read_json(w.cfg.status_path)["state"] == "stopped"


def test_unexpected_errors_are_logged_and_survived(tmp_path, hub, whisper, caplog, monkeypatch):
    """3: a bug in the jobs loop, the hub sending a JSON list, the monitor's command blowing up: logged, carried on."""
    real = W.result_body
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a bug")
        return real(*a, **kw)

    monkeypatch.setattr(W, "result_body", flaky)
    hub.claim_answer = ["not", "an", "object"]

    def smi_bug(argv):
        raise RuntimeError("nvidia-smi bug") if argv[0] == "nvidia-smi" else FileNotFoundError(argv[0])

    sleeps = Sleeps()
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=smi_bug, sleep=sleeps)
    hub.add_job("clip-bug", tone_wav(1.5))
    with running(w):
        wait_for(lambda: len(calls) == 1, what="the bug")
        hub.add_job("clip-ok", tone_wav(1.5))
        wait_for(lambda: hub.accepted(), what="a job after the bug")
        assert w._threads[0].is_alive() and w._threads[1].is_alive()
    assert "isn't a JSON object" in caplog.text
    assert "unexpected error" in caplog.text and "a bug" in caplog.text
    assert "monitor: unexpected error" in caplog.text


def test_audio_not_on_the_hub_is_handed_back_without_a_reconnect(tmp_path, hub, whisper):
    sleeps = Sleeps()
    hub.add_job("clip-x", tone_wav(1.0), audio="https://elsewhere.example.net/a.wav")
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi, sleep=sleeps)
    with running(w):
        wait_for(lambda: hub.calls("/release"), what="released")
        hub.add_job("clip-y", tone_wav(1.5))
        wait_for(lambda: hub.accepted(), what="the next job")
    assert hub.calls("/release")[0][3]["why"] == "audio isn't on the hub"
    assert sleeps == [] and len(hub.calls("/hello")) == 1
    assert not any("elsewhere" in r[1] for r in hub.requests)


def test_no_audio_on_the_hub_410(tmp_path, hub, whisper):
    hub.add_job("clip-gone", None)
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: w.counts["lost"] == 1, what="the 410")
    assert not hub.calls("/release") and not whisper.jobs()


def test_release_of_a_lost_lease_is_not_an_error(tmp_path, hub, whisper, caplog):
    whisper.before = lambda f: hub.expire_all() if len(f["file"]) > 40000 else None
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: "online" in states(hub), what="online")
        whisper.fail = 500
        hub.add_job("clip-e", tone_wav(1.5))
        wait_for(lambda: hub.calls("/release"), what="the release try")
        wait_for(lambda: w.counts["lost"] == 1, what="counted lost")
    assert "already taken the job back" in caplog.text and "release failed" not in caplog.text


def test_extend_stops_at_the_cap_and_frees_the_gpu(tmp_path, hub, whisper):
    """6: the hub's extend answers the remaining time; once it stops growing (the cap) the worker stops asking, and
    when the lease runs out it drops the job and stops whisper-server (the hub already gave it to the next engine)."""
    hub.cap_s = 0.6
    whisper.delay = 2.0
    hub.add_job("clip-cap", tone_wav(1.5), lease_s=0.2)
    w = W.Worker(make_cfg(tmp_path, hub, whisper, lease_margin_s=0.05), run=no_smi)
    with running(w):
        wait_for(lambda: w.counts["lost"] == 1, what="dropped at the cap")
        wait_for(lambda: whisper.aborted == 1, what="the inference aborted")
        extends = len(hub.calls("/extend"))
        time.sleep(0.2)
        assert len(hub.calls("/extend")) == extends
    assert 2 <= extends <= 8
    assert not hub.calls("/complete") and not hub.calls("/release")


def test_complete_retried_on_503(tmp_path, hub, whisper):
    """7: a 503 (a deploy's standby) or a 5xx: complete again while the lease lasts."""
    hub.fail_complete, hub.retry_after = [503, 502], "0.1"
    hub.add_job("clip-r", tone_wav(1.5))
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: hub.accepted(), what="accepted on the third try")
    assert len(hub.calls("/complete")) == 3 and w.counts["done"] == 1


def test_complete_refused_400_hands_the_job_back(tmp_path, hub, whisper):
    hub.fail_complete = [400]
    hub.add_job("clip-400", tone_wav(1.5))
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    with running(w):
        wait_for(lambda: hub.calls("/release"), what="released")
    assert hub.calls("/release")[0][3]["why"] == "complete refused: 400"
    assert len(hub.calls("/complete")) == 1


@pytest.mark.parametrize("fast, field", [(True, b"0.4"), (False, b"0.2"), (None, b"0.2")])
def test_fast_jobs_cap_the_fallback(tmp_path, hub, whisper, fast, field):
    """9: the hub's fast flag (short-lease classes) -> temperature_inc=0.4: the fallback capped at two more passes, never
    off (a greedy pass alone kept a weak airband clip's loops); an older hub sends none: the full decode (0.2)."""
    hub.add_job("clip-fast", tone_wav(1.5), fast=fast)
    with running(W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)):
        wait_for(lambda: hub.accepted(), what="done")
    (req,) = whisper.jobs()
    assert req["fields"].get("temperature_inc") == field
    assert req["fields"]["temperature"] == b"0.0"


@pytest.mark.parametrize("fast", [True, False])
def test_every_request_is_english_with_the_anti_loop_decode(tmp_path, hub, whisper, fast):
    """A short noisy clip came out Icelandic: language=en on every /inference (whisper.cpp b5454 reads it per request,
    whatever the install's whisper_server line says), with non-speech tokens suppressed and the fallback's thresholds;
    never no_fallback, never a language other than en."""
    hub.add_job("clip-en", tone_wav(1.5), fast=fast)
    with running(W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)):
        wait_for(lambda: hub.accepted(), what="done")
    (req,) = whisper.jobs()
    f = req["fields"]
    assert f["language"] == b"en"
    assert (f["suppress_nst"], f["entropy_thold"], f["logprob_thold"], f["no_speech_thold"]) == (
        b"true", b"2.4", b"-1.0", b"0.6")
    assert "no_fallback" not in f and "detect_language" not in f
    assert float(f["temperature_inc"]) > 0


def test_the_health_check_inference_is_english_too(whisper):
    W.Whisper(whisper.url).check()
    (req,) = whisper.requests                       # (jobs() leaves the health check's second of silence out)
    assert req["fields"]["language"] == b"en"


@pytest.mark.parametrize("argv", [
    ["whisper-server", "-m", "m.bin", "--host", "127.0.0.1", "--port", "8179", "-t", "4"],
    ["whisper-server", "-m", "m.bin", "--host", "127.0.0.1", "--port", "8179", "-t", "4", "-l", "en", "-sns",
     "-et", "2.4", "-lpt", "-1.0", "-nth", "0.6", "--vad", "-vm", "models/vad.bin", "-vt", "0.5"],
    ["whisper-server", "--model=m.bin", "--language=en", "--suppress-nst", "--max-context", "0"],
])
def test_whisper_server_args_on_the_allow_list(argv):
    W.check_whisper_argv(argv)


@pytest.mark.parametrize("argv, msg", [
    (["whisper-server", "-m", "m.bin", "-nf"], "-nf not allowed"),
    (["whisper-server", "-m", "m.bin", "--no-fallback"], "--no-fallback not allowed"),
    (["whisper-server", "-m", "m.bin", "-l", "auto"], "English only"),
    (["whisper-server", "-m", "m.bin", "--language=is"], "English only"),
    (["whisper-server", "-m", "m.bin", "-dl"], "-dl not allowed"),
    (["whisper-server", "-m", "m.bin", "--public", "C:/"], "--public not allowed"),
    (["whisper-server", "-m", "m.bin", "--convert"], "--convert not allowed"),
    (["whisper-server", "-m", "a.bin", "-m", "b.bin"], "one -m"),
    (["whisper-server", "-m", "m.bin", "-l"], "-l needs a value"),
    (["whisper-server", "-m", "m.bin", "-sns=1"], "takes no value"),
])
def test_whisper_server_args_off_the_list_are_refused(tmp_path, argv, msg):
    with pytest.raises(W.ConfigError, match=msg):
        W.check_whisper_argv(argv)
    p = tmp_path / "worker.json"                   # ... and validate() says so too: `run`, `check` and the selftest
    p.write_text(json.dumps({"hub_url": "https://hub.example.org", "whisper_server": argv}))
    with pytest.raises(W.ConfigError, match=msg):
        W.validate(W.load_config(p, env={"SIZEUP_STT_WORKER_TOKEN": TOKEN}))


def test_whisper_server_runs_english_only():
    """An install's whisper_server line from before (no -l): the worker adds -l en; one that says it is kept."""
    assert W.whisper_command(["w", "-m", "m.bin", "--port", "8179"]) == ["w", "-m", "m.bin", "--port", "8179", "-l", "en"]
    assert W.whisper_command(["w", "-m", "m.bin", "-l", "en"]) == ["w", "-m", "m.bin", "-l", "en"]
    assert W.whisper_command(["w", "--language=en"]) == ["w", "--language=en"]


def test_no_health_inference_while_gaming(tmp_path, hub, whisper):
    """5: gaming or paused: whisper-server only checked for being there, never given work."""
    cfg = make_cfg(tmp_path, hub, whisper)
    W.Control(cfg.control_path).set(gaming=True)
    with running(W.Worker(cfg, run=no_smi)):
        wait_for(lambda: hub.calls("/state"), what="a heartbeat")
        time.sleep(0.4)
        assert whisper.requests == [] and states(hub)[-1] == "gaming"
        W.Control(cfg.control_path).set(gaming=False)
        wait_for(lambda: states(hub)[-1] == "online", what="online after the check")
    assert len(whisper.requests) == 1


class FakeProc:
    n = 0

    def __init__(self, argv, **kw):
        FakeProc.n += 1
        self.argv, self.kw, self.pid, self.code = argv, kw, 9000 + FakeProc.n, None
        self.stdout = io.BytesIO(b"whisper_init_from_file: loading model\nmain: server is listening\n")

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 1

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def test_whisper_server_stopped_after_a_long_game(tmp_path, hub):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    cfg = W.Config(hub_url=hub.url, token=TOKEN, whisper_url=f"http://127.0.0.1:{port}/inference",
                   whisper_server=["whisper-server", "-m", "m.bin"], state_dir=str(tmp_path / "state"), log_file="-",
                   heartbeat_s=0.05, claim_wait_s=0.2, tick_s=0.02, gaming_stop_whisper_min=0.005)
    procs = []
    w = W.Worker(cfg, run=no_smi, popen=lambda argv, **kw: procs.append(FakeProc(argv, **kw)) or procs[-1])
    with running(w):
        wait_for(lambda: procs, what="whisper-server started")
        assert procs[0].argv == ["whisper-server", "-m", "m.bin", "-l", "en"]     # (whisper_command: English only)
        W.Control(cfg.control_path).set(gaming=True)
        wait_for(lambda: procs[0].code == 1, what="stopped after the game went on")
        assert w.whisper_err == "stopped while gaming"
        time.sleep(0.2)
        assert len(procs) == 1                               # not started again while gaming
    log = (tmp_path / "state" / "whisper-server.log").read_text()
    assert "server is listening" in log


def test_children_get_no_secrets(tmp_path, monkeypatch):
    """8: whisper-server, nvidia-smi and tasklist run without the token or the Cloudflare secret."""
    monkeypatch.setenv("SIZEUP_STT_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("SIZEUP_STT_CF_CLIENT_SECRET", CF_SECRET)
    monkeypatch.setenv("SIZEUP_STT_CF_CLIENT_ID", CF_ID)
    out = W.run_cmd([sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k.startswith('SIZEUP_STT')))"])
    assert out.strip() == "[]"
    procs = []
    wp = W.WhisperProcess(["whisper-server"], tmp_path / "whisper-server.log",
                          popen=lambda argv, **kw: procs.append(FakeProc(argv, **kw)) or procs[-1], clock=Clock())
    wp.ensure(port_alive=False)
    env = procs[0].kw["env"]
    assert not any(k.startswith("SIZEUP_STT") for k in env) and "PATH" in env
    assert procs[0].kw["stdout"] is W.subprocess.PIPE


def test_whisper_server_relative_to_the_config(tmp_path):
    (tmp_path / "whisper" / "Release").mkdir(parents=True)
    exe = tmp_path / "whisper" / "Release" / "whisper-server.exe"
    exe.write_bytes(b"")
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"hub_url": "https://hub.example.org", "state_dir": "state",
                             "whisper_server": ["whisper/Release/whisper-server.exe", "-m", "models/m.bin"]}))
    cfg = W.load_config(p, env={})
    assert cfg.state_dir == str(tmp_path / "state")
    procs = []
    wp = W.WhisperProcess(cfg.whisper_server, tmp_path / "w.log", clock=Clock(), base_dir=cfg.base_dir,
                          popen=lambda argv, **kw: procs.append(FakeProc(argv, **kw)) or procs[-1])
    wp.ensure(port_alive=False)
    assert procs[0].argv == [str(exe), "-m", "models/m.bin"] and procs[0].kw["cwd"] == str(tmp_path)


@pytest.mark.parametrize("doc, msg", [
    ({"whisper_url": "http://192.0.2.10:8179/inference"}, "this PC only"),
    ({"whisper_server": ["whisper-server", "-m", "m.bin", "--host", "0.0.0.0", "--port", "8179"]}, "--host must be"),
    ({"whisper_server": ["whisper-server", "--host"]}, "--host must be"),
])
def test_whisper_server_must_stay_on_loopback(tmp_path, doc, msg):
    """8: whisper-server answers with CORS *: on the network any web page could use it."""
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"hub_url": "https://hub.example.org", **doc}))
    with pytest.raises(W.ConfigError, match=msg):
        W.validate(W.load_config(p, env={"SIZEUP_STT_WORKER_TOKEN": TOKEN}))


def test_a_killed_worker_never_blocks_the_next_and_a_live_one_is_refused(tmp_path):
    """`schtasks /End` kills the worker without a word: its stale status file and its lock file are left, and the next
    worker must start at once. A second worker while one lives is refused."""
    import subprocess
    state = tmp_path / "state"
    holder = ("import sys, time; sys.path.insert(0, sys.argv[1]); import worker as W; "
              "lock = W.InstanceLock(sys.argv[2]); print(lock.acquire(), flush=True); time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", holder, str(WORKER_PY.parent), str(state)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "True"
        W.write_json(state / "status.json", {"pid": child.pid, "updated": time.time(), "state": "online"})
        second = W.InstanceLock(str(state))
        assert not second.acquire()                          # the live one holds it
        assert second.holder() == str(child.pid)
    finally:
        child.kill()                                         # no "stopped", nothing cleaned up
        child.wait(10)
    assert (state / "worker.lock").exists() and W.read_json(state / "status.json")["state"] == "online"
    third = W.InstanceLock(str(state))
    assert third.acquire()                                   # the OS let go of the dead one's lock
    again = W.InstanceLock(str(state))
    assert not again.acquire()                               # and this process's lock refuses another
    third.release()
    assert again.acquire()
    again.release()


def test_cli_run_refuses_a_second_worker(tmp_path, monkeypatch):
    lock = W.InstanceLock(str(tmp_path / "state"))
    assert lock.acquire()
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"hub_url": "https://hub.example.org", "state_dir": str(tmp_path / "state"), "log_file": "-"}))
    monkeypatch.setenv("SIZEUP_STT_WORKER_TOKEN", TOKEN)
    try:
        assert W.main(["run", "-c", str(p)]) == 3
    finally:
        lock.release()
        for h in list(W.log.handlers):
            W.log.removeHandler(h)
        for f in list(W.log.filters):
            W.log.removeFilter(f)


def test_fast_jobs_are_handed_back_at_once_on_gaming(tmp_path, hub, whisper):
    """A pre-alert job (fast) with only 2 s of lease left still goes back at once: the hub takes it back on the
    gaming heartbeat anyway, and waiting would only keep the GPU busy."""
    cfg = make_cfg(tmp_path, hub, whisper)
    whisper.delay = 1.5
    whisper.before = lambda f: W.Control(cfg.control_path).set(gaming=True) if len(f["file"]) > 40000 else None
    hub.add_job("clip-fast-g", tone_wav(1.5), lease_s=2.0, fast=True)
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: hub.calls("/release"), timeout=0.8, what="released at once")
        wait_for(lambda: whisper.aborted == 1, what="the inference aborted")
    assert not hub.calls("/complete")


def test_heartbeat_times_out_within_the_hubs_window(tmp_path, hub, whisper):
    seen = []
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi)
    real = w.hb_hub.request
    w.hb_hub.request = lambda *a, **kw: seen.append(kw.get("timeout")) or real(*a, **kw)
    w.connected = True
    w.heartbeat()
    assert seen == [4.0] and W.HEARTBEAT_TIMEOUT_S < 12 - W.MAX_HEARTBEAT_S


def test_tasklist_parsing():
    out = ('"whisper-server.exe","4321","Console","1","612,004 K"\r\n'
           '"whisper-server.exe","4400","Console","1","1,024 K"\r\n')
    assert W.parse_tasklist(out) == {4321, 4400}
    assert W.parse_tasklist("INFO: No tasks are running which match the specified criteria.\r\n") == set()


def test_hub_takes_back_on_gaming_and_heartbeats_go_on_while_decoding(tmp_path, hub, whisper, caplog):
    """#651: the hub takes back a worker's pre-alert jobs when it goes gaming (the release then gets 409: a lost
    lease, not an error), and it drops a worker it hasn't heard from for 12 s, so the heartbeat runs during a decode."""
    cfg = make_cfg(tmp_path, hub, whisper)
    hub.takeback_on_hold = True
    whisper.delay = 0.8
    hub.add_job("clip-slow", tone_wav(1.5), lease_s=6.0)
    w = W.Worker(cfg, run=no_smi)
    with running(w):
        wait_for(lambda: whisper.jobs(), what="decoding")
        beats = len(hub.calls("/state"))
        time.sleep(0.3)
        assert len(hub.calls("/state")) >= beats + 3              # heartbeats while whisper-server decodes
        W.Control(cfg.control_path).set(gaming=True)
        wait_for(lambda: hub.calls("/release"), what="the release try")
        # the heartbeat and the release race: the hub took it back first (409, lost) or took the release (released)
        wait_for(lambda: w.counts["lost"] + w.counts["released"] == 1, what="handed back or lost")
    assert ("already taken the job back" in caplog.text) == (w.counts["lost"] == 1)
    assert "release failed" not in caplog.text and not hub.accepted()


def test_the_readme_pins_this_worker_py():
    """The README's download is pinned to a commit and a SHA256: the hash must be this worker.py's. A change to
    worker.py needs a README change (the commit that has it, and its hash)."""
    import hashlib
    import re
    readme = (WORKER_PY.parent / "README.md").read_text()
    m = re.search(r"raw\.githubusercontent\.com/actualsecurity/sizeup/([0-9a-f]{40})/deploy/stt-worker/worker\.py", readme)
    assert m, "the README downloads worker.py from a pinned commit, at its path from the move on"
    digest = hashlib.sha256(WORKER_PY.read_bytes()).hexdigest().upper()
    assert digest in readme, f"README: worker.py's SHA256 is now {digest} (pin the commit that has it)"
    import shutil
    import subprocess
    git = shutil.which("git")
    if git:                                # (a shallow checkout, as CI's, may not have the commit)
        r = subprocess.run([git, "show", f"{m.group(1)}:deploy/stt-worker/worker.py"], cwd=WORKER_PY.parent,
                           capture_output=True)
        if r.returncode == 0:
            assert hashlib.sha256(r.stdout).hexdigest().upper() == digest, "the pinned commit has another worker.py"


def test_the_readme_task_runs_as_the_user_and_can_be_checked():
    readme = (WORKER_PY.parent / "README.md").read_text()
    step8 = readme[readme.index("### 8."):readme.index("### Updating")]
    assert "as administrator" not in step8 and "-RunLevel Limited" in step8 and "-LogonType Interactive" in step8
    assert "-RepetitionDuration (New-TimeSpan -Days 3650)" in step8
    assert "(Get-ScheduledTask sizeup-stt-worker).Triggers[1].Repetition" in step8
