"""#365: a page with a street and no house number joins the feed's incident on that street, and a page on a feed's incident
enriches it (its clip, transcript, tones, units and cross streets) instead of making a card of its own. Also types
written "TYPE,SUBTYPE", Mutual aid / Standby, the cross-street parser, "carbon dioxide" as CO and the feed's comments.
Synthetic only: made-up streets, units' words, times and comment text (FakeFeed, tests/fake_feed.py)."""

from __future__ import annotations

import pytest

from sizeup import channels
from sizeup.cad import runcards
from sizeup.cad.sources import street_of
from sizeup.dispatch import parse
from sizeup.transcribe import build_vocabulary
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0, Rig

PAGE_ST = ("Port Chester Fire Department, Engine 60, Ladder 31, respond to a carbon monoxide alarm, Example Terrace, "
           "between Sample Avenue and Specimen Street. Time out 1432.")
PAGE_NO_UNITS = ("Port Chester Fire Department, respond to a carbon monoxide alarm, Example Terrace, between Sample "
                 "Avenue and Specimen Street. Time out 1432.")


def feed_event(rig, eid="A1", location="9 EXAMPLE TER", units=("E60", "L31"), typ="ALARM/RESD", dt=0.0):
    rig.feed.put(eid, type=typ, location=location, units=units, times={"dispatched": T0 + dt})
    rig.poll()
    return rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'county' AND ref = ?",
                             (eid,))["incident_id"]


def open_incidents(rig) -> list[dict]:
    return [i for i in rig.incidents() if i["status"] != "dismissed"]


def lines(rig, iid: int, kind: str) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = ? ORDER BY id",
                                               (iid, kind))]


def possibly(rig, iid: int, other: int) -> bool:
    ref = rig.cad.store.incident(other)
    return any(x.startswith(f"Possibly the same as #{ref['number'] or ref['id']}") for x in rig.feed_lines(iid))


