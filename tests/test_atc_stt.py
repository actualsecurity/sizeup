"""HPN Tower speech-to-text (GitHub #257): the written form of a transcript (sizeup/atc_normalize.py), the AM recorder's
pre-roll, AGC and weak-carrier threshold (sizeup/radio/am.py), the vocabulary and the whisper prompt, the weak-signal
flag and the tower's mark on the map. Transcripts as heard live on HPN Tower (public ATC) and synthetic audio only."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import atc
from sizeup import atc_talkers as T
from sizeup.aircraft import Plane, Snapshot
from sizeup.app import App
from sizeup.atc_normalize import fixes, normalize
from sizeup.cad.service import Cad, Settings
from sizeup.radio import am
from sizeup.radio.am import CarrierSquelch, agc
from sizeup.radio.fm import FRAME, RATE, Channel
from tests.test_app import FakeWhisper, cfg, wait
from tests.test_atc import FIVE, envelope

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
T0 = 1_800_000_000.0
CH = "HPN Tower"
IX = T.load_index()


# -- the written form ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("heard, written", [
    # HPN Tower, 2026-11-06 (Amazon Transcribe, sizeup-atc): the issue's examples
    ("Insight 34 Charlie.", "Insight 34 Charlie."),                          # nothing known to fix: left as heard
    ("All right, Clearly 34 Moxie 283.", "All right, Clearly 34 Moxy 283."),
    ("A White Plains tower exits at 891, cleared for the RNA 3-4 Yankee over Taish.",
     "Westchester Tower exits at 891, cleared for the RNAV Yankee 34 over TAYSH."),
    ("Chester Tower 560. Mike was over to do for the GPS or GPS 34.",
     "Westchester Tower 560. Mike was over TOEDO for the RNAV Yankee 34."),
    ("Westchester Tower citation 568 Lima X-ray 1 mile from Twarf inbound for the Rav Yankee Runway 34.",
     "Westchester Tower citation 568LX 1 mile from TWORF inbound for the RNAV Yankee 34."),
    ("Tower shield 71534.", "Tower shield 71534."),
    ("Westchester Towers 203 on the R9834.", "Westchester Tower 203 on the RNAV Yankee 34."),
    ("Fairlane 34 jet speed 168.", "Fairlane 34 Jet Speed 168."),
    ("34 clear to landing such 583.", "34 cleared to land such 583."),
    ("Sun Country 8766", "Sun Country 8766"),                                 # already right: untouched
    # the forms the issue names
    ("R nav yankee 34", "RNAV Yankee 34"),
    ("cleared R-nav Yankee runway three four", "Cleared RNAV Yankee 34"),
    ("on the rn of yankee runway three four", "On the RNAV Yankee 34"),
    ("cleared RNAV GPS Zulu runway 1-6", "Cleared RNAV Zulu 16"),
    ("RNAV 34", "RNAV 34"),                                                   # a letter never heard isn't added
    ("cleared ILS runway one six approach", "Cleared ILS 16 approach"),
    ("localizer 34", "LOC 34"),
    ("Lima X-ray", "LX"),
    ("November one two three alfa bravo, wind calm", "N123AB, wind calm"),
    ("clear to land runway 34", "Cleared to land runway 34"),
    ("climb to land flexjet five three eight", "Cleared to land Flexjet 538"),
    ("line up in wait runway one six", "Line up and wait runway 16"),
    ("Jet blue 12 34, contact departure", "JetBlue 1234, contact departure"),
    ("Cessna 4 5 6 Papa Romeo, roger", "Cessna 456PR, roger"),
    ("lufthansa tower good morning", "Westchester Tower good morning"),       # the ATC model's European airports
    ("cross Twarf at 2000", "Cross TWORF at 2000"),
    ("direct haarp", "Direct HAARP"),
    ("descend via tworff", "Descend via TWORF"),
])
def test_the_written_form(heard, written):
    assert normalize(heard) == written
    assert normalize(written) == written                                      # idempotent


@pytest.mark.parametrize("text", [
    "taxi via alfa bravo hold short of runway 16",       # a route, not a tail
    "we'd like to do a touch and go",                    # "to do" is a fix (TOEDO) only after over / at / from ...
    "the harp seal",                                     # likewise HAARP
    "contact ground point niner",                        # "ground" isn't a facility without a name before it
    "the tower has the numbers",
    "golf five zero three romeo papa romeo six six four",   # three letters can't end a US tail
    "Delta 45, roger",
])
def test_the_written_form_never_invents(text):
    out = normalize(text)
    assert "TOEDO" not in out and "HAARP" not in out and "Westchester" not in out and not re.search(r"\d[A-Z]{3}", out)
    if text.startswith(("taxi", "Delta")):
        assert out.lower() == text.lower().replace("niner", "9")


def test_the_fixes_are_hpns_approach_fixes():
    procs = json.loads((ROOT / "data" / "airport" / "khpn_procedures.geojson").read_text())
    cifp = {f["properties"]["ident"] for f in procs["features"] if f["properties"].get("kind") == "approach_fix"}
    assert {"TWORF", "TAYSH", "HAARP", "ISLET", "TOEDO", "CZIMR"} <= set(fixes()) <= cifp
    data = json.loads((ROOT / "data" / "atc" / "normalize.json").read_text())
    assert set(data["fixes"]) | set(data["fixes_in_context"]) <= cifp
    assert normalize("over taish") == "Over TAYSH" and normalize("at islet") == "At ISLET" and normalize("islet") == "Islet"


def test_the_matcher_reads_the_written_form():
    """The written tail ("568LX", "N123AB") is the same callsign to sizeup/atc.py as the spoken one, and a tail's letters
    end it: "568LX 1 mile" is 568LX."""
    assert atc.words_of("Citation 568LX") == ["citation", "5", "6", "8", "lima", "xray"]
    assert atc.words_of("N123AB") == ["november", "1", "2", "3", "alfa", "bravo"]
    planes = [{"hex": "a1", "flight": "N568LX", "reg": "N568LX", "dist": 3.0},
              {"hex": "a2", "flight": "MXY283", "dist": 2.0}, {"hex": "a3", "flight": "EJM168", "dist": 5.0}]
    heard = "Westchester Tower citation 568 Lima X-ray 1 mile from Twarf inbound for the Rav Yankee Runway 34."
    a = atc.attribute(normalize(heard), IX, planes)
    assert a.hex == "a1" and a.spoken == "…568LX"
    assert atc.attribute("All right, Clearly 34 Moxie 283.", IX, planes) is None              # as heard: no callsign
    assert atc.attribute(normalize("All right, Clearly 34 Moxie 283."), IX, planes).callsign == "MXY283"
    assert atc.attribute(normalize("november five six eight lima xray"), IX, planes).confidence == 1.0


def test_the_talkers_match_the_written_form_and_the_rows_show_it():
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    cad.atc_talkers.observe([{"hex": "a2", "flight": "MXY283", "phase": "arriving", "dist": 2.0}], T0)
    t = {"id": "HPNTower-1", "source": CH, "start": T0, "end": T0 + 3, "text": "All right, Clearly 34 Moxie 283.",
         "status": "done", "carrier_db": 4.2}
    cad.comms.record(t)
    clock[0] = T0 + 3
    rec = cad.atc_talkers.heard(t)
    assert rec["aircraft"]["callsign"] == "MXY283" and rec["aircraft"]["status"] == "confirmed"
    row = next(r for r in cad.comms_query({"type": ["radio"]})["rows"] if r["id"] == "HPNTower-1")
    assert row["text"] == "All right, Clearly 34 Moxie 283."                 # the transcript itself is kept as heard
    # the written form, then the callsign snapped to the aircraft on ADS-B (Breeze, MOXY), the snap kept for the hover
    assert row["atc_text"] == "All right, Clearly 34 MXY283."
    assert row["atc_snaps"] == [{"from": "Moxy 283", "to": "MXY283", "hex": "a2", "score": 1.0}]
    assert row["atc_weak"] is True and row["carrier_db"] == 4.2              # a marginal carrier: flagged
    # a strong one isn't flagged, and identify off still shows the written form (it's spelling, not a talker)
    t2 = dict(t, id="HPNTower-2", start=T0 + 10, end=T0 + 12, carrier_db=14.0, text="clear to land runway 34")
    cad.comms.record(t2)
    cad.atc_talkers.heard(t2)
    cad.settings_post("atc", {"action": "set", "key": "identify", "value": False, "operator": "Chief"})
    row2 = next(r for r in cad.comms_query({"type": ["radio"]})["rows"] if r["id"] == "HPNTower-2")
    assert row2["atc_text"] == "Cleared to land runway 34" and "atc_weak" not in row2 and "atc" not in row2


# -- the AM recorder: pre-roll, AGC, the weak-carrier threshold -------------------------------------------------------


def stream(late_s: float, rng, voice_s: float = 1.5, open_level: float = 0.05):
    """Frames of an AM envelope: idle, then a voice whose carrier sits just under the open level for late_s (a weak start)
    before it comes up. Returns (frames, the voice's first sample in the stream)."""
    idle = int(5 * RATE)
    n = int(voice_s * RATE)
    total = idle + n + int(1.5 * RATE)
    carrier = np.zeros(total)
    carrier[idle:idle + n] = open_level
    carrier[idle:idle + int(late_s * RATE)] = 0.004                    # +3.2 dB: under the +3.5 dB open level
    t = np.arange(total) / RATE
    voice = np.zeros(total)
    voice[idle:idle + n] = 0.5 * np.sin(2 * np.pi * 500 * t[idle:idle + n])
    env = 0.01 + carrier * (1 + voice) + 0.0003 * rng.standard_normal(total)
    return [env[k:k + FRAME] for k in range(0, total - FRAME + 1, FRAME)], idle


def first_segment(frames):
    now = [0.0]
    sq = CarrierSquelch(3.5, clock=lambda: now[0])
    for k, f in enumerate(frames):
        now[0] = k * FRAME / RATE
        s = sq.feed(f)
        if s is not None:
            return s
    return None


def test_the_pre_roll_keeps_the_first_syllable():
    """The squelch opens 0.3 s into the voice (its carrier under the open level until then): the 0.4 s pre-roll keeps the
    start of it; the old 0.2 s lost the first 0.1 s ("Chester Tower" for "Westchester Tower")."""
    assert am.PREROLL_FRAMES * FRAME / RATE == pytest.approx(0.4) and am.TAIL_FRAMES * FRAME / RATE == pytest.approx(0.2)
    frames, voice_at = stream(0.3, np.random.default_rng(5))
    s = first_segment(frames)
    assert s is not None and s.start * RATE <= voice_at                     # the segment starts before the voice
    assert s.preroll_s == pytest.approx(0.4)
    open_at = voice_at + int(0.3 * RATE)
    assert open_at - s.start * RATE == pytest.approx(0.4 * RATE, abs=FRAME)


def test_a_blip_is_counted_on_its_carrier_not_its_pre_roll():
    """MIN_FRAMES (0.6 s) is the carrier's own length: 0.45 s of carrier with 0.4 s of pre-roll is still a blip."""
    rng = np.random.default_rng(6)
    sq = CarrierSquelch(3.5)
    got = [s for f in envelope([(5, 0.01, False), (0.45, 0.05, True), (2, 0.01, False)], rng) if (s := sq.feed(f))]
    assert got == []
    sq = CarrierSquelch(3.5)
    got = [s for f in envelope([(5, 0.01, False), (0.7, 0.05, True), (2, 0.01, False)], rng) if (s := sq.feed(f))]
    assert len(got) == 1 and got[0].end - got[0].start == pytest.approx(0.4 + 0.7 + 0.2, abs=0.1)


def test_am_agc_holds_the_voice_through_a_fading_carrier():
    """A carrier fading 12 dB through one transmission: without AGC the voice fades with it; with it (the envelope over
    its own carrier) the voice keeps its level. A steady carrier comes out as before (DC removed)."""
    t = np.arange(int(3 * RATE)) / RATE
    carrier = 0.05 * 10 ** (-12 / 20 * t / t[-1])                     # 0 dB down to -12 dB
    voice = 0.5 * np.sin(2 * np.pi * 700 * t)
    env = 0.002 + carrier * (1 + voice)

    def level(x, a, b):                                                # the voice band's level (over 300 Hz)
        X = np.fft.rfft(x - x.mean())
        X[np.fft.rfftfreq(len(x), 1 / RATE) < 300] = 0
        seg = np.fft.irfft(X, len(x))[int(a * RATE):int(b * RATE)]
        return 20 * np.log10(np.sqrt(np.mean(seg ** 2)))
    raw = env
    out = agc(env, float(np.median(env)))
    assert level(raw, 0.2, 0.7) - level(raw, 2.3, 2.8) > 7               # the fade, as recorded (8.4 dB here)
    assert abs(level(out, 0.2, 0.7) - level(out, 2.3, 2.8)) < 1.5        # held by the AGC
    steady = 0.05 * (1 + voice)
    o2 = agc(steady, 0.05)
    assert abs(float(o2.mean())) < 1e-6 and np.allclose(o2[800:-800], (steady - 0.05)[800:-800], atol=2e-3)


@pytest.mark.parametrize("level, weak, marginal", [(0.0133, True, False), (0.0165, False, True), (0.03, False, False)])
def test_the_weak_carrier_threshold(level, weak, marginal):
    """Under 3 dB: not transcribed (speech-to-text invents words from noise). 3-6 dB (was "not transcribed" under 5):
    transcribed and flagged. Over 6 dB: transcribed."""
    rng = np.random.default_rng(7)
    sq = CarrierSquelch(3.5)
    plan = [(5, 0.01, False), (0.1, 0.0155, True), (2, level, True), (2, 0.01, False)]
    segs = [s for f in envelope(plan, rng) if (s := sq.feed(f)) is not None]
    assert len(segs) == 1
    s = segs[0]
    assert (s.weak, s.marginal) == (weak, marginal), s.quieting_db
    assert am.MIN_CARRIER_DB == 3.0 and am.MARGINAL_DB == 6.0 and am.MIN_CARRIER_DB < am.SQUELCH_DB


def test_a_marginal_transmission_is_transcribed_and_flagged(tmp_path):
    texts = {3: "JetBlue one two three four, Westchester Tower, runway three four, clear to land"}
    app = App(replace(cfg(tmp_path), channels=FIVE))
    app.stt.client = FakeWhisper(texts)
    app.stt.start()
    sq = CarrierSquelch(3.5)
    seg = None
    for f in envelope([(5, 0.01, False), (2.4, 0.0165, True), (1, 0.01, False)], np.random.default_rng(3)):
        seg = sq.feed(f) or seg
    assert seg.marginal and not seg.weak
    app._segment(Channel(118.575, CH), seg)
    assert wait(lambda: any(r.get("atc_text") for r in app.cad.comms_query({"type": ["radio"]})["rows"]), 10)
    r = next(r for r in app.cad.comms_query({"type": ["radio"]})["rows"] if r["channel"] == CH)
    assert r["status"] == "done" and r["atc_weak"] is True and 3 <= r["carrier_db"] < 6
    assert r["atc_text"] == "JetBlue 1234, Westchester Tower, runway 34, cleared to land"


# -- the vocabulary and the whisper prompt ----------------------------------------------------------------------------


def test_the_vocabulary_has_hpns_approaches_fixes_and_telephony():
    from sizeup.transcribe import build_vocabulary
    v = atc.vocabulary()
    built = build_vocabulary(v["phrases"])
    said = {p.lower() for p in built["phrases"]}
    assert not built["skipped"] and len(built["phrases"]) <= 500
    assert {"r.-nav-yankee-runway-three-four", "i.l.s.-runway-one-six", "tworf", "taysh", "haarp", "sun-country",
            "netjets"} <= said
    assert "RNAV (GPS) Yankee runway three four" in v["prompt"] and "TWORF" not in v["prompt"]   # fixes: vocabulary only
    assert len(v["prompt"]) < atc.PROMPT_MAX - 150                        # room left for the callsigns


def test_a_station_prompts_with_its_own_adsb(tmp_path):
    """A station forwarding to the hub doesn't read the Aircraft layer: its whisper prompt takes the callsigns from its
    own receiver (the forwarder's last picture): near HPN, under 10,000 ft, nearest first."""
    import time
    app = App(replace(cfg(tmp_path), channels=FIVE))
    near = Plane("a1", flight="JBU1234 ", lat=41.08, lon=-73.71, alt_baro=1500)
    gnd = Plane("a2", flight="EJA521", lat=41.067, lon=-73.708, alt_baro="ground")
    high = Plane("a3", flight="UAL9", lat=41.07, lon=-73.70, alt_baro=35000)
    far = Plane("a4", flight="DAL1", lat=42.5, lon=-73.7, alt_baro=3000)
    anon = Plane("a5", lat=41.07, lon=-73.70, alt_baro=2000)
    app.station = SimpleNamespace(aircraft=SimpleNamespace(last=(Snapshot(time.time(), (near, gnd, high, far, anon)),
                                                                 time.time())))
    got = app._atc_aircraft()
    assert [a["flight"] for a in got] == ["EJA521", "JBU1234"]
    assert "execjet five two one, jetblue one two three four" in app._atc_prompt()
    app.station.aircraft.last = (app.station.aircraft.last[0], time.time() - 120)    # an old picture: none
    assert app._atc_aircraft() == []


# -- the tower on the map ---------------------------------------------------------------------------------------------


def test_the_tower_is_where_the_airport_data_says():
    lat, lon = T.tower_site()
    assert lat == pytest.approx(41.0627, abs=0.001) and lon == pytest.approx(-73.7110, abs=0.001)


def test_the_tower_mark_is_lit_while_the_tower_transmits():
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    cad.atc_talkers.observe([{"hex": "a1b2c3", "flight": "JBU1234", "phase": "arriving", "dist": 4.0}], T0)

    def say(i, text, dt):
        clock[0] = T0 + dt + 3
        t = {"id": f"HPNTower-{i}", "source": CH, "start": T0 + dt, "end": T0 + dt + 3, "text": text, "status": "done"}
        cad.comms.record(t)
        return cad.atc_talkers.heard(t)
    tw = cad.atc_talkers.map_payload()["tower"]
    assert tw["until"] == 0 and tw["tx"] == [] and tw["lat"] == pytest.approx(41.0627, abs=0.001)
    say(1, "Westchester Tower, JetBlue one two three four, ten miles south, inbound", 0)    # the aircraft: not lit
    assert cad.atc_talkers.map_payload()["tower"]["until"] == 0
    say(2, "JetBlue one two three four, Westchester Tower, runway three four, cleared to land", 5)
    tw = cad.atc_talkers.map_payload()["tower"]
    assert tw["until"] > clock[0] and tw["tx"] == ["HPNTower-2"]
    clock[0] += T.TALK_HOLD_S + 1
    tw = cad.atc_talkers.map_payload()["tower"]
    assert tw["until"] < clock[0] and tw["tx"] == ["HPNTower-2"]           # dim again; its transmission still playable
    cad.settings_post("atc", {"action": "set", "key": "map", "value": False, "operator": "Chief"})
    assert cad.atc_talkers.map_payload() is None                          # "Show ATC talker on the map" off: no mark


def test_the_map_draws_the_tower_mark():
    js = (STATIC / "aircraft.js").read_text()
    assert '"ac-twr"' in js and '"ac-twr-label"' in js and '"text-field": ["get", "name"]' in js
    assert 'name: tw.open === false ? "CTAF" : "TWR"' in js                  # #275: closed, the mark says CTAF
    assert "towerLit(d)" in js and "tw.until" in js and "(tw.tx || []).includes(talk.playing)" in js
    assert 'const IDS = ["ac-trail", "ac-ring", "ac-mark", "ac-label", "ac-twr", "ac-twr-label", ' in js   # on with the layer
    assert "localStorage" not in js
    cad_js = (STATIC / "cad.js").read_text()
    assert "t.atc_text || t.text" in cad_js and '"atc-weak"' in cad_js and "As transcribed: " in cad_js


def test_the_eval_tool_runs_offline():
    from tools import atc_eval
    out = atc_eval.run_normalizer()
    assert out["wer_normalised"] < out["wer_raw"] / 4 and out["callsign_normalised"] > out["callsign_raw"]


# -- callsigns snapped to ADS-B (sizeup/atc_snap.py) ------------------------------------------------------------------

from sizeup.atc_snap import snap, telephony  # noqa: E402

MXY = {"hex": "m1", "flight": "MXY283", "phase": "arriving", "dist": 4.0}
SCX = {"hex": "s1", "flight": "SCX8766", "phase": "arriving", "dist": 6.0}
N534C = {"hex": "n1", "flight": "N534C", "reg": "N534C", "phase": "departing", "dist": 3.0}
JBU = {"hex": "j1", "flight": "JBU1234", "phase": "arriving", "dist": 8.0}


def test_moxy_is_breeze():
    assert telephony()["MXY"] == ("moxy",) and "netjets" in telephony()["EJA"]


@pytest.mark.parametrize("heard, planes, said, snapped", [
    ("All right, Clearly 34 Moxie 283.", [MXY, SCX, JBU], "All right, Clearly 34 MXY283.", ("Moxy 283", "MXY283")),
    ("Sun Country 876 cleared to land", [MXY, SCX], "SCX8766 cleared to land",
     ("Sun Country 876", "SCX8766")),                                       # one digit dropped
    ("Insight 34 Charlie.", [N534C, MXY], "Insight N534C.", ("34 Charlie", "N534C")),   # a shortened tail
    ("Jet blue 12 35, roger", [JBU, MXY], "JBU1234, roger", ("JetBlue 1235", "JBU1234")),
    ("JetBlue 1234 cleared to land", [JBU], "JBU1234 cleared to land", ("JetBlue 1234", "JBU1234")),
])
def test_a_callsign_nearly_heard_is_snapped(heard, planes, said, snapped):
    out, snaps = snap(normalize(heard), planes)
    assert out == said and [(x["from"], x["to"]) for x in snaps] == [snapped]


@pytest.mark.parametrize("heard, planes", [
    ("Moxy 283", []),                                                       # no aircraft near: nothing
    ("Moxy 283", [MXY, dict(MXY, hex="m2", flight="MXY288")]),              # two close: never picked between
    ("34 cleared to land such 583.", [MXY, SCX, JBU]),                      # nothing sounds like it
    ("Tower shield 71534.", [MXY, SCX, N534C]),
    ("Sun Country 666", [SCX]),                                             # two digits off: not clear
    ("runway 34, wind 270 at 5", [N534C, dict(N534C, hex="n2", flight="N270", reg="N270")]),   # digits alone: no tail
    ("Moxy 283", [dict(MXY, dist=40.0)]),                                   # not near HPN
])
def test_never_snapped_without_a_clear_winner(heard, planes):
    assert snap(heard, planes) == (heard, [])


def test_a_snapped_near_miss_is_probable_and_the_row_shows_it():
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    cad.atc_talkers.observe([JBU, MXY], T0)
    t = {"id": "HPNTower-1", "source": CH, "start": T0, "end": T0 + 3, "text": "Jet blue 12 35, runway 34, cleared to land",
         "status": "done", "carrier_db": 12.0}
    cad.comms.record(t)
    clock[0] = T0 + 3
    rec = cad.atc_talkers.heard(t)
    assert rec["addressed"]["callsign"] == "JBU1234" and rec["addressed"]["status"] == "probable"   # never confirmed
    assert "snapped to ADS-B" in rec["addressed"]["why"] and rec["snaps"][0]["to"] == "JBU1234"
    row = next(r for r in cad.comms_query({"type": ["radio"]})["rows"] if r["id"] == "HPNTower-1")
    assert row["text"] == t["text"] and row["atc_text"] == "JBU1234, runway 34, cleared to land"
    assert row["atc_snaps"][0]["from"] == "JetBlue 1235"
    js = (STATIC / "cad.js").read_text()
    assert "Callsign matched to ADS-B" in js and "t.atc_snaps" in js


def test_the_station_and_all_in_one_whisper_prompt_has_the_nearby_callsigns(tmp_path):
    """#238's prompt, checked (#257): on the CAD of record from the Aircraft layer's picture, arrivals first."""
    import time
    app = App(replace(cfg(tmp_path), channels=FIVE))
    app.aircraft.st["fetched"] = time.time()
    app.aircraft.view = [dict(MXY), dict(JBU, phase="low", dist=0.5)]
    p = app._atc_prompt()
    assert p.startswith("Westchester Tower") and p.endswith("moxy two eight three, jetblue one two three four")


# -- the vocabulary refreshed from the traffic (sizeup/atc_vocab.py) --------------------------------------------------

from sizeup.atc_vocab import EVERY_S, WINDOW_S, Operators, Refresher, names  # noqa: E402


class FakeVocab:
    def __init__(self):
        self.st = {"state": "READY", "sig": "", "updated": None}
        self.pushed = []

    def state(self):
        return dict(self.st)

    def push(self, phrases):
        from sizeup.transcribe import build_vocabulary
        import hashlib
        self.pushed.append(list(phrases))
        sig = hashlib.sha1(json.dumps(build_vocabulary(phrases)["phrases"]).encode()).hexdigest()[:12]
        self.st.update(state="PENDING", sig=sig, updated=self.clock[0])
        return self.state()


def refresher(clock, on=True, lead=True):
    meta = {}
    ops = Operators(meta.get, meta.__setitem__, lambda: clock[0])
    v = FakeVocab()
    v.clock = clock
    pend = []
    r = Refresher(v, ops, lambda: ["Westchester Tower", "runway three four"], enabled=lambda: on[0] if isinstance(on, list) else on,
                  leader=lambda: lead[0] if isinstance(lead, list) else lead, on_pending=lambda: pend.append(1),
                  clock=lambda: clock[0], log=lambda m: None, background=False)
    return r, v, ops, meta, pend


def test_the_vocabulary_follows_the_operators_seen():
    clock = [T0]
    r, v, ops, meta, pend = refresher(clock)
    ops.add([MXY, JBU, N534C, {"hex": "x", "flight": "ZZZ1"}], T0)
    assert set(names(ops.recent())) == {"Moxy", "Jetblue"}                 # airline callsigns only; ZZZ has no name
    assert r.tick() == "pushed" and pend == [1]                     # new streams without it until READY
    assert v.pushed[0][:2] == ["Westchester Tower", "runway three four"] and {"Moxy", "Jetblue"} <= set(v.pushed[0])
    clock[0] += 60
    assert r.tick() == "not yet"                                    # at most one push every EVERY_S
    clock[0] += EVERY_S
    assert r.tick() == "pending"                                    # AWS still building it: never pushed over it
    v.st["state"] = "READY"
    assert r.tick() == "unchanged" and len(v.pushed) == 1           # the same set: nothing sent
    ops.add([SCX], clock[0])
    clock[0] += EVERY_S + 1
    assert r.tick() == "pushed" and "Sun Country" in v.pushed[-1]
    clock[0] += WINDOW_S + EVERY_S + 1                              # two days on, nobody seen: back to the fixed list
    v.st["state"] = "READY"
    assert r.tick() == "pushed" and v.pushed[-1] == ["Westchester Tower", "runway three four"]
    assert ops.recent() == []


def test_the_vocabulary_refresh_is_the_leaders_and_can_be_switched_off():
    clock = [T0]
    on, lead = [True], [False]
    r, v, ops, meta, pend = refresher(clock, on, lead)
    ops.add([MXY], T0)
    assert r.tick() == "standby" and not v.pushed                    # a standby hub task (#254's fence): never
    lead[0], on[0] = True, False
    assert r.tick() == "off" and not v.pushed
    on[0] = True
    assert r.tick() == "pushed"
    assert json.loads(meta["atc_operators_seen"]) == {"MXY": T0}     # kept across restarts
    assert Operators(meta.get, meta.__setitem__, lambda: T0).recent() == ["MXY"]


def test_the_vocabulary_switch_in_settings():
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None)
    page = cad.settings_get()["atc"]
    assert page["vocab"] == {"label": "Refresh the ATC vocabulary from the traffic", "value": True, "default": True,
                             "edited": False}
    cad.settings_post("atc", {"action": "set", "key": "vocab", "value": False, "operator": "Chief"})
    assert cad.atc_vocab_refresh is False
    js = (STATIC / "settings.js").read_text()
    assert '["identify", "map", "vocab"]' in js


