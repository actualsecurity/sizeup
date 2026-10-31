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
    assert f(TEXT_0704) == ["E64", "E62", "R40"]
    assert f("Engine 64, Ladder 30, 23 91 respond") == ["E64", "L30", "2391"]
    assert f("64, 62 to 1 Main Street") == ["E64", "E62"]
    for none in ("64 King Street, structure fire", "Sixty Control, 60 Control to all units", "time out 1432",
                 "Fairview 34 Longdale Avenue", "Engine 25, 42 Stratton Road"):
        assert f(none) == [], none


def test_a_bare_number_is_an_engine_or_a_chief_never_a_ladder(tmp_path):
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
    assert f("64, 62, Rescue 40") == ["E64", "E62", "R40"]
    assert f("62. 64 to 100 King Street") == ["E62", "E64"]
    assert f("we have 64, that's on Westchester Ave") == []
