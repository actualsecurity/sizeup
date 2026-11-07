"""Remote resets of incident-source adapters (#297, used by #305's sources_reset job): an adapter that stopped itself to
protect a shared account starts over when a person asks. The owner of Resets: sizeup/sources/runner.py keeps one
(SourceRunner.resets) and sizeup/cloud/adapters.py (#305) queues requests into it. Standard library only; it imports
nothing of sizeup, so nothing can import it in a circle."""

from __future__ import annotations

import threading
from typing import Callable


class Resets:
    """Reset requests, each run on its adapter's own thread: request() from anywhere (the hub's sources_reset job, the
    station's `adapter reset`), run_due() by the runner's loop for that adapter before its next poll."""

    def __init__(self):
        self._want: dict[str, str] = {}             # source -> who asked
        self._lock = threading.Lock()
        self.wake: Callable[[str], None] | None = None   # the runner's: cut a back-off short for that source

    def request(self, source: str, by: str = "") -> None:
        with self._lock:
            self._want[source] = by
        if self.wake is not None:
            self.wake(source)

    def pending(self, source: str) -> bool:
        with self._lock:
            return source in self._want

    def run_due(self, adapter, log: Callable[[str], None] = print) -> str | None:
        """On the adapter's thread: its reset(), if one was asked for. What happened (None: nothing asked)."""
        source = str(getattr(adapter, "source", ""))
        with self._lock:
            if source not in self._want:
                return None
            by = self._want.pop(source)
        who = f" (asked by {by})" if by else ""
        fn = getattr(adapter, "reset", None)
        if not callable(fn):
            log(f"incident sources: {source}: reset{who}: it has no reset(): nothing to do")
            return "no reset(): nothing to do"
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - an adapter's bug never stops the station
            log(f"incident sources: {source}: reset{who} failed: {type(e).__name__}")
            return f"failed: {type(e).__name__}"
        log(f"incident sources: {source}: reset{who}")
        return "reset"
