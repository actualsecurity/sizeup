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


def depts(rig, accepts=(), **codes) -> None:
    """The hub's own department entry codes (Settings, never the repo); accepts: those that accept a call alone."""
    for k, v in codes.items():
        rig.cad.settings.post("department_units", {"action": "set", "key": k, "value": v, "accepts": k in accepts,
                                                   "operator": "Admin"})


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
                                                 "short": "Full dept", "label": "Full department (Port Chester FD)",
                                                 "paged": "Paged: full department (Port Chester FD)", "source": "page"}
    rig.at(600)                                                        # two departments' tones on one page
    m = rig.monitor.on_page("p2", ["Port Chester FD", "Rye Brook FD (est.)"],
                            parse(FULL_PAGE.replace("12 Example Avenue", "400 Sample Street")), at=rig.clock.t)
    two = rig.cad.from_monitor(m)
    s = rig.cad.full(two["id"])["scope"]
    assert (s["short"], s["label"]) == ("Full dept", "Full department (Port Chester FD, Rye Brook FD)")
    queued = {i["id"]: i for i in rig.cad.state()["incidents"]}
    assert queued[inc["id"]]["scope"]["short"] == "Full dept"          # on the queue card too


def test_no_label_from_tones_without_a_call_type(rig):
    inc = rig.page(text="Port Chester Fire Department, 12 Example Avenue, cross of Sample Street. Time out 1432.")
    assert rig.cad.full(inc["id"])["scope"] is None


def test_specific_units_from_a_page_naming_apparatus(rig):
    inc = rig.page(text=PAGE)                                          # Engine 60, Ladder 31
    assert rig.cad.full(inc["id"])["scope"]["short"] == "Specific units"


def test_the_feed_overrides_the_page_and_the_disagreement_is_noted(rig):
    rig.mode("pcfd")
    inc = rig.page(text=FULL_PAGE)
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert rig.iid() == inc["id"]                                      # attached to the page's incident
    s = rig.cad.full(inc["id"])["scope"]
    assert (s["label"], s["source"]) == ("Specific units", "feed")
    assert "Feed: units · page: all PCFD" in events(rig, inc["id"])
    rig.at(60)
    rig.page(pid="p2", text=FULL_PAGE)                                 # the page again: the feed's stands
    assert rig.cad.full(inc["id"])["scope"]["label"] == "Specific units"
    tones = [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = 'tones'",
                                                (inc["id"],))]
    assert tones.count("Page: all PCFD · feed: units") == 1            # the repeat on the feed's call: its one line
    rig.at(90)
    rig.page(pid="p3", text=FULL_PAGE)                                 # and once only
    assert sum(e.startswith("Page: ") for e in events(rig, inc["id"])) == 1


def test_a_feed_department_entry_confirms_the_page(rig):
    depts(rig, DPTA="Port Chester FD")
    rig.mode("pcfd")
    inc = rig.page(text=FULL_PAGE)
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPTA",), times={"dispatched": T0})
    rig.poll()
    s = rig.cad.full(inc["id"])["scope"]
    assert (s["label"], s["source"]) == ("Full department (Port Chester FD)", "feed")
    assert not any("· page:" in e for e in events(rig, inc["id"]))     # agreed: nothing to note


def test_a_page_heard_after_the_feed_never_changes_its_label(rig):
    rig.mode("pcfd")
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("E60",), times={"dispatched": T0})
    rig.poll()
    rig.at(20)
    rig.page(text=FULL_PAGE)
    iid = rig.iid()
    assert rig.cad.full(iid)["scope"]["label"] == "Specific units"
    assert "Page: all PCFD · feed: units" in events(rig, iid)


# -- a department entry in 60 Control mode ------------------------------------------------------------------------------


