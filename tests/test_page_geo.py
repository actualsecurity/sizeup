"""A page's location from every reading, checked against its cross streets and its tones' towns (#176).

2026-10-15 10:29, Port Chester · Fire tones: the page said "135 South Remon Street ... Between Clinton Street and
Williams Street", read back as "135 135 South-Regent-Street ... Between Clinton Street, William-Street". It was mapped
to 135 South St, Greenwich (0.47) and placed as if sure; it is 135 S Regent St, Port Chester (Corpus Christi School).

The regression runs on a recorded street extract (tests/fixtures/gazetteer/regent_176.json: the streets around
S Regent St and the sound-alikes that competed, from data/gazetteer.sqlite), so a gazetteer rebuild can't move it;
the rest runs on the real gazetteer. No network."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from sizeup.cad import runcards
from sizeup.dispatch import parse
from sizeup.geo.gazetteer import DEFAULT_DB, SCHEMA, Gazetteer
from sizeup.geo.normalize import tokenize
from sizeup.geo.pagegeo import home_towns, locate_page
from sizeup.incidents import IncidentStore

PAGE_176 = ("60 Control page 10:29: To get Porch a full upon response. 135 South Remon Street, Corpus Christi School. "
            "Between Clinton Street and Williams Street, inside order gas kind of gas on scene. Or a full response 135 135 "
            "South-Regent-Street, the Corpus Christi School. Between Clinton Street, William-Street, inside order gas kind "
            "of gas on team 10:30, 60.")
TONES = ["Port Chester · Fire"]
REGENT_135 = (41.000791, -73.675162)          # the NYS address point of 135 S Regent St
EXTRACT = Path(__file__).parent / "fixtures" / "gazetteer" / "regent_176.json"


def dist_m(lat1, lon1, lat2, lon2):
    return math.hypot((lat1 - lat2) * 110_574, (lon1 - lon2) * 111_320 * math.cos(math.radians(lat1)))


@pytest.fixture(scope="module")
def regent(tmp_path_factory):
    """The recorded extract as a gazetteer of its own."""
    d = json.loads(EXTRACT.read_text())
    path = tmp_path_factory.mktemp("gaz") / "regent.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", d["streets"])
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,1)", d["addresses"])
    db.executemany("INSERT INTO lines VALUES (?,?,?)", [(s, m, json.dumps(c)) for s, m, c in d["lines"]])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", d["intersections"])
    db.executemany("INSERT INTO landmarks VALUES (?,?,?,?,?,?,?,?,?)", d["landmarks"])
    db.commit()
    db.close()
    return Gazetteer(path)


@pytest.fixture(scope="module")
def gaz():
    if not DEFAULT_DB.exists():
        pytest.skip("data/gazetteer.sqlite missing: run tools/fetch_addresses.py")
    return Gazetteer()


def check_176(pg):
    b = pg.best
    assert pg.confirmed, pg.why
    assert b.match.label == "135 S Regent St, Port Chester" and b.match.precision == "exact"
    assert dist_m(b.match.lat, b.match.lon, *REGENT_135) < 15
    assert b.checked == "all" and "Clinton St" in b.crosses and any("William" in c for c in b.crosses)
    assert b.place.startswith("Corpus Christi")                       # the school said on the page backs it
    assert b.score >= 0.9 and len(b.reads) == 2                        # both readings (heard and read back) agree
    assert all("Greenwich" not in r.match.label for r in [b] + pg.alternatives)
    geo = pg.as_geo()
    assert geo["confirmed"] and geo["town"] == "Port Chester" and geo["label"] == "135 S Regent St, Port Chester"


def test_the_176_page_is_135_s_regent_st_port_chester(regent):
    check_176(locate_page(regent, PAGE_176, TONES))


def test_the_176_page_on_the_real_gazetteer(gaz):
    check_176(locate_page(gaz, PAGE_176, TONES))


def test_the_176_page_type_and_read_back():
    d = parse(PAGE_176)
    assert d.incident_type == "Gas leak" and d.type_phrase == "order gas"
    assert runcards.from_page(d.incident_type, d.category, d.text) == ("HAZARD", "GAS", "")   # odor of gas inside
    assert tokenize("135 135 South-Regent-Street") == ["135", "south", "regent", "st"]      # a repeat, not 135135
    assert tokenize("one three five one three five") == ["135135"]                         # one number said is kept


def test_first_reading_alone_sound_alike_checked_by_both_cross_streets(regent):
    # "Remon" is no street; among our streets S Regent St sounds closest and runs between Clinton and William.
    pg = locate_page(regent, "135 South Remon Street, Corpus Christi School. Between Clinton Street and Williams Street",
                     TONES)
    assert pg.confirmed and pg.best.match.label == "135 S Regent St, Port Chester"
    assert pg.best.how == "sound-alike" and pg.best.checked == "all"


def test_one_cross_street_misheard(gaz):
    pg = locate_page(gaz, "Port Chester fire, 135 South Regent Street between Clinton Street and Wilson Street, odor of gas",
                     ["Port Chester FD"])
    assert pg.confirmed and pg.best.match.label == "135 S Regent St, Port Chester"
    assert pg.best.checked == "partial" and pg.best.crosses == ["Clinton St"]


def test_misheard_street_and_cross_street_is_unconfirmed_with_alternatives(gaz):
    pg = locate_page(gaz, "Port Chester fire, respond to 135 South Remon Street between Clinton Street and Smith Street",
                     ["Port Chester FD"])
    assert not pg.confirmed and "only one cross street" in pg.why
    assert pg.best.match.label == "135 S Regent St, Port Chester"
    geo = pg.as_geo()
    assert geo["confirmed"] is False and geo["town"] == ""
    assert geo["alts"][0]["label"] == "135 S Regent St, Port Chester" and len(geo["alts"]) >= 3


def test_low_confidence_offers_alternatives(gaz):
    pg = locate_page(gaz, "135 South Remon Street, odor of gas", ["Port Chester FD"])
    assert not pg.confirmed and pg.best.score < 0.75
    assert "no cross street was said" in pg.why
    labels = [a["label"] for a in pg.as_geo()["alts"]]
    assert "135 S Regent St, Port Chester" in labels and len(labels) >= 3


def test_house_number_not_on_the_street_is_the_block_between_the_cross_streets(regent):
    pg = locate_page(regent, "999 South Regent Street between Clinton Street and William Street", TONES)
    b = pg.best
    assert b.match.kind == "street" and b.match.precision == "between cross streets" and b.checked == "all"
    assert b.match.label.startswith("S Regent St between Clinton St and ")
    # on S Regent St, between where Clinton St (41.000175) and W William St (41.001844) meet it
    assert 41.000175 < b.match.lat < 41.001844 and dist_m(b.match.lat, b.match.lon, *REGENT_135) < 120


def test_greenwich_never_wins_a_port_chester_page_unless_named(gaz):
    said = "24 Field Point Road between West Elm Street and Woodland Drive, structure fire"
    pg = locate_page(gaz, "Port Chester fire, " + said, ["Port Chester FD"])
    assert not pg.confirmed and "not this page's town" in pg.why
    assert pg.as_geo()["town"] == ""
    ma = locate_page(gaz, "Port Chester fire, mutual aid to the Town of Greenwich, " + said, ["Port Chester FD"])
    assert ma.confirmed and ma.best.match.label == "24 Field Point Rd, Greenwich" and ma.best.checked == "all"


def test_mutual_aid_to_a_named_town_and_our_own_otherwise(gaz):
    # 51 Grace Church St is in Port Chester and in the City of Rye
    rye = locate_page(gaz, "Port Chester fire, mutual aid to the City of Rye, 51 Grace Church Street, structure fire",
                      ["Port Chester FD"])
    assert rye.confirmed and rye.best.match.label == "51 Grace Church St, Rye"
    pc = locate_page(gaz, "Port Chester fire, 51 Grace Church Street cross of Central Avenue", ["Port Chester FD"])
    assert pc.confirmed and pc.best.match.label == "51 Grace Church St, Port Chester"
    byram = locate_page(gaz, "Port Chester fire, 15 Beech Street, Byram, mutual aid", ["Port Chester FD"])
    assert byram.confirmed and byram.best.match.municipality == "Greenwich"
    rb = locate_page(gaz, "Rye Brook Fire Department, alarm activation, Blind Brook High School, eight forty King Street",
                     ["Rye Brook FD"])
    assert rb.confirmed and rb.best.match.label == "840 King St, Rye Brook"


def test_intersections_and_the_header_town(gaz):
    pg = locate_page(gaz, "Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester Avenue, "
                          "cross of Pearl Street, Port Chester. Time out 1432.", ["Port Chester FD"])
    assert pg.confirmed and pg.best.match.label == "209 Westchester Ave, Port Chester" and pg.best.checked == "all"
    x = locate_page(gaz, "Port Chester fire, King Street and Arbor Drive, car fire", ["Port Chester FD"])
    assert x.best.match.kind == "intersection" and "King St & Arbor Dr" in x.best.match.label


def test_home_towns_from_the_tones():
    assert home_towns(["Port Chester · Fire"]) == ("Port Chester",)
    assert home_towns(["Rye Brook FD (est.)", "Port Chester FD"]) == ("Rye Brook", "Port Chester")
    assert home_towns(["Fairview"]) == ()


# --------------------------------------------------------------------------- through the monitor to the CAD card

def page_store(gz):
    from sizeup.geo.pagegeo import locate_page as lp
    return IncidentStore({"Port Chester · Fire", "Port Chester FD", "Rye Brook FD"},
                         geocode=lambda d, tones: lp(gz, d.text, tones).as_geo(), clock=lambda: 1000.0)


def test_the_176_page_on_the_cad_card(regent):
    from tests.test_cad import make_cad
    cad = make_cad()
    inc = page_store(regent).on_page("p1", ["Port Chester FD"], parse(PAGE_176), at=1000.0)   # the CAD's name for those tones
    ev = cad.from_monitor(inc)
    assert ev["address"] == "135 S Regent St, Port Chester" and ev["geo_alts"] == ""
    assert dist_m(ev["lat"], ev["lon"], *REGENT_135) < 15
    assert ev["cross_street"].startswith("Clinton St and ") and "William" in ev["cross_street"]
    assert (ev["type"], ev["subtype"]) == ("HAZARD", "GAS")
    assert "location as heard: 135 South Remon Street (map: 135 S Regent St, Port Chester" in ev["remarks"]


def test_an_unconfirmed_page_is_not_placed_and_offers_the_readings(gaz):
    from tests.test_cad import make_cad
    cad = make_cad()
    store = page_store(gaz)
    inc = store.on_page("p1", ["Port Chester FD"], parse("Port Chester fire, 135 South Remon Street, odor of gas"), at=1000.0)
    assert inc.geo["confirmed"] is False
    ev = cad.from_monitor(inc)
    assert ev["lat"] is None and ev["geo_label"] == "" and ev["address"] == "135 South Remon Street"
    assert "location unconfirmed" in ev["remarks"] and "location as heard: 135 South Remon Street" in ev["remarks"]
    card = next(i for i in cad.state()["incidents"] if i["id"] == ev["id"])
    alts = card["geo_alts"]
    assert alts[0]["label"] == "135 S Regent St, Port Chester" and all(a["lat"] and a["why"] is not None for a in alts)
    # the read-back, a repeat page: now it checks out against the cross streets; the card is placed
    inc2 = store.on_page("p2", ["Port Chester FD"],
                         parse("Port Chester fire, 135 South Regent Street between Clinton Street and William Street, "
                               "odor of gas"), at=1030.0)
    assert inc2 is inc and inc.geo["confirmed"] is True
    ev2 = cad.from_monitor(inc2)
    assert ev2["id"] == ev["id"] and ev2["address"] == "135 S Regent St, Port Chester" and ev2["lat"] is not None
    assert ev2["geo_alts"] == ""


def test_picking_a_reading_places_the_event(gaz):
    from tests.test_cad import make_cad
    cad = make_cad()
    inc = page_store(gaz).on_page("p1", ["Port Chester FD"], parse("Port Chester fire, 135 South Remon Street, odor of gas"),
                                  at=1000.0)
    ev = cad.from_monitor(inc)
    a = json.loads(ev["geo_alts"])[0]
    got = cad.update(ev["id"], {"address": a["label"], "lat": a["lat"], "lon": a["lon"], "geo_label": a["label"],
                                "operator": "JD"})
    assert got["lat"] == a["lat"] and got["geo_alts"] == "" and got["address"] == "135 S Regent St, Port Chester"
