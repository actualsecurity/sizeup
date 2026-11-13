"""Radiogeo depth (sizeup/intel/place.py): spoken intersections, blocks, relative and highway forms, per-domain
thresholds, HQ town narrowing, the WHERE score. On a synthetic gazetteer of invented streets (this repository is
public): Example Ave running east-west, Sample St and Test Rd crossing it, Quarry Ln alone, Example Park."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.geo.pagegeo import locator
from sizeup.intel import place as P

PC = "Port Chester"
LAT = 41.0
EX_W, EX_E = -73.690, -73.670
SAMPLE, TEST = -73.685, -73.675


@pytest.fixture(scope="module")
def gaz(tmp_path_factory):
    path = tmp_path_factory.mktemp("gaz") / "synthetic.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(1, "example ave", "Example Ave"), (2, "sample st", "Sample St"),
                                                           (3, "test rd", "Test Rd"), (4, "quarry ln", "Quarry Ln")])
    addrs = []
    for k, n in enumerate(range(100, 200, 4)):
        lon = SAMPLE + (TEST - SAMPLE) * k / 25
        addrs.append((1, n, "", PC, LAT + 0.0002, lon, 1))
    for k, n in enumerate(range(1, 60, 2)):
        addrs.append((2, n, "", PC, LAT + 0.0001 + k * 0.0002, SAMPLE + 0.0002, 1))
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)", addrs)
    lines = [(1, PC, [[EX_W, LAT], [SAMPLE, LAT], [TEST, LAT], [EX_E, LAT]]),
             (2, PC, [[SAMPLE, LAT - 0.005], [SAMPLE, LAT], [SAMPLE, LAT + 0.005]]),
             (3, PC, [[TEST, LAT - 0.005], [TEST, LAT], [TEST, LAT + 0.005]]),
             (4, "Rye", [[-73.70, 40.99], [-73.70, 40.98]])]
    db.executemany("INSERT INTO lines VALUES (?,?,?)", [(s, m, json.dumps(c)) for s, m, c in lines])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", [(1, 2, LAT, SAMPLE, PC), (1, 3, LAT, TEST, PC)])
    db.execute("INSERT INTO landmarks VALUES (1, 'Example Park', 'park', '', ?, 41.002, -73.68, 'test', 'example park')", (PC,))
    db.commit()
    db.close()
    return Gazetteer(path)


SCOPE = {"system": "mrrs", "service": "police", "pack": "police", "towns": [PC, "Rye", "Rye Brook"]}


def test_an_address_said_with_its_cross_street_is_confirmed(gaz):
    p = P.Locator(gaz)("respond to 120 Example Ave between Sample St and Test Rd", SCOPE)
    assert p.precision == "address" and p.label.startswith("120 Example Ave") and p.town == PC


def test_a_spoken_intersection_is_placed_where_they_meet(gaz):
    p = P.Locator(gaz)("shots fired, corner of Sample St and Example Ave", SCOPE)
    assert p.precision == "intersection" and p.confirmed
    assert (round(p.lat, 4), round(p.lon, 4)) == (LAT, SAMPLE) and p.radius_m == P.RADIUS["intersection"]


def test_streets_that_never_meet_are_no_intersection(gaz):
    p = P.Locator(gaz)("units at Sample St and Test Rd", SCOPE)
    assert p is None or p.precision != "intersection" or not p.confirmed


def test_a_block_is_a_block_never_an_exact_address(gaz):
    p = P.Locator(gaz)("fight in progress the 1 hundred block of Example Ave", SCOPE)
    assert p.precision in ("block", "address")
    q = P.Locator(gaz)._block("the 100 block of Example Ave", SCOPE["towns"], [])
    assert q.precision == "block" and q.label == f"100 block of Example Ave, {PC}" and not q.confirmed
    assert q.radius_m == P.RADIUS["block"]


def test_relative_words_widen_the_circle(gaz):
    a = P.Locator(gaz)("respond to 120 Example Ave between Sample St and Test Rd", SCOPE)
    b = P.Locator(gaz)("respond to the rear of 120 Example Ave between Sample St and Test Rd", SCOPE)
    assert b.label == a.label and b.radius_m >= 2 * a.radius_m and "rear of" in b.basis


def test_a_highway_exit_through_the_place_search(gaz):
    class Ix:
        def search(self, text, limit=8):
            assert "exit 21" in text.lower()
            return [{"kind": "exit", "label": "I-95 NB exit 21", "sub": "Rye", "lat": 40.98, "lon": -73.69, "quality": 0.9}]
    p = P.Locator(gaz, places=lambda: Ix())("MVA with injuries I-95 northbound exit 21", SCOPE)
    assert p.precision == "landmark" and p.label == "I-95 NB exit 21" and p.radius_m == 300.0 and not p.confirmed
    assert P.Locator(gaz)("MVA with injuries I-95 northbound exit 21", SCOPE) is None      # no place search: none


def test_police_thresholds_are_stricter_and_pages_keep_theirs(gaz):
    text = "120 Example Ave between Sample St and Test Rd"
    page = locator(gaz).locate(text, (PC,))
    strict = locator(gaz).locate(text, (PC,), {"confirm": 0.995})
    assert page.best.match.label == strict.best.match.label
    assert page.confirmed and not strict.confirmed and "low confidence" in strict.why
    assert P.DOMAIN_PARAMS["police"]["confirm"] > 0.75 and P.DOMAIN_PARAMS["police"]["sound_min"] > 0.6


@pytest.mark.parametrize("text, towns", [("908 to car 3", [PC]), ("266 respond", ["Rye"]), ("car 2662 respond", []),
                                         ("DPT266 to command", []), ("CHIEF266", []), ("unit 908-2", [])])
def test_hq_callsigns_narrow_the_town_on_their_own_system_only(text, towns):
    assert P.hq_towns(text, "mrrs", "police") == towns
    assert P.hq_towns(text, "westchester", "fire") == []


def test_a_read_back_raises_the_where_score(gaz):
    one = P.Locator(gaz)("smoke from 120 Example Ave", SCOPE)
    two = P.Locator(gaz)("smoke from 120 Example Ave . copy 120 Example Ave . 120 Example Ave on the way", SCOPE)
    assert two.where_score > one.where_score


def test_no_cue_no_gazetteer_work(gaz):
    calls = []

    class G:
        def __getattr__(self, k):
            calls.append(k)
            raise AttributeError(k)
    assert P.Locator(G())("copy that, back in service", SCOPE) is None and calls == []


# -- the #502 review --------------------------------------------------------------------------------------------------

def test_an_address_with_its_cross_street_is_the_address_confirmed(gaz):
    p = P.Locator(gaz)("shots fired 120 Example Ave at Sample St", SCOPE)
    assert p.precision == "address" and p.label.startswith("120 Example Ave") and p.confirmed


@pytest.mark.parametrize("text, kept", [("266 Sample St", "266 Sample St"), ("K9 unit 120 Example Ave", "120 Example Ave"),
                                        ("plate is 120 Example Ave", "120 Example Ave")])
def test_a_number_followed_by_its_street_stays_an_address(text, kept):
    assert kept in P.strip(text, "mrrs", "police")


@pytest.mark.parametrize("text, gone", [("266 respond to 40 Example Ave", "266"), ("ID 1452 Example Ave", "1452"),
                                        ("plate is abc 1234 heading down Example Ave", "1234")])
def test_ids_plates_and_callsigns_alone_still_go(text, gone):
    assert gone not in P.strip(text, "mrrs", "police").split()


def test_a_relative_or_landmark_cue_is_worth_the_gazetteer():
    assert P.has_cue("units to the area of the park") and P.has_cue("in front of the school")


def test_an_address_far_from_the_corner_said_is_not_confirmed_by_it(gaz):
    # "100 Example Ave at Test Rd": the 100 block is at Sample St, ~840 m from Test Rd (#502 re-check)
    p = P.Locator(gaz)("shots fired 100 Example Ave at Test Rd", SCOPE)
    assert not (p.precision == "address" and p.confirmed and "at Example Ave & Test Rd" in p.basis)
    assert "m from" in p.basis
    near = P.Locator(gaz)("shots fired 196 Example Ave at Test Rd", SCOPE)             # the far end: at the corner
    assert near.precision == "address" and near.confirmed
