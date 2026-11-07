"""The hub's hot/cold deploy (GitHub #247): the S3 single-writer lock (sizeup/leaderlock.py), the supervisor's roles
and handoff (sizeup/handoff.py), the app's fence (sizeup/fence.py). No AWS: a fake S3 with S3's conditional-write
semantics (If-None-Match: * / If-Match: <ETag> -> 412), fake Litestream / app processes in a shared "world" that
records every writer, so a test fails the moment two tasks would write at once."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from sizeup import aws, handoff
from sizeup.fence import EXIT_FENCED, Fence
from sizeup.handoff import DBS, Env, Supervisor
from sizeup.leaderlock import LeaderLock, LockError, LockLost, S3Object, is_free, txid

CREDS = aws.Credentials("AKIDEXAMPLE", "not-a-secret")


# -- a fake S3 with conditional writes ---------------------------------------------------------------------------

class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.mu = threading.Lock()
        self.n = 0
        self.down: set[str] = set()          # who can't reach it (a partition)
        self.lose_answer: set[str] = set()   # who's next PUT lands but whose answer is lost
        self.unconditional = 0
        self.puts: list[dict] = []

    def sender(self, who: str):
        def send(method, url, headers, body, timeout):
            if who in self.down:
                raise OSError("network unreachable")
            key = urlsplit(url).path.lstrip("/")
            assert headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
            with self.mu:
                cur = self.objects.get(key)
                if method == "GET":
                    if cur is None:
                        return 404, {}, b"<Error><Code>NoSuchKey</Code></Error>"
                    return 200, {"ETag": cur[1]}, cur[0]
                assert method == "PUT"
                inm, im = headers.get("If-None-Match"), headers.get("If-Match")
                if inm is None and im is None:
                    self.unconditional += 1
                if inm == "*" and cur is not None:
                    return 412, {}, b"<Error><Code>PreconditionFailed</Code></Error>"
                if im is not None:
                    if cur is None:
                        return 404, {}, b"<Error><Code>NoSuchKey</Code></Error>"
                    if cur[1] != im:
                        return 412, {}, b"<Error><Code>PreconditionFailed</Code></Error>"
                self.n += 1
                etag = f'"{self.n:032x}"'
                self.objects[key] = (bytes(body), etag)
                self.puts.append({"who": who, **json.loads(body)})
                if who in self.lose_answer:
                    self.lose_answer.discard(who)
                    raise OSError("timed out (the write landed)")
                return 200, {"ETag": etag}, b""
        return send

    def doc(self, key="sizeup-hub/leader.json") -> dict | None:
        cur = self.objects.get(key)
        return json.loads(cur[0]) if cur else None

    def overwrite(self, doc: dict, key="sizeup-hub/leader.json") -> None:
        with self.mu:
            self.n += 1
            self.objects[key] = (json.dumps(doc).encode(), f'"{self.n:032x}"')


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def wall(self) -> float:
        return self.t

    def mono(self) -> float:
        return self.t - 999_000.0      # another epoch, the same rate


def lock_for(s3: FakeS3, who: str, clock: Clock | None = None, **kw) -> LeaderLock:
    obj = S3Object("bucket", "sizeup-hub/leader.json", "us-east-1", endpoint="https://s3.test", send=s3.sender(who),
                   creds=lambda: CREDS)
    if clock is not None:
        kw.update(wall=clock.wall, mono=clock.mono)
    return LeaderLock(obj, who, "sha", log=lambda m: None, **kw)


# -- the lock ------------------------------------------------------------------------------------------------------

def test_acquire_on_a_new_bucket_creates_generation_1_and_a_second_task_stands_by():
    s3, c = FakeS3(), Clock()
    a, b = lock_for(s3, "A", c), lock_for(s3, "B", c)
    assert a.try_take() == {"_created": True}
    assert a.generation == 1 and a.held()
    assert b.try_take() is None and not b.held()
    d = s3.doc()
    assert d["holder"] == "A" and d["state"] == "held" and d["generation"] == 1 and d["expires_at"] == c.t + 30
    assert s3.unconditional == 0                      # every write is conditional


def test_renew_extends_the_lease_and_without_renewal_it_runs_out_on_the_holders_side_first():
    s3, c = FakeS3(), Clock()
    a, b = lock_for(s3, "A", c), lock_for(s3, "B", c)
    a.try_take()
    c.t += 9
    a.renew()
    assert s3.doc()["expires_at"] == c.t + 30
    c.t += 19.9
    assert a.held()                                   # 20 s after the last renew was sent: still ours
    c.t += 0.2
    assert not a.held()                               # ... no more: the holder stops writing here
    assert b.try_take() is None                       # the taker still waits (expires_at + 5 s grace)
    c.t += 14.8
    assert b.try_take() is None
    c.t += 0.3                                        # 35 s after the renew was sent
    prev = b.try_take()
    assert prev["holder"] == "A" and b.generation == 2
    with pytest.raises(LockLost):                     # the stale holder can't renew: fenced
        a.renew()
    assert not a.held()


def test_release_records_positions_and_the_next_taker_gets_them_with_generation_plus_one():
    s3, c = FakeS3(), Clock()
    a, b = lock_for(s3, "A", c), lock_for(s3, "B", c)
    a.try_take()
    tag = a.release({"cad": "0000000000000064"}, clean=True)
    d = s3.doc()
    assert d["state"] == "released" and d["clean"] is True and d["positions"] == {"cad": "0000000000000064"}
    assert d["generation"] == 1 and tag and not a.held()
    prev = b.try_take()
    assert prev["positions"] == {"cad": "0000000000000064"} and b.generation == 2
    with pytest.raises(LockLost):
        a.release(None, clean=False)                  # not ours: never written


def test_fencing_a_stale_writer_refuses_renew_and_release():
    s3, c = FakeS3(), Clock()
    a = lock_for(s3, "A", c)
    a.try_take()
    s3.overwrite({"state": "held", "holder": "Z", "generation": 9, "expires_at": c.t + 30})   # someone else wrote it
    with pytest.raises(LockLost):
        a.renew()
    assert a.generation == 0 and not a.held()
    with pytest.raises(LockLost):
        a.release({}, clean=True)
    assert s3.doc()["holder"] == "Z"


def test_two_takers_at_once_exactly_one_wins():
    s3, c = FakeS3(), Clock()
    a, b = lock_for(s3, "A", c), lock_for(s3, "B", c)
    real = s3.sender("B")
    raced = {}

    def b_send(method, url, headers, body, timeout):     # A writes between B's read and B's write
        if method == "PUT" and not raced:
            raced["a"] = a.try_take()
        return real(method, url, headers, body, timeout)
    b.obj.send = b_send
    assert b.try_take() is None
    assert raced["a"] == {"_created": True} and a.generation == 1 and s3.doc()["holder"] == "A"


def test_a_renew_whose_answer_was_lost_is_recognised_and_the_lease_stays_conservative():
    s3, c = FakeS3(), Clock()
    a = lock_for(s3, "A", c)
    a.try_take()
    c.t += 10
    s3.lose_answer.add("A")
    with pytest.raises(LockError):
        a.renew()                                     # landed, but we don't know
    c.t += 2
    a.renew()                                         # 412 on the old ETag: it is our own write -> carry on, then renew
    assert a.held() and s3.doc()["holder"] == "A" and s3.doc()["generation"] == 1


def test_a_take_whose_answer_was_lost_is_adopted():
    s3, c = FakeS3(), Clock()
    a = lock_for(s3, "A", c)
    s3.lose_answer.add("A")
    with pytest.raises(LockError):
        a.try_take()
    assert a.try_take() == {"_adopted": True} and a.generation == 1 and a.held()


def test_retake_only_our_own_untouched_release():
    s3, c = FakeS3(), Clock()
    a, b = lock_for(s3, "A", c), lock_for(s3, "B", c)
    a.try_take()
    tag = a.release({}, clean=True)
    assert a.retake(tag) and a.generation == 2 and a.held()
    tag = a.release({}, clean=True)
    b.try_take()
    assert not a.retake(tag) and s3.doc()["holder"] == "B"


def test_is_free_and_txid():
    assert is_free(None, 0) and is_free({"state": "released"}, 0)
    assert not is_free({"state": "held", "expires_at": 100}, 104) and is_free({"state": "held", "expires_at": 100}, 105.1)
    assert not is_free({"state": "held", "expires_at": "?"}, 1e12)
    assert txid("00000000000000ff") == 255 and txid(7) == 7 and txid("zz") is None and txid(True) is None


def test_the_s3_requests_are_signed_conditional_puts(monkeypatch):
    seen = []

    def send(method, url, headers, body, timeout):
        seen.append((method, url, dict(headers)))
        return (404, {}, b"") if method == "GET" else (200, {"ETag": '"e1"'}, b"")
    obj = S3Object("sizeup-hub-backups-x", "sizeup-hub/leader.json", "us-east-1", send=send, creds=lambda: CREDS)
    assert obj.put({"a": 1}, if_none_match=True) == '"e1"'
    m, url, h = seen[-1]
    assert m == "PUT" and url == "https://sizeup-hub-backups-x.s3.us-east-1.amazonaws.com/sizeup-hub/leader.json"
    assert h["If-None-Match"] == "*" and "if-none-match" in h["Authorization"]
    obj.put({"a": 2}, if_match='"e1"')
    assert seen[-1][2]["If-Match"] == '"e1"' and "if-match" in seen[-1][2]["Authorization"]
    with pytest.raises(ValueError):
        obj.put({"a": 3})                             # never unconditional


# -- the world: fake Litestream and app, shared by every task -------------------------------------------------------

class World:
    def __init__(self, replica: dict | None = None):
        self.s3 = FakeS3()
        self.replica: dict[str, int | None] = dict(replica or {db: 100 for db in DBS})
        self.mu = threading.Lock()
        self.alive: dict[str, set] = {"app": set(), "litestream": set()}
        self.violations: list[str] = []
        self.log: list[tuple[float, str, str]] = []
        self.restore_delay: dict[str, float] = {}
        self.warm_delay: dict[str, float] = {}      # the gated app's warm-up, per task (inf: never warm)
        self.build_delay: dict[str, float] = {}     # after "run": answering "updating" this long, then the CAD serves
        self.connector_down: set[str] = set()       # whose cloudflared never connects
        self.app_fails: set[str] = set()
        self.rogue = False                  # something else moves the replica (a writer from before the lock)
        self.pids = 1000

    def event(self, task: str, what: str) -> None:
        with self.mu:
            self.log.append((time.monotonic(), task, what))

    def when(self, task: str, what: str) -> float:
        return next(t for t, k, w in self.log if k == task and w == what)

    def up(self, task: str, kind: str) -> None:
        with self.mu:
            self.alive[kind].add(task)
            if kind in self.alive and len(self.alive[kind]) > 1:
                self.violations.append(f"two {kind}s at once: {sorted(self.alive[kind])}")
            self.log.append((time.monotonic(), task, f"{kind} up"))

    def down(self, task: str, kind: str) -> None:
        with self.mu:
            self.alive[kind].discard(task)
            self.log.append((time.monotonic(), task, f"{kind} down"))


class FakeProc:
    def __init__(self, world: World, task: str, kind: str, on_poll=None, track: bool = True):
        world.pids += 1
        self.pid, self.world, self.task, self.kind, self.on_poll = world.pids, world, task, kind, on_poll
        self.code = None
        self.track = track
        if track:
            world.up(task, kind)

    def poll(self):
        if self.code is None and self.on_poll:
            self.on_poll(self)
        return self.code

    def _end(self, code):
        if self.code is None:
            self.code = code
            if self.track:
                self.world.down(self.task, self.kind)

    def stop(self, timeout, sig=None):
        self._end(0)
        return self.code

    def kill(self):
        self._end(-9)


class FakeApp(FakeProc):
    """The gated app (sizeup/app.py run_gated) as its supervisor sees it, moved on by poll(): "warm" after its
    warm-up; the gate's "init" -> the databases made (only with a fresh lease, like the app's fence) and "ok";
    "run" -> it writes (an app in the world) and its port answers ("updating" for build_delay, then it serves)."""

    def __init__(self, env: FakeEnv, gate: Path, lease: Path):
        self.env, self.gate, self.lease = env, gate, lease
        self.answer = Path(f"{gate}.answer")
        self.t0, self.phase = time.monotonic(), "warming"
        super().__init__(env.world, env.task, "app", self._step, track=False)

    def _say(self, text: str) -> None:
        self.answer.write_text(text + "\n")

    def _step(self, _p) -> None:
        try:
            said = self.gate.read_text().strip()
        except OSError:
            said = ""
        if self.phase == "warming":
            if time.monotonic() - self.t0 < self.world.warm_delay.get(self.task, 0.0):
                return
            self._say("warm")
            self.phase = "warm"
        if said in ("init", "run") and self.phase == "warm":
            if not Fence(self.lease, log=lambda m: None).ok():
                self._say("error: no fresh lease")
                self._end(75)
                return
            self.track = True                                  # a writer from here: it opens the databases
            self.world.up(self.task, "app")
            if said == "init":
                self.env.calls.append("init_state")
                for db in DBS:
                    self.env.db_path(db).touch()
                self._say("ok")
                self.phase = "ready"
        if said == "run" and self.phase in ("warm", "ready"):
            self.env.calls.append("app")
            self.env.written = {db: 5 for db in DBS}         # what it writes while it runs
            self.phase, self.run_at = "listening", time.monotonic()
        if self.phase == "listening" and time.monotonic() - self.run_at >= self.world.build_delay.get(self.task, 0.0):
            self.phase = "serving"


class FakeEnv(Env):
    def __init__(self, world: World, task: str, root: Path):
        super().__init__("cfg.toml", root / "data", "ls.yml", "sock", port=0)
        self.state_dir.mkdir(parents=True)
        self.world, self.task = world, task
        self.calls: list[str] = []
        self.app: FakeProc | None = None
        self.written: dict[str, int] = {}

    def plan(self, db):
        self.calls.append(f"plan {db}")
        if self.world.rogue and self.world.replica[db] is not None:
            self.world.replica[db] += 1
        return self.world.replica[db]

    def follower(self, db, out):
        self.calls.append(f"follower {db}")
        out.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        delay = self.world.restore_delay.get(self.task, 0.0)

        def follow(p):
            if self.world.replica[db] is None:
                p._end(0)                             # -if-replica-exists: none
                return
            if time.monotonic() - start < delay:
                return
            out.write_bytes(b"SQLite format 3\0")
            Path(f"{out}-txid").write_text(f"{self.world.replica[db]:016x}\n")
        return FakeProc(self.world, self.task, f"follower {db}", follow, track=False)

    def restore(self, db, out):
        self.calls.append(f"restore {db}")
        out.write_bytes(b"SQLite format 3\0")
        return self.world.replica[db]

    def replicate(self):
        self.calls.append("replicate")
        return FakeProc(self.world, self.task, "litestream")

    def sync(self, db, timeout):
        self.calls.append(f"sync {db}")
        assert self.task in self.world.alive["litestream"] and self.task not in self.world.alive["app"]
        n = self.written.pop(db, 0)
        if self.world.replica[db] is None:
            self.world.replica[db] = 0
        self.world.replica[db] += n
        return self.world.replica[db]

    def start_app(self, env):
        self.calls.append("app start")
        assert env.get("SIZEUP_LEASE_FILE") and env.get("SIZEUP_APP_GATE")
        self.app = FakeApp(self, Path(env["SIZEUP_APP_GATE"]), Path(env["SIZEUP_LEASE_FILE"]))
        return self.app

    def _serving(self) -> bool:
        return self.app is not None and self.app.poll() is None and self.app.phase == "serving"

    def app_answers(self):
        return self.app is not None and self.app.poll() is None and self.app.phase in ("listening", "serving")

    def app_healthy(self):
        return self._serving() and self.task not in self.world.app_fails

    def connector_ready(self):
        return self.task not in self.world.connector_down

    def quick_check(self, path):
        return "ok"

    def warm_caches(self, deadline_s):
        self.calls.append("warm_caches")


FAST = dict(POLL_S=0.05, CATCHUP_S=2.0, RESTORE_S=4.0, APP_READY_S=1.5, CONNECTOR_READY_S=0.2, APP_STOP_S=1.0,
            SYNC_S=2.0, LITESTREAM_STOP_S=1.0, OVERLAP_S=3.0, RETAKE_S=0.8, STOP_TIMEOUT_S=6.0, REDRAIN_S=1.5,
            RENEW_S=0.15, RETAKE_MIN_LEFT_S=1.0, CREATED_WAIT_S=0.1, KILL_MARGIN_S=0.3, INIT_S=2.0, APP_LISTEN_S=1.0,
            WARM_APP_RETRY_S=0.3)
LEASE = dict(ttl=1.0, safety=0.4, grace=0.3)        # holder: 0.6 s after a renew; taker: 1.3 s


class Task:
    def __init__(self, world: World, name: str, tmp: Path, **timing):
        self.world, self.name = world, name
        root = tmp / name
        self.env = FakeEnv(world, name, root)
        obj = S3Object("bucket", "sizeup-hub/leader.json", "us-east-1", endpoint="https://s3.test",
                       send=world.s3.sender(name), creds=lambda: CREDS)
        self.lock = LeaderLock(obj, name, "sha", log=lambda m: None, **LEASE)
        self.sup = Supervisor(self.lock, self.env, root / "run", root / "shared", control_port=0, timing={**FAST, **timing})
        self.code = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.code = self.sup.run()

    def start(self) -> Task:
        self.thread.start()
        return self

    def wait(self, timeout=15.0) -> int | None:
        self.thread.join(timeout)
        return self.code

    @property
    def leader_file(self) -> Path:
        return self.sup.leader_file


def finish(task: Task) -> int | None:
    """The end of a test: stop it quickly (no retake, a short overlap)."""
    task.sup.t.update(RETAKE_S=1e9, OVERLAP_S=0.1)
    task.sup.on_term()
    return task.wait()


def until(cond, timeout=10.0, what="condition") -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def health(task: Task) -> int:
    port = task.sup.control_srv.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def world():
    return World()


def test_a_cold_start_with_no_lock_takes_generation_1_and_serves(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active", what="A active")
    assert a.lock.generation == 1 and world.s3.doc()["phase"] == "serving"
    assert a.leader_file.read_text().strip() == "1"
    assert a.env.calls.index("init_state") < a.env.calls.index("replicate") < a.env.calls.index("app")
    for db in DBS:
        assert a.env.db_path(db).exists() and not a.sup.staged(db).exists()      # promoted
    lease = json.loads(a.sup.lease_file.read_text())
    assert lease["generation"] == 1 and lease["litestream_pid"] and lease["valid_until"] > time.monotonic()
    assert health(a) == 200
    assert finish(a) == 0
    assert world.s3.doc()["state"] == "released" and world.s3.doc()["clean"] is True
    assert not a.leader_file.exists() and world.violations == []


def test_the_standby_never_writes_never_replicates_never_connects(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.restore_delay["B"] = 0.6
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby", what="B standby")
    assert health(b) == 503                                   # not warm yet: ECS doesn't stop A for it
    until(lambda: b.sup.warm(), what="B warm")
    assert health(b) == 200 and b.sup.status()["role"] == "standby"
    time.sleep(0.5)
    assert not ({"replicate", "app", "init_state"} & set(b.env.calls))
    assert "B" not in world.alive["app"] and "B" not in world.alive["litestream"]
    assert not b.leader_file.exists()                         # the connector never starts
    assert not any(b.env.db_path(db).exists() for db in DBS)  # only /data/standby/ is written
    assert all(b.sup.staged(db).exists() for db in DBS)
    assert all(p["who"] != "B" for p in world.s3.puts)       # never touched the lock
    b.sup.on_term()                                           # a standby stopped: it just goes
    assert b.wait() == 0 and world.s3.doc()["holder"] == "A" and a.sup.role == "active"
    finish(a)


def test_the_handoff_old_drains_syncs_releases_new_catches_up_and_serves(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    a.sup.on_term()                                           # ECS: B is healthy, A is stopped
    assert a.wait() == 0
    until(lambda: b.sup.role == "active", what="B active")
    rel = next(p for p in world.s3.puts if p["state"] == "released" and p["who"] == "A")
    assert rel["clean"] and {db: int(v, 16) for db, v in rel["positions"].items()} == {db: 105 for db in DBS}
    assert b.lock.generation == a.lock.generation + 0 or b.lock.generation == 2
    assert world.s3.doc()["holder"] == "B" and world.s3.doc()["generation"] == 2 and world.s3.doc()["phase"] == "serving"
    # the order: A's app gone, A's Litestream gone (after its sync), the release, then B writes
    t = world.when
    assert t("A", "app down") < t("A", "litestream down") < t("B", "app up") < t("B", "litestream up")
    assert b.env.calls.index("init_state") < b.env.calls.index("replicate") < b.env.calls.index("app")
    assert [c for c in a.env.calls if c.startswith("sync")] == [f"sync {db}" for db in DBS]
    assert b.sup.positions == {db: f"{105:016x}" for db in DBS}         # caught up to the release, by the followers
    assert not any(c.startswith("restore") for c in b.env.calls)        # incremental: no full restore
    assert any("the new leader B serves" in e for e in a.sup.events)
    assert not a.leader_file.exists() and b.leader_file.exists()
    assert world.violations == []
    finish(b)


def test_a_crashed_leader_its_lease_runs_out_and_the_standby_takes_over(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    world.s3.down.add("A")                                    # A can't renew (frozen, partitioned: the same to B)
    assert a.wait() == handoff.EXIT_FENCED                    # it fences itself: app and Litestream killed
    until(lambda: b.sup.role == "active", what="B takes over")
    assert world.when("A", "app down") < world.when("B", "app up")
    assert world.when("A", "litestream down") < world.when("B", "litestream up")
    assert b.lock.generation == 2 and not a.leader_file.exists()
    assert world.violations == []
    finish(b)


def test_split_brain_the_lock_written_by_another_fences_the_old_leader_at_once(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.s3.overwrite({"v": 1, "state": "held", "holder": "Z", "generation": 7, "expires_at": time.time() + 60,
                        "phase": "serving", "nonce": "x"})
    t0 = time.monotonic()
    assert a.wait() == handoff.EXIT_FENCED
    assert time.monotonic() - t0 < 1.0                        # at its next renew, not when the lease runs out
    assert "A" not in world.alive["app"] and "A" not in world.alive["litestream"]
    assert not a.leader_file.exists() and not a.sup.lease_file.exists()
    assert any(e.startswith("FENCED") for e in a.sup.events)


def test_a_new_task_that_never_gets_warm_is_never_healthy_and_the_old_one_carries_on(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.restore_delay["B"] = 1e9
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby")
    time.sleep(0.6)
    assert health(b) == 503 and a.sup.role == "active"        # ECS never stops A; the circuit breaker rolls back
    b.sup.on_term()
    assert b.wait() == 0 and world.s3.doc()["holder"] == "A" and world.s3.doc()["generation"] == 1
    finish(a)


def test_a_new_task_that_fails_before_writing_hands_back_and_the_old_one_takes_it_back(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    b.env.quick_check = lambda path: "*** in database main ***"     # B's copy is bad: it never starts writing
    a.sup.on_term()
    until(lambda: b.code is not None, what="B gives up")
    assert b.code == 1 and "replicate" not in b.env.calls and "app" not in b.env.calls
    until(lambda: a.sup.role == "active" and "A" in world.alive["app"], what="A takes it back")
    assert any("taken back" in e for e in a.sup.events) and a.leader_file.exists()
    assert world.s3.doc()["holder"] == "A" and world.s3.doc()["generation"] == 3   # A 1, B 2, A 3
    assert a.wait(10) == 0                                    # it drains again before ECS's SIGKILL
    assert world.s3.doc()["state"] == "released" and world.violations == []


def test_a_new_task_that_wrote_then_failed_is_never_resumed_over_the_next_task_restores(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    world.app_fails.add("B")                                  # B's app wrote, then never answered /healthz
    a.sup.on_term()
    until(lambda: b.code is not None, what="B gives up")
    assert b.code == 1 and not b.leader_file.exists()
    assert a.wait(10) == 0                                    # A's files are behind B's writes: not resumed from
    assert any("moved on since our release" in e for e in a.sup.events)
    assert "A" not in world.alive["app"]
    c = Task(world, "C", tmp_path).start()                    # ECS's next task (or the rollback) takes over
    until(lambda: c.sup.role == "active", what="C active")
    assert c.sup.positions == {db: f"{110:016x}" for db in DBS}   # with B's writes
    assert world.violations == []
    finish(c)


def test_nobody_takes_it_within_t_the_old_leader_takes_it_back(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    a.sup.on_term()                                           # no standby at all (or it died)
    until(lambda: any("taken back" in e for e in a.sup.events), what="retake")
    until(lambda: "A" in world.alive["app"])
    assert a.wait(10) == 0 and world.violations == []


def test_no_backup_but_a_leader_ran_before_refuses_to_start_empty(world, tmp_path):
    world.s3.overwrite({"v": 1, "state": "released", "holder": "OLD", "generation": 3, "positions": None,
                        "clean": False, "nonce": "n"})
    world.replica = {db: None for db in DBS}
    a = Task(world, "A", tmp_path).start()
    assert a.wait() == 1
    assert "app" not in a.env.calls and "replicate" not in a.env.calls
    assert any("refusing to start empty" in e for e in a.sup.events)


def test_a_first_start_ever_with_no_backup_creates_the_databases(world, tmp_path):
    world.replica = {db: None for db in DBS}
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    assert a.sup.no_replica == set(DBS) and "init_state" in a.env.calls
    assert finish(a) == 0


def test_a_replica_that_moves_while_we_hold_the_lock_is_never_promoted(world, tmp_path):
    world.rogue = True                                        # a hub from before the lock, still writing
    a = Task(world, "A", tmp_path).start()
    assert a.wait() == 1
    assert "app" not in a.env.calls and any("another writer" in e for e in a.sup.events)


def test_a_follower_that_falls_behind_falls_back_to_a_full_restore(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    world.restore_delay["B"] = 0.0
    b = Task(world, "B", tmp_path, CATCHUP_S=0.3).start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    orig = b.env.follower

    def frozen(db, out):                                       # a restarted follower that never moves on
        p = orig(db, out)
        p.on_poll = None
        return p
    for db in DBS:
        b.sup.followers[db].on_poll = None                     # stuck at 100
    b.env.follower = frozen
    a.sup.on_term()
    until(lambda: b.sup.role == "active", what="B active")
    assert [c for c in b.env.calls if c.startswith("restore")] == [f"restore {db}" for db in DBS]
    assert world.violations == []
    finish(b)


# -- the app's fence ------------------------------------------------------------------------------------------------

def test_the_app_fence_kills_litestream_and_exits_the_moment_the_lease_is_stale(tmp_path):
    lease = tmp_path / "lease.json"
    now = [100.0]
    killed, exited = [], []
    f = Fence(lease, mono=lambda: now[0], log=lambda m: None, exit_fn=exited.append, kill=lambda p, s: killed.append(p))
    assert not f.ok()                                          # no lease file: never ours
    lease.write_text(json.dumps({"generation": 3, "valid_until": 120.0, "litestream_pid": 4242}))
    now[0] += 1
    assert f.ok() and f.check() and f.generation() == 3
    now[0] = 121.0
    assert not f.check()
    assert killed == [4242] and exited == [EXIT_FENCED] and not f.ok()


def test_the_supervisor_keeps_the_lease_file_fresh_for_the_app(world, tmp_path):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    f = Fence(a.sup.lease_file, log=lambda m: None)
    for _ in range(5):
        time.sleep(0.25)
        assert f.ok()
    world.s3.down.add("A")
    assert a.wait() == handoff.EXIT_FENCED
    assert not f.ok()
