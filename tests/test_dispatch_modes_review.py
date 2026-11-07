"""Regressions from the independent review of #311 (PR #318): automation must never take a wrong live action. Synthetic
only: a scripted external CAD feed (tests/fake_feed.py), page tones and made-up radio lines, places and units."""

from __future__ import annotations

import time

import pytest

from sizeup.cad import comms as comms_mod
from sizeup.cad.radiolog import address_hit, addresses_in, place_hit, sent_elsewhere
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0
from tests.test_radios import tx


@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


# -- H1: never (re)assign; forward only against this incident's history ----------------------------------------------


def test_a_late_feed_time_never_puts_back_a_unit_the_radio_cleared(rig):
    rig.created(units=("E60", "L31"))
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})
    rig.radio("307-1-call_1-t0", T0 + 300, "Engine 60 back in service")
    assert rig.here("E60") is None and rig.status("E60") == "AV"
    rig.at(320)
    rig.change(unit_times={"E60": {"responding": T0 + 55, "on_location": T0 + 200}})     # entered late by the county
    assert rig.here("E60") is None and rig.status("E60") == "AV"
    assert [s for s in rig.steps() if s[0] == "assign" and s[2] == "E60"] == [("assign", "county", "E60", "DP", False)]


def test_a_unit_never_on_the_incident_is_not_placed_by_a_status_time(rig):
    rig.created(units=("E60",))
    rig.change(unit_times={"E62": {"responding": T0 + 30}})                       # not listed: a time alone places nothing
    assert rig.here("E62") is None and rig.status("E62") == "AV"


def test_a_person_accepting_a_hint_marks_the_call_the_unit_was_on(rig):
    rig.created(units=("E60",))
    rig.mode("pcfd")                                                               # the hint stays a suggestion
    rig.radio("307-h-call_h-t0", T0 + 40, "Engine 60 back in service")
    h = next(x for x in rig.cad.status_hints() if x["unit"] == "E60")
    rig.cad.hint(h["id"], "accept", {"operator": "Lt. Test"})
    assert rig.here("E60") is None and rig.cad.auto._manual(rig.iid(), "unit:E60")


# -- H2: no automatic close over a unit still on it; an undone close puts back what it released -------------------------


def test_no_automatic_close_over_a_unit_a_person_dispatched(rig):
    rig.created(units=("E60",))
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E62")], "operator": "Lt. Test"})
    rig.cad.unit_status(rig.uid("E62"), {"status": "OL", "operator": "Lt. Test"})
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    assert rig.inc()["status"] == "active" and rig.status("E62") == "OL"
    ask = rig.step("close?")
    ev = next(e for e in rig.inc()["events"] if e["id"] == ask["event_id"])
    assert ev["text"].startswith("County in service") and "close?" in ev["text"] and "E62" in ev["text"]
    rig.cad.close(rig.iid(), {"disposition": "COMPLETE", "operator": "Lt. Test"})  # the one click: a person's close
    assert rig.inc()["status"] == "closed"


def test_undoing_a_close_puts_back_the_units_it_released(rig):
    rig.created(units=("E60",))
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E62")], "operator": "Lt. Test"})
    rig.cad.unit_status(rig.uid("E62"), {"status": "CN", "operator": "Lt. Test"})  # cancelled: left Cancelled from it
    rig.at(900)
    rig.change(unit_times={"E60": {"in_service": T0 + 880}}, times={"in_service": T0 + 890})
    assert rig.inc()["status"] == "closed" and rig.status("E62") == "AV"
    rig.cad.auto_undo(rig.iid(), {"step": rig.step("close")["id"], "operator": "Lt. Test"})
    assert rig.inc()["status"] == "active" and rig.status("E62") == "CN"


# -- H3: auto-accept only our call, never a likely duplicate -----------------------------------------------------------


def test_a_feed_incident_with_none_of_our_units_and_no_page_stays_queued(rig):
    rig.created(units=("M7", "PD3"))
    assert rig.inc()["status"] == "queued" and rig.inc()["number"] is None
    rig.change(units=("M7", "PD3", "E60"))                                         # now one of ours: ours
    assert rig.inc()["status"] == "active" and rig.here("E60")


def test_a_page_makes_it_ours_without_units(rig):
    rig.created(units=())
    assert rig.inc()["status"] == "queued"
    rig.at(20)
    rig.page()
    rig.change(comments=((T0 + 25, "caller on the line"),))
    assert rig.inc()["status"] == "active" or rig.inc()["status"] == "pending"


