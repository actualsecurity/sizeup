"""What a phone is told (#563, sizeup/push/content.py): an allow-list, never the caller's free text. The bodies here are
the ones the code really builds (the alarm's push from sizeup/cad/alarms.py through the real Alarms, the health and
intel shapes from their modules), with a person's name, a short radio ID, an acknowledging operator, a plate and a phone
number in them: none of those may appear anywhere in what is published to APNs or FCM, for every kind.
Invented values."""
from __future__ import annotations

import json
import time

import pytest

from sizeup.push import content
from tests.test_alarms import make_cad, tx, uid
from tests.test_push_sns import Rig, fake, rig  # noqa: F401  (the fake SNS and the rig as fixtures)

SECRETS = ("John Smith", "Jane Doe", "4821", "ABC1234", "914-555-0123", "555-0123", "heard", "acknowledged by",
           "Jordan Example", "00000102", "radio 4821", "http", "Listen")


class Capture:
    """What the alarms push to ntfy, as the Notifier is called: (title, body, priority, who, click)."""
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority=3, tags=(), click="", who=None, actions=()):
        self.sent.append({"title": title, "body": body, "priority": priority, "who": who, "click": click})
        return True


def everything(fake) -> str:
    """Every byte published to SNS for the test: the whole Message JSON and the attributes."""
    return json.dumps(fake.calls, sort_keys=True)


def assert_clean(fake, extra=()):
    text = everything(fake)
    for s in (*SECRETS, *extra):
        assert s.lower() not in text.lower(), s


def alarm_pushes(tmp_path):
    """A real mayday (a transcript naming a person, a plate and a phone, from radio 4821) and its acknowledgement by a
    named operator, as the alarms push them."""
    cad = make_cad(tmp_path)
    cad.alarms.notifier = cap = Capture()
    inc = cad.create({"type": "STRU", "address": "100 S Ridge St", "operator": "Lt. Ruiz"})
    cad.dispatch(inc["id"], {"unit_ids": [uid(cad, "E64")], "operator": "Lt. Ruiz"})
    t = tx("FG1-7", "mayday mayday John Smith is down, plate ABC1234, call 914-555-0123, Engine 64, Ladder 31",
           rig="E64", unit="E64 P1", radio_id="4821", at=time.time() + 0.01)
    cad.record_traffic(t)
    a = cad.alarms.on_traffic(t)
    cad.alarm_ack(a["id"], {"operator": "Jane Doe"})
    return cad.store.incident(inc["id"])["number"], cap.sent


def test_the_real_alarm_pushes_carry_the_names_the_ntfy_body_has_and_the_phone_gets_none_of_them(rig, fake, tmp_path):
    num, sent = alarm_pushes(tmp_path)
    assert len(sent) == 2
    # ntfy's
    assert "John Smith" in sent[0]["body"] and "radio 4821" in sent[0]["body"] and "Jane Doe" in sent[1]["body"]
    rig.device(1, "admin@example.org")
    for m in sent:
        rig.now[0] += 100
        rig.service.deliver("alerts", m["title"], m["body"], m["priority"], m["who"], m["click"])
    assert len(fake.published()) == 2
    assert_clean(fake)
    first, second = (p["message"]["APNS_SANDBOX"] for p in fake.published())
    assert first["id"] == num and set(first) == {"aps", "kind", "id"}
    # still a useful page: what, which unit, where, and the address as the body
    title = first["aps"]["alert"]["title"]
    assert title.startswith("MAYDAY") and "E64 (Engine 64)" in title
    assert num in first["aps"]["alert"]["title"] and first["aps"]["alert"]["body"] == "100 S Ridge St"
    assert second["aps"]["alert"]["title"].startswith("Acknowledged: MAYDAY")


