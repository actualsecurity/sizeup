"""The change log (docs/mobile-api.md section 3.4, #561): one append-only sequence of REFERENCES `(seq, epoch, kind, id,
rev, at)`, built once and shared by /api/v1/changes, /api/v1/stream and the planned machine feed, each with its own
Scope (the kinds it may see). A reference carries no payload, no position and no transcript: the app fetches the object
(with its own role's filter) when it sees a new rev.

Storage: hub.sqlite (never cad.sqlite), three tables created on first use while the `stream` flag is on (a hub with
mobile off never creates them), through their own connection behind the fence (a fenced task writes nothing):

    mobile_changelog       seq (AUTOINCREMENT: never reused, so a cursor is never ambiguous), kind, id, rev, at
    mobile_changelog_head  the last rev logged per (kind, id): what a change is measured against, and what lets the next
                           task after a handoff go on from where this one stopped (the cursor is in the database,
                           not the process)
    mobile_changelog_meta  epoch (random, new if the log is ever reset or lost) and floor (the highest seq pruned)

Writes: a CAD commit only pokes the Recorder (an Event.set). Its one thread wakes on a poke, waits BATCH_S so the
commits of a busy minute become one transaction, compares the CAD objects it follows with their heads and appends what
changed. Nothing runs on a request thread, nothing at startup, and with the flag off a poke returns at once.

Cost (measured in tests/test_api_v1_changes.py): about one 4 KB page per change in the log plus its head row, in one
transaction per batch; a change of an object that keeps its rev writes nothing.

Retention: RETENTION_S (7 days) and at most MAX_ROWS rows, pruned an hour at a time; a cursor older than what is kept
(`floor`) is a 410 and the app resnapshots. Bounded: 100,000 rows of about 60 bytes is a few MB.

A deletion is a row whose rev is "" (the object is gone)."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, NamedTuple

from sizeup.fence import FencedWrite, GuardedConnection

from .rev import rev_of

RETENTION_S = 7 * 86400
MAX_ROWS = 100_000
BATCH_S = 1.0                  # a poke waits this long so a burst of commits is one write
PRUNE_EVERY_S = 3600.0
SUBSCRIBER_QUEUE = 256         # one stream's queue: past it the stream is dropped (it resumes from the database)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_changelog (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                                             id TEXT NOT NULL, rev TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mobile_changelog_head (kind TEXT NOT NULL, id TEXT NOT NULL, rev TEXT NOT NULL,
                                                  PRIMARY KEY (kind, id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS mobile_changelog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Change(NamedTuple):
    seq: int
    kind: str
    id: str
    rev: str          # "" : the object is gone
    at: float


class Scope(NamedTuple):
    """What one consumer may see: the kinds of reference. The app's is MOBILE; the machine feed (#562) gets its own.
    A per-principal predicate (feed.visible) narrows it further."""
    kinds: frozenset


MOBILE = Scope(frozenset({"incident", "unit"}))


def public(c: Change, epoch: str) -> dict:
    """The only fields a consumer ever gets: a reference, nothing else."""
    return {"seq": c.seq, "epoch": epoch, "kind": c.kind, "id": c.id, "rev": c.rev, "at": c.at}


class ChangeLog:
    """The log itself. `path`: hub.sqlite. may_write: the fence's ok() (None: always). Opened and created on first use."""

    def __init__(self, path: str | Path, may_write: Callable[[], bool] | None = None, clock=time.time,
                 retention_s: float = RETENTION_S, max_rows: int = MAX_ROWS):
        self.path, self.may_write, self.clock = str(path), may_write, clock
        self.retention_s, self.max_rows = retention_s, max_rows
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._epoch = ""
        self._floor = 0
        self._pruned_at = 0.0

    # -- opening ------------------------------------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is not None:
                return self._db
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, factory=GuardedConnection)
            if self.may_write is not None:
                db.may_write = self.may_write
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA busy_timeout = 5000")
            have = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'mobile_changelog_meta'").fetchone()
            if not have:
                self._guard()
                db.executescript(SCHEMA)        # (a fenced task raises here: nothing made, and tried again later)
            rows = dict(db.execute("SELECT key, value FROM mobile_changelog_meta").fetchall())
            if "epoch" not in rows:
                self._guard()
                db.execute("INSERT OR IGNORE INTO mobile_changelog_meta (key, value) VALUES ('epoch', ?)",
                           (secrets.token_hex(6),))
                rows = dict(db.execute("SELECT key, value FROM mobile_changelog_meta").fetchall())
            self._epoch, self._floor = rows["epoch"], int(rows.get("floor", 0))
            self._db = db
            return db

    def _guard(self) -> None:
        if self.may_write is not None and not self.may_write():
            raise FencedWrite("not the leader")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # -- reading ------------------------------------------------------------------------------------------------

    @property
    def epoch(self) -> str:
        self._open()
        return self._epoch

    def floor(self) -> int:
        """The smallest cursor (`after`) that is still served: entries up to it were pruned."""
        db = self._open()
        with self._lock:
            r = db.execute("SELECT value FROM mobile_changelog_meta WHERE key = 'floor'").fetchone()
            self._floor = int(r[0]) if r else 0
            return self._floor

    def head(self) -> int:
        db = self._open()
        with self._lock:
            r = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'mobile_changelog'").fetchone()
        return int(r[0]) if r else 0

    def since(self, after: int, limit: int, kinds: frozenset | None = None) -> list[Change]:
        """The entries after `after`, oldest first, up to `limit`, only of `kinds` (None: all)."""
        db = self._open()
        sql, args = "SELECT seq, kind, id, rev, at FROM mobile_changelog WHERE seq > ?", [int(after)]
        if kinds is not None:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += sorted(kinds)
        with self._lock:
            return [Change(*r) for r in db.execute(sql + " ORDER BY seq LIMIT ?", args + [int(limit)]).fetchall()]

    def heads(self, kind: str | None = None) -> dict[tuple[str, str], str]:
        db = self._open()
        with self._lock:
            rows = db.execute("SELECT kind, id, rev FROM mobile_changelog_head"
                              + (" WHERE kind = ?" if kind else ""), (kind,) if kind else ()).fetchall()
        return {(k, i): r for k, i, r in rows}

    # -- writing (the Recorder's; one transaction per batch) -------------------------------------------------------

    def append(self, entries: list[tuple[str, str, str, bool]]) -> list[Change]:
        """(kind, id, rev, settled) each, in one transaction: the entries and the heads (settled: the object is done, as
        a closed incident is: its head is dropped, so it is not looked at again). FencedWrite when this task is not the
        leader (nothing written). Entries whose rev is already the head's are skipped (a no-op change writes nothing)."""
        db = self._open()
        out: list[Change] = []
        with self._lock:
            self._guard()
            now = self.clock()
            db.execute("BEGIN IMMEDIATE")
            try:
                for kind, id_, rev, settled in entries:
                    cur = db.execute("SELECT rev FROM mobile_changelog_head WHERE kind = ? AND id = ?",
                                     (kind, id_)).fetchone()
                    if cur is not None and cur[0] == rev:
                        continue
                    if cur is None and rev == "":
                        continue
                    c = db.execute("INSERT INTO mobile_changelog (kind, id, rev, at) VALUES (?, ?, ?, ?)",
                                   (kind, id_, rev, now))
                    out.append(Change(int(c.lastrowid), kind, id_, rev, now))
                    if settled or rev == "":
                        db.execute("DELETE FROM mobile_changelog_head WHERE kind = ? AND id = ?", (kind, id_))
                    else:
                        db.execute("INSERT INTO mobile_changelog_head (kind, id, rev) VALUES (?, ?, ?) "
                                   "ON CONFLICT(kind, id) DO UPDATE SET rev = excluded.rev", (kind, id_, rev))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    try:
                        db.execute("ROLLBACK")
                    except Exception:  # noqa: BLE001
                        pass
                raise
        return out

    def baseline(self, entries: list[tuple[str, str, str]]) -> None:
        """Heads only, no entries: what exists when the log is first made. Nothing is announced that a cursor never
        had (an app takes its first cursor from a snapshot, which already has these)."""
        db = self._open()
        with self._lock:
            self._guard()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.executemany("INSERT INTO mobile_changelog_head (kind, id, rev) VALUES (?, ?, ?) "
                               "ON CONFLICT(kind, id) DO UPDATE SET rev = excluded.rev", entries)
                db.execute("INSERT OR IGNORE INTO mobile_changelog_meta (key, value) VALUES ('baselined', '1')")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def baselined(self) -> bool:
        db = self._open()
        with self._lock:
            return db.execute("SELECT 1 FROM mobile_changelog_meta WHERE key = 'baselined'").fetchone() is not None

    def prune(self, force: bool = False) -> int:
        """Entries older than the retention, and any beyond MAX_ROWS: deleted; `floor` is the highest seq gone. At most
        once an hour. Returns the rows removed."""
        now = self.clock()
        if not force and now - self._pruned_at < PRUNE_EVERY_S:
            return 0
        db = self._open()
        with self._lock:
            self._guard()
            self._pruned_at = now
            top = self.head()
            cut = db.execute("SELECT MAX(seq) FROM mobile_changelog WHERE at < ?", (now - self.retention_s,)).fetchone()[0] or 0
            if top - self.max_rows > cut:
                cut = top - self.max_rows
            if cut <= self.floor():
                return 0
            db.execute("BEGIN IMMEDIATE")
            try:
                n = db.execute("DELETE FROM mobile_changelog WHERE seq <= ?", (cut,)).rowcount
                db.execute("INSERT INTO mobile_changelog_meta (key, value) VALUES ('floor', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(cut),))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            self._floor = cut
            return n

    def reset(self) -> str:
        """The log started over (an operator's, or a restored older database): a new epoch, so every cursor is refused
        with a 409 and the apps resnapshot. Returns the new epoch."""
        db = self._open()
        with self._lock:
            self._guard()
            new = secrets.token_hex(6)
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM mobile_changelog")
                db.execute("DELETE FROM mobile_changelog_head")
                db.execute("DELETE FROM mobile_changelog_meta WHERE key IN ('floor', 'baselined')")
                db.execute("UPDATE mobile_changelog_meta SET value = ? WHERE key = 'epoch'", (new,))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            self._epoch, self._floor = new, 0
            return new


# -- who is told -----------------------------------------------------------------------------------------------

class Subscription:
    """One stream's queue of new entries: bounded. A full queue drops the subscription (`dropped`): the client reconnects
    with its Last-Event-ID and is served from the database, so a slow client never costs unbounded memory."""

    def __init__(self, size: int = SUBSCRIBER_QUEUE):
        self._q: deque[Change] = deque()
        self._size = size
        self._cv = threading.Condition()
        self.dropped = False
        self.closed = False

    def put(self, entries: list[Change]) -> None:
        with self._cv:
            if self.dropped or self.closed:
                return
            if len(self._q) + len(entries) > self._size:
                self.dropped = True
                self._q.clear()
            else:
                self._q.extend(entries)
            self._cv.notify_all()

    def get(self, timeout: float) -> list[Change]:
        with self._cv:
            if not self._q and not self.dropped and not self.closed:
                self._cv.wait(timeout)
            out = list(self._q)
            self._q.clear()
            return out

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify_all()


class Recorder:
    """Turns CAD commits into log entries, off every request thread. `follow`: [(kind, scan)], each scan(heads of that
    kind) -> {id: (rev, settled)} for the objects it follows now; an id in `heads` it no longer finds is gone (rev "").
    `active`: the `stream` flag (a poke with it off does nothing and starts nothing)."""

    def __init__(self, log: ChangeLog, follow: list[tuple[str, Callable[[dict], dict]]], active: Callable[[], bool],
                 notify: Callable[[list[Change]], None], batch_s: float = BATCH_S, say: Callable[[str], None] | None = None):
        self.log, self.follow, self.active, self.notify = log, follow, active, notify
        self.batch_s, self.say = batch_s, say or (lambda m: None)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mu = threading.Lock()
        self._said = False
        self._ready = False

    def poke(self) -> None:
        """A CAD commit happened. Cheap and never raises: a flag read, an Event.set, once a thread."""
        try:
            if self._stop.is_set() or not self.active():
                return
            if self._thread is None:
                with self._mu:
                    if self._thread is None and not self._stop.is_set():
                        self._thread = threading.Thread(target=self._run, name="v1-changelog", daemon=True)
                        self._thread.start()
            self._wake.set()
        except Exception:  # noqa: BLE001 - the CAD's commit stands whatever this does
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                break
            self._stop.wait(self.batch_s)               # the batch window: a burst of commits is one write
            self._wake.clear()
            self.flush()

    def ensure_baseline(self) -> None:
        """The first reader of the log (the first /changes or /stream with the flag on) makes the baseline: the heads of
        what is on the board now, once. Without it the first commit after the flag went on would be taken as the state
        to start from, and its change never announced. One write of about a head row per open call and unit; every later
        write is the recorder's."""
        if self._ready:
            return
        with self._mu:
            if self._ready:
                return
            if not self.log.baselined():
                heads = self.log.heads()
                seen = {}
                for kind, scan in self.follow:
                    for id_, got in scan({i: r for (k, i), r in heads.items() if k == kind}).items():
                        seen[(kind, id_)] = got
                self.log.baseline([(k, i, r) for (k, i), (r, done) in seen.items() if r and not done])
            self._ready = True

    def flush(self) -> list[Change]:
        """Compare what is followed with the heads and append what differs. Never raises."""
        try:
            if not self.active():
                return []
            heads = self.log.heads()
            seen: dict[tuple[str, str], tuple[str, bool]] = {}
            for kind, scan in self.follow:
                mine = {i: r for (k, i), r in heads.items() if k == kind}
                for id_, got in scan(mine).items():
                    seen[(kind, id_)] = got
            if not self.log.baselined():
                self.log.baseline([(k, i, r) for (k, i), (r, done) in seen.items() if r and not done])
                return []
            changes = [(k, i, r, done) for (k, i), (r, done) in seen.items() if heads.get((k, i), "") != r]
            out = self.log.append(changes) if changes else []
            self.log.prune()
            if out:
                self.notify(out)
            return out
        except FencedWrite:
            return []                                    # not the leader: nothing written, nothing to say
        except Exception as e:  # noqa: BLE001 - a failure here is the log's, never the CAD's
            if not self._said:
                self._said = True
                try:
                    self.say(f"api v1: the change log could not be written ({type(e).__name__})")
                except Exception:  # noqa: BLE001
                    pass
            return []

    def stop(self) -> None:
        """The hub is draining: the last batch is written (so the next task's cursor has it), the thread ends."""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            if t is not threading.current_thread():
                t.join(5)
            self.flush()


# -- the CAD's objects as references ----------------------------------------------------------------------------

OPEN = ("queued", "pending", "active")


def cad_follow(store) -> list[tuple[str, Callable[[dict], dict]]]:
    """Incidents and units of a CAD store (anything with `q(sql, args) -> [dict]`: sizeup/cad/store.py), as what
    Recorder follows. Only open incidents (and the ones that were open at the last look, so their closing is seen) are
    read: the cost is the open calls and the board, never the whole history. The rev hashes the row and what hangs
    off it (its last event, its assignments), so a note or an assignment is a change of the incident."""
    def incidents(heads: dict) -> dict:
        ids = json.dumps([int(i) for i in heads if str(i).isdigit()])
        rows = store.q(
            "SELECT i.*, (SELECT MAX(e.id) FROM events e WHERE e.incident_id = i.id) AS last_event, "
            "(SELECT group_concat(a.unit_id || ':' || IFNULL(a.dispatched_at, '') || ':' || IFNULL(a.enroute_at, '') "
            "|| ':' || IFNULL(a.onscene_at, '') || ':' || IFNULL(a.cleared_at, ''), ',') FROM assignments a "
            "WHERE a.incident_id = i.id) AS assigned "
            f"FROM incidents i WHERE i.status IN ({','.join('?' * len(OPEN))}) "
            "OR i.id IN (SELECT value FROM json_each(?))", (*OPEN, ids))
        out = {str(r["id"]): (rev_of(r), r["status"] not in OPEN) for r in rows}
        for i in heads:
            out.setdefault(i, ("", True))                # it was followed and is gone
        return out

    def units(heads: dict) -> dict:
        rows = store.q("SELECT u.*, (SELECT group_concat(a.incident_id) FROM assignments a "
                       "WHERE a.unit_id = u.id AND a.cleared_at IS NULL) AS on_incident FROM units u")
        out = {str(r["id"]): (rev_of(r), False) for r in rows}
        for i in heads:
            out.setdefault(i, ("", True))
        return out
    return [("incident", incidents), ("unit", units)]


class Feed:
    """What /api/v1/changes and /api/v1/stream share: the log, the recorder that fills it and the subscribers. Built once
    by the hub (server.py) and handed to V1. `visible(principal, change) -> bool` narrows a scope per person (the
    default: every signed-in reader sees the scope's kinds)."""

    def __init__(self, log: ChangeLog, follow: list | None = None, active: Callable[[], bool] = lambda: True,
                 visible: Callable[[object, Change], bool] | None = None, scope: Scope = MOBILE,
                 fence_ok: Callable[[], bool] | None = None, say=None, batch_s: float = BATCH_S,
                 heartbeat_s: float = 15.0, tick_s: float = 1.0, queue: int = SUBSCRIBER_QUEUE):
        self.log, self.scope, self.visible, self.fence_ok = log, scope, visible, fence_ok
        self.heartbeat_s, self.tick_s, self.queue = heartbeat_s, tick_s, queue
        self._subs: set[Subscription] = set()
        self._mu = threading.Lock()
        self.recorder = Recorder(log, follow or [], active, self._notify, batch_s, say)

    def poke(self) -> None:
        self.recorder.poke()

    def ready(self) -> None:
        self.recorder.ensure_baseline()

    def attach(self, store) -> None:
        """Hook a CAD store's commits (sizeup/cad/store.py commit_listeners): the one line that connects the CAD."""
        store.commit_listeners.append(self.poke)

    def subscribe(self) -> Subscription:
        s = Subscription(self.queue)
        with self._mu:
            self._subs.add(s)
        return s

    def unsubscribe(self, s: Subscription) -> None:
        with self._mu:
            self._subs.discard(s)
        s.close()

    def _notify(self, entries: list[Change]) -> None:
        with self._mu:
            subs = list(self._subs)
        for s in subs:
            s.put(entries)

    def allowed(self, principal, c: Change) -> bool:
        """May this person be told of this reference: in the consumer's scope, and the per-person predicate."""
        if c.kind not in self.scope.kinds:
            return False
        return True if self.visible is None else bool(self.visible(principal, c))

    def writable(self) -> bool:
        return self.fence_ok is None or bool(self.fence_ok())

    def close(self) -> None:
        self.recorder.stop()
        with self._mu:
            subs = list(self._subs)
        for s in subs:
            s.close()
        self.log.close()
