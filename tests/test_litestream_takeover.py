"""A takeover with the real Litestream (docs/cloud.md "Replicate before migrate", the 2026-11 outage).

The supervisor (sizeup/handoff.py) as on the hub, with a local file replica instead of S3, the lock in a fake S3
(tests/test_handoff.py) and a small gated app in place of sizeup's. The old leader writes, drains and releases;
the new task follows the replica, promotes it, starts `litestream replicate` and only once Litestream has confirmed
each file does its app migrate (8 columns, an index, 3 full-table UPDATEs, with SQLite's autocheckpoints, the shape
of #419) and serve. What the replica holds afterwards must be exactly the live files, and the next takeover's quick
check must pass.

Litestream runs with the hub's own deploy/hub/litestream.yml (its intervals and levels; only the S3 replicas are
local folders): the WAL read every 30 s when nobody asks (SIZEUP_LITESTREAM_SYNC_S). So whatever is replicated
here within seconds is replicated because the supervisor asked (`litestream sync`), never by a tick: a drain and
a takeover must neither lose a row nor wait for one.

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

from sizeup import handoff, leaderlock
from sizeup.handoff import DBS, Env, Supervisor, fingerprint, sidecars
from tests.test_handoff import FakeS3, lock_for, until

pytestmark = pytest.mark.skipif(shutil.which("litestream") is None, reason="needs litestream on PATH")

ROWS = 20000
ROOT = Path(__file__).resolve().parent.parent
SYNC_S = handoff.LITESTREAM_SYNC_S                  # the hub's cadence: nothing here may wait for it

# The gated app (sizeup/app.py run_gated's protocol): "warm", then on "init" the migration, "ok"; on "run" it writes
# and serves (a marker file), until SIGTERM.
APP = r'''
import os, signal, sqlite3, sys, time
from pathlib import Path
gate, data, mark, migrate, quiet = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] == "1", sys.argv[4] == "q"
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
    if not quiet:                       # (quiet: the databases open, as an app has them; the test does the writing)
        c.execute("INSERT INTO radio_traffic (id, start, channel, text) VALUES (?, ?, 'FG1', 'made-up words after the takeover')",
                  (f"new-{os.getpid()}-{i}", time.time())); i += 1
    time.sleep(0.01)
c.close()
mark.unlink()
'''


class RealEnv(Env):
    """The real Litestream calls; the app is APP; no connector, no caches."""

    def __init__(self, root: Path, ls_config: Path, socket: str, migrate: bool, quiet: bool = False):
        super().__init__("cfg.toml", root / "data", str(ls_config), socket, port=0)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.root, self.migrate = root, "q" if quiet else migrate
        self.mark = root / "serving"

    def start_app(self, env):
        script = self.root / "app.py"
        script.write_text(APP)
        return handoff.Proc("app", [sys.executable, str(script), env["SIZEUP_APP_GATE"], str(self.state_dir),
                                    str(self.mark), "q" if self.migrate == "q" else "1" if self.migrate else "0"])

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
        """The hub's litestream.yml for this task: its S3 replicas turned into folders, its ${...} filled in."""
        data = tmp_path / task / "data"
        sock = str(sock_dir / f"{task}.sock")
        cfg = tmp_path / task / "litestream.yml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        y = (ROOT / "deploy/hub/litestream.yml").read_text()
        assert y.count("      type: s3\n") == len(DBS) and y.count("${SIZEUP_LITESTREAM_SYNC_S}s") == len(DBS)
        y = "".join(line for line in y.splitlines(keepends=True)
                    if not line.lstrip().startswith(("bucket:", "region:")))
        for a, b in (("      type: s3\n", "      type: file\n"), ("${LITESTREAM_PATH}", str(replica)),
                     ("${SIZEUP_STATE_DIR}", str(data)), ("${LITESTREAM_SOCKET}", sock),
                     ("${SIZEUP_LITESTREAM_SYNC_S}", str(SYNC_S))):
            y = y.replace(a, b)
        assert not any("${" in line.split("#")[0] for line in y.splitlines()), y
        cfg.write_text(y)
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
                asked(env, "cad", wait=False)                       # (more than one sync: a history of LTX files)
        c.close()
        for db in DBS:
            asked(env, db)
    finally:
        ls.stop(30)


