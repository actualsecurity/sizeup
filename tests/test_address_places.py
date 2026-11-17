"""An address typed in a location box with the department's names at it (the county's common places, a hand-made name
pinned at an address): each name its own suggestion right before the plain address ("120 Example Ave @ Example Burger",
"120 Example Ave @ Sample Fitness", then "120 Example Ave"), picked like the place by its name. The place search (a
note's "@", GET /api/cad/places), the location box (GET /api/cad/geocode) and a click on the new call's map (GET
/api/cad/geocode/here). sizeup/geo/places.py (address_keys, AddressPlaces, PlaceIndex.places_at), sizeup/cad/service.py.

Invented data only (this repository is public): the synthetic gazetteer of tests/test_common_places.py (Example Ave,
Sample St) and invented names, imported through the real import (a spreadsheet, as the county's list comes)."""

from __future__ import annotations

import json
import sqlite3
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cad import commonplaces as cp
from sizeup.cad.api import CadApi
from sizeup.geo.places import AddressPlaces, PlaceIndex, address_keys
from sizeup.geo.gazetteer import Gazetteer
from tests.test_common_places import HEADER, PC, RB, b64, gaz, synthetic, xlsx  # noqa: F401  (gaz: the fixture)
from tests.test_new_call import wired
from tests.test_settings import post

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
CODE = PC.upper().replace(" ", "_")             # the county's town code for the synthetic town
ROWS = [HEADER,
        ["120 EXAMPLE AVE", "SAMPLE FITNESS", CODE],
        ["120 EXAMPLE AVENUE", "EXAMPLE BURGER", CODE],          # the same address spelled out
        ["124 EXAMPLE AVE", "SAMPLE HALL", CODE],                # one site: two buildings next door
        ["128 EXAMPLE AVE", "SAMPLE HALL", CODE],
        ["132-136 EXAMPLE AVE", "TEST PLAZA", CODE],             # a range
        ["7 NOWHERE RD", "QUIET GROVE", CODE]]                   # not found: no point, never offered


@pytest.fixture()
def cad(tmp_path, gaz):  # noqa: F811
    c = wired(tmp_path, gaz)
    post(c, "common_places", action="apply", file=b64(xlsx(ROWS)), filename="list.xlsx")
    return c


def places(cad, q, limit=8):
    code, raw, _ = CadApi(cad).get("/api/cad/places", f"q={q.replace(' ', '+')}&limit={limit}", None)
    assert code == 200
    return json.loads(raw)["places"]


# --------------------------------------------------------------------------- the address as keys

@pytest.mark.parametrize("text, keys", [
    ("12 Example Avenue", [(12, "", "", ("example", "ave"))]),
    ("12 EXAMPLE AVE, Sample Town", [(12, "", "", ("example", "ave"))]),          # a town after a comma: not the street
    ("12A Example Ave", [(12, "a", "", ("example", "ave"))]),
    ("189-1/2 Example St", [(189, "", "1/2", ("example", "st"))]),                # localaddr's fractions (#586)
    ("189 1/2 Example St", [(189, "", "1/2", ("example", "st"))]),
    ("149-151 EXAMPLE AVE", [(149, "", "", ("example", "ave")), (151, "", "", ("example", "ave"))]),
    ("132-136 EXAMPLE AVE", [(n, "", "", ("example", "ave")) for n in (132, 134, 136)]),
    ("151-149 EXAMPLE AVE", [(149, "", "", ("example", "ave")), (151, "", "", ("example", "ave"))]),   # reversed
    ("149-51 EXAMPLE AVE", [(149, "", "", ("example", "ave")), (151, "", "", ("example", "ave"))]),    # the end's digits
    ("120-123 EXAMPLE AVE", [(n, "", "", ("example", "ave")) for n in (120, 121, 122, 123)]),          # both sides
    ("100-160 EXAMPLE AVE", [(100, "", "", ("example", "ave")), (160, "", "", ("example", "ave"))]),   # long: the ends
    ("North Example Ave", []), ("12", []), ("", []),
])
def test_an_address_is_found_by_its_number_and_street_words(text, keys):
    assert address_keys(text) == keys


# --------------------------------------------------------------------------- the place search (a note's "@")

