"""The place search (sizeup/geo/places.py, #67): what a note's "@" and the location box suggest.

A small inline gazetteer for the ranking rules, then the real data (data/gazetteer.sqlite)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.geo.gazetteer import DEFAULT_DB, SCHEMA, Gazetteer
from sizeup.geo.places import PlaceIndex, norm_tokens, parse_highway

PC = (41.0016, -73.6657)                 # Port Chester
RB = (41.0270, -73.6870)                 # Rye Brook
GW = (41.0300, -73.6280)                 # Greenwich


def place_row(kind, name, muni, lat, lon, route="", dir_="", value="", category="", source="osm", ref="", aliases=""):
    return (kind, name, category or kind, "", muni, lat, lon, route, dir_, value, source, ref, aliases)


@pytest.fixture()
def ix(tmp_path):
    return PlaceIndex(Gazetteer(fixture_gazetteer(tmp_path / "gaz.sqlite")))


def fixture_gazetteer(path):
    """A gazetteer with a few of every kind (tests/test_cad_places.py uses it too)."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [
        (1, "n main st", "N Main St"), (2, "king st", "King St"), (3, "arbor dr", "Arbor Dr"),
        (4, "n maple ave", "N Maple Ave")])
    db.executemany("INSERT INTO addresses VALUES (?,?,'',?,?,?,1)", [
        (1, 189, "Port Chester", 41.0030, -73.6650), (1, 191, "Port Chester", 41.0031, -73.6650),
        (1, 189, "Greenwich", 41.0400, -73.6200), (2, 287, "Rye Brook", 41.0200, -73.6800),
        (4, 189, "Greenwich", 41.0350, -73.6250)])
    db.execute("INSERT INTO lines VALUES (2, 'Rye Brook', ?)", (json.dumps([[-73.68, 41.01], [-73.68, 41.03]]),))
    db.execute("INSERT INTO lines VALUES (3, 'Rye Brook', ?)", (json.dumps([[-73.68, 41.025], [-73.69, 41.025]]),))
    db.execute("INSERT INTO intersections VALUES (2, 3, 41.0250, -73.6800, 'Rye Brook')")
    db.execute("INSERT INTO landmarks (name, category, address, muni, lat, lon, source, aliases) VALUES "
               "('Lyon Park', 'park', '', 'Port Chester', 41.0010, -73.6640, 'westchester-county', 'lyon park')")
    db.executemany("INSERT INTO places (kind, name, category, address, muni, lat, lon, route, dir, value, source, ref, "
                   "aliases) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        place_row("place", "Oak Park", "Port Chester", *PC, category="park", ref="way/1"),
        place_row("place", "Oak Park", "Rye Brook", *RB, category="park", ref="way/2"),
        place_row("place", "Oak Park", "Greenwich", *GW, category="park", ref="way/3"),
        place_row("place", "Rye Playland", "Rye", 40.9680, -73.6720, category="attraction", ref="way/4",
                  aliases="playland"),
        place_row("milemarker", "I-287 EB MM 10.0", "Rye Brook", 41.0150, -73.6900, "I-287", "EB", "10.0",
                  source="nysdot-rm", ref="287I 8701 1100", aliases="1100"),
        place_row("milemarker", "I-287 WB MM 10.0", "Rye Brook", 41.0151, -73.6902, "I-287", "WB", "10.0",
                  source="nysdot-rm", ref="287I 8701 1100", aliases="1100"),
        place_row("milemarker", "I-287 EB MM 10.1", "Rye Brook", 41.0140, -73.6880, "I-287", "EB", "10.1",
                  source="nysdot-rm", ref="287I 8701 1101"),
        place_row("milemarker", "I-95 MM 14.0", "Rye", 40.9900, -73.6800, "I-95", "", "14.0",
                  source="nysta-mileposts", ref="NE 614.0"),
        place_row("milemarker", "I-287 EB MM 9.1", "Rye Brook", 41.0052, -73.6951, "I-287", "EB", "9.1",
                  source="nysdot-rm", ref="287I 8701 1091", aliases="1091|287i 8701 1091|rye brook town line"),
        place_row("milemarker", "Hutch NB MM 16.6", "Harrison", 41.0223, -73.7102, "Hutch", "NB", "16.6",
                  source="nysdot-lrs", ref="907W 8702 5019", aliases="5019|907w 8702 5019|bridge purchase st"),
        place_row("exit", "Hutch SB exit 19A", "Rye Brook", 41.0350, -73.6750, "Hutch", "SB", "19A",
                  ref="node/9", aliases="old 30s"),
        place_row("exit", "I-95 NB exit 21", "Port Chester", 41.0050, -73.6700, "I-95", "NB", "21",
                  ref="node/10"),
    ])
    db.commit()
    db.close()
    return path


def labels(hits, n=None):
    return [h["label"] for h in hits[:n]]


# --------------------------------------------------------------------------- parsing

