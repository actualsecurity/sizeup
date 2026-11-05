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
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Callable

EXIT_FENCED = 75
READ_EVERY_S = 0.5


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