def test_the_app_refreshes_only_as_the_leader_and_holds_new_streams_while_pending(tmp_path):
    app = App(replace(cfg(tmp_path), channels=FIVE))
    assert app.atc_refresh is None                                   # no Amazon Transcribe here: nothing to refresh
    app.aws = SimpleNamespace(channel_vocabulary={CH: "sizeup-atc", "FG1": "sizeup-pcfd"})
    app._atc_vocab_pending()
    assert app.aws.channel_vocabulary == {"FG1": "sizeup-pcfd"}
    app._atc_vocab_ready("sizeup-atc")
    assert app.aws.channel_vocabulary[CH] == "sizeup-atc"
    assert app._atc_leader() is True
    app.fence = SimpleNamespace(ok=lambda: False)
    assert app._atc_leader() is False


# -- HPN Tower live, after #256 deployed ------------------------------------------------------------------------------

EJA505 = {"hex": "e5", "flight": "EJA505", "phase": "arriving", "dist": 5.0}
JBU665 = {"hex": "j6", "flight": "JBU665", "phase": "arriving", "dist": 6.0}
RPA413 = {"hex": "r4", "flight": "RPA413", "phase": "departing", "dist": 7.0}
LIVE = [EJA505, JBU665, RPA413, MXY]


@pytest.mark.parametrize("heard, said, snapped", [
    ("Tower Executive 505. Good afternoon. R I Vy runway 34 for netchets.",
     "Tower EJA505. Good afternoon. RNAV Yankee 34 for NetJets.", "EJA505"),        # EXECJET: NetJets
    ("34 C clear line that you 505.", "34 C cleared to land that you EJA505.", "EJA505"),
    ("Tower of jet 665.", "Tower of JBU665.", "JBU665"),                             # the name lost: the number alone
    ("Turbo 5 cleared to land runway 34 665.", "Turbo 5 cleared to land runway 34 JBU665.", "JBU665"),
    ("Partnership 413.", "RPA413.", "RPA413"),
])
def test_live_samples_after_256(heard, said, snapped):
    out, snaps = snap(normalize(heard), LIVE)
    assert out == said and [x["to"] for x in snaps] == [snapped]
    a = atc.attribute(out, IX, LIVE)
    assert a is not None and a.callsign == snapped


