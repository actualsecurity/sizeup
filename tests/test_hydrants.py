"""Hydrants (#799, sizeup/cad/hydrants.py, static/hydrants.js): the department's overlay on the county's hydrant layer:
status, flow and its NFPA 291 class, thread, outlets, a nudged point, added and removed hydrants; admins edit it (map,
Settings, CSV), everyone sees it on the maps and on a call.

Department data: everything here is invented ("9001" on Sample St in an invented "Sampletown", county ids 990001 ...,
asset ids "99-0001"); the shapes are the county layer's (OBJECTID as the feature id, FIRENAME, HYD_NUMBER, MUNCIPALIT)."""
import csv
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cad import hydrants as hy
from sizeup.cad.api import HYDRANT, CadApi, needs
from sizeup.cloud.access import Identity
from sizeup.gis import DEFAULT_LAYERS, Gis, geometry_bbox
from tests.test_cad import make_cad
from tests.test_roles import CASEY, JORDAN, VAL, call, people

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")

LAT0, LON0 = 41.0, -73.66
DLAT = 0.0009                          # about 100 m north per step


def hyd(fid, number, n, district="SAMPLE FD", dlon=0.0):
    """A county hydrant as the layer gives it (the feature id is its OBJECTID)."""
    return {"type": "Feature", "id": fid, "properties": {"FIRENAME": district, "HYD_NUMBER": number, "MUNCIPALIT": "SAMPLE_FD"},
            "geometry": {"type": "Point", "coordinates": [round(LON0 + dlon, 6), round(LAT0 + n * DLAT, 6)]}}


