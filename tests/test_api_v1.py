"""/api/v1 (#557): the route table is the spec's `paths`, every answer validates against the spec, every error has the
one envelope, and the station has no app API. The spec is api/openapi/v1/ (tests/openapi_spec.py bundles it)."""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from sizeup.api.v1 import API_VERSION
from sizeup.api.v1.handler import V1
from sizeup.api.v1.routes import ROUTES
from sizeup.api.v1.switches import Switches
from sizeup.cloud.access import Identity
from sizeup.web.server import WebServer
from tests import openapi_spec as oas

# [mobile] enabled: the master switch on (the safeguards' tests are in test_api_v1_safeguards.py)
ON = Switches(lambda: True)
DOC = oas.spec()
PKG = Path(__file__).resolve().parent.parent / "sizeup" / "api"


class Access:
    """Cloudflare Access as far as the server goes: the person named in X-Test-User (none: not signed in)."""

    def identify(self, headers):
        e = headers.get("X-Test-User")
        return Identity(email=e) if e else None


def serve(**kw):
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.httpd.server_address[1]}"


@pytest.fixture
def hub():
    srv, url = serve(access=Access(), role="hub", mobile=ON)
    yield url
    srv.shutdown()


def call(url, path, method="GET", who="person@example.org", data=None):
    req = urllib.request.Request(url + path, method=method, data=data,
                                 headers={"X-Test-User": who} if who else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def conforms(status, body, method, path):
    schema = oas.response_schema(DOC, method, path, status)
    assert schema is not None, f"{method} {path} {status} has no schema in the spec"
    errs = list(Draft202012Validator(schema).iter_errors(json.loads(body)))
    assert not errs, [e.message for e in errs]


def test_the_spec_is_valid_openapi_3_1():
    validate(DOC)
    assert DOC["openapi"].startswith("3.1") and DOC["info"]["version"] == API_VERSION


def test_the_route_table_is_the_specs_paths_both_ways():
    served = {(r.method, r.path) for r in ROUTES}
    promised = oas.operations(DOC)
    assert served - promised == set(), "served but not in the spec"
    assert promised - served == set(), "in the spec but not served"
    assert all(r.path.startswith("/api/v1/") for r in ROUTES)


def test_the_spec_examples_validate_against_their_schemas():
    for path, item in DOC["paths"].items():
        for method, op in item.items():
            for status, r in op["responses"].items():
                for media in (r.get("content") or {}).values():
                    if "example" in media:
                        errs = list(Draft202012Validator(media["schema"]).iter_errors(media["example"]))
                        assert not errs, (method, path, status, [e.message for e in errs])


def test_meta_answers_and_validates_against_the_spec(hub):
    status, headers, body = call(hub, "/api/v1/meta")
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    conforms(status, body, "GET", "/api/v1/meta")
    m = json.loads(body)
    assert m["api"] == API_VERSION and m["epoch"] is None and set(m["min_app"]) == {"ios", "android"}
    assert "no-store" in headers["Cache-Control"]


def test_head_is_a_get_without_its_body(hub):
    g_status, g_headers, g_body = call(hub, "/api/v1/meta")
    h_status, h_headers, h_body = call(hub, "/api/v1/meta", "HEAD")
    assert (g_status, h_status) == (200, 200) and g_body and h_body == b""
    volatile = {"Date", "Server-Timing", "Content-Length"}               # the clock moves; the body is not sent
    assert {k: v for k, v in h_headers.items() if k not in volatile} == \
        {k: v for k, v in g_headers.items() if k not in volatile}
    assert h_headers["Content-Type"].startswith("application/json") and "no-store" in h_headers["Cache-Control"]


def test_every_error_is_the_one_envelope(hub):
    for method, path, who, want in [("GET", "/api/v1/nope", "person@example.org", 404),
                                    ("POST", "/api/v1/meta", "person@example.org", 405),
                                    ("GET", "/api/v1/", None, 404),
                                    ("GET", "/api/v1/" + "x" * 600, "person@example.org", 414)]:
        status, headers, body = call(hub, path, method, who, data=b"{}" if method == "POST" else None)
        assert status == want, (method, path)
        err = json.loads(body)
        assert set(err) == {"error"} and {"code", "message"} <= set(err["error"])
        assert re.fullmatch(r"[a-z_]+", err["error"]["code"])
        conforms(status, body, "GET", "/api/v1/meta")      # the spec's `default` response is the Error schema
    status, headers, _ = call(hub, "/api/v1/meta", "POST", data=b"{}")
    assert headers["Allow"] == "GET"


def test_retry_after_is_in_the_header_and_the_envelope():
    from sizeup.api.v1.errors import error
    status, body, ctype, headers = error(503, "updating", "try again", retry_after_s=5)
    assert headers["Retry-After"] == "5" and json.loads(body)["error"]["retry_after_s"] == 5


def test_access_is_not_v1s_door(hub):
    """/api/v1 is its own door (a device's token, #619): no Access person is asked for, so a request with none gets
    meta (public) and the envelope's 404 elsewhere, never the console's 401 page; a handler is reached by a token
    only."""
    status, _, body = call(hub, "/api/v1/meta", who=None)
    assert status == 200
    conforms(status, body, "GET", "/api/v1/meta")
    assert call(hub, "/api/v1/", who=None)[0] == 404


def test_the_station_has_no_app_api():
    srv, url = serve(role="station")
    try:
        status, _, body = call(url, "/api/v1/meta", who=None)
        assert status == 409 and json.loads(body)["error"]["code"] == "station"
        assert call(url, "/api/v1/meta", "POST", None, data=b"{}")[0] == 409
    finally:
        srv.shutdown()


def test_all_in_one_serves_it_too():
    srv, url = serve()
    try:
        status, _, body = call(url, "/api/v1/meta", who=None)
        assert status == 200
        conforms(status, body, "GET", "/api/v1/meta")
    finally:
        srv.shutdown()


def test_path_templates_match_one_segment():
    seen = []

    def h(ctx):
        seen.append(ctx.params)
        return 200, {}

    from sizeup.api.v1.routes import Route
    v1 = V1([Route("GET", "/api/v1/incidents/{id}", h, "public", "reads")],
            switches=Switches(lambda: True, lambda: {"reads": True}))
    assert v1.respond("GET", "/api/v1/incidents/E99-1", "", {})[0] == 200 and seen == [{"id": "E99-1"}]
    assert v1.respond("GET", "/api/v1/incidents/a/b", "", {})[0] == 404
    assert v1.respond("DELETE", "/api/v1/incidents/E99-1", "", {})[0] == 405
    assert v1.respond("GET", "/api/v1/incidents/", "", {})[0] == 404


def test_the_package_never_touches_a_database():
    """No startup database writes: nothing in sizeup/api reads or writes a store."""
    for f in PKG.rglob("*.py"):
        # (the one import of the CAD's own exception class, CadError, is allowed: reads.py answers a missing row)
        if f.name == "changelog.py":          # the one exception (#561): the change log's own connection to hub.sqlite
            continue
        src = f.read_text()
        if f.name == "reads.py":      # only reads.py, and only that whole line
            src = re.sub(r"^from sizeup\.cad\.store import CadError\s*(#.*)?$", "", src, flags=re.M)
        assert not re.search(r"^\s*(import|from)\s+\S*(sqlite3|store|sizeup\.cad)", src, re.M), f


def test_the_browsers_endpoints_are_not_v1s():
    """The console keeps its own unversioned API until it is migrated (docs/mobile-api.md section 3.1): v1 claims
    only /api/v1/, and nothing the console's scripts call is under it."""
    static = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
    called = set()
    for f in static.glob("*.js"):
        called |= set(re.findall(r"""["'`](/api/[\w/.\-]*)""", f.read_text()))
    assert "/api/cad/state" in called and "/api/stream" in called, "the scan found nothing: the pattern is stale"
    v1 = V1()
    assert not [p for p in called if p.startswith("/api/v1") or v1.handles(p)]
    for p in ("/api/cad/state", "/api/cad/stream", "/api/state", "/api/stream", "/api/scene", "/api/station/state",
              "/healthz", "/api/v1", "/api/v10/meta"):
        assert not v1.handles(p), p


def test_every_route_declares_who_may_call_it_and_public_ones_are_few():
    from sizeup.api.v1.routes import NEEDS, PUBLIC
    assert all(r.need in NEEDS for r in ROUTES)
    assert {(r.method, r.path) for r in ROUTES if r.need == "public"} <= PUBLIC
    # the spec says the same: a public operation has `security: []`, every other one the device token
    for r in ROUTES:
        op = DOC["paths"][r.path][r.method.lower()]
        assert (op.get("security") == []) == (r.need == "public"), (r.method, r.path)


@pytest.fixture
def hub_srv():
    srv, url = serve(access=Access(), role="hub", mobile=ON)
    yield srv, url
    srv.shutdown()


def test_the_handover_503_is_in_the_envelope_and_the_consoles_is_not(hub_srv):
    srv, url = hub_srv
    srv.gate.draining.set()                                   # SIGTERM: draining for the next task
    status, headers, body = call(url, "/api/v1/meta")
    assert status == 503 and headers["Retry-After"] == "2"
    err = json.loads(body)["error"]
    assert err["code"] == "updating" and err["retry_after_s"] == 2
    conforms(status, body, "GET", "/api/v1/meta")
    status, _, body = call(url, "/api/cad/state")
    assert status == 503 and not isinstance(json.loads(body).get("error"), dict)   # the console's own 503, unchanged


def test_other_methods_answer_in_the_envelope_only_under_v1(hub_srv):
    _, url = hub_srv
    status, headers, body = call(url, "/api/v1/meta", "PUT", data=b"{}")
    assert status == 405 and headers["Allow"] == "GET"
    conforms(status, body, "GET", "/api/v1/meta")
    assert call(url, "/api/v1/nope", "DELETE")[0] == 404
    assert call(url, "/api/v1/meta", "PATCH", who=None, data=b"{}")[0] == 405
    status, _, body = call(url, "/api/cad/state", "PUT", data=b"{}")
    assert status == 501 and b"Unsupported method" in body       # http.server's answer, as before
