"""The hub app's side of the single-writer lock (GitHub #247, sizeup/handoff.py, sizeup/leaderlock.py).

The supervisor (sizeup/handoff.py, the hub container's main process) holds the lock and renews it. Each renew
it writes the lease file the app is started with (SIZEUP_LEASE_FILE):

    {"generation": 7, "valid_until": <time.monotonic() until which we may write>, "holder": ..., "litestream_pid": 123}

CLOCK_MONOTONIC is the same clock for every process of the container, so the app needs no clock of its own.

    Fence.from_env()   None without SIZEUP_LEASE_FILE (a station, a Mac, the tests, the hub in compose)
    fence.ok()         the lease is ours and fresh: writes may go ahead (read from the file at most every 0.5 s)
    fence.watch()      a thread: the moment ok() turns False it kills Litestream (its pid from the file) and the
                       process itself (os._exit(75)): a stale writer never writes on. This is the second line:
                       the supervisor kills both first, but this holds even if the supervisor were wedged.

The web server refuses a write (POST) with 503 while not ok() (sizeup/web/server.py), the map caches' S3 copies
aren't uploaded (sizeup/cachesync.py).

GETs are still served, and some of them write as a side effect (the activity log, a first sign-in's operator row,
History here's address keys: #501). The databases' own connections (cad.sqlite: sizeup/cad/store.py, hub.sqlite and
audio.sqlite: sizeup/cloud/hubstore.py) are a GuardedConnection: while its may_write() (the App sets it to fence.ok)
says False every write statement is skipped and a COMMIT is a ROLLBACK, whoever asked for it. Reads go on.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

EXIT_FENCED = 75
READ_EVERY_S = 0.5
SKIP_LOG_EVERY_S = 60.0


def _always() -> bool:
    return True


# The first word of a statement (after comments): these only read, or open / end a transaction (COMMIT: below).
_FIRST = re.compile(r"\s*(?:(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*)*([A-Za-z]+)", re.S)
_READS = frozenset(("SELECT", "VALUES", "EXPLAIN", "BEGIN", "ROLLBACK", "SAVEPOINT", "RELEASE"))
_ENDS = frozenset(("COMMIT", "END"))
_CTE_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
# PRAGMAs that write the file without "=", and those that do with it (the connection's own settings, busy_timeout,
# foreign_keys, query_only ..., change nothing on disk)
_PRAGMA = re.compile(r"\s*PRAGMA\s+(?:\w+\.)?(\w+)\s*(=|\()?", re.I)
_PRAGMA_WRITES = frozenset(("incremental_vacuum", "wal_checkpoint", "optimize"))
_PRAGMA_SET = frozenset(("user_version", "application_id", "schema_version", "journal_mode", "auto_vacuum", "page_size",
                         "writable_schema", "max_page_count"))


def is_write(sql: str) -> bool:
    """Would this statement change the database file? An unknown first word counts as a write."""
    m = _FIRST.match(sql)
    word = m.group(1).upper() if m else ""
    if word in _READS or word in _ENDS:
        return False
    if word == "WITH":
        return bool(_CTE_WRITE.search(sql))
    if word == "PRAGMA":
        p = _PRAGMA.match(sql)
        if p is None:
            return True
        name = p.group(1).lower()
        return name in _PRAGMA_WRITES or (bool(p.group(2)) and name in _PRAGMA_SET)
    return True


class _Skipped:
    """What a skipped write returns: a cursor that changed nothing."""
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
    says False: an INSERT / UPDATE / DELETE / DDL / writing PRAGMA is skipped (a cursor with rowcount 0 comes back,
    nothing is raised: a GET still answers), a COMMIT rolls back (a transaction begun while the lease was ours keeps
    nothing), an executescript is skipped whole. may_write is asked only for a write or a COMMIT: a read costs nothing
    more. log: told of a skip at most once a minute (the statement's first words, never its values)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.may_write: Callable[[], bool] = _always
        self.log: Callable[[str], None] | None = None
        self.skipped = 0
        self._said = -1e18

    def _skip(self, sql: str) -> bool:
        """True (and counted, maybe logged) when a write must not go ahead."""
        if self.may_write():
            return False
        self.skipped += 1
        now = time.monotonic()
        if self.log is not None and now - self._said >= SKIP_LOG_EVERY_S:
            self._said = now
            try:
                self.log(f"fence: not the leader: a write skipped ({' '.join(sql.split()[:3])[:60]}; "
                         f"{self.skipped} so far on this database)")
            except Exception:  # noqa: BLE001 - never in the way of the request
                pass
        return True

    def execute(self, sql, parameters=(), /):
        m = _FIRST.match(sql)
        if m is not None and m.group(1).upper() in _ENDS:
            if self.in_transaction and self._skip(sql):
                return super().execute("ROLLBACK")
        elif is_write(sql) and self._skip(sql):
            return SKIPPED
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        if is_write(sql) and self._skip(sql):
            return SKIPPED
        return super().executemany(sql, parameters)

    def executescript(self, script, /):
        if self._skip(script):
            return SKIPPED
        return super().executescript(script)

    def commit(self) -> None:
        if self.in_transaction and self._skip("COMMIT"):
            return super().rollback()
        return super().commit()


class Fence:
    def __init__(self, path: str | Path, mono: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print,
                 exit_fn: Callable[[int], None] = os._exit, kill: Callable[[int, int], None] = os.kill):
        self.path = Path(path)
        self.mono, self.log, self.exit_fn, self.kill = mono, log, exit_fn, kill
        self._read_at = -1e9
        self._lease: dict = {}
        self._mu = threading.Lock()
        self.fenced = False

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
            return int(d.get("generation") or 0) > 0 and self.mono() < float(d.get("valid_until") or 0)
        except (TypeError, ValueError):
            return False

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
