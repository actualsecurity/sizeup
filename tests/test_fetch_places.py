"""tools/fetch_places.py on small fixtures (no network): the places table's mile markers, exits and
named places, the meta it writes, and a source that fails keeping the last build's rows."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.geo.places import PlaceIndex

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture(scope="module")
def fp():
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("fetch_places", TOOLS / "fetch_places.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def town(lat, lon):
    return "Rye Brook" if lat > 41.01 else "Port Chester" if lat > 40.99 else ""


def rm(rid, panel, measure, lat, lon, to_date=None):
    return {"attributes": {"ROUTE_ID": rid, "Panel": panel, "MEASURE": measure, "TO_DATE": to_date},
            "geometry": {"x": lon, "y": lat}}


RM = [
    # I-287 primary (…1) heads east as the measure grows; reverse (…2) on the other carriageway
    rm("100248021", "287I87011099", 14.1, 41.020, -73.700), rm("100248021", "287I87011100", 14.2, 41.019, -73.698),
    rm("100248022", "287I87011099", 14.1, 41.0202, -73.7001), rm("100248022", "287I87011100", 14.2, 41.0192, -73.6981),
    rm("100248021", "287I87011101", 14.3, 41.018, -73.696, to_date=1600000000000),     # retired
    # US-1: undivided here, one marker for both directions
    rm("100011031", "  1 87035012", 11.8, 41.003, -73.661), rm("100011031", "  1 87035013", 11.9, 41.004, -73.660),
    rm("100999011", "999 87011000", 1.0, 41.0, -73.6),                                    # not one of ours
]
THRUWAY = [{"attributes": {"RTE_ABBR": "NE", "MILEPOST": 614.0, "POSTED_MILEPOST": 14.0}, "geometry": {"x": -73.68, "y": 41.0}},
           {"attributes": {"RTE_ABBR": "NY", "MILEPOST": 20.0, "POSTED_MILEPOST": 20.0}, "geometry": {"x": -73.9, "y": 41.1}}]
EXITS = [
    {"type": "way", "id": 1, "tags": {"highway": "motorway", "ref": "I 95", "name": "New England Thruway"},
     "nodes": [10, 11, 12], "geometry": [{"lat": 40.995, "lon": -73.680}, {"lat": 41.000, "lon": -73.672},
                                         {"lat": 41.005, "lon": -73.664}]},
    {"type": "way", "id": 2, "tags": {"highway": "motorway", "ref": "I 95"},
     "nodes": [22, 21], "geometry": [{"lat": 41.006, "lon": -73.665}, {"lat": 41.001, "lon": -73.673}]},
    {"type": "way", "id": 3, "tags": {"highway": "motorway", "name": "Some Other Expressway"},
     "nodes": [30, 31], "geometry": [{"lat": 41.0, "lon": -73.7}, {"lat": 41.01, "lon": -73.7}]},
    {"type": "node", "id": 11, "lat": 41.000, "lon": -73.672, "tags": {"highway": "motorway_junction", "ref": "21",
                                                                         "exit_to": "I-287 West;US 1", "old_ref": "20 N"}},
    {"type": "node", "id": 21, "lat": 41.001, "lon": -73.673, "tags": {"highway": "motorway_junction", "ref": "21"}},
    {"type": "node", "id": 30, "lat": 41.0, "lon": -73.7, "tags": {"highway": "motorway_junction", "ref": "4"}},
    {"type": "node", "id": 12, "lat": 41.005, "lon": -73.664, "tags": {"highway": "motorway_junction"}},   # no ref
]
OSM_PLACES = [
    {"type": "way", "id": 100, "center": {"lat": 41.030, "lon": -73.690}, "tags": {"leisure": "park", "name": "Crawford Park",
                                                                                     "alt_name": "Crawford Mansion Park"}},
    {"type": "node", "id": 101, "lat": 41.0301, "lon": -73.6901, "tags": {"leisure": "park", "name": "Crawford Park"}},   # twin
    {"type": "way", "id": 102, "center": {"lat": 41.001, "lon": -73.664}, "tags": {"leisure": "park", "name": "Lyon Park"}},   # landmark
    {"type": "node", "id": 103, "lat": 41.002, "lon": -73.665, "tags": {"leisure": "playground", "name": "Playground"}},     # generic
    {"type": "node", "id": 104, "lat": 40.900, "lon": -73.800, "tags": {"amenity": "school", "name": "Far Away School"}},  # outside
    {"type": "node", "id": 105, "lat": 40.970, "lon": -73.760, "tags": {"amenity": "hospital", "name": "White Plains Hospital",
                                                                        "addr:city": "White Plains"}},             # outside, kept
    {"type": "way", "id": 106, "center": {"lat": 41.002, "lon": -73.667}, "tags": {"shop": "mall", "name": "The Waterfront",
                                                                                    "addr:housenumber": "1",
                                                                                    "addr:street": "Main St"}},
    {"type": "node", "id": 107, "lat": 41.003, "lon": -73.666, "tags": {"amenity": "bench", "name": "Memorial Bench"}},
    {"type": "node", "id": 108, "lat": 41.004, "lon": -73.665, "tags": {"railway": "station", "name": "Port Chester"}},
    {"type": "node", "id": 109, "lat": 41.020, "lon": -73.690, "tags": {"amenity": "social_facility",
                                                                        "social_facility": "nursing_home",
                                                                        "name": "Sunrise of Rye Brook"}},
]
LANDMARKS = [{"name": "Lyon Park", "lat": 41.0012, "lon": -73.6642, "aliases": ["lyon park"]}]


def test_reference_markers(fp):
    rows = fp.markers_from_rm(RM, town)
    by = {(r["route"], r["dir"], r["value"]) for r in rows}
    assert by == {("I-287", "EB", "9.9"), ("I-287", "EB", "10.0"), ("I-287", "WB", "9.9"), ("I-287", "WB", "10.0"),
                  ("US-1", "", "1.2"), ("US-1", "", "1.3")}
    eb = next(r for r in rows if r["name"] == "I-287 EB MM 10.0")
    assert (eb["kind"], eb["source"], eb["ref"], eb["muni"]) == ("milemarker", "nysdot-rm", "287I 8701 1100", "Rye Brook")
    assert "1100" in eb["aliases"].split("|")


def test_thruway_mileposts(fp):
    rows = fp.markers_from_thruway(THRUWAY, town)
    assert [(r["name"], r["route"], r["dir"], r["value"], r["source"]) for r in rows] == [
        ("I-95 MM 14.0", "I-95", "", "14.0", "nysta-mileposts")]


def test_exits_take_the_side_from_the_carriageway(fp):
    rows = fp.exits_from_osm(EXITS, town)
    assert sorted((r["name"], r["ref"]) for r in rows) == [("I-95 NB exit 21", "node/11"), ("I-95 SB exit 21", "node/21")]
    nb = next(r for r in rows if r["dir"] == "NB")
    assert nb["address"] == "I-287 West / US 1" and nb["source"] == "osm" and nb["muni"] == "Port Chester"
    assert "old 20n" in nb["aliases"].split("|")


def test_named_places(fp):
    rows, stats = fp.places_from_osm(OSM_PLACES, town, LANDMARKS, keep_outside={"hospital"})
    names = {r["name"]: r for r in rows}
    assert set(names) == {"Crawford Park", "White Plains Hospital", "The Waterfront", "Port Chester", "Sunrise of Rye Brook"}
    assert names["Crawford Park"]["ref"] == "way/100" and "crawford mansion park" in names["Crawford Park"]["aliases"]
    assert names["The Waterfront"]["address"] == "1 Main St" and names["The Waterfront"]["category"] == "shopping center"
    assert "waterfront" in names["The Waterfront"]["aliases"].split("|")
    assert names["White Plains Hospital"]["muni"] == "White Plains"
    assert names["Port Chester"]["category"] == "rail station"
    assert names["Sunrise of Rye Brook"]["category"] == "nursing home"
    assert all(r["source"] == "osm" for r in rows)
    assert stats["already a landmark"] == 1 and stats["duplicate"] == 1 and stats["generic name"] == 1
    assert stats["outside"] == 1


def gazetteer(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', ?)", (json.dumps([{"name": "NYS Address Points", "fetched": "2026-10-07"}]),))
    db.execute("INSERT INTO meta VALUES ('counts', '{}')")
    db.execute("INSERT INTO landmarks (name, category, address, muni, lat, lon, source, aliases) VALUES "
               "('Lyon Park', 'park', '', 'Port Chester', 41.0012, -73.6642, 'westchester-county', 'lyon park')")
    db.commit()
    db.close()


class Bounds:
    polys = {}


def test_build_writes_the_table_and_meta_and_keeps_a_failed_source(fp, tmp_path, monkeypatch):
    path = tmp_path / "gaz.sqlite"
    gazetteer(path)
    monkeypatch.setattr(fp, "muni_finder", lambda bounds: town)
    monkeypatch.setattr(fp, "fetch_rm", lambda bbox: RM)
    monkeypatch.setattr(fp, "fetch_thruway", lambda bbox: THRUWAY)
    monkeypatch.setattr(fp, "fetch_osm_exits", lambda bbox: (EXITS, "https://overpass.example/api"))
    monkeypatch.setattr(fp, "fetch_osm_places", lambda bounds: (OSM_PLACES, "https://overpass.example/api"))
    monkeypatch.setattr(fp.time, "sleep", lambda s: None)
    bounds = type("B", (), {"area": lambda self, margin=0.0: (40.9, -73.8, 41.1, -73.6), "polys": {}})()
    counts = fp.build(path, bounds, "2026-10-13")
    assert counts["by_kind"] == {"milemarker": 7, "exit": 2, "place": 5} and counts["missing"] == []
    db = sqlite3.connect(path)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    sources = json.loads(meta["sources"])
    assert sources[0]["name"] == "NYS Address Points"                  # the rest of the build is left alone
    ours = {s["code"]: s for s in sources if s.get("part") == "places"}
    assert set(ours) == {"nysdot-rm", "nysta-mileposts", "osm"}
    assert all(s["fetched"] == "2026-10-13" and s["license"] for s in ours.values())
    assert "ODbL" in ours["osm"]["license"]
    assert json.loads(meta["counts"])["places"]["by_source"] == {"nysdot-rm": 6, "nysta-mileposts": 1, "osm": 7}
    db.close()
    # the search reads it
    ix = PlaceIndex(Gazetteer(path))
    assert ix.search("287 eb mm 10")[0]["label"] == "I-287 EB MM 10.0"
    assert ix.search("crawford")[0]["label"] == "Crawford Park"
    # Overpass down: the OSM rows of the last build stay, the others are fetched again
    def down(*a):
        raise RuntimeError("all Overpass mirrors failed")
    monkeypatch.setattr(fp, "fetch_osm_exits", down)
    counts = fp.build(path, bounds, "2026-10-14")
    assert counts["by_kind"] == {"milemarker": 7, "exit": 2, "place": 5}
    assert counts["missing"] and "kept 7 rows" in counts["missing"][0]
    sources = json.loads(dict(sqlite3.connect(path).execute("SELECT key, value FROM meta"))["sources"])
    osm = next(s for s in sources if s.get("code") == "osm")
    assert osm["fetched"] == "2026-10-13" and osm["kept_from_previous_build"]
    assert next(s for s in sources if s.get("code") == "nysdot-rm")["fetched"] == "2026-10-14"
