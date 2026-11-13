"""#115: the decoder pairing by length (the 2026-10-14 07:04 South 2 page); the safety net for a page naming our units
whose tones aren't ours; re-reading a page's tones. The tone book itself (Settings > Radios > Tones): test_tone_book.py."""

import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sizeup.app import App
from sizeup.cad import tonelist
from sizeup.cad.api import CadApi
from sizeup.cad.store import CadError
from sizeup.cloud.access import Identity
from sizeup.config import load
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.radio.tones import ToneCode, ToneDetector, ToneEvent, TonePlan
from tests.test_app import FakeWhisper, cfg, tone, voice, wait
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people

ROOT = Path(__file__).resolve().parent.parent
LIST = load(ROOT / "config.example.toml").tones          # data/westchester_tones.csv


def app_with_list(tmp_path, **kw):
    return App(replace(cfg(tmp_path), tones=LIST, **kw))


def tset(app, op="Dana", **body):
    return app.cad.settings.post("tones", dict(body, action=body.pop("action", "set"), operator=op))


def page_row(app, pid, start, pairs, text=""):
    """A page in the CAD's communications log, its pairs matched as the app would."""
    app.cad.record_traffic({"id": pid, "start": start, "end": start + 10, "source": "South 2", "talkgroup": None,
                            "unit": "60 Control", "rig": "", "radio_id": "", "text": text, "status": "done",
                            "tones": [{"a_hz": a, "b_hz": b, "names": list(app.plan.match_pair(a, b)),
                                       "ours": app.paging.is_ours(app.plan.match_pair(a, b))} for a, b in pairs], "voice_at": 4.5})


# -- the decoder: pairs by length (South 2, 2026-10-14 07:04) --------------------------------------------------------

rng = np.random.default_rng(115)


def t(hz, s):
    return 0.5 * np.sin(2 * np.pi * hz * np.arange(int(RATE * s)) / RATE + rng.uniform(0, 6))


def gap(s):
    return np.zeros(int(RATE * s))


def decode(*parts):
    a = np.concatenate(parts)
    a = a + 0.02 * rng.standard_normal(len(a))
    d = ToneDetector(RATE)
    ev = d.feed(a) + d.flush()
    return [(e.a_hz, e.b_hz) for e in ev], d.sequence()


PAIRS = [(767.4, 928.1), (903.2, 810.2)]


def same(got, want=PAIRS):
    return len(got) == len(want) and all(abs(a - wa) <= 0.01 * wa and abs(b - wb) <= 0.01 * wb for (a, b), (wa, wb) in zip(got, want))


@pytest.mark.parametrize("g", [0.0, 0.05, 0.2, 0.5])
def test_the_0704_stack_decodes_as_two_pairs_in_order(g):
    got, seq = decode(gap(0.3), t(767.4, 1), gap(g), t(928.1, 3), gap(g), t(903.2, 1), gap(g), t(810.2, 3), gap(0.5))
    assert same(got), got
    assert [x["role"] for x in seq] == ["A", "B", "A", "B"]
    assert [round(x["dur"]) for x in seq] == [1, 3, 1, 3]


@pytest.mark.parametrize("fade", [0.15, 0.25])
def test_a_fade_in_b_no_longer_splits_the_stack_across_departments(fade):
    """By order alone, a fade in B1 made its second half the next pair's A: 767.4/928.1 + 928.1/903.2 + 810.2/810.2."""
    got, seq = decode(gap(0.3), t(767.4, 1), t(928.1, 1.4), gap(fade), t(928.1, 1.6 - fade), t(903.2, 1), t(810.2, 3), gap(0.5))
    assert same(got), got
    assert not any(abs(a - 928.1) < 9 and b and abs(b - 903.2) < 9 for a, b in got)


def test_broken_pairs_are_reported_not_forced():
    # a short tone then another short one: the first is in no pair (by order it was "767.4/928.1")
    got, seq = decode(gap(0.3), t(767.4, 1), t(928.1, 1), t(903.2, 3), gap(0.5))
    assert same(got, [(928.1, 903.2)]) and [x["role"] for x in seq] == ["", "A", "B"]
    # a long tone with no short one before it (its A lost): not an A=B code
    got, seq = decode(gap(0.3), t(810.2, 3), gap(0.5))
    assert got == [] and [x["role"] for x in seq] == [""]
    # an A=B code (Larchmont): A 1 s + B 3 s at one frequency, one run of ~4 s
    got, seq = decode(gap(0.3), t(435.1, 1), t(435.1, 3), gap(0.5))
    assert same(got, [(435.1, 435.1)]) and [x["role"] for x in seq] == ["AB"]


