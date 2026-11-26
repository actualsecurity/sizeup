"""keep_awake (deploy/stt-worker/worker.py KeepAwake): on Windows the GPU worker keeps the PC from idle sleep while it
runs and isn't paused by hand, from a thread of its own (SetThreadExecutionState is per thread), and lets go on a pause,
a stop, the exit or an error. kernel32 is a fake here: every call is recorded with the thread that made it. The hub
keeps what the worker says ({on, held}) and Health shows "keeps PC awake", or "offline since HH:MM (asleep?)" when a
silent worker looks asleep (sizeup/sttpool.py Pool.asleep). Ids, tokens and URLs are made up."""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import threading

import pytest

from sizeup import sttpool
from sizeup.cloud import sttworkers
from tests import contracts as c
from tests.stt_worker_module import W
from tests.test_contracts_station_state import conforms
from tests.test_contracts_workers import gpu_worker, seen
from tests.test_stt_pool import Clock
from tests.test_stt_worker import TOKEN, FakeHub, FakeSmi, FakeWhisper, make_cfg, no_smi, running, wait_for

SET = W.ES_CONTINUOUS | W.ES_SYSTEM_REQUIRED
CLEAR = W.ES_CONTINUOUS
NUDGE = W.ES_SYSTEM_REQUIRED                # alone (no ES_CONTINUOUS): the idle timer reset before a stop's release
ES_DISPLAY_REQUIRED = 0x00000002
API = "/api/station/stt/"


class FakeKernel32:
    """kernel32's SetThreadExecutionState: each call's flags and thread; ok=False: Windows refuses (returns 0)."""

    def __init__(self, ok: bool = True):
        self.calls: list[tuple[int, str]] = []
        self.ok = ok
        self.SetThreadExecutionState = self._set

    def _set(self, flags: int) -> int:
        self.calls.append((flags, threading.current_thread().name))
        return 0x80000000 if self.ok else 0

    @property
    def flags(self) -> list[int]:
        return [f for f, _ in self.calls]


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


def windows_worker(cfg, k32) -> W.Worker:
    return W.Worker(cfg, run=no_smi, keep_awake=W.KeepAwake(cfg.keep_awake, kernel32=k32, windows=True))


def last_keep_awake(hub) -> dict | None:
    return hub.calls("/state")[-1][3].get("keep_awake") if hub.calls("/state") else None


# --- the worker ---------------------------------------------------------------------------------------------------

def test_set_while_running_cleared_on_pause_set_again_on_resume_cleared_on_stop(tmp_path, hub, whisper):
    k32 = FakeKernel32()
    cfg = make_cfg(tmp_path, hub, whisper)
    w = windows_worker(cfg, k32)
    with running(w):
        wait_for(lambda: k32.flags == [SET] and w.keep_awake.held, what="set at the start")
        wait_for(lambda: last_keep_awake(hub) == {"on": True, "held": True}, what="the hub told it is held")
        W.Control(cfg.control_path).set(paused=True)
        wait_for(lambda: k32.flags == [SET, CLEAR] and not w.keep_awake.held, what="cleared on pause")
        wait_for(lambda: last_keep_awake(hub) == {"on": True, "held": False, "why": "paused"},
                 what="the hub told it is released, and why")
        W.Control(cfg.control_path).set(paused=False)
        wait_for(lambda: k32.flags == [SET, CLEAR, SET], what="set again on resume")
        W.Control(cfg.control_path).set(gaming=True)           # gaming: the PC is awake anyway; nothing changes
        wait_for(lambda: w.state()[0] == "gaming" and hub.calls("/state")[-1][3]["state"] == "gaming", what="gaming")
        assert k32.flags == [SET, CLEAR, SET] and w.keep_awake.held
    assert k32.flags == [SET, CLEAR, SET, NUDGE, CLEAR]         # the stop: the idle timer reset first, then cleared
    assert not w.keep_awake.held and ES_DISPLAY_REQUIRED not in {f & ES_DISPLAY_REQUIRED for f in k32.flags}
    assert {t for _, t in k32.calls} == {"keep-awake"}          # one thread of its own (the flag is per thread)
    last = hub.calls("/state")[-1][3]
    assert (last["state"], last["reason"], last["keep_awake"]) == ("paused", "shutdown",
                                                                  {"on": True, "held": False, "why": "stopped"})


