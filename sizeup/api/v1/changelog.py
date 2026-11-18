"""The change log (docs/mobile-api.md section 3.4, #561): one append-only sequence of REFERENCES `(seq, epoch, kind, id,
rev, at)`, built once and shared by /api/v1/changes, /api/v1/stream and the planned machine feed (each reads it with
its own kinds). A reference carries no payload, no position and no transcript: the app fetches the object (with its
own role's filter) when it sees a new rev. `rev` is a hash of the whole row (rev.py): it is no more than a
confirmation oracle for a row someone could already guess.

Storage: hub.sqlite only (the log never writes to cad.sqlite: it only reads it), three tables created on first use
while the `stream` flag is on (a hub with mobile off never creates them), through their own connection behind the fence
(a fenced task writes nothing):

    mobile_changelog       seq (AUTOINCREMENT: never reused), kind, id, rev, at
    mobile_changelog_head  the last rev logged per (kind, id): what a change is measured against (rebuilt at each start)
    mobile_changelog_meta  floor (the highest seq pruned)

EPOCH: a new random epoch at every start of the log (one per process, held in memory). A cursor is `<epoch>-<seq>`,
so a cursor from an earlier run answers 409 and the app resnapshots. That is what makes a restore safe without any
check: after a crash failover Litestream may give back a hub.sqlite a second behind, with a lower `sqlite_sequence`,
and the next process would hand out seqs a stream already sent; under a new epoch no old cursor can be mistaken for a
new one. The cost: every hub restart or deploy makes connected apps resnapshot once. Rows of an earlier epoch stay
until retention prunes them; a new epoch's first seq is above them (`start_seq`), so nothing of them is ever served
again.

Writes: a CAD commit only pokes the Recorder (an Event.set; never a lock a request thread can hold). Its one thread
wakes on a poke, waits BATCH_S so the commits of a busy minute become one transaction, and appends what changed
since the last rev it logged. Nothing runs on a request thread (a GET never writes: the first reader is told 503
`unavailable` + Retry-After 1 while the recorder makes the tables and the baseline: the heads of what is on the board
now), nothing at startup, and with the flag off a poke returns at once.

Why `Broadcaster.poke` (the console's state stream, sizeup/web/server.py) is not reused: its ids reset on every handoff,
it holds no history and its payload is the UI blob for everyone (docs/mobile-api.md section 3.4).

LOCK ORDER (a CAD commit calls poke() while holding the CAD's `store.lock`, so nothing poke touches may ever be held
by a thread that is waiting for `store.lock`):

    store.lock  ->  Recorder._start (poke: leaf, held only to start the thread; never across a store call)
    Recorder._base  ->  store.lock   (the baseline's scan; poke never takes _base)
    ChangeLog._lock  (the log's connection: leaf, nothing is called from under it but sqlite)
    Feed._mu, Subscription._cv   (leaves)

Cost (tests/test_api_v1_changes.py): about one 4 KB page per change in the log plus its head row, in one transaction
per batch; a change of an object that keeps its rev writes nothing. Each flush also reads the open incidents and the
board (a few small queries, hashed outside the CAD's lock: never every chronology row), skipped when the CAD has not
committed since the last one.

Retention: RETENTION_S (7 days) and at most MAX_ROWS rows, pruned an hour at a time in passes of PRUNE_ROWS; a cursor
older than what is kept (`floor`) is a 410 and the app resnapshots. A deletion is a row whose rev is ""."""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, NamedTuple

from sizeup.fence import FencedWrite, GuardedConnection

from .rev import rev_of
from .serialize import incident_summary, unit

