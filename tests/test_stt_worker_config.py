"""A GPU worker's settings from the hub (#968): the hub's desired-config document per worker (sizeup/cloud/workerconfig.py,
sizeup/cloud/sttworkers.py) and the worker's side of it (deploy/stt-worker/worker.py HubConfig): the same rules on both
sides, precedence over worker.toml, applying at once / when idle / by a server restart and its self-test, the rollback
to the last good values and their keeping over a restart, older hubs and workers, the standby fence, the audit and
Health's label. No GPU and no network beyond 127.0.0.1: whisper-server and llama-server are the fakes of
tests/test_stt_worker*.py, their processes fakes of Popen. Every id, name and path is made up."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import pytest

from sizeup.cloud import workerconfig as wc
from sizeup.cloud.sttworkers import WorkerApi, WorkerError, WorkerRegistry
from tests.stt_worker_module import W
from tests.test_stt_worker import FakeHub, FakeSmi, FakeWhisper, TOKEN, make_cfg, no_smi

FLAG_PATH = "models/made-up-vad-model-7c1e.bin"     # a value that must never reach the log


@pytest.fixture(autouse=True)
def no_values_in_logs(caplog):
    """Every test: the worker logs at DEBUG, and no value the hub sent (only key names) reaches its log."""
    caplog.set_level(logging.DEBUG, logger="sizeup.stt_worker")
    level = W.log.level
    yield
    W.log.setLevel(level)
    assert FLAG_PATH not in caplog.text and "-61.5" not in caplog.text and TOKEN not in caplog.text


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.stop()


@pytest.fixture
def whisper():
    w = FakeWhisper()
    yield w
    w.stop()


def doc(values: dict) -> dict:
    """values as the hub keeps them (a number as a float, ...); a key it wouldn't keep as it is (what a newer or a
    broken hub might send)."""
    out = {}
    for k, v in values.items():
        try:
            out[k] = wc.check_value(k, v)
        except wc.ConfigValueError:
            out[k] = v
    return out


def offer(values: dict) -> dict:
    """A hello or state answer with the hub's settings, as WorkerApi sends them."""
    return {"ok": True, "pool_on": True, "config": wc.desired(doc(values))}


class Proc:
    """A server process (Popen): exits at once with 1 when told to (a flag the server refuses)."""
    n = 0

    def __init__(self, argv, exits: bool):
        Proc.n += 1
        self.argv, self.pid, self.code, self.stdout = list(argv), 7000 + Proc.n, (1 if exits else None), None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15 if self.code is None else self.code

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class Popen:
    """Popen for the servers: a command line with `bad` in it exits at once (or, decode_fail, starts but whisper-server
    answers no decode)."""

    def __init__(self, whisper: FakeWhisper | None = None, bad: str = "99", decode_fail: bool = False):
        self.whisper, self.bad, self.decode_fail, self.started = whisper, bad, decode_fail, []

    def __call__(self, argv, **kw):
        bad = self.bad in argv
        if self.whisper is not None and self.decode_fail:
            self.whisper.fail = 500 if bad else None
        p = Proc(argv, exits=bad and not self.decode_fail)
        self.started.append(p)
        return p


SERVER = ["whisper-server", "-m", "models/ggml-large-v3-turbo-q5_0.bin", "--port", "8179", "-t", "4", "-sns"]


def server_worker(tmp_path, hub, whisper, popen, **kw) -> W.Worker:
    """A worker that runs its own whisper-server (a fake process; the fake whisper-server answers on its port),
    started."""
    cfg = make_cfg(tmp_path, hub, whisper, whisper_server=list(SERVER), local_keys=["whisper_server", "gate_dbfs"],
                   gate_dbfs=-60.0, **kw)
    w = W.Worker(cfg, run=no_smi, popen=popen)
    w.proc.ensure(False)
    assert w.proc.running()
    return w


# --- the same rules on both sides -------------------------------------------------------------------------------

