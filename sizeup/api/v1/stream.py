"""GET /api/v1/stream (#561, docs/mobile-api.md section 3.4): the change log as Server-Sent Events, for an app in the
foreground. References only (kind, id, rev), as /api/v1/changes returns them.

    retry: 3000
    event: hello           data {"epoch","head","server_time"}                     (no id)
    event: change          id: <epoch>-<seq>   data {"seq","epoch","kind","id","rev","at","server_time"}
    event: heartbeat       data {"server_time"}              every 15 s idle, no id (it moves no cursor)
    event: close           data {"reason","server_time"}     then the connection ends: reconnect

Resume: `Last-Event-ID: <epoch>-<seq>` (what EventSource sends by itself), or `?after=<seq>&epoch=<e>`; with neither, from
the head. A cursor of another epoch is a 409 and one older than the log a 410, before the stream opens (changes.py), so
the app resnapshots. The cursor is the log's seq in hub.sqlite: a reconnect after a task handoff resumes.

Caps (safeguard 4): two streams per device and the global cap (limits.open_stream), taken by the handler's caller (V1).
A stream's queue is bounded (changelog.Subscription): a client that cannot keep up is closed, and resumes from the
database. A stream ends cleanly when the mobile switch or the `stream` flag goes off, when the hub drains or loses its
lease, and when the log's epoch changes. It holds its thread only while it is open: a thread waiting on a Condition, woken
by a flush, a heartbeat or the tick (feed.tick_s, which only checks the stop conditions); an idle stream reads
nothing from the database."""
from __future__ import annotations

import json
import time
from typing import Callable

from .changelog import Feed, public
from .changes import one, parse_cursor, problem

BACKLOG = 500
PAD = b":" + b" " * 1024 + b"\n\n"      # a proxy that holds a response until it has enough passes the first event on


class StreamPlan:
    """What the handler answers with: V1 sees it, takes the stream slot and hands the server a Body to run."""

    def __init__(self, feed: Feed, principal, after: int | None):
        self.feed, self.principal, self.after = feed, principal, after


def event(name: str, data: dict, id_: str | None = None) -> bytes:
    head = f"id: {id_}\n" if id_ else ""
    return f"{head}event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def last_event_id(raw: str | None) -> tuple[str | None, str | None]:
    """'<epoch>-<seq>' -> (seq, epoch); anything else -> (the whole thing as a seq to be refused, None)."""
    if not raw:
        return None, None
    epoch, sep, seq = raw.rpartition("-")
    return (seq, epoch) if sep else (raw, None)


def handle(ctx) -> tuple[int, object]:
    feed: Feed | None = ctx.changes
    if feed is None:
        return problem(503, "unavailable", "the change log is not available on this host", retry_after_s=30)
    if not feed.writable():
        from sizeup.fence import FencedWrite
        raise FencedWrite("not the leader")
    feed.ready()
    seq, epoch = last_event_id((ctx.headers.get("Last-Event-ID") or "").strip() or None)
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

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._on_close()

    def run(self, write: Callable[[bytes], None], stop: Callable[[], bool] = lambda: False) -> str:
        """Write until the client goes (write raises: left to the caller), `stop()` or ours says so. Returns why it
        ended (for the log and the close event)."""
        feed, principal, log = self.plan.feed, self.plan.principal, self.plan.feed.log
        sub = feed.subscribe()
        try:
            epoch = log.epoch
            last = self.plan.after if self.plan.after is not None else log.head()
            write(b"retry: 3000\n" + PAD + event("hello", {"epoch": epoch, "head": log.head(),
                                                           "server_time": time.time()}))
            beat = time.monotonic()

            def tell(c) -> bool:
                if not feed.allowed(principal, c):
                    return False
                d = public(c, epoch)
                d["server_time"] = time.time()
                write(event("change", d, f"{epoch}-{c.seq}"))
                return True
            rows = log.since(last, BACKLOG, feed.scope.kinds)           # what the cursor missed: once, from the database
            while rows:
                for c in rows:
                    tell(c)
                last = rows[-1].seq
                beat = time.monotonic()
                rows = log.since(last, BACKLOG, feed.scope.kinds) if len(rows) >= BACKLOG else []
            while True:                                                  # then only what the recorder announces
                why = self._why(stop, sub, epoch)
                if why:
                    break
                for c in sub.get(min(feed.tick_s, max(0.01, feed.heartbeat_s - (time.monotonic() - beat)))):
                    if c.seq > last and c.kind in feed.scope.kinds:      # (the backlog read may have had it already)
                        last = c.seq
                        if tell(c):
                            beat = time.monotonic()
                if time.monotonic() - beat >= feed.heartbeat_s:
                    write(event("heartbeat", {"server_time": time.time()}))
                    beat = time.monotonic()
            try:
                write(event("close", {"reason": why, "server_time": time.time()}))
            except Exception:  # noqa: BLE001 - the client may be gone: that is the end anyway
                pass
            return why
        finally:
            feed.unsubscribe(sub)

    def _why(self, stop, sub, epoch) -> str:
        feed = self.plan.feed
        if self._stop() or stop():
            return "stopping"
        if not feed.writable():
            return "handover"
        if sub.dropped:
            return "slow"
        if sub.closed:
            return "stopping"
        if feed.log.epoch != epoch:
            return "epoch_changed"
        return ""
