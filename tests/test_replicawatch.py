"""The replicas verified all the time, and a task that fails said at once (sizeup/replicawatch.py, the supervisor's hooks in
sizeup/handoff.py, Settings > Health's Replicas row, tools/deploy_check.py replicas). 2026-11-13: a corrupt cad.sqlite
replica sat unnoticed ~110 min, then every new task crash-looped ~35 min. No AWS: the fake S3 of tests/test_handoff.py
(conditional writes), a local fake ntfy (tests/test_alarms.py), synthetic SQLite files made corrupt on purpose."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sizeup import replicawatch as rw
from sizeup.leaderlock import S3Object
from sizeup.replicawatch import Pusher, ReplicaWatch, WatchStore, integrity
from tests.test_alarms import FakeNtfy
from tests.test_handoff import CREDS, FakeS3, Task, World, finish, until

TOPIC = "sizeup-test-status-0000"        # made up: never a real topic
KEY = "sizeup-hub/replica-watch.json"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _notify_stays_local(monkeypatch):
    """Pushes only ever reach the local fake ntfy, never a Tapout topic."""
    from urllib.parse import urlsplit

    import sizeup.notify as notify
    real = notify.urlopen

    def local_only(req, *a, **kw):
        assert urlsplit(req.full_url).hostname in ("127.0.0.1", "localhost"), req.full_url
        assert json.loads(req.data)["topic"] == TOPIC
        return real(req, *a, **kw)
    monkeypatch.setattr(notify, "urlopen", local_only)


@pytest.fixture
def ntfy():
    f = FakeNtfy()
    yield f
    f.close()


class Clock:
    def __init__(self, t: float = 1_763_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_db(path: Path, rows: int = 2000) -> Path:
    """A hub-shaped file: a radio log table with an index (made-up rows), checkpointed."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE transmissions(id TEXT PRIMARY KEY, start REAL, source TEXT, text TEXT)")
    con.execute("CREATE INDEX tx_start ON transmissions(start)")
    con.executemany("INSERT INTO transmissions VALUES (?,?,?,?)",
                    [(f"FG1-{i}", 1_763_000_000 + i * 7.5, "FG1", f"Engine 99 on scene, test row {i}") for i in range(rows)])
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return path


def corrupt(path: Path, page: int = 5) -> Path:
    """A b-tree page's cell pointers overwritten: what quick_check reports as "*** in database main ***"."""
    b = bytearray(path.read_bytes())
    off = (page - 1) * 4096
    b[off + 8:off + 72] = b"\xa5" * 64
    path.write_bytes(bytes(b))
    return path


def store_for(s3: FakeS3, who: str) -> WatchStore:
    return WatchStore(S3Object("bucket", KEY, "us-east-1", endpoint="https://s3.test", send=s3.sender(who), creds=lambda: CREDS))


def watch_for(s3: FakeS3, who: str, tmp: Path, ntfy, clock=None, logs=None, **kw) -> ReplicaWatch:
    store = store_for(s3, who)
    lg = logs.append if logs is not None else (lambda m: None)
    pusher = Pusher(store, ntfy.url, TOPIC, wall=clock or time.time, log=lg)
    kw.setdefault("check", lambda p, full, cancel: integrity(p, full))
    kw.setdefault("standby_first_s", 0.0)
    return ReplicaWatch(store, pusher, who, tmp / who / "run", tmp / who / "work", wall=clock or time.time, log=lg,
                        poll_s=0.02, **kw)


