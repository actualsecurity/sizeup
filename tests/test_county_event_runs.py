"""One county event, one incident (sizeup/cad/sources.py "same event" and "highway"): another run of an event (the
adapter's "<id>-2", or the same "event_number") updates the incident the first run made, whichever arrives first; a
feed's merge of two events closes the automatic duplicate; a page and the feed on a highway match by mile marker in
either order; a page that says no place stays unlocated, never its own words as an address. Invented data only:
made-up event ids, roads, towns, agencies, units, markers and times."""

from __future__ import annotations

import pytest

from sizeup.cad.sources import feed_event_number, later_run, same_place
from sizeup.dispatch import highway, parse
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0, Rig

ALARM = "40 EXAMPLE ST"
TOWNV = {"jurisdiction": "Townville FD", "run_numbers": {"TOWNV": "26-7311"}}
OURS = {"jurisdiction": "Our FD", "run_numbers": {"OURS": "26-0418", "TOWNV": "26-7311"}}
I999 = "I-999 SB MM 12.6 EXAMPLETOWN"
I999_PAGE = ("Engine 61, Rescue 40. Brush fire, I 999 southbound mile marker 12.6, exit 9 to Example Avenue. "
             "Fire along the shoulder.")
RUN_ON = ("Engine 63. Dumpster fire, flames visible. Neighbor reports a strong smell. Engine 63. Sampleville units "
          "standing by. Somewhere behind it.")


def lines(rig, iid: int) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? ORDER BY at, id", (iid,))]


def shown(rig, iid: int) -> dict:
    return next(i for i in rig.cad.state()["incidents"] if i["id"] == iid)


def townville_run(rig, dt: float = 0) -> None:
    rig.feed.put("F0040001", type="ALARM/COMM", location=ALARM, units=("TVE1", "TVL2"), times={"dispatched": T0 + dt},
                 extra=TOWNV)
    rig.poll()


def our_run(rig, dt: float = 300, ref: str = "F0040001-2") -> None:
    rig.feed.put(ref, type="ALARM/COMM", location=ALARM, units=("E64",), times={"dispatched": T0 + dt}, extra=OURS)
    rig.poll()


def highway_run(rig, ref: str = "F0050001", loc: str = I999, units=("E61",), dt: float = 0, **kw) -> None:
    rig.feed.put(ref, type="BRUSH FIRE", location=loc, units=units, times={"dispatched": T0 + dt}, **kw)
    rig.poll()


# -- the parts -------------------------------------------------------------------------------------------------------


def test_event_number_is_the_id_without_an_adapters_run_suffix_only_for_an_event_numbers_shape():
    assert feed_event_number("F0000123") == feed_event_number("F0000123-2") == feed_event_number("F0000123-13") \
        == "F0000123"
    assert feed_event_number("I0044551-2") == "I0044551"
    assert feed_event_number("A1-2", {"extra": {"event_number": "F0000777"}}) == "F0000777"
    for never in ("26-12", "26-0012", "event", "event-2", "F0000123-2-3", "-2", "", "T1-1", "T1-2", "F26-12",
                  "F26-13", "AB12345-2", "F1234-2", "F00001x-2"):
        assert feed_event_number(never) == "", never


def test_the_later_run_is_the_higher_suffix_else_the_one_dispatched_later_never_the_last_to_arrive():
    early, late = {"times": {"dispatched": T0}}, {"times": {"dispatched": T0 + 60}}
    assert later_run("F0000123-2", early, "F0000123", late)          # the suffix, whatever time a run is stamped with
    assert not later_run("F0000123", late, "F0000123-2", early)
    assert later_run("F0000123-3", {}, "F0000123-2", {}) and not later_run("F0000123", {}, "F0000123-2", {})
    assert later_run("X2", late, "X1", early) and not later_run("X1", early, "X2", late)   # one suffix: the time


