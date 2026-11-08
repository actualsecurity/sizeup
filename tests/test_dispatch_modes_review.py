"""Regressions from the independent review of #311 (PR #318): automation must never take a wrong live action. Synthetic
only: a scripted external CAD feed (tests/fake_feed.py), page tones and made-up radio lines, places and units."""

from __future__ import annotations

import json
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


# -- round 2 ----------------------------------------------------------------------------------------------------------


def _times(**kw):
    return {"dispatched": T0, **kw}


def test_incident_times_only_close_the_call_and_free_its_units_for_the_next(rig):
    rig.created(units=("E60", "L31"))                                              # incident-level times only
    rig.at(60)
    rig.change(times=_times(responding=T0 + 50))
    rig.at(240)
    rig.change(times=_times(responding=T0 + 50, on_location=T0 + 230))
    rig.at(900)
    rig.change(times=_times(responding=T0 + 50, on_location=T0 + 230, in_service=T0 + 890))
    a = rig.inc()
    assert a["status"] == "closed" and rig.status("E60") == "AV" and rig.status("L31") == "AV"
    rig.at(3300)
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("E60", "L31"), times={"dispatched": rig.clock.t})
    rig.poll()
    b = rig.inc("B1")
    assert b["status"] == "active" and {x["name"] for x in b["assignments"] if not x["cleared_at"]} == {"E60", "L31"}


def test_a_unit_the_feed_doesnt_list_holds_the_close_until_the_radio_clears_it(rig):
    rig.created(units=("E60",))
    rig.radio("307-r1-call_r1-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")   # placed by its own radio
    assert rig.here("E62")
    rig.at(900)
    rig.change(times=_times(in_service=T0 + 890))
    inc = rig.inc()
    assert inc["status"] == "active" and rig.here("E60") is None and rig.here("E62")
    assert inc["auto"]["close_pending"] and "E62" in inc["auto"]["close_pending"]["text"]
    rig.radio("307-r2-call_r2-t0", T0 + 905, "Engine 62 back in service", rig="E62", unit="E62")
    assert rig.inc()["status"] == "closed" and rig.inc()["auto"]["close_pending"] is None


def test_a_person_clearing_the_last_unit_lets_the_pending_close_go(rig):
    rig.created(units=("E60",))
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E62")], "operator": "Lt. Test"})
    rig.at(900)
    rig.change(times=_times(in_service=T0 + 890))
    assert rig.inc()["status"] == "active" and rig.here("E62")
    rig.cad.unit_status(rig.uid("E62"), {"status": "AV", "operator": "Lt. Test"})  # the board
    assert rig.inc()["status"] == "closed"


def test_nothing_reported_in_pcfd_mode_replays_after_a_person_accepts(rig):
    rig.created(units=("E60", "L31"), type="ZZZ UNKNOWN THING")
    rig.mode("pcfd")
    rig.cad.accept(rig.iid(), {"type": "ALARM", "subtype": "COMM", "operator": "Lt. Test"})
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.at(60)
    rig.change(unit_times={"L31": {"responding": T0 + 55, "on_location": T0 + 58}})
    rig.mode("60control")
    rig.at(70)
    rig.change(comments=((T0 + 65, "nothing for units"),))
    assert rig.status("L31") == "AV" and rig.on("L31") is None and not [s for s in rig.steps() if s[2] == "L31"]


def test_an_unknown_radio_id_never_places_a_unit(rig):
    rig.created(units=("E60",))
    rig.radio("307-u-call_u-t0", T0 + 40, "Engine 62 responding", radio_id="99999")  # an ID nobody named
    assert rig.cad.store.unit(rig.uid("E62"))["incident_id"] is None


