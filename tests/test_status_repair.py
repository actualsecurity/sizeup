"""Unit status words speech-to-text mishears, put back after our callsigns (sizeup/cad/statusrepair.py), who a status
belongs to (the addressee, lists, the talker), and a heard status never leaving Disp after Resp. Transmissions,
times and IDs are invented."""

import re

import pytest

from sizeup.cad.feed import load_cfg
from sizeup.cad.lists import load
from sizeup.cad.statushints import Phrases, StatusHints
from sizeup.channels import vocabulary_suggestions
from sizeup.transcribe import build_vocabulary
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0
from tests.test_radios import tx

LISTS = load()
UNITS = load_cfg(LISTS).units
P = Phrases(UNITS, {c for c in LISTS.units if re.fullmatch(r"\d+", c)})
YES = lambda code: True          # noqa: E731 - "is fine" may be en route (dispatched on an open call)


def fixed(text, talker="", fine_ok=YES, dispatcher=True):
    text, fixes = P.repair(text, talker, fine_ok, dispatcher)
    return text, [{k: v for k, v in x.items() if k != "at"} for x in fixes]


def found(text, talker="", fine_ok=YES, dispatcher=True):
    return [(f.intent, f.units) for f in P.find(P.repair(text, talker, fine_ok, dispatcher)[0])]


def read(text, rig="", dispatcher=0, fine_ok=YES):
    """unit -> intent, as StatusHints gives them for a transmission on rig's radio."""
    h = StatusHints.__new__(StatusHints)
    h.phrases = P
    return {u: f.intent for u, (f, _) in h._read({"text": text, "rig": rig, "dispatcher": dispatcher}, fine_ok).items()}


# -- the repair ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("heard,shown,status", [
    ("Engine 61 is fine.", "Engine 61 en route.", "en route"),
    ("The Engine 62 was fine", "The Engine 62 en route", "en route"),
    ("Ladder 31 is funny.", "Ladder 31 en route.", "en route"),
    ("Ladder 31's fine.", "Ladder 31 en route.", "en route"),
    ("Engine 61 is in rout", "Engine 61 en route", "en route"),
    ("Engine 61 en rout", "Engine 61 en route", "en route"),
    ("Engine 61 and route 1412.", "Engine 61 en route 1412.", "en route"),
    ("Engine 61 on route, 14:12", "Engine 61 en route, 14:12", "en route"),
    ("Engine 61 on vacation.", "Engine 61 on location.", "on location"),
    ("Engine 61 unlocation", "Engine 61 on location", "on location"),
    ("Engine 61 on the occasion.", "Engine 61 on location.", "on location"),
    ("Engine 61 on allocation", "Engine 61 on location", "on location"),
    ("Engine 61 in location", "Engine 61 on location", "on location"),
    ("Engine 61 online location", "Engine 61 on location", "on location"),
    ("Engine 61 sex allegation 0912.", "Engine 61 on location 0912.", "on location"),
    ("Engine 61 on seen", "Engine 61 on scene", "on scene"),
])
def test_each_mishearing_after_a_callsign_is_put_back(heard, shown, status):
    text, fixes = fixed(heard)
    assert text == shown
    assert [x["to"] for x in fixes] == [status]
    assert found(heard)[-1][0] == {"en route": "enroute", "on location": "onscene", "on scene": "onscene"}[status]


@pytest.mark.parametrize("heard,shown", [
    ("Ladder 31 around 1412.", "Ladder 31 en route 1412."),       # 60 Control's readback, its time after
    ("Ladder 31, around 9:05", "Ladder 31, en route 9:05"),
    ("60 Control copies, Engine 61 around 2207", "60 Control copies, Engine 61 en route 2207"),
    ("Engine 61 around.", "Engine 61 en route."),
])
def test_the_readback_unit_around_a_time_is_en_route(heard, shown):
    assert fixed(heard)[0] == shown
    assert found(heard)[-1][0] == "enroute"


