"""The hub's replicas, verified all the time, and a task that fails, said at once (2026-11-13: a corrupt cad.sqlite
replica sat unnoticed for ~110 min until the next deploy's takeover failed its quick_check; then every new task
crash-looped for ~35 min until someone opened a browser). Run by the supervisor (sizeup/handoff.py), stdlib only.

    the standby  (roles starting / standby) every SIZEUP_REPLICA_CHECK_MIN (10): a consistent copy of each file its
                 followers keep (/data/standby/<db>.sqlite: copied while its -txid sidecar, size and mtime hold still,
                 so the follow is never paused), checked read-only (not immutable) in a child process at nice 19:
                 quick_check, every SIZEUP_REPLICA_FULL_EVERY (6) rounds integrity_check
    the leader   (role active) every SIZEUP_REPLICA_LEADER_CHECK_MIN (15), first LEADER_FIRST_S (2 min) after it
                 serves: `litestream restore` of cad and hub (audio every AUDIO_EVERY rounds) into /data/.replica-check
                 at nice 19, checked the same way, then deleted. A file a standby verified within that interval is
                 skipped (the standby's copy is the cheaper check). 0 turns either off.

The results go to s3://<bucket>/<prefix>/replica-watch.json (conditional writes, like the lock), which every task
reads and the leader mirrors to $SIZEUP_RUN_DIR/replica-watch.json for the app (Settings > Health: the Replicas row
and a "Replica corrupt" alert, sizeup/cloud/health.py). A file found bad (or that can't be restored twice running):
one line "replica check FAILED" in the log (CloudWatch metric filter, sizeup-aws step 28) and a push to the STATUS
topic at once; "Replica OK again" when it verifies again.

A task that ends badly (Supervisor.run(): a takeover that failed, the app exiting, fenced, an unclean release, the
supervisor itself crashing) records an event in the same object (it is about to exit: nothing local survives) and
pushes to the STATUS topic before it exits, synchronously with a short timeout. Three or more failures in
LOOP_WINDOW_S (10 min) is a crash loop: one more push says so. "Hub serving again" once a task serves after them.

Pushes (Pusher): at most one per kind per SIZEUP_REPLICA_PUSH_MIN (30), kept in the S3 object, so a crash loop of
fresh tasks can't repeat one; the conditional write also makes sure only one of two tasks failing at once pushes.
S3 unreadable: pushed anyway (it is rare, and the push is the point). The topic is the env's (SSM
/sizeup-hub/status-topic, else the alerts topic: notify.route()), else the config file's [alerts]: a Settings
override lives in the CAD database, which the supervisor never opens. Tapout's topics and the [prealert] ones are
refused (notify.topic_problem). Never a topic's name in a log line.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
STATE_FILE = "replica-watch.json"        # in the run dir: what the app's Settings > Health reads

INTERVAL_S = 600.0          # the standby: a check of each followed file this often
LEADER_INTERVAL_S = 900.0   # the leader: a restore and check this often (no standby verified it meanwhile)
LEADER_FIRST_S = 120.0      # ... the first this long after it serves (a bad start shows in minutes)
FULL_EVERY = 6              # every this many rounds integrity_check, else quick_check
AUDIO_EVERY = 4             # the leader restores audio.sqlite every this many rounds (the big one)
PUSH_EVERY_S = 1800.0       # at most one push per kind this often
REFRESH_S = 60.0            # the leader mirrors the S3 object to the app this often
CHECK_TIMEOUT_S = 900.0     # one check (a child process)
RESTORE_TIMEOUT_S = 600.0   # one restore
SNAPSHOT_TRIES = 5          # copies of a followed file before giving up for this round (it kept moving)
ERRORS_ALERT = 2            # rounds in a row a file couldn't be restored / checked: pushed like a bad one
EVENTS_MAX = 30
LOOP_WINDOW_S = 600.0       # task failures within this long ...
LOOP_N = 3                  # ... this many: a crash loop
SERVING_AGAIN_S = 6 * 3600.0   # "serving again" only for failures pushed this recently
PUSH_TIMEOUT_S = 5.0

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
    (not immutable: SQLite reads its -wal if there is one). ok None: it couldn't be opened or read (not corruption)."""
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


