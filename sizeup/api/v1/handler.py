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
import threading
from urllib.parse import parse_qs

from sizeup.fence import FencedWrite

from . import reads
from .errors import JSON, error
from .limits import STREAMS_PER_DEVICE, Limits
from .principal import Resolver, none_yet, person
from .routes import ROUTES, Ctx, Route
from .stream import Body, StreamPlan
from .switches import Switches

PREFIX = "/api/v1/"
MAX_PATH = 512


def _pattern(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(template)) + "$")


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class V1:
    def __init__(self, routes: list[Route] | None = None, switches: Switches | None = None,
                 resolver: Resolver | None = None, limits: Limits | None = None, log=None, cad=None,
                 changes=None):
        self.routes = [(r, _pattern(r.path)) for r in (ROUTES if routes is None else routes)]
        self.switches = switches or Switches()
        self.resolver = resolver or none_yet
        self.limits = limits or Limits(self.switches.max_requests, self.switches.max_streams)
        self.log = log or _stderr
        # the read resources' data (#560): a read-only facade over the hub's Cad; a handler gets this, never the Cad
        self.reads = reads.Reads(cad, log=self.log) if cad is not None else None
        self._streams: dict[str, list] = {}        # device -> its open streams, oldest first (newest wins, #561)
        self._streams_mu = threading.RLock()
        self.changes = changes            # the change log's Feed (#561): /changes and /stream; None: not on this host

    def handles(self, path: str) -> bool:
        return path.startswith(PREFIX)

    def respond(self, method: str, path: str, query: str, headers, peer: str = "") -> tuple[int, bytes, str, dict]:
        """(status, body, content type, headers). HEAD is a GET without its body (the server drops it). Never
        raises (but FencedWrite, the console's handover signal, which the server answers as a 503)."""
        try:
            return self._respond(method, path, query, headers, peer)
        except FencedWrite:
            raise                             # the handover signal: the server answers it as a 503
        except Exception as e:                # a v1 failure is v1's: the class only, never a token, header or body
            try:
                self.log(f"api v1: {method} {self._where(path)} failed: {type(e).__name__}")
            except Exception:
                pass
            return error(500, "internal", "something went wrong: try again")

    def _respond(self, method, path, query, headers, peer) -> tuple[int, bytes, str, dict]:
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
            return self._serve(route, m.groupdict(), query, headers, peer)
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

    def _serve(self, route: Route, params: dict, query: str, headers, peer: str) -> tuple[int, bytes, str, dict]:
        principal = None
        if route.need == "device":
            principal = self.resolver(headers)
            if not person(principal):             # fail closed: not a device of a person, whatever the resolver said
                principal = None
        # a flood of bad tokens is limited per address too
        wait = self.limits.admit(principal.device if principal else None, write=route.method != "GET", peer=peer,
                                 public=route.need == "public")
        if wait:
            return error(429, "rate_limited", "too many requests: slow down", wait)
        if route.need == "device" and principal is None:
            return error(401, "unauthorized", "sign in")
        if not self.limits.enter():
            return error(503, "busy", "the hub is busy: try again", 1)
        try:
            ctx = Ctx(parse_qs(query), headers, params, principal, self.switches.features(), reads=self.reads,
                      changes=self.changes)
            # a handler answers (status, object), (status, object, headers) (object None: no body, a 304, #560) or a
            # finished (status, body, content type, headers), which is what errors.error() makes
            res = route.handler(ctx)
            if len(res) == 2 and isinstance(res[1], StreamPlan):   # SSE (#561): its own slot; checked before len(res)
                return self._stream(res[1], principal)
            if len(res) == 4:
                return res
            status, obj, extra = (*res, {})[:3]
            body = b"" if obj is None else json.dumps(obj, separators=(",", ":")).encode()
            out = {"Cache-Control": "no-store", **extra}
            wait = obj.get("error", {}).get("retry_after_s") if isinstance(obj, dict) and status >= 400 else None
            if wait is not None:                       # a handler's error: the header says what the body says
                out["Retry-After"] = str(wait)
            return status, body, JSON, out
        finally:
            self.limits.leave()

    def _stream(self, plan: StreamPlan, principal) -> tuple[int, Body, str, dict]:
        """2 streams per device and the global cap (limits.open_stream). A device over its two gets the NEWEST: its
        oldest stream is closed (reason `superseded`) and the new one accepted (a phone that changed networks leaves a
        stale stream open until the hub notices it, and would otherwise be refused). Only the global cap is a 429. The
        body ends itself when mobile or the `stream` flag goes off (read as it runs), and gives its slot back when it
        ends."""
        device = principal.device
        with self._streams_mu:
            if not self.limits.open_stream(device):
                mine = self._streams.get(device, [])
                if len(mine) < STREAMS_PER_DEVICE:                       # not the per-device cap: the global one
                    return error(429, "too_many_streams", "too many open streams: try again", 10)
                mine.pop(0).evict()                                      # (gives its slot back, and drops itself)
                if not self.limits.open_stream(device):
                    return error(429, "too_many_streams", "too many open streams: try again", 10)

            def done() -> None:
                with self._streams_mu:
                    self.limits.close_stream(device)
                    if body in self._streams.get(device, []):
                        self._streams[device].remove(body)
            body = Body(plan, done, lambda: not (self.switches.enabled() and self.switches.allows("stream")))
            self._streams.setdefault(device, []).append(body)
        return 200, body, "text/event-stream", {"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"}