def test_a_highway_place_is_its_route_direction_and_mile_marker():
    a = highway(I999)
    assert (a.route, a.dir, a.mile) == ("I-999", "SB", 12.6)
    assert highway("I-999 SB MM 12.6").shown() == "I-999 SB MM 12.6" == highway("i 999 southbound mild marker 12.6").shown()
    assert highway("Sample Pkwy NB MM 7.25").shown() == "Sample Pkwy NB MM 7.25"
    assert highway("brush fire Sample Parkway northbound mile marker 7.25").route == "Sample Pkwy"   # no call type
    assert highway("southbound on I 999 at mile marker 12.6").dir == "SB"                          # said before it
    assert highway("12 Example Ave") is None and highway("I-999 SB") is None
    assert highway("I-999 SB at exit 9").shown() == "I-999 SB exit 9" == highway("I999 SB EXIT 9").shown()
    assert highway("I-999 north at mile marker 4.1").dir == "NB"
    assert highway("i 2 cars at marker 3") is None and highway("I-2 at marker 3").route == "I-2"
    assert highway("the Sample Parkway at the Model Parkway interchange, mile marker 0") is None   # whose marker?
    assert same_place("I-999 SB MM 12.6", "I-999 SB MM 12.60 EXAMPLETOWN")
    assert not same_place("I-999 SB MM 12.6", "I-999 SB MM 13.1")              # half a mile on: another place
    assert not same_place("I-999 SB MM 12.6", "I-999 NB MM 12.6")              # the other carriageway
    assert not same_place("I-999 SB MM 12.6", "I-998 SB MM 12.6")
    assert same_place("I-999 MM 12.6", "I-999 SB MM 12.6")                      # one says no direction


def test_routes_with_one_number_are_told_apart_and_one_route_s_names_agree():
    us = [highway(f"{r} northbound mile marker 2.2") for r in ("US 919", "US Route 919", "US Highway 919", "U S 919")]
    assert {h.route for h in us} == {"US-919"}
    assert highway("Route 919 northbound mile marker 2.2").route == "Route 919"
    assert highway("State Route 919 NB MM 2.2").route == highway("NY 919 NB MM 2.2").route == "NY-919"
    assert highway("State Highway 918 NB MM 2.2").route == "NY-918" != highway("US Hwy 918 NB MM 2.2").route
    assert not same_place("US 919 NB MM 2.2", "Route 919 NB MM 2.2")            # two roads perhaps: never joined
    assert not same_place("US 919 NB MM 2.2", "NY 919 NB MM 2.2")
    assert not same_place("CR 919 NB MM 2.2", "I-919 NB MM 2.2")


def test_a_page_on_a_highway_is_located_by_its_marker_and_an_exit_is_never_a_house_number():
    d = parse(I999_PAGE)
    assert d.location == "I-999 SB MM 12.6" and d.incident_type == "Brush fire"
    d = parse("Engine 61, brush fire, the Sample Parkway exit 9 to Example Avenue.")
    assert d.location == "Sample Pkwy exit 9"                              # never "9 Example Avenue"
    assert parse("Engine 61, MVA with injuries, I-95 northbound exit 21.").location == "I-95 NB exit 21"
    assert parse("car fire, the Hutch southbound at exit 19.").location == "Hutch SB exit 19"
    assert parse("Engine 62, MVA, 684 NB MM 5.3.").location == "I-684 NB MM 5.3"     # a bare number the search knows
    assert parse("Engine 62, car fire, exit 21 off ramp.").location == "exit 21 off ramp"


def test_an_address_said_before_a_highway_is_the_place_and_the_numbers_before_a_marker_are_read_right():
    d = parse("Port Chester Fire, car fire, 301, 302, Ladder 31, 12 Example Ave, I-999 northbound mile marker 4.2 nearby.")
    assert d.location == "12 example ave" and d.numbers == [301, 302]
    d = parse("Engine 62,  301   302,  Sample Parkway northbound mile marker 7.25 for a car fire.")
    assert d.location == "Sample Pkwy NB MM 7.25" and d.numbers == [301, 302]


def test_a_page_s_own_words_are_never_its_address_but_a_place_after_its_type_is():
    d = parse(RUN_ON)
    assert d.incident_type == "Brush fire" and d.location == ""                # no place said: unlocated
    assert parse("Port Chester Fire Department, activated fire alarm at the Sample Mall food court.").location == \
        "sample mall food court"
    assert parse("Port Chester Fire, odor of smoke at the Example Middle School, Engine 61. Time out 0930.").location \
        == "the example middle school"                                         # cut at its unit, never past it
    assert parse("Rye Brook Fire, fire alarm, Example School gym. Engine 63.").location == "example school gym"
    assert parse("Port Chester Fire, fire alarm at St. Example's Church. Engine 63.").location \
        == "st. example s church"                                            # an abbreviation's dot: no sentence end
    assert parse("Port Chester Fire, MVA, Example and Sample. Engine 63.").location == "example and sample"
    assert parse("Port Chester Fire, brush fire, Sample River. Engine 63.").location == "sample river"
    assert parse("Port Chester Fire, fire alarm, Example Commons. Engine 63.").location == "example commons"
    assert parse("Port Chester Fire, mutual aid, structure fire, to Townville. Engine 63.").location == "townville"
    assert parse("Engine 63. Car fire, smoke and flames. Engine 63.").location == ""


