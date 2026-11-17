"""What the phone gets from each REAL producer of pushes (#563), with a name, a 4-5 digit radio ID, a plate and a phone
number in the parts a person or the radio can type or say: the alarms (a radio alias, an analog ID, the incident's typed
address), the airport pre-alerts, station health and the intel engine. Each producer's own code builds the message;
it goes
through the real `notify.Pushes` call path (`KindNotifier.send` -> `fan_out`) into the fake SNS, and none of it may
appear
in what was published. Invented values."""
from __future__ import annotations

import json
import re
import time

import pytest

from sizeup import notify
from sizeup.cad.airport_alerts import AirportAlert
from sizeup.push import content
from sizeup.push.wire import unit_for
from tests.test_alarms import make_cad, tx, uid
from tests.test_health import Rig as HealthRig
from tests.test_health import radio, station_status
from tests.test_intel import engine, row
from tests.test_push_sns import Rig, fake, rig  # noqa: F401  (the fake SNS and the rig as fixtures)

BAD = ("John Smith", "Jane Doe", "Jordan Example", "4821", "41228", "ABC1234", "914-555-0123", "555-0123", "J. Doe",
       "N123AB", "Officer", "caller")


class Capture:
    """A KindNotifier as the producers call it: records what ntfy would be sent."""
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority=3, tags=(), click="", who=None, actions=()):
        self.sent.append({"title": title, "body": body, "priority": priority, "click": click, "who": who})
        return True


def through_pushes(rig, kind, sent):
    """Each captured message, through the real Pushes -> KindNotifier.send -> fan_out path."""
    p = notify.Pushes("http://127.0.0.1:9", "example-alerts-topic-0001", "example-status-topic-0002",
                      settings=lambda: {}, log=lambda m: None, retries=())
    p.devices = rig.service
    for m in sent:
        rig.now[0] += 100
        p.notifier(kind).send(m["title"], m["body"], m["priority"], (), m.get("click") or "", who=m.get("who"))
    assert rig.service.flush(10)


def published(fake) -> str:
    return json.dumps(fake.calls, sort_keys=True)


def assert_none_of(fake, extra=()):
    text = published(fake).lower()
    for s in (*BAD, *extra):
        assert s.lower() not in text, s


# -- the alarms: units and the typed address ---------------------------------------------------------------------------

@pytest.mark.parametrize("radio_id,unit,rig_code", [("4821", "4821", ""), ("41228", "41228", ""), ("4821", "J. Doe", ""),
                                                      ("4821", "Jane Doe", "E64")])
def test_an_alarms_unit_is_named_only_when_it_is_on_the_cads_list(rig, fake, tmp_path, radio_id, unit, rig_code):
    cad = make_cad(tmp_path)
    cad.alarms.notifier = cap = Capture()
    cad.alarms.on_traffic(tx("FG1-7", "mayday mayday Jane Doe is down", rig=rig_code, unit=unit, radio_id=radio_id,
                             at=time.time() + 0.01))
    assert len(cap.sent) == 1
    rig.service.unit_of = lambda code: unit_for(cad.store, code)
    rig.device(1, "admin@example.org")
    through_pushes(rig, notify.ALERTS, cap.sent)
    assert_none_of(fake)
    title = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]["title"]
    assert not re.search(r"\d{3,}", title.replace("MAYDAY", ""))
    if rig_code:
        assert "E64 (Engine 64)" in title and "Jane" not in title                   # the rig's unit, by the list's name
    else:
        assert title == "MAYDAY on FG1" or title.startswith("MAYDAY")


def test_the_cads_unit_codes_still_appear(rig, fake, tmp_path):
    cad = make_cad(tmp_path)
    rig.service.unit_of = lambda code: unit_for(cad.store, code)
    rig.device(1, "admin@example.org")
    rig.service.deliver("alerts", "MAYDAY", "x", 5, {"units": [("E64 P1", "Engine 64, Jane Doe"), ("L31", "")]})
    title = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]["title"]
    assert title == "MAYDAY \u00b7 E64 (Engine 64), L31 (Ladder 31)"
    assert "Jane" not in title and "P1" not in title


