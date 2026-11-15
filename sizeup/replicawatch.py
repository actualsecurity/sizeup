"""The hub's replicas, verified all the time, and a task that fails, said at once (2026-11-13: a corrupt cad.sqlite
replica sat unnoticed for ~110 min until the next deploy's takeover failed its quick_check; then every new task
crash-looped for ~35 min until someone opened a browser). Run by the supervisor (sizeup/handoff.py), stdlib only.

    the standby  (role standby) STANDBY_FIRST_S (3 min) after its followers have restored, then every
                 SIZEUP_REPLICA_CHECK_MIN (10): a consistent copy of each file its followers keep (/data/standby/,
                 copied while its -txid sidecar, size and mtime hold still: the follow the takeover needs is never
                 paused), checked. A copy or check in flight is stopped the moment the role changes (a takeover).
    the leader   (role active) its own low-rate followers (`litestream restore -f -follow-interval
                 SIZEUP_REPLICA_FOLLOW_S` (60 s), at nice 19) of all three into /data/.replica-check: the first restore
                 once, then only the new changes. LEADER_FIRST_S (2 min) after it serves, then every
                 SIZEUP_REPLICA_LEADER_CHECK_MIN (15), half a follow interval off its polls: each follower stopped,
                 its file checked, the follower started again (it resumes from its -txid). This is what a takeover
                 depends on: a follower's catch-up.
                 A follower that exits with an error (an LTX checksum mismatch, ...) is "replica restore failed
                 (possible corruption)" at once; one with no file (-if-replica-exists) means no backup: not verifiable.
    a drift      the check copy failing is not yet the replica failing (2026-11-15): a follower stopped while it
                 applies an LTX file (SIGTERM cancels its S3 read part way) leaves pages of two positions in its copy,
                 and so does a round that failed part way and is replayed. So a copy written after its -txid
                 (stopped mid-apply), or failing its check, is checked again from a fresh `litestream restore` from S3
                 (CONFIRM): fresh ok, the copy drifted: "re-seeded" (the fresh copy becomes the follower's, its count
                 in the record), no push; fresh bad, "Replica corrupt" (the fresh restore failed too); the restore
                 failing, "restore failed (possible corruption)". Not a drift, though a fresh restore passes: a
                 follower that logged apply errors (an LTX file it can't apply), or the standby's copy (the takeover's
                 own) failing: "Replica follower failing". Each follower is stopped half a poll past its own last
                 poll; the first round after a deploy only looks again at a copy stopped mid-apply (no full restore
                 in the deploy's minutes). A takeover never uses the leader's copies.
    the check    read-only (not immutable) in a child process at nice 19, killed when the role changes or the task
                 stops: quick_check, integrity_check once each SIZEUP_REPLICA_FULL_H (24) hours per file.

The results go to s3://<bucket>/<prefix>/replica-watch.json (conditional writes, like the lock): each file's last check,
the files failing, the last task events, push times, and the leader serving now (its task, build, since when; the
deploy's check reads that: tools/deploy_check.py replicas). The leader mirrors it to $SIZEUP_RUN_DIR/replica-watch.json
for the app (Settings > Station > Health: the Replicas row and a "Replica corrupt" alert, sizeup/cloud/health.py).

Pushes to the STATUS topic, each kind with its own slot (at most one per SIZEUP_REPLICA_PUSH_MIN, 30; a push's time is
recorded in S3 only once ntfy took it, one retry):

    replica_corrupt        "Replica corrupt: cad.sqlite"                          5   the check said so (the leader: a
                                                                                      fresh restore from S3 too)
    replica_restore        "Replica restore failed (possible corruption): ..."    5   a follower failed, at once
    replica_unverifiable   "Replica not verifiable: ..."                          4   ERRORS_ALERT rounds can't check
    replica_follow         "Replica follower failing: ..."                        4   a follower logged apply errors (or
                                                                                      the standby's copy failed) while a
                                                                                      fresh restore passes, at once
    replica_drift          "Replica check copy keeps drifting: ..."               4   re-seeded DRIFT_STREAK rounds
                                                                                      running, or > DRIFT_DAY_N in 24 h
    replica_ok             "Replica OK again: ..."                                3   (no limit) after one of those
    task                   "Hub takeover failed", ...                             4   a task that ends badly
    task_loop              "Hub crash-looping: N failed tasks in 10 min"          5   LOOP_N in LOOP_WINDOW_S
    serving                "Hub serving again"                                    3   (no limit) after task pushes
    litestream             "Hub Litestream restarted"                             4   it exited under the serving app
    replica_suspect        "Replica not verifiable: cad.sqlite"                   4   ... and was started again without
                                                                                      a TRUNCATE checkpoint of the file

Litestream started again under the serving app (Supervisor._restart_litestream -> litestream_restarted()): always an
event in the record (Health's Replicas row) and a push. With a TRUNCATE checkpoint of every file first it snapshots
each, and that is all. A file it couldn't be done for is SUSPECT ("suspect" in the record: what the app wrote while
Litestream was down may be missing from the replica, and no check can tell: a stale page passes quick_check), shown as
"unverified" whatever its last check said, until this task managed the checkpoint after all ("Replica OK again") or
another task serves (then it is history: an event).

A task that ends badly (Supervisor.run(): a takeover that failed, the app exiting, fenced, an unclean release, the
supervisor crashing) records an event in the S3 object and pushes before it exits, all within REPORT_S (20 s, DNS
included: on a thread of its own, joined with that deadline). S3 unreadable: pushed anyway. Every bad check also logs
"replica check FAILED" (a CloudWatch metric filter, sizeup-aws step 28: the backstop).

The topic is the env's (SSM /sizeup-hub/status-topic, else the alerts topic: notify.route()), else the config file's
[alerts]: a Settings override lives in the CAD database, which the supervisor never opens. So it also can't see the
topics protected in Settings (Settings' pre-alert topics and its protected list): it refuses only the config's and
env's [prealert] ones (notify.topic_problem); its topic comes from the env / config, never from Settings. The static
backstop (notify.always_protected(): a few names by hash) needs neither: status_topic() refuses such a topic and says
so, the Pusher refuses it without a retry, and notify.publish() would refuse it again. Never a topic's name in a log line.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup import leaderlock, notify
from sizeup.leaderlock import LockError, S3Object

DBS = ("cad", "hub", "audio")
KEY = "replica-watch.json"
STATE_FILE = "replica-watch.json"        # in the run dir: what the app's Settings > Station > Health reads

INTERVAL_S = 600.0          # the standby: a check of each followed file this often
STANDBY_FIRST_S = 180.0     # ... the first this long after its followers restored (never in the deploy's busy minute)
LEADER_INTERVAL_S = 900.0   # the leader: its followers' files checked this often
LEADER_FIRST_S = 120.0      # ... the first this long after it serves (a bad start shows in minutes)
LEADER_RETRY_S = 60.0       # ... a follower not restored yet: looked at again this soon
RESTORE_S = 600.0           # ... and not restored after this long: not verifiable
FOLLOW_S = 60.0             # the leader's followers poll S3 this often
FULL_S = 86400.0            # integrity_check once a day per file, else quick_check
PUSH_EVERY_S = 1800.0       # at most one push per kind this often
REFRESH_S = 60.0            # the leader mirrors the S3 object to the app this often
CHECK_TIMEOUT_S = 900.0     # one check (a child process)
CONFIRM_TIMEOUT_S = 900.0   # a fresh restore that confirms a failing copy (a child process)
SNAPSHOT_TRIES = 5          # copies of a followed file before giving up for this round (it kept moving)
ERRORS_ALERT = 2            # rounds in a row a file couldn't be checked: "not verifiable"
DRIFT_STREAK = 2            # leader rounds in a row a file's check copy was re-seeded: pushed ("keeps drifting")
DRIFT_DAY_N = 4             # ... or more re-seeds than this in 24 h
FOLLOWER_BACKOFF = (60.0, 900.0)
EVENTS_MAX = 30
LOOP_WINDOW_S = 600.0       # task failures within this long ...
LOOP_N = 3                  # ... this many: a crash loop
NOT_FAILURES = ("unclean_release", "litestream_restarted", "replica_suspect")   # events that are no failed task
SERVING_AGAIN_S = 6 * 3600.0   # "serving again" only for failures pushed this recently
PUSH_TIMEOUT_S = 5.0
REPORT_S = 20.0             # a failing task's record and push, in all, before it exits

TITLES = {"takeover_failed": "Hub takeover failed", "app_exited": "Hub app exited", "fenced": "Hub task fenced",
          "lock_unreadable": "Hub lock unreadable", "unclean_release": "Hub released unclean",
          "crashed": "Hub supervisor crashed", "retake_failed": "Hub retake failed", "exited": "Hub task failed"}


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _hhmm(t: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(t)) if t else "?"


# -- one check -----------------------------------------------------------------------------------------------------

def integrity(path: str | Path, full: bool = False) -> dict:
    """{"ok": True | False | None, "result": text}: quick_check (full: integrity_check) of the file, opened read-only
    (not immutable). ok None: it couldn't be opened or read (not verifiable, not proof of corruption)."""
    import sqlite3
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            rows = con.execute(f"PRAGMA {'integrity_check' if full else 'quick_check'}(20)").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        name = str(getattr(e, "sqlite_errorname", "") or "")
        corrupt = name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")) or (
            not name and any(w in str(e) for w in ("malformed", "not a database")))
        return {"ok": False if corrupt else None, "result": f"{type(e).__name__}: {e}"[:400]}
    except (OSError, ValueError) as e:
        return {"ok": None, "result": f"{type(e).__name__}: {e}"[:400]}
    text = "\n".join(str(r[0]) for r in rows)
    return {"ok": text == "ok", "result": text[:400]}


