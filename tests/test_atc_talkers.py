"""Who is talking on HPN Tower (GitHub #252, sizeup/atc_talkers.py): the callsign heard, matched to ADS-B, tower or aircraft,
shown like unit IDs, toggleable. Synthetic transcripts and fake ADS-B lists only."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sizeup import atc
from sizeup import atc_talkers as T
from sizeup.atc_normalize import normalize as atc_normalize
from sizeup.app import App
from sizeup.cad.service import Cad, Settings
from sizeup.cad.settings import CadError
from sizeup.radio.am import CarrierSquelch
from sizeup.radio.fm import Channel
from tests.test_app import FakeWhisper, cfg, wait
from tests.test_atc import FIVE, envelope

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
T0 = 1_800_000_000.0
IX = T.load_index()
CH = "HPN Tower"


def plane(hex_, flight, reg="", type_="", phase="arriving", dist=4.0, **kw):
    return {"hex": hex_, "flight": flight, "reg": reg, "type": type_, "phase": phase, "dist": dist, **kw}


JBU = plane("a1b2c3", "JBU1234", "N123JB", "A320", "arriving")
N47DQ = plane("c0ffee", "N47DQ", "N47DQ", "C172", "departing", 2.0)
EJA = plane("0dd0dd", "EJA521", "N521QS", "C68A", "ground", 0.4)
PLANES = [JBU, N47DQ, EJA]


def rec(text, planes=(), before=(), levels=(), carrier=None, at=T0):
    return T.analyze(T.Tx("t1", CH, at, at + 3, text, carrier), IX, list(planes), list(before), list(levels))


# -- the callsign: telephony, spoken digits, phonetics -----------------------------------------------------------------


@pytest.mark.parametrize("text, heard", [
    ("JetBlue one two three four, runway three four, cleared to land", "JBU1234"),
    ("jetblue twelve thirty-four contact departure", "JBU1234"),
    ("JetBlue 1234 cleared for takeoff", "JBU1234"),                      # the digits written
    ("Delta four five, contact ground point niner", "DAL45"),
    ("Endeavor fifty two ten, runway one six", "EDV5210"),
    ("Republic forty one twenty, with you at three thousand", "RPA4120"),  # the spoken name (telephony_extra.csv)
    ("Brickyard thirty five forty, cleared to land", "RPA3540"),           # FAA JO 7340.2's telephony
    ("Cactus fifteen ninety, line up and wait", "AAL1590"),
    ("American one twenty three, traffic twelve o'clock", "AAL123"),
    ("Blue Streak five zero one one, hold short runway one six", "JIA5011"),
    ("Execjet five twenty one, taxi to runway one six", "EJA521"),
    ("Reach four five six, cleared to land", "RCH456"),                    # military: Air Mobility Command
    ("November four seven Delta Quebec, runway one six, line up and wait", "N47DQ"),
    ("November one two three alfa bravo, wind calm", "N123AB"),
    ("Cessna seven delta quebec, roger", "…7DQ"),                           # shortened after first contact
])
def test_the_callsign_heard(text, heard):
    r = rec(text)
    assert r["aircraft"]["callsign"] == heard and r["aircraft"]["status"] == "heard"


def test_no_callsign_heard_nothing_labelled():
    for text in ("runway three four wind two seven zero at five", "roger", "say again", "Westchester Tower good morning", ""):
        assert rec(text) is None


# -- who is talking: the tower addressing an aircraft, or the aircraft ---------------------------------------------------


@pytest.mark.parametrize("text, role, talker, addressed", [
    ("JetBlue one two three four, runway three four, cleared to land", "atc", "TWR", "JBU1234"),
    ("cleared to land three four, JetBlue one two three four", "aircraft", "JBU1234", None),
    ("Westchester Tower, JetBlue one two three four, ten miles south, inbound", "aircraft", "JBU1234", "TWR"),
    ("JetBlue one two three four, Westchester Tower, runway three four, cleared to land", "atc", "TWR", "JBU1234"),
    ("good morning JetBlue one two three four Westchester Tower", "atc", "TWR", "JBU1234"),
    ("November four seven Delta Quebec, Westchester Ground, taxi to runway one six via alpha", "atc", "GND", "N47DQ"),
    ("Westchester Ground, November four seven Delta Quebec at the FBO, ready to taxi", "aircraft", "N47DQ", "GND"),
    ("JetBlue one two three four with you at three thousand", "aircraft", "JBU1234", None),
    ("November four seven Delta Quebec, contact ground point niner", "atc", "TWR", "N47DQ"),   # an instruction, not the GND
    ("hold short runway one six, November four seven Delta Quebec", "aircraft", "N47DQ", None),
])
def test_addressing_or_speaking(text, role, talker, addressed):
    r = rec(text, PLANES)
    assert r["role"] == role and r["talker"]["callsign"] == talker
    assert (r["addressed"] or {}).get("callsign") == addressed
    assert r["aircraft"]["role"] == {"atc": "addressed", "aircraft": "speaking"}[role]


@pytest.mark.parametrize("text, role, why", [
    # #270: the pilot's side with the callsign first (made-up callsigns)
    ("November six seven eight Quebec Zulu canceling landing clearance, roger that", "aircraft", "roger / wilco"),
    ("November six seven eight Quebec Zulu, cleared to land runway three four, wilco", "aircraft", "roger / wilco"),
    ("JetBlue one two three four, continuing the approach, we'll call you on the go", "aircraft", "report, request"),
    ("JetBlue one two three four, request runway one six", "aircraft", "report, request"),
    ("JetBlue one two three four on the RNAV Yankee three four", "aircraft", "reports its approach"),
    ("JetBlue one two three four, eight miles out on the ILS", "aircraft", "reports its approach"),
    ("Tower, JetBlue one two three four, with you on the RNAV", "aircraft", "calls tower"),
    ("cleared to land three four, JetBlue one two three four, thanks", "aircraft", "ends with the callsign"),
    ("JetBlue one two three four, mayday mayday mayday, engine fire", "aircraft", "report, request"),
    # the tower's: a directive after the callsign
    ("November six seven eight Quebec Zulu, cancel landing clearance, go around", "atc", "an instruction"),
    ("JetBlue one two three four, runway three four, wind three three zero at eight, cleared to land", "atc", "an instruction"),
    ("JetBlue one two three four, turn left heading two seven zero, contact departure", "atc", "an instruction"),
    ("November six seven eight Quebec Zulu, maintain two thousand until the bridge", "atc", "an instruction"),
    ("JetBlue one two three four, cleared RNAV Yankee three four approach", "atc", "an instruction"),
    # neither: unknown, not a guess
    ("JetBlue one two three four, that's the one off the Sound", "unknown", ""),
    ("confirm JetBlue one two three four cleared to land", "unknown", ""),
    ("November six seven eight Quebec Zulu, are we cleared to land", "aircraft", "report, request"),
])
def test_who_speaks_when_the_callsign_comes_first(text, role, why):
    r = rec(text, PLANES)
    assert r["role"] == role, (text, r["role"], r["talker"].get("why"))
    if role == "unknown":
        assert r["talker"]["kind"] == "unknown" and r["addressed"] is None
    else:
        assert why in r["talker"]["why"]
    if role == "aircraft":
        assert r["talker"]["kind"] == "aircraft" and r["talker"]["callsign"] != "TWR"


def test_a_pilot_mislabelled_as_the_tower_no_longer_teaches_the_tower_carrier():
    """#270: the tower's carrier is learnt from what the words say is the tower's. A pilot's callsign-first readback was
    'TWR', so the pilots' own carrier became the "tower's" and the carrier rule then called more pilots the tower."""
    pilot = rec("November six seven eight Quebec Zulu canceling landing clearance, roger that", PLANES, carrier=28.0)
    assert pilot["role"] == "aircraft" and pilot["talker"]["kind"] not in T.CHIP


