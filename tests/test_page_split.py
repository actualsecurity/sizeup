"""One page, one tone group (GitHub #183).

South 2, 2026-10-15 13:57: Port Chester's page and West Harrison's went out as one transmission (the carrier never
dropped between them). It was shown as one page with both departments' tones, its voice taken after the last tone
(West Harrison's message, read twice), and queued as ours (Hazard / Gas, 193 Park Ave, Harrison) on Port Chester's
tones. A page is TONES (one pair or several stacked, nothing said between them), then the MESSAGE (read twice), then
the end: tones after the message are the next page (radio/page.py split_pages).

No real South 2 recording is in tests/fixtures: the pages here are synthetic (the tones as decoded on the air, the
dispatcher as harmonics with syllables, the carrier as in tests/test_page.py)."""

import time
from dataclasses import replace

import numpy as np
import pytest

from sizeup.app import App
from sizeup.cad.comms import page_kind
from sizeup.radio.fm import FRAME, RATE, Channel, Segment
from sizeup.radio.livepage import LivePaging
from sizeup.radio.page import (STACK_GAP_S, Page, PageHold, page_from_dict, read_twice, speech_mask, split_pages,
                               decode_tones, tone_groups)
from sizeup.radio.tones import ToneCode, TonePlan
from tests.test_app import cfg, wait
from tests.test_page import PAGING, run_squelch

rng = np.random.default_rng(183)
PC, WH = (767.4, 928.1), (349.0, 410.8)            # the pairs on the 13:57 page's chips
RB = (669.9, 1092.4)
PLAN = TonePlan([ToneCode("Port Chester · Fire", *PC, "PCFD"), ToneCode("West Harrison · Fire", *WH),
                 ToneCode("Rye Brook · Fire", *RB)])

# The 13:57 transcript (West Harrison's message, read twice), as whisper wrote it.
WH_TEXT = ("Joel West Harrison, 193 Park Avenue East between Jefferson Street and Washington-Street. Send it on seen "
           "with an active gas leak. West Harrison 193 Park Avenue East between Jefferson Street and Washington-Street. "
           "And I guess on scene with an active gas leak 1358672.")
PC_TEXT = ("Port Chester Fire Department, respond to 209 Westchester Avenue, cross of Pearl Street, for an activated "
           "fire alarm. Port Chester Fire Department, 209 Westchester Avenue, cross of Pearl Street, activated fire "
           "alarm. Time out 1356.")


def hiss(s, amp=0.4):
    """No carrier (tests/test_page.py's, with this file's own random numbers)."""
    return amp * rng.standard_normal(int(RATE * s))


def keyed(audio, ctcss=156.7):
    """Carrier up: quiet, with the sub-audible CTCSS tone under the audio."""
    t = np.arange(len(audio)) / RATE
    return audio + 0.1 * np.sin(2 * np.pi * ctcss * t) + 0.02 * rng.standard_normal(len(audio))


def tone(hz, s, amp=0.5):
    return amp * np.sin(2 * np.pi * hz * np.arange(int(RATE * s)) / RATE)


def talk(s):
    """The dispatcher: harmonics with syllables, over the carrier's quiet dead air."""
    t = np.arange(int(RATE * s)) / RATE
    ph = 2 * np.pi * np.cumsum(130 + 20 * np.sin(2 * np.pi * 1.1 * t)) / RATE
    syll = (np.sin(2 * np.pi * 3 * t) > 0.2).astype(float)
    return 0.3 * syll * sum(np.sin(k * ph) / k for k in range(1, 12))


def dead(s):
    """Carrier up, nobody talking."""
    return 0.003 * rng.standard_normal(int(RATE * s))


def pair(p):
    return np.concatenate([tone(p[0], 1.0), tone(p[1], 3.0)])


def message(s):
    """Read twice: s seconds, then again."""
    return np.concatenate([dead(0.3), talk(s), dead(0.8), talk(s), dead(0.4)])


def page(*pairs, msg_s=5.0):
    return np.concatenate([*(pair(p) for p in pairs), message(msg_s)])


def pages_of(audio, plan=PLAN):
    """One transmission (the carrier up throughout, as at 13:57), through the squelch and the page assembler."""
    segs = run_squelch(np.concatenate([hiss(4.0), keyed(audio), hiss(6.0)]), **PAGING)
    hold, out = PageHold(plan), []
    for s in segs:
        out += hold.add("South 2", 470.2, s)[0]
    return segs, out + hold.due(float("inf"))


