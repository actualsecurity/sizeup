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

Quarantine: an event the hub refuses for good (sizeup/cloud/station.py _rejected: a 400 / 413 / 422 its batch was split
down to, or one the hub lists as "rejected" after it failed to apply) moves to the quarantine table, with the hub's
reason and error, so the events behind it go on. Kept on disk across restarts, bounded (QUARANTINE_ROWS: the oldest
non-critical rows go; QUARANTINE_AUDIO: the oldest clips go, the rows stay; a clip over QUARANTINE_CLIP is not kept).
A critical one (CRITICAL_KINDS: a page, its text, a P25 emergency, an incident source's event; never one the hub calls
malformed) is sent again on a slow schedule (RETRY_S), as a new event at the end of the outbox (its row carries qid),
for CRITICAL_TRIES / CRITICAL_FOR_S, then "stuck"; the hub keys pages, transmissions and alarms by their ids, so a
copy is harmless. A source_event is a snapshot: a quarantined one the feed has a newer snapshot of since
(source_latest) is dropped, never sent again over it (the hub also ignores an older one: sizeup/cad/sources.py
on_event). An alarm the hub took too late to raise stays, "late". Each goes up in the station's status (summary()), and
the hub raises "Station event rejected by hub: <type>" (sizeup/cloud/health.py); a person drops one with
`sizeup-station quarantine drop ID` (main()).

    quarantine(row, reason, error)   moved (or, already acked: copied) out of the outbox; a requeued copy updates its row
    requeue_due()                    the critical ones whose time has come, back at the end of the outbox
    released(qids) / late(qid, why)  the hub took these copies: gone / kept as "late"
    strip_audio(seq)                 an event too large for the hub with its clip: sent without it (the station keeps
                                     the clip in its own store)
    drop(id | None)                  a person's

A row the station cannot read back (its data not JSON, its audio stored as text: only another writer of the file or
a damaged page makes one) goes to the quarantine too, reason "unreadable", said once in the log: the rows behind it go
on (batch()).

The epoch after a stop that was not in order (a power cut, a crash, a copy of the file taken while it was open): the
hub skips an event whose seq is at or under its cursor (epoch, seq) as delivered before. In WAL mode with synchronous
= NORMAL a commit is not synced to the disk, so a power cut can set the file back past seqs the hub already has; the
next events would get those seqs again, be skipped there, acked and deleted here. So each opener leaves a marker row in
meta ("open:<token>": its boot and pid) in a synced commit when it opens, and takes it out when it stops in order
(release(), close()). A marker found at open that is nobody's now (another boot, a process that is gone, or this
process's own after it let go) means the last run did not stop in order: the file gets a new epoch (the hub counts a
new epoch from the start), in the same synced commit, before any seq goes out. The cost is one fsync per open (and per
batch() after a release), never one per event; the price of a new epoch is that the events the hub had but had not
acked yet are applied a second time (it keys pages, transmissions and alarms by their ids). Every seq handed out by
batch() is under an armed marker: a seq a hub can have is never handed out again under the same epoch.

    unclean           why this file got a new epoch at open ("" when it did not)