def test_the_tower_said_its_name_or_is_read_from_the_words():
    r = rec("JetBlue one two three four, Westchester Tower, runway three four, cleared to land", PLANES)
    assert r["talker"] == {"kind": "tower", "callsign": "TWR", "status": "heard", "why": "self-ID: westchester tower"}
    r = rec("JetBlue one two three four, runway three four, cleared to land", PLANES)
    assert r["talker"]["status"] == "probable" and "starts with the callsign" in r["talker"]["why"]
    assert T.channel_facility("HPN Ground") == "ground" and T.channel_facility("HPN Tower") == "tower"
    assert [f.kind for f in T.facilities(atc.words_of("on the ground ground speed tower"))] == ["tower"]
    assert T.facilities(atc.words_of("contact westchester ground"))[0].instruction


def test_the_exchange_says_who_answers():
    words = "cleared to land JetBlue one two three four runway three four"          # the callsign mid-sentence
    assert rec(words, PLANES)["role"] == "unknown"
    tower = rec("JetBlue one two three four, runway three four, cleared to land", PLANES)
    r = rec(words, PLANES, before=[tower], at=T0 + 4)
    assert r["role"] == "aircraft" and r["how"] == "exchange" and "answers the tower" in r["talker"]["why"]
    pilot = rec("Westchester Tower, JetBlue one two three four, inbound", PLANES)
    r = rec(words, PLANES, before=[pilot], at=T0 + 4)
    assert r["role"] == "atc" and r["talker"]["callsign"] == "TWR"
    assert rec(words, PLANES, before=[tower], at=T0 + 4 + T.REPLY_S + 5)["role"] == "unknown"   # too long after


def test_the_carrier_tells_the_steady_tower_from_an_aircraft():
    tower = [30.1, 29.8, 30.4, 30.0, 29.6, 30.2]
    assert T.level_says(tower, 30.3) == "tower" and T.level_says(tower, 14.0) == "aircraft"
    assert T.level_says(tower, 25.0) == "" and T.level_says(tower[:3], 30.0) == "" and T.level_says(tower, None) == ""
    words = "cleared to land JetBlue one two three four runway three four"
    assert rec(words, PLANES, levels=tower, carrier=30.0)["role"] == "atc"
    r = rec(words, PLANES, levels=tower, carrier=12.0)
    assert r["role"] == "aircraft" and r["how"] == "carrier"


# -- the match: confirmed, probable, heard, never picked -----------------------------------------------------------------