def test_possibly_the_same_is_a_relation_not_text(rig):
    rig.page()
    first = rig.incidents()[0]["id"]
    rig.cad.accept(first, {"operator": "Lt. Test"})
    rig.cad.dispatch(first, {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.at(30)
    rig.created()
    if rig.iid() != first:
        assert rig.cad.store.one("SELECT 1 AS x FROM auto_possibly WHERE incident_id = ? AND other_id = ?", (rig.iid(), first))
        with rig.cad.store.tx() as db:                                             # the note's words don't matter
            db.execute("UPDATE events SET text = 'x' WHERE text LIKE 'Possibly the same as%'")
        assert rig.cad.auto._possibly_open(rig.iid())["id"] == first


def test_keep_is_on_the_timeline_undoable_and_gone_with_the_link(rig):
    rig.created()
    iid = rig.iid()
    rig.radio("307-k-call_k-t0", T0 + 30, "60 Control to Engine 60, copy your on location")
    rig.cad.keep_traffic(iid, {"traffic_id": "307-k-call_k-t0", "operator": "Lt. Test"})
    assert any(e["text"].endswith("kept with this event") for e in rig.inc()["events"])
    rig.cad.keep_traffic(iid, {"traffic_id": "307-k-call_k-t0", "undo": True, "operator": "Lt. Test"})
    assert any(e["text"].endswith("no longer kept") for e in rig.inc()["events"])
    assert "307-k-call_k-t0" not in rig.cad.comms.kept_for_events(["307-k-call_k-t0"])
    rig.cad.keep_traffic(iid, {"traffic_id": "307-k-call_k-t0", "operator": "Lt. Test"})
    rig.cad.unlink_traffic(iid, {"traffic_id": "307-k-call_k-t0", "operator": "Lt. Test"})
    assert rig.cad.store.q("SELECT * FROM radio_link_kept") == []


def test_more_common_places_never_count():
    for p in ("Post Office", "The Fire Department", "Train Station", "Parking Garage", "Nursing Home", "Car Wash",
              "Apartment Complex", "Elementary School", "Shopping Center"):
        assert not place_hit(f"units at the {p.lower()}", p), p


# -- round 3 ----------------------------------------------------------------------------------------------------------


def test_one_undo_of_a_mistaken_in_service_puts_the_call_back(rig):
    rig.created(units=("E60", "L31"))
    rig.at(240)
    rig.change(unit_times={"E60": {"on_location": T0 + 230}, "L31": {"responding": T0 + 200}})
    assert rig.status("E60") == "OL" and rig.status("L31") == "RS"
    rig.at(300)
    rig.change(unit_times={"E60": {"on_location": T0 + 230}, "L31": {"responding": T0 + 200}},
               times=_times(in_service=T0 + 295))                                  # by mistake
    assert rig.inc()["status"] == "closed" and rig.status("E60") == "AV" and rig.status("L31") == "AV"
    rig.cad.auto_undo(rig.iid(), {"step": rig.step("close")["id"], "operator": "Lt. Test"})   # one undo
    inc = rig.inc()
    assert inc["status"] == "active" and rig.here("E60") and rig.here("L31")
    assert rig.status("E60") == "OL" and rig.status("L31") == "RS"                # as before, not Dispatched
    assert rig.here("E60")["onscene_at"] == T0 + 230                               # its times kept
    rel = [s for s in inc["auto"]["steps"] if s["kind"] == "status" and s["status"] == "AV"]
    assert len(rel) == 2 and all(s["undone"] for s in rel)


def test_undoing_a_release_on_a_closed_call_says_undo_the_close_first(rig):
    rig.created(units=("E60", "L31"))
    rig.at(300)
    rig.change(times=_times(in_service=T0 + 295))
    assert rig.inc()["status"] == "closed"
    s = next(x for x in rig.inc()["auto"]["steps"] if x["kind"] == "status" and x["unit"] == "E60")
    with pytest.raises(Exception, match="undo the close first"):
        rig.cad.auto_undo(rig.iid(), {"step": s["id"], "operator": "Lt. Test"})
    rig.cad.auto_undo(rig.iid(), {"step": rig.step("close")["id"], "operator": "Lt. Test"})
    assert rig.here("E60") and rig.here("L31") and rig.status("E60") == "DP"


def test_moving_the_last_unit_to_another_call_lets_the_pending_close_go(rig):
    rig.created(units=("E60",))
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E62")], "operator": "Lt. Test"})
    rig.at(300)
    rig.change(times=_times(in_service=T0 + 295))
    assert rig.inc()["status"] == "active" and rig.inc()["auto"]["close_pending"]
    other = rig.cad.create({"type": "STRU", "address": "400 Sample St", "operator": "Lt. Test"})
    rig.cad.dispatch(other["id"], {"unit_ids": [rig.uid("E62")], "operator": "Lt. Test"})
    assert rig.inc()["status"] == "closed"


# -- the fourth review ---------------------------------------------------------------------------------------------------


def test_an_incident_already_accepted_at_the_feeds_first_report_stays_manual(rig):
    """M1: a page a person accepted (or a call from before this code) when the feed's first report reaches automation:
    a baseline only, never tracked, nothing dispatched or replayed."""
    page = rig.page()
    rig.cad.accept(page["id"], {"operator": "Lt. Test"})
    rig.at(40)
    rig.feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60", "L31"),
                 times={"dispatched": T0 + 30, "responding": T0 + 35})
    rig.poll()
    assert rig.iid() == page["id"]                                    # attached to the page's call (#304)
    row = rig.cad.store.one("SELECT * FROM auto_incidents WHERE incident_id = ?", (page["id"],))
    assert row["tracked_at"] is None and json.loads(row["seen"])["units"] == ["E60", "L31"] and row["seen_src"] == "county"
    assert rig.inc()["assignments"] == [] and rig.steps() == []
    rig.at(80)
    rig.change(units=("E60", "L31", "E62"), times={"on_location": T0 + 75})
    assert rig.inc()["assignments"] == [] and rig.steps() == []        # manual for good


def test_only_the_units_own_rig_puts_it_on_a_call_never_60_control(rig):
    """L1: a "responding" for a unit on no call places it only when said on that unit's own identified rig."""
    rig.created(units=("E60",))
    with rig.cad.store.tx() as db:                                    # a dispatcher's alias someone put in the registry
        db.execute("INSERT INTO radios (system, radio_id, unit, label, source, alias, updated_at) "
                   "VALUES ('p25', '9999001', '', '', 'confirmed', '60 CTRL', ?)", (T0,))
    rig.radio("307-5-call_5-t0", T0 + 40, "Engine 62 responding", unit="60 CTRL", radio_id="9999001")
    assert rig.cad.store.unit(rig.uid("E62"))["incident_id"] is None              # 60 Control saying it: never
    rig.radio("307-6-call_6-t0", T0 + 45, "Engine 64 responding", rig="E60", unit="E60")
    assert rig.cad.store.unit(rig.uid("E64"))["incident_id"] is None              # another rig saying it: never
    rig.radio("307-7-call_7-t0", T0 + 50, "Ladder 31 responding", rig="L31", unit="L31")
    assert rig.on("L31") is not None                                               # its own rig: placed


def test_another_feed_ruling_the_incident_is_a_new_baseline(rig):
    """L2: the best-ranked feed's report changes hands: never diffed against the other feed's report."""
    from tests.fake_feed import FakeFeed
    mirror = FakeFeed("mirror", "Mirror feed", rank=2, clock=rig.clock)
    rig.feeds.append(mirror)
    rig.runner = type(rig.runner)(rig.feeds, rig.sink, log=lambda m: None, clock=rig.clock)
    mirror.put("M1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    iid = rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'M1'")["incident_id"]
    assert rig.cad.store.incident(iid)["status"] in ("pending", "active")
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60", "L31", "E62"),
                 times={"dispatched": T0 + 20, "responding": T0 + 25})
    rig.poll()
    assert rig.iid("A1") == iid
    row = rig.cad.store.one("SELECT seen, seen_src FROM auto_incidents WHERE incident_id = ?", (iid,))
    assert row["seen_src"] == "county" and json.loads(row["seen"])["units"] == ["E60", "L31", "E62"]
    here = {a["name"]: a for a in rig.cad.full(iid)["assignments"] if not a["cleared_at"]}
    assert {"E60", "L31", "E62"} <= set(here)                           # the new ruler's units: dispatched
    assert here["L31"]["enroute_at"] is None                             # its times: the baseline, not replayed
    rig.at(60)
    rig.feed.change("A1", units=("E60", "L31", "E62", "E64"))
    rig.poll()
    assert "E64" in {a["name"] for a in rig.cad.full(iid)["assignments"] if not a["cleared_at"]}