def run_check(path: Path, full: bool, timeout: float = CHECK_TIMEOUT_S) -> dict:
    """integrity() in a child process at nice 19: the hub's half vCPU stays the app's, and a corrupt file can't take
    the supervisor down with it."""
    argv = [*_nice(), sys.executable, "-m", "sizeup.replicawatch", "check", *(["--full"] if full else []), str(path)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": None, "result": f"the check took over {timeout:.0f} s"}
    except OSError as e:
        return {"ok": None, "result": f"{type(e).__name__}: {e}"}
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
        if isinstance(d, dict) and "ok" in d:
            return {"ok": d["ok"], "result": str(d.get("result") or "")[:400]}
    except (ValueError, IndexError):
        pass
    return {"ok": None, "result": f"the check exited {r.returncode}: {(r.stderr or r.stdout).strip()[-200:]}"}


def litestream_restore(ls_config: str, db: str, db_path: Path, out: Path, timeout: float = RESTORE_TIMEOUT_S) -> int | None:
    """`litestream restore` of the latest of db into out, at nice 19: its position; None: there is no backup of it.
    RuntimeError: it couldn't."""
    from sizeup.handoff import last_json
    out.parent.mkdir(parents=True, exist_ok=True)
    clean(out)
    try:
        r = subprocess.run([*_nice(), "litestream", "restore", "-json", "-config", ls_config, "-o", str(out), str(db_path)],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"litestream restore {db}: over {timeout:.0f} s") from None
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "no matching backup files" in err or "no snapshots available" in err:
            return None
        raise RuntimeError(f"litestream restore {db}: {err[-300:]}")
    try:
        return leaderlock.txid(last_json(r.stdout).get("txid"))
    except ValueError:
        return None


def clean(out: Path) -> None:
    for p in (out, Path(f"{out}-wal"), Path(f"{out}-shm"), Path(f"{out}-txid")):
        p.unlink(missing_ok=True)


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

def status_topic(config: str = "", env: dict | None = None) -> tuple[str, str, tuple[str, ...]]:
    """(the ntfy server, the STATUS topic in effect or "", topics never to publish to) from the env (SSM) over the
    config file's [alerts]; [prealert]'s topics refused like Tapout's."""
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
    forbid = tuple(str(pre.get(k) or "") for k in ("ring_topic", "status_topic") if pre.get(k))
    alert = (env.get("SIZEUP_ALERT_TOPIC") or str(al.get("ntfy_topic") or "")).strip()
    status = (env.get("SIZEUP_STATUS_TOPIC") or str(al.get("status_topic") or "")).strip()
    server = (env.get("SIZEUP_ALERT_SERVER") or str(al.get("ntfy_server") or "") or notify.DEFAULT_SERVER).strip()
    topic = notify.route(alert if not notify.topic_problem(alert, forbid) else "",
                         status if not notify.topic_problem(status, forbid) else "")[notify.STATUS]
    return server.rstrip("/"), topic, forbid


class Pusher:
    """A push to the STATUS topic now (synchronously: the task may be about to exit), at most one per kind per every_s
    across every task (the time is kept in the store)."""

    def __init__(self, store: WatchStore, server: str, topic: str, forbid: tuple[str, ...] = (),
                 every_s: float = PUSH_EVERY_S, wall: Callable[[], float] = time.time,
                 log: Callable[[str], None] = log, publish: Callable = notify.publish, timeout: float = PUSH_TIMEOUT_S):
        self.store, self.server, self.topic, self.forbid = store, server, topic, forbid
        self.every_s, self.wall, self.log, self.publish, self.timeout = every_s, wall, log, publish, timeout
        self.sent: list[tuple[str, str]] = []          # (kind, title), this task's (the tests read it)

    @property
    def on(self) -> bool:
        return bool(self.topic) and not notify.topic_problem(self.topic, self.forbid)

    def push(self, kind: str, title: str, body: str, priority: int = 4, tags: tuple = ("rotating_light", "sizeup"),
             limit: bool = True) -> bool:
        now = self.wall()
        go = [True]

        def claim(d: dict) -> bool:
            last = (d.get("pushed") or {}).get(kind)
            if limit and isinstance(last, (int, float)) and now - float(last) < self.every_s:
                go[0] = False
                return False
            go[0] = True
            d.setdefault("pushed", {})[kind] = now
            return True
        try:
            self.store.update(claim)
        except Exception as e:  # noqa: BLE001 - S3 unreadable: pushed anyway (rare, and the push is the point)
            self.log(f"replica-watch: can't record the {kind} push ({type(e).__name__}: {e}): pushing anyway")
            go[0] = True
        if not go[0]:
            self.log(f"replica-watch: {title}: not pushed (one {kind} push per {self.every_s / 60:g} min)")
            return False
        if not self.on:
            self.log(f"replica-watch: {title}: no status topic: not pushed")
            return False
        try:
            self.publish(self.server, self.topic, title, body, priority=priority, tags=tags, timeout=self.timeout)
        except notify.NotifyError as e:
            self.log(f"replica-watch: push not sent ({e}): {title}")
            return False
        self.sent.append((kind, title))
        self.log(f"replica-watch: pushed to the status topic: {title}")
        return True


# -- the watcher -------------------------------------------------------------------------------------------------------

class ReplicaWatch:
    """The supervisor's (sizeup/handoff.py Supervisor.watch): start(sup) runs the checks on a thread of their own;
    task_failed() before the task exits."""

    def __init__(self, store: WatchStore, pusher: Pusher, holder: str, run_dir: Path, work_dir: Path, ls_config: str = "",
                 interval_s: float = INTERVAL_S, leader_interval_s: float = LEADER_INTERVAL_S,
                 leader_first_s: float = LEADER_FIRST_S, full_every: int = FULL_EVERY, audio_every: int = AUDIO_EVERY,
                 check: Callable[[Path, bool], dict] = run_check, restore: Callable | None = None,
                 wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = log, poll_s: float = 1.0):
        self.store, self.pusher, self.holder = store, pusher, holder
        self.run_dir, self.work = run_dir, work_dir
        self.ls_config = ls_config
        self.interval_s, self.leader_interval_s, self.leader_first_s = interval_s, leader_interval_s, leader_first_s
        self.full_every, self.audio_every = max(1, int(full_every)), max(1, int(audio_every))
        self.check = check
        self.restore = restore or (lambda db, db_path, out: litestream_restore(self.ls_config, db, db_path, out))
        self.wall, self.mono, self.log, self.poll_s = wall, mono, log, poll_s
        self.rounds = 0
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.reported = False

    # -- the loop ------------------------------------------------------------------------------------------------

    def start(self, sup) -> threading.Thread:
        self.thread = threading.Thread(target=self._loop, args=(sup,), name="replica-watch", daemon=True)
        self.thread.start()
        return self.thread

    def _loop(self, sup) -> None:
        nxt_standby = nxt_leader = None
        nxt_refresh = 0.0
        was_active = False
        while not self.stop.is_set() and sup.role not in ("done", "fenced"):
            try:
                now = self.mono()
                if sup.role in ("starting", "standby") and self.interval_s > 0:
                    if nxt_standby is None and sup.follow_done >= set(DBS):
                        nxt_standby = now                              # all restored: the first check at once
                    if nxt_standby is not None and now >= nxt_standby:
                        self.standby_round(sup)
                        nxt_standby = self.mono() + self.interval_s
                elif sup.role == "active":
                    if not was_active:
                        was_active = True
                        self.serving()
                        nxt_leader = now + self.leader_first_s
                    if self.leader_interval_s > 0 and nxt_leader is not None and now >= nxt_leader:
                        self.leader_round(sup)
                        nxt_leader = self.mono() + self.leader_interval_s
                    if now >= nxt_refresh:
                        self.refresh()
                        nxt_refresh = self.mono() + REFRESH_S
            except Exception as e:  # noqa: BLE001 - the watcher never takes the supervisor down
                self.log(f"replica-watch: {type(e).__name__}: {e}")
            self.stop.wait(self.poll_s)

    # -- the rounds ----------------------------------------------------------------------------------------------

    def _full(self) -> bool:
        self.rounds += 1
        return self.rounds % self.full_every == 0

    def standby_round(self, sup) -> dict:
        """A consistent copy of each followed file, checked."""
        full = self._full()
        out: dict[str, dict] = {}
        for db in DBS:
            if sup.role not in ("starting", "standby"):
                return out                                   # taking over: the takeover checks them itself
            p = sup.followers.get(db)
            if db in sup.no_replica or db not in sup.follow_done or p is None or p.poll() is not None:
                continue
            src, snap = sup.staged(db), self.work / f"{db}.sqlite"
            t0 = self.mono()
            try:
                pos = snapshot(src, snap, lambda: sup.env.applied(src),
                               abort=lambda: sup.role not in ("starting", "standby"))
                copied = self.mono() - t0
                res = self.check(snap, full)
            except Exception as e:  # noqa: BLE001 - the follower restarting, the disk: not the replica's fault
                if sup.role not in ("starting", "standby"):
                    return out
                res, pos, copied = {"ok": None, "result": f"{type(e).__name__}: {e}"[:300]}, None, 0.0
            finally:
                clean(snap)
            if sup.role not in ("starting", "standby"):
                return out
            out[db] = self._result(db, res, full, "standby", pos, self.mono() - t0, f"copy {copied:.1f} s")
        if out:
            self.record(out)
        return out

    def leader_round(self, sup) -> dict:
        """A restore of the latest of each file from S3 into a temp dir, checked, deleted. Skipped for a file anyone
        verified within the leader's interval (a standby's copy)."""
        try:
            doc = self.store.read()
        except Exception:  # noqa: BLE001
            doc = self.store.last
        full = self._full()
        out: dict[str, dict] = {}
        now = self.wall()
        for db in DBS:
            if sup.role != "active":
                break
            if db == "audio" and (self.rounds - 1) % self.audio_every:
                continue
            last = (doc.get("checks") or {}).get(db) or {}
            if last.get("ok") is True and now - float(last.get("at") or 0) < self.leader_interval_s * 0.9:
                continue
            dst = self.work / f"{db}.sqlite"
            t0 = self.mono()
            try:
                pos = self.restore(db, sup.env.db_path(db), dst)
                took = self.mono() - t0
                if pos is None and not dst.exists():
                    continue                                 # no backup of it (yet)
                res = self.check(dst, full)
            except Exception as e:  # noqa: BLE001
                res, pos, took = {"ok": None, "result": f"{type(e).__name__}: {e}"[:300]}, None, self.mono() - t0
            finally:
                clean(dst)
            out[db] = self._result(db, res, full, "leader", pos, self.mono() - t0, f"restore {took:.1f} s")
        if out:
            self.record(out)
        return out

    def _result(self, db: str, res: dict, full: bool, by: str, pos: int | None, secs: float, how: str) -> dict:
        r = {"at": self.wall(), "ok": res.get("ok"), "result": str(res.get("result") or "")[:400],
             "mode": "integrity_check" if full else "quick_check", "by": by, "task": leaderlock.short(self.holder),
             "txid": f"{pos:016x}" if isinstance(pos, int) else None, "secs": round(secs, 1)}
        where = f"{db}.sqlite ({by}, {r['mode']}" + (f" at {r['txid']}" if r["txid"] else "") + f", {how}, {secs:.1f} s in all)"
        if r["ok"] is True:
            self.log(f"replica-watch: {where}: ok")
        elif r["ok"] is False:
            self.log(f"handoff: replica check FAILED: {where}: {r['result'][:300]}")
        else:
            self.log(f"replica-watch: {where}: couldn't check: {r['result'][:300]}")
        return r

    # -- results ---------------------------------------------------------------------------------------------------

    def record(self, results: dict[str, dict]) -> dict:
        """Into the shared record; a file newly bad (or failing to check ERRORS_ALERT times running) pushed; one that
        verifies again after it was pushed: "Replica OK again"."""
        bad: dict[str, dict] = {}
        back: list[str] = []

        def merge(d: dict) -> None:
            checks, errors, failing = d.setdefault("checks", {}), d.setdefault("errors", {}), d.setdefault("failing", {})
            bad.clear()
            back.clear()
            for db, r in results.items():
                if r["ok"] is None:
                    errors[db] = int(errors.get(db) or 0) + 1
                    prev = checks.get(db) or {}
                    checks[db] = dict(prev, last_error=r, errors=errors[db]) if prev.get("ok") is not None else dict(r, errors=errors[db])
                    if errors[db] >= ERRORS_ALERT:
                        bad[db] = r
                        failing.setdefault(db, r["at"])
                    continue
                errors.pop(db, None)
                if r["ok"] is False:
                    failing.setdefault(db, r["at"])
                    checks[db] = dict(r, bad_since=failing[db])
                    bad[db] = r
                else:
                    checks[db] = r
                    if failing.pop(db, None) is not None:
                        back.append(db)
        try:
            doc = self.store.update(merge)
        except Exception as e:  # noqa: BLE001 - S3 unreadable: say it from what this task saw
            self.log(f"replica-watch: can't record the checks ({type(e).__name__}: {e})")
            doc = json.loads(json.dumps(self.store.last or {}))
            merge(doc)
            self.store.last = doc
        self.write_state(doc)
        if bad:
            names = ", ".join(f"{db}.sqlite" for db in bad)
            first = next(iter(bad.values()))
            what = "corrupt" if any(r["ok"] is False for r in bad.values()) else "can't be verified"
            self.pusher.push("replica", f"Replica {what}: {names}",
                             f"{first['mode']} on the {first['by']}'s copy"
                             + (f" at {first['txid']}" if first.get("txid") else "") + f": {first['result'][:240]} · "
                             "a takeover or cold start from it would fail: fix before the next deploy", priority=5)
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
                   "events": (doc.get("events") or [])[-10:], "interval_s": self.interval_s,
                   "leader_interval_s": self.leader_interval_s}
            fd, tmp = tempfile.mkstemp(prefix=f".{STATE_FILE}.", dir=self.run_dir)
            with os.fdopen(fd, "w") as f:
                json.dump(out, f, separators=(",", ":"))
            os.replace(tmp, self.run_dir / STATE_FILE)
        except OSError as e:
            self.log(f"replica-watch: can't write {STATE_FILE} ({e})")

    # -- a task that fails --------------------------------------------------------------------------------------------

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
        try:
            doc = self.store.update(add)
        except Exception as e:  # noqa: BLE001
            self.log(f"replica-watch: can't record the failure ({type(e).__name__}: {e})")
            doc = {"events": [ev]}
        recent = [e for e in doc.get("events") or [] if isinstance(e, dict) and e.get("kind") != "unclean_release"
                  and now - float(e.get("at") or 0) <= LOOP_WINDOW_S]
        title = TITLES.get(kind, TITLES["exited"])
        body = f"{ev['why']} · exit {code}" + ("; ECS starts another task" if code else "")
        self.pusher.push("task", title, body, priority=4)
        if len(recent) >= LOOP_N:
            kinds = sorted({str(e.get("kind")) for e in recent})
            self.pusher.push("task_loop", f"Hub crash-looping: {len(recent)} failed tasks in {LOOP_WINDOW_S / 60:g} min",
                             f"{', '.join(kinds)} · last: {ev['why'][:200]} · the site may be down (Cloudflare 1033)",
                             priority=5)

    def serving(self) -> None:
        """A task serves: "Hub serving again" if task failures were pushed (and not yet answered) recently."""
        now = self.wall()
        was: list[float] = []

        def mark(d: dict) -> bool:
            p = d.get("pushed") or {}
            last = max(float(p.get(k) or 0) for k in ("task", "task_loop"))
            if not last or last <= float(d.get("serving_again") or 0) or now - last > SERVING_AGAIN_S:
                return False
            was.append(last)
            d["serving_again"] = now
            return True
        try:
            doc = self.store.update(mark)
        except Exception as e:  # noqa: BLE001
            self.log(f"replica-watch: can't read the record ({type(e).__name__}: {e})")
            return
        self.write_state(doc)
        if was:
            n = sum(1 for e in doc.get("events") or [] if isinstance(e, dict) and float(e.get("at") or 0) >= was[0] - LOOP_WINDOW_S
                    and e.get("kind") != "unclean_release")
            self.pusher.push("serving", "Hub serving again", f"a task serves since {_hhmm(now)} after {n} failed task(s)",
                             priority=3, tags=("white_check_mark", "sizeup"), limit=False)