def test_a_page_with_no_place_makes_an_unlocated_card_with_its_words_in_the_remarks(tmp_path):
    rig = Rig(tmp_path)
    inc = rig.page(text=RUN_ON)
    assert inc["address"] == "" and inc["address_raw"] == ""
    assert "neighbor reports a strong smell" in inc["remarks"].lower()


# -- one county event, several runs ------------------------------------------------------------------------------------


def test_another_run_of_one_county_event_updates_the_same_incident(tmp_path):
    rig = Rig(tmp_path)
    townville_run(rig)
    [inc] = rig.incidents()
    rig.at(310)
    our_run(rig)
    assert [i["id"] for i in rig.incidents()] == [inc["id"]]
    said = lines(rig, inc["id"])
    assert "Redispatched: Townville FD → Our FD (County feed: run OURS 26-0418)" in said
    assert "County feed: units E64 (+E64) (-TVE1, TVL2)" in said
    old = rig.cad.store.one("SELECT * FROM source_reports WHERE ref = 'F0040001'")
    assert (old["merged_into"], old["incident_id"]) == ("F0040001-2", inc["id"])
    f = shown(rig, inc["id"])["feed"]
    assert f["event_id"] == "F0040001" and f["jurisdiction"] == "Our FD"
    assert f["run_numbers"] == {"TOWNV": "26-7311", "OURS": "26-0418"}
    n = len(lines(rig, inc["id"]))
    rig.at(400)
    rig.feed.change("F0040001", closed=True, times={"in_service": T0 + 390})
    rig.poll()                                      # the earlier run ending changes nothing
    assert len(lines(rig, inc["id"])) == n and rig.cad.store.incident(inc["id"])["status"] == "queued"
    rig.feed.change("F0040001-2", units=("E64", "L31"))
    rig.poll()
    assert lines(rig, inc["id"])[-1] == "County feed: units E64, L31 (+L31)" and len(rig.incidents()) == 1


def test_the_later_run_arriving_first_stays_the_one_that_rules(tmp_path):
    rig = AutoRig(tmp_path)
    our_run(rig, dt=300)                            # a restart: the feed lists the later run before the earlier one
    iid = rig.iid("F0040001-2")
    assert rig.here("E64", "F0040001-2")
    rig.at(320)
    townville_run(rig, dt=0)
    assert rig.iid("F0040001") == iid and len(rig.incidents()) == 1
    old = rig.cad.store.one("SELECT merged_into FROM source_reports WHERE ref = 'F0040001'")
    assert old["merged_into"] == "F0040001-2"       # the earlier run retired, the later one rules
    assert rig.cad.store.one("SELECT merged_into FROM source_reports WHERE ref = 'F0040001-2'")["merged_into"] == ""
    assert not any(t.startswith("Redispatched") for t in lines(rig, iid))      # never said backwards
    rig.at(600)
    rig.feed.change("F0040001", closed=True, times={"in_service": T0 + 590})
    rig.poll()                                      # the earlier run's end: never the call's
    assert rig.cad.store.incident(iid)["status"] == "active" and rig.here("E64", "F0040001-2")


