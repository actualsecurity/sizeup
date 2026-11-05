"""The hub's audio.sqlite, restored after the CAD serves (GitHub #251).

audio.sqlite (the clips and the dispatch audio, hubstore.py AudioDb) is by far the biggest of the hub's three
databases, and nothing in the CAD needs it to work: only ▶. So the entrypoint (deploy/hub/entrypoint.sh) restores
cad.sqlite and hub.sqlite, starts the app under `litestream replicate` for those two, and leaves audio to this:

    1. `litestream restore -if-db-not-exists -if-replica-exists` of audio.sqlite alone (SIZEUP_AUDIO_LITESTREAM:
       deploy/hub/litestream-audio.yml), in a background thread. A failure is retried (RETRY_S), never papered
       over: an empty audio.sqlite is made only when the restore says there is no replica at all (a first start).
    2. The database opened (AudioDb), then its own `litestream replicate -config litestream-audio.yml`, started
       only now: replication never starts on a file that isn't restored yet (single writer: this process; the
       replica is written by that one Litestream only).
    3. Ready: what arrived meanwhile (the station's clips, dispatch audio) is written, in order.

Until then LateAudio stands in for the AudioDb: put() keeps the audio in memory (at most PENDING_MAX bytes, the
oldest dropped first, said in the log), get() answers from that, has() says True (the ▶ shows; the clip route
answers 503 "audio loading" with Retry-After while not ready: sizeup/web/server.py audio_ready), and the pruning
and the byte count wait. stop() ends the audio replicator with SIGTERM (Litestream's own final sync) before the app
exits; #247's standby can leave it unstarted (replicate=False: no replication anywhere but the lock holder).

Without SIZEUP_AUDIO_LITESTREAM (local, compose, the tests) audio.sqlite is opened at once, as before.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

RETRY_S = (5.0, 15.0, 30.0, 60.0, 120.0)        # a failed restore: tried again after these, then every 120 s
PENDING_MAX = 64 * 1024 * 1024                  # audio held in memory while the restore runs
STOP_WAIT_S = 15.0                              # the audio replicator's final sync, at most


class LateAudio:
    """AudioDb's interface (put / has / get / note / prune_clips / prune_dispatch / bytes) over an AudioDb that
    may not be open yet."""

    def __init__(self, open_db: Callable[[], object], path: str | Path = "", litestream_config: str = "",
                 litestream: str = "litestream", run: Callable | None = None, popen: Callable | None = None,
                 log: Callable[[str], None] = print, replicate: bool = True, sleep: Callable[[float], None] = time.sleep):
        self.open_db, self.path, self.config = open_db, Path(path) if path else None, litestream_config
        self.litestream, self.log, self.replicate, self.sleep = litestream, log, replicate, sleep
        self.run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=1800))
        self.popen = popen or (lambda cmd: subprocess.Popen(cmd))
        self.db = None
        self.state = "loading"                    # loading | ready
        self.error = ""
        self.ready = threading.Event()
        self.on_ready: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, tuple] = OrderedDict()      # id -> (wav, kind, note, at)
        self._pending_bytes = 0
        self._proc = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.took_s: float | None = None

    # -- opening ---------------------------------------------------------------------------------------------

    def open_now(self) -> LateAudio:
        """No restore to wait for: open it here and now (local, compose, the tests)."""
        self._ready(self.open_db())
        return self

    def start(self) -> LateAudio:
        """The restore, then replication, in the background."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="audio-restore", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        t0 = time.monotonic()
        k = 0
        while not self._stop.is_set():
            why = self._restore()
            if not why:
                break
            wait = RETRY_S[min(k, len(RETRY_S) - 1)]
            k += 1
            self.error = why
            self.log(f"audio: audio.sqlite not restored ({why}); clips play once it is; again in {wait:.0f} s")
            if self._stop.wait(wait):
                return
        if self._stop.is_set():
            return
        try:
            db = self.open_db()
        except Exception as e:  # a broken restored file: said, and the CAD goes on without clips
            self.error = f"audio.sqlite not opened: {type(e).__name__}: {e}"
            self.log(f"audio: {self.error}")
            return
        if self.replicate and self.config:
            self._proc = self.popen([self.litestream, "replicate", "-config", self.config])
        self.took_s = round(time.monotonic() - t0, 1)
        n = self._ready(db)
        self.log(f"audio: audio.sqlite restored and open in {self.took_s:.1f} s"
                 + (", replicating" if self._proc is not None else ", not replicated here")
                 + (f"; {n} clip(s) that came meanwhile written" if n else ""))

    def _restore(self) -> str:
        """'' when audio.sqlite is here (restored, or there is no replica: a first start), else why not."""
        if self.path is None or not self.config:
            return ""
        try:
            r = self.run([self.litestream, "restore", "-config", self.config, "-if-db-not-exists", "-if-replica-exists",
                          str(self.path)])
        except Exception as e:  # no binary, a timeout: tried again
            return f"{type(e).__name__}: {e}"[:200]
        if r.returncode != 0:
            return (f"litestream restore exit {r.returncode}: " + (r.stderr or r.stdout or "").strip()[-200:]).strip()
        return ""

    def _ready(self, db) -> int:
        with self._lock:
            self.db = db
            pending, self._pending, self._pending_bytes = self._pending, OrderedDict(), 0
            for aid, (wav, kind, note, at) in pending.items():
                db.put(aid, wav, kind, note, at)
            self.state = "ready"
            self.error = ""
            self.ready.set()
        for fn in list(self.on_ready):
            try:
                fn()
            except Exception as e:  # one listener's trouble: never the audio's
                self.log(f"audio: on ready: {type(e).__name__}: {e}")
        return len(pending)

    def stop(self) -> None:
        """The audio replicator's final sync (SIGTERM), before the app exits."""
        self._stop.set()
        p = self._proc
        if p is None or p.poll() is not None:
            return
        try:
            p.send_signal(signal.SIGTERM)
            p.wait(STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            self.log(f"audio: the audio replicator didn't stop in {STOP_WAIT_S:.0f} s: killed")
            p.kill()
        except OSError:
            pass

    # -- AudioDb's interface -----------------------------------------------------------------------------------

    def put(self, aid: str, wav: bytes, kind: str = "clip", note: str = "", at: float | None = None) -> None:
        with self._lock:
            if self.db is not None:
                return self.db.put(aid, wav, kind, note, at)
            old = self._pending.pop(aid, None)
            if old is not None:
                self._pending_bytes -= len(old[0])
            self._pending[aid] = (wav, kind, note, time.time() if at is None else at)
            self._pending_bytes += len(wav)
            dropped = 0
            while self._pending_bytes > PENDING_MAX and len(self._pending) > 1:
                _, (w, *_r) = self._pending.popitem(last=False)
                self._pending_bytes -= len(w)
                dropped += 1
        if dropped:
            self.log(f"audio: {dropped} clip(s) dropped: over {PENDING_MAX // (1024 * 1024)} MB came while audio.sqlite "
                     "was being restored")

    def has(self, aid: str) -> bool:
        db = self.db
        if db is not None:
            return db.has(aid)
        return True                               # not known yet: ▶ shows, and the clip says "audio loading"

    def get(self, aid: str) -> bytes | None:
        with self._lock:
            db = self.db
            if db is None:
                p = self._pending.get(aid)
                return p[0] if p else None
        return db.get(aid)

    def note(self, aid: str) -> str:
        with self._lock:
            db = self.db
            if db is None:
                p = self._pending.get(aid)
                return p[2] if p else ""
        return db.note(aid)

    def prune_clips(self, *a, **k) -> int:
        db = self.db
        return db.prune_clips(*a, **k) if db is not None else 0

    def prune_dispatch(self, *a, **k) -> int:
        db = self.db
        return db.prune_dispatch(*a, **k) if db is not None else 0

    def bytes(self) -> int:
        db = self.db
        return db.bytes() if db is not None else 0

    def __getattr__(self, name: str):             # anything else AudioDb has (retention, vacuum): once it is open
        if name.startswith("_") or name in ("db",):
            raise AttributeError(name)
        db = self.__dict__.get("db")
        if db is None:
            raise AttributeError(f"{name}: audio.sqlite is still loading")
        return getattr(db, name)


def from_env() -> str:
    """The audio replica's Litestream config when the entrypoint left audio to the app, else ''."""
    return os.environ.get("SIZEUP_AUDIO_LITESTREAM", "").strip()
