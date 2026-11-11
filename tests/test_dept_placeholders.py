"""A department's placeholder on the county's unit list ("department dispatched, no unit yet"; cad_lists.json
unit_placeholders, Lists.placeholder): assigned first, back in quarters once a unit of its department signs on or responds
on that call (store.py _release_placeholders, any source), never put back by a repeat listing, never apparatus in
matching or scope, never heard as a unit. Synthetic only: made-up placeholder codes (DPTA, DPTB) and places."""

from __future__ import annotations

import json

import pytest

import tests.test_incident_sources as tis
from sizeup.cad import lists as lists_mod
from sizeup.cad.sources import drop_departments, units_match
from tests.test_cad import make_cad
from tests.test_dispatch_modes import ADDR, AutoRig
from tests.test_incident_sources import T0


def lists_file(tmp_path) -> str:
    """The data file with two made-up placeholders on the main board: DPTA (PCFD's), DPTB (Rye Brook's)."""
    d = json.loads(lists_mod.DEFAULT_PATH.read_text())
    d["unit_placeholders"] = "DPT[A-Z]"
    d["units"]["DPTA"] = {"kind": "other"}
    d["units"]["DPTB"] = {"kind": "other", "department": "Rye Brook"}
    d["unit_boards"][0]["units"] += ["DPTA", "DPTB"]
    p = tmp_path / "cad_lists.json"
    p.write_text(json.dumps(d))
    return str(p)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    path = lists_file(tmp_path)
    monkeypatch.setattr(tis, "make_cad", lambda tmp, clock=None: make_cad(tmp, clock=clock, lists=path))
    return AutoRig(tmp_path)


def events(rig, iid: int) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? ORDER BY at, id", (iid,))]


def assigned(rig) -> list[str]:
    return [a["name"] for a in rig.inc()["assignments"]]


def released_lines(rig) -> list[str]:
    return [e for e in events(rig, rig.iid()) if e.startswith("DPT") and ": " in e]


# -- the rule ---------------------------------------------------------------------------------------------------------


def test_the_rule_is_the_data_files(tmp_path):
    L = lists_mod.load(lists_file(tmp_path))
    assert L.placeholder("DPTA") and L.placeholder("dpt-a") and not L.placeholder("E60") and not L.placeholder("")
    assert L.department_of("DPTA") == L.department_of("E60") == "Port Chester FD"
    assert L.department_of("DPTB") == L.department_of("E14") == "Rye Brook"
    d = json.loads(lists_mod.DEFAULT_PATH.read_text())
    d["unit_placeholders"] = "DPT["
    with pytest.raises(lists_mod.ListsError, match="unit_placeholders"):
        lists_mod.build(d, "test")


# -- 60 Control: assigned first, released when a unit of its department signs on -----------------------------------------


def test_assigned_first_then_back_in_quarters_when_its_unit_responds_and_never_put_back(rig):
    rig.created(units=("E60", "DPTA"))
    assert rig.inc()["status"] == "active"
    assert assigned(rig) == ["DPTA", "E60"]                            # the placeholder first, whatever the feed's order
    assert rig.status("DPTA") == "DP"
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})           # E60 signs on
    assert rig.status("E60") == "RS" and rig.here("DPTA") is None and rig.status("DPTA") == "AQ"
    assert released_lines(rig) == ["DPTA at quarters: E60 responding"]
    rig.at(90)
    rig.change(comments=((T0 + 85, "still listed"),))                # the same listing again: stays released
    rig.at(120)
    rig.change(units=("E60", "DPTA", "L31"))                           # more units, the placeholder still listed
    assert rig.here("DPTA") is None and rig.status("DPTA") == "AQ"
    assert len(released_lines(rig)) == 1


def test_a_placeholder_never_takes_the_feeds_statuses_and_is_not_the_one_unit(rig):
    rig.created(units=("DPTA", "E60"), unit_times={"DPTA": {"responding": T0 + 5}})
    assert rig.status("DPTA") == "DP"                                  # never "responding" itself
    rig.at(60)
    rig.change(times={"dispatched": T0, "responding": T0 + 55})       # the one apparatus listed: E60 moves
    assert rig.status("E60") == "RS" and rig.here("DPTA") is None


