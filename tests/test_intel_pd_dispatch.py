"""The police pack's dispatch rules, each tried on invented scenes: the dispatch cue's wording, a weapon involved or
held, an assault, an officer who needs help, an out-of-control person, the weapons category's "deescalate" note, and
the tie words that keep a disregard or a unit's move off an unrelated event. Every address, unit and person is
invented."""

from __future__ import annotations

import pytest

from sizeup.intel import rules as rules_mod
from tests.test_intel import KEY, engine, row

GUN_CALL = "Respond to 100 Example Ave, caller states a male pointed a gun at a delivery driver in the driveway."
SECURED = "Units be advised the gun was secured by the homeowner, it was a starter pistol."
MACHETE_CALL = "Car 12, can you respond to 7 Sample Street, a male waving a machete outside the deli."
PUNCH_CALL = "Can you respond to 20 Example Plaza, the bartender says a patron was punched, the suspect is still inside."
CUE = "respond to 40 Sample Street, "


def pushed(sink) -> list[str]:
    return [ev.category for ev, d in sink.pushes if d.would_push]


def police():
    [pack] = [p for p in rules_mod.load_packs() if p.name == "police"]
    return pack


# -- weapons and assault push as themselves ---------------------------------------------------------------------------

def test_a_male_waving_a_machete_pushes_as_weapons():
    e, sink = engine()
    evs = e.feed(row("t1", MACHETE_CALL, 1000))
    assert [ev.category for ev in evs] == ["weapons"] and pushed(sink) == ["weapons"]
    assert {"pd.weapon_involved", "pd.dispatch_cue"} <= {r[0] for r in evs[0].rules}


def test_a_patron_punched_pushes_as_assault_with_the_suspect_still_there():
    e, sink = engine()
    evs = e.feed(row("t1", PUNCH_CALL, 1000))
    assert [ev.category for ev in evs] == ["assault"] and pushed(sink) == ["assault"]


# -- de-escalation: which event, which words, and when it stops ---------------------------------------------------------

def test_a_gun_secured_notes_the_weapons_event_de_escalated_and_never_pushes():
    e, sink = engine()
    [ev] = e.feed(row("t1", GUN_CALL, 1000))
    out = e.feed(row("t2", SECURED, 1100))
    assert [(x.id, x.status, x.note) for x in out] == [(ev.id, "open", "de-escalated")]
    assert "t2" in out[0].traffic_ids and pushed(sink) == ["weapons"]
    assert e.feed(row("t3", "be advised the firearm has been recovered", 1150)) == []          # once


def test_a_de_escalation_with_nothing_open_or_on_another_source_does_nothing():
    e, sink = engine()
    assert e.feed(row("t1", SECURED, 1000)) == [] and sink.pushes == []
    e, sink = engine()
    e.feed(row("t1", GUN_CALL, 1000))
    assert e.feed(row("t2", SECURED, 1100, source="p25:mrrs:9212", talkgroup=9212)) == []


def two_weapons_calls():
    e, sink = engine()
    [a] = e.feed(row("t1", "caller states the male has a machete at 100 Example Ave", 1000))
    [b] = e.feed(row("t2", "second caller, a male has a gun at Sample St and Test Rd", 1030))
    assert a.id != b.id and a.precision != "none" and b.precision != "none"
    return e, sink, a, b


def test_with_two_events_open_the_de_escalation_goes_to_the_place_it_says():
    e, sink, a, b = two_weapons_calls()
    e.feed(row("t3", "units be advised the gun was secured at Sample St and Test Rd", 1060))
    assert e.events.get(b.id).note == "de-escalated" and e.events.get(a.id).note == ""


def test_with_two_events_open_and_no_place_said_nothing_is_marked():
    e, sink, a, b = two_weapons_calls()
    e.feed(row("t3", "be advised the weapon is secured", 1060))
    assert e.events.get(a.id).note == "" and e.events.get(b.id).note == ""


@pytest.mark.parametrize("text", [
    "car 20, no threat to anyone, the vehicle is legally parked",
    "car 20 the driver is not armed with paperwork, he is calm",
    "car 20, the subject at the other job wasn't using it to threaten anyone",    # no weapon word, no tie
])
def test_unrelated_traffic_never_de_escalates(text):
    e, sink = engine()
    [ev] = e.feed(row("t1", "caller states the male has a machete at 100 Example Ave", 1000))
    e.feed(row("t2", text, 1060))
    assert e.events.get(ev.id).note == ""


def test_a_row_that_names_a_weapon_again_never_de_escalates():
    e, sink = engine()
    [ev] = e.feed(row("t1", GUN_CALL, 1000))
    e.feed(row("t2", "be advised the gun was secured, the second male has a machete", 1060))
    assert e.events.get(ev.id).note != "de-escalated"


