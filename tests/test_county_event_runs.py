"""One county event, one incident (sizeup/cad/sources.py "same event" and "highway"): another run of an event (the
adapter's "<id>-2", or the same "event_number") updates the incident the first run made; a feed's merge of two events
closes the automatic duplicate; a page and the feed on a highway match by mile marker in either order; a page that says
no place stays unlocated, never its own words as an address. Invented data only: made-up event ids, roads, agencies,
units and times."""

from __future__ import annotations

from sizeup.cad.sources import event_number, same_place
from sizeup.dispatch import highway, parse
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0, Rig

PKWY = "SAMPLE PKWY NB MM 3.1 TOWNVILLE"
PKWY_LATER = "@SAMPLE PKWY NB 03.4 OURTOWN"
FIRST = {"jurisdiction": "Townville FD", "run_numbers": {"OURS": "26-0001"}}
LATER = {"jurisdiction": "Our FD", "run_numbers": {"OURS": "26-0002", "TOWNV": "26-7001"}}
HW_PAGE = ("Control to Townville, Ladder 31. Sample Parkway northbound mile marker 3.1, exit 4 to Example Avenue. "
           "Car fire, vehicle fully involved.")
HW_PAGE_LATER = ("Engine 62. Sample Parkway northbound mile marker 3.4 for a car fire. Engine 62, Ladder 31. Sample "
                 "Parkway northbound mile marker 3.4.")
RUN_ON = ("Engine 62, Ladder 31. Car fire, smoke showing. caller reports heavy smoke. Engine 62 Ladder 31. Townville "
          "units and Townville ems. somewhere out there.")


def lines(rig, iid: int) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? ORDER BY at, id", (iid,))]


def shown(rig, iid: int) -> dict:
    return next(i for i in rig.cad.state()["incidents"] if i["id"] == iid)


def first_run(rig) -> None:
    rig.feed.put("F0000123", type="MVF", location=PKWY, units=("L31",), times={"dispatched": T0}, extra=FIRST)
    rig.poll()


# -- the parts -------------------------------------------------------------------------------------------------------


def test_event_number_is_the_id_without_an_adapters_run_suffix():
    assert event_number("F0000123") == event_number("F0000123-2") == event_number("F0000123-13") == "F0000123"
    assert event_number("I4455-2") == "I4455"
    assert event_number("A1-2", {"extra": {"event_number": "F0000777"}}) == "F0000777"
    for never in ("26-12", "26-0012", "event", "event-2", "F0000123-2-3", "-2", ""):
        assert event_number(never) == ""


def test_a_highway_place_is_its_route_direction_and_mile_marker():
    a, b = highway(PKWY), highway("@Sample Pkwy NB 03.1 OURTOWN")
    assert (a.route, a.dir, a.mile) == (b.route, b.dir, b.mile) == ("Sample Pkwy", "NB", 3.1)
    assert highway("I-999 SB MM 5.7").shown() == "I-999 SB MM 5.7" == highway("i 999 southbound mild marker 5.7").shown()
    assert highway("@I999 SB 05.7 SAMPLE_TOWN").shown() == "I-999 SB MM 5.7"
    assert highway("12 Example Ave") is None and highway("I-999 SB at exit 4") is None
    assert highway("the Sample Parkway at the Model Parkway interchange, mile marker 0") is None   # whose marker?
    assert same_place("I-999 SB MM 5.7", "@I999 SB 05.7 SAMPLE_TOWN")
    assert not same_place("I-999 SB MM 5.7", "I-999 SB MM 6.2")             # half a mile on: another place
    assert not same_place("I-999 SB MM 5.7", "I-999 NB MM 5.7")             # the other carriageway
    assert not same_place("I-999 SB MM 5.7", "I-998 SB MM 5.7")
    assert same_place("I-999 MM 5.7", "I-999 SB MM 5.7")                     # one says no direction


def test_a_page_on_a_highway_is_located_by_its_marker_and_an_exit_is_never_a_house_number():
    d = parse(HW_PAGE)
    assert d.location == "Sample Pkwy NB MM 3.1" and d.incident_type == "Car fire"
    d = parse(HW_PAGE_LATER)
    assert d.location == "Sample Pkwy NB MM 3.4" and d.units == ["Engine 62", "Ladder 31"]
    d = parse("Ladder 31, car fire, the Sample Parkway exit 4 to Example Avenue.")
    assert "4" not in d.location


