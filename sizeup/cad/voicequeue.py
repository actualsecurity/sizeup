"""The dispatch voice, rendered after the answer (#426).

A dispatch is logged and answered at once; its voice (2-3 s of Polly, or the station's) is a job here. One worker
takes the jobs in the order they were put: two dispatches on an event are rendered, keyed and attached in that
order, and nothing synthesizes twice at a time. The queue is bounded: a job that doesn't fit is refused (put()
False) and the caller fails its row at once, so a dispatch never waits for room.

    q = VoiceQueue(work, log)       work(job): render, transmit, write the row; never raises to the worker
    q.put(job)                      False: full, or stopped
    q.wait(timeout)                 True once nothing is queued or being worked on (the tests, a drain)
    q.stop(timeout)                 no new jobs; waits; the jobs not finished (queued: dropped; being worked on: it ends)
    q.join(timeout)                 after stop(): the worker thread ended
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable

MAX_QUEUED = 32          # dispatches waiting for their voice; more than this is refused (the row: failed, text only)


class VoiceQueue:
    def __init__(self, work: Callable[[object], None], log: Callable[[str], None] = print, size: int = MAX_QUEUED):
        self.work, self.log, self.size = work, log, size
        self._cond = threading.Condition()
        self._jobs: deque = deque()
        self._busy: object | None = None
        self._thread: threading.Thread | None = None
        self.stopped = False

    def put(self, job) -> bool:
        with self._cond:
            if self.stopped or len(self._jobs) >= self.size:
                return False
            self._jobs.append(job)
            if self._thread is None:                  # made on the first dispatch: most Cads (tests, tools) never render
                self._thread = threading.Thread(target=self._run, name="dispatch-voice", daemon=True)
                self._thread.start()
            self._cond.notify_all()
            return True

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._jobs or self.stopped)
                if not self._jobs:
                    return
                job = self._busy = self._jobs.popleft()
            try:
                self.work(job)
            except Exception as e:  # noqa: BLE001 - the next dispatch's voice still goes out
                self.log(f"CAD: dispatch voice: {type(e).__name__}: {e}")
            finally:
                with self._cond:
                    self._busy = None
                    self._cond.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._jobs and self._busy is None, timeout)

    def join(self, timeout: float | None = None) -> bool:
        """After stop(): the worker joined (within timeout). True once it has ended (or never started)."""
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
            return not t.is_alive()
        return True

    def stop(self, timeout: float = 0.0) -> list:
        """No new jobs. What is queued gets `timeout` to finish; returns the jobs that didn't (they are dropped; the
        one being worked on runs to its end, and its late result is the caller's to ignore)."""
        with self._cond:
            self.stopped = True
            self._cond.notify_all()
            self._cond.wait_for(lambda: not self._jobs and self._busy is None, timeout)
            left = ([self._busy] if self._busy is not None else []) + list(self._jobs)
            self._jobs.clear()
            return left