def test_cleared_when_the_worker_fails_and_at_the_exit(tmp_path, hub, whisper, monkeypatch):
    k32 = FakeKernel32()
    w = windows_worker(make_cfg(tmp_path, hub, whisper), k32)
    registered = []
    monkeypatch.setattr(W.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(W.atexit, "unregister", lambda fn: None)

    def broken():
        raise RuntimeError("a bug in the shutdown")
    monkeypatch.setattr(w, "shutdown", broken)
    raised = []

    def run():
        try:
            w.run()
        except RuntimeError as e:
            raised.append(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    wait_for(lambda: w.keep_awake.held, what="held")
    w.stop()
    t.join(5)
    assert not t.is_alive() and raised and k32.flags == [SET, NUDGE, CLEAR] and not w.keep_awake.held
    assert registered == [w.keep_awake.close]                   # the exit clears it too (atexit)
    # and at the exit itself: a KeepAwake still held when the interpreter ends is cleared by its atexit hook
    k2 = FakeKernel32()
    ka = W.KeepAwake(True, kernel32=k2, windows=True)
    ka.want(True)
    wait_for(lambda: ka.held, what="held")
    assert registered[-1] == ka.close
    registered[-1]()                                            # what atexit calls
    assert k2.flags == [SET, NUDGE, CLEAR] and not ka.held
    ka.want(True)                                               # closed: never set again
    assert k2.flags == [SET, NUDGE, CLEAR]


def test_an_error_on_its_own_thread_clears_it():
    class Flaky(FakeKernel32):
        def _set(self, flags):
            r = super()._set(flags)
            if len(self.calls) == 2:
                raise OSError("a made-up failure")
            return r

    k32 = Flaky()
    ka = W.KeepAwake(True, kernel32=k32, windows=True)
    ka.want(True)
    wait_for(lambda: ka.held, what="held")
    ka.want(False)                                              # the clear raises: the thread's finally clears again
    ka._thread.join(2)
    assert not ka.held and k32.flags == [SET, CLEAR, CLEAR]
    assert ka.report() == {"on": True, "held": False, "why": "error (see worker.log)"}
    ka.close()


def test_windows_refusing_is_reported_and_logged_once(caplog):
    caplog.set_level(logging.INFO, logger="sizeup.stt_worker")
    k32 = FakeKernel32(ok=False)
    ka = W.KeepAwake(True, kernel32=k32, windows=True)
    ka.want(True)
    wait_for(lambda: k32.flags == [SET], what="tried")
    assert ka.report() == {"on": True, "held": False, "why": "refused by Windows"}
    ka.close()
    assert caplog.text.count("SetThreadExecutionState failed (error ") == 1
    assert k32.flags == [SET, CLEAR]                            # (never held: no idle-timer reset at the stop)


def test_the_stop_resets_the_idle_timer_before_it_clears_and_a_pause_does_not():
    """A stop (a self-update's exit 75 too): ES_SYSTEM_REQUIRED alone first (no ES_CONTINUOUS: a one-off reset of the
    idle timer, a whole timeout's grace until the task starts the worker again), then ES_CONTINUOUS alone."""
    k32 = FakeKernel32()
    ka = W.KeepAwake(True, kernel32=k32, windows=True)
    ka.want(True)
    wait_for(lambda: ka.held, what="held")
    ka.want(False)                                              # a pause: cleared, no reset
    wait_for(lambda: k32.flags == [SET, CLEAR], what="released")
    ka.want(True)
    wait_for(lambda: k32.flags == [SET, CLEAR, SET] and ka.held, what="held again")
    ka.close()
    assert k32.flags == [SET, CLEAR, SET, NUDGE, CLEAR]
    assert NUDGE & W.ES_CONTINUOUS == 0 and {t for _, t in k32.calls} == {"keep-awake"}


def test_a_refusal_is_retried_every_keep_awake_retry_s_whatever_the_ticks(monkeypatch):
    monkeypatch.setattr(W, "KEEP_AWAKE_RETRY_S", 0.4)
    k32 = FakeKernel32(ok=False)
    ka = W.KeepAwake(True, kernel32=k32, windows=True)
    end = W.time.monotonic() + 1.0
    while W.time.monotonic() < end:                             # the heartbeat's ticks: want(True) every 20 ms
        ka.want(True)
        W.time.sleep(0.02)
    n = len(k32.calls)
    assert 2 <= n <= 4, k32.flags                               # the first try, then one each 0.4 s (not one a tick)
    k32.ok = True
    wait_for(lambda: ka.held, timeout=2, what="held once Windows takes it")
    ka.close()


def test_a_dead_thread_is_replaced_after_a_wait(monkeypatch):
    clock = Clock(100.0)

    class DiesOnce(FakeKernel32):
        def _set(self, flags):
            if not self.calls:
                self.calls.append((flags, threading.current_thread().name))
                raise OSError("a made-up failure")
            return super()._set(flags)

    k32 = DiesOnce()
    ka = W.KeepAwake(True, kernel32=k32, windows=True, clock=clock)
    ka.want(True)
    wait_for(lambda: not ka._thread.is_alive(), what="the thread died")
    assert ka.report() == {"on": True, "held": False, "why": "error (see worker.log)"}
    ka.want(True)                                               # too soon: not yet
    assert len(k32.calls) == 1
    clock.t += W.KEEP_AWAKE_RESTART_S[0] + 1
    ka.want(True)
    wait_for(lambda: ka.held, what="a new thread holds it")
    assert k32.flags == [SET, SET]
    ka.close()
    assert k32.flags == [SET, SET, NUDGE, CLEAR]


def test_keep_awake_off_never_calls_windows(tmp_path, hub, whisper):
    k32 = FakeKernel32()
    w = windows_worker(make_cfg(tmp_path, hub, whisper, keep_awake=False), k32)
    with running(w):
        wait_for(lambda: last_keep_awake(hub) == {"on": False, "held": False}, what="told off")
    assert k32.calls == [] and w.keep_awake._thread is None


def test_a_no_op_off_windows(tmp_path, hub, whisper):
    k32 = FakeKernel32()
    ka = W.KeepAwake(True, kernel32=k32, windows=False)
    ka.want(True)
    ka.close()
    assert k32.calls == [] and ka.report() is None and ka._thread is None
    w = W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi, keep_awake=W.KeepAwake(True, kernel32=k32, windows=False))
    assert "keep_awake" not in w._state_body()
    if W.os.name != "nt":                                       # (this machine: the default is the real OS)
        assert W.Worker(make_cfg(tmp_path, hub, whisper), run=no_smi).keep_awake.report() is None


def test_status_shows_it(tmp_path, hub, whisper, capsys):
    k32 = FakeKernel32()
    cfg = make_cfg(tmp_path, hub, whisper)
    w = windows_worker(cfg, k32)
    w.keep_awake.want(True)
    wait_for(lambda: w.keep_awake.held, what="held")
    w._write_status()
    assert "keep awake: on, held (no idle sleep)" in W._status_text(cfg)
    w.keep_awake.close()


# --- the config ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, want", [("", True), ("keep_awake = true", True), ("keep_awake = false", False),
                                        ('keep_awake = "off"', False), ('keep_awake = "yes"', True)])
