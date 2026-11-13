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


def test_back_when_listed_again_while_none_of_its_department_signed_on(rig):
    rig.created(units=("DPTA", "E60", "L31"))                          # E60 and L31 dispatched, not responding
    assert rig.status("DPTA") == "DP" and rig.status("E60") == "DP"
    rig.at(30)
    rig.change(units=("E60", "L31"))                                   # the county drops it for one poll: off the call
    assert rig.here("DPTA") is None
    rig.at(60)
    rig.change(units=("DPTA", "E60", "L31"))                           # listed again: no unit of its department signed on
    assert rig.here("DPTA") is not None and rig.status("DPTA") == "DP"


def test_not_back_when_listed_again_after_its_unit_responded(rig):
    rig.created(units=("DPTA", "E60", "L31"))
    rig.at(30)
    rig.change(units=("E60", "L31"))                                   # dropped
    assert rig.here("DPTA") is None
    rig.at(45)
    rig.change(units=("E60", "L31"), unit_times={"E60": {"responding": T0 + 40}})
    assert rig.status("E60") == "RS"
    rig.at(60)
    rig.change(units=("DPTA", "E60", "L31"), unit_times={"E60": {"responding": T0 + 40}})   # listed again
    assert rig.here("DPTA") is None


def test_not_back_when_listed_again_after_a_person_freed_it(rig):
    rig.created(units=("DPTA", "E60", "L31"))
    iid, ph = rig.iid(), rig.uid("DPTA")
    rig.at(30)
    rig.cad.unit_status(ph, {"status": "AQ", "call": iid, "operator": "Lt. Test"})   # a person frees it
    assert rig.here("DPTA") is None
    rig.at(60)
    rig.change(units=("E60", "L31"))                                   # the county drops it ...
    rig.at(90)
    rig.change(units=("DPTA", "E60", "L31"))                           # ... and lists it again
    assert rig.here("DPTA") is None


def test_never_back_once_its_department_signed_on_there_even_cleared_since(rig):
    rig.created(units=("DPTA", "E60"))
    rig.at(30)
    rig.change(unit_times={"E60": {"responding": T0 + 25}})
    rig.cad.store.set_unit_status(rig.uid("E60"), "AQ", "Lt. Test")    # signed on, then cleared
    rig.at(60)
    rig.change(units=("E60",))
    rig.at(90)
    rig.change(units=("E60", "DPTA"))                                  # listed again
    assert rig.here("DPTA") is None


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


# -- review fixes ------------------------------------------------------------------------------------------------------


