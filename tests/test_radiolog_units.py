"""The radio log matched again when an incident gains units, back to its dispatch (#370, sizeup/cad/radiolog.py), and
full department vs specific units (sizeup/cad/scope.py; a feed's department entries never dispatched: automode.py).
Synthetic only: made-up units' words, places, times and department entry codes (DPTA, DPTB)."""

from __future__ import annotations

import pytest

from sizeup.cad.radiolog import UNITS_LEAD_S
from sizeup.dispatch import parse
from tests.test_dispatch_modes import ADDR, AutoRig
from tests.test_incident_sources import PAGE, T0

FULL_PAGE = ("Port Chester Fire Department, respond to an activated fire alarm. 12 Example Avenue, cross of Sample Street. "
             "Time out 1432.")


@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


def depts(rig, **codes) -> None:
    """The hub's own department entry codes (Settings, never the repo)."""
    for k, v in codes.items():
        rig.cad.settings.post("department_units", {"action": "set", "key": k, "value": v, "operator": "Admin"})


def link_rows(rig, iid: int) -> list[tuple]:
    return [(r["traffic_id"], r["n"]) for r in rig.cad.store.q(
        "SELECT traffic_id, COUNT(*) AS n FROM radio_links WHERE incident_id = ? GROUP BY traffic_id", (iid,))]


def events(rig, iid: int) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? ORDER BY at, id", (iid,))]


# -- the radio log, units added late --------------------------------------------------------------------------------------


def test_units_listed_two_minutes_after_dispatch_get_their_radio_from_dispatch(rig):
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=(), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    assert rig.inc()["status"] == "queued"                            # none of ours listed yet
    said = {
        "307-1-call_1-t0": (T0 - 30, "Engine 63 responding"),        # after dispatch less a minute: linked
        "307-2-call_2-t0": (T0 + 45, "60 Control, Engine 63 responding with four"),
        "307-3-call_3-t0": (T0 - 90, "Engine 63 in quarters"),       # before the window
        "307-4-call_4-t0": (T0 + 60, "Engine 63 respond to 400 Sample Street"),   # sent elsewhere
        "999-1-call_1-t0": (T0 + 65, "Engine 63 responding"),        # not our channel
    }
    for tid, (at, text) in said.items():
        assert rig.radio(tid, at, text, tg=999 if tid.startswith("999") else 307,
                         source="PD 1" if tid.startswith("999") else "Fire/EMS 15") == []
    assert rig.radio("p1", T0 + 70, "Port Chester Engine 63, respond for the alarm", tg=None, source="South 2") == []
    assert rig.radio("FG1-1", T0 + 75, "Engine 63, water supply", tg=None, source="FG1") == []   # a radio nobody named
    assert rig.radio("307-5-call_5-t0", T0 + 80, "Engine 63 on location 12 Example Avenue") == [iid]   # the address
    rig.cad.unlink_traffic(iid, {"traffic_id": "307-5-call_5-t0", "operator": "Lt. Test"})              # a person: off
    assert rig.radio("307-6-call_6-t0", T0 + 90, "Engine 60 responding") == []

    rig.at(120)
    rig.change(units=("E63",))                                         # the feed lists it now: accepted, dispatched
    assert rig.here("E63") is not None
    links = rig.links(iid)
    assert links["307-1-call_1-t0"] == ("mention", "E63") and links["307-2-call_2-t0"] == ("mention", "E63")
    assert not {"307-3-call_3-t0", "307-4-call_4-t0", "999-1-call_1-t0", "p1", "FG1-1", "307-5-call_5-t0",
                "307-6-call_6-t0"} & set(links)

    rig.at(180)                                                        # a person adds E60: its radio too
    rig.cad.dispatch(iid, {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    assert rig.links(iid)["307-6-call_6-t0"] == ("mention", "E60")

    # never twice: matched again from scratch, nothing new and one row per transmission
    with rig.cad.store.tx() as db:
        db.execute("DELETE FROM radiolog_units WHERE incident_id = ?", (iid,))
    assert rig.cad.radiolog.backfill_units(iid) == []
    assert all(n == 1 for _, n in link_rows(rig, iid))
    assert rig.cad.radiolog.dispatched_at(iid) == T0 and UNITS_LEAD_S == 60


def test_a_unit_on_another_call_then_is_not_linked_late(rig):
    rig.mode("pcfd")
    b = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})
    rig.cad.dispatch(b["id"], {"unit_ids": [rig.uid("E63")], "operator": "Lt. Test"})
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=(), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    assert rig.radio("307-1-call_1-t0", T0 + 20, "Engine 63 responding") == [b["id"]]
    rig.at(120)
    rig.cad.accept(iid, {"operator": "Lt. Test"})
    rig.cad.dispatch(iid, {"unit_ids": [rig.uid("E63")], "operator": "Lt. Test"})   # moved over now
    assert "307-1-call_1-t0" not in rig.links(iid)                     # it was on the other call then


def test_the_dispatch_time_is_the_feeds_else_the_page_else_made(rig):
    inc = rig.page(text=FULL_PAGE)
    assert rig.cad.radiolog.dispatched_at(inc["id"]) == T0             # the page's time
    rig.at(30)
    c = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})
    assert rig.cad.radiolog.dispatched_at(c["id"]) == T0 + 30          # when it was made


# -- full department or specific units ------------------------------------------------------------------------------------