def depts(p: Page):
    return p.departments


def test_two_pages_in_one_transmission_are_two_pages_each_with_its_own_tones_and_voice():
    segs, pages = pages_of(np.concatenate([page(PC, msg_s=5.0), dead(0.6), page(WH, msg_s=7.0)]))
    assert len(segs) == 1 and segs[0].breaks == []          # the carrier never dropped: before #183, one page
    pc, wh = pages
    assert [depts(pc), depts(wh)] == [["Port Chester · Fire"], ["West Harrison · Fire"]]
    assert pc.status == wh.status == "pending"
    # Each its own voice: Port Chester's message twice, then West Harrison's twice (not the one after the last tone)
    assert len(pc.voice) / RATE == pytest.approx(11.5, abs=0.6)
    assert len(wh.voice) / RATE == pytest.approx(15.5, abs=0.6)
    # The audio is the transmission's, cut once just before West Harrison's tones: nothing lost, nothing twice
    assert len(pc.audio) + len(wh.audio) == len(segs[0].audio)
    assert wh.start == pytest.approx(pc.end, abs=1e-6)
    assert wh.tones[0].start == pytest.approx(0.25, abs=0.1) and wh.voice_offset_s < 4.6
    assert pc.parts[-1]["reason"] == "tones" and "said, then new tones" in pc.parts[-1]["split"]
    assert all(t.start < len(p.audio) / RATE for p in pages for t in p.tones)      # times into their own audio
    assert [r["role"] for r in wh.tone_seq] == ["A", "B"] and wh.tone_seq[0]["start"] < 0.5


def test_stacked_tones_then_one_message_stay_one_page_to_all_of_them():
    segs, pages = pages_of(page(PC, WH, msg_s=5.0))
    (p,) = pages
    assert depts(p) == ["Port Chester · Fire", "West Harrison · Fire"] and p.status == "pending"
    assert p.message_departments == depts(p)
    # A little dead air between the stacked pairs (a console stepping to the next code) changes nothing
    segs, pages = pages_of(np.concatenate([pair(PC), dead(1.5), pair(RB), message(5.0)]))
    (p,) = pages
    assert depts(p) == ["Port Chester · Fire", "Rye Brook · Fire"]


def test_west_harrison_first_then_port_chester_is_split_too():
    segs, pages = pages_of(np.concatenate([page(WH, msg_s=6.0), dead(0.6), page(PC, msg_s=5.0)]))
    wh, pc = pages
    assert [depts(wh), depts(pc)] == [["West Harrison · Fire"], ["Port Chester · Fire"]]
    assert len(wh.voice) / RATE == pytest.approx(13.5, abs=0.6) and len(pc.voice) / RATE == pytest.approx(11.5, abs=0.6)


def test_tones_with_no_message_are_a_tones_only_page_not_merged_into_the_next():
    # Port Chester's tones, the carrier held with nothing said, then West Harrison's page.
    segs, pages = pages_of(np.concatenate([pair(PC), dead(STACK_GAP_S + 1.0), page(WH, msg_s=5.0)]))
    pc, wh = pages
    assert depts(pc) == ["Port Chester · Fire"] and pc.status == "no_voice"        # "Tones only, no message"
    assert depts(wh) == ["West Harrison · Fire"] and wh.status == "pending"
    assert "after the last" in pc.parts[-1]["split"]
    from sizeup.radio.page import recording_note
    assert "tones only, no message" in recording_note(pc)
    # Re-keyed after the hang (the squelch closed between them): its own transmission, a page of its own as before
    audio = np.concatenate([hiss(4.0), keyed(pair(PC)), hiss(2.5), keyed(page(WH, msg_s=5.0)), hiss(6.0)])
    segs = run_squelch(audio, **PAGING)
    assert len(segs) == 2 and segs[1].cont
    hold, out = PageHold(PLAN), []
    for s in segs:
        out += hold.add("South 2", 470.2, s)[0]
    pc, wh = out + hold.due(float("inf"))
    assert depts(pc) == ["Port Chester · Fire"] and pc.status == "no_voice"
    assert depts(wh) == ["West Harrison · Fire"] and wh.status == "pending"


