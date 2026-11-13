"""The hub container's supervisor: hot/cold deploys behind the single-writer lock (GitHub #247, docs/cloud.md
"Deploys: the hot/cold handoff").

    python -m sizeup.handoff --config deploy/hub/config.hub.toml      (deploy/hub/entrypoint.sh, LITESTREAM_BUCKET set)

The ECS service runs a new task beside the old one (minimum healthy 100 %, maximum 200 %). Only the holder of
the S3 lock (sizeup/leaderlock.py) writes: the SQLite files, Litestream's replica, the cache copies, and only
its task runs the Cloudflare connector. Every task is one of:

  STARTING  the lock is read; followers (`litestream restore -f`) restore cad / hub / audio .sqlite into
            /data/standby/ and keep applying the leader's changes; the map caches come back from S3
            (sizeup/cachesync.py); once they are, the app is started GATED (#264: sizeup/app.py run_gated): it
            imports everything and builds what needs no database (the gazetteer, the map providers and their
            caches, the ATC rules), says "warm" and waits. The lock free (none yet, released, or expired): take
            it -> TAKEOVER.
  STANDBY   the lock is someone else's: nothing is written but /data/standby/ and the caches. The app waits at its
            gate (no lease file: it couldn't write if it tried), no `litestream replicate`, no connector (it waits
            for the `leader` file). /healthz on the control port (8089) answers 200 once every follower has
            restored, the caches are back and the app is warm ("warm"), so ECS stops the old task only when this
            one can take over in seconds.
  TAKEOVER  lock taken (generation + 1): the replica's last position is read (`restore -dry-run`, the three at
            once), at least the positions the old leader released with; the followers catch up to it (only the
            last few changes: seconds), are stopped (cleanly, or nothing is promoted), quick-checked (a normal
            read-only open, no -wal / -shm beside them) and moved into /data; `litestream replicate`, and its
            first position of every promoted file confirmed (THE ORDERING RULE, below); the gate says
            "init" (the app does --init-state's work: the migrations); every file confirmed again; the gate
            says "run": the app's port answers "updating" at once, so the `leader` file is written then (the
            connector registers while the app is built); the app's /healthz; once the connector is up too, phase
            "serving" in the lock. One timing line says where the time went.
  ACTIVE    the lock renewed every 10 s. Not renewed for 20 s, or written by someone else: FENCED (below).
  DRAINING  SIGTERM (ECS, after the new task is healthy): phase "draining"; the app stops taking requests (503 +
            Retry-After), finishes those in flight and exits; port 8090 answers 503 + Retry-After from here;
            `litestream sync -wait` puts every last change in S3 and gives its position; Litestream stops; the
            lock is released with those positions.
  RELEASED  the connector stays up (answering "updating": the port is taken the moment the draining app lets go
            of it) until the new leader says "serving" in the lock (its app answers and its connector is up), for
            at most OVERLAP_S and never past shortly before ECS's SIGKILL: two connectors on the tunnel for a
            moment, never none. If nobody took the lock within RETAKE_S it is taken back and this task serves on
            until shortly before ECS kills it, then drains again. A taker that failed and gave it back is taken
            back from our own files if its release says "pristine_from" our positions (nothing but its
            Litestream's snapshot of them in the replica); not if anything else moved the replica (its migration
            wrote: not taken back; the next task restores). Then the `leader` file goes (the connector stops) and this exits.
  FENCED    the lease isn't ours or fresh: the app and Litestream are SIGKILLed at once, the `leader` file goes,
            exit 3 (ECS replaces the task). The app's own watchdog (sizeup/fence.py) does the same from inside.

Replica checks and failure pushes (sizeup/replicawatch.py, Supervisor.watch): the standby checks copies of what it
follows, the leader restores and checks the replica now and then, and a task that ends badly (failure, unclean) records
it in S3 and pushes to the status topic before it exits (docs/cloud.md "Replica checks").

Never two writers: a task writes only between taking the lock and releasing it (or 20 s after its last
successful renew, 15 s before anyone may take it over). Release happens only after the app and Litestream have
exited.

THE ORDERING RULE (docs/cloud.md "Replicate before migrate"): nothing writes a database before `litestream
replicate` has confirmed it (synced it locally: its snapshot read, its read lock held). A promoted file is exactly the replica's last position; Litestream (with no
local state of its own: "detected database behind replica db_txid=0", expected) takes its first snapshot of that
untouched file, and only then may the app migrate it, every page of the migration going through the WAL Litestream
reads. A migration run before Litestream (as until 2026-11) leaves pages in the main file that Litestream never saw
in the WAL; a replica built from that mixed old and new pages once (an out-of-order b-tree page), and every later
takeover refused it. So, before anything writes:
  * the promoted files must be untouched since they were verified (no -wal / -shm / -journal, the same size,
    mtime and header): else "written before Litestream replicates it", refused;
  * Litestream's first position of each must follow the replica's (above the position promoted);
  * a "detected database behind replica" from Litestream other than that expected first one (a local position
    that isn't 0, or a replica that isn't at the position promoted) is fatal: nothing is served from it.
A file with no replica at all (the very first start) is created by the app; Litestream's first sync of it is a
full snapshot from WAL offset 0 (nothing before it to miss), confirmed before the gate says "run". The upload
to S3 goes on meanwhile; a release waits for all of it (`litestream sync -wait`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from sizeup import leaderlock
from sizeup.leaderlock import LeaderLock, LockError, LockLost, txid
from sizeup.timing import Phases
from sizeup.web.updating import serve_updating

DBS = ("cad", "hub", "audio")
CHECKED = ("cad", "hub")       # quick-checked before they are promoted (audio: big, clips only)
SIDECARS = ("-wal", "-shm", "-journal")   # never beside a staged or promoted file: SQLite state nobody verified

POLL_S = 1.0                   # the lock, while standing by (0.25 s once the leader says "draining")
CATCHUP_S = 30.0               # a follower reaching the target position (else: a full restore)
RESTORE_S = 600.0              # a full restore, or a cold start's followers (no standby before us)
APP_READY_S = 120.0            # the app answering /healthz after it is started
CONNECTOR_READY_S = 30.0       # cloudflared connected to the edge (not fatal: "serving" is said anyway)
APP_STOP_S = 30.0              # the app, after SIGTERM, before SIGKILL
SYNC_S = 45.0                  # `litestream sync -wait` for the three files, in all
LITESTREAM_STOP_S = 40.0       # Litestream after SIGTERM (it syncs once more itself)
OVERLAP_S = 180.0              # the released connector, at most (and never past STOP_TIMEOUT_S - KILL_MARGIN_S)
KILL_MARGIN_S = 3.0            # ... it stops this long before ECS's SIGKILL, cleanly
INIT_S = 300.0                 # the app opening / migrating the databases after the gate says "init"
REPLICATING_S = 120.0          # Litestream's position of each file confirmed (before "init", and after it): each its own
REPLICATION_TOTAL_S = 150.0    # ... and all of them, every round, in all: a hung Litestream fails the takeover
MIN_LITESTREAM = (0, 5, 18)    # litestream#1547 (a snapshot from a stale WAL offset), #1579 (the read lock after a sync)
SKIP_CHECKS_ENV = "SIZEUP_HANDOFF_SKIP_REPLICATION_CHECKS"   # the operator's escape hatch (docs/cloud.md): "1"
APP_LISTEN_S = 30.0            # the app's port answering ("updating") after the gate says "run"
WARM_APP_RETRY_S = 30.0        # a warm (standby) app that exited: started again after this long
WARM_APP_MAX_FAILS = 3         # ... at most this often; then the standby stays unhealthy (ECS rolls the deploy back)
RETAKE_S = 20.0                # nobody took it: take it back
STOP_TIMEOUT_S = 120.0         # the task definition's stopTimeout: ECS SIGKILLs this long after SIGTERM
REDRAIN_S = 40.0               # after a retake: drain again this long before the SIGKILL
RETAKE_MIN_LEFT_S = 30.0       # ... and only with this much more left than REDRAIN_S
CREATED_WAIT_S = 3.0           # the very first lock: the replica must hold still this long (no writer from before it)
LITESTREAM_BACKOFF = (2.0, 60.0)

EXIT_FENCED = 3


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def last_json(text: str) -> dict:
    """The last JSON object Litestream printed (-json prints it indented; anything else before it is a log)."""
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("{"):
            try:
                d = json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
            if isinstance(d, dict):
                return d
    raise ValueError("no JSON answer")


BEHIND_RE = re.compile(r'msg="detected database behind replica".*?\bdb="?([^"\s]+)"?.*?\bdb_txid=([0-9a-fA-F]+)'
                       r'.*?\breplica_txid=([0-9a-fA-F]+)')


def behind_replica(line: str) -> dict | None:
    """Litestream's "detected database behind replica" log line (db.go checkDatabaseBehindReplica): which file, its
    local position and the replica's. None for any other line."""
    m = BEHIND_RE.search(line)
    if not m:
        return None
    name = Path(m.group(1)).name
    return {"db": name[:-len(".sqlite")] if name.endswith(".sqlite") else name,
            "db_txid": int(m.group(2), 16), "replica_txid": int(m.group(3), 16)}