def _two_feeds(rig):
    from tests.fake_feed import FakeFeed
    mirror = FakeFeed("mirror", "Mirror feed", rank=2, clock=rig.clock)
    rig.feeds.append(mirror)
    rig.runner = type(rig.runner)(rig.feeds, rig.sink, log=lambda m: None, clock=rig.clock)
    return mirror


def test_the_primary_recovering_after_the_call_ended_asks_close_never_closes(rig):
    """Seventh review (A): the mirror ran the call; the primary comes back with that same call already in service (first
    seen closed). It may join (the same call) and record its times, but never releases a unit or closes: "close?"."""
    mirror = _two_feeds(rig)
    rig.feed.outage("failing", "x", raise_=True)
    mirror.put("M1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    iid = rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'M1'")["incident_id"]
    rig.at(1200)
    rig.feed.recover()
    rig.feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), closed=True,
                 times={"dispatched": T0, "on_location": T0 + 300, "in_service": T0 + 1100})
    rig.poll()
    assert rig.iid("A1") == iid
    inc = rig.cad.full(iid)
    assert inc["status"] in ("pending", "active") and any(a["name"] == "E60" and not a["cleared_at"] for a in inc["assignments"])
    assert inc["auto"]["close_pending"] and inc["auto"]["close_pending"]["text"].endswith(": close?")
    rig.cad.unit_status(rig.uid("E60"), {"status": "AV", "operator": "Lt. Test"})
    assert rig.cad.store.incident(iid)["status"] in ("pending", "active")    # the last unit off: still a person's close


