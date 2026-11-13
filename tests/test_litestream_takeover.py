"""A takeover with the real Litestream (docs/cloud.md "Replicate before migrate", the 2026-11 outage).

The supervisor (sizeup/handoff.py) as on the hub, with a local file replica instead of S3, the lock in a fake S3
(tests/test_handoff.py) and a small gated app in place of sizeup's. The old leader writes, drains and releases;
the new task follows the replica, promotes it, starts `litestream replicate` and only once Litestream has confirmed
each file does its app migrate (8 columns, an index, 3 full-table UPDATEs, with SQLite's autocheckpoints, the shape
of #419) and serve. What the replica holds afterwards must be exactly the live files, and the next takeover's quick
check must pass.

Needs `litestream` on PATH (the hub image has it; `brew install litestream` locally): skipped without it.
Every row here is invented."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from sizeup import handoff
from sizeup.handoff import DBS, Env, Supervisor, fingerprint, sidecars
from tests.test_handoff import FakeS3, lock_for, until

pytestmark = pytest.mark.skipif(shutil.which("litestream") is None, reason="needs litestream on PATH")

ROWS = 20000

# The gated app (sizeup/app.py run_gated's protocol): "warm", then on "init" the migration, "ok"; on "run" it writes
# and serves (a marker file), until SIGTERM.
APP = r'''
import os, signal, sqlite3, sys, time
from pathlib import Path
gate, data, mark, migrate = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] == "1"
answer = Path(f"{gate}.answer")
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
def say(t):
    answer.with_suffix(".tmp").write_text(t + "\n"); os.replace(answer.with_suffix(".tmp"), answer)
def said():
    try: return gate.read_text().strip()
    except OSError: return ""
def conn(name):
    c = sqlite3.connect(data / f"{name}.sqlite", isolation_level=None)
    c.execute("PRAGMA journal_mode = WAL"); c.execute("PRAGMA busy_timeout = 5000"); c.execute("PRAGMA wal_autocheckpoint = 200")
    return c
say("warm")
while said() not in ("init", "run") and not stop: time.sleep(0.02)
if said() == "init":
    if migrate:
        c = conn("cad")
        for k in range(8): c.execute(f"ALTER TABLE radio_traffic ADD COLUMN made_up_{k} TEXT NOT NULL DEFAULT ''")
        c.execute("CREATE INDEX radio_traffic_made_up ON radio_traffic(made_up_0, start)")
        for k in range(3): c.execute(f"UPDATE radio_traffic SET made_up_{k} = channel || '-' || id")
        c.close()
    say("ok")
    while said() != "run" and not stop: time.sleep(0.02)
c = conn("cad")
mark.write_text("serving")
i = 0
while not stop:
    c.execute("INSERT INTO radio_traffic (id, start, channel, text) VALUES (?, ?, 'FG1', 'made-up words after the takeover')",
              (f"new-{os.getpid()}-{i}", time.time())); i += 1
    time.sleep(0.01)
c.close()
mark.unlink()
'''


class RealEnv(Env):
    """The real Litestream calls; the app is APP; no connector, no caches."""

    def __init__(self, root: Path, ls_config: Path, socket: str, migrate: bool):
        super().__init__("cfg.toml", root / "data", str(ls_config), socket, port=0)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.root, self.migrate = root, migrate
        self.mark = root / "serving"

    def start_app(self, env):
        script = self.root / "app.py"
        script.write_text(APP)
        return handoff.Proc("app", [sys.executable, str(script), env["SIZEUP_APP_GATE"], str(self.state_dir),
                                    str(self.mark), "1" if self.migrate else "0"])

    def app_answers(self):
        return self.mark.exists()

    def app_healthy(self):
        return self.mark.exists()

    def app_status(self):
        return {"serving": True} if self.mark.exists() else None

    def connector_ready(self):
        return True

    def warm_caches(self, deadline_s):
        pass


@pytest.fixture
def rig(tmp_path):
    sock_dir = Path(tempfile.mkdtemp(prefix="ls", dir="/tmp"))      # (a unix socket path must be short)
    replica = tmp_path / "replica"

    def config(task: str) -> tuple[Path, str]:
        data = tmp_path / task / "data"
        sock = str(sock_dir / f"{task}.sock")
        cfg = tmp_path / task / "litestream.yml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(f"socket:\n  enabled: true\n  path: {sock}\nlogging:\n  stderr: true\ndbs:\n" + "".join(
            f"  - path: {data}/{db}.sqlite\n    replica:\n      type: file\n      path: {replica}/{db}.sqlite\n"
            for db in DBS))
        return cfg, sock
    yield tmp_path, config
    shutil.rmtree(sock_dir, ignore_errors=True)


def first_leader(root: Path, cfg: Path, sock: str) -> None:
    """A hub from before: its files, replicated, drained (sync -wait), Litestream stopped."""
    env = Env("cfg.toml", root / "data", str(cfg), sock, 0)
    env.state_dir.mkdir(parents=True, exist_ok=True)
    for db in DBS:
        c = sqlite3.connect(env.db_path(db), isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("CREATE TABLE radio_traffic (id TEXT PRIMARY KEY, start REAL NOT NULL, channel TEXT NOT NULL, "
                  "talkgroup INTEGER, text TEXT NOT NULL DEFAULT '')")
        c.close()
    ls = env.replicate()
    try:
        c = sqlite3.connect(env.db_path("cad"), isolation_level=None)
        now = time.time()
        for i in range(0, ROWS, 1000):
            c.execute("BEGIN")
            c.executemany("INSERT INTO radio_traffic VALUES (?, ?, ?, ?, ?)",
                          [(f"fg-{j}", now - j, ("FG1", "South 2", "Fire/EMS")[j % 3], 9000 + j % 40 if j % 2 else None,
                            f"made-up transmission {j}: engine on location, water supply established")
                           for j in range(i, i + 1000)])
            c.execute("COMMIT")
            if i == ROWS // 2:
                time.sleep(1.2)                                     # (more than one sync: a history of LTX files)
        c.close()
        for db in DBS:
            end = time.monotonic() + 30
            while True:
                try:
                    env.sync(db, 30)
                    break
                except RuntimeError:
                    if time.monotonic() > end:
                        raise
                    time.sleep(0.2)                                 # (its control socket, a moment after it starts)
    finally:
        ls.stop(30)


def snapshot(path: Path) -> tuple:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(radio_traffic)")]
        rows = c.execute(f"SELECT {', '.join(cols)} FROM radio_traffic ORDER BY id").fetchall()
        return cols, hash(tuple(rows)), len(rows)
    finally:
        c.close()


def restored(root: Path, cfg: Path, db: str) -> Path:
    out = root / "check" / f"{db}.sqlite"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["litestream", "restore", "-config", str(cfg), "-o", str(out), str(Path(str(cfg)).parent /
                        "data" / f"{db}.sqlite")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return out


def test_a_takeover_with_a_big_migration_leaves_the_replica_exactly_the_live_files(rig):
    tmp, config = rig
    cfg_a, sock_a = config("A")
    first_leader(tmp / "A", cfg_a, sock_a)
    s3 = FakeS3()
    a = lock_for(s3, "A")                                           # A held it, and releases it with its positions
    a.try_take()

    cfg_b, sock_b = config("B")
    env = RealEnv(tmp / "B", cfg_b, sock_b, migrate=True)
    sup = Supervisor(lock_for(s3, "B"), env, tmp / "B" / "run", tmp / "B" / "shared", control_port=0,
                     timing=dict(CREATED_WAIT_S=0.2, POLL_S=0.1, OVERLAP_S=0.1, RETAKE_S=1e9))
    before = {db: env.plan(db) for db in DBS}
    a.release({db: f"{n:016x}" for db, n in before.items()}, clean=True)
    code = []
    th = threading.Thread(target=lambda: code.append(sup.run()), daemon=True)
    th.start()
    try:
        until(lambda: sup.role == "active" or code, timeout=180, what="B active")
        assert sup.role == "active", sup.events[-5:]
        ev = sup.events
        replicating = next(i for i, e in enumerate(ev) if e.startswith("Litestream replicates"))
        assert replicating < next(i for i, e in enumerate(ev) if e.startswith("migrated and in S3"))
        # Litestream started on the promoted files with no state of its own: "behind replica", db_txid 0, the
        # replica exactly where it was promoted from (the expected one; anything else would have been fatal)
        until(lambda: len(env.behind_events) == len(DBS), timeout=10, what="the behind-replica lines")
        assert {e["db"]: (e["db_txid"], e["replica_txid"]) for e in env.behind_events} == \
            {db: (0, before[db]) for db in DBS}
        time.sleep(2.0)                                             # the app writes on
        sup.on_term()
        th.join(120)
    finally:
        sup.on_term()
        th.join(30)
    assert code == [0], sup.events[-5:]
    assert not any("REFUSED" in e for e in sup.events)
    live = env.db_path("cad")
    for db in DBS:
        out = restored(tmp, cfg_b, db)
        assert sqlite3.connect(out).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot(out) == snapshot(env.db_path(db)), db
    cols, _, n = snapshot(live)
    assert "made_up_7" in cols and n > ROWS                          # migrated, and written after


def test_the_supervisor_checks_the_staged_copy_like_the_real_one(rig):
    """The checks themselves, on what a real follower leaves: no -wal / -shm beside it, quick_check through a
    normal read-only open passes and leaves nothing behind, the fingerprint holds until something writes."""
    tmp, config = rig
    cfg_a, sock_a = config("A")
    first_leader(tmp / "A", cfg_a, sock_a)
    env = RealEnv(tmp / "A", cfg_a, sock_a, migrate=False)
    out = tmp / "standby" / "cad.sqlite"
    p = env.follower("cad", out)
    try:
        until(lambda: env.applied(out) == env.plan("cad"), timeout=60, what="the follower")
    finally:
        assert p.stop(15) == 0                                      # SIGTERM: a clean stop
    assert sidecars(out) == []
    fp = fingerprint(out)
    assert env.quick_check(out) == "ok" and sidecars(out) == [] and fingerprint(out) == fp
    c = sqlite3.connect(out, isolation_level=None)
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("INSERT INTO radio_traffic VALUES ('x', 0, 'FG1', NULL, 'made-up')")
    assert sidecars(out)                                            # a writer leaves its WAL beside it ...
    c.close()
    assert fingerprint(out) != fp                                   # ... and the file changed
