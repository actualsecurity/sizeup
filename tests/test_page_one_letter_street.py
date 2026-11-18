"""A page whose street speech-to-text heard one letter off the county feed's (sizeup/cad/pagematch.py one_letter): the
feed made the incident first, an alarm at a business; the page said the house number exactly, the street with one
letter wrong, the business one letter wrong, a cross street the map doesn't know, then read the address again
misheard as another street, and named only a station (no apparatus). Before: its own card, "possibly the same" and a
"Merge into #N" a person had to tap (a misheard street was weak at most; the strict "misheard" rule needs apparatus in
common). Now it joins, but only on the feed's street one edit away with the map holding no other street that close,
the number exact, the same family of call and the only open county call; every #684 guard still holds.

Synthetic only: made-up streets, businesses, numbers and times."""

from __future__ import annotations

import pytest

from sizeup.cad import pagematch
from sizeup.cad import sources as src
from sizeup.dispatch import parse
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import Rig
from tests.test_page_feed_score import paging_on, state_inc, units_on
from tests.test_street_only import feed_event, open_incidents, possibly

FEED_AT = "47 SAMPLEFORD AVE : @EXAMPLE BISTRO"
PAGE = ("Port Chester Fire Department, 47 Samplefird Avenue. Exmple bistro. Specimum Avenue dead end, commercial "
        "alarm. 47 Modelton Avenue. Exampel bistro, Specimum Avenue dead end, commercial alarm. Station 14.")
MAP = {"port chester": ["Sampleford Ave", "Modelton St", "Specimen Pl", "Patternwood Ave"],
       "rye brook": ["Exampleridge Rd"], "greenwich": ["Samplefird Ave"]}      # (another town's: not ours)


@pytest.fixture(autouse=True)
def streets():
    """The gazetteer's street list (Cad.street_source), restored after."""
    old = src.STREETS
    src.set_streets(MAP)
    yield src.set_streets
    src.STREETS = old


def scenario(rig, page=PAGE, at=FEED_AT, typ="ALARM/COMM", dt=60, units=("PCFD",)):
    a = feed_event(rig, "A1", at, units, typ=typ)
    rig.at(dt)
    return a, rig.page("p1", page)["id"]


# -- the pieces ------------------------------------------------------------------------------------------------------


def test_one_edit():
    one = pagematch.one_edit
    assert one("samplefird", "sampleford") and one("sampleford", "samplefod")      # a letter wrong, one dropped
    assert one("sampel", "sample") and one("sampleforrd", "sampleford")            # two swapped, one doubled
    assert not one("sampleford", "sampleford")                                     # the same: not misheard
    assert not one("samplfird", "sampleford") and not one("modelton", "sampleford")
    assert not one("elm", "elk")                                                    # short: exact only


def test_the_repeat_of_the_address_is_never_a_cross_street():
    d = parse(PAGE)
    assert d.location == "47 samplefird avenue"
    assert "modelton" not in d.cross_street                     # "47 Modelton Avenue": the address read again
    assert d.cross_street.startswith("specimum avenue")
    keep = parse("Port Chester Fire Department, 47 Sampleford Avenue, cross of Specimen Place and Patternwood "
                 "Avenue, commercial alarm.")
    assert keep.cross_street == "specimen place and patternwood avenue"


# -- the scenario --------------------------------------------------------------------------------------------------


def test_the_scenario_joins_the_feeds_incident(tmp_path):
    rig = Rig(tmp_path)
    a, got = scenario(rig)
    assert got == a and [i["id"] for i in open_incidents(rig)] == [a] and paging_on(rig) == a
    lines = rig.feed_lines(a)
    assert not any(x.startswith("Possibly the same") for x in lines)
    line = next(x for x in lines if " matched by: " in x)
    assert "street (one letter misheard)" in line and "the only open county call" in line
    assert "house number" in line and "type family" in line
    inc = rig.cad.store.incident(a)
    assert inc["address"].upper().startswith("47 SAMPLEFORD AVE")                # the feed's location stands