@pytest.mark.parametrize("heard", [
    "Over to 20.83 Charles. Have a good day.",            # a frequency: no tail ends in "Charles"
    "climb and maintain 413", "runway 413", "413 feet", "heading 505", "contact departure 120.665",
    "the executive director",
])
def test_a_number_alone_is_never_a_runway_altitude_or_frequency(heard):
    assert snap(normalize(heard), LIVE)[1] == []
    assert normalize("the executive director") == "The executive director"


def test_a_number_alone_needs_the_only_aircraft_with_it():
    assert snap("Tower of jet 665.", [JBU665, dict(JBU665, hex="x", flight="SWA665")]) == ("Tower of jet 665.", [])


def test_each_transmission_logs_its_talker():
    lines = []
    clock = [T0]
    cad = Cad(Settings(airband=(CH,)), log=lambda m: None, clock=lambda: clock[0])
    cad.atc_talkers.log = lines.append
    cad.atc_talkers.observe(LIVE, T0)
    for i, text in enumerate(["Execjet 505, Westchester Tower, runway 34, cleared to land", "Partnership 413.", "roger"]):
        t = {"id": f"HPNTower-{i}", "source": CH, "start": T0 + 5 * i, "end": T0 + 5 * i + 3, "text": text, "status": "done"}
        cad.comms.record(t)
        clock[0] = T0 + 5 * i + 3
        cad.atc_talkers.heard(t)
    atc_lines = [x for x in lines if x.startswith("atc: ")]
    assert atc_lines == ["atc: HPNTower-0: talker TWR → EJA505 confirmed (snapped “Execjet 505”)",
                         "atc: HPNTower-1: talker RPA413 probable (snapped “Partnership 413”)",
                         "atc: HPNTower-2: no callsign heard"]


