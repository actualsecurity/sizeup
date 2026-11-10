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
    ("say souls on board and fuel remaining", {"souls", "fuel"}),
    ("engine failure on the right side", {"engine"}),
    ("can we get a block of airspace to troubleshoot", {"problem"}),
])
def test_abnormal_phrases(text, kinds):
    got = {a.kind for a, _w in W.abnormal(W.fuzzy(normalize(text).lower().replace(",", "")))}
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


@pytest.mark.parametrize("seq", [
    ["Flexjet six one eight, go around, traffic on the runway", "going around, Flexjet six one eight",
     "Flexjet six one eight, fly runway heading, climb and maintain two thousand",
     "runway heading, two thousand, Flexjet six one eight"],
    ["Flexjet six one eight, flaps 20, cleared to land runway three four", "Flexjet six one eight missed approach",
     "Flexjet six one eight, contact New York approach one two zero point eight", "twenty point eight, Flexjet six one eight"],
    ["Flexjet six one eight, before takeoff checklist complete, ready for departure one six",
     "Flexjet six one eight, runway one six, cleared for takeoff", "checklist complete, rolling, Flexjet six one eight"],
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
    assert a["data"]["aircraft"][0]["hex"] == "a0b1c2" and lines(a)["Alert"].startswith("Alert 1 · standby · Citation")
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
    fire = vocabulary_suggestions({}, [])
    for word in ("alert level one", "alert level two", "alert level three", "flaps", "landing gear", "runway three four",
                 "mile final"):
        assert word in fire, word
