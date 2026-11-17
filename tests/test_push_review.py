"""Device push, round two (#563): a revoked device gets nothing (device_active), a task that is not the leader never
publishes, one deadline per message, the stale-endpoint rule per SNS code, the role lookup against the real operators
table, the admin path for the caps and kill switches, the hub's wiring, quiet hours across daylight saving, and the
caps over an hour and a day. Invented values."""
from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup import notify
from sizeup.cad.api import CadApi
from sizeup.fence import FencedWrite
from sizeup.push import content, service as svc, sns as snsmod
from sizeup.push.wire import role_for, wire
from tests.test_cloud import HubRun, hub_cfg
from tests.test_push_sns import APPS, NOON, SANDBOX_APP, Rig, fake, rig  # noqa: F401  (fixtures)
from tests.test_roles import CASEY, JORDAN, VAL, call, people
from tests.test_cad import make_cad


# -- a revoked device, a task that is not the leader, the deadline -----------------------------------------------------

def test_a_device_marked_inactive_gets_nothing_of_any_kind(rig, fake):
    rig.device(1, "admin@example.org", prefs={"dispatch": True})
    rig.device(2, "admin@example.org", prefs={"dispatch": True})
    rig.policy = {"dispatch": {"on": True}}
    rig.service.device_active = lambda device_id: device_id != "dev-0001"       # #558: dev-0001 is revoked
    for kind in ("alerts", "intel", "status", "dispatch"):
        rig.now[0] += 100
        assert rig.targets(kind, "MAYDAY", "100 Example Ave") == {"dev-0002"}, kind


def test_without_a_predicate_every_registered_device_is_active(rig):
    rig.device(1, "admin@example.org")
    assert svc.PushService(rig.service.devices, APPS).device_active("dev-0001") is True
    assert rig.targets("alerts") == {"dev-0001"}


