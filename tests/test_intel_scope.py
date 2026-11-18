"""The police pack's scope, the neighbor_police pack, the barricade's lone word, and how soon a held call goes
(sizeup/intel/rules.py, engine.py, data/intel/police.json, neighbor_police.json).

The first evening of live police pre-alerts pushed two calls from a neighboring town's PD talkgroup (the pack read
every MRRS police talkgroup), one of them a barricade on a garbled transmission whose only cue was the word
"hostage", and each push came minutes after the transmission (speech-to-text's queue, not the engine). Held here:

  - the police pack reads Port Chester / Rye / Rye Brook PD only (dispatch, tac, secure); every other MRRS police
    talkgroup of the talkgroups file is neighbor_police's ("Area PD"): the same rules, read from police.json at load,
    at log, so nothing is lost and one switch in Settings raises it;
  - a lone "hostage" / "barricade" is under the threshold; with a second cue it is over, and a dispatch of a
    barricaded subject pushes at once, with no place and no wait;
  - a call held for its place ("unplaced") stays held: the tick (no traffic needed) never pushes it, at RELEASE_S or
    after; a place after RELEASE_S stays "placed_late" at log (#640's design: the delay was speech-to-text's);
  - a transmission transcribed after newer ones of its source is still read (it was dropped, or raised a KeyError).

Synthetic only (this repository is public): "12 Example Road", "Car 6", invented talkers. Talkgroup numbers are the
public talkgroups file's."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sizeup.intel import push as push_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import RELEASE_S, IntelEngine, RecordingSink
from sizeup.intel.events import MemoryEvents
from sizeup.intel.model import Place
from tests.test_intel import Clock, row

MRRS = Path(__file__).resolve().parents[1] / "deploy" / "trunk-recorder" / "systems" / "mrrs" / "talkgroups.csv"
OURS = "p25:mrrs:9211"
NEIGHBOR = "p25:mrrs:9563"                                # another town's PD dispatch, in the public talkgroups file


def at(source: str) -> dict:
    return {"source": source, "talkgroup": int(source.rsplit(":", 1)[1])}


def engine(settings=None, clock=None, locate=None, packs=None):
    sink = RecordingSink()
    e = IntelEngine(settings=settings or {}, events=MemoryEvents(), sink=sink, thread=False, clock=clock or Clock(),
                    locate=locate, packs=packs)
    return e, sink


def pushed(sink):
    return [(ev, d) for ev, d in sink.pushes if d.would_push]


def police_talkgroups() -> dict[str, str]:
    """{source key: department} of every police talkgroup of the MRRS talkgroups file."""
    with MRRS.open() as f:
        return {f"p25:mrrs:{r['Decimal']}": r["Department"] for r in csv.DictReader(f) if r["Service"] == "police"}


# -- scope ------------------------------------------------------------------------------------------------------------

def test_every_mrrs_police_talkgroup_is_read_by_exactly_one_of_the_two_packs():
    packs = {p.name: p for p in rules_mod.load_packs()}
    pol, area = packs["police"], packs["neighbor_police"]
    tgs = police_talkgroups()
    assert len(tgs) >= 10
    for key in tgs:
        r = row("t1", "anything", 1000.0, source=key, talkgroup=int(key.rsplit(":", 1)[1]))
        assert [p.name for p in (pol, area) if p.in_scope(r)] == (["police"] if key in pol.scope["sources"] else
                                                                  ["neighbor_police"]), key
    # ours: the three talkgroups the file gives no other department; the rest are another department's, or interop
    assert sorted(pol.scope["sources"]) == ["p25:mrrs:9211", "p25:mrrs:9212", "p25:mrrs:9213"]
    assert all(tgs[k] == "" for k in pol.scope["sources"])
    assert area.scope["exclude"] == pol.scope["sources"]                  # the two files say the same three
    assert {"p25:mrrs:9563", "p25:mrrs:9564", "p25:mrrs:9000", "p25:mrrs:8000", "p25:mrrs:8002"} <= {
        k for k in tgs if area.in_scope(row("t", "x", 1.0, source=k))}


def test_another_systems_or_services_rows_are_neither_packs():
    for p in rules_mod.load_packs():
        if p.name in ("police", "neighbor_police"):
            assert not p.in_scope(row("t1", "x", 1.0, source="p25:mrrs:9570", service="ems"))
            assert not p.in_scope(row("t1", "x", 1.0, source="p25:westchester:363", system="westchester"))


def test_a_neighboring_pds_call_is_logged_never_pushed_and_ours_pushes():
    text = "Respond to 12 Example Road, shots fired."
    e, sink = engine()
    [ev] = e.feed(row("n1", text, e.clock(), **at(NEIGHBOR), tier="text"))
    assert (ev.pack, ev.category, ev.mode) == ("neighbor_police", "weapons", "log")
    assert pushed(sink) == []
    [ev] = e.feed(row("o1", text, e.clock(), **at(OURS)))
    assert (ev.pack, ev.category, ev.mode) == ("police", "weapons", "push")
    assert [x.pack for x, _d in pushed(sink)] == ["police"]


def test_the_area_pack_has_the_police_packs_rules_read_from_its_file():
    raw = json.loads((rules_mod.DATA / "police.json").read_text())
    packs = {p.name: p for p in rules_mod.load_packs()}
    pol, area = packs["police"], packs["neighbor_police"]
    assert [r.id for r in area.rules] == [f"area.{r['id']}" for r in raw["rules"]]
    assert [(r.category, r.w, r.mode, [x.src for x in r.any], [x.src for x in r.nots]) for r in area.rules] == [
        (r.category, r.w, r.mode, [x.src for x in r.any], [x.src for x in r.nots]) for r in pol.rules]
    assert [x.src for x in area.suppress] == [x.src for x in pol.suppress]
    assert list(area.categories) == list(pol.categories)
    for c, cd in area.categories.items():
        assert cd["label"] == f"Area PD: {pol.categories[c]['label']}"
        assert {k: v for k, v in cd.items() if k != "label"} == {k: v for k, v in pol.categories[c].items() if k != "label"}
    assert (area.threshold, area.window_s, area.context_factor) == (pol.threshold, pol.window_s, pol.context_factor)
    assert area.mode == "log" and area.towns == {} and area.area == () and area.src.get("infer") is False
    raw_area = json.loads((rules_mod.DATA / "neighbor_police.json").read_text())
    assert "rules" not in raw_area and "categories" not in raw_area      # one list of rules, in police.json
    # given on its own (replay's --rules with one file), it still reads police.json
    [solo] = rules_mod.load_packs([raw_area])
    assert [r.id for r in solo.rules] == [r.id for r in area.rules]


@pytest.mark.parametrize("pack, why", [
    ({"rules_from": "nope"}, "another pack's name"),
    ({"rules_from": "x"}, "another pack's name"),                             # itself
    ({"rules_from": "police", "rules": []}, "not both"),
    ({"rules_from": "police", "rule_prefix": "Bad"}, "rule_prefix"),
    ({"rules_from": ["police"]}, "a pack's name"),                          # never a TypeError (reload catches ValueError)
    ({"rules_from": 7}, "a pack's name"),
])
def test_rules_from_says_what_is_wrong(pack, why):
    with pytest.raises(ValueError, match=why):
        rules_mod.load_packs([{"name": "x", "scope": {}, **pack}])


def test_rules_from_one_level_only():
    a = {"name": "a", "rules_from": "police"}
    with pytest.raises(ValueError, match="from another pack itself"):
        rules_mod.load_packs([a, {"name": "b", "rules_from": "a"}])


def test_settings_for_the_police_pack_and_its_rules_still_apply_and_only_to_it():
    ov = {"intel_packs": {"police": {"mode": "map", "threshold": 0.7, "categories": {"weapons": {"mode": "log"}}}},
          "intel_rules": {"pd.shots": {"w": 0.4}}}
    packs = {p.name: p for p in rules_mod.load_packs(None, ov)}
    pol, area = packs["police"], packs["neighbor_police"]
    assert (pol.mode, pol.threshold, pol.categories["weapons"]["mode"]) == ("map", 0.7, "log")
    assert next(r.w for r in pol.rules if r.id == "pd.shots") == 0.4
    assert (area.mode, area.threshold, area.categories["weapons"]["mode"]) == ("log", 0.6, "push")
    assert next(r.w for r in area.rules if r.id == "area.pd.shots") == 0.75


def test_one_switch_raises_the_area_pack():
    e, sink = engine({"intel_packs": {"neighbor_police": {"mode": "push"}}})
    [ev] = e.feed(row("n1", "Respond to 12 Example Road, shots fired.", e.clock(), **at(NEIGHBOR)))
    assert ev.mode == "push"
    [(ev, d)] = pushed(sink)
    assert d.message["title"] == "Area PD · Weapon / shots"                # the category's label without the pack's


def test_a_category_label_beside_its_packs_never_says_the_pack_twice():
    area = {p.name: p for p in rules_mod.load_packs()}["neighbor_police"]
    assert area.categories["weapons"]["label"] == "Area PD: Weapon / shots"            # unique in Settings
    assert push_mod.category_label(area.categories["weapons"], "Area PD", "weapons") == "Weapon / shots"
    assert push_mod.category_label({"label": "Barricade"}, "PD", "barricade") == "Barricade"
    assert push_mod.category_label({"label": "PD: smoke / fire"}, "County PD", "fire_report") == "PD: smoke / fire"
    assert push_mod.category_label({}, "Area PD", "edp") == "edp"


def test_the_counters_have_a_line_for_the_area_pack():
    e, _sink = engine()
    e.feed(row("n1", "Car 6 on location.", e.clock(), **at(NEIGHBOR)))
    e.feed(row("n2", "Car 6 clear.", e.clock() + 1, **at(NEIGHBOR)))
    by = {r["pack"]: r for r in e.counts()["packs"]}
    assert by["neighbor_police"]["label"] == "Area PD" and by["neighbor_police"]["in_scope"] == 2
    assert by["neighbor_police"]["on"] is True                            # at log: read and counted
    assert by["police"]["in_scope"] == 0 and by["police"]["considered"] == 2


# -- the barricade: a lone word is not enough ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "And en route, back to my Example Road 40, Hostage.",                 # the garbled shape that pushed
    "Hostage.",
    "Barricade.",
    "Car 6, the barricade on Example Road is down, you can go through.",
])
def test_a_lone_hostage_or_barricade_word_is_under_the_threshold(text):
    e, sink = engine()
    assert e.feed(row("t1", text, e.clock())) == []
    assert pushed(sink) == []
    assert {h.rule_id for h in sink.hits} == {"pd.barricade"}


@pytest.mark.parametrize("text", [
    "Respond to 12 Example Road, barricaded subject with a weapon.",
    "Respond to 12 Example Road for a report of a barricade.",            # the word and the dispatch cue
    "Caller states her husband is holding the kids hostage.",
    "We have a hostage situation at 12 Example Road.",
    "Male barricaded inside 12 Example Road, ESU is en route.",
    "Hostage, subject armed with a knife.",
    "Barricade, ESU requested.",
    "Male barricaded in the apartment at 12 Example Road.",              # no dispatch cue: "barricaded in"
    "Barricade on Example Road, we're in a standoff.",
    "Hostage, get the negotiator started.",
    "Barricade at 12 Example Road, SWAT requested.",
    "Subject refusing to come out at 12 Example Road.",
])
def test_a_barricade_with_a_second_cue_pushes_at_once_with_no_place(text):
    clock = Clock()
    e, sink = engine(clock=clock)                                         # no locate: no place at all
    out = e.feed(row("t1", text, clock()))
    [ev] = [x for x in out if x.category == "barricade"]
    assert ev.mode == "push" and ev.suppressed == "" and ev.precision == "none"
    [(_p, d)] = [(p, d) for p, d in pushed(sink) if p.category == "barricade"]
    assert ev.pushed_at == clock()                                        # the same moment: no hold, no tick
    assert "location not heard" in d.message["body"]


@pytest.mark.parametrize("first, second", [
    ("The system is armed.", "Hostage."),
    ("ESU is setting up on Example Road.", "Barricade on Example Road."),
    ("Car 6, subject armed with a knife, last seen on Sample Street.", "Hostage."),
    ("Car 6 respond to Sample Street, the standoff with the landlord is over.", "Barricade."),
])
def test_a_lone_word_never_takes_a_cue_from_another_transmission(first, second):
    """A cue counts only in the transmission that says the barricade itself: two talkers' words, a minute apart in the
    same window, are no barricade (and a cue alone is no rule of the barricade's at all)."""
    clock = Clock()
    e, sink = engine(clock=clock)
    e.feed(row("t1", first, clock(), talker="CAR 6"))
    clock.t += 20
    e.feed(row("t2", second, clock(), talker="DISP"))
    assert not [ev for ev in e.events.all() if ev.category == "barricade"]
    assert not [ev for ev, _d in pushed(sink) if ev.category == "barricade"]


@pytest.mark.parametrize("text", [
    "Alarm is armed, barricade on Example Road.",                         # a road closure beside an alarm system
    "The barricade on Example Road, the alarm system is armed.",
    "Hostage, no one with a weapon.",                                     # a negation inside the gap
])
def test_a_cue_word_that_is_no_cue_in_the_same_transmission_stays_under(text):
    e, sink = engine()
    e.feed(row("t1", text, e.clock()))
    assert not [ev for ev in e.events.all() if ev.category == "barricade"] and pushed(sink) == []


def test_the_safety_categories_are_never_held_for_a_place():
    """No safety category of any pack is a "place": true one (engine._unplaced holds only those): a barricade or shots
    with no place push at once, and the tick changes nothing."""
    for pack in rules_mod.load_packs():
        for c, cd in pack.categories.items():
            assert not (cd["safety"] and cd["place"]), (pack.name, c)
    clock = Clock()
    e, sink = engine(clock=clock)
    e.feed(row("t1", "Respond to 12 Example Road, barricaded subject with a weapon.", clock()))
    assert {ev.category for ev, _d in pushed(sink)} == {"barricade", "weapons"}
    assert all(ev.suppressed == "" and ev.precision == "none" for ev in e.events.open_events())


# -- a held call stays held: no place, no pre-alert (#640) -------------------------------------------------------------

HELD = "Caller states they smell something burning, I'll be notifying the FD."     # fire_call, no address


def test_an_unplaced_call_is_still_not_pushed_at_release_s_with_nothing_more_heard():
    """The tick runs with no traffic (engine._run waits on its queue at most TICK_S), and never lets a held call go:
    at RELEASE_S and after it is still "unplaced" at log, then it expires, never pushed."""
    clock = Clock()
    e, sink = engine(clock=clock)
    e.feed(row("c1", HELD, clock()))
    [ev] = e.events.open_events()
    assert (ev.category, ev.suppressed, ev.mode) == ("fire_call", "unplaced", "log")
    for dt in (RELEASE_S - 1, RELEASE_S, RELEASE_S + 15, RELEASE_S * 2):
        clock.t = ev.first_at + dt
        e._maybe_tick()                                                   # what _run does on an empty queue
        [now] = e.events.open_events()
        assert (now.suppressed, now.mode, now.pushed_at) == ("unplaced", "log", None), dt
    assert pushed(sink) == []
    clock.t = ev.until
    assert [x.status for x in e.tick(clock())] == ["expired"]
    assert e.events.open_events() == [] and pushed(sink) == []


def test_a_place_after_release_s_stays_placed_late_at_log():
    clock = Clock()
    locate = lambda text, scope: (Place("address", 41.0, -73.66, 25.0, "12 Example Rd, Port Chester", "Port Chester",  # noqa: E731
                                        True, 0.8) if "12 example" in text.lower() else None)
    e, sink = engine(clock=clock, locate=locate)
    e.feed(row("c1", HELD, clock(), talker="DISP"))
    clock.t += RELEASE_S
    e.tick(clock())
    clock.t += 60
    e.feed(row("c2", "The burning smell is at 12 Example Road, FD en route.", clock(), talker="DISP"))
    [ev] = e.events.open_events()
    assert ev.label_full == "12 Example Rd, Port Chester" and (ev.suppressed, ev.mode) == ("placed_late", "log")
    assert pushed(sink) == []


# -- a transmission transcribed late, after newer ones of its source ---------------------------------------------------

def test_a_late_transcript_older_than_the_window_is_still_read():
    clock = Clock(5000.0)
    e, sink = engine(clock=clock)
    e.feed(row("t2", "Car 6 on location.", clock() - 10))
    out = e.feed(row("t1", "Respond to 12 Example Road, shots fired.", clock() - 400))     # transcribed after t2
    assert [x.category for x in out] == ["weapons"] and pushed(sink)


def test_a_late_transcript_with_no_rule_words_beside_an_open_event_is_no_error():
    clock = Clock(5000.0)
    e, sink = engine(clock=clock)
    e.feed(row("t1", "Respond to 12 Example Road, shots fired.", clock() - 30))
    e.feed(row("t3", "Car 6 on location.", clock() - 5))
    assert e.feed(row("t2", "Car 6 copy.", clock() - 200)) == []          # was a KeyError (engine._feed_pack)
    assert len(pushed(sink)) == 1
