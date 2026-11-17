"""Sharing the production hub (#619, docs/mobile-api.md "Sharing the production hub"): /api/v1 is inert until switched
on, a v1 failure never reaches the console, v1 is capped, and the app's door is a device token only. The console's own
responses are pinned in test_console_pin.py. The values here are invented."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sizeup.api.v1 import handler as v1_handler
from sizeup.api.v1.errors import JSON
from sizeup.api.v1.handler import V1
from sizeup.api.v1.limits import Limits
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.routes import NEEDS, PUBLIC, ROUTES, Route
from sizeup.api.v1.switches import FEATURES, Switches
from sizeup.config import ConfigError, load
from sizeup.web import server as web_server
from tests.test_cloud import HubRun, hub_cfg

ROOT = Path(__file__).resolve().parent.parent
DEVICE = Principal("dev-0001", "member@example.org")


class Box:
    """The switches as a test turns them: read on every request, like the hub's config."""

    def __init__(self, enabled=True, **flags):
        self.enabled, self.flags = enabled, flags

    def switches(self, **kw) -> Switches:
        return Switches(lambda: self.enabled, lambda: self.flags, **kw)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def table(seen: list, boom: Exception | None = None):
    def h(ctx):
        seen.append(ctx.principal)
        if boom is not None:
            raise boom
        return 200, {"ok": True, "features": ctx.features}
    return [Route("GET", "/api/v1/meta", h, "public"),
            Route("GET", "/api/v1/things", h, "device", "reads"),
            Route("POST", "/api/v1/things", h, "device", "writes"),
            Route("GET", "/api/v1/open", h, "public", "reads")]


def err(r):
    status, body, ctype, headers = r
    assert ctype == JSON and headers["Cache-Control"] == "no-store"
    e = json.loads(body)["error"]
    return status, e["code"], headers


# -- the master switch and the flags ----------------------------------------------------------------------------

def test_the_switch_is_off_by_default_and_every_v1_path_but_meta_is_a_404():
    seen, resolved = [], []
    v1 = V1(table(seen), resolver=lambda h: resolved.append(1))
    for method, path in [("GET", "/api/v1/things"), ("POST", "/api/v1/things"), ("GET", "/api/v1/open"),
                         ("GET", "/api/v1/nope"), ("DELETE", "/api/v1/things"), ("POST", "/api/v1/meta"),
                         ("PUT", "/api/v1/meta"), ("OPTIONS", "/api/v1/meta")]:
        status, code, _ = err(v1.respond(method, path, "", {"Authorization": "Bearer x"}))
        assert (status, code) == (404, "not_found"), (method, path)
    assert seen == [] and resolved == []            # no handler, no token read
    for method in ("GET", "HEAD"):
        assert v1.respond(method, "/api/v1/meta", "", {})[0] == 200
    assert v1.respond("GET", "/api/v1/" + "x" * 600, "", {})[0] == 414


def test_off_is_the_same_answer_as_a_path_that_does_not_exist():
    v1 = V1(table([]), switches=Box(enabled=False).switches())
    assert v1.respond("GET", "/api/v1/things", "", {})[1] == v1.respond("GET", "/api/v1/nope", "", {})[1]


def test_the_switch_is_read_on_every_request_and_meta_reports_every_flag_off_when_it_is_off():
    box, seen = Box(enabled=False, reads=True), []
    v1 = V1(table(seen), switches=box.switches(), resolver=lambda h: DEVICE)
    meta = lambda: json.loads(v1.respond("GET", "/api/v1/meta", "", {})[1])["features"]
    assert meta() == {f: False for f in FEATURES}
    assert v1.respond("GET", "/api/v1/things", "", {})[0] == 404
    box.enabled = True
    assert v1.respond("GET", "/api/v1/things", "", {})[0] == 200
    assert meta() == {**{f: False for f in FEATURES}, "reads": True}
    box.enabled = False
    assert v1.respond("GET", "/api/v1/things", "", {})[0] == 404 and meta()["reads"] is False
    assert seen.count(DEVICE) == 1                    # the one handler run of /things: the two 404s ran nothing