def sidecars(path: Path) -> list[str]:
    """SQLite's own files beside path (-wal, -shm, -journal) that exist: their names."""
    return [Path(f"{path}{s}").name for s in SIDECARS if Path(f"{path}{s}").exists()]


def fingerprint(path: Path) -> tuple | None:
    """What changes when anything writes the file: size, mtime and its 100-byte header (the change counter, the
    journal mode). None: no file."""
    try:
        st = path.stat()
        with open(path, "rb") as f:
            head = f.read(100)
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns, hashlib.sha256(head).hexdigest())


# -- processes ---------------------------------------------------------------------------------------------

class Proc:
    """A child process in its own process group. If the supervisor dies, so does the container (it is the
    container's main process under the init the task definition turns on), and every child with it."""

    def __init__(self, name: str, argv: list[str], env: dict | None = None,
                 on_stderr: Callable[[str], None] | None = None):
        self.name = name
        piped = dict(stderr=subprocess.PIPE, text=True, errors="replace") if on_stderr is not None else {}
        self.p = subprocess.Popen(argv, env=env, start_new_session=True, **piped)
        self.pid = self.p.pid
        if on_stderr is not None:
            threading.Thread(target=self._pump, args=(on_stderr,), name="stderr-pump", daemon=True).start()

    def _pump(self, on_line: Callable[[str], None]) -> None:
        """Its stderr, line by line, passed on to ours (the container log) and to on_line. Always drained: a full
        pipe would stall the child."""
        for line in self.p.stderr:
            try:
                sys.stderr.write(line)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
            try:
                on_line(line)
            except Exception:
                pass

    def poll(self) -> int | None:
        return self.p.poll()

    def send(self, sig: int) -> None:
        try:
            os.killpg(self.p.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                self.p.send_signal(sig)
            except ProcessLookupError:
                pass

    def stop(self, timeout: float, sig: int = signal.SIGTERM) -> int | None:
        """SIGTERM, wait, then SIGKILL. Its exit code."""
        if self.poll() is None:
            self.send(sig)
            try:
                return self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                log(f"handoff: {self.name} still running after {timeout:.0f} s: killed")
        self.kill()
        return self.p.poll()

    def kill(self) -> None:
        if self.poll() is None:
            self.send(signal.SIGKILL)
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                pass


class Env:
    """Everything the supervisor runs or asks, behind one seam (tests/test_handoff.py fakes it)."""

    def __init__(self, config: str, state_dir: Path, ls_config: str, socket_path: str, port: int,
                 connector_ready_url: str = "http://127.0.0.1:20241/ready"):
        self.config, self.state_dir, self.ls_config, self.socket = config, state_dir, ls_config, socket_path
        self.port, self.connector_ready_url = port, connector_ready_url
        self.python = sys.executable
        self.behind_events: list[dict] = []     # Litestream's "detected database behind replica", as they come

    def db_path(self, db: str) -> Path:
        return self.state_dir / f"{db}.sqlite"

    def _ls(self, *args: str, timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(["litestream", *args], capture_output=True, text=True, timeout=timeout)

    def plan(self, db: str) -> int | None:
        """The replica's last position for it; None: there is no backup of it at all. RuntimeError: can't say."""
        out = self.state_dir / ".plan" / f"{db}.sqlite"
        r = self._ls("restore", "-dry-run", "-json", "-config", self.ls_config, "-o", str(out), str(self.db_path(db)),
                     timeout=60)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            if "no matching backup files" in err or "no snapshots available" in err:
                return None
            raise RuntimeError(f"litestream restore -dry-run {db}: {err[-300:]}")
        n = txid(last_json(r.stdout).get("max_txid"))
        if n is None:
            raise RuntimeError(f"litestream restore -dry-run {db}: no max_txid")
        return n

    def follower(self, db: str, out: Path) -> Proc:
        out.parent.mkdir(parents=True, exist_ok=True)
        return Proc(f"follower {db}", ["litestream", "restore", "-f", "-follow-interval", "1s", "-if-replica-exists",
                                       "-config", self.ls_config, "-o", str(out), str(self.db_path(db))])

    def applied(self, out: Path) -> int | None:
        """A follower's position (its -txid sidecar); None: not restored yet."""
        try:
            return txid(Path(str(out) + "-txid").read_text().strip())
        except OSError:
            return None

    def restore(self, db: str, out: Path) -> int:
        """A full restore of the latest (the followers' fallback): its position."""
        out.parent.mkdir(parents=True, exist_ok=True)
        for p in (out, Path(str(out) + "-wal"), Path(str(out) + "-shm"), Path(str(out) + "-txid")):
            p.unlink(missing_ok=True)
        r = self._ls("restore", "-json", "-config", self.ls_config, "-o", str(out), str(self.db_path(db)), timeout=RESTORE_S)
        if r.returncode != 0:
            raise RuntimeError(f"litestream restore {db}: {(r.stderr or r.stdout).strip()[-300:]}")
        n = txid(last_json(r.stdout).get("txid"))
        if n is None:
            raise RuntimeError(f"litestream restore {db}: no txid")
        return n

    def checkpoint(self, path: Path) -> None:
        """Everything in path's WAL into the file, the WAL emptied and gone (PRAGMA wal_checkpoint(TRUNCATE), then
        the last connection closed). Only with nothing else open on it. RuntimeError if SQLite couldn't."""
        if not Path(f"{path}-wal").exists():
            return
        con = sqlite3.connect(path, isolation_level=None)
        try:
            busy, _log, _done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
        if busy:
            raise RuntimeError(f"{path.name}: the checkpoint was blocked (something still has it open)")
        wal = Path(f"{path}-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise RuntimeError(f"{path.name}: its WAL not empty after the checkpoint")
        for s in ("-wal", "-shm"):                # (an empty WAL kept by SQLITE_FCNTL_PERSIST_WAL builds: macOS)
            Path(f"{path}{s}").unlink(missing_ok=True)

    def litestream_version(self) -> tuple[int, ...] | None:
        """`litestream version` as numbers; None if it can't say."""
        try:
            r = self._ls("version", timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
        return tuple(int(x) for x in m.groups()) if m else None

    def replicate(self) -> Proc:
        """`litestream replicate`; its stderr is watched for "detected database behind replica" (behind_events)."""
        self.behind_events.clear()

        def watch(line: str) -> None:
            ev = behind_replica(line)
            if ev is not None:
                self.behind_events.append(ev)
        return Proc("litestream", ["litestream", "replicate", "-config", self.ls_config], on_stderr=watch)

    def position(self, db: str, timeout: float, wait: bool = True) -> tuple[int, int | None]:
        """`litestream sync`: (Litestream's position of db, the replica's). wait: only once every change up to it
        is in S3 (-wait); else once Litestream has synced the file locally (its LTX file written: it has read the
        file, and holds its read lock), the upload to follow. RuntimeError if Litestream can't say so (not up yet,
        not replicating it, S3 refused)."""
        r = self._ls("sync", *(["-wait"] if wait else []), "-json", "-timeout", str(max(1, int(timeout))), "-socket",
                     self.socket, str(self.db_path(db)), timeout=timeout + 5)
        if r.returncode != 0:
            raise RuntimeError(f"litestream sync {db}: {(r.stderr or r.stdout).strip()[-300:]}")
        d = last_json(r.stdout)
        local, replica = txid(d.get("txid")), txid(d.get("replica_txid"))
        if local is None or (wait and (replica is None or replica < local)):
            raise RuntimeError(f"litestream sync {db}: replica at {replica}, the database at {local}")
        return local, replica

    def sync(self, db: str, timeout: float) -> int:
        """Every change of db in S3: the replica's position. RuntimeError if Litestream can't say so."""
        return self.position(db, timeout)[1]

    def start_app(self, env: dict) -> Proc:
        """The app, gated (env has SIZEUP_APP_GATE: sizeup/app.py run_gated): it warms up and waits for the gate."""
        return Proc("app", [self.python, "-m", "sizeup", "--config", self.config], env=env)

    def app_answers(self) -> bool:
        """Anything answers on the app's port (its early "updating" answer counts: a 503)."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2.0):
                return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False

    def _get(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                return r.status == 200
        except OSError:
            return False

    def app_healthy(self) -> bool:
        return self._get(f"http://127.0.0.1:{self.port}/healthz")

    def app_status(self) -> dict | None:
        """The app's /healthz answer (serving, what is still loading: #251); None if it doesn't answer 200."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2.0) as r:
                d = json.loads(r.read()) if r.status == 200 else None
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def connector_ready(self) -> bool:
        return self._get(self.connector_ready_url)

    def quick_check(self, path: Path) -> str:
        """PRAGMA quick_check through a normal read-only open: SQLite's own view of the file (an immutable open
        read the main file alone and would pass a file whose -wal said otherwise). Refused outright with a -wal,
        -shm or -journal beside it: a follower writes the main file only, so one is state nobody verified. The
        empty -wal / -shm the read-only open of a WAL-mode file makes are removed again."""
        there = sidecars(path)
        if there:
            return f"refused: {', '.join(there)} beside it (not a follower's clean copy)"
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            res = str(con.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            con.close()
        wal = Path(f"{path}-wal")
        if wal.exists() and wal.stat().st_size > 0:
            return f"refused: {wal.name} written by a read-only open"
        for s in SIDECARS:
            Path(f"{path}{s}").unlink(missing_ok=True)
        return res

    def warm_caches(self, deadline_s: float) -> None:
        from sizeup.cachesync import CacheMirror
        m = CacheMirror.from_env(self.state_dir, log=log)
        if m is not None:
            m.restore(deadline_s)
            m.save_manifest()


def serve(port: int, handler, host: str = "127.0.0.1") -> ThreadingHTTPServer | None:
    try:
        srv = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        log(f"handoff: port {port}: {e}")
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, name=f"http-{port}", daemon=True).start()
    return srv


# -- the supervisor --------------------------------------------------------------------------------------

class Supervisor:
    def __init__(self, lock: LeaderLock, env: Env, run_dir: Path, shared_dir: Path, control_port: int = 8089,
                 mono: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 timing: dict | None = None, watch=None):
        self.lock, self.env = lock, env
        self.watch = watch                        # sizeup/replicawatch.py ReplicaWatch: replica checks, failures pushed
        self.run_dir, self.shared = run_dir, shared_dir
        self.control_port = control_port
        self.mono, self.sleep = mono, sleep
        t = dict(POLL_S=POLL_S, CATCHUP_S=CATCHUP_S, RESTORE_S=RESTORE_S, APP_READY_S=APP_READY_S,
                 CONNECTOR_READY_S=CONNECTOR_READY_S, APP_STOP_S=APP_STOP_S, SYNC_S=SYNC_S,
                 LITESTREAM_STOP_S=LITESTREAM_STOP_S, OVERLAP_S=OVERLAP_S, RETAKE_S=RETAKE_S,
                 STOP_TIMEOUT_S=STOP_TIMEOUT_S, REDRAIN_S=REDRAIN_S, RENEW_S=leaderlock.RENEW_S,
                 RETAKE_MIN_LEFT_S=RETAKE_MIN_LEFT_S, CREATED_WAIT_S=CREATED_WAIT_S, KILL_MARGIN_S=KILL_MARGIN_S,
                 INIT_S=INIT_S, APP_LISTEN_S=APP_LISTEN_S, WARM_APP_RETRY_S=WARM_APP_RETRY_S,
                 WARM_APP_MAX_FAILS=WARM_APP_MAX_FAILS, REPLICATING_S=REPLICATING_S,
                 REPLICATION_TOTAL_S=REPLICATION_TOTAL_S)
        t.update(timing or {})
        self.t = t
        self.role = "starting"
        self.detail = ""
        self.term = threading.Event()
        self.term_at: float | None = None
        self.lost = threading.Event()
        self.renewing = threading.Event()
        self._renew_now = threading.Event()
        self._write_lock = threading.RLock()   # the lease is written by the renew thread and the main one (#282)
        self._lease_open = False                # (under _write_lock) the lease file may be written: held, not let go
        self._renew_token: object | None = None
        self.followers: dict[str, Proc] = {}
        self.follow_done: set[str] = set()        # restored (or: no replica to restore)
        self.no_replica: set[str] = set()
        self.caches_warm = threading.Event()
        self.app: Proc | None = None              # the app that may write (after the gate says "init")
        self.warm_app: Proc | None = None         # the app started while standing by, waiting at its gate (#264)
        self.app_warm = threading.Event()         # ... it said "warm"
        self._warm_fails = 0                      # ... how often it exited before the takeover
        self._warm_app_at: float | None = None
        self.litestream: Proc | None = None
        self.updating_srv: ThreadingHTTPServer | None = None
        self._port_stop = threading.Event()
        self._port_thread: threading.Thread | None = None
        self.timing: Phases | None = None
        self.control_srv: ThreadingHTTPServer | None = None
        self.events: list[str] = []               # what happened, in order (the tests read it)
        self.positions: dict | None = None
        self.released_positions: dict | None = None
        self.promoted: dict[str, tuple | None] = {}   # each file as promoted (fingerprint): untouched until Litestream has it
        self._confirm_by: float | None = None          # the replication confirms' overall deadline (this takeover)
        self.first_positions: dict[str, int] = {}      # Litestream's first position of each, its snapshot of the untouched file
        self.app_told = False                          # the app was told "init" / "run" in this takeover: it may have written
        self.failed_takeover = False                   # step_down after a takeover that failed (a release may be "pristine")
        self.skip_checks = os.environ.get(SKIP_CHECKS_ENV, "").strip() == "1"
        self.litestream_warning = ""                   # Litestream older than MIN_LITESTREAM (the Health panel shows it)
        self.app_env: dict = dict(os.environ)
        self.failure: tuple[str, str] | None = None   # (kind, why) this task ends badly for (replicawatch.TITLES)
        self.unclean = False                          # it released the lock without every change confirmed in S3

    # -- small helpers ------------------------------------------------------------------------------------

    def note(self, what: str) -> None:
        self.events.append(what)
        log(f"handoff: {what}")

    @property
    def standby_dir(self) -> Path:
        return self.env.state_dir / "standby"

    def staged(self, db: str) -> Path:
        return self.standby_dir / f"{db}.sqlite"

    @property
    def leader_file(self) -> Path:
        return self.shared / "leader"

    @property
    def lease_file(self) -> Path:
        return self.run_dir / "lease.json"

    @property
    def gate_file(self) -> Path:
        """What the gated app may do (sizeup/app.py run_gated): "init", then "run"."""
        return self.run_dir / "app-gate"

    @property
    def gate_answer_file(self) -> Path:
        """The gated app's answer: "warm", then "ok" (or "error: ...") after "init"."""
        return self.run_dir / "app-gate.answer"

    def _gate(self, word: str) -> None:
        self._write(self.gate_file, word + "\n")

    def _gate_answer(self) -> str:
        try:
            return self.gate_answer_file.read_text().strip()
        except OSError:
            return ""

    def _write(self, path: Path, text: str) -> None:
        """path replaced at once by text. The renew thread and this one both write the lease file: one at a time,
        each through a temp file of its own (#264: a shared ".lease.json.tmp" raced, the second os.replace failing)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)

    def write_lease(self) -> None:
        with self._write_lock:            # (an RLock) what it says and the write in one go: the newest lands last
            if not self._lease_open:      # let go (released, fenced): a renew finishing late never brings it back
                return
            self._write(self.lease_file, json.dumps({
                "generation": self.lock.generation, "valid_until": self.lock.valid_until, "holder": self.lock.holder,
                "litestream_pid": self.litestream.pid if self.litestream is not None else None}))

    def heartbeat(self) -> None:
        self._write(self.shared / "hub-alive", f"{int(time.time())}\n")

    def warm(self) -> bool:
        return self.caches_warm.is_set() and self.follow_done >= set(DBS) and self.app_warm.is_set()

    def healthy(self, app: dict | None = None, asked: bool = False) -> bool:
        """ECS's health check (control port /healthz). A standby: only once warm. Taking over, draining,
        released: yes (this task is in the middle of something with bounds of its own). Active: the app's (which
        answers as soon as it serves the CAD, the map layers still loading: #251)."""
        if self.role in ("starting", "standby"):
            return self.warm()
        if self.role == "active":
            if self.app is None or self.app.poll() is not None:
                return False
            return app is not None if asked else self.env.app_healthy()   # asked: status() has its answer already
        return self.role in ("takeover", "draining", "released", "retaken")

    def _app_status(self) -> dict | None:
        get = getattr(self.env, "app_status", None)
        d = get() if get is not None else None
        if d is None and self.env.app_healthy():  # (an app without the #251 fields, or a test's Env)
            d = {"serving": True}
        return d

    def status(self) -> dict:
        """The control port's /healthz: role standby / active (and the others above), whether this task's app
        serves the CAD, and what it is still loading in the background (#251: map layers)."""
        app = self._app_status() if self.role in ("active", "retaken") and self.app is not None else None
        return {"ok": self.healthy(app, asked=app is not None or self.app is not None), "role": self.role, "detail": self.detail,
                "warm": self.warm(), "app_warm": self.app_warm.is_set(), "serving": bool(app and app.get("serving", True)),
                "loading": list((app or {}).get("loading") or []),
                "generation": self.lock.generation, "holder": leaderlock.short(self.lock.holder),
                "leader": leaderlock.short((self.lock.last_seen or {}).get("holder", "")),
                "leader_phase": (self.lock.last_seen or {}).get("phase"),
                **({"litestream_warning": self.litestream_warning} if self.litestream_warning else {}),
                **({"replication_checks": "skipped"} if self.skip_checks else {})}

    def control(self) -> None:
        sup = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def do_GET(self) -> None:
                st = sup.status()
                body = json.dumps(st).encode()
                self.send_response(200 if st["ok"] else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.control_srv = serve(self.control_port, H)

    # -- standby: followers and caches ---------------------------------------------------------------------

    def start_followers(self) -> None:
        self.standby_dir.mkdir(parents=True, exist_ok=True)
        for db in DBS:
            if db not in self.followers or self.followers[db].poll() is not None:
                self.followers[db] = self.env.follower(db, self.staged(db))
        threading.Thread(target=self._warm_caches, name="cache-warm", daemon=True).start()

    def _warm_caches(self) -> None:
        try:
            self.env.warm_caches(120.0)
        except Exception as e:  # never fatal: the app fetches them from their sources
            log(f"handoff: cache copies not restored ({type(e).__name__}: {e})")
        self.caches_warm.set()

    def check_followers(self) -> None:
        """Which have restored; a follower that died is started again (it resumes from its -txid file)."""
        for db, p in list(self.followers.items()):
            if self.env.applied(self.staged(db)) is not None:
                self.follow_done.add(db)
            code = p.poll()
            if code is None:
                continue
            if code == 0 and not self.staged(db).exists():
                self.no_replica.add(db)                  # -if-replica-exists: there is none
                self.follow_done.add(db)
                continue
            if self.role in ("starting", "standby", "takeover"):
                log(f"handoff: follower {db} exited {code}: started again")
                self.followers[db] = self.env.follower(db, self.staged(db))

    def stop_followers(self) -> dict[str, int | None]:
        """Each follower that was running, stopped: its exit code (0: it stopped cleanly, between two LTX files;
        -9: SIGKILLed, maybe half way through writing one into the file)."""
        codes: dict[str, int | None] = {}
        for db, p in self.followers.items():
            if p.poll() is None:
                codes[db] = p.stop(15.0)
        self.followers.clear()
        return codes

    # -- the app, warm before the takeover (#264) ------------------------------------------------------------

    def _start_app(self) -> Proc:
        """The app, gated: it warms up (imports, the gazetteer, the providers...) and waits for the gate file."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.gate_file.unlink(missing_ok=True)
        self.gate_answer_file.unlink(missing_ok=True)
        self.app_warm.clear()
        self.app_env = dict(os.environ, SIZEUP_LEASE_FILE=str(self.lease_file), SIZEUP_APP_GATE=str(self.gate_file))
        if self.litestream_warning:
            self.app_env["SIZEUP_LITESTREAM_WARNING"] = self.litestream_warning
        self._warm_app_at = self.mono()
        return self.env.start_app(self.app_env)

    def keep_warm_app(self) -> None:
        """While standing by: the gated app started once the caches are back (it reads them), started again if it
        exits, "warm" noted when it says so."""
        if not self.caches_warm.is_set():
            return
        p = self.warm_app
        if p is None:
            if self._warm_fails >= self.t["WARM_APP_MAX_FAILS"]:
                return                    # this image's app doesn't start: never healthy, ECS rolls back
            if self._warm_app_at is None or self.mono() - self._warm_app_at >= self.t["WARM_APP_RETRY_S"]:
                self.warm_app = self._start_app()
                self.note("app started, gated: warming up before the takeover")
            return
        code = p.poll()
        if code is not None:
            self._warm_fails += 1
            self.warm_app = None
            self.app_warm.clear()         # not healthy without it: a bad image must not get the old task stopped
            if self._warm_fails >= self.t["WARM_APP_MAX_FAILS"]:
                self.note(f"the warm app exited {code}, {self._warm_fails} times: given up; this task stays "
                          "unhealthy (ECS's circuit breaker rolls the deploy back, the leader serves on)")
            else:
                self.note(f"the warm app exited {code}: started again in {self.t['WARM_APP_RETRY_S']:.0f} s")
            return
        if not self.app_warm.is_set() and self._gate_answer() == "warm":
            self.app_warm.set()
            self.note("app warm: waiting at its gate")

    def stop_warm_app(self) -> None:
        if self.warm_app is not None:
            self.warm_app.stop(10.0)
            self.warm_app = None

    # -- the port while no app serves on it -------------------------------------------------------------------

    def take_port(self) -> None:
        """The "updating" answer on the app's port the moment the draining app lets go of it (#264: the port
        refused connections for ~11 s while the old app stopped, Cloudflare's 502)."""
        if self.updating_srv is not None or (self._port_thread is not None and self._port_thread.is_alive()):
            return
        self._port_stop.clear()

        def run() -> None:
            failed = None
            while not self._port_stop.is_set():
                try:
                    self.updating_srv = serve_updating("127.0.0.1", self.env.port)
                    return
                except OSError as e:
                    if failed is None:
                        failed = e
                    time.sleep(0.05)
        self._port_thread = threading.Thread(target=run, name="take-port", daemon=True)
        self._port_thread.start()

    def free_port(self) -> None:
        self._port_stop.set()
        if self._port_thread is not None:
            self._port_thread.join(2.0)
            self._port_thread = None
        if self.updating_srv is not None:
            self.updating_srv.shutdown()
            self.updating_srv.server_close()
            self.updating_srv = None

    # -- the renew thread ----------------------------------------------------------------------------------

    def _renew_loop(self, token: object) -> None:
        nxt = self.mono() + self.t["RENEW_S"]
        mine = lambda: self.renewing.is_set() and self._renew_token is token   # noqa: E731
        while mine():
            self._renew_now.wait(max(0.0, nxt - self.mono()))
            self._renew_now.clear()
            if not mine():
                break
            try:
                self.lock.renew()
                if not mine():
                    break
                self.write_lease()
                nxt = self.mono() + self.t["RENEW_S"]
            except LockLost as e:
                if mine():                    # (not: we released it a moment ago)
                    log(f"handoff: LOCK LOST: {e}")
                    self.lost.set()
                return
            except Exception as e:  # LockError and anything else: try again soon; held() runs out by itself
                log(f"handoff: renew failed ({type(e).__name__}: {e}): again in 2 s")
                nxt = self.mono() + 2.0

    def start_renewing(self) -> None:
        with self._write_lock:
            self._lease_open = True
        self.write_lease()
        self._renew_token = token = object()
        self.renewing.set()
        threading.Thread(target=self._renew_loop, args=(token,), name="lock-renew", daemon=True).start()

    def drop_lease(self) -> None:
        """The lease file gone for good (until the next take): the app's fence says "not ours" from here."""
        with self._write_lock:
            self._lease_open = False
            self.lease_file.unlink(missing_ok=True)

    def stop_renewing(self) -> None:
        self.renewing.clear()
        self._renew_now.set()

    def say(self, phase: str) -> None:
        """Our phase in the lock, now (the renew thread writes it)."""
        self.lock.phase = phase
        try:
            self.lock.renew(phase)
            self.write_lease()
        except LockLost:
            self.lost.set()
        except Exception as e:
            log(f"handoff: couldn't say {phase} ({type(e).__name__}: {e}): the next renew does")

    def must_hold(self) -> None:
        """Raise _Fenced unless we may write now."""
        if self.lost.is_set() or not self.lock.held():
            raise _Fenced("the lease was lost" if self.lost.is_set() else "the lease wasn't renewed in time")

    # -- fencing -------------------------------------------------------------------------------------------

    def fence(self, why: str) -> int:
        """Stop every write NOW: SIGKILL, no drain, no release (the lease runs out by itself)."""
        self.role = "fenced"
        self.note(f"FENCED: {why}: app and Litestream killed")
        for p in (self.app, self.warm_app, self.litestream, *self.followers.values()):
            if p is not None:
                p.kill()
        self.stop_renewing()
        self.lock.drop()
        self.leader_file.unlink(missing_ok=True)
        self.drop_lease()
        return EXIT_FENCED

    def check_litestream_version(self) -> None:
        """Said loudly (and on the Health panel, through the app) when Litestream is older than MIN_LITESTREAM."""
        get = getattr(self.env, "litestream_version", None)
        ver = get() if get is not None else None
        if ver is None or tuple(ver) >= MIN_LITESTREAM:
            return
        have, want = ".".join(map(str, ver)), ".".join(map(str, MIN_LITESTREAM))
        self.litestream_warning = (f"Litestream {have} is older than {want}: its snapshots can drop WAL frames "
                                   "(litestream#1547) and its read lock can be lost after a sync (#1579)")
        self.note(f"WARNING: {self.litestream_warning}. Deploy an image with Litestream {want} or later")

    # -- main ----------------------------------------------------------------------------------------------

    def on_term(self, *_a) -> None:
        if not self.term.is_set():
            self.term_at = self.mono()
            log("handoff: SIGTERM")
        self.term.set()

    def run(self) -> int:
        self.leader_file.unlink(missing_ok=True)       # never left over from anything
        self.check_litestream_version()
        self.control()
        threading.Thread(target=self._beat, name="heartbeat", daemon=True).start()
        if self.watch is not None:
            self.watch.start(self)
        code: int | None = None
        try:
            code = self._run()
        except _Fenced as e:
            self.failure = ("fenced", str(e))
            code = self.fence(str(e))
        except Exception as e:
            self.failure = ("crashed", f"{type(e).__name__}: {e}")
            raise
        finally:
            self._port_stop.set()
            if self.updating_srv is not None:
                self.updating_srv.shutdown()
            self._report(code)
        return code

    def _report(self, code: int | None) -> None:
        """A task that ends badly (a failed takeover, the app exiting, fenced, an unclean release, a crash): recorded
        in S3 and pushed before it exits, within replicawatch.REPORT_S (sizeup/replicawatch.py); the checks stop."""
        if self.watch is None:
            return
        self.watch.stop.set()
        if code == 0 and self.failure is None and not self.unclean:
            return
        kind, why = self.failure or (("unclean_release", "the last changes weren't confirmed in S3") if code == 0
                                     else ("exited", self.events[-1] if self.events else "?"))
        try:
            self.watch.report(kind, why, 1 if code is None else code)     # within 20 s in all, DNS included
        except Exception as e:  # noqa: BLE001 - never in the way of the exit
            log(f"handoff: the failure couldn't be reported ({type(e).__name__}: {e})")

    def _beat(self) -> None:
        while self.role not in ("done",):
            try:
                self.heartbeat()
            except OSError:
                pass
            time.sleep(1.0)

    def _run(self) -> int:
        self.note("starting: following the replica, restoring the caches")
        self.start_followers()
        prev = None
        errors_since = None
        while prev is None:
            self.check_followers()
            self.keep_warm_app()
            if self.term.is_set():
                self.note(f"stopped while {self.role}: the lock untouched")
                self.stop_followers()
                self.stop_warm_app()
                return 0
            try:
                prev = self.lock.try_take()
                errors_since = None
            except LockError as e:
                errors_since = errors_since or self.mono()
                log(f"handoff: lock not readable ({e})")
                if self.mono() - errors_since > self.t["RESTORE_S"]:
                    self.failure = ("lock_unreadable", f"the lock unreadable for {self.t['RESTORE_S']:.0f} s: {e}")
                    self.note("the lock unreadable too long: exiting")
                    self.stop_followers()
                    self.stop_warm_app()
                    return 1
            if prev is None:
                if self.role != "standby":
                    holder = (self.lock.last_seen or {}).get("holder", "")
                    self.role = "standby"
                    self.note(f"standby: the leader is {leaderlock.short(holder)}")
                draining = (self.lock.last_seen or {}).get("phase") == "draining"
                self.sleep(0.25 if draining else self.t["POLL_S"])
        return self.take_over(prev)

    # -- taking over ---------------------------------------------------------------------------------------

    def take_over(self, prev: dict) -> int:
        self.role = "takeover"
        self.timing = Phases("takeover", clock=self.mono)
        self.start_renewing()
        positions = prev.get("positions") if isinstance(prev.get("positions"), dict) else {}
        self.note(f"takeover: generation {self.lock.generation}"
                  + (f", the old leader released at {positions}" if positions else ""))
        self._say_skip()
        try:
            self._promote(prev, positions)
            self.must_hold()
            self._start_writer()
        except _Fenced:
            raise
        except Exception as e:
            self.note(f"takeover failed: {type(e).__name__}: {e}")
            self.failure = ("takeover_failed", f"{type(e).__name__}: {e}")
            if isinstance(e, ReplicaMismatch):
                self._stop_mismatched()
            self.failed_takeover = True
            if self.timing is not None:
                log(f"handoff: {self.timing.line('failed')}")
            self.stop_warm_app()
            return self.step_down(code=1, overlap=False, retake=False)
        return self.active()

    def _plan_all(self) -> dict[str, int | None | Exception]:
        """The replica's last position of every file, asked at once (each is a `litestream restore -dry-run` of its
        own, seconds of S3 listing: #264)."""
        def one(db: str):
            try:
                return self.env.plan(db)
            except Exception as e:
                return e
        with ThreadPoolExecutor(len(DBS), thread_name_prefix="plan") as ex:
            return dict(zip(DBS, ex.map(one, DBS)))

    def _target(self, prev: dict, positions: dict) -> dict[str, int | None]:
        """Where each file must be: the replica's last position (we hold the lock: it is frozen now), and never
        behind what the old leader released with."""
        out: dict[str, int | None] = {}
        first = self._plan_all()
        for db in DBS:
            end = self.mono() + 60.0
            have = first[db]
            while isinstance(have, Exception):
                if self.mono() > end:
                    raise RuntimeError(f"the replica of {db} can't be read: {have}") from None
                self.must_hold()
                self.sleep(2.0)
                try:
                    have = self.env.plan(db)
                except Exception as e:
                    have = e
            released = txid(positions.get(db))
            if have is None:
                if not prev.get("_created"):
                    raise RuntimeError(f"no backup of {db}.sqlite, yet a leader ran before: refusing to start empty")
                out[db] = None
                continue
            if released is not None and released > have:
                raise RuntimeError(f"the replica of {db} is at {have}, behind the release ({released})")
            if released is not None and have > released:
                log(f"handoff: the replica of {db} is at {have}, past the release ({released}): restoring to {have}")
            out[db] = have
        return out

    def _promote(self, prev: dict, positions: dict) -> None:
        t = self.timing or Phases("takeover", clock=self.mono)
        target = self._target(prev, positions)
        t.mark("plan")
        self.detail = f"catching up to {target}"
        for db, want in target.items():
            if want is None:
                continue
            bound = self.t["CATCHUP_S"] if db in self.follow_done else self.t["RESTORE_S"]
            end = self.mono() + bound if db in self.followers else self.mono()   # none (a take-back): restore at once
            while (self.env.applied(self.staged(db)) or 0) < want and self.mono() < end:
                self.must_hold()
                self.check_followers()
                self.sleep(0.2)
            got = self.env.applied(self.staged(db)) or 0
            if got < want:
                log(f"handoff: follower {db} at {got}, not {want} after {bound:.0f} s: a full restore")
                p = self.followers.pop(db, None)
                if p is not None:
                    p.stop(15.0)
                got = self.env.restore(db, self.staged(db))
                self.must_hold()
                if got < want:
                    raise RuntimeError(f"{db}: restored to {got}, the replica is at {want}")
                Path(str(self.staged(db)) + "-txid").write_text(f"{got:016x}\n")
        t.mark("catch-up")
        stopped = self.stop_followers()
        for db, code in stopped.items():
            if target.get(db) is not None and code != 0 and self._skipped(f"the follower of {db} exited {code}"):
                continue
            if target.get(db) is not None and code != 0:
                self.note(f"REFUSED: the follower of {db} didn't stop cleanly (exit {code}"
                          + (", SIGKILLed" if code == -signal.SIGKILL else "") + "): its copy may be half written")
                raise RuntimeError(f"{db}: the follower didn't stop cleanly (exit {code}): not promoted")
        # We hold the lock: nobody may move the replica now. If it moved, something else writes to it (a hub
        # from before the lock, still running beside us): never promote.
        if prev.get("_created"):
            self.sleep(self.t["CREATED_WAIT_S"])   # longer than Litestream's 1 s sync: an old writer would show
        again = self._plan_all()
        for v in again.values():
            if isinstance(v, Exception):
                raise v
        moved = {db: (target[db], again[db]) for db in DBS if again[db] != target[db]}
        if moved:
            raise RuntimeError(f"the replica moved while this task held the lock (another writer?): {moved}")
        for db, want in target.items():
            if want is None:
                continue
            got = self.env.applied(self.staged(db))
            if got != want:
                raise RuntimeError(f"{db}: at {got} after the followers stopped, not {want}")
            there = sidecars(self.staged(db))
            if there:
                self.note(f"REFUSED: {', '.join(there)} beside the staged {db}.sqlite: a follower writes the main "
                          "file only, so that is state nobody verified")
                raise RuntimeError(f"{db}: {', '.join(there)} beside the staged copy: not promoted")
            if db in CHECKED:
                res = self.env.quick_check(self.staged(db))
                if res != "ok":
                    self.note(f"REFUSED: {db}.sqlite quick_check: {res[:200]}")
                    raise RuntimeError(f"{db}.sqlite quick_check: {res[:200]}")
        t.mark("verify")
        self.must_hold()
        for db, want in target.items():
            dest = self.env.db_path(db)
            for p in (dest, *(Path(f"{dest}{s}") for s in SIDECARS)):
                p.unlink(missing_ok=True)
            meta = dest.with_name(f".{dest.name}-litestream")
            if meta.exists():
                import shutil
                shutil.rmtree(meta)
            if want is not None:
                os.replace(self.staged(db), dest)
            Path(f"{self.staged(db)}-txid").unlink(missing_ok=True)
            self.promoted[db] = fingerprint(dest)
        self.positions = {db: (f"{n:016x}" if n is not None else None) for db, n in target.items()}
        t.mark("promote")
        self.note(f"promoted: {self.positions}")

    # -- replicate before migrate (the module doc's ordering rule) ------------------------------------------

    def _say_skip(self) -> None:
        if self.skip_checks:
            self.note(f"WARNING: {SKIP_CHECKS_ENV}=1: the replication checks (untouched files, Litestream's "
                      "positions, 'database behind replica', the followers' clean stop) are SKIPPED for this "
                      "takeover. An operator's override for a false positive only: remove it once past")

    def _skipped(self, what: str) -> bool:
        """With the operator's override: the failed check said loudly and let through."""
        if self.skip_checks:
            self.note(f"WARNING: {what}: let through ({SKIP_CHECKS_ENV}=1)")
            return True
        return False

    def _check_untouched(self) -> None:
        """Every promoted file exactly as verified, nothing beside it: nothing has written it before Litestream
        replicates it. ReplicaMismatch if something did."""
        for db in DBS:
            dest = self.env.db_path(db)
            there = sidecars(dest)
            if there or fingerprint(dest) != self.promoted.get(db):
                what = f"{', '.join(there)} beside it" if there else "changed since it was verified"
                if self._skipped(f"{db}.sqlite {what}"):
                    continue
                self.note(f"REFUSED: {db}.sqlite was written before Litestream replicates it ({what}): Litestream "
                          "would replicate pages it never saw in the WAL")
                raise ReplicaMismatch(f"{db}.sqlite written before Litestream replicates it ({what})")

    def _check_behind(self, base: dict[str, int | None], first_start: bool) -> None:
        """Litestream's "detected database behind replica" lines so far. Expected once per promoted file on a
        takeover (first_start): no local state (db_txid 0) and the replica exactly at the position promoted, so its
        snapshot is of the untouched file. Anything else is fatal: the replica and the file disagree."""
        for ev in list(getattr(self.env, "behind_events", [])):
            db = ev.get("db")
            if first_start and ev.get("db_txid") == 0 and db in base and base[db] is not None \
                    and ev.get("replica_txid") == base[db]:
                continue
            if self._skipped(f"Litestream found {db} behind its replica ({ev.get('db_txid')} < {ev.get('replica_txid')})"):
                continue
            self.note(f"REFUSED: Litestream found {db} behind its replica (the file at {ev.get('db_txid')}, the "
                      f"replica at {ev.get('replica_txid')}, promoted at {base.get(db)}): the replica and the file "
                      "disagree; nothing is served from it")
            raise ReplicaMismatch(f"{db}: Litestream found the database behind the replica "
                                  f"({ev.get('db_txid')} < {ev.get('replica_txid')})")

    def _confirm_replicating(self, dbs, base: dict[str, int | None], first_start: bool) -> dict[str, int]:
        """`litestream sync` (no -wait) for each of dbs until it answers: Litestream replicates it, has synced it
        locally (its first LTX file: a snapshot of the untouched file read under its read lock; from here every
        write goes through the WAL it reads) and keeps uploading it. On a takeover (first_start) that position must
        be past the one promoted (its own snapshot); on a retake (its own state kept) at least it. ReplicaMismatch
        otherwise. Each file has REPLICATING_S of its own (a big one's snapshot takes its time). Not waiting for the
        upload keeps the takeover short (docs/cloud.md: ~9 s for 1.2 GB locally); S3 has it all by the release
        (`sync -wait` when draining)."""
        got: dict[str, int] = {}
        if self._confirm_by is None:
            self._confirm_by = self.mono() + self.t["REPLICATION_TOTAL_S"]
        for db in dbs:
            end = min(self.mono() + self.t["REPLICATING_S"], self._confirm_by)
            while True:
                self.must_hold()
                if self.litestream is None or self.litestream.poll() is not None:
                    raise RuntimeError(f"Litestream exited ({self.litestream.poll() if self.litestream else None}) "
                                       f"before it replicated {db}")
                try:
                    local, replica = self.env.position(db, max(1.0, min(30.0, end - self.mono())), wait=False)
                    break
                except Exception as e:
                    if self.mono() >= end:
                        raise RuntimeError(f"Litestream didn't confirm {db} in time ({self.t['REPLICATING_S']:.0f} s "
                                           f"a file, {self.t['REPLICATION_TOTAL_S']:.0f} s in all): {e}") from None
                    self.sleep(0.5)
            was = base.get(db)
            ok = (local >= 1 if was is None else local > was if first_start else local >= was) \
                and (replica is None or replica <= local)
            if not ok and self._skipped(f"Litestream's position of {db} {local} (replica {replica}), promoted {was}"):
                ok = True
            if not ok:
                self.note(f"REFUSED: Litestream's position of {db} ({local}, the replica {replica}) doesn't follow "
                          f"the position promoted ({was})")
                raise ReplicaMismatch(f"{db}: Litestream at {local} (replica {replica}), promoted at {was}")
            got[db] = local
        self._check_behind(base, first_start)
        return got

    def _start_writer(self) -> None:
        """Litestream, confirmed replicating every promoted file; the warm app opens and migrates the databases
        ("init"); every file confirmed again; the app serves ("run"): its port answers at once, so the leader
        file (the connector) goes in then; its /healthz; the connector up; "serving"."""
        t = self.timing or Phases("takeover", clock=self.mono)
        self.must_hold()
        app = self.warm_app if self.warm_app is not None and self.warm_app.poll() is None else None
        self.warm_app = None
        if app is None:
            self.note("no warm app: one started now (it warms up first)")
            app = self._start_app()
        self.app = app
        base = {db: txid(v) for db, v in (self.positions or {}).items()}
        self._confirm_by = None                     # (REPLICATION_TOTAL_S from the first confirm)
        self._check_untouched()
        self.litestream = self.env.replicate()
        self.write_lease()
        have = [db for db in DBS if base.get(db) is not None]
        got = self._confirm_replicating(have, base, first_start=True)
        self.first_positions = dict(got)
        self.note(f"Litestream replicates {', '.join(have) or 'nothing yet (no replica)'}"
                  + (f": its first positions {got} (its snapshots of the untouched files)" if got else "")
                  + "; the app may write now")
        t.mark("litestream")
        self.must_hold()
        self.write_lease()                          # fresh for the app's fence, which it reads at the gate
        self.gate_answer_file.unlink(missing_ok=True)
        self.app_told = True                        # from here the replica may hold the app's writes
        self._gate("init")
        end = self.mono() + self.t["INIT_S"]
        while True:
            self.must_hold()
            ans = self._gate_answer()
            if ans == "ok":
                break
            if ans.startswith("error"):
                raise RuntimeError(f"the app couldn't open the databases: {ans}")
            if app.poll() is not None:
                raise RuntimeError(f"the app exited {app.poll()} while opening the databases")
            if self.mono() > end:
                raise RuntimeError(f"the app didn't open the databases in {self.t['INIT_S']:.0f} s")
            self.sleep(0.05)
        t.mark("init-state")
        got = self._confirm_replicating(DBS, base, first_start=True)   # the migrations synced (and new files replicated)
        self.note(f"migrated, Litestream at {got}")
        t.mark("replicated")
        self.must_hold()
        self.free_port()                            # (a take-back: the "updating" answer held it since the drain)
        self._gate("run")
        self.note("app started")
        end = self.mono() + self.t["APP_LISTEN_S"]
        answers = self.env.app_answers()
        while not answers and self.mono() < end:
            self.must_hold()
            if app.poll() is not None:
                raise RuntimeError(f"the app exited {app.poll()} while starting")
            self.sleep(0.05)
            answers = self.env.app_answers()
        t.mark("listen")
        if answers:                                  # ("updating" until the CAD serves: the connector registers meanwhile)
            self._leader_file_now("the app's port answers")
        end = self.mono() + self.t["APP_READY_S"]
        while not self.env.app_healthy():
            self.must_hold()
            if self.app.poll() is not None:
                raise RuntimeError(f"the app exited {self.app.poll()} while starting")
            if self.mono() > end:
                raise RuntimeError(f"the app didn't answer /healthz in {self.t['APP_READY_S']:.0f} s")
            self.sleep(0.1)
        t.mark("app")
        if not self.leader_file.exists():
            self._leader_file_now("the app serves")
        end = self.mono() + self.t["CONNECTOR_READY_S"]
        ready = self.env.connector_ready()
        while not ready and self.mono() < end:
            self.must_hold()
            self.sleep(0.2)
            ready = self.env.connector_ready()
        t.mark("connector")
        # "serving" in the lock is what lets the old task drop its connector: only once ours is up (#264), else the
        # tunnel would have none. Not up yet: "connecting", and active() says "serving" the moment it is.
        self.say("serving" if ready else "connecting")
        self.role = "active"
        self.detail = ""
        self.note(f"active: serving, generation {self.lock.generation}"
                  + ("" if ready else "; the connector isn't up yet: the old one stays until it is"))
        log(f"handoff: {t.line()}")

    def _stop_mismatched(self) -> None:
        """The replica and the file disagree: Litestream SIGKILLed (no last sync of the file it disagrees with),
        the app stopped; the release that follows is unclean (no positions)."""
        if self.litestream is not None:
            self.litestream.kill()
            self.litestream = None
        self.note("Litestream stopped without a last sync; see docs/cloud.md \"Reseeding a replica\"")

    def _leader_file_now(self, why: str) -> None:
        self.must_hold()
        self._write(self.leader_file, f"{self.lock.generation}\n")
        self.note(f"leader file written ({why}): the connector starts")

    # -- active --------------------------------------------------------------------------------------------

    def active(self, deadline: float | None = None, retake: bool = True) -> int:
        backoff = LITESTREAM_BACKOFF[0]
        restart_at = None
        asked_at = self.mono()
        while True:
            self.must_hold()
            if self.lock.phase == "connecting" and self.mono() - asked_at >= 1.0:
                asked_at = self.mono()
                if self.env.connector_ready():
                    self.say("serving")
                    self.note("the connector is up: serving")
            if self.term.is_set() and deadline is None:
                return self.step_down(code=0, overlap=True, retake=retake)
            if deadline is not None and self.mono() >= deadline:
                self.note("taken back: draining again before ECS stops this task")
                return self.step_down(code=0, overlap=True, retake=False)
            if self.app is not None and self.app.poll() is not None:
                self.note(f"the app exited {self.app.poll()}: handing over")
                self.failure = ("app_exited", f"the app exited {self.app.poll()} while serving")
                return self.step_down(code=1, overlap=False, retake=False)
            if self.litestream is not None and self.litestream.poll() is not None:
                if restart_at is None:
                    log(f"handoff: Litestream exited {self.litestream.poll()}: started again in {backoff:.0f} s")
                    restart_at = self.mono() + backoff
                elif self.mono() >= restart_at:
                    self.must_hold()
                    self.litestream = self.env.replicate()
                    self.write_lease()
                    backoff, restart_at = min(backoff * 2, LITESTREAM_BACKOFF[1]), None
            self.sleep(0.25)

    # -- stepping down ---------------------------------------------------------------------------------------

    def step_down(self, code: int, overlap: bool, retake: bool) -> int:
        self.role = "draining"
        t = Phases("drain", clock=self.mono)
        self.note("draining: the app finishes what's in flight")
        self.say("draining")
        self.take_port()                          # "updating" on the port the moment the app lets go of it
        if self.app is not None:
            self.app.stop(self.t["APP_STOP_S"])
            self.app = None
        t.mark("app")
        self.must_hold()
        positions, clean = {}, self.litestream is not None
        end = self.mono() + self.t["SYNC_S"]
        if self.litestream is not None:
            for db in DBS:
                while True:
                    try:
                        positions[db] = f"{self.env.sync(db, max(1.0, min(30.0, end - self.mono()))):016x}"
                        break
                    except Exception as e:
                        if self.mono() >= end or (self.litestream.poll() is not None):
                            log(f"handoff: {db} not confirmed in S3: {e}")
                            clean = False
                            break
                        self.must_hold()
                        self.sleep(1.0)
                if not clean:
                    break
            t.mark("sync")
            self.must_hold()
            self.litestream.stop(self.t["LITESTREAM_STOP_S"])
            self.litestream = None
            t.mark("litestream")
        self.must_hold()
        self.stop_renewing()
        extra = self._pristine(positions, clean)
        try:
            released = self.lock.release(positions if clean else None, clean, extra)
        except LockLost as e:
            raise _Fenced(f"lost the lock before releasing it: {e}") from None
        except LockError as e:
            self.note(f"release not confirmed ({e}): the lease runs out by itself in {leaderlock.TTL_S:.0f} s")
            released = ""
        self.drop_lease()
        self.released_positions = positions if clean else None
        self.role = "released"
        t.mark("release")
        self.note(f"released: {positions if clean else 'unclean'}")
        self.unclean = self.unclean or not clean
        log(f"handoff: {t.line(self._since_term())}")
        if overlap:
            res = self.overlap(released, retake)
            if res is not None:
                return res
        self.leader_file.unlink(missing_ok=True)
        self.note("leader file removed: the connector stops")
        self.role = "done"
        return code

    def _pristine(self, positions: dict, clean: bool) -> dict | None:
        """A takeover that failed before its app wrote anything leaves the replica at the positions it promoted plus
        only its own Litestream's snapshot of those untouched files: the same data. Its release says so
        ("pristine_from": the positions promoted), and the old leader may take it back from its own files
        (_retake). "Before the app wrote": never told "init", or every file still at Litestream's first position
        after the last sync (a migration that failed before writing a thing)."""
        if not self.failed_takeover or not self.positions:
            return None
        promoted = {db: txid(v) for db, v in self.positions.items()}
        if any(promoted.get(db) is None for db in DBS):
            return None                                 # a file made by this task: not the old leader's
        if self.app_told:
            at = {db: txid(positions.get(db)) for db in DBS} if clean else {}
            if not clean or at != self.first_positions:
                return None
        self.note(f"released as pristine: nothing but Litestream's snapshot of the files promoted at {self.positions}")
        return {"pristine_from": dict(self.positions)}

    def overlap(self, released_etag: str, retake: bool) -> int | None:
        """The connector stays up ("updating") until the new leader says "serving" in the lock: its app answers and
        its own connector is up, so the tunnel never has none (#264). At most OVERLAP_S, and never past KILL_MARGIN_S
        before ECS's SIGKILL. Nobody taking the lock: take it back."""
        start = self.mono()
        end = min(start + self.t["OVERLAP_S"], self._kill_at() - self.t["KILL_MARGIN_S"])
        seen = ""
        while self.mono() < end:
            try:
                doc, etag = self.lock.read()
            except LockError:
                doc, etag = None, ""
            if doc and doc.get("holder") != self.lock.holder and doc.get("state") == "held":
                if doc.get("phase") != seen:
                    seen = str(doc.get("phase") or "")
                    log(f"handoff: the new leader {leaderlock.short(doc.get('holder', ''))} is {seen or 'starting'}")
                if doc.get("phase") == "serving":
                    self.note(f"the new leader {leaderlock.short(doc.get('holder', ''))} serves: after "
                              f"{self.mono() - start:.1f} s of overlap{self._since_term(', ')}")
                    return None
            elif (retake and self.released_positions and leaderlock.is_free(doc, self.lock.wall(), self.lock.grace)
                  and self.mono() - start >= self.t["RETAKE_S"]
                  and self._time_left() > self.t["REDRAIN_S"] + self.t["RETAKE_MIN_LEFT_S"]):
                res = self._retake(released_etag, etag)
                if res is not None:
                    return res
            self.sleep(max(0.0, min(0.25, end - self.mono())))
        cut = self._kill_at() - self.t["KILL_MARGIN_S"] < start + self.t["OVERLAP_S"]
        self.note(f"no new leader serving after {self.mono() - start:.0f} s"
                  + (" (ECS stops this task in moments)" if cut else ""))
        return None

    def _kill_at(self) -> float:
        """When ECS SIGKILLs this task (inf: no SIGTERM yet)."""
        return float("inf") if self.term_at is None else self.term_at + self.t["STOP_TIMEOUT_S"]

    def _since_term(self, sep: str = "") -> str:
        return "" if self.term_at is None else f"{sep}{self.mono() - self.term_at:.1f} s since SIGTERM"

    def _time_left(self) -> float:
        if self.term_at is None:
            return float("inf")
        return self.term_at + self.t["STOP_TIMEOUT_S"] - self.mono()

    def _retake(self, released_etag: str, etag: str) -> int | None:
        """Take the lock back and serve on from our own files, which are exactly what we released. Only when the
        replica is still where we left it: our own release untouched, or a new task that took it and gave it back
        without writing anything (its Litestream never ran). Anything else (it wrote, then failed) means our files
        are behind: never resumed from (the next task restores)."""
        base: dict | None = None                      # the replica moved by a pristine taker: Litestream starts afresh
        try:
            if etag == released_etag:
                if not self.lock.retake(released_etag):
                    return None
            else:
                prev = self.lock.try_take(phase="starting")
                if prev is None:
                    return None
                here = {db: self.env.plan(db) for db in DBS}
                ours = {db: txid(v) for db, v in self.released_positions.items()}
                pf = prev.get("pristine_from")
                if here != ours and isinstance(pf, dict) and {db: txid(pf.get(db)) for db in DBS} == ours:
                    self.note(f"the replica moved on since our release ({ours} -> {here}), by nothing but the "
                              "failed taker's snapshot of our own files (pristine): taking it back from them")
                    base = here
                elif here != ours:
                    self.note(f"the replica moved on since our release ({ours} -> {here}): not taking it back (its "
                              "writes are newer than our files; the next task, or a rolled-back deploy's, restores)")
                    self.lock.release(prev.get("positions") if isinstance(prev.get("positions"), dict) else None,
                                      bool(prev.get("clean")))
                    return None
        except LockLost:
            return None
        except _Fenced:
            raise
        except Exception as e:  # LockError, the replica unreadable: not taken back
            log(f"handoff: not taken back ({type(e).__name__}: {e})")
            if self.lock.generation > 0:
                try:
                    self.lock.release(self.released_positions, True)
                except Exception:
                    self.lock.drop()
            return None
        self.role = "retaken"
        self.note(f"taken back: generation {self.lock.generation}; serving until ECS stops this task")
        self.start_renewing()
        try:
            self.must_hold()
            if base is not None:                      # our files are the data; Litestream snapshots them afresh
                for db in DBS:                        # (nothing runs on them: the app and Litestream are stopped)
                    self.env.checkpoint(self.env.db_path(db))     # no WAL left for a Litestream without state
                    there = sidecars(self.env.db_path(db))
                    if there:
                        raise RuntimeError(f"{db}.sqlite: {', '.join(there)} still there after the checkpoint")
                    shutil.rmtree(self.env.db_path(db).with_name(f".{db}.sqlite-litestream"), ignore_errors=True)
            self._confirm_by = None
            self.litestream = self.env.replicate()
            self.write_lease()
            mine = {db: txid(v) for db, v in self.released_positions.items()} if base is None else base
            self._confirm_replicating([db for db in DBS if mine.get(db) is not None], mine, first_start=base is not None)
            self.app = self._start_app()
            end = self.mono() + self.t["APP_READY_S"]
            while self._gate_answer() != "warm":     # "updating" from here until the app takes the port
                self.must_hold()
                if self.app.poll() is not None or self.mono() > end:
                    raise RuntimeError("the app didn't come back")
                self.sleep(0.1)
            self.free_port()
            self._gate("run")
            while not self.env.app_healthy():
                self.must_hold()
                if self.app.poll() is not None or self.mono() > end:
                    raise RuntimeError("the app didn't come back")
                self.sleep(0.2)
            self.say("serving")
        except _Fenced:
            raise
        except Exception as e:
            self.note(f"taking back failed: {e}")
            self.failure = ("retake_failed", f"{type(e).__name__}: {e}")
            if isinstance(e, ReplicaMismatch):
                self._stop_mismatched()
            return self.step_down(code=1, overlap=False, retake=False)
        self.role = "active"
        deadline = (self.term_at or self.mono()) + self.t["STOP_TIMEOUT_S"] - self.t["REDRAIN_S"]
        return self.active(deadline=deadline, retake=False)


class _Fenced(Exception):
    pass


class ReplicaMismatch(RuntimeError):
    """The replica and the local file disagree (or would): never served, never synced again."""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sizeup.handoff", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("SIZEUP_CONFIG", "/app/deploy/hub/config.hub.toml"))
    ap.add_argument("--litestream-config", default="/app/deploy/hub/litestream.yml")
    args = ap.parse_args(argv)
    state = Path(os.environ.get("SIZEUP_STATE_DIR", "/data"))
    run_dir = Path(os.environ.get("SIZEUP_RUN_DIR", "/tmp/sizeup-run"))
    shared = Path(os.environ.get("SIZEUP_SHARED_DIR", "/run/sizeup-shared"))
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LITESTREAM_SOCKET", str(run_dir / "litestream.sock"))
    lock = leaderlock.from_env(log=log)
    if lock is None:
        print("sizeup.handoff: LITESTREAM_BUCKET is not set", file=sys.stderr)
        return 2
    try:
        shared.mkdir(parents=True, exist_ok=True)
        (shared / ".w").write_text("")
        (shared / ".w").unlink()
    except OSError as e:
        print(f"sizeup.handoff: the shared folder {shared} isn't writable ({e}): the connector could never start",
              file=sys.stderr)
        return 2
    env = Env(args.config, state, args.litestream_config, os.environ["LITESTREAM_SOCKET"],
              int(os.environ.get("SIZEUP_PORT", "8090")))
    watch = None
    try:                                           # replica checks and failure pushes (sizeup/replicawatch.py)
        from sizeup import replicawatch
        watch = replicawatch.from_env(lock.holder, run_dir, state, args.litestream_config, args.config, log=log)
    except Exception as e:  # noqa: BLE001 - never in the way of the hub
        log(f"handoff: replica watch off ({type(e).__name__}: {e})")
    sup = Supervisor(lock, env, run_dir, shared, int(os.environ.get("SIZEUP_CONTROL_PORT", "8089")), watch=watch)
    signal.signal(signal.SIGTERM, sup.on_term)
    signal.signal(signal.SIGINT, sup.on_term)
    log(f"handoff: this task is {leaderlock.short(lock.holder)}; the lock is s3://{lock.obj.bucket}/{lock.obj.key}")
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
