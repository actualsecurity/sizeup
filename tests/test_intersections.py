"""Intersection search (#213): "N Main St/Westchester Ave", "n main & west" in the CAD's location box, the incident's
Edit box and a note's "@". Each side a beginning of a street name; a directional typed has to match; our towns first.

Run on a tiny recorded extract of real TIGER street names and meeting points (tests/fixtures/gazetteer/
intersections_213.json: Port Chester, Rye Brook and the towns around), so a gazetteer rebuild can't move them."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sizeup.app import App
from sizeup.cad.dispatcher import where
from sizeup.config import ConfigError, load
from sizeup.geo import intersections as I
from sizeup.geo.gazetteer import SCHEMA, Gazetteer, Match
from sizeup.geo.intersections import LIMIT, TOO_SHORT, IntersectionSearch, TooShort, like_prefix, parse
from sizeup.geo.places import PlaceIndex
from tests.test_cad import make_cad

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = Path(__file__).parent / "fixtures" / "gazetteer" / "intersections_213.json"
STATIC = ROOT / "sizeup" / "web" / "static"
CORNER = (41.000296, -73.663861)          # N Main St / King St / Westchester Ave, Port Chester (TIGER)


@pytest.fixture(scope="module")
def gaz(tmp_path_factory):
    d = json.loads(EXTRACT.read_text())
    path = tmp_path_factory.mktemp("gaz") / "x213.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", d["streets"])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", d["intersections"])
    db.execute("INSERT INTO landmarks VALUES (1, 'Whole Foods Market', 'shop=supermarket', '575 Boston Post Road', "
               "'Port Chester', 40.99242, -73.675505, 'osm', 'whole foods market|whole foods')")
    db.commit()
    db.close()
    return Gazetteer(path)


@pytest.fixture(scope="module")
def x(gaz):
    return IntersectionSearch(gaz)


def labels(r):
    return [m["label"] for m in r["results"]]


# --------------------------------------------------------------------------- reading what was typed


@pytest.mark.parametrize("text, a, b, sep", [
    ("N Main St/Westchester Ave", "n main st", "westchester ave", "/"),
    ("n main & westchester", "n main", "westchester", "&"),
    ("N Main%/West%", "n main", "west", "/"),                  # another CAD's habit: the "%" is dropped
    ("main*/west*", "main", "west", "/"),
    ("n_main / west", "n main", "west", "/"),
    ("King St and Westchester Av", "king st", "westchester ave", "and"),
    ("S Regent at W William", "s regent", "w william", "at"),
    ("King Street @ Westchester Avenue", "king st", "westchester ave", "@"),
    ("North Main / WEST", "north main", "west", "/"),
])
def test_two_streets_and_a_separator(text, a, b, sep):
    q = parse(text)
    assert (q.a.phrase, q.b.phrase, q.sep) == (a, b, sep)


def test_the_directional_typed_first():
    assert parse("N Main/West").a.dir == "n" and parse("North Main/West").a.dir == "n"
    assert parse("N Main/West").b.dir == ""                        # "West" alone is the name's beginning
    assert parse("North St/King").a.dir == ""                      # "North St" is a name, not N + a type


@pytest.mark.parametrize("text", [
    "", "135 S Regent St", "King St", "King St &", "& King St", "@ Whole Foods Market",
    "575 Boston Post Rd @ Whole Foods Market",                     # a named place's address (#204)
    "135 S Regent St / W William St",                              # an address with its cross street
    "5 @ Lyon Pa",                                                 # a house number typed in front of a place
    "King St / Westchester Ave / Willett Ave",                     # three streets: not one corner
    "King St / " + "Westchester Ave " * 6,                         # longer than a box's intersection (80)
])
def test_not_two_streets(text):
    assert parse(text) is None


def test_an_at_sign_before_a_named_place_is_the_place():
    assert parse("King St @ Whole Foods Market", is_place=lambda t: t.lower() == "whole foods market") is None
    assert parse("King St @ Westchester Ave", is_place=lambda t: False).sep == "@"


@pytest.mark.parametrize("text", ["%/%", "*/*", "_ & _", "%%%/west", "N Main / W", "n main / %w%", "a / b"])
def test_a_street_under_two_letters_says_what_to_type(text, x):
    with pytest.raises(TooShort):
        parse(text)
    r = x.search(text)
    assert r["error"] == TOO_SHORT == "type at least 2 letters per street" and r["results"] == []


# --------------------------------------------------------------------------- matching


def test_the_examples(x):
    for text in ("N Main St/Westchester Ave", "n main & westchester", "N Main/West", "N Main%/West%"):
        r = x.search(text)
        assert labels(r) == ["N Main St & Westchester Ave, Port Chester"], text
        m = r["results"][0]
        assert (m["lat"], m["lon"]) == CORNER and m["streets"] == ["N Main St", "Westchester Ave"]
        assert m["town"] == m["municipality"] == "Port Chester" and m["kind"] == "intersection"
    assert labels(x.search("King St and Westchester Av")) == ["King St & Westchester Ave, Port Chester"]
    assert labels(x.search("S Regent St & W William St")) == ["S Regent St & W William St, Port Chester"]
    assert labels(x.search("Westchester Ave @ King St")) == ["Westchester Ave & King St, Port Chester"]  # as typed


def test_the_old_wildcards_find_what_plain_typing_finds(x):
    assert x.search("N Main%/West%") == x.search("N Main/West") == x.search("n main*/west*")
    assert x.search("main*/west*") == x.search("main/west") == x.search("%main%/%west%")


def test_a_directional_typed_has_to_match(x):
    assert labels(x.search("N Main / Westchester")) == ["N Main St & Westchester Ave, Port Chester"]
    assert labels(x.search("S Main / Westchester")) == ["S Main St & Westchester Ave, Port Chester"]
    assert labels(x.search("South Main / Westchester")) == ["S Main St & Westchester Ave, Port Chester"]
    both = labels(x.search("Main / Westchester"))                 # none typed: either
    assert set(both) == {"N Main St & Westchester Ave, Port Chester", "S Main St & Westchester Ave, Port Chester"}
    assert x.search("E William / S Regent")["results"] == []        # only W William St meets it
    assert labels(x.search("William / Regent")) == ["W William St & S Regent St, Port Chester"]
    # "S Westchester" is never N Westchester Ave
    assert {m["streets"][1] for m in x.search("Purchase / S Westchester")["results"]} == {"S Westchester Ave"}


def test_beginnings_of_names_any_case_abbreviations_spelled_out(x):
    for text in ("KING / WESTCH", "king street & westchester avenue", "king st/westchester aven", "ki / westchester a"):
        assert "King St & Westchester Ave, Port Chester" in labels(x.search(text)), text
    assert "Willett Ave & State Rte 120A, Port Chester" in labels(x.search("willett / rte 120a"))
    assert "Willett Ave & State Rte 120A, Port Chester" in labels(x.search("willett / route 120a"))
    assert "Maple Ave & US Hwy 1, Greenwich" in labels(x.search("maple / us 1"))
    assert "Weaver St & Boston Post Rd, Town of Mamaroneck" in labels(x.search("weaver / post rd"))


def test_a_pair_that_meets_twice_is_two_results(x):
    r = x.search("Putnam Ave / Putnam Dr")
    assert labels(r) == ["Putnam Ave & Putnam Dr, Port Chester"] * 2
    assert len({(m["lat"], m["lon"]) for m in r["results"]}) == 2
    assert len(x.search("roanoke / westview")["results"]) == 2
    # a corner on the town line is one result (ours), the pair's other corner another
    r = x.search("beechwood / woodland")
    assert [(m["town"], m["lat"]) for m in r["results"]] == [("Port Chester", 41.022436), ("Rye Brook", 41.02425)]
    assert labels(x.search("king / arbor")) == ["King St & Arbor Dr, Rye Brook"]          # not again as Greenwich's


# --------------------------------------------------------------------------- the area, the order, the cap


def test_our_towns_first_then_nearest_headquarters(x):
    r = x.search("king / ma")                                     # N Main, S Main (PC), Maple Ave (Harrison)
    assert [m["town"] for m in r["results"]] == ["Port Chester", "Port Chester", "Harrison"]
    r = x.search("king / wi")                                     # Willett Ave (PC), Willow Run Rd (Greenwich)
    assert labels(r) == ["King St & Willett Ave, Port Chester", "King St & Willow Run Rd, Greenwich"]
    r = x.search("purchase / we")["results"]                     # Harrison's three, nearest HQ first
    assert [m["streets"][1] for m in r] == ["S Westchester Ave", "N Westchester Ave", "Westerleigh Rd"]
    assert [m["km"] for m in r] == sorted(m["km"] for m in r)
    r = x.search("boston post / wo")["results"]                   # Woodland Dr, Rye; not Port Chester's (none)
    assert [(m["streets"][1], m["town"]) for m in r] == [("Woodland Dr", "Rye")]
    # every result: ours (Port Chester, then Rye Brook) first, then by distance from HQ, whole names first
    for text in ("king / ma", "bo / pu", "pu / we", "ki / pu", "n regent / pu", "be / wo"):
        res = x.search(text)
        assert res["results"], text
        keys = [x._rank(m) for m in res["results"]]
        assert keys == sorted(keys), text


def test_whole_names_before_beginnings(x):
    # "west" is the whole name of West St (its type left off) and the beginning of Westchester Ave
    r = x.search("s regent / west")["results"]
    assert [m["streets"][1] for m in r] == ["West St", "Westchester Ave"]
    assert [m["exact"] for m in r] == [2, 1] and r[0]["km"] > r[1]["km"]   # though farther from HQ
    assert [m["confidence"] for m in r] == [1.0, 0.9]


def test_only_the_towns_searched(gaz):
    ours = IntersectionSearch(gaz, towns=("Port Chester", "Rye Brook"))
    assert [m["town"] for m in ours.search("king / ma")["results"]] == ["Port Chester", "Port Chester"]
    assert ours.search("boston post / weaver")["results"] == []
    assert labels(IntersectionSearch(gaz, towns=("Greenwich",)).search("king / arbor")) == ["King St & Arbor Dr, Greenwich"]
    assert I.SEARCH_TOWNS == ("Port Chester", "Rye Brook", "Rye", "Harrison", "Greenwich", "Mamaroneck", "Larchmont",
                              "Town of Mamaroneck")


def test_at_most_25_and_how_many_more(x):
    assert LIMIT == 25
    r = x.search("king / ma", limit=2)
    assert (len(r["results"]), r["total"], r["more"]) == (2, 3, 1)
    assert x.search("king / ma")["more"] == 0


# --------------------------------------------------------------------------- safety


class Recording:
    """The search's connection, every statement it runs recorded."""

    def __init__(self, db):
        self.db, self.ran = db, []

    def execute(self, sql, params=()):
        self.ran.append((sql, params))
        return self.db.execute(sql, params)