def test_confirmed_is_an_exact_callsign_or_tail_on_adsb():
    a = rec("JetBlue one two three four, runway three four, cleared to land", PLANES)["addressed"]
    assert (a["status"], a["confidence"], a["hex"], a["type"]) == ("confirmed", 1.0, "a1b2c3", "A320")
    a = rec("hold short runway one six, November four seven Delta Quebec", PLANES)["talker"]
    assert (a["status"], a["hex"], a["callsign"]) == ("confirmed", "c0ffee", "N47DQ")


def test_probable_is_one_near_match_whose_phase_fits():
    r = rec("JetBlue one two three five, cleared to land runway three four", PLANES)
    a = r["addressed"]
    assert (a["status"], a["confidence"], a["callsign"], a["spoken"]) == ("probable", 0.7, "JBU1234", "JBU1235")
    # ... the same words while it is departing: the phase doesn't fit, heard only
    r = rec("JetBlue one two three five, cleared to land runway three four", [dict(JBU, phase="departing"), EJA])
    assert r["addressed"]["status"] == "heard" and r["addressed"]["callsign"] == "JBU1235" and "not arriving" in r["addressed"]["why"]
    # a shortened tail: the only aircraft whose tail ends so
    assert rec("seven delta quebec, roger", PLANES)["talker"]["status"] == "probable"


def test_two_candidates_are_heard_only():
    two = [plane("aaa111", "JBU1233"), plane("bbb222", "JBU1235")]
    a = rec("JetBlue one two three four, cleared to land", two)["addressed"]
    assert a["status"] == "heard" and "hex" not in a and "2 near matches" in a["why"]
    dup = [plane("aaa111", "JBU1234"), plane("bbb222", "JBU1234")]
    assert rec("JetBlue one two three four, cleared to land", dup)["addressed"]["status"] == "heard"
    tails = [plane("aaa111", "N37DQ", "N37DQ"), plane("bbb222", "N57DQ", "N57DQ")]
    assert rec("seven delta quebec, roger", tails)["talker"]["status"] == "heard"


def test_never_an_airliners_own_tail_or_a_plane_not_there():
    a = rec("November one two three juliett bravo, runway one six, line up and wait", [JBU])["addressed"]
    assert a["status"] == "heard" and a["callsign"] == "N123JB"           # JBU1234 is called JetBlue, not its tail
    a = rec("Delta four five, contact ground point niner", PLANES)["addressed"]
    assert a["status"] == "heard" and a["why"] == "not on ADS-B near HPN"


def test_the_time_window_and_matching_again_as_adsb_comes_in():
    s = T.Sightings()
    s.add([JBU], T0)
    s.add([JBU], T0 + 10)
    s.add([dict(EJA, dist=40.0)], T0)                                      # far away: not near HPN
    assert [a["hex"] for a in s.around(T0 + 10 + T.WINDOW_S)] == ["a1b2c3"]
    assert s.around(T0 + 10 + T.WINDOW_S + 1) == [] and s.around(T0 - T.WINDOW_S - 1) == []
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    tk = cad.atc_talkers
    tk.observe([], T0)
    r = tk.heard({"id": "HPNTower-1", "source": CH, "start": T0, "end": T0 + 3,
                  "text": "Westchester Tower, November four seven Delta Quebec, ready for departure"})
    assert r["talker"]["status"] == "heard"                                 # not on ADS-B yet
    clock[0] = T0 + 60
    tk.observe([N47DQ], T0 + 60)                                            # ... seen a minute later: within the window
    got = cad.store.one("SELECT hex, data FROM atc_talkers WHERE traffic_id = 'HPNTower-1'")
    assert got["hex"] == "c0ffee" and json.loads(got["data"])["talker"]["status"] == "confirmed"
    tk.heard({"id": "HPNTower-2", "source": CH, "start": T0 + 61, "end": T0 + 63, "text": "Delta four five, contact ground"})
    clock[0] = T0 + 61 + T.REMATCH_S + 10
    tk.observe([plane("dd0045", "DAL45", phase="ground")], clock[0])         # too late: stays heard
    assert json.loads(cad.store.one("SELECT data FROM atc_talkers WHERE traffic_id = 'HPNTower-2'")["data"])["addressed"]["status"] == "heard"


# -- a callsign run into "N mile final": speech-to-text lost the distance --------------------------------------------------
# Invented callsigns only: XYZ1234 ("Xyzair"), a traffic call on final said twice.

XIX = atc.TelephonyIndex([("XYZ", "Xyzair")])
XYZ = plane("c0a001", "XYZ1234", "N1234X", "CRJ9", "arriving", 4.0)


def heard_as(text, ix=XIX):
    return [(m.text, m.end) for m in atc.find_mentions(T.clauses(text), ix)]