def test_the_config_key(tmp_path, line, want):
    p = tmp_path / "worker.toml"
    p.write_text(f'hub_url = "https://hub.example.org"\n{line}\n')
    cfg = W.load_config(p, env={"SIZEUP_STT_WORKER_TOKEN": TOKEN})
    W.validate(cfg)
    assert cfg.keep_awake is want


@pytest.mark.parametrize("value", ["2", '"flase"', "[true]", '""'])
def test_the_config_key_is_true_or_false_only(tmp_path, value):
    p = tmp_path / "worker.toml"
    p.write_text(f"keep_awake = {value}\n")
    with pytest.raises(W.ConfigError, match="keep_awake: true or false"):
        W.load_config(p, env={})
    j = tmp_path / "worker.json"
    j.write_text(json.dumps({"keep_awake": 1}))
    with pytest.raises(W.ConfigError, match="keep_awake"):
        W.load_config(j, env={})


def test_the_installers_and_the_readmes_say_it():
    here = W.Path(W.__file__).parent
    assert "\nkeep_awake = true\n" in (here / "install.ps1").read_text()
    readme = (here / "README.md").read_text()
    assert "| `keep_awake` | `true` |" in readme and "## Keeping the PC awake" in readme
    assert "keep_awake" in (here.parents[1] / "tools" / "stt_worker" / "README.md").read_text()