def asked(env: Env, db: str, wait: bool = True) -> tuple[int, int | None]:
    """`litestream sync` (-wait) of db, now: (Litestream's position, the replica's)."""
    end = time.monotonic() + 30
    while True:
        try:
            return env.position(db, 30, wait=wait)
        except RuntimeError:
            if time.monotonic() > end:
                raise
            time.sleep(0.2)                                         # (its control socket, a moment after it starts)


def ids(path: Path) -> set[str]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in c.execute("SELECT id FROM radio_traffic")}
    finally:
        c.close()


def snapshot(path: Path) -> tuple:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(radio_traffic)")]
        rows = c.execute(f"SELECT {', '.join(cols)} FROM radio_traffic ORDER BY id").fetchall()
        return cols, hash(tuple(rows)), len(rows)
    finally:
        c.close()


def restored(root: Path, cfg: Path, db: str, to: str = "check") -> Path:
    out = root / to / f"{db}.sqlite"
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
        assert replicating < next(i for i, e in enumerate(ev) if e.startswith("migrated, Litestream at"))
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


def test_a_clean_handover_at_the_30_s_cadence_loses_no_row_and_waits_for_no_tick(rig):
    """Litestream reads the WAL only every 30 s by itself (the hub's litestream.yml), so seconds after a takeover the
    replica is behind the leader's file. SIGTERM: the drain's `litestream sync -wait` puts every committed row in the
    replica at once and releases at that position; the standby's followers reach it within their own second, and
    it serves: in seconds, not after a tick, and with every row the old leader ever committed."""
    assert SYNC_S >= 30
    tmp, config = rig
    cfg_s, sock_s = config("S")
    first_leader(tmp / "S", cfg_s, sock_s)                          # the replica as some leader before left it
    s3 = FakeS3()
    seed = lock_for(s3, "S")
    seed.try_take()
    seed.release({db: f"{n:016x}" for db, n in
                  ((db, Env("cfg.toml", tmp / "S" / "data", str(cfg_s), sock_s, 0).plan(db)) for db in DBS)}, clean=True)

    def task(name: str) -> tuple[RealEnv, Supervisor, list, threading.Thread]:
        cfg, sock = config(name)
        env = RealEnv(tmp / name, cfg, sock, migrate=False)
        sup = Supervisor(lock_for(s3, name), env, tmp / name / "run", tmp / name / "shared", control_port=0,
                         timing=dict(POLL_S=0.1, RETAKE_S=1e9))
        code: list = []
        th = threading.Thread(target=lambda: code.append(sup.run()), daemon=True)
        th.start()
        return env, sup, code, th

    env_a, a, code_a, th_a = task("A")
    b = th_b = None
    try:
        until(lambda: a.role == "active" or code_a, timeout=120, what="A active")
        assert a.role == "active", a.events[-5:]
        env_b, b, code_b, th_b = task("B")
        until(lambda: (b.role == "standby" and b.warm()) or code_b, timeout=120, what="B a warm standby")
        time.sleep(3.0)                                             # A's app writes on; Litestream's tick is far off
        # no tick since the takeover's last confirm: the replica lacks what the app wrote since (a crash now would
        # lose it: the price of the cadence) ...
        before = env_a.plan("cad")
        behind = ids(restored(tmp, env_a.ls_config, "cad", to="before"))
        assert len(ids(env_a.db_path("cad")) - behind) > 50
        t0 = time.monotonic()
        a.on_term()                                                 # ... and a clean handover loses none of it
        until(lambda: a.role in ("released", "done") or code_a, timeout=60, what="A released")
        drain_s = time.monotonic() - t0
        until(lambda: b.role == "active" or code_b, timeout=60, what="B active")
        handover_s = time.monotonic() - t0
        th_a.join(60)
        assert code_a == [0] and b.role == "active", (a.events[-5:], b.events[-5:])
        released = {db: leaderlock.txid(v) for db, v in a.released_positions.items()}
        assert released["cad"] > before                             # the drain's sync moved the replica, at once
        assert {db: leaderlock.txid(v) for db, v in b.positions.items()} == released    # promoted exactly there
        old = ids(env_a.db_path("cad"))                             # every row A committed, its last one included
        assert len(old) > len(behind) and old <= ids(env_b.db_path("cad"))
        assert not any("REFUSED" in e or "full restore" in e for e in b.events)
        print(f"\nreal Litestream, WAL read every {SYNC_S} s: drain {drain_s:.1f} s, SIGTERM to the new leader "
              f"serving {handover_s:.1f} s ({len(old - behind)} rows only the drain's sync replicated)")
        assert drain_s < SYNC_S / 2 and handover_s - drain_s < SYNC_S / 2     # seconds: nothing waited for a tick
    finally:
        for sup, th in ((a, th_a), (b, th_b)):
            if sup is not None:
                sup.t.update(OVERLAP_S=0.1)
                sup.on_term()
                th.join(60)
    assert code_b == [0], b.events[-5:]
    for db in DBS:                                                  # and B's own release: the replica is its files
        assert snapshot(restored(tmp, env_b.ls_config, db, to="after")) == snapshot(env_b.db_path(db)), db