def test_an_earlier_closed_call_at_the_address_never_closes_the_live_one(rig):
    """Sixth review: the primary comes back and lists an EMS call at the same address that ended before the live fire
    call began: it never joins, rules or closes the live call."""
    mirror = _two_feeds(rig)
    rig.feed.outage("failing", "x", raise_=True)
    rig.at(600)
    mirror.put("M1", type="ALARM/COMM", location="12 Example Ave", units=("E60", "L31"), times={"dispatched": T0 + 600})
    rig.poll()
    iid = rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'M1'")["incident_id"]
    assert rig.cad.store.incident(iid)["status"] in ("pending", "active")
    rig.at(900)
    rig.feed.recover()
    rig.feed.put("OLD1", type="EMS", location="12 EXAMPLE AVE", units=("E60",), closed=True,
                 times={"dispatched": T0, "in_service": T0 + 500})
    rig.poll()
    inc = rig.cad.full(iid)
    assert inc["status"] in ("pending", "active") and any(a["name"] == "E60" and not a["cleared_at"] for a in inc["assignments"])
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'OLD1'")["incident_id"] != iid


def test_an_earlier_closed_call_never_takes_a_queued_page(rig):
    """The single-feed variant: our page waits queued; the feed (back from an outage) lists an earlier ended call at the
    address: the page's incident stays open and untouched."""
    rig.feed.outage("failing", "x", raise_=True)
    rig.poll()
    rig.at(600)
    page = rig.page()
    rig.at(700)
    rig.feed.recover()
    rig.feed.put("OLD1", type="EMS", location="12 EXAMPLE AVE", units=("E60",), closed=True,
                 times={"dispatched": T0, "in_service": T0 + 500})
    rig.poll()
    inc = rig.cad.store.incident(page["id"])
    assert inc["status"] == "queued" and rig.cad.store.one(
        "SELECT 1 AS x FROM auto_incidents WHERE incident_id = ?", (page["id"],)) is None