def test_the_scenario_in_60_control_mode_puts_nothing_of_the_page_elsewhere(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("60control")
    a, got = scenario(rig)
    assert got == a and len(open_incidents(rig)) == 1


def test_the_page_first_then_the_feed_sweeps_its_card(tmp_path):
    rig = Rig(tmp_path)
    rig.poll()
    p = rig.page("p1", PAGE)["id"]
    rig.at(40)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="ALARM/COMM", dt=-20)
    assert a != p and rig.cad.store.incident(p)["status"] == "dismissed" and paging_on(rig) == a


def test_the_maps_cross_streets_known_and_none_in_common_still_count(tmp_path):
    """A cross street the map knows, none in common with the feed's: against, as before (offered only)."""
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="ALARM/COMM")
    rig.cad.store.update_incident(a, {"cross_street": "Patternwood Ave"}, operator="test", log=False)
    rig.at(60)
    p = rig.page("p1", PAGE.replace("Specimum Avenue", "Specimen Place"))["id"]
    assert p != a and "not joined: other cross streets" in state_inc(rig, p)["merge_offer"]["why"]


def test_a_cross_street_the_map_does_not_know_is_no_evidence(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="ALARM/COMM")
    rig.cad.store.update_incident(a, {"cross_street": "Patternwood Ave"}, operator="test", log=False)
    rig.at(60)
    assert rig.page("p1", PAGE)["id"] == a


# -- never two different calls ---------------------------------------------------------------------------------------


def test_another_real_street_one_letter_away_never_joins(tmp_path, streets):
    """Two streets of ours one letter apart, both on the map: the page names the other one exactly. Its own call."""
    streets({**MAP, "port chester": [*MAP["port chester"], "Samplefird Ave"]})
    rig = Rig(tmp_path)
    a, p = scenario(rig)
    assert p != a and paging_on(rig) == p and len(open_incidents(rig)) == 2
    assert "merge_offer" not in state_inc(rig, p)


def test_a_third_street_as_close_to_the_heard_one_never_joins(tmp_path, streets):
    """The heard street one letter from the feed's and one from another street of ours: a person picks."""
    streets({**MAP, "port chester": [*MAP["port chester"], "Samplefind Rd"]})
    rig = Rig(tmp_path)
    a, p = scenario(rig)
    assert p != a and len(open_incidents(rig)) == 2


def test_no_street_list_never_joins_on_a_misheard_street(tmp_path, streets):
    streets({})
    rig = Rig(tmp_path)
    a, p = scenario(rig)
    assert p != a and len(open_incidents(rig)) == 2


def test_two_letters_off_is_offered_never_joined(tmp_path):
    rig = Rig(tmp_path)
    a, p = scenario(rig, PAGE.replace("Samplefird", "Samplefurdd"))
    assert p != a and state_inc(rig, p)["merge_offer"]["into"] == a


@pytest.mark.parametrize("n", ["48", "46", "147", "4747"])
def test_another_house_number_never_joins(tmp_path, n):
    """The house next door, a digit more, said twice: a misheard street needs the number exactly."""
    rig = Rig(tmp_path)
    a, p = scenario(rig, PAGE.replace("47 Samplefird", f"{n} Samplefird"))
    assert p != a and len(open_incidents(rig)) == 2


def test_another_kind_of_call_never_joins(tmp_path):
    rig = Rig(tmp_path)
    a, p = scenario(rig, typ="MVA")
    assert p != a and len(open_incidents(rig)) == 2


def test_a_type_with_no_family_never_joins(tmp_path):
    rig = Rig(tmp_path)
    a, p = scenario(rig, typ="NOTIF")
    assert p != a and len(open_incidents(rig)) == 2


def test_two_open_county_calls_never_join(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="ALARM/COMM")
    b = feed_event(rig, "A2", "9 PATTERNWOOD AVE", ("PCFD",), typ="ALARM/RESD", dt=30)
    rig.at(90)
    p = rig.page("p1", PAGE)["id"]
    assert p not in (a, b) and "another open county call" in state_inc(rig, p)["merge_offer"]["why"]