def test_another_departments_unit_never_releases_it(rig):
    rig.created(units=("DPTA", "DPTB", "E60", "E14"))
    assert assigned(rig)[:2] == ["DPTA", "DPTB"]
    rig.at(60)
    rig.change(unit_times={"E14": {"responding": T0 + 55}})           # Rye Brook's engine
    assert rig.here("DPTB") is None and rig.here("DPTA") is not None
    rig.change(unit_times={"E14": {"responding": T0 + 55}, "E60": {"on_location": T0 + 58}})
    assert rig.here("DPTA") is None


def test_not_assigned_when_its_department_already_signed_on(rig):
    rig.created(units=("E60",), unit_times={"E60": {"responding": T0 + 5}})
    rig.at(30)
    rig.change(units=("E60", "DPTA"))
    assert rig.here("DPTA") is None and rig.on("DPTA") is None


def test_back_only_when_the_department_is_dispatched_again_with_none_of_its_units_on(rig):
    rig.created(units=("DPTA",))
    iid = rig.iid()
    rig.at(30)
    rig.cad.dispatch(iid, {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.cad.store.set_unit_status(rig.uid("E60"), "RS", "Lt. Test")    # a person: E60 responding
    assert rig.here("DPTA") is None
    rig.at(60)
    rig.change(units=())                                              # the county drops it ...
    rig.at(90)
    rig.change(units=("DPTA",))                                       # ... and lists it again: E60 is still on
    assert rig.here("DPTA") is None
    rig.cad.store.set_unit_status(rig.uid("E60"), "AQ", "Lt. Test")    # E60 clears
    rig.at(120)
    rig.change(units=())
    rig.at(150)
    rig.change(units=("DPTA",))                                       # dispatched again, none of PCFD's on it
    assert rig.here("DPTA") is not None


def test_every_source_releases_it(rig):
    """A person's status, a unit added responding, a unit sent back to the call: the store releases it each time."""
    rig.mode("pcfd")
    st = rig.cad.store
    inc = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})
    iid = inc["id"]
    st.assign(iid, [rig.uid("DPTA"), rig.uid("DPTB")], "Lt. Test")
    st.assign(iid, [rig.uid("E60")], "Lt. Test", status="RS")           # added responding (not dispatched)
    on = {a["name"] for a in rig.cad.full(iid)["assignments"] if not a["cleared_at"]}
    assert on == {"DPTB", "E60"}
    st.assign(iid, [rig.uid("E14")], "Lt. Test")
    st.set_unit_status(rig.uid("E14"), "AQ", "Lt. Test")                # off before it went
    st.assign(iid, [rig.uid("DPTB")], "Lt. Test")
    st.return_to_incident(iid, rig.uid("E14"), "RS", "Lt. Test")        # sent back responding
    assert not [a for a in rig.cad.full(iid)["assignments"] if a["name"] == "DPTB" and not a["cleared_at"]]
    assert st.unit(rig.uid("DPTB"))["status"] == "AQ"


def test_the_placeholders_assign_undone_then_never_put_back(rig):
    rig.created(units=("DPTA", "E60"))
    s = rig.step("assign", "DPTA")
    rig.cad.auto.undo(rig.iid(), s["id"], "Lt. Test")
    assert rig.here("DPTA") is None
    rig.at(60)
    rig.change(units=())
    rig.at(90)
    rig.change(units=("DPTA", "E60"))                                  # a person took it off: theirs
    assert rig.here("DPTA") is None


# -- open calls from before ------------------------------------------------------------------------------------------


def test_startup_frees_placeholders_whose_unit_already_signed_on_on_open_calls_only(rig):
    rig.mode("pcfd")
    st = rig.cad.store
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    b = rig.cad.create({"type": "STRU", "address": "50 Other Rd", "operator": "Lt. Test"})["id"]
    st.assign(a, [rig.uid("DPTA"), rig.uid("E60"), rig.uid("DPTB")], "Lt. Test")
    st.assign(b, [rig.uid("E14")], "Lt. Test")
    with st.tx() as db:                                               # as left by the code before: E60 responded
        db.execute("UPDATE assignments SET enroute_at = ? WHERE incident_id = ? AND unit_id = ?", (T0, a, rig.uid("E60")))
    assert st.release_placeholders() == [f"DPTA (#{a})"]
    assert st.unit(rig.uid("DPTA"))["status"] == "AQ" and st.unit(rig.uid("DPTB"))["incident_id"] == a
    assert st.release_placeholders() == []                            # once


# -- never apparatus ---------------------------------------------------------------------------------------------------


