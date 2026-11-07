"""#264: the handover without a gap. The old task's connector stays up until the new leader serves (never zero
connectors on the tunnel), the new app is warm before the takeover and listens the moment it may, the drained port
answers "updating" at once, each phase's time is logged, and cachesync's saves never race. The supervisor runs
against tests/test_handoff.py's fake world (every writer recorded); the gated app is the real one, on a fresh hub
database under tmp_path. Nothing reaches AWS."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sizeup import app as app_mod
from sizeup import handoff
from sizeup.cachesync import CacheMirror
from sizeup.fence import Fence
from sizeup.timing import Phases
from tests.test_cloud import free_port, hub_cfg
from tests.test_handoff import DBS, Task, World, finish, health, until

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def soon(fn, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def get(url: str, accept: str = ""):
    req = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    try:
        with DIRECT.open(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture
def world():
    return World()


@pytest.fixture(autouse=True)
def _no_warm_left():
    yield
    app_mod._WARM.clear()


class Connectors:
    """Samples both tasks' leader files (each task's connector runs exactly while its file is there)."""

    def __init__(self, *tasks: Task):
        self.tasks, self.samples, self.stop = tasks, [], threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            self.samples.append(tuple(t.leader_file.exists() for t in self.tasks))
            time.sleep(0.005)

    def __enter__(self) -> Connectors:
        self.thread.start()
        return self

    def __exit__(self, *a) -> None:
        self.stop.set()
        self.thread.join(2)


# -- the warm app ----------------------------------------------------------------------------------------------------

def test_the_standby_warms_the_app_and_the_takeover_uses_that_one(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm(), what="B warm")
    assert b.env.calls.count("app start") == 1 and b.sup.app_warm.is_set()
    assert "B" not in world.alive["app"] and not ({"app", "init_state", "replicate"} & set(b.env.calls))
    assert b.sup.status()["app_warm"] is True
    a.sup.on_term()
    until(lambda: b.sup.role == "active", what="B active")
    assert b.env.calls.count("app start") == 1                       # the warm one, not a second start
    assert not any("no warm app" in e for e in b.sup.events)
    c = b.env.calls
    assert c.index("init_state") < c.index("replicate") < c.index("app")
    assert world.violations == []
    assert a.wait() == 0
    finish(b)


def test_a_standby_is_healthy_only_once_its_app_is_warm(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.warm_delay["B"] = 0.8
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.caches_warm.is_set() and b.sup.follow_done >= set(DBS) and "app start" in b.env.calls)
    assert not b.sup.app_warm.is_set() and health(b) == 503            # ECS doesn't stop A yet
    until(lambda: b.sup.app_warm.is_set(), what="B's app warm")
    assert health(b) == 200
    b.sup.on_term()
    assert b.wait() == 0 and b.env.app.code is not None                # the warm app stopped with it
    finish(a)


def test_a_warm_app_that_dies_is_started_again_and_the_standby_is_unhealthy_meanwhile(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.warm_delay["B"] = 0.0
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.app_warm.is_set())
    world.warm_delay["B"] = 0.6                                         # the next one takes a while
    b.env.app.kill()                                                    # (OOM)
    until(lambda: any("warm app exited" in e for e in b.sup.events))
    assert not b.sup.warm() and health(b) == 503                        # ECS doesn't stop A for it
    until(lambda: b.env.calls.count("app start") == 2, what="started again")
    until(lambda: b.sup.warm(), what="warm again")
    assert health(b) == 200
    b.sup.on_term()
    assert b.wait() == 0
    finish(a)


def test_a_bad_image_whose_app_keeps_dying_never_gets_the_leader_stopped(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path).start()
    until(lambda: "app start" in b.env.calls)
    for n in range(1, 4):                                               # it crashes at every start (import error)
        until(lambda: b.env.calls.count("app start") == n, what=f"start {n}")
        b.env.app.kill()
        until(lambda: sum("warm app exited" in e for e in b.sup.events) == n)
    assert any("given up" in e for e in b.sup.events)
    time.sleep(0.6)                                                     # past WARM_APP_RETRY_S: no fourth start
    assert b.env.calls.count("app start") == 3 and not b.sup.warm() and health(b) == 503
    assert a.sup.role == "active" and world.s3.doc()["holder"] == "A"  # ECS's circuit breaker rolls back
    b.sup.on_term()
    assert b.wait() == 0
    finish(a)


# -- never zero connectors ------------------------------------------------------------------------------------------

def test_the_old_connector_stays_up_until_the_new_leader_serves_never_none_between(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.build_delay["B"] = 1.0                                         # B's app answers "updating" a while first
    b = Task(world, "B", tmp_path, APP_READY_S=5.0).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    a.sup.t["OVERLAP_S"] = handoff.OVERLAP_S      # the real cap (bounded by FAST's STOP_TIMEOUT_S: ECS's SIGKILL)
    with Connectors(a, b) as seen:
        a.sup.on_term()
        until(lambda: b.sup.role == "active", what="B active")
        assert a.wait() == 0
        time.sleep(0.1)
    assert all(any(s) for s in seen.samples), "a moment with no connector at all"
    assert (True, True) in seen.samples                                 # both, for a moment: never none
    assert seen.samples[-1] == (False, True)
    served = next(e for e in a.sup.events if "the new leader B serves" in e)
    assert "of overlap" in served and "since SIGTERM" in served
    assert world.violations == []
    finish(b)


def test_the_new_connector_starts_while_the_app_is_still_being_built(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.build_delay["B"] = 1.0
    b = Task(world, "B", tmp_path, APP_READY_S=5.0).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    a.sup.on_term()
    until(lambda: b.leader_file.exists(), what="B's leader file")
    assert b.sup.role == "takeover" and not b.env.app_healthy()          # the port answers "updating", no CAD yet
    assert world.s3.doc()["phase"] != "serving" and a.leader_file.exists()   # A's connector still up
    until(lambda: b.sup.role == "active")
    assert world.s3.doc()["phase"] == "serving"
    assert any("leader file written (the app's port answers)" in e for e in b.sup.events)
    assert a.wait() == 0
    finish(b)


def test_the_overlap_ends_cleanly_before_ecs_kills_the_task(world, tmp_path):
    a = Task(world, "A", tmp_path, STOP_TIMEOUT_S=2.5, KILL_MARGIN_S=0.5, RETAKE_S=1e9).start()
    until(lambda: a.sup.role == "active")
    world.build_delay["B"] = 60.0                                        # B never serves in time
    b = Task(world, "B", tmp_path, APP_READY_S=60.0).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    a.sup.t["OVERLAP_S"] = 180.0
    a.sup.on_term()
    t0 = time.monotonic()
    assert a.wait(10) == 0
    assert time.monotonic() - t0 < 2.5                                   # before the SIGKILL
    assert any("ECS stops this task in moments" in e for e in a.sup.events)
    assert not a.leader_file.exists() and b.leader_file.exists()         # B's connector up ("updating") meanwhile
    b.sup.t.update(APP_READY_S=0.1)
    world.build_delay["B"] = 0.0
    until(lambda: b.sup.role == "active")
    finish(b)


def test_serving_is_said_only_once_the_new_connector_is_up(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.connector_down.add("B")                                       # B's cloudflared doesn't connect (yet)
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    with Connectors(a, b) as seen:
        a.sup.on_term()
        until(lambda: b.sup.role == "active", what="B active")
        time.sleep(1.0)                                                 # past CONNECTOR_READY_S: still not "serving"
        assert world.s3.doc()["phase"] == "connecting" and a.leader_file.exists()
        assert a.code is None                                           # A's connector stays: no 1033
        world.connector_down.discard("B")
        until(lambda: world.s3.doc()["phase"] == "serving", what="serving once the connector is up")
        assert a.wait() == 0
    assert all(any(s) for s in seen.samples)
    assert any("the new leader B serves" in e for e in a.sup.events)
    finish(b)


def test_a_connector_that_never_comes_up_keeps_the_old_one_until_shortly_before_the_sigkill(world, tmp_path):
    a = Task(world, "A", tmp_path, STOP_TIMEOUT_S=3.0, KILL_MARGIN_S=0.5, RETAKE_S=1e9).start()
    until(lambda: a.sup.role == "active")
    world.connector_down.add("B")
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    a.sup.t["OVERLAP_S"] = 180.0
    a.sup.on_term()
    t0 = time.monotonic()
    assert a.wait(10) == 0
    assert 2.0 < time.monotonic() - t0 < 3.0                           # held to kill - margin, not cut at "active"
    assert any("ECS stops this task in moments" in e for e in a.sup.events)
    assert world.s3.doc()["phase"] == "connecting"
    finish(b)


def test_a_late_renew_never_brings_the_lease_file_back_after_the_release(world, tmp_path):
    t = Task(world, "A", tmp_path)
    t.lock.try_take()
    t.sup.start_renewing()
    assert t.sup.lease_file.exists()
    t.sup.stop_renewing()
    t.sup.drop_lease()
    t.sup.write_lease()                                                 # a renew that finished just after
    assert not t.sup.lease_file.exists()
    t.sup.start_renewing()                                              # the next take: written again
    assert t.sup.lease_file.exists()
    t.sup.stop_renewing()


# -- the drained port ----------------------------------------------------------------------------------------------

def test_the_updating_answer_takes_the_port_the_moment_the_app_lets_go(world, tmp_path):
    port = free_port()
    t = Task(world, "A", tmp_path)
    t.sup.env.port = port
    app_sock = socket.create_server(("127.0.0.1", port))               # the draining app still has it
    t.sup.take_port()
    time.sleep(0.3)
    assert t.sup.updating_srv is None
    app_sock.close()                                                     # the app's server let go
    until(lambda: t.sup.updating_srv is not None, timeout=2, what="the port taken")
    code, hdr, body = get(f"http://127.0.0.1:{port}/cad", accept="text/html")
    assert code == 503 and hdr["Retry-After"] == "2" and b"update in progress" in body
    code, _, body = get(f"http://127.0.0.1:{port}/api/cad/state")
    assert code == 503 and json.loads(body)["updating"] is True
    code, _, body = get(f"http://127.0.0.1:{port}/healthz")
    assert code == 503 and json.loads(body)["serving"] is False         # never mistaken for the app
    t.sup.free_port()
    with socket.create_server(("127.0.0.1", port)):                     # given back: a retake's app can bind it
        pass


# -- where the time goes --------------------------------------------------------------------------------------------

def test_phases_add_up_and_read_as_one_line():
    now = [0.0]
    p = Phases("takeover", clock=lambda: now[0])
    now[0] = 1.25
    p.mark("plan")
    now[0] = 1.5
    p.skip()
    now[0] = 2.0
    p.mark("app")
    now[0] = 3.0
    p.mark("app")
    assert p.parts == {"plan": 1.25, "app": 1.5}
    assert p.line("x") == "takeover timing: plan 1.2 s · app 1.5 s (total 3.0 s; x)"


def test_the_takeover_and_the_drain_log_where_the_time_went(world, tmp_path, capsys):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    a.sup.on_term()
    until(lambda: b.sup.role == "active")
    assert a.wait() == 0
    out = capsys.readouterr().out
    take = [ln for ln in out.splitlines() if "takeover timing:" in ln]
    assert take, out[-2000:]
    for phase in ("plan", "catch-up", "verify", "promote", "init-state", "litestream", "listen", "app", "connector"):
        assert f" {phase} " in take[-1], take[-1]
    drain = next(ln for ln in out.splitlines() if "drain timing:" in ln)
    for phase in ("app", "sync", "litestream", "release"):
        assert f" {phase} " in drain
    assert "since SIGTERM" in drain
    finish(b)


def test_the_three_plans_are_asked_at_once(world, tmp_path):
    t = Task(world, "A", tmp_path)
    inside, most = [0], [0]
    mu = threading.Lock()

    def slow_plan(db):
        with mu:
            inside[0] += 1
            most[0] = max(most[0], inside[0])
        time.sleep(0.2)
        with mu:
            inside[0] -= 1
        return 100
    t.env.plan = slow_plan
    t0 = time.monotonic()
    assert t.sup._plan_all() == {db: 100 for db in DBS}
    assert most[0] == 3 and time.monotonic() - t0 < 0.5


# -- the gated app (the real one) -------------------------------------------------------------------------------------

def _gated(tmp_path, monkeypatch, lease: bool = True):
    """run_gated in a thread, as the supervisor starts it: (cfg, gate, thread, result, servers)."""
    monkeypatch.setattr(app_mod.App, "start_layers", lambda self: None)        # no providers, no network
    monkeypatch.setattr(Fence, "watch", lambda self, every=0.5: None)          # (its os._exit is not for pytest)
    cfg = hub_cfg(tmp_path, port=free_port(), gis_layers=())
    run = tmp_path / "run"
    run.mkdir()
    gate, lease_file = run / "app-gate", run / "lease.json"
    if lease:
        lease_file.write_text(json.dumps({"generation": 4, "valid_until": time.monotonic() + 600}))
    monkeypatch.setenv("SIZEUP_LEASE_FILE", str(lease_file))
    servers, made = [], threading.Event()
    real_server = app_mod.make_server

    def capture(app, fence=None, listener=None):
        s = real_server(app, fence, listener)
        servers.append((app, s))
        made.set()
        return s
    monkeypatch.setattr(app_mod, "make_server", capture)
    result: list = []
    th = threading.Thread(target=lambda: result.append(app_mod.run_gated(cfg, gate)), daemon=True)
    th.start()
    return cfg, gate, th, result, servers, made


def _answer(gate: Path) -> str:
    try:
        return Path(f"{gate}.answer").read_text().strip()
    except OSError:
        return ""


def test_the_gated_app_warms_waits_opens_the_databases_then_listens_at_once(tmp_path, monkeypatch, capsys):
    building, go = threading.Event(), threading.Event()
    real_make = app_mod.make_app

    def slow_make(cfg):
        building.set()
        assert go.wait(10)
        return real_make(cfg)
    monkeypatch.setattr(app_mod, "make_app", slow_make)
    cfg, gate, th, result, servers, made = _gated(tmp_path, monkeypatch)
    state = Path(cfg.cad_db).parent
    url = f"http://127.0.0.1:{cfg.port}"
    try:
        assert soon(lambda: _answer(gate) == "warm", 30)
        assert not any(state.glob("*.sqlite"))                             # warm: no database touched
        time.sleep(0.2)
        with pytest.raises(OSError):
            DIRECT.open(url + "/healthz", timeout=1)                         # nothing on the port while standing by
        gate.write_text("init\n")
        assert soon(lambda: _answer(gate) == "ok", 30)
        assert all((state / f"{db}.sqlite").exists() for db in DBS)        # --init-state's work, in this process
        assert not building.is_set()                                        # waits for "run" (Litestream first)
        gate.write_text("run\n")
        assert building.wait(10)
        code, hdr, body = get(url + "/cad", accept="text/html")             # listening already, "updating"
        assert code == 503 and hdr["Retry-After"] == "2" and b"update in progress" in body
        assert get(url + "/healthz")[0] == 503
        go.set()
        assert made.wait(30)
        assert soon(lambda: get(url + "/healthz")[0] == 200, 10)
        h = json.loads(get(url + "/healthz")[2])
        assert h["serving"] is True and h["hub_role"] == "active"
        out = capsys.readouterr().out
        assert "warm-up timing: imports" in out and "gazetteer" in out
        line = next(ln for ln in out.splitlines() if "startup timing:" in ln)
        for phase in ("init-state", "litestream", "listen", "cad", "start", "server"):
            assert f" {phase} " in line, line
    finally:
        go.set()
        for app, s in servers:
            s.shutdown()
        th.join(10)
    assert result == [0]


def test_a_gated_app_without_a_fresh_lease_never_opens_the_databases(tmp_path, monkeypatch):
    cfg, gate, th, result, servers, made = _gated(tmp_path, monkeypatch, lease=False)
    assert soon(lambda: _answer(gate) == "warm", 30)
    gate.write_text("init\n")
    th.join(10)
    assert result == [75] and _answer(gate).startswith("error")
    assert not any(Path(cfg.cad_db).parent.glob("*.sqlite"))


def test_a_gated_app_told_run_at_once_skips_init_state(tmp_path, monkeypatch):
    # a retake: the files are this task's own, already open-able; straight to serving
    from sizeup.app import init_state
    cfg, gate, th, result, servers, made = _gated(tmp_path, monkeypatch)
    init_state(cfg)
    called = []
    monkeypatch.setattr(app_mod, "init_state", lambda c: called.append(c))
    gate.write_text("run\n")
    try:
        assert made.wait(30)
        assert soon(lambda: get(f"http://127.0.0.1:{cfg.port}/healthz")[0] == 200, 10)
        assert called == []
    finally:
        for app, s in servers:
            s.shutdown()
        th.join(10)


def test_make_app_takes_what_prewarm_built(tmp_path, monkeypatch):
    cfg = hub_cfg(tmp_path, gis_layers=())
    app_mod._WARM.clear()
    before = sorted(p.name for p in tmp_path.rglob("*"))
    app_mod.prewarm(cfg)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before        # nothing written
    gaz, prov = app_mod._WARM.get("gaz"), app_mod._WARM.get("providers")
    assert prov is not None
    app = app_mod.make_app(cfg)
    try:
        assert app.gaz is gaz and app.roads is prov["roads"] and app.weather is prov["weather"]
        assert app.gis is prov["gis"] and app.trains is prov["trains"] and app.aircraft is prov["aircraft"]
        assert app.roadwork is prov["roadwork"]
        assert set(app_mod._WARM) <= {"_for", "atc_rules"}
    finally:
        app.stop()
        app_mod._WARM.clear()


# -- cachesync: saves never race ---------------------------------------------------------------------------------------

def test_cachesync_saves_from_many_threads_never_race(tmp_path):
    logs: list[str] = []
    m = CacheMirror(tmp_path, "bucket", log=logs.append, send=lambda *a: (500, b""))
    for i in range(20):
        m.files[f"gis/l{i}.geojson"] = {"sha": f"{i:064x}", "up": 1.0, "fails": 0, "retry": 0.0, "error": "",
                                        "restored": True}
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            for _ in range(40):
                m._save_state()
                m.save_manifest()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and not [ln for ln in logs if "not saved" in ln or "not written" in ln], logs[:3]
    assert len(json.loads((tmp_path / "cachesync.json").read_text())["files"]) == 20
    assert len(json.loads((tmp_path / ".cachesync-restored.json").read_text())) == 20
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]       # no temp file left behind