def test_a_task_that_is_not_the_leader_queues_and_publishes_nothing(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.devices._may_write = lambda: False                      # the old task of a handover overlap
    rig.service.offer("alerts", "MAYDAY", "100 Example Ave")
    assert rig.service.flush() and fake.published() == []
    assert rig.service.deliver("alerts", "MAYDAY", "100 Example Ave") == 0
    assert fake.published() == [] and rig.service.stats["skipped"] == 1
    rig.service.devices._may_write = lambda: True
    rig.service.offer("alerts", "MAYDAY", "100 Example Ave")
    assert rig.service.flush() and len(fake.published()) == 1


def test_one_message_has_one_deadline_for_the_serial_publishes(rig, fake):
    for n in range(6):
        rig.device(n, "admin@example.org")
    ticks = iter(range(0, 10_000, 12))                                  # the clock moves 12 s per look
    rig.service.mono = lambda: next(ticks)
    sent = rig.service.deliver("alerts", "MAYDAY", "100 Example Ave")
    assert 1 <= sent < 6 and len(fake.published()) == sent               # the rest were not tried
    assert any("ran out of time" in m for m in rig.logs)


# -- the stale-endpoint rule, per SNS code -----------------------------------------------------------------------

@pytest.mark.parametrize("code,kept", [("NotFound", False), ("EndpointDisabled", False), ("InvalidParameter", True),
                                       ("InternalFailure", True)])
def test_only_a_gone_or_disabled_endpoint_is_removed(rig, fake, code, kept):
    row = rig.device(1, "admin@example.org")
    fake.fail["Publish"] = (400, code)
    rig.service.deliver("alerts", "MAYDAY", "100 Example Ave")
    assert (rig.service.devices.get("dev-0001") is not None) is kept
    assert (row["endpoint"] in fake.endpoints) is kept                  # and its SNS endpoint with it


def test_a_stale_endpoint_on_a_fenced_task_is_kept_for_the_leader(rig, fake):
    rig.device(1, "admin@example.org")
    fake.fail["Publish"] = (400, "EndpointDisabled")
    real = rig.service.unregister

    def fenced(device_id):
        raise FencedWrite("not the leader")
    rig.service.unregister = fenced
    rig.service.deliver("alerts", "MAYDAY", "100 Example Ave")
    assert rig.service.devices.get("dev-0001") is not None and any("stale endpoint kept" in m for m in rig.logs)
    rig.service.unregister = real


def test_unregister_and_drop_device_refuse_on_a_fenced_task(rig):
    rig.device(1, "admin@example.org")
    rig.service.devices._may_write = lambda: False
    with pytest.raises(FencedWrite):
        rig.service.unregister("dev-0001")
    svc.install(rig.service)
    try:
        with pytest.raises(FencedWrite):
            svc.drop_device("dev-0001")                                 # a revoke says so: no pretending
    finally:
        svc.install(None)
    assert rig.service.devices.get("dev-0001") is not None


# -- the role lookup against the real operators table ----------------------------------------------------------------

def test_the_role_comes_from_the_real_operators_table(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)                                   # Casey admin, Jordan dispatcher, Val viewer, Dee deactivated
    store = cad.store
    assert [role_for(store, e) for e in (CASEY, JORDAN, VAL)] == ["admin", "dispatcher", "viewer"]
    assert role_for(store, CASEY.upper()) == "admin" and role_for(store, f"  {JORDAN}  ") == "dispatcher"
    assert role_for(store, "stranger@example.org") is None                # not an operator: nothing
    # deactivated: nothing (not a viewer, unlike standing)
    assert role_for(store, "dee@example.org") is None
    with store.tx() as db:
        db.execute("UPDATE operators SET role = 'supervisor' WHERE email = ?", (VAL,))
    assert role_for(store, VAL) == "viewer"                               # an unmapped role is a viewer


def test_with_no_admin_yet_status_reaches_nobody_and_alerts_still_reach_active_people(tmp_path, fake):
    cad = make_cad(tmp_path)
    people(cad, admin=False)                      # bootstrap: Settings.standing makes everyone an admin; push does not
    r = Rig(tmp_path, fake)
    r.service.role_of = lambda e: role_for(cad.store, e)
    r.device(1, CASEY)
    r.device(2, JORDAN)
    assert r.targets("status", "Station silent") == set()
    assert r.targets("alerts") == {"dev-0001", "dev-0002"}


def test_a_person_with_no_operators_row_gets_nothing_until_a_console_sign_in_makes_one(tmp_path):
    """Found: only Settings.person() (a console request through CadApi) adds the operators row. The app's sign-in
    (#558) must make it too, or that person's devices get no pushes (role_for is None and never writes)."""
    cad = make_cad(tmp_path)
    people(cad)
    new = "newcomer@example.org"
    n = len(cad.store.q("SELECT 1 FROM operators"))
    assert role_for(cad.store, new) is None and len(cad.store.q("SELECT 1 FROM operators")) == n     # reads only
    cad.settings.person(new)
    assert role_for(cad.store, new) == "dispatcher" and len(cad.store.q("SELECT 1 FROM operators")) == n + 1


# -- the admin path for the caps and kill switches ----------------------------------------------------------------

class Admin:
    def __init__(self, tmp_path):
        self.cad = make_cad(tmp_path)
        people(self.cad)
        self.api = CadApi(self.cad)
        section = lambda: self.cad.settings.section(notify.SECTION)          # noqa: E731
        self.cad.pushes = self.p = notify.Pushes("http://127.0.0.1:9", "", "", settings=section, log=lambda m: None)

    def post(self, body, who=CASEY):
        return call(self.api, "/api/cad/settings/notify", {"key": "devices", **body}, who)


def test_an_admin_sets_and_resets_the_policy_and_it_is_range_checked(tmp_path):
    a = Admin(tmp_path)
    assert a.post({"action": "set", "policy": {"dispatch": {"on": True, "per_minute": 5}}})[0] == 200
    assert svc.policy(a.p.settings().get("devices"))["dispatch"] == {"on": True, "per_minute": 5, "per_hour": 120,
                                                                     "per_day": 600}
    assert a.post({"action": "set", "policy": {"off": True}})[0] == 200
    assert all(not v["on"] for v in svc.policy(a.p.settings()["devices"]).values())
    assert a.post({"action": "reset"})[0] == 200 and "devices" not in a.p.settings()
    assert a.post({"action": "reset"})[0] == 400                          # already on its defaults
    for bad in ({"policy": {"alerts": {"per_hour": 0}}}, {"policy": {"alerts": {"per_hour": 10 ** 9}}},
                {"policy": {"alerts": {"per_hour": True}}}, {"policy": {"alerts": {"on": "yes"}}},
                {"policy": {"emergency": {"on": True}}}, {"policy": {"alerts": {"bogus": 1}}}, {"policy": "off"},
                {"policy": {}}, {}):
        code, out = a.post({"action": "set", **bad})
        assert code == 400, bad
    assert a.p.settings().get("devices") is None


def test_only_an_admin_may_set_it(tmp_path):
    a = Admin(tmp_path)
    for who in (JORDAN, VAL):
        assert a.post({"action": "set", "policy": {"dispatch": {"on": True}}}, who)[0] == 403
    assert a.p.settings().get("devices") is None


def test_the_policy_an_admin_set_is_the_one_the_fan_out_reads(tmp_path, fake):
    a = Admin(tmp_path)
    w = wire(a.p, tmp_path / "state", type("S", (), {"allows": lambda s, f: True})(), a.cad.store, env={})
    try:
        assert w.settings()["dispatch"] if "dispatch" in w.settings() else True
        assert a.post({"action": "set", "policy": {"intel": {"on": False}}})[0] == 200
        assert svc.policy(w.settings())["intel"]["on"] is False
    finally:
        svc.install(None)


# -- the hub's wiring -------------------------------------------------------------------------------------------------

def test_a_hub_start_wires_push_and_writes_nothing_of_its_own(tmp_path):
    h = HubRun(hub_cfg(tmp_path, mobile_enabled=True))                    # master switch on, every feature flag off
    try:
        s = h.app.pushes.devices
        assert s is not None and svc.installed() is s and s.active() is False       # the `push` flag: off
        assert s.devices.path.endswith("hub.sqlite") and s.role_of("nobody@example.org") is None
        # the live wiring's role_of and unit_of read the hub's own CAD store (not role_for / unit_for called directly)
        people(h.app.cad)                                               # Casey admin, Jordan dispatcher, Val viewer, Dee off
        assert [s.role_of(e) for e in (CASEY, JORDAN, VAL, "dee@example.org", "x@example.org")] == [
            "admin", "dispatcher", "viewer", None, None]
        assert s.unit_of("E64") is not None and s.unit_of("4821") is None and s.unit_of("J. Doe") is None
        s.offer("alerts", "MAYDAY", "100 Example Ave")                # flag off: nothing queued
        assert s._q.qsize() == 0
        db = sqlite3.connect(s.devices.path)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name = 'push_devices'").fetchone() is None
        db.close()
    finally:
        h.close()
        svc.install(None)


# -- caps over an hour and a day, and every kind ----------------------------------------------------------------------

@pytest.mark.parametrize("kind,title,body", [("alerts", "MAYDAY", "100 Example Ave"),
                                              ("intel", "PD · Smoke reported", "100 Example Ave · 14:32 · high"),
                                              ("status", "Station silent", "x"), ("dispatch", "Structure fire", "x")])
def test_the_hour_and_day_caps_hold_for_every_kind(rig, fake, kind, title, body):
    rig.device(1, "admin@example.org", prefs={"dispatch": True})
    rig.policy = {kind: {"on": True, "per_minute": 1000, "per_hour": 3, "per_day": 5}}
    sent = lambda: len(fake.published())                                  # noqa: E731
    for _ in range(10):
        rig.now[0] += 1
        rig.service.deliver(kind, title, body)
    assert sent() == 3                                                    # the hour's cap
    rig.now[0] += 3601                                                    # the hour passed, the day's budget is 2 left
    for _ in range(10):
        rig.now[0] += 1
        rig.service.deliver(kind, title, body)
    assert sent() == 5                                                    # the day's cap
    rig.now[0] += 86400                                                   # a day later: a clean slate
    rig.service.deliver(kind, title, body)
    assert sent() == 6


def test_dispatch_without_the_entitlement_stays_time_sensitive(rig, fake):
    assert rig.service.apps.critical is False
    rig.device(1, "admin@example.org", prefs={"dispatch": True})
    rig.policy = {"dispatch": {"on": True}}
    rig.service.deliver("dispatch", "Structure fire", "x", priority=5)
    aps = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]
    assert aps["interruption-level"] == "time-sensitive" and aps["sound"] == "default"