# -- Litestream started again under the serving app (sizeup/handoff.py "TRUNCATE BEFORE LITESTREAM IS STARTED AGAIN") --

def serving_leader(rig, quiet: bool = True):
    """A leader serving from a seeded replica: (env, supervisor, its exit codes, its thread)."""
    tmp, config = rig
    cfg_s, sock_s = config("S")
    first_leader(tmp / "S", cfg_s, sock_s)
    s3 = FakeS3()
    seed = lock_for(s3, "S")
    seed.try_take()
    seed.release({db: f"{n:016x}" for db, n in
                  ((db, Env("cfg.toml", tmp / "S" / "data", str(cfg_s), sock_s, 0).plan(db)) for db in DBS)}, clean=True)
    cfg, sock = config("A")
    env = RealEnv(tmp / "A", cfg, sock, migrate=False, quiet=quiet)
    sup = Supervisor(lock_for(s3, "A"), env, tmp / "A" / "run", tmp / "A" / "shared", control_port=0,
                     timing=dict(POLL_S=0.1, RETAKE_S=1e9, OVERLAP_S=0.1))
    code: list = []
    th = threading.Thread(target=lambda: code.append(sup.run()), daemon=True)
    th.start()
    until(lambda: sup.role == "active" or code, timeout=120, what="the leader active")
    assert sup.role == "active", sup.events[-5:]
    return env, sup, code, th


def counters(path: Path) -> tuple:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return (c.execute("SELECT count(*), coalesce(sum(length(pad)), 0) FROM made_up_t").fetchone(),
                hash(tuple(c.execute("SELECT k, v FROM made_up_u ORDER BY k"))),
                c.execute("SELECT sum(v) FROM made_up_u").fetchone()[0])
    finally:
        c.close()


