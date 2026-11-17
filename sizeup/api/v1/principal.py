"""The principal-resolver seam of /api/v1 (docs/mobile-api.md sections 1.4 and 3.3, #619).

On /api/v1/ the only identity is a device's bearer token, resolved here. It never consults the console's Cloudflare
Access (a cookie or header there is ignored, so a signed-in browser is not a device), never yields who=None and
lets the request through (the trap in CadApi.permit, which checks nothing without a person), and never hands a handler a
CadApi. A resolver returns a Principal or None; None is a 401. Until the sign-in work (#558) lands there is no
device token to verify, so the resolver answers None for everything: every route that needs a device is a 401.
The same rule from the other side: the console refuses a bearer on /api/cad/ (web/server.py)."""
from __future__ import annotations

from typing import Callable, NamedTuple


class Principal(NamedTuple):
    """A signed-in person's device: `device` its id (the rate limit's and the stream cap's key), `email` the person.
    Both are required: a station, a feed or a service token is never a Principal."""
    device: str
    email: str


Resolver = Callable[[object], "Principal | None"]


def none_yet(headers) -> Principal | None:
    """No device tokens exist yet (#558): nobody resolves."""
    return None


def person(p: object) -> bool:
    """The fail-closed check on whatever a resolver returned: a Principal with a device and a person, nothing else."""
    return isinstance(p, Principal) and bool(p.device) and "@" in (p.email or "")
