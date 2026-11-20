"""Location qualifiers (Settings > Lists > Location qualifiers, sizeup/cad/lists.py): "In front of" / "IFO" / "in front of"
in front of an address. The list's edits (add, rename, reorder, retire, delete), an event's and a hydrant's
location_qualifier (stored by code, shown "IFO 12 Sample St", never a point moved), the dispatch voice ("respond, in front
of 12 Sample Street"), a page's transcript read for one (sizeup/dispatch.py parse) and the hydrant CSV.

Everything here is invented: "Sample St", "Example Ave", the "Sample Market", Sampletown; no real place or call."""
import csv
import io
import json

import pytest

from sizeup.cad import dispatcher, lists
from sizeup.cad.store import CadError
from sizeup.dispatch import parse
from tests.test_cad import Clock, local, make_cad, monitor_incident, uid
from tests.test_hydrants import fake_gis, served, wire

PT = {"lat": 41.0, "lon": -73.66, "geo_label": "12 Sample St, Sampletown"}


def post(cad, section, **body):
    return cad.settings_post(section, dict(body, operator=body.get("operator", "JD")))


def item(cad, code):
    return next(x for x in cad.settings_get()["lists"]["location_qualifiers"]["items"] if x["code"] == code)


def counting_geocoder():
    calls = []

    def geo(q):
        calls.append(q)
        return [{"label": "40 Example Ave, Sampletown", "municipality": "Port Chester", "confidence": 0.95, "lat": 41.01,
                 "lon": -73.65, "kind": "address", "precision": "exact"}]
    return geo, calls


# -- the list ---------------------------------------------------------------------------------------------------------

def test_the_defaults_and_their_three_forms():
    L = lists.load()
    assert [q["code"] for q in L.location_qualifiers] == ["IFO", "ACROSS", "REAR", "CORNER", "OPP", "NEAR", "SIDE", "BETWEEN"]
    ifo = L.qualifier("IFO")
    assert (ifo["name"], ifo["short"], ifo["spoken"]) == ("In front of", "IFO", "in front of")
    assert L.qualified("IFO", "12 Sample St") == "IFO 12 Sample St"                   # the text: the short form
    assert L.qualified("IFO", "12 Sample St", "name") == "In front of 12 Sample St"   # hover
    assert L.qualified("", "12 Sample St") == "12 Sample St"
    # what was picked or typed -> the code: the code, the short form, the name or the spoken form, any case
    assert [L.qualifier_code(x) for x in ("IFO", "ifo", "In front of", "in front of", "COR", "corner of", "")] == [
        "IFO", "IFO", "IFO", "IFO", "CORNER", "CORNER", ""]
    with pytest.raises(ValueError, match="location qualifier: one of IFO, ACROSS"):
        L.qualifier_code("behind")                                     # not free text
    assert L.split_qualifier("across from 40 Example Ave") == ("ACROSS", "40 Example Ave")
    assert L.split_qualifier("IFO 12 Sample St") == ("IFO", "12 Sample St")
    assert L.split_qualifier("Near") == ("", "Near")                     # never the whole text
    assert L.split_qualifier("Nearby Rd") == ("", "Nearby Rd")           # a word, not a prefix of one
    assert L.qualifier_phrases()["across from"] == "ACROSS"
    assert lists.build({**lists.read(), "location_qualifiers": None}, "t").location_qualifiers == L.location_qualifiers
    assert {q["code"] for q in L.public()["location_qualifiers"]} == {q["code"] for q in L.location_qualifiers}


def test_the_data_file_is_checked():
    d = lists.read()
    for bad, why in (({"code": "X", "name": "X", "short": "IFO", "spoken": "x"}, "short: 'IFO' twice"),
                     ({"code": "X", "name": "X", "short": "X", "spoken": "in front of"}, "spoken: 'in front of' twice"),
                     ({"code": "X", "name": "X", "short": "", "spoken": "x"}, "short"),
                     ({"code": "X", "name": "X", "short": "X", "spoken": "behind 2"}, "spoken")):
        with pytest.raises(lists.ListsError, match=why):
            lists.build(dict(d, location_qualifiers=[*d["location_qualifiers"], bad]), "t")