def test_litestream_killed_under_the_app_and_started_again_misses_nothing_written_meanwhile(rig, monkeypatch):
    """The replica divergence (PR #522's review): Litestream dies under a serving app; the app writes on, its
    checkpoint moves those frames into the main file, and the WAL starts again, shorter than where Litestream had
    read to. Started again as it was, Litestream 0.5.18 finds its last frame still lying in the WAL file and
    carries on with no snapshot: the pages written in the gap stay stale in the replica, integrity_check ok,
    `sync -wait` 0. The supervisor's TRUNCATE checkpoint before it starts Litestream again forces the snapshot: a
    restore must be the live file."""
    monkeypatch.setattr(handoff, "LITESTREAM_BACKOFF", (4.0, 4.0))          # (room for the gap's writes)
    tmp, _ = rig
    env, sup, code, th = serving_leader(rig)
    try:
        c = sqlite3.connect(env.db_path("cad"), isolation_level=None, timeout=5)
        c.execute("PRAGMA wal_autocheckpoint = 0")                          # (the test says when, as exp2 did)
        c.execute("CREATE TABLE made_up_t (id INTEGER PRIMARY KEY, ts REAL, pad BLOB)")
        c.execute("CREATE TABLE made_up_u (k INTEGER PRIMARY KEY, v INTEGER, pad TEXT)")
        c.execute("BEGIN")
        c.executemany("INSERT INTO made_up_u VALUES (?, 0, ?)", [(i, "x" * 200) for i in range(3000)])
        c.execute("COMMIT")

        def ins(n: int) -> None:
            for _ in range(n):
                c.execute("INSERT INTO made_up_t (ts, pad) VALUES (?, randomblob(1500))", (time.time(),))
        ins(20)
        asked(env, "cad")
        ins(150)                                                            # (under Litestream's own checkpoint size)
        asked(env, "cad")                                                   # Litestream has read to here: a long WAL
        ls0 = sup.litestream
        ls0.kill()                                                          # ... and dies
        c.execute("UPDATE made_up_u SET v = v + 1")                         # the gap: every row of u rewritten
        ins(60)
        assert c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0   # the app's autocheckpoint
        ins(30)                                                             # the WAL restarted, shorter than before
        until(lambda: sup.litestream is not ls0 and sup.litestream is not None and sup.litestream.poll() is None,
              timeout=30, what="Litestream started again")
        ins(5)
        asked(env, "cad")                                                   # sync -wait says all is in the replica
        live = counters(env.db_path("cad"))
        assert live[2] == 3000
        assert counters(restored(tmp, env.ls_config, "cad", to="gap")) == live
        assert getattr(sup, "suspect", {}) == {} and sup.role == "active"
        c.close()
    finally:
        sup.on_term()
        th.join(60)
    assert code == [0], sup.events[-5:]
    assert sup.released_positions                                           # a clean release: nothing was suspect
    assert counters(restored(tmp, env.ls_config, "cad", to="end")) == counters(env.db_path("cad"))


def test_litestream_killed_under_a_steadily_writing_app_and_started_again_misses_nothing(rig):
    """The same at a natural rate: SQLite's default autocheckpoint, a writer at ~200 commits a second, Litestream
    SIGKILLed and started again by the supervisor 2 s later, as on the hub. (Without the TRUNCATE this one fails only
    when the restarted WAL happens to be shorter than where Litestream had read to: the test above pins that.)"""
    tmp, _ = rig
    env, sup, code, th = serving_leader(rig)
    stop = threading.Event()

    def writer() -> None:
        import random
        rnd = random.Random(7)
        w = sqlite3.connect(env.db_path("cad"), isolation_level=None, timeout=5)
        while not stop.is_set():
            w.execute("BEGIN IMMEDIATE")
            w.execute("INSERT INTO made_up_t (ts, pad) VALUES (?, randomblob(600))", (time.time(),))
            for _ in range(2):
                w.execute("UPDATE made_up_u SET v = v + 1 WHERE k = ?", (rnd.randrange(30000),))
            w.execute("COMMIT")
            time.sleep(0.005)
        w.close()
    wt = threading.Thread(target=writer, daemon=True)
    try:
        c = sqlite3.connect(env.db_path("cad"), isolation_level=None, timeout=5)
        c.execute("CREATE TABLE made_up_t (id INTEGER PRIMARY KEY, ts REAL, pad BLOB)")
        c.execute("CREATE TABLE made_up_u (k INTEGER PRIMARY KEY, v INTEGER, pad TEXT)")
        c.execute("BEGIN")
        c.executemany("INSERT INTO made_up_u VALUES (?, 0, ?)", [(i, "x" * 200) for i in range(30000)])
        c.execute("COMMIT")
        c.close()
        asked(env, "cad")
        wt.start()
        time.sleep(1.0)
        asked(env, "cad", wait=False)                                       # (a tick: Litestream well into the WAL)
        ls0 = sup.litestream
        ls0.kill()
        until(lambda: sup.litestream is not ls0 and sup.litestream is not None and sup.litestream.poll() is None,
              timeout=30, what="Litestream started again")
        time.sleep(4.0)                                                     # it writes on under the new Litestream
        stop.set()
        wt.join(10)
        asked(env, "cad")
        assert counters(restored(tmp, env.ls_config, "cad", to="gap")) == counters(env.db_path("cad"))
        assert getattr(sup, "suspect", {}) == {}
    finally:
        stop.set()
        sup.on_term()
        th.join(60)
    assert code == [0], sup.events[-5:]
