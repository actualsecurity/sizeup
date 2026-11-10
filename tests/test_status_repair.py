"""Unit status words speech-to-text mishears, put back after our callsigns (sizeup/cad/statusrepair.py), lists of
units with one status, and a heard status never leaving Disp after Resp. Transmissions, times and IDs are invented."""

import pytest

from sizeup.cad.feed import load_cfg
from sizeup.cad.lists import load
from sizeup.cad.statushints import Phrases
from sizeup.channels import vocabulary_suggestions
from sizeup.transcribe import build_vocabulary
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0
from tests.test_radios import tx

UNITS = load_cfg(load()).units
P = Phrases(UNITS)


def fixed(text):
    return P.repair(text)


def found(text):
    return [(f.intent, f.units) for f in P.find(P.repair(text)[0])]


@pytest.mark.parametrize("heard,shown,status", [
    ("Engine 61 is fine.", "Engine 61 en route.", "en route"),
    ("The Engine 62 was fine", "The Engine 62 en route", "en route"),
    ("Ladder 31 is funny.", "Ladder 31 en route.", "en route"),
    ("Engine 61 in route", "Engine 61 en route", "en route"),
    ("Engine 61 on route.", "Engine 61 en route.", "en route"),
    ("Engine 61 and route", "Engine 61 en route", "en route"),
    ("Engine 61 is in route", "Engine 61 en route", "en route"),
    ("Engine 61 en rout", "Engine 61 en route", "en route"),
    ("Engine 61 and root.", "Engine 61 en route.", "en route"),
    ("23 94 n route.", "23 94 en route.", "en route"),
    ("Engine 61 on vacation.", "Engine 61 on location.", "on location"),
    ("Engine 61 on relocation", "Engine 61 on location", "on location"),
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
    "Engine 61 on Route 120", "Engine 61 stuck in Route 1 traffic", "Engine 61 is fine with that plan",
    "Engine 61 on vacation next week", "The man is on vacation.", "That was in route 22.", "Engine 61 on location",
    "Engine 61 en route", "", "And she's 61 years old.", "Imagine 62",
])
def test_the_same_words_out_of_a_callsign_and_status_slot_stay(text):
    assert fixed(text) == (text, [])


def test_a_misheard_engine_is_put_back_only_before_a_status():
    assert fixed("And she's 61 sex allegation 0912.")[0] == "Engine 61 on location 0912."
    assert fixed("Imagine 62 responding")[0] == "Engine 62 responding"
    assert fixed("Letter 31 is fine.")[0] == "Ladder 31 en route."
    assert found("And she's 61 sex allegation 0912.") == [("onscene", ["E61"])]
    assert fixed("And she's 64 and stable")[1] == []             # no status after: a person's age, left


def test_en_route_and_responding_mean_the_same_status():
    assert found("Engine 61 en route") == found("Engine 61 responding") == found("Engine 61 is fine.") == [("enroute", ["E61"])]
    assert found("Engine 61 en route, 1412") == [("enroute", ["E61"])]        # the readback's time doesn't stop it
    assert found("Engine 61 stuck in Route 1 traffic") == []


@pytest.mark.parametrize("text,units", [
    ("23 91, Engine 61, Ladder 31 on location", ["2391", "E61", "L31"]),
    ("Engine 61, Ladder 31, on location", ["E61", "L31"]),
    ("Engine 61 and Ladder 31 en route", ["E61", "L31"]),
    ("23 91, 23 91 and Engine 61 on location", ["2391", "E61"]),            # the speaker naming himself: once
    ("Engine 61, Ladder 31 is fine.", ["E61", "L31"]),
])
def test_one_status_for_a_list_of_units(text, units):
    [(intent, got)] = found(text)
    assert got == units and intent in ("onscene", "enroute")


@pytest.mark.parametrize("text", ["23 91, Engine 61, Ladder 31", "Engine 61, Ladder 31, 1412.", "Engine 61, are you on location?"])
def test_a_list_with_no_status_is_left_unparsed(text):
    assert found(text) == []


def test_arriving_and_at_location_are_on_location():
    assert found("Engine 61 arriving") == [("onscene", ["E61"])]
    assert found("Engine 61 arriving on location") == [("onscene", ["E61"])]
    assert found("Engine 61 at location") == [("onscene", ["E61"])]
    assert found("Engine 61 arriving in 2 minutes") == found("Engine 61 arriving shortly") == []


def test_the_vocabulary_has_the_status_words_in_radio_phrasing():
    v = vocabulary_suggestions(load().units, [])
    for w in ("en route", "responding", "on location", "on scene", "in service", "available", "returning", "out of service"):
        assert w in v
    assert "Engine 61 en route" in v and "23 91 on location" in v
    built = build_vocabulary(v)
    assert not built["skipped"] and "en-route" in built["phrases"] and "Engine-sixty-one-en-route" in built["phrases"]


# -- the CAD: the suggestion, the rows, the automation ------------------------------------------------------------

@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


def test_a_misheard_en_route_moves_the_unit_and_keeps_what_was_heard(rig):
    rig.created(units=("E60", "L31"))
    rig.radio("307-11-call_11-t0", T0 + 20, "Ladder 31 is fine.", rig="L31", unit="L31")
    assert rig.status("L31") == rig.cad.hints.status_for("enroute")
    h = rig.cad.store.one("SELECT * FROM status_hints WHERE traffic_id = '307-11-call_11-t0'")
    assert h["said"] == "Ladder 31 en route." and h["text"] == "Ladder 31 is fine."
    assert h["phrase"] == "en route (heard “is fine”)" and h["outcome"] == "accepted"


def test_a_misheard_on_location_from_60_controls_readback(rig):
    rig.created(units=("E60", "L31"))
    rig.radio("307-12-call_12-t0", T0 + 20, "Engine 60 on vacation.", rig="E60", unit="E60")
    assert rig.status("E60") == rig.cad.hints.status_for("onscene")


def test_the_radio_rows_show_the_repair_with_the_heard_text(rig):
    rig.cad.record_traffic(tx("307-13-call_13-t0", T0, source="Fire/EMS 15", tg=307, text="Engine 61 around 1412."))
    rig.cad.record_traffic(tx("307-14-call_14-t0", T0 + 1, source="Fire/EMS 15", tg=307, text="The patient is fine."))
    rows = {r["id"]: r for r in rig.cad.comms.query(limit=10)}
    r = rows["307-13-call_13-t0"]
    assert r["text"] == "Engine 61 around 1412." and r["fixed_text"] == "Engine 61 en route 1412."
    assert r["fixes"] == [{"heard": "around", "to": "en route"}]
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


def test_a_chiefs_own_number_one_digit_off_from_his_radio():
    from sizeup.cad.statushints import StatusHints
    h = StatusHints.__new__(StatusHints)
    h.phrases = P
    row = {"text": "2381 on location.", "rig": "2391", "dispatcher": 0}
    assert {u: f.intent for u, (f, _) in h._read(row).items()} == {"2391": "onscene"}
    assert h._read(dict(row, rig="E61")) == {}                                     # someone else's radio: nobody
    assert h._read(dict(row, text="2491 on location.", rig="E61")) == {}