@pytest.mark.parametrize("text, callsign", [
    # a flight written by its designator (as sizeup/atc_snap.py writes it): its digits are the callsign's
    ("Westchester traffic XYZ1234 is 4 mile final. Runway 34 Westchester.", "XYZ1234"),
    ("Westchester traffic XYZ1234 mile final for runway 34 Westchester.", "XYZ1234"),   # "2" lost: not XYZ1 at 234
    ("Westchester traffic XYZ1234 miles out for runway 34.", "XYZ1234"),
    ("XYZ1234 3 mile final", "XYZ1234"),
    ("XYZ1234 two mile final", "XYZ1234"),
    ("XYZ1234, 5 miles out", "XYZ1234"),
    ("XYZ1234 5 miles south", "XYZ1234"),
    ("XYZ123 4 mile final", "XYZ123"),                          # a written flight, then its distance
    ("XYZ4 mile final", "XYZ4"),
    # a number word too big to be a distance on final: the callsign's, never glued onto one before it
    ("Xyzair 1234 mile final", "XYZ1234"),                      # the spoken digits written (atc_normalize): not 1234 miles
    ("Xyzair 123 25 miles out", "XYZ123"),
    ("Xyzair 123, 25 miles out", "XYZ123"),
    ("Xyzair 1 2 3 25 miles out", "XYZ123"),
    ("Xyzair 123 25 mile final", "XYZ123"),
    ("Xyzair 12 25 mile final", "XYZ1225"),                    # within a flight number's 4: joined (XYZ12 the other)
    # 21 to 99 miles out: a check-in's distance (XYZ1225 only an aircraft around can make it)
    ("Xyzair 12 25 miles out", "XYZ12"),
    ("Xyzair 12 30 miles out", "XYZ12"),
    ("Xyzair 12 21 miles out", "XYZ12"),
    ("Xyzair 1234 miles out", "XYZ1234"),                       # a 3-4 digit number: no distance out, the callsign's
    # a whole number before the distance or the unit's own number: the flight number's end ("JetBlue 12 34")
    ("Xyzair 12 34 mile final", "XYZ1234"),
    ("Xyzair 12 34 3 thousand", "XYZ1234"),
    # spoken: a short flight and its distance read as before, nothing on ADS-B to say otherwise
    ("Xyzair one two three four mile final", "XYZ123"),
    ("Xyzair one two three four, two mile final", "XYZ1234"),
    ("Xyzair 12 4 mile final", "XYZ12"),
    ("Xyzair 12, 4 mile final", "XYZ12"),
    ("Xyzair 12 10 mile final", "XYZ12"),
    ("Xyzair 123 1 8 mile final", "XYZ123"),
    ("Xyzair 45 2 miles out", "XYZ45"),
    ("Xyzair one two ten mile final", "XYZ12"),
    ("Xyzair twelve four mile final", "XYZ12"),
    ("Xyzair 12 3 miles south", "XYZ12"),
    ("Xyzair 1 2 3 4 mile final", "XYZ1"),
])
def test_a_callsign_before_a_distance(text, callsign):
    assert [m[0] for m in heard_as(text)] == [callsign]
    if "," not in text:
        assert [m.text for m in atc.find_mentions(text, XIX)] == [callsign]     # the raw words too


@pytest.mark.parametrize("text", ["Xyzair 4 2 mile final", "Xyzair 7 3 mile final"])
def test_a_one_digit_flight_before_a_distance_is_no_callsign(text):
    assert heard_as(text) == [] and atc.find_mentions(text, XIX) == []


def test_a_distance_said_after_the_callsign_is_still_a_distance():
    for text, said, dist in (("XYZ1234 3 mile final", "3", 3), ("XYZ1234 two mile final", "two", 2),
                             ("XYZ1234, 5 miles out", "5", 5)):
        words = atc.words_of(T.clauses(text))
        numbers = atc.number_words(words)[0]
        assert words[max(numbers)] == said and words[max(numbers) + 1] in ("mile", "miles")   # the distance
        assert [m.text for m in atc.find_mentions(T.clauses(text), XIX)] == ["XYZ1234"]     # ... after the callsign
        assert atc.where(atc.normalize(text)).dist_nm == dist          # the pre-alert's location words: unchanged


def test_no_number_word_on_final_is_over_twenty_miles():
    def dist(text):
        words = atc.words_of(text)
        return [words[k] for k in sorted(atc.number_words(words)[0])]
    assert dist("1234 mile final") == [] and dist("25 miles on final") == [] and dist("1234 miles out") == []
    assert dist("34 miles out") == ["34"] and dist("99 miles out") == ["99"]     # out: a check-in's distance
    assert dist("20 miles out") == ["20"] and dist("1 8 mile final") == ["1", "8"] and dist("twelve mile final") == ["twelve"]
    assert dist("2 1 miles on final") == ["2", "1"]                  # digits said one by one: as before
    assert dist("traffic 1 2 3 miles south") == ["1", "2", "3"] and dist("traffic 25 miles south") == ["25"]


X12, X124, X123, X1210 = (plane(f"x{n}", f"XYZ{n}") for n in ("12", "124", "123", "1210"))


@pytest.mark.parametrize("text, planes, callsign, conf", [
    ("Xyzair 12 4 mile final", [X12], "XYZ12", 1.0),
    ("Xyzair 12 4 mile final", [X124], "XYZ124", 1.0),               # the only one around: its reading
    ("Xyzair 12 4 mile final", [X12, X124], "XYZ12", 1.0),           # both: the one read before (the shorter)
    ("Xyzair 12 4 mile final", [X124, X12], "XYZ12", 1.0),
    ("Xyzair 12 10 mile final", [X12, X1210], "XYZ12", 1.0),         # a distance's whole number word: never the callsign's
    ("Xyzair 123 25 miles out", [X123], "XYZ123", 1.0),
    ("Xyzair 123, 25 miles out", [X123], "XYZ123", 1.0),
    ("Xyzair 1 2 3 25 miles out", [X123], "XYZ123", 1.0),
    ("Xyzair 123 25 mile final", [X123], "XYZ123", 1.0),
    ("XYZ123 4 mile final", [X123, XYZ], "XYZ123", 1.0),
    ("Westchester traffic XYZ1234 mile final for runway 34", [XYZ], "XYZ1234", 1.0),
])
def test_the_reading_an_aircraft_on_adsb_has(text, planes, callsign, conf):
    a = atc.attribute(text, XIX, planes)                              # the pre-alert's matcher, the raw words
    assert (a.callsign, a.confidence) == (callsign, conf)
    r = rec_x(text, planes)["aircraft"]                               # the talker
    assert (r["callsign"], r["status"]) == (callsign, "confirmed")