def test_airband_rows_never_show_the_radio_id_chip():
    js = (STATIC / "cad.js").read_text()
    assert "!isAirband(t)) meta.append(noIdChip(t))" in js
    assert "const isAirband = (t) =>" in js and "cad.state.atc.channels || []).includes(t.channel)" in js


# -- #256 on the hub: a loose telephony never stands alone; departures handed off ----------------------------------------


def test_a_loose_telephony_is_never_shown_as_heard():
    """"partial radio 210" read as YSY210 (FAA JO 7340.2's AERONAUTICAL STATION: RADIO): not an HPN operator, one generic
    word, not on ADS-B -> nothing labelled. On ADS-B it would be corroborated (and shown)."""
    text = "Pret turning right, going to partial radio 210."
    assert T.analyze(T.Tx("t1", CH, T0, T0 + 3, text), IX, []) is None
    on = T.analyze(T.Tx("t1", CH, T0, T0 + 3, text), IX, [{"hex": "y1", "flight": "YSY210", "phase": "low", "dist": 5.0}])
    assert on["aircraft"]["callsign"] == "YSY210" and on["aircraft"]["status"] == "confirmed"
    # what #256 got right stays right: an ADS-B-confirmed telephony, and HPN's own operators heard off ADS-B
    rbi = T.analyze(T.Tx("t2", CH, T0, T0 + 3, "Right turn 320, 3000 Major League One."), IX,
                    [{"hex": "r1", "flight": "RBI1", "phase": "departing", "dist": 3.0}])
    assert rbi["aircraft"]["callsign"] == "RBI1" and rbi["aircraft"]["status"] == "confirmed"
    assert T.analyze(T.Tx("t3", CH, T0, T0 + 3, "Delta four five, contact ground"), IX, [])["aircraft"]["status"] == "heard"