def test_a_possible_duplicate_of_an_accepted_call_is_left_for_a_person(rig):
    rig.page()
    first = rig.incidents()[0]["id"]
    rig.cad.accept(first, {"operator": "Lt. Test"})
    rig.cad.dispatch(first, {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.at(30)
    rig.created()
    incs = rig.incidents()
    numbered = [i for i in incs if i["status"] in ("pending", "active") and i["number"]]
    assert len(numbered) == 1 and numbered[0]["id"] == first
    if rig.iid() != first:                                                         # its own (possibly the same): queued, said once
        assert rig.inc()["status"] == "queued" and rig.inc()["assignments"] == []
        rig.change(comments=((T0 + 40, "more"),))
        assert sum(1 for e in rig.inc()["events"] if e["text"].startswith("Not accepted automatically")) == 1
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    assert rig.cad.store.incident(first)["status"] == "active" and rig.status("E60") == "DP"


# -- H4: the radio places a unit only by its own words, with one call open ------------------------------------------


def test_the_window_counts_every_open_call(rig):
    rig.cad.create({"type": "STRU", "address": "400 Sample St", "operator": "Lt. Test"})   # a local call, not tracked
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    assert rig.cad.store.unit(rig.uid("E62"))["incident_id"] is None              # two open: which? neither


def test_an_unidentified_radio_never_places_a_unit(rig):
    rig.created(units=("E60",))
    rig.radio("307-4-call_4-t0", T0 + 40, "Engine 62 responding")                  # no talker, no ID
    assert rig.cad.store.unit(rig.uid("E62"))["incident_id"] is None


def test_the_fg1_guess_never_places_a_unit(rig):
    rig.created(units=("E60",))
    rig.cad.unit_status(rig.uid("E60"), {"status": "OL", "operator": "Lt. Test"})
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-x", T0 + 60, text="Engine 64 on location"))     # analog, no ID
    assert rig.cad.store.unit(rig.uid("E64"))["incident_id"] is None and rig.status("E64") == "AV"


# -- M1: exact unit codes only ------------------------------------------------------------------------------------------


def test_feed_codes_match_exactly_and_unknown_ones_are_listed_not_sent(rig):
    rig.created(units=("Engine 60", "RYE40", "LAR31", "60E"))
    names = {a["name"] for a in rig.inc()["assignments"]}
    assert names == {"E60"}                                                        # "Engine 60" is E60's spoken name
    assert any("not on our boards" in e["text"] and "RYE40" in e["text"] and "LAR31" in e["text"]
               for e in rig.inc()["events"])
    assert rig.status("R40") == "AV" and rig.status("L31") == "AV"


def test_feed_unit_aliases_from_the_data_file(rig, monkeypatch):
    monkeypatch.setattr(rig.cad.auto, "_aliases", lambda: {"LAR31": "L31"})
    rig.created(units=("LAR31",))
    assert {a["name"] for a in rig.inc()["assignments"]} == {"L31"}


# -- M2: the radio log's words --------------------------------------------------------------------------------------------


def test_radio_log_words():
    assert address_hit("12 North Street", "12 North St") and address_hit("12 Court Street", "12 Court St")
    assert not address_hit("main is open, spring water on the floor", "Spring St & Main St")
    assert address_hit("at Spring Street and Main Street", "Spring St & Main St")
    assert address_hit("Spring and Main", "Spring St & Main St")
    assert not place_hit("back at the fire house", "Fire House") and not place_hit("going to quarters", "Quarters")
    assert place_hit("units to the Example Mall loading dock", "Example Mall")
    assert addresses_in("Engine 60 respond to 400 Sample for an alarm") == ["400 sample"]
    assert addresses_in("Engine 60 to 60 Control, on location") == []
    inc = {"address": "12 Example Ave", "place_address": "", "place": ""}
    assert sent_elsewhere("Engine 60 respond to the Example Mall, second call", inc)
    assert sent_elsewhere("Engine 60 respond to 400 Sample", inc)
    assert not sent_elsewhere("Engine 60 respond to 12 Example Avenue", inc)
    assert not sent_elsewhere("Engine 60 responding with four", inc)


def test_a_unit_sent_elsewhere_without_a_suffix_is_not_logged(rig):
    rig.created()
    assert rig.radio("307-30-call_30-t0", T0 + 50, "Engine 60 respond to the Example Mall, second call") == []


def test_feed_units_off_the_boards_never_match_by_text(rig):
    rig.created(units=("E60", "PD"))
    assert rig.radio("307-31-call_31-t0", T0 + 50, "Tell pd the hydrant is out on Other Road") == []


def test_more_than_three_incidents_link_none(rig):
    for k, u in enumerate(("E60", "E61", "E62", "E63")):
        rig.feed.put(f"M{k}", type="ALARM/COMM", location=f"{100 + k} Example Ave", units=(u,), times={"dispatched": rig.clock.t})
    rig.poll()
    assert rig.radio("307-40-call_40-t0", T0 + 30, "Engine 60, Engine 61, Engine 62 and Engine 63, switch to channel 2") == []


def test_auto_links_keep_nothing_until_a_person_keeps_them(rig):
    rig.created()
    iid = rig.iid()
    rig.radio("307-50-call_50-t0", T0 + 30, "60 Control to Engine 60, copy your on location")
    assert rig.links(iid)["307-50-call_50-t0"][0] == "mention"
    assert "307-50-call_50-t0" not in rig.cad.comms.kept_for_events(["307-50-call_50-t0"])
    row = next(r for r in rig.inc()["radio"] if r["id"] == "307-50-call_50-t0")
    assert not row["link_kept"]
    rig.cad.keep_traffic(iid, {"traffic_id": "307-50-call_50-t0", "operator": "Lt. Test"})
    assert "307-50-call_50-t0" in rig.cad.comms.kept_for_events(["307-50-call_50-t0"])
    assert next(r for r in rig.inc()["radio"] if r["id"] == "307-50-call_50-t0")["link_kept"]
    rig.clock.t = T0 + comms_mod.KEEP_UNLINKED_S + 100                              # past the unlinked retention
    rig.radio("307-51-call_51-t0", T0 + 40, "Engine 60 on location")
    rig.cad.comms.prune()
    left = {r["id"] for r in rig.cad.store.q("SELECT id FROM radio_traffic")}
    assert "307-50-call_50-t0" in left


# -- M3: the backfill off the lock, read once -------------------------------------------------------------------------


def test_backfill_and_matching_stay_fast(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    for k in range(50):
        rig.feed.put(f"X{k}", type="ALARM/COMM", location=f"{100 + k} Example Ave", units=("E60", "L31", "M7", "PD3"),
                     times={"dispatched": rig.clock.t}, extra={"fireground": "FG1"})
    rig.poll()
    long = ("units operating " * 200) + " Engine 60 at 105 Example Avenue"
    for i in range(5):
        rig.clock.t = T0 + 10 + i
        rig.cad.record_traffic(tx(f"307-{i}-call_{i}-t0", T0 + 5 + i, source="Fire/EMS 15", tg=307, text=long))
    t0 = time.perf_counter()
    for i in range(5):
        rig.cad.radiolog.consider(f"307-{i}-call_{i}-t0")
    assert (time.perf_counter() - t0) / 5 < 0.5
    for i in range(100):
        rig.cad.record_traffic(tx(f"307-b{i}-call_b{i}-t0", T0 + 30 + i * 0.1, source="Fire/EMS 15", tg=307,
                                  text="routine traffic Engine 62"))
    rig.clock.t = T0 + 70
    t1 = time.perf_counter()
    rig.feed.put("NEW", type="ALARM/COMM", location="1 Sample St", units=("E62",), times={"dispatched": rig.clock.t})
    rig.poll()
    assert time.perf_counter() - t1 < 5


# -- L1, L2, L3 --------------------------------------------------------------------------------------------------------


def test_an_undone_page_stays_undone(rig):
    rig.created()
    rig.at(20)
    rig.page()
    rig.cad.auto_undo(rig.iid(), {"step": rig.step("paged")["id"], "operator": "Lt. Test"})
    rig.at(60)
    rig.change(comments=((T0 + 55, "x"),))
    assert [s for s in rig.steps() if s[0] == "paged"] == [("paged", "radio", "", "", True)]


def test_undo_refuses_after_a_person_changed_the_unit(rig):
    rig.created()
    a = rig.step("assign", "E60")
    rig.clock.t += 5
    rig.cad.unit_status(rig.uid("E60"), {"status": "DP", "operator": "Lt. Test"})
    with pytest.raises(Exception, match="changed by hand"):
        rig.cad.auto_undo(rig.iid(), {"step": a["id"], "operator": "Lt. Test"})


def test_the_switch_takes_the_lock(rig):
    with rig.cad.auto._lock:
        import threading
        out = []
        th = threading.Thread(target=lambda: out.append(rig.cad.dispatch_mode({"mode": "pcfd", "operator": "x"})))
        th.start()
        th.join(0.3)
        assert th.is_alive() and not out                                           # waits for automation in progress
    th.join(2)
    assert out and rig.cad.auto.mode() == "pcfd"