RETENTION_S = 7 * 86400
MAX_ROWS = 100_000
# rows deleted per pass (one transaction, so a prune never writes a table's worth at once)
PRUNE_ROWS = 5000
BATCH_S = 1.0                  # a poke waits this long so a burst of commits is one write
PRUNE_EVERY_S = 3600.0
SUBSCRIBER_QUEUE = 256         # one stream's queue: past it the stream is dropped (it resumes from the database)
# the stream's timing, in one place
HEARTBEAT_S = 15.0
TICK_S = 1.0                   # how often an open stream looks at its stop conditions
RETRY_MS = 3000                # EventSource's reconnect delay (the console's is 2000: a phone's radio is dearer)
STREAM_WRITE_S = 10.0          # a client that takes no bytes for this long is dropped (it resumes by id)
BACKLOG_ROWS = 500
VALID_EVERY_S = 5.0            # a stream asks whether its device is still valid this often

MOBILE_KINDS = frozenset({"incident", "unit"})

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


def public(c: Change, epoch: str) -> dict:
    """The only fields a consumer ever gets: a reference, nothing else."""
    return {"seq": c.seq, "epoch": epoch, "kind": c.kind, "id": c.id, "rev": c.rev, "at": c.at}


class ChangeLogClosed(RuntimeError):
    """The log was closed (the hub is draining): it is not opened again."""