class Proc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def standby(tmp: Path, files: dict[str, Path]) -> SimpleNamespace:
    """What the watcher reads of a standby Supervisor: its followed files, their -txid sidecars, its role."""
    d = tmp / "standby"
    d.mkdir(parents=True, exist_ok=True)
    for i, (db, src) in enumerate(files.items()):
        shutil.copy(src, d / f"{db}.sqlite")
        (d / f"{db}.sqlite-txid").write_text(f"{100 + i:016x}\n")

    def applied(out):
        try:
            return int(Path(f"{out}-txid").read_text(), 16)
        except OSError:
            return None
    return SimpleNamespace(role="standby", followers={db: Proc() for db in files}, follow_done=set(files), no_replica=set(),
                           staged=lambda db: d / f"{db}.sqlite", env=SimpleNamespace(applied=applied, db_path=lambda db: d / db))


# -- the check itself --------------------------------------------------------------------------------------------------

def test_integrity_tells_ok_corrupt_and_unreadable_apart(tmp_path):
    good = make_db(tmp_path / "good.sqlite")
    assert integrity(good) == {"ok": True, "result": "ok"} and integrity(good, full=True)["ok"] is True
    bad = corrupt(make_db(tmp_path / "bad.sqlite"))
    r = integrity(bad)
    assert r["ok"] is False and "*** in database main ***" in r["result"]
    hdr = tmp_path / "hdr.sqlite"
    hdr.write_bytes(b"\0" * 100 + good.read_bytes()[100:])
    assert integrity(hdr)["ok"] is False                       # "file is not a database": corrupt too
    before = good.read_bytes()
    assert integrity(tmp_path / "missing.sqlite")["ok"] is None    # can't open: not corruption
    integrity(good, full=True)
    assert good.read_bytes() == before                                     # read-only: untouched


def test_the_check_runs_in_a_child_process_and_says_the_same(tmp_path):
    bad = corrupt(make_db(tmp_path / "bad.sqlite"))
    r = rw.run_check(bad, full=False, timeout=60)
    assert r["ok"] is False and "in database main" in r["result"]
    assert rw.run_check(make_db(tmp_path / "good.sqlite"), full=True, timeout=60) == {"ok": True, "result": "ok"}
    t0 = time.monotonic()
    r = rw.run_check(bad, full=True, cancel=lambda: True, timeout=60)      # the role changed: killed, nothing said
    assert r.get("cancelled") and r["ok"] is None and time.monotonic() - t0 < 10


def test_a_snapshot_is_taken_only_while_the_follower_holds_still(tmp_path):
    src = make_db(tmp_path / "cad.sqlite")
    pos = iter([5, 6, 6, 6])                      # moved during the first copy, still during the second
    got = rw.snapshot(src, tmp_path / "snap" / "cad.sqlite", lambda: next(pos), sleep=lambda s: None)
    assert got == 6 and (tmp_path / "snap" / "cad.sqlite").read_bytes() == src.read_bytes()
    moving = iter(range(100))
    with pytest.raises(RuntimeError, match="kept changing"):
        rw.snapshot(src, tmp_path / "snap" / "cad.sqlite", lambda: next(moving), tries=3, sleep=lambda s: None)
    assert not (tmp_path / "snap" / "cad.sqlite").exists()
    with pytest.raises(RuntimeError, match="taking over"):                 # a takeover starting: the copy stops
        rw.snapshot(src, tmp_path / "snap" / "cad.sqlite", lambda: 1, abort=lambda: True)


# -- the standby -------------------------------------------------------------------------------------------------------