def test_a_safety_escalation_after_a_de_escalation_clears_the_note_and_pushes_again():
    e, sink = engine()
    [ev] = e.feed(row("t1", GUN_CALL, 1000))
    e.feed(row("t2", SECURED, 1100))
    e.feed(row("t3", "units be advised, he grabbed a gun from the car", 1500))
    assert e.events.get(ev.id).note == "re-opened" and pushed(sink) == ["weapons", "weapons"]


@pytest.mark.parametrize("text", [
    "for the report, he had a gun in his waistband when we arrived",
    "he had a gun in his pocket too, he's in custody",
    "for the report, a gun was involved, no injuries",
    "be advised a gun was involved, one in custody",
])
def test_an_after_action_mention_after_a_de_escalation_clears_the_note_without_a_push(text):
    e, sink = engine()
    [ev] = e.feed(row("t1", GUN_CALL, 1000))
    e.feed(row("t2", SECURED, 1100))
    e.feed(row("t3", text, 1110))
    assert e.events.get(ev.id).note == "" and pushed(sink) == ["weapons"]


def test_after_the_window_an_after_action_mention_clears_the_note_and_an_escalation_pushes_again():
    e, sink = engine()
    [ev] = e.feed(row("t1", GUN_CALL, 1000))
    e.feed(row("t2", SECURED, 1100))
    e.feed(row("t3", "the male had the gun in his waistband when we arrived", 1600))     # the call-out left the window
    assert e.events.get(ev.id).note == "" and pushed(sink) == ["weapons"]
    e, sink = engine()
    [ev] = e.feed(row("t1", GUN_CALL, 1000))
    e.feed(row("t2", SECURED, 1100))
    e.feed(row("t3", "units be advised, he is armed again", 1600))
    assert e.events.get(ev.id).note == "re-opened" and pushed(sink) == ["weapons", "weapons"]


def test_a_de_escalation_from_another_job_needs_the_place():
    e, sink = engine()
    [ev] = e.feed(row("t1", "caller states the male has a machete at 100 Example Ave", 1000))
    e.feed(row("t2", "car 20 at the other job, the knife was secured in the kitchen", 1060))
    assert e.events.get(ev.id).note == ""
    e.feed(row("t3", "car 20, the machete was secured at 100 Example Ave", 1080))
    assert e.events.get(ev.id).note == "de-escalated"


def test_officers_are_a_safety_call_and_never_tie_by_the_word_officer():
    e, sink = engine()
    [ev] = e.feed(row("t1", "officer down, officer down, 40 Sample Street", 1000))
    e.feed(row("t2", "disregard, officer 14 back in service", 1300))
    assert e.events.get(ev.id).status == "open"
    e, sink = engine()
    [ev] = e.feed(row("t1", "officer down, officer down, 40 Sample Street", 1000))
    e.feed(row("t2", "disregard, disregard", 1020))                      # nameless: only "possibly cancelled"
    got = e.events.get(ev.id)
    assert got.status == "open" and got.note == "possibly cancelled"
    assert "officer" not in police().category("officer")["words"]


def test_a_possibly_cancelled_note_is_never_overwritten():
    e, sink = engine()
    [ev] = e.feed(row("t1", "caller states the male has a machete at 100 Example Ave", 1000))
    e.feed(row("t2", "disregard, disregard", 1020))
    assert e.events.get(ev.id).note == "possibly cancelled"
    e.feed(row("t3", "be advised the machete was secured", 1040))
    assert e.events.get(ev.id).note == "possibly cancelled"


# -- tie words: a nameless disregard or a unit's move never reaches an unrelated event -------------------------------

def test_a_disregard_with_common_words_never_ends_a_weapons_event():
    e, sink = engine()
    [ev] = e.feed(row("t1", "caller states the male has a machete at 100 Example Ave", 1000))
    e.feed(row("t2", "disregard, car 14 going back in service, coming from the house", 1300))
    assert e.events.get(ev.id).status == "open"


@pytest.mark.parametrize("text", [
    "respond to 100 Example Ave for an EDP, he is threatening to jump",
    "respond to 100 Example Ave, large fight in progress",
])
def test_a_disregard_with_common_words_never_ends_an_edp_or_a_fight(text):
    e, sink = engine()
    [ev] = e.feed(row("t1", text, 1000))
    e.feed(row("t2", "disregard, car 14 going back in service, getting coffee near the house", 1300))
    assert e.events.get(ev.id).status == "open"


