"""A 60 Control page whose street speech-to-text misheard joins the County CAD Times incident for the same call
(sizeup/cad/fuzzy.py, sizeup/cad/sources.py heard_place): a misspelled street, a house number with a digit too many,
another type and run-together engine numbers. Guards: an exactly-right street with another number, a known street in
another district town, no street list, a missing direction, another town, two near streets, another house with
nothing shared, a department entry, halves that aren't apparatus, the 5 min window for exact matches. Synthetic only: made-up streets, numbers, codes and times."""

from __future__ import annotations

import pytest

from sizeup.cad import fuzzy
from sizeup.cad import sources as src
from sizeup.dispatch import parse, split_joined
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0, Rig
from tests.test_radiolog_units import depts, scope_of
from tests.test_street_only import open_incidents, possibly


def gas_page(where: str = "220 Samplewerth Place", units: str = "Engine 6158, Engine 59/63") -> str:
    return f"Port Chester Fire Department, {units}, respond to a gas leak, {where}. Time out 1436."


def feed(rig, eid="A1", location="20 SAMPLEWORTH PL, RYE BROOK", units=("E58", "E61", "L30", "DPTA"),
         typ="ODOR/OUTSIDE", dt=0.0):
    rig.feed.put(eid, type=typ, location=location, units=units, times={"dispatched": T0 + dt})
    rig.poll()
    return rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'county' AND ref = ?",
                             (eid,))["incident_id"]


DISTRICT_STREETS = {"rye brook": ["Sampleworth Pl", "Examplewood Ave", "Sampleton St", "Exampleridges St", "S Exampleton Ave"],
                    "port chester": ["Specimen St", "Exampleridge St"], "greenwich": ["Sampleworth Pl"]}


@pytest.fixture(autouse=True)
def streets():
    """The gazetteer's street list (Cad.street_source): the district's made-up streets unless a test sets its own;
    restored after."""
    old = src.STREETS
    src.set_streets(DISTRICT_STREETS)
    yield src.set_streets
    src.STREETS = old