def test_the_end_of_the_last_message_before_new_tones_goes_back_to_its_page():
    # Port Chester's page fades out; the carrier comes back in the continuation window with the rest of its message,
    # then West Harrison's tones and message on the same carrier.
    head = np.concatenate([pair(PC), dead(0.3), talk(5.0)])
    tail = np.concatenate([talk(4.0), dead(0.5), page(WH, msg_s=5.0)])
    now = 1000.0
    first = Segment(now, now + len(head) / RATE, head.astype(np.float32), 25.0, reason="quiet", hold_until=now + 30)
    cont = Segment(first.end + 2.0, first.end + 2.0 + len(tail) / RATE, tail.astype(np.float32), 9.0, reason="quiet",
                   cont=True, hold_until=first.end + 40)
    hold = PageHold(PLAN)
    assert hold.add("South 2", 470.2, first)[0] == []
    done, touched = hold.add("South 2", 470.2, cont)
    pc, wh = done + hold.due(float("inf"))
    assert [j for _p, j in touched] == [True, False]                  # the voice joined, the new tones a new page
    assert depts(pc) == ["Port Chester · Fire"] and len(pc.voice) / RATE == pytest.approx(5.15 + 2.0 + 4.25, abs=0.6)
    assert depts(wh) == ["West Harrison · Fire"] and wh.tones[0].start < 0.5


def test_speech_and_tones_are_told_apart():
    audio = np.concatenate([pair(PC), dead(1.0), talk(3.0), dead(1.0)])
    tones, seq = decode_tones(audio, PLAN)
    m = speech_mask(audio, [(r["start"], r["start"] + r["dur"]) for r in seq])
    step = FRAME / RATE
    assert not m[:int(5.0 / step)].any()                 # the tones and the dead air after them: not speech
    assert m[int(5.0 / step):int(8.0 / step)].sum() * step > 1.5      # the dispatcher is
    assert not m[int(8.2 / step):].any()
    # Muted carrier breaks (zeros) and a transmission of nothing but tones: no speech
    assert not speech_mask(np.concatenate([pair(PC), np.zeros(RATE), pair(WH)]),
                           [(0, 4.05), (5, 9.05)]).any()
    assert [len(p.tones) for p in split_pages(Segment(0, 9, np.concatenate([pair(PC), pair(WH)]).astype(np.float32)),
                                              *decode_tones(np.concatenate([pair(PC), pair(WH)]), PLAN))] == [2]


def test_read_twice_is_seen_in_the_transcript():
    assert read_twice(WH_TEXT) is True and read_twice(PC_TEXT) is True
    assert read_twice("West Harrison, 193 Park Avenue East between Jefferson Street and Washington Street, "
                      "on scene with an active gas leak, time out 1358.") is False
    assert read_twice("time out 1358") is None
    # Both pages' messages run together (one page heard as both, the incident's shape) don't read as one, twice
    assert read_twice(PC_TEXT + " " + WH_TEXT) is False


# -- a page made before #183 (or by a station on an older build): two pages' tones on one page --------------------

def legacy_doc(first, second, gap_s=14.0):
    """The 13:57 page as an older station sent it: both pairs, no times on them (tones[].start/end came with #183),
    the tones heard in order (tone_seq), the voice after the last tone."""
    b = 4.05 + gap_s
    return {"id": "a1b2c3d4e5", "label": "South 2", "mhz": 470.2, "start": 1000.0, "end": 1000.0 + b + 4.05 + 15,
            "kind": "page", "status": "pending", "departments": [],
            "tones": [{"kind": "two_tone", "a_hz": first[0], "b_hz": first[1], "names": list(PLAN.match_pair(*first))},
                      {"kind": "two_tone", "a_hz": second[0], "b_hz": second[1], "names": list(PLAN.match_pair(*second))}],
            "raw": "", "confidence": None, "voice_offset_s": round(b + 4.2, 3),
            "tone_seq": [{"hz": first[0], "start": 0.0, "dur": 1.0, "role": "A"},
                         {"hz": first[1], "start": 0.95, "dur": 3.1, "role": "B"},
                         {"hz": second[0], "start": b, "dur": 1.0, "role": "A"},
                         {"hz": second[1], "start": b + 0.95, "dur": 3.1, "role": "B"}]}