@pytest.mark.parametrize("text", [
    "a'; drop table x;--/b", "a'; drop table names;--/bb", "%' OR 1=1 --/xx", "x' UNION SELECT 1, 2, 3 --/king",
    "king\\/we\\", "king st'/westchester\"", "king st/west%' --", "ki%_%ng / we", "king\x00/west", "Robert'); DROP TABLE streets;--/king",
])
def test_injection_attempts_are_just_text(gaz, text):
    x = IntersectionSearch(gaz)
    rec = x.db = Recording(x.db)
    r = x.search(text)
    assert r is None or isinstance(r["results"], list)            # no error escapes, nothing made up
    if r and not r.get("error"):
        assert all(m["kind"] == "intersection" for m in r["results"])
    for sql, params in rec.ran:
        assert sql in (I._SQL_ANY, I._SQL_WHOLE, I._SQL_DIR)        # the constant statements, nothing else
        assert all(p.endswith("%") or p in "nsew" for p in params)
    assert x.db.db.execute("SELECT count(*) FROM names").fetchone()[0] > 0    # still there
    assert labels(x.search("king / westchester")) == ["King St & Westchester Ave, Port Chester"]


def test_the_injections_find_nothing(x):
    assert x.search("%' OR 1=1 --/xx")["results"] == []
    assert x.search("a'; drop table names;--/bb")["results"] == []
    assert x.search("a'; drop table x;--/b")["error"] == TOO_SHORT


