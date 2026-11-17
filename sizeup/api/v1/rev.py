"""The content revision of an object, as the app and the machine feed see it (docs/mobile-api.md section 3.4, #561).

`rev` is a hash of the object's own content, so a change is detected by comparing revs, an app dedupes at-least-once
delivery by `(kind, id, rev)`, and `If-Match` can use it. The function is the same one everywhere an object gets a
rev: keep it exactly this."""
from __future__ import annotations

import hashlib
import json


def rev_of(obj: dict) -> str:
    """16 hex digits: sha256 of the object (without its own `rev`) as canonical JSON."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]