@pytest.mark.parametrize("text", [
    "The patient is fine.", "She is fine, 1412.", "It's fine.", "Turn around 1412.", "Engine 191 is fine.",
    "Engine 61 around the back", "Engine 61 around 5 minutes out", "Engine 61 around 10 minutes",
    "Engine 61 around 120 King Street", "Engine 61 around 1412 King Street",
    "Engine 61 on Route 120", "Engine 61 on route 120.", "Engine 61 on route to King Street", "Engine 61 on route.",
    "Engine 61 stuck in Route 1 traffic", "Engine 61 is fine with that plan", "Engine 61 in relocation",
    "Engine 61 on vacation next week", "The man is on vacation.", "That was in route 22.", "Engine 61 on location",
    "Engine 61 en route", "", "And she's 61 years old.", "Imagine 62",
])
def test_the_same_words_out_of_a_callsign_and_status_slot_stay(text):
    assert fixed(text) == (text, [])


def test_on_route_clause_final_only_from_the_units_own_radio():
    assert fixed("Engine 61 on route.", talker="E61")[0] == "Engine 61 en route."
    assert fixed("Engine 61 on route.", talker="L31")[1] == []
    assert fixed("Engine 61 on route to King Street", talker="E61")[1] == []


def test_is_fine_only_for_a_unit_dispatched_on_an_open_call():
    assert fixed("Engine 61 is fine.", fine_ok=lambda code: False) == ("Engine 61 is fine.", [])
    assert fixed("Engine 61 is fine.", fine_ok=lambda code: code == "E61")[0] == "Engine 61 en route."
    assert fixed("Engine 61 is fine.", fine_ok=None)[1] == []


@pytest.mark.parametrize("text", [
    "Command, PAR, Engine 61 is fine.", "Engine 61 is fine, all members accounted for.", "Engine 61 is fine, no injuries.",
    "Engine 61 is fine, everyone's OK.", "Engine 61 was fine, all good.", "IC, Engine 61 is fine.",
])
def test_is_fine_in_a_welfare_check_stays(text):
    assert fixed(text) == (text, [])


def test_a_misheard_engine_is_put_back_only_before_a_status():
    assert fixed("And she's 61 sex allegation 0912.")[0] == "Engine 61 on location 0912."
    assert fixed("Imagine 62 responding")[0] == "Engine 62 responding"
    assert fixed("Letter 31 around 1412.")[0] == "Ladder 31 en route 1412."
    assert found("And she's 61 sex allegation 0912.") == [("onscene", ["E61"])]
    assert fixed("And she's 64 and stable")[1] == []             # no status after: a person's age, left


def test_en_route_and_responding_mean_the_same_status():
    assert found("Engine 61 en route") == found("Engine 61 responding") == found("Engine 61 is fine.") == [("enroute", ["E61"])]
    assert found("Engine 61 en route, 1412") == [("enroute", ["E61"])]        # the readback's time doesn't stop it
    assert found("Engine 61 stuck in Route 1 traffic") == []


def test_the_heard_label_goes_with_its_own_unit():
    h = StatusHints.__new__(StatusHints)
    h.phrases = P
    got = h._read({"text": "Engine 61 is fine. Ladder 31 en route.", "rig": "", "dispatcher": 1}, YES)
    assert got["E61"][0].phrase == "en route (heard “is fine”)" and got["L31"][0].phrase == "en route"


# -- who gets it: the addressee, lists, the talker ------------------------------------------------------------------

@pytest.mark.parametrize("text,rig,dispatcher,want", [
    ("23 91, responding.", "E61", 0, {"E61": "enroute"}),                       # the one called is not the one going
    ("23 91, responding.", "", 0, {}),
    ("Ladder 31, Engine 61 on location", "E61", 0, {"E61": "onscene"}),
    ("Ladder 31, Engine 61 on location", "", 0, {"E61": "onscene"}),
    ("Engine 62, Engine 61 responding", "E61", 0, {"E61": "enroute"}),
    ("Ladder 31. Engine 61 is fine.", "E61", 0, {"E61": "enroute"}),            # nothing carries past a full stop
    ("23 91, Engine 61, Ladder 31 on location", "2391", 0, {"2391": "onscene", "E61": "onscene", "L31": "onscene"}),
    ("23 91, Engine 61, Ladder 31 on location", "E61", 0, {"E61": "onscene", "L31": "onscene"}),   # 2391 is called
    ("23 91, Engine 61, Ladder 31 on location", "", 0, {"L31": "onscene"}),
    ("Engine 61, Ladder 31, on location, 1412", "", 1, {"E61": "onscene", "L31": "onscene"}),     # 60 Control's readback
    ("Engine 61, responding, 1412", "", 1, {"E61": "enroute"}),
    ("Engine 61 and Ladder 31 en route", "", 0, {"E61": "enroute", "L31": "enroute"}),
    ("Engine 61, Ladder 31 on location.", "L31", 0, {"L31": "onscene"}),        # E61 is called; L31 its own
    ("Engine 61, Ladder 31 on location.", "2391", 0, {"L31": "onscene"}),
])
def test_who_gets_the_status(text, rig, dispatcher, want):
    assert read(text, rig, dispatcher) == want