A file SQLite cannot open (open_outbox(): damaged, or written with a newer SQLite's features) is moved aside, kept
(<name>.unreadable-<time>, its -wal and -shm with it), said in the log and in the station's status (notes, Health),
and a new outbox (a new epoch) is made: the station starts.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

CONTEXT_SHARE = 0.2
STT_SHARE = 0.3
STT_PRE = 3                     # an STT input's priority (sizeup/cloud/sttinput.py PRE / FIRE / NORMAL / LOW): ...
STT_FIRE = 2                    # ... pre-alert, fire traffic,
STT_NORMAL = 1                  # ... other traffic (and every row from before #677)
FRESH_S = 300.0                 # PRE / FIRE: queued within this long goes before the older (an outage's backlog)
# Quarantine (the module doc): what a person must hear about if the hub refuses it, retried until the hub takes it
CRITICAL_KINDS = ("page", "page_text", "alarm", "source_event")
RETRY_S = (600.0, 1800.0, 3600.0)   # a critical one sent again after this long (its 1st, 2nd, 3rd+ refusal)
QUARANTINE_ROWS = 500               # kept at most (the critical ones always)
QUARANTINE_AUDIO = 50 * 1024 * 1024     # ... and their clips
QUARANTINE_ERROR = 300              # the hub's error, kept this long
QUARANTINE_CLIP = 8 * 1024 * 1024   # a clip over this is not kept in the quarantine (the event is; the station's own
                                    # store still has the clip)
CRITICAL_TRIES = 8                  # a critical one is sent again this many times at most (about 7 h) ...
CRITICAL_FOR_S = 24 * 3600.0        # ... or for this long: then "stuck", kept for a person (the alert stays)
LATE_KEEP_S = 24 * 3600.0           # an alarm the hub took too late to raise: on Health this long, then gone
PERMANENT_REASONS = ("malformed", "unreadable")  # reasons that never change with a retry: never critical
SOURCE_LATEST_S = 7 * 24 * 3600.0   # a feed event's latest snapshot seq is kept this long (source_latest)
UNREADABLE = "unreadable"           # the quarantine's reason for a row the station cannot read back (batch())
MARK = "open:"                      # meta: an opener's marker ("open:<token>" -> {"boot", "pid"}), the module doc
NOTES_KEPT = 10                     # the outbox's own notes (a file set aside, a new epoch, a row unreadable) kept

_open_tokens: set[str] = set()      # this process's openers whose marker is armed (a marker of ours not in it: let go)
_open_lock = threading.Lock()
_boot: str | None = None


def boot_id() -> str:
    """This boot of the machine: Linux's boot_id, else the kernel's boot time (macOS); "" when neither is known."""
    global _boot
    if _boot is None:
        try:
            _boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            try:
                out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                                     timeout=5).stdout
                m = re.search(r"sec = (\d+)", out)
                _boot = f"boottime-{m[1]}" if m else ""
            except (OSError, subprocess.SubprocessError):
                _boot = ""
    return _boot


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _marker_live(token: str, value: str) -> bool:
    """An opener's marker: is that opener still there? This process's own while it holds it; another process's while it
    runs in this same boot. Anything else (another boot: a power cut or a reboot; a process that is gone; a marker that
    does not parse) is a stop that was not in order."""
    try:
        m = json.loads(value)
        boot, pid = str(m["boot"]), int(m["pid"])
    except (ValueError, TypeError, KeyError):
        return False
    if boot != boot_id() or pid <= 0:
        return False
    if pid == os.getpid():
        with _open_lock:
            return token in _open_tokens
    return _alive(pid)


def _source_key(data: dict) -> str:
    """A source_event's feed event: "<source> <event id>" ("" when it doesn't say)."""
    ev = data.get("event") if isinstance(data, dict) else None
    src = data.get("source") if isinstance(data, dict) else None
    ref = ev.get("event_id") if isinstance(ev, dict) else None
    return f"{src}\x1f{ref}" if src and ref else ""

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
CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    audio BLOB,
    audio_dropped INTEGER NOT NULL DEFAULT 0,
    context INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    error TEXT NOT NULL,
    first_at REAL NOT NULL,
    last_at REAL NOT NULL,
    tries INTEGER NOT NULL DEFAULT 1,
    critical INTEGER NOT NULL DEFAULT 0,
    next_at REAL,
    state TEXT NOT NULL DEFAULT 'held'
);
CREATE TABLE IF NOT EXISTS source_latest (key TEXT PRIMARY KEY, seq INTEGER NOT NULL, at REAL NOT NULL);
"""


def _loads(text):
    """A quarantined row's data, or None where it is not JSON (one set aside as unreadable)."""
    try:
        return json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None


def _plain(o):
    """numpy scalars (a confidence, a flag) as plain JSON values."""
    return o.item() if hasattr(o, "item") else str(o)