def _nice() -> list[str]:
    n = shutil.which("nice")
    return [n, "-n", "19"] if n else []


CANCELLED = {"ok": None, "result": "cancelled", "cancelled": True}


class Cancelled(Exception):
    """The role changed while a check or a confirming restore ran: nothing is recorded."""


def run_check(path: Path, full: bool, cancel: Callable[[], bool] = lambda: False,
              timeout: float = CHECK_TIMEOUT_S) -> dict:
    """integrity() in a child process at nice 19 (the hub's half vCPU stays the app's; a corrupt file can't take the
    supervisor down), killed the moment cancel() says so (CANCELLED) or after timeout."""
    argv = [*_nice(), sys.executable, "-m", "sizeup.replicawatch", "check", *(["--full"] if full else []), str(path)]
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        return {"ok": None, "result": f"{type(e).__name__}: {e}"}
    end = time.monotonic() + timeout
    while p.poll() is None:
        if cancel() or time.monotonic() > end:
            _kill(p)
            return dict(CANCELLED) if cancel() else {"ok": None, "result": f"the check took over {timeout:.0f} s"}
        try:
            p.wait(0.25)
        except subprocess.TimeoutExpired:
            pass
    out, err = p.communicate()
    try:
        d = json.loads(out.strip().splitlines()[-1])
        if isinstance(d, dict) and "ok" in d:
            return {"ok": d["ok"], "result": str(d.get("result") or "")[:400]}
    except (ValueError, IndexError):
        pass
    return {"ok": None, "result": f"the check exited {p.returncode}: {(err or out).strip()[-200:]}"}


def _group(p) -> int | None:
    """The child's own process group (start_new_session), never ours: pid 0 / 1 / unknown is no group to signal."""
    pid = getattr(p, "pid", None)
    return pid if isinstance(pid, int) and pid > 1 else None