def test_a_flag_off_route_behaves_like_the_switch_off_and_a_flag_on_one_is_served():
    box, seen = Box(reads=False, writes=False), []
    v1 = V1(table(seen), switches=box.switches(), resolver=lambda h: DEVICE)
    for method, path in [("GET", "/api/v1/things"), ("POST", "/api/v1/things"), ("GET", "/api/v1/open")]:
        assert err(v1.respond(method, path, "", {}))[:2] == (404, "not_found")
    box.flags["reads"] = True
    assert v1.respond("GET", "/api/v1/things", "", {})[0] == 200 and v1.respond("GET", "/api/v1/open", "", {})[0] == 200
    assert v1.respond("POST", "/api/v1/things", "", {})[0] == 404         # its own flag, still off
    assert v1.respond("GET", "/api/v1/meta", "", {})[0] == 200
    flags = json.loads(v1.respond("GET", "/api/v1/meta", "", {})[1])["features"]
    assert list(flags) == list(FEATURES) and flags["reads"] and not flags["writes"]


def test_a_broken_switch_is_an_off_switch():
    def boom():
        raise RuntimeError("no config")
    v1 = V1(table([]), switches=Switches(boom))
    assert v1.respond("GET", "/api/v1/things", "", {})[0] == 404 and v1.respond("GET", "/api/v1/meta", "", {})[0] == 200
    assert V1(table([]), switches=Switches(lambda: True, boom)).respond("GET", "/api/v1/things", "", {})[0] == 404


def test_every_route_declares_its_flag_and_who_may_call_it():
    assert ROUTES and all(r.need in NEEDS for r in ROUTES)
    for r in ROUTES:
        if (r.method, r.path) in PUBLIC:
            assert r.need == "public" and r.flag == ""             # meta: the one route served with mobile off
        else:
            assert r.flag in FEATURES, (r.method, r.path)           # every other route names the flag it needs
    assert set(FEATURES) == {"auth", "reads", "stream", "writes", "push", "media", "responders"}


# -- the catch-all ----------------------------------------------------------------------------------------------

def test_a_handler_that_raises_is_a_500_in_the_envelope_and_logs_the_class_only():
    logs, secret = [], "Bearer sk-live-0123456789abcdef"
    v1 = V1(table([], ValueError(f"bad token {secret}")), switches=Box(reads=True).switches(),
            resolver=lambda h: DEVICE, log=logs.append)
    status, code, headers = err(v1.respond("GET", "/api/v1/things", "q=1", {"Authorization": secret}))
    assert (status, code) == (500, "internal")
    assert logs == ["api v1: GET /api/v1/things failed: ValueError"]
    assert "sk-live" not in json.dumps(logs) and "bad token" not in json.dumps(logs)


def test_a_broken_resolver_or_log_is_a_500_not_a_crash_and_no_handler_runs():
    seen = []

    def resolver(h):
        raise OSError("keystore down")

    def log(msg):
        raise OSError("disk full")
    v1 = V1(table(seen), switches=Box(reads=True).switches(), resolver=resolver, log=log)
    assert err(v1.respond("GET", "/api/v1/things", "", {}))[:2] == (500, "internal") and seen == []


def test_the_handover_signal_is_not_swallowed():
    from sizeup.fence import FencedWrite
    v1 = V1(table([], FencedWrite("not the leader")), switches=Box(reads=True).switches(), resolver=lambda h: DEVICE)
    with pytest.raises(FencedWrite):
        v1.respond("GET", "/api/v1/things", "", {})


def test_the_in_flight_slot_is_given_back_when_a_handler_raises():
    v1 = V1(table([], KeyError("x")), switches=Box(reads=True).switches(), resolver=lambda h: DEVICE,
            limits=Limits(1, 1, Clock()), log=lambda m: None)
    for _ in range(3):
        assert v1.respond("GET", "/api/v1/things", "", {})[0] == 500        # never a 503 busy: the slot came back


@pytest.fixture
def hub_with_v1(tmp_path, monkeypatch):
    """A real hub (the console and its database) with [mobile] on and a v1 route table that fails on /api/v1/boom."""
    seen = []

    def boom(ctx):
        raise RuntimeError("secret body " + (ctx.headers.get("Authorization") or ""))
    routes = table(seen) + [Route("GET", "/api/v1/boom", boom, "public", "reads")]
    on = Switches(lambda: True, lambda: {"reads": True})
    monkeypatch.setattr(web_server, "V1", lambda **kw: V1(routes, **{**kw, "switches": on}))
    h = HubRun(hub_cfg(tmp_path, mobile_enabled=True))
    h.seen = seen
    yield h
    h.close()


