"""FakeFeed: a scripted external CAD feed adapter (#297) for tests. Synthetic only: made-up events, places and times.

    feed = FakeFeed("county", "County feed", clock=clock)
    feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": t})
    feed.change("A1", location="14 Example Ave")       # a field change
    feed.comment("A1", t + 30, "caller reports smoke")  # a new comment
    feed.outage("failing", "HTTP 503")                  # health says so; raise_=True makes poll() raise too
    feed.recover()
    feed.script([(t0, lambda f: f.put(...)), (t1, lambda f: f.outage(...))]); feed.run_until(t)   # by the clock
"""

from __future__ import annotations

from dataclasses import replace

from sizeup.sources import FeedEvent, Health


class FakeFeed:
    def __init__(self, source: str = "county", label: str = "County feed", rank: int = 1, poll_s: float = 10.0,
                 clock=None):
        self.source, self.label, self.rank, self.poll_s = source, label, rank, poll_s
        self.clock = clock
        self.events: dict[str, FeedEvent] = {}
        self._health = Health("ok")
        self.raise_ = False
        self.polls = 0
        self.closed = False
        self._script: list[tuple[float, object]] = []
        self.pokes: list[str] = []

    # -- the script -----------------------------------------------------------------------------------------------

    def put(self, event_id: str, **kw) -> FeedEvent:
        kw.setdefault("units", ())
        kw["units"] = tuple(kw["units"])
        kw["comments"] = tuple(tuple(c) for c in kw.get("comments", ()))
        kw["aliases"] = tuple(kw.get("aliases", ()))
        ev = FeedEvent(source=self.source, event_id=event_id, **kw)
        self.events[event_id] = ev
        return ev

    def change(self, event_id: str, **kw) -> FeedEvent:
        for k in ("units", "aliases"):
            if k in kw:
                kw[k] = tuple(kw[k])
        if "times" in kw:
            kw["times"] = {**self.events[event_id].times, **kw["times"]}
        self.events[event_id] = ev = replace(self.events[event_id], **kw)
        return ev

    def comment(self, event_id: str, at: float, text: str) -> FeedEvent:
        ev = self.events[event_id]
        return self.change(event_id, comments=(*ev.comments, (at, text)))

    def outage(self, state: str = "failing", reason: str = "HTTP 503", raise_: bool = False) -> None:
        self._health, self.raise_ = Health(state, reason), raise_

    def recover(self) -> None:
        self._health, self.raise_ = Health("ok"), False

    def script(self, steps) -> None:
        """(at, fn(feed)) steps, run by run_until() as the clock passes them."""
        self._script = sorted(steps, key=lambda s: s[0])

    def run_until(self, t: float) -> None:
        while self._script and self._script[0][0] <= t:
            _, fn = self._script.pop(0)
            fn(self)

    # -- the adapter interface ------------------------------------------------------------------------------------------

    def poll(self):
        self.polls += 1
        if self.clock is not None:
            self.run_until(self.clock())
        if self.raise_:
            raise ConnectionError(self._health.reason or "down")
        return list(self.events.values())

    def health(self) -> Health:
        return self._health

    def poll_now(self, reason: str) -> None:
        self.pokes.append(reason)

    def close(self) -> None:
        self.closed = True


def make(ctx):
    """A factory as an adapter package has one (the tests load it by module path and by entry point)."""
    return FakeFeed("fake", "Fake feed", clock=ctx.clock)