def test_admins_add_rename_reorder_retire_and_delete():
    cad = make_cad()
    assert item(cad, "IFO")["state"] == "default"
    post(cad, "location_qualifiers", action="add", code="BEHIND", item={"name": "Behind", "short": "BHND", "spoken": "Behind"})
    q = cad.lists.qualifier("BEHIND")
    assert (q["name"], q["short"], q["spoken"]) == ("Behind", "BHND", "behind")         # said in lower case
    assert cad.state()["lists"]["location_qualifiers"][-1]["code"] == "BEHIND"          # pushed to the consoles
    post(cad, "location_qualifiers", action="edit", code="BEHIND", item={"name": "Behind of", "short": "B/O"})
    assert cad.lists.qualified("BEHIND", "12 Sample St") == "B/O 12 Sample St"
    post(cad, "location_qualifiers", action="move", code="BEHIND", to=0)
    assert cad.lists.location_qualifiers[0]["code"] == "BEHIND"
    with pytest.raises(CadError, match="spoken"):
        post(cad, "location_qualifiers", action="edit", code="BEHIND", item={"spoken": "behind #2"})
    with pytest.raises(CadError, match="short"):
        post(cad, "location_qualifiers", action="add", code="ODD", item={"name": "Odd", "short": "", "spoken": "odd"})
    with pytest.raises(CadError, match="twice"):        # one spoken form, one meaning
        post(cad, "location_qualifiers", action="add", code="FRONT", item={"name": "Front", "short": "FR", "spoken": "in front of"})
    with pytest.raises(CadError, match="codes don't change"):
        post(cad, "location_qualifiers", action="edit", code="IFO", item={"code": "FRONT", "name": "x"})
    # retired: off the pick lists and the hub's reading, still named on the records that have it
    iid = cad.create({"type": "STRU", "address": "12 Sample St", "location_qualifier": "IFO", **PT})["id"]
    post(cad, "location_qualifiers", action="retire", code="IFO")
    assert "in front of" not in cad.lists.qualifier_phrases()
    assert cad.lists.qualified("IFO", "12 Sample St") == "IFO 12 Sample St"
    with pytest.raises(CadError, match="location qualifier: one of"):
        cad.create({"type": "STRU", "address": "14 Sample St", "location_qualifier": "IFO"})
    cad.update(iid, {"location_qualifier": "IFO", "remarks": "kept", "operator": "JD"})   # the event's own: stays
    assert cad.store.incident(iid)["location_qualifier"] == "IFO"
    # in use: never deleted (retire it); unused: deleted; restore puts it back on the pick lists
    with pytest.raises(CadError, match="used by 1 record"):
        post(cad, "location_qualifiers", action="delete", code="IFO")
    post(cad, "location_qualifiers", action="restore", code="IFO")
    assert "in front of" in cad.lists.qualifier_phrases()
    assert item(cad, "IFO")["used"] == 1 and item(cad, "OPP")["used"] == 0
    post(cad, "location_qualifiers", action="delete", code="OPP")
    assert cad.lists.qualifier("OPP") is None and item(cad, "OPP")["state"] == "deleted"
    post(cad, "location_qualifiers", action="reset", code="OPP")
    assert cad.lists.qualifier("OPP")["short"] == "OPP"
    post(cad, "location_qualifiers", action="delete", code="BEHIND")
    assert cad.lists.qualifier("BEHIND") is None
    audit = [a["key"] for a in cad.store.q("SELECT key FROM settings_audit WHERE section = 'lists'")]
    assert "location_qualifiers/BEHIND" in audit and "location_qualifiers/IFO" in audit


