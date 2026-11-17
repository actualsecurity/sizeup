"""Every transcript an engine produced, as it produced it (#639; the intel session's requirements): appended, never
overwritten, so the raw text, which engine and model wrote it, its confidence (per word where the engine gives it) and
its timing outlive any re-run, correction or later engine. The radio log's row (radio_traffic: text, model, alt_text)
works as before; this is next to it, keyed by the same clip id.

    stt.sqlite "stt_runs"   its own file next to the hub's databases, made by the first write. Not cad.sqlite and not in
                            Litestream (the handover never carries it): copies in the backups bucket (s3://.../stt/, the
                            station metrics' scheme), merged in when a new task settles, a copy every SNAPSHOT_S and when
                            the hub stops. One row a transcript:
                            clip_id, engine (gpu-worker | aws | station-whisper), model, worker, prompt_v (a short hash of
                            the prompt, "" when none was sent), role (text | alt | late | rerun | catchup), text (the
                            engine's raw words, before the no-speech rule), status, confidence, words (JSON [[word, p,
                            start, end], ...] where the engine gives them), channel, class, clip_end, queued_at,
                            engine_start, done_at (the hub's clock), wait_s (queued -> the engine took it) and run_s
                            (the engine's own time; the station's whisper measures both itself), keyup (a cut
                            transmission's {parent, prev, split} as the radio sends it, JSON; "" for a whole one), call_id
                            and turn (a P25 call split per transmission: its call and 0-based turn). Pruned by the hub's
                            janitor: older than KEEP_DAYS, and a run whose radio log row is gone (its channel's transcript
                            retention: never longer than the transcript), batch after batch for up to PRUNE_BUDGET_S
    LatencyBook             transmission end -> text on the hub, per channel, over WINDOW_S in memory: p50 / p95 and
                            the engines that wrote it (Settings > Station > Health). The pre-alert channels' target:
                            p50 <= TARGET_P50_S, p95 <= TARGET_P95_S
Writing here never stops a transcript: a failure is logged (once a while) and forgotten."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from sizeup.stt import CONTEXT, CONTEXT_FALLBACK, HIGH, URGENT

SCHEMA = """CREATE TABLE IF NOT EXISTS stt_runs (
  id INTEGER PRIMARY KEY, clip_id TEXT NOT NULL, engine TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
  worker TEXT NOT NULL DEFAULT '', prompt_v TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'text',
  text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', confidence REAL, words TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '', class TEXT NOT NULL DEFAULT '', clip_end REAL, queued_at REAL, engine_start REAL,
  done_at REAL NOT NULL, wait_s REAL, run_s REAL, keyup TEXT NOT NULL DEFAULT '', call_id TEXT NOT NULL DEFAULT '',
  turn INTEGER);