def test_a_mayday_with_nothing_structured_is_still_a_generic_page(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.deliver("alerts", "MAYDAY", "12:01:09 · radio 4821 · heard: “John Smith is down”", 5,
                        {"talker": "radio 4821"})
    a = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]
    assert a == {"title": "MAYDAY", "body": "Open sizeup for details"}
    assert_clean(fake)


def test_status_never_carries_the_callers_body(rig, fake):
    rig.device(1, "admin@example.org")
    rig.service.deliver("status", "FG1 not running",
                        "FG1 not running · SDR 00000102 on FG1\nlast keyed by Jordan Example, radio 4821", 3,
                        {"talker": "Jordan Example", "dongle": "SDR 00000102", "station": "Port Chester FD",
                         "detail": "last heard 14:32"})
    a = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]
    assert a == {"title": "A radio job is not running", "body": "Open sizeup for details"}
    assert_clean(fake)


def test_intel_keeps_the_place_time_and_level_and_drops_everything_else(rig, fake):
    rig.device(1, "admin@example.org")
    body = ("John Smith · 100 Example Ave · 14:32 · high · call 914-555-0123\n"
            "Listen: https://bucket.example.org/listen/abc.m4a?X-Amz-Signature=zz")
    rig.service.deliver("intel", "PD · Smoke / fire reported · Port Chester", body, 4,
                        click="https://hub.example.org/radio?view=all&intel=77")
    pay = fake.published()[0]["message"]["APNS_SANDBOX"]
    assert pay["aps"]["alert"]["body"] == "100 Example Ave · 14:32 · high" and pay["id"] == "intel:77"
    assert_clean(fake)


def test_dispatch_stays_generic_until_it_has_a_producer(rig, fake):
    rig.device(1, "admin@example.org", prefs={"dispatch": True})
    rig.policy = {"dispatch": {"on": True}}
    rig.service.deliver("dispatch", "Structure Fire - Jane Doe", "caller John Smith, plate ABC1234, 914-555-0123", 3,
                        {"talker": "radio 4821", "units": [("E64", "Engine 64")], "incident": "26-000015",
                         "address": "100 Example Ave"})
    pay = fake.published()[0]["message"]["APNS_SANDBOX"]
    assert pay["aps"]["alert"] == {"title": "Dispatch", "body": "Open sizeup for details"} and pay["id"] == "26-000015"
    assert_clean(fake)


@pytest.mark.parametrize("kind,title,body", [
    ("alerts", "MAYDAY", "radio 4821 · heard: “John Smith”"),
    ("intel", "PD · Smoke reported · Port Chester", "John Smith · 14:32 · high"),
    ("status", "Station silent", "last keyed by John Smith"),
    ("dispatch", "Structure fire", "John Smith, 914-555-0123"),
])
def test_every_kind_has_a_generic_fallback_when_the_title_itself_fails_the_scrub(rig, fake, kind, title, body):
    """A title that still carries a radio ID, a plate or a date after the allow-list is replaced, not sent."""
    rig.device(1, "admin@example.org", prefs={"dispatch": True})
    rig.policy = {"dispatch": {"on": True}}
    rig.service.deliver(kind, f"{title} 4412090 ABC1234", body, 3)
    a = fake.published()[0]["message"]["APNS_SANDBOX"]["aps"]["alert"]
    assert (a["title"], a["body"]) == content.GENERIC[kind]
    assert_clean(fake, ("4412090",))


def test_android_gets_the_same_text_and_nothing_more(rig, fake):
    rig.device(1, "view@example.org", platform="android")
    rig.service.deliver("alerts", "MAYDAY", "heard: “John Smith is down”, radio 4821", 5,
                        {"talker": "radio 4821", "incident": "26-000015", "address": "100 S Ridge St"})
    msg = fake.published()[0]["message"]["GCM"]["fcmV1Message"]["message"]
    assert msg["data"] == {"kind": "alerts", "id": "26-000015", "title": "MAYDAY · 26-000015 100 S Ridge St",
                           "body": "100 S Ridge St"}
    assert_clean(fake)