def test_the_0704_pairs_against_the_list_and_scarsdale_ems():
    """(c) the plan's tolerance is +/-1.5 % (about half the spacing of neighbouring Motorola codes); (d) 928.1 and
    810.2 are Scarsdale EMS's A and B, but in the order heard they are B1 and B2 of two pairs: only a pair whose A
    and B are those two matches Scarsdale EMS."""
    plan = TonePlan(list(LIST))
    assert plan.tolerance == 0.015
    # #47: the two pairs of the 07:04 stack are Rye Brook's and Port Chester's, heard on air (60 Control tone tests)
    assert plan.match_pair(767.4, 928.1) == ("Rye Brook FD (heard)",)
    assert plan.match_pair(903.2, 810.2) == ("Port Chester FD (heard)",)
    assert plan.match_pair(928.1, 810.2) == ("Scarsdale EMS (heard)",)
    assert plan.match_pair(928.1, 903.2) == ()                    # B1 + A2 of the stack: nobody's
    assert plan.match_pair(903.2 * 0.986, 810.2 * 1.014) == ("Port Chester FD (heard)",)
    assert plan.match_pair(903.2 * 0.98, 810.2) == ()


def test_reread_tones_runs_the_decoder_again_and_queues_a_page_of_ours(tmp_path):
    app = App(replace(cfg(tmp_path), unit_departments={"Engine 61": "Port Chester FD"}))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: "Engine 61, 209 Westchester Avenue, structure fire"})
    app.stt.start()
    tset(app, a_hz=903.2, b_hz=810.2, name="Port Chester FD", ours=True)
    audio = np.concatenate([tone(767.4, 1.0), tone(928.1, 3.0), tone(903.2, 1.0), tone(810.2, 3.0),
                            np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    seg = Segment(now, now + len(audio) / RATE, audio, 25.0)
    app._segment(Channel(470.2, "South 2"), seg)
    pid = app.state()["pages"][0]["id"]
    assert wait(lambda: len(app.cad.store.incidents()) == 1)          # PCFD's pair: queued from the air
    page = next(p for p in app.pages if p.id == pid)
    # As if decoded before the fix: split across (928.1/903.2), nothing of ours, no event
    page.tones = [ToneEvent("two_tone", 767.4, 928.1, 0, 4), ToneEvent("two_tone", 928.1, 903.2, 4, 6)]
    page.tone_seq = []
    app.store.incidents.clear()
    out = tset(app, action="reread", page=pid)
    assert same([(x["a_hz"], x["b_hz"]) for x in out["pairs"]]) and out["pairs"][1]["names"] == ["Port Chester · Fire"]
    assert [x["role"] for x in out["sequence"]] == ["A", "B", "A", "B"]
    assert len(app.store.incidents) == 1 and wait(lambda: len(app.cad.store.incidents()) == 2)   # read again: queued
    r = next(r for r in app.cad.feed.rows(limit=20) if r["id"] == pid)
    assert r["page"] == "ours" and len(r["tone_seq"]) == 4
    with pytest.raises(CadError, match="no page"):
        tset(app, action="reread", page="nope")


# -- the safety net: a page naming our units whose tones aren't ours -----------------------------------------------

TEXT_0704 = ("6462 to rescue 40. Number 2 Hanger Road, Westchester County Airport, hangar A. airport rows and a commercial "
             "arm. 64 622 rescue 402 hangar Road, Westchester County Airport, Hangar A. Airport the commercial alarm 0704690.")


def test_a_page_naming_our_units_with_tones_not_ours_is_flagged_with_the_ring_before_it(tmp_path):
    from sizeup.prealert import PreAlerts
    from tests.test_prealert import RING, STATUS, ring_msg
    app = app_with_list(tmp_path)
    pa = PreAlerts(app.cad, "http://127.0.0.1:9", RING, STATUS, log=lambda m: None)
    t0 = time.time() - 3600
    pa.ring(ring_msg("r1", at=t0 - 77))
    pa.tick(t0 + 600)                                                     # nothing came: expired
    # 07:04's stack was Rye Brook's and Port Chester's pairs (#47: ours now); as a page on a pair nobody has named yet
    page_row(app, "p0704", t0, [(741.3, 623.7), (1700.0, 1100.0)], TEXT_0704)
    page_row(app, "pfv", t0 + 60, [(832.5, 1201.0)], "Fairview, 34 Longdale Avenue, smoke in the building")
    page_row(app, "pmine", t0 + 120, [(903.2, 810.2)], "Engine 64, 209 Westchester Avenue")
    rows = {r["id"]: r for r in app.cad.feed.rows(limit=10)}
    r = rows["p0704"]
    assert r["page"] == "unknown" and r["names_ours"] == ["E64", "E62", "R40"] and r["events"] == []
    assert r["prealert"]["status"] == "expired" and abs(r["prealert"]["at"] - (t0 - 77)) < 1
    assert rows["pfv"]["names_ours"] == [] and "prealert" not in rows["pfv"]
    assert rows["pmine"]["names_ours"] == []                               # ours: an event, nothing to flag
    radio = {x["id"]: x for x in app.cad.comms.query(limit=10, kind="radio")}        # the Radio view's rows too
    assert radio["p0704"]["names_ours"] == ["E64", "E62", "R40"] and radio["pfv"]["names_ours"] == []
    assert app.cad.store.incidents() == []                                 # still not an event (#80)


def test_units_named_as_said(tmp_path):
    app = app_with_list(tmp_path)
    f = app.cad.feed.names_ours
    assert f(TEXT_0704) == ["R40"] and f(TEXT_0704, True) == ["E64", "E62", "R40"]   # bare: unknown tones only
    assert f("Engine 64, Ladder 30, 23 91 respond") == ["E64", "L30", "2391"]
    assert f("64, 62 to 1 Main Street") == [] and f("64, 62 to 1 Main Street", True) == ["E64", "E62"]
    for none in ("64 King Street, structure fire", "Sixty Control, 60 Control to all units", "time out 1432",
                 "Fairview 34 Longdale Avenue", "Engine 25, 42 Stratton Road"):
        assert f(none) == [], none


def test_a_bare_number_is_never_a_unit(tmp_path):
    # 2026-10-14 11:38 South 2, Fairview tones: STT garbled "Ladder 12, 1230 Manhattan Avenue" and the repeat
    # "...Ladder 12 122. 30, that's 30 Manhattan Avenue": the bare "30" was read as Rye Brook's L30.
    app = app_with_list(tmp_path)
    f = app.cad.feed.names_ours
    fairview = ("174, 175, Ladder 1,212,230 Manhattan Avenue. The Greenberg Alcohol Treatment Center Warren Avenue "
                "and West Grand Street for a commercial alarm. 17 175 Ladder 12 122. 30, that's 30 Manhattan Avenue. "
                "Greenberg Alcohol Treatment Center. Warren Avenue, West Grand Street, commercial arm, 1138.")
    assert f(fairview) == []
    assert f("30, 31 to the alarm") == []                  # no engine 30/31: bare numbers never become ladders
    assert f("Ladder 30 and Engine 60") == ["L30", "E60"]  # said as a unit, it counts
    assert f("64, 62, Rescue 40") == ["R40"]
    assert f("62. 64 to 100 King Street") == []
    assert f("we have 64, that's on Westchester Ave") == []


def test_a_bare_number_next_to_another_departments_unit_names_none_of_ours(tmp_path):
    # 60 Control on another department's page: a bare number, a run number and a mutual-aid EMS unit. Rye Brook's
    # Engine 14 is never "14" alone, nor E64 "64": an apparatus word or the code as written ("E14") is.
    app = app_with_list(tmp_path)
    f = app.cad.feed.names_ours
    for none in ("17 4410 with XPD EMS 31, that's 31 Example Drive", "14, 4410 with XPD EMS 31, that's 31 Example Drive",
                 "14 to 4410 with XPD EMS 31", "14. 4410, XPD EMS 31", "64, 4410 with XPD EMS 31", "14 44 10 with XPD EMS 31",
                 "60, 61 to the alarm", "Box 14", "I'm 60 seconds out", "e14 and r40"):
        assert f(none) == [], none
    assert f("Engine 14 with XPD EMS 31 to 31 Example Drive") == ["E14"]
    assert f("E14, E-60 and Rescue 40") == ["E14", "E60", "R40"]


def test_a_chiefs_number_is_never_a_house_number_or_another_agencys(tmp_path):
    app = app_with_list(tmp_path)
    f = app.cad.feed.names_ours
    assert f("2391 responding") == ["2391"] and f("23 91, Engine 61 on location") == ["2391", "E61"]
    for none in ("2391 Example Drive", "2662 North Example Road", "number 2391", "#2391", "XPD EMS 2391", "PD 26 62"):
        assert f(none) == [], none


def test_a_page_with_other_tones_and_a_bare_number_is_not_flagged(tmp_path):
    app = app_with_list(tmp_path)
    t0 = time.time() - 3600
    page_row(app, "pbare", t0, [(700.0, 900.0)], "14, 4410 with XPD EMS 31, that's 31 Example Drive")
    page_row(app, "pnamed", t0 + 60, [(700.0, 900.0)], "Engine 14, 4410 with XPD EMS 31, 31 Example Drive")
    rows = {r["id"]: r for r in app.cad.feed.rows(limit=10)}
    assert rows["pbare"]["page"] != "ours" and rows["pbare"]["names_ours"] == []
    assert rows["pnamed"]["page"] != "ours" and rows["pnamed"]["names_ours"] == ["E14"]


def test_a_list_of_bare_numbers_counts_only_on_unknown_tones_all_ours_and_two_or_more(tmp_path):
    app = app_with_list(tmp_path)
    f = app.cad.feed.names_ours
    assert f("64, 62 to 1 Example Street", True) == ["E64", "E62"]
    assert f("6462 to Rescue 40", True) == ["E64", "E62", "R40"]
    assert f("64 and 14 to the alarm", True) == ["E64", "E14"]
    for none in ("64 to 1 Example Street", "64, 17 to the alarm", "14, 4410 with XPD EMS 31", "14 44 10 with XPD EMS 31",
                 "64 62 Example Street", "number 64, 62", "box 64, 62"):
        assert f(none, True) == [], none
    assert f("60 Control, 64, 62 respond", True) == ["E64", "E62"]
    t0 = time.time() - 3600
    page_row(app, "punk", t0, [(700.0, 900.0)], "64, 62 to 1 Example Street")        # tones nobody's: unknown
    page_row(app, "poth", t0 + 60, [(581.4, 796.7)], "64, 62 to 1 Example Street")   # another department's tones
    page_row(app, "pnone", t0 + 120, [], "64, 62 to 1 Example Street")               # no tones decoded
    rows = {r["id"]: r for r in app.cad.feed.rows(limit=10)}
    assert rows["punk"]["page"] == "unknown" and rows["punk"]["names_ours"] == ["E64", "E62"]
    assert rows["poth"]["page"] == "other" and rows["poth"]["names_ours"] == []
    assert rows["pnone"]["page"] == "none" and rows["pnone"]["names_ours"] == ["E64", "E62"]


def test_a_chief_naming_his_command_at_a_street_is_still_named(tmp_path):
    f = app_with_list(tmp_path).cad.feed.names_ours
    assert f("2391 Example Street command") == ["2391"]
    assert f("2391 North Example Avenue Command establishing") == ["2391"]
    assert f("2391 Example Road, on location") == ["2391"]
    assert f("unit 2391 responding") == ["2391"]
    assert f("2391 Example Drive") == []                                         # a house number read out
    assert f("piano 2391 responding") == ["2391"] and f("problems 2391 responding") == ["2391"]   # word-bound: not "no"/"ems"
    for none in ("EMS-2391", "PD-23 91", "no. 2391", "No 2391"):
        assert f(none) == [], none


# -- bare-number lists: no false flags (#472 re-check) --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "time out 1458", "1459 hours", "incident 6061", "event 6162", "run 6061", "6014, Example Street",
    "6462, Example Street", "6460 Example Way", "1460 Example Way", "6462 main street", "6062 Boston Post",
    "60, 62 minutes", "we'll be 60, 62 seconds", "rooms 58, 59", "call back 914 555 6462",
    "14, 4410 with XPD EMS 31", "6460 Example Walk", "6462 Example Row", "6462 Example Path", "6462 Example Park",
    "6462 Example Point", "6462 Example Terrace", "6462 Example Plaza",
])
def test_a_bare_number_that_is_not_60_controls_list_names_no_unit(tmp_path, text):
    assert app_with_list(tmp_path).cad.feed.names_ours(text, True) == []


@pytest.mark.parametrize("text, units", [
    ("64, 62 to 1 Main Street", ["E64", "E62"]), ("6462 to Rescue 40", ["E64", "E62", "R40"]),
    ("64 and 62", ["E64", "E62"]), ("64 & 62", ["E64", "E62"]), ("58 59 60 61", ["E58", "E59", "E60", "E61"]),
    (TEXT_0704, ["E64", "E62", "R40"]),
])
def test_60_controls_list_of_our_engines_is_still_named(tmp_path, text, units):
    assert app_with_list(tmp_path).cad.feed.names_ours(text, True) == units


@pytest.mark.parametrize("text, units", [
    ("2391 Example Street is command", ["2391"]), ("2391 Example Street to command", ["2391"]),
    ("2391 en route", ["2391"]), ("2391 Ladder 31 en route", ["2391", "L31"]), ("2391 Example Way", []),
    ("2391 main street", []),
])
def test_a_chief_at_a_street_is_named_only_when_he_takes_command(tmp_path, text, units):
    assert app_with_list(tmp_path).cad.feed.names_ours(text) == units