@pytest.mark.parametrize("heard, planes, said", [
    ("In the right turn and over to departure XY 450.", [dict(MXY, flight="SWA450", phase="departing", hex="w4")],
     "In the right turn and over to departure SWA450."),
    ("Departure sky 51.", [dict(MXY, flight="SKW51", phase="departing", hex="k5")], "Departure SKW51."),
])
def test_departures_handed_off_are_snapped_by_number(heard, planes, said):
    assert snap(normalize(heard), planes)[0] == said


def test_a_two_digit_number_alone_is_only_a_departure_handed_off():
    arriving = [dict(MXY, flight="UAL51", phase="arriving", hex="u5")]
    assert snap("Departure xy 51.", arriving)[1] == []                 # not departing
    assert snap("Tower xy 51.", [dict(arriving[0], phase="departing")])[1] == []   # no hand-off


# -- a runway, heading ... number run into the tail after it (speech-to-text merging "runway 34, 7KZ") ----------------
# Invented wording and tails only.

N687KZ = {"hex": "c1", "flight": "N687KZ", "reg": "N687KZ", "phase": "arriving", "dist": 3.0}


def mentions(text: str) -> list[tuple[str, bool]]:
    return [(m.text, m.after_number) for m in atc.find_mentions(T.clauses(normalize(text)), IX)]