def test_the_hub_and_the_worker_have_the_same_keys_and_rules():
    assert wc.KEYS == W.HUB_KEYS
    assert wc.SERVER_OWNED == W.SERVER_OWNED and wc.STT_LANGUAGE == W.STT_LANGUAGE
    assert wc.FLAGS["whisper"] == W.hub_flags_allowed("whisper") and wc.FLAGS["llm"] == W.hub_flags_allowed("llm")
    assert wc.LOG_LEVELS == tuple(W.LOG_LEVEL_NAMES) and wc.LLM_PRIORITIES == W.LLM_PRIORITIES
    assert wc.VRAM_KEYS == W.HUB_VRAM_KEYS and wc.FLAGS_MAX == W.HUB_FLAGS_MAX
    # every hub key is a worker.toml key too (worker.toml's value: the fallback), but a server's flags (its argv's)
    fields = {f.name for f in __import__("dataclasses").fields(W.Config)}
    assert {k for k in wc.KEYS if not k.endswith("_flags")} <= fields
    # the bootstrap is never the hub's (#968's audit)
    for local in ("hub_url", "token", "token_file", "cf_client_id", "cf_client_secret", "state_dir", "log_file",
                  "models_dir", "base_dir", "nvidia_smi", "catalog_file", "whisper_url", "llm_url", "whisper_server",
                  "llm_server", "llm_model", "llm_model_sha256", "heartbeat_s", "claim_wait_s", "tick_s"):
        assert local not in wc.KEYS


CASES = [
    ("gate_dbfs", -50, -50.0), ("gate_dbfs", -120, -120.0), ("gate_dbfs", 0.5, None), ("gate_dbfs", "x", None),
    ("gate_dbfs", True, None), ("gate_dbfs", math.inf, None), ("gaming_samples", 3, 3), ("gaming_samples", 3.0, 3),
    ("gaming_samples", 2.5, None), ("gaming_samples", 0, None), ("keep_awake", False, False), ("keep_awake", 0, None),
    ("keep_awake", "false", None), ("log_level", "debug", "debug"), ("log_level", "trace", None),
    ("llm_priority", "keep", "keep"), ("llm_priority", "Keep", None), ("llm_stop_gaming_s", -1, -1.0),
    ("vram_budget_mb", 10752, 10752.0), ("vram_budget_mb", 999999, None),
    ("whisper_flags", ["-t", "6", "--vad", "-vm", FLAG_PATH], ["-t", "6", "--vad", "-vm", FLAG_PATH]),
    ("whisper_flags", ["-l", "en"], ["-l", "en"]), ("whisper_flags", ["--language=en"], ["--language=en"]),
    ("whisper_flags", ["-l", "de"], None), ("whisper_flags", ["-m", "other.bin"], None),
    ("whisper_flags", ["--host", "0.0.0.0"], None), ("whisper_flags", ["--port", "9000"], None),
    ("whisper_flags", ["-dl"], None), ("whisper_flags", ["-t"], None), ("whisper_flags", ["--vad=1"], None),
    ("whisper_flags", "-t 6", None), ("whisper_flags", ["-t", 6], None), ("whisper_flags", ["-sns"] * 41, None),
    ("llm_flags", ["-c", "4096", "-ngl", "99", "--jinja"], ["-c", "4096", "-ngl", "99", "--jinja"]),
    ("llm_flags", ["--api-key", "k"], None), ("llm_flags", ["-m", "x.gguf"], None), ("llm_flags", ["-hf", "o/m"], None),
    ("llm_flags", ["--host=0.0.0.0"], None), ("llm_flags", ["--mmproj", "p.gguf"], None),
]


@pytest.mark.parametrize("key, value, want", CASES)
def test_a_value_is_taken_or_refused_alike_on_the_hub_and_the_worker(key, value, want):
    if want is None:
        with pytest.raises(wc.ConfigValueError):
            wc.check_value(key, value)
        with pytest.raises(W.ConfigError):
            W.check_hub_value(key, value)
    else:
        assert wc.check_value(key, value) == want == W.check_hub_value(key, value)
        assert type(wc.check_value(key, value)) is type(want)


def test_the_vram_settings_stay_within_the_gpus_memory_on_both_sides():
    assert wc.check_value("vram_budget_mb", 12000, gpu_total=12282) == 12000.0
    with pytest.raises(wc.ConfigValueError, match="GPU's 12282 MB"):
        wc.check_value("vram_budget_mb", 16000, gpu_total=12282)
    with pytest.raises(W.ConfigError, match="GPU's 12282 MB"):
        W.check_hub_value("vram_budget_mb", 16000, 12282.0)
    assert W.check_hub_value("vram_budget_mb", 16000) == 16000.0         # (no GPU reading: the range only)


