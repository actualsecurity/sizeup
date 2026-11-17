"""GET /api/v1/changes?after=<seq>&epoch=<e>&limit=<n> (#561, docs/mobile-api.md section 3.4): what changed since a
cursor, as references `(seq, epoch, kind, id, rev, at)`; the app fetches an object when it sees a rev it does not have.

- no `after`: the log's head and epoch, no entries (where a live app starts from, next to its snapshot);
- `after` older than what is kept: 410 `cursor_too_old` with `oldest` (the smallest cursor still served);
- `epoch` that is not the log's, or an `after` beyond its head (a log restored from something older): 409
  `epoch_changed` with the current `epoch`. Either way the app resnapshots;
- `more`: true when the page was full: ask again with `cursor`.

Which references a person gets is the feed's scope and its per-person predicate (changelog.Feed.allowed); the payload
is only ever changelog.public(): kind, id, rev, seq, epoch, at."""
from __future__ import annotations

import time

from sizeup.fence import FencedWrite

from .changelog import Feed, public

DEFAULT_LIMIT, MAX_LIMIT = 100, 500


def problem(status: int, code: str, message: str, **extra) -> tuple[int, dict]:
    """An error in v1's envelope with the fields a resync needs beside code and message."""
    return status, {"error": {"code": code, "message": message, **extra}}


def parse_cursor(feed: Feed, after, epoch: str | None) -> tuple[int | None, tuple[int, dict] | None]:
    """(the cursor as an int, None) or (None, the error to answer). `after` None: no cursor (a live start)."""
    if after is None:
        if epoch is not None and epoch != feed.log.epoch:
            return None, problem(409, "epoch_changed", "the change log started over: load a snapshot again",
                                 epoch=feed.log.epoch)
        return None, None
    try:
        n = int(after)
        if n < 0 or len(str(after)) > 18:
            raise ValueError
    except (TypeError, ValueError):
        return None, problem(400, "bad_cursor", "after is a whole number")
    log = feed.log
    if epoch is not None and epoch != log.epoch:
        return None, problem(409, "epoch_changed", "the change log started over: load a snapshot again", epoch=log.epoch)
    if n > log.head():
        return None, problem(409, "epoch_changed", "that cursor is ahead of the log: load a snapshot again",
                             epoch=log.epoch)
    floor = log.floor()
    if n < floor:
        return None, problem(410, "cursor_too_old", "that cursor is older than the log: load a snapshot again",
                             oldest=floor)
    return n, None


def one(q: dict, name: str) -> str | None:
    v = q.get(name)
    return v[0] if v else None


def handle(ctx) -> tuple[int, dict]:
    feed: Feed | None = ctx.changes
    if feed is None:
        return problem(503, "unavailable", "the change log is not available on this host", retry_after_s=30)
    if not feed.writable():
        raise FencedWrite("not the leader")           # a standby / fenced task: the server's 503
    feed.ready()
    limit_s = one(ctx.query, "limit")
    try:
        limit = DEFAULT_LIMIT if limit_s is None else int(limit_s)
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError
    except ValueError:
        return problem(400, "bad_limit", f"limit is a whole number, 1 to {MAX_LIMIT}")
    after, bad = parse_cursor(feed, one(ctx.query, "after"), one(ctx.query, "epoch"))
    if bad:
        return bad
    epoch, head = feed.log.epoch, feed.log.head()
    if after is None:
        return 200, {"epoch": epoch, "cursor": head, "head": head, "more": False, "changes": [],
                     "server_time": time.time()}
    rows = feed.log.since(after, limit, feed.scope.kinds)
    cursor = rows[-1].seq if len(rows) >= limit else max(head, rows[-1].seq if rows else 0)   # (a short page: all of it)
    out = [public(c, epoch) for c in rows if feed.allowed(ctx.principal, c)]
    return 200, {"epoch": epoch, "cursor": cursor, "head": head, "more": len(rows) >= limit and cursor < head,
                 "changes": out, "server_time": time.time()}
