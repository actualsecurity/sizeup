"""Every transcript an engine produced, as it produced it (#639; the intel session's requirements): appended, never
overwritten, so the raw text, which engine and model wrote it, its confidence (per word where the engine gives it) and
its timing outlive any re-run, correction or later engine. The radio log's row (radio_traffic: text, model, alt_text)
works as before; this is next to it, keyed by the same clip id.

    cad.sqlite "stt_runs"   made by the first write (never at start: tests/test_startup_writes.py). One row a transcript:
                            clip_id, engine (gpu-worker | aws | station-whisper), model, worker, prompt_v (a short hash of
                            the prompt, "" when none was sent), role (text | alt | late | rerun | catchup), text (the
                            engine's raw words, before the no-speech rule), status, confidence, words (JSON [[word, p,
                            start, end], ...] where the engine gives them), channel, class, clip_end, queued_at,
                            engine_start, done_at (the hub's clock), wait_s (queued -> the engine took it) and run_s
                            (the engine's own time; the station's whisper measures both itself), keyup (a cut
                            transmission's {parent, prev, split} as the radio sends it, JSON; "" for a whole one). Rows older than KEEP_DAYS are pruned by the hub's
                            janitor, in small batches
    LatencyBook             transmission end -> text on the hub, per channel, over WINDOW_S in memory: p50 / p95 and
                            the engines that wrote it (Settings > Station > Health). The pre-alert channels' target:
                            p50 <= TARGET_P50_S, p95 <= TARGET_P95_S
Writing here never stops a transcript: a failure is logged (once a while) and forgotten."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from typing import Callable

from sizeup.stt import CONTEXT, CONTEXT_FALLBACK, HIGH, URGENT

SCHEMA = """CREATE TABLE IF NOT EXISTS stt_runs (
  id INTEGER PRIMARY KEY, clip_id TEXT NOT NULL, engine TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
  worker TEXT NOT NULL DEFAULT '', prompt_v TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'text',
  text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', confidence REAL, words TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '', class TEXT NOT NULL DEFAULT '', clip_end REAL, queued_at REAL, engine_start REAL,
  done_at REAL NOT NULL, wait_s REAL, run_s REAL, keyup TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS stt_runs_clip ON stt_runs (clip_id);
CREATE INDEX IF NOT EXISTS stt_runs_done ON stt_runs (done_at);"""
COLUMNS = ("clip_id", "engine", "model", "worker", "prompt_v", "role", "text", "status", "confidence", "words",
           "channel", "class", "clip_end", "queued_at", "engine_start", "done_at", "wait_s", "run_s", "keyup")
ENGINES = ("gpu-worker", "aws", "station-whisper")
ROLES = ("text", "alt", "late", "rerun", "catchup")
KEEP_DAYS = 90.0
PRUNE_BATCH = 500
WINDOW_S = 7200.0
TARGET_P50_S, TARGET_P95_S = 5.0, 15.0
TARGET_CLASSES = (URGENT, HIGH, CONTEXT, CONTEXT_FALLBACK)   # pages, Fire/EMS dispatch, the airband, police dispatch
WORDS_MAX = 4000
SAY_EVERY_S = 600.0


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
    """The append-only log. store: the CAD's Store (tx()); clock for the tests."""

    def __init__(self, store, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.store, self.clock, self.log = store, clock, log
        self._made = False
        self._said = 0.0
        self._lock = threading.Lock()

    def _say(self, msg: str) -> None:
        now = self.clock()
        if now - self._said >= SAY_EVERY_S:
            self._said = now
            self.log(msg)

    def add(self, clip_id: str, engine: str, *, text: str = "", status: str = "", model: str = "", worker: str = "",
            prompt_v: str = "", role: str = "text", confidence: float | None = None, words: list | None = None,
            channel: str = "", cls: str = "", clip_end: float | None = None, queued_at: float | None = None,
            engine_start: float | None = None, done_at: float | None = None, wait_s: float | None = None,
            run_s: float | None = None, keyup: dict | None = None) -> bool:
        """One transcript. True when it was written."""
        row = (str(clip_id)[:128], engine if engine in ENGINES else "other", str(model or "")[:120], str(worker or "")[:40],
               str(prompt_v or "")[:20], role if role in ROLES else "text", str(text or "")[:20000],
               str(status or "")[:20], float(confidence) if isinstance(confidence, (int, float)) else None,
               json.dumps(words[:WORDS_MAX], separators=(",", ":")) if words else "", str(channel or "")[:60],
               str(cls or "")[:20], clip_end, queued_at, engine_start, self.clock() if done_at is None else done_at,
               _sec(wait_s), _sec(run_s), _keyup(keyup))
        try:
            with self.store.tx() as db:
                if not self._made:              # (statement by statement: executescript would commit)
                    for stmt in SCHEMA.split(";"):
                        if stmt.strip():
                            db.execute(stmt)
                db.execute(f"INSERT INTO stt_runs ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})", row)
            self._made = True
            return True
        except Exception as e:  # noqa: BLE001 - bookkeeping: never in the way of the transcript
            self._say(f"stt runs: not recorded: {type(e).__name__}: {e}")
            return False

    def of(self, clip_id: str) -> list[dict]:
        """Every run of a clip, oldest first (words parsed)."""
        try:
            rows = self.store.q(f"SELECT {', '.join(COLUMNS)} FROM stt_runs WHERE clip_id = ? ORDER BY id", (clip_id,))
        except Exception:  # noqa: BLE001 - no table yet: none
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["words"] = json.loads(d["words"]) if d.get("words") else []
            d["keyup"] = json.loads(d["keyup"]) if d.get("keyup") else None
            out.append(d)
        return out

    def prune(self, now: float | None = None, keep_days: float = KEEP_DAYS, batch: int = PRUNE_BATCH) -> int:
        """Rows done more than keep_days ago, at most `batch` of them: how many went."""
        now = self.clock() if now is None else now
        try:
            with self.store.tx() as db:
                n = db.execute("DELETE FROM stt_runs WHERE id IN (SELECT id FROM stt_runs WHERE done_at < ? "
                               "ORDER BY done_at LIMIT ?)", (now - keep_days * 86400.0, int(batch))).rowcount
            return int(n or 0)
        except Exception as e:  # noqa: BLE001 - no table yet, or not the leader: nothing to do
            if "no such table" not in str(e):
                self._say(f"stt runs: prune: {type(e).__name__}: {e}")
            return 0


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