# --- the hub ------------------------------------------------------------------------------------------------------

def test_the_hub_keeps_what_the_worker_says_and_health_guesses_asleep(tmp_path):
    lv = c.Live(tmp_path)
    try:
        k32 = FakeKernel32()
        w = gpu_worker(lv, tmp_path)
        w.keep_awake = W.KeepAwake(True, kernel32=k32, windows=True)
        w.keep_awake.want(True)
        wait_for(lambda: w.keep_awake.held, what="held")
        w.gpu.sample()
        w.whisper_ok = True
        w.hello()
        w.heartbeat()
        hello, state = seen(lv, "hello"), seen(lv, "state")     # (each conforming to its schema)
        assert hello[-1][0]["keep_awake"] == state[-1][0]["keep_awake"] == {"on": True, "held": True}
        app = lv.hub.app
        (row,) = [x for x in app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert (row["state"], row["keep_awake"], row["asleep"]) == ("online", {"on": True, "held": True}, False)
        real = app.sttpool.clock
        app.sttpool.clock = lambda: real() + 60                 # heartbeats stopped while online: asleep?
        (row,) = [x for x in app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert (row["state"], row["asleep"]) == ("offline", True)
        app.sttpool.clock = real
        w.shutdown()                                            # a clean stop: offline, never asleep
        app.sttpool.clock = lambda: real() + 60
        (row,) = [x for x in app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert (row["state"], row["asleep"], row["keep_awake"]) == ("offline", False,
                                                                       {"on": True, "held": False, "why": "stopped"})
        assert k32.flags == [SET, NUDGE, CLEAR]
    finally:
        lv.close()


def test_a_worker_from_before_or_off_windows_says_nothing_and_bad_values_are_dropped(tmp_path):
    lv = c.Live(tmp_path)
    try:
        pool = lv.hub.app.sttpool
        code, _ = lv.call("POST", API + "state", {"state": "online", "model": "m",
                                                  "keep_awake": {"on": True, "held": True}}, token="sw1")
        assert code == 200 and pool.workers[c.WORKER].keep_awake == {"on": True, "held": True}
        code, _ = lv.call("POST", API + "state", {"state": "online", "model": "m"}, token="sw1")  # a rollback
        assert code == 200 and pool.workers[c.WORKER].keep_awake == {}
        for bad in ({"on": "yes", "held": 1}, [True], "on", None):
            code, ans = lv.call("POST", API + "state", {"state": "online", "model": "m", "keep_awake": bad}, token="sw1")
            assert code == 200 and ans["ok"] is True and pool.workers[c.WORKER].keep_awake == {}
        assert sttworkers.clean_keep_awake({"on": False, "held": False, "x": 1}) == {"on": False, "held": False}
        assert sttworkers.clean_keep_awake({"on": True, "held": False, "why": "  refused \n by Windows " + "x" * 60}) == \
            {"on": True, "held": False, "why": ("refused by Windows " + "x" * 60)[:40]}
        assert sttworkers.clean_keep_awake({"on": True, "held": False, "why": 3}) == {"on": True, "held": False}
    finally:
        lv.close()


def test_an_older_hub_reads_it_as_a_key_it_does_not_know():
    """A hub from before keep_awake: its schema had no such key and lets any key through (additionalProperties), so
    the new hello and state are valid to it as they are; the answer the worker reads is the same."""
    for name in ("stt-worker-hello", "stt-worker-state"):
        old = copy.deepcopy(c.load(name))
        assert old["additionalProperties"] is True
        del old["properties"]["keep_awake"]
        from jsonschema import Draft202012Validator
        for file, body in c.vectors(name, "valid"):
            if "keep_awake" in body:
                assert list(Draft202012Validator(old).iter_errors(body)) == [], file
        assert any("keep_awake" in b for _, b in c.vectors(name, "valid"))
    assert c.errors("stt-worker-state", {"state": "online", "model": "m", "keep_awake": {"held": "yes"}}) != []


def test_the_worker_against_an_older_hub_is_unchanged(tmp_path, hub, whisper):
    """The fake hub knows nothing of keep_awake (an older hub): the worker connects, heartbeats and transcribes."""
    from tests.test_stt_worker import tone_wav
    k32 = FakeKernel32()
    w = windows_worker(make_cfg(tmp_path, hub, whisper), k32)
    hub.add_job("clip-ka", tone_wav(1.0))
    with running(w):
        wait_for(lambda: hub.accepted(), what="the job done")
    assert w.counts["done"] == 1 and k32.flags == [SET, NUDGE, CLEAR]


def pool_with(clock, wid="gpu-1", state="online", ka=None, reason=""):
    p = sttpool.Pool(enabled=lambda cls: True, clock=clock, log=lambda s: None)
    p.seen(wid, state, "m", kind="gpu", keep_awake=ka if ka is not None else {}, reason=reason)
    return p


@pytest.mark.parametrize("state, ka, reason, asleep", [
    ("online", {"on": True, "held": True}, "", True),           # stopped abruptly while online (a sleep, by hand too)
    ("online", {}, "", True),                                   # a worker from before: the gap alone
    ("paused", {"on": True, "held": False}, "manual", True),    # paused (released) and then silent
    ("gaming", {"on": False, "held": False}, "manual", True),   # keep_awake off
    ("gaming", {"on": True, "held": True}, "manual", False),    # gaming, then silent: not a sleep's pattern
    ("paused", {}, "manual", False),                            # paused, from before: nothing says it slept
    ("paused", {"on": True, "held": False}, "shutdown", False),  # a clean stop
])
def test_the_asleep_guess(state, ka, reason, asleep):
    clock = Clock()
    p = pool_with(clock, state=state, ka=ka, reason=reason)
    assert p.stats()["workers"][0]["asleep"] is False           # not offline yet
    clock.t += sttpool.OFFLINE_S + 1
    (row,) = p.stats()["workers"]
    assert (row["state"], row["asleep"]) == ("offline", asleep)


def test_a_stations_whisper_is_never_asleep():
    clock = Clock()
    p = sttpool.Pool(enabled=lambda cls: True, clock=clock, log=lambda s: None)
    p.seen("station:st-1", "online", "m", kind="station")
    clock.t += sttpool.OFFLINE_S + 1
    assert p.stats()["workers"][0]["asleep"] is False


def test_the_health_row_says_it():
    """health.js: the GPU worker's state cell: "keeps PC awake" while held, "offline since HH:MM", "(asleep?)"."""
    js = (W.Path(W.__file__).parents[2] / "sizeup" / "web" / "static" / "health.js").read_text()
    assert '" · keeps PC awake"' in js and "`offline since ${fmtTime(w.seen)}`" in js and '" (asleep?)"' in js
    assert "ka.held ?" in js and "w.asleep ?" in js
