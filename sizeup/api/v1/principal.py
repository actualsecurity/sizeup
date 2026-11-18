"""The principal-resolver seam of /api/v1 (docs/mobile-api.md sections 1.4 and 3.3, #619).

On /api/v1/ the only identity is a device's bearer token, resolved here. It never consults the console's Cloudflare
Access (a cookie or header there is ignored, so a signed-in browser is not a device), never yields who=None and
lets the request through (the trap in CadApi.permit, which checks nothing without a person), and never hands a handler a
CadApi. A resolver returns a Principal, a Refusal (a reasoned 401) or None (a plain 401). none_yet, which answers
None for everything, is the fallback when no sign-in is built (mobile off, no token key): every route that needs a
device is then a 401. The real resolver is the sign-in's (#558, sizeup/api/v1/auth.py AuthService.resolve).
The same rule from the other side: the console refuses a bearer on /api/cad/ (web/server.py)."""
from __future__ import annotations

from typing import Callable, NamedTuple


# What each console role may do (the same sets as sizeup/cad/api.py MAY, kept here: this package never imports the CAD).
# A route's `role` is the least level it needs; a Principal with no role is a viewer (fail closed).
MAY = {"admin": ("read", "dispatch", "admin"), "dispatcher": ("read", "dispatch"), "viewer": ("read",)}


class Principal(NamedTuple):
    """A signed-in person's device: `device` its id (the rate limit's and the stream cap's key), `email` the person.
    Both are required: a station, a feed or a service token is never a Principal. `role` is the person's console role
    (admin, dispatcher, viewer) as Settings > Users has it NOW (the resolver reads it on every request); `name` their
    name there. A resolver that sets neither gives a viewer with no name."""
    device: str
    email: str
    role: str = "viewer"
    name: str = ""


class Refusal(NamedTuple):
    """A resolver's reasoned no: a 401 with this v1 error code (token_expired, device_revoked, user_inactive) instead of
    the plain `unauthorized`, so the app knows whether to refresh, wipe, or sign in again."""
    code: str
    message: str


Resolver = Callable[[object], "Principal | Refusal | None"]


def may(p: "Principal", level: str) -> bool:
    """Does this principal's role reach `level` ("" = nothing is asked)?"""
    return not level or level in MAY.get(p.role, MAY["viewer"])


def none_yet(headers) -> Principal | None:
    """The fallback when no sign-in is built (mobile off, no token key): nobody resolves."""
    return None


def person(p: object) -> bool:
    """The fail-closed check on whatever a resolver returned: a Principal with a device and a person, nothing else."""
    return isinstance(p, Principal) and bool(p.device) and "@" in (p.email or "")
