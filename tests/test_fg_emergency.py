"""The emergency button on the analog fireground channels (#128): an MDC-1200 (Motorola) or FleetSync (Kenwood)
emergency packet raises the alarm at the key-up through the live tap (radio/tap.py), again (the same alarm) from
the finished transmission, and from the finished transmission alone when the tap missed it."""

import time

import numpy as np
import pytest

from sizeup.radio import fleetsync as fs
from sizeup.radio.fm import RATE
from sizeup.radio.mdc1200 import encode
from tests.test_app import FakeWhisper, cfg, wait
from tests.test_livetap import Rig, hiss, keyed, quiet, voice


@pytest.fixture
def app(tmp_path):
    from sizeup.app import App
    a = App(cfg(tmp_path))
    a._kind = {"FG1": "traffic"}
    a.stt.client = FakeWhisper({k: "Engine 64 mayday mayday" for k in range(1, 9)})
    a.stt.start()
    yield a
    a.stt.stop() if hasattr(a.stt, "stop") else None


def rig(app):
    r = Rig()
    r.live, r.tap = app.live, app.tap
    r.clock.t = time.time() - 20          # the alarm's age is checked against the wall clock
    return r


def alarms(app):
    return app.cad.state()["alarms"]


def press(r, app, x):
    """Feed until an alarm is open; the clock then, or None."""
    return r.feed(x, until=lambda _r: bool(alarms(app)["open"]))


def finish(r, app, x, seen):
    r.feed(x[int((seen - r.t0) * RATE):] if seen else x)
    for seg in r.segs:
        app._segment(r.ch, seg)


def carrier_with(burst, talk=2.0):
    return np.concatenate([hiss(3.0), keyed(np.concatenate([burst, voice(talk) if talk else quiet(0.3)])), hiss(1.0)])


def test_mdc_emergency_alarms_at_the_key_up_and_the_transmission_joins_it(app):
    r = rig(app)
    r.t0 = r.clock.t
    x = carrier_with(np.concatenate([encode(0x00, 0x80, 0x6401), quiet(0.05), encode(0x00, 0x80, 0x6401)]))
    seen = press(r, app, x)
    assert seen is not None and r.sq.open and not r.segs                     # the carrier is still up
    assert seen - (r.t0 + 3.0) < 1.0                                         # under a second from the key-up
    [a] = alarms(app)["open"]
    assert (a["what"], a["radio_id"], a["rig"], a["channel"], a["level"]) == ("emergency button (MDC)", "6401", "E64",
                                                                              "FG1", "alarm")
    assert a["talker"]["units"][0] == ("E64", "Engine 64") and a["traffic_id"].startswith("FG1-live-")
    finish(r, app, x, seen)
    assert len(r.segs) == 1
    assert wait(lambda: alarms(app)["open"][0]["text"] == "Engine 64 mayday mayday", 10)
    [a] = alarms(app)["open"]                                                # one alarm for the transmission
    assert a["traffic_id"] == next(t.id for t in app.store.loose) and a["has_clip"]
    assert a["what"] == "emergency button (MDC) + mayday"


def test_the_finished_transmission_alarms_when_the_tap_missed_it(app):
    r = rig(app)
    r.t0 = r.clock.t
    app.tap.mdc = lambda a: []                    # the live decode found nothing (a burst it couldn't read)
    x = carrier_with(encode(0x00, 0x80, 0x6401))
    r.feed(x)
    assert not alarms(app)["open"]
    for seg in r.segs:
        app._segment(r.ch, seg)
    [a] = alarms(app)["open"]
    assert a["what"].startswith("emergency button (MDC)") and a["traffic_id"] == next(t.id for t in app.store.loose)


def test_a_button_burst_with_no_voice_still_alarms_and_an_unknown_radio_shows_raw(app):
    r = rig(app)
    r.t0 = r.clock.t
    x = carrier_with(encode(0x00, 0x81, 0x7A7A), talk=0)
    seen = press(r, app, x)
    assert seen is not None
    [a] = alarms(app)["open"]
    assert (a["radio_id"], a["rig"], a["who"]) == ("7A7A", "", "7A7A")
    finish(r, app, x, seen)
    assert len(alarms(app)["open"]) == 1


def test_the_same_radio_keying_its_emergency_again_is_the_same_alarm(app):
    r = rig(app)
    r.t0 = r.clock.t
    one = carrier_with(encode(0x00, 0x80, 0x6401), talk=0.5)
    r.feed(one)
    r.feed(one)
    for seg in r.segs:
        app._segment(r.ch, seg)
    assert len(r.segs) == 2 and len(alarms(app)["open"]) == 1


def test_an_emergency_acknowledge_or_a_plain_id_is_no_alarm(app):
    app.stt.client = FakeWhisper({k: "Engine 63 on location" for k in range(1, 9)})
    r = rig(app)
    r.t0 = r.clock.t
    r.feed(carrier_with(encode(0x20, 0x00, 0x6401)))
    r.feed(carrier_with(encode(0x01, 0x80, 0x6301)))
    for seg in r.segs:
        app._segment(r.ch, seg)
    assert wait(lambda: len([t for t in app.store.loose if t.status == "done"]) == 2, 10)
    assert not alarms(app)["open"] and not alarms(app)["notices"]