def town(name, w, s, e, n):
    return {"type": "Feature", "id": len(name), "properties": {"NAME": name},
            "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}}


COUNTY = [hyd(990001, "9001", 0), hyd(990002, "9002", 1), hyd(990003, "9003", 2), hyd(990004, "9001", 30),   # 9001 twice
          hyd(990005, "9005", 3, dlon=0.002)]
TOWNS = [town("Sampletown", -73.70, 40.95, -73.659, 41.10), town("Exampleville", -73.659, 40.95, -73.60, 41.10)]


def set_layer(g, key, feats):
    g.data[key] = feats
    g._boxes[key] = [geometry_bbox(f["geometry"]) for f in feats]
    g.status[key].update(at=1.8e9, n=len(feats))
    with g._lock:
        g._changed(key)


def fake_gis(county=COUNTY):
    g = Gis(layers=tuple(l for l in DEFAULT_LAYERS if l.key in ("hydrants", "municipalities")), load=False)
    g.cache_loaded = True
    set_layer(g, "hydrants", [json.loads(json.dumps(f)) for f in county])
    set_layer(g, "municipalities", TOWNS)
    return g


def wire(c, g):
    c.gis = g
    g.overlays["hydrants"] = c.hydrants


@pytest.fixture
def cad(tmp_path):
    c = make_cad(tmp_path)
    wire(c, fake_gis())
    return c


@pytest.fixture
def api(tmp_path):
    a = CadApi(make_cad(tmp_path))
    wire(a.cad, fake_gis())
    a.ids = people(a.cad)
    return a


def get(api, path, email=CASEY):
    code, raw, *_ = api.get(path.split("?")[0], path.split("?")[1] if "?" in path else "", Identity(email=email) if email else None)
    return code, json.loads(raw) if raw[:1] == b"{" else raw


def served(g):
    code, body, _, h = g.get("/api/cad/gis/hydrants", "")
    assert code == 200
    return json.loads(body), h


def by_key(doc):
    return {f["properties"]["_k"]: f for f in doc["features"]}


# -- the class (NFPA 291) ---------------------------------------------------------------------------------------------

def test_the_nfpa_291_class_from_the_flow():
    assert [hy.class_of(g) for g in (2000, 1500, 1499, 1000, 999, 500, 499, 0)] == ["AA", "AA", "A", "A", "B", "B", "C", "C"]
    assert hy.class_of(None) is None                                         # no flow: no class, never red
    assert [hy.color_of(c) for c in ("AA", "A", "B", "C", None)] == ["Light blue", "Green", "Orange", "Red", ""]
    # with both pressures: the flow at 20 psi residual (Q_R = Q_F x ((S - 20) / (S - R)) ^ 0.54)
    assert hy.rated_gpm(1000, 80, 60) == round(1000 * (60 / 20) ** 0.54)      # 1810: AA though 1000 flowed
    assert hy.class_of(hy.rated_gpm(1000, 80, 60)) == "AA"
    assert hy.rated_gpm(800, None, 50) == 800 and hy.rated_gpm(800, 50, 60) == 800   # no sense: as entered
    assert hy.rated_gpm(None, 80, 60) is None


def test_the_class_derived_and_set_by_hand(cad):
    cad.hydrants.edit("c990001", {"flow_gpm": 1200})
    h = cad.hydrants.one("c990001", "viewer")["hydrant"]
    assert h["cls"] == "A" and h["class_color"] == "Green" and not h["class_set"] and h["gpm"] == 1200
    cad.hydrants.edit("c990001", {"nfpa_class": "B"})                         # an admin's class wins
    h = cad.hydrants.one("c990001", "viewer")["hydrant"]
    assert h["cls"] == "B" and h["class_set"] and h["class_flow"] == "A"
    cad.hydrants.edit("c990001", {"nfpa_class": "", "flow_gpm": ""})
    assert cad.hydrants.one("c990001", "viewer")["hydrant"]["cls"] is None


# -- the merge, served as the county layer ------------------------------------------------------------------------------

def test_the_layer_is_served_with_the_overlay(cad):
    g = cad.gis
    doc, h = served(g)
    assert len(doc["features"]) == 5 and all("_cls" not in f["properties"] for f in doc["features"])
    etag = h["ETag"]
    cad.hydrants.edit("c990002", {"status": "oos", "status_reason": "Broken stem", "flow_gpm": 1600})
    doc, h2 = served(g)
    assert h2["ETag"] != etag                                                # a write: a new ETag and a new rev
    p = by_key(doc)["c990002"]["properties"]
    assert p["_st"] == "oos" and p["_cls"] == "AA" and p["_gpm"] == 1600 and p["_no"] == "9002"
    assert by_key(doc)["c990002"]["id"] == 990002
    cat = {l["key"]: l for l in g.public()["layers"]}
    assert str(cat["hydrants"]["rev"]).endswith(f".{cad.hydrants.gen}")      # the pages fetch it again
    # the county's cached copy is untouched
    assert g.data["hydrants"][1]["properties"] == COUNTY[1]["properties"]
    # an overlay that fails: the county's layer is still served
    class Broken:
        gen = 1

        def apply(self, key, feats):
            raise RuntimeError("boom")
    g.overlays["hydrants"] = Broken()
    g._bytes.clear()
    assert len(served(g)[0]["features"]) == 5


def test_move_nudge_with_the_county_point_kept(cad):
    h = cad.hydrants
    out = h.edit("c990003", {"lat": LAT0 + 2 * DLAT + 0.00009, "lon": LON0})   # about 10 m north
    assert out["changed"] == ["lat", "lon"] and 9 < out["hydrant"]["nudge_m"] < 11
    assert out["hydrant"]["county_lat"] == pytest.approx(LAT0 + 2 * DLAT)
    f = by_key(served(cad.gis)[0])["c990003"]
    assert f["geometry"]["coordinates"][1] == pytest.approx(LAT0 + 2 * DLAT + 0.00009)
    with pytest.raises(hy.CadError, match="at most 300 m"):                 # a mistake: Remove it and Add one
        h.edit("c990003", {"lat": LAT0 + 2 * DLAT + 0.01, "lon": LON0})
    with pytest.raises(hy.CadError, match="outside the map"):
        h.create({"lat": 10.0, "lon": 10.0})
    out = h.edit("c990003", {"action": "reset_position"})                    # back to the county's point
    assert out["hydrant"]["nudge_m"] is None and out["hydrant"]["lat"] == pytest.approx(LAT0 + 2 * DLAT)


def test_add_and_remove_and_restore(cad):
    h = cad.hydrants
    out = h.create({"lat": LAT0 + 5 * DLAT, "lon": LON0, "number": "P-1", "owner": "Private", "body_color": "Red"})
    k = out["key"]
    assert re.fullmatch(r"h\d+", k) and out["hydrant"]["hub"] and out["hydrant"]["owner"] == "private"
    doc = by_key(served(cad.gis)[0])
    assert doc[k]["properties"]["_hub"] is True and doc[k]["properties"]["_no"] == "P-1" and "id" not in doc[k]
    h.edit("c990004", {"action": "remove"})
    assert "c990004" not in by_key(served(cad.gis)[0])
    assert h.listing({"flag": ["removed"]})["rows"][0]["key"] == "c990004"
    h.edit("c990004", {"action": "restore"})
    assert "c990004" in by_key(served(cad.gis)[0])
    assert h.create({"lat": LAT0 + 6 * DLAT, "lon": LON0})["key"] != k


# -- the county's monthly refresh -------------------------------------------------------------------------------------

def test_the_overlay_survives_a_county_refresh(cad):
    h, g = cad.hydrants, cad.gis
    h.edit("c990001", {"status": "oos", "asset_id": "99-0001"})
    h.edit("c990002", {"flow_gpm": 900})
    set_layer(g, "hydrants", [json.loads(json.dumps(f)) for f in COUNTY])      # the same layer fetched again
    doc = by_key(served(g)[0])
    assert doc["c990001"]["properties"]["_st"] == "oos" and doc["c990002"]["properties"]["_cls"] == "B"
    assert h.listing({"flag": ["missing"]})["total"] == 0


def test_a_renumbered_layer_rebinds_by_the_anchor(cad):
    """The county republished the layer with new object ids: each overlay follows its hydrant (the same district and
    number near the anchor; or the same point), the duplicated number 9001 included."""
    h, g = cad.hydrants, cad.gis
    h.edit("c990001", {"status": "oos"})
    h.edit("c990004", {"flow_gpm": 1600})                                   # the other 9001, 3 km north
    h.edit("c990005", {"notes": "Behind the fence"})
    moved = [json.loads(json.dumps(f)) for f in COUNTY]
    for f in moved:
        f["id"] += 100                                                      # every id new
    moved[4]["properties"]["HYD_NUMBER"] = "9055"                          # and a number corrected, the point the same
    set_layer(g, "hydrants", moved)
    items = h.index()["by_key"]
    assert items["c990001"]["fid"] == 990101 and items["c990001"]["rebound"] and items["c990001"]["status"] == "oos"
    assert items["c990004"]["fid"] == 990104 and items["c990004"]["cls"] == "AA"
    assert items["c990005"]["fid"] == 990105
    assert "c990102" in items and "c990101" not in items                    # the unbound ones by their new id; no twins
    assert len(served(g)[0]["features"]) == 5 and h.listing({"flag": ["missing"]})["total"] == 0


def test_an_id_that_disappears_is_kept_and_flagged(cad):
    h, g = cad.hydrants, cad.gis
    h.edit("c990003", {"status": "dry", "notes": "Gone?"})
    set_layer(g, "hydrants", [json.loads(json.dumps(f)) for f in COUNTY if f["id"] != 990003])
    it = h.index()["by_key"]["c990003"]
    assert it["missing"] and it["status"] == "dry" and it["lat"] == pytest.approx(LAT0 + 2 * DLAT)
    lst = h.listing({"flag": ["missing"]})
    assert lst["total"] == 1 and lst["counts"]["missing"] == 1
    assert by_key(served(g)[0])["c990003"]["properties"]["_miss"] is True   # still drawn, where it was
    with pytest.raises(hy.CadError, match="no longer has"):
        h.edit("c990003", {"action": "reset_position"})
    # the same id now somewhere else and numbered otherwise: not this hydrant (missing, the new one plain)
    other = [json.loads(json.dumps(f)) for f in COUNTY]
    other[2]["geometry"]["coordinates"][1] += 0.01
    other[2]["properties"]["HYD_NUMBER"] = "9999"
    set_layer(g, "hydrants", other)
    assert h.index()["by_key"]["c990003"]["missing"]


def test_swapped_ids_follow_their_hydrants(cad):
    """Review of #799: the county swaps two edited hydrants' ids: each overlay stays on its own hydrant, no key twice."""
    h, g = cad.hydrants, cad.gis
    h.edit("c990001", {"status": "oos"})
    h.edit("c990002", {"status": "dry"})
    sw = [json.loads(json.dumps(f)) for f in COUNTY]
    sw[0]["id"], sw[1]["id"] = 990002, 990001
    set_layer(g, "hydrants", sw)
    items = h.index()["by_key"]
    assert items["c990001"]["fid"] == 990002 and items["c990001"]["status"] == "oos" and not items["c990001"]["missing"]
    assert items["c990002"]["fid"] == 990001 and items["c990002"]["status"] == "dry"
    feats = served(g)[0]["features"]
    keys = [f["properties"]["_k"] for f in feats]
    assert len(keys) == len(set(keys)) == 5
    ids = [f["id"] for f in feats if "id" in f]
    assert len(ids) == len(set(ids))
    # the hydrant now at 9001's point (id 990002) opens and edits 9001's row
    h.edit("c990001", {"notes": "Checked"})
    assert h.store.one("SELECT county_id, notes FROM hydrants WHERE key = 'c990001'")["notes"] == "Checked"


def test_a_reused_id_gets_a_key_of_its_own(cad):
    """A row rebound elsewhere keeps its key; a new hydrant given the old id gets another key, and is editable."""
    h, g = cad.hydrants, cad.gis
    h.edit("c990001", {"flow_gpm": 1200})
    re_ = [json.loads(json.dumps(f)) for f in COUNTY]
    re_[0]["id"] = 990009                                                # 9001 under a new id ...
    re_.append(hyd(990001, "9100", 10))                                  # ... and its old id on a new hydrant 900 m off
    set_layer(g, "hydrants", re_)
    items = h.index()["by_key"]
    assert items["c990001"]["fid"] == 990009 and items["c990001"]["cls"] == "A"
    assert items["c990001-2"]["fid"] == 990001 and items["c990001-2"]["row"] is None
    keys = [f["properties"]["_k"] for f in served(g)[0]["features"]]
    assert len(keys) == len(set(keys))
    out = h.edit("c990001-2", {"status": "oos"})
    assert out["changed"] == ["status"] and h.index()["by_key"]["c990001-2"]["status"] == "oos"
    assert h.index()["by_key"]["c990001"]["cls"] == "A"                    # both bound to their own after the write


def test_an_edit_that_changes_nothing_writes_nothing(cad):
    h = cad.hydrants
    for body in ({}, {"status": "unknown"}, {"notes": ""}, {"status": "Unknown", "flow_gpm": ""}):
        assert h.edit("c990001", body)["changed"] == []
    assert h.store.q("SELECT * FROM hydrants") == []
    assert h.edit("c990001", {"status": "oos"})["changed"] == ["status"]


def test_a_failed_merge_is_never_kept(cad):
    g = cad.gis
    real = g.overlays["hydrants"]

    class Flaky:
        gen, calls = real.gen, 0

        def apply(self, key, feats):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("database is locked")
            return real.apply(key, feats)
    cad.hydrants.edit("c990002", {"status": "oos"})
    Flaky.gen = real.gen
    g.overlays["hydrants"] = Flaky()
    g._bytes.clear()
    assert "_st" not in json.dumps(served(g)[0])                             # the county's, this once
    assert by_key(served(g)[0])["c990002"]["properties"]["_st"] == "oos"     # and the overlay's on the next request


def test_forged_and_unknown_keys(cad):
    for k in ("c1", "c990099", "h1", "x990001", "c0", "../c990001", "c990001; DROP"):
        with pytest.raises(hy.CadError):
            cad.hydrants.edit(k, {"status": "oos"})
    assert cad.store.q("SELECT * FROM hydrants") == []
    assert not HYDRANT.fullmatch("/api/cad/hydrants/c99/../x") and not HYDRANT.fullmatch("/api/cad/hydrants/c0")


# -- validation ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body,err", [
    ({"status": "broken"}, "status"), ({"flow_gpm": -5}, "flow_gpm"), ({"flow_gpm": "lots"}, "flow_gpm"),
    ({"flow_gpm": 1200.5}, "whole"), ({"static_psi": 400}, "static_psi"), ({"nfpa_class": "D"}, "nfpa_class"),
    ({"thread": "garden"}, "thread"), ({"flow_date": "31/12/2026"}, "flow_date"), ({"notes": "x" * 501}, "notes"),
    ({"lat": "abc", "lon": 1}, "lat"), ({"lat": float("nan"), "lon": LON0}, "lat"), ({"owner": "me"}, "owner"),
    ({"action": "explode"}, "action"), ({"flow_gpm": True}, "flow_gpm"),
    ({"lat": 10 ** 400, "lon": LON0}, "lat"), ({"flow_gpm": 10 ** 400}, "flow_gpm"), ({"notes": ["a", "b"]}, "notes"),
    ({"flow_date": "2026-05-01junk"}, "flow_date"), ({"flow_date": "13/45/2026"}, "flow_date"),
])
def test_bad_values_are_refused(cad, body, err):
    with pytest.raises(hy.CadError, match=err):
        cad.hydrants.edit("c990001", body)
    assert cad.store.q("SELECT * FROM hydrants") == []