@pytest.fixture
def pcfd(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    return rig


# -- the street-only rule --------------------------------------------------------------------------------------------


def test_street_of_keeps_the_strict_street():
    assert street_of("9 EXAMPLE TER") == (True, street_of("Example Terrace")[1])
    assert street_of("Example Terrace")[0] is False
    assert street_of("9 N Example Ave")[1] != street_of("South Example Avenue")[1]          # North is not South
    assert street_of("9 Example Ave Ext")[1] != street_of("Example Avenue")[1]              # EXT kept
    assert street_of("Example") is None and street_of("Elm St & Example Ave") is None       # no suffix, a corner
    assert street_of("@SAMPLE SCHOOL") is None


def test_the_feed_first_then_the_street_only_page_attaches_and_enriches_it(tmp_path):
    rig = AutoRig(tmp_path)
    a = feed_event(rig)
    rig.at(28)
    got = rig.page("p1", PAGE_ST)
    assert got["id"] == a and len(open_incidents(rig)) == 1                     # no second card
    inc = rig.cad.store.incident(a)
    assert inc["address"] == "9 EXAMPLE TER"                                     # the feed's location stands
    assert (inc["type"], inc["subtype"]) == ("ALARM", "RESD")                    # the feed's type stands
    assert inc["cross_street"] == "Sample Avenue And Specimen Street"              # the page's, the incident had none
    assert any(x.startswith("Page ") and "attached" in x for x in rig.feed_lines(a))
    tones = lines(rig, a, "tones")
    assert any(x.startswith("60 Control page ") and "Example Terrace" in x for x in tones)  # its transcript
    assert "Tones: Port Chester FD" in tones
    assert "Units named on the page: Engine 60, Ladder 31" in tones
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'paging'")["incident_id"] == a
    # 60 Control mode: accepted, the units on it, paged
    full = rig.cad.full(a)
    assert full["status"] == "active" and {x["name"] for x in full["assignments"]} == {"E60", "L31"}
    assert any(s["kind"] == "paged" for s in full["auto"]["steps"])


def test_the_only_open_incident_on_the_street_joins_without_shared_units(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, units=("E60",))
    rig.at(40)
    assert rig.page("p1", PAGE_NO_UNITS)["id"] == a and len(open_incidents(rig)) == 1


def test_the_page_first_then_the_feeds_numbered_event_attaches_to_its_incident(tmp_path):
    rig = Rig(tmp_path)
    p = rig.page("p1", PAGE_ST)["id"]
    rig.at(45)
    a = feed_event(rig, dt=10)
    assert a == p and len(open_incidents(rig)) == 1
    assert rig.cad.store.incident(p)["address"] == "9 EXAMPLE TER"              # the feed's numbered address now


def test_two_calls_on_one_street_never_join_and_every_one_is_noted(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", "9 EXAMPLE TER", ("E60",))
    b = feed_event(rig, "A2", "40 EXAMPLE TER", ("E62",), dt=30)
    rig.at(60)
    p = rig.page("p1", PAGE_ST)["id"]                                           # E60 shared with A1: still not joined
    assert p not in (a, b) and len(open_incidents(rig)) == 3
    for x in (a, b):
        assert possibly(rig, p, x) and possibly(rig, x, p)


def test_two_calls_on_one_street_with_the_pages_first(tmp_path):
    """Two pages' incidents on the street (a second call there): the feed's numbered event joins neither, noted on both."""
    rig = Rig(tmp_path)
    p = rig.page("p1", PAGE_NO_UNITS)["id"]
    rig.at(30)
    q = rig.page("p2", PAGE_NO_UNITS, fresh=True)["id"]
    assert q != p
    rig.at(45)
    a = feed_event(rig, units=("E60",), dt=20)
    assert a not in (p, q) and len(open_incidents(rig)) == 3
    for x in (p, q):
        assert possibly(rig, a, x) and possibly(rig, x, a)


def test_a_street_only_page_on_another_street_stays_separate(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, units=("E62",))
    rig.at(30)
    p = rig.page("p1", PAGE_ST.replace("Example Terrace", "Sample Terrace"))["id"]
    assert p != a and not possibly(rig, p, a) and not possibly(rig, a, p)      # no units in common: nothing to say


def test_another_street_with_shared_units_is_noted(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig)
    rig.at(30)
    p = rig.page("p1", PAGE_ST.replace("Example Terrace", "Sample Terrace"))["id"]
    assert p != a and possibly(rig, p, a) and possibly(rig, a, p)


def test_north_is_not_south_and_ext_is_its_own_street(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, location="9 N EXAMPLE AVE")
    rig.at(30)
    p = rig.page("p1", PAGE_ST.replace("Example Terrace", "South Example Avenue"))["id"]
    assert p != a and possibly(rig, p, a)
    rig2 = Rig(tmp_path / "ext")
    b = feed_event(rig2, location="9 EXAMPLE AVE EXT")
    rig2.at(30)
    q = rig2.page("p1", PAGE_ST.replace("Example Terrace", "Example Avenue"))["id"]
    assert q != b and possibly(rig2, q, b)


def test_more_than_five_minutes_apart_is_not_the_street_rule(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, units=("E62",))
    rig.at(6 * 60)
    p = rig.page("p1", PAGE_ST)["id"]
    assert p != a


def test_other_units_on_both_sides_are_noted_not_joined(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, units=("E62",), typ="EMS")
    rig.at(30)
    p = rig.page("p1", PAGE_ST)["id"]                                           # E60, L31 vs E62, another type
    assert p != a and possibly(rig, p, a)


def test_street_only_against_street_only_still_joins(tmp_path):
    """The rule from #304 that already held: two locations with no number on one street are the same place."""
    rig = Rig(tmp_path)
    a = feed_event(rig, location="EXAMPLE TER")
    rig.at(30)
    assert rig.page("p1", PAGE_ST)["id"] == a and len(open_incidents(rig)) == 1


def test_a_repeat_that_now_names_the_street_moves_the_page_onto_the_feeds_incident(tmp_path):
    """The first reading had no location (a garbled street): its own card. The repeat says the street: the page joins
    the feed's incident, its own untouched card is dismissed, never two cards."""
    rig = Rig(tmp_path)
    a = feed_event(rig, units=("E62",))
    rig.at(20)
    first = rig.page("p1", "Port Chester Fire Department, respond to a CO alarm. Time out 1432.")["id"]
    assert first != a
    rig.at(50)
    got = rig.page("p2", PAGE_NO_UNITS)
    assert got["id"] == a
    assert rig.cad.store.incident(first)["status"] == "dismissed" and len(open_incidents(rig)) == 1
    rig.at(70)
    assert rig.page("p3", PAGE_NO_UNITS)["id"] == a                             # a later repeat: still the feed's
    assert len(open_incidents(rig)) == 1


def test_a_repeat_never_dismisses_a_card_a_person_worked(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, units=("E62",))
    rig.at(20)
    first = rig.page("p1", "Port Chester Fire Department, respond to a CO alarm. Time out 1432.")["id"]
    rig.cad.store.update_incident(first, {"remarks": "caller on the line"}, operator="Lt. Test")
    rig.cad.sources.mark_manual(first, ["location"], "Lt. Test")
    rig.at(50)
    got = rig.page("p2", PAGE_NO_UNITS)
    assert got["id"] == first and rig.cad.store.incident(first)["status"] == "queued"
    assert possibly(rig, first, a) and possibly(rig, a, first)
    rig.at(60)
    rig.page("p3", PAGE_NO_UNITS)
    assert sum(x.startswith("Possibly the same as") for x in rig.feed_lines(first)) == 1     # noted once


def test_pcfd_mode_attaches_and_enriches_but_dispatches_nothing(pcfd):
    a = feed_event(pcfd)
    pcfd.at(28)
    assert pcfd.page("p1", PAGE_ST)["id"] == a and len(open_incidents(pcfd)) == 1
    full = pcfd.cad.full(a)
    assert full["status"] == "queued" and not full["assignments"]
    assert "Units named on the page: Engine 60, Ladder 31" in lines(pcfd, a, "tones")
    assert full["cross_street"] == "Sample Avenue And Specimen Street"


def test_page_named_units_never_move_a_unit_off_another_call(tmp_path):
    rig = AutoRig(tmp_path)
    other = feed_event(rig, "B1", "400 SAMPLE ST", ("L31",), typ="ALARM/COMM")
    assert rig.cad.full(other)["status"] == "active"
    rig.at(5)
    a = feed_event(rig, "A1", "9 EXAMPLE TER", ("E60",), dt=5)
    rig.at(30)
    assert rig.page("p1", PAGE_ST)["id"] == a
    names = {x["name"] for x in rig.cad.full(a)["assignments"] if not x["cleared_at"]}
    assert names == {"E60"} and rig.status("L31") == "DP"                        # L31 stays on its own call
    assert any("L31 on" in x and "not moved" in x for x in lines(rig, a, "auto"))


# -- the feed's types -------------------------------------------------------------------------------------------------


def test_a_comma_type_is_mapped_and_60_control_takes_the_call_and_the_rigs_added_later(tmp_path):
    """A type written "TYPE,SUBTYPE" left the type unset: the call waited for a person, and the rigs the feed listed as
    they responded were never put on it."""
    rig = AutoRig(tmp_path)
    a = feed_event(rig, location="12 EXAMPLE AVE", units=("E64", "TL2"), typ="ALARM,COMM")
    assert rig.cad.full(a)["status"] == "active"
    rig.at(90)
    rig.change(units=("E64", "TL2", "R41"))
    rig.at(150)
    rig.change(units=("E64", "TL2", "R41", "E60"))
    assert {x["name"] for x in rig.cad.full(a)["assignments"]} == {"E64", "TL2", "R41", "E60"}


@pytest.mark.parametrize("raw,want", [
    ("ALARM,COMM", ("ALARM", "COMM")),
    ("ALARM , RESD", ("ALARM", "RESD")),
    ("ALARM/COMM", ("ALARM", "COMM")),
    ("MA,STANDBY", ("MA", "STANDBY")),
    ("TYPEA,SUBB", ("", "")),                                                     # nobody's code: left to a person
])
def test_type_separators(tmp_path, raw, want):
    rig = Rig(tmp_path)
    assert rig.cad.map_type(raw, "County feed")[:2] == want


def test_a_page_for_a_mutual_aid_standby():
    d = parse("Port Chester Fire Department, Engine 60, respond to a mutual aid standby, 400 Sample Street. Time out 1432.")
    assert d.incident_type == "Mutual aid"
    assert runcards.from_page(d.incident_type, d.category, d.text)[:2] == ("MA", "STANDBY")
    assert runcards.from_page("Mutual aid", "service", "mutual aid, fast team")[:2] == ("MA", "")


# -- the page's words -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,location,cross", [
    ("Port Chester Fire Department, Engine 61, Ladder 31, respond to Example Terrace, CO alarm between Sample Avenue "
     "and Specimen Street. Time out 1432.", "example terrace", "sample avenue and specimen street"),
    ("Port Chester respond to Example Terrace for a carbon monoxide alarm between Sample Avenue and Specimen Street. "
     "Time out 1432.", "example terrace", "sample avenue and specimen street"),
    ("Port Chester respond to 12 Example Terrace for a carbon dioxide alarm between Sample Avenue and Specimen Street, "
     "time out 1432", "12 example terrace", "sample avenue and specimen street"),
    ("Example Parkway southbound at the Sample Parkway interchange", "example parkway",
     "sample parkway"),
    ("Port Chester respond to Old Example Road for a C.O. detector activation", "old example road", ""),
])
def test_cross_streets_leave_the_call_type_out(text, location, cross):
    d = parse(text)
    assert (d.location, d.cross_street) == (location, cross)
    assert "alarm" not in d.cross_street and "between" not in d.cross_street


