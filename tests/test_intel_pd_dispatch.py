"""The police pack on dispatches as PD dispatchers say them (two calls that raised nothing): a call relayed
from another agency ("got a call from ...", "a woman who said ..."), a knife "involved", an assault, an
out-of-control teenager, and the follow-up that takes the weapon out of it (the weapons category's "deescalate").

Every address, unit and person here is invented."""

from __future__ import annotations

import pytest

from tests.test_intel import KEY, engine, row

# the dispatch shapes (invented details): a relayed call with a knife, and an assault "can you respond to"
KNIFE_CALL = ("OK car 12 and car 14, 12 Example Road, apartment number 2. Got a call from the neighboring PD, their "
              "transferred call dropped. But they got a call from a woman who said her teenager is destroying his room "
              "and mentioned a knife is involved. I'm going to try and call back the caller to get further.")
KNIFE_FOLLOW_UP = ("Units responding, be advised the caller stated she was able to get the knife away from her "
                   "14-year-old son. He wasn't using it to threaten or harm anyone. He was using it to try and open "
                   "locked doors or damage property. We're out with those parties.")
ASSAULT_CALL = ("For car 12, can you respond to 40 Sample Street. A woman called. She stated her daughter was "
                "assaulted by another girl. The other person is not on scene.")


def pushed(sink) -> list[str]:
    return [ev.category for ev, d in sink.pushes if d.would_push]


def test_a_relayed_call_with_a_knife_involved_pushes_as_weapons_once():
    e, sink = engine()
    evs = e.feed(row("t1", KNIFE_CALL, 1000))
    assert [ev.category for ev in evs] == ["weapons"]          # the out-of-control teen with a knife: one pre-alert
    assert pushed(sink) == ["weapons"]
    rules = {r[0] for r in evs[0].rules}
    assert {"pd.weapon_involved", "pd.dispatch_cue"} <= rules
    assert evs[0].what_score >= 0.8


def test_the_follow_up_marks_the_weapons_event_de_escalated_and_never_pushes():
    e, sink = engine()
    [ev] = e.feed(row("t1", KNIFE_CALL, 1000))
    out = e.feed(row("t2", KNIFE_FOLLOW_UP, 1108))
    assert [(x.id, x.category, x.status, x.note) for x in out] == [(ev.id, "weapons", "open", "de-escalated")]
    assert "t2" in out[0].traffic_ids
    assert pushed(sink) == ["weapons"]                          # the follow-up is no new push
    assert sink.events[-1][1] == "updated"
    assert e.feed(row("t3", "be advised she got the knife away from him", 1150)) == []     # once


def test_a_weapons_call_with_no_place_never_swallows_the_next_assault_call():
    e, sink = engine(locate=None)
    e.feed(row("t1", KNIFE_CALL, 1000))
    evs = e.feed(row("t2", ASSAULT_CALL, 1500))
    assert [ev.category for ev in evs] == ["assault"] and pushed(sink) == ["weapons", "assault"]


def test_a_de_escalation_with_nothing_open_does_nothing():
    e, sink = engine()
    assert e.feed(row("t1", KNIFE_FOLLOW_UP, 1000)) == [] and sink.pushes == []


def test_a_de_escalation_on_another_source_leaves_the_event_alone():
    e, sink = engine()
    e.feed(row("t1", KNIFE_CALL, 1000))
    assert e.feed(row("t2", KNIFE_FOLLOW_UP, 1100, source="p25:mrrs:9212", talkgroup=9212)) == []


def test_an_assault_dispatch_pushes_as_assault_with_the_suspect_gone():
    e, sink = engine()
    evs = e.feed(row("t1", ASSAULT_CALL, 1000))
    assert [ev.category for ev in evs] == ["assault"] and pushed(sink) == ["assault"]
    assert evs[0].what_score >= 0.6


