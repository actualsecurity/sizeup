"""A house-number range ("14-16 Sample Ave": one building numbered at both ends) end to end (sizeup/geo/houserange.py):
the page parser (written and spoken), the feed's and console's address, the gazetteer and the page locator, the page
joining the feed's incident (#684 exact-number rules, #807 two towns), History here, premise notes and pre-plans, the
radio log and the dispatch voice.

Invented data only (this repository is public): made-up streets, towns, numbers and times. The shapes are the county
feed's (upper case, a town token after the street) and a 60 Control page's (a department, a type, the address, a time
out), as the other tests here use them."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.cad import addrkey, pagematch
from sizeup.cad import sources as src
from sizeup.cad.dispatcher import speech, where
from sizeup.cad.radiolog import address_hit
from sizeup.cad.speech import words
from sizeup.dispatch import parse
from sizeup.geo import houserange as hr
from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.geo.pagegeo import locate_page
from tests.test_address_history import ALIASES, call_at, fake_geocode, hist  # noqa: F401 - the fixture
from tests.test_incident_sources import Rig
from tests.test_street_only import feed_event, open_incidents

PAGE = "Port Chester Fire Department, Engine 60, Ladder 31, respond to an activated fire alarm. {at}. Time out 2214."


# -- the range itself ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("14-16 SAMPLE AVE", "14-16"),
    ("14 - 16 SAMPLE AVE", "14-16"),
    ("101-103 EXAMPLE ST", "101-103"),
    ("149-51 EXAMPLE ST", "149-151"),             # the end's last digits
    ("199-01 EXAMPLE ST", "199-201"),             # ... into the next hundred
    ("14-16A SAMPLE AVE", "14-16A"),
    ("respond to 14 to 16 Sample Avenue", "14-16"),
    ("respond to 14 through 16 Sample Avenue", "14-16"),
    ("respond to fourteen to sixteen Sample Avenue", "14-16"),
    ("respond to forty dash forty-two Sample Street", "40-42"),
    ("respond to forty-forty-two Sample Street", "40-42"),
    ("respond to one forty nine to fifty one Example Street", "149-151"),
])
def test_a_range_written_or_said(text, want):
    [f] = hr.find(text)
    assert f.range.text == want


@pytest.mark.parametrize("text", [
    "10-200 SAMPLE AVE",                          # a block, never one building
    "16-14 SAMPLE AVE",                           # backwards
    "15-16 SAMPLE AVE",                           # two sides of the street
    "14-14 SAMPLE AVE",
    "914-555-0100 SAMPLE AVE",                    # a phone number
    "call back 555-0100 Sample Avenue",
    "189-1/2 EXAMPLE ST",                         # a fraction
    "I-95 NB EXIT 9-10 SAMPLE AVE",               # a highway's exit
    "exit 10-12 Sample Avenue",
    "I-95/287 SAMPLE AVE",
    "Engine 14 to 16 Sample Avenue",              # an apparatus
    "14 to 16 people",                            # said, but no street after it
    "1.5-3 miles Sample Avenue",
    "16-10-191-22, time out",                     # 60 Control's run number
])
def test_not_a_range(text):
    assert hr.find(text) == []


@pytest.mark.parametrize("a, b, same", [
    ("16", "14-16", True), ("14", "14-16", True), ("14-16", "16", True),
    ("16", "14-18", True),                        # inside it, on its side
    ("18", "14-16", False),                       # the house past it
    ("15", "14-16", False),                       # across the street
    ("3", "1-3", True), ("5", "1-3", False),
    ("14-16", "14-18", True), ("14-16", "16-18", False),
    ("100", "10-200", False),                     # no range: two numbers
    ("16a", "14-16A", True), ("12", "12", True), ("12", "14", False),
])
def test_same_number(a, b, same):
    assert hr.same_number(a, b) is same


# -- the page parser ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("said", ["14-16 Sample Avenue", "14 to 16 Sample Avenue", "14 through 16 Sample Avenue",
                                  "fourteen to sixteen Sample Avenue", "fourteen dash sixteen Sample Avenue",
                                  "14 - 16 Sample Avenue"])
def test_the_page_keeps_the_range(said):
    d = parse(PAGE.format(at=f"{said}, cross of Example Street"))
    assert d.location == "14-16 sample avenue"
    assert d.numbers == []                        # never 14 read as an engine
    assert d.cross_street == "example street" and d.time_out == "22:14"
    assert d.units == ["Engine 60", "Ladder 31"]


def test_the_page_never_reads_a_span_or_an_exit_as_a_range():
    assert parse(PAGE.format(at="10-200 Sample Avenue")).location == "200 sample avenue"   # as before
    assert "Engine 14" in parse("Engine 14 to 16 Sample Avenue for a fire alarm").units
    d = parse("Port Chester Fire Department, car fire, I-95 northbound exit 9-10 Sample Avenue. Time out 2214.")
    assert d.location.startswith("I-95 NB")


# -- the feed's and the console's address ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, shown", [
    ("14-16 SAMPLE AVE PORT_CHESTER", "14-16 Sample Ave, Port Chester"),
    ("14 - 16 SAMPLE AVE APT 2 PORT_CHESTER", "14-16 Sample Ave Apt 2, Port Chester"),
    ("101-103 EXAMPLE ST : @SAMPLE DINER PORT_CHESTER", "101-103 Example St (Sample Diner), Port Chester"),
    ("@SAMPLE SCHOOL (14-16 SAMPLE AVE) PORT_CHESTER", "14-16 Sample Ave, Port Chester"),
    ("10-200 SAMPLE AVE PORT_CHESTER", "10 Sample Ave, Port Chester"),     # no range: its first number, as before
])
def test_the_address_shows_the_range_as_given(raw, shown):
    assert src.checked_location(raw, src.normal_location(raw, typed=True)) == shown


@pytest.mark.parametrize("a, b, same", [
    ("16 Sample Avenue", "14-16 SAMPLE AVE", True),
    ("14 Sample Avenue", "14-16 SAMPLE AVE", True),
    ("14-16 sample avenue", "16 SAMPLE AVE", True),
    ("18 Sample Avenue", "14-16 SAMPLE AVE", False),
    ("15 Sample Avenue", "14-16 SAMPLE AVE", False),
    ("5 Sample Ave", "1-3 SAMPLE AVE", False),
    ("16 Example Ave", "14-16 SAMPLE AVE", False),
    ("100 Sample Ave", "10-200 SAMPLE AVE", False),
])
def test_same_place(a, b, same):
    assert src.same_place(a, b) is same


def test_number_fit_and_heard_place():
    assert [pagematch.number_fit(h, "14-16") for h in ("16", "14", "1616", "18", "15", "")] == \
        ["exact", "exact", "other", "near", "near", ""]
    assert pagematch.number_fit("1616", "14-16", "16") == "doubled"


def test_a_misheard_street_with_either_end_may_join():
    """heard_place (#365): the misheard street's number against the range's (a digit in or out of either end)."""
    from tests.test_page_fuzzy_match import DISTRICT_STREETS
    old = src.STREETS
    src.set_streets(DISTRICT_STREETS)
    try:
        D = ("Port Chester", "Rye Brook")
        for heard, want in (("16 Samplewerth Place", "num"), ("14 Samplewerth Place", "num"),
                            ("160 Samplewerth Place", "num"), ("18 Samplewerth Place", "other"),
                            ("40 Samplewerth Place", "other")):
            assert src.heard_place(heard, "14-16 SAMPLEWORTH PL", D) == want, heard
    finally:
        src.STREETS = old


def test_two_towns_runs_of_one_range_are_one_place():
    assert src.cross_town_place("14-16 SAMPLE AVE", "16 SAMPLE AVE") in ("same", "near")
    assert src.cross_town_place("14-16 SAMPLE AVE", "18 SAMPLE AVE") == ""


# -- the page joins the feed's incident ------------------------------------------------------------------------------

@pytest.mark.parametrize("said", ["16 Sample Avenue", "14 Sample Avenue", "14 to 16 Sample Avenue",
                                  "fourteen to sixteen Sample Avenue"])
def test_a_page_for_either_end_joins_the_feeds_range(tmp_path, said):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", "14-16 SAMPLE AVE PORT_CHESTER", ("E60", "L31"), typ="ALARM/COMM")
    assert rig.cad.store.incident(a)["address"] == "14-16 Sample Ave, Port Chester"
    rig.at(60)
    got = rig.page("p1", PAGE.format(at=said))
    assert got["id"] == a and [i["id"] for i in open_incidents(rig)] == [a]


@pytest.mark.parametrize("feed, said", [("14-16 SAMPLE AVE PORT_CHESTER", "18 Sample Avenue"),
                                        ("1-3 SAMPLE AVE PORT_CHESTER", "5 Sample Avenue"),
                                        ("14-16 SAMPLE AVE PORT_CHESTER", "16 Example Avenue"),
                                        ("10-200 SAMPLE AVE PORT_CHESTER", "100 Sample Avenue")])
def test_a_page_for_another_house_never_joins(tmp_path, feed, said):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", feed, ("E60", "L31"), typ="ALARM/COMM")
    rig.at(60)
    got = rig.page("p1", PAGE.format(at=said))
    assert got["id"] != a and len(open_incidents(rig)) == 2


def test_the_feeds_range_after_a_page_for_one_end_joins_it(tmp_path):
    rig = Rig(tmp_path)
    p = rig.page("p1", PAGE.format(at="16 Sample Avenue"))
    rig.at(60)
    a = feed_event(rig, "A1", "14-16 SAMPLE AVE PORT_CHESTER", ("E60", "L31"), typ="ALARM/COMM", dt=0)
    assert a == p["id"] and len(open_incidents(rig)) == 1


# -- the gazetteer and the page locator --------------------------------------------------------------------------------

TOWN, LAT, LON = "Exampleton", 41.0, -73.70


def _gaz(path):
    """Sample Ave: even numbers 10 to 24 east along LAT, 5 m apart a number, but no 18; Example St crosses it."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(1, "sample ave", "Sample Ave"), (2, "example st", "Example St")])
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)",
                   [(1, n, "", TOWN, LAT, LON + n * 0.00006, 1) for n in range(10, 25, 2) if n != 18])
    lines = [(1, TOWN, [[LON, LAT - 0.00005], [LON + 0.003, LAT - 0.00005]]),
             (2, TOWN, [[LON + 0.0003, LAT - 0.002], [LON + 0.0003, LAT + 0.002]])]
    db.executemany("INSERT INTO lines VALUES (?,?,?)", [(s, m, json.dumps(c)) for s, m, c in lines])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", [(1, 2, LAT - 0.00005, LON + 0.0003, TOWN)])
    db.commit()
    db.close()
    return Gazetteer(path)


@pytest.fixture()
def gaz(tmp_path):
    return _gaz(tmp_path / "ranges.sqlite")


def lon_of(n):
    return LON + n * 0.00006


def test_the_gazetteer_places_a_range_at_its_ends_points(gaz):
    m = gaz.geocode(f"14-16 Sample Ave, {TOWN}")
    assert (m.kind, m.label, m.house_number, m.precision) == ("address", f"14-16 Sample Ave, {TOWN}", "14-16", "exact")
    assert m.lon == pytest.approx((lon_of(14) + lon_of(16)) / 2, abs=1e-6)              # between both ends
    m = gaz.geocode(f"16-18 Sample Ave, {TOWN}")                                        # no 18: 16's own point
    assert m.precision == "exact" and m.lon == pytest.approx(lon_of(16), abs=1e-6) and m.label.startswith("16-18 ")
    m = gaz.geocode(f"18-20 Sample Ave, {TOWN}")                                        # no 18: 20's, never no point
    assert m.precision == "exact" and m.lon == pytest.approx(lon_of(20), abs=1e-6) and m.label.startswith("18-20 ")
    m = gaz.geocode(f"10-200 Sample Ave, {TOWN}")                                       # no range: 10, as before
    assert m.house_number == "10" and m.lon == pytest.approx(lon_of(10), abs=1e-6)


@pytest.mark.parametrize("said", ["14-16 Sample Avenue", "14 to 16 Sample Avenue", "fourteen to sixteen Sample Avenue"])
def test_the_page_locator_reads_a_range(gaz, said):
    g = locate_page(gaz, PAGE.format(at=said)).as_geo()
    assert g["label"] == f"14-16 Sample Ave, {TOWN}"
    assert g["lon"] == pytest.approx((lon_of(14) + lon_of(16)) / 2, abs=1e-6)


# -- the address keys: History here, premise notes, pre-plans --------------------------------------------------------

def test_the_key_of_a_range_is_its_own_and_one_building_with_its_numbers():
    k = addrkey.key_text("14-16 SAMPLE ST PORT_CHESTER", ALIASES, geocode=fake_geocode)
    assert k.bldg_key == "14-16|sample st|port chester" and k.label == "14-16 Sample St, Port Chester"
    assert addrkey.key_text("149-51 SAMPLE ST", ALIASES).bldg_key.startswith("149-151|")
    assert addrkey.key_text("10-200 SAMPLE ST", ALIASES).bldg_key.startswith("10|")      # no range
    for n, same in (("14", True), ("16", True), ("18", False), ("15", False)):
        assert addrkey.same_building(k.bldg_key, f"{n}|sample st|port chester") is same
    assert not addrkey.same_building(k.bldg_key, "16|sample st|other town")
    assert addrkey.same_building("14-18|sample st|port chester", "16|sample st|port chester")


def test_history_here_joins_a_range_and_every_number_in_it(hist):
    c, clock = hist, hist.clock_

    def pt(n):
        return {"lat": 41.01 + n * 0.00009, "lon": -73.67}
    rng = call_at(c, clock, 9, "14-18 SAMPLE ST PORT_CHESTER", **pt(14))
    mid = call_at(c, clock, 8, "16 Sample St", **pt(16))
    past = call_at(c, clock, 7, "20 Sample St", **pt(20))
    across = call_at(c, clock, 6, "15 Sample St", **pt(15))
    now = c.store.create_incident(type="ALARM", subtype="COMM", address="16 Sample St", **pt(16))
    c.addrs.catch_up(transcripts=False)                                   # every call keyed
    r = c.store.incident(rng)
    assert r["bldg_key"] == "14-18|sample st|port chester"
    ids = {x["id"] for x in c.addrs.building(r["bldg_key"], r["lat"], r["lon"], r["addr_street"])}
    assert mid in ids and not {past, across} & ids
    inc = c.store.incident(now)
    ids = {x["id"] for x in c.addrs.building(inc["bldg_key"], inc["lat"], inc["lon"], inc["addr_street"])}
    assert {rng, mid} <= ids and not {past, across} & ids


def test_a_premise_note_at_either_end_shows_on_a_range_and_back(hist):
    c = hist
    n16 = c.premise.create({"where": "16 Sample St", "text": "Knox box", "category": "access"})["note"]
    n20 = c.premise.create({"where": "20 Sample St", "text": "Dog", "category": "animals"})["note"]
    iid = c.store.create_incident(type="STRU", subtype="RESD", address="14-16 Sample St")
    assert [n["id"] for n in c.addrs.here(iid)["notes"]] == [n16["id"]]
    nr = c.premise.create({"where": "14-16 Sample St", "text": "Standpipe", "category": "access"})["note"]
    one = c.store.create_incident(type="STRU", subtype="RESD", address="14 Sample St")
    assert {n["id"] for n in c.addrs.here(one)["notes"]} == {nr["id"]}
    assert n20["id"] not in {n["id"] for n in c.addrs.here(one)["notes"]}


def test_building_rows_for_pre_plans():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE preplan_points (id INTEGER PRIMARY KEY, bldg_key TEXT, active INTEGER)")
    keys = ["14-16|sample st|exampleton", "16|sample st|exampleton", "18|sample st|exampleton",
            "16|sample st|othertown", "14-16|sample st|exampleton|bldg 2", "12-16|sample st|exampleton"]
    db.executemany("INSERT INTO preplan_points (bldg_key, active) VALUES (?, 1)", [(k,) for k in keys])

    def q(sql, args=()):
        return [dict(r) for r in db.execute(sql, args)]

    def got(k):
        return {r["bldg_key"] for r in addrkey.building_rows(q, "preplan_points", k)}
    assert got("16|sample st|exampleton") == {"14-16|sample st|exampleton", "16|sample st|exampleton",
                                              "12-16|sample st|exampleton"}
    assert got("14-16|sample st|exampleton") == {"14-16|sample st|exampleton", "16|sample st|exampleton",
                                                 "12-16|sample st|exampleton"}
    assert got("18|sample st|exampleton") == {"18|sample st|exampleton"}
    assert got("") == set()


# -- the radio log and the dispatch voice ------------------------------------------------------------------------------

def test_the_radio_log_hears_either_end():
    assert address_hit("Engine 60 on location 16 Sample Avenue", "14-16 Sample Ave, Port Chester")
    assert address_hit("Engine 60 on location 14 Sample Avenue", "14-16 Sample Ave, Port Chester")
    assert not address_hit("Engine 60 on location 18 Sample Avenue", "14-16 Sample Ave, Port Chester")


@pytest.mark.parametrize("written, said", [
    ("14-16 Sample Ave", "Fourteen to sixteen Sample Ave"),
    ("149-51 Example St", "One forty-nine to one fifty-one Example St"),
    ("14-16A Sample Ave", "Fourteen to sixteen A Sample Ave"),
])
def test_the_voice_says_to(written, said):
    assert words(written) == said


def test_the_dispatch_says_the_range_and_the_street_in_full():
    w = where({"address": "14-16 N Sample Ave, Port Chester"})
    assert w == "14-16 North Sample Avenue"
    assert speech(w) == "Fourteen to sixteen North Sample Avenue"