def test_a_department_entry_is_never_dispatched(rig, monkeypatch):
    depts(rig, accepts=("DPTA",), DPTA="Port Chester FD", DPTB="Elsewhere FD")
    monkeypatch.setattr(rig.cad.auto, "_aliases", lambda: {"DPTA": "E60"})   # even a code also aliased to a unit
    assert rig.cad.auto.mode() == "60control"
    rig.created(units=("DPTA", "DPTB"))
    inc = rig.inc()
    assert inc["status"] == "pending" and inc["number"]               # our department's entry set to accept
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


# -- review fixes: each unit's own window, removal, the caps, late transcripts --------------------------------------------


def test_an_add_on_unit_gets_only_its_own_radio_not_its_earlier_chatter(rig):
    rig.created(units=("E60",))                                        # dispatched at T0, accepted
    iid = rig.iid()
    for tid, at, text in (("307-1-call_1-t0", T0 + 600, "Engine 61 back in quarters"),
                          ("307-2-call_2-t0", T0 + 1200, "Engine 61 available"),
                          ("307-3-call_3-t0", T0 + 1300, "Engine 62 back in quarters"),
                          ("307-4-call_4-t0", T0 + 1460, "Engine 62 copy, en route"),
                          ("307-5-call_5-t0", T0 + 2390, "60 Control, Engine 61 copy")):
        rig.radio(tid, at, text)
    rig.at(2400)                                                       # 40 min in: added on
    rig.change(units=("E60", "E61", "E62"), unit_times={"E62": {"dispatched": T0 + 1500}})
    links = rig.links(iid)
    assert "307-5-call_5-t0" in links and "307-4-call_4-t0" in links   # its own dispatch less a minute on
    assert not {"307-1-call_1-t0", "307-2-call_2-t0", "307-3-call_3-t0"} & set(links)
    since = {r["unit"]: r["since"] for r in rig.cad.store.q("SELECT * FROM radiolog_units WHERE incident_id = ?", (iid,))}
    assert since["E61"] == T0 + 2400 - 60 and since["E62"] == T0 + 1500 - 60 and since["E60"] == T0 - 60


def test_the_cap_keeps_the_newest_on_our_channels(rig, monkeypatch):
    import sizeup.cad.radiolog as rl
    monkeypatch.setattr(rl, "BACKFILL_MAX", 3)
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=(), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    for k in range(4):                                                 # older traffic on our channel
        rig.radio(f"307-{k}-call_{k}-t0", T0 + k, "nothing to say")
    rig.radio("307-9-call_9-t0", T0 + 60, "Engine 63 responding")
    for k in range(6):                                                 # newer, on someone else's channel
        rig.radio(f"999-{k}-call_{k}-t0", T0 + 70 + k, "words", tg=999, source="PD 1")
    rig.at(120)
    rig.change(units=("E63",))
    assert "307-9-call_9-t0" in rig.links(iid)


