"""GET /api/v1/stream (#561, docs/mobile-api.md section 3.4): the change log as Server-Sent Events, for an app in the
foreground. References only (kind, id, rev), as /api/v1/changes returns them.

    retry: 3000
    event: hello           data {"epoch","head","server_time"}                     (no id)
    event: change          id: <epoch>-<seq>   data {"seq","epoch","kind","id","rev","at","server_time"}
    event: heartbeat       data {"server_time"}              every 15 s idle, no id (it moves no cursor)
    event: close           data {"reason","server_time"}     then the connection ends: reconnect

Resume: `Last-Event-ID: <epoch>-<seq>` (what EventSource sends by itself), or `?after=<seq>&epoch=<e>`; with neither,
from the head. A cursor of another epoch is a 409 and one older than the log a 410, before the stream opens
(changes.py), so the app resnapshots. The cursor is the log's seq in hub.sqlite: a reconnect after a task handoff
resumes.

Caps (safeguard 4): two streams per device and the global cap (limits.open_stream), taken by the handler's caller
(V1). A stream's queue is bounded (changelog.Subscription): a client that cannot keep up is closed, and resumes from
the database. A stream ends cleanly (event `close`, reason) when the mobile switch or the `stream` flag goes off or
the hub drains (stopping), when it loses its lease (handover), when the log's epoch changes (epoch_changed), when its
device is no longer valid (revoked) and when the client closes. It holds its thread only while it is open: a thread
waiting on a Condition, woken by a flush, a heartbeat or the tick (feed.tick_s, which only checks the stop
conditions); an idle stream reads nothing from the database. A stream is a request in the server's Gate (`inflight`)
until it ends: the drain closes the feed first, so the streams end within a tick and `wait_idle` does not run to its
timeout."""
from __future__ import annotations

import json
import time
from typing import Callable

from .changelog import BACKLOG_ROWS, RETRY_MS, VALID_EVERY_S, Feed, public
from .changes import DIGITS, one, parse_cursor, problem, unavailable

MIN_WAIT_S = 0.01              # never a zero wait: a loop with nothing to wait for would spin
PAD = b":" + b" " * 1024 + b"\n\n"   # a proxy that holds a response until it has enough passes the first event on (the
#                                      console pads 2 KB; a phone's first event does not need more than this)


class StreamPlan:
    """What the handler answers with: V1 sees it, takes the stream slot and hands the server a Body to run."""

    def __init__(self, feed: Feed, principal, after: int | None):
        self.feed, self.principal, self.after = feed, principal, after


def event(name: str, data: dict, id_: str | None = None) -> bytes:
    head = f"id: {id_}\n" if id_ else ""
    return f"{head}event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def last_event_id(raw: str | None) -> tuple[str | None, str | None]:
    """The `Last-Event-ID` header as (epoch, seq): "<epoch>-<seq>" splits at its last dash. Anything without a dash is
    (None, the whole value), which parse_cursor then refuses as a bad cursor. No header: (None, None)."""
    if not raw:
        return None, None
    epoch, sep, seq = raw.rpartition("-")
    return (epoch, seq) if sep else (None, raw)


def handle(ctx) -> tuple[int, object]:
    feed: Feed | None = ctx.changes
    if (refused := unavailable(feed)) is not None:
        return refused
    epoch, seq = last_event_id((ctx.headers.get("Last-Event-ID") or "").strip() or None)
    if seq is None:
        seq, epoch = one(ctx.query, "after"), one(ctx.query, "epoch")
    after, bad = parse_cursor(feed, seq, epoch)
    if bad:
        return bad
    return 200, StreamPlan(feed, ctx.principal, after)


class Body:
    """An open stream. run(write, stop) until it ends; close() gives the slot back, once, whatever happened."""

    sse = True

    def __init__(self, plan: StreamPlan, on_close: Callable[[], None], stop: Callable[[], bool]):
        self.plan, self._on_close, self._stop = plan, on_close, stop
        self._closed = False
        self._valid_at = time.monotonic()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._on_close()

    def run(self, write: Callable[[bytes], None], stop: Callable[[], bool] = lambda: False) -> str:
        """Write until the client goes (write raises: left to the caller), `stop()` or ours says so. Returns why it
        ended (for the log and the close event)."""
        feed, log = self.plan.feed, self.plan.feed.log
        sub = feed.subscribe()
        try:
            epoch = log.epoch
            last = self.plan.after if self.plan.after is not None else log.head()
            write(b"retry: %d\n" % RETRY_MS + PAD + event("hello", {"epoch": epoch, "head": log.head(),
                                                                   "server_time": time.time()}))
            beat = time.monotonic()
            last, why = self._backlog(write, last, epoch, stop, sub)         # what the cursor missed: from the database
            while not why:                                                    # then only what the recorder announces
                for c in sub.get(self._next_wait(beat)):
                    # (the backlog read may have had it already)
                    if c.seq > last and c.kind in feed.kinds:
                        last = c.seq
                        if self._tell(write, c, epoch):
                            beat = time.monotonic()
                if time.monotonic() - beat >= feed.heartbeat_s:
                    write(event("heartbeat", {"server_time": time.time()}))
                    beat = time.monotonic()
                why = self._why(stop, sub, epoch)
            try:
                write(event("close", {"reason": why, "server_time": time.time()}))
            except Exception:  # noqa: BLE001 - the client may be gone: that is the end anyway
                pass
            return why
        finally:
            feed.unsubscribe(sub)

    def _tell(self, write, c, epoch: str) -> bool:
        feed = self.plan.feed
        if not feed.allowed(self.plan.principal, c):
            return False
        write(event("change", {**public(c, epoch), "server_time": time.time()}, f"{epoch}-{c.seq}"))
        return True

    def _backlog(self, write, last: int, epoch: str, stop, sub) -> tuple[int, str]:
        """The entries after `last`, a page at a time (a stop condition is looked at between pages: a replay of a whole
        log never outlives a revoked device or a drain). Returns (the last seq looked at, why it stopped or "")."""
        feed = self.plan.feed
        while True:
            rows = feed.log.since(last, BACKLOG_ROWS, feed.kinds)
            for c in rows:
                self._tell(write, c, epoch)
            if rows:
                last = rows[-1].seq
            why = self._why(stop, sub, epoch)
            if why or len(rows) < BACKLOG_ROWS:
                return last, why

    def _next_wait(self, beat: float) -> float:
        feed = self.plan.feed
        return min(feed.tick_s, max(MIN_WAIT_S, feed.heartbeat_s - (time.monotonic() - beat)))

    def _why(self, stop, sub, epoch) -> str:
        feed = self.plan.feed
        if self._stop() or stop() or sub.closed:
            return "stopping"
        if not feed.writable():
            return "handover"
        if sub.dropped:
            return "slow"
        if feed.log.epoch != epoch:
            return "epoch_changed"
        if time.monotonic() - self._valid_at >= VALID_EVERY_S:
            self._valid_at = time.monotonic()
            if not feed.valid(self.plan.principal):
                return "revoked"
        return ""