def test_a_redispatch_is_one_write_a_failure_part_way_leaves_nothing_and_a_retry_joins(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    townville_run(rig)
    [inc] = rig.incidents()
    store, real = rig.cad.store, rig.cad.store._event

    def boom(db, iid, kind, text, *a, **kw):
        if text.startswith("Redispatched"):
            raise RuntimeError("the lease went")
        return real(db, iid, kind, text, *a, **kw)

    monkeypatch.setattr(store, "_event", boom)
    rig.at(310)
    rig.feed.put("F0040001-2", type="ALARM/COMM", location=ALARM, units=("E64",), times={"dispatched": T0 + 300},
                 extra=OURS)
    with pytest.raises(RuntimeError):
        rig.cad.sources.on_event({"source": "county", "label": "County feed", "rank": 1, "seen_at": rig.clock.t,
                                  "event": rig.feed.events["F0040001-2"].as_dict()})
    assert store.one("SELECT 1 AS x FROM source_reports WHERE ref = 'F0040001-2'") is None
    assert store.one("SELECT merged_into FROM source_reports WHERE ref = 'F0040001'")["merged_into"] == ""
    monkeypatch.setattr(store, "_event", real)
    rig.poll()                                      # sent again (the station's retry): joined once, as one write
    rig.poll()
    assert [i["id"] for i in rig.incidents()] == [inc["id"]]
    assert store.one("SELECT merged_into FROM source_reports WHERE ref = 'F0040001'")["merged_into"] == "F0040001-2"
    assert sum(t.startswith("Redispatched") for t in lines(rig, inc["id"])) == 1


def test_an_adapter_s_event_number_ties_two_ids_to_one_event(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("X1", type="ALARM/COMM", location=ALARM, units=("TVE1",), times={"dispatched": T0},
                 extra={**TOWNV, "event_number": "F0000777"})
    rig.poll()
    rig.at(120)
    rig.feed.put("X2", type="ALARM/COMM", location=ALARM, units=("E64",), times={"dispatched": T0 + 110},
                 extra={**OURS, "event_number": "F0000777"})
    rig.poll()
    [inc] = rig.incidents()
    assert shown(rig, inc["id"])["feed"]["event_id"] == "F0000777"


def test_another_run_in_60_control_mode_dispatches_its_units_and_only_its_end_closes(tmp_path):
    rig = AutoRig(tmp_path)
    rig.feed.put("F0040001", type="ALARM/COMM", location=ALARM, units=("E62",), times={"dispatched": T0}, extra=TOWNV)
    rig.poll()
    iid = rig.iid("F0040001")
    assert rig.cad.store.incident(iid)["status"] in ("pending", "active") and rig.here("E62", "F0040001")
    rig.at(310)
    rig.feed.put("F0040001-2", type="ALARM/COMM", location=ALARM, units=("E62", "E64"), times={"dispatched": T0 + 300},
                 extra=OURS)
    rig.poll()
    assert rig.iid("F0040001-2") == iid and rig.here("E64", "F0040001-2") and len(rig.incidents()) == 1
    rig.at(600)                                     # the first run in service: never the call's end
    rig.feed.change("F0040001", closed=True, times={"in_service": T0 + 590})
    rig.poll()
    assert rig.cad.store.incident(iid)["status"] == "active"
    rig.at(1200)
    rig.feed.change("F0040001-2", closed=True, times={"in_service": T0 + 1190})
    rig.poll()
    assert rig.cad.store.incident(iid)["status"] == "closed"


# -- never one incident for two calls ------------------------------------------------------------------------------------


def test_another_adapter_s_short_ids_with_a_dash_are_two_events(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("T1-1", type="ALARM/COMM", location="12 Example Ave", units=("E62",), times={"dispatched": T0})
    rig.feed.put("T1-2", type="ALARM/COMM", location="400 Sample St", units=("E64",), times={"dispatched": T0 + 30})
    rig.poll()
    a, b = rig.incidents()
    assert not any(t.startswith("Redispatched") for t in lines(rig, a["id"]) + lines(rig, b["id"]))


def test_two_county_events_near_each_other_on_one_highway_are_two_incidents(tmp_path):
    rig = Rig(tmp_path)
    highway_run(rig)
    rig.at(60)
    highway_run(rig, "F0050002", "I-999 SB MM 13.0", ("E63",), dt=50)
    a, b = rig.incidents()
    assert not any(t.startswith("Redispatched") for t in lines(rig, a["id"]) + lines(rig, b["id"]))


def test_another_department_s_event_at_the_same_marker_is_its_own(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("F0050001", type="BRUSH FIRE", location=I999, units=("E61",), times={"dispatched": T0})
    rig.feed.put("F0050200", type="BRUSH FIRE", location=I999, units=("TVE1",), times={"dispatched": T0 + 20},
                 extra={"jurisdiction": "Townville FD", "run_numbers": {"TOWNV": "26-7400"}})
    rig.poll()
    assert len(rig.incidents()) == 2


def test_a_run_number_used_again_never_joins_two_events(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("F0040300", type="ALARM", location="12 Example Ave", units=("E62",), times={"dispatched": T0},
                 extra={"jurisdiction": "Our FD", "run_numbers": {"OURS": "26-0420"}})
    rig.poll()
    rig.at(90)
    rig.feed.put("F0040301", type="ALARM", location="400 Sample St", units=("E64",), times={"dispatched": T0 + 80},
                 extra={"jurisdiction": "Our FD", "run_numbers": {"OURS": "26-0420"}})
    rig.poll()
    assert len(rig.incidents()) == 2


def test_a_run_first_seen_over_or_one_after_the_incident_closed_never_joins(tmp_path):
    rig = AutoRig(tmp_path)
    rig.feed.put("F0040001", type="ALARM/COMM", location=ALARM, units=("E62",), times={"dispatched": T0}, extra=TOWNV)
    rig.poll()
    iid = rig.iid("F0040001")
    rig.at(60)
    rig.feed.put("F0040001-2", type="ALARM/COMM", location=ALARM, units=("E64",), closed=True,
                 times={"dispatched": T0 + 55, "in_service": T0 + 58}, extra=OURS)
    rig.poll()                                      # over when first seen: recorded, the live run untouched
    assert rig.cad.store.one("SELECT merged_into FROM source_reports WHERE ref = 'F0040001'")["merged_into"] == ""
    assert rig.cad.store.incident(iid)["status"] == "active"
    rig.cad.close(iid, {"disposition": "COMPLETE", "operator": "Lt. Test"})
    rig.at(4000)
    rig.feed.put("F0040001-3", type="ALARM/COMM", location=ALARM, units=("E64",), times={"dispatched": T0 + 3990},
                 extra=OURS)
    rig.poll()
    assert rig.iid("F0040001-3") != iid


# -- the feed merging two events: the automatic duplicate closed ---------------------------------------------------------


def two_accepted(rig) -> tuple[int, int]:
    rig.feed.put("F0060001", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E61",), times={"dispatched": T0})
    rig.poll()
    rig.at(240)                                     # another id the adapter can't tie to the first yet
    rig.feed.put("F0060555", type="ALARM/COMM", location="400 SAMPLE ST", units=("E63",), times={"dispatched": T0 + 235})
    rig.poll()
    a, b = rig.iid("F0060001"), rig.iid("F0060555")
    assert a != b and rig.cad.store.incident(b)["status"] in ("pending", "active")    # taken by 60 Control mode
    return a, b


def test_a_feed_merge_closes_the_automatically_accepted_duplicate_and_moves_its_units(tmp_path):
    rig = AutoRig(tmp_path)
    a, b = two_accepted(rig)
    assert rig.here("E63", "F0060555")
    rig.radio("tx-e63", rig.clock.t + 5, "60 Control, Engine 63 responding", rig="E63")
    linked = {r["traffic_id"] for r in rig.cad.store.q("SELECT traffic_id FROM radio_links WHERE incident_id = ?", (b,))}
    before = rig.cad.store.one("SELECT * FROM assignments WHERE incident_id = ? AND cleared_at IS NULL", (b,))
    was = rig.status("E63")
    step = rig.cad.store.one("SELECT id FROM auto_steps WHERE incident_id = ? AND unit = 'E63'", (b,))["id"]
    rig.at(420)                                     # ... until it learns they are one
    rig.feed.change("F0060001", aliases=("F0060555",), units=("E61", "E63"))
    rig.poll()
    gone = rig.cad.store.incident(b)
    assert (gone["status"], gone["disposition"]) == ("closed", "DUPLICATE")
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'F0060555'")["incident_id"] == a
    assert f"County feed: merged into #{rig.cad.store.incident(a)['number']}" in lines(rig, b)
    moved = rig.cad.store.one("SELECT * FROM assignments WHERE unit_id = ? AND cleared_at IS NULL", (rig.uid("E63"),))
    assert (moved["id"], moved["incident_id"], moved["dispatched_at"]) == (before["id"], a, before["dispatched_at"])
    assert rig.status("E63") == was                 # as it was: never released by the close
    h = rig.cad.store.one("SELECT * FROM unit_history WHERE unit_id = ? ORDER BY id DESC LIMIT 1", (rig.uid("E63"),))
    assert h["incident_id"] == a and h["note"].startswith("moved from")
    assert rig.cad.store.one("SELECT incident_id FROM auto_steps WHERE id = ?", (step,))["incident_id"] == a
    assert len(rig.cad.store.q("SELECT * FROM assignments WHERE unit_id = ? AND cleared_at IS NULL",
                               (rig.uid("E63"),))) == 1
    assert linked and linked <= {r["traffic_id"] for r in rig.cad.store.q(
        "SELECT traffic_id FROM radio_links WHERE incident_id = ?", (a,))}           # its radio moved with it
    assert not rig.cad.store.q("SELECT 1 FROM radio_links WHERE incident_id = ?", (b,))


def test_a_feed_merge_into_a_queued_incident_keeps_both(tmp_path):
    rig = AutoRig(tmp_path)
    rig.feed.put("F0060001", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("TVE1",), times={"dispatched": T0})
    rig.poll()                                      # none of ours: it waits for a person
    rig.at(240)
    rig.feed.put("F0060555", type="ALARM/COMM", location="400 SAMPLE ST", units=("E63",), times={"dispatched": T0 + 235})
    rig.poll()
    a, b = rig.iid("F0060001"), rig.iid("F0060555")
    assert rig.cad.store.incident(a)["status"] == "queued" and rig.cad.store.incident(b)["status"] in ("pending", "active")
    rig.at(420)
    rig.feed.change("F0060001", aliases=("F0060555",))
    rig.poll()
    assert rig.cad.store.incident(b)["status"] in ("pending", "active") and rig.here("E63", "F0060555")
    assert any(t.endswith("(kept: check it)") for t in lines(rig, b))


@pytest.mark.parametrize("work", ["note", "kept radio"])
def test_a_feed_merge_keeps_a_duplicate_a_person_worked_on(tmp_path, work):
    rig = AutoRig(tmp_path)
    a, b = two_accepted(rig)
    if work == "note":
        rig.cad.note(b, {"text": "staging in the lot", "operator": "Lt. Test"})
    else:
        with rig.cad.store.tx() as db:
            db.execute("INSERT INTO radio_link_kept (traffic_id, incident_id, at, operator) VALUES ('t1', ?, ?, '')",
                       (b, rig.clock.t))
    rig.at(420)
    rig.feed.change("F0060001", aliases=("F0060555",))
    rig.poll()
    assert rig.cad.store.incident(b)["status"] in ("pending", "active")
    assert any(t.endswith("(kept: check it)") for t in lines(rig, b))


# -- a page and the feed on a highway, either order ----------------------------------------------------------------------


def test_a_page_first_then_the_feed_one_incident_even_once_a_person_accepted_it(tmp_path):
    rig = Rig(tmp_path)
    page = rig.page(text=I999_PAGE)
    assert highway(page["address_raw"]).shown() == "I-999 SB MM 12.6"
    rig.cad.accept(page["id"], {"operator": "Lt. Test"})
    rig.at(50)
    highway_run(rig, dt=-10)
    assert [i["id"] for i in rig.incidents()] == [page["id"]]


def test_the_feed_first_then_the_page_one_incident(tmp_path):
    rig = Rig(tmp_path)
    highway_run(rig)
    [inc] = rig.incidents()
    rig.at(40)
    assert rig.page(text=I999_PAGE)["id"] == inc["id"] and len(rig.incidents()) == 1


def test_mutual_aid_in_another_town_on_the_same_marker_is_the_same_place(tmp_path):
    rig = Rig(tmp_path)
    highway_run(rig, loc="I-999 SB MM 12.6 SAMPLEVILLE")
    [inc] = rig.incidents()
    rig.at(40)
    assert rig.page(text=I999_PAGE)["id"] == inc["id"]


def test_a_page_joins_the_feed_s_marker_whatever_a_person_typed_as_the_address(tmp_path):
    rig = Rig(tmp_path)
    highway_run(rig)
    [inc] = rig.incidents()
    rig.cad.accept(inc["id"], {"operator": "Lt. Test"})
    rig.cad.update(inc["id"], {"address": "I-999 by the rest area", "operator": "Lt. Test"})
    rig.at(60)
    assert rig.page(text=I999_PAGE)["id"] == inc["id"] and len(rig.incidents()) == 1


def test_a_page_at_another_marker_or_carriageway_is_its_own_card(tmp_path):
    rig = Rig(tmp_path)
    highway_run(rig)
    rig.at(40)
    far = rig.page(text=I999_PAGE.replace("12.6", "14.2"))
    assert far["source"] == "60 Control page" and len(rig.incidents()) == 2
    rig.at(60)
    other = rig.page("p2", I999_PAGE.replace("southbound", "northbound"), fresh=True)
    assert other["source"] == "60 Control page" and len(rig.incidents()) == 3
    near = rig.page("p3", I999_PAGE.replace("12.6", "13.0"), fresh=True)       # near it: noted, never joined
    assert near["source"] == "60 Control page" and len(rig.incidents()) == 4
    assert any(t.startswith("Possibly the same as") for t in lines(rig, near["id"]))


def test_a_page_that_says_no_direction_against_both_carriageways_is_noted_never_joined(tmp_path):
    rig = Rig(tmp_path)
    highway_run(rig)
    highway_run(rig, "F0050900", "I-999 NB MM 12.6", ("E63",), dt=20)
    rig.at(60)
    p = rig.page(text=I999_PAGE.replace(" southbound", ""))
    assert p["source"] == "60 Control page" and len(rig.incidents()) == 3
    assert any(t.startswith("Possibly the same as") for t in lines(rig, p["id"]))


ALARM_PAGE = "Engine 64, Ladder 31. Commercial alarm, 40 Example Street. Time out 1432."


def test_one_page_per_run_the_second_run_s_page_joins_a_second_page_for_one_run_never(tmp_path):
    rig = Rig(tmp_path)
    page = rig.page(text=ALARM_PAGE.replace("Engine 64, Ladder 31", "Engine 62"))
    rig.at(50)
    townville_run(rig, dt=-10)
    assert [i["id"] for i in rig.incidents()] == [page["id"]]
    rig.at(310)
    our_run(rig, dt=305)
    rig.at(320)                                     # the second run's page
    assert rig.page("p2", ALARM_PAGE, fresh=True)["id"] == page["id"]
    rig.at(330)                                     # the same run paged again: a person decides
    again = rig.page("p3", ALARM_PAGE.replace("Engine 64", "Engine 63"), fresh=True)
    assert again["id"] != page["id"] and again["source"] == "60 Control page"
    assert any(t.startswith("Possibly the same as") for t in lines(rig, again["id"]))


def test_a_second_page_for_one_run_just_before_its_dispatch_never_joins(tmp_path):
    rig = Rig(tmp_path)
    townville_run(rig)
    [inc] = rig.incidents()
    rig.at(310)
    our_run(rig, dt=305)
    rig.at(295)                                     # the second run's page: a little before its dispatch (the clocks)
    assert rig.page("p2", ALARM_PAGE, fresh=True)["id"] == inc["id"]
    rig.at(300)
    again = rig.page("p3", ALARM_PAGE.replace("Engine 64", "Engine 63"), fresh=True)
    assert again["id"] != inc["id"]


def test_another_feed_s_report_of_the_same_run_never_lets_a_second_page_for_it_join(tmp_path):
    from tests.fake_feed import FakeFeed
    rig = Rig(tmp_path, FakeFeed(), FakeFeed(source="county2", label="Second feed", rank=2))
    page = rig.page(text=ALARM_PAGE)
    rig.at(20)
    townville_run(rig)
    rig.feeds[1].put("F0040001", type="ALARM/COMM", location=ALARM, units=("TVE1",), times={"dispatched": T0 + 200})
    rig.poll()
    assert [i["id"] for i in rig.incidents()] == [page["id"]]
    rig.at(210)                                     # one run (two feeds' copies of it), paged again: a person decides
    again = rig.page("p2", ALARM_PAGE.replace("Engine 64", "Engine 63"), fresh=True)
    assert again["id"] != page["id"]


# -- the review of #807 ---------------------------------------------------------------------------------------------------


def test_a_page_by_exit_and_the_feed_by_exit_join_in_either_order(tmp_path):
    rig = Rig(tmp_path)
    page = rig.page(text="Engine 61, MVA with injuries, I-95 northbound exit 21.")
    rig.at(40)
    rig.feed.put("F0070001", type="MVA", location="I-95 NB EXIT 21", units=("E61",), times={"dispatched": T0 + 20})
    rig.poll()
    assert [i["id"] for i in rig.incidents()] == [page["id"]]
    rig = Rig(tmp_path / "2")
    rig.feed.put("F0070002", type="MVA", location="HUTCH SB EXIT 19", units=("E61",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    rig.at(40)
    assert rig.page(text="Engine 61, car fire, the Hutch southbound at exit 19.")["id"] == inc["id"]
    rig.at(60)                                      # another exit: its own card
    assert rig.page("p2", "Engine 63, car fire, the Hutch southbound at exit 30.", fresh=True)["id"] != inc["id"]


def test_another_feed_s_report_of_the_duplicate_never_rules_the_kept_call(tmp_path):
    from tests.fake_feed import FakeFeed
    rig = AutoRig(tmp_path, FakeFeed(rank=2), FakeFeed(source="county2", label="Second feed", rank=1))
    a, b = two_accepted(rig)
    rig.at(260)
    rig.feeds[1].put("Z555", type="ALARM/COMM", location="400 SAMPLE ST", units=("E63",), times={"dispatched": T0 + 236})
    rig.poll()
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'Z555'")["incident_id"] == b
    rig.at(420)
    rig.feed.change("F0060001", aliases=("F0060555",), units=("E61", "E63"))
    rig.poll()
    assert rig.cad.store.incident(b)["disposition"] == "DUPLICATE"
    z = rig.cad.store.one("SELECT incident_id, merged_into FROM source_reports WHERE ref = 'Z555'")
    assert z["incident_id"] == b and z["merged_into"] == ""                    # on the closed duplicate, not here
    rig.at(500)
    rig.feeds[1].change("Z555", closed=True, times={"in_service": T0 + 490})
    rig.poll()                                      # its end: never the kept call's
    assert rig.cad.store.incident(a)["status"] == "active" and rig.here("E63", "F0060001")
    assert not rig.cad.store.q("SELECT 1 FROM auto_steps WHERE incident_id = ? AND kind IN ('close', 'close?')", (a,))


def test_another_feed_s_report_of_the_duplicate_is_retired_into_its_own_report_on_the_kept_call(tmp_path):
    from tests.fake_feed import FakeFeed
    rig = AutoRig(tmp_path, FakeFeed(rank=2), FakeFeed(source="county2", label="Second feed", rank=1))
    a, b = two_accepted(rig)
    rig.at(260)
    rig.feeds[1].put("Z001", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E61",), times={"dispatched": T0 + 1})
    rig.feeds[1].put("Z555", type="ALARM/COMM", location="400 SAMPLE ST", units=("E63",), times={"dispatched": T0 + 236})
    rig.poll()
    rig.at(420)
    rig.feed.change("F0060001", aliases=("F0060555",), units=("E61", "E63"))
    rig.poll()
    z = rig.cad.store.one("SELECT incident_id, merged_into FROM source_reports WHERE ref = 'Z555'")
    assert (z["incident_id"], z["merged_into"]) == (a, "Z001")
    rig.feeds[1].change("Z555", closed=True, times={"in_service": T0 + 490})
    rig.poll()
    assert rig.cad.store.incident(a)["status"] == "active"


def test_a_duplicate_a_person_accepted_unsigned_is_never_closed_by_a_merge(tmp_path):
    rig = Rig(tmp_path)                             # PCFD mode: nothing accepted by automation
    rig.feed.put("F0060001", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E61",), times={"dispatched": T0})
    rig.poll()
    rig.at(240)
    rig.feed.put("F0060555", type="ALARM/COMM", location="400 SAMPLE ST", units=("E63",), times={"dispatched": T0 + 235})
    rig.poll()
    a, b = [i["id"] for i in rig.incidents()]
    rig.cad.store.accept(a, "Lt. Test")
    rig.cad.store.accept(b, "")                     # a person, no name given
    rig.at(420)
    rig.feed.change("F0060001", aliases=("F0060555",))
    rig.poll()
    assert rig.cad.store.incident(b)["status"] in ("pending", "active")
    assert any(t.endswith("(kept: check it)") for t in lines(rig, b))


def test_a_fold_never_links_a_transmission_a_person_took_off_the_kept_call(tmp_path):
    rig = AutoRig(tmp_path)
    a, b = two_accepted(rig)
    rig.radio("tx-e63", rig.clock.t + 5, "60 Control, Engine 63 responding", rig="E63")
    assert rig.cad.store.one("SELECT 1 AS x FROM radio_links WHERE traffic_id = 'tx-e63' AND incident_id = ?", (b,))
    with rig.cad.store.tx() as db:
        db.execute("INSERT INTO radio_link_exclusions (traffic_id, incident_id, at, operator) VALUES ('tx-e63', ?, ?, "
                   "'Lt. Test')", (a, rig.clock.t))
    rig.at(420)
    rig.feed.change("F0060001", aliases=("F0060555",), units=("E61", "E63"))
    rig.poll()
    assert rig.cad.store.incident(b)["disposition"] == "DUPLICATE"
    assert not rig.cad.store.one("SELECT 1 AS x FROM radio_links WHERE traffic_id = 'tx-e63' AND incident_id = ?", (a,))