@pytest.mark.parametrize("text", [
    "Port Chester respond to 12 Example Avenue for a carbon dioxide smoke detector activation. Time out 1432.",
    "Port Chester respond to 12 Example Avenue for a carbon monoxide detector activation. Time out 1432.",
    "Port Chester respond to 12 Example Avenue for a C.O. alarm. Time out 1432.",
    "Port Chester respond to 12 Example Avenue for a CO detector. Time out 1432.",
])
def test_carbon_dioxide_is_read_as_a_co_alarm(text):
    assert parse(text).incident_type == "CO alarm"


def test_the_vocabulary_hints_carbon_monoxide():
    got = channels.vocabulary_suggestions({}, [])
    for p in ("carbon monoxide", "carbon monoxide detector activation", "C.O. detector", "C.O. alarm"):
        assert p in got
    sent = build_vocabulary(got)["phrases"]
    assert "C.O.-detector" in sent and "carbon-monoxide-detector-activation" in sent and not build_vocabulary(got)["skipped"]


# -- the feed's comments ----------------------------------------------------------------------------------------------


def test_empty_and_repeated_feed_comments_add_no_lines(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, location="12 EXAMPLE AVE")
    for dt, text in [(10, "checking the second floor"), (11, ""), (12, "   "), (13, "checking the second floor"),
                     (14, "caller reports smoke"), (15, "checking the second floor"), (16, "checking the basement"),
                     (700, "checking the second floor")]:                         # over 10 min after: a line again
        rig.at(dt)
        rig.feed.comment("A1", T0 + dt, text)
        rig.poll()
    got = [x for x in rig.feed_lines(a) if " comment:" in x]
    assert got == ["County feed comment: checking the second floor", "County feed comment: caller reports smoke",
                   "County feed comment: checking the basement", "County feed comment: checking the second floor"]