def tones(rig, iid: int) -> list[str]:
    return [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = 'tones'", (iid,))]


# -- the pieces ------------------------------------------------------------------------------------------------------


def test_name_sim_folds_doubled_letters_and_sound_alikes():
    assert fuzzy.name_sim("Sampleworth", "SAMPLEWORTH") == 1.0
    assert fuzzy.name_sim("Samplewerth", "Sampleworth") >= fuzzy.STREET_SIM
    assert fuzzy.name_sim("Examplton", "Exampleton") >= fuzzy.STREET_SIM
    assert fuzzy.name_sim("Exampeltton", "Exampleton") >= fuzzy.STREET_SIM          # doubled, transposed
    assert fuzzy.name_sim("Sampleworth", "Specimen") < fuzzy.STREET_SIM
    assert fuzzy.name_sim("Elm", "Elk") == 0.0                                     # short: exact only


def test_misread_house_numbers():
    assert fuzzy.misread("220", "20") and fuzzy.misread("20", "220")              # "two-zero" heard as 220
    assert fuzzy.misread("177", "17") and fuzzy.misread("45", "455")              # a digit repeated, or dropped
    assert not fuzzy.misread("30", "20") and not fuzzy.misread("20", "20")        # another house; the same
    assert not fuzzy.misread("2020", "20") and not fuzzy.misread("020", "20")


def test_heard_place(streets):
    D = ("Port Chester", "Rye Brook")
    assert src.heard_place("220 Samplewerth Place", "20 SAMPLEWORTH PL, RYE BROOK", D) == "num"
    assert src.heard_place("20 Samplewerth Place", "20 SAMPLEWORTH PL", D) == "num"
    assert src.heard_place("2020 Samplewerth Place", "20 SAMPLEWORTH PL", D) == "num"   # doubled, halved
    assert src.heard_place("48 Samplewerth Place", "20 SAMPLEWORTH PL", D) == "other"
    assert src.heard_place("220 Samplewerth Avenue", "20 SAMPLEWORTH PL", D) == ""      # another suffix
    assert src.heard_place("220 Specimen Place", "20 SAMPLEWORTH PL", D) == ""
    assert src.heard_place("220 Sampleworth Place", "20 SAMPLEWORTH PL", D) == ""       # spelled right: strict rules
    assert src.heard_place("120 Exampleridge Street", "120 EXAMPLERIDGES ST, RYE BROOK", D) == ""   # another known street
    assert src.heard_place("100 Examplton Avenue", "100 S EXAMPLETON AVE", D) == "weak"     # a direction missing
    streets({})
    assert src.heard_place("220 Samplewerth Place", "20 SAMPLEWORTH PL", D) == "weak"   # no street list: unsure


def test_snap_to_the_one_known_street(streets):
    streets({"rye brook": ["Sampleworth Pl", "Specimen St"], "port chester": ["Exampleton Ave"]})
    assert src.snapped("220 Samplewerth Place", ("Rye Brook", "Port Chester")) == "220 Sampleworth Pl, Rye Brook"
    assert src.snapped("220 Samplewerth Place", "Port Chester") == ""              # not that town's
    assert src.snapped("220 Sampleworth Pl", "Rye Brook") == ""                    # spelled so already
    streets({"rye brook": ["Sampleworth Pl", "Samplewirth Pl"]})
    assert src.snap_street("220 Samplewerth Place", "Rye Brook")[0] == "ambiguous"
    assert src.snapped("220 Samplewerth Place", "Rye Brook") == ""


def test_run_together_engines_split_only_into_real_apparatus():
    board = ["E58", "E61", "E59", "L30"]
    assert split_joined(["Engine 6158", "Engine 59"], board) == ["Engine 61", "Engine 58", "Engine 59"]
    assert split_joined(["Engine 6171"], board) == ["Engine 6171"]                  # 61 is, 71 isn't
    assert split_joined(["Engine 6158"], ["E61", "E58", "E6158"]) == ["Engine 6158"]   # a unit as written
    assert split_joined(["Engine 6105"], ["E61", "E05", "E610", "E5"]) == ["Engine 6105"]
    assert split_joined(["EMS 6158"], ["EMS61", "EMS58", "E61", "E58"]) == ["EMS 6158"]   # not an apparatus word


def test_engine_numbers_with_a_slash_only_when_the_roster_has_them():
    roster = ["Engine 59", "Engine 61", "Engine 63", "Engine 25"]
    assert parse(gas_page(), roster).units == ["Engine 6158", "Engine 59", "Engine 63"]
    assert parse("Engines 61 / 63 respond, 12 Example Avenue.", roster).units == ["Engine 61", "Engine 63"]
    assert parse(gas_page()).units == ["Engine 6158", "Engine 59"]                 # no roster: never invented
    assert parse("Engine 59/71, respond.", roster).units == ["Engine 59"]          # 71: not a unit


def test_a_slash_before_the_house_number_is_never_a_unit():
    d = parse("Engine 61 / 25 Example Street, gas leak.", ["Engine 61", "Engine 25"])
    assert d.units == ["Engine 61"] and d.location == "25 example street"


# -- the call: the feed first, then the page -------------------------------------------------------------------------


def test_the_misheard_page_after_the_feed_joins_it_and_its_scope_wins(tmp_path):
    rig = AutoRig(tmp_path)
    depts(rig, DPTA="Port Chester FD")                                   # the department entry: never apparatus
    rig.mode("pcfd")
    a = feed(rig)
    rig.at(6 * 60)                                                       # 60 Control pages six minutes on
    got = rig.page("p1", gas_page())
    assert got["id"] == a and len(open_incidents(rig)) == 1             # one incident, no queued card
    inc = rig.cad.store.incident(a)
    assert inc["address"].upper().startswith("20 SAMPLEWORTH PL")       # the feed's location stands
    assert scope_of(rig, a)[:2] == ("Specific units", "page")           # the page's reading wins (#387)
    assert any(x.startswith("60 Control page ") and "Samplewerth" in x for x in tones(rig, a))   # its transcript on it
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'paging'")["incident_id"] == a


def test_the_page_first_then_the_feed_joins_its_card(tmp_path):
    rig = Rig(tmp_path)
    p = rig.page("p1", gas_page())["id"]
    rig.at(40)
    assert feed(rig, dt=-6 * 60) == p                                     # dispatched six minutes before the page
    assert len(open_incidents(rig)) == 1


def test_a_card_left_queued_is_swept_and_enriches_when_the_feed_adds_the_apparatus(tmp_path):
    """The feed's event had one engine when the page came (a note), then the county adds the rest: the card moves, and
    the incident gets what a page that joined it gets (transcript, cross streets), with one line saying so."""
    rig = AutoRig(tmp_path)
    a = feed(rig, units=("E61",))
    rig.at(6 * 60)
    p = rig.page("p1", gas_page(where="220 Samplewerth Place, between Specimen Street and the dead end"))["id"]
    assert p != a and possibly(rig, p, a)
    rig.at(6 * 60 + 30)
    rig.feed.change("A1", location="20 SAMPLEWORTH PL, RYE BROOK (REAR)")      # nothing new for the match
    rig.poll()
    assert rig.cad.store.incident(p)["status"] == "queued"
    rig.at(7 * 60)
    rig.feed.change("A1", units=("E61", "E58", "L30"))
    rig.poll()
    assert rig.cad.store.incident(p)["status"] == "dismissed"
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'paging'")["incident_id"] == a
    assert any(x.startswith("60 Control page ") and "Samplewerth" in x for x in tones(rig, a))
    assert sum(x.startswith("Page card #") and "merged into this" in x for x in rig.feed_lines(a)) == 1
    assert rig.cad.store.incident(a)["cross_street"]


def test_the_page_card_shows_the_gazetteer_street_but_keeps_the_heard_one(tmp_path, streets):
    streets({"port chester": ["Sampleworth Pl", "Specimen St"]})
    rig = Rig(tmp_path)
    p = rig.cad.store.incident(rig.page("p1", gas_page())["id"])
    assert p["address"] == "220 Samplewerth Place"                       # matched as heard
    assert "the map's street: 220 Sampleworth Pl, Port Chester" in p["remarks"]
    assert p["lat"] is None                                              # never a pin by itself


# -- never joined ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed,heard", [("10 EXAMPLEWOOD AVE", "100 Examplewood Avenue"),
                                         ("5 SAMPLETON ST", "50 Sampleton Street"),
                                         ("20 SAMPLEWORTH PL", "220 Sampleworth Place")])
