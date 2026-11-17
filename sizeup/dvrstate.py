"""The camera DVR's per-camera state (#596, sizeup/dvr.py): kept beside the recordings so the next leader records at once
instead of first reading every file name on the shared storage.

One small JSON file in the DVR's folder (STATE_FILE), written whole (a temp file, fsync, rename, the folder fsync'd
where the file system allows) and only while may_write() (the lease holder, sizeup/fence.py):

    {"v": 1, "at": <when written>, "cameras": {<camera id>: {"last": key, "end": t, "offset": n, ...}, ...}}

A camera's record is whatever its owner puts in it (the DVR: last key, its end, the sequence offset, the stream's last
media sequence and source, the bytes it had on disk). Keys are merged, never dropped, so other per-camera flags can
live here too.

    DvrState(path, clock, may_write, log)
    .load()                  read it; True when it is there, readable, ours (v) and fresh (written within max_age_s)
    .cameras() / .get(cid)   the camera ids / a copy of one camera's record ({} when none)
    .update(cid, **fields)   merged in memory; marks it changed only when a value changed
    .due()                   changed, and every_s has passed since the last write
    .checkpoint(force)       written when due (force: when changed, at once); False when not written

A small interface on purpose: the DVR's state could move into a recorder service of its own later without the DVR's
callers changing.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

STATE_FILE = ".dvr-state.json"     # (a dot: never taken for a camera's folder)
VERSION = 1
EVERY_S = 5.0                      # written at most this often while anything changes (and on a clean stop)
MAX_AGE_S = 600.0                  # older than this: not trusted (the index is read first, as before)
TOKEN = f"{os.getpid()}-{secrets.token_hex(4)}"   # this process's temp files (never another writer's)


class DvrState:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time,
                 may_write: Callable[[], bool] = lambda: True, log: Callable[[str], None] = print,
                 every_s: float = EVERY_S, max_age_s: float = MAX_AGE_S):
        self.path, self.clock, self.may_write, self.log = Path(path), clock, may_write, log
        self.every_s, self.max_age_s = float(every_s), float(max_age_s)
        self._lock = threading.Lock()
        self._cams: dict[str, dict] = {}
        self._dirty = False
        self.written_at = -1e18
        self.loaded_at: float | None = None        # when the file read by load() was written

    def load(self) -> bool:
        """The file the leader before left: its records taken in (even a stale one's, for what they say of each
        camera); True only when it is fresh enough to resume from."""
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as e:
            self.log(f"dvr: the state file not read ({type(e).__name__}): reading the index first")
            return False
        if not isinstance(raw, dict) or raw.get("v") != VERSION or not isinstance(raw.get("cameras"), dict) \
                or not isinstance(raw.get("at"), (int, float)):
            self.log("dvr: the state file isn't one this release reads: reading the index first")
            return False
        with self._lock:
            for cid, rec in raw["cameras"].items():
                if isinstance(cid, str) and isinstance(rec, dict):
                    self._cams.setdefault(cid, {}).update(rec)
        self.loaded_at = float(raw["at"])
        age = self.clock() - self.loaded_at
        if age > self.max_age_s or age < -self.max_age_s:
            self.log(f"dvr: the state file is {age:.0f} s old: reading the index first")
            return False
        return True

    def cameras(self) -> list[str]:
        with self._lock:
            return list(self._cams)

    def get(self, cid: str) -> dict:
        with self._lock:
            return dict(self._cams.get(cid) or {})

    def update(self, cid: str, **fields) -> None:
        with self._lock:
            rec = self._cams.setdefault(cid, {})
            for k, v in fields.items():
                if rec.get(k, ...) != v:
                    rec[k] = v
                    self._dirty = True

    def due(self) -> bool:
        with self._lock:
            return self._dirty and self.clock() - self.written_at >= self.every_s

    def checkpoint(self, force: bool = False) -> bool:
        if not (force and self._dirty) and not self.due():
            return False
        if not self.may_write():
            return False
        with self._lock:
            now = self.clock()
            body = json.dumps({"v": VERSION, "at": now, "cameras": self._cams}, separators=(",", ":")).encode()
            self._dirty, self.written_at = False, now
        if self._write(body):
            return True
        with self._lock:
            self._dirty = True                   # (asked again at the next one)
        return False

    def _write(self, body: bytes) -> bool:
        tmp = self.path.with_name(f".{self.path.name}.{TOKEN}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            if not self.may_write():             # (the lease went while it was written: never in its place)
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, self.path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            self.log(f"dvr: the state file not written ({type(e).__name__}: {e})")
            return False
        try:                                     # the rename itself made durable, where the folder can be fsync'd
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        return True
