"""Serves /api/v1 from the route table (routes.py). server.py calls respond() for every path under PREFIX, before
/api/cad/. Nothing here writes to a database."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from .errors import JSON, error
from .routes import ROUTES, Ctx, Route

PREFIX = "/api/v1/"
MAX_PATH = 512


def _pattern(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(template)) + "$")


class V1:
    def __init__(self, routes: list[Route] | None = None):
        self.routes = [(r, _pattern(r.path)) for r in (ROUTES if routes is None else routes)]

    def handles(self, path: str) -> bool:
        return path.startswith(PREFIX)

    def respond(self, method: str, path: str, query: str, headers) -> tuple[int, bytes, str, dict]:
        """(status, body, content type, headers). HEAD is a GET without its body (the server drops it)."""
        if len(path) > MAX_PATH:
            return error(414, "uri_too_long", "path too long")
        allowed = []
        for route, pat in self.routes:
            m = pat.match(path)
            if m is None:
                continue
            if route.method == ("GET" if method == "HEAD" else method):
                status, obj = route.handler(Ctx(parse_qs(query), headers, m.groupdict()))
                return status, json.dumps(obj, separators=(",", ":")).encode(), JSON, {"Cache-Control": "no-store"}
            allowed.append(route.method)
        if allowed:
            return error(405, "method_not_allowed", f"{method} is not allowed here",
                         headers={"Allow": ", ".join(sorted(set(allowed)))})
        return error(404, "not_found", "no such endpoint")
