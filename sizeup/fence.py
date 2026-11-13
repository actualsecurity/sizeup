"""The hub app's side of the single-writer lock (GitHub #247, sizeup/handoff.py, sizeup/leaderlock.py).

The supervisor (sizeup/handoff.py, the hub container's main process) holds the lock and renews it. Each renew
it writes the lease file the app is started with (SIZEUP_LEASE_FILE):

    {"generation": 7, "valid_until": <time.monotonic() until which we may write>, "holder": ..., "litestream_pid": 123}

CLOCK_MONOTONIC is the same clock for every process of the container, so the app needs no clock of its own.

    Fence.from_env()   None without SIZEUP_LEASE_FILE (a station, a Mac, the tests, the hub in compose)
    fence.ok()         the lease is ours and fresh: writes may go ahead (read from the file at most every 0.5 s).
                       Sticky (#501): once it has been ours and then isn't, it stays False for this process (a late
                       renew never lets a write in again: a takeover or a retake is a new process). One per process:
                       main / run_gated make it and hand it to serve -> the App, the Gate, the databases' guards.
    fence.watch()      a thread: the moment ok() turns False it kills Litestream (its pid from the file) and the
                       process itself (os._exit(75)): a stale writer never writes on. This is the second line:
                       the supervisor kills both first, but this holds even if the supervisor were wedged.

The web server refuses a write (POST) with 503 while not ok() (sizeup/web/server.py), the map caches' S3 copies
aren't uploaded (sizeup/cachesync.py).

Every write to the hub's databases goes through a GuardedConnection (#501): cad.sqlite (sizeup/cad/store.py),
hub.sqlite and audio.sqlite (sizeup/cloud/hubstore.py). While its may_write() (the App sets it to fence.ok) says
False, a write is never made, and never silently:

    incoming data     (the default) FencedWrite is raised: the request answers 503 + Retry-After (sizeup/web/server.py),
                      the station's outbox keeps the event and sends it again (to the next leader); the hub's ingest
                      acks nothing a refused write touched (sizeup/cloud/hub.py Hub._events). Every refusal is also
                      counted per thread (refused()), so one swallowed by an `except Exception` on the way still turns
                      the answer into a 503: no data path answers 2xx after a write was refused.
    bookkeeping       a GET's side effects (the activity log, last seen, an attachment view's audit) inside
                      `with bookkeeping():`: skipped, said in the log once a minute, nothing raised.

A COMMIT while fenced is a ROLLBACK (then FencedWrite, outside bookkeeping): a transaction the lease ran out in keeps
nothing. Reads go on.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

EXIT_FENCED = 75
READ_EVERY_S = 0.5
SKIP_LOG_EVERY_S = 60.0


def _always() -> bool:
    return True


class FencedWrite(RuntimeError):
    """A write refused: this task isn't the leader (its lease isn't ours and fresh). Nothing was written; the caller
    must not say it was (the web server answers 503 + Retry-After)."""


_tl = threading.local()


@contextmanager
def bookkeeping():
    """Writes in here are bookkeeping (a GET's side effects): on a fenced task they're skipped, not refused."""
    depth = getattr(_tl, "book", 0)
    _tl.book = depth + 1
    try:
        yield
    finally:
        _tl.book = depth


def in_bookkeeping() -> bool:
    return getattr(_tl, "book", 0) > 0


def refused() -> int:
    """How many writes this thread has had refused (outside bookkeeping), ever: a request compares it before and after."""
    return getattr(_tl, "refused", 0)


def refuse(what: str) -> None:
    """Count a refused write on this thread and raise FencedWrite."""
    _tl.refused = refused() + 1
    raise FencedWrite(f"not the leader: {what} refused (nothing written)")


# The first word of a statement (after comments): these only read, or open / end a transaction (the ends: below).
_FIRST = re.compile(r"\s*(?:(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*)*([A-Za-z]+)", re.S)
_READS = frozenset(("SELECT", "VALUES", "EXPLAIN", "BEGIN", "SAVEPOINT", "DETACH"))
_ENDS = frozenset(("COMMIT", "END", "RELEASE"))     # RELEASE: of the outermost savepoint it is a COMMIT
# What a statement says once its strings, quoted names and comments are blanked out (a "delete" in a text, a column
# named "update", replace() in a SELECT are no writes): the words outside any parentheses decide.
_NOISE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/", re.S)
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|[()]")
_MAIN = frozenset(("SELECT", "VALUES", "INSERT", "UPDATE", "DELETE", "REPLACE"))
_TEMP = re.compile(r"\s*CREATE\s+(?:TEMP|TEMPORARY)\s", re.I)
# PRAGMAs that write the file without "=", and those that do with it (the connection's own settings, busy_timeout,
# foreign_keys, query_only ..., change nothing on disk)
_PRAGMA = re.compile(r"\s*PRAGMA\s+(?:\w+\.)?(\w+)\s*(=|\()?", re.I)
_PRAGMA_WRITES = frozenset(("incremental_vacuum", "wal_checkpoint", "optimize"))
_PRAGMA_SET = frozenset(("user_version", "application_id", "schema_version", "journal_mode", "auto_vacuum", "page_size",
                         "writable_schema", "max_page_count"))


def _first(sql: str) -> str:
    m = _FIRST.match(sql)
    return m.group(1).upper() if m else ""


def _cte_main(sql: str) -> str:
    """WITH ...: the statement the CTEs are for (its first word outside every parenthesis after them)."""
    depth = 0
    for t in _TOKEN.findall(_NOISE.sub(" ", sql)):
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        elif depth == 0 and t.upper() in _MAIN:
            return t.upper()
    return ""


def is_write(sql: str) -> bool:
    """Would this statement change the database file? An unknown first word counts as a write.

    Known limits, all on the safe side: a write to a TEMP table counts as a write (CREATE TEMP itself doesn't);
    ATTACH counts (it may create a file); a ROLLBACK, a DETACH, a SAVEPOINT don't."""
    word = _first(sql)
    if word in _READS or word in _ENDS or word == "ROLLBACK":
        return False
    if word == "WITH":
        return _cte_main(sql) not in ("SELECT", "VALUES")
    if word == "CREATE" and _TEMP.match(_NOISE.sub(" ", sql)):
        return False
    if word == "PRAGMA":
        p = _PRAGMA.match(_NOISE.sub(" ", sql))
        if p is None:
            return True
        name = p.group(1).lower()
        return name in _PRAGMA_WRITES or (bool(p.group(2)) and name in _PRAGMA_SET)
    return True


class _Skipped:
    """What a skipped bookkeeping write returns: a cursor that changed nothing."""
    rowcount = 0
    lastrowid = None
    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def __iter__(self):
        return iter(())

    def close(self) -> None:
        pass


SKIPPED = _Skipped()


class GuardedConnection(sqlite3.Connection):
    """A connection that writes only while may_write() (#501): `sqlite3.connect(path, factory=GuardedConnection)`.

    may_write: the fence's ok() on the hub (the App sets it once it has the fence); always True before that, so the
    store's own opening and migrations (the takeover's --init-state work, a test's store) are never held up. While it
    says False an INSERT / UPDATE / DELETE / DDL / writing PRAGMA / executescript raises FencedWrite (after on_refuse:
    the App's fence.check(), which stops the process), or inside bookkeeping() is skipped (a cursor with rowcount 0).
    A transaction that had a write refused or skipped is tainted: whatever ends it (COMMIT, END, RELEASE, commit(),
    `with conn:`, executescript's implied COMMIT) rolls it back and refuses, even if may_write() says True again by
    then: never half a transaction. cursor() hands out a GuardedCursor: the same rules. may_write is asked only for a
    write or an end: a read costs nothing more. log: told at most once a minute (the first words, never the values)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.may_write: Callable[[], bool] = _always
        self.log: Callable[[str], None] | None = None
        self.on_refuse: Callable[[], object] | None = None
        self.skipped = self.refused = 0
        self.tainted = False
        self._said = -1e18

    def _refuse(self, sql: str) -> bool:
        """Not ours: True for a bookkeeping write (skipped), else FencedWrite (counted on this thread too)."""
        if self.in_transaction:
            self.tainted = True
        what = " ".join(sql.split()[:3])[:60]
        book = in_bookkeeping()
        if book:
            self.skipped += 1
        else:
            self.refused += 1
        now = time.monotonic()
        if self.log is not None and now - self._said >= SKIP_LOG_EVERY_S:
            self._said = now
            try:
                self.log(f"fence: not the leader: a write {'skipped' if book else 'refused'} ({what}; "
                         f"{self.skipped} skipped, {self.refused} refused so far on this database)")
            except Exception:  # noqa: BLE001 - never in the way of the request
                pass
        if book:
            return True
        if self.on_refuse is not None:
            self.on_refuse()                   # the fence's check(): Litestream killed, the process gone
        refuse(what)
        return True

    def _end_refused(self, sql: str) -> bool:
        """An end of the open transaction that mustn't commit (tainted, or not ours): rolled back, then refused."""
        if not self.in_transaction or not (self.tainted or not self.may_write()):
            return False
        super().rollback()
        self.tainted = False
        return self._refuse(sql)

    def _gate(self, sql: str) -> bool:
        """Before a statement: False = run it; True = skip it (bookkeeping); FencedWrite = refused."""
        word = _first(sql)
        if word in _ENDS:
            return self._end_refused(sql)
        if word == "ROLLBACK":
            return False
        return is_write(sql) and not self.may_write() and self._refuse(sql)

    def _after(self) -> None:
        if not self.in_transaction:
            self.tainted = False               # ended (committed clean, or rolled back)

    def execute(self, sql, parameters=(), /):
        if self._gate(sql):
            return SKIPPED
        try:
            return super().execute(sql, parameters)
        finally:
            self._after()

    def executemany(self, sql, parameters, /):
        if self._gate(sql):
            return SKIPPED
        try:
            return super().executemany(sql, parameters)
        finally:
            self._after()

    def executescript(self, script, /):
        if self._end_refused("COMMIT") or (not self.may_write() and self._refuse(script)):
            return SKIPPED
        try:
            return super().executescript(script)
        finally:
            self._after()

    def commit(self) -> None:
        if self._end_refused("COMMIT"):
            return None
        super().commit()
        self._after()

    def rollback(self) -> None:
        super().rollback()
        self.tainted = False

    def __exit__(self, et, ev, tb):
        if et is None and self._end_refused("COMMIT"):   # `with conn:` never commits half a transaction either
            return False
        try:
            return super().__exit__(et, ev, tb)
        finally:
            self._after()

    def cursor(self, factory=None):
        return super().cursor(factory or GuardedCursor)


class GuardedCursor(sqlite3.Cursor):
    """conn.cursor().execute(...): through its connection's gate, as conn.execute is."""

    def execute(self, sql, parameters=(), /):
        if self.connection._gate(sql):
            return SKIPPED
        try:
            return super().execute(sql, parameters)
        finally:
            self.connection._after()

    def executemany(self, sql, parameters, /):
        if self.connection._gate(sql):
            return SKIPPED
        try:
            return super().executemany(sql, parameters)
        finally:
            self.connection._after()

    def executescript(self, script, /):
        c = self.connection
        if c._end_refused("COMMIT") or (not c.may_write() and c._refuse(script)):
            return SKIPPED
        try:
            return super().executescript(script)
        finally:
            c._after()


class Fence:
    def __init__(self, path: str | Path, mono: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print,
                 exit_fn: Callable[[int], None] = os._exit, kill: Callable[[int, int], None] = os.kill):
        self.path = Path(path)
        self.mono, self.log, self.exit_fn, self.kill = mono, log, exit_fn, kill
        self._read_at = -1e9
        self._lease: dict = {}
        self._mu = threading.Lock()
        self.fenced = False
        self._was_ours = False

    @classmethod
    def from_env(cls, log: Callable[[str], None] = print) -> Fence | None:
        p = os.environ.get("SIZEUP_LEASE_FILE", "").strip()
        return cls(p, log=log) if p else None

    def lease(self) -> dict:
        with self._mu:
            now = self.mono()
            if now - self._read_at >= READ_EVERY_S:
                try:
                    d = json.loads(self.path.read_text())
                    self._lease = d if isinstance(d, dict) else {}
                except (OSError, ValueError):
                    self._lease = {}          # gone or torn: not ours (it is replaced atomically, so: gone)
                self._read_at = now
            return self._lease

    def ok(self) -> bool:
        if self.fenced:
            return False
        d = self.lease()
        try:
            ours = int(d.get("generation") or 0) > 0 and self.mono() < float(d.get("valid_until") or 0)
        except (TypeError, ValueError):
            ours = False
        if ours:
            self._was_ours = True
        elif self._was_ours:
            self.fenced = True                # lost once: never ours again in this process
        return ours

    def generation(self) -> int:
        try:
            return int(self.lease().get("generation") or 0)
        except (TypeError, ValueError):
            return 0

    def check(self) -> bool:
        """One look: not ok -> kill Litestream and exit. True while ok."""
        if self.ok():
            return True
        self.fenced = True
        d = self._lease
        self.log(f"fence: the lease (generation {d.get('generation')}) isn't ours or fresh any more: "
                 "stopping writes now (Litestream killed, the app exits)")
        pid = d.get("litestream_pid")
        if isinstance(pid, int) and pid > 1:
            try:
                self.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.exit_fn(EXIT_FENCED)
        return False

    def watch(self, every: float = 0.5) -> threading.Thread:
        def run() -> None:
            while self.check():
                time.sleep(every)
        t = threading.Thread(target=run, name="fence", daemon=True)
        t.start()
        return t
