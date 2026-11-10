"""#365: a page with a street and no house number joins the feed's incident on that street, and a page on a feed's incident
enriches it (its clip, transcript, tones, units and cross streets) instead of making a card of its own. Also the feed's
"TYPE,SUBTYPE" types, Mutual aid / Standby, the cross-street parser, "carbon dioxide" as CO and the feed's comments.
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


def feed_event(rig, eid="A1", location="9 EXAMPLE TER", units=("E60", "L31"), typ="ALARM,CO-RESD", dt=0.0):
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
    assert (inc["type"], inc["subtype"]) == ("ALARM", "CO-RESD")                 # "ALARM,CO-RESD" mapped
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
    """Seen live: "TYPE,SUBTYPE" left the type unset, so the call waited for a person and the rigs the feed listed as
    they responded were never put on it."""
    rig = AutoRig(tmp_path)
    a = feed_event(rig, location="12 EXAMPLE AVE", units=("E64", "TL2"))
    assert rig.cad.full(a)["status"] == "active"
    rig.at(90)
    rig.change(units=("E64", "TL2", "R41"))
    rig.at(150)
    rig.change(units=("E64", "TL2", "R41", "E60"))
    assert {x["name"] for x in rig.cad.full(a)["assignments"]} == {"E64", "TL2", "R41", "E60"}


@pytest.mark.parametrize("raw,want", [
    ("ALARM,CO-RESD", ("ALARM", "CO-RESD")),
    ("ALARM, CO-COMM", ("ALARM", "CO-COMM")),
    ("MA,STANDBY", ("MA", "STANDBY")),
    ("MUTUAL AID,STANDBY", ("MA", "STANDBY")),
    ("MUTUAL AID/STANDBY", ("MA", "STANDBY")),
    ("ALARM/COMM", ("ALARM", "COMM")),
])
def test_feed_types(tmp_path, raw, want):
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
    for i, text in enumerate(["** status check by 101", "", "   ", "** status check by 101", "caller reports smoke",
                              "** status check by 101", "** status check by 202"]):
        rig.at(10 + i)
        rig.feed.comment("A1", T0 + 10 + i, text)
        rig.poll()
    got = [x for x in rig.feed_lines(a) if " comment:" in x]
    assert got == ["County feed comment: ** status check by 101", "County feed comment: caller reports smoke",
                   "County feed comment: ** status check by 202"]


def test_numbered_addresses_on_one_street_are_not_the_street_rule(tmp_path):
    """Only a report with no house number (or an incident with none) takes the street rule: two numbered addresses on
    one street stay the strict rules' (no join, no note without units in common)."""
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", "9 EXAMPLE TER", ("E62",))
    b = feed_event(rig, "A2", "40 EXAMPLE TER", ("E63",), dt=10)
    rig.at(30)
    p = rig.page("p1", PAGE_ST.replace("Example Terrace", "14 Example Terrace"))["id"]
    assert p not in (a, b) and not possibly(rig, p, a) and not possibly(rig, p, b)