def test_numbered_addresses_on_one_street_are_not_the_street_rule(tmp_path):
    """Only a report with no house number (or an incident with none) takes the street rule: two numbered addresses on
    one street stay the strict rules' (no join, no note without units in common)."""
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", "9 EXAMPLE TER", ("E62",))
    b = feed_event(rig, "A2", "40 EXAMPLE TER", ("E63",), dt=10)
    rig.at(30)
    p = rig.page("p1", PAGE_ST.replace("Example Terrace", "14 Example Terrace"))["id"]
    assert p not in (a, b) and not possibly(rig, p, a) and not possibly(rig, p, b)


# -- review of #367 ---------------------------------------------------------------------------------------------------

CAR_FIRE = ("Port Chester Fire Department, Engine 61, respond to a car fire, Example Terrace, between Sample Avenue and "
            "Specimen Street. Time out 1432.")


def test_another_type_on_the_street_never_joins_the_feed_first(tmp_path):
    """A car fire paged 90 s after a CO alarm on the same street (no units listed): two calls, noted, and E61 is never
    dispatched to the alarm."""
    rig = AutoRig(tmp_path)
    a = feed_event(rig, units=(), typ="ALARM/RESD")
    rig.at(90)
    p = rig.page("p1", CAR_FIRE)["id"]
    assert p != a and len(open_incidents(rig)) == 2
    assert possibly(rig, p, a) and possibly(rig, a, p)
    assert not [x for x in rig.cad.full(a)["assignments"] if x["name"] == "E61"]


