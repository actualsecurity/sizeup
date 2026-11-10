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
from sizeup import atc
from sizeup.atc_normalize import normalize
from sizeup.cad.service import Cad, Settings
from sizeup.channels import vocabulary_suggestions
from sizeup.config import ChannelCfg, ConfigError, load
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore

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
        self.plane = {"hex": "a0b1c2", "flight": "N123AB", "type": "C68A", "lat": 41.04, "lon": -73.69, "alt": 1400,
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

    def atc(self, s: float, text: str, ch: str = "HPN Tower", tid: str = "", start: float | None = None):
        self.n += 1
        t = self.at(s)
        st = t - 3 if start is None else self.t0 + start
        return self.w.heard_atc({"id": tid or f"tx{self.n}", "source": ch, "start": st, "end": st + 3,
                                 "text": normalize(text)})

    def radio(self, s: float, text: str, channel: str = "Fire/EMS 15", tg=307, tid: str = "", start: float | None = None):
        self.n += 1
        t = self.at(s)
        return self.w.heard_radio({"id": tid or f"r{self.n}", "channel": channel, "talkgroup": tg,
                                   "start": t if start is None else self.t0 + start, "text": text})

    def tick(self, s: float, **plane):
        self.plane.update(plane)
        self.w.tick(self.planes, self.at(s))

    def open(self):
        return self.cad.state()["airport_alerts"]["open"]

    def incidents(self):
        return self.cad.store.q("SELECT * FROM incidents ORDER BY id")

    def feed(self, s: float, eid: str, typ: str, location: str, dispatched: float | None = None, **kw):
        t = self.at(s)
        d = t if dispatched is None else self.t0 + dispatched
        return self.cad.source_event({"source": "county", "label": "County feed", "rank": 1, "seen_at": t, "first_seen": t,
                                      "event": {"source": "county", "event_id": eid, "type": typ, "location": location,
                                                "times": {"dispatched": d}, **kw}})

    def page(self, s: float, text: str, pid: str = "", fresh: bool = False):
        """60 Control's page, as the monitor hands it to the CAD (a repeat within 5 min: the same monitor incident)."""
        self.n += 1
        if fresh or not hasattr(self, "monitor"):
            self.monitor = IncidentStore({"Port Chester FD", "Rye Brook FD"}, geocode=lambda d: None)
        inc = self.monitor.on_page(pid or f"p{self.n}", ["Port Chester FD"], parse(text), at=self.at(s))
        return self.cad.from_monitor(inc)


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
    "N123AB, runway three four, cleared to land, wind three three zero at eight",
    "flaps 20, gear down, cleared to land three four, N123AB",
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
    ("twenty point eight, November one two three alpha bravo", 120.8),
    ("20.8 N123AB", 120.8),
    ("26.4 good day", 126.4),
    ("contact approach one two four point six five", 124.65),
    ("climb and maintain three thousand", None),
    ("runway three four cleared to land", None),
    ("altimeter two niner niner two", None),
    ("29.92, N123AB", None),                     # an altimeter read back, not 129.92
    ("30.01 N123AB", None),
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
    assert w.atc(0, "November one two three alpha bravo, RNAV yankee three four, cleared to land") is None
    a = w.atc(60, "November one two three alpha bravo is going around, we have a flat problem")
    assert a is not None and a["source"] == "atc" and a["kind"] == "abnormal" and a["status"] == "open"
    assert a["title"] == "HPN ATC: go-around · flaps · N123AB (C68A)"
    assert a["data"]["aircraft"][0]["hex"] == "a0b1c2"
    assert lines(a)["Heard"] == "go-around · flaps"
    assert len(w.push.sent) == 1 and w.push.sent[0][2] == 4
    # the controller, then the pilot with no callsign (the exchange): the checklist and a different airport escalate it
    b = w.atc(75, "November one two three alpha bravo, fly runway heading, climb and maintain three thousand")
    c = w.atc(100, "yeah we just had a flat fail indication, we have to run a checklist, we may go to a different airport")
    assert b["id"] == c["id"] == a["id"] and c["confidence"] > a["confidence"]
    assert c["title"] == "HPN ATC: go-around · flaps, checklist, divert · N123AB (C68A)"
    assert [s["id"] for s in c["data"]["snippets"]][-1] == "tx4" and len(w.push.sent) == 1      # pushed once
    assert len(w.open()) == 1


def test_the_track_s_go_around_confirms_and_boosts_it():
    words = World()
    words.atc(0, "November one two three alpha bravo going around")
    plain = words.atc(30, "November one two three alpha bravo, we have a flap problem")
    seen = World()
    seen.tick(0)                                                 # arriving ...
    seen.tick(20, phase="near", vr=1800, alt=1900)               # ... then climbing away near HPN: a go-around
    a = seen.atc(30, "November one two three alpha bravo, we have a flap problem")       # no "go around" said at all
    assert a is not None and lines(a)["ADS-B"] == "go-around (climb after approach)"
    assert a["confidence"] > plain["confidence"] - 0.01 and a["title"].startswith("HPN ATC: go-around · flaps")


def test_repeated_abnormal_phrases_alone_are_a_pre_alert():
    w = World()
    assert w.atc(0, "Westchester tower, November one two three alpha bravo, we have an unsafe gear indication") is None
    a = w.atc(90, "November one two three alpha bravo, we need to run the checklist, we may divert")
    assert a is not None and a["title"] == "HPN ATC: abnormal · gear, checklist, divert · N123AB (C68A)"


@pytest.mark.parametrize("gap", [20, 50, 70, 90])
@pytest.mark.parametrize("named", [True, False])
def test_the_shape_of_a_real_one_cut_off_and_unnamed(gap, named):
    """The pilot's go-around cut off at "a flat" (with or without its callsign), the controller's answer naming it,
    then, gap seconds on, the problem with no callsign: one pre-alert, then the hand-off."""
    w = World()
    w.tick(0)
    first = ("November one two three alpha bravo, we're going to go around, we have a flat." if named
             else "Westchester tower, we're going to go around, we have a flat.")
    w.atc(30, first)
    b = w.atc(40, "November one two three alpha bravo, roger, fly runway heading, climb and maintain three thousand")
    assert b is not None and b["kind"] == "abnormal" and lines(b)["Heard"].startswith("go-around · flaps")
    c = w.atc(40 + gap, "we just had a flap fail indication, we may have to go to a different airport, we'll run a "
                        "checklist, can we get some airspace at three thousand")
    assert c is not None and c["id"] == b["id"] and "checklist" in lines(c)["Heard"]
    e = w.atc(60 + gap, "twenty point eight, November one two three alpha bravo")
    assert lines(e)["Where"] == "NY App 120.8" and len(w.open()) == 1


def test_an_unnamed_go_around_goes_to_the_one_aircraft_whose_track_went_around():
    w = World()
    w.tick(0)
    w.tick(20, phase="near", vr=1500, alt=1800)
    a = w.atc(30, "we're going around, we've got a flap failure")
    assert a is not None and a["data"]["aircraft"][0]["hex"] == "a0b1c2"


GROUND_B = {"hex": "b0b0b0", "flight": "N456CD", "type": "C56X", "lat": 41.068, "lon": -73.708, "alt": "ground", "gs": 10,
            "trk": 160, "vr": 0, "phase": "ground", "rwy": "16", "dist": 0.4}
B = "November four five six charlie delta"


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
    a = w.atc(60, "November one two three alpha bravo, roger, fly runway heading, climb and maintain three thousand")
    assert a is not None and [x["callsign"] for x in a["data"]["aircraft"]] == ["N123AB"]
    assert all(t.callsign != "N456CD" or not (t.goaround or t.abn) for t in w.w.tracks.values())


def test_an_exchange_never_reaches_past_another_aircraft_named():
    w = with_ground_b()
    w.tick(0)
    a = w.atc(30, "November one two three alpha bravo going around, we have a flap failure")
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
    """The unnamed go-around is heard BEFORE the track shows it (it waits, an orphan); then N123AB's track goes around;
    then the tower names another airborne aircraft first: the orphan is never that one's (_take_orphans: another
    aircraft's track went around), it waits for N123AB."""
    w = World()
    w.planes.append({"hex": "c1c1c1", "flight": "N789EF", "type": "A320", "lat": 41.12, "lon": -73.73, "alt": 2400, "gs": 150,
                     "trk": 160, "vr": -600, "phase": "arriving", "rwy": "16", "dist": 4.0})
    w.tick(0)
    assert w.atc(10, "Westchester tower, we're going around, we have a flat") is None       # nobody yet: it waits
    w.tick(20, phase="near", vr=1800, alt=1900)                    # N123AB's track went around, N789EF's didn't
    b = w.atc(30, "November seven eight niner echo foxtrot, continue, number two")
    assert b is None and w.open() == []
    assert all(t.callsign != "N789EF" or not (t.goaround or t.abn) for t in w.w.tracks.values())
    a = w.atc(40, "November one two three alpha bravo, roger, climb and maintain three thousand")
    assert a is not None and [x["callsign"] for x in a["data"]["aircraft"]] == ["N123AB"] and len(w.open()) == 1


def test_one_transmission_with_several_kinds_is_enough():
    w = World()
    a = w.atc(0, "November one two three alpha bravo, we have a gear unsafe indication, we'll run the checklist and may divert")
    assert a is not None and a["title"] == "HPN ATC: abnormal · gear, checklist, divert · N123AB (C68A)"


def test_an_unnamed_problem_too_long_before_anyone_is_named_is_dropped():
    w = World()
    w.atc(0, "Westchester tower, we're going around, we have a flap failure")
    assert w.atc(120, "November one two three alpha bravo, roger, climb and maintain three thousand") is None


def test_a_late_clip_raises_nothing_but_still_joins_an_open_one():
    w = World()
    w.at(5 * 3600)
    old = w.w.heard_atc({"id": "old", "source": "HPN Tower", "start": w.t0, "end": w.t0 + 3,
                         "text": normalize("November one two three alpha bravo going around, we have a flap failure")})
    assert old is None and w.open() == []
    assert w.w.heard_radio({"id": "r-old", "channel": "Fire/EMS 15", "talkgroup": 307, "start": w.t0,
                            "text": "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34"}) is None
    assert w.incidents() == [] and w.push.sent == []
    a = w.atc(5 * 3600 + 10, "November one two three alpha bravo going around, we have a flap failure")
    late = w.w.heard_atc({"id": "late", "source": "HPN Tower", "start": w.clock.t - 900, "end": w.clock.t - 897,
                          "text": normalize("November one two three alpha bravo, we're running the checklist")})
    assert late["id"] == a["id"] and late["data"]["snippets"][-1]["id"] == "late"


@pytest.mark.parametrize("seq", [
    ["November one two three alpha bravo, go around, traffic on the runway", "going around, November one two three alpha bravo",
     "November one two three alpha bravo, fly runway heading, climb and maintain two thousand",
     "runway heading, two thousand, November one two three alpha bravo"],
    ["November one two three alpha bravo, flaps 20, cleared to land runway three four", "November one two three alpha bravo missed approach",
     "November one two three alpha bravo, contact New York approach one two zero point eight", "twenty point eight, November one two three alpha bravo"],
    ["November one two three alpha bravo, before takeoff checklist complete, ready for departure one six",
     "November one two three alpha bravo, runway one six, cleared for takeoff", "checklist complete, rolling, November one two three alpha bravo"],
    # a weather miss and the alternate; a missed approach and a divert for the weather
    ["November one two three alpha bravo, going around, wind shear on short final", "November one two three alpha bravo, roger, climb and maintain "
     "three thousand", "November one two three alpha bravo, we'd like to go to our alternate", "November one two three alpha bravo, contact New York "
     "approach one two zero point eight"],
    ["November one two three alpha bravo, missed approach, couldn't see the runway", "November one two three alpha bravo, we're going to divert"],
    # training: a practice single-engine approach, flapless practice
    ["Westchester tower, November one two three alpha bravo, practice single engine approach, low approach then the missed",
     "November one two three alpha bravo, at the missed approach point execute the published missed",
     "November one two three alpha bravo, we are flapless on this one, just practice"],
    # unable to keep visual separation; the fuel question; a wind issue; the after-takeoff checklist
    ["November one two three alpha bravo, go around, traffic on the runway", "going around, November one two three alpha bravo",
     "November one two three alpha bravo, unable to maintain visual separation with the traffic"],
    ["November one two three alpha bravo, go around, the jet didn't clear", "November one two three alpha bravo, say fuel remaining",
     "two hours of fuel, November one two three alpha bravo"],
    ["November one two three alpha bravo, going around", "November one two three alpha bravo, we have an issue with the wind, request runway one six"],
    ["November one two three alpha bravo, go around", "going around, November one two three alpha bravo",
     "November one two three alpha bravo, we'll run the after takeoff checklist and call you back"],
    # a brush fire's smoke reported twice (sizeup/atc.py's, not the aircraft's)
    ["Westchester tower, November one two three alpha bravo, there's smoke off the departure end, looks like a brush fire",
     "November one two three alpha bravo, thanks, where's the smoke", "about a mile south of the field, lot of smoke, November one two three alpha bravo"],
    # routine words around the field
    ["November one two three alpha bravo, fire up when ready", "November one two three alpha bravo, engine start approved",
     "gear down, cleared to land three four, November one two three alpha bravo", "November one two three alpha bravo, unable higher",
     "we'd like to divert around the weather to the west, November one two three alpha bravo"],
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
    a = w.atc(30, "November one two three alpha bravo going around, flap failure")
    w.atc(45, "November one two three alpha bravo, contact New York approach one two zero point eight")
    b = w.atc(55, "twenty point eight, November one two three alpha bravo")   # the readback, its leading 1 dropped
    assert b["id"] == a["id"] and lines(b)["Where"] == "NY App 120.8"
    # heard on approach (we receive it at night): added, scanned
    c = w.atc(200, "New York approach, November one two three alpha bravo, still working the checklist, request a block of airspace",
              ch="NY App 126.4/120.8")
    assert c["id"] == a["id"] and c["data"]["snippets"][-1]["channel"] == "NY App 126.4/120.8"
    assert "checklist" in lines(c)["Heard"]
    # ten minutes away, still on ADS-B: never expired
    for s in range(260, 900, 60):
        w.tick(s, phase="", dist=11.0, alt=3000, vr=0)
    assert [x["id"] for x in w.open()] == [a["id"]] and lines(w.open()[0])["Where"] == "NY App 120.8"
    d = w.atc(920, "Westchester tower, November one two three alpha bravo on the visual three four")
    assert lines(d)["Where"] == "back on HPN Tower"
    w.tick(1100, phase="ground", alt="ground", dist=0.4, gs=20)
    assert w.open() == []
    done = w.alerts.get(a["id"])
    assert done["status"] == "ended" and done["note"] == "ADS-B: landed at HPN"


def test_off_our_channels_it_is_tracked_by_adsb_and_leaving_ends_it():
    w = World(t0=DAY)
    a = w.atc(0, "November one two three alpha bravo going around, we have a hydraulic problem")
    b = w.atc(20, "November one two three alpha bravo, contact New York approach one two seven point six")
    assert lines(b)["Where"] == "NY App 127.6 · off our channels · tracking by ADS-B"
    w.tick(60, phase="", dist=20.0, alt=6000, vr=1500)
    assert w.open()
    w.tick(120, dist=45.0)                                       # diverting: out of the area
    assert w.alerts.get(a["id"])["status"] == "ended" and "left the area" in w.alerts.get(a["id"])["note"]


# -- 60 Control ----------------------------------------------------------------------------------------------------------

DECLARE = ("60 Control, car 9901, declaring an alert level 1 for a Citation with flap issues, currently on a 5 mile "
           "final for runway 16")


def test_an_alert_level_declared_on_60_control_and_its_cancel():
    w = World()
    assert w.radio(0, "60 Control, car 9901, urgent") is None
    a = w.radio(20, DECLARE)
    assert a["source"] == "radio" and a["kind"] == "alert_level"
    assert a["title"] == "HPN Alert 1 · standby · Citation · flaps · 5 mi final RWY 16"
    assert a["data"]["alert_level"]["level"] == 1 and a["data"]["event"] == {"type": "AIRPORT", "subtype": "ALERT-1"}
    assert a["confidence"] == 1.0 and len(w.push.sent) == 1 and w.push.sent[0][0] == a["title"] and w.push.sent[0][2] == 5
    b = w.radio(40, "60 Control copies, alert level one for a citation with flap issues", tg=301)   # the readback
    assert b["id"] == a["id"] and b["title"] == a["title"] and len(w.push.sent) == 1
    c = w.radio(600, "60 Control, car 9901, alert level one is canceled, aircraft landed without incident")
    assert c["id"] == a["id"] and c["status"] == "ended" and "landed without incident" in c["note"]
    assert w.open() == [] and w.incidents() == []                 # Alert 1: never an incident


def test_an_upgrade_to_alert_2_escalates_the_same_pre_alert_into_an_incident():
    w = World()
    a = w.radio(0, DECLARE)
    assert w.incidents() == []                                    # Alert 1: standby, never an incident
    b = w.radio(120, "60 Control, car 9901, upgrade to an alert level 2, the aircraft is now reporting smoke")
    assert b["id"] == a["id"] and b["title"].startswith("HPN Alert 2 · Citation") and "standby" not in b["title"]
    assert len(w.push.sent) == 2 and w.push.sent[1][2] == 5       # pushed again, the top priority
    [inc] = w.incidents()
    assert (inc["type"], inc["subtype"], inc["status"], inc["source"]) == ("AIRPORT", "ALERT-2", "queued", "60 Control radio")
    assert inc["address"] == "240 Airport Rd" and inc["acft_type"] == "Citation" and inc["acft_runway"] == "16"
    assert b["incident_id"] == inc["id"] and b["status"] == "open" and lines(b)["Event"] == "queued · not dispatched"
    assert w.cad.store.q("SELECT 1 FROM assignments") == []       # never dispatched
    c = w.radio(200, "60 Control, car 9901, go to an alert level 3")
    assert c["id"] == a["id"] and [i["subtype"] for i in w.incidents()] == ["ALERT-3"]     # the same incident
    # the cancel ends the pre-alert and is a line on the incident, which stays open
    d = w.radio(900, "60 Control, car 9901, cancel the alert, aircraft landed without incident")
    assert d["status"] == "ended" and w.incidents()[0]["status"] == "queued"
    said = [e["text"] for e in w.cad.store.q("SELECT text FROM events WHERE incident_id = ?", (inc["id"],))]
    assert any("landed without incident" in x and "not closed" in x for x in said)


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
@pytest.mark.parametrize("level", [1, 2, 3])
def test_each_level_in_each_dispatch_mode(mode, level):
    w = World()
    w.cad.dispatch_mode({"mode": mode, "operator": "Lt. Test"})
    a = w.radio(0, f"60 Control, car 9901, declaring an alert level {level} for a Citation, gear problem, runway 34")
    assert a["status"] == "open" and a["title"].startswith(f"HPN Alert {level}") and w.push.sent[0][2] == 5
    assert ("standby" in a["title"]) == (level == 1)
    assert w.open()[0]["id"] == a["id"]
    assert [(i["subtype"], i["status"]) for i in w.incidents()] == ([] if level == 1 else [(f"ALERT-{level}", "queued")])
    assert w.polls                                                # the county's feed polled at once


def test_the_levels_are_a_setting():
    w = World(incident_levels=())                                 # [atc] alert_level_incident = []: pre-alerts only
    w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    assert w.incidents() == [] and len(w.open()) == 1
    w = World(incident_levels=(1, 2, 3))                          # Alert 1 in it anyway (config.py refuses it)
    a = w.radio(0, DECLARE)
    assert w.incidents() == [] and a["title"].startswith("HPN Alert 1 · standby")    # a standby: never an incident


def test_accepting_the_pre_alert_of_an_alert_2_makes_no_second_incident():
    w = World()
    a = w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    r = w.alerts.accept(a["id"], {"operator": "Lt. Test"})
    assert r["status"] == "accepted" and r["incident"]["id"] == a["incident_id"] and len(w.incidents()) == 1


def test_one_chain_from_the_tower_to_60_control_to_the_county():
    w = World()
    w.cad.dispatch_mode({"mode": "60control", "operator": "Lt. Test"})
    w.tick(0)
    t = w.atc(30, "November one two three alpha bravo going around, we have a flat fail")
    assert lines(t)["Status"] == "possible incoming" and w.incidents() == [] and w.polls == ["airport pre-alert (ATC)"]
    a = w.radio(300, "60 Control, car 9901, declaring an alert level 2 for a Citation with flap issues, 5 mile final "
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
    w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    [inc] = w.incidents()
    how, got = w.cad.sources.page("m1", w.at(60), "Westchester County Airport", ["E64"],
                                  {"f": {"type": "AIRPORT"}, "type_heard": "Alert 2", "pages": []})
    assert (how, got) == ("attached", inc["id"]) and inc["status"] == "queued"


def test_tower_first_then_60_control_attaches():
    w = World()
    w.tick(0)
    t = w.atc(30, "November one two three alpha bravo going around, we have a flat fail")
    a = w.radio(600, "60 Control, car 9901, declaring an alert level 1 for a Citation with flap issues, 5 mile final runway 16")
    assert a["id"] == t["id"] and a["source"] == "atc" and a["kind"] == "alert_level"
    assert a["title"] == "HPN Alert 1 · standby · Citation · flaps · 5 mi final RWY 16"
    assert a["data"]["aircraft"][0]["hex"] == "a0b1c2" and lines(a)["Alert"].startswith("declared ")
    assert [p[2] for p in w.push.sent] == [4, 5]                  # pushed again, at the top priority
    b = w.atc(700, "Westchester tower, November one two three alpha bravo on the visual one six")
    assert b["title"] == a["title"] and "Alert" in lines(b) and len(w.open()) == 1
    c = w.radio(900, "the plane is down safely on the ground")
    assert c["id"] == t["id"] and c["status"] == "ended"


def test_60_control_first_then_the_tower_joins_it():
    w = World()
    a = w.radio(0, "60 Control, car 9901, declaring an alert level 1, Citation with a gear problem inbound runway 34")
    t = w.atc(60, "November one two three alpha bravo going around, gear won't come down")
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
    a = w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    t = World()
    t.atc(0, "November one two three alpha bravo going around, we have a flap failure")
    assert w.radio(120, text) is None and t.radio(120, text) is None
    assert [(x["id"], x["status"], x["title"]) for x in w.open()] == [(a["id"], "open", a["title"])]
    assert len(t.open()) == 1 and [i["subtype"] for i in w.incidents()] == ["ALERT-2"]


def test_a_cancel_said_with_an_upgrade_is_the_upgrade():
    w = World()
    a = w.radio(0, DECLARE)
    b = w.radio(120, "60 Control, car 9901, cancel the alert 1, we're upgrading to an alert level 2, smoke in the cabin")
    assert b["id"] == a["id"] and b["status"] == "open" and b["title"].startswith("HPN Alert 2 · Citation · smoke")
    assert [i["subtype"] for i in w.incidents()] == ["ALERT-2"]


def test_a_downgrade_changes_the_level_and_closes_nothing():
    w = World()
    w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    b = w.radio(300, "60 Control, car 9901, the alert has been downgraded to an alert level 1")
    assert b["title"].startswith("HPN Alert 1 · standby") and lines(b)["Alert"].startswith("Alert 2 → 1 ")
    assert [(i["subtype"], i["status"]) for i in w.incidents()] == [("ALERT-1", "queued")]


def test_a_new_declaration_after_a_cancel_is_a_new_alert():
    w = World()
    a = w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    w.radio(600, "60 Control, car 9901, alert level 2 canceled, aircraft landed without incident")
    b = w.radio(3000, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 16")
    assert b["id"] != a["id"] and b["status"] == "open" and len(w.incidents()) == 2 and len(w.push.sent) == 2


def test_an_alert_with_an_open_incident_never_expires():
    w = World()
    w.alerts.expire_s = 600.0
    a = w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    w.at(2100)
    assert [x["id"] for x in w.open()] == [a["id"]]
    b = w.radio(2160, "60 Control, car 9901, still holding the alert level 2 for the Citation, runway 34")
    assert b["id"] == a["id"] and len(w.incidents()) == 1


def test_accepted_alert_takes_a_later_level_only_for_the_same_aircraft():
    w = World()
    a = w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
    w.alerts.accept(a["id"], {"operator": "Lt. Test"})
    b = w.radio(300, "60 Control, car 9901, the Citation is now an alert level 3, runway 34")
    assert b["id"] == a["id"] and [i["subtype"] for i in w.incidents()] == ["ALERT-3"]
    c = w.radio(900, "60 Control, car 9901, declaring an alert level 2 for a King Air, runway 16")
    assert c["id"] != a["id"] and len(w.incidents()) == 2


@pytest.mark.parametrize("text, level", [
    ("60 Control, airport car 9901, declaring an alert level 1 for a Citation, flap issues, 5 mile final runway 16", 1),
    ("car 9901 airport alert level 2, Citation, gear problem, runway 34", 2),
    ("all units responding airport alert 2 runway 34", 2),
    ("60 Control, car 9901, alert 1 at West Chester airport, Citation on final runway 16", 1),
    ("60 Control, car 9901, declaring an alert to for a Citation on runway 16", 2),
    ("60 Control, car 9901, alert level won for a Citation runway 16", 1),
    ("60 Control, car 9901, alert level tree, aircraft on a 3 mile final runway 34", 3),
])
def test_misheard_levels_with_the_airport_in_it(text, level):
    a = World().radio(0, text)
    assert a is not None and a["title"].startswith(f"HPN Alert {level}")


def test_another_airport_and_a_drill():
    w = World()
    assert w.radio(0, "60 Control, be advised Danbury airport has an alert 1 for a Cessna, no response needed", tg=301) is None
    assert w.radio(20, "60 Control, Teterboro has an alert level 2 for a Gulfstream, runway 6, FYI only", tg=301) is None
    a = w.radio(60, "60 Control, car 9901, this is a drill, alert level 2 drill at the airport, runway 16")
    assert a["data"].get("drill") and w.incidents() == []          # a drill: never an incident


def test_an_ems_call_at_the_terminal_never_joins_the_alert_incident():
    w = World()
    w.radio(0, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34")
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
    for bad in ("[4]", "[1]", "[1, 2, 3]"):                           # Alert 1: a standby pre-alert, never an incident
        p.write_text(base + f"\n[atc]\nalert_level_incident = {bad}\n")
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


# -- the review of #372: one incident, Alert 1 a standby, re-runs, noise, the wrong aircraft --------------------------------

ALERT2 = "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34"
PAGE2 = ("Port Chester Fire Department, Engine 64, respond to an airport alert 2, Westchester County Airport, runway 34. "
         "Time out 0301.")
PAGE1 = ("Port Chester Fire Department, Engine 64, respond to an airport alert 1, Westchester County Airport, runway 34. "
         "Time out 0301.")


def _one_incident(w: World, a: dict) -> dict:
    [inc] = w.incidents()
    assert w.alerts.get(a["id"])["incident_id"] == inc["id"]
    return inc


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
def test_county_first_then_the_declaration_is_one_incident(mode):
    w = World()
    w.cad.dispatch_mode({"mode": mode, "operator": "Lt. Test"})
    iid = w.feed(0, "C-1", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",))
    a = w.radio(60, ALERT2)
    inc = _one_incident(w, a)
    assert inc["id"] == iid and inc["source"] == "CAD feed"
    assert lines(w.alerts.get(a["id"]))["Event"].endswith(("queued · not dispatched", "active", "pending"))
    assert w.push.sent and w.push.sent[0][2] == 5 and w.polls       # still the top priority, the feed polled


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
def test_page_first_then_the_declaration_is_one_incident(mode):
    w = World()
    w.cad.dispatch_mode({"mode": mode, "operator": "Lt. Test"})
    p = w.page(0, PAGE2)
    a = w.radio(60, ALERT2)
    inc = _one_incident(w, a)
    assert inc["id"] == p["id"] and inc["source"] == "60 Control page"


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
def test_the_declaration_then_a_county_event_dispatched_before_it_is_one_incident(mode):
    w = World()
    w.cad.dispatch_mode({"mode": mode, "operator": "Lt. Test"})
    a = w.radio(300, ALERT2)
    iid = w.feed(330, "C-2", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", dispatched=180, units=("E64",))
    inc = _one_incident(w, a)
    assert iid == inc["id"] and inc["source"] == "60 Control radio"
    assert w.cad.auto.tracked(iid) == (mode == "60control")         # 60 Control mode: the county's event tracks it


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
def test_the_declaration_then_the_page_heard_before_it_is_one_incident(mode):
    w = World()
    w.cad.dispatch_mode({"mode": mode, "operator": "Lt. Test"})
    a = w.radio(300, ALERT2)
    w.at(0)
    p = w.page(180, PAGE2)
    assert p["id"] == _one_incident(w, a)["id"]


def test_a_repeat_after_accept_without_a_type_joins_the_taken_incident():
    w = World()
    a = w.radio(0, ALERT2)
    w.alerts.accept(a["id"], {"operator": "Lt. Test"})
    pushes, polls = len(w.push.sent), len(w.polls)
    b = w.radio(120, "60 Control, car 9901, alert level 2 at the airport, runway 34")
    assert b["id"] == a["id"] and len(w.incidents()) == 1 and w.open() == []
    assert (len(w.push.sent), len(w.polls)) == (pushes, polls)        # nothing pushed or polled again


def test_alert_1_is_acknowledged_never_an_event():
    w = World()
    a = w.radio(0, DECLARE)
    r = w.alerts.accept(a["id"], {"operator": "Lt. Test"})
    assert r.get("acknowledged") and "incident" not in r and w.incidents() == []
    assert r["status"] == "open" and r["data"]["acknowledged"]["by"] == "Lt. Test"
    b = w.radio(120, "60 Control, car 9901, upgrade to an alert level 2, the aircraft is now reporting smoke")
    assert b["id"] == a["id"] and [i["subtype"] for i in w.incidents()] == ["ALERT-2"]   # the upgrade: the same one


def test_the_alert_1_card_says_acknowledge():
    js = (ROOT / "sizeup" / "web" / "static" / "airport-alerts.js").read_text()
    assert '"Acknowledge"' in js and 'standby ? "mini" : "mini primary"' in js
    assert "Take its queued event" in js                            # Alert 2 / 3: Accept takes the queued one


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
def test_a_county_alert_1_joins_the_standby_and_never_makes_an_incident(mode):
    w = World()
    w.cad.dispatch_mode({"mode": mode, "operator": "Lt. Test"})
    a = w.radio(0, DECLARE)
    pushes = len(w.push.sent)
    assert w.feed(60, "C-3", "AIRPORT ALERT 1", "WESTCHESTER COUNTY AIRPORT", units=("E64",)) is None
    assert w.incidents() == [] and [x["id"] for x in w.open()] == [a["id"]] and len(w.push.sent) == pushes
    assert any("County feed" in s["text"] for s in w.alerts.get(a["id"])["data"]["snippets"])
    iid = w.feed(300, "C-3", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",))   # it became an Alert 2
    assert iid is not None and [i["subtype"] for i in w.incidents()] == ["ALERT-2"]


def test_a_county_alert_1_alone_is_a_quiet_standby_pre_alert():
    w = World()
    w.cad.dispatch_mode({"mode": "60control", "operator": "Lt. Test"})
    assert w.feed(0, "C-4", "AIRPORT ALERT 1", "WESTCHESTER COUNTY AIRPORT", units=("E64",)) is None
    [a] = w.open()
    assert a["title"].startswith("HPN Alert 1 · standby") and w.incidents() == [] and w.push.sent == []
    b = w.radio(60, DECLARE)                                        # 60 Control says it: pushed now
    assert b["id"] == a["id"] and [p[2] for p in w.push.sent] == [5]


def test_a_page_for_alert_1_joins_the_standby_and_never_makes_an_incident():
    w = World()
    w.cad.dispatch_mode({"mode": "60control", "operator": "Lt. Test"})
    a = w.radio(0, DECLARE)
    assert w.page(60, PAGE1) is None
    assert w.incidents() == [] and [x["id"] for x in w.open()] == [a["id"]]


def test_a_typed_repeat_page_joins_the_alert_incident():
    w = World()
    a = w.radio(0, ALERT2)
    first = w.page(30, "Port Chester Fire Department, Engine 64.")               # the tones half: no type, no place
    assert first is not None and len(w.incidents()) == 2
    again = w.page(40, "Port Chester Fire Department, Engine 64, respond to an airport alert 2. Time out 0301.")   # its type
    inc = w.alerts.get(a["id"])["incident_id"]
    assert again["id"] == inc
    assert [(i["id"], i["status"]) for i in w.incidents()] == [(inc, "queued"), (first["id"], "dismissed")]


def test_a_weather_miss_with_an_alternate_is_quiet():
    w = World()
    assert w.atc(0, "November one two three alpha bravo going missed") is None
    assert w.atc(20, "November one two three alpha bravo, unable to land with this weather, divert to our alternate") is None
    assert w.atc(40, "November one two three alpha bravo, we're running the checklist") is None
    assert w.open() == []
    t = World()                                                     # a real problem with the alternate: still an alert
    t.atc(0, "November one two three alpha bravo going missed")
    a = t.atc(20, "November one two three alpha bravo, gear won't come down, we'll divert to our alternate")
    assert a is not None and "gear" in a["title"]


def test_a_single_engine_taxi_is_not_an_engine_problem():
    w = with_ground_b()
    assert w.atc(0, f"Westchester ground, {B}, number two engine shut down, single engine taxi to the ramp",
                 ch="HPN Ground") is None
    assert w.atc(10, f"{B}, we'll need to run the checklist, single engine taxi", ch="HPN Ground") is None
    assert w.open() == []


@pytest.mark.parametrize("text", [
    f"{B}, hold short runway three four, emergency aircraft on final",
    f"{B}, hold short, caution, an aircraft with a gear problem on a five mile final",
    f"{B}, traffic is an emergency aircraft on a three mile final, report it in sight",
])
def test_the_controller_s_words_about_another_aircraft_are_not_the_named_one_s(text):
    w = with_ground_b()
    if "traffic is" in text:                                        # airborne, told of another aircraft
        w.planes[1] = dict(GROUND_B, alt=2000, phase="arriving", dist=6.0)
    assert w.atc(0, text) is None
    assert w.atc(20, f"Westchester tower, {B}, we'll run the checklist") is None     # its own words: weak alone
    assert all(t.callsign != "N456CD" or {x[1] for x in t.abn} <= {"checklist"} for t in w.w.tracks.values())
    assert w.open() == []


@pytest.mark.parametrize("text", [
    f"{B}, mayday mayday mayday, engine fire, we're stopping on the taxiway",
    f"Westchester ground, {B}, we have smoke in the cabin, we're declaring an emergency",
])
def test_an_aircraft_s_own_emergency_on_the_ground_is_never_silenced(text):
    w = with_ground_b()
    a = w.atc(0, text, ch="HPN Ground")
    assert a is not None and [x["callsign"] for x in a["data"]["aircraft"]] == ["N456CD"]


def test_the_controller_s_words_about_the_named_aircraft_are_its_own():
    w = with_ground_b()
    w.atc(0, f"{B}, hold short, you have a brake fire on the left main, equipment is rolling", ch="HPN Ground")
    assert any(t.callsign == "N456CD" and {x[1] for x in t.abn} == {"smoke"} for t in w.w.tracks.values())


@pytest.mark.parametrize("text", ["runway three four, three miles", "November one two three alpha bravo, runway 34, "
                                  "three miles, cleared to land", "heading 120, 5 miles from the marker"])
def test_a_runway_and_miles_are_not_a_frequency(text):
    said = lambda x: W.fuzzy(atc.normalize(normalize(x)))       # noqa: E731 - as heard_atc reads it
    assert W.frequency(said(text)) is None
    assert W.frequency(said("contact New York approach one two zero point eight"))[0] == 120.8


def test_a_re_run_replaces_a_misheard_higher_level():
    w = World()
    a = w.radio(0, "60 Control, car 9901, declaring an alert level 3 for a Citation, runway 34", tid="r-x")
    assert [(i["subtype"], i["status"]) for i in w.incidents()] == [("ALERT-3", "queued")]
    b = w.radio(5, "60 Control, car 9901, declaring an alert level 1 for a Citation, runway 34", tid="r-x", start=0)
    assert b["id"] == a["id"] and b["title"].startswith("HPN Alert 1 · standby") and b["incident_id"] is None
    assert [(i["subtype"], i["status"]) for i in w.incidents()] == [("ALERT-3", "dismissed")]     # taken back
    assert len(w.alerts.get(a["id"])["data"]["snippets"]) == 1                                     # replaced, not stacked
    c = w.radio(30, "60 Control, car 9901, declaring an alert level 2 for a Citation, runway 34", tid="r-x", start=0)
    assert c["title"].startswith("HPN Alert 2") and [i["status"] for i in w.incidents()] == ["dismissed", "queued"]


def test_a_re_run_of_an_airband_transmission_replaces_it():
    w = World()
    assert w.atc(0, "November one two three alpha bravo going around", tid="t-1") is None
    assert w.atc(5, "November one two three alpha bravo, roger", tid="t-1", start=-3) is None     # re-heard: no go-around
    assert w.atc(30, "November one two three alpha bravo, we need to run the checklist, we may divert") is None
    assert w.open() == []


def test_60_control_s_alert_levels_are_not_the_airband_setting_s():
    w = World()
    w.w.enabled = False                                             # [atc] enabled = false: the airband's only
    assert w.radio(0, ALERT2)["title"].startswith("HPN Alert 2")


def test_a_flapping_level_is_pushed_and_polled_once():
    w = World()
    w.radio(0, DECLARE)
    w.radio(60, "60 Control, car 9901, upgrade to an alert level 2")
    w.radio(90, "60 Control, car 9901, downgraded to an alert level 1")
    w.radio(120, "60 Control, car 9901, upgrade to an alert level 2")
    assert len(w.push.sent) == 2 and len(w.polls) == 2              # Alert 1, then 2: not again within REPUSH_S
    w.radio(90 + W.REPUSH_S, "60 Control, car 9901, downgraded to an alert level 1")
    w.radio(120 + W.REPUSH_S, "60 Control, car 9901, upgrade to an alert level 2")
    assert len(w.push.sent) == 3 and len(w.polls) == 3


# -- the re-review of #372 --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("first, then", [
    ("November one two three alpha bravo, we've lost an engine, request emergency equipment",
     "November one two three alpha bravo, we'll run the checklist"),
    ("November one two three alpha bravo, engine failure, traffic permitting we'd like the long runway",
     "November one two three alpha bravo, souls on board four"),
])
def test_a_pilot_saying_its_own_callsign_first_is_never_silenced(first, then):
    w = World()
    w.atc(0, first)
    assert any(t.callsign == "N123AB" and "engine" in {x[1] for x in t.abn} for t in w.w.tracks.values())
    a = w.atc(20, then)
    assert a is not None and [x["callsign"] for x in a["data"]["aircraft"]] == ["N123AB"]


def test_the_alternate_alone_is_not_the_weather():
    w = World()
    w.atc(0, "November one two three alpha bravo going around")
    a = w.atc(20, "November one two three alpha bravo, we have a fail indication, we'll run the checklist, we may go "
                  "to our alternate")
    assert a is not None and a["title"].startswith("HPN ATC: go-around")
    t = World()                                                     # the tower's wind in a clearance is not either
    t.atc(0, "November one two three alpha bravo, wind three three zero at eight, go around")
    assert t.atc(20, "November one two three alpha bravo, we'll run the checklist and divert") is not None


def test_an_unrelated_airport_call_earlier_is_not_the_alert_s():
    w = World()
    iid = w.feed(0, "C-5", "AIRPORT FUEL SPILL", "WESTCHESTER COUNTY AIRPORT", units=("E64",))
    a = w.radio(18 * 60, ALERT2)
    assert len(w.incidents()) == 2 and w.alerts.get(a["id"])["incident_id"] != iid
    v = World()                                                     # an alert's, only once
    v.feed(0, "C-6", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",))
    b = v.radio(60, ALERT2)
    v.radio(300, "60 Control, car 9901, alert level 2 canceled, aircraft landed without incident")
    c = v.radio(400, "60 Control, car 9901, declaring an alert level 2 for a King Air, runway 16")
    assert c["id"] != b["id"] and len(v.incidents()) == 2


def test_a_county_event_after_the_cancel_is_another_call():
    w = World()
    a = w.radio(0, ALERT2)
    w.radio(300, "60 Control, car 9901, alert level 2 canceled, aircraft landed without incident")
    iid = w.feed(400, "C-7", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",))
    assert iid != w.alerts.get(a["id"])["incident_id"] and len(w.incidents()) == 2


def test_single_engine_taxi_clears_only_its_own_words():
    said = lambda x: {a.kind for a, _w in W.abnormal(W.fuzzy(atc.normalize(normalize(x))))}   # noqa: E731
    assert "engine" not in said("number two engine shut down, single engine taxi to the ramp")
    assert "engine" in said("we had an engine failure on the approach, single engine taxi to the ramp")


# -- round 3 of #372: one rule for whose trouble it is; the alert window; the cancel ----------------------------------------


@pytest.mark.parametrize("text", [
    f"{B}, we have traffic for you, emergency aircraft on final",
    f"{B}, hold short runway three four, we'll call you, emergency aircraft on final",
    f"{B}, hold short, emergency aircraft, they were on a two mile final",
    f"{B}, well, hold short, emergency aircraft with a gear problem",
    f"{B}, hold short runway three four, emergency aircraft on final",
])
def test_a_controller_s_we_is_not_the_ground_aircraft_s_trouble(text):
    w = with_ground_b()
    w.atc(0, text)
    assert all(t.callsign != "N456CD" or not t.abn for t in w.w.tracks.values())
    assert w.atc(20, f"Westchester tower, {B}, we'll run the checklist") is None and w.open() == []


@pytest.mark.parametrize("norm, own", [
    ("n123ab we ve lost an engine request emergency equipment", True),
    ("n123ab we have a flap problem", True),
    ("n123ab our left engine failed", True),
    ("n456cd we have traffic for you emergency aircraft on final", False),
    ("n456cd we have an emergency aircraft on final", False),
    ("n456cd well hold short emergency aircraft with a gear problem", False),
])
def test_own_trouble_is_a_first_person_trouble_phrase(norm, own):
    assert W.own_trouble(norm, W.abnormal(norm)) is own


@pytest.mark.parametrize("minutes", [6, 8, 15])
@pytest.mark.parametrize("county_first", [True, False])
def test_an_alert_2_county_event_joins_within_20_minutes(minutes, county_first):
    w = World()
    s = minutes * 60
    if county_first:
        w.feed(0, "C-8", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",))
        a = w.radio(s, ALERT2)
    else:                                                           # dispatched that long before, seen after it
        a = w.radio(s, ALERT2)
        w.feed(s + 30, "C-8", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", dispatched=0, units=("E64",))
    _one_incident(w, a)


@pytest.mark.parametrize("minutes", [8, 15])
def test_an_alert_2_page_joins_within_20_minutes(minutes):
    w = World()
    a = w.radio(minutes * 60, ALERT2)
    w.at(0)
    w.page(0, PAGE2)
    _one_incident(w, a)


def test_an_untyped_airport_event_8_minutes_before_is_separate():
    w = World()
    iid = w.feed(0, "C-9", "AIRPORT", "WESTCHESTER COUNTY AIRPORT", units=("E64",))
    a = w.radio(8 * 60, ALERT2)
    assert len(w.incidents()) == 2 and w.alerts.get(a["id"])["incident_id"] != iid
    v = World()                                                     # ... and the other way
    b = v.radio(8 * 60, ALERT2)
    v.feed(8 * 60 + 30, "C-9", "AIRPORT", "WESTCHESTER COUNTY AIRPORT", dispatched=0, units=("E64",))
    assert len(v.incidents()) == 2 and v.alerts.get(b["id"])["incident_id"] == v.incidents()[0]["id"]


def test_a_county_event_dispatched_before_the_cancel_still_joins():
    w = World()
    a = w.radio(0, ALERT2)
    w.radio(300, "60 Control, car 9901, alert level 2 canceled, aircraft landed without incident")
    iid = w.feed(400, "C-10", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", dispatched=120, units=("E64",))
    assert iid == w.alerts.get(a["id"])["incident_id"] and len(w.incidents()) == 1


# -- #380: whose trouble (another aircraft's words, a pilot's "be advised"), the smoke indication, two aircraft ---------------

C = "November seven eight nine echo foxtrot"
AIR_C = {"hex": "c0c0c0", "flight": "N789EF", "type": "C25B", "lat": 41.0, "lon": -73.68, "alt": 2500, "gs": 150,
         "trk": 340, "vr": -500, "phase": "arriving", "rwy": "34", "dist": 6.0}


def with_air_c() -> World:
    w = World()
    w.planes.append(dict(AIR_C))
    return w


def _abn(w: World, cs: str) -> set:
    return {x[1] for t in w.w.tracks.values() if t.callsign == cs for x in t.abn}


@pytest.mark.parametrize("text", [
    f"{C}, extend downwind, the aircraft ahead has a gear problem on final",
    f"{C}, number two following a King Air with a hydraulic failure on final",
    f"{C}, extend downwind, a Citation with a gear problem is on final",
    f"{C}, continue, traffic ahead, he's got a hydraulic failure, I'll call your base",
    f"{C}, they have a gear problem on the King Air on final, expect a short delay",
    f"{C}, the Citation has a flap problem, plan the long runway",
    f"{C}, reduce speed, the airplane in front of you has an engine failure",
])
def test_the_controller_s_words_about_another_aircraft_never_alert_the_airborne_one(text):
    w = with_air_c()
    assert w.atc(0, text) is None
    assert w.atc(40, text) is None                                  # said twice: still not its own
    assert _abn(w, "N789EF") == set() and w.open() == []


@pytest.mark.parametrize("text, kind", [
    (f"{C}, we have a gear problem", "gear"),
    (f"{C}, we've got a hydraulic failure, number two following the traffic ahead", "hydraulics"),
    (f"{C}, our gear won't come down, the aircraft ahead can go first", "gear"),
    (f"{C}, mayday mayday, hydraulic failure, following a King Air with a gear problem", "hydraulics"),
])
def test_the_same_words_as_its_own_trouble_still_alert(text, kind):
    w = with_air_c()
    w.atc(0, text)
    a = w.atc(40, text)
    assert a is not None and [x["callsign"] for x in a["data"]["aircraft"]] == ["N789EF"] and kind in _abn(w, "N789EF")


@pytest.mark.parametrize("text, own", [
    ("November one two three alpha bravo, be advised, hydraulic failure, request the long runway", True),
    ("November one two three alpha bravo, be advised, engine failure, we'd like runway three four", True),
    ("November one two three alpha bravo, be advised, flap problem, unable to land on one six", True),
    ("November one two three alpha bravo, be advised, the aircraft ahead has a hydraulic failure, request "
     "you extend downwind", False),
    ("November one two three alpha bravo, be advised, traffic is a King Air with a gear problem, unable "
     "the visual", False),
])
def test_a_pilot_s_be_advised_with_a_request_is_its_own(text, own):
    w = World()
    w.atc(0, text)
    assert bool(_abn(w, "N123AB")) is own
    if own:
        assert w.atc(40, text) is not None


def test_be_advised_alone_is_no_longer_an_advisory():
    norm = "n123ab be advised hydraulic failure request the long runway"
    assert W.aimed_elsewhere(norm, True, {"alt": 2000, "phase": "arriving"}, W.abnormal(norm)) is False
    other = "n123ab be advised the aircraft ahead has a hydraulic failure request you extend downwind"
    assert W.aimed_elsewhere(other, True, {"alt": 2000, "phase": "arriving"}, W.abnormal(other)) is True


@pytest.mark.parametrize("text", ["we have a smoke indication", "caution, we have a smoke indication",
                                  "we've got a smoke light in the aft lav", "smoke warning in the cargo hold"])
def test_a_smoke_indication_light_or_warning_is_smoke(text):
    norm = W.fuzzy(atc.normalize(normalize(text)))
    assert "smoke" in {a.kind for a, _w in W.abnormal(norm)}


def test_a_pilot_s_caution_smoke_indication_is_its_own():
    w = World()
    text = "November one two three alpha bravo, caution, we have a smoke indication, request the long runway"
    norm = W.fuzzy(atc.normalize(normalize(text)))
    assert W.own_trouble(norm, W.abnormal(norm))
    w.atc(0, text)
    a = w.atc(40, text)
    assert a is not None and "smoke" in _abn(w, "N123AB")


@pytest.mark.parametrize("a, b, differ", [
    ("Citation", "King Air", True),
    ("C68A", "King Air", True),
    ("Citation N123AB", "Citation N456CD", True),
    ("Citation", "Cessna Citation X", False),
    ("King Air", "Beechcraft", False),
    ("Citation", "business jet", False),
    ("", "King Air", False),
    ("N123AB", "King Air", False),
    ("Citation", "Zephyrcraft 9", False),                          # a maker not known: unknown, never different
    # an ADS-B type code and the name for the same aircraft: one
    ("C56X", "Citation", False), ("C68A", "Citation", False), ("G450", "Gulfstream", False), ("GLF4", "Gulfstream", False),
    ("BE20", "King Air", False),
    ("PC12", "Pilatus", False), ("PC-12", "Pilatus", False), ("LJ45", "Lear", False), ("CL35", "Challenger", False),
    ("GLEX", "Global Express", False), ("Global", "Bombardier", False), ("Hawker", "Beechcraft", False),
    ("E55P", "Phenom", False),
])
def test_other_aircraft(a, b, differ):
    assert W.other_aircraft(a, b) is differ and W.other_aircraft(b, a) is differ


KING_AIR2 = "60 Control, car 9901, declaring an alert level 2 for a King Air, runway 16"


@pytest.mark.parametrize("decl, joins", [(KING_AIR2, False), (ALERT2, True),
                                         ("60 Control, car 9901, declaring an alert level 2 at the airport, runway 34",
                                          True)])
def test_a_county_alert_for_another_aircraft_15_minutes_before_is_another_call(decl, joins):
    w = World()
    iid = w.feed(0, "C-11", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",),
                 comments=((w.t0 + 30, "CITATION GEAR PROBLEM 5 MI FINAL"),))
    a = w.radio(15 * 60, decl)
    assert (w.alerts.get(a["id"])["incident_id"] == iid) is joins
    assert len(w.incidents()) == (1 if joins else 2)


@pytest.mark.parametrize("decl, joins", [(KING_AIR2, False), (ALERT2, True)])
def test_a_page_for_another_aircraft_is_another_call(decl, joins):
    w = World()
    p = w.page(0, PAGE2.replace("airport alert 2,", "airport alert 2, a Citation with a gear problem,"))
    a = w.radio(15 * 60, decl)
    assert (w.alerts.get(a["id"])["incident_id"] == p["id"]) is joins


@pytest.mark.parametrize("comment, joins", [("KING AIR HYDRAULIC FAILURE", False), ("CITATION GEAR", True), ("", True)])
def test_a_county_event_for_another_aircraft_after_the_declaration_is_another_call(comment, joins):
    w = World()
    a = w.radio(0, ALERT2)
    kw = {"comments": ((w.t0 + 15 * 60, comment),)} if comment else {}
    iid = w.feed(15 * 60 + 30, "C-12", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",), **kw)
    assert (iid == w.alerts.get(a["id"])["incident_id"]) is joins
    assert len(w.incidents()) == (1 if joins else 2)


def test_two_tails_said_and_different_never_join():
    """The same type, another tail: another aircraft."""
    w = World()
    w.feed(0, "C-13", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",),
           comments=((w.t0 + 30, "CITATION N456CD UNSAFE GEAR"),))
    a = w.radio(10 * 60, "60 Control, car 9901, declaring an alert level 2 for a Citation N123AB, runway 34")
    assert len(w.incidents()) == 2 and w.alerts.get(a["id"])["incident_id"] != w.incidents()[0]["id"]


# -- #381 review: a code and a name are one aircraft; pronouns; a pilot's request ---------------------------------------


@pytest.mark.parametrize("decl, comment", [
    ("60 Control, car 9901, declaring an alert level 2 at the airport, runway 34", "CITATION GEAR PROBLEM"),   # ADS-B C68A
    ("60 Control, car 9901, declaring an alert level 2 for a Gulfstream, runway 34", "G450 GEAR PROBLEM"),
    ("60 Control, car 9901, declaring an alert level 2 for a Pilatus, runway 34", "PC12 GEAR PROBLEM"),
])
def test_the_main_chain_with_a_code_and_a_name_is_one_incident(decl, comment):
    """The tower's pre-alert (ADS-B: C68A) -> 60 Control's declaration (its type from ADS-B when it says none) -> the
    county's event naming the same aircraft by name: one incident."""
    w = World()
    w.atc(0, "November one two three alpha bravo going around")
    t = w.atc(20, "November one two three alpha bravo, we have a flap fail indication, we'll run the checklist")
    assert t is not None
    a = w.radio(120, decl)
    iid = w.feed(300, "C-14", "AIRPORT ALERT 2", "WESTCHESTER COUNTY AIRPORT", units=("E64",),
                 comments=((w.t0 + 290, f"ALERT 2 {comment}"),))
    inc = _one_incident(w, a)
    assert iid == inc["id"]


@pytest.mark.parametrize("text, kind", [
    ("November one two three alpha bravo, they're reporting smoke in the cabin", "smoke"),
    ("November one two three alpha bravo, a Citation with a gear problem, request the long runway", "gear"),
    ("November one two three alpha bravo, he's got a flap problem up here, we'd like to hold", "flaps"),
])
def test_a_pilot_s_own_crew_or_aircraft_is_its_own(text, kind):
    w = World()
    w.atc(0, text)
    assert kind in _abn(w, "N123AB")


@pytest.mark.parametrize("text", [
    f"{C}, traffic ahead, they're reporting smoke in the cabin",
    f"{C}, number two following a Citation, he's got a gear problem, request you extend downwind",
    f"{C}, a Citation with a gear problem is on a three mile final, unable the visual",
])
def test_a_pronoun_or_a_request_with_another_aircraft_said_is_not_its_own(text):
    w = with_air_c()
    w.atc(0, text)
    assert w.atc(40, text) is None and _abn(w, "N789EF") == set()