def test_the_standby_catches_a_corrupt_replica_and_pushes_once_then_ok_again(tmp_path, ntfy):
    s3, clock, logs = FakeS3(), Clock(), []
    good, bad = make_db(tmp_path / "hub.sqlite"), corrupt(make_db(tmp_path / "cad.sqlite"))
    sup = standby(tmp_path / "B", {"cad": bad, "hub": good, "audio": make_db(tmp_path / "audio.sqlite", 50)})
    w = watch_for(s3, "B", tmp_path, ntfy, clock, logs)
    out = w.standby_round(sup)
    assert out["cad"]["ok"] is False and out["hub"]["ok"] is True and out["audio"]["ok"] is True
    assert out["cad"]["by"] == "standby" and out["cad"]["txid"] == f"{100:016x}"
    assert [m["title"] for m in ntfy.got] == ["Replica corrupt: cad.sqlite"]
    assert ntfy.got[0]["priority"] == 5 and "in database main" in ntfy.got[0]["message"]
    assert any("replica check FAILED: cad.sqlite" in m for m in logs)       # the CloudWatch metric filter's line
    doc = s3.doc(KEY)
    assert doc["checks"]["cad"]["ok"] is False and "cad" in doc["failing"] and doc["checks"]["hub"]["ok"] is True
    state = json.loads((tmp_path / "B" / "run" / rw.STATE_FILE).read_text())    # what the app's Health reads
    assert state["checks"]["cad"]["ok"] is False and "cad" in state["failing"]
    assert not list((tmp_path / "B" / "work").glob("*.sqlite"))              # the copies are deleted

    clock.t += 600
    w.standby_round(sup)                                                    # still bad 10 min later: not pushed again
    assert len(ntfy.got) == 1 and any("not pushed (one replica_corrupt push per 30 min)" in m for m in logs)
    shutil.copy(good, sup.staged("cad"))                                    # fixed
    clock.t += 600
    w.standby_round(sup)
    assert [m["title"] for m in ntfy.got] == ["Replica corrupt: cad.sqlite", "Replica OK again: cad.sqlite"]
    assert s3.doc(KEY)["failing"] == {}


def test_the_standby_never_checks_a_file_its_follower_hasnt_restored_or_lost(tmp_path, ntfy):
    s3 = FakeS3()
    sup = standby(tmp_path / "B", {"cad": corrupt(make_db(tmp_path / "cad.sqlite"))})
    sup.follow_done = set()
    w = watch_for(s3, "B", tmp_path, ntfy)
    assert w.standby_round(sup) == {} and ntfy.got == []
    sup.follow_done, sup.followers["cad"] = {"cad"}, Proc(code=1)          # the follower exited: restarting
    assert w.standby_round(sup) == {} and ntfy.got == []


def test_a_standby_supervisor_runs_the_checks_and_pushes_before_any_takeover(world, tmp_path, ntfy):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path)
    b.sup.watch = watch_for(world.s3, "B", tmp_path, ntfy, leader_interval_s=0,
                            check=lambda p, full, cancel: {"ok": False, "result": "*** in database main ***"} if p.name == "cad.sqlite"
                            else {"ok": True, "result": "ok"})
    b.start()
    until(lambda: len(ntfy.got) >= 1, what="the standby's push")
    assert b.sup.role == "standby" and ntfy.got[0]["title"] == "Replica corrupt: cad.sqlite"
    assert world.s3.doc()["holder"] == "A"                                   # the leader untouched
    finish(b)
    assert finish(a) == 0 and world.violations == []


# -- the leader --------------------------------------------------------------------------------------------------------

class FakeFollower:
    """`litestream restore -f` as the leader's watcher sees it: restores src into out (its -txid sidecar), runs until
    stopped; code 0 with no file: no backup (-if-replica-exists); another code: a failed restore, its stderr logged."""

    started: list = []

    def __init__(self, src: Path | None, out: Path, code=None, err: str = ""):
        self.pid, self.code = None, code
        out.parent.mkdir(parents=True, exist_ok=True)
        if src is not None and code is None:
            shutil.copy(src, out)
            Path(f"{out}-txid").write_text(f"{0x2a:016x}\n")
        if err:
            Path(f"{out}.log").write_text(err)
        FakeFollower.started.append(out.name)

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0 if self.code is None else self.code

    def wait(self, timeout=None):
        return self.code


