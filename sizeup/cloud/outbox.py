"""The station's durable outbox: everything for the hub waits here (state/outbox.sqlite) until the
hub has it, so a dropped internet link or a hub redeploy loses nothing.

    add(kind, data, audio)   one event, in order (seq never reused: AUTOINCREMENT), committed at once
    batch()                  the oldest events, up to a size; ack(seq) deletes them once the hub has them
    epoch                    a random id made with the file: the hub keys its "applied up to seq N"
                             on it, so a new outbox (file deleted) starts over instead of being skipped

Bounded: queued audio over max_audio_bytes drops the OLDEST audio first (the event stays, marked
audio_dropped); metadata (transcripts, pages, radio IDs) is never dropped.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    audio BLOB,
    audio_dropped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _plain(o):
    """numpy scalars (a confidence, a flag) as plain JSON values."""
    return o.item() if hasattr(o, "item") else str(o)


class Outbox:
    def __init__(self, path: str | Path, max_audio_bytes: int = 300 * 1024 * 1024):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.max_audio_bytes = int(max_audio_bytes)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta (key, value) VALUES ('epoch', ?)", (secrets.token_hex(8),))
            row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        self.epoch: str = row[0]
        self.audio_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox").fetchone()[0])
        self.dropped_audio = 0          # this run: events whose audio was dropped to stay under the limit

    def add(self, kind: str, data: dict, audio: bytes | None = None, at: float | None = None) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO outbox (at, kind, data, audio) VALUES (?, ?, ?, ?)",
                                  (time.time() if at is None else at, kind, json.dumps(data, separators=(",", ":"), default=_plain),
                                   audio))
            self.audio_bytes += len(audio or b"")
            while self.audio_bytes > self.max_audio_bytes:
                old = self.db.execute("SELECT seq, LENGTH(audio) FROM outbox WHERE audio IS NOT NULL "
                                      "ORDER BY seq LIMIT 20").fetchall()
                if not old:
                    break
                for seq, n in old:
                    self.db.execute("UPDATE outbox SET audio = NULL, audio_dropped = 1 WHERE seq = ?", (seq,))
                    self.audio_bytes -= n
                    self.dropped_audio += 1
                    if self.audio_bytes <= self.max_audio_bytes:
                        break
            return int(cur.lastrowid)

    def batch(self, max_bytes: int = 4 * 1024 * 1024, max_events: int = 200) -> list[dict]:
        """The oldest events, at least one, up to max_bytes of data + audio."""
        out, size = [], 0
        with self._lock:
            rows = self.db.execute("SELECT seq, at, kind, data, audio, audio_dropped FROM outbox ORDER BY seq LIMIT ?",
                                   (max_events,)).fetchall()
        for seq, at, kind, data, audio, dropped in rows:
            n = len(data) + len(audio or b"")
            if out and size + n > max_bytes:
                break
            out.append({"seq": seq, "at": at, "type": kind, "data": json.loads(data), "audio": audio,
                        "audio_dropped": bool(dropped)})
            size += n
        return out

    def ack(self, upto: int) -> int:
        """The hub has everything up to seq `upto`: gone from here."""
        with self._lock:
            n = self.db.execute("DELETE FROM outbox WHERE seq <= ?", (int(upto),)).rowcount
            self.audio_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox").fetchone()[0])
            return n

    def pending(self) -> tuple[int, float | None]:
        """(events waiting, when the oldest was queued)."""
        with self._lock:
            n, oldest = self.db.execute("SELECT COUNT(*), MIN(at) FROM outbox").fetchone()
        return int(n), oldest

    def close(self) -> None:
        with self._lock:
            self.db.close()
