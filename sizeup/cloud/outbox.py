"""The station's durable outbox: everything for the hub waits here (state/outbox.sqlite) until the
hub has it, so a dropped internet link or a hub redeploy loses nothing.

    add(kind, data, audio)   one event, in order (seq never reused: AUTOINCREMENT), committed at once
    batch()                  the oldest events, up to a size; ack(seq) deletes them once the hub has them
    epoch                    a random id made with the file: the hub keys its "applied up to seq N"
                             on it, so a new outbox (file deleted) starts over instead of being skipped

Bounded: queued audio over max_audio_bytes drops the OLDEST audio first (the event stays, marked
audio_dropped); metadata (transcripts, pages, radio IDs) is never dropped. Context audio (#409: another P25
system's clips, add(context=True)) has its own smaller budget (CONTEXT_SHARE of the limit) and goes first when the
whole is over: a hub outage never costs a fire clip for a police one.

The speech-to-text input (add(stt=(flac, meta)), sizeup/cloud/sttinput.py: the exact audio the station's whisper
transcribes, for the hub's engines) rides with its event in its own column and has its own budget (STT_SHARE of the
limit). It counts toward the limit with the clips and goes before any of them: over its budget, or the whole over the
limit, the oldest STT inputs are dropped first (the event and its clip stay, marked stt_dropped; dropped_stt counts
them), and only then context audio, then our clips. The hub then transcribes the listening clip and says so.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

CONTEXT_SHARE = 0.2
STT_SHARE = 0.3

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
        if "context" not in {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN context INTEGER NOT NULL DEFAULT 0")
        row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta (key, value) VALUES ('epoch', ?)", (secrets.token_hex(8),))
            row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        self.epoch: str = row[0]
        self.audio_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox").fetchone()[0])
        self.dropped_audio = 0          # this run: events whose audio was dropped to stay under the limit
        self.context_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1").fetchone()[0])
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}
        for col, decl in (("stt", "BLOB"), ("stt_meta", "TEXT"), ("stt_dropped", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:             # an outbox from before the STT input: its rows go up as they were
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {col} {decl}")
        self.stt_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(stt)), 0) FROM outbox").fetchone()[0])
        self.dropped_stt = 0            # this run: STT inputs dropped to stay under the limit (their clips kept)

    def _drop_stt(self, limit: int) -> None:
        """STT inputs, oldest first, until they are inside their own budget and the whole is inside the limit."""
        while self.stt_bytes > limit or (self.stt_bytes > 0 and self.audio_bytes + self.stt_bytes > self.max_audio_bytes):
            old = self.db.execute("SELECT seq, LENGTH(stt) FROM outbox WHERE stt IS NOT NULL ORDER BY seq LIMIT 20").fetchall()
            if not old:
                self.stt_bytes = 0
                return
            for seq, n in old:
                self.db.execute("UPDATE outbox SET stt = NULL, stt_dropped = 1 WHERE seq = ?", (seq,))
                self.stt_bytes -= n
                self.dropped_stt += 1
                if self.stt_bytes <= limit and self.audio_bytes + self.stt_bytes <= self.max_audio_bytes:
                    return

    def _drop_context(self, limit: int, whole: bool) -> None:
        """Context audio, oldest first, until it is inside its own budget (whole: until the whole outbox is too)."""
        while self.context_bytes > limit or (whole and self.audio_bytes > self.max_audio_bytes and self.context_bytes > 0):
            old = self.db.execute("SELECT seq, LENGTH(audio) FROM outbox WHERE audio IS NOT NULL AND context = 1 "
                                  "ORDER BY seq LIMIT 20").fetchall()
            if not old:
                self.context_bytes = 0
                return
            for seq, n in old:
                self.db.execute("UPDATE outbox SET audio = NULL, audio_dropped = 1 WHERE seq = ?", (seq,))
                self.audio_bytes -= n
                self.context_bytes -= n
                self.dropped_audio += 1
                if self.context_bytes <= limit and not (whole and self.audio_bytes > self.max_audio_bytes):
                    return

    def add(self, kind: str, data: dict, audio: bytes | None = None, at: float | None = None, context: bool = False,
            stt: tuple[bytes, dict] | None = None) -> int:
        """stt: (FLAC, its metadata), the speech-to-text input that goes with this event's clip."""
        with self._lock:
            cur = self.db.execute("INSERT INTO outbox (at, kind, data, audio, context, stt, stt_meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (time.time() if at is None else at, kind, json.dumps(data, separators=(",", ":"), default=_plain),
                                   audio, int(bool(context)), stt[0] if stt else None,
                                   json.dumps(stt[1], separators=(",", ":")) if stt else None))
            self.stt_bytes += len(stt[0]) if stt else 0
            self.audio_bytes += len(audio or b"")
            if context:
                self.context_bytes += len(audio or b"")
            self._drop_stt(int(self.max_audio_bytes * STT_SHARE))      # (first: before any clip)
            self._drop_context(int(self.max_audio_bytes * CONTEXT_SHARE), whole=True)
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
            rows = self.db.execute("SELECT seq, at, kind, data, audio, audio_dropped, stt, stt_meta, stt_dropped FROM outbox "
                                   "ORDER BY seq LIMIT ?", (max_events,)).fetchall()
        for seq, at, kind, data, audio, dropped, stt, stt_meta, stt_dropped in rows:
            n = len(data) + len(audio or b"") + len(stt or b"")
            if out and size + n > max_bytes:
                break
            ev = {"seq": seq, "at": at, "type": kind, "data": json.loads(data), "audio": audio,
                  "audio_dropped": bool(dropped)}
            if stt:
                ev["stt"] = (stt, json.loads(stt_meta or "{}"))
            if stt_dropped:
                ev["stt_dropped"] = True
            out.append(ev)
            size += n
        return out

    def ack(self, upto: int) -> int:
        """The hub has everything up to seq `upto`: gone from here."""
        with self._lock:
            n = self.db.execute("DELETE FROM outbox WHERE seq <= ?", (int(upto),)).rowcount
            self.audio_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox").fetchone()[0])
            self.context_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1").fetchone()[0])
            self.stt_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(stt)), 0) FROM outbox").fetchone()[0])
            return n

    def pending(self) -> tuple[int, float | None]:
        """(events waiting, when the oldest was queued)."""
        with self._lock:
            n, oldest = self.db.execute("SELECT COUNT(*), MIN(at) FROM outbox").fetchone()
        return int(n), oldest

    def close(self) -> None:
        with self._lock:
            self.db.close()