def test_an_older_page_with_two_pages_tones_is_ours_only_by_the_group_its_message_answers():
    p = page_from_dict(legacy_doc(PC, WH))
    PLAN.match_all(p.tones)
    assert depts(p) == ["Port Chester · Fire", "West Harrison · Fire"]
    assert [len(g) for g in tone_groups(p.tones, p.tone_seq)] == [1, 1]
    assert p.message_departments == ["West Harrison · Fire"]
    p = page_from_dict(legacy_doc(WH, PC))
    PLAN.match_all(p.tones)
    assert p.message_departments == ["Port Chester · Fire"]
    # Stacked pairs on an old page (back to back) are still one group; with no times at all, one group as before
    p = page_from_dict(legacy_doc(PC, WH, gap_s=0.1))
    PLAN.match_all(p.tones)
    assert p.message_departments == ["Port Chester · Fire", "West Harrison · Fire"]
    d = legacy_doc(PC, WH)
    d["tone_seq"] = []
    p = page_from_dict(d)
    PLAN.match_all(p.tones)
    assert p.message_departments == ["Port Chester · Fire", "West Harrison · Fire"]


def test_a_new_page_round_trips_with_its_tones_times():
    _segs, (pc, wh) = pages_of(np.concatenate([page(PC, msg_s=5.0), dead(0.6), page(WH, msg_s=5.0)]))
    d = wh.as_dict()
    assert d["tones"][0]["start"] < 0.5 and d["tones"][0]["end"] == pytest.approx(4.3, abs=0.2)
    back = page_from_dict(d)
    assert [(t.start, t.end) for t in back.tones] == [(t["start"], t["end"]) for t in d["tones"]]
    assert set(d["tones"][0]) >= {"kind", "a_hz", "b_hz", "names"}     # what an older hub reads; the rest it ignores


def test_cad_row_kind_ignores_an_earlier_pages_tones():
    ours = {"a_hz": PC[0], "b_hz": PC[1], "names": ["Port Chester · Fire"], "ours": True}
    other = {"a_hz": WH[0], "b_hz": WH[1], "names": ["West Harrison · Fire"], "ours": False}
    assert page_kind([ours, other]) == "ours"
    assert page_kind([dict(ours, other_page=True), other]) == "other"


# -- the app: each page its own transcript and suggestion ---------------------------------------------------------

class ByLength:
    """whisper: Port Chester's message is the shorter voice, West Harrison's the longer."""

    def __init__(self):
        self.heard = []

    def transcribe(self, audio, prompt=""):
        s = len(audio) / RATE
        self.heard.append(round(s, 1))
        return (PC_TEXT if s < 13.0 else WH_TEXT), 0.9


def app_with_pages(tmp_path, audio):
    c = replace(cfg(tmp_path), tones=(ToneCode("Port Chester FD", *PC, "PCFD"), ToneCode("West Harrison FD", *WH)))
    app = App(c)
    app._kind = {"South 2": "paging"}
    app.stt.client = ByLength()
    app.stt.start()
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio.astype(np.float32), 25.0))
    assert wait(lambda: len(app.pages) == 2 and all(p.status == "done" for p in app.pages), 10)
    return app


def queued(app):
    return [i for i in app.cad.state()["incidents"] if i["status"] == "queued"]


@pytest.mark.parametrize("order", ["pc_first", "wh_first"])
def test_only_port_chesters_own_page_is_queued(tmp_path, order):
    pcp, whp = page(PC, msg_s=5.0), page(WH, msg_s=7.0)
    audio = np.concatenate([pcp, dead(0.6), whp] if order == "pc_first" else [whp, dead(0.6), pcp])
    app = app_with_pages(tmp_path, audio)
    by = {p.departments[0]: p for p in app.pages}
    assert set(by) == {"Port Chester · Fire", "West Harrison · Fire"}
    assert by["Port Chester · Fire"].raw == PC_TEXT and by["West Harrison · Fire"].raw == WH_TEXT
    assert getattr(by["West Harrison · Fire"], "read_twice") is True
    assert wait(lambda: len(queued(app)) == 1, 5)
    time.sleep(0.3)
    (q,) = queued(app)
    assert "209" in (q["address"] or "") and "Park" not in (q["address"] or "")
    assert [i.pages for i in app.store.incidents] == [[by["Port Chester · Fire"].id]]
    assert not any("Park" in (i.location or "") for i in app.store.incidents)


