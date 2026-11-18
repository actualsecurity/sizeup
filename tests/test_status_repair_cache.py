"""The status-word repair memoised per radio row (sizeup/cad/statushints.py Phrases.repair): a CAD rebuild repairs
every radio row of every open event again, so the result is kept. It must give exactly what the repair gives, follow
the live "is fine" answers (dispatch state, a suggestion taken), and start again when the units change. Transmissions
are invented, in the radio's shape (the board's callsigns from data/cad_lists.json)."""

import json
import re
import threading

import pytest

from sizeup.cad import statushints
from sizeup.cad.feed import load_cfg
from sizeup.cad.lists import DEFAULT_PATH, load
from sizeup.cad.statushints import Phrases, StatusHints
from sizeup.cad.statusrepair import repair
from tests.test_radios import Clock, _event_with, cad, uid

LISTS = load()
UNITS = load_cfg(LISTS).units
KNOWN = {c for c in LISTS.units if re.fullmatch(r"\d+", c)}

TEXTS = [
    "Engine 61 is fine.", "Ladder 31's fine.", "Engine 61 is fine, PAR.", "The patient is fine.",
    "Engine 61 around 1412.", "Engine 61 around 1412 North Main Street", "Engine 61 on vacation.",
    "Engine 61 and route 1412.", "Engine 61 on route", "Engine 61 on route to King Street", "Engine 61 on seen",
    "and she's 63 on location", "and she's 63 years old", "Engine 60 on location, nothing showing.",
    "23 91 is fine.", "60 Control, Engine 62 was fine, 1412.", "Engine 61 is fine. Ladder 31 is fine.",
    "Engine 61 sex allegation 0912.", "", "Responding.",
]
TALKERS = ["", "E61", "L31", "2391", "E62"]
ANSWERS = {"all": lambda code: True, "none": lambda code: False, "E61": lambda code: code == "E61",
           "L31": lambda code: code == "L31", "None": None}


def fresh() -> Phrases:
    return Phrases(UNITS, KNOWN)


def test_the_kept_answer_is_the_repairs_for_every_row():
    P = fresh()
    for _ in range(2):                                   # the first pass makes each, the second takes it kept
        for text in TEXTS:
            for talker in TALKERS:
                for name, ok in ANSWERS.items():
                    for disp in (False, True):
                        want = repair(text, P.named, P.spoken, talker, ok, disp)
                        assert P.repair(text, talker, ok, disp) == want, (text, talker, name, disp)