def test_full_department_from_the_page_tones(rig):
    inc = rig.page(text=FULL_PAGE)
    assert rig.cad.full(inc["id"])["scope"] == {"kind": "full", "departments": ["Port Chester FD"],
                                                 "label": "Full department (Port Chester FD)", "source": "page"}
    rig.at(600)                                                        # two departments' tones on one page
    m = rig.monitor.on_page("p2", ["Port Chester FD", "Rye Brook FD (est.)"],
                            parse(FULL_PAGE.replace("12 Example Avenue", "400 Sample Street")), at=rig.clock.t)
    two = rig.cad.from_monitor(m)
    assert rig.cad.full(two["id"])["scope"]["label"] == "Full department (Port Chester FD, Rye Brook FD)"
    queued = {i["id"]: i for i in rig.cad.state()["incidents"]}
    assert queued[inc["id"]]["scope"]["label"] == "Full department (Port Chester FD)"   # on the queue card too


def test_no_label_from_tones_without_a_call_type(rig):
    inc = rig.page(text="Port Chester Fire Department, 12 Example Avenue, cross of Sample Street. Time out 1432.")
    assert rig.cad.full(inc["id"])["scope"] is None


def test_specific_units_from_a_page_naming_apparatus(rig):
    inc = rig.page(text=PAGE)                                          # Engine 60, Ladder 31
    assert rig.cad.full(inc["id"])["scope"]["label"] == "Specific units"


def test_the_feed_overrides_the_page_and_the_disagreement_is_noted(rig):
    rig.mode("pcfd")
    inc = rig.page(text=FULL_PAGE)
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert rig.iid() == inc["id"]                                      # attached to the page's incident
    s = rig.cad.full(inc["id"])["scope"]
    assert (s["label"], s["source"]) == ("Specific units", "feed")
    assert "County feed: Specific units (page: Full department (Port Chester FD))" in events(rig, inc["id"])
    rig.at(60)
    rig.page(pid="p2", text=FULL_PAGE)                                 # the page again: the feed's stands
    assert rig.cad.full(inc["id"])["scope"]["label"] == "Specific units"
    assert sum("page: Full department" in e or "Page: Full department" in e for e in events(rig, inc["id"])) == 1


def test_a_feed_department_entry_confirms_the_page(rig):
    depts(rig, DPTA="Port Chester FD")
    rig.mode("pcfd")
    inc = rig.page(text=FULL_PAGE)
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPTA",), times={"dispatched": T0})
    rig.poll()
    s = rig.cad.full(inc["id"])["scope"]
    assert (s["label"], s["source"]) == ("Full department (Port Chester FD)", "feed")
    assert not any("(page:" in e for e in events(rig, inc["id"]))      # agreed: nothing to note


def test_a_page_heard_after_the_feed_never_changes_its_label(rig):
    rig.mode("pcfd")
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("E60",), times={"dispatched": T0})
    rig.poll()
    rig.at(20)
    rig.page(text=FULL_PAGE)
    iid = rig.iid()
    assert rig.cad.full(iid)["scope"]["label"] == "Specific units"
    assert "Page: Full department (Port Chester FD) (feed: Specific units)" in events(rig, iid)


# -- a department entry in 60 Control mode ------------------------------------------------------------------------------


def test_a_department_entry_is_never_dispatched(rig, monkeypatch):
    depts(rig, DPTA="Port Chester FD", DPTB="Elsewhere FD")
    monkeypatch.setattr(rig.cad.auto, "_aliases", lambda: {"DPTA": "E60"})   # even a code also aliased to a unit
    assert rig.cad.auto.mode() == "60control"
    rig.created(units=("DPTA", "DPTB"))
    inc = rig.inc()
    assert inc["status"] == "pending" and inc["number"]               # our department paged: our call, accepted
    assert inc["assignments"] == []                                    # no unit for it
    ev = events(rig, inc["id"])
    assert any(e.startswith("Port Chester FD paged") for e in ev) and any(e.startswith("Elsewhere FD paged") for e in ev)
    assert not any("not on our boards" in e for e in ev)
    assert inc["scope"]["label"] == "Full department (Port Chester FD, Elsewhere FD)"
    rig.at(90)
    rig.change(units=("DPTA", "DPTB", "E61"))                          # apparatus as they come
    assert {a["name"] for a in rig.inc()["assignments"]} == {"E61"}
    assert rig.inc()["scope"]["label"] == "Full department (Port Chester FD, Elsewhere FD)"
    assert sum(e.startswith("Port Chester FD paged") for e in events(rig, inc["id"])) == 1


def test_a_department_entry_is_never_a_unit_in_common(rig):
    depts(rig, DPTA="Port Chester FD")
    rig.mode("pcfd")
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPTA",), times={"dispatched": T0})
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("DPTA",), times={"dispatched": T0})
    rig.poll()
    a, b = rig.iid("A1"), rig.iid("B1")
    assert a != b
    assert not any("possibly the same" in e for e in events(rig, a) + events(rig, b))


def test_department_entries_are_a_hub_setting(rig):
    depts(rig, DPTA="Port Chester FD")
    assert rig.cad.sources.department_units() == {"DPTA": "Port Chester FD"}
    audit = rig.cad.settings.audit_log("dispatch")
    assert any(r["key"] == "department_units/DPTA" for r in audit)
    rig.cad.settings.post("department_units", {"action": "reset", "key": "DPTA", "operator": "Admin"})
    assert rig.cad.sources.department_units() == {}
