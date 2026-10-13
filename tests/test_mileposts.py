"""The mile markers in data/gazetteer.sqlite against 60 Control's lists (GitHub #83): every point of a
list lands on the marker with its mileage, the markers run one way without repeats, the roads whose
markers were right (I-287, I-95, I-684) are as they were, and the unverified roads say so."""

from __future__ import annotations

import importlib.util
import math
import sqlite3
import sys
from pathlib import Path

import pytest

from sizeup.geo.gazetteer import DEFAULT_DB
from sizeup.geo.places import M_PER_DEG_LAT, M_PER_DEG_LON, ROUTES

TOOLS = Path(__file__).resolve().parent.parent / "tools"
LRS = ("Hutch", "US-1", "NY-120", "NY-120A")            # markers from NYSDOT's milepoints (fetch_places.LRS_ROUTES)


@pytest.fixture(scope="module")
def fp():
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("fetch_places", TOOLS / "fetch_places.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def markers():
    """route -> dir -> the marker rows, sorted by value."""
    if not DEFAULT_DB.exists():
        pytest.skip("data/gazetteer.sqlite missing")
    db = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT route, dir, value, lat, lon, source, category, address, aliases FROM places "
                          "WHERE kind = 'milemarker'").fetchall()
    except sqlite3.Error:
        pytest.skip("no places table: run tools/fetch_places.py")
    finally:
        db.close()
    if not rows:
        pytest.skip("no mile markers in this build")
    out: dict[str, dict[str, list[dict]]] = {}
    for route, d, value, lat, lon, source, category, address, aliases in rows:
        out.setdefault(route, {}).setdefault(d, []).append({
            "value": value, "mile": float(value), "lat": lat, "lon": lon, "source": source, "category": category,
            "address": address, "aliases": aliases.split("|")})
    for dirs in out.values():
        for lst in dirs.values():
            lst.sort(key=lambda r: r["mile"])
    return out


def dist_mi(a: dict, lat: float, lon: float) -> float:
    return math.hypot((a["lat"] - lat) * M_PER_DEG_LAT, (a["lon"] - lon) * M_PER_DEG_LON) / 1609.344


def nearest(rows: list[dict], lat: float, lon: float) -> tuple[dict, float]:
    return min(((r, dist_mi(r, lat, lon)) for r in rows), key=lambda x: x[1])


def test_60_control_points_land_on_their_marker(fp, markers):
    """Each placed point of a 60 Control list (an exit's junction node, a bridge): the marker nearest it reads
    the list's mileage, within a tenth; the point is on the road (a marker within 100 m: they stand 161 m apart)."""
    checked = 0
    for (route, d), cal in fp.SIXTY_CONTROL.items():
        rows = markers[route][d]
        for c in cal.points:
            if c.lat is None or not c.fit:
                continue
            m, dist = nearest(rows, c.lat, c.lon)
            assert abs(m["mile"] - c.mile) <= 0.1 + 1e-9, (route, d, c.mile, c.text, m["value"])
            assert dist * 1609.344 <= 100, (route, d, c.text, dist)
            checked += 1
    assert checked >= 14                    # Hutch NB 11, Hutch SB 4 (the bridges), I-287 EB 3


def test_60_control_names_are_on_the_markers(fp, markers):
    by = {(route, d, r["value"]): r for route, dirs in markers.items() for d, rows in dirs.items() for r in rows}
    missing = []
    for (route, d), cal in fp.SIXTY_CONTROL.items():
        for c in cal.points:
            r = by.get((route, d, f"{c.mile:.1f}"))
            if r is None:
                missing.append((route, d, c.mile))
                continue
            assert c.text in r["address"], (route, d, c.mile)
            if not c.text.upper().startswith("EXIT"):
                assert fp.norm_phrase(c.text) in r["aliases"], (route, d, c.mile)
    # the Hutch's NY panels end at the state line: 60 Control's two points past it have no marker
    assert missing == [("Hutch", "NB", 19.0), ("Hutch", "NB", 19.1)]
    assert "rye brook town line" in by[("I-287", "EB", "9.1")]["aliases"]
    assert by[("Hutch", "NB", "15.8")]["address"] == "bridge I-287 / Westchester Ave"
    assert by[("Hutch", "SB", "16.6")]["address"] == "bridge Purchase St"