def test_another_type_on_the_street_never_joins_the_page_first(tmp_path):
    rig = Rig(tmp_path)
    p = rig.page("p1", CAR_FIRE)["id"]
    rig.at(60)
    a = feed_event(rig, units=(), typ="ALARM/RESD", dt=50)
    assert a != p and len(open_incidents(rig)) == 2 and possibly(rig, a, p)


def test_the_live_shape_still_joins_a_co_page_and_a_residential_alarm(tmp_path):
    """The page's CO alarm (ALARM, no subtype) and the feed's residential alarm (ALARM/RESD): one top-level type."""
    rig = Rig(tmp_path)
    a = feed_event(rig, typ="ALARM/RESD")
    rig.at(28)
    assert rig.page("p1", PAGE_ST)["id"] == a and len(open_incidents(rig)) == 1


@pytest.mark.parametrize("text,location,cross", [
    ("Port Chester Fire Department, respond to Gas House Lane for an odor of gas. Time out 1432.", "gas house lane", ""),
    ("Port Chester respond to Smoke Rise Road for a smoke detector activation. Time out 1432.", "smoke rise road", ""),
    ("Port Chester respond to Fire House Lane for a fire alarm. Time out 1432.", "fire house lane", ""),
    ("Port Chester respond to 12 Example Avenue for a fire alarm, cross Commercial Avenue and Fire House Lane. "
     "Time out 1432.", "12 example avenue", "commercial avenue and fire house lane"),
    ("Port Chester Fire Department, respond to Example Terrace, co alarm between Sample Avenue and Specimen Street. "
     "Time out 1432.", "example terrace", "sample avenue and specimen street"),
])
def test_road_names_that_look_like_call_types_are_kept(text, location, cross):
    d = parse(text)
    assert (d.location, d.cross_street) == (location, cross)


@pytest.mark.parametrize("text,want", [
    ("Port Chester respond to 12 Example Avenue for a fire alarm, carbon dioxide system discharge. Time out 1432.",
     "Fire alarm"),
    ("Port Chester respond to 12 Example Avenue for a fire alarm, smoke and carbon monoxide detectors sounding. "
     "Time out 1432.", "Fire alarm"),
    ("Port Chester respond to 12 Example Avenue for a carbon monoxide smoke detector activation. Time out 1432.",
     "CO alarm"),
    ("Port Chester respond to 12 Example Avenue for a CO detector activation. Time out 1432.", "CO alarm"),
    ("Port Chester respond to 12 Example Avenue for a carbon monoxide detector. Time out 1432.", "CO alarm"),
])
def test_co_against_fire_alarm(text, want):
    assert parse(text).incident_type == want


def test_a_repeat_never_dismisses_an_upgraded_card(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, units=("E62",))
    rig.at(20)
    first = rig.page("p1", "Port Chester Fire Department, respond to a CO alarm. Time out 1432.")["id"]
    with rig.cad.store.tx() as db:
        rig.cad.store._event(db, first, "upgrade", "Upgraded", "Lt. Test")
    rig.at(50)
    assert rig.page("p2", PAGE_NO_UNITS)["id"] == first
    assert rig.cad.store.incident(first)["status"] == "queued" and possibly(rig, first, a)


def test_the_page_never_sets_cross_streets_on_a_location_a_person_set(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig)
    rig.cad.sources.mark_manual(a, ["location"], "Lt. Test")
    rig.at(28)
    assert rig.page("p1", PAGE_ST)["id"] == a
    assert not rig.cad.store.incident(a)["cross_street"]


# -- a doubled house number ("17, 17 Example Lane" read as "1717") ------------------------------------------------------

DOUBLED = ("Port Chester Fire Department, Engine 60, respond to 1717 Example Lane between Sample Road and the dead end, a "
           "residential alarm. Time out 1432.")


