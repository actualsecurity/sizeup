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

The speech-to-text inputs (sizeup/cloud/sttinput.py: the exact audio the station's whisper transcribes, FLAC) are a
lane of their own: the stt_inputs table, sent by their own thread to POST /api/station/stt only while no event waits
here, so a page or a transmission never queues behind one. They are not events (no seq, never in the hub's cursor:
the hub keeps each one by its clip id, whenever it comes, so the order doesn't matter and a resend is harmless).

    add_stt(data, flac, meta, prio)  one, committed at once
    stt_batch()                the highest priority first, up to a size (the order below); stt_ack(ns) deletes the ones
                               the hub answered for

Each has a priority (#677, sttinput.PRE / FIRE / NORMAL / LOW, App._stt_lane): PRE the pre-alerts (pages, a talkgroup of
the alert tier), FIRE our fire traffic (whisper's URGENT: Fire/EMS 15, the firegrounds), NORMAL the airband and other
traffic, LOW a context talkgroup. Sent: the highest priority first; within PRE and FIRE the fresh ones (queued within
FRESH_S) before the stale, so a fresh page isn't behind an outage's backlog; else the oldest first. They have their own
budget (STT_SHARE of max_audio_bytes) and count toward the limit with the clips, and go before any of them: over their
budget, or the whole over the limit, STT inputs are dropped first, the lowest priority first and the oldest first within
it (dropped_stt counts them, dropped_stt_pre the pre-alert ones), only then context audio, then our clips. The hub then
transcribes the listening clip and says so.

The events never wait on the lane: its order is an index (stt_inputs_order), the batch picks its rows from the index
alone and reads each FLAC on its own, so the lock add() needs is only ever held for one small read.
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
STT_PRE = 3                     # an STT input's priority (sizeup/cloud/sttinput.py PRE / FIRE / NORMAL / LOW): ...
STT_FIRE = 2                    # ... pre-alert, fire traffic,
STT_NORMAL = 1                  # ... other traffic (and every row from before #677)
FRESH_S = 300.0                 # PRE / FIRE: queued within this long goes before the older (an outage's backlog)

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
CREATE TABLE IF NOT EXISTS stt_inputs (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    data TEXT NOT NULL,
    flac BLOB NOT NULL,
    meta TEXT NOT NULL,
    prio INTEGER NOT NULL DEFAULT 1
);
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
        if "prio" not in {r[1] for r in self.db.execute("PRAGMA table_info(stt_inputs)")}:    # (a lane before #677)
            self.db.execute(f"ALTER TABLE stt_inputs ADD COLUMN prio INTEGER NOT NULL DEFAULT {STT_NORMAL}")
        self.db.execute("CREATE INDEX IF NOT EXISTS stt_inputs_order ON stt_inputs (prio, n, at)")
        row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta (key, value) VALUES ('epoch', ?)", (secrets.token_hex(8),))
            row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        self.epoch: str = row[0]
        self.audio_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox").fetchone()[0])
        self.dropped_audio = 0          # this run: events whose audio was dropped to stay under the limit
        self.context_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1").fetchone()[0])
        self.stt_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs").fetchone()[0])
        self.dropped_stt = 0            # this run: STT inputs dropped to stay under the limit (their clips kept)
        self.dropped_stt_pre = 0        # ... of them, the pre-alert channels' (only once nothing lower was left)
        self.dropped_stt_fire = 0       # ... and fire traffic's

    def _drop_stt(self) -> None:
        """STT inputs, the lowest priority first and the oldest first within it, until they are inside their own budget
        and the whole is inside the limit."""
        limit = int(self.max_audio_bytes * STT_SHARE)
        while self.stt_bytes > limit or (self.stt_bytes > 0 and self.audio_bytes + self.stt_bytes > self.max_audio_bytes):
            old = self.db.execute("SELECT n, prio FROM stt_inputs INDEXED BY stt_inputs_order ORDER BY prio, n "
                                  "LIMIT 20").fetchall()
            if not old:
                self.stt_bytes = 0
                return
            for n, prio in old:
                size = self.db.execute("SELECT LENGTH(flac) FROM stt_inputs WHERE n = ?", (n,)).fetchone()[0]
                self.db.execute("DELETE FROM stt_inputs WHERE n = ?", (n,))
                self.stt_bytes -= size
                self.dropped_stt += 1
                self.dropped_stt_pre += int(prio >= STT_PRE)
                self.dropped_stt_fire += int(prio == STT_FIRE)
                if self.stt_bytes <= limit and self.audio_bytes + self.stt_bytes <= self.max_audio_bytes:
                    return

    def add_stt(self, data: dict, flac: bytes, meta: dict, at: float | None = None, prio: int = STT_NORMAL) -> int:
        """One clip's STT input (data: its id and where it was heard, sizeup/cloud/sttinput.py; prio: its channel's,
        sizeup/app.py App._stt_lane)."""
        with self._lock:
            cur = self.db.execute("INSERT INTO stt_inputs (at, data, flac, meta, prio) VALUES (?, ?, ?, ?, ?)",
                                  (time.time() if at is None else at, json.dumps(data, separators=(",", ":"), default=_plain),
                                   flac, json.dumps(meta, separators=(",", ":"), default=_plain), int(prio)))
            self.stt_bytes += len(flac)
            self._drop_stt()
            return int(cur.lastrowid)

    def stt_batch(self, max_bytes: int = 256 * 1024, max_items: int = 50, now: float | None = None) -> list[dict]:
        """The STT inputs to send next, at least one, up to max_bytes of FLAC (small: a post already on its way when a
        page starts shares the uplink with it), in the lane's order (the module doc). Picked from the index alone, then
        each read on its own: the lock is never held over the lane's FLAC."""
        out, size = [], 0
        for n in self._stt_order(max_items, now):
            with self._lock:
                row = self.db.execute("SELECT at, data, flac, meta, prio FROM stt_inputs WHERE n = ?", (n,)).fetchone()
            if row is None:                         # (dropped meanwhile)
                continue
            at, data, flac, meta, prio = row
            if out and size + len(flac) > max_bytes:
                break
            out.append({"n": n, "at": at, "data": json.loads(data), "flac": bytes(flac), "meta": json.loads(meta),
                        "prio": prio})
            size += len(flac)
        return out

    def _stt_order(self, k: int, now: float | None = None) -> list[int]:
        """The next k rows' n, from the index (stt_inputs_order) alone."""
        fresh = (time.time() if now is None else now) - FRESH_S
        out: list[int] = []
        with self._lock:
            prios = [p for (p,) in self.db.execute("SELECT DISTINCT prio FROM stt_inputs INDEXED BY stt_inputs_order "
                                                   "ORDER BY prio DESC")]
            for p in prios:
                parts = (("AND at >= ?", (fresh,)), ("AND at < ?", (fresh,))) if p >= STT_FIRE else (("", ()),)
                for cond, arg in parts:
                    if len(out) >= k:
                        return out
                    out += [n for (n,) in self.db.execute(
                        f"SELECT n FROM stt_inputs INDEXED BY stt_inputs_order WHERE prio = ? {cond} ORDER BY n LIMIT ?",
                        (p, *arg, k - len(out)))]
        return out[:k]

    def stt_ack(self, ns) -> int:
        """The hub has answered for these (kept, or refused for good): gone from here."""
        ns = [int(n) for n in ns]
        if not ns:
            return 0
        marks = ",".join("?" * len(ns))
        with self._lock:                            # (those rows only: never a pass over the lane's FLAC)
            gone = self.db.execute(f"SELECT COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs WHERE n IN ({marks})",
                                   ns).fetchone()[0]
            k = self.db.execute(f"DELETE FROM stt_inputs WHERE n IN ({marks})", ns).rowcount
            self.stt_bytes = max(0, self.stt_bytes - int(gone))
            return k

    def stt_pending(self) -> tuple[int, int]:
        """(STT inputs waiting, their bytes): the count from the index, the bytes as kept."""
        with self._lock:
            n = self.db.execute("SELECT COUNT(*) FROM stt_inputs INDEXED BY stt_inputs_order").fetchone()[0]
            return int(n), self.stt_bytes

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

    def add(self, kind: str, data: dict, audio: bytes | None = None, at: float | None = None, context: bool = False) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO outbox (at, kind, data, audio, context) VALUES (?, ?, ?, ?, ?)",
                                  (time.time() if at is None else at, kind, json.dumps(data, separators=(",", ":"), default=_plain),
                                   audio, int(bool(context))))
            self.audio_bytes += len(audio or b"")
            if context:
                self.context_bytes += len(audio or b"")
            self._drop_stt()                    # (first: the STT inputs go before any clip)
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
            self.context_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1").fetchone()[0])
            return n

    def pending(self) -> tuple[int, float | None]:
        """(events waiting, when the oldest was queued)."""
        with self._lock:
            n, oldest = self.db.execute("SELECT COUNT(*), MIN(at) FROM outbox").fetchone()
        return int(n), oldest

    def close(self) -> None:
        with self._lock:
            self.db.close()