@pytest.mark.parametrize("heard, written", [
    ("JetBlue 12 34 cleared to land", "JetBlue 1234 cleared to land"),          # groups joined, as before
    ("JetBlue 12 4 mile final", "JetBlue 12 4 mile final"),                    # ... never with a distance after them
    ("JetBlue 123 25 miles out", "JetBlue 123 25 miles out"),
    ("JetBlue 123 1 8 mile final", "JetBlue 123 1 8 mile final"),
    ("JetBlue 45 2 miles out", "JetBlue 45 2 miles out"),
])
def test_the_written_form_never_joins_a_distance_to_the_flight_number(heard, written):
    assert atc_normalize(heard) == written
    m, = atc.find_mentions(T.clauses(written), IX)
    assert m.text == written.split()[0].replace("JetBlue", "JBU") + written.split()[1]


X1, X1234 = plane("x1", "XYZ1"), plane("x1234", "XYZ1234")


@pytest.mark.parametrize("text, planes, callsign", [
    ("XYZ1234 3 mile final", [X12, X1234], "XYZ1234"),
    ("Westchester traffic XYZ1234 mile final for runway 34 Westchester.", [X1, X1234], "XYZ1234"),
    ("Westchester traffic XYZ1234 mile final for runway 34 Westchester.", [X1], "XYZ1234"),   # never fewer digits
    ("XYZ123 4 mile final", [X1, X123], "XYZ123"),
])
def test_a_written_flight_keeps_its_digits_whoever_else_is_around(text, planes, callsign):
    m, = atc.find_mentions(T.clauses(text), XIX)
    assert (m.text, m.alts) == (callsign, ())                        # no shorter reading to pick
    assert atc.reading(m, planes, known=["XYZ1"]).text == callsign
    assert atc.attribute(text, XIX, planes).spoken == callsign


def test_xyz123_4_mile_final_with_xyz1_and_xyz1234_around_is_never_xyz1():
    a = atc.attribute("XYZ123 4 mile final", XIX, [X1, X1234])
    assert a.spoken == "XYZ123" and a.callsign == "XYZ1234" and a.confidence == 0.7   # one digit off, as for any
    r = rec_x("XYZ123 4 mile final", [X1, X1234])["aircraft"]
    assert (r["spoken"], r["callsign"], r["status"]) == ("XYZ123", "XYZ1234", "probable")


JBU12, JBU1234 = plane("j12", "JBU12"), plane("j1234", "JBU1234")


@pytest.mark.parametrize("heard, planes, callsign, conf", [
    ("JetBlue 12 34 mile final", [JBU1234], "JBU1234", 1.0),
    ("JetBlue 12 34 mile final", [JBU12, JBU1234], "JBU1234", 1.0),  # the reading found first, when on ADS-B
    ("JetBlue 12 34 mile final", [JBU12], "JBU12", 1.0),              # ... else the other on ADS-B (34 miles out?)
    ("JetBlue 12 34 3 thousand", [JBU1234], "JBU1234", 1.0),
    ("JetBlue 12 34 cleared to land", [JBU1234], "JBU1234", 1.0),    # no unit: joined in the written form, as before
])
def test_a_flight_number_said_in_groups_before_a_unit(heard, planes, callsign, conf):
    text = atc_normalize(heard)                                       # the written form, as the app reads it
    a = atc.attribute(text, IX, planes)
    assert (a.callsign, a.confidence) == (callsign, conf)
    r = rec(text, planes)["aircraft"]
    assert (r["callsign"], r["status"]) == (callsign, "confirmed")


def test_a_flight_number_said_in_groups_then_a_comma():
    # the talker keeps the comma (clauses); the pre-alert's raw words never did, before #650 too: not changed here
    r = rec(atc_normalize("JetBlue 12 34, 3 thousand"), [JBU1234])["aircraft"]
    assert (r["callsign"], r["status"]) == ("JBU1234", "confirmed")


X1225 = plane("x1225", "XYZ1225")


