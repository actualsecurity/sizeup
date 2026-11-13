"""Pre-plan points (#456, sizeup/cad/preplan.py, static/preplan.js): where a building's features are (the Knox box, the
FDC, the shutoffs ...), linked to the county footprint they are in, shown on the map of every call there.

Physical-security data: everything here is invented ("100 Example Ave" on an invented street grid, footprints drawn
for these tests), and nothing a point says or where it is ever reaches the server log."""
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sizeup.cad import preplan as pp
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.gis import DEFAULT_LAYERS, Gis, geometry_bbox
from tests.test_address_history import fake_geocode
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")

# The invented gazetteer (tests/test_address_history.py): N Example Ave is at lat 41.0 + N * 0.00009, lon -73.66.
LON = -73.66
DLAT = 0.00009                   # one house number north, about 10 m


def lat_of(n):
    return 41.0 + n * DLAT


def square(lat, lon, half_lat=0.00003, half_lon=0.00004):
    return {"type": "Polygon", "coordinates": [[[lon - half_lon, lat - half_lat], [lon + half_lon, lat - half_lat],
                                                [lon + half_lon, lat + half_lat], [lon - half_lon, lat + half_lat],
                                                [lon - half_lon, lat - half_lat]]]}


def lot(n, geometry):
    return {"type": "Feature", "id": 9000 + n, "properties": {"PARCEL_ADDR": f"{n} EXAMPLE AVE", "MUNI_NAME": "Port Chester"},
            "geometry": geometry}


def fake_gis(numbers=(100, 102), parcels=True, more_buildings=(), more_lots=()):
    """A real Gis with the buildings and parcels layers, holding a footprint (about 7 m x 7 m) and a parcel (the whole
    10 m lot) per house number on Example Ave (parcels: True, False, or the numbers that have one); more_*: as given."""
    g = Gis(layers=tuple(l for l in DEFAULT_LAYERS if l.key in ("buildings", "parcels")), load=False)
    g.cache_loaded = True
    blds = [{"type": "Feature", "id": 7000 + n, "properties": {"ACTUAL_HEIGHT": 30}, "geometry": square(lat_of(n), LON)}
            for n in numbers] + list(more_buildings)
    with_lot = numbers if parcels is True else (parcels or ())
    lots = [lot(n, square(lat_of(n), LON, DLAT / 2 - 0.000001, 0.0002)) for n in with_lot] + list(more_lots)
    for key, feats in (("buildings", blds), ("parcels", lots)):
        g.data[key] = feats
        g._boxes[key] = [geometry_bbox(f["geometry"]) for f in feats]
        g.status[key]["at"] = 1.0
    return g


@pytest.fixture
def cad():
    c = make_cad(geocoder=fake_geocode)
    c.gis = fake_gis()
    c.logged = []
    c.log = c.logged.append
    return c


def at(c, n, **kw):
    """An open call at N Example Ave, its pin on the building."""
    return c.store.create_incident(type="STRU", address=f"{n} Example Ave", lat=lat_of(n), lon=LON, **kw)


def pushed(c, iid):
    return next(i for i in c.state()["incidents"] if i["id"] == iid).get("preplan")


# -- the table ------------------------------------------------------------------------------------------------------------