def test_an_exactly_right_street_with_another_number_never_joins(tmp_path, typed, heard):
    """A digit more or less is a misread only when the street was misheard too: here it's another house."""
    rig = Rig(tmp_path)
    a = feed(rig, location=typed, units=("E61", "E58"))
    rig.at(3 * 60)
    p = rig.page("p1", gas_page(where=heard))["id"]
    assert p != a and possibly(rig, p, a)                               # units shared: a note at most


def test_a_known_street_in_another_district_town_never_joins(tmp_path):
    rig = Rig(tmp_path)
    a = feed(rig, location="120 EXAMPLERIDGES ST, RYE BROOK")
    rig.at(60)
    assert rig.page("p1", gas_page(where="120 Exampleridge Street"))["id"] != a


def test_no_street_list_is_a_note_only(tmp_path, streets):
    streets({})
    rig = Rig(tmp_path)
    a = feed(rig)
    rig.at(60)
    p = rig.page("p1", gas_page())["id"]
    assert p != a and possibly(rig, p, a)


def test_a_missing_direction_is_a_note_only(tmp_path):
    rig = Rig(tmp_path)
    a = feed(rig, location="100 S EXAMPLETON AVE")
    rig.at(60)
    p = rig.page("p1", gas_page(where="100 Examplton Avenue"))["id"]
    assert p != a and possibly(rig, p, a)


def test_an_exact_address_keeps_its_5_min_window(tmp_path):
    """PAGE_LAG_S is the misheard rule's only: the same address 7 min after the dispatch, 2 units shared, stays apart."""
    rig = Rig(tmp_path)
    a = feed(rig, location="20 SAMPLEWORTH PL")
    rig.at(7 * 60)
    p = rig.page("p1", gas_page(where="20 Sampleworth Place"))["id"]
    assert p != a


def test_a_similar_street_in_another_town_never_joins(tmp_path):
    assert src.heard_place("220 Samplewerth Place, Rye Brook", "20 SAMPLEWORTH PL, GREENWICH") == ""
    rig = Rig(tmp_path)
    a = feed(rig, location="20 SAMPLEWORTH PL, GREENWICH")
    rig.at(60)
    assert rig.page("p1", gas_page())["id"] != a                         # a PCFD page: the district, not Greenwich


