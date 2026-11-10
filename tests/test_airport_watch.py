"""Airport alerts that build up over several transmissions (GitHub #369, sizeup/airport_watch.py).

Synthetic transcripts only (the repo is public): made-up callsigns, types, cars, runways and times, in the written form
the app hands the watch (sizeup/atc_normalize.py), shaped like what HPN Tower and 60 Control really say: a go-around, a
flap problem heard as "flat", a checklist, "a different airport", a frequency change read back without its leading 1,
the return "on the visual", and the airport car declaring and canceling an alert level on the fire radio.

    abnormal go-around   go-around + abnormal phrase -> a pre-alert; escalated on repeats; ADS-B's go-around boosts it
    never                a normal go-around, "flaps 20", "checklist complete" on departure, a routine hand-off
    hand-offs            "20.8" readback -> on NY App 120.8; kept open while away; back on the tower; landed -> ended
    60 Control           "alert level N" declared -> the prominent pre-alert; its readback; its cancel; attached to the
                         tower's pre-alert (either order); "alert" in other fire radio never counts
    per level            the department's: Alert 1 a standby pre-alert, never an incident; Alert 2 / 3 also a queued
                         Airport incident (both dispatch modes) the county's event or the page attaches to; an upgrade
                         escalates; a cancel ends the pre-alert and is only a line on an incident; the setting
    one chain            ATC pre-alert -> 60 Control declaration -> the county's event: one pre-alert, one incident, the
                         feed polled at once at each step
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sizeup import airport_watch as W
from sizeup.atc_normalize import normalize
from sizeup.cad.service import Cad, Settings
from sizeup.channels import vocabulary_suggestions
from sizeup.config import ChannelCfg, ConfigError, load

ROOT = Path(__file__).resolve().parent.parent
NIGHT = 1_800_000_000.0           # 03:00 local: NY Approach is on 120.8
DAY = NIGHT + 9 * 3600            # 12:00 local: 126.4
AIRBAND = ("HPN Tower", "NY App 126.4/120.8", "NY App 124.65", "HPN Ground")
CHANNELS = [ChannelCfg("HPN Tower", 118.575, "airband"),
            ChannelCfg("NY App 126.4/120.8", 126.4, "airband", schedule=((420, 126.4), (1380, 120.8)),
                       also=((124.65, 420, 1380, "NY App 124.65"),)),
            ChannelCfg("NY App 124.65", 124.65, "airband"),
            ChannelCfg("HPN Ground", 121.825, "airband")]


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self):
        return self.t


class Push:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority=3, tags=(), click="", who=None):
        self.sent.append((title, body, priority))
        return True


class World:
    def __init__(self, t0: float = NIGHT, incident_levels=(2, 3)):
        self.t0 = t0
        self.clock = Clock(t0)
        self.cad = Cad(Settings(airband=AIRBAND), log=lambda m: None, clock=self.clock)
        self.alerts = self.cad.airport_alerts
        self.push = Push()
        self.alerts.notifier = self.push
        self.plane = {"hex": "a0b1c2", "flight": "LXJ618", "type": "C68A", "lat": 41.04, "lon": -73.69, "alt": 1400,
                      "gs": 130, "trk": 340, "vr": -700, "phase": "arriving", "rwy": "34", "dist": 3.1}
        self.planes = [self.plane]
        self.w = self.cad.airport_watch
        self.w.configure(CHANNELS, lambda: self.planes, incident_levels)
        self.polls = []
        self.cad.sources.trigger = self.polls.append       # the station's sources_poll_now
        self.n = 0

    def at(self, s: float) -> float:
        self.clock.t = self.t0 + s
        return self.clock.t

    def atc(self, s: float, text: str, ch: str = "HPN Tower"):
        self.n += 1
        t = self.at(s)
        return self.w.heard_atc({"id": f"tx{self.n}", "source": ch, "start": t - 3, "end": t, "text": normalize(text)})

    def radio(self, s: float, text: str, channel: str = "Fire/EMS 15", tg=307):
        self.n += 1
        t = self.at(s)
        return self.w.heard_radio({"id": f"r{self.n}", "channel": channel, "talkgroup": tg, "start": t, "text": text})

    def tick(self, s: float, **plane):
        self.plane.update(plane)
        self.w.tick(self.planes, self.at(s))

    def open(self):
        return self.cad.state()["airport_alerts"]["open"]

    def incidents(self):
        return self.cad.store.q("SELECT * FROM incidents ORDER BY id")

    def feed(self, s: float, eid: str, typ: str, location: str, **kw):
        t = self.at(s)
        return self.cad.source_event({"source": "county", "label": "County feed", "rank": 1, "seen_at": t, "first_seen": t,
                                      "event": {"source": "county", "event_id": eid, "type": typ, "location": location,
                                                "times": {"dispatched": t}, **kw}})


def lines(a) -> dict:
    return dict(a["data"].get("lines") or [])


# -- the words ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("heard, fixed", [
    ("we have a flat fail indication", "we have a flap fail indication"),
    ("the flats are not extending", "the flaps are not extending"),
    ("flab problem", "flap problem"),
    ("going round", "going around"),
    ("a flat tire on the left main", "a flat tire on the left main"),       # a tire: left alone
])
def test_mis_hearings_are_fixed_first(heard, fixed):
    assert W.fuzzy(heard) == fixed


@pytest.mark.parametrize("text, kinds", [
    ("we just had a flap fail indication", {"flaps", "problem"}),
    ("we've got no flaps", {"flaps"}),
    ("unsafe gear indication on the nose", {"gear"}),
    ("we've lost the left hydraulics", {"hydraulics"}),
    ("we have to run a checklist, stand by", {"checklist"}),
    ("we may have to go to a different airport", {"divert"}),
    ("we're declaring an emergency", {"emergency"}),
    ("say souls on board and fuel remaining", {"souls", "fuel_left"}),
    ("minimum fuel, we're declaring an emergency", {"fuel", "emergency"}),
    ("engine failure on the right side", {"engine"}),
    ("can we get a block of airspace to troubleshoot", {"problem"}),
])
def test_abnormal_phrases(text, kinds):
    from sizeup import atc
    got = {a.kind for a, _w in W.abnormal(W.fuzzy(atc.normalize(normalize(text))))}
    assert kinds <= got, got


@pytest.mark.parametrize("text", [
    "Flexjet 618, runway three four, cleared to land, wind three three zero at eight",
    "flaps 20, gear down, cleared to land three four, Flexjet 618",
    "before takeoff checklist complete, ready for departure runway one six",
    "checklist complete, holding short one six",
    "taxi to runway one six via alfa, hold short of one one",
    "no smoking sign is on",
    "contact New York departure one two six point four, good day",
])
def test_normal_words_are_not_abnormal(text):
    from sizeup import atc
    assert W.abnormal(W.fuzzy(atc.normalize(normalize(text)))) == []


@pytest.mark.parametrize("text, mhz", [
    ("contact New York approach one two zero point eight", 120.8),
    ("twenty point eight, Flexjet six one eight", 120.8),
    ("20.8 Flexjet 618", 120.8),
    ("26.4 good day", 126.4),
    ("contact approach one two four point six five", 124.65),
    ("climb and maintain three thousand", None),
    ("runway three four cleared to land", None),
    ("altimeter two niner niner two", None),
    ("29.92, Flexjet 618", None),                     # an altimeter read back, not 129.92
    ("30.01 Flexjet 618", None),
    ("contact approach one three two point five four zero", 132.54),     # an 8.33 kHz channel name
])
def test_frequencies_heard_and_read_back(text, mhz):
    from sizeup import atc
    f = W.frequency(atc.normalize(normalize(text)))
    assert (f[0] if f else None) == mhz


def test_frequencies_map_to_our_channels():
    ch = W.Channels(CHANNELS)
    night = ch.where(120.8, "approach", NIGHT)
    assert (night.label, night.channel, night.receiving, night.text()) == ("NY App 120.8", "NY App 126.4/120.8", True,
                                                                          "NY App 120.8")
    day = ch.where(120.8, "approach", DAY)                       # the dongle is on 126.4 by day
    assert not day.receiving and day.text() == "NY App 120.8 · not on now · tracking by ADS-B"
    assert ch.where(124.65, "", DAY).receiving                   # the "also" frequency by day
    gone = ch.where(127.6, "approach", NIGHT)                    # not one of ours
    assert (gone.channel, gone.text()) == ("", "NY App 127.6 · off our channels · tracking by ADS-B")
    assert ch.where(None, "ground", NIGHT).channel == "HPN Ground"


# -- the abnormal go-around --------------------------------------------------------------------------------------------


def test_a_go_around_with_a_misheard_flap_problem_is_a_pre_alert():
    w = World()
    assert w.atc(0, "Flexjet six one eight, RNAV yankee three four, cleared to land") is None
    a = w.atc(60, "Flexjet six one eight is going around, we have a flat problem")
    assert a is not None and a["source"] == "atc" and a["kind"] == "abnormal" and a["status"] == "open"
    assert a["title"] == "HPN ATC: go-around · flaps · LXJ618 (C68A)"
    assert a["data"]["aircraft"][0]["hex"] == "a0b1c2"
    assert lines(a)["Heard"] == "go-around · flaps"
    assert len(w.push.sent) == 1 and w.push.sent[0][2] == 4
    # the controller, then the pilot with no callsign (the exchange): the checklist and a different airport escalate it
    b = w.atc(75, "Flexjet six one eight, fly runway heading, climb and maintain three thousand")
    c = w.atc(100, "yeah we just had a flat fail indication, we have to run a checklist, we may go to a different airport")
    assert b["id"] == c["id"] == a["id"] and c["confidence"] > a["confidence"]
    assert c["title"] == "HPN ATC: go-around · flaps, checklist, divert · LXJ618 (C68A)"
    assert [s["id"] for s in c["data"]["snippets"]][-1] == "tx4" and len(w.push.sent) == 1      # pushed once
    assert len(w.open()) == 1


def test_the_track_s_go_around_confirms_and_boosts_it():
    words = World()
    words.atc(0, "Flexjet six one eight going around")
    plain = words.atc(30, "Flexjet six one eight, we have a flap problem")
    seen = World()
    seen.tick(0)                                                 # arriving ...
    seen.tick(20, phase="near", vr=1800, alt=1900)               # ... then climbing away near HPN: a go-around
    a = seen.atc(30, "Flexjet six one eight, we have a flap problem")       # no "go around" said at all
    assert a is not None and lines(a)["ADS-B"] == "go-around (climb after approach)"
    assert a["confidence"] > plain["confidence"] - 0.01 and a["title"].startswith("HPN ATC: go-around · flaps")


def test_repeated_abnormal_phrases_alone_are_a_pre_alert():
    w = World()
    assert w.atc(0, "Westchester tower, Flexjet six one eight, we have an unsafe gear indication") is None
    a = w.atc(90, "Flexjet six one eight, we need to run the checklist, we may divert")
    assert a is not None and a["title"] == "HPN ATC: abnormal · gear, checklist, divert · LXJ618 (C68A)"


@pytest.mark.parametrize("gap", [20, 50, 70, 90])
@pytest.mark.parametrize("named", [True, False])
def test_the_shape_of_a_real_one_cut_off_and_unnamed(gap, named):
    """The pilot's go-around cut off at "a flat" (with or without its callsign), the controller's answer naming it,
    then, gap seconds on, the problem with no callsign: one pre-alert, then the hand-off."""
    w = World()
    w.tick(0)
    first = ("Flexjet six one eight, we're going to go around, we have a flat." if named
             else "Westchester tower, we're going to go around, we have a flat.")
    w.atc(30, first)
    b = w.atc(40, "Flexjet six one eight, roger, fly runway heading, climb and maintain three thousand")
    assert b is not None and b["kind"] == "abnormal" and lines(b)["Heard"].startswith("go-around · flaps")
    c = w.atc(40 + gap, "we just had a flap fail indication, we may have to go to a different airport, we'll run a "
                        "checklist, can we get some airspace at three thousand")
    assert c is not None and c["id"] == b["id"] and "checklist" in lines(c)["Heard"]
    e = w.atc(60 + gap, "twenty point eight, Flexjet six one eight")
    assert lines(e)["Where"] == "NY App 120.8" and len(w.open()) == 1


def test_an_unnamed_go_around_goes_to_the_one_aircraft_whose_track_went_around():
    w = World()
    w.tick(0)
    w.tick(20, phase="near", vr=1500, alt=1800)
    a = w.atc(30, "we're going around, we've got a flap failure")
    assert a is not None and a["data"]["aircraft"][0]["hex"] == "a0b1c2"


GROUND_B = {"hex": "b0b0b0", "flight": "EJA22", "type": "C56X", "lat": 41.068, "lon": -73.708, "alt": "ground", "gs": 10,
            "trk": 160, "vr": 0, "phase": "ground", "rwy": "16", "dist": 0.4}
B = "Execjet two two"


def with_ground_b() -> World:
    w = World()
    w.planes.append(dict(GROUND_B))
    return w


def test_an_unnamed_go_around_never_goes_to_an_aircraft_on_the_ground():
    """The tower first answers another aircraft, on the ground ("hold short, traffic going around"): the unnamed
    go-around waits for the airborne one."""
    w = with_ground_b()
    w.tick(0)
    w.atc(30, "Westchester tower, we're going around, we have a flat")
    assert w.atc(40, f"{B}, hold short runway one six, traffic going around") is None
    assert w.atc(50, f"hold short one six, {B}") is None
    a = w.atc(60, "Flexjet six one eight, roger, fly runway heading, climb and maintain three thousand")
    assert a is not None and [x["callsign"] for x in a["data"]["aircraft"]] == ["LXJ618"]
    assert all(t.callsign != "EJA22" or not (t.goaround or t.abn) for t in w.w.tracks.values())


def test_an_exchange_never_reaches_past_another_aircraft_named():
    w = with_ground_b()
    w.tick(0)
    a = w.atc(30, "Flexjet six one eight going around, we have a flap failure")
    w.atc(60, f"{B}, runway one six, line up and wait")
    w.atc(70, f"line up and wait one six, {B}")
    w.atc(90, "uh tower we've got an unsafe gear indication, we need to run a checklist, we'll taxi back")
    assert "gear" not in lines(w.alerts.get(a["id"]))["Heard"]           # not the go-around aircraft's
    assert len(w.open()) == 1


def test_a_ground_problem_is_never_the_airborne_go_around_s():
    w = with_ground_b()
    w.tick(0)
    w.tick(20, phase="near", vr=1800, alt=1900)                    # the airborne one's track went around
    assert w.atc(60, "tower, we've got a hydraulic leak indication, we need to run the checklist, we'll go back to "
                     "the ramp") is None
    assert w.open() == []


def test_the_track_s_go_around_wins_over_another_airborne_aircraft_named_next():
    w = World()
    w.planes.append({"hex": "c1c1c1", "flight": "JBU77", "type": "A320", "lat": 41.12, "lon": -73.73, "alt": 2400, "gs": 150,
                     "trk": 160, "vr": -600, "phase": "arriving", "rwy": "16", "dist": 4.0})
    w.tick(0)
    w.tick(20, phase="near", vr=1800, alt=1900)                    # LXJ618's track went around, JetBlue's didn't
    a = w.atc(30, "Westchester tower, we're going around, we have a flat")
    assert a is not None and [x["callsign"] for x in a["data"]["aircraft"]] == ["LXJ618"]
    b = w.atc(40, "JetBlue seven seven, continue, number two")
    assert b is None and len(w.open()) == 1


def test_one_transmission_with_several_kinds_is_enough():
    w = World()
    a = w.atc(0, "Flexjet six one eight, we have a gear unsafe indication, we'll run the checklist and may divert")
    assert a is not None and a["title"] == "HPN ATC: abnormal · gear, checklist, divert · LXJ618 (C68A)"


def test_an_unnamed_problem_too_long_before_anyone_is_named_is_dropped():
    w = World()
    w.atc(0, "Westchester tower, we're going around, we have a flap failure")
    assert w.atc(120, "Flexjet six one eight, roger, climb and maintain three thousand") is None


def test_a_late_clip_raises_nothing_but_still_joins_an_open_one():
    w = World()
    w.at(5 * 3600)
    old = w.w.heard_atc({"id": "old", "source": "HPN Tower", "start": w.t0, "end": w.t0 + 3,
                         "text": normalize("Flexjet six one eight going around, we have a flap failure")})
    assert old is None and w.open() == []
    assert w.w.heard_radio({"id": "r-old", "channel": "Fire/EMS 15", "talkgroup": 307, "start": w.t0,
                            "text": "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34"}) is None
    assert w.incidents() == [] and w.push.sent == []
    a = w.atc(5 * 3600 + 10, "Flexjet six one eight going around, we have a flap failure")
    late = w.w.heard_atc({"id": "late", "source": "HPN Tower", "start": w.clock.t - 900, "end": w.clock.t - 897,
                          "text": normalize("Flexjet six one eight, we're running the checklist")})
    assert late["id"] == a["id"] and late["data"]["snippets"][-1]["id"] == "late"


@pytest.mark.parametrize("seq", [
    ["Flexjet six one eight, go around, traffic on the runway", "going around, Flexjet six one eight",
     "Flexjet six one eight, fly runway heading, climb and maintain two thousand",
     "runway heading, two thousand, Flexjet six one eight"],
    ["Flexjet six one eight, flaps 20, cleared to land runway three four", "Flexjet six one eight missed approach",
     "Flexjet six one eight, contact New York approach one two zero point eight", "twenty point eight, Flexjet six one eight"],
    ["Flexjet six one eight, before takeoff checklist complete, ready for departure one six",
     "Flexjet six one eight, runway one six, cleared for takeoff", "checklist complete, rolling, Flexjet six one eight"],
    # a weather miss and the alternate; a missed approach and a divert for the weather
    ["Flexjet six one eight, going around, wind shear on short final", "Flexjet six one eight, roger, climb and maintain "
     "three thousand", "Flexjet six one eight, we'd like to go to our alternate", "Flexjet six one eight, contact New York "
     "approach one two zero point eight"],
    ["Flexjet six one eight, missed approach, couldn't see the runway", "Flexjet six one eight, we're going to divert"],
    # training: a practice single-engine approach, flapless practice
    ["Westchester tower, Flexjet six one eight, practice single engine approach, low approach then the missed",
     "Flexjet six one eight, at the missed approach point execute the published missed",
     "Flexjet six one eight, we are flapless on this one, just practice"],
    # unable to keep visual separation; the fuel question; a wind issue; the after-takeoff checklist
    ["Flexjet six one eight, go around, traffic on the runway", "going around, Flexjet six one eight",
     "Flexjet six one eight, unable to maintain visual separation with the traffic"],
    ["Flexjet six one eight, go around, the jet didn't clear", "Flexjet six one eight, say fuel remaining",
     "two hours of fuel, Flexjet six one eight"],
    ["Flexjet six one eight, going around", "Flexjet six one eight, we have an issue with the wind, request runway one six"],
    ["Flexjet six one eight, go around", "going around, Flexjet six one eight",
     "Flexjet six one eight, we'll run the after takeoff checklist and call you back"],
    # a brush fire's smoke reported twice (sizeup/atc.py's, not the aircraft's)
    ["Westchester tower, Flexjet six one eight, there's smoke off the departure end, looks like a brush fire",
     "Flexjet six one eight, thanks, where's the smoke", "about a mile south of the field, lot of smoke, Flexjet six one eight"],
    # routine words around the field
    ["Flexjet six one eight, fire up when ready", "Flexjet six one eight, engine start approved",
     "gear down, cleared to land three four, Flexjet six one eight", "Flexjet six one eight, unable higher",
     "we'd like to divert around the weather to the west, Flexjet six one eight"],
])
def test_normal_traffic_and_a_routine_hand_off_raise_nothing(seq):
    w = World()
    for i, text in enumerate(seq):
        assert w.atc(i * 30, text) is None, text
    assert w.cad.state()["airport_alerts"]["items"] == [] and w.push.sent == [] and w.polls == []
    assert not any(t.alert_id for t in w.w.tracks.values())


# -- hand-offs -----------------------------------------------------------------------------------------------------------


def test_followed_through_its_frequency_change_back_to_the_tower_until_it_lands():
    w = World()
    w.alerts.expire_s = 300.0                                    # shorter than its time away
    w.tick(0)
    a = w.atc(30, "Flexjet six one eight going around, flap failure")
    w.atc(45, "Flexjet six one eight, contact New York approach one two zero point eight")
    b = w.atc(55, "twenty point eight, Flexjet six one eight")   # the readback, its leading 1 dropped
    assert b["id"] == a["id"] and lines(b)["Where"] == "NY App 120.8"
    # heard on approach (we receive it at night): added, scanned
    c = w.atc(200, "New York approach, Flexjet six one eight, still working the checklist, request a block of airspace",
              ch="NY App 126.4/120.8")
    assert c["id"] == a["id"] and c["data"]["snippets"][-1]["channel"] == "NY App 126.4/120.8"
    assert "checklist" in lines(c)["Heard"]
    # ten minutes away, still on ADS-B: never expired
    for s in range(260, 900, 60):
        w.tick(s, phase="", dist=11.0, alt=3000, vr=0)
    assert [x["id"] for x in w.open()] == [a["id"]] and lines(w.open()[0])["Where"] == "NY App 120.8"
    d = w.atc(920, "Westchester tower, Flexjet six one eight on the visual three four")
    assert lines(d)["Where"] == "back on HPN Tower"
    w.tick(1100, phase="ground", alt="ground", dist=0.4, gs=20)
    assert w.open() == []
    done = w.alerts.get(a["id"])
    assert done["status"] == "ended" and done["note"] == "ADS-B: landed at HPN"


def test_off_our_channels_it_is_tracked_by_adsb_and_leaving_ends_it():
    w = World(t0=DAY)
    a = w.atc(0, "Flexjet six one eight going around, we have a hydraulic problem")
    b = w.atc(20, "Flexjet six one eight, contact New York approach one two seven point six")
    assert lines(b)["Where"] == "NY App 127.6 · off our channels · tracking by ADS-B"
    w.tick(60, phase="", dist=20.0, alt=6000, vr=1500)
    assert w.open()
    w.tick(120, dist=45.0)                                       # diverting: out of the area
    assert w.alerts.get(a["id"])["status"] == "ended" and "left the area" in w.alerts.get(a["id"])["note"]


# -- 60 Control ----------------------------------------------------------------------------------------------------------

DECLARE = ("60 Control, car 4417, declaring an alert level 1 for a Citation with flap issues, currently on a 5 mile "
           "final for runway 16")


def test_an_alert_level_declared_on_60_control_and_its_cancel():
    w = World()
    assert w.radio(0, "60 Control, car 4417, urgent") is None
    a = w.radio(20, DECLARE)
    assert a["source"] == "radio" and a["kind"] == "alert_level"
    assert a["title"] == "HPN Alert 1 · standby · Citation · flaps · 5 mi final RWY 16"
    assert a["data"]["alert_level"]["level"] == 1 and a["data"]["event"] == {"type": "AIRPORT", "subtype": "ALERT-1"}
    assert a["confidence"] == 1.0 and len(w.push.sent) == 1 and w.push.sent[0][0] == a["title"] and w.push.sent[0][2] == 5
    b = w.radio(40, "60 Control copies, alert level one for a citation with flap issues", tg=301)   # the readback
    assert b["id"] == a["id"] and b["title"] == a["title"] and len(w.push.sent) == 1
    c = w.radio(600, "60 Control, car 4417, alert level one is canceled, aircraft landed without incident")
    assert c["id"] == a["id"] and c["status"] == "ended" and "landed without incident" in c["note"]
    assert w.open() == [] and w.incidents() == []                 # Alert 1: never an incident


def test_an_upgrade_to_alert_2_escalates_the_same_pre_alert_into_an_incident():
    w = World()
    a = w.radio(0, DECLARE)
    assert w.incidents() == []                                    # Alert 1: standby, never an incident
    b = w.radio(120, "60 Control, car 4417, upgrade to an alert level 2, the aircraft is now reporting smoke")
    assert b["id"] == a["id"] and b["title"].startswith("HPN Alert 2 · Citation") and "standby" not in b["title"]
    assert len(w.push.sent) == 2 and w.push.sent[1][2] == 5       # pushed again, the top priority
    [inc] = w.incidents()
    assert (inc["type"], inc["subtype"], inc["status"], inc["source"]) == ("AIRPORT", "ALERT-2", "queued", "60 Control radio")
    assert inc["address"] == "240 Airport Rd" and inc["acft_type"] == "Citation" and inc["acft_runway"] == "16"
    assert b["incident_id"] == inc["id"] and b["status"] == "open" and lines(b)["Event"] == "queued · not dispatched"
    assert w.cad.store.q("SELECT 1 FROM assignments") == []       # never dispatched
    c = w.radio(200, "60 Control, car 4417, go to an alert level 3")
    assert c["id"] == a["id"] and [i["subtype"] for i in w.incidents()] == ["ALERT-3"]     # the same incident
    # the cancel ends the pre-alert and is a line on the incident, which stays open
    d = w.radio(900, "60 Control, car 4417, cancel the alert, aircraft landed without incident")
    assert d["status"] == "ended" and w.incidents()[0]["status"] == "queued"
    said = [e["text"] for e in w.cad.store.q("SELECT text FROM events WHERE incident_id = ?", (inc["id"],))]
    assert any("landed without incident" in x and "not closed" in x for x in said)


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
@pytest.mark.parametrize("level", [1, 2, 3])
def test_each_level_in_each_dispatch_mode(mode, level):
    w = World()
    w.cad.dispatch_mode({"mode": mode, "operator": "Lt. Test"})
    a = w.radio(0, f"60 Control, car 4417, declaring an alert level {level} for a Citation, gear problem, runway 34")
    assert a["status"] == "open" and a["title"].startswith(f"HPN Alert {level}") and w.push.sent[0][2] == 5
    assert ("standby" in a["title"]) == (level == 1)
    assert w.open()[0]["id"] == a["id"]
    assert [(i["subtype"], i["status"]) for i in w.incidents()] == ([] if level == 1 else [(f"ALERT-{level}", "queued")])
    assert w.polls                                                # the county's feed polled at once


def test_the_levels_are_a_setting():
    w = World(incident_levels=())                                 # [atc] alert_level_incident = []: pre-alerts only
    w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    assert w.incidents() == [] and len(w.open()) == 1
    w = World(incident_levels=(1, 2, 3))
    w.radio(0, DECLARE)
    assert [i["subtype"] for i in w.incidents()] == ["ALERT-1"]


def test_accepting_the_pre_alert_of_an_alert_2_makes_no_second_incident():
    w = World()
    a = w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    r = w.alerts.accept(a["id"], {"operator": "Lt. Test"})
    assert r["status"] == "accepted" and r["incident"]["id"] == a["incident_id"] and len(w.incidents()) == 1


def test_one_chain_from_the_tower_to_60_control_to_the_county():
    w = World()
    w.cad.dispatch_mode({"mode": "60control", "operator": "Lt. Test"})
    w.tick(0)
    t = w.atc(30, "Flexjet six one eight going around, we have a flat fail")
    assert lines(t)["Status"] == "possible incoming" and w.incidents() == [] and w.polls == ["airport pre-alert (ATC)"]
    a = w.radio(300, "60 Control, car 4417, declaring an alert level 2 for a Citation with flap issues, 5 mile final "
                     "runway 16")
    assert a["id"] == t["id"] and "Status" not in lines(a) and len(w.polls) == 2
    [inc] = w.incidents()
    assert a["incident_id"] == inc["id"]
    iid = w.feed(420, "C-77", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",))
    assert iid == inc["id"] and len(w.incidents()) == 1           # attached, not a second incident
    assert w.cad.auto.tracked(iid)                                # 60 Control mode: the county's event tracks it
    assert len(w.open()) == 1 and w.open()[0]["id"] == t["id"]


def test_the_page_attaches_to_an_alert_2_incident_too():
    w = World()
    w.cad.dispatch_mode({"mode": "pcfd", "operator": "Lt. Test"})
    w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    [inc] = w.incidents()
    how, got = w.cad.sources.page("m1", w.at(60), "Westchester County Airport", ["E64"],
                                  {"f": {"type": "AIRPORT"}, "type_heard": "Alert 2", "pages": []})
    assert (how, got) == ("attached", inc["id"]) and inc["status"] == "queued"


def test_tower_first_then_60_control_attaches():
    w = World()
    w.tick(0)
    t = w.atc(30, "Flexjet six one eight going around, we have a flat fail")
    a = w.radio(600, "60 Control, car 4417, declaring an alert level 1 for a Citation with flap issues, 5 mile final runway 16")
    assert a["id"] == t["id"] and a["source"] == "atc" and a["kind"] == "alert_level"
    assert a["title"] == "HPN Alert 1 · standby · Citation · flaps · 5 mi final RWY 16"
    assert a["data"]["aircraft"][0]["hex"] == "a0b1c2" and lines(a)["Alert"].startswith("declared ")
    assert [p[2] for p in w.push.sent] == [4, 5]                  # pushed again, at the top priority
    b = w.atc(700, "Westchester tower, Flexjet six one eight on the visual one six")
    assert b["title"] == a["title"] and "Alert" in lines(b) and len(w.open()) == 1
    c = w.radio(900, "the plane is down safely on the ground")
    assert c["id"] == t["id"] and c["status"] == "ended"


def test_60_control_first_then_the_tower_joins_it():
    w = World()
    a = w.radio(0, "60 Control, car 4417, declaring an alert level 1, Citation with a gear problem inbound runway 34")
    t = w.atc(60, "Flexjet six one eight going around, gear won't come down")
    assert t["id"] == a["id"] and t["title"] == "HPN Alert 1 · standby · Citation · gear · RWY 34"
    assert t["data"]["aircraft"][0]["hex"] == "a0b1c2" and len(w.open()) == 1


@pytest.mark.parametrize("text, channel, tg", [
    ("60 Control, Engine 64, alert level 2 heat advisory in effect, we're back in quarters", "Fire/EMS 15", 307),
    ("60 Control, be advised the alarm company says it's a medic alert activation", "Fire/EMS 15", 307),
    ("Ladder 31, the panel shows a trouble alert on level 2 of the garage", "FG1", None),
    ("Tower Ladder 2 to 60 Control, alert level one, we're available", "Fire/EMS 15", 307),
    ("declaring an alert level 1 for a Citation, runway 16", "WCPD Ch 1", 383),       # not a fire talkgroup
    ("declaring an alert level 1 for a Citation, runway 16", "HPN Tower", None),      # airband: the tower's watch
    ("the patient is down safely, transporting", "Fire/EMS 15", 307),                   # no alert open: nothing to end
])
def test_alert_in_other_fire_radio_never_counts(text, channel, tg):
    w = World()
    assert w.radio(0, text, channel, tg) is None
    assert w.cad.state()["airport_alerts"]["items"] == []


@pytest.mark.parametrize("text", [
    "60 Control, Engine 61, the alarm company says the alert was canceled, we're returning",
    "60 Control, Engine 61, the medic alert was cancelled",
    "Rescue 40, the patient is down safely from the ladder",
    "Ladder 31, the victim is down safely, we're coming off the roof",
    "60 Control, Engine 64, alert level 2 heat advisory in effect, we're back in quarters",
    "all units, county alert two for the storm, stand by",
])
def test_unrelated_fire_radio_never_ends_or_changes_an_open_alert(text):
    w = World()
    a = w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    t = World()
    t.atc(0, "Flexjet six one eight going around, we have a flap failure")
    assert w.radio(120, text) is None and t.radio(120, text) is None
    assert [(x["id"], x["status"], x["title"]) for x in w.open()] == [(a["id"], "open", a["title"])]
    assert len(t.open()) == 1 and [i["subtype"] for i in w.incidents()] == ["ALERT-2"]


def test_a_cancel_said_with_an_upgrade_is_the_upgrade():
    w = World()
    a = w.radio(0, DECLARE)
    b = w.radio(120, "60 Control, car 4417, cancel the alert 1, we're upgrading to an alert level 2, smoke in the cabin")
    assert b["id"] == a["id"] and b["status"] == "open" and b["title"].startswith("HPN Alert 2 · Citation · smoke")
    assert [i["subtype"] for i in w.incidents()] == ["ALERT-2"]


def test_a_downgrade_changes_the_level_and_closes_nothing():
    w = World()
    w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    b = w.radio(300, "60 Control, car 4417, the alert has been downgraded to an alert level 1")
    assert b["title"].startswith("HPN Alert 1 · standby") and lines(b)["Alert"].startswith("Alert 2 → 1 ")
    assert [(i["subtype"], i["status"]) for i in w.incidents()] == [("ALERT-1", "queued")]


def test_a_new_declaration_after_a_cancel_is_a_new_alert():
    w = World()
    a = w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    w.radio(600, "60 Control, car 4417, alert level 2 canceled, aircraft landed without incident")
    b = w.radio(3000, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 16")
    assert b["id"] != a["id"] and b["status"] == "open" and len(w.incidents()) == 2 and len(w.push.sent) == 2


def test_an_alert_with_an_open_incident_never_expires():
    w = World()
    w.alerts.expire_s = 600.0
    a = w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    w.at(2100)
    assert [x["id"] for x in w.open()] == [a["id"]]
    b = w.radio(2160, "60 Control, car 4417, still holding the alert level 2 for the Citation, runway 34")
    assert b["id"] == a["id"] and len(w.incidents()) == 1


def test_accepted_alert_takes_a_later_level_only_for_the_same_aircraft():
    w = World()
    a = w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    w.alerts.accept(a["id"], {"operator": "Lt. Test"})
    b = w.radio(300, "60 Control, car 4417, the Citation is now an alert level 3, runway 34")
    assert b["id"] == a["id"] and [i["subtype"] for i in w.incidents()] == ["ALERT-3"]
    c = w.radio(900, "60 Control, car 4417, declaring an alert level 2 for a King Air, runway 16")
    assert c["id"] != a["id"] and len(w.incidents()) == 2


@pytest.mark.parametrize("text, level", [
    ("60 Control, airport car 4417, declaring an alert level 1 for a Citation, flap issues, 5 mile final runway 16", 1),
    ("car 4417 airport alert level 2, Citation, gear problem, runway 34", 2),
    ("all units responding airport alert 2 runway 34", 2),
    ("60 Control, car 4417, alert 1 at West Chester airport, Citation on final runway 16", 1),
    ("60 Control, car 4417, declaring an alert to for a Citation on runway 16", 2),
    ("60 Control, car 4417, alert level won for a Citation runway 16", 1),
    ("60 Control, car 4417, alert level tree, aircraft on a 3 mile final runway 34", 3),
])
def test_misheard_levels_with_the_airport_in_it(text, level):
    a = World().radio(0, text)
    assert a is not None and a["title"].startswith(f"HPN Alert {level}")


def test_another_airport_and_a_drill():
    w = World()
    assert w.radio(0, "60 Control, be advised Danbury airport has an alert 1 for a Cessna, no response needed", tg=301) is None
    assert w.radio(20, "60 Control, Teterboro has an alert level 2 for a Gulfstream, runway 6, FYI only", tg=301) is None
    a = w.radio(60, "60 Control, car 4417, this is a drill, alert level 2 drill at the airport, runway 16")
    assert a["data"].get("drill") and w.incidents() == []          # a drill: never an incident


def test_an_ems_call_at_the_terminal_never_joins_the_alert_incident():
    w = World()
    w.radio(0, "60 Control, car 4417, declaring an alert level 2 for a Citation, runway 34")
    iid = w.feed(300, "C-9", "EMS SICK PERSON", "WESTCHESTER COUNTY AIRPORT TERMINAL", units=("E60",))
    assert len(w.incidents()) == 2 and iid != w.incidents()[0]["id"]
    assert w.feed(400, "C-10", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",)) == w.incidents()[0]["id"]


def test_the_cad_hears_60_control_through_the_comms_log():
    w = World()
    w.cad.comms.record({"id": "307-1-call_1-t1", "start": w.at(0), "end": w.at(8), "source": "Fire/EMS 15",
                        "talkgroup": 307, "unit": "", "text": DECLARE, "status": "done"})
    assert [x["title"] for x in w.open()] == ["HPN Alert 1 · standby · Citation · flaps · 5 mi final RWY 16"]


# -- config and vocabularies ---------------------------------------------------------------------------------------------


def test_alert_level_incident_config(tmp_path):
    base = '[radio]\ntones_file = ""\n[[radio.channels]]\nlabel = "HPN Tower"\nmhz = 118.575\nkind = "airband"\n'
    p = tmp_path / "c.toml"
    p.write_text(base)
    assert load(p).atc_alert_level_incident == (2, 3)                  # the department's: PCFD responds to 2 and 3
    p.write_text(base + "\n[atc]\nalert_level_incident = [3, 2]\n")
    assert load(p).atc_alert_level_incident == (2, 3)
    p.write_text(base + "\n[atc]\nalert_level_incident = []\n")
    assert load(p).atc_alert_level_incident == ()
    p.write_text(base + "\n[atc]\nalert_level_incident = [4]\n")
    with pytest.raises(ConfigError, match="alert_level_incident"):
        load(p)


def test_the_vocabularies_have_the_words():
    v = json.loads((ROOT / "data" / "atc" / "vocabulary.json").read_text())
    for word in ("flaps", "landing gear", "hydraulic", "go around", "missed approach", "checklist", "divert", "alert level"):
        assert word in v["phrases"], word
    assert not {"flaps", "checklist", "divert", "hydraulics"} & set(v["prompt"].lower().replace(",", " ").split())
    fire = vocabulary_suggestions({}, [])
    for word in ("alert level one", "alert level two", "alert level three", "flaps", "landing gear", "runway three four",
                 "mile final"):
        assert word in fire, word