def http(url, path, method="GET", headers=None, data=None):
    req = urllib.request.Request(url + path, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_a_v1_failure_never_stops_the_console(hub_with_v1, capsys):
    h = hub_with_v1
    status, _, body = http(h.url, "/api/v1/boom", headers={"Authorization": "Bearer sk-live-1234"})
    assert status == 500 and json.loads(body)["error"]["code"] == "internal"
    for _ in range(3):
        assert http(h.url, "/api/v1/boom")[0] == 500
        assert http(h.url, "/healthz")[0] == 200
        assert http(h.url, "/api/cad/whoami")[0] == 200
    out = capsys.readouterr().out
    assert "api v1: GET /api/v1/boom failed: RuntimeError" in out
    assert "secret body" not in out and "sk-live" not in out


# -- resource caps ----------------------------------------------------------------------------------------------

def test_the_token_buckets_are_the_designs_reads_10_a_second_burst_30_writes_2_burst_10():
    clock = Clock()
    lim = Limits(clock=clock)
    assert [lim.admit("d1", False) for _ in range(30)] == [0] * 30
    assert lim.admit("d1", False) == 1                           # the 31st: wait 1 s
    clock.t += 0.1
    assert lim.admit("d1", False) == 0 and lim.admit("d1", False) == 1     # 10 a second: one token per 0.1 s
    clock.t += 10
    assert [lim.admit("d1", False) for _ in range(30)] == [0] * 30       # refilled, never past the burst
    assert lim.admit("d1", False) > 0
    assert [lim.admit("d1", True) for _ in range(10)] == [0] * 10       # writes: their own bucket
    assert lim.admit("d1", True) == 1
    clock.t += 0.5
    assert lim.admit("d1", True) == 0 and lim.admit("d1", True) == 1
    assert lim.admit("d2", False) == 0 and lim.admit("d2", True) == 0    # another device: its own


def test_the_limit_is_a_429_with_retry_after_and_the_envelopes_retry_after_s():
    clock = Clock()
    v1 = V1(table([]), switches=Box(reads=True, writes=True).switches(), resolver=lambda h: DEVICE,
            limits=Limits(clock=clock))
    assert all(v1.respond("POST", "/api/v1/things", "", {})[0] == 200 for _ in range(10))
    status, body, _, headers = v1.respond("POST", "/api/v1/things", "", {})
    assert status == 429 and headers["Retry-After"] == "1" and "Retry-After" in headers
    e = json.loads(body)["error"]
    assert e["code"] == "rate_limited" and e["retry_after_s"] == 1
    assert v1.respond("GET", "/api/v1/things", "", {})[0] == 200      # reads are not the writes' bucket
    clock.t += 1
    assert v1.respond("POST", "/api/v1/things", "", {})[0] == 200


def test_requests_without_a_principal_are_limited_per_address_and_all_together():
    clock = Clock()
    v1 = V1(table([]), switches=Box(reads=True).switches(), limits=Limits(clock=clock))
    codes = lambda peer, n: [v1.respond("GET", "/api/v1/meta", "", {}, peer)[0] for _ in range(n)]
    assert codes("203.0.113.5", 15) == [200] * 15 and codes("203.0.113.5", 1) == [429]
    assert codes("203.0.113.6", 1) == [200]                          # another address
    # a flood of bad tokens is limited too, and still a 401 until it is
    assert v1.respond("GET", "/api/v1/things", "", {}, "203.0.113.7")[0] == 401
    for i in range(100):                                              # many addresses: the shared bucket (burst 100)
        v1.respond("GET", "/api/v1/meta", "", {}, f"198.51.100.{i % 250}")
    assert v1.respond("GET", "/api/v1/meta", "", {}, "198.51.100.250")[0] == 429


def test_the_buckets_are_bounded_in_memory():
    from sizeup.api.v1 import limits
    clock, lim = Clock(), Limits(clock=Clock())
    for i in range(limits.MAX_KEYS + 500):
        lim.admit(f"dev-{i}", False)
    assert len(lim._buckets) <= limits.MAX_KEYS


def test_v1s_share_of_concurrent_requests_is_capped_and_the_console_is_not_held_up():
    gate, inside = threading.Event(), threading.Semaphore(0)

    def slow(ctx):
        inside.release()
        gate.wait(10)
        return 200, {}
    v1 = V1([Route("GET", "/api/v1/slow", slow, "public", "reads")], switches=Box(reads=True).switches(),
            limits=Limits(2, 4, Clock()))
    out = []
    ts = [threading.Thread(target=lambda: out.append(v1.respond("GET", "/api/v1/slow", "", {}, f"192.0.2.{i}")[0]))
          for i in range(2)]
    for t in ts:
        t.start()
    assert inside.acquire(timeout=5) and inside.acquire(timeout=5)
    status, body, _, headers = v1.respond("GET", "/api/v1/slow", "", {}, "192.0.2.9")      # a third: refused at once
    assert status == 503 and headers["Retry-After"] == "1" and json.loads(body)["error"]["code"] == "busy"
    gate.set()
    for t in ts:
        t.join(5)
    assert out == [200, 200] and v1.respond("GET", "/api/v1/slow", "", {}, "192.0.2.9")[0] == 200


def test_at_most_two_streams_per_device_and_a_cap_on_all():
    lim = Limits(max_streams=3)
    assert lim.open_stream("d1") and lim.open_stream("d1") and not lim.open_stream("d1")
    assert lim.open_stream("d2") and not lim.open_stream("d3") and lim.streams() == 3     # the global cap
    lim.close_stream("d1")
    assert lim.open_stream("d3") and not lim.open_stream("d1")
    for d in ("d1", "d2", "d3", "d3", "d1", "zz"):
        lim.close_stream(d)                                                               # never negative
    assert lim.streams() == 0 and lim.open_stream("d1")


# -- who: fail closed ---------------------------------------------------------------------------------------------

def test_a_device_route_is_a_401_until_there_are_devices():
    seen = []
    v1 = V1(table(seen), switches=Box(reads=True, writes=True).switches())
    for method in ("GET", "POST"):
        assert err(v1.respond(method, "/api/v1/things", "", {"Authorization": "Bearer abc.def.ghi"}))[:2] == (401, "unauthorized")
    assert seen == []


@pytest.mark.parametrize("who", [None, "", "member@example.org", {"email": "member@example.org"}, ("dev", "m@e.org"),
                                 Principal("", "member@example.org"), Principal("dev-1", ""), Principal("dev-1", "svc"),
                                 object()])
def test_only_a_device_of_a_person_is_a_principal(who):
    """A resolver that yields anything else (nobody, a station, a service, a bare email) is a 401: never a handler."""
    seen = []
    v1 = V1(table(seen), switches=Box(reads=True).switches(), resolver=lambda h: who)
    assert err(v1.respond("GET", "/api/v1/things", "", {}))[:2] == (401, "unauthorized") and seen == []


def test_the_handler_gets_the_principal_and_never_a_cad():
    seen = []
    v1 = V1(table(seen), switches=Box(reads=True).switches(), resolver=lambda h: DEVICE)
    assert v1.respond("GET", "/api/v1/things", "", {})[0] == 200 and seen == [DEVICE]
    from sizeup.api.v1.routes import Ctx
    assert not {"cad", "api", "who", "store"} & set(Ctx._fields)         # nothing in a handler's hands reaches the CAD


def test_an_access_cookie_or_header_is_not_a_device(hub_with_v1):
    """A signed-in browser (Access) on a device route is a 401, and the handler never runs."""
    h = hub_with_v1
    for hdr in ({"Cf-Access-Jwt-Assertion": "a.b.c"}, {"Cookie": "CF_Authorization=a.b.c"}, {"X-Test-User": "m@e.org"}):
        status, _, body = http(h.url, "/api/v1/things", headers=hdr)
        assert status == 401 and json.loads(body)["error"]["code"] == "unauthorized"
    assert h.seen == []


def test_v1_does_not_ask_for_access_and_the_console_still_does():
    from sizeup.cloud.access import Identity
    from tests.test_api_v1 import serve

    class Access:
        def identify(self, headers):
            return Identity(email="member@example.org") if headers.get("X-Test-User") else None
    srv, url = serve(access=Access(), role="hub", mobile=Box(reads=True).switches())
    try:
        assert http(url, "/api/v1/meta")[0] == 200                      # no Access person: public meta is served
        assert http(url, "/api/v1/meta", headers={"X-Test-User": "x"})[0] == 200
        assert http(url, "/api/v1/nope")[0] == 404                       # the envelope's, not Access's 401
        assert http(url, "/api/cad/whoami")[0] == 401                   # the console still wants its person
    finally:
        srv.shutdown()


@pytest.mark.parametrize("path", ["/api/cad/whoami", "/api/cad/state", "/api/cad/nope"])
def test_a_device_bearer_is_refused_on_the_console(path, tmp_path):
    from sizeup.cloud.access import Identity
    from tests.test_api_v1 import serve

    class Access:
        def identify(self, headers):
            return Identity(email="member@example.org") if headers.get("X-Test-User") else None
    srv, url = serve(access=Access(), role="hub", cad=None)
    try:
        for hdrs in ({"Authorization": "Bearer dev.abc.def"}, {"Authorization": "bearer x", "X-Test-User": "m@e.org"}):
            status, _, body = http(url, path, headers=hdrs)
            assert status == 401 and b"device token" in body
        status, _, _ = http(url, path, headers={"X-Test-User": "m@e.org"})
        assert status != 401 or path == "/api/cad/nope"                   # the console's own answer without a bearer
        assert http(url, path, "POST", {"Authorization": "Bearer x", "Content-Type": "application/json"}, b"{}")[0] == 401
    finally:
        srv.shutdown()


def test_a_bearer_is_refused_by_the_console_on_a_real_hub_and_the_stations_still_works(hub_with_v1):
    h = hub_with_v1
    assert http(h.url, "/api/cad/whoami")[0] == 200
    assert http(h.url, "/api/cad/whoami", headers={"Authorization": "Bearer dev.abc.def"})[0] == 401
    status, _, body = http(h.url, "/api/station/status", headers={"Authorization": "Bearer not-a-station-token"})
    assert status in (401, 403) and b"device token" not in body        # the station's own door, its own answer
    status, _, body = http(h.url, "/api/v1/things", headers={"Authorization": "Bearer not-a-station-token"})
    assert status == 401 and json.loads(body)["error"]["code"] == "unauthorized"        # a station bearer on v1: no


def test_the_hub_reads_the_switch_from_the_config_on_every_request(tmp_path):
    h = HubRun(hub_cfg(tmp_path))
    try:
        assert http(h.url, "/api/v1/nope")[0] == 404 and h.app.cfg.mobile_enabled is False
        assert json.loads(http(h.url, "/api/v1/meta")[2])["features"] == {f: False for f in FEATURES}
        object.__setattr__(h.app.cfg, "mobile_enabled", True)
        status, _, body = http(h.url, "/api/v1/nope")
        assert status == 404 and json.loads(body)["error"]["code"] == "not_found"
    finally:
        h.close()


# -- config -----------------------------------------------------------------------------------------------------

def conf(tmp_path, text: str):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return load(p)


def test_mobile_is_off_without_a_section_and_the_shipped_configs_say_off():
    assert load(ROOT / "config.example.toml").mobile_enabled is False
    hub = load(ROOT / "deploy" / "hub" / "config.hub.toml")
    assert hub.mobile_enabled is False and (hub.mobile_max_requests, hub.mobile_max_streams) == (8, 16)
    assert "[mobile]" not in (ROOT / "deploy" / "station" / "config.station.toml").read_text()


def test_mobile_section_enables_and_bounds(tmp_path):
    c = conf(tmp_path, '[cloud]\nrole = "hub"\n[mobile]\nenabled = true\nmax_requests = 4\nmax_streams = 0\n')
    assert (c.mobile_enabled, c.mobile_max_requests, c.mobile_max_streams) == (True, 4, 0)
    assert conf(tmp_path, "[mobile]\n").mobile_enabled is False
    assert conf(tmp_path, "").mobile_enabled is False


@pytest.mark.parametrize("body", ["enabled = 1", 'enabled = "true"', "max_requests = 0", "max_requests = 65",
                                  "max_requests = 2.5", "max_requests = true", "max_streams = -1", "max_streams = 65",
                                  "enabeld = true", "token = 'x'"])
def test_mobile_bad_values_and_unknown_keys_are_refused(tmp_path, body):
    with pytest.raises(ConfigError, match="mobile"):
        conf(tmp_path, f"[mobile]\n{body}\n")


def test_mobile_is_never_a_station_key(tmp_path):
    with pytest.raises(ConfigError, match="hub's section"):
        conf(tmp_path, '[cloud]\nrole = "station"\nhub_url = "https://hub.example.org"\ntoken = "' + "t" * 32 +
             '"\n[mobile]\nenabled = true\n')


def test_the_configs_document_the_switch():
    for f in ("config.example.toml", "deploy/hub/config.hub.toml"):
        text = (ROOT / f).read_text()
        assert "[mobile]" in text and "enabled = false" in text, f
