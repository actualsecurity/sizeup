"""contracts/routes.json (ADR 0004; docs/nodes.md 3): every /api/station/ route the hub serves has a row, and every
row is served. The routes are read from the hub's own dispatchers (tests/contracts.py served_paths), then each row is
asked of a real hub with each kind of token. A route added to the code without a row fails here: fail closed."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from sizeup.cloud import hub as hub_mod
from tests import contracts as c

ROOT = Path(__file__).resolve().parent.parent
KINDS = ("sz1", "shared", "status", "sw1")
TABLE = c.routes()
ROWS = TABLE["routes"]


def row_id(row) -> str:
    return f"{row['method']} {row['path']}"


def test_the_table_is_well_formed():
    roles = set(c.load("roles")["properties"])
    assert len({row_id(r) for r in ROWS}) == len(ROWS), "a route listed twice"
    for r in ROWS + TABLE["reserved"]:
        if "path" in r:
            assert set(r) - {"note"} == {"method", "path", "credential", "role", "request", "response"}, row_id(r)
            assert r["method"] in ("GET", "POST") and r["path"].startswith("/api/station/")
            assert r["credential"] and set(r["credential"]) <= set(KINDS)
            for side in ("request", "response"):
                assert r[side] is None or r[side] in c.names(), (row_id(r), r[side])
        assert set(r["role"]) == {"sz1", "sw1"}
        for kind, need in r["role"].items():
            assert need in ("always", "never") or (isinstance(need, list) and need and set(need) <= roles), (r, kind)
    assert all(r["role"]["sw1"] != "always" for r in ROWS)             # a worker's token opens nothing without a role


def test_the_tables_invariants_from_the_design():
    """docs/nodes.md 3: a worker's token opens nothing outside /api/station/stt/; a station's opens no infer or train
    route; a status token opens the status route only; prompts go to `infer` alone."""
    for r in ROWS:
        worker_route = r["path"].startswith("/api/station/stt/")
        if not worker_route:
            assert r["role"]["sw1"] == "never" and "sw1" not in r["credential"], row_id(r)
        if "/infer/" in r["path"]:
            assert r["role"] == {"sz1": "never", "sw1": ["infer"]} and r["credential"] == ["sw1"], row_id(r)
        assert ("status" in r["credential"]) == (r["path"] == "/api/station/status"), row_id(r)
    for r in TABLE["reserved"]:
        assert r["role"]["sz1"] == "never"


def test_every_route_in_the_hubs_code_has_a_row_and_every_row_is_in_the_code():
    served = c.served_paths()
    listed = {c.example(r["path"]) for r in ROWS}
    assert served - listed == set(), "the hub routes these and contracts/routes.json has no row for them"
    assert listed - served == set(), "contracts/routes.json lists these and the hub's dispatchers do not route them"
    assert {p for _, p in hub_mod.STATION_ROUTES} <= listed            # the hub's own list of the station's routes
    for res in TABLE["reserved"]:
        assert not [p for p in served if p.startswith(res["prefix"])], f"{res['prefix']} is served now: list its routes"


def test_no_path_under_api_station_is_named_in_the_code_without_a_row():
    """The wider net: any string in sizeup/ or tools/ that starts with /api/station/ (a client's path, a regex, a
    prefix) is a listed route or the start of one. A route written anywhere before it is listed fails here."""
    listed = {c.example(r["path"]) for r in ROWS}
    found: dict[str, str] = {}
    for f in sorted((ROOT / "sizeup").rglob("*.py")) + sorted((ROOT / "tools").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs \
                    and n.value.startswith("/api/station/"):
                found[n.value] = str(f.relative_to(ROOT))
    assert len(found) >= 15, found                                     # (the scan sees them at all)
    for text, where in found.items():
        paths = c._expand(text) if "(" in text else [text.split("?")[0]]
        for p in paths:
            assert any(x == p or x.startswith(p) for x in listed), f"{where} names {text!r}: no such route is listed"


@pytest.fixture
def live(tmp_path):
    lv = c.Live(tmp_path)
    lv.hub.app.hub.workers.set_pool(stations={c.STATION: True})       # the station's whisper is in the pool
    yield lv
    lv.close()


def ask(live, method: str, row: dict, token: str | None):
    path = c.example(row["path"]) + ("?wait=0" if row["path"] == "/api/station/jobs" else "")
    return live.call(method, path, {"wait": 0} if method == "POST" else None, token=token)


def unserved(code, body) -> bool:
    """The hub's answer for a method and path it routes nowhere."""
    return (code == 404 and body == {"error": "not found"}) or (code == 405 and body == {"error": "GET only"})


def test_every_row_is_served_with_its_method_and_its_kinds_of_token_and_refuses_the_others(live):
    for row in ROWS:
        other = "GET" if row["method"] == "POST" else "POST"
        for kind in KINDS:
            code, body = ask(live, row["method"], row, kind)
            if kind in row["credential"]:
                assert code not in (401, 403) and not unserved(code, body), (row_id(row), kind, code, body)
            else:
                assert code in (401, 403) and isinstance(body, dict) and body.get("reason"), (row_id(row), kind, code, body)
        code, body = ask(live, row["method"], row, None)
        assert code == 401 and body["reason"] == "no_token", (row_id(row), code, body)
        code, body = ask(live, other, row, row["credential"][0])
        assert unserved(code, body), (row_id(row), "is also served as", other, code, body)


def test_a_stations_token_opens_the_workers_routes_only_while_its_whisper_is_in_the_pool(live):
    """docs/nodes.md 3, as built today: off is 403 (the station backs off instead of retrying), never 404; and the
    prompts' routes are 403 for a station's token whatever the pool says."""
    live.hub.app.hub.workers.set_pool(stations={c.STATION: False})
    for row in ROWS:
        if row["path"].startswith("/api/station/stt/"):
            for kind in ("sz1", "shared"):
                code, body = ask(live, row["method"], row, kind)
                assert code == 403 and body["reason"] == "stations_off", (row_id(row), kind, code, body)
    live.hub.app.hub.workers.set_pool(stations={c.STATION: True})
    for row in ROWS:
        if "/infer/" in row["path"]:
            code, body = ask(live, row["method"], row, "sz1")
            assert code == 403 and body["reason"] == "gpu_only", (row_id(row), code, body)


def test_a_path_that_is_no_route_is_not_served(live):
    for method, path in (("POST", "/api/station/train/claim"), ("GET", "/api/station/train/claim"),
                         ("POST", "/api/station/from_the_future"), ("GET", "/api/station/stt/from_the_future"),
                         ("POST", "/api/station/stt/jobs/" + c.HEX16 + "/from_the_future"),
                         ("POST", "/api/station/jobs/not-a-job-id")):
        for kind in ("sz1", "sw1"):
            code, body = live.call(method, path, {} if method == "POST" else None, token=kind)
            assert code in (401, 403) or unserved(code, body), (method, path, kind, code, body)


def test_the_path_templates_expand_as_the_codes_patterns_do():
    assert c._expand(hub_mod.JOB_PATH.pattern) == ["/api/station/jobs/" + c.HEX16]
    assert c.example("/api/station/stt/jobs/{lease}/audio") == f"/api/station/stt/jobs/{c.HEX16}/audio"
    assert all(re.fullmatch(r"[a-z0-9/_{}-]+", r["path"]) for r in ROWS)
    with pytest.raises(AssertionError):
        c._expand(r"/api/station/x/(\d+)")                            # a pattern it can't read fails, never passes