def test_like_patterns_are_escaped_and_literal():
    assert like_prefix("n main") == "n main%"
    assert like_prefix("a%b_c\\") == "a\\%b\\_c\\\\%"
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (v TEXT)")
    db.executemany("INSERT INTO t VALUES (?)", [("a%b",), ("axb",), ("a_c",), ("abc",)])
    assert [r[0] for r in db.execute("SELECT v FROM t WHERE v LIKE ? ESCAPE '\\'", (like_prefix("a%"),))] == ["a%b"]
    assert [r[0] for r in db.execute("SELECT v FROM t WHERE v LIKE ? ESCAPE '\\'", (like_prefix("a_"),))] == ["a_c"]


def test_no_sql_is_built_from_text():
    src = (ROOT / "sizeup" / "geo" / "intersections.py").read_text()
    assert "execute(f" not in src and ".format(" not in src and "% (" not in src
    assert "ESCAPE" in I._SQL_ANY and "?" in I._SQL_ANY


def test_the_name_index_is_used(x):
    plan = " ".join(str(r) for r in x.db.execute("EXPLAIN QUERY PLAN " + I._SQL_ANY, ("main%",)))
    assert "names_variant" in plan


# --------------------------------------------------------------------------- the CAD: the location box, Edit, "@"


def wired(tmp_path, gaz):
    def geocoder(q):
        return [{k: getattr(m, k) for k in ("label", "lat", "lon", "kind", "municipality", "precision")}
                | {"confidence": round(float(m.confidence), 2)} | App._geocode_parts(m) for m in gaz.candidates(q)[:20]]
    cad = make_cad(tmp_path, geocoder=geocoder)
    cad.cross_source = lambda: IntersectionSearch(gaz)
    cad.place_source = lambda: PlaceIndex(gaz, crossings=cad.crossings())
    return cad