def test_dates_as_a_spreadsheet_saves_them(cad):
    out = cad.hydrants.edit("c990001", {"flow_date": "6/30/2026", "inspected": "2026-11-01T00:00", "status_since": "11/2/2026 0:00"})
    hh = out["hydrant"]
    assert (hh["flow_date"], hh["inspected"], hh["status_since"]) == ("2026-06-30", "2026-11-01", "2026-11-02")


def test_the_labels_and_formula_guard_are_read(cad):
    out = cad.hydrants.edit("c990001", {"status": "Out of service", "thread": "Storz 5\"", "owner": "Water company",
                                        "outlets": "2×2.5, 1×4.5", "location": "'=Sample St", "flow_date": "2026-05-01"})
    hh = out["hydrant"]
    assert (hh["status"], hh["thread"], hh["owner"], hh["outlets"]) == ("oos", "storz5", "water_company", "2x2.5, 1x4.5")
    assert hh["location"] == "=Sample St" and hh["flow_date"] == "2026-05-01"


# -- permissions and the audit trail ---------------------------------------------------------------------------------------

def test_only_admins_write(api):
    for p in ("/api/cad/hydrants", "/api/cad/hydrants/c990001", "/api/cad/hydrants/h1", "/api/cad/hydrants/import"):
        assert needs(p) == "admin", p
    for email in (JORDAN, VAL):
        code, out = call(api, "/api/cad/hydrants/c990001", {"status": "oos"}, email)
        assert code == 403, (email, out)
        assert call(api, "/api/cad/hydrants", {"lat": LAT0, "lon": LON0}, email)[0] == 403
        assert call(api, "/api/cad/hydrants/import", {"action": "apply", "csv": "key,status\nc990001,oos\n"}, email)[0] == 403
        assert get(api, "/api/cad/hydrants", email)[0] == 403                   # the list and the CSV: admins
        assert get(api, "/api/cad/hydrants.csv", email)[0] == 403
        code, card = get(api, "/api/cad/hydrants/c990001", email)                # the card: anyone signed in
        assert code == 200 and card["history"] == []
    assert api.cad.store.q("SELECT * FROM hydrants") == []
    code, out = call(api, "/api/cad/hydrants/c990001", {"status": "oos", "operator": "Someone Else"}, CASEY)
    assert code == 200 and out["changed"] == ["status"]
    assert get(api, "/api/cad/hydrants/c990999")[0] == 404
    assert call(api, "/api/cad/hydrants/c990999", {"status": "oos"}, CASEY)[0] == 400


def test_the_audit_trail_is_the_hydrants_history(api):
    call(api, "/api/cad/hydrants/c990001", {"status": "oos", "status_reason": "Leaking"}, CASEY)
    call(api, "/api/cad/hydrants/c990001", {"status": "in_service"}, CASEY)
    call(api, "/api/cad/hydrants/c990001", {"lat": LAT0 + 0.00005, "lon": LON0}, CASEY)
    code, card = get(api, "/api/cad/hydrants/c990001", CASEY)
    hist = card["history"]
    assert code == 200 and len(hist) == 3 and all(h["by"] == "Casey" for h in hist)    # the verified person, not the page's
    assert json.loads(hist[2]["new"]) == {"status": "oos", "status_reason": "Leaking"}
    assert json.loads(hist[1]["old"]) == {"status": "oos"} and json.loads(hist[1]["new"]) == {"status": "in_service"}
    assert set(json.loads(hist[0]["new"])) == {"lat", "lon"}
    row = api.cad.store.one("SELECT updated_by FROM hydrants WHERE key = 'c990001'")
    assert row["updated_by"].startswith("Casey")
    acts = [a["summary"] for a in api.cad.activity.rows(CASEY, "action", None, 50)]
    assert "Moved hydrant c990001" in acts and "Edited hydrant c990001: status" in acts


# -- an incident's nearest hydrants -----------------------------------------------------------------------------------------

def test_an_incident_gets_its_nearest_and_the_out_of_service_ones(cad):
    cad.hydrants.edit("c990002", {"status": "oos"})
    cad.hydrants.edit("c990001", {"flow_gpm": 1100})
    iid = cad.store.create_incident(type="STRU", address="10 Sample St", lat=LAT0 + 0.5 * DLAT, lon=LON0)
    inc = next(i for i in cad.state()["incidents"] if i["id"] == iid)
    near = inc["hydrants"]["near"]
    assert [h["key"] for h in near[:2]] == ["c990001", "c990002"] or [h["key"] for h in near[:2]] == ["c990002", "c990001"]
    assert all(h["dist_m"] <= hy.NEAR_M for h in near) and "c990004" not in [h["key"] for h in near]   # 3 km off
    assert [h["key"] for h in inc["hydrants"]["down"]] == ["c990002"]
    assert next(h for h in near if h["key"] == "c990001")["cls"] == "A"
    assert cad.state()["hydrants"]["gen"] == cad.hydrants.gen
    cad.hydrants.edit("c990002", {"action": "remove"})                       # a removed one is never offered
    inc = next(i for i in cad.state()["incidents"] if i["id"] == iid)
    assert "c990002" not in [h["key"] for h in inc["hydrants"]["near"]] and inc["hydrants"]["down"] == []


def test_the_town_comes_from_the_boundary_not_the_district(cad):
    assert cad.hydrants.one("c990001", "viewer")["hydrant"]["town"] == "Sampletown"
    assert cad.hydrants.one("c990005", "viewer")["hydrant"]["town"] == "Exampleville"
    lst = cad.hydrants.listing({"town": ["Exampleville"]})
    assert [r["key"] for r in lst["rows"]] == ["c990005"] and lst["towns"] == ["Exampleville", "Sampletown"]