@pytest.mark.parametrize("text, planes, known, callsign", [
    ("Xyzair 12 25 miles out", [], [], "XYZ12"),
    ("Xyzair 12 25 miles out", [X12, X1225], [], "XYZ12"),           # the distance read first
    ("Xyzair 12 25 miles out", [X1225], [], "XYZ1225"),              # ... unless only XYZ1225 is around
    ("Xyzair 12 25 miles out", [], ["XYZ1225"], "XYZ1225"),          # ... or just talked
    ("Xyzair 12 25 miles out", [X12], ["XYZ1225"], "XYZ12"),
    ("Xyzair 12 25 mile final", [X12, X1225], [], "XYZ1225"),        # on final: no one is 25 miles out on final
    ("Xyzair 12 25 mile final", [X12], [], "XYZ12"),
])
def test_miles_out_is_a_distance_unless_an_aircraft_says_otherwise(text, planes, known, callsign):
    m, = atc.find_mentions(T.clauses(text), XIX)
    assert atc.reading(m, planes, known).text == callsign
    if planes:
        a = atc.attribute(text, XIX, planes)
        assert (a.callsign, a.confidence) == (callsign, 1.0)
        r = rec_x(text, planes)["aircraft"]
        assert (r["callsign"], r["status"]) == (callsign, "confirmed")


X125 = plane("x125", "XYZ125")


def test_a_one_digit_flight_before_a_check_in_distance_is_no_callsign_unless_an_aircraft_says_so():
    text = "Xyzair 1 25 miles out"
    m, = atc.find_mentions(T.clauses(text), XIX)
    assert (m.alone, [a[0] for a in m.alts]) == (False, ["125"])
    assert not atc.reading(m, []).alone and not atc.reading(m, [X1]).alone      # XYZ1 never, as before
    assert atc.attribute(text, XIX, []) is None and atc.attribute(text, XIX, [X1]) is None
    assert rec_x(text, [X1]) is None
    a = atc.attribute(text, XIX, [X125])                                          # ... XYZ125 when it is around
    assert (a.callsign, a.confidence) == ("XYZ125", 1.0)
    assert (rec_x(text, [X125])["aircraft"]["callsign"], rec_x(text, [X125])["aircraft"]["status"]) == ("XYZ125", "confirmed")
    assert atc.reading(m, [], known=["XYZ125"]).text == "XYZ125"                # ... or just talked


def test_a_tail_before_a_check_in_distance():
    m, = atc.find_mentions("November 1 2 3 25 miles out", XIX)
    assert (m.text, [a[0] for a in m.alts]) == ("N123", ["12325"])
    assert atc.attribute("November 1 2 3 25 miles out", XIX, [plane("n1", "N123", "N123")]).confidence == 1.0


def test_adsb_ranks_above_a_recent_talker():
    m, = atc.find_mentions("Xyzair 12 4 mile final", XIX)
    assert (m.text, [a[0] for a in m.alts]) == ("XYZ12", ["124"])
    assert atc.reading(m, []).text == "XYZ12"                         # nothing around: as read
    assert atc.reading(m, [], known=["XYZ124"]).text == "XYZ124"      # just matched on the channel
    assert atc.reading(m, [X12], known=["XYZ124"]).text == "XYZ12"    # ... but XYZ12 is on ADS-B
    before = [{"aircraft": {"callsign": "XYZ124", "status": "confirmed"}}]
    assert rec_x("Xyzair 12 4 mile final", [X12], before)["aircraft"]["callsign"] == "XYZ12"


def rec_x(text, planes=(), before=()):
    return T.analyze(T.Tx("t1", CH, T0, T0 + 3, text), XIX, list(planes), list(before))


@pytest.mark.parametrize("text", [
    "Westchester traffic XYZ1234 is 4 mile final. Runway 34 Westchester.",
    "Westchester traffic XYZ1234 mile final for runway 34 Westchester.",
])
def test_the_traffic_call_on_final_names_the_aircraft_on_adsb(text):
    a = rec_x(text, [XYZ, EJA])["aircraft"]
    assert (a["callsign"], a["status"], a["hex"], a["type"]) == ("XYZ1234", "confirmed", "c0a001", "CRJ9")


def test_the_same_call_74_s_later_with_the_distance_lost_is_the_same_aircraft():
    """As live: "... is 4 mile final" matched; 74 s later "... mile final" (its "2" lost) was heard as XYZ1."""
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    tk = cad.atc_talkers
    tk.observe([XYZ, EJA], T0)
    first = tk.heard({"id": "HPNTower-1", "source": CH, "start": T0, "end": T0 + 4,
                      "text": "Westchester traffic XYZ1234 is 4 mile final. Runway 34 Westchester."})
    clock[0] = T0 + 74
    tk.observe([dict(XYZ, dist=2.0), EJA], clock[0])
    second = tk.heard({"id": "HPNTower-2", "source": CH, "start": T0 + 74, "end": T0 + 78,
                       "text": "Westchester traffic XYZ1234 mile final for runway 34 Westchester."})
    for r in (first, second):
        assert (r["aircraft"]["callsign"], r["aircraft"]["hex"], r["aircraft"]["type"]) == ("XYZ1234", "c0a001", "CRJ9")
        assert r["aircraft"]["status"] in ("confirmed", "probable")
    assert "XYZ1234" in T.describe(second)


# -- exchanges -------------------------------------------------------------------------------------------------------------


def item(i, at, cs=None, hex_="", ch=CH):
    return {"id": f"t{i}", "channel": ch, "at": at, "aircraft": {"callsign": cs, "hex": hex_, "status": "heard"} if cs else None}