def _kill(p) -> None:
    """A child and its process group, now."""
    try:
        if _group(p) is None:
            raise OSError("no group of its own")
        os.killpg(_group(p), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except OSError:
            pass
    try:
        p.wait(5)
    except Exception:  # noqa: BLE001
        pass


def clean(out: Path, txid: bool = True) -> None:
    for p in (out, Path(f"{out}-wal"), Path(f"{out}-shm"), Path(f"{out}.tmp"), *([Path(f"{out}-txid")] if txid else [])):
        p.unlink(missing_ok=True)


def settled(out: Path) -> bool:
    """A stopped follower's copy is one position of the replica: nothing written to it after its -txid sidecar.
    Litestream (0.5.18 follow mode) writes a round's pages, syncs, and only then the sidecar; a round cut short (a
    SIGTERM cancels its S3 read part way through a file; a file gone between its listing and its read) leaves pages
    written after the sidecar, of two positions."""
    try:
        return out.stat().st_mtime_ns <= Path(f"{out}-txid").stat().st_mtime_ns
    except OSError:
        return False


def _stat(p: Path) -> tuple | None:
    try:
        s = p.stat()
        return s.st_size, s.st_mtime_ns
    except OSError:
        return None


def _copy(src: Path, dst: Path, abort: Callable[[], bool]) -> None:
    """src to dst in 8 MB pieces, given up the moment abort() says so (a takeover starting: its disk is the takeover's)."""
    with open(src, "rb") as f, open(dst, "wb") as g:
        while True:
            if abort():
                raise RuntimeError("stopped: this task is taking over")
            b = f.read(8 << 20)
            if not b:
                return
            g.write(b)


def snapshot(src: Path, dst: Path, position: Callable[[], int | None], tries: int = SNAPSHOT_TRIES,
             sleep: Callable[[float], None] = time.sleep, abort: Callable[[], bool] = lambda: False) -> int | None:
    """A consistent copy of a followed file (and its -wal, if any) without pausing its follower: copied while its
    position (the -txid sidecar), size and mtime hold still across the copy. Its position; RuntimeError when it kept
    moving."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    wal = Path(f"{src}-wal")
    for i in range(tries):
        clean(dst)
        before = (position(), _stat(src), _stat(wal))
        if before[1] is None:
            raise RuntimeError(f"{src.name}: not there")
        _copy(src, dst, abort)
        if before[2] is not None:
            try:
                _copy(wal, Path(f"{dst}-wal"), abort)
            except FileNotFoundError:
                pass
        if (position(), _stat(src), _stat(wal)) == before:
            return before[0]
        sleep(0.5 * (i + 1))
    clean(dst)
    raise RuntimeError(f"{src.name}: kept changing during {tries} copies")


def _tail(path: Path, n: int = 300) -> str:
    try:
        lines = [x for x in path.read_text(errors="replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return " | ".join(lines[-3:])[-n:]


# -- the shared record in S3 ----------------------------------------------------------------------------------------

class WatchStore:
    """The S3 object every task shares (conditional writes: read, change, put If-Match; again on a 412). obj None
    (no bucket: local, or tests): the same in memory."""

    def __init__(self, obj: S3Object | None):
        self.obj = obj
        self.mem: dict = {}
        self.last: dict = {}          # as last read or written

    def read(self) -> dict:
        if self.obj is None:
            self.last = json.loads(json.dumps(self.mem))
            return self.last
        doc, _ = self.obj.get()
        self.last = doc or {}
        return self.last

    def update(self, fn: Callable[[dict], bool | None]) -> dict:
        """fn changes the doc in place (False: nothing to write). The doc as it is now. LockError: S3 couldn't say."""
        for _ in range(5):
            if self.obj is None:
                d = json.loads(json.dumps(self.mem))
                if fn(d) is not False:
                    self.mem = d
                self.last = d
                return d
            doc, etag = self.obj.get()
            d = json.loads(json.dumps(doc or {}))
            if fn(d) is False:
                self.last = d
                return d
            d["v"] = 1
            tag = self.obj.put(d, if_match=etag) if doc is not None else self.obj.put(d, if_none_match=True)
            if tag is not None:
                self.last = d
                return d
        raise LockError(f"{KEY}: written by others five times in a row")


# -- pushes ---------------------------------------------------------------------------------------------------------

def status_topic(config: str = "", env: dict | None = None,
                 log: Callable[[str], None] | None = None) -> tuple[str, str, tuple[str, ...]]:
    """(the ntfy server, the STATUS topic in effect or "", topics never to publish to) from the env (SSM) over the
    config file's [alerts]; [prealert]'s topics refused like Tapout's, and the static backstop
    (notify.always_protected()) always: said in the log (never the name)."""
    env = os.environ if env is None else env
    al, pre = {}, {}
    if config:
        try:
            import tomllib
            with open(config, "rb") as f:
                data = tomllib.load(f)
            al = data.get("alerts") if isinstance(data.get("alerts"), dict) else {}
            pre = data.get("prealert") if isinstance(data.get("prealert"), dict) else {}
        except (OSError, ValueError):
            pass
    forbid = tuple(x for x in (env.get("SIZEUP_PREALERT_RING_TOPIC") or str(pre.get("ring_topic") or ""),
                               env.get("SIZEUP_PREALERT_STATUS_TOPIC") or str(pre.get("status_topic") or "")) if x.strip())
    alert = (env.get("SIZEUP_ALERT_TOPIC") or str(al.get("ntfy_topic") or "")).strip()
    status = (env.get("SIZEUP_STATUS_TOPIC") or str(al.get("status_topic") or "")).strip()
    server = (env.get("SIZEUP_ALERT_SERVER") or str(al.get("ntfy_server") or "") or notify.DEFAULT_SERVER).strip()
    for what, t in (("alerts", alert), ("status", status)):
        if t and log is not None and (notify.always_protected(t) or notify.topic_problem(t, forbid) == notify.PROTECTED_WHY):
            log(f"replica-watch: the {what} topic is a protected topic (one sizeup only subscribes to): REFUSED, "
                "never published to; set a topic of our own")
    topic = notify.route(alert if not notify.topic_problem(alert, forbid) else "",
                         status if not notify.topic_problem(status, forbid) else "")[notify.STATUS]
    return server.rstrip("/"), topic, forbid


class Pusher:
    """A push to the STATUS topic now (synchronously: the task may be about to exit), at most one per kind per every_s
    across every task. The time is recorded in the store only after ntfy took it (one retry)."""

    def __init__(self, store: WatchStore, server: str, topic: str, forbid: tuple[str, ...] = (),
                 every_s: float = PUSH_EVERY_S, wall: Callable[[], float] = time.time,
                 log: Callable[[str], None] = log, publish: Callable = notify.publish, timeout: float = PUSH_TIMEOUT_S,
                 sleep: Callable[[float], None] = time.sleep):
        self.store, self.server, self.topic, self.forbid = store, server, topic, forbid
        self.every_s, self.wall, self.log, self.publish, self.timeout = every_s, wall, log, publish, timeout
        self.sleep = sleep
        self.sent: list[tuple[str, str]] = []          # (kind, title), this task's (the tests read it)

    @property
    def on(self) -> bool:
        return bool(self.topic) and not self.refused

    @property
    def refused(self) -> bool:
        """The topic is one never published to: the static backstop (notify.always_protected()) or a subscribed one
        (or not a topic name at all)."""
        return bool(self.topic) and (notify.always_protected(self.topic)
                                     or bool(notify.topic_problem(self.topic, self.forbid)))

    def push(self, kind: str, title: str, body: str, priority: int = 4, tags: tuple = ("rotating_light", "sizeup"),
             limit: bool = True, known: dict | None = None) -> bool:
        """known: the record as just read or written (no second read on a failing task's way out)."""
        now = self.wall()
        if limit:
            try:
                d = known if known is not None else self.store.read()
            except Exception as e:  # noqa: BLE001 - S3 unreadable: pushed anyway (rare, and the push is the point)
                self.log(f"replica-watch: can't read the {kind} push's last time ({type(e).__name__}): pushing anyway")
                d = {}
            last = (d.get("pushed") or {}).get(kind)
            if isinstance(last, (int, float)) and now - float(last) < self.every_s:
                self.log(f"replica-watch: {title}: not pushed (one {kind} push per {self.every_s / 60:g} min)")
                return False
        if self.refused:        # no attempt, no retry: publish() would refuse it as well
            self.log(f"replica-watch: {title}: the status topic is refused (protected, or not a topic of ours): not pushed")
            return False
        if not self.on:
            self.log(f"replica-watch: {title}: no status topic: not pushed")
            return False
        for attempt in range(2):
            try:
                self.publish(self.server, self.topic, title, body, priority=priority, tags=tags, timeout=self.timeout)
                break
            except notify.NotifyError as e:
                self.log(f"replica-watch: push not sent ({e}){': again in 1 s' if not attempt else ''}: {title}")
                if attempt:
                    return False
                self.sleep(1.0)
        self.sent.append((kind, title))
        self.log(f"replica-watch: pushed to the status topic: {title}")

        def mark(d: dict) -> None:
            d.setdefault("pushed", {})[kind] = now
        try:
            self.store.update(mark)
        except Exception as e:  # noqa: BLE001
            self.log(f"replica-watch: the {kind} push's time not recorded ({type(e).__name__}: {e})")
        return True


# -- the watcher -------------------------------------------------------------------------------------------------------

class ReplicaWatch:
    """The supervisor's (sizeup/handoff.py Supervisor.watch): start(sup) runs the checks on a thread of their own;
    report() before the task exits."""

    def __init__(self, store: WatchStore, pusher: Pusher, holder: str, run_dir: Path, work_dir: Path, ls_config: str = "",
                 build: str = "", interval_s: float = INTERVAL_S, standby_first_s: float = STANDBY_FIRST_S,
                 leader_interval_s: float = LEADER_INTERVAL_S, leader_first_s: float = LEADER_FIRST_S,
                 follow_s: float = FOLLOW_S, full_s: float = FULL_S,
                 check: Callable[..., dict] = run_check, follower: Callable | None = None,
                 restorer: Callable | None = None,
                 wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = log, poll_s: float = 1.0):
        self.store, self.pusher, self.holder, self.build = store, pusher, holder, build
        self.run_dir, self.work = run_dir, work_dir
        self.ls_config = ls_config
        self.interval_s, self.standby_first_s = interval_s, standby_first_s
        self.leader_interval_s, self.leader_first_s, self.follow_s, self.full_s = leader_interval_s, leader_first_s, follow_s, full_s
        self.check = check
        self.follower = follower or self._litestream_follower
        self.restorer = restorer or self._litestream_restore
        self.wall, self.mono, self.log, self.poll_s = wall, mono, log, poll_s
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.reported = False
        self.last_full: dict[str, float] = {}
        self.followers: dict[str, object] = {}            # the leader's own (db -> a Popen-like)
        self.fstate: dict[str, dict] = {}                 # db -> {fails, restart_at, missing, started}
        self.checked: set[str] = set()                    # files this leader's followers have had a check of
        self.serving_at: float | None = None              # monotonic: this task serves since
        self.to_start: list[str] = []                     # the leader's followers not started yet (one at a time)
        self.start_at = 0.0                               # ... from then (monotonic)

    # -- the loop ------------------------------------------------------------------------------------------------

    def start(self, sup) -> threading.Thread:
        self.thread = threading.Thread(target=self._loop, args=(sup,), name="replica-watch", daemon=True)
        self.thread.start()
        return self.thread

    def _loop(self, sup) -> None:
        nxt_standby = nxt_leader = None
        nxt_refresh = 0.0
        prev = None
        try:
            while not self.stop.is_set() and sup.role not in ("done", "fenced"):
                try:
                    now = self.mono()
                    if sup.role == "standby" and self.interval_s > 0:
                        if nxt_standby is None and sup.follow_done >= set(DBS):
                            nxt_standby = now + self.standby_first_s       # after the followers settle
                        if nxt_standby is not None and now >= nxt_standby:
                            self.standby_round(sup)
                            nxt_standby = self.mono() + self.interval_s
                    elif sup.role == "active":
                        if prev != "active":                       # serving: first, or again after a retake
                            self.serving()
                            self.serving_at = now
                            if self.leader_interval_s > 0:
                                self.start_followers(sup, at=now + self.leader_first_s)
                            nxt_leader = now + self.leader_first_s
                        if self.leader_interval_s > 0:
                            self.tend_followers(sup)
                            if nxt_leader is not None and now >= nxt_leader:
                                pending = self.leader_round(sup)
                                nxt_leader = self.mono() + (LEADER_RETRY_S if pending else self.leader_interval_s)
                        if now >= nxt_refresh:
                            self.refresh()
                            nxt_refresh = self.mono() + REFRESH_S
                    elif self.followers:
                        self.stop_followers()                      # draining: nothing of ours in its way
                except Exception as e:  # noqa: BLE001 - the watcher never takes the supervisor down
                    self.log(f"replica-watch: {type(e).__name__}: {e}")
                prev = sup.role
                self.stop.wait(self.poll_s)
        finally:
            self.stop_followers()

    def _full(self, db: str) -> bool:
        now = self.wall()
        last = self.last_full.setdefault(db, now)          # never the first round: a quick one first
        if now - last >= self.full_s:
            self.last_full[db] = now
            return True
        return False

    def _checked(self, path: Path, full: bool, cancel: Callable[[], bool]) -> dict:
        try:
            return self.check(path, full, cancel)
        except Exception as e:  # noqa: BLE001
            return {"ok": None, "result": f"{type(e).__name__}: {e}"[:300]}

    def phase_delay(self, db: str, now: float) -> float:
        """How long to wait before stopping db's follower so that it is half a follow interval past a poll (it applies
        what it finds right after one; 2026-11-15: stopped part way through a file). Each follower's own phase:
        anchored at its -txid's last write (right after a poll that applied, or its first restore) when that is
        newer than its start, else its start plus a second (its resume's S3 listings before the first poll)."""
        f = self.follow_s
        if f <= 0:
            return 0.0
        started = float((self.fstate.get(db) or {}).get("started") or 0.0)
        try:
            t = Path(f"{self._out(db)}-txid").stat().st_mtime
        except OSError:
            t = 0.0
        anchor = t if t > started else started + 1.0
        return (anchor + f / 2 - now) % f

    def _wait_phase(self, db: str, gone: Callable[[], bool]) -> None:
        end = time.time() + self.phase_delay(db, time.time())
        while not gone() and time.time() < end:
            self.stop.wait(min(0.5, max(0.0, end - time.time())))

    def _litestream_restore(self, db: str, db_path: Path, out: Path, cancel: Callable[[], bool]) -> int | None:
        """A fresh `litestream restore` of the latest into out, at nice 19 in a child process, killed the moment
        cancel() says so (Cancelled). Its position; None: no backup (-if-replica-exists). RuntimeError: it failed."""
        out.parent.mkdir(parents=True, exist_ok=True)
        clean(out)
        errlog = Path(f"{out}.log")
        with open(errlog, "wb") as err:
            p = subprocess.Popen([*_nice(), "litestream", "restore", "-json", "-if-replica-exists", "-config", self.ls_config,
                                  "-o", str(out), str(db_path)], stdout=subprocess.PIPE, stderr=err, text=True,
                                 start_new_session=True)
        end = time.monotonic() + CONFIRM_TIMEOUT_S
        try:
            while p.poll() is None:
                if cancel() or time.monotonic() > end:
                    _kill(p)
                    if cancel():
                        raise Cancelled()
                    raise RuntimeError(f"litestream restore took over {CONFIRM_TIMEOUT_S:.0f} s")
                try:
                    p.wait(0.25)
                except subprocess.TimeoutExpired:
                    pass
            stdout = p.stdout.read() if p.stdout else ""
            if p.returncode != 0:
                raise RuntimeError(f"litestream restore exited {p.returncode}: {_tail(errlog) or stdout.strip()[-300:]}")
        finally:
            if p.stdout:
                p.stdout.close()
            errlog.unlink(missing_ok=True)
        if not out.exists():
            return None
        lines = stdout.splitlines()
        for i in range(len(lines) - 1, -1, -1):              # -json: one indented object, logs before it
            if lines[i].startswith("{"):
                try:
                    d = json.loads("\n".join(lines[i:]))
                except ValueError:
                    continue
                n = leaderlock.txid(d.get("txid")) if isinstance(d, dict) else None
                if n is not None:
                    return n
        raise RuntimeError("litestream restore: no txid in its answer")

    def confirm(self, sup, db: str, full: bool, cancel: Callable[[], bool]) -> tuple[dict, int | None, Path | None]:
        """A copy failed its check (or can't be trusted): a fresh restore from S3, checked. (its result, its position,
        the fresh file: the caller's to keep or delete). Its result: the check's; restore_failed if the restore
        failed; ok None if there is no backup or it couldn't be checked. Cancelled: the role changed."""
        fresh = self.work / f"{db}.fresh.sqlite"
        for attempt in (1, 2):                      # (twice: a file of its plan compacted away while it read)
            try:
                pos = self.restorer(db, sup.env.db_path(db), fresh, cancel)
                break
            except Cancelled:
                clean(fresh)
                raise
            except Exception as e:  # noqa: BLE001 - Litestream couldn't restore it: possibly the replica itself
                clean(fresh)
                if attempt == 2:
                    return {"ok": None, "result": f"a fresh restore failed: {e}"[:400], "restore_failed": True}, None, None
                self.log(f"replica-watch: {db}.sqlite: a fresh restore failed ({str(e)[:200]}): once more")
        if pos is None or not fresh.exists():
            clean(fresh)
            return {"ok": None, "result": "a fresh restore found no backup in S3"}, None, None
        res = self._checked(fresh, full, cancel)
        for s in ("-wal", "-shm"):
            Path(f"{fresh}{s}").unlink(missing_ok=True)
        if res.get("cancelled"):
            clean(fresh)
            raise Cancelled()
        return res, pos, fresh

    def apply_errors(self, db: str) -> str:
        """What the leader's stopped follower of db logged as errors since it started (its stderr, `<out>.log`):
        "follow: error applying updates" (an LTX file it can't apply: corrupt, or gone) and the like; "" if none.
        A copy stopped mid-apply by our SIGTERM logs none ("follow mode stopped")."""
        try:
            lines = Path(f"{self._out(db)}.log").read_text(errors="replace").splitlines()
        except OSError:
            return ""
        bad = [x for x in lines if "level=ERROR" in x or "error applying" in x]
        return " | ".join(bad[-2:])[-300:]

    def _decide(self, sup, db: str, copy: dict | None, full: bool, cancel: Callable[[], bool], pos: int | None,
                how: str, keep: Path | None, follow_err: str = "", standby: bool = False) -> tuple[dict, int | None, str]:
        """The check copy failed (copy: its result) or was stopped mid-apply (copy None): confirm() decides. Fresh
        ok: the copy drifted, the replica is fine ("reseeded" in the result; keep: the fresh file becomes the
        follower's there, with its -txid). Otherwise the fresh restore's verdict, said as such. (result, position,
        how). Cancelled: the role changed."""
        why = "stopped mid-apply" if copy is None else "failed " + " · ".join(
            str(copy.get("result") or "").splitlines()[:2])[:160]
        fres, fpos, fresh = self.confirm(sup, db, full, cancel)
        try:
            at = f"{fpos:016x}" if isinstance(fpos, int) else "?"
            if fres.get("ok") is True:
                if keep is not None and fresh is not None:
                    clean(keep)
                    os.replace(fresh, keep)
                    Path(f"{keep}-txid").write_text(f"{fpos:016x}\n")
                if follow_err:                       # not our SIGTERM: the follower can't apply what S3 holds
                    return ({"ok": None, "follow_failed": True,
                             "result": f"follower apply errors; a fresh restore from S3 at {at} passes: {follow_err}"[:400]},
                            fpos, f"{how}; check copy {why}")
                if standby:                          # the takeover's copy is the bad one: re-seeding ours fixes nothing
                    return ({"ok": None, "follow_failed": True,
                             "result": (f"the standby's copy (the takeover's) {why}; a fresh restore from S3 at {at} "
                                        "passes · restart the standby task before deploying: a takeover from this "
                                        "copy may be refused")[:400]}, pos, f"{how}; a fresh restore ok")
                self.log(f"replica-watch: {db}.sqlite: the check copy drifted ({why}) at "
                         f"{f'{pos:016x}' if isinstance(pos, int) else '?'}; a fresh restore from S3 at {at} is ok: "
                         "re-seeded")
                return dict(fres, reseeded=why), fpos, f"{how}; check copy {why}, a fresh restore ok"
            if fres.get("ok") is False:
                return dict(fres, confirmed=True), fpos, f"{how}; check copy {why}, a fresh restore too"
            if copy is None:                         # nothing failed a check: not verifiable this round
                return fres, pos, f"{how}; check copy {why}"
            return (dict(fres, result=f"check copy {why}; {fres.get('result') or ''}"[:400]), pos,
                    f"{how}; not confirmed")
        finally:
            if fresh is not None:
                clean(fresh)

    # -- the standby -------------------------------------------------------------------------------------------------

    def standby_round(self, sup) -> dict:
        """A consistent copy of each followed file, checked. Stopped (nothing recorded) the moment the role changes."""
        gone = lambda: self.stop.is_set() or sup.role != "standby"   # noqa: E731
        out: dict[str, dict] = {}
        for db in DBS:
            if gone():
                return out
            if db in sup.no_replica:                   # a leader is running, yet there is no backup of it
                out[db] = self._result(db, {"ok": None, "result": "no backup in S3"}, False, "standby", None, 0.0, "")
                continue
            p = sup.followers.get(db)
            if db not in sup.follow_done or p is None or p.poll() is not None:
                continue
            src, snap = sup.staged(db), self.work / f"{db}.sqlite"
            t0, full = self.mono(), self._full(db)
            try:
                pos = snapshot(src, snap, lambda: sup.env.applied(src), abort=gone)
                copied = self.mono() - t0
                res = self._checked(snap, full, gone)
            except Exception as e:  # noqa: BLE001 - the follower restarting, the disk: not the replica's fault
                res, pos, copied = {"ok": None, "result": f"{type(e).__name__}: {e}"[:300]}, None, 0.0
            finally:
                clean(snap)
            if gone() or res.get("cancelled"):
                return out
            how = f"copy {copied:.1f} s"
            if res.get("ok") is False:                 # the copy, or the replica? A fresh restore decides
                try:
                    res, pos, how = self._decide(sup, db, res, full, gone, pos, how, keep=None, standby=True)
                except Cancelled:
                    return out
            out[db] = self._result(db, res, full, "standby", pos, self.mono() - t0, how)
        if out:
            self.record(out)
        return out

    # -- the leader: its own low-rate followers --------------------------------------------------------------------------

    def _out(self, db: str) -> Path:
        return self.work / f"{db}.sqlite"

    def _litestream_follower(self, db: str, db_path: Path, out: Path):
        out.parent.mkdir(parents=True, exist_ok=True)
        errlog = open(Path(f"{out}.log"), "ab")
        try:
            return subprocess.Popen([*_nice(), "litestream", "restore", "-f", "-follow-interval", f"{int(self.follow_s)}s",
                                     "-if-replica-exists", "-config", self.ls_config, "-o", str(out), str(db_path)],
                                    stdout=subprocess.DEVNULL, stderr=errlog, start_new_session=True)
        finally:
            errlog.close()

    def start_followers(self, sup, at: float | None = None) -> None:
        """Fresh (whatever was in the work dir goes: an earlier task's or this one's from before a retake, frozen since):
        from `at` (monotonic; LEADER_FIRST_S after it serves, out of the new leader's busiest minute), one at a time in
        DBS order: each starts once the one before has done its first restore (tend_followers)."""
        self.stop_followers()
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.fstate.clear()
        self.checked.clear()
        self.to_start = list(DBS)
        self.start_at = self.mono() if at is None else at
        self.tend_followers(sup)

    def _restoring(self) -> bool:
        """A follower still doing its first restore."""
        return any(p.poll() is None and self.applied(db) is None for db, p in self.followers.items())

    def _start_follower(self, sup, db: str) -> None:
        Path(f"{self._out(db)}.log").unlink(missing_ok=True)
        self.followers[db] = self.follower(db, sup.env.db_path(db), self._out(db))
        self.fstate.setdefault(db, {"fails": 0, "restart_at": None, "missing": False})["started"] = time.time()

    def stop_followers(self) -> None:
        for p in list(self.followers.values()):
            _stop(p)
        self.followers.clear()

    def applied(self, db: str) -> int | None:
        try:
            return leaderlock.txid(Path(f"{self._out(db)}-txid").read_text().strip())
        except OSError:
            return None

    def tend_followers(self, sup) -> None:
        """A follower that exited: with no file (-if-replica-exists) the replica has no backup; with an error, the
        restore failed (an LTX checksum mismatch, ...): said at once, started again after a backoff."""
        now = self.mono()
        if self.to_start and now >= self.start_at and not self._restoring() and sup.role == "active":
            self._start_follower(sup, self.to_start.pop(0))
        for db in DBS:
            if db in self.to_start:
                continue
            st = self.fstate.setdefault(db, {"fails": 0, "restart_at": None, "missing": False})
            p = self.followers.get(db)
            if p is None:
                if st["restart_at"] is not None and now >= st["restart_at"] and sup.role == "active":
                    st["restart_at"] = None
                    self._start_follower(sup, db)
                continue
            code = p.poll()
            if code is None:
                continue
            self.followers.pop(db, None)
            if code == 0 and not self._out(db).exists():
                st["missing"] = True                         # no backup: the round says "not verifiable"
                st["restart_at"] = now + FOLLOWER_BACKOFF[0]
                continue
            st["fails"] += 1
            st["restart_at"] = now + min(FOLLOWER_BACKOFF[1], FOLLOWER_BACKOFF[0] * 2 ** (st["fails"] - 1))
            tail = _tail(Path(f"{self._out(db)}.log"))
            if code > 0 and "error" in tail.lower():         # Litestream said why: possibly the replica itself
                r = self._result(db, {"ok": None, "result": f"litestream restore failed: {tail}"}, False, "leader",
                                 self.applied(db), 0.0, "follower")
                r["restore_failed"] = True
                self.log(f"handoff: replica check FAILED: {db}.sqlite: restore failed (possible corruption): {tail}")
            else:                                            # killed (OOM -9, a SIGTERM from elsewhere), or silent
                how = f"killed by signal {-code}" if code < 0 else f"exited {code}"
                r = self._result(db, {"ok": None, "result": f"the follower {how}" + (f": {tail}" if tail else "")}, False,
                                 "leader", self.applied(db), 0.0, "follower")
            self.record({db: r})

    def leader_round(self, sup) -> bool:
        """Each follower stopped, its file checked, the follower started again (it resumes from its -txid). True: a
        file not restored yet (look again in a minute)."""
        gone = lambda: self.stop.is_set() or sup.role != "active"     # noqa: E731
        out: dict[str, dict] = {}
        pending = False
        since = self.mono() - (self.serving_at or self.mono())
        for db in DBS:
            if gone():
                return False
            st = self.fstate.get(db) or {}
            p, path = self.followers.get(db), self._out(db)
            if st.get("missing") and not path.exists():
                out[db] = self._result(db, {"ok": None, "result": "no backup in S3"}, False, "leader", None, 0.0, "")
                continue
            if self.applied(db) is None or not path.exists():
                if since < RESTORE_S:
                    pending = True
                    continue
                out[db] = self._result(db, {"ok": None, "result": f"not restored after {RESTORE_S / 60:g} min"}, False,
                                       "leader", None, 0.0, "")
                continue
            if p is not None:
                self._wait_phase(db, gone)                    # half a poll off its own polls
                if gone():
                    return False
            t0, full = self.mono(), self._full(db)
            if p is not None:
                _stop(p)                                      # (SIGTERM: it stops between two LTX files, or in one)
                self.followers.pop(db, None)
            pos, how = self.applied(db), "follower paused"
            err = self.apply_errors(db)
            if not settled(path) and not err and db not in self.checked and since < RESTORE_S:
                self.log(f"replica-watch: {db}.sqlite: its follower was stopped mid-apply: looked at again in "
                         f"{LEADER_RETRY_S:g} s")                # (the deploy's first round: no full restore for it)
                if p is not None and not gone():
                    self._start_follower(sup, db)
                pending = True
                continue
            try:
                if not settled(path):                        # stopped part way through a round: two positions
                    res, pos, how = self._decide(sup, db, None, full, gone, pos, how, keep=path, follow_err=err)
                else:
                    had = {s: Path(f"{path}{s}").exists() for s in ("-wal", "-shm")}
                    try:
                        res = self._checked(path, full, gone)
                    finally:
                        for s, existed in had.items():       # (what our read-only open left: not the follower's)
                            if not existed:
                                Path(f"{path}{s}").unlink(missing_ok=True)
                    if res.get("ok") is False and not gone():
                        res, pos, how = self._decide(sup, db, res, full, gone, pos, how, keep=path, follow_err=err)
            except Cancelled:
                return False
            finally:
                if p is not None and not gone():
                    self._start_follower(sup, db)
            if gone() or res.get("cancelled"):
                return False
            out[db] = self._result(db, res, full, "leader", pos, self.mono() - t0, how)
            self.checked.add(db)
        if out:
            self.record(out)
        return pending

    def _result(self, db: str, res: dict, full: bool, by: str, pos: int | None, secs: float, how: str) -> dict:
        r = {"at": self.wall(), "ok": res.get("ok"), "result": str(res.get("result") or "")[:400],
             "mode": "integrity_check" if full else "quick_check", "by": by, "task": leaderlock.short(self.holder),
             "txid": f"{pos:016x}" if isinstance(pos, int) else None, "secs": round(secs, 1)}
        for k in ("reseeded", "confirmed", "restore_failed", "follow_failed"):      # (_decide's: how the verdict was reached)
            if res.get(k):
                r[k] = res[k]
        where = f"{db}.sqlite ({by}, {r['mode']}" + (f" at {r['txid']}" if r["txid"] else "") \
            + (f", {how}" if how else "") + f", {secs:.1f} s)"
        if r["ok"] is True:
            self.log(f"replica-watch: {where}: ok")
        elif r["ok"] is False:
            self.log(f"handoff: replica check FAILED: {where}: {r['result'][:300]}")
        elif r.get("restore_failed"):                     # (the CloudWatch metric filter's line, as for corrupt)
            self.log(f"handoff: replica check FAILED: {where}: restore failed (possible corruption): {r['result'][:300]}")
        elif r.get("follow_failed"):
            self.log(f"handoff: replica check FAILED: {where}: {r['result'][:300]}")
        else:
            self.log(f"replica-watch: {where}: not verifiable: {r['result'][:300]}")
        return r

    # -- results ---------------------------------------------------------------------------------------------------

    def record(self, results: dict[str, dict]) -> dict:
        """Into the shared record, then pushed: corrupt (5), a restore that failed (5, at once), not verifiable
        ERRORS_ALERT rounds running (4), each its own slot; "Replica OK again" for a file failing before."""
        corrupt: dict[str, dict] = {}
        restore: dict[str, dict] = {}
        unverif: dict[str, dict] = {}
        follow: dict[str, dict] = {}
        drift: dict[str, dict] = {}
        back: list[str] = []

        def merge(d: dict) -> None:
            checks, errors, failing = d.setdefault("checks", {}), d.setdefault("errors", {}), d.setdefault("failing", {})
            for x in (corrupt, restore, unverif, follow, drift):
                x.clear()
            back.clear()
            for db, r in results.items():
                if r["ok"] is None:
                    errors[db] = int(errors.get(db) or 0) + 1
                    checks[db] = dict(r, errors=errors[db])
                    if r.get("restore_failed"):
                        restore[db] = r
                        failing.setdefault(db, r["at"])
                    elif r.get("follow_failed"):             # a follower can't apply what S3 holds: at once
                        follow[db] = r
                        failing.setdefault(db, r["at"])
                    elif errors[db] >= ERRORS_ALERT:
                        unverif[db] = r
                        failing.setdefault(db, r["at"])
                    continue
                errors.pop(db, None)
                rs = d.setdefault("reseeds", {})
                was = rs.get(db) if isinstance(rs.get(db), dict) else {}
                if r.get("reseeded"):                        # the check copy drifted, the replica is fine: counted
                    day = [float(t) for t in (was.get("day") or []) if r["at"] - float(t) < 86400.0] + [r["at"]]
                    rs[db] = {"n": int(was.get("n") or 0) + 1, "since": was.get("since") or r["at"], "at": r["at"],
                              "why": str(r["reseeded"])[:200], "task": r["task"],
                              "streak": int(was.get("streak") or 0) + 1, "day": day[-(DRIFT_DAY_N + 1):]}
                    if rs[db]["streak"] >= DRIFT_STREAK or len(day) > DRIFT_DAY_N:   # once is a drift; this isn't
                        drift[db] = dict(r, result=f"re-seeded {rs[db]['streak']} rounds running, {len(day)} in 24 h: "
                                                   f"{rs[db]['why']}")
                elif was.get("streak"):
                    rs[db] = dict(was, streak=0)
                if r["ok"] is False:
                    failing.setdefault(db, r["at"])
                    checks[db] = dict(r, bad_since=failing[db])
                    corrupt[db] = r
                else:
                    checks[db] = r
                    if failing.pop(db, None) is not None:
                        back.append(db)
        try:
            doc = self.store.update(merge)
        except Exception as e:  # noqa: BLE001 - S3 unreadable: said from what this task saw
            self.log(f"replica-watch: can't record the checks ({type(e).__name__}: {e})")
            doc = json.loads(json.dumps(self.store.last or {}))
            merge(doc)
            self.store.last = doc
        self.write_state(doc)

        def say(kind: str, title: str, bad: dict[str, dict], prio: int,
                tail: str = "a takeover or cold start from it would fail: fix before the next deploy") -> None:
            if not bad:
                return
            first = next(iter(bad.values()))
            self.pusher.push(kind, f"{title}: {', '.join(f'{db}.sqlite' for db in bad)}",
                             f"{first['mode'] if kind == 'replica_corrupt' else first['by']}"
                             + (f" at {first['txid']}" if first.get("txid") else "") + f": {first['result'][:240]} · "
                             + ("a fresh restore from S3 fails it too · " if first.get("confirmed") else "")
                             + tail, priority=prio)
        say("replica_corrupt", "Replica corrupt", corrupt, 5)
        say("replica_restore", "Replica restore failed (possible corruption)", restore, 5)
        say("replica_unverifiable", "Replica not verifiable", unverif, 4)
        say("replica_follow", "Replica follower failing", follow, 4,
            "a follower (a takeover's catch-up) may fail the same way: look at the follower's errors before the next deploy")
        say("replica_drift", "Replica check copy keeps drifting", drift, 4,
            "a fresh restore passes each time; a follower that keeps drifting is worth a look (docs/cloud.md Replica checks)")
        for db in drift:
            self.log(f"replica-watch: {db}.sqlite: {drift[db]['result'][:300]}")
        for db in back:
            self.pusher.push("replica_ok", f"Replica OK again: {db}.sqlite",
                             f"{results[db]['mode']} ok on the {results[db]['by']}'s copy at {_hhmm(results[db]['at'])}",
                             priority=3, tags=("white_check_mark", "sizeup"), limit=False)
        return doc

    def refresh(self) -> dict | None:
        """The shared record, read again and handed to the app (the standby's checks reach the leader's Health)."""
        try:
            doc = self.store.read()
        except Exception as e:  # noqa: BLE001
            self.log(f"replica-watch: can't read the record ({type(e).__name__}: {e})")
            return None
        self.write_state(doc)
        return doc

    def write_state(self, doc: dict) -> None:
        """$SIZEUP_RUN_DIR/replica-watch.json for the app: the checks, the failing files and the last task events."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            out = {"at": self.wall(), "checks": doc.get("checks") or {}, "failing": doc.get("failing") or {},
                   "suspect": doc.get("suspect") or {}, "reseeds": doc.get("reseeds") or {},
                   "events": (doc.get("events") or [])[-10:], "interval_s": self.interval_s,
                   "leader_interval_s": self.leader_interval_s}
            fd, tmp = tempfile.mkstemp(prefix=f".{STATE_FILE}.", dir=self.run_dir)
            with os.fdopen(fd, "w") as f:
                json.dump(out, f, separators=(",", ":"))
            os.replace(tmp, self.run_dir / STATE_FILE)
        except OSError as e:
            self.log(f"replica-watch: can't write {STATE_FILE} ({e})")

    # -- a task that fails --------------------------------------------------------------------------------------------

    def report(self, kind: str, why: str, code: int, deadline_s: float = REPORT_S) -> bool:
        """task_failed() within deadline_s in all (DNS included: on a thread of its own). True: it finished in time."""
        self.stop.set()
        t = threading.Thread(target=self.task_failed, args=(kind, why, code), name="replica-report", daemon=True)
        t.start()
        t.join(deadline_s)
        if t.is_alive():
            self.log(f"replica-watch: the failure report didn't finish in {deadline_s:.0f} s: exiting anyway")
            return False
        return True

    def task_failed(self, kind: str, why: str, code: int) -> None:
        """This task ends badly: recorded in S3 and pushed now, before it exits (nothing local survives it)."""
        if self.reported:
            return
        self.reported = True
        self.stop.set()
        now = self.wall()
        ev = {"at": now, "kind": kind, "why": str(why)[:300], "code": code, "task": leaderlock.short(self.holder)}
        self.log(f"handoff: task failure recorded: {kind}: {ev['why']} (exit {code})")

        def add(d: dict) -> None:
            d["events"] = ((d.get("events") or []) + [ev])[-EVENTS_MAX:]
        known = None
        try:
            doc = known = self.store.update(add)
        except Exception as e:  # noqa: BLE001
            self.log(f"replica-watch: can't record the failure ({type(e).__name__}: {e})")
            doc = {"events": [ev]}
        recent = [e for e in doc.get("events") or [] if isinstance(e, dict) and e.get("kind") not in NOT_FAILURES
                  and now - float(e.get("at") or 0) <= LOOP_WINDOW_S]
        if len(recent) >= LOOP_N:                         # the outage first: its own slot, priority 5
            kinds = sorted({str(e.get("kind")) for e in recent})
            self.pusher.push("task_loop", f"Hub crash-looping: {len(recent)} failed tasks in {LOOP_WINDOW_S / 60:g} min",
                             f"{', '.join(kinds)} · last: {ev['why'][:200]} · the site may be down (Cloudflare 1033)",
                             priority=5, known=known if known is not None else {})
        body = f"{ev['why']} · exit {code}" + ("; ECS starts another task" if code else "")
        self.pusher.push("task", TITLES.get(kind, TITLES["exited"]), body, priority=4,
                         known=known if known is not None else {})

    def litestream_restarted(self, why: str, unverified: dict[str, str], healed: list[str] | tuple = ()) -> None:
        """Litestream was started again under the serving app (the supervisor's _restart_litestream): an event in the
        record and a push, always (a suspect moment). unverified (db -> why its TRUNCATE checkpoint couldn't be done
        first): suspect in the record until healed (this task managed the checkpoint after all: "Replica OK again")."""
        now, me = self.wall(), leaderlock.short(self.holder)
        ok = not unverified
        text = (f"{why}: started again after a TRUNCATE checkpoint of every file (a full snapshot of each follows)" if ok
                else f"{why}: started again WITHOUT a TRUNCATE checkpoint of "
                     + ", ".join(f"{db}.sqlite ({w})" for db, w in unverified.items())
                     + ": what was written while it was down may be missing from the replica")[:300]
        ev = {"at": now, "kind": "litestream_restarted" if ok else "replica_suspect", "why": text, "code": None, "task": me}
        self.log(f"handoff: {'Litestream restarted' if ok else 'replica check FAILED: not verifiable'}: {text}")

        def add(d: dict) -> None:
            d["events"] = ((d.get("events") or []) + [ev])[-EVENTS_MAX:]
            sus = dict(d.get("suspect") or {})
            for db in healed:
                sus.pop(db, None)
            for db, w in unverified.items():
                sus[db] = {"at": (sus.get(db) or {}).get("at") or now, "why": str(w)[:200], "task": me}
            d["suspect"] = sus
        known = None
        try:
            known = self.store.update(add)
            self.write_state(known)
        except Exception as e:  # noqa: BLE001 - S3 unreadable: pushed anyway
            self.log(f"replica-watch: can't record Litestream's restart ({type(e).__name__}: {e})")
        k = known if known is not None else {}
        if ok:
            self.pusher.push("litestream", "Hub Litestream restarted", text, priority=4, known=k)
        else:
            self.pusher.push("replica_suspect", "Replica not verifiable: " + ", ".join(f"{db}.sqlite" for db in unverified),
                             f"{text} · tried again every minute or so; a deploy before \"Replica OK again\" may lose "
                             "those writes", priority=4, known=k)
        for db in healed:
            if db not in unverified:
                self.pusher.push("replica_ok", f"Replica OK again: {db}.sqlite",
                                 f"TRUNCATE checkpoint done at {_hhmm(now)}: Litestream snapshots the file", priority=3,
                                 tags=("white_check_mark", "sizeup"), limit=False)

    def serving(self) -> None:
        """A task serves: it says so in the record (task, build, since: the deploy's check waits for it), and "Hub
        serving again" if task failures were pushed (and not yet answered) recently."""
        now = self.wall()
        was: list[float] = []

        def mark(d: dict) -> None:
            d["leader"] = {"task": leaderlock.short(self.holder), "build": self.build, "serving_at": now}
            old = {db: x for db, x in (d.get("suspect") or {}).items()
                   if not isinstance(x, dict) or x.get("task") != leaderlock.short(self.holder)}
            if old:                                   # another task's: this one serves what the replica had (history)
                d["suspect"] = {db: x for db, x in d["suspect"].items() if db not in old}
                d["events"] = ((d.get("events") or []) + [{
                    "at": now, "kind": "replica_suspect", "code": None, "task": leaderlock.short(self.holder),
                    "why": f"took over from a replica not verifiable since Litestream's restart: {', '.join(sorted(old))}"}]
                               )[-EVENTS_MAX:]
            p = d.get("pushed") or {}
            last = max(float(p.get(k) or 0) for k in ("task", "task_loop"))
            if last and last > float(d.get("serving_again") or 0) and now - last <= SERVING_AGAIN_S:
                was.append(last)
                d["serving_again"] = now
        try:
            doc = self.store.update(mark)
        except Exception as e:  # noqa: BLE001
            self.log(f"replica-watch: can't record that this task serves ({type(e).__name__}: {e})")
            return
        self.write_state(doc)
        if was:
            n = sum(1 for e in doc.get("events") or [] if isinstance(e, dict) and float(e.get("at") or 0) >= was[0] - LOOP_WINDOW_S
                    and e.get("kind") not in NOT_FAILURES)
            self.pusher.push("serving", "Hub serving again", f"a task serves since {_hhmm(now)} after {n} failed task(s)",
                             priority=3, tags=("white_check_mark", "sizeup"), limit=False)


def _stop(p) -> None:
    """A follower: SIGTERM, then SIGKILL. SIGTERM cancels what it applies, maybe part way through a file (its copy
    then holds two positions: settled() says so)."""
    try:
        if p.poll() is None:
            try:
                if _group(p) is None:
                    raise OSError("no group of its own")
                os.killpg(_group(p), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                p.terminate()
            p.wait(15)
    except Exception:  # noqa: BLE001
        _kill(p)


def _minutes(env: dict, name: str, default_s: float, unit: float = 60.0) -> float:
    try:
        v = float(env.get(name, "") or default_s / unit)
    except ValueError:
        return default_s
    return max(0.0, v) * unit


def from_env(holder: str, run_dir: Path, state_dir: Path, ls_config: str, config: str = "",
             env: dict | None = None, log: Callable[[str], None] = log) -> ReplicaWatch:
    """The hub's (deploy/aws/taskdef.json: LITESTREAM_BUCKET / _PATH / _REGION; the topics from SSM)."""
    env = os.environ if env is None else env
    bucket = env.get("LITESTREAM_BUCKET", "").strip()
    prefix = (env.get("LITESTREAM_PATH", "").strip() or "sizeup-hub").strip("/")
    obj = S3Object(bucket, f"{prefix}/{KEY}", env.get("LITESTREAM_REGION", "").strip()) if bucket else None
    store = WatchStore(obj)
    server, topic, forbid = status_topic(config, env, log)
    pusher = Pusher(store, server, topic, forbid, every_s=_minutes(env, "SIZEUP_REPLICA_PUSH_MIN", PUSH_EVERY_S), log=log)
    w = ReplicaWatch(store, pusher, holder, run_dir, state_dir / ".replica-check", ls_config,
                     build=env.get("SIZEUP_BUILD", "").strip(),
                     interval_s=_minutes(env, "SIZEUP_REPLICA_CHECK_MIN", INTERVAL_S),
                     leader_interval_s=_minutes(env, "SIZEUP_REPLICA_LEADER_CHECK_MIN", LEADER_INTERVAL_S),
                     follow_s=max(10.0, _minutes(env, "SIZEUP_REPLICA_FOLLOW_S", FOLLOW_S, 1.0)),
                     full_s=_minutes(env, "SIZEUP_REPLICA_FULL_H", FULL_S, 3600.0) or FULL_S, log=log)
    log(f"replica-watch: standby every {w.interval_s / 60:g} min, leader every {w.leader_interval_s / 60:g} min "
        f"(followers every {w.follow_s:g} s; 0: off), integrity_check every {w.full_s / 3600:g} h; pushes "
        f"{'to the status topic' if pusher.on else 'off (no status topic)'}, one per kind per {pusher.every_s / 60:g} min")
    return w


def read_state(path: Path) -> dict | None:
    """The app's side: what the supervisor last wrote (None: no file, so no Replicas row)."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sizeup.replicawatch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="quick_check (--full: integrity_check) of one file, read-only: JSON {ok, result}")
    c.add_argument("--full", action="store_true")
    c.add_argument("path")
    a = ap.parse_args(argv)
    print(json.dumps(integrity(a.path, a.full)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
