"""Offline geocoder: normalisation, real-data geocodes and a small inline fixture."""

from __future__ import annotations

import json
import math
import sqlite3

import pytest

from sizeup.geo.gazetteer import DEFAULT_DB, SCHEMA, Gazetteer
from sizeup.geo.normalize import street_key, tokenize


def dist_m(lat1, lon1, lat2, lon2):
    dy = (lat1 - lat2) * 110_574
    dx = (lon1 - lon2) * 111_320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------- normalisation

@pytest.mark.parametrize("text, expected", [
    ("one two three Westchester Avenue", ["123", "westchester", "ave"]),
    ("twenty two Grace Church Street", ["22", "grace", "church", "st"]),
    ("one twenty three King Street", ["123", "king", "st"]),
    ("two hundred nine Westchester Ave", ["209", "westchester", "ave"]),
    ("five oh nine Willett Avenue", ["509", "willett", "ave"]),
    ("nine forty King St.", ["940", "king", "st"]),
    ("fourteen thirty two", ["1432"]),
    ("one hundred and five Poningo", ["105", "poningo"]),
    ("51-53 Grace Church St", ["51", "grace", "church", "st"]),
    ("1-2-3 West Chester Av", ["123", "west", "chester", "ave"]),
    ("twenty first street", ["21st", "st"]),
    ("Second Street", ["2nd", "st"]),
    ("I-95 northbound", ["i", "95", "northbound"]),
    ("two eighty seven eastbound", ["i", "287", "eastbound"]),
    ("N. Main St. & Adee Boulevard", ["n", "main", "st", "|", "and", "adee", "blvd"]),
    ("time out 14:32", ["time", "out", "1432"]),
])
def test_tokenize_spoken_numbers_and_types(text, expected):
    assert tokenize(text) == expected


@pytest.mark.parametrize("name, key", [
    ("N Main St", "n main st"),
    ("North Main Street", "n main st"),
    ("Westchester Avenue", "westchester ave"),
    ("Interstate 95", "i 95"),
    ("I- 95", "i 95"),
    ("Hutchinson River Pkwy", "hutchinson pkwy"),
    ("North St", "north st"),
    ("Mount Holly Drive", "mt holly dr"),
])
def test_street_key(name, key):
    assert street_key(name) == key


# --------------------------------------------------------------------------- real data

@pytest.fixture(scope="module")
def gaz():
    if not DEFAULT_DB.exists():
        pytest.skip("data/gazetteer.sqlite missing: run tools/fetch_addresses.py")
    return Gazetteer()


def test_data_provenance(gaz):
    sources = json.loads(gaz.meta["sources"])
    names = " ".join(s["name"] for s in sources)
    assert "NYS Address Points" in names and "TIGER" in names
    assert all(s.get("license") and s.get("fetched") for s in sources)
    counts = json.loads(gaz.meta["counts"])
    assert counts["addresses_by_muni"]["Port Chester"] > 4000
    assert counts["addresses_by_muni"]["Rye Brook"] > 2500


def test_exact_address_pcfd_headquarters(gaz):
    m = gaz.geocode("209 Westchester Ave")
    assert m and m.kind == "address" and m.precision == "exact"
    assert m.label == "209 Westchester Ave, Port Chester"
    assert m.municipality == "Port Chester"
    assert dist_m(m.lat, m.lon, 41.00253, -73.66692) < 30
    assert m.confidence >= 0.9


def test_spoken_address_rye_brook_firehouse(gaz):
    m = gaz.geocode("nine forty King Street Rye Brook")
    assert m and m.kind == "address"
    assert m.municipality == "Rye Brook" and m.house_number == "940"
    assert dist_m(m.lat, m.lon, 41.03513, -73.67464) < 30


def test_intersection_king_and_arbor(gaz):
    m = gaz.geocode("King Street and Arbor Drive Rye Brook")
    assert m and m.kind == "intersection"
    assert {m.street, m.cross_street} == {"King St", "Arbor Dr"}
    assert m.municipality == "Rye Brook"
    # OpenStreetMap has Arbor Drive meeting King Street at 41.03390, -73.67308
    assert dist_m(m.lat, m.lon, 41.03390, -73.67308) < 60
    # bare names, no street types, still resolve because the two streets meet
    m2 = gaz.geocode("King and Arbor")
    assert m2 and m2.kind == "intersection" and dist_m(m.lat, m.lon, m2.lat, m2.lon) < 5


def test_intersection_phrasings(gaz):
    for text in ("corner of North Main Street and Adee Street", "N Main St at Adee St",
                 "Adee Street & North Main Street"):
        m = gaz.geocode(text)
        assert m and m.kind == "intersection", text
        assert {m.street, m.cross_street} == {"N Main St", "Adee St"}, text
        assert m.municipality == "Port Chester"
    m = gaz.geocode("Westchester Avenue cross of Grove Street")
    assert m and m.kind == "intersection" and m.cross_street == "Grove St"


def test_landmark_high_school(gaz):
    m = gaz.geocode("Port Chester High School")
    assert m and m.kind == "landmark"
    assert "High School" in m.label
    # county facility point for PCHS, 1 Tamarack Rd
    assert dist_m(m.lat, m.lon, 41.01174, -73.67889) < 100


def test_landmark_firehouse(gaz):
    m = gaz.geocode("Brooksville firehouse")
    assert m and m.kind == "landmark"
    assert dist_m(m.lat, m.lon, 41.01177, -73.65962) < 30


