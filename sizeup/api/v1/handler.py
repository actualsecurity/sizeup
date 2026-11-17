"""Serves /api/v1 from the route table (routes.py). server.py calls respond() for every path under PREFIX, before
/api/cad/. Nothing here writes to a database.

Order, so that a v1 problem never reaches the console (docs/mobile-api.md "Sharing the production hub"):
 1. a path too long: 414;
 2. the master switch ([mobile] enabled) off: 404, as if the path did not exist. Meta is the one path served with
    mobile off;
 3. no such route 404, a wrong method 405;
 4. the matched route's feature flag off: the same 404. Nothing up to here runs a resolver, a handler or any
    database or key work;
 5. who: a route that needs a device gets the principal resolver's answer (principal.py). A request with no principal
    is rate limited per address, one with a principal per device (limits.py): 429 + Retry-After. Then no principal
    on a device route: 401;
 6. v1's share of concurrent requests: 503 + Retry-After when it is used up;
 7. the handler, inside one try/except (respond): whatever it raises is a 500 in v1's envelope, logged by class only."""
from __future__ import annotations

import json
import re
import sys
from typing import Callable
from urllib.parse import parse_qs

from sizeup.fence import FencedWrite

from .errors import JSON, error
from .limits import Limits
from .principal import Refusal, Resolver, may, none_yet, person
from .routes import ROUTES, Ctx, Raw, Route
from .switches import Switches

PREFIX = "/api/v1/"
# the sign-in's paths (#558), served by the same switch / flag / catch-all / limit code as /api/v1 but not under it:
# the whole /auth/v1/ namespace (nothing else is there) and exactly the two association files of /.well-known/
OTHER_PREFIXES = ("/auth/v1/",)
WELL_KNOWN = ("/.well-known/apple-app-site-association", "/.well-known/assetlinks.json")
MAX_PATH = 512


def _pattern(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(template)) + "$")


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class V1:
    def __init__(self, routes: list[Route] | None = None, switches: Switches | None = None,
                 resolver: Resolver | None = None, limits: Limits | None = None, log=None, auth=None):
        """auth: the sign-in service (sizeup/api/v1/auth.py), or None (not built: mobile off, no token key, no hub
        database): the /auth/v1 and /.well-known paths are then 404s, and the resolver is the one given."""
        self.auth = auth
        self.routes = [(r, _pattern(r.path)) for r in (ROUTES if routes is None else routes)]
        if auth is not None and routes is None:
            self.routes += [(r, _pattern(r.path)) for r in auth.routes()]
            resolver = resolver or auth.resolve
        self.switches = switches or Switches()
        self.resolver = resolver or none_yet
        self.limits = limits or Limits(self.switches.max_requests, self.switches.max_streams)
        self.log = log or _stderr

    def handles(self, path: str) -> bool:
        return path.startswith((PREFIX, *OTHER_PREFIXES)) or path in WELL_KNOWN

    def respond(self, method: str, path: str, query: str, headers, peer: str = "",
                read: Callable | None = None) -> tuple[int, bytes, str, dict]:
        """(status, body, content type, headers). HEAD is a GET without its body (the server drops it). Never
        raises (but FencedWrite, the console's handover signal, which the server answers as a 503)."""
        try:
            return self._respond(method, path, query, headers, peer, read)
        except FencedWrite:
            raise                             # the handover signal: the server answers it as a 503
        except Exception as e:                # a v1 failure is v1's: the class only, never a token, header or body
            try:
                self.log(f"api v1: {method} {self._where(path)} failed: {type(e).__name__}")
            except Exception:
                pass
            return error(500, "internal", "something went wrong: try again")

    def _respond(self, method, path, query, headers, peer, read=None) -> tuple[int, bytes, str, dict]:
        if len(path) > MAX_PATH:
            return error(414, "uri_too_long", "path too long")
        if not self.switches.enabled() and not (method in ("GET", "HEAD") and path == "/api/v1/meta"):
            return self._absent()
        allowed = []
        for route, pat in self.routes:
            m = pat.match(path)
            if m is None:
                continue
            if route.method != ("GET" if method == "HEAD" else method):
                allowed.append(route.method)
                continue
            if not self.switches.allows(route.flag):
                return self._absent()
            return self._serve(route, m.groupdict(), query, headers, peer, method, read)
        if allowed:
            return error(405, "method_not_allowed", f"{method} is not allowed here",
                         headers={"Allow": ", ".join(sorted(set(allowed)))})
        return self._absent()

    def _where(self, path: str) -> str:
        """The matched route's template for the log (the path itself could carry a token), else /api/v1/..."""
        return next((r.path for r, pat in self.routes if pat.match(path)), "/api/v1/...")

    @staticmethod
    def _absent() -> tuple[int, bytes, str, dict]:
        return error(404, "not_found", "no such endpoint")

    def _serve(self, route: Route, params: dict, query: str, headers, peer: str, method: str = "GET",
               read: Callable | None = None) -> tuple[int, bytes, str, dict]:
        principal, refusal = None, None
        if route.need == "device":
            principal = self.resolver(headers)
            if isinstance(principal, Refusal):     # a reasoned 401 (token_expired, device_revoked, user_inactive)
                principal, refusal = None, principal
            elif not person(principal):           # fail closed: not a device of a person, whatever the resolver said
                principal = None
        # a flood of bad tokens is limited per address too
        wait = self.limits.admit(principal.device if principal else None, write=route.method != "GET", peer=peer,
                                 public=route.need == "public")
        if wait:
            return error(429, "rate_limited", "too many requests: slow down", wait)
        if route.need == "device" and principal is None:
            return error(401, *(refusal or ("unauthorized", "sign in")))
        if principal is not None and not may(principal, route.role):      # the person's role now (the resolver's read)
            return error(403, "forbidden", "your role may not do this")
        if not self.limits.enter():
            return error(503, "busy", "the hub is busy: try again", 1)
        try:
            ctx = Ctx(parse_qs(query), headers, params, principal, self.switches.features(), method, peer, read)
            status, obj = route.handler(ctx)
            if isinstance(obj, Raw):
                return status, obj.body, obj.ctype, {"Cache-Control": "no-store", **obj.headers}
            return status, json.dumps(obj, separators=(",", ":")).encode(), JSON, {"Cache-Control": "no-store"}
        finally:
            self.limits.leave()