def test_a_key_the_worker_owns_or_doesnt_know_is_never_taken():
    clean, errors = W.check_hub_config({"hub_url": "https://elsewhere.example.org", "token": "x", "state_dir": "/tmp",
                                        "from_a_newer_hub": 1, "gate_dbfs": -50, "gaming_samples": "two"})
    assert clean == {"gate_dbfs": -50.0}
    assert errors == {"hub_url": "worker.toml only", "token": "worker.toml only", "state_dir": "worker.toml only",
                      "from_a_newer_hub": "unknown", "gaming_samples": "a whole number from 1 to 60"}


def test_the_rev_is_a_hash_of_the_values_and_the_schema_lists_every_key():
    from tests import contracts as c
    a = wc.rev({"gate_dbfs": -50.0, "keep_awake": True})
    assert a == wc.rev({"keep_awake": True, "gate_dbfs": -50.0}) and wc.REV.fullmatch(a) and a != wc.rev({})
    for name in ("stt-worker-hello-answer", "stt-worker-state-answer"):
        props = c.load(name)["properties"]["config"]["properties"]["values"]["properties"]
        assert list(props) == list(wc.KEYS), name
        for k, (kind, lo, hi, _when) in wc.KEYS.items():
            p = props[k]
            assert p["type"] == {"bool": "boolean", "int": "integer", "float": "number", "choice": "string",
                                 "flags": "array"}[kind], k
            if kind in ("int", "float"):
                assert (p["minimum"], p["maximum"]) == (lo, hi), k
            if kind == "choice":
                assert p["x-known"] == list(lo), k
    body = c.vector(Path(c.ROOT) / "vectors" / "stt-worker-state-answer" / "valid" / "with-settings.json")
    assert body["config"]["rev"] == wc.rev(body["config"]["values"])


# --- the worker: precedence, applying, the self-test and the rollback --------------------------------------------

def test_the_hub_wins_worker_toml_is_the_fallback_and_the_bootstrap_stays_local(tmp_path, hub, whisper, caplog):
    w = server_worker(tmp_path, hub, whisper, Popen())
    hc = w.hub_config
    assert hc.local["gate_dbfs"] == -60.0 and hc.local["whisper_flags"] == ["-t", "4", "-sns"]
    hc.offer(offer({"gate_dbfs": -61.5, "hub_url": "https://elsewhere.example.org"}), w)
    assert w.cfg.gate_dbfs == -60.0 and hc.state == "pending"            # (an "idle" key: the config thread's)
    assert hc.step(w) is True
    assert w.cfg.gate_dbfs == -61.5 and w.cfg.hub_url == hub.url and hc.applied_rev == wc.rev(
        doc({"gate_dbfs": -61.5, "hub_url": "https://elsewhere.example.org"}))
    assert hc.report()["keys"] == {"gate_dbfs": "ok", "hub_url": "worker.toml only"}
    assert caplog.text.count("worker.toml: gate_dbfs is set on the hub") == 1
    hc.offer(offer({"gate_dbfs": -50.0}), w)                            # another value: no second warning
    hc.step(w)
    assert w.cfg.gate_dbfs == -50.0 and caplog.text.count("worker.toml: gate_dbfs is set on the hub") == 1
    hc.offer(offer({}), w)                                               # the admin cleared it: worker.toml's again
    hc.step(w)
    assert w.cfg.gate_dbfs == -60.0 and hc.report() == {"applied_rev": wc.rev({}), "state": "applied",
                                                        "rev": wc.rev({})}


def test_safe_keys_apply_at_once_even_with_a_clip_in_hand(tmp_path, hub, whisper):
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=FakeSmi())
    w.job = {"lease": "a" * 16}
    vals = {"gaming_mem_mb": 2048, "gaming_util_pct": 70, "keep_awake": False, "log_level": "warning",
            "whisper_timeout_s": 90, "auto_update": False}
    w.hub_config.offer(offer(vals), w)
    assert w.hub_config.applied_rev == wc.rev(doc(vals)) and w.hub_config.state == "applied"
    assert (w.cfg.gaming_mem_mb, w.cfg.gaming_util_pct, w.gpu.cfg.gaming_mem_mb) == (2048.0, 70.0, 2048.0)
    assert w.keep_awake.on is False and W.log.level == logging.WARNING and w.whisper.timeout_s == 90.0
    assert w.updater.cfg.auto_update is False