def _minutes(env: dict, name: str, default_s: float) -> float:
    try:
        v = float(env.get(name, "") or default_s / 60)
    except ValueError:
        return default_s
    return max(0.0, v) * 60


def from_env(holder: str, run_dir: Path, state_dir: Path, ls_config: str, config: str = "",
             env: dict | None = None, log: Callable[[str], None] = log) -> ReplicaWatch:
    """The hub's (deploy/aws/taskdef.json: LITESTREAM_BUCKET / _PATH / _REGION; the topics from SSM)."""
    env = os.environ if env is None else env
    bucket = env.get("LITESTREAM_BUCKET", "").strip()
    prefix = (env.get("LITESTREAM_PATH", "").strip() or "sizeup-hub").strip("/")
    obj = S3Object(bucket, f"{prefix}/{KEY}", env.get("LITESTREAM_REGION", "").strip()) if bucket else None
    store = WatchStore(obj)
    server, topic, forbid = status_topic(config, env)
    pusher = Pusher(store, server, topic, forbid, every_s=_minutes(env, "SIZEUP_REPLICA_PUSH_MIN", PUSH_EVERY_S), log=log)
    try:
        full = int(env.get("SIZEUP_REPLICA_FULL_EVERY", "") or FULL_EVERY)
    except ValueError:
        full = FULL_EVERY
    w = ReplicaWatch(store, pusher, holder, run_dir, state_dir / ".replica-check", ls_config,
                     interval_s=_minutes(env, "SIZEUP_REPLICA_CHECK_MIN", INTERVAL_S),
                     leader_interval_s=_minutes(env, "SIZEUP_REPLICA_LEADER_CHECK_MIN", LEADER_INTERVAL_S),
                     full_every=full, log=log)
    log(f"replica-watch: standby every {w.interval_s / 60:g} min, leader every {w.leader_interval_s / 60:g} min "
        f"(0: off); pushes {'to the status topic' if pusher.on else 'off (no status topic)'}, "
        f"at most one per kind per {pusher.every_s / 60:g} min")
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