def test_the_airports_runways_come_from_the_airport_data():
    assert {"16", "34", "11", "29"} <= atc.runway_designators()
    assert atc.split_runway("347") == ("34", "7") and atc.split_runway("165") == ("16", "5")
    assert atc.split_runway("1634") == ("16", "34") and atc.split_runway("34") is None
    assert atc.split_runway("785") is None                       # not one of this airport's runways
    assert atc.runways("go ahead runway 347 kilo zulu") == ["34"]


@pytest.mark.parametrize("heard, written, found", [
    ("go ahead runway 347 kilo zulu", "Go ahead runway 34, 7 kilo zulu", [("…7KZ", True)]),
    ("runway 165 alfa bravo", "Runway 16, 5 alfa bravo", [("…5AB", True)]),
    ("runway three four seven kilo zulu", "Runway 34, 7 kilo zulu", [("…7KZ", True)]),
    ("heading 3407 kilo zulu", "Heading 340, 7 kilo zulu", [("…7KZ", True)]),
    ("runway 34 cleared to land", "Runway 34 cleared to land", []),
    ("runway 1634", "Runway 1634", []),                         # nothing after it: left as heard
    ("runway 165", "Runway 165", []),
    ("runway 290", "Runway 290", []),                           # the rest can't start a tail (no leading 0)
    ("runway 290 alfa bravo", "Runway 290 alfa bravo", []),
    ("squawk 12001", "Squawk 12001", []),
    ("runway 160 cleared to land", "Runway 160 cleared to land", []),
    ("runway 34123 alfa bravo", "Runway 34123 alfa bravo", [("…123AB", True)]),   # a longer rest: kept together, still flagged
    ("climb and maintain 2000 alfa bravo", "Climb and maintain 2000 alfa bravo", []),
    ("heading 340 November 123AB", "Heading 340 N123AB", [("N123AB", False)]),
    ("November 347KZ", "N347KZ", [("N347KZ", False)]),
    ("November three four seven kilo zulu, runway 34", "N347KZ, runway 34", [("N347KZ", False)]),
    ("7 kilo zulu, runway 34 cleared to land", "7KZ, runway 34 cleared to land", [("…7KZ", False)]),
    ("contact departure 678 alfa bravo", "Contact departure 678AB", [("…678AB", False)]),   # not a frequency
    # a whole tail after a frequency word or a complete number keeps its normal confidence (not after_number)
    ("contact departure 123AB", "Contact departure 123AB", [("…123AB", False)]),
    ("contact departure 1 2 3 alfa bravo", "Contact departure 1 2 3AB", [("…123AB", False)]),
    ("monitor 123 alfa bravo", "Monitor 123AB", [("…123AB", False)]),
    ("runway 34, 123AB", "Runway 34, 123AB", [("…123AB", False)]),
    ("squawk 4521 123AB", "Squawk 4521 123AB", [("…123AB", False)]),
    # an altitude word takes a whole number, never a lone digit that starts a tail
    ("leaving 123AB", "Leaving 123AB", [("…123AB", False)]),
    ("through 118AB", "Through 118AB", [("…118AB", False)]),
    ("descend and maintain 2000 N123AB", "Descend and maintain 2000 N123AB", [("N123AB", False)]),
    # a frequency's decimals stop where a tail begins
    ("121 point 9 1 2 3 alfa bravo", "121 point 9 1 2 3 alfa bravo", [("…123AB", False)]),
])
def test_a_runway_run_into_a_tail_is_split(heard, written, found):
    assert normalize(heard) == written
    assert normalize(written) == written                         # idempotent
    assert mentions(heard) == found