def test_a_units_move_with_common_words_never_places_an_unplaced_weapons_event():
    e, sink = engine()
    [ev] = e.feed(row("t1", "caller states the male has a machete", 1000))
    assert ev.precision == "none"
    e.feed(row("t2", "car 14 going to Sample St and Test Rd to check a vehicle", 1060))
    assert e.events.get(ev.id).precision == "none"


def test_weapons_edp_and_dispute_tie_by_telling_words_only():
    p = police()
    for cat in ("weapons", "edp", "dispute"):
        ws = set(p.category(cat)["words"])
        assert ws and not ws & {"going", "coming", "have", "were", "kitchen", "house", "room", "them", "being", "getting"}


# -- what pushes, and as what -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, cat", [
    ("can you respond to 40 Sample Street, a combative male in the hotel lobby", "edp"),
    (CUE + "for a violent male at the bus stop", "edp"),
    (CUE + "caller states her husband has a knife", "weapons"),
    ("a man is threatening people with a knife in front of the store", "weapons"),
    (CUE + "caller states he's got a knife", "weapons"),
    ("caller states the male pulled out a gun in the lot", "weapons"),
    (CUE + "the male has a box cutter", "weapons"),
    ("a male swinging a golf club in the street, units respond", "weapons"),
    (CUE + "the suspect has a bat", "weapons"),
    (CUE + "the man has a bb gun at the bus stop", "weapons"),
    (CUE + "the suspect has an airsoft gun", "weapons"),
    (CUE + "caller believes the male may have a weapon", "weapons"),
    (CUE + "the male has an assault rifle", "weapons"),                              # a weapon, never assault
    (CUE + "a male was attacked with a bat", "weapons"),                             # an armed assault: one push
    (CUE + "assault in progress", "dispute"),
    (CUE + "be advised, he's assaulting her right now", "dispute"),
    (CUE + "caller states he is choking her", "dispute"),
    (CUE + "for a sexual assault, the victim is with the manager", "assault"),
    ("he said he got jumped by three males, can you respond", "assault"),
    (CUE + "complainant states a coworker punched him", "assault"),
    ("car 12 to headquarters, I've been assaulted, send me another car", "officer"),
    ("officer down, officer down, 40 Sample Street", "officer"),
    ("car 12, I'm being attacked", "officer"),                                       # one push: never dispute too
    ("car 12, I'm hit, I'm hit", "officer"),
    ("caller states I've been assaulted, can you respond to 40 Sample Street", "assault"),
    ("the caller said I'm being attacked by my neighbor, respond to 40 Sample Street", "dispute"),
])
def test_dispatch_shapes_push_their_category(text, cat):
    e, sink = engine()
    evs = e.feed(row("t1", text, 1000))
    assert [ev.category for ev in evs] == [cat], [h.rule_id for h in sink.hits]
    assert pushed(sink) == [cat]


