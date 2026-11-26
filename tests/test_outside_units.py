"""Another department's units on our calls (mutual aid, sizeup/cad/outside.py): the feed's unit lists (and its
"group_units") and the radio assign them beside ours, never as board units. Invented data only: a made-up mutual-aid
board (Sampleville FD: E9901), made-up county apparatus (L9902, R9903, chief 9905), made-up codes and places."""

from __future__ import annotations

import json

import pytest

import tests.test_incident_sources as tis
from sizeup.cad import lists as lists_mod
from sizeup.cad.api import needs
from sizeup.cad.outside import _abbrev, county_departments
from tests.test_cad import make_cad
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0
from tests.test_radios import tx

COUNTY = ({"L9902": "Sampleville", "ENGINE9902": "Sampleville", "R9903": "Exampleton"}, {"990": "Exampleton"})
GROUP = {"OURS": ["E60", "L31", "E9901", "R9903", "9905", "DPT99", "ZZ7"]}     # every department's units, under one group


def lists_file(tmp_path) -> str:
    """The data file with a made-up mutual-aid board (Sampleville FD) and a made-up EMS unit on another board."""
    d = json.loads(lists_mod.DEFAULT_PATH.read_text())
    d["units"]["E9901"] = {"kind": "engine", "spoken": "Engine 9901", "department": "Sampleville FD", "mutual_aid": True}
    d["units"]["DPT99"] = {"kind": "other", "department": "Sampleville FD", "mutual_aid": True}
    d["units"]["99_Eng"] = {"kind": "ma-request", "mutual_aid": True}
    d["units"]["SVEMS1"] = {"kind": "ems", "department": "Sampleville EMS"}
    d["unit_boards"].append({"id": "ma-sampleville", "title": "Sampleville FD mutual aid", "folded": True,
                             "units": ["E9901", "DPT99", "99_Eng", "SVEMS1"]})
    p = tmp_path / "cad_lists.json"
    p.write_text(json.dumps(d))
    return str(p)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    path = lists_file(tmp_path)
    monkeypatch.setattr(tis, "make_cad", lambda tmp, clock=None: make_cad(tmp, clock=clock, lists=path))
    r = AutoRig(tmp_path)
    r.cad.outside._county = COUNTY
    return r


def outside(rig, eid: str = "A1") -> dict[str, dict]:
    return {o["code"]: o for o in rig.inc(eid)["outside"]}


def lines(rig, iid: int) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? ORDER BY at, id", (iid,))]


# -- the feed ---------------------------------------------------------------------------------------------------------


def test_the_feeds_group_list_assigns_outside_units_beside_ours(rig):
    before = {u["name"] for u in rig.cad.store.units()}
    rig.created(units=("E60", "L31", "L9902"), unit_times={"L9902": {"dispatched": T0 + 5, "on_location": T0 + 200}},
                extra={"group_units": GROUP})
    inc = rig.inc()
    assert {a["name"]: a["status"] for a in inc["assignments"]} == {"E60": "DP", "L31": "DP"}   # ours as before
    got = outside(rig)
    assert {k: v["department"] for k, v in got.items()} == {
        "L9902": "Sampleville", "E9901": "Sampleville FD", "R9903": "Exampleton", "9905": "Exampleton", "ZZ7": ""}
    assert "DPT99" not in got                                                     # a department's placeholder: never
    assert (got["L9902"]["dispatched_at"], got["L9902"]["onscene_at"], got["L9902"]["via"]) == (T0 + 5, T0 + 200, "feed")
    assert all(v["cleared_at"] is None for v in got.values())
    ev = lines(rig, inc["id"])
    assert "Mutual aid (County feed): Sampleville L9902; Sampleville FD E9901; Exampleton R9903, 9905; ZZ7" in ev
    assert not any("not on our boards" in e or "not assigned" in e for e in ev)
    # never ours: not on the boards or roster, no board status, no dispatch, no reminder, never our staffing
    assert {u["name"] for u in rig.cad.store.units()} == before
    assert rig.status("E9901") == "AV" and rig.cad.store.unit(rig.uid("E9901"))["incident_id"] is None
    assert rig.cad.store.q("SELECT * FROM dispatches") == []
    assert {s[2] for s in rig.steps() if s[0] == "assign"} == {"E60", "L31"}
    st = rig.cad.state()
    assert not any(code in json.dumps(st["reminders"]) for code in got)
    [shown] = [i for i in st["incidents"] if i["id"] == inc["id"]]
    assert {o["code"] for o in shown["outside"]} == set(got)


def test_a_unit_the_feed_drops_is_cleared_and_back_on_when_listed_again(rig):
    rig.created(units=("E60", "L9902"))
    rig.at(60)
    rig.change(units=("E60",))
    assert outside(rig)["L9902"]["cleared_at"] == T0 + 60
    rig.at(90)
    rig.change(units=("E60", "L9902"))
    assert outside(rig)["L9902"]["cleared_at"] is None
    rig.at(300)
    rig.change(unit_times={"L9902": {"in_service": T0 + 290}})                   # the feed's own in service
    assert outside(rig)["L9902"]["cleared_at"] == T0 + 290
    assert sum(e.startswith("Mutual aid (") for e in lines(rig, rig.iid())) == 1   # said once