def test_a_placeholder_is_never_said_on_the_air(rig):
    rig.mode("pcfd")
    iid = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    rig.cad.dispatch(iid, {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.cad.store.assign(iid, [rig.uid("DPTA")], "Lt. Test")
    text = rig.cad.preview(iid, {"unit_ids": [rig.uid("E61")]})["text"]
    assert "Engine 61" in text and "DPTA" not in text
    rig.cad.store.assign(iid, [rig.uid("DPTB")], "Lt. Test")            # Rye Brook's placeholder on it too
    text = rig.cad.preview(iid, {"unit_ids": [rig.uid("E61")]})["text"]
    assert "DPTB" not in text and "Rye Brook" not in text                  # nor its department's lead-in or tones


def test_a_calls_unit_row_frees_it_there_only_and_the_board_from_every_call(rig):
    rig.mode("pcfd")
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    b = rig.cad.create({"type": "STRU", "address": "50 Other Rd", "operator": "Lt. Test"})["id"]
    c = rig.cad.create({"type": "STRU", "address": "60 Other Rd", "operator": "Lt. Test"})["id"]
    ph = rig.uid("DPTA")
    for i in (a, b, c):
        rig.cad.store.assign(i, [ph], "Lt. Test")
    out = rig.cad.unit_status(ph, {"status": "AQ", "call": a, "operator": "Lt. Test"})
    assert out["status"] == "DP" and sorted(out["incident_ids"]) == sorted([b, c])
    marked = lambda: {r["incident_id"] for r in rig.cad.store.q("SELECT incident_id FROM manual_edits WHERE field = 'unit:DPTA'")}
    assert marked() == {a}
    out = rig.cad.unit_status(ph, {"status": "AQ", "operator": "Lt. Test"})              # the board: every call
    assert out["status"] == "AQ" and out["incident_ids"] == [] and sorted(out["cleared_from"]) == sorted([b, c])
    assert marked() == {a, b, c}


def test_dropped_from_the_listing_it_never_holds_up_the_close(rig):
    rig.created(units=("DPTA", "E60"))
    rig.at(30)
    rig.change(units=("E60",))
    assert rig.here("DPTA") is None
    rig.at(600)
    rig.change(times={"in_service": T0 + 590}, closed=True)
    assert rig.inc()["status"] == "closed"


def test_an_unlisted_placeholder_still_on_the_call_is_freed_at_the_close(rig):
    rig.created(units=("E60",))
    rig.cad.store.assign(rig.iid(), [rig.uid("DPTA")], "Paging · auto")   # put on by something but the feed's listing
    rig.at(600)
    rig.change(times={"in_service": T0 + 590}, closed=True)
    assert rig.inc()["status"] == "closed" and rig.status("DPTA") == "AQ"


def test_only_dispatched_or_in_quarters(rig):
    from sizeup.cad.store import CadError
    rig.mode("pcfd")
    st = rig.cad.store
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    b = rig.cad.create({"type": "STRU", "address": "50 Other Rd", "operator": "Lt. Test"})["id"]
    ph = rig.uid("DPTA")
    st.assign(a, [ph], "Lt. Test")
    for code in ("RS", "OL", "OS"):
        with pytest.raises(CadError, match="placeholder"):
            st.set_unit_status(ph, code, "Lt. Test")
    with pytest.raises(CadError, match="placeholder"):
        st.set_unit_status_on(ph, "RS", a, "Lt. Test")
    with pytest.raises(CadError, match="placeholder"):
        st.assign(b, [ph], "Lt. Test", status="RS")
    with pytest.raises(CadError, match="placeholder"):
        rig.cad.attach(b, {"unit_ids": [ph], "status": "RS", "operator": "Lt. Test"})
    st.set_unit_status_on(ph, "DP", b, "Lt. Test")                       # on b too, a kept
    assert sorted(st.unit(ph)["incident_ids"]) == sorted([a, b])
    st.set_unit_status(ph, "AQ", "Lt. Test", call=b)
    st.return_to_incident(b, ph, "DP", "Lt. Test")                       # back on b while on a
    assert sorted(st.unit(ph)["incident_ids"]) == sorted([a, b])
    with pytest.raises(CadError, match="placeholder"):
        st.return_to_incident(b, ph, "RS", "Lt. Test")


def test_undoing_the_sign_on_puts_it_back(rig):
    rig.created(units=("DPTA", "E60"))
    rig.at(30)
    rig.change(unit_times={"E60": {"responding": T0 + 25}})
    assert rig.here("DPTA") is None
    rig.cad.auto.undo(rig.iid(), rig.step("status", "E60")["id"], "Lt. Test")
    assert rig.here("DPTA") is not None and rig.status("DPTA") == "DP"


def test_undoing_the_automatic_close_puts_it_back(rig):
    rig.created(units=("DPTA", "E60", "L31"))
    rig.at(600)
    rig.change(times={"in_service": T0 + 590}, closed=True)
    assert rig.inc()["status"] == "closed" and rig.status("DPTA") == "AQ"
    rig.cad.auto.undo(rig.iid(), rig.step("close")["id"], "Lt. Test")
    assert rig.here("DPTA") is not None and rig.status("DPTA") == "DP"


def test_a_unit_with_no_department_never_releases_one(tmp_path, monkeypatch):
    path = lists_file(tmp_path)
    d = json.loads(open(path).read())
    d["units"]["MAX1"] = {"kind": "engine", "mutual_aid": True}
    d["unit_boards"][0]["units"].append("MAX1")
    open(path, "w").write(json.dumps(d))
    L = lists_mod.load(path)
    assert L.department_of("MAX1") == "" and "MAX1" in L.no_department() and "DPTA" not in L.no_department()
    monkeypatch.setattr(tis, "make_cad", lambda tmp, clock=None: make_cad(tmp, clock=clock, lists=path))
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    st = rig.cad.store
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    st.assign(a, [rig.uid("DPTA"), rig.uid("MAX1")], "Lt. Test")
    st.set_unit_status(rig.uid("MAX1"), "RS", "Lt. Test")
    assert a in st.unit(rig.uid("DPTA"))["incident_ids"]


# -- second review ---------------------------------------------------------------------------------------------------


def test_a_person_only_ever_puts_it_in_quarters_and_a_refusal_marks_nothing(rig):
    from sizeup.cad.store import CadError
    rig.mode("pcfd")
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    ph = rig.uid("DPTA")
    rig.cad.store.assign(a, [ph], "Lt. Test")
    edits = lambda: rig.cad.store.q("SELECT * FROM manual_edits WHERE field = 'unit:DPTA'")
    for body in ({"status": "DP", "incident_id": 0}, {"status": "DP", "incident_id": a}, {"status": "CN"},
                 {"status": "AV", "call": a}, {"status": "RS"}):
        with pytest.raises(CadError, match="placeholder"):
            rig.cad.unit_status(ph, dict(body, operator="Lt. Test"))
    assert edits() == []
    with pytest.raises(CadError, match="placeholder"):
        rig.cad.store.set_unit_status(ph, "CN", "Lt. Test")               # the store too: in quarters only
    assert a in rig.cad.store.unit(ph)["incident_ids"]


def test_never_dispatched_or_attached_by_hand(rig):
    from sizeup.cad.store import CadError
    rig.mode("pcfd")
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    for call in (lambda: rig.cad.dispatch(a, {"unit_ids": [rig.uid("DPTA")], "operator": "Lt. Test"}),
                 lambda: rig.cad.preview(a, {"unit_ids": [rig.uid("DPTA")]}),
                 lambda: rig.cad.attach(a, {"unit_ids": [rig.uid("DPTA")], "operator": "Lt. Test"})):
        with pytest.raises(CadError, match="placeholder"):
            call()
    assert rig.cad.store.unit(rig.uid("DPTA"))["incident_ids"] == []
    rig.mode("60control")
    rig.created(units=("DPTA", "E60"))                                 # the county's: still assigned
    assert rig.here("DPTA") is not None


def test_undoing_two_sign_ons_in_turn_puts_it_back_after_the_last(rig):
    rig.created(units=("DPTA", "E60", "L31"))
    rig.at(30)
    rig.change(unit_times={"E60": {"responding": T0 + 25}})
    rig.at(40)
    rig.change(unit_times={"E60": {"responding": T0 + 25}, "L31": {"responding": T0 + 35}})
    assert rig.here("DPTA") is None
    rig.cad.auto.undo(rig.iid(), rig.step("status", "E60")["id"], "Lt. Test")
    assert rig.here("DPTA") is None                                     # L31 still signed on
    rig.cad.auto.undo(rig.iid(), rig.step("status", "L31")["id"], "Lt. Test")
    assert rig.here("DPTA") is not None and rig.status("DPTA") == "DP"