@pytest.mark.parametrize("address,shown", [("100 S Ridge St", True), ("Rt 22 & Example Ave", True),
                                            ("Example Ave, 100 blk", True), ("I-95 NB Exit 8", True),
                                            ("call John Smith at 100 Main", False),
                                            ("call John Smith at 100 Main St", False),
                                            ("caller says 914-555-0123 100 Example Ave", False),
                                            ("100 Example Ave " + "x" * 80, False), ("", False)])
def test_an_address_is_shown_only_when_it_looks_like_one(address, shown):
    assert content.place_ok(address) is shown


def test_a_dispatchers_typed_free_text_address_never_reaches_the_phone(rig, fake, tmp_path):
    """The incident's address is what a dispatcher typed (sizeup/cad/service.py: up to 200 characters)."""
    cad = make_cad(tmp_path)
    cad.alarms.notifier = cap = Capture()
    inc = cad.create({"type": "STRU", "address": "call John Smith at 100 Main St", "operator": "Lt. Ruiz"})
    cad.dispatch(inc["id"], {"unit_ids": [uid(cad, "E64")], "operator": "Lt. Ruiz"})
    t = tx("FG1-7", "mayday mayday", rig="E64", unit="E64 P1", radio_id="6401", at=time.time() + 0.01)
    cad.record_traffic(t)
    cad.alarms.on_traffic(t)
    assert "John Smith" in cap.sent[0]["who"]["address"]                  # the producer really did carry the typed text
    rig.service.unit_of = lambda code: unit_for(cad.store, code)
    rig.device(1, "admin@example.org")
    through_pushes(rig, notify.ALERTS, cap.sent)
    a = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]
    assert a["body"] == "Open sizeup for details" and "Smith" not in json.dumps(fake.calls)
    assert a["title"].startswith("MAYDAY") and "E64 (Engine 64)" in a["title"]


# -- the airport pre-alerts --------------------------------------------------------------------------------------------

def test_an_airport_pre_alerts_title_is_rebuilt_from_its_fixed_lead(rig, fake, tmp_path):
    cad = make_cad(tmp_path)
    cad.airport_alerts.notifier = cap = Capture()
    for i, title in enumerate(["HPN ATC: possible brush fire reported off runway 16 · call John Smith 4821 N123AB",
                               "HPN Alert 2 · Citation · caller Jane Doe 914-555-0123",
                               "AIRCRAFT EMERGENCY · N123AB · squawk 7700 (emergency)"]):
        cad.airport_alerts.raise_alert(AirportAlert(source="atc", kind=f"fire{i}", subject=f"s{i}", title=title,
                                                    detail="heard: John Smith 4821", confidence=0.9, at=time.time()))
    assert len(cap.sent) == 3
    rig.device(1, "admin@example.org")
    through_pushes(rig, notify.ALERTS, cap.sent)
    assert_none_of(fake)
    titles = [p["message"]["APNS_SANDBOX"]["aps"]["alert"]["title"] for p in fake.published()]
    assert titles == ["HPN ATC pre-alert", "HPN Alert 2", "AIRCRAFT EMERGENCY: SQUAWK 7700"]


# -- station health ----------------------------------------------------------------------------------------------------

def test_a_health_alert_keeps_its_fixed_sentence_and_loses_the_configured_names(rig, fake):
    h = HealthRig()
    raw = Capture()
    h.h.notifier = raw                                                  # the raw title and Who, as ntfy composes them
    st = station_status(name="PCFD station John Smith 4821", radios=[radio("FG1 Jordan Example 4821 ABC1234")])
    h.at(0, status=st)
    h.at(300, post=False)                                               # silent: "Station silent 5 min" and the station
    assert raw.sent and "John Smith" in json.dumps(raw.sent[0]["who"])  # the producer really carried the name
    rig.device(1, "admin@example.org")
    through_pushes(rig, notify.STATUS, raw.sent)
    assert_none_of(fake, ("PCFD station", "South 2", "00000102", "FG1"))
    titles = {p["message"]["APNS_SANDBOX"]["aps"]["alert"]["title"] for p in fake.published()}
    assert titles == {"Station silent 5 min"}, titles
    assert fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]["body"] == "Open sizeup for details"