def test_codes_that_are_not_another_departments_unit_are_one_line_never_assigned(rig):
    rig.created(units=("E60", "SVEMS1", "99_Eng"))
    assert outside(rig) == {}
    assert "County feed units not assigned: SVEMS1, 99_Eng" in lines(rig, rig.iid())


def test_a_closed_calls_outside_units_show_cleared_at_its_close(rig):
    rig.created(units=("E60", "L9902"))
    rig.at(600)
    rig.change(times={"in_service": T0 + 590}, unit_times={"E60": {"in_service": T0 + 580}}, closed=True)
    inc = rig.inc()
    assert inc["status"] == "closed" and outside(rig)["L9902"]["cleared_at"] == inc["closed_at"]


def test_in_pcfd_mode_a_person_accepting_the_call_brings_its_outside_units(rig):
    rig.mode("pcfd")
    rig.created(units=("E60", "L9902"))
    assert rig.inc()["status"] == "queued" and outside(rig) == {}                 # not our call yet
    rig.cad.accept(rig.iid(), {"operator": "Lt. Test"})
    assert set(outside(rig)) == {"L9902"} and rig.inc()["assignments"] == []    # nothing of ours automatic


def test_a_notification_never_gets_outside_units(rig):
    rig.created(type="NOTIF", units=("E60", "L9902"))
    assert outside(rig) == {}


def test_a_mutual_aid_board_unit_a_person_put_on_shows_once_as_ours(rig):
    rig.created(units=("E60", "E9901"))
    assert set(outside(rig)) == {"E9901"}
    rig.put_on(rig.iid(), "E9901")
    assert "E9901" not in outside(rig) and rig.here("E9901") is not None


# -- the radio --------------------------------------------------------------------------------------------------------


def test_a_mutual_aid_unit_heard_on_the_calls_fireground_is_assigned_from_radio(rig):
    rig.created(units=("E60",), extra={"fireground": "FG1"})
    iid = rig.iid()
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-1", T0 + 60, rig="E9901", radio_id="9901", text="Engine 9901 on location"))
    assert rig.links(iid)["FG1-1"] == ("fireground", "FG1")                       # on the Radio card: its key
    o = outside(rig)["E9901"]
    assert (o["via"], o["department"], o["onscene_at"], o["cleared_at"]) == ("radio", "Sampleville FD", T0 + 60, None)
    ev = lines(rig, iid)
    assert "Mutual aid from radio: Sampleville FD E9901" in ev and "E9901 on location (radio)" in ev
    assert rig.status("E9901") == "AV" and rig.here("E9901") is None            # never a board assignment
    rig.clock.t = T0 + 90
    rig.cad.record_traffic(tx("FG1-2", T0 + 90, rig="E9901", radio_id="9901", text="Engine 9901 has command"))
    assert outside(rig)["E9901"]["command_at"] == T0 + 90
    assert sum(e.startswith("Mutual aid from radio") for e in lines(rig, iid)) == 1


def test_another_units_status_said_on_its_radio_is_never_its_own(rig):
    rig.created(units=("E60",), extra={"fireground": "FG1"})
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-1", T0 + 60, rig="E9901", radio_id="9901", text="Engine 60 on location"))
    o = outside(rig)["E9901"]
    assert o["via"] == "radio" and o["onscene_at"] is None


def test_heard_and_listed_is_the_feeds_never_from_radio(rig):
    rig.created(units=("E60",), extra={"fireground": "FG1", "group_units": {"OURS": ["E60", "E9901"]}})
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-1", T0 + 60, rig="E9901", radio_id="9901", text="water on the fire"))
    assert [(o["code"], o["via"]) for o in rig.inc()["outside"]] == [("E9901", "feed")]
    assert not any(e.startswith("Mutual aid from radio") for e in lines(rig, rig.iid()))


def test_heard_first_then_listed_drops_the_from_radio_mark(rig):
    rig.created(units=("E60",), extra={"fireground": "FG1"})
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-1", T0 + 60, rig="E9901", radio_id="9901", text="water on the fire"))
    assert outside(rig)["E9901"]["via"] == "radio"
    rig.change(units=("E60", "E9901"))
    assert outside(rig)["E9901"]["via"] == "feed"
    rig.at(200)
    rig.change(units=("E60",))                                                    # dropped by the feed: cleared
    assert outside(rig)["E9901"]["cleared_at"] == T0 + 200


