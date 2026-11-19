"""Device push, round four (#563): the intel audience (a minimum role, opt-in), the urgent lane, one retry and the small
pool, the stale-endpoint race, a token already registered under another device, pruning, the FYI and model titles, the
intel body's place rule, and the per-kind consistency. Invented values."""
from __future__ import annotations

import json
import threading
import time

import pytest

from sizeup import notify
from sizeup.push import content, service as svc, sns as snsmod
from sizeup.cad.api import CadApi
from tests.test_cad import make_cad
from tests.test_push_sns import APPS, FCM_APP, NOON, SANDBOX_APP, Rig, fake, rig  # noqa: F401  (fixtures)
from tests.test_roles import CASEY, JORDAN, VAL, call, people

INTEL = ("PD · Smoke reported · Port Chester", "100 Example St · 14:32 · high")


# -- 1. who may get intel -----------------------------------------------------------------------------------------------

@pytest.fixture
def three(tmp_path, fake):
    """admin / dispatcher / viewer devices that ALL turned intel on, under the DEFAULT policy (no settings)."""
    r = Rig(tmp_path, fake, policy={})
    r.device(1, "admin@example.org")
    r.device(2, "disp@example.org")
    r.device(3, "view@example.org")
    return r


def test_the_default_is_admin_only_even_when_the_pref_is_on(three):
    assert three.targets("intel", *INTEL) == {"dev-0001"}                 # a dispatcher and a viewer never get it


def test_a_device_pref_defaults_to_off_for_intel(tmp_path, fake):
    r = Rig(tmp_path, fake, policy={})
    row = r.service.register("dev-0001", "admin@example.org", "ios", "sandbox", "0123456789abcdef" * 2, None, None)
    assert row["prefs"] == {"alerts": True, "intel": False, "status": True, "dispatch": False}
    assert r.targets("intel", *INTEL) == set()                            # an admin who did not opt in: nothing


def test_the_policy_lowers_the_minimum_role_and_a_device_still_needs_its_pref(three):
    three.policy = {"intel": {"min_role": "dispatcher"}}
    assert three.targets("intel", *INTEL) == {"dev-0001", "dev-0002"}
    three.policy = {"intel": {"min_role": "viewer"}}
    three.service.devices.put("dev-0003", "view@example.org", "ios", "sandbox",
                              three.service.devices.get("dev-0003")["endpoint"], SANDBOX_APP,
                              {**svc.DEFAULT_PREFS}, None)                # the viewer's pref is off
    assert three.targets("intel", *INTEL) == {"dev-0001", "dev-0002"}


def test_status_stays_admin_only_and_alerts_reach_every_role(three):
    three.policy = {"intel": {"min_role": "viewer"}}
    assert three.targets("status", "Station silent 5 min") == {"dev-0001"}
    assert three.targets("alerts", "MAYDAY") == {"dev-0001", "dev-0002", "dev-0003"}


@pytest.mark.parametrize("bad", ["root", "", None, 3, "Admin"])
def test_an_invalid_min_role_is_refused(bad):
    with pytest.raises(ValueError):
        svc.validate_policy({"intel": {"min_role": bad}})
    assert svc.policy({"intel": {"min_role": bad}})["intel"]["min_role"] == "admin"        # the reader stays on the default


def test_min_role_belongs_to_intel_only():
    with pytest.raises(ValueError):
        svc.validate_policy({"alerts": {"min_role": "viewer"}})
    assert svc.validate_policy({"intel": {"min_role": "dispatcher"}}) == {"intel": {"min_role": "dispatcher"}}


