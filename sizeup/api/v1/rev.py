"""A per-object revision without a schema change (#560, docs/mobile-api.md section 3.2): a stable content hash of what
the API says about the object, so the same object always has the same rev and a changed one never does. The ETag of a
read is that rev. (Today the CAD only has a global store.rev; a per-row column would need a migration.)"""
from __future__ import annotations

import hashlib
import json


def rev_of(obj) -> str:
    """The 16-hex rev of `obj` (a JSON-able object that does NOT yet carry its own `rev`)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()[:16]
