"""Local address points (sizeup/geo/localaddr.py): addresses the map data lacks, pinned in Settings > Lists > Address
points (cad.sqlite section "local_addresses"), found like the gazetteer's own points everywhere an address is looked up:
the place search, the location box, a page, a map click and the common places import.

Invented data only (this repository is public): a synthetic gazetteer of invented streets in invented towns."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.cad import commonplaces as cp
from sizeup.cad.store import CadError
from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.geo.localaddr import HouseNumber, fractions_in, parse_number, set_local_addresses, split_address
from sizeup.geo.pagegeo import locate_page, what_is_here
from sizeup.geo.places import PlaceIndex
from tests.test_cad import make_cad
from tests.test_settings import post

EX, SA = "Exampleton", "Sampleville"
LAT, LON = 41.0, -73.70
BEHIND = (41.0004, -73.6993)           # the building behind 189, in its parking lot
STATION = (41.0051, -73.6890)          # the station on Sample St


def synthetic(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(1, "example st", "Example St"), (2, "sample st", "Sample St"),
                                                           (3, "test ave", "Test Ave")])
    # Example St: 181 ... 199 odd, east along LAT
    addrs = [(1, n, "", EX, LAT + 0.0001, LON + (n - 181) * 0.00005, 1) for n in range(181, 200, 2)]
    # Sample St: 1, 5, 7 (no 3), north along LON + 0.011
    addrs += [(2, n, "", EX, LAT + 0.004 + n * 0.0002, -73.6890, 1) for n in (1, 5, 7)]
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)", addrs)
    lines = [(1, EX, [[LON - 0.001, LAT], [LON + 0.002, LAT]]),
             (2, EX, [[-73.6890, LAT + 0.003], [-73.6890, LAT + 0.007]]),
             (3, SA, [[-73.68, LAT + 0.01], [-73.67, LAT + 0.01]])]
    db.executemany("INSERT INTO lines VALUES (?,?,?)", [(s, m, json.dumps(c)) for s, m, c in lines])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", [(1, 2, LAT, -73.6890, EX)])
    db.commit()
    db.close()
    return path


@pytest.fixture()
def gaz(tmp_path):
    return Gazetteer(synthetic(tmp_path / "synthetic.sqlite"))


@pytest.fixture()
def cad(tmp_path, gaz):
    c = make_cad(tmp_path, geocoder=lambda q: [])
    c.place_source = lambda: PlaceIndex(gaz)
    set_local_addresses(gaz, c.local_addresses)
    return c


def pin(cad, address, at, **more):
    return post(cad, "local_addresses", action="set", address=address, lat=at[0], lon=at[1], **more)


# -- the house number helper -------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["189-1/2", "189 1/2", "189½", "189.5", "189 - 1/2"])
def test_every_way_of_writing_a_half_is_one_number(text):
    assert parse_number(text) == HouseNumber(189, "", "1/2")
    assert parse_number(text).label == "189 1/2"


def test_numbers_letters_and_what_is_not_one():
    assert parse_number("189A") == HouseNumber(189, "a", "")
    assert parse_number("23.5") == HouseNumber(23, "", "1/2")
    assert parse_number("7¾") == HouseNumber(7, "", "3/4")
    for bad in ("", "0", "23.3", "12th", "Example", "189-191"):
        assert parse_number(bad) is None
    assert split_address("189-1/2 Example St") == (HouseNumber(189, "", "1/2"), "Example St", "189-1/2")
    assert split_address("12-14 Example St") is None              # a range: not this helper's
    assert split_address("12 1st Ave")[0] == HouseNumber(12)


def test_fractions_in_free_text_are_tied_to_the_street_after_them(gaz):
    text, fr = fractions_in("Fire at 189-1/2 Example St, 23.5 Sample St", gaz.streets_at)
    ex, sa = gaz.street_id("Example St"), gaz.street_id("Sample St")
    assert text == "Fire at 189 Example St, 23 Sample St" and fr == {(189, ex): "1/2", (23, sa): "1/2"}


@pytest.mark.parametrize("text", ["I-287 EB MM 10.5 eastbound", "10.5 min ago on Example St", "rt 9.5 Example St",
                                  "16.5 north of the bridge", "Box 12.25 Sample St", "3 1/2 story frame",
                                  "3.5 min out", "units 2.5 miles away", "189½, Example St"])
def test_a_decimal_or_fraction_that_is_no_house_number_is_left_alone(gaz, text):
    assert fractions_in(text, gaz.streets_at) == (text, {})


# -- the gazetteer ---------------------------------------------------------------------------------------

def rows(*pts):
    return tuple({"number": n, "street": s, "muni": m, "lat": a[0], "lon": a[1], "note": ""} for n, s, m, a in pts)


def test_a_local_point_is_exact_and_wins_over_interpolation(gaz):
    before = gaz.geocode("3 Sample St")
    assert before.precision == "interpolated"
    set_local_addresses(gaz, lambda: rows(("3", "Sample St", EX, STATION)))
    after = gaz.geocode("3 Sample St")
    assert (after.precision, after.lat, after.lon) == ("exact", *STATION)


@pytest.mark.parametrize("typed", ["189-1/2 Example St", "189 1/2 Example St", "189½ Example St", "189.5 Example St"])
def test_a_fractional_number_finds_its_point_not_the_integer_part(gaz, typed):
    set_local_addresses(gaz, lambda: rows(("189-1/2", "Example St", EX, BEHIND)))
    m = gaz.geocode(typed)
    assert (m.lat, m.lon, m.house_number, m.precision) == (*BEHIND, "189 1/2", "exact")
    assert gaz.geocode("189 Example St").lat != BEHIND[0]          # 189 itself: the address point


def test_without_a_local_point_a_fraction_is_its_integer_part(gaz):
    m = gaz.geocode("189-1/2 Example St")
    assert m.house_number == "189" and m.precision == "exact"


def test_a_street_with_no_address_points_gets_one(gaz):
    assert gaz.geocode("10 Test Ave").kind == "street"
    set_local_addresses(gaz, lambda: rows(("10", "Test Ave", SA, (41.01, -73.675))))
    m = gaz.geocode("10 Test Ave")
    assert (m.kind, m.municipality, m.precision) == ("address", SA, "exact")


def test_rows_with_an_unknown_street_or_number_are_skipped(gaz):
    set_local_addresses(gaz, lambda: rows(("3", "Nowhere Rd", EX, STATION), ("x", "Sample St", EX, STATION)))
    assert not gaz.local_addresses()
    set_local_addresses(gaz, lambda: 1 / 0)                         # a broken source: the address points as before
    assert gaz.geocode("3 Sample St").precision == "interpolated"


def test_a_fraction_elsewhere_in_the_text_never_hides_a_local_point(gaz):
    set_local_addresses(gaz, lambda: rows(("3", "Sample St", EX, STATION)))
    best = locate_page(gaz, "Structure fire 3 Sample St, 3 1/2 story frame").best.match
    assert (best.lat, best.lon, best.precision) == (*STATION, "exact")
    m = gaz.geocode("3 Sample St, 3.5 min out")
    assert (m.lat, m.lon, m.precision) == (*STATION, "exact")


def test_a_fraction_not_pinned_takes_the_local_integer_point_next(gaz):
    set_local_addresses(gaz, lambda: rows(("3", "Sample St", EX, STATION), ("10", "Test Ave", SA, (41.01, -73.675))))
    m = gaz.geocode("3-1/2 Sample St")
    assert (m.lat, m.lon, m.house_number, m.precision) == (*STATION, "3", "approximate")
    m = gaz.geocode("10-1/2 Test Ave")                       # a street with no address points: the local 10
    assert (m.kind, m.lat, m.house_number, m.precision) == ("address", 41.01, "10", "approximate")


def test_a_local_point_beats_the_gazetteers_own_exact_point(gaz):
    set_local_addresses(gaz, lambda: rows(("189", "Example St", EX, BEHIND)))
    m = gaz.geocode("189 Example St")
    assert (m.lat, m.precision) == (BEHIND[0], "exact")


def test_a_misheard_street_keeps_the_fraction_for_the_street_the_page_picks(gaz):
    set_local_addresses(gaz, lambda: rows(("189-1/2", "Example St", EX, BEHIND)))
    best = locate_page(gaz, "Engine 58 respond 189-1/2 Exampel Street odor").best.match
    assert (best.lat, best.lon, best.house_number) == (*BEHIND, "189 1/2")
    # the number said twice: never another 189's fraction
    assert fractions_in("189 Sample St, 189-1/2 Exampel St", gaz.streets_at)[1] == {}


def test_number_before_the_house_number_keeps_the_fraction(gaz):
    set_local_addresses(gaz, lambda: rows(("189-1/2", "Example St", EX, BEHIND)))
    assert gaz.geocode("number 189 1/2 Example St").lat == BEHIND[0]


def test_a_page_reads_the_fraction(gaz):
    set_local_addresses(gaz, lambda: rows(("189-1/2", "Example St", EX, BEHIND)))
    best = locate_page(gaz, "Engine respond to 189-1/2 Example Street for an odor").best.match
    assert (best.lat, best.lon, best.house_number) == (*BEHIND, "189 1/2")


def test_a_map_click_finds_the_local_point(gaz):
    set_local_addresses(gaz, lambda: rows(("189-1/2", "Example St", EX, BEHIND)))
    got = what_is_here(gaz, BEHIND[0] + 0.00002, BEHIND[1])
    assert got["address"]["label"] == f"189 1/2 Example St, {EX}"


def test_the_place_search_offers_it_tagged_local(gaz):
    set_local_addresses(gaz, lambda: rows(("189-1/2", "Example St", EX, BEHIND)))
    ix = PlaceIndex(gaz)
    hit = ix.search("189-1/2 example st")[0]
    assert (hit["label"], hit["source"], hit["precision"], hit["lat"]) == ("189 1/2 Example St", "local", "exact", BEHIND[0])
    labels = [h["label"] for h in ix.search("189 example")]
    assert labels[:2] == ["189 Example St", "189 1/2 Example St"]     # the point, then the one behind it


# -- Settings ------------------------------------------------------------------------------------------

def test_settings_pin_edit_remove_audited(cad, gaz):
    r = pin(cad, "189-1/2 Example Street", BEHIND, note="rear building")
    key = r["key"]
    assert key == f"189 1/2 example st, {EX.lower()}"
    page = cad.settings_get()["local_addresses"]
    assert page["items"] == [{"key": key, "number": "189-1/2", "street": "Example St", "town": EX, "lat": BEHIND[0],
                              "lon": BEHIND[1], "note": "rear building"}]
    assert gaz.geocode("189½ Example St").lat == BEHIND[0]
    # moved: the same key, the new spot
    pin(cad, "189-1/2 Example St", (BEHIND[0] + 0.0001, BEHIND[1]), key=key)
    assert gaz.geocode("189 1/2 Example St").lat == round(BEHIND[0] + 0.0001, 6)
    # renumbered: the old key goes
    r = pin(cad, "189A Example St", BEHIND, key=key)
    assert [i["key"] for i in cad.settings_get()["local_addresses"]["items"]] == [r["key"]]
    post(cad, "local_addresses", action="delete", key=r["key"])
    assert cad.settings_get()["local_addresses"]["items"] == []
    log = [a for a in cad.settings.audit_log("lists") if a["key"].startswith("address/")]
    assert len(log) == 5 and not log[0]["new"]                       # set, moved, renumbered (two), deleted


def test_settings_refuses_what_it_cannot_place(cad):
    with pytest.raises(CadError, match="house number"):
        pin(cad, "Example St", BEHIND)
    with pytest.raises(CadError, match="not in the map data"):
        pin(cad, "3 Nowhere Rd", STATION)
    with pytest.raises(CadError, match="over 400 m"):
        pin(cad, "3 Sample St", (40.9, -73.8))
    with pytest.raises(CadError, match="town: Sample St is in"):
        pin(cad, "3 Sample St", STATION, town=SA)
    with pytest.raises(CadError, match="pin"):
        post(cad, "local_addresses", action="set", address="3 Sample St")
    pin(cad, "3 Sample St", STATION)
    with pytest.raises(CadError, match="already pinned"):
        pin(cad, "3 Sample Street", STATION)


def test_on_a_street_through_two_towns_the_pin_is_on_the_typed_towns_stretch(tmp_path):
    db = sqlite3.connect(synthetic(tmp_path / "two.sqlite"))
    db.execute("INSERT INTO lines VALUES (1, ?, ?)", (SA, json.dumps([[LON + 0.008, LAT], [LON + 0.02, LAT]])))
    db.commit()
    db.close()
    g = Gazetteer(tmp_path / "two.sqlite")
    c = make_cad(tmp_path, geocoder=lambda q: [])
    c.place_source = lambda: PlaceIndex(g)
    set_local_addresses(g, c.local_addresses)
    with pytest.raises(CadError, match=f"over 400 m from Example St in {SA}"):
        pin(c, "189-1/2 Example St", BEHIND, town=SA)                 # on the street, but in the other town
    assert pin(c, "301 Example St", (LAT, LON + 0.015), town=SA)["key"] == f"301 example st, {SA.lower()}"
    assert pin(c, "303 Example St", (LAT, LON + 0.015))["key"] == f"303 example st, {SA.lower()}"


def test_the_town_comes_from_the_pin_or_the_address(cad):
    assert pin(cad, "10 Test Ave", (41.01, -73.675))["key"] == f"10 test ave, {SA.lower()}"
    assert pin(cad, "12 Test Ave, Sampleville", (41.01, -73.675))["key"] == f"12 test ave, {SA.lower()}"


def test_the_cad_search_and_location_box_see_it(cad):
    pin(cad, "3 Sample St", STATION)
    hits = cad.search_places({"q": ["3 sample st"]})["places"]
    assert hits[0]["lat"] == STATION[0] and hits[0]["source"] == "local"


def test_exported_and_imported_with_the_rest(cad, tmp_path, gaz):
    pin(cad, "189-1/2 Example St", BEHIND)
    data = cad.settings_export()
    assert list(data["overrides"]["local_addresses"]) == [f"189 1/2 example st, {EX.lower()}"]
    bad = json.loads(json.dumps(data))
    pts = bad["overrides"]["local_addresses"]
    pts["x"] = {"number": "x", "street": "Example St", "muni": EX, "lat": 41, "lon": -73.7}           # no number
    pts["3 nowhere rd, exampleton"] = {"number": "3", "street": "Nowhere Rd", "muni": EX, "lat": 41, "lon": -73.7}
    pts["3 sample st, sampleville"] = {"number": "3", "street": "Sample St", "muni": SA, "lat": STATION[0],
                                       "lon": STATION[1]}                                          # not its town
    pts["5 sample st, exampleton"] = {"number": "5", "street": "Sample St", "muni": EX, "lat": 40.9, "lon": -73.8}
    pts["9 sample st, exampleton"] = {"number": "7", "street": "Sample St", "muni": EX, "lat": STATION[0],
                                      "lon": STATION[1]}                                           # key not its value's
    pts["1 sample st, exampleton"] = {"number": "1", "street": "Sample St", "muni": EX, "lat": STATION[0],
                                      "lon": STATION[1], "note": 7}
    dry = post(cad, "import", action="dry_run", settings=bad)
    assert "address points: 6 skipped (not placeable here)" in dry["notes"]
    post(cad, "local_addresses", action="delete", key=f"189 1/2 example st, {EX.lower()}")
    post(cad, "import", action="apply", settings=bad)
    assert [i["key"] for i in cad.settings_get()["local_addresses"]["items"]] == [f"189 1/2 example st, {EX.lower()}"]


def test_the_common_places_import_takes_the_local_point_first(cad, gaz):
    ix = cad.place_index()
    assert cp.locate_address(ix, "3 SAMPLE ST", EX)["precision"] == "interpolated"
    pin(cad, "3 Sample St", STATION)
    pin(cad, "189-1/2 Example St", BEHIND)
    got = cp.locate_address(ix, "3 SAMPLE ST", EX)
    assert (got["lat"], got["precision"], got["muni"]) == (STATION[0], "exact", EX)
    assert cp.locate_address(ix, "189 1/2 EXAMPLE ST", EX)["lat"] == BEHIND[0]
    assert cp.locate_address(ix, "189 1/2 EXAMPLE ST", SA)["lat"] == BEHIND[0]    # the one town that has it