def test_an_admin_sets_the_minimum_role_through_settings_and_the_fan_out_reads_it(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    cad.pushes = p = notify.Pushes("http://127.0.0.1:9", "", "", settings=lambda: cad.settings.section(notify.SECTION),
                                   log=lambda m: None)
    post = lambda body, who=CASEY: call(api, "/api/cad/settings/notify", {"key": "devices", **body}, who)   # noqa: E731
    assert post({"action": "set", "policy": {"intel": {"min_role": "dispatcher"}}})[0] == 200
    assert svc.policy(p.settings()["devices"])["intel"]["min_role"] == "dispatcher"
    assert post({"action": "set", "policy": {"intel": {"min_role": "everyone"}}})[0] == 400
    assert post({"action": "set", "policy": {"intel": {"min_role": "viewer"}}}, VAL)[0] == 403
    assert svc.policy(p.settings()["devices"])["intel"]["min_role"] == "dispatcher"


# -- 4. the urgent lane --------------------------------------------------------------------------------------------------

def test_a_mayday_does_not_wait_behind_a_slow_intel_fan_out(tmp_path, fake):
    r = Rig(tmp_path, fake)
    r.device(1, "admin@example.org")
    gate, started = threading.Event(), threading.Event()
    real = r.service._publish_one

    def slow(row, kind, *a):
        if kind == "intel":
            started.set()
            gate.wait(10)                                               # an intel fan-out stuck at SNS
        return real(row, kind, *a)
    r.service._publish_one = slow
    r.service.offer("intel", *INTEL)
    assert started.wait(5)
    r.service.offer("alerts", "MAYDAY", "x", 5)
    end = time.time() + 5
    while time.time() < end and not [p for p in fake.published() if p["message"]["APNS_SANDBOX"]["kind"] == "alerts"]:
        time.sleep(0.02)
    assert [p["message"]["APNS_SANDBOX"]["kind"] for p in fake.published()] == ["alerts"]       # intel is still waiting
    gate.set()
    assert r.service.flush(10)
    assert [p["message"]["APNS_SANDBOX"]["kind"] for p in fake.published()] == ["alerts", "intel"]


def test_dispatch_shares_the_urgent_lane_and_intel_with_status_the_routine_one(tmp_path, fake):
    r = Rig(tmp_path, fake)
    assert {"alerts", "dispatch"} == svc.URGENT
    r.service.offer("intel", *INTEL)
    r.service.offer("alerts", "MAYDAY", "x")
    assert r.service._threads["routine"] is not None and r.service._threads["urgent"] is not None


def test_an_idle_worker_that_exits_never_strands_a_message(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(svc, "IDLE_S", 0.05)
    r = Rig(tmp_path, fake)
    r.device(1, "admin@example.org")
    for i in range(5):
        r.service.offer("alerts", "MAYDAY", f"x{i}")
        time.sleep(0.06)                                                # about when the idle worker gives up
        r.now[0] += 100
    assert r.service.flush(10) and len(fake.published()) == 5


# -- 5. one retry, the small pool ----------------------------------------------------------------------------------------

def test_a_transient_failure_is_tried_once_more(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.sleep = lambda s: None
    fake.publish_errors = [(503, "ServiceUnavailable")]
    assert rig.service.deliver("alerts", "MAYDAY", "x") == 1
    assert len(fake.published()) == 2 and rig.service.stats["retried"] == 1
    assert rig.service.devices.get("dev-0001") is not None


def test_a_second_transient_failure_gives_up_and_keeps_the_device(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.sleep = lambda s: None
    fake.publish_errors = [(500, "InternalFailure"), (500, "InternalFailure")]
    assert rig.service.deliver("alerts", "MAYDAY", "x") == 0
    assert len(fake.published()) == 2 and rig.service.devices.get("dev-0001") is not None


@pytest.mark.parametrize("status,code", [(400, "InvalidParameter"), (403, "AuthorizationError"),
                                         (400, "EndpointDisabled"), (404, "NotFound")])
def test_a_4xx_or_a_gone_endpoint_is_never_retried(rig, fake, status, code):
    rig.device(1, "admin@example.org")
    rig.service.sleep = lambda s: None
    fake.publish_errors = [(status, code)]
    assert rig.service.deliver("alerts", "MAYDAY", "x") == 0
    assert len(fake.published()) == 1 and rig.service.stats["retried"] == 0


def test_the_outbound_guard_is_never_retried():
    assert snsmod.transient(snsmod.SnsError("x", "OutboundOff", 0)) is False
    assert snsmod.transient(snsmod.SnsError("x", "Network", 0)) is True
    assert snsmod.transient(snsmod.SnsError("x", "InternalFailure", 500)) is True
    assert snsmod.transient(snsmod.SnsError("x", "EndpointDisabled", 400)) is False


def test_a_slow_sns_is_published_to_by_a_small_pool_not_one_at_a_time(tmp_path, fake):
    r = Rig(tmp_path, fake)
    for n in range(1, 7):
        r.device(n, "admin@example.org")
    fake.publish_delay = 0.3
    start = time.time()
    assert r.service.deliver("alerts", "MAYDAY", "x") == 6
    took = time.time() - start
    assert took < 6 * 0.3 * 0.75                                        # serial would be 1.8 s; three at a time is 0.6 s
    assert svc.POOL == 3


# -- 6. the stale-endpoint race ------------------------------------------------------------------------------------------

def test_a_stale_publish_never_deletes_a_fresh_registration_of_the_same_device(rig, fake):
    old = rig.device(1, "admin@example.org")
    row = rig.service.devices.get("dev-0001")                          # what the fan-out read
    fresh = rig.service.register("dev-0001", "admin@example.org", "ios", "sandbox", "fedcba9876543210" * 2, None, None)
    assert fresh["endpoint"] != old["endpoint"]
    fake.disabled.add(old["endpoint"])
    assert rig.service._publish_one(row, "alerts", "x", "t", "b", "active") == "failed"
    assert rig.service.devices.get("dev-0001")["endpoint"] == fresh["endpoint"]      # the new registration survives
    assert fresh["endpoint"] in fake.endpoints


def test_the_same_endpoint_written_again_since_is_not_deleted_either(rig, fake):
    rig.device(1, "admin@example.org")
    row = rig.service.devices.get("dev-0001")
    rig.now[0] += 60
    rig.device(1, "admin@example.org")                                  # same token, same endpoint, newer `updated`
    fake.disabled.add(row["endpoint"])
    rig.service._publish_one(row, "alerts", "x", "t", "b", "active")
    assert rig.service.devices.get("dev-0001") is not None


# -- 7. a token already registered under another device id -----------------------------------------------------------------

def test_a_token_that_is_already_an_endpoint_under_another_device_is_taken_over(tmp_path, fake):
    r = Rig(tmp_path, fake)
    fake.strict = True
    token = "0123456789abcdef" * 2
    first = r.service.register("dev-0001", "view@example.org", "ios", "sandbox", token, None, None)
    row = r.service.register("dev-0002", "admin@example.org", "ios", "sandbox", token, None, None)   # same phone, new person
    assert row["endpoint"] == first["endpoint"]                         # the existing endpoint, its attributes updated
    assert fake.endpoints[row["endpoint"]]["user_data"] == "dev-0002" and fake.endpoints[row["endpoint"]]["enabled"]
    assert r.service.devices.get("dev-0001") is None                    # the old person's row stops pushing
    assert r.targets("alerts") == {"dev-0002"}


def test_an_invalid_parameter_that_is_not_the_existing_token_error_is_still_a_502(rig, fake):
    fake.fail["CreatePlatformEndpoint"] = (400, "InvalidParameter")
    with pytest.raises(svc.PushError) as e:
        rig.device(1, "admin@example.org")
    assert (e.value.status, e.value.code) == (502, "push_failed")


@pytest.mark.parametrize("arn", [
    "arn:aws:sns:us-east-1:999999999999:endpoint/GCM/evil/x",                      # other platform, account, app name
    "arn:aws:sns:us-east-1:123456789012:endpoint/APNS_SANDBOX/other-app/x",        # same account, another application
    "arn:aws:sns:eu-west-1:123456789012:endpoint/APNS_SANDBOX/example-ios-dev/x",  # another region
    "arn:aws:sns:us-east-1:999999999999:endpoint/APNS_SANDBOX/example-ios-dev/x",  # another account
    "arn:aws:sns:us-east-1:123456789012:endpoint/APNS/example-ios-dev/x",          # another platform
    "arn:aws:sns:us-east-1:123456789012:endpoint/APNS_SANDBOX/example-ios-dev-evil/x"])   # a longer application name
def test_an_endpoint_arn_out_of_the_error_message_must_be_of_the_configured_application(rig, fake, arn):
    real = fake.answer

    def answer(f):
        if f["Action"] == "CreatePlatformEndpoint":
            return fake._err("InvalidParameter", 400, f"Invalid parameter: Token Reason: Endpoint {arn} already exists "
                                                      "with the same Token, but different attributes.")
        return real(f)
    fake.answer = answer
    with pytest.raises(svc.PushError) as e:
        rig.device(1, "admin@example.org")
    assert (e.value.status, e.value.code) == (502, "push_failed")
    assert "SetEndpointAttributes" not in fake.actions() and "DeleteEndpoint" not in fake.actions()
    assert rig.service.devices.get("dev-0001") is None


def test_the_row_holding_the_same_endpoint_is_replaced_even_without_the_error(rig):
    a = rig.device(1, "view@example.org")
    rig.service.devices.put("dev-0009", "admin@example.org", "ios", "sandbox", a["endpoint"], SANDBOX_APP,
                            {**svc.DEFAULT_PREFS}, None)                # a stray row on the same endpoint
    rig.device(1, "view@example.org")
    assert rig.service.devices.get("dev-0009") is None and rig.service.devices.get("dev-0001") is not None


# -- 8. pruning --------------------------------------------------------------------------------------------------------------

def test_rows_of_revoked_devices_and_gone_people_are_pruned_at_a_register(rig, fake):
    a, b, c = rig.device(1, "admin@example.org"), rig.device(2, "disp@example.org"), rig.device(3, "view@example.org")
    rig.service.device_active = lambda d: d != "dev-0002"              # revoked
    rig.service.role_of = lambda e: None if e == "view@example.org" else "admin"     # a person who is gone
    rig.device(4, "admin@example.org")
    assert {r["device_id"] for r in rig.service.devices.rows()} == {"dev-0001", "dev-0004"}
    assert b["endpoint"] not in fake.endpoints and c["endpoint"] not in fake.endpoints and a["endpoint"] in fake.endpoints
    assert rig.service.stats["pruned"] == 2


def test_a_full_table_of_dead_rows_frees_its_places_for_a_new_device(rig):
    from sizeup.push.devices import MAX_DEVICES
    for n in range(MAX_DEVICES):
        rig.device(n, "admin@example.org")
    rig.service.device_active = lambda d: d != "dev-0000"
    rig.device(MAX_DEVICES, "admin@example.org")                       # would be 409 push_full without the prune
    assert rig.service.devices.count() == MAX_DEVICES


def test_the_periodic_pass_runs_in_a_fan_out_at_most_every_ten_minutes(rig):
    ticks = [1000.0]
    rig.service.mono = lambda: ticks[0]
    rig.device(1, "admin@example.org")
    rig.service.device_active = lambda d: False
    ticks[0] += svc.PRUNE_EVERY_S
    assert rig.service.deliver("alerts", "MAYDAY", "x") == 0            # a fan-out does the pass ...
    assert rig.service.devices.count() == 0
    rig.service.device_active = lambda d: True
    rig.device(2, "admin@example.org")
    rig.service.device_active = lambda d: False
    ticks[0] += 100
    rig.service.prune()
    assert rig.service.devices.count() == 1                             # ... but not again within ten minutes
    ticks[0] += svc.PRUNE_EVERY_S
    rig.service.prune()
    assert rig.service.devices.count() == 0


# -- 9. FYI and model titles --------------------------------------------------------------------------------------------------

LABELS = frozenset({"PD", "Smoke / fire reported", "Port Chester"})


@pytest.mark.parametrize("title", ["PD · Smoke / fire reported · Port Chester",
                                   "FYI · PD · Smoke / fire reported · Port Chester",
                                   "PD · Smoke / fire reported · Port Chester ·m",
                                   "FYI · PD · Smoke / fire reported · Port Chester ·m"])
def test_an_fyi_and_a_model_raised_title_are_kept(title):
    assert content.headline("intel", title, LABELS) == title


@pytest.mark.parametrize("title", ["FYI · Jane Doe · Smoke / fire reported", "FYI", "PD · Jane Doe ·m",
                                   "FYI · FYI · PD · Port Chester", "PD · Smoke / fire reported · Jane Doe ·m"])
def test_the_marks_do_not_let_other_text_through(title):
    assert content.headline("intel", title, LABELS) is None


def test_the_fd_lines_of_an_fyi_body_are_kept_and_nothing_else(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.intel_labels = lambda: LABELS
    rig.service.deliver("intel", "FYI · PD · Smoke / fire reported · Port Chester",
                        "100 Example St\n14:32 · high · FD on scene · #26-000015 · call Jane Doe", 4)
    a = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]
    assert a["title"].startswith("FYI · PD") and a["body"] == "100 Example St · 14:32 · high · FD on scene · #26-000015"


# -- 11. the intel body's place --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seg,ok", [("100 Example St", True), ("Example St, 100 blk", True),
                                    ("12 Sample Ave, Rye", True), ("Example Ave, 100 blk", True),
                                    ("Example St", False), ("Example St (unconfirmed)", False),
                                    ("Example St, 100 blk (unconfirmed)", False), ("John Smith St", False),
                                    ("Jane Doe Ave (unconfirmed)", False), ("N Jane Doe St", False),
                                    ("Mary Ann Smith Ave", False), ("Jane Doe Ave, 100 blk (unconfirmed)", False),
                                    ("Jane Doe near Example St", False), ("caller Jane Doe Example St", False),
                                    ("Jane said call 914 555 0147 Example St", False), ("Example St 41228", False),
                                    ("the St", False), ("St", False), ("x" * 80 + " St", False)])
def test_an_intel_place_segment_is_kept_only_when_it_has_the_address_shape(seg, ok):
    if ok is None:
        return
    assert content.intel_place_ok(seg) is ok


def test_a_street_with_no_number_is_dropped_from_the_body_and_a_numbered_one_kept(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.intel_labels = lambda: LABELS
    for i, (where, want) in enumerate([("John Smith St", "14:32 \u00b7 high"), ("Example St, 100 blk", "Example St, 100 blk \u00b7 14:32 \u00b7 high"),
                                       ("Jane Doe Ave (unconfirmed)", "14:32 \u00b7 high")]):
        rig.now[0] += 100
        rig.service.deliver("intel", "PD \u00b7 Smoke / fire reported \u00b7 Port Chester", f"{where}\n14:32 \u00b7 high", 4)
        assert fake.published()[-1]["message"]["APNS_SANDBOX"]["aps"]["alert"]["body"] == want


def test_names_beside_a_street_word_do_not_reach_the_phone(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.intel_labels = lambda: LABELS
    rig.service.deliver("intel", "PD · Smoke / fire reported · Port Chester",
                        "Jane Doe lives on Example St\n14:32 · high", 4)
    a = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]
    assert a["body"] == "14:32 · high" and "Jane" not in json.dumps(fake.calls)


# -- 10. one rule for every kind ------------------------------------------------------------------------------------------------

def test_every_kinds_device_push_follows_its_own_policy_and_the_producers_gate_is_the_ntfy_topic(tmp_path, fake):
    """The seam (`Pushes.fan_out`, `KindNotifier.send`) hands every kind to the devices whatever its ntfy topic is; the
    producers (alarms, health, the intel engine) each build a message only while `notifier.enabled` (a topic in effect).
    So the rule is the same for alerts, status and intel: no topic, no message; with one, the device policy decides."""
    r = Rig(tmp_path, fake)
    r.device(1, "admin@example.org", prefs={"dispatch": True})
    r.policy = {"dispatch": {"on": True}, "intel": {"min_role": "viewer"}}
    p = notify.Pushes("http://127.0.0.1:9", "", "", settings=lambda: {}, log=lambda m: None, retries=())
    p.devices = r.service
    for kind in (notify.ALERTS, notify.STATUS, notify.INTEL):
        assert p.notifier(kind).enabled is False                        # no ntfy topic: the producers send nothing ...
    p.notifier(notify.ALERTS).send("MAYDAY", "x", 5)                    # ... but the seam itself does not look at it
    p.notifier(notify.INTEL).send(*INTEL, 4)
    assert r.service.flush(10)
    kinds = [x["message"]["APNS_SANDBOX"]["kind"] for x in fake.published()]
    assert sorted(kinds) == ["alerts", "intel"]
    r.policy = {"off": True}
    p.notifier(notify.ALERTS).send("MAYDAY", "x", 5)
    assert r.service.flush(10) and len(fake.published()) == 2           # a kill switch stops every kind alike