def test_idle_keys_wait_for_the_clip_and_the_llm_call(tmp_path, hub, whisper):
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=FakeSmi())
    w.job = {"lease": "a" * 16}
    w.hub_config.offer(offer({"gate_dbfs": -50, "gaming_samples": 4}), w)
    assert w.hub_config.step(w) is False and w.cfg.gate_dbfs == -55.0 and w.cfg.gaming_samples == 2
    assert w.hub_config.report()["state"] == "pending" and w.hub_config.report()["why"] == "a clip"
    assert w.hub_config.report()["keys"] == {"gate_dbfs": "pending", "gaming_samples": "pending"}
    w.job, w.llm_job = None, {"lease": "b" * 16}
    assert w.hub_config.step(w) is False and w.hub_config.why == "an LLM job"
    w.llm_job = None
    assert w.hub_config.step(w) is True and (w.cfg.gate_dbfs, w.cfg.gaming_samples) == (-50.0, 4)
    assert w.hub_config.report()["keys"] == {"gate_dbfs": "ok", "gaming_samples": "ok"}


def test_new_flags_restart_whisper_server_when_idle_then_its_self_test(tmp_path, hub, whisper):
    popen = Popen()
    w = server_worker(tmp_path, hub, whisper, popen)
    first = w.proc.proc
    w.control.set(gaming=True)
    w.hub_config.offer(offer({"whisper_flags": ["-t", "6", "--vad", "-vm", FLAG_PATH]}), w)
    assert w.hub_config.step(w) is False and w.hub_config.why == "gaming" and w.proc.proc is first
    w.control.set(gaming=False)
    held = []
    w.check_whisper = (lambda real: lambda: (held.append(w.state()), real())[1])(w.check_whisper)
    assert w.hub_config.step(w) is True
    want = ["whisper-server", "-m", "models/ggml-large-v3-turbo-q5_0.bin", "--port", "8179", "-t", "6", "--vad", "-vm",
            FLAG_PATH]
    assert w.cfg.whisper_server == want and first.code == -15 and popen.started[-1].argv[:len(want)] == want
    assert held and all(s[0] == "paused" for s in held)                   # no new jobs while it restarted
    assert w.whisper_ok and not w.config_hold and w.state() == ("online", "")
    assert w.hub_config.report()["keys"] == {"whisper_flags": "ok"}


@pytest.mark.parametrize("decode_fail", [False, True])
def test_a_failed_self_test_rolls_back_to_the_last_good_values(tmp_path, hub, whisper, monkeypatch, decode_fail):
    monkeypatch.setattr(W, "HUB_CONFIG_SELFTEST_S", 1.5)
    w = server_worker(tmp_path, hub, whisper, Popen(whisper, decode_fail=decode_fail))
    good = {"gate_dbfs": -50}
    w.hub_config.offer(offer(good), w)
    w.hub_config.step(w)
    bad = {"gate_dbfs": -45, "whisper_flags": ["-t", "99"]}
    w.hub_config.offer(offer(bad), w)
    assert w.hub_config.step(w) is True
    assert w.cfg.whisper_server == SERVER and w.proc.argv == SERVER and w.cfg.gate_dbfs == -50.0
    assert w.proc.running() and w.whisper_ok                              # back on the last good flags, answering
    r = w.hub_config.report()
    assert r["applied_rev"] == wc.rev(doc(good)) and r["rev"] == wc.rev(doc(bad))
    assert r["state"] == "rolled back" and r["keys"] == {"gate_dbfs": "rolled back", "whisper_flags": "failed"}
    assert r["errors"]["whisper_flags"].startswith("self-test: whisper-server")
    kept = json.loads((Path(w.cfg.state_dir) / W.HUB_CONFIG_FILE).read_text())
    assert kept["last_good"]["rev"] == r["applied_rev"] and kept["failed"]["key"] == "whisper_flags"
    # the hub sends it again with every answer: not tried again (HUB_CONFIG_RETRY_S); a new rev is
    w.hub_config.offer(offer(doc(bad)), w)
    assert w.hub_config.offered is None and w.hub_config.report()["state"] == "rolled back"
    w.hub_config.offer(offer({"gate_dbfs": -45}), w)
    assert w.hub_config.step(w) is True and w.cfg.gate_dbfs == -45.0