def test_halved():
    from sizeup.cad.sources import halved, same_place_halved
    assert halved("1717 Example Lane") == "17 Example Lane" and halved("4545 EXAMPLE LN") == "45 EXAMPLE LN"
    assert halved("1212 Example Ln Apt 3") == "12 Example Ln"
    assert halved("1718 Example Lane") is None and halved("17 Example Lane") is None and halved("0101 Example Ln") is None
    assert same_place_halved("1717 Example Lane", "17 EXAMPLE LN") and same_place_halved("17 EXAMPLE LN", "1717 Example Lane")
    assert not same_place_halved("1717 Example Lane", "17 SAMPLE LN") and not same_place_halved("17 Example Ln", "17 Example Ln")


def test_a_doubled_number_page_joins_the_feed_incident(tmp_path):
    rig = AutoRig(tmp_path)
    a = feed_event(rig, location="17 EXAMPLE LN", units=("E60",), typ="ALARM/RESD")
    rig.at(30)
    assert rig.page("p1", DOUBLED)["id"] == a and len(open_incidents(rig)) == 1
    assert rig.cad.store.incident(a)["address"] == "17 EXAMPLE LN"


def test_a_doubled_number_page_first_then_the_feed_joins_it(tmp_path):
    rig = Rig(tmp_path)
    p = rig.page("p1", DOUBLED)["id"]
    rig.at(40)
    assert feed_event(rig, location="17 EXAMPLE LN", units=("E60",), typ="ALARM/RESD", dt=20) == p


def test_a_real_1717_is_guarded_by_type_and_window(tmp_path):
    """Both 17 and 1717 exist on the street: another type, or over 5 min apart, is a note, never a join; an open
    incident at 1717 as written takes the page, never the half."""
    rig = Rig(tmp_path)
    a = feed_event(rig, location="17 EXAMPLE LN", units=("E60",), typ="MVA")
    rig.at(30)
    p = rig.page("p1", DOUBLED)["id"]
    assert p != a and possibly(rig, p, a) and possibly(rig, a, p)

    rig2 = Rig(tmp_path / "late")
    b = feed_event(rig2, location="17 EXAMPLE LN", units=("E60",), typ="ALARM/RESD")
    rig2.at(6 * 60)
    q = rig2.page("p1", DOUBLED)["id"]
    assert q != b and possibly(rig2, q, b)

    rig3 = Rig(tmp_path / "both")
    c17 = feed_event(rig3, "A1", "17 EXAMPLE LN", ("E60",), typ="ALARM/RESD")
    c1717 = feed_event(rig3, "A2", "1717 EXAMPLE LN", ("E60",), typ="ALARM/RESD", dt=10)
    rig3.at(30)
    assert rig3.page("p1", DOUBLED)["id"] == c1717 != c17


@pytest.mark.parametrize("accepted", [True, False])
def test_an_untouched_page_card_left_queued_is_swept_when_the_feed_closes_the_call(tmp_path, accepted):
    rig = Rig(tmp_path)
    a = feed_event(rig, location="17 EXAMPLE LN", units=("E60",), typ="ALARM/RESD")
    if accepted:
        rig.cad.store.accept(a, "Lt. Test")
    # a card the page made before the rule (or that never joined): queued, untouched
    p = rig.cad.store.create_incident(type="ALARM", address="1717 Example Lane", source="60 Control page",
                                      source_ref="monitor:old", status="queued")
    rig.cad.sources._put("paging", "old", 1000, "Paging", T0 + 30, {"location": "1717 Example Lane"}, incident_id=p)
    worked = rig.cad.store.create_incident(type="ALARM", address="1717 Example Lane", source="60 Control page",
                                           source_ref="monitor:worked", status="queued")
    rig.cad.sources._put("paging", "worked", 1000, "Paging", T0 + 40, {"location": "1717 Example Lane"}, incident_id=worked)
    with rig.cad.store.tx() as db:
        rig.cad.store._event(db, worked, "note", "caller called back", "Lt. Test")
    rig.at(600)
    rig.feed.change("A1", times={"in_service": T0 + 590}, closed=True)
    rig.poll()
    assert rig.cad.store.incident(p)["status"] == "dismissed"
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'paging' AND ref = 'old'")["incident_id"] == a
    assert rig.cad.store.incident(worked)["status"] == "queued"                  # a card a person worked: left alone