def test_typed_text_is_normalised_like_the_names():
    assert norm_tokens("189 North Main Street") == ["189", "n", "main", "st"]
    assert norm_tokens("I287 EB MM 10.0") == ["i", "287", "eb", "mm", "10.0"]
    assert norm_tokens("Kohl's plaza") == ["kohls", "plz"]


@pytest.mark.parametrize("text, route, d, kind, value", [
    ("I-287 EB MM 10.0", "I-287", "EB", "mm", "10.0"),
    ("287 eastbound mile marker 10", "I-287", "EB", "mm", "10"),
    ("I-95 NB exit 21", "I-95", "NB", "exit", "21"),
    ("Hutch SB exit 30", "Hutch", "SB", "exit", "30"),
    ("new england thruway northbound 14", "I-95", "NB", "", "14"),
    ("hutc", "Hutch", "", "", ""),
])
def test_highway_phrasings(text, route, d, kind, value):
    h = parse_highway(norm_tokens(text))
    assert (h.route, h.dir, h.kind, h.value) == (route, d, kind, value)


@pytest.mark.parametrize("text", ["287 King St", "95 Main Street", "120 Westchester Ave", "lyon park"])
def test_an_address_or_a_name_is_not_a_highway(text):
    assert parse_highway(norm_tokens(text)) is None


# --------------------------------------------------------------------------- kinds and ranking

def test_every_kind(ix):
    first = {q: ix.search(q)[0] for q in ("189 n main", "king & arbor", "lyon park", "287 eb mm 10",
                                          "hutch sb exit 30", "arbor dr")}
    assert {q: h["kind"] for q, h in first.items()} == {
        "189 n main": "address", "king & arbor": "intersection", "lyon park": "place",
        "287 eb mm 10": "milemarker", "hutch sb exit 30": "exit", "arbor dr": "street"}
    for h in first.values():
        assert set(h) >= {"kind", "label", "sub", "lat", "lon", "source"}
    assert first["king & arbor"]["label"] == "King St & Arbor Dr"
    assert first["lyon park"]["source"] == "westchester-county"


def test_mile_marker_queries(ix):
    for q in ("I287 EB MM 10.0", "287 eastbound mile marker 10", "i-287 eb 10", "cross westchester eb mm 10"):
        h = ix.search(q)[0]
        assert (h["label"], h["kind"], h["sub"], h["source"]) == ("I-287 EB MM 10.0", "milemarker", "Rye Brook", "nysdot-rm"), q
    assert labels(ix.search("287 mm 10"), 2) == ["I-287 EB MM 10.0", "I-287 WB MM 10.0"]    # no side said: both
    assert ix.search("287 10.1")[0]["label"] == "I-287 EB MM 10.1"
    assert ix.search("287 1101")[0]["label"] == "I-287 EB MM 10.1"         # the marker's own number
    # a Thruway post stands for both sides: the side typed goes in the label
    assert ix.search("I-95 NB MM 14")[0]["label"] == "I-95 NB MM 14.0"
    assert ix.search("thruway 14")[0]["label"] == "I-95 MM 14.0"
    assert ix.search("95 nb exit 21")[0]["label"] == "I-95 NB exit 21"
    # the Hutch's exits were renumbered: the old number still finds it, and says so
    assert ix.search("hutch sb exit 19a")[0]["label"] == "Hutch SB exit 19A"
    assert ix.search("Hutch SB exit 30")[0]["label"] == "Hutch SB exit 19A (old 30S)"
    # an address on a street whose number is a highway's stays an address
    assert ix.search("287 King St")[0]["kind"] == "address"


def test_markers_by_the_name_60_control_gives_them(ix):
    # the spot's name from 60 Control's list (#83) finds the marker; its own numbers don't match a name search
    h = ix.search("rye brook town line")[0]
    assert (h["label"], h["kind"], h["value"], h["sub"]) == ("I-287 EB MM 9.1", "milemarker", "9.1", "Rye Brook")
    assert ix.search("purchase st bridge")[0]["label"] == "Hutch NB MM 16.6"
    assert ix.search("bridge purch")[0]["label"] == "Hutch NB MM 16.6"
    assert ix.search("hutch nb mm 16.6")[0]["label"] == "Hutch NB MM 16.6"
    assert ix.search("hutch 5019")[0]["label"] == "Hutch NB MM 16.6"                 # the panel it stands by
    assert all(h["kind"] != "milemarker" for h in ix.search("1091"))


def test_park_query_and_aliases(ix):
    assert ix.search("lyon pa")[0]["label"] == "Lyon Park"
    assert ix.search("playland")[0]["label"] == "Rye Playland"
    assert ix.search("rye play")[0]["label"] == "Rye Playland"