def test_the_last_good_values_hold_over_a_restart(tmp_path, hub, whisper):
    w = server_worker(tmp_path, hub, whisper, Popen())
    vals = {"gate_dbfs": -48, "keep_awake": False, "whisper_flags": ["-t", "8"], "managed_models": False}
    w.hub_config.offer(offer(vals), w)
    assert w.hub_config.step(w) is True
    rev = w.hub_config.applied_rev
    assert w.stt_models.enabled is False
    # the next start: worker.toml's values, then the kept ones before anything is built on them
    cfg = make_cfg(tmp_path, hub, whisper, whisper_server=list(SERVER), local_keys=["whisper_server"], gate_dbfs=-60.0)
    w2 = W.Worker(cfg, run=no_smi, popen=Popen())
    assert (cfg.gate_dbfs, cfg.keep_awake, w2.keep_awake.on, w2.stt_models.enabled) == (-48.0, False, False, False)
    assert w2.proc.argv[-2:] == ["-t", "8"] and w2.hub_config.local["whisper_flags"] == ["-t", "4", "-sns"]
    assert w2._state_body()["config"] == {"applied_rev": rev, "state": "applied", "rev": rev,
                                          "keys": {k: "ok" for k in vals}}
    # the same rev again from the hub: nothing to do; worker.toml's value comes back when the hub drops the key
    w2.hub_config.offer(offer(doc(vals)), w2)
    assert w2.hub_config.offered is None
    w2.hub_config.offer(offer({"keep_awake": False}), w2)
    w2.hub_config.step(w2)
    assert cfg.gate_dbfs == -60.0 and w2.proc.argv == SERVER and w2.stt_models.enabled is True


def test_a_failed_rev_isnt_tried_again_at_once_after_a_restart(tmp_path, hub, whisper, monkeypatch):
    monkeypatch.setattr(W, "HUB_CONFIG_SELFTEST_S", 1.0)
    w = server_worker(tmp_path, hub, whisper, Popen())
    w.hub_config.offer(offer({"whisper_flags": ["-t", "99"]}), w)
    w.hub_config.step(w)
    assert w.hub_config.state == "rolled back"
    w2 = W.Worker(make_cfg(tmp_path, hub, whisper, whisper_server=list(SERVER)), run=no_smi, popen=Popen())
    w2.hub_config.offer(offer({"whisper_flags": ["-t", "99"]}), w2)
    assert w2.hub_config.offered is None


def test_llama_servers_flags_restart_it_and_its_generation_is_the_self_test(tmp_path, hub, whisper, monkeypatch):
    from tests.test_stt_worker_llm import FakeLlama
    monkeypatch.setattr(W, "HUB_CONFIG_SELFTEST_S", 15.0)
    llama = FakeLlama()
    llama.answer = {"ok": True}                                           # (what the worker's check asks for)
    try:
        server = ["llama-server", "-m", "m.gguf", "--port", str(llama.port), "-c", "4096"]
        cfg = make_cfg(tmp_path, hub, whisper, llm_url=llama.url, llm_model="made-up-llm", llm_server=server,
                       llm_model_sha256="a" * 64)
        w = W.Worker(cfg, run=no_smi, popen=Popen())
        w.llm_proc.ensure(False)
        w.hub_config.offer(offer({"llm_flags": ["-c", "8192", "--jinja"]}), w)
        assert w.hub_config.step(w) is True
        assert w.llm_proc.argv == ["llama-server", "-m", "m.gguf", "--port", str(llama.port), "-c", "8192", "--jinja"]
        assert w.llm_ok and w.hub_config.report()["keys"] == {"llm_flags": "ok"}
        assert llama.requests and llama.requests[-1]["body"]["response_format"]["type"] == "json_schema"
    finally:
        llama.stop()