def test_never_a_unit_in_common_nor_specific_units_nor_heard(rig):
    rig.mode("pcfd")
    rig.cad.sources._towns()
    assert drop_departments(["DPTA", "E60"]) == ["E60"] and units_match(["DPTA"], ["DPTA"]) == set()
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPTA",), times={"dispatched": T0})
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("DPTA",), times={"dispatched": T0})
    rig.poll()
    a, b = rig.iid("A1"), rig.iid("B1")
    assert a != b and not any("possibly the same" in e for e in events(rig, a) + events(rig, b))
    assert rig.cad.full(a)["scope"] is None                            # a placeholder alone: not "Specific units"
    rig.feed.change("A1", units=("DPTA", "E60"))
    rig.poll()
    assert rig.cad.full(a)["scope"]["short"] == "Specific units"
    assert "DPTA" not in {b[0] for b in rig.cad.radiolog.board().values()}


# -- on several calls at once (not a rig) -------------------------------------------------------------------------------


def test_one_placeholder_on_two_calls_released_per_call(rig):
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPTA",), times={"dispatched": T0})
    rig.poll()
    rig.at(30)
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("DPTA", "E61"), times={"dispatched": T0 + 30})
    rig.poll()
    a, b = rig.iid("A1"), rig.iid("B1")
    assert rig.here("DPTA", "A1") is not None and rig.here("DPTA", "B1") is not None    # on both, never moved
    assert not any("not moved" in e for e in events(rig, a) + events(rig, b))
    board = [u for u in rig.cad.store.units() if u["name"] == "DPTA"]
    assert len(board) == 1 and board[0]["status"] == "DP" and sorted(board[0]["incident_ids"]) == sorted([a, b])
    rig.at(60)
    rig.feed.change("B1", unit_times={"E61": {"responding": T0 + 55}})                   # B's engine signs on
    rig.poll()
    assert rig.here("DPTA", "B1") is None and rig.here("DPTA", "A1") is not None        # released on B only
    assert rig.status("DPTA") == "DP"                                                   # still on A
    assert any(e == "DPTA off this call: E61 responding" for e in events(rig, b))
    rig.at(90)
    rig.cad.dispatch(a, {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.cad.store.set_unit_status(rig.uid("E60"), "RS", "Lt. Test")
    assert rig.here("DPTA", "A1") is None and rig.status("DPTA") == "AQ"               # off every call: in quarters
    assert [u for u in rig.cad.store.units() if u["name"] == "DPTA"][0]["incident_id"] is None


def test_a_close_or_undo_on_one_call_leaves_it_on_the_other(rig):
    rig.mode("pcfd")
    st = rig.cad.store
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    b = rig.cad.create({"type": "STRU", "address": "50 Other Rd", "operator": "Lt. Test"})["id"]
    st.assign(a, [rig.uid("DPTA")], "Lt. Test")
    st.assign(b, [rig.uid("DPTA")], "Lt. Test")
    st.close(a, st.lists.disposition_codes[0], "Lt. Test")
    assert rig.status("DPTA") == "DP" and st.unit(rig.uid("DPTA"))["incident_id"] == b
    assert st.placeholder_off_call(b, rig.uid("DPTA"), "Lt. Test") and rig.status("DPTA") == "AQ"
    st.assign(a := rig.cad.create({"type": "STRU", "address": "60 Other Rd", "operator": "Lt. Test"})["id"],
              [rig.uid("DPTA")], "Lt. Test")
    st.assign(b := rig.cad.create({"type": "STRU", "address": "70 Other Rd", "operator": "Lt. Test"})["id"],
              [rig.uid("DPTA")], "Lt. Test")
    st.set_unit_status(rig.uid("DPTA"), "AQ", "Lt. Test")                               # the board: off all of them
    assert not st.q("SELECT 1 FROM assignments WHERE unit_id = ? AND cleared_at IS NULL", (rig.uid("DPTA"),))


def test_the_feeds_in_service_frees_it_on_that_call_only(rig):
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPTA",), times={"dispatched": T0})
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("DPTA",), times={"dispatched": T0})
    rig.poll()
    rig.at(600)
    rig.feed.change("A1", times={"in_service": T0 + 590}, closed=True)
    rig.poll()
    assert rig.inc("A1")["status"] == "closed"
    assert rig.here("DPTA", "B1") is not None and rig.status("DPTA") == "DP"
    s = rig.step("assign", "DPTA", "B1")
    rig.cad.auto.undo(rig.iid("B1"), s["id"], "Lt. Test")                                # undone there: in quarters
    assert rig.here("DPTA", "B1") is None and rig.status("DPTA") == "AQ"