def test_the_edits_survive_a_restart(tmp_path):
    cad = make_cad(tmp_path)
    post(cad, "location_qualifiers", action="edit", code="NEAR", item={"short": "NEAR"})
    assert make_cad(tmp_path).lists.qualified("NEAR", "12 Sample St") == "NEAR 12 Sample St"


# -- an event's location ----------------------------------------------------------------------------------------------

def test_an_event_stores_the_code_and_shows_the_short_form():
    cad = make_cad()
    inc = cad.create({"type": "STRU", "address": "12 Sample St", "location_qualifier": "In front of", **PT, "operator": "JD"})
    assert (inc["location_qualifier"], inc["address"]) == ("IFO", "12 Sample St")      # by its code, the address alone
    assert (inc["lat"], inc["lon"]) == (41.0, -73.66)
    created = cad.store.full(inc["id"])["events"][0]["text"]
    assert "at IFO 12 Sample St" in created
    cad.update(inc["id"], {"location_qualifier": "OPP", "operator": "JD"})
    assert any("location: OPP 12 Sample St" in e["text"] for e in cad.store.full(inc["id"])["events"])
    cad.update(inc["id"], {"location_qualifier": "", "operator": "JD"})
    assert cad.store.incident(inc["id"])["location_qualifier"] == ""
    with pytest.raises(CadError, match="location qualifier"):
        cad.update(inc["id"], {"location_qualifier": "behind", "operator": "JD"})


def test_a_qualifier_typed_in_front_of_the_address_is_taken_off_it_and_never_geocoded():
    geo, calls = counting_geocoder()
    cad = make_cad(geocoder=geo)
    inc = cad.create({"type": "STRU", "address": "across from 40 Example Ave", "operator": "JD"})
    assert (inc["location_qualifier"], inc["address"]) == ("ACROSS", "40 Example Ave")
    assert calls and all("across" not in q.lower() for q in calls)            # the address alone placed it
    assert (inc["lat"], inc["lon"]) == (41.01, -73.65)
    # a picked qualifier wins: the text is left as typed
    inc2 = cad.create({"type": "STRU", "address": "Near 12 Sample St", "location_qualifier": "REAR", **PT})
    assert (inc2["location_qualifier"], inc2["address"]) == ("REAR", "Near 12 Sample St")


def test_the_qualifier_never_moves_the_events_point():
    geo, calls = counting_geocoder()
    cad = make_cad(geocoder=geo)
    iid = cad.create({"type": "STRU", "address": "12 Sample St", **PT})["id"]
    before = {k: cad.store.incident(iid)[k] for k in ("address", "lat", "lon", "geo_label")}
    n = len(calls)
    for q in ("IFO", "ACROSS", "", "between"):
        cad.update(iid, {"location_qualifier": q, "operator": "JD"})
        assert {k: cad.store.incident(iid)[k] for k in before} == before        # the address's point, as it was
    assert len(calls) == n                                                    # never geocoded again


# -- the dispatch voice ----------------------------------------------------------------------------------------------

def test_the_dispatch_says_the_spoken_form():
    cad = make_cad(clock=Clock(local(2026, 11, 20, 15, 13)))
    iid = cad.create({"type": "STRU", "address": "12 Sample St", "location_qualifier": "IFO", **PT})["id"]
    d = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert d["text"].startswith("Port Chester Fire, Engine 61, respond, in front of 12 Sample Street, for a structure fire.")
    assert "in front of twelve Sample Street" in dispatcher.speech(d["text"])
    post(cad, "location_qualifiers", action="edit", code="IFO", item={"spoken": "out front of"})
    pv = cad.preview(iid, {"unit_ids": [uid(cad, "E58")]})
    assert "Out front of 12 Sample Street, structure fire" in pv["text"]   # the list's spoken form, as edited (an add-on)
    # without one: as before
    plain = cad.create({"type": "STRU", "address": "14 Sample St", **PT})["id"]
    assert "respond to 14 Sample Street, for" in cad.preview(plain, {"unit_ids": [uid(cad, "E58")]})["text"]