class Outbox:
    def __init__(self, path: str | Path, max_audio_bytes: int = 300 * 1024 * 1024,
                 log: Callable[[str], None] | None = None, set_aside: bool = False, arm: bool = True):
        """set_aside (open_outbox, the station): a file SQLite cannot open is moved aside and a new one made (the
        module doc); otherwise its error is raised. arm=False (the quarantine command, which hands out no seq): no
        marker, the epoch only read, no other opener's marker touched."""
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.max_audio_bytes = int(max_audio_bytes)
        self.log = log or (lambda _m: None)
        self._lock = threading.Lock()
        self.notes: list[dict] = []     # this run's: {at, text} (a file set aside, a new epoch, a row unreadable)
        self.unreadable = 0             # this run: rows set aside as unreadable (batch())
        self._unreadable_said: set[str] = set()
        self.unclean = ""
        self._token = secrets.token_hex(8)
        self._armed = False
        self._arming = arm
        self._released = False
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            self._open()
        except BaseException as e:
            with _open_lock:
                _open_tokens.discard(self._token)
            if not (set_aside and self.path != ":memory:" and _cannot_open(e)):
                self.db.close()
                raise
            why = f"{type(e).__name__}: {e}"[:160]
            aside = _move_aside(Path(self.path))
            self.db.close()
            self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            try:
                self._open()
            except BaseException:
                with _open_lock:
                    _open_tokens.discard(self._token)
                self.db.close()
                raise
            self._note(f"file unreadable: moved aside as {aside.name}, new outbox",
                       f"the outbox file could not be opened ({why}): moved aside as {aside.name} (kept), a new outbox "
                       "started (a new epoch); what waited in the old one did not go to the hub")

    def _open(self) -> None:
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}
        if "context" not in cols:
            self.db.execute("ALTER TABLE outbox ADD COLUMN context INTEGER NOT NULL DEFAULT 0")
        if "qid" not in cols:               # a quarantined event sent again: its quarantine row (an older release
            self.db.execute("ALTER TABLE outbox ADD COLUMN qid INTEGER")     # ignores the column and just sends it)
        self.db.execute("CREATE INDEX IF NOT EXISTS outbox_qid ON outbox (qid) WHERE qid IS NOT NULL")
        if "prio" not in {r[1] for r in self.db.execute("PRAGMA table_info(stt_inputs)")}:    # (a lane before #677)
            self.db.execute(f"ALTER TABLE stt_inputs ADD COLUMN prio INTEGER NOT NULL DEFAULT {STT_NORMAL}")
        self.db.execute("CREATE INDEX IF NOT EXISTS stt_inputs_order ON stt_inputs (prio, n, at)")
        self.epoch: str = self._epoch_and_arm()
        self.audio_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox").fetchone()[0])
        self.dropped_audio = 0          # this run: events whose audio was dropped to stay under the limit
        self.context_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1").fetchone()[0])
        self.stt_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs").fetchone()[0])
        self.dropped_stt = 0            # this run: STT inputs dropped to stay under the limit (their clips kept)
        self.dropped_stt_pre = 0        # ... of them, the pre-alert channels' (only once nothing lower was left)
        self.dropped_stt_fire = 0       # ... and fire traffic's
        self.quarantine_evicted = 0     # this run: quarantined (non-critical) events dropped to stay under its bound
        self.too_large_audio = 0        # this run: clips left off an event the hub refused as too large with them

    # -- the epoch and the openers' markers (the module doc) ------------------------------------------------------

    def _note(self, short: str, said: str) -> None:
        """The outbox's own trouble: said in the log in full, and kept short for the station's status (Health's hint)."""
        self.log(f"outbox: {said}")
        self.notes = (self.notes + [{"at": time.time(), "text": short[:120]}])[-NOTES_KEPT:]

    def _durable(self, fn) -> None:
        """fn's writes in one transaction, synced to the disk (synchronous = FULL for that commit alone: in WAL mode
        the log is synced, and with it every commit before; fullfsync: on macOS the drive's cache too)."""
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.execute("PRAGMA fullfsync = 1")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                fn()
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:      # (a failed COMMIT may have rolled back already: its error, not ours)
                    self.db.execute("ROLLBACK")
                raise
        finally:
            self.db.execute("PRAGMA fullfsync = 0")
            self.db.execute("PRAGMA synchronous = NORMAL")

    def _arm(self) -> tuple[str, str | None]:
        """In one synced commit: the stale markers out, a new epoch if there were any (or none yet), this opener's
        marker in. (epoch, the epoch it replaced or None)."""
        out: dict = {}

        def tx() -> None:
            row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
            stale = [(k, v) for k, v in self.db.execute("SELECT key, value FROM meta WHERE key LIKE ? || '%'", (MARK,))
                     if not _marker_live(k[len(MARK):], v)]
            epoch = row[0] if row is not None else None
            if row is None or stale:
                epoch = secrets.token_hex(8)
                self.db.execute("INSERT INTO meta (key, value) VALUES ('epoch', ?) ON CONFLICT(key) DO UPDATE "
                                "SET value = excluded.value", (epoch,))
                for k, _v in stale:
                    self.db.execute("DELETE FROM meta WHERE key = ?", (k,))
                if row is not None:
                    out["was"] = row[0]
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            (MARK + self._token, json.dumps({"boot": boot_id(), "pid": os.getpid()})))
            out["epoch"] = epoch
        with _open_lock:
            _open_tokens.add(self._token)
        try:
            self._durable(tx)
        except BaseException:
            with _open_lock:
                _open_tokens.discard(self._token)
            raise
        self._armed = True
        if out.get("was"):
            self.unclean = "the last run did not stop in order (a power cut, a crash, or a copy taken while it was open)"
            self._note("new epoch: the last run did not stop in order",
                       f"{self.unclean}: a new epoch, so no event of the old one is skipped by the hub")
        return out["epoch"], out.get("was")

    def _epoch_and_arm(self) -> str:
        """The file's epoch (a new file: a new one), a new one after a stop that was not in order, and this opener's
        marker (_arm). Not armed (the quarantine command, or a write that failed: a full disk) the epoch is only read:
        batch() arms first, so no seq goes out unmarked."""
        if self.path == ":memory:" or not self._arming:
            row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
            if row is None:
                self.db.execute("INSERT INTO meta (key, value) VALUES ('epoch', ?)", (secrets.token_hex(8),))
                return self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()[0]
            return row[0]
        try:
            return self._arm()[0]
        except sqlite3.Error as e:
            row = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
            if _cannot_open(e) or row is None:
                raise
            self.log(f"outbox: its marker not written at the start ({type(e).__name__}: {str(e)[:120]}): "
                     "nothing goes to the hub until it is")
            return row[0]

    def _rearm(self) -> None:
        """Before a seq goes out unarmed (after resume(), or a start whose marker could not be written): _arm, and the
        epoch it may have changed."""
        if self._armed or self.path == ":memory:":
            return
        self.epoch = self._arm()[0]

    def release(self) -> None:
        """The station stops in order (StationMode.stop): this opener's marker goes, in a synced commit (every seq it
        handed out is then on the disk). The file stays open; batch() hands out nothing more until resume()."""
        with self._lock:
            self._released = True
            self._release()

    def resume(self) -> None:
        """The station starts again over the same outbox (StationMode.start): batch() arms first, then hands out."""
        with self._lock:
            self._released = False

    def _release(self) -> None:
        if not self._armed or self.path == ":memory:":
            return
        try:
            self._durable(lambda: self.db.execute("DELETE FROM meta WHERE key = ?", (MARK + self._token,)))
        except sqlite3.Error as e:          # (left in the file: the next start takes it as a stop not in order)
            self.log(f"outbox: its marker not taken out at the stop ({type(e).__name__}): a new epoch at the next start")
        finally:
            with _open_lock:
                _open_tokens.discard(self._token)
            self._armed = False

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
            try:
                if not isinstance(flac, bytes):
                    raise TypeError(f"flac stored as {type(flac).__name__}")
                doc, info = json.loads(data), json.loads(meta)
            except (ValueError, TypeError, RecursionError) as e:
                # (an input it cannot read back: not an event, gone; the clip stays in the station's store and the
                # hub transcribes its listening copy)
                self.stt_ack([n])
                self.unreadable += 1
                if "stt" not in self._unreadable_said:
                    self._unreadable_said.add("stt")
                    self._note(f"unreadable STT input #{n} dropped ({type(e).__name__})",
                              f"a speech-to-text input it cannot read back (#{n}, {type(e).__name__}) dropped; the "
                              "ones behind it go on")
                continue
            if out and size + len(flac) > max_bytes:
                break
            out.append({"n": n, "at": at, "data": doc, "flac": bytes(flac), "meta": info, "prio": prio})
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
            now = time.time() if at is None else at
            cur = self.db.execute("INSERT INTO outbox (at, kind, data, audio, context) VALUES (?, ?, ?, ?, ?)",
                                  (now, kind, json.dumps(data, separators=(",", ":"), default=_plain),
                                   audio, int(bool(context))))
            if kind == "source_event" and (key := _source_key(data)):
                # the latest snapshot of this feed event: a quarantined older one is never sent again (requeue_due)
                self.db.execute("INSERT INTO source_latest (key, seq, at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                                "SET seq = excluded.seq, at = excluded.at", (key, cur.lastrowid, now))
                self.db.execute("DELETE FROM source_latest WHERE at < ?", (now - SOURCE_LATEST_S,))
            self.audio_bytes += len(audio or b"")
            if context:
                self.context_bytes += len(audio or b"")
            self._limit_audio()
            return int(cur.lastrowid)

    def _limit_audio(self) -> None:
        """Over max_audio_bytes: the STT inputs first, then context audio, then the oldest clips (the events stay)."""
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

    def batch(self, max_bytes: int = 4 * 1024 * 1024, max_events: int = 200, meta: bool = False) -> list[dict]:
        """The oldest events, at least one, up to max_bytes of data + audio. meta: each also says whether it is
        context audio ("context") and, a quarantined event sent again, its quarantine row ("qid"). A row it cannot read
        back is set aside (_unreadable) and the ones behind it come instead. Every seq returned is under this opener's
        armed marker (the module doc)."""
        while True:
            out, size, aside = [], 0, False
            with self._lock:
                if self._released:
                    return []                   # (stopping: nothing more goes out, no marker written again)
                self._rearm()
                rows = self.db.execute("SELECT seq, at, kind, data, audio, audio_dropped, context, qid FROM outbox "
                                       "ORDER BY seq LIMIT ?", (max(1, int(max_events)),)).fetchall()
            for seq, at, kind, data, audio, dropped, context, qid in rows:
                try:
                    if not isinstance(at, (int, float)) or not isinstance(kind, str):
                        raise TypeError(f"at / type stored as {type(at).__name__} / {type(kind).__name__}")
                    if audio is not None and not isinstance(audio, bytes):
                        raise TypeError(f"audio stored as {type(audio).__name__}")
                    doc = json.loads(data)
                except (ValueError, TypeError, RecursionError) as e:
                    self._unreadable(seq, kind, e)
                    aside = True
                    continue
                n = len(data) + len(audio or b"")
                if out and size + n > max_bytes:
                    break
                out.append({"seq": seq, "at": at, "type": kind, "data": doc, "audio": audio,
                            "audio_dropped": bool(dropped), **({"context": bool(context), "qid": qid} if meta else {})})
                size += n
            if out or not aside:
                return out

    def _unreadable(self, seq: int, kind, e: BaseException) -> None:
        """A row the station cannot read back: moved to the quarantine (reason "unreadable", never critical, never sent
        again), its data kept as {"unreadable": its text} (JSON, so any build reads the quarantine), its audio as it
        is; a requeued copy (qid) turns its own quarantine row so. Said once per kind of error in the log and in the
        notes; the rows behind it go on."""
        now = time.time()
        error = f"{type(e).__name__}: {e}"[:QUARANTINE_ERROR]
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT at, kind, data, audio, audio_dropped, context, qid FROM outbox "
                                      "WHERE seq = ?", (int(seq),)).fetchone()
                if row is not None:
                    at, kd, data, audio, dropped, context, qid = row
                    raw = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
                    kept = json.dumps({"unreadable": raw}, separators=(",", ":"))
                    at = at if isinstance(at, (int, float)) and not isinstance(at, bool) else now
                    q = qid if isinstance(qid, int) else None
                    if q is not None and self.db.execute("UPDATE quarantine SET data = ?, reason = ?, error = ?, "
                                                         "last_at = ?, critical = 0, next_at = NULL, state = 'held' "
                                                         "WHERE id = ?", (kept, UNREADABLE, error, now, q)).rowcount:
                        pass
                    else:
                        self.db.execute(
                            "INSERT INTO quarantine (seq, at, kind, data, audio, audio_dropped, context, reason, error, "
                            "first_at, last_at, tries, critical, next_at, state) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, 'held')",
                            (int(seq), float(at), str(kd), kept, audio, int(bool(dropped)), int(bool(context)),
                             UNREADABLE, error, now, now))
                    self.db.execute("DELETE FROM outbox WHERE seq = ?", (int(seq),))
                    self._bound_quarantine()
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
            self._recount()
            self.unreadable += 1
        if type(e).__name__ not in self._unreadable_said:
            self._unreadable_said.add(type(e).__name__)
            self._note(f"unreadable {str(kind)[:40]} #{seq} set aside ({type(e).__name__})",
                       f"a {str(kind)[:40]} it cannot read back (#{seq}, {error[:120]}) set aside in the quarantine "
                       "(unreadable); the events behind it go on")

    def _recount(self) -> None:
        self.audio_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox").fetchone()[0])
        self.context_bytes = int(self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1").fetchone()[0])

    def ack(self, upto: int) -> int:
        """The hub has everything up to seq `upto`: gone from here."""
        with self._lock:
            n = self.db.execute("DELETE FROM outbox WHERE seq <= ?", (int(upto),)).rowcount
            self._recount()
            return n

    # -- quarantine (the module doc) -----------------------------------------------------------------------------

    def quarantine(self, row: dict, reason: str, error: str, now: float | None = None) -> dict:
        """The hub refused `row` (a batch() row) for good: out of the outbox (if still there), into the quarantine. A
        requeued copy (row["qid"]) updates its quarantine row instead. Critical (sent again) only for a CRITICAL_KINDS
        event the hub didn't call malformed (that never changes), and only for CRITICAL_TRIES or CRITICAL_FOR_S: then
        "stuck" (kept, the alert stays, a person drops it: `sizeup-station quarantine`). A clip over QUARANTINE_CLIP is
        not kept. Returns {id, kind, tries, critical, state, next_at}."""
        now = time.time() if now is None else now
        kind = str(row["type"])
        reason, error = str(reason or "rejected")[:40], str(error or "")[:QUARANTINE_ERROR]
        critical = kind in CRITICAL_KINDS and reason not in PERMANENT_REASONS
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                q = row.get("qid")
                cur = (self.db.execute("SELECT tries, first_at, critical FROM quarantine WHERE id = ?", (q,)).fetchone()
                       if q else None)
                if q and cur is None:
                    # a copy of a row a person dropped meanwhile (in flight then): it goes too, never back
                    self.db.execute("DELETE FROM outbox WHERE seq = ?", (int(row["seq"]),))
                    self.db.execute("COMMIT")
                    self._recount()
                    return {"id": int(q), "kind": kind, "tries": 0, "critical": False, "state": "dropped", "next_at": None}
                tries = (cur[0] + 1) if cur else 1
                first = cur[1] if cur else now
                critical = critical and (not cur or bool(cur[2]))
                state, nxt = "held", None
                if critical:
                    if tries > CRITICAL_TRIES or now - first > CRITICAL_FOR_S:
                        state = "stuck"
                    else:
                        nxt = now + RETRY_S[min(tries, len(RETRY_S)) - 1]
                if cur:
                    self.db.execute("UPDATE quarantine SET reason = ?, error = ?, last_at = ?, tries = ?, next_at = ?, "
                                    "critical = ?, state = ? WHERE id = ?",
                                    (reason, error, now, tries, nxt, int(critical), state, q))
                    qid = int(q)
                else:
                    audio = row.get("audio")
                    big = bool(audio) and len(audio) > QUARANTINE_CLIP
                    qid = int(self.db.execute(
                        "INSERT INTO quarantine (seq, at, kind, data, audio, audio_dropped, context, reason, error, "
                        "first_at, last_at, tries, critical, next_at, state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                        (int(row["seq"]), float(row.get("at") or now), kind,
                         json.dumps(row.get("data") or {}, separators=(",", ":"), default=_plain),
                         None if big else audio, int(bool(row.get("audio_dropped")) or big), int(bool(row.get("context"))),
                         reason, error, now, now, int(critical), nxt, state)).lastrowid)
                    if kind == "source_event" and (key := _source_key(row.get("data") or {})):
                        # an older quarantined snapshot of the same feed event: this one replaces it
                        for oid, odata in self.db.execute("SELECT id, data FROM quarantine WHERE kind = 'source_event' "
                                                          "AND id != ? AND seq < ?", (qid, int(row["seq"]))).fetchall():
                            if _source_key(_loads(odata)) == key:
                                self.db.execute("DELETE FROM quarantine WHERE id = ?", (oid,))
                self.db.execute("DELETE FROM outbox WHERE seq = ?", (int(row["seq"]),))
                self._bound_quarantine()
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._recount()
        return {"id": qid, "kind": kind, "tries": tries, "critical": critical, "state": state, "next_at": nxt}

    def _bound_quarantine(self) -> None:
        """At most QUARANTINE_ROWS rows (the oldest non-critical go; never a critical one) and QUARANTINE_AUDIO of clips
        (the oldest clips go, the non-critical ones' first: the rows stay, marked audio_dropped)."""
        while self.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] > QUARANTINE_ROWS:
            old = self.db.execute("SELECT id FROM quarantine WHERE critical = 0 ORDER BY id LIMIT 1").fetchone()
            if old is None:
                break
            self.db.execute("DELETE FROM quarantine WHERE id = ?", (old[0],))
            self.quarantine_evicted += 1
        while self.db.execute("SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM quarantine").fetchone()[0] > QUARANTINE_AUDIO:
            old = self.db.execute("SELECT id FROM quarantine WHERE audio IS NOT NULL ORDER BY critical, id LIMIT 1").fetchone()
            if old is None:
                break
            self.db.execute("UPDATE quarantine SET audio = NULL, audio_dropped = 1 WHERE id = ?", (old[0],))

    def requeue_due(self, now: float | None = None) -> list[dict]:
        """The critical quarantined events whose time has come (and that have no copy in the outbox yet): back at the end
        of the outbox, as a new event carrying qid, within the outbox's audio limit (add()'s). A source_event the feed
        has a newer snapshot of since (source_latest) is dropped instead: sending the older one would put back what the
        feed changed. [{id, kind, tries, superseded}]."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            # (a "late" alarm: never raised, said on Health for LATE_KEEP_S; nothing to do about it after that)
            self.db.execute("DELETE FROM quarantine WHERE state = 'late' AND last_at < ?", (now - LATE_KEEP_S,))
            due = self.db.execute("SELECT id, seq, kind, data, audio, audio_dropped, context, tries FROM quarantine "
                                  "WHERE critical = 1 AND next_at IS NOT NULL AND next_at <= ? AND id NOT IN "
                                  "(SELECT qid FROM outbox WHERE qid IS NOT NULL) ORDER BY id LIMIT 20", (now,)).fetchall()
            for qid, seq, kind, data, audio, dropped, context, tries in due:
                if kind == "source_event" and (key := _source_key(_loads(data))):
                    latest = self.db.execute("SELECT seq FROM source_latest WHERE key = ?", (key,)).fetchone()
                    if latest is not None and latest[0] > seq:
                        self.db.execute("DELETE FROM quarantine WHERE id = ?", (qid,))
                        out.append({"id": qid, "kind": kind, "tries": tries, "superseded": True})
                        continue
                # (queued now: the oldest event's age is the outbox's backlog on Health, never this one's first refusal;
                # the hub reads an event's times from its data)
                self.db.execute("INSERT INTO outbox (at, kind, data, audio, audio_dropped, context, qid) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)", (now, kind, data, audio, dropped, context, qid))
                self.audio_bytes += len(audio or b"")
                if context:
                    self.context_bytes += len(audio or b"")
                out.append({"id": qid, "kind": kind, "tries": tries, "superseded": False})
            if any(not x["superseded"] for x in out):
                self._limit_audio()
        return out

    def released(self, qids) -> int:
        """The hub took these requeued copies: their quarantine rows are gone."""
        qids = [int(q) for q in qids if q]
        if not qids:
            return 0
        with self._lock:
            return self.db.execute(f"DELETE FROM quarantine WHERE id IN ({','.join('?' * len(qids))})", qids).rowcount

    def late(self, qid: int, why: str, now: float | None = None) -> None:
        """The hub took a requeued copy too late to act on it (an alarm past alarms.MAX_AGE_S): kept, not sent again,
        state "late" (the critical alert stays: a person hears it was never raised, and drops it)."""
        now = time.time() if now is None else now
        with self._lock:
            self.db.execute("UPDATE quarantine SET state = 'late', next_at = NULL, reason = 'late', error = ?, last_at = ? "
                            "WHERE id = ?", (str(why)[:QUARANTINE_ERROR], now, int(qid)))

    def drop(self, qid: int | None = None) -> int:
        """A person drops a quarantined event (`sizeup-station quarantine drop ID|all`). None: all of them. A copy of it
        queued to go again goes too (never sent once dropped)."""
        where, args = ("", ()) if qid is None else (" WHERE id = ?", (int(qid),))
        return self._drop_where(where, args)

    def dismiss(self, kind: str) -> int:
        """An admin's Dismiss on Health (the hub's quarantine_dismiss call): this type's stuck and late rows go (one still
        being sent again stays: it may yet get through)."""
        return self._drop_where(" WHERE kind = ? AND state IN ('stuck', 'late')", (str(kind),))

    def _drop_where(self, where: str, args: tuple) -> int:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                ids = [i for (i,) in self.db.execute(f"SELECT id FROM quarantine{where}", args)]
                for k in range(0, len(ids), 500):
                    part = ids[k:k + 500]
                    marks = ",".join("?" * len(part))
                    self.db.execute(f"DELETE FROM outbox WHERE qid IN ({marks})", part)
                    self.db.execute(f"DELETE FROM quarantine WHERE id IN ({marks})", part)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._recount()
            return len(ids)

    def strip_audio(self, seq: int) -> bool:
        """An event the hub refuses as too large with its clip: sent without it from now on (audio_dropped). Counted in
        too_large_audio (not dropped_audio: that one is the outbox's own limit)."""
        with self._lock:
            k = self.db.execute("UPDATE outbox SET audio = NULL, audio_dropped = 1 WHERE seq = ? AND audio IS NOT NULL",
                                (int(seq),)).rowcount
            if k:
                self.too_large_audio += 1
                self._recount()
            return bool(k)

    def summary(self, limit: int = 10) -> dict:
        """The quarantine for the status: how many (critical), per kind ({kind: {n, critical, stuck, late, critical_first,
        minor_first, minor_last}}), and the
        newest rows, the critical first. Kinds, states, reasons and the hub's error only, never an event's data; an
        incident source's error never leaves the station (it may quote the county's data, #297)."""
        with self._lock:
            n, crit = self.db.execute("SELECT COUNT(*), COALESCE(SUM(critical), 0) FROM quarantine").fetchone()
            # per type, over every row (the rows below are a sample): the counts, and when the critical / the other
            # ones were first and last refused (Health's alerts are decided from these)
            kinds = {k: {"n": int(c), "critical": int(cr or 0), "stuck": int(st or 0), "late": int(la or 0),
                         "critical_first": cf, "minor_first": mf, "minor_last": ml}
                     for k, c, cr, st, la, cf, mf, ml in self.db.execute(
                         "SELECT kind, COUNT(*), SUM(critical), SUM(state = 'stuck'), SUM(state = 'late'), "
                         "MIN(CASE WHEN critical = 1 THEN first_at END), MIN(CASE WHEN critical = 0 THEN first_at END), "
                         "MAX(CASE WHEN critical = 0 THEN last_at END) FROM quarantine GROUP BY kind ORDER BY kind LIMIT 40")}
            rows = self.db.execute("SELECT id, kind, reason, error, first_at, last_at, tries, critical, next_at, state "
                                   "FROM quarantine ORDER BY critical DESC, id DESC LIMIT ?", (int(limit),)).fetchall()
        return {"n": int(n), "critical": int(crit or 0), "evicted": self.quarantine_evicted,
                "too_large_audio": self.too_large_audio, "kinds": kinds,
                "rows": [{"id": i, "kind": k, "reason": r, "error": "" if k.startswith("source_") else e, "first_at": f,
                          "last_at": la, "tries": t, "critical": bool(c), "next_at": nx, "state": st}
                         for i, k, r, e, f, la, t, c, nx, st in rows]}

    def pending(self) -> tuple[int, float | None]:
        """(events waiting, when the oldest was queued)."""
        with self._lock:
            n, oldest = self.db.execute("SELECT COUNT(*), MIN(at) FROM outbox").fetchone()
        return int(n), oldest

    def close(self) -> None:
        """Stopped in order: this opener's marker out (release), then the file closed."""
        with self._lock:
            try:
                self._release()
            finally:
                self.db.close()


def _cannot_open(e: BaseException) -> bool:
    """SQLite says the file is no database it can read (damaged, or a newer SQLite's): never a lock, a full disk or a
    folder it may not write, which moving the file would not mend."""
    if not isinstance(e, sqlite3.DatabaseError):
        return False
    name = getattr(e, "sqlite_errorname", "") or ""
    if name:
        return name in ("SQLITE_NOTADB", "SQLITE_CORRUPT") or name.startswith(("SQLITE_NOTADB_", "SQLITE_CORRUPT_"))
    return type(e) is sqlite3.DatabaseError and ("not a database" in str(e) or "malformed" in str(e))


def open_outbox(path: str | Path, max_audio_bytes: int = 300 * 1024 * 1024,
                log: Callable[[str], None] | None = None) -> Outbox:
    """The station's outbox. A file SQLite cannot open is moved aside (kept: <name>.unreadable-<time>, with its -wal
    and -shm), said in the log and in the new outbox's notes, and a new outbox is made (a new epoch): the station
    starts, over a file that only queues what goes to the hub."""
    return Outbox(path, max_audio_bytes, log=log, set_aside=True)


def _move_aside(src: Path) -> Path:
    """The file and its -wal and -shm, renamed beside it (kept for a person). Called while the connection that failed
    still has them open: SQLite deletes the log by its name when that connection closes, which is then no file."""
    aside = src.with_name(f"{src.name}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}")
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{src}{suffix}")
        if p.exists():
            p.replace(Path(f"{aside}{suffix}"))
    return aside


def main(argv: list[str] | None = None) -> int:
    """`sizeup-station quarantine [list | drop ID | drop all]` (python -m sizeup.cloud.outbox --path ... ): the events the
    hub refused, and a person dropping one (a critical one stuck, an alarm delivered too late, a page they have
    handled). The running station reads the quarantine afresh for every status: nothing to restart."""
    import argparse
    p = argparse.ArgumentParser(prog="sizeup-station quarantine")
    p.add_argument("--path", default="/var/lib/sizeup/state/outbox.sqlite")
    p.add_argument("cmd", nargs="?", default="list", choices=("list", "drop"))
    p.add_argument("what", nargs="?", default="")
    a = p.parse_args(argv)
    if not Path(a.path).is_file():
        print(f"no outbox at {a.path}")
        return 1
    ob = Outbox(a.path, arm=False)
    try:
        if a.cmd == "drop":
            if a.what != "all" and not a.what.isdigit():
                print("drop: an id from `sizeup-station quarantine`, or all")
                return 2
            n = ob.drop(None if a.what == "all" else int(a.what))
            print(f"dropped {n}")
            return 0 if n else 1
        s = ob.summary(limit=200)
        if not s["n"]:
            print("nothing refused by the hub")
        for r in s["rows"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["first_at"]))
            nxt = f", next try {time.strftime('%H:%M', time.localtime(r['next_at']))}" if r["next_at"] else ""
            print(f"{r['id']:>6}  {r['kind']:<12} {r['state']:<6} {'critical' if r['critical'] else '        '}  since {when}, "
                  f"refused {r['tries']}x{nxt}: {r['reason']}" + (f": {r['error'][:120]}" if r["error"] else ""))
        return 0
    finally:
        ob.close()


if __name__ == "__main__":
    raise SystemExit(main())