def test_the_list_filters(cad):
    h = cad.hydrants
    h.edit("c990001", {"status": "oos", "location": "Sample St at Example Ave"})
    h.edit("c990002", {"flow_gpm": 700})
    assert {r["key"] for r in h.listing({"flag": ["oos"]})["rows"]} == {"c990001"}
    assert "c990002" not in {r["key"] for r in h.listing({"flag": ["no_flow"]})["rows"]}
    assert [r["key"] for r in h.listing({"q": ["sample st"]})["rows"]] == ["c990001"]
    assert h.listing({"q": ["9001"]})["total"] == 2
    with pytest.raises(hy.CadError):
        h.listing({"flag": ["bogus"]})


# -- CSV: export, import with preview, apply -------------------------------------------------------------------------------

def rows_of(data: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def test_csv_round_trip_preview_and_apply(cad):
    h = cad.hydrants
    h.edit("c990001", {"flow_gpm": 1200, "asset_id": "99-0001"})
    data, n = h.csv({})
    rows = rows_of(data)
    assert n == 5 and list(rows[0]) == list(hy.Hydrants.COLUMNS)
    r1 = next(r for r in rows if r["key"] == "c990001")
    assert r1["flow_gpm"] == "1200" and r1["class"] == "A" and r1["town"] == "Sampletown" and r1["county_number"] == "9001"
    # the chiefs' extra columns: only asset_id and location (the other fields untouched), one new hydrant, an unknown column
    text = ("key,asset_id,location,water_zone\n"
            "c990001,99-0001,Sample St,Z1\n"
            "c990002,99-0002,Sample St at Example Ave,Z1\n"
            "c990003,,,Z2\n")
    pv = h.import_csv({"action": "preview", "csv": text})
    assert pv["changed"] == 2 and pv["unchanged"] == 1 and pv["errors"] == [] and pv["ignored"] == ["water_zone"]
    c1 = next(p for p in pv["preview"] if p["key"] == "c990001")
    assert c1["changes"] == {"location": ["", "Sample St"]}                  # the asset id was the same
    assert h.store.one("SELECT location FROM hydrants WHERE key = 'c990001'")["location"] == ""   # a preview writes nothing
    out = h.import_csv({"action": "apply", "csv": text, "operator": "Casey <casey@example.org>"})
    assert out["applied"] and out["changed"] == 2
    one = h.one("c990001", "admin")["hydrant"]
    assert one["location"] == "Sample St" and one["flow_gpm"] == 1200          # flow untouched: not a column of the file
    assert h.one("c990002", "admin")["hydrant"]["asset_id"] == "99-0002"
    assert len(h.store.q("SELECT * FROM settings_audit WHERE section = 'hydrants'")) == 3   # one row per hydrant changed
    # the whole export back in: nothing to do
    assert h.import_csv({"action": "preview", "csv": h.csv({})[0].decode("utf-8-sig")})["changed"] == 0


def test_csv_import_of_every_point_moves_nothing(cad):
    """A file with each hydrant's own point (another export of the same points): no nudges; a real move: one."""
    text = "key,lat,lon\n" + "".join(f"c{f['id']},{f['geometry']['coordinates'][1] + 0.000001},{f['geometry']['coordinates'][0]}\n"
                                      for f in COUNTY[:4])
    text += f"c990005,{LAT0 + 3 * DLAT + 0.00009},{LON0 + 0.002}\n"
    pv = cad.hydrants.import_csv({"action": "preview", "csv": text})
    assert pv["changed"] == 1 and pv["unchanged"] == 4 and pv["preview"][0]["key"] == "c990005"


def test_csv_import_validation_refuses_the_whole_file(cad):
    h = cad.hydrants
    text = ("key,status,flow_gpm,lat,lon\n"
            "c990001,oos,1500,,\n"
            "c990002,sideways,,,\n"                                          # a bad status
            "c990099,oos,,,\n"                                               # no such hydrant
            "c990001,oos,,,\n"                                               # twice
            "c990003,,abc,,\n"                                               # a bad number
            "c990004,,,41.5,-73.66\n"                                        # moved 50 km
            ",in_service,800,41.0001,-73.6601\n")                           # a new one: fine
    pv = h.import_csv({"action": "preview", "csv": text})
    assert pv["error_count"] == 5 and {e["line"] for e in pv["errors"]} == {3, 4, 5, 6, 7}
    assert pv["new"] == 1 and pv["changed"] == 1
    with pytest.raises(hy.CadError, match="nothing applied"):
        h.import_csv({"action": "apply", "csv": text})
    assert h.store.q("SELECT * FROM hydrants") == []
    for bad in ("", "status\noos\n", "key,key\nc990001,c990001\n", "key,notes\nc990001,\"" + "x" * 200000 + "\"\n"):
        with pytest.raises(hy.CadError):
            h.import_csv({"action": "preview", "csv": bad})


def test_csv_import_never_adds_a_hydrant_twice(cad):
    text = "key,lat,lon,number\n,41.0002,-73.66,P-9\n"
    assert cad.hydrants.import_csv({"action": "apply", "csv": text})["new"] == 1
    pv = cad.hydrants.import_csv({"action": "preview", "csv": text})          # the same file again
    assert pv["error_count"] == 1 and "within 3 m" in pv["errors"][0]["error"]
    pv = cad.hydrants.import_csv({"action": "preview", "csv": "key,lat,lon\n,41.01,-73.66\n,41.01001,-73.66\n"})
    assert pv["error_count"] == 1 and "this file" in pv["errors"][0]["error"]
    pv = cad.hydrants.import_csv({"action": "preview", "csv": "key,class,town,notes\nc990001,AA,X,hi\n"})
    assert pv["read_only"] == ["class", "town"] and pv["changed"] == 1


def test_csv_import_through_the_api(api):
    text = "key,status,status_reason,flow_gpm\nc990001,oos,Broken cap,450\n,,,\n"
    code, pv = call(api, "/api/cad/hydrants/import", {"action": "preview", "csv": text}, CASEY)
    assert code == 200 and pv["changed"] == 1 and pv["rows"] == 1
    code, out = call(api, "/api/cad/hydrants/import", {"action": "apply", "csv": text}, CASEY)
    assert code == 200 and out["applied"]
    h = get(api, "/api/cad/hydrants/c990001")[1]["hydrant"]
    assert h["status"] == "oos" and h["cls"] == "C" and h["status_reason"] == "Broken cap"
    acts = [a["summary"] for a in api.cad.activity.rows(CASEY, "action", None, 50)]
    assert "Imported hydrants: 1 changed, 0 new" in acts


def test_the_csv_is_guarded_against_formulas(cad):
    cad.hydrants.edit("c990001", {"notes": "=HYPERLINK(1)"})
    r = next(r for r in rows_of(cad.hydrants.csv({})[0]) if r["key"] == "c990001")
    assert r["notes"] == "'=HYPERLINK(1)"
    pv = cad.hydrants.import_csv({"action": "preview", "csv": "key,notes\nc990001,'=HYPERLINK(1)\n"})
    assert pv["changed"] == 0                                               # the guard is read back off


def test_a_cad_without_the_county_layer(tmp_path):
    c = make_cad(tmp_path)                                                  # (a station, a dev box): nothing to merge
    assert c.hydrants.index()["items"] == []
    with pytest.raises(hy.CadError):
        c.hydrants.create({"lat": LAT0, "lon": LON0})
    iid = c.store.create_incident(type="STRU", address="10 Sample St", lat=LAT0, lon=LON0)
    assert "hydrants" not in next(i for i in c.state()["incidents"] if i["id"] == iid)


def test_a_thousand_hydrants_merge_quickly(tmp_path):
    import time
    county = [hyd(900000 + i, str(i % 400), 0, dlon=0.0) for i in range(7000)]
    for i, f in enumerate(county):
        f["geometry"]["coordinates"] = [round(LON0 + (i % 100) * 0.0005, 6), round(LAT0 + (i // 100) * 0.0005, 6)]
    c = make_cad(tmp_path)
    wire(c, fake_gis(county))
    text = "key,flow_gpm,status\n" + "".join(f"c{900000 + i},{400 + i % 1500},{'oos' if i % 50 == 0 else ''}\n"
                                                for i in range(0, 7000, 7))
    assert c.hydrants.import_csv({"action": "apply", "csv": text})["changed"] == 1000
    t = time.perf_counter()
    doc, _ = served(c.gis)
    near = c.hydrants.near(LAT0 + 0.01, LON0 + 0.01)
    took = time.perf_counter() - t
    assert len(doc["features"]) == 7000 and len(near) == hy.NEAR_N and took < 2.0, took


# -- the page ------------------------------------------------------------------------------------------------------------

NODE_HARNESS = r"""
global.window = global;
require(process.argv[2]);
const H = window.Hydrants;
const out = {};
out.classes = H.CLASSES.map((c) => c[0]);
out.cls = [2000, 1500, 1499, 1000, 999, 500, 499, null].map(H.classOf);
out.rated = H.ratedGpm(1000, 80, 60);
out.img = [{ _cls: "AA" }, { _cls: "C", _st: "oos" }, { _st: "dry" }, {}, { _cls: "B", _st: "restricted" }].map(H.imageFor);
out.icon = H.iconExpr();
out.label = H.labelExpr();
out.dist = H.fmtDist(42.4);
out.line = H.line({ number: "9001", cls: "A", gpm: 1200, status: "oos", dist_m: 40 });
out.lineBare = H.line({ number: "9002", cls: null, gpm: null, status: "unknown", dist_m: 120 });
out.keys = H.LEGEND.map((l) => l[0]);
out.fc = [H.flowCheck(12000, 80, 10), H.flowCheck(1000, 60, 60), H.flowCheck(3000, null, null), H.flowCheck(1000, 80, 10),
          H.flowCheck(900, 70, null), H.flowCheck(1000, 80, 60)];
const tok = H.atToken("IFO @sam", 8);
out.tok = tok;
out.take = H.takeAt("IFO @sam", tok, { label: "Sample Market", lat: 41, lon: -73.6, kind: "place", sub: "Sampletown" }, []);
out.noTok = [H.atToken("a@b", 3), H.atToken("@ x", 3), H.atToken("@Sample Market x", 16, [{ label: "Sample Market" }])];
out.named = H.named("rear of @Sample Market", [{ label: "Sample Market" }, { label: "Gone" }]).map((p) => p.label);
out.down = H.DOWN;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_map_helpers_in_node(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(NODE_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "hydrants.js")], capture_output=True, text=True, check=True,
                                    timeout=30).stdout)
    assert out["classes"] == [c for c, _, _ in hy.CLASSES]                   # one table, both ends
    assert out["cls"] == ["AA", "AA", "A", "A", "B", "B", "C", None]
    assert out["rated"] == hy.rated_gpm(1000, 80, 60)
    assert out["img"] == ["hy-AA", "hy-down", "hy-down", "hy-unk", "hy-B-r"]   # out of service wins; no flow: neutral
    assert out["down"] == list(hy.DOWN)
    assert "hy-unk" in json.dumps(out["icon"]) and "_st" in json.dumps(out["icon"])
    assert "_cls" in json.dumps(out["label"]) and "_no" in json.dumps(out["label"])
    assert out["line"] == "9001 · A 1200 gpm · OOS · 40 m" and out["lineBare"] == "9002 · 120 m"
    assert out["keys"] == ["AA", "A", "B", "C", "unk", "down"]
    fc = out["fc"]
    assert "over 10000" in fc[0]["error"] and "under the static" in fc[1]["error"]
    assert fc[2]["error"] == "" and "unusually high" in fc[2]["notes"][0]
    assert fc[3]["error"] == "" and "under 20 psi" in fc[3]["notes"][0]
    assert "One pressure" in fc[4]["notes"][0] and fc[5] == {"error": "", "notes": []}
    assert out["tok"] == {"text": "sam", "start": 4, "end": 8}
    assert out["take"]["value"] == "IFO @Sample Market " and out["take"]["places"][0]["label"] == "Sample Market"
    assert out["noTok"] == [None, None, None] and out["named"] == ["Sample Market"]


def test_the_script_wires_the_layer_card_editor_and_settings():
    src = (STATIC / "hydrants.js").read_text()
    gis = (STATIC / "gislayers.js").read_text()
    assert "Hydrants.iconExpr()" in gis and "Hydrants.show(" in gis and "Hydrants.legend(" in gis
    assert '"/api/cad/hydrants/import"' in src and "/api/cad/hydrants.csv" in src and 'action: "preview"' in src
    assert "draggable: true" in src and "reset_position" in src and 'act(h, "remove")' in src
    assert 'canDo("admin")' in src                                           # editing: admins only (the hub refuses others)
    html = (STATIC / "cad.html").read_text()
    assert '<script src="/static/hydrants.js"></script>' in html and 'id="d-hydrants"' in html and 'id="s-hydrants"' in html
    assert html.index('src="/static/cad.js"') < html.index("/static/hydrants.js")       # cad.js's helpers first
    for line in src.splitlines():                                           # never a hydrant's details in the console
        assert not re.search(r"console\.\w+\([^)]*\b(lat|lon|notes|asset)\b", line), line


# -- in the browser (headless Chromium; skipped without Playwright). SIZEUP_SHOTS=<dir> keeps the screenshots ---------------

def test_the_maps_card_editor_call_and_settings_in_a_browser(tmp_path):
    import os
    import threading
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    from sizeup.web.server import WebServer
    c = make_cad(tmp_path)
    g = fake_gis()
    wire(c, g)
    h = c.hydrants
    h.edit("c990001", {"flow_gpm": 1650, "flow_date": "2026-05-01", "thread": "nst", "outlets": "2x2.5, 1x4.5",
                       "owner": "water_company", "asset_id": "99-0001", "location": "Sample St at Example Ave", "main_in": 8})
    h.edit("c990002", {"status": "oos", "status_reason": "Broken stem", "status_since": "2026-11-12", "flow_gpm": 1100,
                       "operator": "Casey <casey@example.org>"})
    h.edit("c990003", {"flow_gpm": 750, "status": "restricted", "status_reason": "One outlet seized"})
    h.edit("c990005", {"flow_gpm": 420})
    h.create({"lat": LAT0 + 1.5 * DLAT, "lon": LON0 + 0.0008, "number": "P-1", "owner": "private"})
    c.store.create_incident(type="STRU", address="10 Sample St", lat=LAT0 + 0.6 * DLAT, lon=LON0 + 0.0003)
    shots = os.environ.get("SIZEUP_SHOTS", "")
    # no basemap tiles (an empty dark map: no real streets); the labels' font from the basemap's font server only when
    # screenshots are being taken (the test itself runs offline: the style just needs a glyphs URL)
    fonts = "https://tiles.openfreemap.org/fonts/"
    scene = {"timezone": "America/New_York", "home": {"center": [LON0 + 0.0008, LAT0 + 0.00135], "zoom": 17.0},
             "glyphs": fonts + "{fontstack}/{range}.pbf"}
    ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda iid: False, cad=CadApi(c), gis=g)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    shot = (lambda loc, name: loc.screenshot(path=os.path.join(shots, name))) if shots else (lambda loc, name: None)
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001
        pw.stop()
        ws.shutdown()
        pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
    try:
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(base) and not (shots and url.startswith(fonts)), lambda route: route.abort())
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/cad")
        pg.fill("#operator", "JD")
        pg.wait_for_selector("#ov-map canvas.maplibregl-canvas", state="attached")
        pg.wait_for_function("() => window.GisLayers && GisLayers.catalog() && [...GisLayers.maps].length > 0")
        pg.evaluate("() => MapLayers.setLayer('gis:hydrants', true)")
        pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            gm.map.jumpTo({ center: cad.scene.home.center, zoom: cad.scene.home.zoom }); }""")
        # the layer drawn by class and status: every hydrant's image as the map would pick it
        pg.wait_for_function("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            return gm && gm.map.getLayer('gis-hydrants-pt') && gm.map.hasImage('hy-down')
              && gm.map.querySourceFeatures('gis-hydrants').length >= 5; }""")
        got = pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            const out = {}; for (const f of gm.map.querySourceFeatures('gis-hydrants')) out[f.properties._k] = Hydrants.imageFor(f.properties);
            return out; }""")
        assert got["c990001"] == "hy-AA" and got["c990002"] == "hy-down" and got["c990003"] == "hy-B-r"
        assert got.get("c990004", "hy-unk") == "hy-unk" and got["c990005"] == "hy-C" and any(k.startswith("h") for k in got)
        pg.wait_for_timeout(600)
        shot(pg.locator("#ov-map"), "hydrants-map.png")
        # the card: a click on the out-of-service one
        pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            const f = gm.map.querySourceFeatures('gis-hydrants').find((x) => x.properties._k === 'c990002');
            Hydrants.show(gm, f.properties); }""")
        pg.wait_for_selector(".ml-info:not([hidden]) .hy-acts")
        info = pg.inner_text(".ml-info:not([hidden])")
        assert "Hydrant 9002" in info and "Out of service: Broken stem" in info and "Class A · 1100 gpm" in info
        assert "Sampletown" in info and "SAMPLE FD" in info
        pg.click(".ml-info:not([hidden]) .hy-hist summary")
        shot(pg.locator("#ov-map"), "hydrants-card.png")
        # the layer's legend in the Layers box
        pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            gm.control.closeInfo(); gm.control.toggle(); }""")
        pg.wait_for_selector("#ov-map .hy-legend")
        pg.evaluate("""() => { const d = document.querySelector('#ov-map .hy-legend').closest('details'); if (d) d.open = true;
            document.querySelector('#ov-map .hy-legend').scrollIntoView(); }""")
        shot(pg.locator("#ov-map .ml-panel"), "hydrants-legend.png")
        assert "no flow" in pg.inner_text("#ov-map .hy-legend")
        pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map')); gm.control.close(); }""")
        # Edit: the dialog, a save
        pg.evaluate("""async () => { const d = await api('GET', '/api/cad/hydrants/c990005'); Hydrants.editor(d.hydrant); }""")
        pg.wait_for_selector(".dialog .hy-form")
        pg.fill(".hy-form label:has-text('Flow (gpm)') input", "1250")
        pg.wait_for_selector(".hy-form .hy-line:has-text('Class A')")
        shot(pg.locator(".dialog:has(.hy-form)"), "hydrants-edit.png")
        pg.click(".dialog:has(.hy-form) button[type=submit]")
        pg.wait_for_selector(".dialog .hy-form", state="detached")
        assert h.one("c990005", "admin")["hydrant"]["flow_gpm"] == 1250
        # the call: its nearest hydrants, the out-of-service one called out
        iid = c.store.q("SELECT id FROM incidents")[0]["id"]
        pg.wait_for_function(f"() => cad.state && (cad.state.incidents || []).some((i) => i.id === {iid} && i.hydrants)")
        pg.evaluate(f"() => selectIncident({iid})")
        pg.wait_for_selector("#d-hydrants:not([hidden]) .pp-row")
        txt = pg.inner_text("#d-hydrants")
        assert "1 OOS nearby" in txt and "9002 · A 1100 gpm · OOS" in txt
        shot(pg.locator("#d-hydrants"), "hydrants-call.png")
        # Settings > Hydrants: the list, a CSV preview
        pg.goto(base + "/cad#settings/hydrants")
        pg.wait_for_selector("#s-hydrants .hy-table tbody tr")
        assert pg.locator("#s-hydrants .hy-table tbody tr").count() == 6
        csvf = tmp_path / "chiefs.csv"
        csvf.write_text("key,asset_id,location,status\nc990004,99-0004,Sample St,in_service\nc990099,99-0099,Nowhere,\n")
        pg.set_input_files("#s-hydrants input[type=file]", str(csvf))
        pg.wait_for_selector("#s-hydrants .hy-import")
        imp = pg.inner_text("#s-hydrants .hy-import")
        assert "1 changed" in imp and "1 in error" in imp and "no hydrant c990099" in imp
        assert pg.is_disabled("#s-hydrants .hy-import button.primary")
        shot(pg.locator("#s-hydrants"), "hydrants-settings.png")
        assert errors == []
    finally:
        browser.close()
        pw.stop()
        ws.shutdown()


