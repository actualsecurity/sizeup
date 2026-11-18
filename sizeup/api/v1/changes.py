"""GET /api/v1/changes?after=<seq>&epoch=<e>&limit=<n> (#561, docs/mobile-api.md section 3.4): what changed since a
cursor, as references `(seq, epoch, kind, id, rev, at)`; the app fetches an object when it sees a rev it does not have.

- no `after`: the log's head and epoch, no entries (where a live app starts from, next to its snapshot);
- `after` without `epoch`: 400 `bad_cursor` (a bare number could be from any run of the hub);
- `after` older than what is kept: 410 `cursor_too_old` with `oldest` (the smallest cursor still served);
- `epoch` that is not the log's, or an `after` beyond its head (a log restored from something older): 409
  `epoch_changed` with the current `epoch`. Either way the app resnapshots;
- `more`: true when the page was full: ask again with `cursor`;
- 503 `unavailable` (Retry-After) while the log has no baseline yet, or where there is no log, or while no visibility
  predicate is configured (changelog.Feed: fail closed).

Which references a person gets is the feed's kinds and its per-person predicate (changelog.Feed.allowed); the payload
is only ever changelog.public(): kind, id, rev, seq, epoch, at."""
from __future__ import annotations

import re
import time

from sizeup.fence import FencedWrite

from .changelog import Feed, public
from .errors import envelope

DEFAULT_LIMIT, MAX_LIMIT = 100, 500
# a cursor or a limit: digits only ("1_0", "+5" and " 5" are not numbers here)
DIGITS = re.compile(r"[0-9]{1,18}")
EPOCH_CHANGED = "the change log started over: load a snapshot again"


def problem(status: int, code: str, message: str, retry_after_s: int | None = None, **extra) -> tuple[int, dict]:
    """An error in v1's envelope with the fields a resync needs beside code and message."""
    return status, envelope(code, message, retry_after_s, extra=extra)


def one(q: dict, name: str) -> str | None:
    v = q.get(name)
    return v[0] if v else None


def unavailable(feed: Feed | None) -> tuple[int, dict] | None:
    """Why the log cannot be read now (503 + Retry-After), or None. A fenced task raises the handover signal."""
    if feed is None or not feed.serving():
        return problem(503, "unavailable", "the change log is not available", retry_after_s=30)
    if not feed.writable():
        raise FencedWrite("not the leader")
    if not feed.ready():
        return problem(503, "unavailable", "the change log is starting: try again", retry_after_s=1)
    return None


def parse_cursor(feed: Feed, after: str | None, epoch: str | None) -> tuple[int | None, tuple[int, dict] | None]:
    """(the cursor as an int, None) or (None, the error to answer). `after` None: no cursor (a live start)."""
    log = feed.log
    if epoch is not None and epoch != log.epoch:
        return None, problem(409, "epoch_changed", EPOCH_CHANGED, epoch=log.epoch)
    if after is None:
        return None, None
    if not DIGITS.fullmatch(after):
        return None, problem(400, "bad_cursor", "after is a whole number")
    if epoch is None:                          # a bare number could be from any run: the epoch says which
        return None, problem(400, "bad_cursor", "after needs the epoch it came from")
    n = int(after)
    if n > log.head():
        return None, problem(409, "epoch_changed", "that cursor is ahead of the log: load a snapshot again",
                             epoch=log.epoch)
    if n < log.floor():
        return None, problem(410, "cursor_too_old", "that cursor is older than the log: load a snapshot again",
                             oldest=log.floor())
    return n, None


def handle(ctx) -> tuple[int, dict]:
    feed: Feed | None = ctx.changes
    if (refused := unavailable(feed)) is not None:
        return refused
    limit_s = one(ctx.query, "limit")
    if limit_s is not None and not (DIGITS.fullmatch(limit_s) and 1 <= int(limit_s) <= MAX_LIMIT):
        return problem(400, "bad_limit", f"limit is a whole number, 1 to {MAX_LIMIT}")
    limit = DEFAULT_LIMIT if limit_s is None else int(limit_s)
    after, bad = parse_cursor(feed, one(ctx.query, "after"), one(ctx.query, "epoch"))
    if bad:
        return bad
    epoch, head = feed.log.epoch, feed.log.head()
    if after is None:
        return 200, {"epoch": epoch, "cursor": head, "head": head, "more": False, "changes": [],
                     "server_time": time.time()}
    rows = feed.log.since(after, limit, feed.kinds)
    full = len(rows) >= limit
    cursor = rows[-1].seq if full else max(head, rows[-1].seq if rows else 0)       # (a short page: all of it)
    out = [public(c, epoch) for c in rows if feed.allowed(ctx.principal, c)]
    return 200, {"epoch": epoch, "cursor": cursor, "head": head, "more": full and cursor < head,
                 "changes": out, "server_time": time.time()}
