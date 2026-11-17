"""The devices registered for push (#563): one table, `push_devices`, in hub.sqlite, created on the first registration
(never at start: tests/test_startup_writes.py) and keyed by the device's id with no link to any other table (the
sign-in's device records, #558, are built apart; Revoke calls PushService.drop). Old code ignores the table.

A row: device_id, email (the person: the role is looked up from it when a push goes out, never stored), platform
(ios | android), env (sandbox | production for ios, "" for android), endpoint (the SNS endpoint ARN: never shown to
the app), app (the platform application ARN it was made under), prefs (per kind, JSON), quiet (from / to HH:MM and the
IANA zone), created, updated. The push token itself is not kept: SNS holds it.

Writes go through the fence (sizeup/fence.py GuardedConnection): a task that is not the leader refuses them."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

MAX_DEVICES = 40          # the design's fan-out bound (docs/mobile-api.md section 4): one Publish each
TABLE = "push_devices"
SCHEMA = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
  device_id TEXT PRIMARY KEY, email TEXT NOT NULL, platform TEXT NOT NULL, env TEXT NOT NULL DEFAULT '',
  endpoint TEXT NOT NULL, app TEXT NOT NULL, prefs TEXT NOT NULL DEFAULT '{{}}', quiet TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL, updated REAL NOT NULL)"""
COLUMNS = "device_id, email, platform, env, endpoint, app, prefs, quiet, created, updated"


class Full(RuntimeError):
    """MAX_DEVICES are registered already."""


class PushDevices:
    def __init__(self, path: str | Path, may_write: Callable[[], bool] | None = None, clock=time.time, log=print):
        self.path, self.clock, self.log = str(path), clock, log
        self._may_write = may_write
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._made = False

    def writable(self) -> bool:
        """May this process write now (the fence)? True without one."""
        return self._may_write is None or bool(self._may_write())

    def _conn(self, create: bool) -> sqlite3.Connection | None:
        """The connection; None for a read when the file or the table isn't there yet (nothing is created by a read)."""
        if self._db is None:
            if not create and not os.path.exists(self.path):
                return None
            from sizeup.cloud.hubstore import _connect          # the hub's own settings (WAL, busy timeout, the fence)
            self._db = _connect(self.path)
        if self._may_write is not None:
            self._db.may_write = self._may_write                # type: ignore[attr-defined]
        if not self._made:
            if create:
                self._db.execute(SCHEMA)
                self._made = True
            elif self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone():
                self._made = True
            else:
                return None
        return self._db

    @staticmethod
    def _row(r) -> dict:
        d = dict(zip(COLUMNS.split(", "), r))
        d["prefs"] = json.loads(d["prefs"] or "{}")
        d["quiet"] = json.loads(d["quiet"]) if d["quiet"] else None
        return d

    def get(self, device_id: str) -> dict | None:
        with self._lock:
            db = self._conn(False)
            r = db.execute(f"SELECT {COLUMNS} FROM {TABLE} WHERE device_id = ?", (device_id,)).fetchone() if db else None
        return self._row(r) if r else None

    def rows(self) -> list[dict]:
        """Every registered device, at most MAX_DEVICES (oldest registration first): the fan-out's list."""
        with self._lock:
            db = self._conn(False)
            rs = db.execute(f"SELECT {COLUMNS} FROM {TABLE} ORDER BY created, device_id LIMIT ?",
                            (MAX_DEVICES,)).fetchall() if db else []
        return [self._row(r) for r in rs]

    def count(self) -> int:
        with self._lock:
            db = self._conn(False)
            return db.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] if db else 0

    def put(self, device_id: str, email: str, platform: str, env: str, endpoint: str, app: str, prefs: dict,
            quiet: dict | None) -> dict:
        """Register or replace. Full when this would be device number MAX_DEVICES + 1. FencedWrite when not the leader."""
        now = self.clock()
        with self._lock:
            db = self._conn(True)
            old = db.execute(f"SELECT created FROM {TABLE} WHERE device_id = ?", (device_id,)).fetchone()
            if old is None and db.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] >= MAX_DEVICES:
                raise Full(f"{MAX_DEVICES} devices are registered")
            db.execute(f"INSERT OR REPLACE INTO {TABLE} ({COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (device_id, email, platform, env, endpoint, app, json.dumps(prefs, sort_keys=True),
                        json.dumps(quiet, sort_keys=True) if quiet else "", old[0] if old else now, now))
        return self.get(device_id) or {}

    def remove(self, device_id: str) -> dict | None:
        """Delete the row: the row it was, or None. A hub that never registered anyone writes nothing."""
        with self._lock:
            db = self._conn(False)
            if db is None:
                return None
            r = db.execute(f"SELECT {COLUMNS} FROM {TABLE} WHERE device_id = ?", (device_id,)).fetchone()
            if r is None:
                return None
            db.execute(f"DELETE FROM {TABLE} WHERE device_id = ?", (device_id,))
        return self._row(r)

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
                self._made = False
