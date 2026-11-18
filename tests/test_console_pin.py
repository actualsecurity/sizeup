"""The console is unchanged (#619, safeguard 2): the route tables of /api/cad, /api/station and /healthz and a sample
of their responses (status, body, headers) are pinned in tests/fixtures/console_pin.json, written from main before
any mobile change. A later mobile change that alters one of them fails here; an intended console change re-pins:

    SIZEUP_PIN_WRITE=1 .venv/bin/python -m pytest tests/test_console_pin.py

Routes are the string literals in the three files that serve them (the server's if-chain, the CAD's and the station
hub's tables). The route half is a SUBSET check: every pinned route must still be there, so a route removed or renamed
fails, but a route ADDED passes. Many branches add console routes in parallel; an equality check would fail every
unrelated pull request and conflict on the fixture. The sample responses are exact (status, headers, body).
The responses come from a real hub run in this process with an empty database (nothing here is a real
incident), the clock-dependent parts normalised.

The one intended console change of the mobile work (#619) is the last sample: an /api/cad request carrying
`Authorization: Bearer <fake>` is a 401 in the console's JSON shape (a device token is not for the console). It was
added to the pin alone: the other eight samples and the route tables are byte-identical to what main produced."""
from __future__ import annotations

import ast
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.test_cloud import HubRun, hub_cfg

ROOT = Path(__file__).resolve().parent.parent
PIN = ROOT / "tests" / "fixtures" / "console_pin.json"
SERVING = ("sizeup/web/server.py", "sizeup/cad/api.py", "sizeup/cloud/hub.py")
VOLATILE_HEADERS = {"date", "server-timing", "x-request-id"}

SAMPLE = [("GET", "/healthz"), ("GET", "/api/cad/whoami"), ("GET", "/api/cad/nope"), ("POST", "/api/cad/nope"),
          ("GET", "/api/station/status"), ("GET", "/api/station/nope"),
          ("POST", "/api/station/events"), ("GET", "/api/cad/search?q="),
          ("GET", "/api/cad/whoami", {"Authorization": "Bearer fake-device-token"})]


def route_table() -> list[str]:
    out = set()
    for f in SERVING:
        for node in ast.walk(ast.parse((ROOT / f).read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and not re.search(r"\s", node.value)
                    and (node.value.startswith(("/api/cad", "/api/station")) or node.value == "/healthz")):
                out.add(f"{f}: {node.value}")
    return sorted(out)


def missing_routes(pinned: list[str], now: list[str]) -> list[str]:
    """The pinned routes that are no longer served: removed or changed. Added routes are not reported."""
    return sorted(set(pinned) - set(now))


def normal(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    s = re.sub(r'("(?:at|ts|time|server_time|now|started|built|uptime_s|since)":\s*)[0-9.eE+-]+', r"\1T", s)
    return re.sub(r'"(?:build|sha|git)":\s*"[^"]*"', '"build":"B"', s)


def fetch(url, method, path, extra=None):
    req = urllib.request.Request(url + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={**({"Content-Type": "application/json"} if method == "POST" else {}),
                                          **(extra or {})})
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        r = e
    h = {k.lower(): v for k, v in r.headers.items() if k.lower() not in VOLATILE_HEADERS}
    h["server"] = re.sub(r"[0-9][0-9.]*", "N", h.get("server", ""))      # the Python version differs by machine
    return {"status": r.status if hasattr(r, "status") else r.code, "headers": dict(sorted(h.items())),
            "body": normal(r.read())}


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    h = HubRun(hub_cfg(tmp_path_factory.mktemp("pin")))
    yield h
    h.close()


def sample(hub) -> dict:
    return {f"{m} {p}" + (" +Authorization" if x else ""): fetch(hub.url, m, p, *x) for m, p, *x in SAMPLE}


def test_the_consoles_routes_and_responses_are_pinned(hub):
    now = {"routes": route_table(), "responses": sample(hub)}
    if os.environ.get("SIZEUP_PIN_WRITE"):
        PIN.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
    pinned = json.loads(PIN.read_text())
    assert missing_routes(pinned["routes"], now["routes"]) == []
    for k, v in pinned["responses"].items():
        assert now["responses"][k] == v, k


def test_the_route_check_catches_a_removed_route_and_allows_an_added_one():
    pinned = json.loads(PIN.read_text())["routes"]
    code = route_table()
    assert missing_routes(pinned, code) == []
    assert missing_routes(pinned + ["sizeup/cad/api.py: /api/cad/not-served"], code) == \
        ["sizeup/cad/api.py: /api/cad/not-served"]                       # a pinned route the code lacks: caught
    assert missing_routes(pinned, [r for r in code if not r.endswith(": /healthz")]) != []    # one removed: caught
    assert missing_routes(pinned, code + ["sizeup/cad/api.py: /api/cad/a-new-route"]) == []   # one added: passes


def test_the_pin_has_teeth():
    pinned = json.loads(PIN.read_text())
    assert len(pinned["routes"]) > 100 and len(pinned["responses"]) == len(SAMPLE) == 9
    assert any(r.endswith(": /healthz") for r in pinned["routes"])