def leader(tmp_path, s3, ntfy, src: dict, clock=None, logs=None, **kw):
    """A watcher on an active Supervisor with FakeFollowers of src ({db: path | None (no backup) | ("fail", text)})."""
    def follower(db, db_path, out):
        x = src.get(db)
        if isinstance(x, tuple):
            return FakeFollower(None, out, code=1, err=x[1])
        return FakeFollower(x, out, code=0 if x is None else None)
    w = watch_for(s3, "A", tmp_path, ntfy, clock, logs, follower=follower, **kw)
    sup = SimpleNamespace(role="active", env=SimpleNamespace(db_path=lambda db: tmp_path / "data" / f"{db}.sqlite"))
    w.start_followers(sup)
    w.serving_at = w.mono()
    return w, sup


def test_the_leader_checks_its_own_followers_files_pausing_each(tmp_path, ntfy):
    s3, clock = FakeS3(), Clock()
    FakeFollower.started.clear()
    w, sup = leader(tmp_path, s3, ntfy, {"cad": corrupt(make_db(tmp_path / "cad.sqlite")), "hub": make_db(tmp_path / "hub.sqlite"),
                                         "audio": make_db(tmp_path / "audio.sqlite", 50)}, clock)
    w.tend_followers(sup)
    assert w.leader_round(sup) is False
    doc = s3.doc(KEY)
    assert doc["checks"]["cad"]["ok"] is False and doc["checks"]["cad"]["by"] == "leader"
    assert doc["checks"]["cad"]["txid"] == f"{0x2a:016x}" and doc["checks"]["hub"]["ok"] is True
    assert [m["title"] for m in ntfy.got] == ["Replica corrupt: cad.sqlite"] and ntfy.got[0]["priority"] == 5
    # each follower was stopped for its check and started again (it resumes from its -txid)
    assert FakeFollower.started.count("cad.sqlite") == 2 and set(w.followers) == set(rw.DBS)
    assert not Path(f"{w._out('hub')}-shm").exists()                      # nothing of our read left beside it


def test_the_leaders_first_round_waits_for_its_followers_then_says_not_verifiable(tmp_path, ntfy):
    s3, clock = FakeS3(), Clock()
    w, sup = leader(tmp_path, s3, ntfy, {db: make_db(tmp_path / f"{db}.sqlite", 50) for db in rw.DBS}, clock)
    Path(f"{w._out('audio')}-txid").unlink()                               # audio still restoring
    assert w.leader_round(sup) is True and s3.doc(KEY)["checks"].keys() == {"cad", "hub"}
    w.serving_at = w.mono() - rw.RESTORE_S - 1                             # ten minutes on, still not
    w.leader_round(sup)
    assert s3.doc(KEY)["checks"]["audio"]["ok"] is None and "not restored" in s3.doc(KEY)["checks"]["audio"]["result"]


def test_a_missing_backup_is_not_verifiable_and_pushed_after_two_rounds(tmp_path, ntfy):
    s3, clock = FakeS3(), Clock()
    w, sup = leader(tmp_path, s3, ntfy, {"cad": None, "hub": make_db(tmp_path / "hub.sqlite", 50),
                                         "audio": make_db(tmp_path / "audio.sqlite", 50)}, clock)
    w.tend_followers(sup)
    w.leader_round(sup)
    assert s3.doc(KEY)["checks"]["cad"]["result"] == "no backup in S3" and ntfy.got == []
    clock.t += 900
    w.leader_round(sup)
    assert [(m["title"], m["priority"]) for m in ntfy.got] == [("Replica not verifiable: cad.sqlite", 4)]
    assert "cad" in s3.doc(KEY)["failing"]


def test_a_failed_restore_is_pushed_at_once_as_possible_corruption(tmp_path, ntfy):
    s3, clock = FakeS3(), Clock()
    w, sup = leader(tmp_path, s3, ntfy, {"cad": ("fail", "level=ERROR msg=\"restore\" error=\"ltx checksum mismatch\"\n"),
                                         "hub": make_db(tmp_path / "hub.sqlite", 50), "audio": make_db(tmp_path / "audio.sqlite", 50)},
                     clock)
    w.tend_followers(sup)
    assert [(m["title"], m["priority"]) for m in ntfy.got] == [("Replica restore failed (possible corruption): cad.sqlite", 5)]
    assert "checksum mismatch" in ntfy.got[0]["message"] and "cad" in s3.doc(KEY)["failing"]
    assert "cad" not in w.followers and w.fstate["cad"]["restart_at"] is not None     # started again after a backoff