@pytest.mark.parametrize("text,rig", [("23 91, Engine 61, Ladder 31", "2391"), ("Engine 61, Ladder 31, 1412.", "E61"),
                                      ("Engine 61, are you on location?", "")])
def test_a_list_with_no_status_is_left_unparsed(text, rig):
    assert read(text, rig) == {}


def test_arriving_and_at_location_are_on_location():
    assert found("Engine 61 arriving") == [("onscene", ["E61"])]
    assert found("Engine 61 arriving on location") == [("onscene", ["E61"])]
    assert found("Engine 61 at location") == [("onscene", ["E61"])]
    assert found("Engine 61 arriving in 2 minutes") == found("Engine 61 arriving shortly") == []


# -- a bare number, a chief's number one digit off ------------------------------------------------------------------

def test_a_chiefs_own_number_last_digit_off_from_his_radio():
    assert read("2390 on location.", "2391") == {"2391": "onscene"}             # 2390: no unit's number
    assert read("2390 on location.", "E61") == {}                                # someone else's radio: nobody
    assert read("2399 on location.", "2391") == {"2399": "onscene"}              # 2399 is a unit itself: its own
    assert read("2381 on location.", "2391") == {}                               # not the last digit
    assert read("2161 on location.", "2162") == {}                               # another department's chief, known
    assert read("2490 on location.", "2391") == {}                               # another department's prefix


# -- in the area ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,rig,want", [
    ("Engine 61 in the area", "E61", {"E61": "onscene"}),
    ("In the area.", "E61", {"E61": "onscene"}),
    ("Engine 61 is in the vicinity.", "E61", {"E61": "onscene"}),
    ("Ladder 31 on the block", "L31", {"L31": "onscene"}),
    ("Engine 61 pulling up.", "E61", {"E61": "onscene"}),
    ("Engine 61 in the area, 1412", "E61", {"E61": "onscene"}),
    ("Engine 61, Ladder 31 in the area", "L31", {"L31": "onscene"}),           # E61 is called
    ("23 91, Engine 61, Ladder 31 in the area", "2391", {"2391": "onscene", "E61": "onscene", "L31": "onscene"}),
])
def test_in_the_area_is_the_talkers_own_arrival(text, rig, want):
    assert read(text, rig) == want                       # no arriving status in cad_lists.json: on location


@pytest.mark.parametrize("text,rig", [
    ("Lots of smoke in the area", "E61"), ("Engine 61 in the area of King Street", "E61"), ("PD is in the area", "E61"),
    ("Engine 61 pulling up the hydrant", "E61"), ("In the area of the park", "E61"), ("Engine 61 in the area", "L31"),
    ("Engine 61 in the area", ""), ("Engine 61 back in the area", "E61"), ("Engine 61 in the area if you need us", "E61"),
    ("Engine 61 in the area, we can take it", "E61"), ("Engine 61 available in the area", "E61"),
    ("Engine 61 in the area?", "E61"), ("Engine 61 in the area checking the alarm", "E61"),
])
def test_in_the_area_otherwise_is_not_a_status(text, rig):
    assert {u: i for u, i in read(text, rig).items() if i == "onscene"} == {}


def test_the_vocabulary_has_the_status_words_in_radio_phrasing():
    v = vocabulary_suggestions(LISTS.units, [])
    for w in ("en route", "responding", "on location", "on scene", "in service", "available", "returning", "out of service",
              "in the area"):
        assert w in v
    assert "Engine 61 en route" in v and "23 91 on location" in v
    built = build_vocabulary(v)
    assert not built["skipped"] and "en-route" in built["phrases"] and "Engine-sixty-one-en-route" in built["phrases"]