@pytest.mark.parametrize("heard", [
    "squawk 4521", "squawk 4521 kilo zulu", "contact departure 126.4", "contact new york approach 128.55",
    "contact departure 118.025", "contact departure one two four point six five", "maintain 3000",
    "one two four point six five", "wind 340 at 12", "wind 340 at 12 gusts 18", "altimeter 30.01",
    "altimeter three zero zero one", "traffic 2 o'clock, 3 miles", "climb and maintain 3,000",
    "descend and maintain 2000", "at 2 thousand 5 hundred", "heading 340", "fly runway heading",
    "runway 34 kilo zulu",
])
def test_an_atc_number_never_feeds_a_tail(heard):
    assert mentions(heard) == []
    assert atc.find_mentions(heard, IX) == []                    # the raw words too


def test_a_tail_after_a_number_is_matched_low_and_never_snapped():
    # the real tail ends 7KZ: matched, but no better than AFTER_NUMBER_CONF (under a link's 0.7)
    a = atc.attribute(normalize("go ahead runway 347 kilo zulu"), IX, [N687KZ])
    assert a.spoken == "…7KZ" and a.hex == "c1" and a.confidence == atc.AFTER_NUMBER_CONF < atc.ADSB_MATCH
    assert atc.attribute("go ahead runway 34, 7 kilo zulu", IX, [N687KZ]).confidence == atc.AFTER_NUMBER_CONF
    # a plane whose tail is the merged number is never matched to the whole "347KZ": its end only, as low
    wrong = dict(N687KZ, hex="c2", flight="N347KZ", reg="N347KZ")
    assert atc.attribute(normalize("go ahead runway 347 kilo zulu"), IX, [wrong]).confidence == atc.AFTER_NUMBER_CONF
    assert atc.attribute(normalize("go ahead runway 347 kilo zulu"), IX, [N687KZ, wrong]).hex is None   # two: none
    assert snap(normalize("go ahead runway 347 kilo zulu"), [N687KZ, wrong])[1] == []
    assert snap("go ahead runway 347 kilo zulu", [wrong])[1] == []
    # said in full, it is the whole tail, exactly
    assert atc.attribute(normalize("November 347KZ"), IX, [wrong]).confidence == 1.0
    # the talker record: heard, never picked (no highlight on the map or the chip), even with one aircraft that fits
    rec = T.analyze(T.Tx("t1", CH, T0, T0 + 3, normalize("go ahead runway 347 kilo zulu")), IX, [N687KZ])
    assert rec["aircraft"]["spoken"] == "…7KZ" and rec["aircraft"]["status"] == "heard"
    assert "hex" not in rec["aircraft"] and rec["aircraft"]["confidence"] == 0.0 and rec["runway"] == "34"
    # a whole tail after a complete number is matched as usual
    whole = dict(N687KZ, hex="c3", flight="N123AB", reg="N123AB")
    assert atc.attribute("runway 34, 123AB", IX, [whole]).confidence == 0.6
    assert T.analyze(T.Tx("t3", CH, T0, T0 + 3, "runway 34, 123AB"), IX, [whole])["aircraft"]["status"] == "probable"
    off = T.analyze(T.Tx("t2", CH, T0, T0 + 3, normalize("go ahead runway 347 kilo zulu")), IX, [])
    assert off["aircraft"]["callsign"] == "…7KZ" and off["aircraft"]["status"] == "heard"