def test_a_server_the_worker_doesnt_run_takes_no_flags(tmp_path, hub, whisper):
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=FakeSmi())          # whisper-server started by someone else
    w.hub_config.offer(offer({"whisper_flags": ["-t", "6"]}), w)
    assert w.hub_config.step(w) is True and w.cfg.whisper_server == [] and w.proc is None
    assert w.hub_config.report()["keys"] == {"whisper_flags": "unused"}


def test_auto_update_off_never_stops_the_settings(tmp_path, hub, whisper):
    """#968's guard: auto_update off (a code update) still takes the hub's settings, auto_update back on included."""
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=FakeSmi())
    w.hub_config.offer(offer({"auto_update": False}), w)
    assert w.cfg.auto_update is False
    w.hub_config.offer(offer({"auto_update": True, "gaming_samples": 5}), w)
    assert (w.cfg.auto_update, w.cfg.gaming_samples) == (True, 5)


def test_log_ship_and_level_from_the_hub(tmp_path, hub, whisper):
    w = W.Worker(make_cfg(tmp_path, hub, whisper, log_ship=False), run=FakeSmi())
    assert w.logship is None
    w.hub_config.offer(offer({"log_ship": True, "log_level": "debug"}), w)
    try:
        assert w.logship is not None and w.logship in W.log.handlers and W.log.level == logging.DEBUG
        w.hub_config.offer(offer({"log_ship": False}), w)
        assert w.logship not in W.log.handlers and W.log.level == logging.INFO     # (worker.toml's level again)
    finally:
        w.stop()
        if w.logship in W.log.handlers:
            W.log.removeHandler(w.logship)


def test_worker_toml_says_its_keys_and_a_log_level(tmp_path):
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"hub_url": "https://hub.example.org", "gate_dbfs": -60, "log_level": "Debug",
                             "whisper_server": "whisper-server -m m.bin -t 4"}))
    cfg = W.load_config(p, env={})
    assert cfg.local_keys == ["gate_dbfs", "hub_url", "log_level", "whisper_server"] and cfg.log_level == "debug"
    p.write_text(json.dumps({"log_level": "loud"}))
    with pytest.raises(W.ConfigError, match="log_level"):
        W.load_config(p, env={})


def test_an_older_hub_sends_none_and_nothing_changes(tmp_path, hub, whisper):
    w = W.Worker(make_cfg(tmp_path, hub, whisper, gate_dbfs=-60.0), run=FakeSmi())
    for ans in ({"ok": True, "pool_on": True}, {"ok": True, "config": "x"}, {"ok": True, "config": {"rev": "short"}},
                {"ok": True, "config": {"rev": "a" * 16, "values": []}}):
        w.hub_config.offer(ans, w)
    assert w.hub_config.offered is None and w.cfg.gate_dbfs == -60.0
    assert w.hub_config.report() == {"applied_rev": "", "state": "none"}


# --- the hub ----------------------------------------------------------------------------------------------------

def registry(tmp_path):
    from sizeup.cloud.hubstore import MonitorDb
    reg = WorkerRegistry(MonitorDb(tmp_path / "hub.sqlite"), log=lambda s: None)
    reg.add("gpu-1", "GPU box")
    return reg


def post(wa, path, body, token):
    raw = json.dumps(body).encode()
    code, data, _ctype = wa.handle("POST", path, {"Authorization": f"Bearer {token}"}, lambda n: raw)
    return code, json.loads(data)