def test_compose_with_a_qualifier():
    inc = {"address": "40 Example Ave @ Sample Market", "cross_street": "Sample St"}
    new = dispatcher.compose("new", department="Port Chester Fire", incident=inc, units=["Engine 61"], phrase="a structure fire",
                             at=local(2026, 11, 20, 9, 5), qualifier="across from")
    assert new == ("Port Chester Fire, Engine 61, respond, across from 40 Example Avenue, Sample Market, cross of Sample Street, "
                   "for a structure fire. Time out 09:05.")
    up = dispatcher.compose("upgrade", department="Port Chester Fire", incident=inc, units=[], phrase="a structure fire",
                            at=local(2026, 11, 20, 9, 5), qualifier="across from", info="smoke showing")
    assert up.startswith("Port Chester Fire, upgrade on your last alarm, across from 40 Example Avenue, Sample Market")
    add = dispatcher.compose("add_on", department="Port Chester Fire", incident=inc, units=["Engine 58"], phrase="a fire alarm",
                             at=local(2026, 11, 20, 9, 5), qualifier="across from")
    assert "added to the assignment. Across from 40 Example Avenue" in add
    assert dispatcher.where({"address": ""}, "in front of") == ""          # no location: no qualifier said alone


# -- a page's transcript ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, location, qualifier, cross", [
    ("Port Chester Fire, respond to a structure fire, across from 40 Example Avenue, cross of Sample Street. Time out 1432.",
     "40 example avenue", "ACROSS", "sample street"),
    ("Port Chester Fire, respond to a fire alarm in front of 12 Sample Street. Time out 0915.", "12 sample street", "IFO", ""),
    ("Port Chester Fire, fire alarm, rear of Sample Avenue. Time out 1432.", "sample avenue", "REAR", ""),
    ("Port Chester Fire, car fire across from Example Avenue. Time out 1432.", "example avenue", "ACROSS", ""),
    ("Port Chester Fire, structure fire in front of the Sample Market. Time out 1432.", "the sample market", "IFO", ""),
    ("Port Chester Fire, respond to a structure fire, 40 Example Avenue, cross of Sample Street. Time out 1432.",
     "40 example avenue", "", "sample street"),
])
def test_a_page_reads_the_qualifier_apart_from_the_street(text, location, qualifier, cross):
    d = parse(text)
    assert (d.location, d.qualifier, d.cross_street) == (location, qualifier, cross)


def test_the_page_reads_the_lists_own_spoken_forms():
    t = "Port Chester Fire, respond to a structure fire, out back of 40 Example Avenue. Time out 1432."
    assert (parse(t).location, parse(t).qualifier) == ("40 example avenue", "")
    d = parse(t, qualifiers={"out back of": "REAR"})
    assert (d.location, d.qualifier) == ("40 example avenue", "REAR")
    assert parse("across from 40 Example Avenue", qualifiers={}).qualifier == ""      # an empty list: none read


def test_a_page_brings_its_qualifier_to_the_queued_event():
    cad = make_cad()
    geo = {"lat": 41.01, "lon": -73.65, "label": "40 Example Ave, Port Chester", "kind": "address", "confidence": 0.91}
    _, inc = monitor_incident("Port Chester Fire Department, respond to an activated fire alarm, across from 40 Example Avenue, "
                              "cross of Sample Street. Time out 1432.", geo=geo)
    assert inc.qualifier == "ACROSS" and inc.location == "40 example avenue"
    p = cad.from_monitor(inc)
    assert (p["location_qualifier"], p["address"], p["lat"]) == ("ACROSS", "40 Example Ave, Port Chester", 41.01)


# -- hydrants ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def hcad(tmp_path):
    c = make_cad(tmp_path)
    wire(c, fake_gis())
    return c