def test_fleetsync_emergency_alarms_at_the_key_up_raw_then_named_by_the_registry(app):
    r = rig(app)
    r.t0 = r.clock.t
    x = carrier_with(fs.encode(167, 1002, "emergency", 1200))
    seen = press(r, app, x)
    assert seen is not None and r.sq.open
    [a] = alarms(app)["open"]
    assert (a["what"], a["radio_id"], a["who"], a["channel"]) == ("emergency button (FleetSync)", "167-1002", "167-1002", "FG1")
    finish(r, app, x, seen)
    assert len(alarms(app)["open"]) == 1
    row = app.radios.get("fleetsync", "167-1002")                       # heard: in the registry, unmapped
    assert row and not row["unit"]
    # the department maps it (Settings > Radios > Radio IDs): named from then on
    app.radios.save({"system": "fleetsync", "radio_id": "167-1002", "unit": "E64", "label": "TK-5220 portable"}, "Corey")
    assert app.radios.rig_mdc(["167-1002"]) == "E64"


def test_fleetsync_ptt_id_names_the_talker_on_fg1(app):
    app.stt.client = FakeWhisper({k: "Engine 64 on location" for k in range(1, 9)})
    r = rig(app)
    r.t0 = r.clock.t
    x = carrier_with(fs.encode(330, 3004, "pre-id", 2400))
    seen = r.feed(x, until=lambda _r: any(e["radio_id"] == "330-3004" for e in app.live.state()))
    assert seen is not None and r.sq.open                                   # shown at the key-up
    r.feed(x[int((seen - r.t0) * RATE):])
    app._segment(r.ch, r.segs[0])
    assert wait(lambda: any(t.status == "done" for t in app.store.loose), 10)
    t = app.store.loose[-1]
    assert t.radio_id == "330-3004" and not alarms(app)["open"]


def test_a_station_sends_the_key_up_alarm_to_the_hub_at_once(app):
    sent = []
    app.forwarding = lambda: True
    app.events = lambda kind, data, clip: sent.append((kind, data, clip))
    r = rig(app)
    r.t0 = r.clock.t
    x = carrier_with(encode(0x00, 0x80, 0x6401))
    seen = r.feed(x, until=lambda _r: any(k == "alarm" for k, _d, _c in sent))
    assert seen is not None and r.sq.open
    kind, d, clip = next(s for s in sent if s[0] == "alarm")
    assert d["id"].startswith("FG1-live-") and d["emergency"] and d["emergency_via"] == "MDC" and clip is None
    assert not alarms(app)["open"]                                         # the station raises nothing itself
    r.feed(x[int((seen - r.t0) * RATE):])
    app._segment(r.ch, r.segs[0])
    later = [s for s in sent if s[0] == "alarm"][-1]
    assert later[1]["alarm_key"] == d["id"] and later[2] == later[1]["id"]  # the transmission, with its clip


def test_the_hub_keeps_one_alarm_from_the_key_up_to_the_transmission(tmp_path):
    import base64

    from sizeup.cad.dispatcher import wav_bytes
    from tests.test_cloud import HubRun, call, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        at = time.time() - 5
        live = {"id": f"FG1-live-{int(at * 1000)}", "source": "FG1", "start": at, "end": at, "text": "",
                "status": "pending", "unit": "6401", "talkgroup": None, "rig": "", "radio_id": "6401", "emergency": True,
                "emergency_via": "MDC"}
        code, out = call(hub.url + "/api/station/events", "POST",
                         {"epoch": "e", "events": [{"seq": 1, "at": at, "type": "alarm", "data": live}]})
        assert code == 200 and out["applied"] == 1
        [a] = hub.get("/api/cad/state")["alarms"]["open"]
        assert (a["what"], a["rig"]) == ("emergency button (MDC)", "E64")          # the hub's registry names it
        wav = base64.b64encode(wav_bytes(np.zeros(8000, np.float32), 8000)).decode()
        tr = dict(live, id=f"FG1-{int(at * 1000) + 120}", end=at + 3, alarm_key=live["id"], text="mayday mayday",
                  raw="mayday mayday", status="done", model="m.bin", attach=True)
        code, out = call(hub.url + "/api/station/events", "POST",
                         {"epoch": "e", "events": [{"seq": 2, "at": at, "type": "traffic", "data": tr, "audio": wav}]})
        assert code == 200
        [a] = hub.get("/api/cad/state")["alarms"]["open"]
        assert a["traffic_id"] == tr["id"] and a["has_clip"] and a["what"] == "emergency button (MDC) + mayday"
    finally:
        hub.close()


def test_the_push_says_emergency_button_who_and_where(app, tmp_path):
    from sizeup.notify import Notifier
    from tests.test_alarms import FakeNtfy
    ntfy = FakeNtfy()
    try:
        app.cad.alarms.notifier = Notifier(ntfy.url, "sizeup-test-alarms-0123", lambda m: None, sleep=lambda s: None)
        r = rig(app)
        r.t0 = r.clock.t
        r.feed(carrier_with(encode(0x00, 0x80, 0x6401), talk=0.5))
        assert app.cad.alarms.notifier.flush(5) and len(ntfy.got) == 1
        m = ntfy.got[0]
        assert m["title"].startswith("EMERGENCY BUTTON (MDC) · E64 (Engine 64)") and "on FG1" in m["title"]
        assert m["priority"] == 5 and "radio 6401" in m["title"] + m["message"]
    finally:
        ntfy.close()