def test_our_area_first_then_nearness(ix):
    # three Oak Parks: Port Chester and Rye Brook before Greenwich
    hits = [h for h in ix.search("oak park") if h["label"] == "Oak Park"]
    assert [h["sub"] for h in hits][-1] == "Greenwich"
    assert {h["sub"] for h in hits[:2]} == {"Port Chester", "Rye Brook"}
    # between our two, the one nearer the event
    assert ix.search("oak park", near=RB)[0]["sub"] == "Rye Brook"
    assert ix.search("oak park", near=PC)[0]["sub"] == "Port Chester"
    # the same address in two towns: ours first
    assert [h["sub"] for h in ix.search("189 N Main St")[:2]] == ["Port Chester", "Greenwich"]
    # the town order can come from the CAD (Settings > Lists > Town order)
    assert ix.search("oak park", priority=("Greenwich", "Rye"))[0]["sub"] == "Greenwich"


def test_cross_streets_while_typing(ix):
    assert ix.search("king st &")[0]["label"] == "King St & Arbor Dr"
    assert ix.search("king st and arb")[0]["label"] == "King St & Arbor Dr"


def test_local_names_are_offered(ix):
    extra = [{"name": "the Waterfront", "aliases": ["waterfront plaza"], "label": "The Waterfront at Port Chester",
              "lat": 41.0000, "lon": -73.6600, "muni": "Port Chester"}]
    h = ix.search("waterfront", extra=extra)[0]
    assert (h["label"], h["source"], h["sub"]) == ("The Waterfront at Port Chester", "local", "Port Chester")
    assert ix.search("waterfront pl", extra=extra)[0]["source"] == "local"
    assert ix.search("waterfront") == []


def test_nothing_for_a_unit_code_or_noise(ix):
    assert ix.search("E63") == []
    assert ix.search("") == [] and ix.search("@") == [] and ix.search("x") == []


def test_an_old_gazetteer_without_places_still_searches(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA.split("CREATE TABLE places")[0])
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.execute("INSERT INTO streets VALUES (1, 'king st', 'King St')")
    db.execute("INSERT INTO addresses VALUES (1, 5, '', 'Rye Brook', 41.02, -73.68, 1)")
    db.commit()
    db.close()
    ix = PlaceIndex(Gazetteer(path))
    assert ix.search("5 king")[0]["label"] == "5 King St"
    assert ix.search("287 eb mm 10") == []


# --------------------------------------------------------------------------- the real data

@pytest.fixture(scope="module")
def real():
    if not DEFAULT_DB.exists():
        pytest.skip("data/gazetteer.sqlite missing")
    ix = PlaceIndex(Gazetteer())
    if not ix.counts["markers"]:
        pytest.skip("data/gazetteer.sqlite has no places table: run tools/fetch_places.py")
    return ix


def test_real_mile_markers(real):
    h = real.search("287 eastbound mile marker 10")[0]
    assert h["label"] == "I-287 EB MM 10.0" and h["sub"] in ("Rye Brook", "Port Chester", "Rye", "Harrison")
    assert real.search("287 eb mm 10", near=(h["lat"], h["lon"]))[0]["label"] == "I-287 EB MM 10.0"
    assert real.search("I-287 WB MM 10.0")[0]["label"] == "I-287 WB MM 10.0"
    assert real.search("I-95 NB MM 14")[0]["label"] == "I-95 NB MM 14.0"
    assert real.search("hutch nb 5041")[0]["route"] == "Hutch"
    # the Hutch reads as 60 Control's list (#83): exit 15A / I-287 at 15.6-16.0, and the spots by name
    h = real.search("hutch nb mm 15.8")[0]
    assert h["label"] == "Hutch NB MM 15.8" and h["detail"] == "bridge I-287 / Westchester Ave"
    assert real.search("rye brook town line")[0]["label"] == "I-287 EB MM 9.1"
    assert real.search("i-287 ends")[0]["label"] == "I-287 EB MM 10.8"
    assert real.search("bowman ave bridge")[0]["label"] == "I-287 EB MM 9.0"
    assert {real.search(q)[0]["label"] for q in ("purchase st bridge", "bridge purchase")} <= {"Hutch NB MM 16.6", "Hutch SB MM 16.6"}


def test_real_exits_and_parks(real):
    if real.counts["places"] <= 467:
        pytest.skip("no OSM places in this build")
    exits = real.search("I-95 NB exit 21")
    assert exits and exits[0]["kind"] == "exit" and exits[0]["label"] == "I-95 NB exit 21"
    hutch = real.search("hutch sb exit 30")[0]
    assert hutch["kind"] == "exit" and "(old 30" in hutch["label"]
    assert any(h["kind"] == "place" for h in real.search("port chester station"))
    park = real.search("crawford park")[0]
    assert park["kind"] == "place" and park["sub"] == "Rye Brook"


def test_real_address_and_landmark(real):
    h = real.search("189 North Main Street")[0]
    assert (h["label"], h["sub"], h["kind"]) == ("189 N Main St", "Port Chester", "address")
    assert real.search("lyon park")[0]["sub"] == "Port Chester"
    assert real.search("king st & arbor")[0]["label"] == "King St & Arbor Dr"
