"""Device push (#563): the SNS client against a fake SNS on this machine, registration, the fan-out by kind and role,
quiet hours, caps and kill switches, the scrub, the outbound guard, stale endpoints, and the ntfy path left alone.
No real AWS, Apple or Google call: every ARN, token, account and address here is invented."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sizeup import aws, notify, outbound
from sizeup.fence import FencedWrite
from sizeup.push import content, service as svc, sns as snsmod
from sizeup.push.devices import MAX_DEVICES, PushDevices
from sizeup.push.wire import wire

CREDS = aws.Credentials("AKIDEXAMPLE", "secret-example")
ACCT = "123456789012"
SANDBOX_APP = f"arn:aws:sns:us-east-1:{ACCT}:app/APNS_SANDBOX/example-ios-dev"
PROD_APP = f"arn:aws:sns:us-east-1:{ACCT}:app/APNS/example-ios"
FCM_APP = f"arn:aws:sns:us-east-1:{ACCT}:app/GCM/example-android"
APPS = snsmod.Apps(SANDBOX_APP, PROD_APP, FCM_APP)
IOS_TOKEN = "0123456789abcdef" * 4
FCM_TOKEN = "fXyz1234567:APA91bExampleTokenExampleToken_-0123456789"
INTEL_LABELS = frozenset({"PD", "Smoke / fire reported", "Smoke reported", "Port Chester"})   # packs' labels and towns
UNITS = {"E64": "Engine 64", "L31": "Ladder 31"}         # the CAD's units list, as the lookup answers
NOON = 1767268800.0              # 2026-01-01 12:00 UTC = 07:00 America/New_York
NIGHT = NOON + 15 * 3600         # 03:00 America/New_York


class FakeSns:
    """SNS's Query API as far as the client goes: records every call; endpoints per (app, token); a Publish to a
    disabled endpoint answers EndpointDisabled."""

    def __init__(self):
        self.calls: list[dict] = []
        self.headers: list[dict] = []
        self.endpoints: dict[str, dict] = {}
        self.disabled: set[str] = set()
        self.fail: dict[str, tuple[int, str]] = {}
        self.publish_errors: list[tuple[int, str]] = []        # consumed one per Publish, before it works again
        self.publish_delay = 0.0
        self.strict = False                                     # SNS's "same Token, but different attributes"
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
                fake.calls.append(form)
                fake.headers.append(dict(self.headers))
                status, xml = fake.answer(form)
                body = xml.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _err(code, status=400, msg="refused"):
        return status, (f'<ErrorResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/"><Error><Type>Sender</Type>'
                        f"<Code>{code}</Code><Message>{msg}</Message></Error></ErrorResponse>")

    def answer(self, f):
        act = f["Action"]
        if act in self.fail:
            st, code = self.fail[act]
            return self._err(code, st)
        ns = 'xmlns="http://sns.amazonaws.com/doc/2010-03-31/"'
        if act == "CreatePlatformEndpoint":
            app, tok = f["PlatformApplicationArn"], f["Token"]
            plat = app.split("/")[1]
            for arn, e in self.endpoints.items():
                if e["app"] == app and e["token"] == tok:
                    if self.strict and e.get("user_data") != f.get("CustomUserData"):
                        return self._err("InvalidParameter", 400, f"Invalid parameter: Token Reason: Endpoint {arn} "
                                         "already exists with the same Token, but different attributes.")
                    break
            else:
                arn = f"arn:aws:sns:us-east-1:{ACCT}:endpoint/{plat}/{app.split('/')[2]}/{uuid.uuid4()}"
                self.endpoints[arn] = {"app": app, "token": tok, "enabled": True, "user_data": f.get("CustomUserData")}
            return 200, (f"<CreatePlatformEndpointResponse {ns}><CreatePlatformEndpointResult>"
                         f"<EndpointArn>{arn}</EndpointArn></CreatePlatformEndpointResult>"
                         f"</CreatePlatformEndpointResponse>")
        if act == "SetEndpointAttributes":
            attrs = {f[k]: f[k.replace(".key", ".value")] for k in f if k.endswith(".key")}
            self.endpoints[f["EndpointArn"]]["enabled"] = attrs.get("Enabled") != "false"
            if "CustomUserData" in attrs:
                self.endpoints[f["EndpointArn"]]["user_data"] = attrs["CustomUserData"]
            return 200, f"<SetEndpointAttributesResponse {ns}/>"
        if act == "DeleteEndpoint":
            self.endpoints.pop(f["EndpointArn"], None)
            return 200, f"<DeleteEndpointResponse {ns}/>"
        if act == "Publish":
            if self.publish_delay:
                time.sleep(self.publish_delay)
            if self.publish_errors:
                st, code = self.publish_errors.pop(0)
                return self._err(code, st)
            arn = f["TargetArn"]
            if arn in self.disabled:
                return self._err("EndpointDisabled", 400, "Endpoint is disabled")
            return 200, (f"<PublishResponse {ns}><PublishResult><MessageId>m-{len(self.calls)}</MessageId>"
                         f"</PublishResult></PublishResponse>")
        return self._err("InvalidAction")

    def published(self) -> list[dict]:
        """Each Publish: {arn, message (platform key -> decoded payload), attrs}."""
        out = []
        for f in self.calls:
            if f["Action"] != "Publish":
                continue
            msg = json.loads(f["Message"])
            attrs = {f[k]: f[k.replace("Name", "Value.StringValue")] for k in f if k.endswith(".Name")}
            out.append({"arn": f["TargetArn"], "structure": f["MessageStructure"], "default": msg["default"],
                        "message": {k: json.loads(v) for k, v in msg.items() if k != "default"}, "attrs": attrs})
        return out

    def actions(self) -> list[str]:
        return [f["Action"] for f in self.calls]


@pytest.fixture
def fake():
    f = FakeSns()
    yield f
    f.close()


class Rig:
    """A PushService on a temp hub.sqlite and the fake SNS, with four people and the policy as a test turns it."""
    PEOPLE = {"admin@example.org": "admin", "disp@example.org": "dispatcher", "view@example.org": "viewer"}

    def __init__(self, tmp_path, fake, apps=APPS, now=NOON, policy=None):
        # most tests are about something else than who may get intel: the policy lets every role have it (test_push_intel.py)
        self.now, self.policy, self.logs = [now], policy if policy is not None else {"intel": {"min_role": "viewer"}}, []
        self.fake = fake
        self.db = tmp_path / "hub.sqlite"
        self.service = svc.PushService(PushDevices(self.db, clock=lambda: self.now[0]), apps,
                                       snsmod.Sns("us-east-1", fake.url, lambda: CREDS), settings=lambda: self.policy,
                                       role_of=self.PEOPLE.get, clock=lambda: self.now[0], log=self.logs.append,
                                       device_active=lambda device_id: True, unit_of=UNITS.get,
                                       intel_labels=lambda: INTEL_LABELS)

    def device(self, n: int, email: str, platform="ios", env="sandbox", prefs=None, quiet=None):
        token = IOS_TOKEN[:-2] + f"{n:02d}" if platform == "ios" else FCM_TOKEN + str(n)
        prefs = {"intel": True, **(prefs or {})}                # the device opted in (the default is off)
        return self.service.register(f"dev-{n:04d}", email, platform, env if platform == "ios" else "", token, prefs,
                                     quiet)

    def targets(self, kind="alerts", title="MAYDAY", body="100 Example Ave", **kw) -> set[str]:
        before = len(self.fake.published())
        arn_to_dev = {r["endpoint"]: r["device_id"] for r in self.service.devices.rows()}
        self.service.deliver(kind, title, body, **kw)
        return {arn_to_dev[p["arn"]] for p in self.fake.published()[before:] if p["arn"] not in self.fake.disabled}


@pytest.fixture
def rig(tmp_path, fake):
    return Rig(tmp_path, fake)


# -- the SNS client ------------------------------------------------------------------------------------------------

def test_the_client_signs_every_call_and_never_sends_the_token_in_a_header(fake):
    c = snsmod.Sns("us-east-1", fake.url, lambda: CREDS)
    arn = c.create_endpoint(SANDBOX_APP, IOS_TOKEN, "dev-0001")
    c.set_attributes(arn, {"Token": IOS_TOKEN, "Enabled": "true"})
    c.publish(arn, {"default": "t", "APNS_SANDBOX": "{}"}, {"AWS.SNS.MOBILE.APNS.PRIORITY": "10"})
    c.delete_endpoint(arn)
    assert fake.actions() == ["CreatePlatformEndpoint", "SetEndpointAttributes", "Publish", "DeleteEndpoint"]
    assert all(h["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
               and "/sns/aws4_request" in h["Authorization"]
               for h in fake.headers)
    assert all(IOS_TOKEN not in json.dumps(h) for h in fake.headers)
    assert fake.calls[0]["Version"] == "2010-03-31" and fake.calls[0]["CustomUserData"] == "dev-0001"


def test_an_sns_refusal_is_an_error_with_its_code(fake):
    fake.fail["Publish"] = (400, "EndpointDisabled")
    with pytest.raises(snsmod.SnsError) as e:
        snsmod.Sns("us-east-1", fake.url, lambda: CREDS).publish("arn:x", {"default": "t"})
    assert e.value.code == "EndpointDisabled" and e.value.status == 400


def test_the_platform_applications_come_from_optional_variables_and_fail_closed():
    assert not snsmod.Apps.from_env({}).configured()
    env = {"SIZEUP_SNS_APNS_SANDBOX_ARN": PROD_APP,          # a production ARN in the sandbox slot: left off
           "SIZEUP_SNS_APNS_ARN": PROD_APP, "SIZEUP_SNS_FCM_ARN": "not an arn", "SIZEUP_PUSH_CRITICAL": "1",
           "SIZEUP_SNS_ENDPOINT": "https://sns.us-east-1.amazonaws.com"}      # a remote override is refused
    a = snsmod.Apps.from_env(env)
    assert (a.apns_sandbox, a.apns, a.fcm, a.critical, a.endpoint) == ("", PROD_APP, "", True, "")
    assert a.app("ios", "sandbox") == "" and a.app("ios", "production") == PROD_APP and a.app("android") == ""
    assert snsmod.Apps.from_env({"SIZEUP_SNS_ENDPOINT": "http://127.0.0.1:9"}).endpoint == "http://127.0.0.1:9"
    sandbox = f"arn:aws:sns:us-east-1:{ACCT}:endpoint/APNS_SANDBOX/example-ios-dev/abc"
    assert snsmod.endpoint_platform(sandbox) == "APNS_SANDBOX"
    assert snsmod.endpoint_platform(SANDBOX_APP) == ""


# -- registration --------------------------------------------------------------------------------------------------

def test_sandbox_and_production_go_to_their_own_applications_and_back_never_across(rig, fake):
    rig.device(1, "view@example.org", env="sandbox")
    rig.device(2, "view@example.org", env="production")
    rig.device(3, "view@example.org", platform="android")
    made = [f["PlatformApplicationArn"] for f in fake.calls if f["Action"] == "CreatePlatformEndpoint"]
    assert made == [SANDBOX_APP, PROD_APP, FCM_APP]
    rows = {r["device_id"]: r for r in rig.service.devices.rows()}
    assert [(r["platform"], r["env"], r["app"]) for r in rows.values()] == [
        ("ios", "sandbox", SANDBOX_APP), ("ios", "production", PROD_APP), ("android", "", FCM_APP)]
    rig.service.deliver("alerts", "MAYDAY", "100 Example Ave")
    keys = {p["arn"].split("/")[1]: list(p["message"]) for p in fake.published()}
    assert keys == {"APNS_SANDBOX": ["APNS_SANDBOX"], "APNS": ["APNS"], "GCM": ["GCM"]}


def test_a_platform_without_its_application_refuses_and_stores_nothing(tmp_path, fake):
    r = Rig(tmp_path, fake, apps=snsmod.Apps(apns=PROD_APP))
    with pytest.raises(svc.PushError) as e:
        r.device(1, "view@example.org", env="sandbox")          # no sandbox application: never the production one
    assert (e.value.status, e.value.code) == (503, "push_unavailable")
    with pytest.raises(svc.PushError):
        r.device(2, "view@example.org", platform="android")
    assert fake.calls == [] and not r.db.exists()


def test_replacing_a_registration_moves_it_and_removes_the_old_endpoint(rig, fake):
    first = rig.device(1, "view@example.org", env="sandbox")
    again = rig.device(1, "view@example.org", env="sandbox")            # same token: SNS answers the same endpoint
    assert again["endpoint"] == first["endpoint"] and again["created"] == first["created"]
    moved = rig.service.register("dev-0001", "view@example.org", "ios", "production", IOS_TOKEN[:-2] + "01", None, None)
    assert moved["endpoint"] != first["endpoint"] and moved["app"] == PROD_APP and moved["env"] == "production"
    assert first["endpoint"] not in fake.endpoints and moved["endpoint"] in fake.endpoints
    assert rig.service.devices.count() == 1


def test_an_sns_refusal_at_registration_stores_nothing(rig, fake):
    fake.fail["CreatePlatformEndpoint"] = (400, "InvalidParameter")
    with pytest.raises(svc.PushError) as e:
        rig.device(1, "view@example.org")
    assert (e.value.status, e.value.code) == (502, "push_failed") and rig.service.devices.count() == 0


def test_unregister_and_drop_device_remove_the_endpoint_and_the_row(rig, fake):
    row = rig.device(1, "view@example.org")
    svc.install(rig.service)
    try:
        assert svc.drop_device("dev-0001") is True and svc.drop_device("dev-0001") is False
    finally:
        svc.install(None)
    assert row["endpoint"] not in fake.endpoints and rig.service.devices.get("dev-0001") is None
    assert svc.drop_device("dev-0001") is False                          # nothing installed: nothing, no error


def test_a_revoke_survives_sns_being_down_and_the_row_still_goes(rig, fake):
    rig.device(1, "view@example.org")
    fake.fail["DeleteEndpoint"] = (500, "InternalError")
    assert rig.service.unregister("dev-0001") is True and rig.service.devices.get("dev-0001") is None


def test_a_fenced_task_registers_nothing_and_makes_no_endpoint(tmp_path, fake):
    r = Rig(tmp_path, fake)
    r.service.devices._may_write = lambda: False
    with pytest.raises(FencedWrite):
        r.device(1, "view@example.org")
    assert fake.calls == []


def test_at_most_forty_devices(rig, fake):
    for n in range(MAX_DEVICES):
        rig.device(n, "view@example.org")
    with pytest.raises(svc.PushError) as e:
        rig.device(MAX_DEVICES, "view@example.org")
    assert (e.value.status, e.value.code) == (409, "push_full")
    rig.device(0, "view@example.org")                                    # a replacement is not a new device
    assert len(rig.service.devices.rows()) == MAX_DEVICES


def test_nothing_is_created_by_a_start_a_read_or_a_drop(tmp_path, fake):
    r = Rig(tmp_path, fake)
    assert r.service.devices.rows() == [] and r.service.devices.count() == 0 and r.service.devices.get("x") is None
    assert r.service.drop("x") is False and r.service.deliver("alerts", "t", "b") == 0
    assert not r.db.exists()
    r.device(1, "view@example.org")                                      # the first registration makes the table
    assert r.db.exists() and r.service.devices.count() == 1


def test_wire_reads_and_writes_nothing(tmp_path):
    class Store:
        def one(self, *a):
            raise AssertionError("no read at start")

    class Switches:
        def allows(self, flag):
            return False
    pushes = notify.Pushes("http://127.0.0.1:9", "", "", settings=lambda: {})
    s = wire(pushes, tmp_path, Switches(), Store(), env={})
    try:
        assert pushes.devices is s and list(tmp_path.iterdir()) == []
    finally:
        svc.install(None)


# -- who gets which kind -------------------------------------------------------------------------------------------

def fleet(rig):
    rig.device(1, "admin@example.org")
    rig.device(2, "disp@example.org", env="production")
    rig.device(3, "view@example.org", platform="android")
    rig.device(4, "gone@example.org")                                    # not an operator (deactivated or unknown)


def test_alerts_and_intel_reach_everyone_signed_in_status_only_admins(rig):
    fleet(rig)
    assert rig.targets("alerts") == {"dev-0001", "dev-0002", "dev-0003"}
    three = {"dev-0001", "dev-0002", "dev-0003"}
    assert rig.targets("intel", "PD · Smoke reported · Port Chester", "100 Example Ave · 14:32 · high") == three
    assert rig.targets("status", "Station silent 4 min") == {"dev-0001"}


def test_dispatch_is_off_by_default_and_opt_in_per_device(rig):
    fleet(rig)
    rig.device(5, "disp@example.org", prefs={"dispatch": True})
    assert rig.targets("dispatch", "Structure fire", "100 Example Ave") == set()      # the kill switch is the default
    rig.policy = {"dispatch": {"on": True}}
    assert rig.targets("dispatch", "Structure fire", "100 Example Ave") == {"dev-0005"}     # opted in only


def test_a_device_that_turned_a_kind_off_does_not_get_it(rig):
    rig.device(1, "admin@example.org", prefs={"alerts": False, "intel": True, "status": False})
    assert rig.targets("alerts") == set() and rig.targets("status", "Station silent") == set()
    assert rig.targets("intel", "PD · Smoke reported", "100 Example Ave") == {"dev-0001"}


def test_quiet_hours_hold_intel_and_status_never_alerts_or_dispatch(rig):
    q = {"from": "22:00", "to": "06:00", "tz": "America/New_York"}
    rig.device(1, "admin@example.org", quiet=q, prefs={"dispatch": True})
    rig.policy = {"dispatch": {"on": True}}
    rig.now[0] = NIGHT
    assert rig.targets("intel", "PD · Smoke reported", "100 Example Ave") == set()
    assert rig.targets("status", "Station silent") == set()
    assert rig.targets("alerts") == {"dev-0001"}
    assert rig.targets("dispatch", "Structure fire", "100 Example Ave") == {"dev-0001"}
    rig.now[0] = NOON                                                   # 07:00 there: quiet hours are over
    assert rig.targets("intel", "PD · Smoke reported", "100 Example Ave") == {"dev-0001"}


def test_quiet_hours_across_midnight_and_inside_a_day():
    q = {"from": "22:00", "to": "06:00", "tz": "America/New_York"}
    assert svc.in_quiet(q, NIGHT) and not svc.in_quiet(q, NOON) and not svc.in_quiet(None, NIGHT)
    assert svc.in_quiet({"from": "01:00", "to": "05:00", "tz": "UTC"}, NOON - 11 * 3600)      # 01:00 UTC
    assert not svc.in_quiet({"from": "01:00", "to": "05:00", "tz": "UTC"}, NOON)
    assert svc.in_quiet({"from": "11:00", "to": "13:00", "tz": "UTC"}, NOON)
    assert not svc.in_quiet({"from": "11:00", "to": "11:00", "tz": "UTC"}, NOON)
    assert not svc.in_quiet({"from": "11:00", "to": "13:00", "tz": "Not/AZone"}, NOON)      # broken: never held


# -- caps and the kill switch ---------------------------------------------------------------------------------------

def test_a_kind_is_capped_and_a_loop_cannot_page_forty_phones_a_second(rig, fake):
    rig.device(1, "admin@example.org")
    rig.policy = {"intel": {"per_minute": 2}}
    for i in range(30):
        rig.now[0] += 0.1
        rig.service.deliver("intel", "PD · Smoke reported", f"{i} Example Ave")
    assert len(fake.published()) == 2 and rig.service.stats["capped"] == 28
    rig.now[0] += 61                                                    # the minute passed: it goes again
    rig.service.deliver("intel", "PD · Smoke reported", "9 Example Ave")
    assert len(fake.published()) == 3
    assert len([m for m in rig.logs if "over its cap" in m]) == 1       # said once, not per message


def test_the_default_caps_hold_every_kind_per_minute_per_hour_and_per_day(rig, fake):
    rig.device(1, "admin@example.org")
    for _ in range(40):
        rig.service.deliver("alerts", "MAYDAY", "100 Example Ave")
    assert len(fake.published()) == svc.DEFAULT_POLICY["alerts"]["per_minute"]


def test_the_kill_switch_per_kind_and_for_all(rig, fake):
    rig.device(1, "admin@example.org")
    rig.policy = {"intel": {"on": False}}
    assert rig.targets("intel", "PD · Smoke reported", "100 Example Ave") == set()
    assert rig.targets("alerts") == {"dev-0001"}                        # the other kinds go on
    rig.policy = {"off": True}
    assert rig.targets("alerts") == set() and rig.targets("status", "Station silent") == set()
    assert rig.service.stats["killed"] == 3


def test_a_broken_policy_never_turns_dispatch_on():
    assert svc.policy({"dispatch": {"on": "yes", "per_minute": -5}})["dispatch"] == svc.DEFAULT_POLICY["dispatch"]
    assert svc.policy("garbage")["dispatch"]["on"] is False
    assert svc.policy({"alerts": {"per_hour": 5}})["alerts"]["per_hour"] == 5


# -- the payload -----------------------------------------------------------------------------------------------------

def test_the_apns_payload_is_kind_id_title_body_and_time_sensitive_by_default(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.deliver("alerts", "MAYDAY", "100 Example Ave", priority=5,
                        who={"units": [("E64 P1", "Engine 64")], "channel": "FG1", "incident": "26-000015",
                             "address": "100 Example Ave"})
    p = fake.published()[0]
    pay = p["message"]["APNS_SANDBOX"]
    assert p["structure"] == "json" and set(pay) == {"aps", "kind", "id"}
    assert (pay["kind"], pay["id"]) == ("alerts", "26-000015")
    assert pay["aps"]["alert"] == {"title": "MAYDAY · E64 (Engine 64) on FG1 · 26-000015 100 Example Ave",
                                   "body": "100 Example Ave"}
    assert pay["aps"]["interruption-level"] == "time-sensitive" and pay["aps"]["sound"] == "default"   # no entitlement
    assert p["attrs"] == {"AWS.SNS.MOBILE.APNS.PUSH_TYPE": "alert", "AWS.SNS.MOBILE.APNS.PRIORITY": "10",
                          "AWS.SNS.MOBILE.APNS.TTL": "900"}


def test_critical_needs_the_entitlement_and_an_emergency_or_a_dispatch(tmp_path, fake):
    r = Rig(tmp_path, fake, apps=snsmod.Apps(SANDBOX_APP, PROD_APP, FCM_APP, critical=True),
            policy={"dispatch": {"on": True}})
    r.device(1, "admin@example.org", prefs={"dispatch": True})

    def last(kind, prio, title="MAYDAY"):
        r.now[0] += 100
        r.service.deliver(kind, title, "100 Example Ave", priority=prio)
        return fake.published()[-1]["message"]["APNS_SANDBOX"]["aps"]
    crit = last("alerts", 5)
    assert crit["interruption-level"] == "critical"
    assert crit["sound"] == {"critical": 1, "name": "default", "volume": 1.0}
    assert last("alerts", 4)["interruption-level"] == "time-sensitive"
    assert last("dispatch", 3, "Structure fire")["interruption-level"] == "critical"
    assert last("intel", 5, "PD · Smoke reported")["interruption-level"] == "time-sensitive"
    quiet = last("status", 2, "Station silent")
    assert quiet["interruption-level"] == "passive" and "sound" not in quiet
    assert fake.published()[-1]["attrs"]["AWS.SNS.MOBILE.APNS.PRIORITY"] == "5"      # passive: apns-priority 5


def test_android_gets_a_high_priority_data_message_with_the_same_four_fields(rig, fake):
    rig.device(1, "view@example.org", platform="android")
    rig.service.deliver("alerts", "MAYDAY", "heard: anything", priority=5, who={"address": "100 Example Ave"})
    p = fake.published()[0]
    msg = p["message"]["GCM"]["fcmV1Message"]["message"]
    assert msg["data"] == {"kind": "alerts", "id": msg["data"]["id"], "title": "MAYDAY \u00b7 100 Example Ave",
                           "body": "100 Example Ave"}
    assert msg["android"]["priority"] == "high" and "notification" not in msg and p["attrs"] == {}


def test_a_title_that_fails_the_scrub_becomes_the_generic_push(rig, fake):
    rig.device(1, "admin@example.org")
    cases = [("Unit down radio 4412090", "x"),                    # a radio ID
             ("Vehicle ABC1234", "x"),                            # a plate
             ("Caller born 03/04/1980", "x"),                     # a date of birth
             ("Call 914-555-0123", "x")]                          # a phone number
    for t, b in cases:
        rig.now[0] += 100
        rig.service.deliver("alerts", t, b)
        a = fake.published()[-1]["message"]["APNS_SANDBOX"]["aps"]["alert"]
        assert a == {"title": "sizeup alert", "body": "Open sizeup for details"}, t
    assert not any(x in json.dumps(p) for p in fake.published() for x in ("4412090", "ABC1234", "555-0123"))


def test_the_talker_the_dongle_and_every_link_stay_out_and_the_event_number_stays_in(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.deliver("intel", "PD · Smoke reported · Port Chester",
                        "100 Example Ave · 14:32 · high\nListen: https://bucket.example.org/listen/abc.m4a?sig=zz",
                        click="https://hub.example.org/radio?view=all&intel=77",
                        who={"talker": "Jordan Example", "dongle": "SDR 00000102", "incident": "26-000015"})
    pay = fake.published()[0]["message"]["APNS_SANDBOX"]
    assert pay["id"] == "26-000015" and pay["aps"]["alert"]["body"] == "100 Example Ave · 14:32 · high"
    text = json.dumps(pay)
    assert "Jordan" not in text and "00000102" not in text and "http" not in text and "Listen" not in text


def test_the_id_is_the_event_the_intel_link_names_else_a_random_reference():
    assert content.message_id("intel", "https://h.example.org/radio?view=all&intel=77") == "intel:77"
    first, second = content.message_id("status"), content.message_id("status")
    assert first.startswith("status:") and first != second
    assert content.message_id("alerts", ref="inc-9") == "inc-9"


# -- failure -------------------------------------------------------------------------------------------------------

def test_a_disabled_endpoint_is_removed_with_its_row(rig, fake):
    a, b = rig.device(1, "admin@example.org"), rig.device(2, "disp@example.org")
    fake.disabled.add(a["endpoint"])
    # the disabled one was tried, refused, and removed
    assert rig.targets("alerts") == {"dev-0002"}
    assert rig.service.devices.get("dev-0001") is None and rig.service.devices.get("dev-0002") is not None
    assert a["endpoint"] not in fake.endpoints and rig.service.stats["removed"] == 1
    assert rig.targets("alerts", "Second", "100 Example Ave") == {"dev-0002"}


def test_another_sns_failure_keeps_the_device_and_goes_on_to_the_next(rig, fake):
    rig.device(1, "admin@example.org")
    rig.device(2, "disp@example.org")
    fake.fail["Publish"] = (500, "InternalError")
    assert rig.service.deliver("alerts", "MAYDAY", "100 Example Ave") == 0
    assert rig.service.devices.count() == 2 and rig.service.stats["failed"] == 2


def test_an_endpoint_of_the_other_platform_is_never_published_to(rig, fake):
    row = rig.device(1, "admin@example.org", env="sandbox")
    rig.service.devices.put("dev-0001", row["email"], "ios", "production", row["endpoint"], PROD_APP, row["prefs"],
                            None)
    assert rig.service.deliver("alerts", "MAYDAY", "100 Example Ave") == 0 and fake.published() == []


# -- the outbound guard ---------------------------------------------------------------------------------------------

def test_a_hand_started_hub_never_pushes_to_real_phones(tmp_path, monkeypatch):
    """Armed off (a hub without the supervisor's lease): the first Publish is refused in aws.sign, before any byte
    leaves, so a restored copy with real registrations and credentials cannot reach a phone."""
    def no_network(*a, **kw):
        raise AssertionError("a request left the process")
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    outbound.arm("hub", env={})
    r = Rig.__new__(Rig)
    r.now, r.policy, r.logs = [NOON], {}, []
    r.service = svc.PushService(PushDevices(tmp_path / "hub.sqlite", clock=lambda: NOON), APPS,
                                # the real SNS address, no override
                                snsmod.Sns("us-east-1", creds=lambda: CREDS),
                                role_of=Rig.PEOPLE.get, clock=lambda: NOON, log=r.logs.append,
                                device_active=lambda d: True)
    r.service.devices.put("dev-0001", "admin@example.org", "ios", "production",
                          f"arn:aws:sns:us-east-1:{ACCT}:endpoint/APNS/example-ios/abc", PROD_APP,
                          dict(svc.DEFAULT_PREFS), None)
    r.service.devices.put("dev-0002", "disp@example.org", "ios", "production",
                          f"arn:aws:sns:us-east-1:{ACCT}:endpoint/APNS/example-ios/def", PROD_APP,
                          dict(svc.DEFAULT_PREFS), None)
    assert r.service.deliver("alerts", "MAYDAY", "100 Example Ave") == 0
    assert r.service.stats["sent"] == 0 and any("outbound" in m.lower() for m in r.logs)
    with pytest.raises(svc.PushError) as e:                            # registration is refused the same way
        r.service.register("dev-0003", "view@example.org", "ios", "production", IOS_TOKEN, None, None)
    assert (e.value.status, e.value.code) == (503, "push_unavailable")
    assert r.service.devices.get("dev-0003") is None


# -- the ntfy path ---------------------------------------------------------------------------------------------------

class Ntfy:
    def __init__(self):
        self.sent = []

    def __call__(self, server, topic, title, body, priority=3, tags=(), click="", timeout=10.0, who=None, actions=(),
                 protected=()):
        self.sent.append((topic, notify.compose(title, who), body, priority, tuple(tags), click))
        return {"id": "m1"}


def pushes(monkeypatch):
    ntfy = Ntfy()
    monkeypatch.setattr(notify, "publish", ntfy)
    return notify.Pushes("http://127.0.0.1:9", "example-alerts-topic-0001", "example-status-topic-0002",
                         settings=lambda: {}, log=lambda m: None, retries=()), ntfy


def test_the_ntfy_message_is_the_same_with_and_without_devices(monkeypatch, rig):
    p, ntfy = pushes(monkeypatch)
    n = p.notifier(notify.ALERTS)
    assert n.send("MAYDAY", "100 Example Ave", 5, ["rotating_light"], "https://h.example.org/x",
                  who={"talker": "Jordan Example", "incident": "26-000015"}) is True
    n.flush()
    plain = list(ntfy.sent)
    ntfy.sent.clear()
    rig.device(1, "admin@example.org")
    p.devices = rig.service
    p.devices.active = lambda: True
    assert n.send("MAYDAY", "100 Example Ave", 5, ["rotating_light"], "https://h.example.org/x",
                  who={"talker": "Jordan Example", "incident": "26-000015"}) is True
    n.flush()
    rig.service.flush()
    assert ntfy.sent == plain and "Jordan Example" in plain[0][1]       # ntfy keeps its own text, talker included
    pay = rig.fake.published()[0]["message"]["APNS_SANDBOX"]
    assert "Jordan" not in json.dumps(pay)                              # the phone's never has it


def test_a_device_push_never_touches_an_ntfy_topic_and_the_dispatch_kind_has_none(monkeypatch, rig):
    p, ntfy = pushes(monkeypatch)
    rig.device(1, "admin@example.org", prefs={"dispatch": True})
    rig.policy = {"dispatch": {"on": True}}
    p.devices = rig.service
    p.fan_out("dispatch", "Structure fire", "100 Example Ave")
    rig.service.flush()
    assert len(rig.fake.published()) == 1 and ntfy.sent == []
    # no ntfy topic, Settings page or fallback for it
    assert notify.KINDS == ("alerts", "status", "intel")


def test_a_broken_device_path_never_changes_what_ntfy_does(monkeypatch):
    p, ntfy = pushes(monkeypatch)

    class Boom:
        def offer(self, *a, **kw):
            raise RuntimeError("boom")
    p.devices = Boom()
    assert p.notifier(notify.ALERTS).send("MAYDAY", "x") is True        # a device path that raises: ntfy still sends
    p.notifier(notify.ALERTS).flush()
    assert len(ntfy.sent) == 1
    ntfy.sent.clear()
    p.devices = svc.PushService(PushDevices(Path("/nonexistent/x/hub.sqlite")), APPS, role_of=lambda e: "admin",
                            device_active=lambda d: True)
    assert p.notifier(notify.ALERTS).send("MAYDAY", "x") is True        # nothing registered, nothing wrong
    p.notifier(notify.ALERTS).flush()
    assert len(ntfy.sent) == 1


def test_offer_never_raises_never_blocks_and_is_silent_while_off(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.active = lambda: False                                  # the master switch or the push flag is off
    rig.service.offer("alerts", "MAYDAY", "100 Example Ave")
    rig.service.offer("nonsense", "t", "b")
    assert rig.service.flush() and fake.published() == []
    rig.service.active = lambda: True
    rig.service.offer("alerts", "MAYDAY", "100 Example Ave")
    assert rig.service.flush() and len(fake.published()) == 1
