"""What every v1 list read shares (#560, docs/mobile-api.md sections 3.2 and 8): the opaque cursor (`after`, `limit`,
`next`), the ETag and If-None-Match (304), and the bounds. Nothing here reads a database."""
from __future__ import annotations

import base64
import binascii
import json
import math
import re

from .rev import rev_of

DEFAULT_LIMIT, MAX_LIMIT = 50, 200            # a page of incidents; documented in the spec (the cap is silent)
CURSOR_MAX = 200                              # characters: a longer one is not ours


class BadRequest(ValueError):
    """The query is wrong: the handler answers 400 with `code`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def one(query: dict, name: str) -> str:
    v = query.get(name) or [""]
    return (v[0] or "").strip()


def limit_of(query: dict, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> int:
    """?limit=: 1 or more, clamped to `cap`; anything else is a 400."""
    raw = one(query, "limit")
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) < 1:
        raise BadRequest("bad_request", f"limit: a number from 1 to {cap}")
    return min(int(raw), cap)


def encode(kind: str, key: list) -> str:
    """An opaque cursor: which list it is for and the sort key of the last row sent (keyset, never an offset: rows
    added or changed between pages move nothing already sent)."""
    raw = json.dumps({"v": 1, "t": kind, "k": key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(token: str, kind: str, shape: tuple[type, ...]) -> tuple:
    """The sort key of a cursor made by encode() for this list, else BadRequest("bad_cursor")."""
    bad = BadRequest("bad_cursor", "after: not a cursor from this list")
    if not token or len(token) > CURSOR_MAX or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise bad
    try:
        obj = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise bad from None
    k = obj.get("k") if isinstance(obj, dict) else None
    if (obj.get("v") != 1 or obj.get("t") != kind or not isinstance(k, list) or len(k) != len(shape)
            or set(obj) != {"v", "t", "k"}):
        raise bad
    for x, t in zip(k, shape):
        if isinstance(x, bool) or not isinstance(x, t) or (isinstance(x, float) and not math.isfinite(x)):
            raise bad
    return tuple(k)


def etag(rev: str) -> str:
    return f'"{rev}"'


def list_rev(revs: list[str], nxt: str | None) -> str:
    """A list's rev: the item revs in order and where the next page starts."""
    return rev_of({"items": revs, "next": nxt})


def matches(headers, rev: str) -> bool:
    """Does If-None-Match name this rev (a list of ETags, weak or not, quoted or not, or *)?"""
    try:
        want = headers.get("If-None-Match")
    except Exception:  # noqa: BLE001 - not a mapping: no conditional
        return False
    if not want:
        return False
    for tag in str(want).split(","):
        tag = tag.strip()
        if tag == "*" or tag.removeprefix("W/").strip('"') == rev:
            return True
    return False


def answer(headers, obj: dict, rev: str) -> tuple:
    """(200, obj, {ETag}) or (304, None, {ETag}) when the client already has this rev. `obj` carries its rev."""
    h = {"ETag": etag(rev)}
    return (304, None, h) if matches(headers, rev) else (200, obj, h)