def test_stacked_mutual_aid_is_one_page_and_ours(tmp_path):
    c = replace(cfg(tmp_path), tones=(ToneCode("Port Chester FD", *PC, "PCFD"), ToneCode("West Harrison FD", *WH)))
    app = App(c)
    app._kind = {"South 2": "paging"}
    app.stt.client = type("W", (), {"transcribe": lambda self, a, prompt="": (PC_TEXT, 0.9)})()
    app.stt.start()
    audio = page(WH, PC, msg_s=5.0).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: len(queued(app)) == 1, 10)
    (p,) = app.pages
    assert p.departments == ["West Harrison · Fire", "Port Chester · Fire"] == p.message_departments


def test_the_hub_never_queues_an_older_stations_merged_page_on_the_earlier_pages_tones(tmp_path):
    # A hub on this build, the station still on the one before: the 13:57 page arrives merged.
    from tests.test_cloud import hub_cfg
    from sizeup.cloud.hub import HubApp
    from sizeup.app import make_app
    c = replace(hub_cfg(tmp_path), tones=(ToneCode("Port Chester FD", *PC, "PCFD"), ToneCode("West Harrison FD", *WH)))
    hub = make_app(c)
    assert isinstance(hub, HubApp)
    try:
        d = legacy_doc(PC, WH)
        hub.apply("page", d, None)
        hub.apply("page_text", dict(d, raw=WH_TEXT, status="done", model="m.bin"), None)
        (p,) = [p for p in hub.pages if p.id == d["id"]]
        assert p.raw == WH_TEXT and p.dispatch
        assert list(hub.store.incidents) == [] and queued(hub) == []
        row = hub.cad.store.q("SELECT tones FROM radio_traffic WHERE id = ?", (d["id"],))[0]
        import json
        tones = json.loads(row["tones"])
        assert [t.get("other_page", False) for t in tones] == [True, False]
        assert page_kind(tones) == "other"
        # West Harrison's page first, Port Chester's last: the message is Port Chester's, and it is ours
        d = dict(legacy_doc(WH, PC), id="f0e1d2c3b4")
        hub.apply("page", d, None)
        hub.apply("page_text", dict(d, raw=PC_TEXT, status="done", model="m.bin"), None)
        assert wait(lambda: len(queued(hub)) == 1, 5)
    finally:
        hub.stop()


# -- the live display ---------------------------------------------------------------------------------------------

def test_the_live_display_starts_the_next_page_at_its_tones():
    clock = {"t": 0.0}
    lp = LivePaging(PLAN, ours={"Port Chester · Fire"}, clock=lambda: clock["t"])
    x = np.concatenate([pair(PC), dead(0.3), talk(4.0), dead(0.5), pair(WH), dead(0.2)])
    x = x + 0.01 * rng.standard_normal(len(x))
    for i in range(0, len(x) - FRAME + 1, FRAME):
        lp.feed("South 2", 100.0, x[i:i + FRAME])
    (e,) = lp.state()
    assert [p["names"] for p in e["pairs"]] == [["West Harrison · Fire"]] and not e["ours"]
    assert e["started"] == pytest.approx(100.0 + 8.8, abs=0.2)
    clock["t"] = 200.0
    assert not lp.finished("South 2", "pcpage", end=100.0 + 8.5)       # Port Chester's page, cut before these tones
    assert not lp.state()[0]["ended"]
    assert lp.finished("South 2", "whpage", end=100.0 + 14.0)
    assert lp.state()[0]["ended"] and lp.state()[0]["page_id"] == "whpage"
    # Stacked pairs with a moment between them stay one page on the live display too
    lp = LivePaging(PLAN, ours={"Port Chester · Fire"}, clock=lambda: clock["t"])
    x = np.concatenate([pair(PC), dead(0.6), pair(WH), dead(0.2)]) + 0.01 * rng.standard_normal(int(RATE * 8.8))
    for i in range(0, len(x) - FRAME + 1, FRAME):
        lp.feed("South 2", 300.0, x[i:i + FRAME])
    (e,) = lp.state()
    assert len(e["pairs"]) == 2 and e["ours"]
