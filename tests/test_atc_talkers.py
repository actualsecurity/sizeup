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
    for s in ('id: "ac-ring"', '"sizeup:playing"', "function focus(hex)", "heardLines(d, a)", "» TALKING", "« TWR calling",
              "cad.state.atc.map === false", "return { load, maps, data: () => store.data, KEY, focus"):
        assert s in ac, s
    st = (STATIC / "settings.js").read_text()
    assert 'sPost("atc", { action: "set", key, value: cb.checked }' in st and "function sAtcTalkers(a)" in st
    css = (STATIC / "cad.css").read_text() + (STATIC / "cadmap.css").read_text()
    for c in (".atc-talk", ".atc-arrow", ".atc-fac", ".atc-ac", ".xch-head", ".xch-calls", ".xch-when", ".ac-tx", ".ac-heard-head"):
        assert c in css, c
    ml = (STATIC / "maplayers.js").read_text()
    assert [i for i, line in enumerate(ml.splitlines()) if "localStorage" in line and not line.lstrip().startswith("//")] \
        == [80, 106]                                                            # still only its two helpers
    api = (Path(__file__).resolve().parent.parent / "sizeup" / "cad" / "api.py").read_text()
    assert re.search(r"POST /api/cad/settings/atc\s", api)