# -- quiet hours across daylight saving ---------------------------------------------------------------------------

NY = "America/New_York"
SPRING_UTC = 1772953200          # 2026-03-08 07:00 UTC: 02:00 EST jumps to 03:00 EDT
FALL_UTC = 1793509200            # 2026-11-01 05:00 UTC: 01:00 EDT (the first one)


@pytest.mark.parametrize("secs,expected", [(-3600, False),        # 01:00 EST
                                           (-1800, False),        # 01:30 EST: before the window
                                           # 03:00 EDT: 02:30 never happened, the window is [02:00,03:30)
                                           (0, True),
                                           (1799, True),          # 03:29 EDT
                                           (1800, False)])        # 03:30 EDT
def test_quiet_hours_when_the_clocks_spring_forward(secs, expected):
    q = {"from": "02:00", "to": "03:30", "tz": NY}
    assert svc.in_quiet(q, SPRING_UTC + secs) is expected


def test_the_nonexistent_hour_is_never_quiet_and_the_repeated_hour_is_quiet_both_times():
    skipped = {"from": "02:00", "to": "03:00", "tz": NY}
    assert not any(svc.in_quiet(skipped, SPRING_UTC + m * 60) for m in range(-120, 180))      # 02:xx does not exist
    repeated = {"from": "01:00", "to": "02:00", "tz": NY}
    assert svc.in_quiet(repeated, FALL_UTC + 30 * 60)                     # 01:30 EDT
    assert svc.in_quiet(repeated, FALL_UTC + 90 * 60)                     # 01:30 EST, an hour later on the wall clock
    assert not svc.in_quiet(repeated, FALL_UTC + 150 * 60)                # 02:30 EST


def test_the_edges_of_the_window():
    assert not svc.in_quiet({"from": "00:00", "to": "00:00", "tz": "UTC"}, NOON)          # from == to: no window
    assert svc.in_quiet({"from": "00:00", "to": "23:59", "tz": "UTC"}, NOON)
    # 23:59:30 UTC: the window ends at 23:59
    assert not svc.in_quiet({"from": "00:00", "to": "23:59", "tz": "UTC"}, 1767311970)
    from sizeup.api.v1.push import parse
    with pytest.raises(ValueError):                                       # "24:00" is not an hour: no all-day window
        parse(json.dumps({"platform": "android", "token": "x" * 20,
                          "quiet": {"from": "00:00", "to": "24:00", "tz": "UTC"}}).encode())


def test_a_broken_quiet_record_is_never_quiet():
    for q in ({"from": "x", "to": "06:00", "tz": "UTC"}, {"from": "22:00"},
              {"from": "22:00", "to": "06:00", "tz": ""}, "x"):
        assert svc.in_quiet(q, NOON) is False