def test_a_unit_is_marked_done_only_once_its_links_are_made(rig, monkeypatch):
    rig.mode("pcfd")
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=(), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    rig.radio("307-1-call_1-t0", T0 + 20, "Engine 63 responding")
    real = rig.cad.radiolog._link
    monkeypatch.setattr(rig.cad.radiolog, "_link", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db busy")))
    rig.at(60)
    rig.change(units=("E63",))
    assert rig.cad.store.q("SELECT * FROM radiolog_units WHERE incident_id = ?", (iid,)) == []
    monkeypatch.setattr(rig.cad.radiolog, "_link", real)
    rig.cad.radiolog.units_soon(iid)                                   # tried again
    assert "307-1-call_1-t0" in rig.links(iid)


def test_an_undone_dispatch_takes_its_unkept_mentions_off(rig):
    rig.created(units=("E60",))
    iid = rig.iid()
    rig.radio("307-1-call_1-t0", T0 + 20, "60 Control to Engine 60, copy")
    rig.radio("307-2-call_2-t0", T0 + 30, "Engine 60, copy that")
    rig.cad.radiolog.keep("307-2-call_2-t0", iid, "Lt. Test")         # a person kept this one
    assert {"307-1-call_1-t0", "307-2-call_2-t0"} <= set(rig.links(iid))
    step = rig.step("assign", "E60")
    rig.cad.auto_undo(iid, {"step": step["id"], "operator": "Lt. Test"})
    assert set(rig.links(iid)) == {"307-2-call_2-t0"}
    assert rig.cad.store.q("SELECT * FROM radiolog_units WHERE incident_id = ? AND unit = 'E60'", (iid,)) == []


def test_a_unit_cleared_before_it_went_or_dropped_by_the_feed_loses_its_mentions(rig):
    rig.mode("pcfd")
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})
    rig.cad.dispatch(a["id"], {"unit_ids": [rig.uid("E60"), rig.uid("E61")], "operator": "Lt. Test"})
    rig.radio("307-1-call_1-t0", T0 + 20, "60 Control to Engine 60, copy")
    rig.radio("307-2-call_2-t0", T0 + 25, "60 Control to Engine 61, copy")
    rig.cad.unit_status(rig.uid("E61"), {"status": "RS", "operator": "Lt. Test"})       # E61 went ...
    rig.cad.unit_status(rig.uid("E61"), {"status": "AV", "operator": "Lt. Test"})       # ... and cleared: kept
    rig.cad.unit_status(rig.uid("E60"), {"status": "AV", "operator": "Lt. Test"})       # E60 never went: off
    assert set(rig.links(a["id"])) == {"307-2-call_2-t0"}

    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("E62",), times={"dispatched": rig.clock.t})
    rig.poll()
    iid = rig.iid()
    rig.radio("307-3-call_3-t0", rig.clock.t + 5, "Engine 62, copy")
    assert "307-3-call_3-t0" in rig.links(iid)
    rig.change(units=())                                               # the feed drops it
    assert "307-3-call_3-t0" not in rig.links(iid)


def test_link_caps_hold_across_every_path(rig):
    rig.mode("pcfd")
    incs = [rig.cad.create({"type": "STRU", "address": f"{n} Other Rd", "operator": "Lt. Test"})["id"] for n in (40, 41, 42, 43)]
    rig.radio("307-1-call_1-t0", T0 + 20, "nothing to say")
    log = rig.cad.radiolog
    assert log._link("307-1-call_1-t0", {incs[0]: ("mention", "E60")}) == [incs[0]]
    assert log._link("307-1-call_1-t0", {incs[1]: ("mention", "E60")}) == []         # one unit mention, one call
    assert log._link("307-1-call_1-t0", {incs[1]: ("mention", "address"), incs[2]: ("mention", "place")}) == incs[1:3]
    assert log._link("307-1-call_1-t0", {incs[3]: ("mention", "E61")}) == []          # a fourth: never
    assert len(rig.cad.store.q("SELECT * FROM radio_links WHERE traffic_id = '307-1-call_1-t0'")) == 3


def test_a_late_transcript_links_from_the_units_window(rig):
    rig.mode("pcfd")
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=(), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    rig.radio("307-1-call_1-t0", T0 + 20, "")                          # its words come later
    rig.at(120)
    rig.cad.accept(iid, {"operator": "Lt. Test"})
    rig.cad.dispatch(iid, {"unit_ids": [rig.uid("E63")], "operator": "Lt. Test"})
    rig.cad.comms.retext("307-1-call_1-t0", "Engine 63 responding", "done")
    assert rig.links(iid)["307-1-call_1-t0"] == ("mention", "E63")


def test_a_dismissed_call_still_owns_its_units(rig):
    rig.mode("pcfd")
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("E63",), times={"dispatched": T0})
    rig.poll()
    b = rig.iid("B1")
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})
    rig.cad.dispatch(a["id"], {"unit_ids": [rig.uid("E63")], "operator": "Lt. Test"})
    assert rig.radio("307-1-call_1-t0", T0 + 20, "60 Control to Engine 63, copy") == []   # which call? neither
    rig.at(60)
    rig.cad.dismiss(b, {"operator": "Lt. Test", "reason": "not ours"})
    with rig.cad.store.tx() as db:
        db.execute("DELETE FROM radiolog_units")
    rig.cad.radiolog.units_soon(a["id"])                               # matched again after: B still owned it then
    assert "307-1-call_1-t0" not in rig.links(a["id"]) and "307-1-call_1-t0" not in rig.links(b)