# -- #831 in production: every hydrant gone from the maps ------------------------------------------------------------------
# The county's f=geojson gives a feature no "id" unless the object id field is among the outFields (checked against the
# live service, 2026-11-20); the hub asked for the shown fields only, so its copy had no ids, and the merge dropped every
# feature without one: an empty layer. These run the layer as the county really answers, with an empty overlay (the
# default in production).

def arcgis_like(features):
    """A fetch answering as the county's MapServer does: the description (objectIdField OBJECTID), and each query page with
    only the outFields asked for, and the feature "id" only when OBJECTID is one of them."""
    from tests.test_gis import arcgis_answer
    info = {"name": "Fire Hydrants", "geometryType": "esriGeometryPoint", "maxRecordCount": 1000, "capabilities": "Map,Query,Data",
            "objectIdField": "OBJECTID", "fields": [{"name": n, "type": t} for n, t in (
                ("OBJECTID", "esriFieldTypeOID"), ("DISTRICT", "esriFieldTypeString"), ("MUNCIPALIT", "esriFieldTypeString"),
                ("HYD_NUMBER", "esriFieldTypeString"), ("FIRENAME", "esriFieldTypeString"))]}
    full = [{"type": "Feature", "id": f["id"], "geometry": f["geometry"], "properties": {"OBJECTID": f["id"], **f["properties"]}}
            for f in features]
    url = next(l.url for l in DEFAULT_LAYERS if l.key == "hydrants")
    asked = []

    def fetch(u, timeout, limit):
        asked.append(u)
        code, h, body = arcgis_answer(u, {"hydrants": {"url": url, "info": info, "features": full}})
        if "/query" in u and code == 200:
            from urllib.parse import parse_qs, urlsplit
            out = (parse_qs(urlsplit(u).query).get("outFields") or ["*"])[0]
            if out != "*" and "OBJECTID" not in out.split(","):
                doc = json.loads(body)
                for f in doc["features"]:
                    f.pop("id", None)
                body = json.dumps(doc).encode()
        return code, h, body
    fetch.asked = asked
    return fetch