class ChangeLog:
    """The log itself. `path`: hub.sqlite. may_write: the fence's ok() (None: always). Opened and created on first
    use."""

    def __init__(self, path: str | Path, may_write: Callable[[], bool] | None = None, clock=time.time,
                 retention_s: float = RETENTION_S, max_rows: int = MAX_ROWS):
        self.path, self.may_write, self.clock = str(path), may_write, clock
        self.retention_s, self.max_rows = retention_s, max_rows
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._closed = False
        self._epoch = secrets.token_hex(6)    # new at every start (see the module docstring)
        self._start_seq = 0                   # the head when this run opened the log: its entries all come after it
        self._pruned_at = 0.0
        self.commits = 0                      # transactions committed through this log (a test counts them)

    # -- opening ------------------------------------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise ChangeLogClosed("the change log is closed")
            if self._db is not None:
                return self._db
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, factory=GuardedConnection)
            if self.may_write is not None:
                db.may_write = self.may_write
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA busy_timeout = 5000")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'mobile_changelog_meta'").fetchone():
                self._guard()
                db.executescript(SCHEMA)        # (a fenced task raises here: nothing made, and tried again later)
            r = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'mobile_changelog'").fetchone()
            self._start_seq = int(r[0]) if r else 0
            self._db = db
            return db

    def _guard(self) -> None:
        if self.may_write is not None and not self.may_write():
            raise FencedWrite("not the leader")

    @contextmanager
    def _txn(self):
        """One write transaction under the log's lock: refused when fenced, rolled back when the body raises."""
        db = self._open()
        with self._lock:
            self._guard()
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
                self.commits += 1
            except BaseException:
                if db.in_transaction:
                    try:
                        db.execute("ROLLBACK")
                    except Exception:  # noqa: BLE001 - the body's error is the one raised
                        pass
                raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._db is not None:
                self._db.close()
                self._db = None

    # -- reading ------------------------------------------------------------------------------------------------

    @property
    def epoch(self) -> str:
        return self._epoch

    def _meta(self, key: str) -> str | None:
        db = self._open()
        with self._lock:
            r = db.execute("SELECT value FROM mobile_changelog_meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def floor(self) -> int:
        """The smallest cursor (`after`) that is still served: entries up to it were pruned, or are an earlier run's."""
        self._open()
        return max(int(self._meta("floor") or 0), self._start_seq)

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

    def heads(self) -> dict[tuple[str, str], str]:
        db = self._open()
        with self._lock:
            rows = db.execute("SELECT kind, id, rev FROM mobile_changelog_head").fetchall()
        return {(k, i): r for k, i, r in rows}

    # -- writing (the Recorder's; one transaction per batch) -------------------------------------------------------

    @staticmethod
    def _put_meta(db, key: str, value: str) -> None:
        db.execute("INSERT INTO mobile_changelog_meta (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def append(self, entries: list[tuple[str, str, str | None]]) -> list[Change]:
        """(kind, id, rev) each, in one transaction: the entries and the heads. rev "" is a deletion; rev None only
        drops the head (an object that left the followed window, as a long-closed incident: no entry). FencedWrite when
        this task is not the leader (nothing written). Entries whose rev is already the head's are skipped."""
        out: list[Change] = []
        with self._txn() as db:
            now = self.clock()
            for kind, id_, rev in entries:
                if rev is None:
                    db.execute("DELETE FROM mobile_changelog_head WHERE kind = ? AND id = ?", (kind, id_))
                    continue
                cur = db.execute("SELECT rev FROM mobile_changelog_head WHERE kind = ? AND id = ?",
                                 (kind, id_)).fetchone()
                if cur is not None and cur[0] == rev or cur is None and rev == "":
                    continue
                c = db.execute("INSERT INTO mobile_changelog (kind, id, rev, at) VALUES (?, ?, ?, ?)",
                               (kind, id_, rev, now))
                out.append(Change(int(c.lastrowid), kind, id_, rev, now))
                if rev == "":
                    db.execute("DELETE FROM mobile_changelog_head WHERE kind = ? AND id = ?", (kind, id_))
                else:
                    db.execute("INSERT INTO mobile_changelog_head (kind, id, rev) VALUES (?, ?, ?) "
                               "ON CONFLICT(kind, id) DO UPDATE SET rev = excluded.rev", (kind, id_, rev))
        return out

    def baseline(self, entries: list[tuple[str, str, str]]) -> None:
        """The heads of what exists now, no entries (rebuilt at each start: an earlier run's heads may be from a
        restored database). Nothing is announced that a cursor never had: an app takes its first cursor at the head."""
        with self._txn() as db:
            db.execute("DELETE FROM mobile_changelog_head")
            db.executemany("INSERT INTO mobile_changelog_head (kind, id, rev) VALUES (?, ?, ?)", entries)

    def prune(self, force: bool = False) -> int:
        """Entries older than the retention, and any beyond MAX_ROWS: deleted, at most PRUNE_ROWS a pass (a pass that
        finds more leaves the rest to the next flush); `floor` is the highest seq gone. At most once an hour.
        Returns the rows removed."""
        now = self.clock()
        if not force and now - self._pruned_at < PRUNE_EVERY_S:
            return 0
        with self._lock:
            floor = int(self._meta("floor") or 0)
            db = self._open()
            old = db.execute("SELECT MAX(seq) FROM mobile_changelog WHERE at < ?", (now - self.retention_s,)).fetchone()
            cut = old[0] or 0
            cut = max(cut, self.head() - self.max_rows)
            if cut <= floor:
                self._pruned_at = now
                return 0
            step = db.execute("SELECT MAX(seq) FROM (SELECT seq FROM mobile_changelog WHERE seq <= ? "
                              "ORDER BY seq LIMIT ?)", (cut, PRUNE_ROWS)).fetchone()[0] or cut
            with self._txn() as db:
                n = db.execute("DELETE FROM mobile_changelog WHERE seq <= ?", (step,)).rowcount
                self._put_meta(db, "floor", str(step))
            if step >= cut:
                self._pruned_at = now                      # (done; else the next flush goes on)
            return n

    def reset(self) -> str:
        """Start a new epoch now (an operator's, in code): every cursor is refused with a 409 and the apps resnapshot.
        Nothing is deleted: the entries so far are never served again (`start_seq`). Returns the new epoch."""
        with self._lock:
            self._epoch = secrets.token_hex(6)
            self._start_seq = self.head()
            return self._epoch


# -- who is told -----------------------------------------------------------------------------------------------

class Subscription:
    """One stream's queue of new entries: bounded. A full queue drops the subscription (`dropped`): the client
    reconnects with its Last-Event-ID and is served from the database, so a slow client never costs unbounded memory."""

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
    kind) -> {id: rev} for the objects it follows now (rev "": gone; None: left the followed window, head dropped).
    `active`: the `stream` flag (a poke with it off does nothing and starts nothing).
    `version`: the CAD's commit counter, so a flush with no commit since the last reads nothing."""

    def __init__(self, log: ChangeLog, follow: list[tuple[str, Callable[[dict], dict]]], active: Callable[[], bool],
                 notify: Callable[[list[Change]], None], batch_s: float = BATCH_S,
                 report: Callable[[str], None] | None = None, version: Callable[[], int] | None = None):
        self.log, self.follow, self.active, self.notify = log, follow, active, notify
        self.batch_s, self.report = batch_s, report or (lambda m: None)
        self.version = version
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = threading.Lock()        # poke's, held only to start the thread (see LOCK ORDER)
        self._base = threading.Lock()         # the baseline's, held across the CAD scan: poke never takes it
        self._ready = False
        self._said = False
        self._seen_version: int | None = None

    def poke(self) -> None:
        """A CAD commit happened (called with the CAD's lock held). Cheap and never raises: a flag read, an Event.set,
        once a thread. It takes only `_start`, which nobody holds across a call into the CAD."""
        try:
            if self._stop.is_set() or not self.active():
                return
            if self._thread is None:
                with self._start:
                    if self._thread is None and not self._stop.is_set():
                        t = threading.Thread(target=self._run, name="v1-changelog", daemon=True)
                        t.start()
                        self._thread = t
            self._wake.set()
        except Exception:  # noqa: BLE001 - the CAD's commit stands whatever this does
            pass

    @property
    def ready(self) -> bool:
        """The log has its baseline and has been checked against the CAD (a reader is served only then)."""
        return self._ready

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                break
            self._stop.wait(self.batch_s)               # the batch window: a burst of commits is one write
            self._wake.clear()
            self.flush()

    def _scan(self, heads: dict) -> dict[tuple[str, str], str | None]:
        seen: dict[tuple[str, str], str | None] = {}
        for kind, scan in self.follow:
            for id_, got in scan({i: r for (k, i), r in heads.items() if k == kind}).items():
                seen[(kind, id_)] = got
        return seen

    def prepare(self) -> None:
        """Once per run: the log's tables and baseline (the heads of what is on the board now), so the first commit
        after the flag went on is a change and not the state to start from. Runs on the recorder's thread (a GET never
        writes); holds `_base` across the CAD scan, which poke never takes."""
        with self._base:
            if self._ready:
                return
            self.log.baseline([(k, i, r) for (k, i), r in self._scan({}).items() if r])
            self._ready = True

    def flush(self) -> list[Change]:
        """Compare what is followed with the heads and append what differs. Never raises."""
        try:
            if not self.active():
                return []
            self.prepare()
            v = self.version() if self.version is not None else None     # (before the scan: a commit during it is next)
            if v is not None and v == self._seen_version:
                return []
            heads = self.log.heads()
            seen = self._scan(heads)
            changes = [(k, i, r) for (k, i), r in seen.items()
                       if heads.get((k, i), "") != r and (r is not None or (k, i) in heads)]
            out = self.log.append(changes) if changes else []
            self._seen_version = v
            self.log.prune()
            if out:
                self.notify(out)
            return out
        except FencedWrite:
            self._seen_version = None
            return []                                    # not the leader: nothing written, nothing to say
        except ChangeLogClosed:
            return []
        except Exception as e:  # noqa: BLE001 - a failure here is the log's, never the CAD's
            self._seen_version = None
            if not self._said:
                self._said = True
                try:
                    self.report(f"api v1: the change log could not be written ({type(e).__name__})")
                except Exception:  # noqa: BLE001
                    pass
            return []

    def stop(self) -> None:
        """The hub is draining: the last batch is written, the thread ends."""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is None:
            return
        if t is not threading.current_thread():
            t.join(5)
        self.flush()


# -- the CAD's objects as references ----------------------------------------------------------------------------

def cad_follow(store) -> list[tuple[str, Callable[[dict], dict]]]:
    """Incidents and units of a CAD store (sizeup/cad/store.py), as what Recorder follows. The rev of each is the
    REV THE READS SERVE (docs/mobile-api.md section 3.4): the hash of reads' own serializers' output
    (serialize.incident_summary and serialize.unit through rev_of, as with_rev does), so a log entry's (kind, id, rev)
    equals the rev of that object in GET /api/v1/incidents (list items and the snapshot) and /api/v1/units. It is
    never a hash of a raw row. The detail GET /api/v1/incidents/{id} serves a different object, so its rev is its own.
    Incidents are the ones the reads list (open, and closed in the last day). The scan is lean: store.incidents_lean()
    reads the incident rows, their assignments and the newest chronology id of each, never the chronologies; the rows
    come from the store under its lock and are serialized and hashed after it is released."""
    def incidents(heads: dict) -> dict:
        out = {str(i["id"]): rev_of(incident_summary(i)) for i in store.incidents_lean()}
        for i in heads:
            out.setdefault(i, None)                      # it left the window (closed long ago): not a deletion
        return out

    def units(heads: dict) -> dict:
        out = {str(u["id"]): rev_of(unit(u)) for u in store.units()}
        for i in heads:
            out.setdefault(i, "")                        # it was followed and is gone
        return out
    return [("incident", incidents), ("unit", units)]


class Feed:
    """What /api/v1/changes and /api/v1/stream share: the log, the recorder that fills it and the subscribers. Built
    once by the hub (server.py) and handed to V1.

    visible(principal, change) -> bool: may this person be told of this reference. FAILS CLOSED: with none supplied
    nothing is shown and the routes answer 503 `unavailable`, so the sign-in work (#558, PR 2) must supply it from the
    person's role and sourcekeys before the `stream` flag is turned on. valid(principal) -> bool: is the device still
    signed in (a revoked one is cut off mid-stream). Both fail closed: with either missing the routes answer 503."""

    def __init__(self, log: ChangeLog, follow: list | None = None, active: Callable[[], bool] = lambda: True,
                 visible: Callable[[object, Change], bool] | None = None, kinds: frozenset = MOBILE_KINDS,
                 fence_ok: Callable[[], bool] | None = None, report=None, batch_s: float = BATCH_S,
                 heartbeat_s: float = HEARTBEAT_S, tick_s: float = TICK_S, queue: int = SUBSCRIBER_QUEUE,
                 valid: Callable[[object], bool] | None = None, version: Callable[[], int] | None = None):
        self.log, self.kinds, self.visible, self.fence_ok = log, kinds, visible, fence_ok
        self.valid = valid
        self.heartbeat_s, self.tick_s, self.queue = heartbeat_s, tick_s, queue
        self._subs: set[Subscription] = set()
        self._mu = threading.Lock()
        self.recorder = Recorder(log, follow or [], active, self._notify, batch_s, report, version)

    def poke(self) -> None:
        self.recorder.poke()

    def bind(self, store) -> None:
        """Tie the recorder to a CAD store's commit counter (the gate that skips a flush with no commit since)."""
        self.recorder.version = self.recorder.version or (lambda: store.rev)

    def attach(self, store) -> None:
        """Bind, and hook the store's commits (store.py commit_listeners): the one line that connects the CAD."""
        self.bind(store)
        store.commit_listeners.append(self.poke)

    def ready(self) -> bool:
        """May a reader be served: the baseline exists. If not, the recorder is woken to make it (a GET never
        writes)."""
        if self.recorder.ready:
            return True
        self.recorder.poke()
        return False

    def serving(self) -> bool:
        """Both a visibility predicate and a device check are supplied: else the feed is not served (fail closed)."""
        return self.visible is not None and self.valid is not None

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
        """May this person be told of this reference: a kind this consumer reads, and the person's predicate says so."""
        return c.kind in self.kinds and self.visible is not None and bool(self.visible(principal, c))

    def writable(self) -> bool:
        return self.fence_ok is None or bool(self.fence_ok())

    def close(self) -> None:
        self.recorder.stop()
        with self._mu:
            subs = list(self._subs)
        for s in subs:
            s.close()
        self.log.close()