def test_a_radio_not_on_the_calls_channels_nor_about_it_is_never_assigned(rig):
    rig.created(units=("E60",))                                                   # no fireground known
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-1", T0 + 60, rig="E9901", radio_id="9901", text="water on the fire"))
    assert "FG1-1" not in rig.links(rig.iid())
    rig.radio("307-1-call_1-t0", T0 + 70, "Engine 9901 responding", rig="E9901", unit="E9901")   # our dispatch talkgroup
    assert outside(rig) == {}


def test_two_open_calls_on_one_fireground_assign_neither(rig):
    rig.created(units=("E60",), extra={"fireground": "FG1"})
    rig.feed.put("B1", type="STRUCTURE FIRE", location="400 Sample St", units=("E61",), times={"dispatched": rig.clock.t},
                 extra={"fireground": "FG1"})
    rig.poll()
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-1", T0 + 60, rig="E9901", radio_id="9901", text="water on the fire"))
    assert outside(rig) == {} and outside(rig, "B1") == {}


# -- one call run in two of our towns (#807) --------------------------------------------------------------------------


def test_a_two_town_call_is_one_incident_with_both_runs_outside_units(tmp_path, monkeypatch):
    from sizeup.cad import sources
    monkeypatch.setattr(sources, "OWN_TOWNS", frozenset({"our town a", "our town b"}))
    monkeypatch.setattr(sources, "STREETS", {})
    path = lists_file(tmp_path)
    monkeypatch.setattr(tis, "make_cad", lambda tmp, clock=None: make_cad(tmp, clock=clock, lists=path))
    rig = AutoRig(tmp_path)
    rig.cad.outside._county = COUNTY
    rig.feed.put("F0080001", type="ALARM/COMM", location="40 EXAMPLE ST", units=("E62", "L9902"),
                 times={"dispatched": T0}, extra={"town": "Our Town A", "jurisdiction": "Our FD"})
    rig.poll()
    iid = rig.iid("F0080001")
    rig.at(130)
    rig.feed.put("F0080907", type="ALARM/COMM", location="40 EXAMPLE ST", units=("E62", "E64", "R9903"),
                 times={"dispatched": T0 + 120}, extra={"town": "our town b", "jurisdiction": "Our FD"})
    rig.poll()
    assert rig.iid("F0080907") == iid and len(rig.incidents()) == 1
    assert set(outside(rig, "F0080001")) == {"L9902", "R9903"}
    assert {a["name"] for a in rig.inc("F0080001")["assignments"]} == {"E62", "E64"}
    rig.at(400)                                                                   # one run over: the other's stay
    rig.feed.change("F0080907", closed=True, times={"in_service": T0 + 390}, unit_times={"R9903": {"in_service": T0 + 385}})
    rig.poll()
    got = outside(rig, "F0080001")
    assert got["L9902"]["cleared_at"] is None and got["R9903"]["cleared_at"] == T0 + 385


# -- whose, and an admin's name for one --------------------------------------------------------------------------------


def test_the_countys_apparatus_and_department_numbers_say_whose(tmp_path):
    app = tmp_path / "apparatus.csv"
    app.write_text("# made up\ndepartment,dept_number,battalion,station,unit,unit_type,notes\n"
                   "Sampleville,990,1,,Engine 9902,engine,\nSampleville,990,1,,Tower Ladder 99,tower,\n"
                   "Exampleton,991,1,,Engine 9903,engine,\nOtherplace,992,1,,Engine 9903,engine,\n")
    dep = tmp_path / "departments.csv"
    dep.write_text("department,dept_number,battalion,zone,municipality,chief_numbers,notes\nSampleville,990,1,1,X,,\n")
    units, numbers = county_departments(app, dep)
    assert units["E9902"] == units["ENGINE9902"] == units["TL99"] == "Sampleville"
    assert "E9903" not in units                                                   # two departments' number: nobody's
    assert numbers == {"990": "Sampleville"} and _abbrev("Mini Attack 6") == "MA6"


def test_an_admin_names_an_outside_unit_and_it_is_audited(rig):
    rig.created(units=("E60", "ZZ7"))
    o = outside(rig)["ZZ7"]
    r = rig.cad.outside.edit({"id": o["unit_id"], "name": "Sample Engine 7", "department": "Sampleton FD", "operator": "Admin"})
    assert (r["name"], r["department"]) == ("Sample Engine 7", "Sampleton FD")
    o = outside(rig)["ZZ7"]
    assert (o["name"], o["department"]) == ("Sample Engine 7", "Sampleton FD")
    assert [(a["key"], a["new"]) for a in rig.cad.store.q(
        "SELECT key, new FROM settings_audit WHERE section = 'outside_units' ORDER BY id")] == [
        ("ZZ7/name", '"Sample Engine 7"'), ("ZZ7/shown_dept", '"Sampleton FD"')]
    assert needs("/api/cad/outside-units") == "admin"
    from sizeup.cad.store import CadError
    with pytest.raises(CadError):
        rig.cad.outside.edit({"id": o["unit_id"], "name": "x" * 41})