def test_each_push_kind_has_its_own_slot(tmp_path, ntfy):
    s3, clock = FakeS3(), Clock()
    w, sup = leader(tmp_path, s3, ntfy, {"cad": None, "hub": make_db(tmp_path / "hub.sqlite", 50),
                                         "audio": make_db(tmp_path / "audio.sqlite", 50)}, clock)
    w.tend_followers(sup)
    w.leader_round(sup)
    clock.t += 60
    w.leader_round(sup)                                                    # cad not verifiable: pushed (4)
    shutil.copy(corrupt(make_db(tmp_path / "hub2.sqlite")), w._out("hub"))
    clock.t += 60
    w.leader_round(sup)                                                    # then hub corrupt: never held back by it
    assert [m["title"] for m in ntfy.got] == ["Replica not verifiable: cad.sqlite", "Replica corrupt: hub.sqlite"]


# -- a task that fails ------------------------------------------------------------------------------------------------

def test_a_failed_takeover_is_recorded_and_pushed_before_the_task_exits(world, tmp_path, ntfy):
    a = Task(world, "A", tmp_path).start()
    until(lambda: a.sup.role == "active")
    b = Task(world, "B", tmp_path)
    b.sup.watch = watch_for(world.s3, "B", tmp_path, ntfy, interval_s=0, leader_interval_s=0)
    b.start()
    until(lambda: b.sup.role == "standby" and b.sup.warm())
    b.env.quick_check = lambda path: "*** in database main ***"             # tonight's: the replica's copy is bad
    a.sup.on_term()
    until(lambda: b.code is not None, what="B gives up")
    assert b.code == 1
    # synchronous: by the time the task's run() returned, the push had reached ntfy and the event S3
    assert [m["title"] for m in ntfy.got] == ["Hub takeover failed"]
    assert "cad.sqlite quick_check" in ntfy.got[0]["message"] and "exit 1" in ntfy.got[0]["message"]
    ev = world.s3.doc(KEY)["events"][-1]
    assert ev["kind"] == "takeover_failed" and ev["task"] == "B" and ev["code"] == 1
    assert a.wait(10) == 0 and world.violations == []


def test_an_unclean_release_is_pushed(world, tmp_path, ntfy):
    a = Task(world, "A", tmp_path)
    a.sup.watch = watch_for(world.s3, "A", tmp_path, ntfy, interval_s=0, leader_interval_s=0)
    a.start()
    until(lambda: a.sup.role == "active")

    def sync(db, timeout):
        raise RuntimeError("litestream sync cad: timed out")
    a.env.sync = sync
    assert finish(a) == 0
    assert [m["title"] for m in ntfy.got] == ["Hub released unclean"]
    assert world.s3.doc(KEY)["events"][-1]["kind"] == "unclean_release"


def test_a_crash_loop_pushes_once_then_says_it_loops_then_serving_again(tmp_path, ntfy):
    s3, clock = FakeS3(), Clock()
    why = "RuntimeError: cad.sqlite quick_check: *** in database main ***"
    for i in range(5):                                       # five fresh tasks, a minute apart, each failing its takeover
        watch_for(s3, f"T{i}", tmp_path, ntfy, clock).task_failed("takeover_failed", why, 1)
        clock.t += 60
    assert [m["title"] for m in ntfy.got] == ["Hub takeover failed", "Hub crash-looping: 3 failed tasks in 10 min"]
    assert ntfy.got[1]["priority"] == 5 and len(s3.doc(KEY)["events"]) == 5
    watch_for(s3, "T5", tmp_path, ntfy, clock).serving()      # one serves at last
    assert ntfy.got[-1]["title"] == "Hub serving again" and "after 5 failed task(s)" in ntfy.got[-1]["message"]
    watch_for(s3, "T6", tmp_path, ntfy, clock).serving()      # the next deploy: nothing to say
    assert len(ntfy.got) == 3
    clock.t += 31 * 60                                         # past the limit: a new failure is pushed again
    watch_for(s3, "T7", tmp_path, ntfy, clock).task_failed("app_exited", "the app exited 1 while serving", 1)
    assert ntfy.got[-1]["title"] == "Hub app exited"