@pytest.mark.parametrize("text", [
    # weapons: the past, kitchens, shops and licences, lawful ownership, an officer's own equipment, questions, negatives
    CUE + "in the earlier incident last week a knife was involved",
    CUE + "the male is holding a knife and fork at the table",
    CUE + "the chef has a knife in the kitchen",
    CUE + "he was cutting vegetables, he has a knife",
    "respond to the gun range on Sample Street, a question about a gun permit",
    CUE + "caller states he has a gun, it's registered, he's a collector",
    CUE + "the officer has his gun",
    "car 12 has a shotgun in the trunk",
    CUE + "he is holding the gun for property",
    "car 12, I have a rifle in the car",
    "we'll be carrying a gun for the detail, " + CUE,
    "caller states a fight, weapons involved?",
    CUE + "was a weapon involved?",
    "caller states a fight, any weapons involved?",
    CUE + "caller states no weapons involved",
    CUE + "caller states no knife involved",
    CUE + "be advised, no reported weapons involved",
    CUE + "negative weapons",
    CUE + "unknown if he has a knife",
    CUE + "she doesn't know if he has a gun",
    CUE + "nobody has a weapon",
    CUE + "she doesn't think he has a knife",
    CUE + "he put the knife away",
    CUE + "kids with a toy gun",
    CUE + "male has a water gun, squirt gun seen",
    "respond to the knife sharpening place on Sample Street for a lockout",
    "assault rifle",
    "car 12, records check on the assault rifle, registered to the owner",
    # bare armed / brandishing, a barricade for a parade, a fight on TV, a dispute that was physical earlier
    CUE + "he is in the armed services",
    "armed guards on scene, respond to the bank",
    CUE + "the security guard is armed",
    CUE + "a male brandishing a cell phone",
    "be advised Sample Street is barricaded for the parade",
    CUE + "caller states a large fight on the TV",
    CUE + "the dispute was physical earlier",
    # an out-of-control person who isn't one now
    CUE + "caller states her husband is out of control with his spending",
    CUE + "the boarder keeps breaking the house rules",
    CUE + "her daughter is out of control, the school wants a PINS petition",
    CUE + "her brother was violent last week",
    CUE + "her husband was out of control but he is calm now",
    "her husband is out of control",                                                      # no dispatch cue: under
    # assault: arrests and charges, the past, report-only, animals, jump starts, sport
    CUE + "the male was arrested for assault",
    "can you respond, she was assaulted last night",
    CUE + "he was assaulted this morning, wants to speak to someone",
    CUE + "for an assault report",
    CUE + "charges pressed for the assault",
    "caller states he was assaulted last year, just wants a report",
    "can you respond to 40 Sample Street, a man said he wants to make a report about an assault",
    CUE + "caller wants to report a past assault",
    CUE + "a dog attacked her",
    CUE + "she was attacked by a coyote",
    CUE + "a pitbull attacked her dog",
    "can you respond, he jumped her car, the battery is dead",
    CUE + "the car was jumped already",
    CUE + "he got jumped during the game",
    "she was assaulted",                                                              # no dispatch cue: under
    # an officer's routine request, and words that only look like an officer in trouble
    "send me another car",
    "officer down the street at the corner store",
    "officer by the river, officer down by the river checking the boat",
    "officer at headquarters, officer down at headquarters for lunch",
    "officer needs help with the tow",
    "officer needs assistance with traffic at the parade",
    "officer needs help lifting the patient",
    "officer needs help carrying the stretcher",
    "officer shot a video of the damage",
    "officer shot the photos for the report",
    "car 12, I'm hit, I'm okay, minor accident",
    "car 12, I'm hit, rear ended at the light",
    "caller states I'm being attacked by my dog",
    "I just got attacked by a dog",
    # an assault in progress that isn't one
    CUE + "she is being attacked by bees",
    CUE + "he's attacking her online, posting about her",
    CUE + "a dog is attacking him",
    # an assault report
    CUE + "assault victim at the hospital, wants to speak with an officer",
    CUE + "she was assaulted on the bus, she's at the station",
    CUE + "she was assaulted, she wants to file a complaint",
    CUE + "the student was assaulted at school today",
    CUE + "he says he was attacked in a dream",
    CUE + "she was assaulted on social media",
    # bats, sport, tools at work, hunters, locked up, last time
    CUE + "she has a bat in her bedroom",
    CUE + "a bat flying in the apartment, the male has a bat trapped",
    CUE + "the boy has a bat at little league practice",
    CUE + "the contractor has a hammer working on the roof",
    CUE + "he has a gun at home, he's a hunter",
    CUE + "he has a gun but it's locked up",
    CUE + "he had a knife last time",
    "car 12, send me another car for the transport",
    # cues alone
    "be advised car 12 is responding",
    "the complainant states she wants a callback about her parking spot",
])
def test_quiet_shapes_never_push(text):
    e, sink = engine()
    e.feed(row("t1", text, 1000))
    assert pushed(sink) == [], [(h.rule_id, h.weight) for h in sink.hits]


# -- the cues and the weights -----------------------------------------------------------------------------------------

def test_the_dispatch_cue_knows_more_dispatcher_wording():
    [cue] = [r for r in police().rules if r.id == "pd.dispatch_cue"]
    assert cue.w == 0.2
    for text in ("we have a call regarding a parked van", "the neighbor states", "she stated", "can you respond",
                 "units responding", "be advised", "please call back the caller"):
        assert cue.hit(text, rules_mod.words(text)), text


def test_the_urgent_cue_is_no_rush_hour():
    [cue] = [r for r in police().rules if r.id == "pd.urgent_cue"]
    for text, want in (("rush units to the scene", True), ("heavy rush hour traffic", False), ("no rush", False)):
        assert cue.hit(text, rules_mod.words(text)) is want, text


def test_assault_and_out_of_control_need_a_cue_and_assault_is_no_safety_call():
    p = police()
    w = {r.id: r.w for r in p.rules}
    assert w["pd.assault"] < p.threshold and w["pd.out_of_control"] < p.threshold and w["pd.officer_backup"] < p.threshold
    assert round(1 - (1 - w["pd.assault"]) * (1 - w["pd.dispatch_cue"]), 4) >= p.threshold
    assert p.category("assault")["safety"] is False and p.category("officer")["safety"] is True
    assert p.category("officer")["priority"] == p.category("weapons")["priority"] == 5
    assert KEY in p.scope["sources"]
