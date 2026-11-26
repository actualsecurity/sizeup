"""A county feed's street address with a common place's name at it ("45 W SAMPLE GREENS DR : @SAMPLE GREEN COMPLEX
RYE_BROOK", shown "45 W Sample Greens Dr (Sample Green Complex), Rye Brook") gets its map point. Seen live: the address
was right but the event had "No map point". The name was never the cause (the gazetteer reads the street part only);
the street was: the county writes the direction in front ("W SAMPLE GREENS DR"), the address data after the name
("Sample Greens Dr W"), so the gazetteer found only the street, never the house. And a page's call that the feed then
attached to kept the page's unplaced address with no point, because only a highway's location was looked up again.

The name is kept as the incident's place (the radio log reads it), and a common place of that name is the point when
the address itself has none. Invented data only: made-up streets, place names, points and event ids."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.cad import commonplaces as cp
from sizeup.cad.sources import shown_location
from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from tests.test_incident_sources import T0, Rig

RB = "Rye Brook"
WEST = (41.0402, -73.6851)          # 45 Sample Greens Dr W
EAST = (41.0386, -73.6822)          # 47 Sample Greens Dr E
COMPLEX = (41.0395, -73.6840)       # the common place "Sample Green Complex"
LOC = "45 W SAMPLE GREENS DR : @SAMPLE GREEN COMPLEX RYE_BROOK"     # the feed's own shape


def fixture_gazetteer(path) -> Gazetteer:
    """Two streets of one name, the direction after it (as the address data has them): 43 and 45 on W, 47 and 49 on E."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(1, "sample greens dr w", "Sample Greens Dr W"),
                                                           (2, "sample greens dr e", "Sample Greens Dr E")])
    db.executemany("INSERT INTO addresses VALUES (?, ?, '', ?, ?, ?, 1)",
                   [(1, 45, RB, *WEST), (1, 43, RB, WEST[0] - 0.0001, WEST[1] - 0.0002),
                    (2, 47, RB, *EAST), (2, 49, RB, EAST[0] + 0.0001, EAST[1] + 0.0002)])
    db.executemany("INSERT INTO lines VALUES (?, ?, ?)",
                   [(1, RB, json.dumps([[WEST[1] - 0.001, WEST[0]], [WEST[1] + 0.001, WEST[0]]])),
                    (2, RB, json.dumps([[EAST[1] - 0.001, EAST[0]], [EAST[1] + 0.001, EAST[0]]]))])
    db.commit()
    db.close()
    return Gazetteer(path)


def as_geocoder(gaz: Gazetteer):
    """The CAD's geocoder over this gazetteer, as the app makes it (App._geocode_text)."""
    def geocode(q: str) -> list[dict]:
        return [{k: getattr(m, k) for k in ("label", "lat", "lon", "kind", "municipality", "precision")}
                | {"confidence": round(float(m.confidence), 2)} for m in gaz.candidates(q)[:20]]
    return geocode


@pytest.fixture
def gaz(tmp_path):
    return fixture_gazetteer(tmp_path / "gaz.sqlite")


@pytest.fixture
def rig(tmp_path, gaz):
    r = Rig(tmp_path)
    r.cad.geocoder = as_geocoder(gaz)
    return r


def point(rig, iid: int):
    inc = rig.cad.store.incident(iid)
    return None if inc["lat"] is None else (inc["lat"], inc["lon"])


@pytest.mark.parametrize("text, at", [
    ("45 W Sample Greens Dr", WEST),
    ("45 West Sample Greens Dr", WEST),
    ("45 W Sample Greens Dr, Rye Brook", WEST),
    ("47 E Sample Greens Dr", EAST),
    ("45 Sample Greens Dr W", WEST),                          # as the data has it: as before
])
def test_the_direction_in_front_finds_the_house_on_the_street_that_has_it_after(gaz, text, at):
    m = gaz.geocode(text)
    assert m.kind == "address" and m.confidence >= 0.6 and (m.lat, m.lon) == at


@pytest.mark.parametrize("loc", [LOC, "45 W SAMPLE GREENS DR (SAMPLE GREEN COMPLEX), RYE BROOK"])
def test_the_feed_s_address_is_geocoded_without_its_place_name(loc):
    assert shown_location(loc).startswith("45 W Sample Greens Dr") and "Complex" not in shown_location(loc)


@pytest.mark.parametrize("loc", [
    LOC,
    "45 W SAMPLE GREENS DR (SAMPLE GREEN COMPLEX), RYE BROOK",
    "45 W SAMPLE GREENS DR (SAMPLE GREEN COMPLEX), , RYE BROOK",       # an extra comma
    "45 W SAMPLE GREENS DR :APT 3B : @SAMPLE GREEN COMPLEX RYE_BROOK",    # a unit
    "45 W SAMPLE GREENS DR APT 3B RYE_BROOK",
])
def test_a_feed_s_address_with_a_place_name_gets_its_point(rig, loc):
    rig.feed.put("F0071001", type="STRU", location=loc, units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) == WEST
    assert inc["address_raw"] == loc                          # the feed's own text kept