def test_a_new_ruler_on_a_queued_incident_is_its_first_report(rig):
    """The mirror's call is none of ours (queued); the primary attaches listing E60: accepted and dispatched as a first
    report would be, and automation's start recorded."""
    mirror = _two_feeds(rig)
    mirror.put("M1", type="ALARM/COMM", location="12 Example Ave", units=("Z99",), times={"dispatched": T0})
    rig.poll()
    iid = rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'M1'")["incident_id"]
    assert rig.cad.store.incident(iid)["status"] == "queued"
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0 + 10})
    rig.poll()
    assert rig.iid("A1") == iid
    assert rig.cad.store.incident(iid)["status"] in ("pending", "active")
    assert any(a["name"] == "E60" and not a["cleared_at"] for a in rig.cad.full(iid)["assignments"])
    row = rig.cad.store.one("SELECT started_at, seen_src FROM auto_incidents WHERE incident_id = ?", (iid,))
    assert row["started_at"] is not None and row["seen_src"] == "county"


# -- the seventh review: the live call stays open --------------------------------------------------------------------

PAGE_E60 = ("Port Chester Fire Department, Engine 60, respond to an activated fire alarm. 12 Example Avenue, "
            "cross of Sample Street. Time out 1442.")


def _open(rig, iid):
    return rig.cad.store.incident(iid)["status"] in ("queued", "pending", "active")


def test_probe_1_a_closed_event_from_before_the_page_leaves_the_live_call_open(rig):
    rig.feed.outage("failing", "x", raise_=True)
    rig.poll()
    rig.at(600)
    page = rig.page()
    rig.at(1000)
    rig.feed.recover()
    rig.feed.put("C1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), closed=True,
                 times={"dispatched": T0 + 400, "in_service": T0 + 900})
    rig.poll()
    assert _open(rig, page["id"])


def test_probe_2_the_first_alarm_s_close_leaves_the_second_call_open(rig):
    rig.feed.put("A1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), times={"dispatched": T0})
    rig.poll()
    a1 = rig.iid("A1")
    rig.feed.outage("failing", "x", raise_=True)
    rig.at(900)
    rig.poll()
    p = rig.page(fresh=True)
    assert p["id"] != a1
    rig.at(2400)
    rig.feed.recover()
    rig.feed.put("A2", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), times={"dispatched": T0 + 890})
    rig.feed.change("A1", closed=True, times={"in_service": T0 + 2000})
    rig.poll()
    assert rig.cad.store.incident(a1)["status"] == "closed"            # A1 was seen open: its close stands
    assert _open(rig, p["id"]) and rig.iid("A2") == p["id"]


def test_probe_3_two_calls_at_one_address_the_earlier_s_close_leaves_the_later_open(rig):
    rig.feed.outage("failing", "x", raise_=True)
    rig.poll()
    first = rig.page()
    rig.at(700)
    second = rig.page("p2", PAGE_E60, fresh=True)
    rig.at(1500)
    rig.feed.recover()
    rig.feed.put("B1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), closed=True,
                 times={"dispatched": T0 - 5, "in_service": T0 + 1400})
    rig.feed.put("B2", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60",), times={"dispatched": T0 + 695})
    rig.poll()
    assert rig.iid("B2") == second["id"] and _open(rig, second["id"])
    assert _open(rig, first["id"])                                      # B1 first seen closed: never closes it


def test_a_report_first_seen_over_never_accepts_a_queued_page_or_dispatches(rig):
    """Eighth review: a report first seen closed joins the queued page's incident (the same call) but never accepts it
    or dispatches its units: recorded, and a person decides."""
    rig.feed.outage("failing", "x", raise_=True)
    rig.poll()
    rig.at(600)
    page = rig.page()
    rig.at(2000)
    rig.feed.recover()
    rig.feed.put("Z1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), closed=True,
                 times={"dispatched": T0 + 590, "in_service": T0 + 1900})
    rig.poll()
    assert rig.iid("Z1") == page["id"]
    inc = rig.cad.full(page["id"])
    assert inc["status"] == "queued" and inc["assignments"] == []
    assert any("shows it over" in e["text"] for e in inc["events"])