def test_a_kept_row_is_not_repaired_again(monkeypatch):
    calls = []
    real = statushints.repair
    monkeypatch.setattr(statushints, "repair", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    P = fresh()
    first = P.repair("Engine 61 around 1412.", "E61", lambda c: True, False)
    assert P.repair("Engine 61 around 1412.", "E61", lambda c: True, False) == first and len(calls) == 1
    P.repair("Engine 61 around 1412.", "E62", lambda c: True, False)          # another talker: its own
    P.repair("Engine 61 around 1412.", "E61", lambda c: True, True)           # 60 Control's: its own
    P.repair("Engine 61 around 1412.", "E61", None, False)                    # no "is fine" answer: its own
    assert len(calls) == 4
    P.repair("Engine 61 around 14:12.", "E61", lambda c: True, False)         # the text changed: repaired
    assert calls[-1] == "Engine 61 around 14:12." and len(calls) == 5


def test_the_is_fine_answer_is_asked_again_and_a_changed_one_repairs_again(monkeypatch):
    calls, asked = [], []
    real = statushints.repair
    monkeypatch.setattr(statushints, "repair", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    P = fresh()
    yes = lambda code: asked.append(code) or True         # noqa: E731
    no = lambda code: asked.append(code) or False         # noqa: E731
    assert P.repair("Engine 61 is fine.", "E61", yes)[0] == "Engine 61 en route."
    assert P.repair("Engine 61 is fine.", "E61", yes)[0] == "Engine 61 en route." and len(calls) == 1
    assert asked == ["E61", "E61"]                        # kept, but the live answer is asked each time
    assert P.repair("Engine 61 is fine.", "E61", no) == ("Engine 61 is fine.", [])     # E61 moved on: as heard
    assert P.repair("Engine 61 is fine.", "E61", yes)[0] == "Engine 61 en route." and len(calls) == 3
    # rows without "fine" never ask
    asked.clear()
    P.repair("Engine 61 on vacation.", "E61", yes)
    P.repair("Engine 61 on vacation.", "E61", yes)
    assert asked == []


def test_two_units_fine_each_answer_counts():
    P = fresh()
    text = "Engine 61 is fine. Ladder 31 is fine."
    both = P.repair(text, "", lambda c: True, True)
    assert both[0] == "Engine 61 en route. Ladder 31 en route."
    assert P.repair(text, "", lambda c: c == "E61", True)[0] == "Engine 61 en route. Ladder 31 is fine."
    assert P.repair(text, "", lambda c: c == "L31", True)[0] == "Engine 61 is fine. Ladder 31 en route."
    assert P.repair(text, "", lambda c: True, True) == both


def test_the_fixes_handed_out_are_the_callers_own():
    P = fresh()
    _, fixes = P.repair("Engine 61 on vacation.", "E61")
    fixes[0]["to"] = "changed"
    fixes.append({"heard": "x", "to": "y", "at": 0})
    assert P.repair("Engine 61 on vacation.", "E61")[1] == [{"heard": "on vacation", "to": "on location", "at": 10}]


def test_it_is_bounded(monkeypatch):
    monkeypatch.setattr(statushints, "REPAIRED_MAX", 3)
    P = fresh()
    for i in range(10):
        P.repair(f"Engine 61 around 14{i}0.", "E61")
    assert len(P._repaired) == 3
    assert [k[0] for k in P._repaired] == ["Engine 61 around 1470.", "Engine 61 around 1480.", "Engine 61 around 1490."]


def test_threads_share_it_safely(monkeypatch):
    monkeypatch.setattr(statushints, "REPAIRED_MAX", 16)
    P = fresh()
    texts = [f"Engine 6{i % 5} around 14{i % 6}{i % 10}." for i in range(40)]
    want = {t: repair(t, P.named, P.spoken, "E61", None, False) for t in texts}
    bad = []

    def run():
        for _ in range(20):
            for t in texts:
                if P.repair(t, "E61") != want[t]:
                    bad.append(t)
    ts = [threading.Thread(target=run) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not bad and len(P._repaired) <= 16


# -- in the CAD: the rows a rebuild decorates ------------------------------------------------------------------------

def row(tid, text, rig="E61", **kw):
    return {"id": tid, "kind": "radio", "channel": "FG1", "talkgroup": None, "rig": rig, "text": text, "dispatcher": 0, **kw}


def shown(h, *rows):
    return [r.get("fixed_text") for r in h.decorate([dict(r) for r in rows])]


def test_dispatch_state_and_a_taken_suggestion_change_what_is_fine(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    r = row("FG1-cache-1", "Engine 61 is fine.")
    assert shown(c.hints, r) == [None]                                    # E61 on no call: as heard
    _event_with(c, ["E61"], clock)
    assert shown(c.hints, r) == ["Engine 61 en route."]                   # dispatched now: en route
    assert shown(c.hints, r) == ["Engine 61 en route."]
    c.store.set_unit_status(uid(c, "E61"), "OL", "Lt. Ruiz")
    assert shown(c.hints, r) == [None]                                    # on location: a welfare "fine"
    with c.store.tx() as db:                                              # taken as en route when it was heard
        db.execute("INSERT INTO status_hints (traffic_id, unit, unit_id, status, intent, phrase, heard_at, created_at, "
                   "expires_at, outcome) VALUES (?, 'E61', ?, 'ER', 'enroute', 'en route (heard “is fine”)', ?, ?, ?, "
                   "'accepted')", (r["id"], uid(c, "E61"), clock.t, clock.t, clock.t + 300))
    assert shown(c.hints, r) == ["Engine 61 en route."]
    assert shown(c.hints, row("FG1-cache-2", "Engine 61 is fine.")) == [None]   # another transmission: not taken


def test_a_row_whose_text_changes_is_repaired_again(tmp_path):
    c = cad(tmp_path, Clock())
    assert shown(c.hints, row("FG1-cache-3", "Engine 61 on vacation.")) == ["Engine 61 on location."]
    assert shown(c.hints, row("FG1-cache-3", "Engine 61 on location.")) == [None]       # a re-run's new text
    assert shown(c.hints, row("FG1-cache-3", "Engine 61 on seen")) == ["Engine 61 on scene"]


def test_a_change_of_units_starts_again(tmp_path):
    d = json.loads(DEFAULT_PATH.read_text())
    assert "E65" not in d["units"]
    d["units"]["E65"] = {"kind": "engine", "spoken": "Engine 65"}       # a new engine on the board
    p = tmp_path / "cad_lists.json"
    p.write_text(json.dumps(d))
    with_e65 = load(p)
    c = cad(tmp_path, Clock())
    now = [LISTS]
    h = StatusHints(c.store, lambda: now[0], channels=("FG1",), log=lambda m: None)
    r = row("FG1-cache-4", "Engine 65 on vacation.", rig="")
    assert shown(h, r) == [None]
    before = h.phrases
    now[0] = with_e65
    assert shown(h, r) == ["Engine 65 on location."]
    assert h.phrases is not before                                      # new lists, new phrases: nothing kept
    now[0] = LISTS
    assert shown(h, r) == [None]