def test_the_layer_as_the_county_serves_it_keeps_ids_and_draws_every_hydrant(tmp_path):
    fetch = arcgis_like(COUNTY)
    g = Gis(layers=tuple(l for l in DEFAULT_LAYERS if l.key == "hydrants"), cache_dir=tmp_path / "gis", fetch=fetch,
            log=lambda m: None)
    assert g.refresh("hydrants")
    q = next(u for u in fetch.asked if "/query" in u)
    assert "OBJECTID" in q                                                   # asked for: the county gives ids only then
    assert sorted(f["id"] for f in g.data["hydrants"]) == [f["id"] for f in COUNTY]
    assert all("OBJECTID" not in f["properties"] for f in g.data["hydrants"])   # (only the shown fields kept)
    c = make_cad(tmp_path)
    wire(c, g)
    assert c.store.q("SELECT * FROM hydrants") == []                         # the overlay empty: production's default
    feats = served(g)[0]["features"]
    assert len(feats) == len(COUNTY) and all(f["properties"]["_k"] == f"c{f['id']}" for f in feats)


def test_a_copy_without_ids_still_draws_every_hydrant(tmp_path):
    """The hub's cached copy from before (no ids): every hydrant still served (not editable) until it is fetched again."""
    noid = [{k: v for k, v in json.loads(json.dumps(f)).items() if k != "id"} for f in COUNTY]
    c = make_cad(tmp_path)
    g = fake_gis(noid)
    wire(c, g)
    feats = served(g)[0]["features"]
    assert len(feats) == len(COUNTY) and all("_k" not in f["properties"] for f in feats)
    assert c.hydrants.listing({})["counts"]["unkeyed"] == len(COUNTY)
    # the recorded fixture (the county's real shape: no ids) through an empty overlay: every feature
    real = json.loads((Path(__file__).parent / "fixtures" / "wcgis" / "hydrants.geojson").read_text())["features"]
    set_layer(g, "hydrants", real)
    assert len(served(g)[0]["features"]) == len(real) > 0