CREATE INDEX IF NOT EXISTS stt_runs_clip ON stt_runs (clip_id);
CREATE INDEX IF NOT EXISTS stt_runs_done ON stt_runs (done_at);"""
COLUMNS = ("clip_id", "engine", "model", "worker", "prompt_v", "role", "text", "status", "confidence", "words",
           "channel", "class", "clip_end", "queued_at", "engine_start", "done_at", "wait_s", "run_s", "keyup", "call_id", "turn")
ENGINES = ("gpu-worker", "aws", "station-whisper")
ROLES = ("text", "alt", "late", "rerun", "catchup")
KEEP_DAYS = 90.0
PRUNE_BATCH = 500
PRUNE_BUDGET_S = 2.0       # a janitor pass prunes for this long at most (batch after batch: what came in goes out)
ORPHAN_AFTER_S = 3600.0    # a run whose radio log row is gone, looked for once this old
SNAPSHOT_S = 3 * 3600.0    # a copy to the bucket this often (and when the hub stops) ...
FIRST_UPLOAD_S = 900.0     # ... the first this long after settling
SETTLE_RETRY_S = 60.0
WINDOW_S = 7200.0
TARGET_P50_S, TARGET_P95_S = 5.0, 15.0
TARGET_CLASSES = (URGENT, HIGH, CONTEXT, CONTEXT_FALLBACK)   # pages, Fire/EMS dispatch, the airband, police dispatch
WORDS_MAX = 4000
SAY_EVERY_S = 600.0


def _fin(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _sec(v) -> float | None:
    return round(float(v), 3) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e6 else None


def _keyup(k) -> str:
    """A cut transmission's piece ({parent, prev, split}: the radio's key-up split), as JSON; "" for none."""
    if not isinstance(k, dict):
        return ""
    return json.dumps({f: str(k.get(f) or "")[:128] for f in ("parent", "prev", "split")}, separators=(",", ":"))


def prompt_version(prompt: str) -> str:
    """A short, stable name for a prompt (its sha1's first 10 hex digits), "" for none."""
    return hashlib.sha1(prompt.encode()).hexdigest()[:10] if prompt else ""


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return round(s[k], 1)


class Runs:
    """The append-only log, in its own file (stt.sqlite next to the hub's databases; not cad.sqlite, not in Litestream:
    the handover never carries it). may_write(): this task is the leader. backup: where its copies go (an S3Copies of
    sizeup/cloud/metricsbackup.py with sub "stt"), None for none. exists(ids) -> the ids still in the radio log (a run
    whose row was pruned goes with it)."""

    def __init__(self, path=":memory:", may_write: Callable[[], bool] = lambda: True, backup=None,
                 exists: Callable[[list[str]], set] | None = None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print):
        self.path, self.may_write, self.backup, self.exists = str(path), may_write, backup, exists
        self.clock, self.log = clock, log
        self._db: sqlite3.Connection | None = None
        self._said = 0.0
        self._lock = threading.Lock()
        self.settled = backup is None              # the copies looked at (merged, or none there)
        self._settle_at = 0.0                      # (a failed look: again then)
        self._uploaded = 0.0
        self._sweep_from = 0                       # the orphan sweep's place (row id)
        self.status = {"state": "settled" if backup is None else "unsettled", "error": "", "uploaded": None}

    def _say(self, msg: str) -> None:
        now = self.clock()
        if now - self._said >= SAY_EVERY_S:
            self._said = now
            self.log(msg)

    def _open(self) -> sqlite3.Connection:
        """The file, made on the first write (never at start)."""
        if self._db is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.execute("PRAGMA busy_timeout = 5000")
            db.executescript(SCHEMA)
            self._db = db
        return self._db

    def _exists(self) -> bool:
        return self._db is not None or (self.path != ":memory:" and Path(self.path).exists())

    def add(self, clip_id: str, engine: str, *, text: str = "", status: str = "", model: str = "", worker: str = "",
            prompt_v: str = "", role: str = "text", confidence: float | None = None, words: list | None = None,
            channel: str = "", cls: str = "", clip_end: float | None = None, queued_at: float | None = None,
            engine_start: float | None = None, done_at: float | None = None, wait_s: float | None = None,
            run_s: float | None = None, keyup: dict | None = None, call_id: str = "", turn: int | None = None) -> bool:
        """One transcript. True when it was written."""
        row = (str(clip_id)[:128], engine if engine in ENGINES else "other", str(model or "")[:120], str(worker or "")[:40],
               str(prompt_v or "")[:20], role if role in ROLES else "text", str(text or "")[:20000],
               str(status or "")[:20], _fin(confidence),
               json.dumps(words[:WORDS_MAX], separators=(",", ":")) if words else "", str(channel or "")[:60],
               str(cls or "")[:20], _fin(clip_end), _fin(queued_at), _fin(engine_start),
               self.clock() if done_at is None else done_at, _sec(wait_s), _sec(run_s), _keyup(keyup),
               str(call_id or "")[:128], int(turn) if isinstance(turn, int) and not isinstance(turn, bool) else None)
        if not self.may_write():
            return False
        try:
            with self._lock:
                self._open().execute(f"INSERT INTO stt_runs ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                                     row)
            return True
        except Exception as e:  # noqa: BLE001 - bookkeeping: never in the way of the transcript
            self._say(f"stt runs: not recorded: {type(e).__name__}: {e}")
            return False

    def of(self, clip_id: str) -> list[dict]:
        """Every run of a clip, oldest first (words parsed)."""
        if not self._exists():
            return []
        try:
            with self._lock:
                cur = self._open().execute(f"SELECT {', '.join(COLUMNS)} FROM stt_runs WHERE clip_id = ? ORDER BY id",
                                           (clip_id,))
                rows = [dict(zip(COLUMNS, r)) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001 - none
            return []
        for d in rows:
            d["words"] = json.loads(d["words"]) if d.get("words") else []
            d["keyup"] = json.loads(d["keyup"]) if d.get("keyup") else None
        return rows

    def prune(self, now: float | None = None, keep_days: float = KEEP_DAYS, batch: int = PRUNE_BATCH,
              budget_s: float = PRUNE_BUDGET_S) -> int:
        """Rows done more than keep_days ago, and rows whose radio log row is gone (its channel's transcript retention
        pruned it: a run never outlives its transcript), batch by batch until none is left or budget_s is spent (sized
        to what comes in). How many went."""
        if not self._exists() or not self.may_write():
            return 0
        now = self.clock() if now is None else now
        t0, n = time.monotonic(), 0
        try:
            while time.monotonic() - t0 < budget_s:
                with self._lock:
                    k = self._open().execute("DELETE FROM stt_runs WHERE id IN (SELECT id FROM stt_runs WHERE done_at < ? "
                                             "ORDER BY done_at LIMIT ?)", (now - keep_days * 86400.0, int(batch))).rowcount
                n += k
                if k < batch:
                    break
            while self.exists is not None and time.monotonic() - t0 < budget_s:
                with self._lock:
                    got = self._open().execute("SELECT id, clip_id FROM stt_runs WHERE id > ? AND done_at < ? ORDER BY id "
                                               "LIMIT ?", (self._sweep_from, now - ORPHAN_AFTER_S, int(batch))).fetchall()
                if not got:
                    self._sweep_from = 0               # round again next time
                    break
                keep = self.exists(sorted({c for _i, c in got}))
                gone = [i for i, c in got if c not in keep]
                if gone:
                    with self._lock:
                        n += self._open().execute(f"DELETE FROM stt_runs WHERE id IN ({', '.join('?' * len(gone))})",
                                                  gone).rowcount
                self._sweep_from = got[-1][0]
        except Exception as e:  # noqa: BLE001 - again next time
            self._say(f"stt runs: prune: {type(e).__name__}: {e}")
        return n

    # -- the copies (like the station metrics': sizeup/cloud/metricsbackup.py) ------------------------------------------

    def backup_turn(self, now: float | None = None, force: bool = False) -> str:
        """The leader's turn: settle (the newest copy's rows from before ours merged in), then a copy every
        SNAPSHOT_S (force: now, the hub stopping). What it did ("" nothing)."""
        if self.backup is None or not self.may_write():
            return ""
        now = self.clock() if now is None else now
        try:
            if not self.settled:
                if now < self._settle_at:
                    return ""
                return self._settle()
            if force or now - self._uploaded >= SNAPSHOT_S:
                if not self._exists():
                    return ""
                self._upload(now)
                return "uploaded"
        except Exception as e:  # noqa: BLE001 - again later
            self._settle_at = now + SETTLE_RETRY_S
            self.status["error"] = f"{type(e).__name__}: {e}"[:200]
            self._say(f"stt runs: copies: {self.status['error']}")
        return ""

    def _settle(self) -> str:
        import os
        import tempfile
        from sizeup.cloud.metricsbackup import next_slot
        copies = self.backup.list()               # (raises when the listing fails: never "none" for no answer)
        if copies:
            raw = self.backup.get(copies[0]["slot"])
            fd, tmp = tempfile.mkstemp(suffix=".sqlite")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                with self._lock:
                    db = self._open()
                    db.execute("ATTACH DATABASE ? AS copy", (tmp,))
                    try:
                        first = db.execute("SELECT MIN(done_at) FROM stt_runs").fetchone()[0]
                        cols = ", ".join(COLUMNS)
                        db.execute(f"INSERT INTO stt_runs ({cols}) SELECT {cols} FROM copy.stt_runs WHERE done_at < ?",
                                   (first if first is not None else float("inf"),))
                    finally:
                        db.execute("DETACH DATABASE copy")
            finally:
                os.unlink(tmp)
        self.settled, self._next_slot = True, next_slot(copies)
        self._uploaded = self.clock() - SNAPSHOT_S + FIRST_UPLOAD_S     # (the first copy a little after settling)
        self.status.update(state="settled", error="")
        return "settled"

    def _upload(self, now: float) -> None:
        from sizeup.cloud.metricsbackup import next_slot
        with self._lock:
            raw = self._open().serialize()
        slot = next_slot(self.backup.list())
        self.backup.put(slot, raw)
        self._uploaded = now
        self.status.update(uploaded=now, error="")


class LatencyBook:
    """Transmission end -> text, per channel, over WINDOW_S (memory only: Health's numbers since the hub started)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._rows: dict[str, deque] = {}
        self._target: dict[str, bool] = {}
        self._lock = threading.Lock()

    def add(self, channel: str, engine: str, latency_s: float, target: bool = False, at: float | None = None) -> None:
        if not channel or not isinstance(latency_s, (int, float)) or latency_s < -30 or latency_s > 86400:
            return
        latency_s = max(0.0, float(latency_s))          # (a few seconds under 0: the station's clock against ours)
        at = self.clock() if at is None else at
        with self._lock:
            q = self._rows.setdefault(channel, deque())
            q.append((at, engine, float(latency_s)))
            self._target[channel] = self._target.get(channel, False) or target
            while q and at - q[0][0] > WINDOW_S:
                q.popleft()

    def table(self) -> list[dict]:
        """[{channel, n, p50_s, p95_s, engines: {engine: n}, target, over}], the target channels first, then the busiest."""
        now = self.clock()
        out = []
        with self._lock:
            for ch, q in list(self._rows.items()):
                while q and now - q[0][0] > WINDOW_S:
                    q.popleft()
                if not q:
                    self._rows.pop(ch, None)
                    continue
                xs = [x[2] for x in q]
                eng: dict[str, int] = {}
                for x in q:
                    eng[x[1]] = eng.get(x[1], 0) + 1
                p50, p95 = _pct(xs, 50), _pct(xs, 95)
                tgt = self._target.get(ch, False)
                out.append({"channel": ch, "n": len(xs), "p50_s": p50, "p95_s": p95, "engines": eng, "target": tgt,
                            "over": bool(tgt and ((p50 or 0) > TARGET_P50_S or (p95 or 0) > TARGET_P95_S))})
        out.sort(key=lambda r: (not r["target"], -r["n"], r["channel"]))
        return out
