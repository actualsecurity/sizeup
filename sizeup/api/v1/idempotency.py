"""Idempotency-Key on every v1 POST (docs/mobile-api.md section 8, #562): a write the app retries on a flaky network (or
replays from its offline queue) never applies twice.

A table `(principal, key) -> (fingerprint, status, body, at, owner)` kept 24 h, in hub.sqlite (never cad.sqlite),
created on the first write (a lazily made, additive table: nothing reads it while the `writes` flag is off, and an
older release ignores it). Per principal, so one device's key can never replay another's answer.

    claim()    first sight of the key: reserved (status 0 = running, with an `owner` token) and the caller does
               the work; a claim running for over STALE_RUNNING_S (its worker died) is taken over with a new owner,
               so the slow original's finish() / release() (which name the owner) change nothing;
               the same key and the same request again: the stored answer, no second application;
               the same key with another path or body: 422 (a client bug: never a silent second write);
               the same key while the first is still running: 409 `in_progress` + Retry-After.
    finish()   keeps the answer (the owner's only). release() forgets the claim (the work raised, or the answer
               was not a decision: a 400 or a 404 is retried with a corrected request under the same key).

Bounds: rows older than 24 h are purged (at most once a minute, on a claim), each principal keeps at most
MAX_PER_PRINCIPAL (the oldest finished ones go first) and an answer is stored only up to MAX_STORED_BODY bytes. A
write is 2/s per device
(limits.py), so the bounds are a backstop, not a limit anyone meets. A fenced hub refuses these writes like any other
(sizeup/fence.py: FencedWrite -> 503), and the CAD write that follows is never reached."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

KEEP_S = 24 * 3600
PURGE_EVERY_S = 60.0
MAX_PER_PRINCIPAL = 1000
MAX_STORED_BODY = 16 * 1024     # an answer longer than this is not kept (its key is released)
STALE_RUNNING_S = 120.0          # a claim whose worker died (a crash mid-request) is retried after this
KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS v1_idempotency (
  principal TEXT NOT NULL, key TEXT NOT NULL, fp TEXT NOT NULL, status INTEGER NOT NULL DEFAULT 0,
  body TEXT NOT NULL DEFAULT '', at REAL NOT NULL, owner TEXT NOT NULL DEFAULT '', PRIMARY KEY (principal, key));
CREATE INDEX IF NOT EXISTS v1_idempotency_at ON v1_idempotency (at);
"""

NEW, REPLAY, MISMATCH, BUSY = "new", "replay", "mismatch", "busy"


def fingerprint(method: str, path: str, body: dict) -> str:
    """What the key stands for: the request's method, path and body (as data: key order and spacing do not matter)."""
    canon = json.dumps([method, path, body], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


class Idempotency:
    def __init__(self, connect: Callable[[], sqlite3.Connection], clock: Callable[[], float] = time.time):
        self._connect, self.clock = connect, clock
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._purged = -1e18

    @classmethod
    def for_cad(cls, cad) -> "Idempotency":
        """The store next to the CAD's: hub.sqlite in the same state dir (":memory:" for a throwaway CAD). Opened on
        the first write only; its connection obeys the CAD's fence (a fenced task writes nothing)."""
        store = cad.store

        def connect() -> sqlite3.Connection:
            from sizeup.cloud.hubstore import _connect
            path = ":memory:" if store.path == ":memory:" else str(Path(store.path).parent / "hub.sqlite")
            db = _connect(path)
            db.may_write = lambda: store.may_write()
            db.on_refuse = lambda: (getattr(store.db, "on_refuse", None) or (lambda: None))()
            return db
        return cls(connect, store.clock)

    def _open(self) -> sqlite3.Connection:
        if self._db is None:
            db = self._connect()
            db.executescript(SCHEMA)
            if not any(r[1] == "owner" for r in db.execute("PRAGMA table_info(v1_idempotency)")):
                db.execute("ALTER TABLE v1_idempotency ADD COLUMN owner TEXT NOT NULL DEFAULT ''")    # an older table
            self._db = db
        return self._db

    def claim(self, principal: str, key: str, fp: str) -> tuple[str, "tuple[int, str] | str | None"]:
        """(NEW | REPLAY | MISMATCH | BUSY, the owner token for NEW, the stored (status, body) for a REPLAY)."""
        with self._lock:
            db, now = self._open(), self.clock()
            self._purge(db, now)
            r = db.execute("SELECT fp, status, body, at FROM v1_idempotency WHERE principal = ? AND key = ?",
                           (principal, key)).fetchone()
            if r is not None and now - r[3] <= KEEP_S:
                if r[0] != fp:
                    return MISMATCH, None
                if r[1] == 0 and now - r[3] <= STALE_RUNNING_S:
                    return BUSY, None
                if r[1] != 0:
                    return REPLAY, (r[1], r[2])
                # (a running claim past STALE_RUNNING_S, or a row past KEEP_S, falls through: taken over below)
            owner = secrets.token_hex(8)
            db.execute("INSERT OR REPLACE INTO v1_idempotency (principal, key, fp, status, body, at, owner) "
                       "VALUES (?, ?, ?, 0, '', ?, ?)", (principal, key, fp, now, owner))
            n = db.execute("SELECT COUNT(*) FROM v1_idempotency WHERE principal = ?", (principal,)).fetchone()[0]
            if n > MAX_PER_PRINCIPAL:       # the oldest finished answers go first (never the one just reserved)
                db.execute("DELETE FROM v1_idempotency WHERE rowid IN (SELECT rowid FROM v1_idempotency WHERE "
                           "principal = ? AND status != 0 ORDER BY at LIMIT ?)", (principal, n - MAX_PER_PRINCIPAL))
            return NEW, owner

    def finish(self, principal: str, key: str, owner: str, status: int, body: str) -> None:
        if len(body) > MAX_STORED_BODY:
            return self.release(principal, key, owner)
        with self._lock:
            self._open().execute("UPDATE v1_idempotency SET status = ?, body = ? WHERE principal = ? AND key = ? "
                                 "AND owner = ?", (status, body, principal, key, owner))

    def release(self, principal: str, key: str, owner: str) -> None:
        with self._lock:
            self._open().execute("DELETE FROM v1_idempotency WHERE principal = ? AND key = ? AND status = 0 "
                                 "AND owner = ?", (principal, key, owner))

    def _purge(self, db: sqlite3.Connection, now: float) -> None:
        if now - self._purged >= PURGE_EVERY_S:
            self._purged = now
            db.execute("DELETE FROM v1_idempotency WHERE at < ?", (now - KEEP_S,))

    def count(self) -> int:
        with self._lock:
            return self._open().execute("SELECT COUNT(*) FROM v1_idempotency").fetchone()[0]