def test_each_name_at_the_address_typed_comes_first_then_the_address(cad):
    hits = places(cad, "120 example ave")
    assert [(h["kind"], h["label"]) for h in hits[:3]] == [("place", "Example Burger"), ("place", "Sample Fitness"),
                                                           ("address", "120 Example Ave")]
    for h in hits[:2]:
        assert (h["at"], h["source"], h["sub"]) == ("120 Example Ave", "local", PC)
    # while still typing the street: the same
    assert [h["label"] for h in places(cad, "120 exam")[:3]] == ["Example Burger", "Sample Fitness", "120 Example Ave"]
    # another number: none of them
    assert [h["kind"] for h in places(cad, "116 example ave")][:1] == ["address"]
    assert not any(h.get("at") for h in places(cad, "116 example ave"))


def test_a_name_typed_is_found_as_before(cad):
    first = places(cad, "sample fitness")[0]
    assert (first["label"], first["kind"], first.get("at")) == ("Sample Fitness", "place", None)


def test_a_site_is_offered_at_each_of_its_addresses_once(cad):
    for n in (124, 128):
        hits = places(cad, f"{n} example ave")
        assert [h["label"] for h in hits[:2]] == ["Sample Hall", f"{n} Example Ave"]
        assert sum(h["label"] == "Sample Hall" for h in hits) == 1
    # a range: any number of it
    for n in (132, 134, 136):
        assert [h["label"] for h in places(cad, f"{n} example ave")[:2]] == ["Test Plaza", f"{n} Example Ave"]


def test_the_limit_keeps_the_address(cad):
    assert [h["label"] for h in places(cad, "120 example ave", limit=2)] == ["Example Burger", "120 Example Ave"]
    assert [h["label"] for h in places(cad, "120 example ave", limit=1)] == ["120 Example Ave"]


def test_a_hand_made_name_pinned_at_an_address(cad):
    pt = next(h for h in places(cad, "140 example ave") if h["kind"] == "address")
    post(cad, "places", action="set", name="the Annex", place={"label": pt["label"], "lat": pt["lat"], "lon": pt["lon"],
                                                                 "sub": PC})
    hits = places(cad, "140 example ave")
    assert [(h["label"], h.get("at")) for h in hits[:2]] == [("the Annex", "140 Example Ave"), ("140 Example Ave", None)]


# --------------------------------------------------------------------------- what is matched, unit

def site_rows(rows):
    """(name, address, lat, lon) as the import's entries, sites grouped, keyed; the located ones (what is searched)."""
    out = {cp.entry_key(n, a): {"name": n, "aliases": [], "label": n.title(), "address": a, "lat": la, "lon": lo,
                                "muni": PC, "located": "exact" if la is not None else "", "source": cp.SOURCE}
           for n, a, la, lo in rows}
    cp.group_sites(out)
    return [dict(v, key=k) for k, v in out.items() if v["lat"] is not None]


def test_only_near_the_address_and_a_sites_address_with_no_point_gets_its_primary():
    at = (41.0, -73.68)
    rows = site_rows([("SAMPLE HALL", "120 EXAMPLE AVE", *at), ("SAMPLE HALL", "126 EXAMPLE AVE", None, None),
                      ("FAR DINER", "120 EXAMPLE AVE", 41.03, -73.66)])             # another town's 120, ~3.5 km
    ix = AddressPlaces(rows)
    assert [n for _r, n in ix.at("120 Example Ave", *at)] == ["Sample Hall"]
    got = ix.at("126 Example Ave", *at)                       # no point of its own: the site's (its primary's)
    assert [(r["address"], n) for r, n in got] == [("120 EXAMPLE AVE", "Sample Hall")]
    assert [n for _r, n in ix.at("120 Example Ave", 41.03, -73.66)] == ["Far Diner"]
    assert ix.at("120 Example St", *at) == [] and ix.at("120A Example Ave", *at) == []


def test_the_gazetteers_spelling_counts_only_for_the_entrys_own_number():
    """The import's at (the address point's label) of a fractional number is its whole number's: "189 Example St" is
    not the place at "189-1/2"; a spelling of the same number ("Avenue" / "Ave") still is."""
    at = (41.0, -73.68)
    rows = [{"key": "county/sample rear @ 189 1 2 example st", "name": "SAMPLE REAR", "label": "Sample Rear",
             "address": "189-1/2 EXAMPLE ST", "at": "189 Example St", "lat": at[0], "lon": at[1], "muni": PC},
            {"key": "county/sample front @ 12 example avenue", "name": "SAMPLE FRONT", "label": "Sample Front",
             "address": "12 EXAMPLE AVENUE", "at": "12 Example Ave", "lat": at[0], "lon": at[1], "muni": PC},
            {"key": "county/sample odd @ 7 exmpl st", "name": "SAMPLE ODD", "label": "Sample Odd",
             "address": "SEVEN EXMPL ST", "at": "7 Example St", "lat": at[0], "lon": at[1], "muni": PC}]
    ix = AddressPlaces(rows)
    assert ix.at("189 Example St", *at) == []
    assert [n for _r, n in ix.at("189 1/2 Example St", *at)] == ["Sample Rear"]
    assert [n for _r, n in ix.at("12 Example Ave", *at)] == ["Sample Front"]
    assert [n for _r, n in ix.at("7 Example St", *at)] == ["Sample Odd"]          # no number read: the spelling's