def test_a_page_s_own_words_are_never_its_address():
    d = parse(RUN_ON)
    assert d.incident_type == "Car fire" and d.location == ""               # no place said: unlocated
    assert parse("Port Chester Fire Department, activated fire alarm at the Sample Mall food court.").location == \
        "sample mall food court"                                            # a short place phrase still is one


def test_a_page_with_no_place_makes_an_unlocated_card_with_its_words_in_the_remarks(tmp_path):
    rig = Rig(tmp_path)
    inc = rig.page(text=RUN_ON)
    assert inc["address"] == "" and inc["address_raw"] == ""
    assert "caller reports heavy smoke" in inc["remarks"].lower()


# -- one county event, several runs ------------------------------------------------------------------------------------


def test_another_run_of_one_county_event_updates_the_same_incident(tmp_path):
    rig = Rig(tmp_path)
    first_run(rig)
    [inc] = rig.incidents()
    rig.at(240)
    rig.feed.put("F0000123-2", type="MVF", location=PKWY_LATER, units=("E62", "L31"), times={"dispatched": T0 + 235},
                 extra=LATER)
    rig.poll()
    assert [i["id"] for i in rig.incidents()] == [inc["id"]]
    said = lines(rig, inc["id"])
    assert "Redispatched: Townville FD → Our FD (County feed: run OURS 26-0002, TOWNV 26-7001)" in said
    assert "County feed: units E62, L31 (+E62)" in said
    old = rig.cad.store.one("SELECT * FROM source_reports WHERE ref = 'F0000123'")
    assert (old["merged_into"], old["incident_id"]) == ("F0000123-2", inc["id"])
    f = shown(rig, inc["id"])["feed"]
    assert f["event_id"] == "F0000123" and f["jurisdiction"] == "Our FD"
    assert f["run_numbers"] == {"OURS": ["26-0001", "26-0002"], "TOWNV": "26-7001"}
    assert same_place(rig.cad.store.incident(inc["id"])["address_raw"], PKWY_LATER)   # the new run's place
    n = len(lines(rig, inc["id"]))
    rig.at(300)
    rig.feed.change("F0000123", location="SAMPLE PKWY NB MM 2.0", closed=True, times={"in_service": T0 + 290})
    rig.poll()                                      # the earlier run ending changes nothing
    assert len(lines(rig, inc["id"])) == n and rig.cad.store.incident(inc["id"])["status"] == "queued"
    rig.feed.change("F0000123-2", units=("E62", "L31", "E64"))
    rig.poll()
    assert lines(rig, inc["id"])[-1] == "County feed: units E62, L31, E64 (+E64)" and len(rig.incidents()) == 1


