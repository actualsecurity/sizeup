"""The GPU worker's local LLM named by the hub (deploy/stt-worker/worker.py: models.json llm_models, stt_models llm):
the catalog checked whole, the GGUF downloaded like a speech model's, llama-server switched to it (its -m, its
--reasoning, its hash checked before its first start), the label, hash and VRAM the hub hears, worker.toml's model
again on any failure (with a backoff, parked after repeated out-of-VRAM: #940), and the 12 GB numbers. Fakes only:
no llama-server is started (a fake Popen), no network (a fake download), model files are a few made-up bytes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.stt_worker_module import W
from tests.test_stt_worker import FakeProc, TOKEN, no_smi, wait_for  # noqa: F401
from tests.test_stt_worker import no_secrets_in_logs  # noqa: F401
from tests.test_stt_worker_models import ASR, ATC, GPU_12GB, QWEN3_8B, real_12gb

PINS = json.loads(Path(W.__file__).with_name("pins.json").read_text())["artifacts"]
CATALOG = json.loads(Path(W.__file__).with_name("models.json").read_text())
HF = "https://huggingface.co/some-org/some-llm/resolve/" + "b" * 40
Q4B, Q8B, GEMMA = "qwen3-4b-instruct-2507-q4_k_m", "qwen3-8b-q4_k_m", "gemma-3-12b-it-q4_k_m"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- the catalog -----------------------------------------------------------------------------------------------------

def test_the_repo_llm_catalog_passes_its_check_and_mirrors_pins_json():
    llms = W.check_llm_catalog(CATALOG)
    assert set(llms) == {Q8B, Q4B, GEMMA}
    for mid, key in ((Q8B, "qwen3_8b"), (Q4B, "qwen3_4b"), (GEMMA, "gemma3_12b")):
        f = llms[mid]["files"]["model"]
        assert (f["url"], f["bytes"], f["sha256"]) == (PINS[key]["url"], PINS[key]["bytes"], PINS[key]["sha256"].lower())
        assert W.model_url_ok(f["url"]) and f["file"] == f["url"].rsplit("/", 1)[1]
        # the worker's own estimate (README): the GGUF's size x 1.05 + 900 MB
        assert llms[mid]["vram_mb"] == round(f["bytes"] / 1e6 * 1.05 + 900)
    assert llms[Q8B]["reasoning_off"] and llms[Q4B]["reasoning_off"] and not llms[GEMMA]["reasoning_off"]
    # the default is worker.toml's (the README's example): by hash, the 8B is today's model
    assert llms[Q8B]["sha256"] == PINS["qwen3_8b"]["sha256"].lower()
    # an older worker reads only stt_models: the new key never refuses its catalog
    assert W.check_catalog(CATALOG) and W.LLM_CATALOG_KEY not in CATALOG[W.CATALOG_KEY]


def test_the_4b_fits_beside_every_speech_model_on_12gb_with_130_mb_to_spare():
    stt = W.check_catalog(CATALOG)
    llms = W.check_llm_catalog(CATALOG)
    budget = 12288 - W.Config().vram_reserve_mb
    used = stt["large-v3-turbo-q5_0"]["vram_mb"] + stt[ASR]["vram_mb"] + stt[ATC]["vram_mb"]
    assert budget - used - llms[Q4B]["vram_mb"] == 130
    assert budget - used - llms[Q8B]["vram_mb"] < 0


def llm_entry(**kw) -> dict:
    return {"files": {"model": {"file": "small.gguf", "url": f"{HF}/small.gguf", "bytes": 5, "sha256": sha(b"small")}},
            "vram_mb": 3000, "reasoning_off": True, **kw}


@pytest.mark.parametrize("bad, why", [
    ({"Bad Id": llm_entry()}, "not a model id"),
    ({"m": llm_entry(vram_mb=5)}, "vram_mb"),
    ({"m": llm_entry(reasoning_off="yes")}, "reasoning_off"),
    ({"m": llm_entry(files={"model": {"file": "x.bin", "url": f"{HF}/x.bin", "bytes": 3, "sha256": sha(b"x")}})},
     "a .gguf"),
    ({"m": llm_entry(files={"model": {"file": "x.gguf", "url": "https://evil.example/x.gguf", "bytes": 3,
                                      "sha256": sha(b"x")}})}, "url not allowed"),
    ({"m": llm_entry(files={"model": {"file": "x.gguf", "url": f"{HF}/x.gguf", "bytes": 3,
                                      "sha256": sha(b"x").upper()}})}, "sha256"),
    ({"m": llm_entry(files={"model": {"file": "x.gguf", "url": f"{HF}/x.gguf", "bytes": 0, "sha256": sha(b"x")}})},
     "bytes"),
    ({"m": llm_entry(files={"model": llm_entry()["files"]["model"], "mmproj": llm_entry()["files"]["model"]})},
     "exactly model"),
    ({"a": llm_entry(), "b": llm_entry()}, "another model's file name"),
    ([], "an object"),
])
def test_a_bad_llm_entry_refuses_llm_models(bad, why):
    with pytest.raises(W.CatalogError, match=why):
        W.check_llm_catalog({"stt_models": {}, "llm_models": bad})


def test_no_llm_models_is_none_and_a_bad_one_leaves_the_speech_models(tmp_path):
    assert W.check_llm_catalog({"stt_models": {}}) == {}
    doc = json.loads(json.dumps(CATALOG))
    doc["llm_models"]["broken"] = {"files": {}}
    p = tmp_path / "models.json"
    p.write_text(json.dumps(doc))
    m = W.Models(W.Config(state_dir=str(tmp_path / "state"), models_dir=str(tmp_path / "models"),
                          catalog_file=str(p)))
    assert m.catalog and m.llm_catalog == {}


def test_llm_argv_with_replaces_the_model_and_the_reasoning():
    toml = ["llama\\llama-server.exe", "-m", "models\\Qwen3-8B-Q4_K_M.gguf", "--host", "127.0.0.1", "--port", "8180",
            "-c", "4096", "-ngl", "99", "--jinja", "--no-webui", "--reasoning", "off"]
    got = W.llm_argv_with(toml, "C:\\m\\managed\\x-small.gguf", False)
    assert got == ["llama\\llama-server.exe", "-m", "C:\\m\\managed\\x-small.gguf", "--host", "127.0.0.1", "--port",
                   "8180", "-c", "4096", "-ngl", "99", "--jinja", "--no-webui"]
    W.check_llm_argv(got)
    got = W.llm_argv_with(["llama-server", "--model=a.gguf", "--reasoning=auto", "--port", "8180"], "b.gguf", True)
    assert got == ["llama-server", "--model", "b.gguf", "--port", "8180", "--reasoning", "off"]
    W.check_llm_argv(got)


# --- the hub names it ------------------------------------------------------------------------------------------------

OFFER = {"catalog": {"commit": "c" * 40, "sha256": "d" * 64}, "want": []}


def test_the_hub_names_the_llm_and_it_is_kept_for_the_next_start(tmp_path):
    cfg = W.Config(state_dir=str(tmp_path / "state"), models_dir=str(tmp_path / "models"))
    m = W.Models(cfg)
    m.offer({"stt_models": dict(OFFER, llm=Q4B)})
    assert m.hub_llm == Q4B and W.Models(cfg).hub_llm == Q4B
    m.offer({"stt_models": dict(OFFER, llm="Not An Id!")})
    assert m.hub_llm == ""
    m.offer({"stt_models": dict(OFFER, llm=Q4B)})
    m.offer({"stt_models": OFFER})                                    # an older hub: none named
    assert m.hub_llm == "" and W.Models(cfg).hub_llm == ""
    m.offer({"stt_models": dict(OFFER, llm=Q4B)})
    m.forget_offer({"ok": True})                                      # no stt_models at all
    assert m.hub_llm == ""


# --- the switch (a Worker with fakes) --------------------------------------------------------------------------------

class Rig:
    """A Worker whose llama-server is a FakeProc and whose llama-server answers (status) as the test says; the catalog
    a local file with one made-up LLM ("small-llm") beside worker.toml's ("toml-llm")."""

    def __init__(self, tmp_path, monkeypatch, same=False, entry: dict | None = None):
        self.toml_gguf = tmp_path / "toml.gguf"
        self.toml_gguf.write_bytes(b"toml-gguf")
        self.data = b"toml-gguf" if same else b"small-gguf"
        e = entry or {"files": {"model": {"file": "small.gguf", "url": f"{HF}/small.gguf", "bytes": len(self.data),
                                          "sha256": sha(self.data)}}, "vram_mb": 3000, "reasoning_off": False}
        cat = {"stt_models": {"tiny": {"kind": "whisper-ggml", "files": {"model": {
            "file": "ggml-tiny.bin", "url": f"{HF}/ggml-tiny.bin", "bytes": 4, "sha256": sha(b"tiny")}},
            "vram_mb": 500, "langs": ["en"], "services": ["fire"]}}, "llm_models": {"small-llm": e}}
        (tmp_path / "models.json").write_text(json.dumps(cat))
        self.procs: list = []
        self.clock = [1000.0]
        self.code = [0]
        self.downloads: list = []
        cfg = W.Config(hub_url="http://127.0.0.1:1", token=TOKEN, state_dir=str(tmp_path / "state"), log_file="-",
                       models_dir=str(tmp_path / "models"), catalog_file=str(tmp_path / "models.json"),
                       llm_url="http://127.0.0.1:1", llm_model="toml-llm", llm_model_sha256=sha(b"toml-gguf"),
                       llm_server=["llama-server", "-m", str(self.toml_gguf), "--port", "1", "--reasoning", "off"],
                       tick_s=0.01)
        self.w = W.Worker(cfg, run=no_smi, clock=lambda: self.clock[0],
                          popen=lambda argv, **kw: self.procs.append(FakeProc(argv, **kw)) or self.procs[-1])
        self.w.llm.status = lambda: self.code[0]
        self.answer = [({"ok": True}, 5, 1)]                          # llama-server's test answer (llm_answer)

        def check():
            a = self.answer[0]
            if isinstance(a, Exception):
                raise a
            return a
        self.w.llm.check = check
        self.w.llm.alive = lambda: False
        self.m = self.w.stt_models

        def download(url, dest, size, sha_, stop):
            self.downloads.append(url)
            if stop():
                raise W.DownloadError("stopped: resumed later")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.data)
        self.m.download = download

    def name(self, mid: str) -> None:
        self.m.offer({"stt_models": dict(OFFER, llm=mid)})

    def models_step(self, held=False) -> None:
        self.m.step(idle=lambda: True, held=held, gpu=None, llm_running=bool(self.w.llm_proc.running()),
                    stop=lambda: held)

    def tick(self, n: int = 1) -> None:
        """llm monitor passes, each 6 s on (past its start throttle), the hash check thread waited for."""
        for _ in range(n):
            self.clock[0] += 6
            self.w._llm_monitor(self.clock[0], self.w.held())
            wait_for(lambda: self.w.llm_verified is not None, what="the hash checked")

    def running(self) -> list:
        return [p for p in self.procs if p.code is None]