def test_exchanges_group_consecutive_transmissions_about_one_aircraft():
    ids = T.exchanges([item(1, 0, "JBU1234"), item(2, 5, "JBU1234"), item(3, 9), item(4, 12, "JBU1234"),   # one between joins
                       item(5, 20, "N47DQ"), item(6, 25, "JBU1234"),                                       # another breaks
                       item(7, 25 + T.EXCHANGE_GAP_S + 1, "JBU1234"),                                       # too long after
                       item(8, 200, "N47DQ"), item(9, 203), item(10, 204), item(11, 206, "N47DQ"),           # two between: no
                       item(12, 300, "N47DQ"), item(13, 304, "…7DQ"),                                       # shortened: joins
                       item(14, 301, "JBU1234", ch="HPN Ground")])                                         # its own channel
    assert ids["t1"] == ids["t2"] == ids["t3"] == ids["t4"] == "t1"
    assert ids["t5"] == "t5" and ids["t6"] == "t6" and ids["t7"] == "t7"
    assert ids["t8"] == "t8" and "t9" not in ids and ids["t11"] == "t11"
    assert ids["t12"] == ids["t13"] == "t12" and ids["t14"] == "t14"


def test_an_exchange_is_named_by_its_aircraft_and_what_is_happening():
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    tk = cad.atc_talkers
    tk.observe(PLANES, T0)
    for i, (dt, text) in enumerate([(0, "Westchester Tower, JetBlue one two three four, ten miles south, inbound"),
                                    (6, "JetBlue one two three four, runway three four, cleared to land"),
                                    (12, "roger"),
                                    (15, "cleared to land three four, JetBlue one two three four")]):
        clock[0] = T0 + dt + 3
        tk.heard({"id": f"HPNTower-{i}", "source": CH, "start": T0 + dt, "end": T0 + dt + 3, "text": text})
    rows = {r["traffic_id"]: json.loads(r["data"]) for r in cad.store.q("SELECT traffic_id, data FROM atc_talkers")}
    ex = rows["HPNTower-3"]["exchange"]
    assert ex == {"id": "HPNTower-0", "subject": "JBU1234", "status": "confirmed", "hex": "a1b2c3", "activity": "landing",
                  "runway": "34", "n": 4}
    assert rows["HPNTower-2"]["talker"] is None and rows["HPNTower-2"]["exchange"]["id"] == "HPNTower-0"   # unlabelled, in it


# -- the CAD: the rows, the toggles, the map ---------------------------------------------------------------------------------


@pytest.fixture
def world():
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    cad.atc_talkers.observe(PLANES, T0)

    def say(i, text, dt=0.0):
        clock[0] = T0 + dt + 3
        t = {"id": f"HPNTower-{i}", "source": CH, "start": T0 + dt, "end": T0 + dt + 3, "text": text, "status": "done"}
        cad.comms.record(t)
        return cad.atc_talkers.heard(t)
    return cad, say, clock


def test_the_rows_carry_the_talker_and_never_a_radio_id(world):
    cad, say, clock = world
    say(1, "JetBlue one two three four, runway three four, cleared to land")
    say(2, "runway three four wind calm", 5)
    rows = {r["id"]: r for r in cad.comms_query({"type": ["radio"]})["rows"]}
    a = rows["HPNTower-1"]["atc"]
    assert a["talker"]["callsign"] == "TWR" and a["addressed"]["callsign"] == "JBU1234" and a["addressed"]["status"] == "confirmed"
    assert "not a radio ID" in a["note"] and "atc" not in rows["HPNTower-2"]                  # no callsign: nothing
    assert all(rows["HPNTower-1"][k] == "" for k in ("talker", "rig", "radio_id"))           # the registry's fields untouched
    assert cad.state()["atc"] == {"identify": True, "map": True, "channels": [CH]}


def test_the_toggles(world):
    cad, say, clock = world
    say(1, "JetBlue one two three four, runway three four, cleared to land")
    assert cad.atc_talkers.map_payload()["talking"][0]["hex"] == "a1b2c3"
    page = cad.settings_get()["atc"]
    assert page["identify"] == {"label": "Identify ATC talkers", "value": True, "default": True, "edited": False}
    assert page["map"]["label"] == "Show ATC talker on the map" and page["channels"] == [CH]
    cad.settings_post("atc", {"action": "set", "key": "map", "value": False, "operator": "Chief"})
    assert cad.atc_talkers.map_payload() is None and cad.state()["atc"]["map"] is False
    assert "atc" in cad.comms_query({"type": ["radio"]})["rows"][0]                           # the chips stay
    cad.settings_post("atc", {"action": "set", "key": "identify", "value": False, "operator": "Chief"})
    assert "atc" not in cad.comms_query({"type": ["radio"]})["rows"][0]                       # the raw transmissions
    assert say(2, "JetBlue one two three four, cleared to land", 10) is None and cad.state()["atc"]["identify"] is False
    assert any(r["key"] == "atc/identify" for r in cad.settings.audit_log("system"))
    cad.settings_post("atc", {"action": "reset", "key": "identify", "operator": "Chief"})
    assert "atc" in cad.comms_query({"type": ["radio"]})["rows"][-1] and cad.atc_talkers.identify
    with pytest.raises(CadError):
        cad.settings_post("atc", {"action": "set", "key": "identify", "value": "yes", "operator": "Chief"})
    with pytest.raises(CadError):
        cad.settings_post("atc", {"action": "set", "key": "ring", "value": True, "operator": "Chief"})
    exp = cad.settings_export()
    exp["overrides"]["atc"] = {"identify": "off"}
    with pytest.raises(CadError):
        cad.settings._check_import(exp)