def test_the_rate_limit_is_per_kind_shared_by_every_task_and_kept_in_s3(tmp_path, ntfy):
    s3, clock, logs = FakeS3(), Clock(), []
    p1 = Pusher(store_for(s3, "A"), ntfy.url, TOPIC, wall=clock, log=logs.append, every_s=1800)
    p2 = Pusher(store_for(s3, "B"), ntfy.url, TOPIC, wall=clock, log=logs.append, every_s=1800)
    assert p1.push("task", "one", "x") is True
    assert p2.push("task", "two", "x") is False                # another task, the same kind, within 30 min
    assert p1.push("replica", "three", "x") is True            # another kind: its own limit
    clock.t += 1799
    assert p1.push("task", "four", "x") is False
    clock.t += 2
    assert p2.push("task", "five", "x") is True
    assert p2.push("replica_ok", "six", "x", limit=False) is True and p2.push("replica_ok", "seven", "x", limit=False)
    assert [m["title"] for m in ntfy.got] == ["one", "three", "five", "six", "seven"]
    assert s3.doc(KEY)["pushed"]["task"] == clock.t


def test_a_push_is_recorded_only_once_ntfy_took_it_with_one_retry(tmp_path):
    s3, clock, calls = FakeS3(), Clock(), []

    def down(*a, **kw):
        calls.append(a[2])
        raise rw.notify.NotifyError("can't reach ntfy")
    p = Pusher(store_for(s3, "A"), "http://127.0.0.1:1", TOPIC, wall=clock, log=lambda m: None, publish=down, sleep=lambda s: None)
    assert p.push("replica_corrupt", "Replica corrupt: cad.sqlite", "x") is False
    assert calls == ["Replica corrupt: cad.sqlite"] * 2 and (s3.doc(KEY) or {}).get("pushed") is None   # slot still free
    p.publish = lambda *a, **kw: calls.append("ok")
    assert p.push("replica_corrupt", "Replica corrupt: cad.sqlite", "x") is True
    assert s3.doc(KEY)["pushed"]["replica_corrupt"] == clock.t


def test_the_failure_report_never_outlasts_its_deadline(tmp_path, ntfy):
    s3 = FakeS3()
    slow = s3.sender("A")

    def hang(*a, **kw):
        time.sleep(3)
        return slow(*a, **kw)
    store = WatchStore(S3Object("bucket", KEY, "us-east-1", endpoint="https://s3.test", send=hang, creds=lambda: CREDS))
    w = ReplicaWatch(store, Pusher(store, ntfy.url, TOPIC, log=lambda m: None), "A", tmp_path / "run", tmp_path / "w",
                     log=lambda m: None)
    t0 = time.monotonic()
    assert w.report("takeover_failed", "RuntimeError: x", 1, deadline_s=0.5) is False
    assert time.monotonic() - t0 < 1.5


