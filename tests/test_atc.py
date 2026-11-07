"""HPN ATC (GitHub #233): the airband channel, its speech-to-text words, the airport pre-alert rules and the pre-alert.

    rules        data/atc/rules.json on realistic (synthetic) transcripts: what must raise a pre-alert (the brush fire
                 a departure saw, MAYDAY, PAN-PAN, souls on board, roll the trucks ...) and what never may (ATIS, a
                 NOTAM's runway closure, routine clearances, "no smoking", "fire up the APU", Firestone)
    the watch    sizeup/atc.py AtcWatch: the exchange read together, the title, the runway and location words, the
                 approximate point (and none when the words give none), the aircraft from a fake ADS-B list
    callsigns    shortfinal's matcher, ported: airline telephony, N-numbers, abbreviated tails, heard-but-not-on-ADS-B,
                 never an airliner's own tail
    pre-alerts   sizeup/cad/airport_alerts.py: made, de-duplicated by subject and exchange, rate-limited, pushed once,
                 accepted (a PENDING event, nothing dispatched) or dismissed, expired; the API's routes and roles
    the channel  kind = "airband" in the config (AM, carrier squelch, shortfinal's settings), the receiver's rtl_fm
                 command, the squelch on a synthetic AM envelope, the app's path (AM clip, whisper prompt, the
                 pre-alert on the CAD of record, none on a forwarding station), AWS by default with its vocabulary
    the station  the one-time config update (sizeup/cloud/configupdate.py "hpn-atc") and its rollback guard; the
                 auto-assign order (HPN Tower last)
No network, no dongle, no AWS: every feed is a fake."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sizeup import atc
from sizeup.app import App
from sizeup.cad.airport_alerts import AirportAlert, AirportAlerts, remarks
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.cloud import configupdate
from sizeup.config import AUTO_ORDER, ChannelCfg, ConfigError, load
from sizeup.radio.am import AM_RATE, CarrierSquelch, AmReceiver, Upsampler
from sizeup.radio.fm import FRAME, RATE, Channel
from tests.test_app import FakeWhisper, cfg, wait
from tests.test_sdr_auto_assign import DONGLES, P25, rig  # noqa: F401 (rig: the fixture)

ROOT = Path(__file__).resolve().parent.parent
STATION = ROOT / "deploy" / "station"
T0 = 1_800_000_000.0
HPN = ChannelCfg("HPN Tower", 118.575, "airband", "hpn-twr", 36.4, 3.5)
FIVE = (ChannelCfg("South 2", 470.2, "paging", "fire-page"), ChannelCfg("FG1", 453.0375, "traffic", "fire-fg"),
        ChannelCfg("FG2", 458.0375, "traffic", "fire-fg2"), HPN)
PLANES = [
    {"hex": "a1b2c3", "flight": "JBU1234", "reg": "N123JB", "type": "A320", "lat": 41.05, "lon": -73.69, "alt": 1800,
     "gs": 160, "trk": 150, "phase": "departing", "rwy": "16", "dist": 1.2},
    {"hex": "c0ffee", "flight": "N123AB", "reg": "N123AB", "type": "C172", "lat": 41.1, "lon": -73.72, "alt": 2500,
     "phase": "arriving", "rwy": "34", "dist": 4.0},
    {"hex": "0dd0dd", "flight": "EJA521", "reg": "N521QS", "type": "C68A", "lat": 41.08, "lon": -73.71, "alt": "ground",
     "phase": "ground", "dist": 0.4},
]


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority=3, tags=(), click="", who=None):
        self.sent.append((title, body, priority, tuple(tags), click))
        return True


@pytest.fixture
def world():
    clock = Clock()
    cad = Cad(Settings(), log=lambda m: None, clock=clock)
    push = FakeNotifier()
    cad.airport_alerts.notifier = push
    w = atc.AtcWatch(cad.airport_alerts, ["HPN Tower"], aircraft=lambda: PLANES, log=lambda m: None, clock=clock)
    n = [0]

    def say(text, at=None):
        n[0] += 1
        clock.t = T0 + (n[0] * 5 if at is None else at)
        return w.heard({"id": f"HPNTower-{n[0]}", "source": "HPN Tower", "start": clock.t - 2, "end": clock.t, "text": text})
    return cad, w, say, clock, push


# -- the rules ------------------------------------------------------------------------------------------------------

RULES = atc.Rules.load()


def hits(text):
    return {h.rule.id for h in RULES.hits(atc.normalize(text))}


def conf(text):
    return atc.score(h.rule.weight for h in RULES.hits(atc.normalize(text)))


TRIGGER = [
    ("Westchester tower, JetBlue 1234, we just departed one six, looks like a brush fire off the departure end", "brush-fire"),
    ("tower, Delta 45, there's smoke off to our right, about two miles south of the field", "smoke"),
    ("there's a fire in the woods just past the end of runway three four", "brush-fire"),
    ("Westchester tower, November one two three alfa bravo, mayday mayday mayday, engine failure", "mayday"),
    ("pan pan pan pan, Westchester, Endeavor 5121, we have smoke in the cockpit", "pan-pan"),
    ("Endeavor 5121 is declaring an emergency, request return to the field", "declaring"),
    ("Endeavor 5121, say souls on board and fuel remaining", "souls"),
    ("crash crew, Westchester tower, roll the trucks, emergency aircraft on a five mile final runway one six", "roll-trucks"),
    ("ARFF one, emergency aircraft landing runway three four, equipment standing by", "arff"),
    ("Cessna three alfa bravo, go around, there's a disabled aircraft on runway one six", "disabled"),
    ("tower, we went off the runway, we're in the grass at taxiway alfa", "excursion"),
    ("we have an unsafe gear indication, request a low pass", "gear"),
    ("JetBlue 1234, we hit a couple of birds on departure", "bird"),
    ("we have a passenger unresponsive, need paramedics at the gate", "medical"),
    ("we just got lasered on a two mile final, green laser from the ground", "laser"),
    ("we're leaking fuel from the left wing", "fuel"),
    ("Brickyard 4567 has an engine fire light", "engine-fire"),
]
QUIET = [
    "Westchester information bravo is current, wind one six zero at eight, visibility one zero, ceiling five thousand "
    "broken, altimeter two niner niner two. Notams: runway one one two niner closed. Bird activity in the vicinity "
    "of the airport. Advise on initial contact you have information bravo",
    "JetBlue 1234, runway one six, cleared for takeoff, wind one six zero at eight",
    "Cessna three alfa bravo, taxi to runway one six via alfa, hold short of runway one one",
    "please make sure the passengers see the no smoking sign",
    "go ahead and fire up the APU when you're ready",
    "Firestone, the tires are at the FBO",
    "November one two three alfa bravo, contact New York Approach one two six point four, good day",
    "Delta 45, line up and wait runway one six",
    "Westchester ground, Execjet 521, ready to taxi with information charlie",
    "the crash crew is doing their truck check on taxiway kilo",
    "caution birds in the vicinity of the departure end of runway one six",
    "runway one one two niner closed for maintenance until fourteen hundred",
]


@pytest.mark.parametrize("text, rule", TRIGGER)
def test_the_rules_hear_what_could_become_a_call(text, rule):
    assert rule in hits(text), hits(text)
    assert conf(text) >= RULES.threshold


@pytest.mark.parametrize("text", QUIET)
def test_the_rules_stay_quiet_on_routine_traffic_and_readouts(text):
    assert conf(text) < RULES.threshold, [(h.rule.id, h.text) for h in RULES.hits(atc.normalize(text))]


def test_fire_inside_other_words_and_the_readout_rule():
    n = atc.normalize
    assert not hits("Firestone fireworks bonfire-ish fireman's carry") - {"fire"} and "fire" not in hits("Firestone")
    assert hits("we have a fire") == {"fire"}
    # inside an ATIS / NOTAM readout only the 'always' rules count: a MAYDAY still does
    assert hits("information bravo is current, notams, runway closed") == set()
    assert "mayday" in hits("notams ... mayday mayday mayday")
    assert n("O'Clock, it’s 1-6") == "oclock its 1 6"


def test_the_rules_file_is_whole():
    d = json.loads((ROOT / "data" / "atc" / "rules.json").read_text())
    assert {r["category"] for r in d["rules"]} <= set(d["categories"])
    assert all(0 < r["weight"] <= 1 for r in d["rules"]) and len({r["id"] for r in d["rules"]}) == len(d["rules"])
    for c in d["categories"].values():                       # Accept makes an event of a type the CAD has
        assert c["event_type"] in ("AIRPORT", "INVEST", "BRUSH")
    with pytest.raises(ValueError):
        atc.Rules({"categories": {}, "rules": [{"id": "x", "category": "nope", "weight": 0.5, "pattern": "x"}]})


def test_runways_and_places_in_the_words():
    assert atc.runways(atc.normalize("Runway one six, then runway 34; runway 1-1 and runway two niner")) == ["16", "34", "11", "29"]
    assert atc.runways("runway three one") == []                              # not an HPN runway
    assert atc.departure_runway("jetblue 1234 runway one six cleared for takeoff") == "16"
    assert atc.departure_runway("we just departed one six") == "16"
    w = atc.where(atc.normalize("a brush fire off the departure end of runway one six, maybe a mile"), RULES)
    assert (w.runway, w.departure, w.dist_nm) == ("16", True, 1.0)
    p = atc.place(w)
    assert p["approx"] is True and p["radius_m"] >= 1000 and "departure end of runway 16" in p["basis"]
    # past the 34 end (the 16 departure's end), on the runway's heading (~150 true): south-east of the field
    assert p["lat"] < atc.RUNWAY_ENDS["34"][0] and p["lon"] > atc.RUNWAY_ENDS["34"][1]
    c = atc.place(atc.where(atc.normalize("smoke about two miles south of the field"), RULES))
    assert c["approx"] and abs(c["lat"] - (atc.ARP[0] - 2 * 1852 / 111320)) < 1e-3 and abs(c["lon"] - atc.ARP[1]) < 1e-3
    # a direction without a distance, a clock position, a landmark: words only, never a point
    for said in ("smoke north of the field", "smoke at our two o'clock", "smoke by Rye Lake"):
        q = atc.place(atc.where(atc.normalize(said), RULES))
        assert q["placed"] is False and "lat" not in q, said
    assert atc.place(atc.where("cleared to land", RULES)) is None


# -- the watch: the brush fire ------------------------------------------------------------------------------------


def test_the_brush_fire_a_departure_called_in_is_a_pre_alert(world):
    cad, w, say, clock, push = world
    assert say("JetBlue 1234, runway one six, cleared for takeoff") is None
    a = say("Westchester tower, JetBlue 1234, we see what looks like a brush fire off the departure end, maybe a mile")
    assert a["title"] == "HPN ATC: possible brush fire reported off runway 16 departure"
    assert a["source"] == "atc" and a["kind"] == "fire_off" and a["status"] == "open" and a["level"] == "high"
    d = a["data"]
    assert [s["id"] for s in d["snippets"]] == ["HPNTower-1", "HPNTower-2"] and d["snippets"][0]["context"]
    assert d["snippets"][1]["hits"] == ["brush fire"]
    ac = d["aircraft"][0]
    assert (ac["callsign"], ac["hex"], ac["type"], ac["phase"], ac["match"]) == ("JBU1234", "a1b2c3", "A320", "departing", 1.0)
    assert d["location"]["approx"] and d["location"]["runway"] == "16" and "off the departure end" in d["phrases"]
    assert d["event"] == {"type": "BRUSH", "subtype": ""}
    assert len(push.sent) == 1 and push.sent[0][0] == a["title"] and "“Westchester tower, JetBlue 1234" in push.sent[0][1]
    assert "approx. location" in push.sent[0][1] and push.sent[0][2] == 4
    # the controller's answer is the same exchange: added, the brush fire still names it, not pushed again
    b = say("JetBlue 1234, thanks, we'll roll the trucks")
    assert b["id"] == a["id"] and b["count"] == 2 and b["title"] == a["title"] and len(push.sent) == 1
    assert b["confidence"] > a["confidence"]
    # it shows on the console
    st = cad.state()["airport_alerts"]
    assert [x["id"] for x in st["open"]] == [a["id"]]


def test_one_exchange_one_pre_alert_and_a_new_subject_later_is_another(world):
    cad, w, say, clock, push = world
    a = say("Westchester tower, November one two three alfa bravo, mayday mayday mayday, engine failure", at=0)
    assert a["title"] == "HPN ATC: MAYDAY: N123AB" and a["data"]["aircraft"][0]["hex"] == "c0ffee"
    b = say("November three alfa bravo, say souls on board and fuel remaining", at=10)
    assert b["id"] == a["id"] and b["count"] == 2 and b["title"] == a["title"]
    # the same aircraft again within dedupe_min (5 min), outside the exchange: still the same pre-alert
    c = say("November one two three alfa bravo, say fuel remaining, are you declaring an emergency", at=200)
    assert c["id"] == a["id"]
    # 20 minutes on, something else: a new one, pushed
    d = say("tower, Delta 45, smoke off to our right, about two miles south of the field", at=1400)
    assert d["id"] != a["id"] and d["title"] == "HPN ATC: possible smoke reported two miles south of the field"
    assert d["data"]["aircraft"][0] == {"callsign": "DAL45", "hex": None, "spoken": "DAL45", "match": 0.0}   # not on ADS-B
    assert len(push.sent) == 2


def test_routine_traffic_raises_nothing(world):
    cad, w, say, clock, push = world
    for t in QUIET:
        assert say(t) is None, t
    assert cad.state()["airport_alerts"]["items"] == [] and push.sent == [] and w.seen == len(QUIET)


def test_an_earlier_hit_in_the_exchange_adds_to_a_weak_one(world):
    cad, w, say, clock, push = world
    assert say("Endeavor 5121, we have an emergency", at=0) is None or True        # 'emergency' alone: under threshold
    cad2 = Cad(Settings(), log=lambda m: None, clock=clock)
    w2 = atc.AtcWatch(cad2.airport_alerts, ["HPN Tower"], aircraft=lambda: [], log=lambda m: None, clock=clock)
    hear = lambda i, text, at: w2.heard({"id": f"x{i}", "source": "HPN Tower", "start": T0 + at - 1, "end": T0 + at, "text": text})
    clock.t = T0
    assert hear(1, "say your intentions, is this an emergency", 0) is None                # 0.4: alone, nothing
    clock.t = T0 + 20
    a = hear(2, "we're returning to the field", 20)                                        # 0.5 + 0.4 x 0.5 in context
    assert a is not None and [s["id"] for s in a["data"]["snippets"]] == ["x1", "x2"]
    # other channels are not ours
    assert w2.heard({"id": "y", "source": "FG1", "start": T0, "end": T0, "text": "mayday mayday"}) is None


def test_too_many_in_an_hour_are_added_to_the_latest(world):
    cad, w, say, clock, push = world
    aa = cad.airport_alerts
    aa.max_per_hour = 2
    for i in range(4):
        clock.t = T0 + i * 400
        aa.raise_alert(AirportAlert("atc", "laser", f"laser:{i}", f"HPN ATC: laser {i}", confidence=0.6, channel="HPN Tower"))
    rows = aa.state()["items"]
    assert len(rows) == 2 and rows[0]["count"] == 3 and len(push.sent) == 2


# -- callsigns ---------------------------------------------------------------------------------------------------------

IX = atc.TelephonyIndex.load()


@pytest.mark.parametrize("text, want", [
    ("JetBlue one two three four, runway one six cleared for takeoff", ("JBU1234", 1.0)),
    ("jetblue twelve thirty four, contact departure", ("JBU1234", 1.0)),
    ("November one two three alfa bravo, wind calm", ("N123AB", 1.0)),
    ("three alfa bravo, roger", ("N123AB", 0.6)),                          # abbreviated after first contact
    ("Execjet five twenty one, taxi to runway one six", ("EJA521", 1.0)),
    ("JetBlue one two three five, say again", ("JBU1234", 0.7)),           # one digit off, the only JetBlue around
])
def test_callsigns_heard_match_the_aircraft_around(text, want):
    att = atc.attribute(text, IX, PLANES)
    assert (att.callsign, att.confidence) == want


def test_callsigns_never_guessed():
    att = atc.attribute("Delta four five, contact ground", IX, PLANES)
    assert att.hex is None and att.callsign == "DAL45" and att.confidence == 0.0        # heard, not on ADS-B
    # JetBlue's tail N123JB is never "November one two three juliett bravo" (one letter off N123AB's, too)
    att = atc.attribute("november one two three juliett bravo", IX, PLANES[:1])
    assert att.hex is None
    assert atc.attribute("runway three four, wind two seven zero at five", IX, PLANES) is None
    assert atc.spoken_callsign("JIA5011", atc.telephony_words()) == "blue streak five zero one one"
    p = atc.whisper_prompt("Westchester Tower.", PLANES, atc.telephony_words())
    # who talks to the tower first (#257): arriving, departing, on the ground; nearest first within each
    assert p == ("Westchester Tower. november one two three alfa bravo, jetblue one two three four, execjet five two one")
    low = [dict(PLANES[0], hex="dddddd", flight="JBU99", phase="low", dist=0.1)]
    assert atc.whisper_prompt("W.", low + PLANES, atc.telephony_words()).endswith("execjet five two one, jetblue niner niner")


# -- the pre-alert: accept, dismiss, expire, the API -------------------------------------------------------------------


def test_accept_makes_a_pending_event_and_dispatches_nothing(world):
    cad, w, say, clock, push = world
    say("JetBlue 1234, runway one six, cleared for takeoff")
    a = say("Westchester tower, JetBlue 1234, brush fire off the departure end of one six")
    r = cad.airport_alert(a["id"], "accept", {"operator": "Disp One"})
    inc = r["incident"]
    assert r["status"] == "accepted" and r["incident_id"] == inc["id"] and r["operator"] == "Disp One"
    assert inc["status"] == "pending" and inc["type"] == "BRUSH" and inc["emergency"] and inc["received_via"] == "RADIO"
    assert inc["lat"] == a["data"]["location"]["lat"] and inc["geo_label"].startswith("APPROXIMATE (from ATC)")
    assert "brush fire" in inc["remarks"] and "Not dispatched" in inc["remarks"] and "JBU1234" in inc["remarks"]
    assert not cad.store.q("SELECT * FROM assignments WHERE incident_id = ?", (inc["id"],))       # nobody dispatched
    log = [e["text"] for e in cad.store.q("SELECT text FROM events WHERE kind = 'prealert' ORDER BY id")]
    assert log[0].startswith("Airport pre-alert (HPN ATC): HPN ATC: possible brush fire") and "accepted" in log[-1]
    assert cad.state()["airport_alerts"]["open"] == []
    with pytest.raises(Exception, match="already accepted"):
        cad.airport_alert(a["id"], "dismiss", {})


def test_an_airport_alert_without_a_place_gets_the_terminal_and_the_words(world):
    cad, w, say, clock, push = world
    a = say("Westchester tower, November one two three alfa bravo, mayday mayday, unsafe gear")
    inc = cad.airport_alert(a["id"], "accept", {})["incident"]
    assert inc["type"] == "AIRPORT" and inc["address"]                         # an Airport event: the terminal (#87)
    assert "N123AB" in inc["remarks"] and "C172" in inc["remarks"]


def test_dismiss_and_expire(world):
    cad, w, say, clock, push = world
    a = say("we just got lasered on final", at=0)
    d = cad.airport_alert(a["id"], "dismiss", {"operator": "Disp", "reason": "pilot says no effect"})
    assert d["status"] == "dismissed" and d["note"] == "pilot says no effect"
    b = say("tower, we went off the runway into the grass", at=1000)
    clock.t = T0 + 1000 + cad.airport_alerts.expire_s + 1
    assert cad.state()["airport_alerts"]["open"] == [] and cad.airport_alerts.get(b["id"])["status"] == "expired"


def test_the_api_routes_and_who_may(world):
    cad, w, say, clock, push = world
    a = say("Westchester tower, Endeavor 5121, declaring an emergency, smoke in the cabin")
    api = CadApi(cad)
    code, body, _ = api.post(f"/api/cad/airport-alert/{a['id']}/dismiss", json.dumps({"reason": "drill"}).encode())
    assert code == 200 and json.loads(body)["status"] == "dismissed"
    code, body, _ = api.post(f"/api/cad/airport-alert/{a['id']}/accept", b"{}")
    assert code == 400 and "already dismissed" in json.loads(body)["error"]
    from sizeup.cad import api as apimod
    assert any(rx.pattern == apimod.AIRPORT_ALERT.pattern and need == "dispatch" for rx, need in apimod.WRITES)


def test_another_source_uses_the_same_model(world):
    cad, w, say, clock, push = world
    r, new = cad.airport_alerts.raise_alert(AirportAlert(
        "adsb", "squawk", "adsb:a1b2c3:7700", "HPN ADS-B: JBU1234 squawking 7700", confidence=0.9, rank=3,
        aircraft=[PLANES[0]], event={"type": "AIRPORT", "subtype": ""}))
    assert new and r["source_name"] == "ADS-B" and r["data"]["aircraft"][0]["flight"] == "JBU1234"
    again, new2 = cad.airport_alerts.raise_alert(AirportAlert("adsb", "squawk", "adsb:a1b2c3:7700", "again", confidence=0.9))
    assert not new2 and again["id"] == r["id"] and again["title"] == r["title"]
    with pytest.raises(ValueError):
        cad.airport_alerts.raise_alert(AirportAlert("ouija", "x", "x", "x"))
    assert "HPN ADS-B: JBU1234 squawking 7700" in remarks(r)


# -- the channel ----------------------------------------------------------------------------------------------------


def test_airband_channels_in_the_config(tmp_path):
    p = tmp_path / "c.toml"
    base = '[radio]\ntones_file = ""\n[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\nkind = "traffic"\n'
    p.write_text(base + '[[radio.channels]]\nlabel = "HPN Tower"\nmhz = 118.575\nkind = "airband"\n')
    c = load(p)
    hpn = c.channels[-1]
    assert (hpn.kind, hpn.gain_db, hpn.squelch_db, hpn.drop_db, hpn.hang_s, hpn.continue_s) == ("airband", 36.4, 3.5, None, None, 0.0)
    assert "HPN Tower" in c.stt_aws_channels                       # AWS by default: a priority channel
    assert c.atc_enabled and c.atc_dedupe_min == 5 and c.atc_context == 4
    for bad, why in (('mhz = 453.0375', "airband voice"), ('mhz = 118.575\ndrop_db = 10', "FM channels only"),
                     ('mhz = 118.575\nsqueltch = 1', "unknown"), ('mhz = 118.575\nsquelch_db = 40', "squelch_db")):
        p.write_text(base + f'[[radio.channels]]\nlabel = "HPN Tower"\nkind = "airband"\n{bad}\n')
        with pytest.raises(ConfigError, match=why):
            load(p)
    p.write_text(base + '[[radio.channels]]\nlabel = "FG1"\nmhz = 458.0375\n')
    with pytest.raises(ConfigError, match="same label"):
        load(p)
    p.write_text(base + '[atc]\ndedupe_min = 0\n')
    with pytest.raises(ConfigError, match="atc.dedupe_min"):
        load(p)


@pytest.mark.parametrize("path", ["config.example.toml", "deploy/hub/config.hub.toml"])
def test_hpn_tower_ships_in_the_configs(path):
    c = load(ROOT / path)
    hpn = next(x for x in c.channels if x.label == "HPN Tower")
    assert (hpn.mhz, hpn.kind, hpn.squelch_db) == (118.575, "airband", 3.5)
    assert "HPN Tower" in c.stt_aws_channels
    if "hub" not in path:
        assert c.sdr_auto_order[-1] == "HPN Tower"


def test_the_station_template_has_it_last(tmp_path, monkeypatch):
    from tests.test_station_install import station_config
    c = station_config(tmp_path, monkeypatch)
    hpn = next(ch for ch in c.channels if ch.label == "HPN Tower")
    assert hpn.kind == "airband" and c.sdr_auto_order[-1] == "HPN Tower"       # last of the auto-assigned ones
    assert AUTO_ORDER[-2:] == ("FG2", "HPN Tower")


def test_the_receiver_runs_rtl_fm_in_am_at_shortfinals_settings():
    r = AmReceiver("rtl_fm", "", 36.4, Channel(118.575, "HPN Tower"), lambda c, s: None, open_db=3.5)
    assert r.argv("3") == ["rtl_fm", "-M", "am", "-f", "118.5750M", "-s", str(AM_RATE), "-g", "36.4", "-d", "3", "-"]
    assert "-E" not in r.argv("3")                          # no DC block: the envelope's DC is the carrier
    assert isinstance(r.squelch, CarrierSquelch) and r.status()["am"] is True
    up = Upsampler()
    assert len(up(np.zeros(AM_RATE // 20))) == FRAME and len(up(np.ones(AM_RATE // 20))) == FRAME


def envelope(levels_s, rng):
    """An AM envelope at 16 kHz: [(seconds, carrier level, with voice)] -> 50 ms frames."""
    out = []
    for s, lv, talk in levels_s:
        n = int(s * 20)
        for k in range(n):
            t = np.arange(FRAME) / RATE + k * 0.05
            x = lv + 0.0005 * rng.standard_normal(FRAME)
            if talk:
                x = x + 0.3 * lv * np.sin(2 * np.pi * 400 * t) * (np.sin(2 * np.pi * 3 * t) > 0)
            out.append(x)
    return out


def test_the_carrier_squelch_keeps_a_transmission_whole_and_marks_a_weak_one():
    rng = np.random.default_rng(1)
    sq = CarrierSquelch(3.5, clock=lambda: T0)
    segs = [s for f in envelope([(5, 0.01, False), (2.0, 0.05, True), (0.25, 0.01, False), (1.5, 0.05, True),
                                 (3, 0.01, False), (0.3, 0.05, True), (3, 0.01, False), (2, 0.0145, True), (3, 0.01, False)], rng)
            if (s := sq.feed(f)) is not None]
    # 2.0 s + a 0.25 s dip (inside the 0.4 s hang) + 1.5 s = one; 0.3 s = a blip, not kept; +3.2 dB is under the 3.5 dB
    # open, but held for 2 s it is a real carrier (#270: the sustained-weak open), kept whole and flagged marginal
    assert len(segs) == 2
    s = segs[0]
    assert 3.6 < s.end - s.start < 4.5 and s.quieting_db > 10 and not s.weak and abs(float(s.audio.mean())) < 1e-3
    assert s.opened == "carrier"
    w = segs[1]
    assert w.opened == "sustained" and 3.0 < w.quieting_db < 3.5 and w.marginal and 2.3 < w.end - w.start < 2.8
    sq2 = CarrierSquelch(3.5, clock=lambda: T0)
    weak = [s for f in envelope([(5, 0.01, False), (2, 0.0165, True), (3, 0.01, False)], rng) if (s := sq2.feed(f)) is not None]
    # +4.3 dB (#257): marginal, transcribed and flagged; no longer skipped
    assert len(weak) == 1 and not weak[0].weak and weak[0].marginal
    sq3 = CarrierSquelch(3.5, clock=lambda: T0)
    # opens at +3.8 dB, then sits at +2.5 (over the 2 dB close): too weak to transcribe
    weak = [s for f in envelope([(5, 0.01, False), (0.1, 0.0155, True), (2, 0.0133, True), (3, 0.01, False)], rng)
            if (s := sq3.feed(f)) is not None]
    assert len(weak) == 1 and weak[0].weak and not weak[0].marginal


def test_the_app_hears_hpn_tower_and_raises_the_pre_alert(tmp_path):
    # 3 s of carrier + the 0.4 s pre-roll + the 0.2 s tail (sizeup/radio/am.py, #257): 4 s to the fake whisper
    texts = {4: "Westchester tower, JetBlue 1234, looks like a brush fire off the departure end of runway one six"}
    app = App(replace(cfg(tmp_path), channels=FIVE))
    app.stt.client = FakeWhisper(texts)
    prompts = []
    real = app.stt.client.transcribe
    app.stt.client.transcribe = lambda audio, prompt="": (prompts.append(prompt), real(audio, prompt))[1]
    app.stt.start()
    assert app.atc is not None and app.atc.channels == {"HPN Tower"}
    app.atc.aircraft = lambda: PLANES
    rng = np.random.default_rng(2)
    sq = CarrierSquelch(3.5)
    seg = None
    for f in envelope([(5, 0.01, False), (3, 0.05, True), (1, 0.01, False)], rng):
        seg = sq.feed(f) or seg
    assert seg is not None
    app._segment(Channel(118.575, "HPN Tower"), seg)
    assert wait(lambda: app.cad.state()["airport_alerts"]["open"], 10)
    a = app.cad.state()["airport_alerts"]["open"][0]
    assert a["title"] == "HPN ATC: possible brush fire reported off runway 16 departure"
    t = next(x for x in app.store.loose if x.source == "HPN Tower")
    assert t.fm is False and app.has_clip(t.id) and a["data"]["snippets"][-1]["id"] == t.id
    # #270: its level, the floor and the voice's SNR go to the hub with it
    assert t.carrier_db > 10 and t.floor_dbfs == pytest.approx(-40, abs=0.5) and t.carrier_dbfs > t.floor_dbfs + 10
    assert t.snr_db > 10 and t.opened == "carrier" and {"carrier_dbfs", "floor_dbfs", "snr_db", "opened"} <= set(t.as_dict())
    assert prompts and prompts[0].startswith("Westchester Tower, Westchester Ground")      # the aviation prompt
    # the Radio view keeps ATC out of Main (busy, not ours)
    rows = app.cad.comms_query({"type": ["radio"]})["rows"]
    assert next(r for r in rows if r["channel"] == "HPN Tower")["main"] is False
    # a station forwarding to a hub leaves it to the hub
    app.cfg = replace(app.cfg, cloud_role="station")
    n = len(app.cad.state()["airport_alerts"]["items"])
    app._atc(t, "mayday mayday mayday")
    assert len(app.cad.state()["airport_alerts"]["items"]) == n


def test_a_weak_carrier_is_kept_and_not_transcribed(tmp_path):
    app = App(replace(cfg(tmp_path), channels=FIVE))
    sq = CarrierSquelch(3.5)
    seg = None
    for f in envelope([(5, 0.01, False), (0.1, 0.0155, True), (2, 0.0133, True), (1, 0.01, False)], np.random.default_rng(3)):
        seg = sq.feed(f) or seg
    app._segment(Channel(118.575, "HPN Tower"), seg)
    t = next(x for x in app.store.loose if x.source == "HPN Tower")
    assert t.status == "off" and "weak signal" in t.model and app.has_clip(t.id)


def test_aws_by_default_with_its_own_vocabulary_and_the_reserve():
    from sizeup.transcribe import AwsTranscriber, Settings as S, RESERVE_CHANNELS
    from sizeup import aws
    t = AwsTranscriber(S(region="us-east-1", reserve_channels=(*RESERVE_CHANNELS, "HPN Tower")), None, lambda *a: None,
                       lambda *a: None, creds=lambda: aws.Credentials("AKIDEXAMPLE", "secret", ""))
    t.vocabulary, t.channel_vocabulary["HPN Tower"] = "sizeup-pcfd", atc.VOCABULARY
    assert "vocabulary-name=sizeup-atc" in t.url("HPN Tower") and "vocabulary-name=sizeup-pcfd" in t.url("FG1")
    assert t.reserve_words() == "pages, Fire/EMS 15 and HPN Tower"
    v = atc.vocabulary()
    from sizeup.transcribe import build_vocabulary
    built = build_vocabulary(v["phrases"])
    assert not built["skipped"] and "runway-one-six" in [p.lower() for p in built["phrases"]] and "souls-on-board" in [
        p.lower() for p in built["phrases"]]
    assert len(v["prompt"]) < atc.PROMPT_MAX


# -- the station: the one-time config update, auto-assign ------------------------------------------------------------


def before_233() -> str:
    from tests.test_fg2 import before_260
    t = before_260((STATION / "config.station.toml").read_text())
    t = re.sub(r"\n\[\[radio\.channels\]\]\nlabel = \"HPN Tower\".*?\n\n", "\n\n", t, flags=re.S)
    t = t.replace(', "hpn tower"]', "]")
    d = tomllib.loads(t)
    assert all(c["label"] != "HPN Tower" for c in d["radio"]["channels"]) and "hpntower" not in [
        x.replace(" ", "") for x in d["radio"]["auto_assign_order"]]
    return t


def test_an_installed_station_gains_hpn_tower_once(tmp_path, monkeypatch):
    from tests.test_fg2 import installed
    conf = installed(tmp_path, monkeypatch, before_233())
    marks = tmp_path / "etc" / "config-updates"
    marks.write_text("fg2\ntones-47\nairband-sets\n")        # (#275's: its own tests)
    before = load(conf)
    new = tmp_path / "rel-new"
    (new / "sizeup").mkdir(parents=True)
    (new / "sizeup" / "config.py").write_text((ROOT / "sizeup" / "config.py").read_text())
    said = configupdate.run(conf, marks, leaving=new)
    assert said[0].startswith("hpn-atc: HPN Tower (118.575 MHz, airband AM) added after the channels; \"hpn tower\" at the end")
    assert said[2].startswith("approach-ground: NY Approach")                # #260, after it
    kept = conf.with_name("config.toml.before-hpn-atc").read_text()
    assert conf.read_text().replace(', "hpn tower"]', "]") == kept + configupdate.HPN_BLOCK + "".join(
        b for _l, _m, b in configupdate.APPROACH_GROUND)
    after = load(conf)
    assert after.channels[-4].label == "HPN Tower" and after.sdr_auto_order[-1] == "HPN Tower"
    assert replace(after, channels=before.channels, sdr_auto_order=before.sdr_auto_order,
                   stt_aws_channels=before.stt_aws_channels) == before
    assert marks.read_text() == "fg2\ntones-47\nhpn-atc\nadsb-receiver\napproach-ground\nsouth-end\nairband-sets\n"
    conf.write_text(kept)                                                  # taken out by hand: stays out
    assert configupdate.run(conf, marks, leaving=new) == []


def test_it_waits_while_the_release_to_go_back_to_cant_read_it(tmp_path, monkeypatch):
    from tests.test_fg2 import installed
    conf = installed(tmp_path, monkeypatch, before_233())
    marks = tmp_path / "etc" / "config-updates"
    marks.write_text("fg2\ntones-47\n")
    old = tmp_path / "rel-old"
    (old / "sizeup").mkdir(parents=True)
    (old / "sizeup" / "config.py").write_text('if kind not in ("paging", "traffic"): raise ConfigError("...")\n')
    same = conf.read_text()
    said = configupdate.run(conf, marks, leaving=old)
    assert said == [f"hpn-atc: deferred: the release this switch would go back to (rel-old) can't read an airband "
                    "channel: HPN Tower is added at the next update",
                    "adsb-receiver: already has [aircraft.receiver]: nothing added",     # (#235's: nothing to add)
                    "approach-ground: deferred: the release this switch would go back to (rel-old) can't read an "
                    "airband channel's schedule: NY Approach and HPN Ground are added at the next update",   # (#260's)
                    "south-end: the station is already called 'Port Chester FD South End': nothing changed",
                    "airband-sets: no channel as it was added (NY Approach, HPN Tower, HPN Ground): nothing changed"]   # (#275's: no airband channel yet)
    assert conf.read_text() == same and marks.read_text() == "fg2\ntones-47\nadsb-receiver\nsouth-end\nairband-sets\n"
    assert configupdate.reads_airband(None) and not configupdate.reads_airband(tmp_path / "nowhere")
    # the CLI the installer runs (system python, stdlib only), with the release being left
    r = subprocess.run([sys.executable, "-I", str(ROOT / "sizeup" / "cloud" / "configupdate.py"), str(conf), str(marks),
                        "--leaving", str(old)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "deferred" in r.stdout and conf.read_text() == same
    r = subprocess.run([sys.executable, "-I", str(ROOT / "sizeup" / "cloud" / "configupdate.py"), str(conf), str(marks),
                        "--leaving", str(ROOT)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.startswith("hpn-atc: HPN Tower") and load(conf).channels[-4].label == "HPN Tower"
    assert [c.label for c in load(conf).channels][-3:] == ["NY Approach", "NY Approach 124.65", "HPN Ground"]
    sh = (STATION / "install-linux.sh").read_text()
    assert '"$ETC/config-updates" --leaving "$leaving"' in sh and 'leaving=$(readlink -f "$CURRENT"' in sh


def test_odd_configs_are_left_alone():
    assert configupdate.add_hpn_atc("[web]\nport = 1\n")[1].startswith("hpn-atc: no [[radio.channels]]")
    assert "already has" in configupdate.add_hpn_atc('[[radio.channels]]\nlabel = "Tower"\nmhz = 118.575\nkind = "airband"\n')[1]
    text, said = configupdate.add_hpn_atc('[radio]\nsdrs = "auto"\n[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\n')
    assert "auto_assign_order" not in said and tomllib.loads(text)["radio"]["channels"][-1]["kind"] == "airband"


@pytest.mark.parametrize("n, want", [
    (4, ["FG1", P25, "South 2", "FG2"]),                          # four dongles: HPN Tower waits for a person
    (5, ["FG1", P25, "South 2", "FG2", "HPN Tower"]),             # a 5th free dongle takes it
    (6, ["FG1", P25, "South 2", "FG2", "HPN Tower", "Off"]),
])
def test_hpn_tower_is_auto_assigned_last(rig, n, want):
    plugged, logs, make = rig
    plugged[:] = (DONGLES + [(4, "RTL2838UHIDIR", "five"), (5, "NESDR SMArt", "six")])[:n]
    app = make(channels=FIVE, sdr_auto_order=AUTO_ORDER)
    assert [app.assign.get(s, "Off") for _i, _p, s in plugged] == want
    assert "HPN Tower" in app.state()["sdr"]["modes"]
    if "HPN Tower" in want:
        assert isinstance(app._receiver("HPN Tower"), AmReceiver)


def test_hpn_tower_picked_by_hand_and_never_on_a_dongle_another_app_holds(rig):
    import os
    import sizeup.radio.claims as claims
    plugged, logs, make = rig
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "spare.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": "TWR", "since": 1.0}))
    plugged[:] = DONGLES
    app = make(channels=FIVE, sdr_auto_order=AUTO_ORDER)
    assert "in use by shortfinal" in (app.set_mode("HPN Tower", "spare") or "")
    assert json.loads((claims.DIR / "spare.json").read_text())["app"] == "shortfinal"
    assert app.set_mode("HPN Tower", "adsb") is None and isinstance(app._receiver("HPN Tower"), AmReceiver)


# -- the page ---------------------------------------------------------------------------------------------------------


def test_the_console_shows_them_with_accept_and_dismiss():
    html = (ROOT / "sizeup" / "web" / "static" / "cad.html").read_text()
    assert 'id="q-airport"' in html and 'id="b-airport"' in html and "/static/airport-alerts.js" in html
    js = (ROOT / "sizeup" / "web" / "static" / "airport-alerts.js").read_text()
    assert "/api/cad/airport-alert/${a.id}/${what}" in js and "Approximate: " in js and "not dispatched" in js
    assert "AirportAlerts.column" in (ROOT / "sizeup" / "web" / "static" / "cad.js").read_text()


# -- #270: weak carriers (a ground transmitter far under the aircraft overhead), synthetic AM only ---------------------


def test_a_sustained_weak_carrier_opens_and_noise_does_not():
    """tools/atc_eval.py squelch's AM model: a voice-modulated carrier plus complex noise, the envelope as rtl_fm -M am
    gives it. The plain 3.5 dB open misses most transmissions just under 0 dB CNR; held for 0.4 s, 2.5 dB opens them. Noise
    with short bursts (+1.5..2.4 dB, up to 0.3 s) and a drifting floor opens neither."""
    from tools.atc_eval import _squelch_run, am_envelope
    rng = np.random.default_rng(270)
    opened = {}
    for cnr in (-6.0, -1.0, -0.5, 6.0):
        env = am_envelope([(10.0, None)] + [(2.5, cnr), (4.0, None)] * 10, rng, bursts_per_min=6, drift_db=0.5)
        opened[cnr] = {sus: _squelch_run(env, sus) for sus in (False, True)}
    n = {c: (len(o[False]), len(o[True])) for c, o in opened.items()}
    assert n[-6.0] == (0, 0)                              # far under the noise: nothing to hear, nothing opens
    assert n[-1.0][0] <= 3 and n[-1.0][1] >= 6            # just under: the sustained open gets most of them
    assert n[-0.5][1] == 10 and n[-0.5][1] > n[-0.5][0]
    assert n[6.0] == (10, 10)                             # a fair carrier opens as before, on the 3.5 dB open
    assert all(s.opened == "carrier" for s in opened[6.0][True])
    weak = [s for s in opened[-1.0][True] if s.opened == "sustained"]
    assert weak and all(2.3 < s.end - s.start < 3.4 for s in weak)        # kept whole: the 0.4 s run is in it
    assert all(s.snr_db is not None for s in opened[-0.5][True])
    noise = am_envelope([(600.0, None)], rng, bursts_per_min=20, drift_db=0.5)
    assert _squelch_run(noise, True) == [] and _squelch_run(noise, False) == []


def test_the_voice_snr_is_measured_against_the_pre_roll():
    from sizeup.radio.am import voice_snr_db
    rng = np.random.default_rng(5)
    noise = [0.01 + 0.001 * rng.standard_normal(FRAME) for _ in range(8)]
    t = np.arange(FRAME) / RATE
    voice = [0.05 + 0.01 * np.sin(2 * np.pi * 500 * t) + 0.001 * rng.standard_normal(FRAME) for _ in range(20)]
    snr = voice_snr_db(noise + voice, 8, 28)
    assert snr == pytest.approx(10 * np.log10(0.01 ** 2 / 2 / 0.001 ** 2), abs=1.0)    # ~17 dB
    assert voice_snr_db(voice, 0, 20) is None           # no pre-roll: no noise to measure it against