def test_the_location_box(tmp_path, gaz):
    cad = wired(tmp_path, gaz)
    r = cad.geocode("N Main%/West%")
    m = r["matches"][0]
    assert (m["label"], m["name"], m["town"]) == ("N Main St & Westchester Ave, Port Chester",
                                                 "N Main St & Westchester Ave", "Port Chester")
    assert (m["lat"], m["lon"]) == CORNER and m["streets"] == ["N Main St", "Westchester Ave"]
    assert m["cross"] == "N Main St and Westchester Ave"           # the form's Cross streets
    assert (r["total"], r["more"]) == (1, 0)
    assert cad.geocode("%/%") == {"matches": [], "error": TOO_SHORT}
    assert isinstance(cad.geocode("a'; drop table names;--/bb")["matches"], list)
    # not two streets, or nowhere they meet: the box's usual suggestions
    assert "error" not in cad.geocode("King St &") and "more" not in cad.geocode("King St")
    assert "more" not in cad.geocode("king st / nowhere ln")
    assert "more" not in cad.geocode("King St @ Whole Foods Market")        # the place, not a street


def test_the_place_search_built_with_the_intersection_search_does_not_wait_on_itself(tmp_path, gaz):
    import threading
    cad = wired(tmp_path, gaz)                    # place_source asks crossings(), as the app wires it
    t = threading.Thread(target=cad.place_index, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and cad.place_index().crossings is cad.crossings()


def test_at_most_25_in_the_box(tmp_path, gaz, monkeypatch):
    cad = wired(tmp_path, gaz)
    real = IntersectionSearch.search
    monkeypatch.setattr(IntersectionSearch, "search", lambda self, t, limit=LIMIT, is_place=None: real(self, t, 2, is_place))
    r = cad.geocode("king / ma")
    assert len(r["matches"]) == 2 and (r["total"], r["more"]) == (3, 1)


def test_an_intersection_picked_is_stored_with_its_streets_town_and_point(tmp_path, gaz):
    cad = wired(tmp_path, gaz)
    m = cad.geocode("king st and westchester av")["matches"][0]
    inc = cad.create({"type": "SERVICE", "subtype": "LIFT", "operator": "JD", "address": m["label"],
                      "cross_street": m["cross"], "lat": m["lat"], "lon": m["lon"], "geo_label": m["label"]})
    assert (inc["address"], inc["cross_street"], inc["lat"], inc["lon"]) == \
        ("King St & Westchester Ave, Port Chester", "King St and Westchester Ave", *CORNER)
    assert cad.lists.event_town(inc) == "Port Chester"
    # said once on the air, not again as its cross streets
    w = where(inc)
    assert "cross of" not in w and w.count("King") == 1
    assert "cross of" in where({"address": "135 S Regent St", "cross_street": "Clinton St and W William St"})


def test_the_gazetteer_s_own_intersections_fill_the_cross_streets_too(tmp_path, gaz):
    m = Match("intersection", 41.0, -73.66, "King St & Willett Ave, Port Chester", "Port Chester", 0.9,
              cross_street="Willett Ave", street="King St", precision="intersection")
    assert App._geocode_parts(m)["streets"] == ["King St", "Willett Ave"]
    cad = wired(tmp_path, gaz)
    d = cad._detailed({"kind": "intersection", "label": m.label, "lat": 41.0, "lon": -73.66,
                       "streets": ["King St", "Willett Ave"]})
    assert d["cross"] == "King St and Willett Ave"


def test_a_note_s_at_search(gaz):
    ix = PlaceIndex(gaz, crossings=IntersectionSearch(gaz))
    hits = ix.search("n main / west")
    assert [(h["kind"], h["label"], h["sub"]) for h in hits] == [("intersection", "N Main St & Westchester Ave", "Port Chester")]
    assert [h["label"] for h in ix.search("king / ma", limit=2)] == ["King St & N Main St", "King St & S Main St"]
    assert ix.names_place("Whole Foods Market") and not ix.names_place("Westchester Ave")
    assert any(h["kind"] == "place" for h in ix.search("whole foods"))          # everything else as before


# --------------------------------------------------------------------------- config and the page


def test_the_search_towns_come_from_config(tmp_path):
    ex = load(ROOT / "config.example.toml")
    assert ex.geo_search_towns == I.SEARCH_TOWNS and ex.geo_hq == I.HQ
    f = tmp_path / "c.toml"
    f.write_text("")
    assert load(f).geo_search_towns == I.SEARCH_TOWNS                     # left out: the defaults
    f.write_text('[geo]\nsearch_towns = ["Port Chester", "Rye Brook"]\nhq_lat = 41.0\nhq_lon = -73.67\n')
    c = load(f)
    assert c.geo_search_towns == ("Port Chester", "Rye Brook") and c.geo_hq == (41.0, -73.67)
    for bad in ('search_towns = [3]', 'search_towns = []', 'search_towns = "Rye"', 'hq_lat = 12.0\nhq_lon = -73.6',
                'hq_lat = 41.0', 'towns = ["Rye"]'):
        f.write_text(f"[geo]\n{bad}\n")
        with pytest.raises(ConfigError):
            load(f)


def test_the_box_says_nothing_about_wildcards():
    html = (STATIC / "cad.html").read_text()
    js = (STATIC / "cad.js").read_text()
    for box in ('id="f-address"', 'id="e-address"'):
        tag = html[html.index(box):html.index(">", html.index(box))]
        assert "N Main / Westchester" in tag and "%" not in tag and "*" not in tag
    assert "+${box.more} more of ${box.total}" in js            # the cap shown
    assert "meet ? m.name : m.label" in js                            # "N Main St & Westchester Ave" · its town
    assert "wildcard" not in (html + js).lower()