def test_a_stale_feed_never_joins(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="ALARM/COMM")
    rig.feed.outage("backing_off", "HTTP 429")
    rig.poll()
    rig.at(60)
    p = rig.page("p1", PAGE)["id"]
    assert p != a and "not joined: the county feed not fresh" in state_inc(rig, p)["merge_offer"]["why"]


def test_another_town_never_joins(tmp_path):
    rig = Rig(tmp_path)
    a, p = scenario(rig, PAGE.replace("commercial alarm. 47", "commercial alarm in Rye Brook. 47"))
    assert p != a and "merge_offer" not in state_inc(rig, p)


def test_mutual_aid_never_joins(tmp_path):
    rig = AutoRig(tmp_path)
    a, p = scenario(rig, PAGE.replace("Station 14.", "Station 14, mutual aid."))
    assert p != a and "merge_offer" not in state_inc(rig, p) and possibly(rig, p, a)


def test_a_chains_store_elsewhere_named_without_our_number_never_joins(tmp_path):
    rig = Rig(tmp_path)
    rig.cad.local_places = lambda: [
        {"key": "cp:a", "name": "EXAMPLE BISTRO", "aliases": [], "address": "47 SAMPLEFORD AVE", "muni": "port chester",
         "label": "Example Bistro"},
        {"key": "cp:b", "name": "EXAMPLE BISTRO", "aliases": [], "address": "300 PATTERNWOOD AVE",
         "muni": "port chester", "label": "Example Bistro"}]
    a, p = scenario(rig, PAGE.replace("47 Samplefird", "Samplefird").replace("Exmple bistro", "Example Bistro"))
    assert p != a


def test_another_of_our_places_on_the_feeds_street_named_is_only_offered(tmp_path):
    """The page's street misheard: our places are looked up on the feed's street, as spelled."""
    rig = Rig(tmp_path)
    rig.cad.local_places = lambda: [{"key": "cp:m", "name": "MODEL DINER", "aliases": [], "address": "90 SAMPLEFORD AVE",
                                     "muni": "port chester", "label": "Model Diner"}]
    a, p = scenario(rig, PAGE.replace("Exmple bistro", "Model Diner"))
    assert p != a and "not joined: names Model Diner" in state_inc(rig, p)["merge_offer"]["why"]


def test_over_5_min_after_the_dispatch_is_offered(tmp_path):
    rig = Rig(tmp_path)
    a, p = scenario(rig, dt=7 * 60)
    assert p != a and state_inc(rig, p)["merge_offer"]["into"] == a


def test_the_mutual_aid_page_in_another_town_keeps_its_units_off_our_call(tmp_path):
    rig = AutoRig(tmp_path)
    a, p = scenario(rig, PAGE.replace("Station 14.", "Engine 60, mutual aid to the City of Rye."))
    assert p != a and "E60" not in units_on(rig, a)


# -- the decision on its own -----------------------------------------------------------------------------------------


def test_decide_one_letter():
    ok = dict(street="snapped", one_letter=True, feed_numbered=True, fresh=True, number="exact", family="same")
    d = lambda **kw: pagematch.decide([pagematch.Evidence(inc={"id": 1}, **{**ok, **kw})])
    assert d().decision == "strong" and d().why[0] == "street (one letter misheard)"
    assert "street one letter" in d().kinds()
    assert d(one_letter=False).decision == "weak"                       # misheard further: a person decides
    for kw in (dict(number="doubled"), dict(number="misread"), dict(number="near"), dict(family=""),
               dict(family="differ"), dict(fresh=False), dict(taken=True), dict(lag_s=6 * 60), dict(cross="differ"),
               dict(units="differ"), dict(feed_numbered=False), dict(elsewhere="Example Bistro")):
        assert d(**kw) is None or d(**kw).decision == "weak", kw
    assert d(town="differ") is None and d(mutual="differ") is None and d(number="other") is None
    two = pagematch.decide([pagematch.Evidence(inc={"id": 1}, **ok), pagematch.Evidence(inc={"id": 2})])
    assert two.decision == "weak"