def test_a_fresh_hub_with_the_cache_from_before_serves_it_and_fetches_again_at_once(tmp_path):
    """A new hub task with the copy #831's hub saved (the old signature, no ids; checks.json saying it was looked at just
    now): every hydrant served from it at once, the layer due at once (not in a month), then fetched whole with ids; the
    Settings list counts every county hydrant with no overlay row."""
    import hashlib
    layer = next(l for l in DEFAULT_LAYERS if l.key == "hydrants")
    clock = [1.8e9]
    fetch = arcgis_like(COUNTY)
    g = Gis(layers=(layer,), cache_dir=tmp_path / "gis", fetch=fetch, log=lambda m: None, load=False, clock=lambda: clock[0])
    old = {"url": layer.url, "bbox": [round(x, 4) for x in g.layer_bbox(layer)], "where": layer.where,
           "fields": [f for f, _ in layer.fields], "simplify_m": layer.simplify_m}
    sig = hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()[:16]
    assert sig == g._signature(layer, ids=False) != g._signature(layer)
    noid = [{k: v for k, v in json.loads(json.dumps(f)).items() if k != "id"} for f in COUNTY]
    (tmp_path / "gis").mkdir()
    (tmp_path / "gis" / "hydrants.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": noid, "sizeup": {"at": clock[0] - 3600, "signature": sig, "how": "full"}}))
    (tmp_path / "gis" / "checks.json").write_text(json.dumps({"layers": {"hydrants": {"checked": clock[0] - 60, "how": "full"}}}))
    g.load_cache()
    c = make_cad(tmp_path)
    wire(c, g)
    assert len(served(g)[0]["features"]) == len(COUNTY)                      # served from the old copy meanwhile
    assert g.due() == ["hydrants"]                                           # and due now, not in a month
    assert fetch.asked == []
    assert g.refresh("hydrants") and g.status["hydrants"]["how"] == "full"
    assert all(f.get("id") for f in g.data["hydrants"])
    lst = c.hydrants.listing({})
    assert lst["counts"]["all"] == lst["total"] == len(COUNTY) and lst["counts"]["edited"] == 0 and lst["counts"]["unkeyed"] == 0
    assert json.loads((tmp_path / "gis" / "hydrants.geojson").read_text())["sizeup"]["signature"] == g._signature(layer)


def test_no_cache_at_all_is_fetched_at_once(tmp_path):
    layer = next(l for l in DEFAULT_LAYERS if l.key == "hydrants")
    g = Gis(layers=(layer,), cache_dir=tmp_path / "gis", fetch=arcgis_like(COUNTY), log=lambda m: None, load=False)
    g.load_cache()
    assert g.status["hydrants"]["at"] is None and g.due() == ["hydrants"]
    c = make_cad(tmp_path)
    wire(c, g)
    assert c.hydrants.listing({})["counts"]["all"] == 0                       # (nothing yet: the catalog says loading)
    assert g.refresh("hydrants") and c.hydrants.listing({})["counts"]["all"] == len(COUNTY)


def test_every_hydrant_is_drawn_with_an_empty_overlay_in_a_browser(tmp_path):
    """#831's regression in the browser: the county's layer as fetched (ids and, from an old copy, none), nothing in the
    overlay: every hydrant rendered on the map, grey (no flow data)."""
    import threading
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    from sizeup.web.server import WebServer
    c = make_cad(tmp_path)
    g = Gis(layers=tuple(l for l in DEFAULT_LAYERS if l.key in ("hydrants", "municipalities")), cache_dir=tmp_path / "gis",
            fetch=arcgis_like(COUNTY[:3]), log=lambda m: None, load=False)
    g.cache_loaded = True
    assert g.refresh("hydrants")
    set_layer(g, "municipalities", TOWNS)
    g.data["hydrants"] += [{k: v for k, v in hyd(990006 + i, f"90{i}", 0.3 + i * 0.3, dlon=0.0006).items() if k != "id"}
                           for i in range(2)]                                # two from an old copy, with no id
    g._boxes["hydrants"] = [geometry_bbox(f["geometry"]) for f in g.data["hydrants"]]
    with g._lock:
        g._changed("hydrants")
    wire(c, g)
    scene = {"timezone": "America/New_York", "home": {"center": [LON0 + 0.0003, LAT0 + 0.0009], "zoom": 17.0},
             "glyphs": "https://tiles.invalid/fonts/{fontstack}/{range}.pbf"}
    ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda iid: False, cad=CadApi(c), gis=g)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001
        pw.stop()
        ws.shutdown()
        pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
    try:
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(base), lambda route: route.abort())
        errors, console = [], []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: console.append(m.text) if m.type == "error" and "Failed to load" not in m.text else None)
        pg.goto(base + "/cad")
        pg.wait_for_function("() => window.GisLayers && GisLayers.catalog() && [...GisLayers.maps].length > 0")
        pg.evaluate("""() => { MapLayers.setLayer('gis:hydrants', true);
            const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            gm.map.jumpTo({ center: cad.scene.home.center, zoom: cad.scene.home.zoom }); }""")
        pg.wait_for_function("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            return gm && gm.map.getLayer('gis-hydrants-pt')
              && gm.map.queryRenderedFeatures({ layers: ['gis-hydrants-pt'] }).length >= 5; }""")
        drawn = pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#ov-map'));
            return gm.map.queryRenderedFeatures({ layers: ['gis-hydrants-pt'] }).map((f) => Hydrants.imageFor(f.properties)); }""")
        assert len(drawn) == 5 and set(drawn) == {"hy-unk"}, drawn
        assert errors == [] and not [m for m in console if "Expected" in m or "expression" in m.lower()], (errors, console)
    finally:
        browser.close()
        pw.stop()
        ws.shutdown()


# -- the flow test's checks (server, CSV, form: the hub refuses what the form does) ---------------------------------------

@pytest.mark.parametrize("body,err", [
    ({"flow_gpm": 0}, "flow_gpm"), ({"flow_gpm": 5001}, "flow_gpm"), ({"flow_gpm": 12000}, "flow_gpm"),
    ({"static_psi": 0}, "static_psi"), ({"static_psi": 301}, "static_psi"), ({"residual_psi": -1}, "residual_psi"),
    ({"flow_gpm": 1000, "static_psi": 60, "residual_psi": 60}, "under the static"),
    ({"flow_gpm": 1000, "static_psi": 60, "residual_psi": 70}, "under the static"),
    ({"flow_gpm": 5000, "static_psi": 100, "residual_psi": 80}, "over 10000"),      # rates ~10,600 at 20 psi
    ({"main_in": 1}, "main_in"), ({"main_in": 30}, "main_in"),
    ({"outlets": "2x2.5, 9x4.5"}, "outlets"), ({"outlets": "1x12"}, "outlets"), ({"outlets": "lots of them"}, "outlets"),
    ({"flow_date": "2099-01-01"}, "not in the future"), ({"inspected": "2099-01-01"}, "not in the future"),
    ({"status_since": "2026-11-10", "status_until": "2026-11-01"}, "status_until"),
])
def test_the_flow_test_checks_refuse(cad, body, err):
    with pytest.raises(hy.CadError, match=err):
        cad.hydrants.edit("c990001", body)
    assert cad.store.q("SELECT * FROM hydrants") == []