def test_another_towns_name_across_the_town_line_is_not_it():
    """The same street in two towns, ~300 m apart (inside SAME_ADDRESS_KM): a name counts only in its own town."""
    here, there = (41.0, -73.68), (41.0027, -73.68)
    rows = [{"key": "county/sample fitness @ 120 example ave", "name": "SAMPLE FITNESS", "label": "Sample Fitness",
             "address": "120 EXAMPLE AVE", "at": "120 Example Ave", "lat": here[0], "lon": here[1], "muni": PC}]
    ix = AddressPlaces(rows)
    assert [n for _r, n in ix.at("120 Example Ave", *there)] == ["Sample Fitness"]          # by distance alone
    assert ix.at("120 Example Ave", *there, town=RB) == []
    assert [n for _r, n in ix.at("120 Example Ave", *here, town=PC)] == ["Sample Fitness"]
    assert ix.at("120 Example Ave", *there, town="rb", town_of={"rb": RB}.get) == []         # the CAD's name for it
    assert [n for _r, n in ix.at("120 Example Ave", *here, town="")] == ["Sample Fitness"]  # unknown: distance only


@pytest.fixture()
def two_towns(tmp_path):
    """The synthetic gazetteer with 120 Example Ave in a second town too, ~300 m north across the town line."""
    path = synthetic(tmp_path / "two.sqlite")
    db = sqlite3.connect(path)
    lat, lon = db.execute("SELECT lat, lon FROM addresses WHERE street_id = 1 AND number = 120").fetchone()
    db.execute("INSERT INTO addresses VALUES (1, 120, '', ?, ?, ?, 1)", (RB, lat + 0.0027, lon))
    db.commit()
    db.close()
    return Gazetteer(path), (lat + 0.0027, lon)


def test_the_place_search_puts_the_names_only_before_their_own_towns_address(tmp_path, two_towns):
    g, there = two_towns
    c = wired(tmp_path, g)
    post(c, "common_places", action="apply", file=b64(xlsx(ROWS)), filename="list.xlsx")
    code, raw, _ = CadApi(c).get("/api/cad/places", f"q=120+example+ave&near={there[0]},{there[1]}", None)
    hits = json.loads(raw)["places"]
    assert [(h["label"], h["sub"]) for h in hits[:4]] == [("120 Example Ave", RB), ("Example Burger", PC),
                                                          ("Sample Fitness", PC), ("120 Example Ave", PC)]


def test_only_the_place_search_and_the_location_box_get_the_names(cad):
    # a location typed and not picked (Cad._auto_geo), the location box's own slots: the plain search
    assert not any(h.get("at") for h in cad._place_hits("120 example ave"))
    ix = cad.place_index()
    assert not any(h.get("at") for h in ix.search("120 example ave", extra=cad.local_places()))
    assert [h["label"] for h in ix.search("120 example ave", extra=cad.local_places(), at_address=True)][:2] == \
        ["Example Burger", "Sample Fitness"]
    inc = cad.create({"type": "SERVICE", "subtype": "LIFT", "operator": "JD", "address": "120 Example Ave"})
    assert inc["geo_label"] in (None, "") or "Example Burger" not in inc["geo_label"]


def test_no_names_no_change(gaz):  # noqa: F811
    ix = PlaceIndex(gaz)
    assert ix.search("120 example ave") == ix.search("120 example ave", extra=[])
    assert ix.search("120 example ave")[0]["label"] == "120 Example Ave"


# --------------------------------------------------------------------------- the location box