def test_restart_picks_the_work_up_again_and_stop_ends_it(rig):
    rig.mode("pcfd")
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("E63",), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    rig.radio("307-1-call_1-t0", T0 + 20, "Engine 63 responding")
    with rig.cad.store.tx() as db:                                     # as if it went down before the work was done
        db.execute("DELETE FROM radio_links")
        db.execute("DELETE FROM radiolog_units")
    rig.cad.radiolog.start()
    assert "307-1-call_1-t0" in rig.links(iid)
    rig.cad.radiolog.stop()
    with rig.cad.store.tx() as db:
        db.execute("DELETE FROM radiolog_units")
    rig.cad.radiolog.units_soon(iid)
    assert rig.cad.store.q("SELECT * FROM radiolog_units") == []


# -- review fixes: department entries ----------------------------------------------------------------------------------


@pytest.mark.parametrize("dept, accepts, taken", [("Port Chester FD", False, False), ("Port Chester FD", True, True),
                                                  ("Rye Brook FD", False, False), ("Rye Brook FD", True, True),
                                                  ("Elsewhere FD", True, False)])
def test_a_department_entry_alone_accepts_only_when_set_to_and_ours(rig, dept, accepts, taken):
    depts(rig, accepts=("DPTA",) if accepts else (), DPTA=dept)
    rig.created(units=("DPTA",))
    assert (rig.inc()["status"] != "queued") is taken


def test_a_department_entry_and_a_page_accept(rig):
    depts(rig, DPTA="Port Chester FD")
    rig.created(units=("DPTA",))
    assert rig.inc()["status"] == "queued"
    rig.at(20)
    rig.page(text=FULL_PAGE)                                           # our tones heard
    rig.change(comments=((T0 + 25, "units requested"),))
    assert rig.inc()["status"] == "pending"


def test_a_department_entry_is_never_a_board_units_code(rig):
    from sizeup.cad.store import CadError
    with pytest.raises(CadError, match="unit E60"):
        depts(rig, **{"e-60": "Port Chester FD"})


def test_the_one_unit_rule_counts_apparatus_only(rig):
    depts(rig, accepts=("DPTA",), DPTA="Port Chester FD")
    rig.created(units=("DPTA", "E60"))
    rig.at(60)
    rig.change(times={"dispatched": T0, "responding": T0 + 55})
    assert rig.status("E60") == "RS"


# -- with #365's street-only pages ------------------------------------------------------------------------------------


def test_a_street_only_page_attaching_to_a_feed_incident_gets_its_label_in_one_line(tmp_path):
    from tests.test_street_only import PAGE_NO_UNITS, feed_event, lines
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    a = feed_event(rig, units=("E60",))                                # the feed: specific units
    rig.at(40)
    assert rig.page("p1", PAGE_NO_UNITS)["id"] == a                    # street only: joins it
    s = rig.cad.full(a)["scope"]
    assert (s["short"], s["source"]) == ("Specific units", "feed")              # the feed's stands
    tones = lines(rig, a, "tones")
    assert "Page: all PCFD · feed: units" in tones                     # one line for who the page paged
    assert not any(x.startswith(("Tones:", "Units named")) for x in tones)
    assert not any("· feed:" in e or "· page:" in e for e in events(rig, a) if e not in tones)
    rig.at(70)
    rig.page("p2", PAGE_NO_UNITS)                                      # a repeat: nothing new
    assert sum(x.startswith("Page: ") for x in lines(rig, a, "tones")) == 1