def test_an_admins_settings_are_checked_kept_and_audited_a_line_per_key(tmp_path):
    reg = registry(tmp_path)
    reg.gpu_total = lambda wid: 12282.0
    notes = []
    note = lambda k, o, n: notes.append((k, o, n))                       # noqa: E731
    out = reg.act({"action": "config", "id": "gpu-1", "set": {"gate_dbfs": -50, "whisper_flags": ["-t", "6"]}},
                  "casey", note)
    assert out["config"] == {"rev": wc.rev({"gate_dbfs": -50.0, "whisper_flags": ["-t", "6"]}),
                             "values": {"gate_dbfs": -50.0, "whisper_flags": ["-t", "6"]}}
    assert reg.config_of("gpu-1") == out["config"] and reg.config_of("gpu-2") == {"rev": wc.rev({}), "values": {}}
    reg.act({"action": "config", "id": "gpu-1", "set": {"gate_dbfs": None, "keep_awake": False}}, "casey", note)
    assert reg.config_of("gpu-1")["values"] == {"keep_awake": False, "whisper_flags": ["-t", "6"]}
    assert notes == [("stt workers/gpu-1/config/gate_dbfs", "worker.toml's", "-50.0"),
                     ("stt workers/gpu-1/config/whisper_flags", "worker.toml's", "-t 6"),
                     ("stt workers/gpu-1/config/gate_dbfs", "-50.0", "worker.toml's"),
                     ("stt workers/gpu-1/config/keep_awake", "worker.toml's", "false")]
    for bad, why in (({"gate_dbfs": 3}, "gate_dbfs: a number"), ({"hub_url": "https://x.example.org"}, "worker.toml keeps"),
                     ({"vram_budget_mb": 16000}, "GPU's 12282 MB"), ({"whisper_flags": ["--host", "0.0.0.0"]}, "not allowed"),
                     ({}, "set:"), ({"keep_awake": True, "gaming_samples": 0}, "gaming_samples")):
        with pytest.raises(WorkerError, match=why):
            reg.act({"action": "config", "id": "gpu-1", "set": bad}, "casey", note)
    assert reg.config_of("gpu-1")["values"] == {"keep_awake": False, "whisper_flags": ["-t", "6"]}   # nothing changed
    with pytest.raises(WorkerError, match="no worker"):
        reg.act({"action": "config", "id": "gpu-9", "set": {"keep_awake": True}}, "casey", note)
    assert len(notes) == 4
    reg.act({"action": "config", "id": "gpu-1", "set": {"keep_awake": None, "whisper_flags": None}}, "casey", note)
    assert json.loads(reg.mdb.get(wc.META)) == {} and reg.panel()["workers"][0]["config"]["values"] == {}
    assert set(reg.panel()["config_keys"]) == set(wc.KEYS)


def test_the_answer_carries_the_settings_only_to_a_worker_that_says_it_hasnt_them(tmp_path):
    from tests.test_stt_pool import Clock, pool
    reg = registry(tmp_path)
    token = reg.rotate("gpu-1")
    wa = WorkerApi(reg, pool(Clock()), log=lambda s: None)
    reg.set_config("gpu-1", {"gate_dbfs": -50})
    want = reg.config_of("gpu-1")
    old = {"state": "online", "model": "m"}                              # a worker from before: never told
    for path in ("hello", "state"):
        assert "config" not in post(wa, f"/api/station/stt/{path}", old, token)[1]
    mine = {"applied_rev": "", "state": "none"}
    for path in ("hello", "state"):
        assert post(wa, f"/api/station/stt/{path}", {**old, "config": mine}, token)[1]["config"] == want
    applied = {"applied_rev": want["rev"], "state": "applied", "rev": want["rev"], "keys": {"gate_dbfs": "ok"}}
    assert "config" not in post(wa, "/api/station/stt/state", {**old, "config": applied}, token)[1]
    assert wa.config_view("gpu-1")["config_view"]["label"] == f"{want['rev'][:7]} ✓"
    # a claim never carries them
    assert "config" not in post(wa, "/api/station/stt/claim", {**old, "config": mine, "wait": 0}, token)[1]


def test_a_standby_hub_answers_no_worker_and_keeps_no_setting(tmp_path):
    from sizeup.fence import FencedWrite
    from tests.test_stt_pool import Clock, pool
    reg = registry(tmp_path)
    token = reg.rotate("gpu-1")
    wa = WorkerApi(reg, pool(Clock()), may_write=lambda: False, log=lambda s: None)
    with pytest.raises(FencedWrite):
        post(wa, "/api/station/stt/state", {"state": "online", "model": "m",
                                            "config": {"applied_rev": "", "state": "none"}}, token)
    assert wa.config_seen == {}
    reg.mdb.may_write = lambda: False
    with pytest.raises(FencedWrite):
        reg.set_config("gpu-1", {"gate_dbfs": -50})
    reg.mdb.may_write = lambda: True
    assert reg.config_of("gpu-1")["values"] == {}


R = "35bd305b272cee4a"
OLD = "44136fa355b3678a"