# -- the CAD: the suggestion, the rows, the automation ------------------------------------------------------------

@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


def test_a_misheard_en_route_moves_the_dispatched_unit_and_keeps_what_was_heard(rig):
    rig.created(units=("E60", "L31"))
    rig.radio("307-11-call_11-t0", T0 + 20, "Ladder 31 is fine.", rig="L31", unit="L31")
    assert rig.status("L31") == rig.cad.hints.status_for("enroute")
    h = rig.cad.store.one("SELECT * FROM status_hints WHERE traffic_id = '307-11-call_11-t0'")
    assert h["said"] == "Ladder 31 en route." and h["text"] == "Ladder 31 is fine."
    assert h["phrase"] == "en route (heard “is fine”)" and h["outcome"] == "accepted"
    rows = {r["id"]: r for r in rig.cad.comms.query(limit=10)}
    assert rows["307-11-call_11-t0"]["fixed_text"] == "Ladder 31 en route."      # still shown so once it moved on


def test_is_fine_from_a_unit_already_on_location_is_left(rig):
    rig.created(units=("E60", "L31"))
    rig.radio("307-18-call_18-t0", T0 + 20, "Ladder 31 on location.", rig="L31", unit="L31")
    rig.radio("307-19-call_19-t0", T0 + 40, "Ladder 31 is fine.", rig="L31", unit="L31")
    assert rig.status("L31") == rig.cad.hints.status_for("onscene")
    assert rig.cad.store.one("SELECT 1 AS x FROM status_hints WHERE traffic_id = '307-19-call_19-t0'") is None


def test_a_misheard_on_location(rig):
    rig.created(units=("E60", "L31"))
    rig.radio("307-12-call_12-t0", T0 + 20, "Engine 60 on vacation.", rig="E60", unit="E60")
    assert rig.status("E60") == rig.cad.hints.status_for("onscene")


def test_the_radio_rows_show_the_repair_with_the_heard_text(rig):
    rig.cad.record_traffic(tx("307-13-call_13-t0", T0, source="Fire/EMS 15", tg=307, text="Engine 61 around 1412."))
    rig.cad.record_traffic(tx("307-14-call_14-t0", T0 + 1, source="Fire/EMS 15", tg=307, text="The patient is fine."))
    rows = {r["id"]: r for r in rig.cad.comms.query(limit=10)}
    r = rows["307-13-call_13-t0"]
    assert r["text"] == "Engine 61 around 1412." and r["fixed_text"] == "Engine 61 en route 1412."
    assert [(x["heard"], x["to"]) for x in r["fixes"]] == [("around", "en route")]
    assert "fixed_text" not in rows["307-14-call_14-t0"]


def test_a_unit_put_on_a_call_by_its_own_responding_is_never_dispatched_after_it(rig):
    """Placed by its own "responding" (heard 14 s before it was added): Disp is not after Resp."""
    rig.created(units=("E60",))
    rig.clock.t = T0 + 64
    rig.radio("307-15-call_15-t0", T0 + 50, "Ladder 31 responding", rig="L31", unit="L31")
    a = rig.on("L31")
    assert a is not None and a["enroute_at"] == T0 + 50
    assert a["dispatched_at"] is not None and a["dispatched_at"] <= a["enroute_at"]


def test_a_bare_number_is_the_talkers_own_unit_only(rig):
    rig.created(units=("E60", "L31"))
    rig.radio("307-16-call_16-t0", T0 + 20, "31 responding.", rig="E60", unit="E60")        # another rig: not E60's
    assert rig.status("L31") != rig.cad.hints.status_for("enroute") and rig.status("E60") != rig.cad.hints.status_for("enroute")
    rig.radio("307-17-call_17-t0", T0 + 25, "31 responding.", rig="L31", unit="L31")        # its own radio
    assert rig.status("L31") == rig.cad.hints.status_for("enroute")


# -- re-review: 60 Control, "is fine" next to a change, addresses, a bare space ------------------------------------