def test_s3_unreachable_still_pushes_and_no_topic_never_does(tmp_path, ntfy):
    s3, logs = FakeS3(), []
    s3.down.add("A")
    assert Pusher(store_for(s3, "A"), ntfy.url, TOPIC, log=logs.append).push("task", "Hub takeover failed", "x") is True
    assert any("pushing anyway" in m for m in logs) and len(ntfy.got) == 1
    assert Pusher(store_for(s3, "B"), ntfy.url, "", log=logs.append).push("task", "t", "x") is False
    for refused in ("pcfd-phone-prealert", "pcfd-status"):
        p = Pusher(store_for(s3, "C"), ntfy.url, refused, log=logs.append)
        assert not p.on and p.push("replica", "t", "x") is False
    assert len(ntfy.got) == 1 and not any(TOPIC in m for m in logs)       # never a topic's name in the log


def test_the_status_topic_comes_from_the_env_over_the_config_and_tapouts_are_refused(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[alerts]\nstatus_topic = "sizeup-test-from-config"\n[prealert]\nring_topic = "sizeup-test-ring-topic"\n')
    assert rw.status_topic(str(cfg), {})[1] == "sizeup-test-from-config"
    assert rw.status_topic(str(cfg), {"SIZEUP_STATUS_TOPIC": TOPIC})[1] == TOPIC
    assert rw.status_topic("", {"SIZEUP_ALERT_TOPIC": TOPIC})[1] == TOPIC        # only one set: it gets both kinds
    assert rw.status_topic(str(cfg), {"SIZEUP_STATUS_TOPIC": "sizeup-test-ring-topic"})[1] == ""      # [prealert]'s: refused
    assert rw.status_topic("", {"SIZEUP_STATUS_TOPIC": "pcfd-status"})[1] == ""
    w = rw.from_env("A", tmp_path / "run", tmp_path, "ls.yml", str(cfg),
                    env={"SIZEUP_REPLICA_CHECK_MIN": "5", "SIZEUP_REPLICA_LEADER_CHECK_MIN": "0", "SIZEUP_REPLICA_PUSH_MIN": "60"},
                    log=lambda m: None)
    assert (w.interval_s, w.leader_interval_s, w.pusher.every_s, w.store.obj) == (300, 0, 3600, None)


# -- Settings > Health ---------------------------------------------------------------------------------------------------

def test_health_shows_the_replicas_row_and_a_quiet_alert_even_with_the_station_silent():
    from tests.test_health import Rig
    r = Rig()
    now = r.hub.now
    state = {"checks": {"cad": {"at": now - 60, "ok": False, "result": "*** in database main ***", "mode": "quick_check",
                                "by": "standby", "txid": "0000000000000064", "bad_since": now - 60},
                        "hub": {"at": now - 60, "ok": True, "result": "ok", "mode": "integrity_check", "by": "leader"}},
             "failing": {"cad": now - 60}, "leader_interval_s": 900,
             "events": [{"at": now - 30, "kind": "takeover_failed", "why": "RuntimeError: cad.sqlite quick_check", "code": 1}]}
    r.h.replicas = lambda: state
    r.at(3600, post=False)                                          # the station silent: the hub's own still judged
    a = r.h.active["replica:cad"]
    assert a["severity"] == "critical" and a["title"] == "Replica corrupt: cad.sqlite" and a.get("quiet")
    assert not [m for m in r.n.msgs if "Replica" in m["title"]]     # the supervisor pushed it: never twice
    rows = {x["db"]: x for x in r.h.panel()["replicas"]["rows"]}
    assert rows["cad"]["state"] == "corrupt" and rows["hub"]["state"] == "stale" and rows["audio"]["state"] == "none"
    assert r.h.panel()["replicas"]["events"][0]["kind"] == "takeover_failed"
    state["checks"]["cad"] = {"at": r.hub.now, "ok": True, "result": "ok", "mode": "quick_check", "by": "leader"}
    state["failing"] = {}
    r.at(3700, post=False)
    assert "replica:cad" not in r.h.active
    assert Rig().h.panel()["replicas"] is None                       # not on ECS: no row


# -- the deploy's check ---------------------------------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location("deploy_check", ROOT / "tools" / "deploy_check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


BUILD = "c" * 40


def record(**kw):
    """replica-watch.json as the new leader writes it (made-up times and task ids)."""
    d = {"leader": {"task": "t-new", "build": BUILD, "serving_at": 1000.0}, "failing": {},
         "checks": {db: {"at": 1130.0, "ok": True, "result": "ok", "mode": "quick_check", "by": "leader"} for db in rw.DBS}}
    d.update(kw)
    return d


def test_the_deploy_check_reads_only_the_record_and_waits_for_the_new_leaders_checks():
    v = dc.replica_verdict
    assert v(None, BUILD)[0] == dc.WAIT
    assert v(record(leader={"build": "d" * 40, "serving_at": 1}), BUILD)[0] == dc.WAIT     # the old leader still
    old = record()
    old["checks"]["audio"]["at"] = 900.0                                   # verified before this leader served
    assert v(old, BUILD) == (dc.WAIT, "the leader (serving since 00:16:40 UTC) hasn't verified audio yet")
    assert v(record(), BUILD)[0] == dc.OK
    bad = record()
    bad["checks"]["cad"].update(ok=False, result="*** in database main ***\nTree 7 page 4183 cell 12: bad")
    assert v(bad, BUILD) == (dc.FAIL, "cad.sqlite: corrupt (quick_check by the leader at 00:18:50 UTC): "
                                     "*** in database main *** · Tree 7 page 4183 cell 12: bad")
    unv = record(failing={"hub": 1100.0})
    unv["checks"]["hub"].update(ok=None, result="no backup in S3")
    assert v(unv, BUILD)[0] == dc.FAIL and "hub.sqlite: not verifiable" in v(unv, BUILD)[1]   # never "corrupt"


def test_the_deploy_check_polls_until_verified_or_times_out(monkeypatch, capsys):
    c = Clock(0.0)
    seq = [None, record(leader={"build": "d" * 40, "serving_at": 1}), record()]
    got = iter(seq + [seq[-1]] * 10)
    ok, msg = dc.wait_replicas(lambda: next(got), BUILD, timeout=600, every=20, clock=c, sleep=lambda s: setattr(c, "t", c.t + s),
                               out=lambda m: None)
    assert ok and "cad.sqlite: ok" in msg
    ok, msg = dc.wait_replicas(lambda: None, BUILD, timeout=60, every=20, clock=c, sleep=lambda s: setattr(c, "t", c.t + s),
                               out=lambda m: None)
    assert not ok and msg.startswith("not verified within 1 min")
    bad = record(failing={"cad": 1100.0})
    bad["checks"]["cad"].update(ok=False, result="*** in database main ***")
    monkeypatch.setattr(dc, "fetch_watch", lambda bucket, key: bad)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert dc.main(["replicas", "--build", BUILD, "--taskdef", str(ROOT / "deploy/aws/taskdef.json"), "--every", "0"]) == 1
    assert "::error title=Replica check failed::cad.sqlite: corrupt (quick_check by the leader" in capsys.readouterr().out
    keys = []
    monkeypatch.setattr(dc, "fetch_watch", lambda bucket, key: keys.append((bucket, key)) or record())
    assert dc.main(["replicas", "--build", BUILD, "--taskdef", str(ROOT / "deploy/aws/taskdef.json")]) == 0
    assert keys[0][0].startswith("sizeup-hub-backups-") and keys[0][1] == "sizeup-hub/replica-watch.json"   # one key only


def test_the_workflow_reads_the_record_and_never_restores_a_database():
    wf = (ROOT / ".github/workflows/deploy.yml").read_text()
    step = wf[wf.index("- name: The new leader verified the replicas"):wf.index("- name: Wait for the station")]
    assert 'python3 tools/deploy_check.py replicas --build "$SHA"' in step and "steps.deploy.outcome == 'success'" in step
    assert "litestream" not in wf.lower() and "integrity_check" not in step     # no database ever reaches a runner


@pytest.fixture
def world():
    return World()