def test_the_flow_test_checks_warn(cad):
    h = cad.hydrants
    out = h.edit("c990001", {"flow_gpm": 3000})
    assert out["changed"] == ["flow_gpm"] and any("unusually high" in w for w in out["warnings"])
    out = h.edit("c990002", {"flow_gpm": 1000, "static_psi": 80, "residual_psi": 10})
    assert any("under 20 psi" in w for w in out["warnings"]) and out["hydrant"]["gpm"] == hy.rated_gpm(1000, 80, 10)
    out = h.edit("c990003", {"flow_gpm": 900, "static_psi": 70})
    assert any("One pressure" in w for w in out["warnings"]) and out["hydrant"]["gpm"] == 900 and out["hydrant"]["cls"] == "B"
    assert h.edit("c990004", {"outlets": "2 x 2½\", 1x4.5 steamer", "main_in": 8, "flow_date": cad.hydrants._today()})["changed"]
    # a later edit is checked against what is saved: residual over the saved static
    with pytest.raises(hy.CadError, match="under the static"):
        h.edit("c990002", {"residual_psi": 85})


def test_the_flow_test_checks_on_csv_import(cad):
    text = ("key,flow_gpm,static_psi,residual_psi,main_in,flow_date\n"
            "c990001,12000,80,10,,\n"                                       # over the hard limit
            "c990002,1000,60,65,,\n"                                         # residual over static
            "c990003,3000,,,8,\n"                                            # high: a warning, allowed
            "c990004,,,,40,\n"                                               # main too big
            "c990005,800,,,,2099-12-31\n")                                   # a test in the future
    pv = cad.hydrants.import_csv({"action": "preview", "csv": text})
    assert {e["line"] for e in pv["errors"]} == {2, 3, 5, 6}
    assert [w["line"] for w in pv["warnings"]] == [4] and "unusually high" in pv["warnings"][0]["warning"]


# -- Location: the "@" places (text + references), never the hydrant's point ------------------------------------------------

def test_the_location_keeps_its_places_and_never_moves_the_hydrant(cad):
    h = cad.hydrants
    before = h.index()["by_key"]["c990001"]
    far = {"label": "Sample Market", "lat": LAT0 + 0.004, "lon": LON0 + 0.004, "kind": "place", "sub": "Sampletown"}
    out = h.edit("c990001", {"location": "IFO @Sample Market", "location_places": [far]})
    hh = out["hydrant"]
    assert hh["location"] == "IFO @Sample Market" and hh["location_places"][0]["label"] == "Sample Market"
    assert (hh["lat"], hh["lon"]) == (before["lat"], before["lon"]) and hh["nudge_m"] is None      # never moved
    assert by_key(served(cad.gis)[0])["c990001"]["geometry"]["coordinates"] == [LON0, LAT0]
    # a place no longer named in the text goes; one never named is not kept
    out = h.edit("c990001", {"location": "Rear of the lot"})
    assert out["hydrant"]["location_places"] == []
    out = h.edit("c990001", {"location": "near the gate", "location_places": [far]})
    assert out["hydrant"]["location_places"] == []
    with pytest.raises(hy.CadError, match="places"):
        h.edit("c990001", {"location": "@X", "location_places": [{"label": "X"}]})
    # the CSV has the text only; an import keeps the places still named in it
    h.edit("c990002", {"location": "across from @Sample Market", "location_places": [far]})
    r = next(r for r in rows_of(h.csv({})[0]) if r["key"] == "c990002")
    assert r["location"] == "across from @Sample Market" and "location_places" not in r
    h.import_csv({"action": "apply", "csv": "key,location\nc990002,rear of @Sample Market\n"})
    assert h.one("c990002")["hydrant"]["location_places"][0]["label"] == "Sample Market"
    assert h.index()["by_key"]["c990002"]["lat"] == pytest.approx(LAT0 + DLAT)


def test_a_table_from_831_gets_the_new_column(tmp_path):
    import sqlite3
    c = make_cad(tmp_path)
    db = sqlite3.connect(str(tmp_path / "cad.sqlite"))
    db.execute("ALTER TABLE hydrants DROP COLUMN location_places")             # the table as #831 made it
    db.commit()
    db.close()
    c2 = make_cad(tmp_path)
    wire(c2, fake_gis())
    assert c2.hydrants.edit("c990001", {"location": "IFO @A", "location_places": [{"label": "A", "lat": 41.0, "lon": -73.66}]})["changed"]


def saved_raw(c, key, **cols):
    """Data saved before today's rules (the previous release allowed it): written straight into the table."""
    c.hydrants.edit(key, {"notes": "x"})
    c.store.db.execute(f"UPDATE hydrants SET {', '.join(f'{k} = ?' for k in cols)} WHERE key = ?", (*cols.values(), key))
    c.store.db.commit()
    c.hydrants._bump()


def test_data_saved_before_the_rules_never_blocks_another_edit(cad):
    h = cad.hydrants
    saved_raw(cad, "c990001", flow_gpm=1000, static_psi=60, residual_psi=60, flow_date="2099-06-30", main_in=30.0)
    out = h.edit("c990001", {"status": "oos", "status_reason": "Broken stem"})           # marking it OOS always works
    assert out["changed"] == ["status", "status_reason"] and any(w.startswith("Saved data: residual") for w in out["warnings"])
    with pytest.raises(hy.CadError, match="under the static"):                             # touching the pressures: checked
        h.edit("c990001", {"residual_psi": 61})
    assert h.edit("c990001", {"residual_psi": 40})["changed"] == ["residual_psi"]
    # the CSV: a row with an unrelated column, and the whole export read back (main 30 in, from before the 2-24 rule)
    pv = h.import_csv({"action": "preview", "csv": "key,notes\nc990001,Checked\n"})
    assert pv["error_count"] == 0 and pv["changed"] == 1
    pv = h.import_csv({"action": "preview", "csv": h.csv({})[0].decode("utf-8-sig")})
    assert pv["error_count"] == 0 and pv["changed"] == 0
    pv = h.import_csv({"action": "preview", "csv": "key,main_in\nc990001,31\n"})            # a new out-of-range value: refused
    assert pv["error_count"] == 1


@pytest.mark.parametrize("text", ["2X2.5", "2x2 1/2, 1x4 1/2", "2-2.5", "2×2½, 1x4.5 steamer", "1x5\" Storz", "4.5"])
def test_outlets_as_people_write_them(cad, text):
    assert cad.hydrants.edit("c990001", {"outlets": text})["hydrant"]["outlets"]


def test_clearing_one_pressure_classes_from_the_observed_flow(cad):
    h = cad.hydrants
    h.edit("c990001", {"flow_gpm": 1000, "static_psi": 80, "residual_psi": 60})
    assert h.one("c990001")["hydrant"]["cls"] == "AA"                                     # rated at 20 psi
    out = h.edit("c990001", {"residual_psi": ""})
    assert out["hydrant"]["gpm"] == 1000 and out["hydrant"]["cls"] == "A" and any("One pressure" in w for w in out["warnings"])
    assert not any("under 20" in w for w in h.edit("c990002", {"flow_gpm": 500, "static_psi": 18, "residual_psi": 10})["warnings"])