@pytest.mark.parametrize("title,sent", [("Station silent 3 min", "Station silent 3 min"),
                                        ("Still: Station disk full 1 h 5 min", "Still: Station disk full 1 h 5 min"),
                                        ("Recovered: Station silent", "Recovered: Station silent"),
                                        ("FG1 John Smith not running", "A radio job is not running"),
                                        ("Duplicate dongle serial 00000102", "Duplicate dongle serial"),
                                        ("Replica corrupt: cad.sqlite", "Replica problem"),
                                        ("John Smith called, plate ABC1234", None)])
def test_status_headlines(title, sent):
    assert content.headline("status", title) == sent


# -- the intel engine --------------------------------------------------------------------------------------------------

def test_an_intel_push_composed_from_a_transcript_naming_people_carries_none_of_it(rig, fake):
    e, sink = engine()
    e.public_url = "https://sizeup.example.org"
    e.feed(row("t1", "respond to 100 Example Ave, caller Jane Example reports smoke coming from the house", 1000,
               talker="Officer Example", radio_id="7654321"))
    msgs = [d.message for _x, d in sink.pushes if d.would_push]
    assert msgs
    extra = ("\nListen: https://bucket.example.org/listen/abc.m4a?X-Amz-Signature=zz"
             "\ncall John Smith 914-555-0123 plate ABC1234 radio 4821")
    sent = [dict(m, body=m["body"] + extra, who=None) for m in msgs]
    rig.device(1, "admin@example.org")
    through_pushes(rig, notify.INTEL, sent)
    assert_none_of(fake, ("jane", "respond", "reports", "7654321", "Listen", "http"))
    a = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]
    assert a["title"] == "PD · Smoke / fire reported · Port Chester" and a["body"].startswith("100 Example Ave · ")


# -- device_active is not wired ----------------------------------------------------------------------------------------

def test_registering_while_device_active_is_not_wired_warns_once(tmp_path, fake):
    from tests.test_push_sns import APPS, CREDS, IOS_TOKEN
    from sizeup.push import service as svc, sns as snsmod
    from sizeup.push.devices import PushDevices
    logs = []
    s = svc.PushService(PushDevices(tmp_path / "hub.sqlite"), APPS, snsmod.Sns("us-east-1", fake.url, lambda: CREDS),
                        log=logs.append)
    s.register("dev-0001", "a@example.org", "ios", "sandbox", IOS_TOKEN, None, None)
    s.register("dev-0002", "b@example.org", "ios", "sandbox", IOS_TOKEN[:-2] + "02", None, None)
    assert [m for m in logs if "device_active not wired" in m] == ["push: device_active not wired: revoked devices keep "
                                                                  "receiving pushes"]
    logs.clear()
    w = svc.PushService(PushDevices(tmp_path / "w.sqlite"), APPS, snsmod.Sns("us-east-1", fake.url, lambda: CREDS),
                        device_active=lambda d: True, log=logs.append)
    w.register("dev-0003", "a@example.org", "ios", "sandbox", IOS_TOKEN[:-2] + "03", None, None)
    assert not any("device_active" in m for m in logs)


def test_the_hub_start_says_device_active_is_not_wired(tmp_path):
    from sizeup.push.wire import wire
    logs = []
    pushes = notify.Pushes("http://127.0.0.1:9", "", "", settings=lambda: {}, log=lambda m: None)
    try:
        wire(pushes, tmp_path, type("S", (), {"allows": lambda s, f: False})(), None, log=logs.append, env={})
        assert any("device_active not wired" in m and "stays off" in m for m in logs)
        logs.clear()
        wire(pushes, tmp_path, type("S", (), {"allows": lambda s, f: False})(), None, log=logs.append, env={},
             device_active=lambda d: True)
        assert logs == []
    finally:
        from sizeup.push import service as svc
        svc.install(None)