def test_the_migration_is_idempotent_and_an_older_release_still_opens_the_database(tmp_path, monkeypatch):
    c = make_cad(tmp_path, geocoder=fake_geocode)
    iid = at(c, 100)
    c.preplan.create({"incident_id": iid, "kind": "fdc", "lat": lat_of(100) - 0.00005, "lon": LON, "label": "Side A"})
    c.shutdown()
    c = make_cad(tmp_path, geocoder=fake_geocode)                   # opened again: nothing made twice, nothing lost
    assert [p["label"] for p in c.preplan.for_incident(c.store.incident(iid))] == ["Side A"]
    idx = {r["name"] for r in c.store.q("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"preplan_bldg", "preplan_cell"} <= idx
    plan = " ".join(r["detail"] for r in c.store.q("EXPLAIN QUERY PLAN SELECT * FROM preplan_points WHERE bldg_key = ? "
                                                    "AND active = 1", ("x",)))
    assert "preplan_bldg" in plan                                   # a call's points never scan
    plan = " ".join(r["detail"] for r in c.store.q("EXPLAIN QUERY PLAN SELECT * FROM preplan_points WHERE active = 1 "
                                                    "AND cell IN (?, ?)", ("1:2", "1:3")))
    assert "preplan_cell" in plan                                   # nor does a viewport
    c.shutdown()

    # the release before this one: no Preplan at all; the extra table is none of its business
    from sizeup.cad import service

    class Before:
        def __init__(self, cad):
            pass

        def decorate(self, incs):
            pass

        def state(self):
            return {}

        def redact_audit(self, rows, may):
            return rows
    monkeypatch.setattr(service, "Preplan", Before)
    old = make_cad(tmp_path, geocoder=fake_geocode)
    j = old.store.create_incident(type="ALARM", subtype="COMM", address="102 Example Ave")
    assert any(i["id"] == j for i in old.state()["incidents"])
    assert old.store.q("PRAGMA integrity_check")[0]["integrity_check"] == "ok"
    old.shutdown()
    db = sqlite3.connect(tmp_path / "cad.sqlite")
    assert db.execute("SELECT COUNT(*) FROM preplan_points").fetchone()[0] == 1
    db.close()


# -- the building a point belongs to ------------------------------------------------------------------------------------

def test_a_point_in_a_footprint_takes_its_parcel_address_key(cad):
    p = cad.preplan.create({"kind": "knox_box", "lat": lat_of(100) + 0.00001, "lon": LON + 0.00002, "label": "Front door left"})["point"]
    assert (p["bldg_key"], p["link"], p["building_ref"], p["addr_label"]) == (
        "100|example ave|port chester", "inside", "buildings/7100", "100 Example Ave, Port Chester")


def test_outside_every_footprint_the_nearest_within_30_m(cad):
    # an FDC on the sidewalk, 9 m south of the footprint's edge
    a = cad.preplan.associate(lat_of(100) - 0.00003 - 0.00008, LON, None)
    assert (a["bldg_key"], a["link"], a["building_ref"]) == ("100|example ave|port chester", "nearest", "buildings/7100")
    assert 8 <= a["dist_m"] <= 10
    # 40 m from anything: no building, unless a call's
    far = (lat_of(100), LON + 0.0005)
    a = cad.preplan.associate(*far, None)
    assert (a["bldg_key"], a["link"], a["building_ref"]) == ("", "point", None)
    iid = at(cad, 100)
    a = cad.preplan.associate(*far, cad.preplan._ctx({"incident_id": iid}))
    assert (a["bldg_key"], a["link"]) == ("100|example ave|port chester", "call")


def test_placed_from_a_call_its_own_footprint_keeps_the_calls_key_and_a_neighbour_is_a_choice(cad):
    iid = cad.store.create_incident(type="STRU", address="100 Example Ave Apt 2", lat=lat_of(100), lon=LON)
    ctx = cad.preplan._ctx({"incident_id": iid})
    a = cad.preplan.associate(lat_of(100) + 0.00002, LON, ctx)
    assert (a["bldg_key"], a["link"], set(a["choices"])) == ("100|example ave|port chester", "inside", {"call", "point"})
    # in the neighbour's footprint: the neighbour's building, the call's one click away
    a = cad.preplan.associate(lat_of(102), LON, ctx)
    assert (a["bldg_key"], a["building_ref"]) == ("102|example ave|port chester", "buildings/7102")
    assert a["choices"] == {"building": "102 Example Ave, Port Chester", "call": "100 Example Ave, Port Chester",
                            "point": "Map point only"}
    a = cad.preplan.associate(lat_of(102), LON, ctx, "call")
    assert (a["bldg_key"], a["link"], a["building_ref"]) == ("100|example ave|port chester", "call", "buildings/7102")
    a = cad.preplan.associate(lat_of(102), LON, ctx, "point")
    assert (a["bldg_key"], a["link"]) == ("", "point")
    from sizeup.cad.store import CadError
    with pytest.raises(CadError, match="link"):
        cad.preplan.associate(lat_of(100), LON, None, "call")       # no call to link to


def test_with_no_footprint_layer_the_calls_key_or_the_point_alone(cad):
    cad.gis = None                                                  # a dev box, or a hub still reading its cache
    iid = at(cad, 100)
    p = cad.preplan.create({"incident_id": iid, "kind": "fdc", "lat": lat_of(100) - 0.0001, "lon": LON})["point"]
    assert (p["bldg_key"], p["link"], p["building_ref"]) == ("100|example ave|port chester", "call", None)
    p = cad.preplan.create({"kind": "gate_access", "lat": lat_of(100), "lon": LON + 0.0003})["point"]
    assert (p["bldg_key"], p["link"]) == ("", "point")
    assert cad.preplan.associate(lat_of(100), LON, None)["footprints"] is False
    cad.gis = fake_gis(parcels=False)                               # footprints, no parcel address: the call's key
    a = cad.preplan.associate(lat_of(100), LON, cad.preplan._ctx({"incident_id": iid}))
    assert (a["bldg_key"], a["building_ref"]) == ("100|example ave|port chester", "buildings/7100")


def test_a_move_looks_at_the_building_again(cad):
    p = cad.preplan.create({"kind": "fdc", "lat": lat_of(100), "lon": LON})["point"]
    out = cad.preplan.edit(p["id"], {"lat": lat_of(100) - 0.00009 + 0.00001, "lon": LON})    # to the sidewalk: the same building
    assert out["point"]["bldg_key"] == "100|example ave|port chester" and out["point"]["link"] == "nearest"
    assert out["changed"] == ["lat", "link"]
    out = cad.preplan.edit(p["id"], {"lat": lat_of(102), "lon": LON})                          # into the neighbour's
    assert out["point"]["bldg_key"] == "102|example ave|port chester"
    out = cad.preplan.edit(p["id"], {"lat": lat_of(102), "lon": LON, "link": "call"})           # ... kept on its own, explicitly
    assert out["point"]["bldg_key"] == "102|example ave|port chester"


# -- the incident's map -------------------------------------------------------------------------------------------------

def test_a_call_shows_its_buildings_points_and_never_the_neighbours(cad):
    mk = cad.preplan.create
    knox = mk({"kind": "knox_box", "lat": lat_of(100) + 0.00002, "lon": LON, "label": "Front door left"})["point"]
    fdc = mk({"kind": "fdc", "lat": lat_of(100) - 0.00006, "lon": LON, "label": "Example Ave side", "priority": 1})["point"]
    mk({"kind": "gas_shutoff", "lat": lat_of(102), "lon": LON, "label": "Rear"})                # next door, 9 m away
    gone = mk({"kind": "elevator", "lat": lat_of(100), "lon": LON})["point"]
    cad.preplan.edit(gone["id"], {"action": "delete"})
    iid = at(cad, 100)
    pts = pushed(cad, iid)
    assert [p["id"] for p in pts] == [fdc["id"], knox["id"]]          # priority first, then the kind's order
    assert {p["nearby"] for p in pts} == {False}
    assert pts[1]["label"] == "Front door left" and pts[1]["kind"] == "knox_box"
    nxt = at(cad, 102)
    assert [p["kind"] for p in pushed(cad, nxt)] == ["gas_shutoff"]
    cad.store.close(iid, "COMPLETE")                                 # closed: off the push, still asked for
    assert pushed(cad, iid) is None
    assert [p["id"] for p in cad.preplan.get({"incident": [str(iid)]}, "viewer")["points"]] == [fdc["id"], knox["id"]]


def test_a_call_with_no_building_key_shows_points_within_20_m_as_nearby(cad):
    near = cad.preplan.create({"kind": "fdc", "lat": lat_of(100) - 0.00008, "lon": LON, "label": "Example Ave side"})["point"]
    cad.preplan.create({"kind": "fdc", "lat": lat_of(100) - 0.00008, "lon": LON + 0.0004})          # 34 m east
    iid = cad.store.create_incident(type="MVF", address="Example Ave / Sample St", lat=lat_of(100) - 0.0003, lon=LON)
    assert cad.store.incident(iid)["bldg_key"] == ""                 # two streets: no building
    assert pushed(cad, iid) == []                                    # 24 m: too far
    cad.update(iid, {"lat": lat_of(100) - 0.00015, "lon": LON})      # 8 m
    (p,) = pushed(cad, iid)
    assert (p["id"], p["nearby"]) == (near["id"], True)


def test_the_push_carries_only_what_everyone_may_see(cad, monkeypatch):
    iid = at(cad, 100)
    cad.preplan.create({"incident_id": iid, "kind": "knox_box", "lat": lat_of(100), "lon": LON, "note": "Key 1"})
    cad.preplan.create({"incident_id": iid, "kind": "fdc", "lat": lat_of(100), "lon": LON})
    monkeypatch.setattr(pp, "VISIBLE_TO", {"knox_box": "dispatcher"})
    cad._state = None
    st = cad.state()
    assert [p["kind"] for p in pushed(cad, iid)] == ["fdc"]
    assert st["preplan"]["restricted"] == ["knox_box"]
    assert [p["kind"] for p in cad.preplan.get({"incident": [str(iid)]}, "dispatcher")["points"]] == ["knox_box", "fdc"]
    assert "Key 1" not in json.dumps(st)


# -- the API: roles, the audit, visibility ------------------------------------------------------------------------------

@pytest.fixture
def api(cad):
    a = CadApi(cad)
    a.ids = people(cad)
    a.iid = at(cad, 100)
    return a


def get(api, path, query="", email=None):
    code, raw, ctype, *h = api.get(path, query, Identity(email=email) if email else None)
    return code, (json.loads(raw) if "json" in ctype else raw), (h[0] if h else {})


def test_crud_audit_and_roles(api):
    c, iid = api.cad, api.iid
    body = {"incident_id": iid, "kind": "knox_box", "lat": lat_of(100), "lon": LON, "label": "Front door left", "note": "Code 1234"}
    assert call(api, "/api/cad/preplan", body, VAL)[0] == 403              # viewers only look
    code, out = call(api, "/api/cad/preplan", body, JULIAN)
    assert code == 200
    p = out["point"]
    assert (p["kind"], p["label"], p["note"], p["by"], p["bldg_key"]) == ("knox_box", "Front door left", "Code 1234", "Julian",
                                                                          "100|example ave|port chester")
    code, out = call(api, f"/api/cad/preplan/{p['id']}", {"label": "Front door right", "level": "1", "review_by": "2027-06-30"}, JULIAN)
    assert code == 200 and (out["point"]["label"], out["point"]["level"]) == ("Front door right", "1")
    code, out = call(api, f"/api/cad/preplan/{p['id']}", {"lat": lat_of(100) + 0.00002, "lon": LON}, JULIAN)
    assert code == 200 and out["changed"] == ["lat"]
    assert call(api, f"/api/cad/preplan/{p['id']}", {"action": "restore"}, JULIAN)[0] == 400       # restore: admins
    code, out = call(api, f"/api/cad/preplan/{p['id']}", {"action": "delete"}, JULIAN)
    assert code == 200 and out["point"]["active"] is False
    assert pushed(c, iid) == []
    assert call(api, f"/api/cad/preplan/{p['id']}", {"action": "restore"}, COREY)[0] == 200
    audit = c.store.q("SELECT * FROM settings_audit WHERE section = 'preplan' ORDER BY id")
    assert [a["key"] for a in audit] == [f"{p['id']} Knox box"] * 5
    assert json.loads(audit[0]["new"])["note"] == "Code 1234"
    assert [json.loads(a["new"]) for a in audit[1:]] == [
        {"label": "Front door right", "level": "1", "review_by": json.loads(audit[1]["new"])["review_by"]},
        {"lat": round(lat_of(100) + 0.00002, 7)}, {"active": 0}, {"active": 1}]
    assert [a["operator"].split(" <")[0] for a in audit] == ["Julian"] * 4 + ["Corey"]
    acts = [r["summary"] for r in c.activity.rows(kind="action")]
    number = c.store.incident(iid)["number"]
    assert acts[:5] == [f"Restored pre-plan point {p['id']}", f"Deleted pre-plan point {p['id']}",
                        f"Moved pre-plan point {p['id']}", f"Edited pre-plan point {p['id']}: label, level, review_by",
                        f"Added pre-plan point {p['id']} (Knox box)" + (f" from {number}" if number else "")]
    assert any(s == f"Moved pre-plan point {p['id']}" for s in acts)
    assert not any("Front" in s or "1234" in s or "41." in s for s in acts)
    for bad in ({"kind": "pool"}, {"label": "x" * 61}, {"note": "x" * 301}, {"level": "x" * 21}, {"priority": 7},
                {"lat": "north"}, {"review_by": "soon"}):
        code, out = call(api, "/api/cad/preplan", {**body, **bad}, JULIAN)
        assert code == 400, bad


def test_restricted_kinds_are_per_person_in_reads_writes_and_the_audit(api):
    c, iid = api.cad, api.iid
    knox = c.preplan.create({"incident_id": iid, "kind": "knox_box", "lat": lat_of(100), "lon": LON, "note": "Box behind sign"})["point"]
    call(api, f"/api/cad/preplan/{knox['id']}", {"note": "Box behind the sign"}, JULIAN)          # written in the clear
    code, out = call(api, "/api/cad/settings/premise_visibility", {"action": "set", "key": "knox_box", "value": "dispatcher"}, JULIAN)
    assert code == 403                                                 # admins only
    code, out = call(api, "/api/cad/settings/premise_visibility", {"action": "set", "key": "knox_box", "value": "dispatcher"}, COREY)
    assert code == 200 and out["premise_visibility"]["knox_box"] == {
        "label": "Pre-plan: Knox box", "value": "dispatcher", "edited": True, "choices": ["everyone", "dispatcher", "admin"]}
    assert set(out["premise_visibility"]) >= {"gate_access", "medical"}
    assert pushed(c, iid) == []
    assert get(api, "/api/cad/preplan", f"incident={iid}", VAL)[1]["points"] == []
    assert [p["id"] for p in get(api, "/api/cad/preplan", f"incident={iid}", JULIAN)[1]["points"]] == [knox["id"]]
    code, out = call(api, f"/api/cad/preplan/{knox['id']}", {"note": "x"}, VAL)
    assert code == 403
    rows = [r for r in get(api, "/api/cad/settings/audit", "section=preplan", VAL)[1]["audit"] if r["section"] == "preplan"]
    assert len(rows) == 2 and not any("sign" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in rows)
    assert json.loads(rows[0]["new"]) == {"kind": "knox_box", "changed": ["note"]}
    assert any("sign" in (r["new"] or "") for r in get(api, "/api/cad/settings/audit", "section=preplan", JULIAN)[1]["audit"])
    call(api, f"/api/cad/preplan/{knox['id']}", {"note": "Box by the door"}, JULIAN)               # written restricted
    last = c.store.q("SELECT * FROM settings_audit WHERE section = 'preplan' ORDER BY id DESC LIMIT 1")[0]
    assert last["key"] == str(knox["id"]) and "door" not in last["old"] + last["new"]
    code, out = call(api, "/api/cad/settings/premise_visibility", {"action": "set", "key": "fdc", "value": "admin"}, COREY)
    assert code == 400                                                 # only the Knox boxes and the gates


def test_the_viewport_fetch_with_etag(api):
    c = api.cad
    a = c.preplan.create({"kind": "fdc", "lat": lat_of(100), "lon": LON})["point"]
    c.preplan.create({"kind": "fdc", "lat": lat_of(100) + 0.01, "lon": LON})                         # 1 km north
    box = f"{LON - 0.001},{lat_of(100) - 0.001},{LON + 0.001},{lat_of(100) + 0.001}"
    code, out, h = get(api, "/api/cad/preplan", f"bbox={box}", VAL)
    assert code == 200 and [p["id"] for p in out["points"]] == [a["id"]]
    assert h["ETag"].startswith('W/"pp-') and "no-cache" in h["Cache-Control"]
    assert get(api, "/api/cad/preplan", f"bbox={box}", VAL)[2]["ETag"] == h["ETag"]         # unchanged: the same tag
    assert get(api, "/api/cad/preplan", f"bbox={box}", JULIAN)[2]["ETag"] != h["ETag"]      # another standing: its own
    c.preplan.edit(a["id"], {"label": "Side A"})
    assert get(api, "/api/cad/preplan", f"bbox={box}", VAL)[2]["ETag"] != h["ETag"]         # a write: a new one
    code, out, _ = get(api, "/api/cad/preplan", f"bbox={LON - 0.1},40.9,{LON + 0.1},41.1", VAL)
    assert code == 400                                                 # a whole town at once: no
    assert get(api, "/api/cad/preplan", "", VAL)[0] == 403             # every point: admins
    assert get(api, "/api/cad/preplan", "", JULIAN)[0] == 403
    assert len(get(api, "/api/cad/preplan", "", COREY)[1]["points"]) == 2


def test_the_server_answers_304_for_the_viewport(api):
    from sizeup.web.server import etag_matches
    box = f"{LON - 0.001},{lat_of(100) - 0.001},{LON + 0.001},{lat_of(100) + 0.001}"
    tag = get(api, "/api/cad/preplan", f"bbox={box}", VAL)[2]["ETag"]
    assert etag_matches(tag, tag)


def test_the_list_and_csv_are_admins_and_the_building_view_is_one_building(api):
    c = api.cad
    c.preplan.create({"kind": "fdc", "lat": lat_of(100), "lon": LON, "label": "Side A"})
    c.preplan.create({"kind": "fdc", "lat": lat_of(102), "lon": LON})
    out = get(api, "/api/cad/preplan", "where=100+Example+Ave", VAL)[1]
    assert [p["label"] for p in out["points"]] == ["Side A"] and out["key"]["bldg_key"] == "100|example ave|port chester"
    assert out["point"] == [pytest.approx(lat_of(100)), LON]
    assert get(api, "/api/cad/preplan", "where=Zqxv", VAL)[0] == 400
    assert get(api, "/api/cad/preplan.csv", "", JULIAN)[0] == 403
    code, raw, _ = get(api, "/api/cad/preplan.csv", "", COREY)
    lines = raw.decode("utf-8-sig").splitlines()
    assert code == 200 and lines[0].startswith("Building,Kind,Label,Note,Level") and len(lines) == 3
    assert [r["summary"] for r in c.activity.rows(kind="search", email=COREY)] == ["CSV: pre-plan points: 2"]
    assert get(api, "/api/cad/preplan/where", f"lat={lat_of(100)}&lon={LON}", VAL)[0] == 403
    code, out, _ = get(api, "/api/cad/preplan/where", f"lat={lat_of(102)}&lon={LON}&incident_id={api.iid}", JULIAN)
    assert code == 200 and out["addr_label"] == "102 Example Ave, Port Chester" and out["geometry"]["type"] == "Polygon"


def test_no_label_note_or_coordinates_in_the_server_log(api):
    c = api.cad
    log = []
    c.activity.log = log.append
    c.log = log.append
    secret = {"label": "Behind blue door", "note": "Gate code 4321"}
    code, out = call(api, "/api/cad/preplan", {"incident_id": api.iid, "kind": "gate_access", "lat": 41.0123456, "lon": -73.6543219,
                                                **secret}, JULIAN)
    pid = out["point"]["id"]
    call(api, f"/api/cad/preplan/{pid}", {"note": "Gate code 9876", "lat": 41.0123999, "lon": -73.6543999}, JULIAN)
    call(api, f"/api/cad/preplan/{pid}", {"action": "delete"}, JULIAN)
    get(api, "/api/cad/preplan", "bbox=-73.66,41.0,-73.65,41.02", VAL)
    c.preplan._at = lambda *a: (_ for _ in ()).throw(RuntimeError("Gate code 4321 at 41.0123456"))
    c._state = None
    c.store.rev += 1
    c.state()                                                          # a failing read never says what it read
    text = "\n".join(map(str, log))
    assert log and "pre-plan point" in text
    for s in ("blue door", "4321", "9876", "41.012", "73.654", "Gate code"):
        assert s not in text, s


# -- the page -------------------------------------------------------------------------------------------------------------

def test_the_page_loads_the_script_and_hooks_it_in():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/maplayers.js"') < html.index('src="/static/preplan.js"')
    assert html.index('src="/static/cadmap.js"') < html.index('src="/static/preplan.js"')
    grid = html[html.index('<div id="d-grid"'):html.index('<form id="edit-form"')]
    assert grid.index('id="d-mapbox"') < grid.index('id="d-preplan"') < grid.index('id="d-cams"')
    cad = (STATIC / "cad.js").read_text()
    assert 'if (window.Preplan) Preplan.card(inc, { readonly: BOARD || !canDo("dispatch") });' in cad
    src = (STATIC / "preplan.js").read_text()
    assert "MapLayers.addGroup(" in src
    for f in ("cadmap.js", "map-style.js", "cadmap.css"):                 # their own files are other branches' (#455)
        assert "preplan" not in (STATIC / f).read_text().lower(), f


NODE_HARNESS = r"""
global.window = global;
require(process.argv[2]);
const P = window.Preplan;
const out = {};
out.kinds = Object.keys(P.KINDS);
out.line = P.line({ kind: "knox_box", label: "Front door left", level: "" });
out.lineLevel = P.line({ kind: "fdc", label: "Example Ave side", level: "2" });
out.lineBare = P.line({ kind: "elevator", label: "", level: "" });
out.lineRoof = P.line({ kind: "roof_access", label: "Hatch", level: "Roof" });
out.before = P.beforeId(["bg", "gis-buildings", "ml-cameras", "cm-lines", "cm-trail", "cm-routes", "firehouse"]);
out.beforeRoutes = P.beforeId(["bg", "cm-trail", "cm-routes", "firehouse"]);
out.beforeNone = P.beforeId(["bg", "roads"]);
const inc = [{ id: 1, kind: "fdc", lat: 41, lon: -73.66, label: "A", nearby: false }];
const box = [{ id: 1, kind: "fdc", lat: 41, lon: -73.66, label: "A" }, { id: 2, kind: "elevator", lat: 41.001, lon: -73.66, label: "" }];
out.fc = P.features(inc, box);
out.fcInc = P.features(inc, []);
out.pad = P.padBox([-73.67, 41.0, -73.66, 41.01]);
out.inside = P.inside([-73.67, 41.0, -73.66, 41.01], [-73.68, 40.99, -73.65, 41.02]);
out.outside = P.inside([-73.67, 41.0, -73.66, 41.01], [-73.665, 40.99, -73.65, 41.02]);
out.zoom = P.LAYER_ZOOM;
out.key = P.PREF_KEY;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_layer_and_editing_helpers_in_node(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(NODE_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "preplan.js")], capture_output=True, text=True, check=True,
                                    timeout=30).stdout)
    assert out["kinds"] == list(pp.KINDS)                                  # one list, both ends
    assert out["line"] == "Knox box · Front door left"
    assert out["lineLevel"] == "FDC · Example Ave side · Fl 2"
    assert out["lineBare"] == "Elevator" and out["lineRoof"] == "Roof access · Hatch · Roof"
    assert out["before"] == "cm-lines" and out["beforeRoutes"] == "cm-trail" and out["beforeNone"] is None
    fs = out["fc"]["features"]
    assert [f["properties"]["id"] for f in fs] == [1, 2]                   # the call's and the viewport's, once each
    assert fs[0]["properties"]["icon"] == "pp-fdc" and fs[0]["geometry"]["coordinates"] == [-73.66, 41]
    assert fs[1]["properties"]["text"] == "Elevator"
    assert len(out["fcInc"]["features"]) == 1
    assert out["inside"] is True and out["outside"] is False
    w, s, e, n = out["pad"]
    assert w < -73.67 and e > -73.66 and s < 41.0 and n > 41.01
    assert out["zoom"] == 16 and out["key"] == "sizeup.preplanLayer"


def test_the_script_wires_the_layer_the_list_and_the_editor():
    src = (STATIC / "preplan.js").read_text()
    # the layer: its own source and layers, under the route lines, rigs and pins (DOM marks above every map layer)
    assert 'addSource(SRC, ' in src and '"pp-icons"' in src and '"pp-labels"' in src
    assert "this.map.addLayer(spec, beforeId(ids()))" in src
    assert 'const BELOW = ["cm-lines", "cm-trail", "cm-routes", "firehouse"];' in src
    cm = (STATIC / "cadmap.js").read_text()
    for lid in ("cm-lines", "cm-trail", "cm-routes"):                      # the ids are cadmap.js's own
        assert f'id: "{lid}", type: "line"' in cm, lid
    # off by default, per viewer, in a try; a viewport by bbox at zoom 16+, again only when the map leaves it
    assert re.search(r"try \{ return localStorage\.getItem\(PREF_KEY\) === \"1\"; \} catch", src)
    assert "/api/cad/preplan?bbox=" in src and "this.map.getZoom() >= LAYER_ZOOM" in src
    assert 'this.map.on("moveend"' in src and "requestAnimationFrame" not in src           # nothing per frame
    # the call's: from the push, plus a restricted kind's per person (only when one is restricted and they may)
    assert "inc.preplan" in src and "/api/cad/preplan?incident=" in src and "restricted.length" in src
    # editing: a kind grid, a click to place, a draggable mark, the building, the save; delete and review by
    assert 'new maplibregl.Marker({ element: mk, draggable: true' in src
    assert '"/api/cad/preplan/where?' in src and 'api("POST", "/api/cad/preplan"' in src
    assert "`/api/cad/preplan/${p.id}`" in src and 'action: "delete"' in src and "review_by" in src
    for k in pp.KINDS:
        assert f"  {k}: " in src, k                                          # a drawing for every kind
    for line in src.splitlines():                                          # never a point in the console
        assert not re.search(r"console\.\w+\([^)]*\b(lat|lon|note|label)\b", line), line


# -- the review of #458 (each a regression test) -----------------------------------------------------------------------

def test_a_calls_points_have_no_etag_and_follow_a_rekeyed_call(api):
    c = api.cad
    c.preplan.create({"kind": "knox_box", "lat": lat_of(100), "lon": LON, "label": "A"})
    c.preplan.create({"kind": "fdc", "lat": lat_of(102), "lon": LON, "label": "B"})
    c.state()                                                              # the call keyed (to 100)
    code, out, h = get(api, "/api/cad/preplan", f"incident={api.iid}", JULIAN)
    assert [p["label"] for p in out["points"]] == ["A"]
    assert "ETag" not in h and h["Cache-Control"] == "no-store"            # never a 304 for a call
    c.update(api.iid, {"address": "102 Example Ave", "lat": lat_of(102), "lon": LON})
    c.state()                                                              # keyed again
    assert [p["label"] for p in get(api, "/api/cad/preplan", f"incident={api.iid}", JULIAN)[1]["points"]] == ["B"]
    assert "ETag" in get(api, "/api/cad/preplan", "where=100+Example+Ave", JULIAN)[2]


def test_the_preview_of_a_point_they_may_not_see_or_a_deleted_one_is_no_point(api, monkeypatch):
    c = api.cad
    knox = c.preplan.create({"kind": "knox_box", "lat": lat_of(100), "lon": LON})["point"]
    gone = c.preplan.create({"kind": "fdc", "lat": lat_of(100), "lon": LON})["point"]
    c.preplan.edit(gone["id"], {"action": "delete"})
    monkeypatch.setattr(pp, "VISIBLE_TO", {"knox_box": "admin"})
    q = f"lat={lat_of(102)}&lon={LON}&point="
    for pid in (knox["id"], gone["id"], 999):
        code, out, _ = get(api, "/api/cad/preplan/where", q + str(pid), JULIAN)
        assert (code, out) == (400, {"error": f"no point {pid}"}), pid       # the same answer as a point that never was
    assert get(api, "/api/cad/preplan/where", q + str(knox["id"]), COREY)[0] == 200


def test_a_neighbours_footprint_with_no_address_is_never_the_calls(cad):
    cad.gis = fake_gis(parcels=(100,))                                     # 102's parcel has no address here
    iid = at(cad, 100)
    a = cad.preplan.associate(lat_of(102), LON, cad.preplan._ctx({"incident_id": iid}))
    assert (a["bldg_key"], a["link"], a["building_ref"]) == ("", "point", "buildings/7102")
    assert set(a["choices"]) == {"call", "point"}                          # the call's building: one pick away
    assert cad.preplan.associate(lat_of(102), LON, cad.preplan._ctx({"incident_id": iid}), "call")["bldg_key"] == \
        "100|example ave|port chester"


def test_a_footprints_parcel_is_the_one_most_of_it_is_on_wherever_it_is_tapped(cad):
    y0, x0 = 41.05, -73.64
    fp = {"type": "Feature", "id": 7200, "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
        [x0, y0], [x0 + 0.0001, y0], [x0 + 0.0001, y0 + 0.0001], [x0, y0 + 0.0001], [x0, y0]]]}}
    south = lot(200, {"type": "Polygon", "coordinates": [[[x0 - 0.001, y0 - 0.001], [x0 + 0.001, y0 - 0.001],
                                                          [x0 + 0.001, y0 + 0.00007], [x0 - 0.001, y0 + 0.00007], [x0 - 0.001, y0 - 0.001]]]})
    north = lot(202, {"type": "Polygon", "coordinates": [[[x0 - 0.001, y0 + 0.00007], [x0 + 0.001, y0 + 0.00007],
                                                          [x0 + 0.001, y0 + 0.001], [x0 - 0.001, y0 + 0.001], [x0 - 0.001, y0 + 0.00007]]]})
    # an L whose vertices' mean is outside it, on its own lot
    u, ly, lx = 0.00002, 41.06, -73.63
    L = {"type": "Feature", "id": 7300, "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
        [lx, ly], [lx + 10 * u, ly], [lx + 10 * u, ly + u], [lx + u, ly + u], [lx + u, ly + 10 * u], [lx, ly + 10 * u], [lx, ly]]]}}
    l_lot = lot(300, {"type": "Polygon", "coordinates": [[[lx, ly], [lx + 10 * u, ly], [lx + 10 * u, ly + 10 * u], [lx, ly + 10 * u], [lx, ly]]]})
    cad.gis = fake_gis(more_buildings=(fp, L), more_lots=(south, north, l_lot))
    for y in (y0 + 0.00001, y0 + 0.00009):                                # its south end, its north end (the other lot)
        assert cad.preplan.associate(y, x0 + 0.00005, None)["bldg_key"] == "200|example ave|port chester"
    a = cad.preplan.associate(ly + 9.5 * u, lx - 0.00005, None)            # 4 m off the end of an arm, outside the lot
    assert (a["bldg_key"], a["link"], a["building_ref"]) == ("300|example ave|port chester", "nearest", "buildings/7300")


def test_a_call_with_no_key_takes_its_footprints_sure_key(cad):
    iid = cad.store.create_incident(type="STRU", address="Example Ave / Sample St", lat=lat_of(100), lon=LON)
    ctx = cad.preplan._ctx({"incident_id": iid})
    assert ctx["bldg_key"] == ""
    p = cad.preplan.create({"incident_id": iid, "kind": "fdc", "lat": lat_of(100) + 0.00001, "lon": LON})["point"]
    assert (p["bldg_key"], p["link"]) == ("100|example ave|port chester", "inside")


def test_the_generation_never_repeats_across_a_restart(tmp_path):
    import time
    a = make_cad(tmp_path, geocoder=fake_geocode)
    for _ in range(3):
        a.preplan.create({"kind": "fdc", "lat": lat_of(100), "lon": LON})
    before = a.preplan.gen
    box = f"bbox={LON - 0.001},41.0,{LON + 0.001},41.01"
    tag = a.preplan.etag(box, "viewer")
    a.shutdown()
    time.sleep(0.01)
    b = make_cad(tmp_path, geocoder=fake_geocode)
    assert b.preplan.gen > before                                          # a new task: never an old tag again
    assert b.preplan.etag(box, "viewer") != tag


def test_older_audit_rows_of_a_since_restricted_kind_are_redacted(api, monkeypatch):
    c = api.cad
    p = c.preplan.create({"kind": "knox_box", "lat": lat_of(100), "lon": LON, "note": "Box behind sign"})["point"]
    c.preplan.edit(p["id"], {"kind": "fdc", "note": "Side A"})            # now an FDC: the first row still says Knox box
    monkeypatch.setattr(pp, "VISIBLE_TO", {"knox_box": "admin"})
    rows = [r for r in get(api, "/api/cad/settings/audit", "section=preplan", JULIAN)[1]["audit"] if r["section"] == "preplan"]
    assert len(rows) == 2 and not any("sign" in (r["old"] or "") + (r["new"] or "") for r in rows)
    assert {json.loads(r["new"])["kind"] for r in rows} == {"knox_box"}
    assert any("sign" in (r["new"] or "") for r in get(api, "/api/cad/settings/audit", "section=preplan", COREY)[1]["audit"])


def test_a_kind_they_may_not_see_cannot_be_created(api, monkeypatch):
    monkeypatch.setattr(pp, "VISIBLE_TO", {"knox_box": "admin"})
    body = {"incident_id": api.iid, "kind": "knox_box", "lat": lat_of(100), "lon": LON}
    code, out = call(api, "/api/cad/preplan", body, JULIAN)
    assert code == 400 and "knox_box" not in out["error"] and "fdc" in out["error"]
    assert call(api, "/api/cad/preplan", body, COREY)[0] == 200
    api.cad._state = None
    assert api.cad.state()["preplan"]["levels"] == {"knox_box": "admin"}   # the page leaves it out of the picker
    src = (STATIC / "preplan.js").read_text()
    assert "if (!mayKind(k) && !(p && p.kind === k)) continue;" in src
    assert 'kind: p ? p.kind : (Object.keys(KINDS).find(mayKind) || "other")' in src


def test_the_editors_preview_writes_nothing(api):
    c = api.cad
    iid = c.store.create_incident(type="STRU", address="102 Example Ave", lat=lat_of(102), lon=LON)
    assert c.store.one("SELECT id FROM addr_queue WHERE id = ?", (iid,))   # not keyed yet
    rev = c.store.rev
    code, out, _ = get(api, "/api/cad/preplan/where", f"lat={lat_of(102)}&lon={LON}&incident_id={iid}", JULIAN)
    assert code == 200 and out["bldg_key"] == "102|example ave|port chester"
    assert c.store.rev == rev and c.store.one("SELECT id FROM addr_queue WHERE id = ?", (iid,))


def test_edit_only_from_the_calls_own_map_and_one_idle_wait():
    src = (STATIC / "preplan.js").read_text()
    assert '!st.readonly && this.map.getContainer().closest("#d-map")' in src         # never under the large map's overlay
    assert 'if (!this.waiting) { this.waiting = true; this.map.once("idle"' in src
    assert "urls[kind] || (urls[kind] = drawn(kind).toDataURL())" in src
    assert "inc.status, pstate().levels || {}]" in src


PAD_HARNESS = r"""
global.window = global;
require(process.argv[2]);
console.log(JSON.stringify(window.Preplan.padBox([-73.70, 41.0, -73.64, 41.01])));   // a 0.06-degree-wide screen at zoom 16
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_very_wide_screen_asks_for_no_more_than_the_hub_serves(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(PAD_HARNESS)
    w, s, e, n = json.loads(subprocess.run([NODE, str(h), str(STATIC / "preplan.js")], capture_output=True, text=True,
                                           check=True, timeout=30).stdout)
    assert e - w <= pp.BBOX_MAX_DEG and n - s <= pp.BBOX_MAX_DEG
    assert abs((w + e) / 2 - -73.67) < 1e-4                                  # its middle
    from sizeup.gis import parse_bbox
    parse_bbox(f"{w},{s},{e},{n}", pp.BBOX_MAX_DEG)                          # the hub takes it


def test_the_previews_footprint_choice_saves_on_a_call_keyed_meanwhile(api):
    c = api.cad
    c.gis = fake_gis(numbers=(100, 102, 104))
    iid = c.store.create_incident(type="STRU", address="104 Example Ave", lat=lat_of(104), lon=LON)
    q = f"lat={lat_of(104)}&lon={LON}&incident_id={iid}"
    out = get(api, "/api/cad/preplan/where", q, JULIAN)[1]               # the call not keyed yet: its footprint, a choice
    assert out["choices"]["building"] == "104 Example Ave, Port Chester"
    code, out = call(api, "/api/cad/preplan", {"incident_id": iid, "kind": "fdc", "lat": lat_of(104), "lon": LON,
                                               "link": "building"}, JULIAN)  # keyed on save: the same building
    assert code == 200 and out["point"]["bldg_key"] == "104|example ave|port chester"


@pytest.mark.parametrize("pin", [None, (lat_of(100) + 0.001, LON)])         # no pin; a pin 110 m from any footprint
def test_a_call_with_no_footprint_of_its_own_keeps_its_unaddressed_building(cad, pin):
    cad.gis = fake_gis(parcels=False)                                      # no parcel can be keyed
    kw = {"lat": pin[0], "lon": pin[1]} if pin else {}
    iid = cad.store.create_incident(type="STRU", address="100 Example Ave", **kw)
    cad.state()
    assert cad.store.incident(iid)["bldg_key"] == "100|example ave|port chester"
    p = cad.preplan.create({"incident_id": iid, "kind": "knox_box", "lat": lat_of(100), "lon": LON})["point"]
    assert (p["bldg_key"], p["link"], p["building_ref"]) == ("100|example ave|port chester", "call", "buildings/7100")
    assert [x["id"] for x in pushed(cad, iid)] == [p["id"]]                # it shows on that call
