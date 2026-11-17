"""PUT / DELETE /api/v1/devices/{id}/push (#563) over HTTP: a signed-in device registers only itself, the flag and the
master switch gate it, the body is checked, push not set up is a 503, and the token and the SNS endpoint are never
answered back. The SNS is a fake on this machine; every value is invented."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from sizeup.api.v1.principal import Principal
from sizeup.api.v1.routes import ROUTES
from sizeup.api.v1.switches import Switches
from sizeup.push import service as svc, sns as snsmod
from sizeup.push.devices import PushDevices
from tests import openapi_spec as oas
from tests.test_api_v1 import DOC, conforms, serve
from tests.test_push_sns import APPS, CREDS, FCM_TOKEN, IOS_TOKEN, FakeSns

ME = Principal("dev-0001", "member@example.org")
BEARER = {"Authorization": "Bearer example-device-token"}
PATH = "/api/v1/devices/dev-0001/push"
GOOD = {"platform": "ios", "environment": "sandbox", "token": IOS_TOKEN}


@pytest.fixture
def rig(tmp_path):
    fake = FakeSns()
    flags = {"push": True}
    service = svc.PushService(PushDevices(tmp_path / "hub.sqlite"), APPS, snsmod.Sns("us-east-1", fake.url, CREDS),
                              role_of=lambda e: "viewer", log=lambda m: None)
    svc.install(service)
    switches = Switches(lambda: True, lambda: flags)
    srv, url = serve(role="hub", mobile=switches, mobile_resolver=lambda h: ME if h.get("Authorization") == BEARER["Authorization"] else None)
    yield type("Rig", (), {"url": url, "fake": fake, "service": service, "flags": flags, "db": tmp_path / "hub.sqlite"})
    srv.shutdown()
    svc.install(None)
    fake.close()


def call(rig, method="PUT", path=PATH, body=None, headers=BEARER, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(rig.url + path, method=method, data=data, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def code(r):
    return json.loads(r[1])["error"]["code"]


def test_the_routes_need_a_device_the_push_flag_and_the_read_role():
    mine = [r for r in ROUTES if r.path == "/api/v1/devices/{id}/push"]
    assert {r.method for r in mine} == {"PUT", "DELETE"}
    assert all((r.need, r.flag, r.role) == ("device", "push", "read") for r in mine)
    assert oas.operations(DOC) >= {("PUT", "/api/v1/devices/{id}/push"), ("DELETE", "/api/v1/devices/{id}/push")}


def test_register_answers_the_device_and_never_the_token_or_the_endpoint(rig):
    status, body = call(rig, body={**GOOD, "prefs": {"status": False}, "quiet": {"from": "22:00", "to": "06:00", "tz": "America/New_York"}})
    assert status == 200
    conforms(status, body, "PUT", "/api/v1/devices/{id}/push")
    d = json.loads(body)
    assert (d["device"], d["platform"], d["environment"]) == ("dev-0001", "ios", "sandbox")
    assert d["prefs"] == {"alerts": True, "intel": True, "status": False, "dispatch": False}
    assert IOS_TOKEN not in body.decode() and "arn:" not in body.decode()
    row = rig.service.devices.get("dev-0001")
    assert row["email"] == "member@example.org" and row["app"].endswith("example-ios-dev") and "token" not in row


def test_android_registers_without_an_environment(rig):
    status, body = call(rig, body={"platform": "android", "token": FCM_TOKEN})
    assert status == 200 and json.loads(body)["environment"] is None
    assert call(rig, body={"platform": "android", "token": FCM_TOKEN, "environment": "production"})[0] == 400


def test_a_device_registers_only_itself(rig):
    for method in ("PUT", "DELETE"):
        r = call(rig, method, "/api/v1/devices/dev-0002/push", GOOD if method == "PUT" else None)
        assert (r[0], code(r)) == (403, "forbidden")
    assert rig.fake.calls == [] and not rig.db.exists()


def test_no_token_a_wrong_token_flag_off_or_master_off_is_refused_before_anything_happens(rig):
    assert call(rig, body=GOOD, headers={})[0] == 401
    assert call(rig, body=GOOD, headers={"Authorization": "Bearer wrong"})[0] == 401
    rig.flags["push"] = False
    r = call(rig, body=GOOD)
    assert (r[0], code(r)) == (404, "not_found") and call(rig, "DELETE")[0] == 404
    assert rig.fake.calls == [] and not rig.db.exists()


def test_the_body_is_checked():
    from sizeup.api.v1.push import parse
    bad = [{**GOOD, "environment": None}, {**GOOD, "environment": "staging"}, {k: v for k, v in GOOD.items() if k != "environment"},
           {**GOOD, "platform": "windows"}, {**GOOD, "token": "short"}, {**GOOD, "token": "not a token with spaces " * 2},
           {**GOOD, "extra": 1}, {**GOOD, "prefs": {"alerts": "yes"}}, {**GOOD, "prefs": {"emergency": True}},
           {**GOOD, "quiet": {"from": "25:00", "to": "06:00", "tz": "UTC"}},
           {**GOOD, "quiet": {"from": "22:00", "to": "06:00", "tz": "Not/AZone"}},
           {**GOOD, "quiet": {"from": "22:00", "to": "06:00"}}, ["not", "an", "object"]]
    for b in bad:
        with pytest.raises(ValueError):
            parse(json.dumps(b).encode())
    for raw in (b"", b"{not json", b" " * 5000):
        with pytest.raises(ValueError):
            parse(raw)
    ok = parse(json.dumps({**GOOD, "quiet": None, "prefs": {"dispatch": True}}).encode())
    assert ok == {"platform": "ios", "environment": "sandbox", "token": IOS_TOKEN, "prefs": {"dispatch": True}, "quiet": None}


def test_a_bad_body_over_http_is_a_400_in_the_envelope_and_nothing_is_stored(rig):
    for raw in (b"", b"{not json", json.dumps({**GOOD, "environment": "staging"}).encode(), b" " * 5000):
        r = call(rig, raw=raw)
        assert (r[0], code(r)) == (400, "bad_request")
        conforms(r[0], r[1], "PUT", "/api/v1/devices/{id}/push")
    assert rig.fake.calls == [] and not rig.db.exists()


def test_push_not_set_up_is_a_503_and_stores_nothing(rig):
    rig.service.apps = snsmod.Apps(apns=APPS.apns)                      # no sandbox application
    r = call(rig, body=GOOD)
    assert (r[0], code(r)) == (503, "push_unavailable") and rig.fake.calls == [] and not rig.db.exists()
    svc.install(None)                                                   # not even a service (not a hub)
    assert code(call(rig, body=GOOD)) == "push_unavailable" and code(call(rig, "DELETE")) == "push_unavailable"


def test_the_push_service_refusing_is_a_502_and_nothing_is_stored(rig):
    rig.fake.fail["CreatePlatformEndpoint"] = (400, "InvalidParameter")
    r = call(rig, body=GOOD)
    assert (r[0], code(r)) == (502, "push_failed") and rig.service.devices.get("dev-0001") is None


def test_unregister_removes_the_endpoint_and_is_idempotent(rig):
    call(rig, body=GOOD)
    arn = rig.service.devices.get("dev-0001")["endpoint"]
    status, body = call(rig, "DELETE")
    assert status == 200 and json.loads(body) == {"device": "dev-0001", "registered": False, "removed": True}
    assert arn not in rig.fake.endpoints and rig.service.devices.get("dev-0001") is None
    status, body = call(rig, "DELETE")
    assert status == 200 and json.loads(body)["removed"] is False


def test_replace_is_a_put_again_and_a_get_is_a_405(rig):
    call(rig, body=GOOD)
    status, body = call(rig, body={**GOOD, "environment": "production", "prefs": {"dispatch": True}})
    assert status == 200 and json.loads(body)["environment"] == "production" and json.loads(body)["prefs"]["dispatch"] is True
    assert rig.service.devices.count() == 1
    assert call(rig, "GET")[0] == 405 and call(rig, "POST", body=GOOD)[0] == 405


def test_a_revoke_through_drop_device_removes_it(rig):
    call(rig, body=GOOD)
    assert svc.drop_device("dev-0001") is True
    assert call(rig, "DELETE")[0] == 200 and rig.service.devices.count() == 0