def test_the_map_gets_who_talked_and_each_aircrafts_transmissions(world):
    cad, say, clock = world
    say(1, "cleared to land three four, JetBlue one two three four")
    say(2, "JetBlue one two three four, wind calm, runway three four, cleared to land", 8)
    m = cad.atc_talkers.map_payload()
    assert [(x["hex"], x["role"], x["status"]) for x in m["talking"]] == [("a1b2c3", "speaking", "confirmed"),
                                                                           ("a1b2c3", "addressed", "confirmed")]
    tx = m["tx"]["a1b2c3"]
    assert [x["id"] for x in tx] == ["HPNTower-2", "HPNTower-1"] and tx[0]["talker"] == "TWR" and "wind calm" in tx[0]["text"]
    clock[0] += T.TALK_HOLD_S + 1
    assert cad.atc_talkers.map_payload()["talking"] == []                      # the ring is for the moment it's in
    say(3, "Delta four five, contact ground", 30)
    assert "dd0045" not in cad.atc_talkers.map_payload()["tx"]                 # heard only: not on the map


def test_the_pre_alert_is_untouched(world):
    cad, say, clock = world
    n = len(cad.airport_alerts.state()["items"])
    r = say(1, "JetBlue one two three five, mayday mayday mayday, engine fire")
    assert r["role"] == "aircraft" and r["talker"]["status"] == "probable"     # the pilot declares it (#270)
    assert len(cad.airport_alerts.state()["items"]) == n                      # the talker raises and feeds nothing there


def test_the_app_identifies_hpn_tower_from_the_air(tmp_path):
    texts = {4: "JetBlue one two three four, Westchester Tower, runway three four, cleared to land"}   # 3 s + pre-roll + tail
    app = App(replace(cfg(tmp_path), channels=FIVE))
    app.stt.client = FakeWhisper(texts)
    app.stt.start()
    tk = app.cad.atc_talkers
    assert tk.channels == {"HPN Tower"} and app.aircraft.on_view is not None and app.aircraft.extra is not None
    tk.aircraft = lambda: PLANES
    rng = np.random.default_rng(2)
    sq = CarrierSquelch(3.5)
    seg = None
    for f in envelope([(5, 0.01, False), (3, 0.05, True), (1, 0.01, False)], rng):
        seg = sq.feed(f) or seg
    app._segment(Channel(118.575, "HPN Tower"), seg)
    assert wait(lambda: any("atc" in r for r in app.cad.comms_query({"type": ["radio"]})["rows"]), 10)
    r = next(r for r in app.cad.comms_query({"type": ["radio"]})["rows"] if r["channel"] == "HPN Tower")
    assert r["atc"]["talker"]["callsign"] == "TWR" and r["atc"]["addressed"]["hex"] == "a1b2c3"
    assert r["atc"]["carrier_db"] > 10                                          # the AM carrier, kept for the tower's level
    assert app.aircraft._extra()["tx"]["a1b2c3"][0]["id"] == r["id"]


# -- the console --------------------------------------------------------------------------------------------------------------


def test_the_console_shows_the_talker_like_unit_ids():
    cadjs = (STATIC / "cad.js").read_text()
    for s in ('confirmed: "src-confirmed", probable: "src-guessed", heard: "src-heard"', "`heard: ${cs}`",
              "`probable: ${cs}", "Speech-derived (the words heard), not a radio ID", "function atcChip(t)",
              "if (!r || !atcOn() || !r.talker) return null;", "AircraftLayer.focus(c.dataset.hex)",
              "function atcExchanges(items", "function fillExchange(li, g, opts)", "atcExchanges(shownItems",
              '"sizeup:playing"', "else if (!tag && t.kind !== \"note\""):
        assert s in cadjs, s
    rv = (STATIC / "radio-view.js").read_text()
    assert "atcExchanges(g.calls)" in rv and "atcSig(t)" in rv and "window.atcSig = atcSig;" in cadjs
    ac = (STATIC / "aircraft.js").read_text()
    for s in ('id: "ac-ring"', '"sizeup:playing"', "function focus(hex, opts)", "heardLines(d, a)", "» TALKING", "« TWR calling",
              "cad.state.atc.map === false", "return { load, maps, data: () => store.data, KEY, focus"):
        assert s in ac, s
    st = (STATIC / "settings.js").read_text()
    assert 'sPost("atc", { action: "set", key, value: cb.checked }' in st and "function sAtcTalkers(a)" in st
    css = (STATIC / "cad.css").read_text() + (STATIC / "cadmap.css").read_text()
    for c in (".atc-talk", ".atc-arrow", ".atc-fac", ".atc-ac", ".xch-head", ".xch-calls", ".xch-when", ".ac-tx", ".ac-heard-head"):
        assert c in css, c
    ml = (STATIC / "maplayers.js").read_text()
    assert [i for i, line in enumerate(ml.splitlines()) if "localStorage" in line and not line.lstrip().startswith("//")] \
        == [85, 120]                                                            # still only its two helpers
    api = (Path(__file__).resolve().parent.parent / "sizeup" / "cad" / "api.py").read_text()
    assert re.search(r"POST /api/cad/settings/atc\s", api)