def test_two_open_calls_on_near_streets_tie_and_never_join(tmp_path, streets):
    streets({})
    rig = Rig(tmp_path)
    a = feed(rig, "A1", "20 SAMPLEWORTH PL")
    b = feed(rig, "A2", "20 SAMPLEWIRTH PL", dt=10)
    rig.at(60)
    p = rig.page("p1", gas_page())["id"]
    assert p not in (a, b) and possibly(rig, p, a) and possibly(rig, p, b)


def test_a_street_list_with_two_near_streets_never_joins(tmp_path, streets):
    streets({"rye brook": ["Sampleworth Pl", "Samplewirth Pl"]})
    rig = Rig(tmp_path)
    a = feed(rig)
    rig.at(60)
    p = rig.page("p1", gas_page())["id"]
    assert p != a and possibly(rig, p, a)


def test_the_page_naming_another_known_street_never_joins(tmp_path, streets):
    streets({"rye brook": ["Sampleworth Pl", "Samplewerth Pl"]})          # both are real
    rig = Rig(tmp_path)
    a = feed(rig)
    rig.at(60)
    assert rig.page("p1", gas_page())["id"] != a


def test_another_house_on_that_street_with_nothing_shared_is_a_note_at_most(tmp_path):
    rig = Rig(tmp_path)
    a = feed(rig, location="48 SAMPLEWORTH PL", units=("E60",))
    rig.at(60)
    p = rig.page("p1", gas_page())["id"]
    assert p != a and len(open_incidents(rig)) == 2


def test_one_engine_shared_is_a_note(tmp_path):
    rig = Rig(tmp_path)
    a = feed(rig, units=("E61", "E60"))
    rig.at(60)
    p = rig.page("p1", gas_page())["id"]
    assert p != a and possibly(rig, p, a) and possibly(rig, a, p)


def test_a_department_entry_is_never_apparatus_in_common(tmp_path):
    rig = AutoRig(tmp_path)
    depts(rig, DPTA="Port Chester FD", DPTB="Port Chester FD")
    a = feed(rig, units=("DPTA", "DPTB", "E58"))
    rig.at(60)
    best, nears = rig.cad.sources.match_all(src.PAGING, rig.clock.t, "220 Samplewerth Place", ["DPTA", "DPTB"])
    assert best is None                                                 # two department entries shared: not apparatus
    best, _ = rig.cad.sources.match_all(src.PAGING, rig.clock.t, "220 Samplewerth Place", ["DPTA", "DPTB", "E58", "E62"])
    assert best is None                                                 # one engine: still not enough
    assert a in [n["id"] for n in nears]                                # a note, at most


def test_halves_that_are_not_apparatus_are_never_split(tmp_path):
    rig = Rig(tmp_path)
    a = feed(rig, units=("E61", "E71"))                                 # E71: not on the board
    rig.at(60)
    p = rig.page("p1", gas_page(units="Engine 6171"))["id"]
    assert p != a


def test_strong_units_with_no_street_match_is_a_note(tmp_path):
    rig = Rig(tmp_path)
    a = feed(rig, location="5 SPECIMEN ST")
    rig.at(60)
    p = rig.page("p1", gas_page())["id"]
    assert p != a and possibly(rig, p, a)


def test_too_late_after_the_dispatch_is_a_note(tmp_path):
    rig = Rig(tmp_path)
    a = feed(rig)
    rig.at(9 * 60)
    p = rig.page("p1", gas_page())["id"]
    assert p != a and possibly(rig, p, a)


def test_two_calls_that_both_qualify_tie_and_never_join(tmp_path):
    """20 (a digit dropped) and 220 (as heard) on the street the page misheard, both sharing two engines: a person picks."""
    rig = Rig(tmp_path)
    a = feed(rig, "A1", "20 SAMPLEWORTH PL")
    b = feed(rig, "A2", "220 SAMPLEWORTH PL", dt=10)
    rig.at(60)
    p = rig.page("p1", gas_page())["id"]
    assert p not in (a, b) and possibly(rig, p, a) and possibly(rig, p, b)