def test_an_adapter_s_event_number_ties_two_ids_to_one_event(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("X1", type="MVF", location=PKWY, units=("L31",), times={"dispatched": T0},
                 extra={**FIRST, "event_number": "F0000777"})
    rig.poll()
    rig.at(120)
    rig.feed.put("X2", type="MVF", location=PKWY_LATER, units=("E62",), times={"dispatched": T0 + 110},
                 extra={**LATER, "event_number": "F0000777"})
    rig.poll()
    [inc] = rig.incidents()
    assert shown(rig, inc["id"])["feed"]["event_id"] == "F0000777"


def test_another_run_in_60_control_mode_dispatches_its_units_and_only_its_end_closes(tmp_path):
    rig = AutoRig(tmp_path)
    first_run(rig)
    iid = rig.iid("F0000123")
    assert rig.cad.store.incident(iid)["status"] in ("pending", "active") and rig.here("L31", "F0000123")
    rig.at(240)
    rig.feed.put("F0000123-2", type="MVF", location=PKWY_LATER, units=("L31", "E62"), times={"dispatched": T0 + 235},
                 extra=LATER)
    rig.poll()
    assert rig.iid("F0000123-2") == iid and rig.here("E62", "F0000123-2") and len(rig.incidents()) == 1
    rig.at(600)                                     # the first run in service: never the call's end
    rig.feed.change("F0000123", closed=True, times={"in_service": T0 + 590})
    rig.poll()
    assert rig.cad.store.incident(iid)["status"] == "active"
    rig.at(1200)
    rig.feed.change("F0000123-2", closed=True, times={"in_service": T0 + 1190})
    rig.poll()
    assert rig.cad.store.incident(iid)["status"] == "closed"


# -- never one incident for two calls ------------------------------------------------------------------------------------


def test_two_county_events_near_each_other_on_one_highway_are_two_incidents(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("F0000123", type="MVF", location=PKWY, units=("L31",), times={"dispatched": T0}, extra=FIRST)
    rig.at(60)
    rig.feed.put("F0000124", type="MVF", location="SAMPLE PKWY NB MM 3.6", units=("E62",),
                 times={"dispatched": T0 + 50}, extra=LATER)
    rig.poll()
    a, b = rig.incidents()
    assert not any(t.startswith("Redispatched") for t in lines(rig, a["id"]) + lines(rig, b["id"]))


def test_another_department_s_event_at_the_same_marker_is_its_own(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("F0000123", type="MVF", location=PKWY, units=("L31",), times={"dispatched": T0}, extra=FIRST)
    rig.feed.put("F0000200", type="MVF", location=PKWY, units=("TV1",), times={"dispatched": T0 + 20},
                 extra={"jurisdiction": "Townville FD", "run_numbers": {"TOWNV": "26-7001"}})
    rig.poll()
    assert len(rig.incidents()) == 2


def test_a_run_number_used_again_never_joins_two_events(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("F0000300", type="ALARM", location="12 Example Ave", units=("E62",), times={"dispatched": T0},
                 extra={"jurisdiction": "Our FD", "run_numbers": {"OURS": "26-0005"}})
    rig.poll()
    rig.at(90)
    rig.feed.put("F0000301", type="ALARM", location="400 Sample St", units=("E64",), times={"dispatched": T0 + 80},
                 extra={"jurisdiction": "Our FD", "run_numbers": {"OURS": "26-0005"}})
    rig.poll()
    assert len(rig.incidents()) == 2


def test_a_run_first_seen_over_or_one_after_the_incident_closed_never_joins(tmp_path):
    rig = AutoRig(tmp_path)
    first_run(rig)
    iid = rig.iid("F0000123")
    rig.at(60)
    rig.feed.put("F0000123-2", type="MVF", location=PKWY_LATER, units=("E62",), closed=True,
                 times={"dispatched": T0 + 55, "in_service": T0 + 58}, extra=LATER)
    rig.poll()                                      # over when first seen: recorded, the live run untouched
    assert rig.cad.store.one("SELECT merged_into FROM source_reports WHERE ref = 'F0000123'")["merged_into"] == ""
    assert rig.cad.store.incident(iid)["status"] == "active"
    rig.cad.close(iid, {"disposition": "COMPLETE", "operator": "Lt. Test"})
    rig.at(4000)
    rig.feed.put("F0000123-3", type="MVF", location=PKWY, units=("E62",), times={"dispatched": T0 + 3990}, extra=LATER)
    rig.poll()
    assert rig.iid("F0000123-3") != iid


# -- the feed merging two events: the automatic duplicate closed ---------------------------------------------------------


def test_a_feed_merge_closes_the_automatically_accepted_duplicate(tmp_path):
    rig = AutoRig(tmp_path)
    first_run(rig)
    rig.at(240)                                     # another id the adapter can't tie to the first yet
    rig.feed.put("F0000555", type="MVF", location=PKWY_LATER, units=("L31",), times={"dispatched": T0 + 235}, extra=LATER)
    rig.poll()
    a, b = rig.iid("F0000123"), rig.iid("F0000555")
    assert a != b and rig.cad.store.incident(b)["status"] in ("pending", "active")    # taken by 60 Control mode
    rig.at(420)                                     # ... until it learns they are one
    rig.feed.change("F0000123", aliases=("F0000555",))
    rig.poll()
    gone = rig.cad.store.incident(b)
    assert (gone["status"], gone["disposition"]) == ("closed", "DUPLICATE")
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'F0000555'")["incident_id"] == a
    assert f"County feed: merged into #{rig.cad.store.incident(a)['number']}" in lines(rig, b)


def test_a_feed_merge_keeps_a_duplicate_a_person_worked_on(tmp_path):
    rig = AutoRig(tmp_path)
    first_run(rig)
    rig.at(240)
    rig.feed.put("F0000555", type="MVF", location=PKWY_LATER, units=("L31",), times={"dispatched": T0 + 235}, extra=LATER)
    rig.poll()
    b = rig.iid("F0000555")
    rig.cad.note(b, {"text": "staging on the shoulder", "operator": "Lt. Test"})
    rig.at(420)
    rig.feed.change("F0000123", aliases=("F0000555",))
    rig.poll()
    assert rig.cad.store.incident(b)["status"] in ("pending", "active")
    assert any(t.endswith("(kept: check it)") for t in lines(rig, b))


# -- a page and the feed on a highway, either order ----------------------------------------------------------------------


def test_a_page_first_then_the_feed_one_incident_even_once_a_person_accepted_it(tmp_path):
    rig = Rig(tmp_path)
    page = rig.page(text=HW_PAGE)
    assert highway(page["address_raw"]).shown() == "Sample Pkwy NB MM 3.1"
    rig.cad.accept(page["id"], {"operator": "Lt. Test"})
    rig.at(50)
    rig.feed.put("F0000123", type="MVF", location=PKWY, units=("L31",), times={"dispatched": T0 - 10}, extra=FIRST)
    rig.poll()
    assert [i["id"] for i in rig.incidents()] == [page["id"]]


def test_the_feed_first_then_the_page_one_incident(tmp_path):
    rig = Rig(tmp_path)
    first_run(rig)
    [inc] = rig.incidents()
    rig.at(40)
    assert rig.page(text=HW_PAGE)["id"] == inc["id"] and len(rig.incidents()) == 1


def test_a_page_after_another_run_joins_by_its_marker_whatever_a_person_typed(tmp_path):
    rig = Rig(tmp_path)
    first_run(rig)
    [inc] = rig.incidents()
    rig.cad.accept(inc["id"], {"operator": "Lt. Test"})
    rig.cad.update(inc["id"], {"address": "Sample Pkwy by the bridge", "operator": "Lt. Test"})
    rig.at(240)
    rig.feed.put("F0000123-2", type="MVF", location=PKWY_LATER, units=("E62", "L31"), times={"dispatched": T0 + 235},
                 extra=LATER)
    rig.poll()
    rig.at(300)
    assert rig.page(text=HW_PAGE_LATER)["id"] == inc["id"] and len(rig.incidents()) == 1


def test_a_page_at_another_marker_or_carriageway_is_its_own_card(tmp_path):
    rig = Rig(tmp_path)
    first_run(rig)
    rig.at(40)
    far = rig.page(text=HW_PAGE.replace("3.1", "4.6"))
    assert far["source"] == "60 Control page" and len(rig.incidents()) == 2
    rig.at(60)
    other = rig.page("p2", HW_PAGE.replace("northbound", "southbound"), fresh=True)
    assert other["source"] == "60 Control page" and len(rig.incidents()) == 3
    near = rig.page("p3", HW_PAGE.replace("3.1", "3.5"), fresh=True)        # near it: noted, never joined
    assert near["source"] == "60 Control page" and len(rig.incidents()) == 4
    assert any(t.startswith("Possibly the same as") for t in lines(rig, near["id"]))


def test_one_page_per_run_the_second_run_s_page_joins_a_second_page_for_one_run_never(tmp_path):
    rig = Rig(tmp_path)
    page = rig.page(text=HW_PAGE)
    rig.at(50)
    first_run(rig)
    assert [i["id"] for i in rig.incidents()] == [page["id"]]
    rig.at(240)
    rig.feed.put("F0000123-2", type="MVF", location=PKWY_LATER, units=("E62", "L31"), times={"dispatched": T0 + 235},
                 extra=LATER)
    rig.poll()
    rig.at(300)
    assert rig.page("p2", HW_PAGE_LATER, fresh=True)["id"] == page["id"]          # the second run's page
    rig.at(330)
    again = rig.page("p3", HW_PAGE_LATER.replace("Engine 62", "Engine 64"), fresh=True)
    assert again["id"] != page["id"] and again["source"] == "60 Control page"     # a third page: a person decides
    assert any(t.startswith("Possibly the same as") for t in lines(rig, again["id"]))


def test_a_feed_merge_moves_the_units_automation_put_on_the_duplicate(tmp_path):
    rig = AutoRig(tmp_path)
    first_run(rig)
    rig.at(240)
    rig.feed.put("F0000555", type="MVF", location=PKWY_LATER, units=("E62",), times={"dispatched": T0 + 235}, extra=LATER)
    rig.poll()
    a, b = rig.iid("F0000123"), rig.iid("F0000555")
    assert rig.here("E62", "F0000555")
    before = rig.cad.store.one("SELECT * FROM assignments WHERE incident_id = ? AND cleared_at IS NULL", (b,))
    rig.at(420)
    rig.feed.change("F0000123", aliases=("F0000555",), units=("L31", "E62"))
    rig.poll()
    assert rig.cad.store.incident(b)["disposition"] == "DUPLICATE"
    moved = rig.cad.store.one("SELECT * FROM assignments WHERE unit_id = ? AND cleared_at IS NULL", (rig.uid("E62"),))
    assert (moved["incident_id"], moved["dispatched_at"]) == (a, before["dispatched_at"])
    assert rig.status("E62") == rig.cad.store.lists.status_for("dispatched")
