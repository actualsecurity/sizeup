"""The CAD's side of #67: GET /api/cad/places (read only), a note's "@" places stored with it (old notes
unchanged), the location box with mile markers, and the department's own place names (Settings)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.cad.api import CadApi
from sizeup.cad.store import CadError, Store
from sizeup.cloud.access import Identity
from sizeup.geo.gazetteer import Gazetteer
from sizeup.geo.places import PlaceIndex
from tests.test_cad import make_cad, uid
from tests.test_places import PC, RB, fixture_gazetteer
from tests.test_roles import JULIAN, VAL, people
from tests.test_settings import post


@pytest.fixture()
def cad(tmp_path):
    gaz = Gazetteer(fixture_gazetteer(tmp_path / "gaz.sqlite"))
    c = make_cad(tmp_path, geocoder=lambda q: [])
    c.place_source = lambda: PlaceIndex(gaz)
    return c


def get(c, path, query="", who=None):
    code, raw, _ = CadApi(c).get(path, query, who)
    return code, json.loads(raw)


def event(c, **kw):
    return c.create(dict({"type": "SERVICE", "subtype": "LIFT", "address": "209 Westchester Ave", "operator": "JD"}, **kw))


# --------------------------------------------------------------------------- the API

def test_place_search_api_kinds_and_shape(cad):
    code, r = get(cad, "/api/cad/places", "q=287+eastbound+mile+marker+10")
    assert code == 200
    first = r["places"][0]
    assert (first["kind"], first["label"], first["sub"], first["source"]) == ("milemarker", "I-287 EB MM 10.0", "Rye Brook",
                                                                               "nysdot-rm")
    assert set(first) >= {"kind", "label", "sub", "lat", "lon", "source"}
    assert get(cad, "/api/cad/places", "q=lyon+park")[1]["places"][0]["kind"] == "place"
    assert get(cad, "/api/cad/places", "q=hutch+sb+exit+30")[1]["places"][0]["kind"] == "exit"
    assert get(cad, "/api/cad/places", "q=189+n+main")[1]["places"][0]["kind"] == "address"
    assert len(get(cad, "/api/cad/places", "q=oak+park&limit=2")[1]["places"]) == 2
    for bad in ("q=", "q=" + "x" * 121, "q=oak&near=abc", "q=oak&near=10,10", "q=oak&limit=x"):
        assert get(cad, "/api/cad/places", bad)[0] == 400, bad


def test_near_is_the_point_given_or_the_events(cad):
    assert get(cad, "/api/cad/places", f"q=oak+park&near={RB[0]},{RB[1]}")[1]["places"][0]["sub"] == "Rye Brook"
    assert get(cad, "/api/cad/places", f"q=oak+park&near={PC[0]},{PC[1]}")[1]["places"][0]["sub"] == "Port Chester"
    inc = event(cad, lat=RB[0], lon=RB[1])
    assert get(cad, "/api/cad/places", f"q=oak+park&iid={inc['id']}")[1]["places"][0]["sub"] == "Rye Brook"


def test_viewers_may_search_places(cad):
    people(cad)
    for email in (VAL, JULIAN):                    # a viewer, a dispatcher: GETs are read only
        code, r = get(cad, "/api/cad/places", "q=lyon", Identity(email=email))
        assert code == 200 and r["places"][0]["label"] == "Lyon Park"


def test_no_gazetteer_no_places(tmp_path):
    c = make_cad(tmp_path)
    assert get(c, "/api/cad/places", "q=lyon")[1] == {"places": [], "error": "place search unavailable"}


def test_a_broken_index_only_turns_the_place_search_off(tmp_path):
    c = make_cad(tmp_path, geocoder=lambda q: [])
    logged = []
    c.log = logged.append

    def boom():
        raise FileNotFoundError("gazetteer.sqlite")
    c.place_source = boom
    assert get(c, "/api/cad/places", "q=lyon")[1]["places"] == []
    assert "place search unavailable" in logged[0]
    assert get(c, "/api/cad/geocode", "q=lyon")[0] == 200


# --------------------------------------------------------------------------- notes

LYON = {"label": "Lyon Park", "lat": 41.001, "lon": -73.664, "kind": "place", "sub": "Port Chester", "source": "westchester-county"}
MM = {"label": "I-287 EB MM 10.0", "lat": 41.015, "lon": -73.69, "kind": "milemarker", "sub": "Rye Brook", "source": "nysdot-rm"}


def note(c, iid, **body):
    code, raw, _ = CadApi(c).post(f"/api/cad/incident/{iid}/note", json.dumps(dict({"operator": "JD"}, **body)).encode())
    return code, json.loads(raw)


def test_note_with_a_unit_and_places(cad):
    inc = event(cad)
    cad.attach(inc["id"], {"unit_ids": [uid(cad, "E63")], "status": "RS", "operator": "JD"})
    code, r = note(cad, inc["id"], text="@E63 staging at @Lyon Park, crash reported @I-287 EB MM 10.0",
                   units=["E63"], places=[LYON, MM])
    assert code == 200 and r["units"] == ["E63"] and [p["label"] for p in r["places"]] == ["Lyon Park", "I-287 EB MM 10.0"]
    n = [e for e in cad.full(inc["id"])["events"] if e["kind"] == "note"][-1]
    assert n["units"] == "E63"
    assert n["places"] == [dict(LYON), dict(MM)]
    # the event did not move
    after = cad.store.incident(inc["id"])
    assert (after["address"], after["lat"], after["lon"]) == ("209 Westchester Ave", inc["lat"], inc["lon"])
    # the state pushed to the consoles carries the places too
    st = next(i for i in cad.state()["incidents"] if i["id"] == inc["id"])
    assert [e for e in st["events"] if e["kind"] == "note"][-1]["places"][0]["label"] == "Lyon Park"


def test_a_place_whose_mention_was_edited_away_is_dropped(cad):
    inc = event(cad)
    code, r = note(cad, inc["id"], text="units staging near the park", places=[LYON])
    assert code == 200 and r["places"] == []
    code, r = note(cad, inc["id"], text="@lyon park twice @Lyon Park", places=[LYON, LYON])
    assert [p["label"] for p in r["places"]] == ["Lyon Park"]          # case-insensitive, once


@pytest.mark.parametrize("places, why", [
    ("Lyon Park", "a list"), ([LYON] * 11, "up to 10"), (["x"], "label, lat, lon"),
    ([dict(LYON, lat=None)], "lat and lon"), ([dict(LYON, lat=10.0)], "lat"), ([dict(LYON, kind="planet")], "kind"),
    ([dict(LYON, label="")], "label"),
])
def test_bad_places_are_refused(cad, places, why):
    inc = event(cad)
    code, r = note(cad, inc["id"], text="@Lyon Park", places=places)
    assert code == 400 and why in r["error"]
    assert not [e for e in cad.full(inc["id"])["events"] if e["kind"] == "note"]


def test_old_notes_and_plain_notes_are_unchanged(cad, tmp_path):
    inc = event(cad)
    assert note(cad, inc["id"], text="Con Ed notified")[1] == {"ok": True, "units": [], "places": []}
    n = [e for e in cad.full(inc["id"])["events"] if e["kind"] == "note"][0]
    assert (n["text"], n["units"], n["places"]) == ("Con Ed notified", "", [])
    # a database from before #67: the events table without the places column
    path = tmp_path / "old.sqlite"
    s = Store(str(path))
    iid = s.create_incident(type="SERVICE", subtype="LIFT", address="1 Main St", status="pending")
    s.note(iid, "an old note", "JD", units=["E63"])
    s.db.execute("ALTER TABLE events DROP COLUMN places")
    s.db.close()
    s = Store(str(path))
    assert "places" in {r["name"] for r in s.db.execute("PRAGMA table_info(events)")}
    old = [e for e in s.full(iid)["events"] if e["kind"] == "note"][0]
    assert (old["text"], old["units"], old["places"]) == ("an old note", "E63", [])


def test_the_activity_says_the_places(cad):
    from sizeup.cad.api import summarize
    inc = event(cad)
    said = summarize(f"/api/cad/incident/{inc['id']}/note", {"text": "@Lyon Park"}, {"units": ["E63"], "places": [LYON]}, cad)
    assert said[0].startswith(f"Note on {inc['number']} (E63, @Lyon Park)")


# --------------------------------------------------------------------------- the location box

def test_the_location_box_offers_mile_markers_first(cad):
    r = get(cad, "/api/cad/geocode", "q=I-287+EB+MM+10")[1]["matches"]
    assert r[0]["label"] == "I-287 EB MM 10.0" and r[0]["kind"] == "mile marker" and r[0]["town"] == "Rye Brook"
    assert r[0]["confidence"] >= 0.9
    r = get(cad, "/api/cad/geocode", "q=rye+playland")[1]["matches"]
    assert r[0]["label"] == "Rye Playland" and r[0]["kind"] == "place"
    # picked: the event's location is the marker, placed on the map
    inc = event(cad, address=r[0]["label"], lat=r[0]["lat"], lon=r[0]["lon"], geo_label=r[0]["label"])
    assert (inc["address"], inc["lat"]) == ("Rye Playland", r[0]["lat"])


def test_a_typed_highway_location_is_placed(cad):
    # the shape events already have ("@I287 EB MM 10.0"), typed without picking a suggestion
    inc = event(cad, address="@I287 EB MM 10.0")
    assert (inc["geo_label"], inc["lat"], inc["lon"]) == ("I-287 EB MM 10.0", 41.015, -73.69)
    assert event(cad, address="Hutch SB exit 19A")["geo_label"] == "Hutch SB exit 19A"
    assert event(cad, address="I287 somewhere")["lat"] is None          # not sure: no pin, as before


# --------------------------------------------------------------------------- Settings > Lists > Place names

def test_the_departments_own_place_names(cad):
    where = {"label": "The Waterfront at Port Chester", "lat": 41.0, "lon": -73.66, "sub": "Port Chester"}
    post(cad, "places", action="set", name="the Waterfront", aliases="waterfront plaza, the plaza", place=where)
    post(cad, "places", action="set", name="Kohl's plaza", place=dict(where, label="Kohl's"), note="behind the Waterfront")
    items = cad.settings_get()["place_names"]["items"]
    assert [(i["name"], i["aliases"]) for i in items] == [("Kohl's plaza", []), ("the Waterfront", ["waterfront plaza", "the plaza"])]
    hit = get(cad, "/api/cad/places", "q=waterfront")[1]["places"][0]
    assert (hit["label"], hit["source"], hit["sub"]) == ("The Waterfront at Port Chester", "local", "Port Chester")
    assert get(cad, "/api/cad/places", "q=kohls")[1]["places"][0]["label"] == "Kohl's"
    assert get(cad, "/api/cad/places", "q=the+plaza")[1]["places"][0]["source"] == "local"
    # audited with the lists, exported with the other edits
    audit = cad.settings_audit_log({"section": ["lists"]})["audit"]
    assert audit[0]["key"] == "place/Kohl's plaza"
    assert "the waterfront" in cad.settings_export()["overrides"]["places"]
    # edited (same name) and removed
    post(cad, "places", action="set", name="The Waterfront", place=dict(where, lat=41.001))
    assert [i["lat"] for i in cad.settings_get()["place_names"]["items"] if i["key"] == "the waterfront"] == [41.001]
    post(cad, "places", action="delete", key="the waterfront")
    assert get(cad, "/api/cad/places", "q=waterfront+plaza")[1]["places"] == []
    for body, why in (({"action": "set", "name": "", "place": where}, "name"), ({"action": "set", "name": "x"}, "pick where"),
                      ({"action": "set", "name": "x", "place": dict(where, lat=1)}, "in the area"),
                      ({"action": "delete", "key": "nope"}, "no place name"), ({"action": "rename"}, "set or delete")):
        with pytest.raises(CadError, match=why):
            post(cad, "places", **body)


def test_only_admins_set_place_names(cad):
    people(cad)
    where = {"label": "Lyon Park", "lat": 41.0, "lon": -73.66}
    code, raw, _ = CadApi(cad).post("/api/cad/settings/places", json.dumps({"action": "set", "name": "the green",
                                                                            "place": where}).encode(), Identity(email=JULIAN))
    assert code == 403