@pytest.mark.parametrize("text,want", [
    ("Engine 61, be advised Ladder 31 on location.", {"L31": "onscene"}),
    ("Engine 61, Ladder 31 on location.", {"L31": "onscene"}),
    ("Engine 61, in the area", {}),                               # Control's "in the area": never a unit's arrival
    ("Engine 61, on location", {}),                               # alone: may be a question with its "?" dropped
    ("Engine 61, on location, 1412", {"E61": "onscene"}),         # a readback: its time
    ("Copy, Engine 61, on location.", {"E61": "onscene"}),        # an acknowledgement of what it heard
    ("Engine 61, copy, on location.", {"E61": "onscene"}),        # acknowledged
    ("Engine 61, en route 1412", {"E61": "enroute"}),
    ("Engine 61, Ladder 31, en route 1412", {"E61": "enroute", "L31": "enroute"}),
])
def test_60_controls_readback_and_the_one_it_calls(text, want):
    assert read(text, "", 1) == want


@pytest.mark.parametrize("text", [
    "Engine 61 is fine, you can cancel.", "Engine 61, you can return, Ladder 31 is fine.", "Disregard, Engine 61 is fine.",
    "Engine 61 is fine, hold in quarters.", "Engine 61 is fine, not needed.", "Engine 61 was fine, returning.",
])
def test_is_fine_next_to_a_cancel_or_return_stays(text):
    assert fixed(text, talker="E61")[1] == [] and fixed(text)[1] == []


def test_is_fine_only_from_the_units_own_radio_or_60_control():
    assert fixed("Engine 61 is fine.", talker="E61", dispatcher=False)[0] == "Engine 61 en route."
    assert fixed("Engine 61 is fine.", talker="", dispatcher=True)[0] == "Engine 61 en route."
    assert fixed("Engine 61 is fine.", talker="L31", dispatcher=False)[1] == []
    assert fixed("Engine 61 is fine.", talker="", dispatcher=False)[1] == []
    assert read("Engine 61 is fine.", "L31") == {}


@pytest.mark.parametrize("text", ["Engine 61 around 1412 North Main Street", "Engine 61 and route 1412 King Street",
                                  "Engine 61 around 1412 Westchester Ave."])
def test_a_four_digit_address_is_not_a_readback_time(text):
    assert fixed(text) == (text, [])


def test_en_route_before_an_address_is_not_a_status():
    assert found("Engine 61 en route 1412 Westchester Ave") == []
    assert found("Engine 61 en route 1412.") == [("enroute", ["E61"])]


@pytest.mark.parametrize("text,rig,want", [
    ("23 91 Engine 61 responding", "E61", {"E61": "enroute"}),
    ("23 91 Engine 61 responding", "E62", {"E61": "enroute"}),
    ("23 91 Engine 61 responding", "", {"E61": "enroute"}),
    ("Engine 62 and Engine 64 on location", "", {"E62": "onscene", "E64": "onscene"}),
    ("Engine 62 and Engine 64 on location", "E64", {"E62": "onscene", "E64": "onscene"}),
])
def test_a_bare_space_between_callsigns_is_the_one_called_then_the_talker(text, rig, want):
    assert read(text, rig) == want


@pytest.mark.parametrize("text,want", [
    ("Copy, Engine 61, Ladder 31 responding", {"L31": "enroute"}),
    ("60 Control, Engine 61, Ladder 31 on location.", {"L31": "onscene"}),
    ("Engine 61, Ladder 31, en route 1412", {"E61": "enroute", "L31": "enroute"}),
    ("Copy, Engine 61, Ladder 31 responding, 1412", {"E61": "enroute", "L31": "enroute"}),
])
def test_60_control_credits_a_list_before_the_unit_only_in_a_timed_readback(text, want):
    assert read(text, "", 1) == want


def test_an_unknown_radio_never_repairs_is_fine_or_a_clause_final_on_route():
    """FG1 with no radio ID: not the unit's own radio, not 60 Control."""
    assert fixed("Engine 61 is fine.", talker="", dispatcher=False) == ("Engine 61 is fine.", [])
    assert fixed("Engine 61 on route.", talker="", dispatcher=False) == ("Engine 61 on route.", [])
    assert read("Engine 61 is fine.") == read("Engine 61 on route.") == {}
