"""The hub container's supervisor: hot/cold deploys behind the single-writer lock (GitHub #247, docs/cloud.md
"Deploys: the hot/cold handoff").

    python -m sizeup.handoff --config deploy/hub/config.hub.toml      (deploy/hub/entrypoint.sh, LITESTREAM_BUCKET set)

The ECS service runs a new task beside the old one (minimum healthy 100 %, maximum 200 %). Only the holder of
the S3 lock (sizeup/leaderlock.py) writes: the SQLite files, Litestream's replica, the cache copies, and only
its task runs the Cloudflare connector. Every task is one of:

  STARTING  the lock is read; followers (`litestream restore -f`) restore cad / hub / audio .sqlite into
            /data/standby/ and keep applying the leader's changes; the map caches come back from S3
            (sizeup/cachesync.py). The lock free (none yet, released, or expired): take it -> TAKEOVER.
  STANDBY   the lock is someone else's: nothing is written but /data/standby/ and the caches. No app, no
            `litestream replicate`, no connector (it waits for the `leader` file). /healthz on the control
            port (8089) answers 200 once every follower has restored and the caches are back ("warm"), so ECS
            stops the old task only when this one can take over in seconds.
  TAKEOVER  lock taken (generation + 1): the replica's last position is read (`restore -dry-run`), at least the
            positions the old leader released with; the followers catch up to it (only the last few changes:
            seconds), are stopped, quick-checked and moved into /data; `--init-state`; `litestream replicate`;
            the app; its /healthz; then the `leader` file (the connector starts) and phase "serving" in the lock.
  ACTIVE    the lock renewed every 10 s. Not renewed for 20 s, or written by someone else: FENCED (below).
  DRAINING  SIGTERM (ECS, after the new task is healthy): phase "draining"; the app stops taking requests (503 +
            Retry-After), finishes those in flight and exits; port 8090 answers 503 + Retry-After from here;
            `litestream sync -wait` puts every last change in S3 and gives its position; Litestream stops; the
            lock is released with those positions.
  RELEASED  the connector stays up (answering 503) until the new leader says "serving" or OVERLAP_S; if nobody
            took the lock within RETAKE_S it is taken back and this task serves on until shortly before ECS
            kills it, then drains again. Then the `leader` file goes (the connector stops) and this exits.
  FENCED    the lease isn't ours or fresh: the app and Litestream are SIGKILLed at once, the `leader` file goes,
            exit 3 (ECS replaces the task). The app's own watchdog (sizeup/fence.py) does the same from inside.

Never two writers: a task writes only between taking the lock and releasing it (or 20 s after its last
successful renew, 15 s before anyone may take it over). Release happens only after the app and Litestream have
exited.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from sizeup import leaderlock
from sizeup.leaderlock import LeaderLock, LockError, LockLost, txid

DBS = ("cad", "hub", "audio")
CHECKED = ("cad", "hub")       # quick-checked before they are promoted (audio: big, clips only)

POLL_S = 1.0                   # the lock, while standing by (0.25 s once the leader says "draining")
CATCHUP_S = 30.0               # a follower reaching the target position (else: a full restore)
RESTORE_S = 600.0              # a full restore, or a cold start's followers (no standby before us)
APP_READY_S = 120.0            # the app answering /healthz after it is started
CONNECTOR_READY_S = 30.0       # cloudflared connected to the edge (not fatal: "serving" is said anyway)
APP_STOP_S = 30.0              # the app, after SIGTERM, before SIGKILL
SYNC_S = 45.0                  # `litestream sync -wait` for the three files, in all
LITESTREAM_STOP_S = 40.0       # Litestream after SIGTERM (it syncs once more itself)
OVERLAP_S = 45.0               # the released connector, at most
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


# -- processes ---------------------------------------------------------------------------------------------

class Proc:
    """A child process in its own process group. If the supervisor dies, so does the container (it is the
    container's main process under the init the task definition turns on), and every child with it."""

    def __init__(self, name: str, argv: list[str], env: dict | None = None):
        self.name = name
        self.p = subprocess.Popen(argv, env=env, start_new_session=True)
        self.pid = self.p.pid

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

    def replicate(self) -> Proc:
        return Proc("litestream", ["litestream", "replicate", "-config", self.ls_config])

    def sync(self, db: str, timeout: float) -> int:
        """Every change of db in S3: the replica's position. RuntimeError if Litestream can't say so."""
        r = self._ls("sync", "-wait", "-json", "-timeout", str(max(1, int(timeout))), "-socket", self.socket,
                     str(self.db_path(db)), timeout=timeout + 5)
        if r.returncode != 0:
            raise RuntimeError(f"litestream sync {db}: {(r.stderr or r.stdout).strip()[-300:]}")
        d = last_json(r.stdout)
        local, replica = txid(d.get("txid")), txid(d.get("replica_txid"))
        if local is None or replica is None or replica < local:
            raise RuntimeError(f"litestream sync {db}: replica at {replica}, the database at {local}")
        return replica

    def init_state(self) -> None:
        r = subprocess.run([self.python, "-m", "sizeup", "--config", self.config, "--init-state"], timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"sizeup --init-state exited {r.returncode}")

    def start_app(self, env: dict) -> Proc:
        return Proc("app", [self.python, "-m", "sizeup", "--config", self.config], env=env)

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
        con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        try:
            return str(con.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            con.close()

    def warm_caches(self, deadline_s: float) -> None:
        from sizeup.cachesync import CacheMirror
        m = CacheMirror.from_env(self.state_dir, log=log)
        if m is not None:
            m.restore(deadline_s)
            m.save_manifest()


# -- the 503 answer while no app serves on this task ----------------------------------------------------------

UPDATING_JSON = json.dumps({"error": "the hub is updating: back in a few seconds", "updating": True}).encode()
UPDATING_HTML = (b"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3><title>Updating</title>"
                 b"<body style=\"font:16px system-ui;margin:3em;color:#333\"><p>Reconnecting&hellip; (update in progress)"
                 b"</p><p>This page reloads by itself.</p>")
RETRY_AFTER = "2"


class _Updating(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        pass

    def _answer(self) -> None:
        html = "text/html" in (self.headers.get("Accept") or "") and not self.path.startswith("/api/")
        body = UPDATING_HTML if html else UPDATING_JSON
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Retry-After", RETRY_AFTER)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _answer


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
                 timing: dict | None = None):
        self.lock, self.env = lock, env
        self.run_dir, self.shared = run_dir, shared_dir
        self.control_port = control_port
        self.mono, self.sleep = mono, sleep
        t = dict(POLL_S=POLL_S, CATCHUP_S=CATCHUP_S, RESTORE_S=RESTORE_S, APP_READY_S=APP_READY_S,
                 CONNECTOR_READY_S=CONNECTOR_READY_S, APP_STOP_S=APP_STOP_S, SYNC_S=SYNC_S,
                 LITESTREAM_STOP_S=LITESTREAM_STOP_S, OVERLAP_S=OVERLAP_S, RETAKE_S=RETAKE_S,
                 STOP_TIMEOUT_S=STOP_TIMEOUT_S, REDRAIN_S=REDRAIN_S, RENEW_S=leaderlock.RENEW_S,
                 RETAKE_MIN_LEFT_S=RETAKE_MIN_LEFT_S, CREATED_WAIT_S=CREATED_WAIT_S)
        t.update(timing or {})
        self.t = t
        self.role = "starting"
        self.detail = ""
        self.term = threading.Event()
        self.term_at: float | None = None
        self.lost = threading.Event()
        self.renewing = threading.Event()
        self._renew_now = threading.Event()
        self._renew_token: object | None = None
        self.followers: dict[str, Proc] = {}
        self.follow_done: set[str] = set()        # restored (or: no replica to restore)
        self.no_replica: set[str] = set()
        self.caches_warm = threading.Event()
        self.app: Proc | None = None
        self.litestream: Proc | None = None
        self.updating_srv: ThreadingHTTPServer | None = None
        self.control_srv: ThreadingHTTPServer | None = None
        self.events: list[str] = []               # what happened, in order (the tests read it)
        self.positions: dict | None = None
        self.released_positions: dict | None = None
        self.app_env: dict = dict(os.environ)

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

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)

    def write_lease(self) -> None:
        self._write(self.lease_file, json.dumps({
            "generation": self.lock.generation, "valid_until": self.lock.valid_until, "holder": self.lock.holder,
            "litestream_pid": self.litestream.pid if self.litestream is not None else None}))

    def heartbeat(self) -> None:
        self._write(self.shared / "hub-alive", f"{int(time.time())}\n")

    def warm(self) -> bool:
        return self.caches_warm.is_set() and self.follow_done >= set(DBS)

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
                "warm": self.warm(), "serving": bool(app and app.get("serving", True)),
                "loading": list((app or {}).get("loading") or []),
                "generation": self.lock.generation, "holder": leaderlock.short(self.lock.holder),
                "leader": leaderlock.short((self.lock.last_seen or {}).get("holder", "")),
                "leader_phase": (self.lock.last_seen or {}).get("phase")}

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

    def stop_followers(self) -> None:
        for p in self.followers.values():
            p.stop(15.0)
        self.followers.clear()

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
        self.write_lease()
        self._renew_token = token = object()
        self.renewing.set()
        threading.Thread(target=self._renew_loop, args=(token,), name="lock-renew", daemon=True).start()

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
        for p in (self.app, self.litestream, *self.followers.values()):
            if p is not None:
                p.kill()
        self.stop_renewing()
        self.lock.drop()
        self.leader_file.unlink(missing_ok=True)
        self.lease_file.unlink(missing_ok=True)
        return EXIT_FENCED

    # -- main ----------------------------------------------------------------------------------------------

    def on_term(self, *_a) -> None:
        if not self.term.is_set():
            self.term_at = self.mono()
            log("handoff: SIGTERM")
        self.term.set()

    def run(self) -> int:
        self.leader_file.unlink(missing_ok=True)       # never left over from anything
        self.control()
        threading.Thread(target=self._beat, name="heartbeat", daemon=True).start()
        try:
            return self._run()
        except _Fenced as e:
            return self.fence(str(e))
        finally:
            if self.updating_srv is not None:
                self.updating_srv.shutdown()

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
            if self.term.is_set():
                self.note(f"stopped while {self.role}: the lock untouched")
                self.stop_followers()
                return 0
            try:
                prev = self.lock.try_take()
                errors_since = None
            except LockError as e:
                errors_since = errors_since or self.mono()
                log(f"handoff: lock not readable ({e})")
                if self.mono() - errors_since > self.t["RESTORE_S"]:
                    self.note("the lock unreadable too long: exiting")
                    self.stop_followers()
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
        self.start_renewing()
        positions = prev.get("positions") if isinstance(prev.get("positions"), dict) else {}
        self.note(f"takeover: generation {self.lock.generation}"
                  + (f", the old leader released at {positions}" if positions else ""))
        try:
            self._promote(prev, positions)
            self.must_hold()
            self._start_writer()
        except _Fenced:
            raise
        except Exception as e:
            self.note(f"takeover failed: {type(e).__name__}: {e}")
            return self.step_down(code=1, overlap=False, retake=False)
        return self.active()

    def _target(self, prev: dict, positions: dict) -> dict[str, int | None]:
        """Where each file must be: the replica's last position (we hold the lock: it is frozen now), and never
        behind what the old leader released with."""
        out: dict[str, int | None] = {}
        for db in DBS:
            end = self.mono() + 60.0
            while True:
                try:
                    have = self.env.plan(db)
                    break
                except Exception as e:
                    if self.mono() > end:
                        raise RuntimeError(f"the replica of {db} can't be read: {e}") from None
                    self.must_hold()
                    self.sleep(2.0)
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
        target = self._target(prev, positions)
        self.detail = f"catching up to {target}"
        for db, want in target.items():
            if want is None:
                continue
            bound = self.t["CATCHUP_S"] if db in self.follow_done else self.t["RESTORE_S"]
            end = self.mono() + bound
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
        self.stop_followers()
        # We hold the lock: nobody may move the replica now. If it moved, something else writes to it (a hub
        # from before the lock, still running beside us): never promote.
        if prev.get("_created"):
            self.sleep(self.t["CREATED_WAIT_S"])   # longer than Litestream's 1 s sync: an old writer would show
        again = {db: self.env.plan(db) for db in DBS}
        moved = {db: (target[db], again[db]) for db in DBS if again[db] != target[db]}
        if moved:
            raise RuntimeError(f"the replica moved while this task held the lock (another writer?): {moved}")
        for db, want in target.items():
            if want is None:
                continue
            got = self.env.applied(self.staged(db))
            if got != want:
                raise RuntimeError(f"{db}: at {got} after the followers stopped, not {want}")
            if db in CHECKED:
                res = self.env.quick_check(self.staged(db))
                if res != "ok":
                    raise RuntimeError(f"{db}.sqlite quick_check: {res[:200]}")
        self.must_hold()
        for db, want in target.items():
            dest = self.env.db_path(db)
            for p in (dest, Path(f"{dest}-wal"), Path(f"{dest}-shm")):
                p.unlink(missing_ok=True)
            meta = dest.with_name(f".{dest.name}-litestream")
            if meta.exists():
                import shutil
                shutil.rmtree(meta)
            if want is not None:
                os.replace(self.staged(db), dest)
            Path(f"{self.staged(db)}-txid").unlink(missing_ok=True)
        self.positions = {db: (f"{n:016x}" if n is not None else None) for db, n in target.items()}
        self.note(f"promoted: {self.positions}")

    def _start_writer(self) -> None:
        """--init-state, Litestream, the app, its health, the leader file, "serving"."""
        self.must_hold()
        self.env.init_state()
        self.must_hold()
        self.litestream = self.env.replicate()
        self.write_lease()
        self.app_env = dict(os.environ, SIZEUP_LEASE_FILE=str(self.lease_file))
        self.must_hold()
        self.app = self.env.start_app(self.app_env)
        self.note("app started")
        end = self.mono() + self.t["APP_READY_S"]
        while not self.env.app_healthy():
            self.must_hold()
            if self.app.poll() is not None:
                raise RuntimeError(f"the app exited {self.app.poll()} while starting")
            if self.mono() > end:
                raise RuntimeError(f"the app didn't answer /healthz in {self.t['APP_READY_S']:.0f} s")
            self.sleep(0.2)
        self.must_hold()
        self._write(self.leader_file, f"{self.lock.generation}\n")
        self.note("leader file written: the connector starts")
        end = self.mono() + self.t["CONNECTOR_READY_S"]
        while not self.env.connector_ready() and self.mono() < end:
            self.must_hold()
            self.sleep(0.2)
        self.say("serving")
        self.role = "active"
        self.detail = ""
        self.note(f"active: serving, generation {self.lock.generation}")

    # -- active --------------------------------------------------------------------------------------------

    def active(self, deadline: float | None = None, retake: bool = True) -> int:
        backoff = LITESTREAM_BACKOFF[0]
        restart_at = None
        while True:
            self.must_hold()
            if self.term.is_set() and deadline is None:
                return self.step_down(code=0, overlap=True, retake=retake)
            if deadline is not None and self.mono() >= deadline:
                self.note("taken back: draining again before ECS stops this task")
                return self.step_down(code=0, overlap=True, retake=False)
            if self.app is not None and self.app.poll() is not None:
                self.note(f"the app exited {self.app.poll()}: handing over")
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
        self.note("draining: the app finishes what's in flight")
        self.say("draining")
        if self.app is not None:
            self.app.stop(self.t["APP_STOP_S"])
            self.app = None
        self.must_hold()
        if self.updating_srv is None:
            self.updating_srv = serve(self.env.port, _Updating)
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
            self.must_hold()
            self.litestream.stop(self.t["LITESTREAM_STOP_S"])
            self.litestream = None
        self.must_hold()
        self.stop_renewing()
        try:
            released = self.lock.release(positions if clean else None, clean)
        except LockLost as e:
            raise _Fenced(f"lost the lock before releasing it: {e}") from None
        except LockError as e:
            self.note(f"release not confirmed ({e}): the lease runs out by itself in {leaderlock.TTL_S:.0f} s")
            released = ""
        self.lease_file.unlink(missing_ok=True)
        self.released_positions = positions if clean else None
        self.role = "released"
        self.note(f"released: {positions if clean else 'unclean'}")
        if overlap:
            res = self.overlap(released, retake)
            if res is not None:
                return res
        self.leader_file.unlink(missing_ok=True)
        self.note("leader file removed: the connector stops")
        self.role = "done"
        return code

    def overlap(self, released_etag: str, retake: bool) -> int | None:
        """The connector stays up (503s) until the new leader serves; or, nobody taking it, take it back."""
        start = self.mono()
        while self.mono() - start < self.t["OVERLAP_S"]:
            try:
                doc, etag = self.lock.read()
            except LockError:
                doc, etag = None, ""
            if doc and doc.get("holder") != self.lock.holder and doc.get("state") == "held":
                if doc.get("phase") == "serving":
                    self.note(f"the new leader {leaderlock.short(doc.get('holder', ''))} serves")
                    return None
            elif (retake and self.released_positions and leaderlock.is_free(doc, self.lock.wall(), self.lock.grace)
                  and self.mono() - start >= self.t["RETAKE_S"]
                  and self._time_left() > self.t["REDRAIN_S"] + self.t["RETAKE_MIN_LEFT_S"]):
                res = self._retake(released_etag, etag)
                if res is not None:
                    return res
            self.sleep(0.25)
        self.note(f"no new leader serving after {self.t['OVERLAP_S']:.0f} s")
        return None

    def _time_left(self) -> float:
        if self.term_at is None:
            return float("inf")
        return self.term_at + self.t["STOP_TIMEOUT_S"] - self.mono()

    def _retake(self, released_etag: str, etag: str) -> int | None:
        """Take the lock back and serve on from our own files, which are exactly what we released. Only when the
        replica is still where we left it: our own release untouched, or a new task that took it and gave it back
        without writing anything (its Litestream never ran). Anything else (it wrote, then failed) means our files
        are behind: never resumed from (the next task restores)."""
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
                if here != ours:
                    self.note(f"the replica moved on since our release ({ours} -> {here}): not taking it back")
                    self.lock.release(prev.get("positions") if isinstance(prev.get("positions"), dict) else None,
                                      bool(prev.get("clean")))
                    return None
        except LockLost:
            return None
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
            self.litestream = self.env.replicate()
            self.write_lease()
            self.app_env = dict(os.environ, SIZEUP_LEASE_FILE=str(self.lease_file))
            if self.updating_srv is not None:
                self.updating_srv.shutdown()
                self.updating_srv.server_close()
                self.updating_srv = None
            self.app = self.env.start_app(self.app_env)
            end = self.mono() + self.t["APP_READY_S"]
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
            return self.step_down(code=1, overlap=False, retake=False)
        self.role = "active"
        deadline = (self.term_at or self.mono()) + self.t["STOP_TIMEOUT_S"] - self.t["REDRAIN_S"]
        return self.active(deadline=deadline, retake=False)


class _Fenced(Exception):
    pass


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
    sup = Supervisor(lock, env, run_dir, shared, int(os.environ.get("SIZEUP_CONTROL_PORT", "8089")))
    signal.signal(signal.SIGTERM, sup.on_term)
    signal.signal(signal.SIGINT, sup.on_term)
    log(f"handoff: this task is {leaderlock.short(lock.holder)}; the lock is s3://{lock.obj.bucket}/{lock.obj.key}")
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
