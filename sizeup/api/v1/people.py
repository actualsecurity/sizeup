"""Who a signed-in person is NOW, for the app's per-request check (#558, docs/mobile-api.md section 3.3 "Per-request
check"): their role and whether they are active, from Settings > Users, cached by the CAD's settings rev so a role
change, a deactivation or an email edit takes effect on the very next request (the rev moves on every write).

Read only, on purpose. The console's `settings.standing()` ADDS an operator for an email it has not seen (a first Access
sign-in); a device token must never do that (an admin who edits a person's email would otherwise leave the old email's
devices minting a new dispatcher). So the lookup here never writes: an email that is not on the list is `None`, and the
resolver refuses it. There is no bootstrap rule either (no admin yet: everyone is an admin, on the console): a device
is the role its person has, a viewer when they are deactivated.

lookup(email) -> {name, role, active} | None reads the operators; rev() is the settings rev. Both are the app's
(sizeup/api/v1/wire.py): this package never imports the CAD."""
from __future__ import annotations

import threading
from typing import Callable, NamedTuple

ROLES = ("admin", "dispatcher", "viewer")
MAX_CACHED = 2048


class Person(NamedTuple):
    email: str
    name: str
    role: str            # admin | dispatcher | viewer (a deactivated person: viewer)
    active: bool


class People:
    def __init__(self, lookup: Callable[[str], dict | None], rev: Callable[[], object]):
        self._lookup, self._rev = lookup, rev
        self._seen: object = object()
        self._cache: dict[str, Person | None] = {}
        self._lock = threading.Lock()

    def get(self, email: str) -> Person | None:
        email = (email or "").strip().lower()
        rev = self._rev()
        with self._lock:
            if rev != self._seen or len(self._cache) >= MAX_CACHED:
                self._cache.clear()
                self._seen = rev
            if email in self._cache:
                return self._cache[email]
        row = self._lookup(email)
        person = None
        if row is not None:
            active = bool(row.get("active"))
            role = row.get("role") if active and row.get("role") in ROLES else "viewer"
            person = Person(email, str(row.get("name") or email), role, active)
        with self._lock:
            if rev == self._seen:
                self._cache[email] = person
        return person