@pytest.mark.parametrize("text, street, number", [
    ("two zero nine west chester av", "Westchester Ave", "209"),
    ("respond to sixty eight Pening Go Street", "Poningo St", "68"),
    ("one five Webber Drive", "Weber Dr", "15"),
    ("fifty seven grace church street", "Grace Church St", "57"),
    ("two two five King Street", "King St", "225"),
])
def test_fuzzy_street_names(gaz, text, street, number):
    m = gaz.geocode(text)
    assert m and m.kind == "address", text
    assert m.street == street and m.house_number == number
    assert m.municipality == "Port Chester"


def test_interpolated_address(gaz):
    st = next(s for s in gaz.streets.values() if s.name == "Westchester Ave")
    nums = st.nums["Port Chester"]
    rows = st.addrs["Port Chester"]
    have = set(nums)
    # a missing odd number with existing odd neighbours on both sides
    missing = next(n for n in range(101, 501, 2) if n not in have and n - 2 in have and n + 2 in have)
    m = gaz.geocode(f"{missing} Westchester Avenue")
    assert m and m.kind == "address" and m.precision == "interpolated"
    below = next(r for r in rows if r[0] == missing - 2)
    above = next(r for r in rows if r[0] == missing + 2)
    assert dist_m(m.lat, m.lon, (below[2] + above[2]) / 2, (below[3] + above[3]) / 2) < 5
    exact = gaz.geocode("209 Westchester Avenue")
    assert m.confidence < exact.confidence


def test_cross_street_resolves_duplicate_street(gaz):
    # 51 Grace Church St exists in Port Chester (South End firehouse) and in the City of Rye
    pc = gaz.geocode("51 Grace Church Street cross of Central Avenue")
    assert pc.municipality == "Port Chester" and pc.cross_street == "Central Ave"
    assert dist_m(pc.lat, pc.lon, 40.99600, -73.66453) < 40
    plain = gaz.geocode("51 Grace Church Street")
    assert pc.confidence > plain.confidence
    rye = gaz.geocode("51 Grace Church Street, City of Rye")
    assert rye.municipality == "Rye" and dist_m(rye.lat, rye.lon, pc.lat, pc.lon) > 1000


def test_find_in_full_page(gaz):
    page = ("Port Chester Fire Department, Engine 4, Ladder 1, respond to a reported "
            "structure fire at two zero nine Westchester Avenue, cross of Pearl Street, "
            "time out one four three two")
    m = gaz.find_in_text(page)
    assert m and m.kind == "address" and m.house_number == "209"
    assert m.cross_street in ("Pearl St", "N Pearl St")
    assert m.confidence >= 0.95
    page2 = ("Rye Brook Fire Department, alarm activation, Blind Brook High School, "
             "eight forty King Street, time out zero nine one five")
    m2 = gaz.find_in_text(page2)
    assert m2 and m2.label == "840 King St, Rye Brook"


def test_time_and_unit_numbers_are_not_house_numbers(gaz):
    m = gaz.find_in_text("Engine four respond to Poningo Street, time out one four three two")
    assert m is None or m.house_number is None
    m = gaz.find_in_text("box twelve King Street and Westchester Avenue")
    assert m and m.kind == "intersection"


@pytest.mark.parametrize("text", [
    "the quick brown fox jumps over the lazy dog",
    "Port Chester Fire Department, Engine 4, Ladder 1, respond for a medical assist, time out 1432",
    "Rye Brook units, carbon monoxide alarm, caller states the detector is going off",
    "Battalion 14 to 60 control, all units back in service",
    "",
])
def test_no_location(gaz, text):
    assert gaz.find_in_text(text) is None


def test_street_only_is_low_confidence(gaz):
    m = gaz.geocode("Poningo Street")
    assert m and m.kind == "street" and m.confidence < 0.5
    assert m.municipality == "Port Chester"


# --------------------------------------------------------------------------- inline fixture

@pytest.fixture()
def tiny(tmp_path):
    """Two fixture streets in made-up places; exercises interpolation without real data."""
    path = tmp_path / "tiny.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)",
                   [(1, "alpha st", "Alpha St"), (2, "beta ave", "Beta Ave")])
    rows = [(1, 11, 41.0000, -73.7000), (1, 31, 41.0010, -73.7000),
            (1, 10, 41.0000, -73.7001), (1, 40, 41.0030, -73.7001)]
    db.executemany("INSERT INTO addresses VALUES (?,?,'', 'Testville', ?, ?, 1)", rows)
    db.execute("INSERT INTO lines VALUES (2, 'Testville', ?)",
               (json.dumps([[-73.7005, 41.0005], [-73.6995, 41.0005]]),))
    db.execute("INSERT INTO lines VALUES (1, 'Testville', ?)",
               (json.dumps([[-73.7000, 40.9995], [-73.7000, 41.0035]]),))
    db.execute("INSERT INTO intersections VALUES (1, 2, 41.0005, -73.7000, 'Testville')")
    db.commit()
    db.close()
    return Gazetteer(path)


def test_fixture_interpolation_same_parity(tiny):
    m = tiny.geocode("fifteen Alpha Street")
    assert m.precision == "interpolated" and m.house_number == "15"
    # odd side: 11 -> 31, 15 is 20 % of the way
    assert abs(m.lat - 41.0002) < 1e-6 and abs(m.lon + 73.7000) < 1e-6
    exact = tiny.geocode("eleven Alpha Street")
    assert exact.precision == "exact" and exact.confidence > m.confidence


def test_fixture_intersection_and_outside_range(tiny):
    m = tiny.geocode("Alpha Street and Beta Avenue")
    assert m.kind == "intersection" and (m.lat, m.lon) == (41.0005, -73.7)
    assert tiny.geocode("nine thousand Alpha Street").kind == "street"