@pytest.mark.parametrize("rep, label, tone", [
    (None, None, None),
    ({"applied_rev": R, "state": "applied", "rev": R, "keys": {"gate_dbfs": "ok"}}, f"{R[:7]} ✓", "ok"),
    ({"applied_rev": OLD, "state": "pending", "rev": R, "why": "a clip"}, "pending", "dim"),
    ({"applied_rev": OLD, "state": "applied", "rev": OLD}, "pending", "dim"),
    ({"applied_rev": R, "state": "applied", "rev": R, "keys": {"gate_dbfs": "ok", "whisper_flags": "invalid"},
      "errors": {"whisper_flags": "whisper_flags: -dl not allowed"}}, "✗ whisper_flags", "warn"),
    ({"applied_rev": R, "state": "applied", "rev": R, "keys": {"from_a_newer_hub": "unknown"}}, "✗ from_a_newer_hub",
     "warn"),
    ({"applied_rev": OLD, "state": "rolled back", "rev": R, "keys": {"whisper_flags": "failed"},
      "errors": {"whisper_flags": "self-test: whisper-server exited (1)"}}, "✗ whisper_flags", "warn"),
    # the rev it failed was another one: what the hub wants now (the last good) is applied
    ({"applied_rev": R, "state": "rolled back", "rev": OLD, "keys": {"whisper_flags": "failed"}}, f"{R[:7]} ✓", "ok"),
])
def test_healths_config_label(rep, label, tone):
    want = {"rev": R, "values": {"gate_dbfs": -50.0, "whisper_flags": ["-t", "6"]}}
    v = wc.view(want, wc.clean_report(rep))
    if label is None:
        assert v == {}
        return
    assert (v["label"], v["tone"]) == (label, tone)
    assert v["title"].startswith(f"Hub {R[:7]} · applied ") and "gate_dbfs = -50.0" in v["title"]
    if rep.get("errors") and rep.get("rev") == R:
        assert next(iter(rep["errors"].values())) in v["title"]


def test_health_reads_the_workers_settings_end_to_end(tmp_path):
    """The real worker client and a real hub: an admin's settings reach the worker in its next answer, the "now" ones
    are applied there and then, the next answer carries none, and Health and Settings show the rev ✓."""
    from tests import contracts as c
    lv = c.Live(tmp_path)
    try:
        reg = lv.hub.app.hub.workers
        cfg = W.Config(hub_url=lv.url, token=lv.worker, whisper_url="http://127.0.0.1:9", state_dir=str(tmp_path / "w"),
                       log_file="-")
        w = W.Worker(cfg, run=FakeSmi())
        w.hello()
        reg.act({"action": "config", "id": c.WORKER, "set": {"gaming_mem_mb": 3000, "gate_dbfs": -50}}, "casey",
                lambda *a: None)
        want = reg.config_of(c.WORKER)
        w.heartbeat()                                     # told; gate_dbfs is an "idle" key: the config thread's
        assert cfg.gaming_mem_mb == 1024.0 and w.hub_config.state == "pending"
        w.heartbeat()
        (h,) = [x for x in lv.hub.app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert h["config_view"]["label"] == "pending"
        assert w.hub_config.step(w) is True and (cfg.gate_dbfs, cfg.gaming_mem_mb) == (-50.0, 3000.0)
        w.heartbeat()
        assert json.loads(lv.worker_seen[-1][3]).get("config") is None
        (h,) = [x for x in lv.hub.app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert h["config_view"]["label"] == f"{want['rev'][:7]} ✓"
        assert lv.hub.app._worker_live(c.WORKER)["config_view"]["label"] == f"{want['rev'][:7]} ✓"
        panel = reg.panel(lv.hub.app._worker_live)["workers"][0]
        assert panel["config"] == want and panel["config_view"]["applied_rev"] == want["rev"]
        # a station's whisper is never told
        reg.set_pool(stations={c.STATION: True})
        code, ans = lv.call("POST", "/api/station/stt/state", {"state": "paused", "model": "m", "config": {
            "applied_rev": "", "state": "none"}}, token="sz1")
        assert code == 200 and "config" not in ans
    finally:
        lv.close()


def test_health_shows_a_config_column_for_gpu_workers():
    js = (Path(__file__).resolve().parents[1] / "sizeup" / "web" / "static" / "health.js").read_text()
    assert '"Version", "Config", "Load"' in js and "w.config_view" in js and 'w.kind === "station" ? "" : conf(w)' in js