def test_department_entries_never_block_a_street_only_join(tmp_path):
    from tests.test_street_only import PAGE_ST, feed_event, open_incidents
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    depts(rig, DPTA="Port Chester FD")
    a = feed_event(rig, units=("DPTA",))                               # only the department entry listed
    rig.at(30)
    assert rig.page("p1", PAGE_ST)["id"] == a and len(open_incidents(rig)) == 1   # the page names Engine 60, Ladder 31
    assert rig.cad.full(a)["scope"]["short"] == "Full dept"             # the feed's department entry stands


def test_a_page_card_moved_onto_the_feeds_incident_takes_its_reading(tmp_path):
    from tests.test_street_only import PAGE_NO_UNITS, feed_event
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    a = feed_event(rig, units=())                                      # the feed lists nobody yet: no label
    rig.at(20)
    first = rig.page("p1", "Port Chester Fire Department, respond to a CO alarm. Time out 1432.")["id"]
    assert first != a and rig.cad.full(first)["scope"]["short"] == "Full dept"
    rig.at(50)
    assert rig.page("p2", PAGE_NO_UNITS)["id"] == a                    # rematch_page: the card moves onto it
    assert rig.cad.full(a)["scope"]["short"] == "Full dept"


def test_a_swept_page_card_takes_its_reading_along(tmp_path):
    from tests.test_incident_sources import Rig
    from tests.test_street_only import feed_event
    rig = Rig(tmp_path)
    a = feed_event(rig, location="17 EXAMPLE LN", units=(), typ="ALARM/RESD")
    p = rig.cad.store.create_incident(type="ALARM", address="1717 Example Lane", source="60 Control page",
                                      source_ref="monitor:old", status="queued")
    rig.cad.sources._put("paging", "old", 1000, "Paging", T0 + 30, {"location": "1717 Example Lane"}, incident_id=p)
    rig.cad.scope._reading(p, "full", ["Port Chester FD"], line="Page: all PCFD")
    rig.at(600)
    rig.feed.change("A1", times={"in_service": T0 + 590}, closed=True)
    rig.poll()
    assert rig.cad.store.incident(p)["status"] == "dismissed"
    assert rig.cad.full(a)["scope"]["short"] == "Full dept"
    assert tone_lines(rig, a).count("Page: all PCFD") == 1             # its line moved along, kind tones


def tone_lines(rig, iid: int) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = 'tones' ORDER BY id",
                                               (iid,))]


def test_a_page_card_moved_onto_the_feeds_call_writes_its_one_line(tmp_path):
    from tests.test_street_only import PAGE_ST, feed_event
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    a = feed_event(rig, units=("E62",))                                # 9 Example Ter, E62
    rig.at(20)
    first = rig.page("p1", "Port Chester Fire Department, Engine 60, Ladder 31, respond to a CO alarm. Time out 1432.")["id"]
    assert first != a                                                  # no street: its own card
    rig.at(50)
    assert rig.page("p2", PAGE_ST)["id"] == a                          # the repeat names the street: moved on
    page = [x for x in tone_lines(rig, a) if x.startswith("Page: ")]
    assert page == ["Page: PCFD · units Engine 60, Ladder 31"]         # exactly one, kind tones (the feed agrees)
    assert not any(e.startswith("Page: ") for e in events(rig, a) if e not in tone_lines(rig, a))
    rig.at(70)
    rig.page("p3", PAGE_ST)                                            # a repeat: no second line
    assert sum(x.startswith("Page: ") for x in tone_lines(rig, a)) == 1


def test_the_attach_path_writes_exactly_one_line(tmp_path):
    from tests.test_street_only import PAGE_ST, feed_event
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    a = feed_event(rig, units=("E60", "L31"))
    rig.at(30)
    assert rig.page("p1", PAGE_ST)["id"] == a
    rig.at(60)
    rig.page("p2", PAGE_ST)
    assert [x for x in tone_lines(rig, a) if x.startswith("Page: ")] == ["Page: PCFD · units Engine 60, Ladder 31"]
