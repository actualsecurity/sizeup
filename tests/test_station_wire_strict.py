"""Station <-> hub wire hygiene: strict JSON both ways (no NaN / Infinity, contracts/README.md D1) and no HEAD on the
station API (a HEAD on the job poll took the waiting jobs, D3). Real HTTP on ephemeral ports, in-process."""

import json
import math
import urllib.error
import urllib.request

import numpy as np
import pytest

from sizeup.cloud import client as wire
from sizeup.cloud import hub as hub_mod
from tests.test_cloud import TOKEN, HubRun, call, hub_cfg


@pytest.fixture
def hub(tmp_path):
    h = HubRun(hub_cfg(tmp_path))
    try:
        yield h
    finally:
        h.close()


def raw_post(url: str, data: bytes, token: str = TOKEN) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="POST", data=data,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def head(url: str, token: str | None = TOKEN) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method="HEAD", headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# -- D1: strict JSON ------------------------------------------------------------------------------------------------

def test_the_station_never_writes_nan_or_infinity_and_a_finite_body_is_written_as_before():
    body = {"at": float("nan"), "talking": [{"q": float("inf"), "f": np.float32("nan"), "g": np.float64("-inf"),
                                             "ok": np.float32(1.5), "n": np.int64(3)}],
            "status": {"radios": ({"quieting_db": -float("inf")}, {"quieting_db": 12.5})}}
    raw = wire.dumps(body)
    assert json.loads(raw, parse_constant=lambda name: pytest.fail(f"{name} on the wire")) == {
        "at": None, "talking": [{"q": None, "f": None, "g": None, "ok": 1.5, "n": 3}],
        "status": {"radios": [{"quieting_db": None}, {"quieting_db": 12.5}]}}
    plain = {"at": 1700000000.25, "talking": [{"label": "FG1", "q": 3.0}], "paging": [], "x": np.float32(2.0)}
    old = json.dumps(plain, separators=(",", ":"), default=lambda o: o.item() if hasattr(o, "item") else str(o)).encode()
    assert wire.dumps(plain) == old                                      # nothing else changes on the wire
    loop: list = []
    loop.append(loop)
    with pytest.raises(ValueError, match="Circular"):                    # not swallowed by the walk
        wire.dumps(loop)


def test_a_bare_nan_from_an_older_sender_is_read_as_none():
    assert wire.loads(b'{"at":NaN,"a":[Infinity,-Infinity,1.5]}') == {"at": None, "a": [None, None, 1.5]}


def test_the_hub_answers_strict_json():
    code, raw, _ = hub_mod._json(200, {"skew": float("nan"), "n": [float("inf"), 2]})
    assert code == 200 and raw == b'{"skew":null,"n":[null,2]}'
    with pytest.raises(TypeError):                                       # an object it can't write: as before
        hub_mod._json(200, {"x": object()})


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_at_never_makes_the_hubs_clock_skew_nan(hub, token):
    st = hub.app.hub
    code, ans = raw_post(hub.url + "/api/station/state", b'{"at":1.0e3,"talking":[],"paging":[]}')
    assert code == 200 and ans["ok"] is True and st.skew > 1e9           # a real figure first (a clock far behind)
    before = st.skew
    code, ans = raw_post(hub.url + "/api/station/state", f'{{"at":{token},"talking":[],"paging":[]}}'.encode())
    assert code == 200 and ans["ok"] is True
    assert math.isfinite(st.skew) and st.skew == before                  # left as it was: no time to compare
    code, ans = raw_post(hub.url + "/api/station/state", b'{"at":1e999,"talking":[],"paging":[]}')   # inf by overflow
    assert code == 200 and math.isfinite(st.skew) and st.skew == before


def test_the_station_client_posts_nan_as_null_and_the_hub_takes_it(hub):
    c = wire.HubClient(hub.url, TOKEN, timeout=10.0)
    try:
        ans = c.request("POST", "/api/station/state", {"at": float("nan"), "talking": [], "paging": []})
    finally:
        c.close()
    assert ans["ok"] is True and math.isfinite(hub.app.hub.skew)


# -- D3: no HEAD on the station API ---------------------------------------------------------------------------------

def test_a_head_on_the_job_poll_takes_no_job(hub):
    st = hub.app.hub
    job = st.jobs.put("call", {"fn": "set_mode", "args": []}, 60.0)
    code, headers, body = head(hub.url + "/api/station/jobs?wait=0")
    assert code == 405 and body == b"" and headers["Allow"] == "GET, POST"
    code, got = call(hub.url + "/api/station/jobs?wait=0")
    assert code == 200 and [j["id"] for j in got["jobs"]] == [job["id"]]  # the station still gets it


def test_no_head_reaches_a_station_handler(hub, monkeypatch):
    ran = []
    monkeypatch.setattr(hub.app.hub, "handle", lambda *a, **k: ran.append(a) or (200, b"{}", "application/json"))
    for path in ("/api/station/hello", "/api/station/status", "/api/station/jobs?wait=5", "/api/station/state",
                 "/api/station/stt/jobs/0123456789abcdef/audio"):
        for token in (TOKEN, None):
            code, _, body = head(hub.url + path, token)
            assert (code, body) == (405, b""), path
    assert ran == []
    assert not hub.app.hub.connected()                                   # a HEAD never counts as the station there


def test_head_still_answers_as_get_for_pages_and_reads(hub):
    for path in ("/healthz", "/api/state", "/api/scene"):
        code, headers, body = head(hub.url + path, None)
        assert code == 200 and body == b"" and headers["Content-Type"].startswith("application/json"), path
