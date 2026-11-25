"""The GPU worker's catalog models (tools/stt_worker/worker.py Models, #639 per-channel models): the catalog in
pins.json checked whole, downloads only from allowed hosts at pinned revisions with exact sizes and hashes, the VRAM
plan (the local LLM gives way), servers started only while idle and dropped (marked bad) when they don't answer, the
silence gate, and a job decoded by the model the hub names. No GPU, no network: downloads go through fake openers,
servers are fake HTTP servers on 127.0.0.1, model files are a few made-up bytes."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import math
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

import tools.stt_worker.worker as W
from tests.test_stt_worker import (TOKEN, FakeHub, FakeWhisper, _serve, make_cfg, running, tone_wav,  # noqa: F401
                                   hub, no_secrets_in_logs, wait_for, whisper)

PINS = Path(W.__file__).with_name("pins.json")
HF = "https://huggingface.co/some-org/some-model/resolve/" + "a" * 40


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def entry(kind: str = "whisper-ggml", files: dict | None = None, **kw) -> dict:
    files = files or {"model": {"file": "ggml-test.bin", "url": f"{HF}/ggml-test.bin", "bytes": 4,
                                "sha256": sha(b"abcd")}}
    return {"kind": kind, "files": files, "vram_mb": 1000, "langs": ["en"], "services": ["fire"], **kw}


def catalog(**models) -> dict:
    return {"stt_models": {"_what": "a note", **models}}


# --- the catalog -----------------------------------------------------------------------------------------------------

def test_the_repo_catalog_passes_the_workers_own_check():
    cat = W.check_catalog(json.loads(PINS.read_text()))
    assert cat, "pins.json lists speech-to-text models"
    for mid, e in cat.items():
        assert e["kind"] in W.MODEL_KINDS
        for f in e["files"].values():
            assert W.model_url_ok(f["url"]) and W.SHA256.fullmatch(f["sha256"])
    kinds = {e["kind"] for e in cat.values()}
    assert kinds == {"whisper-ggml", "llama-asr"}
    # today's whisper-server model is in it under the label worker.toml uses
    assert W.Config().model in cat


@pytest.mark.parametrize("url", [
    "https://huggingface.co/org/repo/resolve/main/ggml-x.bin",                     # a branch, not a pinned revision
    "http://huggingface.co/org/repo/resolve/" + "a" * 40 + "/ggml-x.bin",         # not https
    "https://huggingface.co:8443/org/repo/resolve/" + "a" * 40 + "/ggml-x.bin",   # a port
    "https://huggingface.co/org/repo/resolve/" + "a" * 40 + "/ggml-x.bin?x=1",    # a query
    "https://huggingface.co/org/repo/resolve/" + "a" * 40 + "/../ggml-x.bin",     # a dot segment
    "https://huggingface.co/org/repo/resolve/" + "a" * 40 + "/%2e%2e/ggml-x.bin",  # an escape
    "https://evil.example/org/repo/resolve/" + "a" * 40 + "/ggml-x.bin",          # another host
    "https://github.com/someone/whisper.cpp/releases/download/b1/x.zip",          # not ggml-org or this repo
    "https://user@huggingface.co/org/repo/resolve/" + "a" * 40 + "/ggml-x.bin",   # a user
    "https://raw.githubusercontent.com/ggml-org/whisper.cpp/" + "a" * 40 + "/ggml-x.bin",   # not a model host
])
def test_urls_that_are_refused(url):
    assert not W.model_url_ok(url)


@pytest.mark.parametrize("url", [
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/" + "5" * 40 + "/ggml-large-v3-turbo-q5_0.bin",
    "https://github.com/ggml-org/llama.cpp/releases/download/b12728/x.zip",
    "https://github.com/actualsecurity/sizeup/releases/download/stt-models-1/ggml-x-f16.bin",
])
def test_urls_that_are_allowed(url):
    assert W.model_url_ok(url)


def test_redirects_only_to_the_known_cdns():
    assert W.redirect_ok("huggingface.co", "https://us.aws.cdn.hf.co/xet-bridge-us/abc?sig=1")
    assert W.redirect_ok("huggingface.co", "https://huggingface.co/api/resolve-cache/models/x")
    assert W.redirect_ok("github.com", "https://release-assets.githubusercontent.com/github-production/1")
    assert not W.redirect_ok("huggingface.co", "https://evilhf.co/x")
    assert not W.redirect_ok("huggingface.co", "http://us.aws.cdn.hf.co/x")
    assert not W.redirect_ok("github.com", "https://us.aws.cdn.hf.co/x")
    assert not W.redirect_ok("github.com", "https://release-assets.githubusercontent.com:444/x")


@pytest.mark.parametrize("bad, why", [
    (catalog(**{"Bad Id": entry()}), "not a model id"),
    (catalog(m=entry(kind="onnx")), "kind"),
    (catalog(m=entry(kind="llama-asr")), "files: exactly model, mmproj"),
    (catalog(m=entry(vram_mb=10)), "vram_mb"),
    (catalog(m=entry(services=["fire", "cooking"])), "services"),
    (catalog(m=entry(context_default="yes")), "context_default"),
    (catalog(m=entry(files={"model": {"file": "ggml-test.bin", "url": f"{HF}/ggml-test.bin", "bytes": 4,
                                      "sha256": "ABCD" * 16}})), "sha256"),
    (catalog(m=entry(files={"model": {"file": "../x.bin", "url": f"{HF}/x.bin", "bytes": 4,
                                      "sha256": sha(b"x")}})), "file"),
    (catalog(a=entry(), b=entry()), "another model's file name"),
    ({"other": {}}, "no stt_models"),
])
def test_a_bad_entry_refuses_the_whole_catalog(bad, why):
    with pytest.raises(W.CatalogError, match=why):
        W.check_catalog(bad)


def test_the_catalog_comes_only_from_main_with_the_hubs_hash():
    body = json.dumps(catalog(m=entry())).encode()
    commit = "c" * 40
    calls = []

    def fetch(host, path, max_bytes):
        calls.append((host, path))
        if "/compare/" in path:
            return 200, {}, json.dumps({"status": "behind", "merge_base_commit": {"sha": commit}}).encode()
        if path.endswith("/pulls?per_page=5"):
            return 200, {}, json.dumps([{"merge_commit_sha": commit, "merged_at": "x",
                                         "base": {"ref": "main"}}]).encode()
        return 200, {}, body

    assert W.download_catalog(commit, sha(body), fetch, now=0) == body
    assert calls[-1] == (W.GITHUB_RAW, f"/actualsecurity/sizeup/{commit}/tools/stt_worker/pins.json")
    with pytest.raises(W.UpdateError, match="hash"):
        W.download_catalog(commit, "0" * 64, fetch, now=0)


# --- downloads -----------------------------------------------------------------------------------------------------

class FakeResp:
    def __init__(self, data: bytes, status: int = 200):
        self.buf, self.status = io.BytesIO(data), status

    def read(self, n=-1):
        return self.buf.read(n)


class FakeConn:
    def close(self):
        pass


def opener(data: bytes, honour_range: bool = True, log: list | None = None):
    def open_(url, offset=0):
        if log is not None:
            log.append(offset)
        if offset and honour_range:
            return FakeConn(), FakeResp(data[offset:], 206), True
        return FakeConn(), FakeResp(data), False
    return open_


def test_a_download_is_exactly_the_pinned_bytes(tmp_path):
    data = b"made-up model bytes" * 1000
    dest = tmp_path / "m.bin"
    W.download_file("u", dest, len(data), sha(data), opener=opener(data))
    assert dest.read_bytes() == data and not dest.with_name("m.bin.part").exists()


def test_a_wrong_hash_or_size_is_final_and_leaves_nothing(tmp_path):
    data = b"x" * 5000
    dest = tmp_path / "m.bin"
    with pytest.raises(W.DownloadError, match="hash mismatch") as e:
        W.download_file("u", dest, len(data), sha(b"other"), opener=opener(data))
    assert e.value.final and not dest.exists() and not dest.with_name("m.bin.part").exists()
    with pytest.raises(W.DownloadError, match="larger than pinned") as e:
        W.download_file("u", dest, 100, sha(data[:100]), opener=opener(data))
    assert e.value.final and not dest.with_name("m.bin.part").exists()


def test_a_stopped_download_is_resumed_where_it_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "DOWNLOAD_CHUNK", 1000)
    data = bytes(range(256)) * 40
    dest = tmp_path / "m.bin"
    n = {"chunks": 0}

    def stop():
        n["chunks"] += 1
        return n["chunks"] > 3                     # gaming began after 3 chunks
    with pytest.raises(W.DownloadError, match="stopped") as e:
        W.download_file("u", dest, len(data), sha(data), stop=stop, opener=opener(data))
    assert not e.value.final
    part = dest.with_name("m.bin.part")
    assert part.stat().st_size == 3000
    offsets: list = []
    W.download_file("u", dest, len(data), sha(data), opener=opener(data, log=offsets))
    assert offsets == [3000] and dest.read_bytes() == data
    # a server that ignores the range sends it all again: started over, still right
    part.write_bytes(data[:2000])
    dest.unlink()
    W.download_file("u", dest, len(data), sha(data), opener=opener(data, honour_range=False))
    assert dest.read_bytes() == data


def test_a_redirect_off_the_allowed_hosts_is_refused(monkeypatch):
    seen = []

    class Conn:
        def __init__(self, host, port, timeout=None, context=None):
            self.host = host
            seen.append(host)

        def request(self, method, path, headers=None):
            pass

        def getresponse(self):
            class R:
                status = 302

                @staticmethod
                def getheader(name):
                    return "https://downloads.evil.example/model.bin"
            return R()

        def close(self):
            pass

    monkeypatch.setattr(W.http.client, "HTTPSConnection", Conn)
    with pytest.raises(W.DownloadError, match="not allowed") as e:
        W.open_url(f"{HF}/ggml-test.bin")
    assert e.value.final and seen == ["huggingface.co"]
    with pytest.raises(W.DownloadError, match="url not allowed"):
        W.open_url("https://downloads.evil.example/model.bin")
    assert seen == ["huggingface.co"]               # never even connected


# --- the plan: VRAM, the LLM gives way -----------------------------------------------------------------------------

def test_plan_takes_the_hubs_order_while_it_fits():
    cat = {"a": {"vram_mb": 3800}, "b": {"vram_mb": 2300}, "c": {"vram_mb": 4000}}
    assert W.plan_models(["a", "b", "c"], cat, 10500, primary_mb=1000) == ["a", "b"]   # 1000 + 3800 + 2300 + (4000 no)
    assert W.plan_models(["c", "a", "b"], cat, 10500, primary_mb=1000) == ["c", "a"]
    assert W.plan_models(["a", "zz"], cat, 0) == []                                     # no GPU reading: none
    assert W.plan_models(["a", "a", "b"], cat, 1e9) == ["a", "b"]


def models_for(tmp_path, cat: dict, **kw) -> W.Models:
    p = tmp_path / "pins.json"
    p.write_text(json.dumps(cat))
    cfg = W.Config(state_dir=str(tmp_path / "state"), models_dir=str(tmp_path / "models"), catalog_file=str(p),
                   whisper_server=["whisper-server", "-m", "x.bin", "--host", "127.0.0.1", "--port", "8179"], **kw)
    return W.Models(cfg)


def two_models() -> dict:
    asr = entry("llama-asr", files={
        "model": {"file": "asr.gguf", "url": f"{HF}/asr.gguf", "bytes": 3, "sha256": sha(b"asr")},
        "mmproj": {"file": "mmproj-asr.gguf", "url": f"{HF}/mmproj-asr.gguf", "bytes": 2, "sha256": sha(b"mm")}},
        vram_mb=3800, services=["fire", "ems", "police"])
    atc = entry(files={"model": {"file": "ggml-atc.bin", "url": f"{HF}/ggml-atc.bin", "bytes": 3,
                                 "sha256": sha(b"atc")}}, vram_mb=2300, services=["air"], context_default=False)
    return catalog(**{"asr-test": asr, "atc-test": atc})


def test_the_llm_gives_way_only_once_a_model_that_needs_its_room_is_ready(tmp_path):
    m = models_for(tmp_path, two_models(), llm_server=["llama-server", "-m", "q.gguf"], llm_vram_mb=6000)
    m.want = ["asr-test", "atc-test"]
    gpu = {"mem_total_mb": 12288}
    assert m.make_plan(gpu) == ["asr-test", "atc-test"]     # 12288 - 1536 = 10752: 1000 + 3800 + 2300 fit
    assert m.llm_room_ok                                    # nothing downloaded yet: the LLM keeps its 6 GB
    for name, data in (("asr.gguf", b"asr"), ("mmproj-asr.gguf", b"mm")):
        f = m.catalog["asr-test"]["files"]["model" if name == "asr.gguf" else "mmproj"]
        m.path_of(f).parent.mkdir(parents=True, exist_ok=True)
        m.path_of(f).write_bytes(data)
    m.make_plan(gpu)
    assert not m.llm_room_ok                                # 1000 + 3800 + 6000 = 10800 > 10752: it gives way
    m.want = ["atc-test"]
    m.make_plan(gpu)
    assert m.llm_room_ok                                    # (only ready or running models count)


# --- servers: started while idle, checked, dropped when they don't answer -------------------------------------------

class FakeProc:
    def __init__(self, argv, **kw):
        self.argv, self.pid, self.returncode = argv, 4242, None
        self.stdout = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, t=None):
        return 0

    def kill(self):
        self.returncode = -9


def ready_models(tmp_path, monkeypatch, cat=None):
    """A Models with its files in place (made-up bytes, their hashes pinned) and a fake Popen."""
    cat = cat or two_models()
    procs: list = []

    def popen(argv, **kw):
        p = FakeProc(argv)
        procs.append((argv, kw))
        return p
    p = tmp_path / "pins.json"
    p.write_text(json.dumps(cat))
    cfg = W.Config(state_dir=str(tmp_path / "state"), models_dir=str(tmp_path / "models"), catalog_file=str(p),
                   whisper_server=["whisper-server", "-m", "x.bin", "--host", "127.0.0.1", "--port", "8179"],
                   llama_bin="llama-server", vram_budget_mb=20000)
    clock = [1000.0]
    m = W.Models(cfg, popen=popen, clock=lambda: clock[0], run=lambda argv: "version: 12728 (ea722948)\n")
    for e in m.catalog.values():
        for f in e["files"].values():
            m.path_of(f).parent.mkdir(parents=True, exist_ok=True)
            m.path_of(f).write_bytes({sha(b): b for b in (b"asr", b"mm", b"atc", b"abcd")}[f["sha256"]])
    monkeypatch.setattr(W, "MODEL_PORT_FIRST", 18181)       # (nothing of ours listens there)
    return m, procs, clock


def test_servers_start_one_at_a_time_and_only_while_idle(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    m.want = ["asr-test", "atc-test"]
    m.step(idle=lambda: False, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert procs == []                                       # a clip in hand: nothing starts
    m.step(idle=lambda: True, held=True, gpu=None, llm_running=False, stop=lambda: False)
    assert procs == []                                       # gaming: nothing starts
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert len(procs) == 1
    argv, kw = procs[0]
    assert argv[:2] == ["llama-server", "-m"] and "--mmproj" in argv and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--no-slots" in argv and "--no-webui" in argv
    assert kw["env"]["LLAMA_API_KEY"] == m.servers["asr-test"].key and m.servers["asr-test"].key
    assert TOKEN not in json.dumps(kw["env"])
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert len(procs) == 1                                   # the first one still loading: nothing else meanwhile
    m.servers["asr-test"].ok = True
    m.servers["asr-test"].alive = lambda: True               # (a fake answering on its port)
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert len(procs) == 2 and procs[1][0][0] == "whisper-server"
    assert procs[1][0][procs[1][0].index("--port") + 1] == "18182"
    assert m.ready() == ["asr-test"]                         # the second hasn't answered a test decode yet


def test_an_asr_model_needs_a_new_enough_llama_server(tmp_path, monkeypatch):
    cat = two_models()
    cat["stt_models"]["asr-test"]["runtime_min"] = 12643
    m, procs, clock = ready_models(tmp_path, monkeypatch, cat)
    m.run = lambda argv: "version: 12359 (b1b770d9)\n"
    m.want = ["asr-test"]
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert procs == [] and "needs llama-server b12643+" in m.status["asr-test"]


def test_a_server_that_never_answers_is_dropped_and_its_hash_marked_bad(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    m.want = ["atc-test"]
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert "atc-test" in m.servers
    clock[0] += W.MODEL_CHECK_S + 1
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert "atc-test" not in m.servers and m.bad(m.catalog["atc-test"]["sha256"])
    n = len(procs)
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert len(procs) == n and "marked bad" in m.status["atc-test"]     # not started again while marked


def test_a_new_version_waits_for_its_files_and_the_old_one_keeps_running(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    m.want = ["atc-test"]
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    old = m.servers["atc-test"]
    old.ok = True
    old.alive = lambda: True
    new = json.loads((tmp_path / "pins.json").read_text())
    new["stt_models"]["atc-test"]["files"]["model"].update(bytes=4, sha256=sha(b"atc2"))
    m.catalog = W.check_catalog(new)
    gate = threading.Event()
    m.download = lambda url, dest, size, sha_, stop: gate.wait(0) or (_ for _ in ()).throw(W.DownloadError("net"))
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert m.servers["atc-test"] is old and m.ready() == ["atc-test"]   # still the old one
    f = m.catalog["atc-test"]["files"]["model"]
    m.path_of(f).write_bytes(b"atc2")
    m.step(idle=lambda: False, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert m.servers["atc-test"] is old                                      # a clip in hand: not swapped
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    fresh = m.servers["atc-test"]                                             # swapped while idle, the old one kept
    assert fresh is not old and m.prev["atc-test"]["sha256"] == old.sha
    assert fresh.sha == m.catalog["atc-test"]["sha256"]
    clock[0] += W.MODEL_CHECK_S + 1                                          # the new version never answers ...
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert m.bad(m.catalog["atc-test"]["sha256"])                            # ... marked bad, so the old version
    assert m.servers["atc-test"].sha == old.sha                              # runs again
    assert all(m.path_of(f).exists() for f in m.prev["atc-test"]["files"].values())   # (its files never pruned)


def test_a_stopped_download_keeps_its_part_through_a_step(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    f = m.catalog["atc-test"]["files"]["model"]
    m.path_of(f).unlink()
    part = m.path_of(f).with_name(m.path_of(f).name + ".part")

    def download(url, dest, size, sha_, stop):
        part.write_bytes(b"at")
        raise W.DownloadError("stopped: resumed later")
    m.download = download
    m.want = ["atc-test"]
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert part.exists() and m.status["atc-test"] == "waiting (gaming)"


def test_a_ready_server_that_exits_is_dropped_not_marked_bad(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    m.want = ["atc-test"]
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    s = m.servers["atc-test"]
    s.ok = True
    s.proc.proc.returncode = 1                                               # it crashed
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert "atc-test" not in m.servers and not m.bad(s.sha) and m.ready() == []


def test_a_server_in_use_is_never_stopped_by_the_models_thread(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    m.want = ["atc-test"]
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    s = m.servers["atc-test"]
    s.ok, s.alive = True, lambda: True
    assert m.acquire("atc-test") is s
    m.want = []                                                               # no longer wanted ...
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert m.servers["atc-test"] is s                                         # ... but a clip is on it
    m.release(s)
    m.step(idle=lambda: True, held=False, gpu=None, llm_running=False, stop=lambda: False)
    assert "atc-test" not in m.servers


def test_llama_servers_build_is_read_from_either_version_line(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    for out, want in (("version: 12728 (ea722948)\nbuilt with cc", 12728),
                      ("version: 0.5.0 (build 11146, commit 0123abcd)", 11146), ("", 0)):
        m._llama_build, m.run = None, (lambda o: lambda argv: o)(out)
        assert m.llama_build() == want


def test_only_the_workers_own_files_are_pruned(tmp_path, monkeypatch):
    m, procs, clock = ready_models(tmp_path, monkeypatch)
    stale = m.dir / ("0" * 16 + "-ggml-old.bin")
    mine = m.dir / "my-notes.txt"
    stale.write_bytes(b"old")
    mine.write_text("keep me")
    m.prune()
    assert not stale.exists() and mine.exists()
    assert all(m.path_of(f).exists() for e in m.catalog.values() for f in e["files"].values())


# --- the silence gate ----------------------------------------------------------------------------------------------

def quiet_wav(seconds=1.0, amp=3, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * math.sin(i))) for i in range(int(rate * seconds))))
    return buf.getvalue()


def test_speech_level():
    assert W.speech_dbfs(W.silence_wav(1.0)) == -120.0
    assert W.speech_dbfs(quiet_wav()) < -70
    assert -16 < W.speech_dbfs(tone_wav(1.0)) < -10                  # 8000 / 32768 peak: about -15 dBFS RMS
    assert W.speech_dbfs(b"not a wav") is None


def test_a_near_silent_clip_is_never_decoded(tmp_path, hub, whisper):
    cfg = make_cfg(tmp_path, hub, whisper)
    w = W.Worker(cfg, run=lambda a: (_ for _ in ()).throw(FileNotFoundError(a[0])))
    hub.add_job("clip-quiet", quiet_wav(2.0))
    with running(w):
        wait_for(lambda: hub.accepted(), what="the result")
    res = hub.accepted()[0]
    assert res["text"] == "" and res["gate"] == "quiet" and res["level_db"] < cfg.gate_dbfs
    assert whisper.jobs() == []


# --- a job on the model the hub names ------------------------------------------------------------------------------

class FakeAsr:
    """llama-server's /v1/chat/completions for an audio model (Qwen3-ASR's answer: "language English<asr_text>...")."""

    def __init__(self, text="language English<asr_text>Engine 60 responding."):
        self.requests: list = []
        self.text = text
        self.reasoning = ""
        asr = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                asr.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                msg = {"role": "assistant", "content": asr.text}
                if asr.reasoning:
                    msg["reasoning_content"] = asr.reasoning
                data = json.dumps({"choices": [{"message": msg}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = _serve(H)
        self.port = self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_job_goes_to_the_named_model_with_its_context(tmp_path, hub, whisper):
    asr = FakeAsr()
    try:
        cfg = make_cfg(tmp_path, hub, whisper)
        w = W.Worker(cfg, run=lambda a: (_ for _ in ()).throw(FileNotFoundError(a[0])))
        e = W.check_catalog(two_models())["asr-test"]
        s = W.ModelServer(e, {}, asr.port, proc=None, timeout_s=10)
        s.ok = True
        w.stt_models.servers["asr-test"] = s
        wav = tone_wav(1.5)
        hub.add_job("clip-asr", wav)
        hub.pending[-1]["extra"] = {"model": "asr-test", "context": ["Engine 60", "Ladder 31", "60 Control"],
                                    "prompt": "Engine 60, Ladder 31."}
        _claim_extra(hub)
        with running(w):
            wait_for(lambda: hub.accepted(), what="the result")
        res = hub.accepted()[0]
        assert res["model"] == "asr-test" and res["text"] == "Engine 60 responding." and res["context"] == "list"
        assert whisper.jobs() == []
        req = asr.requests[-1]
        assert req["path"] == "/v1/chat/completions" and req["auth"] == f"Bearer {s.key}"
        msgs = req["body"]["messages"]
        assert msgs[0] == {"role": "system", "content": "Engine 60, Ladder 31, 60 Control"}
        part = msgs[1]["content"][0]
        assert part["type"] == "input_audio" and base64.b64decode(part["input_audio"]["data"]) == wav
        claims = [r[3] for r in hub.calls("/claim")]
        assert claims and "models" in claims[0]
    finally:
        asr.stop()


def test_context_off_for_the_model_used_sends_none(tmp_path, hub, whisper):
    cfg = make_cfg(tmp_path, hub, whisper)
    w = W.Worker(cfg, run=lambda a: (_ for _ in ()).throw(FileNotFoundError(a[0])))
    hub.add_job("clip-plain", tone_wav(1.5), prompt="Tower, Delta 766")
    hub.pending[-1]["extra"] = {"plain": [cfg.model]}
    _claim_extra(hub)
    with running(w):
        wait_for(lambda: hub.accepted(), what="the result")
    assert "prompt" not in whisper.jobs()[0]["fields"] and hub.accepted()[0]["context"] == "none"


def _claim_extra(hub: FakeHub) -> None:
    """The fake hub's claim answer carries each job's extra keys (model, context, plain) as the real hub's does."""
    send = hub.send

    def send_(req, status, doc, headers=None):
        if isinstance(doc, dict) and isinstance(doc.get("job"), dict):
            with hub.cv:
                lv = hub.leases.get(doc["job"]["lease"])
                if lv is not None:
                    doc["job"].update(lv["job"].get("extra") or {})
        return send(req, status, doc, headers)
    hub.send = send_


def test_an_asr_models_no_speech_is_no_text():
    asr = FakeAsr(text="um.")
    try:
        e = W.check_catalog(two_models())["asr-test"]
        s = W.ModelServer(e, {}, asr.port, proc=None, timeout_s=10)
        out = W.AsrCall(s, tone_wav(1.5), ["Engine 60"], 10).run()
        assert out["text"] == "um."
        asr.reasoning = "language None"
        assert W.AsrCall(s, tone_wav(1.5), [], 10).run() == {"text": "", "segments": []}
    finally:
        asr.stop()