def test_the_place_name_is_kept_as_the_incident_s_place(rig):
    rig.feed.put("F0071001", type="STRU", location=LOC, units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert inc["address"] == "45 W Sample Greens Dr (Sample Green Complex), Rye Brook"    # shown as before
    assert (inc["place"], inc["place_address"]) == ("Sample Green Complex", "45 W Sample Greens Dr, Rye Brook")
    # a person's edit of something else keeps it; a new address drops it
    rig.cad.update(inc["id"], {"address": inc["address"], "remarks": "gate code at the office", "operator": "JD"})
    assert rig.cad.store.incident(inc["id"])["place"] == "Sample Green Complex"
    rig.cad.update(inc["id"], {"address": "47 E Sample Greens Dr", "operator": "JD"})
    assert rig.cad.store.incident(inc["id"])["place"] == ""


def test_an_address_with_no_place_name_has_none(rig):
    rig.feed.put("F0071001", type="STRU", location="45 W SAMPLE GREENS DR RYE_BROOK", units=("E60",),
                 times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert (inc["place"], inc["place_address"]) == ("", "") and point(rig, inc["id"]) == WEST


def common_place(rig, lat=COMPLEX[0], lon=COMPLEX[1]) -> None:
    entry = {"name": "SAMPLE GREEN COMPLEX", "aliases": [], "label": "Sample Green Complex", "lat": lat, "lon": lon,
             "muni": RB, "note": "", "address": "1 SAMPLE GREENS DR W", "at": "1 Sample Greens Dr W",
             "source": cp.SOURCE, "imported": "2026-01-01", "located": "exact"}
    rig.cad.settings._commit([("places", "county/sample green complex @ 1 sample greens dr w", entry)], "test", [])


def test_an_address_the_gazetteer_lacks_takes_the_common_place_s_point(rig):
    common_place(rig)
    rig.feed.put("F0071001", type="STRU", location="12 UNHEARD OF WAY : @SAMPLE GREEN COMPLEX RYE_BROOK",
                 units=("E60",), times={"dispatched": T0})
    rig.feed.put("F0071002", type="STRU", location="14 UNHEARD OF WAY : @OTHER UNLISTED PLACE RYE_BROOK",
                 units=("E61",), times={"dispatched": T0 + 3600})
    rig.poll()
    a, b = rig.incidents()
    assert point(rig, a["id"]) == COMPLEX and a["place"] == "Sample Green Complex"
    assert point(rig, b["id"]) is None                        # no such common place: no point, nothing guessed


def test_the_address_s_own_point_beats_the_common_place_s(rig):
    common_place(rig)
    rig.feed.put("F0071001", type="STRU", location=LOC, units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) == WEST


def test_never_another_town_s_common_place(rig):
    common_place(rig)
    rig.feed.put("F0071001", type="STRU", location="12 UNHEARD OF WAY : @SAMPLE GREEN COMPLEX PORT_CHESTER",
                 units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) is None


def test_a_page_s_unplaced_call_gets_the_point_when_the_feed_attaches_at_the_same_place(rig, gaz):
    rig.cad.geocoder = lambda q: []                           # the page's address not placed (as seen live)
    page = rig.page(text="Port Chester Fire Department, Engine 60, respond to a structure fire. "
                         "45 West Sample Greens Drive. Time out 14:30.")
    assert page["lat"] is None
    rig.cad.geocoder = as_geocoder(gaz)
    rig.at(40)
    rig.feed.put("F0071001", type="STRU", location=LOC, units=("E60",), times={"dispatched": T0 - 10})
    rig.poll()
    assert [i["id"] for i in rig.incidents()] == [page["id"]]
    assert point(rig, page["id"]) == WEST
    assert sum("map point" in t for t in rig.feed_lines(page["id"])) == 1
    rig.at(90)                                                # the next report: nothing looked for again
    rig.feed.change("F0071001", units=("E60", "E61"))
    rig.poll()
    assert sum("map point" in t for t in rig.feed_lines(page["id"])) == 1


def test_a_street_address_left_with_no_point_is_looked_for_once_per_search(rig, monkeypatch):
    rig.feed.put("F0071001", type="STRU", location="12 UNHEARD OF WAY RYE_BROOK", units=("E60",),
                 times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    asked = []
    geo = rig.cad.geocoder
    rig.cad.geocoder = geo                                    # the same search: never asked again
    monkeypatch.setattr(rig.cad, "_auto_geo", lambda f: asked.append(f["address"]))
    for k, units in enumerate((("E60", "E61"), ("E60",), ("E60", "E62"))):
        rig.at(60 * (k + 1))
        rig.feed.change("F0071001", units=units)
        rig.poll()
    assert asked == [] and point(rig, inc["id"]) is None


def test_the_start_s_sweep_places_an_open_feed_incident_at_a_street_address(rig, gaz):
    rig.cad.geocoder = lambda q: []
    rig.feed.put("F0071001", type="STRU", location=LOC, units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) is None
    rig.cad.geocoder = as_geocoder(gaz)
    assert rig.cad.sources.backfill_points() == 1
    assert point(rig, inc["id"]) == WEST
    assert sum("map point" in t for t in rig.feed_lines(inc["id"])) == 1
