"""The route table of /api/v1: (method, path template, handler, need). tests/test_api_v1.py asserts that the set of
(method, path) here equals the spec's `paths` (api/openapi/v1/), both ways, so the served API and the promised one
cannot drift.

need: who may call it. "device" is a signed-in device's token (#558, not built yet). "public" answers without a token,
and is kept to the design's short list (docs/mobile-api.md section 3.3, PUBLIC below): it reveals nothing beyond
versions. Until #558 the hub's Cloudflare Access in front of every path is the only way in, so on the hub a request that
reaches a handler, public or not, is already a signed-in person."""
from __future__ import annotations

import time
from typing import Callable, NamedTuple

from . import API_VERSION

# The oldest app each platform may run (forced upgrades, #566 enforces it). 0.0.0: nothing is refused yet.
MIN_APP = {"ios": "0.0.0", "android": "0.0.0"}

NEEDS = {"public", "device"}
# The only /api/v1 paths that may answer without a token (docs/mobile-api.md section 3.3: with /auth/v1/token,
# /auth/v1/revoke and /.well-known/, which are not under /api/v1).
PUBLIC = {("GET", "/api/v1/meta")}


class Route(NamedTuple):
    method: str
    path: str          # "/api/v1/incidents/{id}": {name} is one path segment
    handler: Callable
    need: str          # one of NEEDS (see above)


class Ctx(NamedTuple):
    """What a handler gets: the request's query (parsed) and headers, and the path's {names}."""
    query: dict
    headers: object
    params: dict


def meta(ctx: Ctx) -> tuple[int, dict]:
    """GET /api/v1/meta: what the app checks at launch (forced upgrade, feature flags). epoch is the change log's
    (#561): null until there is one."""
    return 200, {"api": API_VERSION, "min_app": dict(MIN_APP), "features": {}, "server_time": time.time(),
                 "epoch": None}


ROUTES: list[Route] = [
    Route("GET", "/api/v1/meta", meta, "public"),
]