@pytest.mark.parametrize("text, cat", [
    ("a woman who said her son is out of control and destroying the apartment", "edp"),
    ("respond to 40 Sample Street for a violent juvenile, mother called", "edp"),
    ("caller states her husband has a knife, respond to 40 Sample Street", "weapons"),
    ("a man is threatening people with a knife in front of the store", "weapons"),
    ("respond to 40 Sample Street, assault in progress", "dispute"),
    ("caller states a male was attacked with a bat, respond to the park", "weapons"),   # an armed assault: one push
    ("got a call about a sexual assault, respond to 40 Sample Street", "assault"),
    ("he said he got jumped by three males, can you respond", "assault"),
    ("be advised the male has an assault rifle, respond to 40 Sample Street", "weapons"),   # a weapon, never assault
    ("security believes the male does have a weapon, respond to the courtyard", "weapons"),
    ("can you respond to 40 Sample Street, complainant states his cousin punched him in the eye", "assault"),
])
def test_dispatch_shapes_push_their_category(text, cat):
    e, sink = engine()
    evs = e.feed(row("t1", text, 1000))
    assert [ev.category for ev in evs] == [cat], [h.rule_id for h in sink.hits]
    assert pushed(sink) == [cat]


@pytest.mark.parametrize("text", [
    "car 12, records check on the assault rifle, registered to the owner",              # no assault, no weapons
    "assault rifle",
    "respond to 40 Sample Street, caller states no weapons involved",
    "respond to 40 Sample Street, caller states no knife involved",
    "caller states she cut her hand with a knife and fork, respond to 40 Sample Street",
    "respond to the knife sharpening place on Sample Street for a lockout",
    "caller states he was assaulted last year, just wants a report",                  # report only: no push
    "can you respond to 40 Sample Street, a man said he wants to make a report about an assault",
    "respond to the school for a report of a past sexual assault, the victim is at the hospital",       # report only
    "be advised, no reported weapons involved, respond to 40 Sample Street",
    "caller states a fight, any weapons involved?",
    "car 12 to dispatch, starting mileage 1234, I have a rifle for you",                 # an officer's own rifle
    "we'll be carrying a gun for the detail, respond to 40 Sample Street",
    "she was assaulted",                                                              # no dispatch cue: under
    "her son is out of control",                                                      # no dispatch cue: under
    "a dog attacked her, she was attacked by a dog, respond to 40 Sample Street",
    "be advised car 12 is responding",                                                # cues alone
    "got a call from the caller, she stated she wants a callback",
])
def test_quiet_shapes_never_push(text):
    e, sink = engine()
    e.feed(row("t1", text, 1000))
    assert pushed(sink) == [], [(h.rule_id, h.weight) for h in sink.hits]


def test_the_dispatch_cue_knows_the_relayed_call_shapes():
    from sizeup.intel import rules as rules_mod
    [pack] = [p for p in rules_mod.load_packs() if p.name == "police"]
    [cue] = [r for r in pack.rules if r.id == "pd.dispatch_cue"]
    assert cue.w == 0.2
    for text in ("got a call from the neighboring PD", "received a call regarding a male", "a woman who said",
                 "the complainant states", "she stated", "call back the caller", "can you respond",
                 "units responding", "be advised"):
        assert cue.hit(text, rules_mod.words(text)), text


def test_assault_alone_is_under_and_with_a_cue_is_at_the_threshold():
    from sizeup.intel import rules as rules_mod
    [pack] = [p for p in rules_mod.load_packs() if p.name == "police"]
    w = {r.id: r.w for r in pack.rules}
    assert w["pd.assault"] < pack.threshold and w["pd.out_of_control"] < pack.threshold
    assert round(1 - (1 - w["pd.assault"]) * (1 - w["pd.dispatch_cue"]), 4) >= pack.threshold
    assert pack.category("assault")["safety"] is False and pack.category("assault")["defer_to"] == ""
    assert KEY in pack.scope["sources"]
