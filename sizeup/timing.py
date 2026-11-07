"""Where the time goes: one log line per startup / takeover / drain (GitHub #264).

    t = Phases("takeover")
    ... t.mark("plan") ... t.mark("catch-up") ...
    log(t.line())     # "takeover: plan 3.1 s · catch-up 0.4 s · ... (total 9.7 s)"

A phase is the time since the previous mark (or since the start). Marking the same name again adds to it.
"""

from __future__ import annotations

import time
from typing import Callable


class Phases:
    def __init__(self, what: str, clock: Callable[[], float] = time.monotonic):
        self.what, self.clock = what, clock
        self.t0 = self.last = clock()
        self.parts: dict[str, float] = {}

    def mark(self, name: str) -> float:
        """The time since the previous mark, put down as `name`."""
        now = self.clock()
        d = now - self.last
        self.parts[name] = self.parts.get(name, 0.0) + d
        self.last = now
        return d

    def skip(self) -> None:
        """Leave out the time since the previous mark (waiting on something else: not ours to count)."""
        self.last = self.clock()

    def total(self) -> float:
        return self.last - self.t0

    def line(self, extra: str = "") -> str:
        parts = " · ".join(f"{n} {s:.1f} s" for n, s in self.parts.items())
        return f"{self.what} timing: {parts or 'nothing'} (total {self.total():.1f} s{'; ' + extra if extra else ''})"