def model_of(argv) -> str:
    return argv[argv.index("-m") + 1]


def test_the_hub_names_another_llm_downloaded_checked_and_switched(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    w, m = r.w, r.m
    w.verify_llm()
    r.code[0] = 200
    r.tick(2)
    assert len(r.procs) == 1 and model_of(r.procs[0].argv) == str(r.toml_gguf) and w.llm_ok
    assert w.models()["llm"] == "toml-llm" and w.llm_entry is None
    r.name("small-llm")
    r.models_step(held=True)                                      # gaming: nothing downloaded
    assert r.downloads == [] and m.llm_target() is None
    r.tick()
    assert len(r.procs) == 1 and r.procs[0].code is None          # not ready yet: the old one runs on
    assert w.models()["llm_state"] == "downloading" and w.models()["llm_id"] == ""
    r.models_step()
    assert r.downloads == [f"{HF}/small.gguf"] and m.llm_target()["id"] == "small-llm"
    managed = m.path_of(m.llm_catalog["small-llm"]["files"]["model"])
    assert managed.read_bytes() == b"small-gguf" and managed.parent.name == W.MANAGED_DIR
    r.code[0] = 0                                                 # (the new one isn't answering yet)
    r.tick()
    assert r.procs[0].code is not None                            # the old one stopped first ...
    assert model_of(w.llm_proc.argv) == str(managed) and "--reasoning" not in w.llm_proc.argv   # (its reasoning)
    assert w.llm_verified is True and w._llm_sha_now == sha(b"small-gguf")
    assert m.llm_mb() == 3000                                     # the catalog's vram_mb
    r.tick()
    assert len(r.procs) == 2 and model_of(r.procs[1].argv) == str(managed)   # ... then the new one, checked
    r.code[0] = 200
    r.tick()
    mo = w.models()
    assert w.llm_ok and mo["llm"] == "small-llm" and mo["llm_version"] == sha(b"small-gguf")[:12]
    assert mo["llm_state"] == "ok" and mo["llm_id"] == "small-llm"
    m.prune()
    assert managed.exists()                                       # (an LLM file is never pruned)
    r.name("")                                                    # the hub names none: worker.toml's again
    r.code[0] = 0
    r.tick(2)
    assert len(r.procs) == 3 and model_of(r.procs[2].argv) == str(r.toml_gguf) and r.procs[1].code is not None
    assert "--reasoning" in w.llm_proc.argv and w.llm_entry is None and w._llm_sha_now == sha(b"toml-gguf")


def test_its_hash_is_checked_before_its_first_start(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.w.verify_llm()
    r.name("small-llm")
    r.models_step()
    managed = r.m.path_of(r.m.llm_catalog["small-llm"]["files"]["model"])
    managed.write_bytes(b"small-gguX")                            # changed under it after the download
    os.utime(managed, ns=(10**9, 10**9))
    r.tick(3)
    assert all(model_of(p.argv) != str(managed) for p in r.procs)  # never started with it
    assert model_of(r.w.llm_proc.argv) == str(r.toml_gguf)        # worker.toml's again
    assert r.m.llm_note().startswith("failed: model hash mismatch (again in 5 min)")
    assert r.w.models()["llm_state"].startswith("failed: model hash mismatch")


def test_the_same_model_as_worker_toml_is_no_restart_and_no_download(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, same=True)
    w = r.w
    w.verify_llm()
    r.code[0] = 200
    r.tick(2)
    assert len(r.procs) == 1 and w.models()["llm"] == "toml-llm"
    r.name("small-llm")
    r.models_step()
    r.tick(3)
    assert r.downloads == [] and len(r.procs) == 1 and r.procs[0].code is None
    assert w.llm_proc.argv == w.cfg.llm_server
    mo = w.models()
    assert mo["llm"] == "small-llm" and mo["llm_id"] == "small-llm" and mo["llm_state"] == "ok"
    assert mo["llm_version"] == sha(b"toml-gguf")[:12] and r.m.llm_mb() == 3000


def test_an_unknown_id_or_an_older_hub_keeps_worker_tomls(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.w.verify_llm()
    r.name("no-such-llm")
    r.models_step()
    r.tick(2)
    assert r.downloads == [] and model_of(r.w.llm_proc.argv) == str(r.toml_gguf)
    assert r.w.models()["llm_state"] == "not in the catalog" and r.w.models()["llm_id"] == ""


def test_a_download_that_fails_keeps_worker_tomls_and_is_tried_again_later(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.w.verify_llm()

    def fail(url, dest, size, sha_, stop):
        r.downloads.append(url)
        raise W.DownloadError("huggingface.co said 503")
    r.m.download = fail
    r.name("small-llm")
    r.models_step()
    r.models_step()
    r.tick(2)
    assert len(r.downloads) == 1                                  # (again only after MODEL_RETRY_S)
    assert model_of(r.w.llm_proc.argv) == str(r.toml_gguf) and r.procs and r.procs[-1].code is None
    assert r.w.models()["llm_state"] == "failed: huggingface.co said 503"
    r.clock[0] += W.MODEL_RETRY_S
    r.models_step()
    assert len(r.downloads) == 2


def oom(r: Rig) -> None:
    with open(r.w.llm_proc.log_path, "a", encoding="utf-8") as f:
        f.write("ggml_backend_cuda_buffer_type_alloc_buffer: cudaMalloc failed: out of memory\n")
    r.procs[-1].code = 1


def test_a_new_llm_that_runs_out_of_vram_falls_back_then_is_parked(tmp_path, monkeypatch):
    """Each out-of-VRAM start: worker.toml's again at once, the new one tried again after 5, 10 min; the
    MODEL_OOM_PARK-th in a row parks it "no room (VRAM)" (#940): no switch back and forth every 30 min."""
    r = Rig(tmp_path, monkeypatch)
    w = r.w
    w.verify_llm()
    r.name("small-llm")
    r.models_step()
    for n in range(1, W.MODEL_OOM_PARK + 1):
        r.tick(2)
        assert model_of(r.procs[-1].argv).endswith("small.gguf") and r.procs[-1].code is None
        starts = len(r.procs)
        oom(r)
        r.tick()                                                  # its exit seen: back to worker.toml's
        assert not w.llm_ok and w.llm_entry is None and model_of(w.llm_proc.argv) == str(r.toml_gguf)
        r.tick(2)
        assert len(r.procs) == starts + 1 and model_of(r.procs[-1].argv) == str(r.toml_gguf)
        if n < W.MODEL_OOM_PARK:
            wait = W.MODEL_SHORT_RETRY_S * 2 ** (n - 1)
            assert r.m.llm_note() == f"failed: out of VRAM (again in {wait / 60:.0f} min)"
            r.tick(2)
            assert model_of(r.procs[-1].argv) == str(r.toml_gguf)   # not before its retry
            r.clock[0] += wait
    assert r.m.llm_note() == "no room (VRAM)" and w.models()["llm_state"] == "no room (VRAM)"
    starts = len(r.procs)
    r.clock[0] += W.MODEL_RETRY_S * 10
    r.tick(3)
    assert len(r.procs) == starts and model_of(r.procs[-1].argv) == str(r.toml_gguf)   # parked: worker.toml's runs
    assert not r.m.bad(sha(b"small-gguf"))


def test_a_new_llm_that_never_answers_is_marked_bad_at_the_third_time(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.w.verify_llm()
    r.name("small-llm")
    r.models_step()
    for n in range(1, W.MODEL_BAD_TRIES + 1):
        r.tick(2)
        assert model_of(r.procs[-1].argv).endswith("small.gguf")
        r.clock[0] += W.MODEL_CHECK_S
        r.tick()
        assert model_of(r.w.llm_proc.argv) == str(r.toml_gguf)
        r.clock[0] += W.MODEL_SHORT_RETRY_S * 2 ** (n - 1)
    assert r.m.bad(sha(b"small-gguf")) and r.m.llm_target() is None
    assert r.m.llm_note().startswith("marked bad")


def test_gaming_stops_the_new_llm_and_it_comes_back_after(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    w = r.w
    w.verify_llm()
    r.name("small-llm")
    r.models_step()
    r.tick(2)
    assert model_of(r.procs[-1].argv).endswith("small.gguf")
    w._gaming_since = r.clock[0]                                  # gaming before it answered: never its failure
    W.Control(w.cfg.control_path).set(gaming=True)
    r.tick(20)
    assert r.procs[-1].code is not None and not r.running()      # stopped for the game
    n = len(r.procs)
    W.Control(w.cfg.control_path).set(gaming=False)
    w._gaming_since = None
    r.tick(2)
    assert len(r.procs) == n + 1 and model_of(r.procs[-1].argv).endswith("small.gguf")   # the same one again
    assert w.llm_entry["id"] == "small-llm" and r.m.llm_note() == ""
    r.code[0] = 200
    r.tick(2)
    assert w.models()["llm"] == "small-llm" and w._llm_new is None


def test_no_switch_while_an_llm_job_is_in_hand(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    w = r.w
    w.verify_llm()
    r.code[0] = 200
    r.tick(2)
    r.name("small-llm")
    r.models_step()
    w.llm_job = {"lease": "a" * 16}                               # a generation in hand
    r.tick(3)
    assert len(r.procs) == 1 and r.procs[0].code is None and not w.llm_ok    # no new claims meanwhile
    assert w.llm_err == "switching models"
    w.llm_job = None
    r.tick(2)
    assert r.procs[0].code is not None and model_of(r.procs[-1].argv).endswith("small.gguf")


# --- the 12 GB numbers with the hub's LLM (llm_priority yield) --------------------------------------------------------

def test_yield_with_the_4b_keeps_the_llms_room_and_with_the_8b_it_gives_way(tmp_path, monkeypatch):
    """q5_0 primary 1000 + Qwen3-ASR 3800 + ATC 2300 on 10,752 MB: the hub's Qwen3-4B (3522) fits beside them (130
    left), so the LLM keeps its room under yield; the hub's Qwen3-8B (6179) doesn't, and gives way."""
    m, procs = real_12gb(tmp_path, monkeypatch, QWEN3_8B, "version: 12728 (ea722948)\n")
    m.want = [ASR, ATC]
    assert m.llm_priority() == "yield" and m.llm_catalog
    m.hub_llm = Q4B
    assert m.make_plan(GPU_12GB) == [ASR, ATC] and not m.llm_room_ok      # its file not downloaded yet: worker.toml's 8B
    m._llm_ready[m.llm_catalog[Q4B]["sha256"]] = True
    assert m.llm_mb() > 6000                                               # (L2) worker.toml's 8B holds its VRAM
    assert m.make_plan(GPU_12GB) == [ASR, ATC] and not m.llm_room_ok      # until the switch has happened
    m.llm_use = m.llm_catalog[Q4B]                                         # (the Worker switched llama-server)
    assert m.llm_mb() == 3522
    assert m.make_plan(GPU_12GB) == [ASR, ATC] and m.llm_room_ok
    step_ = dict(idle=lambda: True, held=False, gpu=GPU_12GB, llm_running=True, stop=lambda: False)
    m.fetch_llm = lambda stop: None
    m.step(**step_)
    assert m.llm_room_ok and procs                                         # a speech model starts, the LLM stays
    m.hub_llm = Q8B
    m._llm_ready[m.llm_catalog[Q8B]["sha256"]] = True
    assert m.llm_mb() == 6179
    m.make_plan(GPU_12GB)
    assert not m.llm_room_ok


def test_a_new_llm_that_fails_its_test_answer_falls_back_and_is_tried_again(tmp_path, monkeypatch):
    """The test answer every llama-server switch passes (llm_answer, as a new llama.cpp build's): a new model that
    answers /v1/models but not a 1-token constrained answer is never given jobs: worker.toml's again, its backoff."""
    r = Rig(tmp_path, monkeypatch)
    w = r.w
    w.verify_llm()
    r.name("small-llm")
    r.models_step()
    r.tick(2)
    assert model_of(r.procs[-1].argv).endswith("small.gguf")
    r.answer[0] = W.LlmError("llama-server said 500")
    r.code[0] = 200
    r.tick(2)                                                     # (its answer; then worker.toml's argv)
    assert not w.llm_ok and model_of(w.llm_proc.argv) == str(r.toml_gguf)
    assert r.m.llm_note() == "failed: its test answer failed (again in 5 min)"
    r.answer[0] = ({"ok": True}, 5, 1)
    r.tick(2)
    assert w.llm_ok and w.models()["llm"] == "toml-llm"               # worker.toml's passes and serves meanwhile


def test_a_runtime_switch_waits_for_the_llms_model_switch(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.w.verify_llm()
    r.name("small-llm")
    r.models_step()
    r.tick(2)
    assert r.w._llm_new is not None
    monkeypatch.setattr(W, "UPDATE_QUIET_S", 0.0)
    assert r.w.runtime_busy() == "the LLM's model switching"


def test_the_llms_model_switch_takes_the_one_idle_switch_lock(tmp_path, monkeypatch):
    """Never during another switch (a llama.cpp build, the self-update, the hub's config): it waits, no new LLM
    claim meanwhile; then it switches, holding only the LLM's lane (the clips go on)."""
    import threading
    r = Rig(tmp_path, monkeypatch)
    w = r.w
    w.verify_llm()
    r.code[0] = 200
    r.tick(2)
    r.name("small-llm")
    r.models_step()
    inside, leave = threading.Event(), threading.Event()

    def hold():
        with w.idle_switch("llama", drain_s=0.0, force=True):
            inside.set()
            leave.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    assert inside.wait(5)
    r.tick(3)
    assert len(r.procs) == 1 and r.procs[0].code is None and w.llm_err == "switching models"   # waited
    leave.set()
    t.join(5)
    seen = []
    real = w.llm_proc.stop
    w.llm_proc.stop = lambda: (seen.append((w.llm_switching, w.switching)), real())[1]
    r.tick(2)
    assert seen and seen[0] == ("the LLM's model", "") and model_of(r.procs[-1].argv).endswith("small.gguf")
    assert w.llm_switching == "" and w._switch_mx.acquire(blocking=False)


def test_a_failed_switch_falls_back_to_the_last_good_named_llm_not_worker_tomls(tmp_path, monkeypatch):
    """The 4B answered here once; then the hub names another that never answers: the 4B runs again (kept in state:
    a restart too), never worker.toml's 8B quietly back with its VRAM. worker.toml's only when none ever answered."""
    r = Rig(tmp_path, monkeypatch)
    w, m = r.w, r.m
    w.verify_llm()
    r.name("small-llm")
    r.models_step()
    r.tick(2)
    r.code[0] = 200
    r.tick(2)
    assert w.llm_ok and w.models()["llm"] == "small-llm"
    assert m.llm_good["id"] == "small-llm" and W.Models(w.cfg).llm_good["id"] == "small-llm"   # (kept)
    cat = json.loads((tmp_path / "models.json").read_text())
    big = b"big-gguf"
    cat["llm_models"]["big-llm"] = {"files": {"model": {"file": "big.gguf", "url": f"{HF}/big.gguf", "bytes": len(big),
                                                        "sha256": sha(big)}}, "vram_mb": 5000, "reasoning_off": False}
    (tmp_path / "models.json").write_text(json.dumps(cat))
    m._load_local(str(tmp_path / "models.json"))
    data = {f"{HF}/big.gguf": big, f"{HF}/small.gguf": b"small-gguf"}

    def download(url, dest, size, sha_, stop):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data[url])
    m.download = download
    r.name("big-llm")
    r.models_step()
    r.code[0] = 0
    r.tick(2)
    assert model_of(r.procs[-1].argv).endswith("big.gguf")
    assert w.llm_proc.argv and m.llm_mb() == 5000                          # (the big one holds its share now)
    r.clock[0] += W.MODEL_CHECK_S
    r.tick()                                                               # it never answered: back ...
    r.tick(2)
    assert model_of(r.procs[-1].argv).endswith("small.gguf")              # ... to the last good one, not toml.gguf
    assert all(model_of(p.argv) != str(r.toml_gguf) for p in r.procs[1:])
    assert w.models()["llm_state"].startswith("failed: llama-server didn't answer")
    m.llm_good = {}                                                        # none ever answered: worker.toml's
    assert m.llm_fallback() is None