def test_the_location_box_offers_each_name_then_the_address(cad):
    ms = cad.geocode("120 example ave")["matches"]
    assert [(m["kind"], m.get("place"), m.get("at_address")) for m in ms[:3]] == [
        ("place", "Example Burger", "120 Example Ave"), ("place", "Sample Fitness", "120 Example Ave"),
        ("address", "", None)]
    burger, address = ms[0], ms[2]
    # picked: the place by its name at the address typed ("120 Example Ave @ Example Burger"), its point, its block
    assert burger["street_address"] == "120 Example Ave" and burger["town"] == PC
    assert burger["confidence"] == address["confidence"]
    by_name = next(m for m in cad.geocode("example burger")["matches"] if m.get("place") == "Example Burger")
    assert (burger["lat"], burger["lon"], burger["street_address"]) == (by_name["lat"], by_name["lon"],
                                                                         by_name["street_address"])
    assert burger["cross"] == address["cross"]
    # the incident made from it: the address and the place apart, as for a place picked by its name
    inc = cad.create({"type": "SERVICE", "subtype": "LIFT", "operator": "JD", "address": "120 Example Ave @ Example Burger",
                      "place": "Example Burger", "lat": burger["lat"], "lon": burger["lon"], "geo_label": burger["label"]})
    assert inc["place"] == "Example Burger"


def test_a_map_click_on_the_address_lists_the_names_there(cad, gaz):  # noqa: F811
    pt = next(h for h in places(cad, "120 example ave") if h["kind"] == "address")
    code, raw, _ = CadApi(cad).get("/api/cad/geocode/here", f"lat={pt['lat']}&lon={pt['lon']}", None)
    r = json.loads(raw)
    assert code == 200 and r["address"]["street_address"] == "120 Example Ave"
    assert [(m["place"], m["at_address"], m["street_address"]) for m in r["at_address"]] == [
        ("Example Burger", "120 Example Ave", "120 Example Ave"), ("Sample Fitness", "120 Example Ave", "120 Example Ave")]
    assert r["place"] is None or r["place"]["place"] not in ("Example Burger", "Sample Fitness")
    other = next(h for h in places(cad, "100 example ave") if h["kind"] == "address")
    assert json.loads(CadApi(cad).get("/api/cad/geocode/here", f"lat={other['lat']}&lon={other['lon']}", None)[1])[
        "at_address"] == []


# --------------------------------------------------------------------------- the pages (node)

DRAW = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const pick = (name) => {
  const i = src.indexOf(`function ${name}(`);
  let j = src.indexOf("{", src.indexOf(")", i)) + 1, d = 1;
  while (d) { d += { "{": 1, "}": -1 }[src[j]] || 0; j++; }
  return src.slice(i, j);
};
const el = (tag, cls, text) => ({ tag, cls, text });
eval(pick("locationText") + pick("suggestionHead"));
const m = { kind: "place", place: "Example Burger", street_address: "120 Example Ave", at_address: "120 Example Ave" };
const byName = { kind: "place", place: "Example Burger", street_address: "120 Example Ave" };
const noAddr = { kind: "place", place: "Example Burger", street_address: "" };
const note = { kind: "place", label: "Example Burger", at: "120 Example Ave" };
console.log(JSON.stringify({ at: suggestionHead(m), byName: suggestionHead(byName), noAddr: suggestionHead(noAddr),
  plain: suggestionHead({ kind: "address", label: "120 Example Ave" }), note: suggestionHead(note),
  text: locationText(m) }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_suggestion_reads_address_at_place(tmp_path):
    h = tmp_path / "draw.js"
    h.write_text(DRAW)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True, check=True,
                                    timeout=30).stdout)
    assert out["at"] == [{"tag": "b", "cls": "", "text": "120 Example Ave"}, {"tag": "span", "cls": "sg-at", "text": "@ Example Burger"}]
    assert out["note"] == out["at"]                                  # a note's "@" list: the same line
    assert out["byName"] == [{"tag": "b", "cls": "", "text": "Example Burger"},
                             {"tag": "span", "cls": "sg-addr", "text": "· 120 Example Ave"}]       # as before
    assert out["noAddr"][1] == {"tag": "span", "cls": "sg-addr dim", "text": "· no street address"}
    assert out["plain"] == [{"tag": "b", "cls": "", "text": "120 Example Ave"}]
    assert out["text"] == "120 Example Ave @ Example Burger"          # what the field says once picked


def test_the_pages_use_it():
    js = (STATIC / "cad.js").read_text()
    box = js[js.index("function addressBox("):js.index("// ---- new incident / edit")]
    assert "li.append(...suggestionHead(m, meet));" in box
    note = js[js.index("function suggestNote("):js.index("function takeItem(")]
    assert "li.append(...suggestionHead(p));" in note
    assert "li.append(...suggestionHead(p));" in (STATIC / "settings.js").read_text()      # Settings' place picker
    nc = (STATIC / "newcall-map.js").read_text()
    assert "for (const p of r.at_address || [])" in nc
    assert ".sg-at" in (STATIC / "cad.css").read_text()