def rows_of(data: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def test_a_hydrants_qualifier_is_stored_shown_and_never_moves_it(hcad):
    h = hcad.hydrants
    before = {f["properties"]["_k"]: f["geometry"]["coordinates"] for f in served(hcad.gis)[0]["features"]}
    out = h.edit("c990001", {"location_qualifier": "In front of", "location": "@Sample Market",
                             "location_places": [{"label": "Sample Market", "lat": 41.0101, "lon": -73.6501}]})
    hh = out["hydrant"]
    assert (hh["location_qualifier"], hh["location"]) == ("IFO", "@Sample Market")
    assert (hh["lat"], hh["lon"]) == (before["c990001"][1], before["c990001"][0])        # the county's point
    after = {f["properties"]["_k"]: f["geometry"]["coordinates"] for f in served(hcad.gis)[0]["features"]}
    assert after == before
    assert hcad.store.one("SELECT lat, lon FROM hydrants WHERE key = 'c990001'") == {"lat": None, "lon": None}  # no nudge
    with pytest.raises(CadError, match="location qualifier"):
        h.edit("c990001", {"location_qualifier": "behind"})
    hist = h.one("c990001")["history"]
    assert json.loads(hist[0]["new"])["location_qualifier"] == "IFO"                  # audited
    # retired: kept on the hydrant that has it, refused on another
    post(hcad, "location_qualifiers", action="retire", code="IFO")
    h.edit("c990001", {"location_qualifier": "IFO", "notes": "kept"})
    with pytest.raises(CadError, match="location qualifier"):
        h.edit("c990002", {"location_qualifier": "IFO"})
    assert item(hcad, "IFO")["used"] == 1


def test_the_hydrant_csv_carries_the_qualifier_and_the_location(hcad):
    h = hcad.hydrants
    h.edit("c990001", {"location_qualifier": "REAR", "location": "Sample St"})
    rows = rows_of(h.csv({})[0])
    cols = list(rows[0])
    assert cols.index("location_qualifier") == cols.index("location") - 1
    r1 = next(r for r in rows if r["key"] == "c990001")
    assert (r1["location_qualifier"], r1["location"]) == ("REAR", "Sample St")          # the code, the text as it is
    # back in: the code, the short form, the name or the spoken form; nothing else
    text = ("key,location_qualifier,location\n"
            "c990001,REAR,Sample St\n"
            "c990002,Across from,Example Ave\n"
            "c990003,nr,@Sample Market\n")
    pv = h.import_csv({"action": "preview", "csv": text})
    assert pv["errors"] == [] and pv["changed"] == 2 and pv["unchanged"] == 1
    out = h.import_csv({"action": "apply", "csv": text})
    assert out["applied"]
    assert [(h.one(k)["hydrant"]["location_qualifier"], h.one(k)["hydrant"]["location"]) for k in ("c990002", "c990003")] == [
        ("ACROSS", "Example Ave"), ("NEAR", "@Sample Market")]
    bad = h.import_csv({"action": "preview", "csv": "key,location_qualifier\nc990001,behind\n"})
    assert bad["error_count"] == 1 and "location qualifier" in bad["errors"][0]["error"]
    # a retired one the hydrant has: an export read back changes nothing
    post(hcad, "location_qualifiers", action="retire", code="REAR")
    assert h.import_csv({"action": "preview", "csv": h.csv({})[0].decode("utf-8-sig")})["changed"] == 0
    new = h.import_csv({"action": "preview", "csv": "key,location_qualifier\nc990002,REAR\n"})
    assert new["error_count"] == 1                                              # ... never put on another


# -- the console ------------------------------------------------------------------------------------------------------

def test_the_console_has_the_picker_in_both_location_boxes():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
    html = (static / "cad.html").read_text()
    assert 'id="f-qual"' in html and 'id="e-qual"' in html
    js = (static / "cad.js").read_text()
    assert "location_qualifier: $(\"#f-qual\").value" in js and "body.location_qualifier = $(\"#e-qual\").value" in js
    assert "qualSelect(el(\"select\", \"loc-qual\")" in (static / "hydrants.js").read_text()
    assert '["location_qualifiers", "Location qualifiers"]' in (static / "settings.js").read_text()


# -- the review's cases -----------------------------------------------------------------------------------------------

def test_a_qualifier_typed_in_front_of_the_same_address_keeps_the_pin():
    geo, calls = counting_geocoder()
    cad = make_cad(geocoder=geo)
    iid = cad.create({"type": "STRU", "address": "12 Sample St", "location_qualifier": "REAR", "lat": 41.02, "lon": -73.67})["id"]
    n = len(calls)
    cad.update(iid, {"address": "IFO 12 Sample St", "operator": "JD"})
    inc = cad.store.incident(iid)
    assert (inc["location_qualifier"], inc["address"], inc["lat"], inc["lon"]) == ("IFO", "12 Sample St", 41.02, -73.67)
    assert len(calls) == n


def test_another_address_without_a_qualifier_drops_the_old_one():
    cad = make_cad()
    iid = cad.create({"type": "STRU", "address": "40 Example Ave", "location_qualifier": "ACROSS", **PT})["id"]
    cad.store.update_incident(iid, {"address": "12 Sample St"}, operator="CAD feed")      # a feed's address
    assert cad.store.incident(iid)["location_qualifier"] == ""
    cad.update(iid, {"address": "14 Sample St", "location_qualifier": "OPP", **PT, "operator": "JD"})
    assert cad.store.incident(iid)["location_qualifier"] == "OPP"
    cad.update(iid, {"address": "", "location_qualifier": "OPP", "operator": "JD"})      # no location: none
    assert cad.store.incident(iid)["location_qualifier"] == ""


def test_a_data_file_without_the_list_keeps_the_defaults_through_an_edit(tmp_path):
    from tests.test_cad import DATA
    d = json.loads(DATA.read_text())
    d.pop("location_qualifiers")
    p = tmp_path / "lists.json"
    p.write_text(json.dumps(d))
    cad = make_cad(tmp_path, lists=str(p))
    post(cad, "location_qualifiers", action="retire", code="SIDE")
    assert len(cad.lists.location_qualifiers) == 8 and cad.lists.qualifier("SIDE")["retired"]
    assert cad.lists.qualified("IFO", "12 Sample St") == "IFO 12 Sample St"


def test_a_street_named_like_a_qualifier_is_left_whole():
    L = lists.load()
    for street in ("Side Hill Rd", "Near Rd", "Opp Ln", "Rear Admiral Way", "Cor Sample Dr", "Between Example Ave"):
        assert L.split_qualifier(street) == ("", street)
    assert L.split_qualifier("Opposite 12 Sample St") == ("OPP", "12 Sample St")     # a house number after it
    assert L.split_qualifier("IFO @Sample Market") == ("IFO", "@Sample Market")       # a place after it
    assert L.split_qualifier("Rear of Sample Market") == ("REAR", "Sample Market")    # two words: always one
    d = parse("Engine 61 respond to 5 Near Street for a fire alarm")
    assert (d.location, d.qualifier) == ("5 near street", "")
    d = parse("Port Chester Fire, car fire between Model Street and Specimen Road. Time out 1432.")
    assert (d.location, d.qualifier, d.cross_street) == ("model street", "", "specimen road")   # the cross streets


def test_one_form_one_meaning():
    d = lists.read()
    bad = {"code": "CLOSE", "name": "Close", "short": "NEAR", "spoken": "close to"}       # NEAR is another's code
    with pytest.raises(lists.ListsError, match="is NEAR's already"):
        lists.build(dict(d, location_qualifiers=[*d["location_qualifiers"], bad]), "t")


def test_a_hydrants_location_cleared_takes_its_qualifier(hcad):
    h = hcad.hydrants
    h.edit("c990001", {"location_qualifier": "IFO", "location": "Sample St"})
    out = h.edit("c990001", {"location": "", "location_qualifier": "IFO"})
    assert (out["hydrant"]["location_qualifier"], out["hydrant"]["location"]) == ("", "")
