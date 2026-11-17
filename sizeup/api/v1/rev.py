"""The rev of a v1 object: its content hash (docs/mobile-api.md section 3.2: every mutable resource has a `rev`, an ETag
on GET and `If-Match` on edits). Per object, never the CAD's global store.rev, so one call's edit never makes another's
rev stale. The same function is defined by every part of v1 that needs it, byte for byte, so the rev a read hands the
app is the one a write checks."""
from __future__ import annotations

import hashlib
import json


def rev_of(obj) -> str:
    """16 hex digits of sha256 over the object (without its own `rev`) as sorted, compact JSON."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode()).hexdigest()[:16]