def test_markers_run_one_way_without_repeats(markers):
    for route, dirs in markers.items():
        _pos, _neg, (vlat, vlon) = ROUTES[route]
        for d, rows in dirs.items():
            values = [r["value"] for r in rows]
            assert len(values) == len(set(values)), (route, d, "a value twice")
            assert all(float(v) >= 0 for v in values)
            # the mileage grows the signed positive way on both sides (the Hutch's old panels restarted mid-way)
            first, last = rows[0], rows[-1]
            along = (last["lat"] - first["lat"]) * vlat + (last["lon"] - first["lon"]) * vlon * 0.76
            assert along > 0, (route, d)
            # tenth after tenth stands a tenth apart (0.05-0.2 mi: a panel line's bends, a carriageway's offset)
            for a, b in zip(rows, rows[1:]):
                if abs(b["mile"] - a["mile"] - 0.1) < 1e-6:
                    step = dist_mi(a, b["lat"], b["lon"])
                    assert 0.05 <= step <= 0.2, (route, d, a["value"], b["value"], step)


def test_the_hutch_reads_like_60_control(markers):
    nb, sb = markers["Hutch"]["NB"], markers["Hutch"]["SB"]
    assert nb[0]["mile"] >= 10 and nb[-1]["value"] == "18.9" and sb[-1]["value"] == "18.8"
    assert all(r["source"] == "nysdot-lrs" and "60 Control" in r["category"] for r in nb + sb)
    assert all("SB list unverified" in r["category"] for r in sb)
    assert not any("unverified" in r["category"] for r in nb)
    # the two sides agree where they share a bridge (within 0.1 mi after their separate fits)
    for value in ("15.8", "16.6", "17.4", "18.1"):
        a = next(r for r in nb if r["value"] == value)
        b = next(r for r in sb if r["value"] == value)
        assert dist_mi(a, b["lat"], b["lon"]) <= 0.1, value


def test_the_unverified_roads_say_so(markers):
    for route in ("US-1", "NY-120", "NY-120A"):
        rows = markers[route][""]
        assert len(rows) >= 50 and all(r["source"] == "nysdot-lrs" and "unverified vs 60 Control" in r["category"]
                                       for r in rows), route
        assert set(markers[route]) == {""}                         # one marker a tenth, for both directions
    assert markers["NY-120"][""][0]["value"] == "0.1"             # from US-1 in Rye
    assert markers["US-1"][""][0]["mile"] < 2.0                    # from the Bronx line, in Pelham Manor


def test_the_right_roads_are_as_they_were(markers):
    """I-287 (60 Control's list agrees), I-95 (the Thruway's posts) and I-684 (continuous posted panels,
    0.0 at I-287): the rows of the build before #83."""
    def at(route, d, value):
        return next(r for r in markers[route][d] if r["value"] == value)
    assert len(markers["I-287"]["EB"]) == len(markers["I-287"]["WB"]) == 86
    assert all(r["source"] == "nysdot-rm" for d in markers["I-287"].values() for r in d)
    assert (at("I-287", "EB", "10.0")["lat"], at("I-287", "EB", "10.0")["lon"]) == (40.998126, -73.680999)
    assert (at("I-287", "EB", "8.4")["lat"], at("I-287", "EB", "8.4")["lon"]) == (41.012756, -73.703876)
    assert (at("I-287", "WB", "10.0")["lat"], at("I-287", "WB", "10.0")["lon"]) == (40.998809, -73.681432)
    assert len(markers["I-95"][""]) == 107 and all(r["source"] == "nysta-mileposts" for r in markers["I-95"][""])
    assert (at("I-95", "", "14.0")["lat"], at("I-95", "", "14.0")["lon"]) == (40.987727, -73.674843)
    assert markers["I-95"][""][0]["value"] == "4.4" and markers["I-95"][""][-1]["value"] == "15.0"
    assert len(markers["I-684"]["NB"]) == len(markers["I-684"]["SB"]) == 100
    assert (at("I-684", "NB", "0.0")["lat"], at("I-684", "NB", "0.0")["lon"]) == (41.02177, -73.728226)
    assert at("I-684", "NB", "10.4")["lat"] > 41.16
