"""The county's common place names (sizeup/cad/commonplaces.py): the spreadsheet read (.xlsx with the standard library,
.csv), its columns, each address looked up in the gazetteer's address points, the import (preview, apply, re-import,
remove; admins; audited as counts), and the names used wherever a place is said or typed: the place search and the
location box, a page, a radio pre-alert, the county feed's location.

Invented data only (this repository is public): a synthetic gazetteer of invented streets (Example Ave, Sample St) and
invented place names. The real list is uploaded through the app and stays in the CAD database."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import zipfile

import pytest

from sizeup.cad import commonplaces as cp
from sizeup.cad.api import CadApi
from sizeup.cad.store import CadError
from sizeup.cloud.access import Identity
from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.geo.pagegeo import PageLocator, locate_page, set_local_source
from sizeup.geo.places import PlaceIndex
from sizeup.intel import place as P
from tests.test_cad import make_cad
from tests.test_roles import CASEY, VAL, people
from tests.test_settings import post

PC, RB = "Port Chester", "Rye Brook"
LAT = 41.0
SAMPLE, TEST = -73.685, -73.675
HEADER = ["Address", "Common Place", "Mun"]
ROWS = [["120 EXAMPLE AVE", "SAMPLE MARINA", "PORT_CHESTER"],
        ["120 EXAMPLE AVE", "SAMPLE BOAT CLUB", "PORT_CHESTER"],        # two names, one address
        ["21 SAMPLE ST", "COMMUNITY HALL - PORT CHESTER", "PORT_CHESTER"],
        ["21 SAMPLE ST", "PORT CHESTER COMMUNITY HALL", "PORT_CHESTER"],
        ["122 EXAMPLE AVENUE", "TESTVILLE DINER", "PORT_CHESTER"],     # interpolated between 120 and 124
        ["7 NOWHERE RD", "QUIET GROVE", "PORT_CHESTER"]]              # no such street: kept, flagged


def synthetic(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(1, "example ave", "Example Ave"), (2, "sample st", "Sample St"),
                                                           (3, "test rd", "Test Rd"), (5, "example ave ext", "Example Ave Ext")])
    addrs = [(1, n, "", PC, LAT + 0.0002, SAMPLE + (TEST - SAMPLE) * k / 25, 1) for k, n in enumerate(range(100, 200, 4))]
    addrs += [(2, n, "", PC, LAT + 0.0001 + k * 0.0002, SAMPLE + 0.0002, 1) for k, n in enumerate(range(1, 60, 2))]
    addrs += [(5, 3, "", RB, 41.02, -73.69, 1)]
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)", addrs)
    lines = [(1, PC, [[-73.690, LAT], [SAMPLE, LAT], [TEST, LAT], [-73.670, LAT]]),
             (2, PC, [[SAMPLE, LAT - 0.005], [SAMPLE, LAT], [SAMPLE, LAT + 0.005]]),
             (3, PC, [[TEST, LAT - 0.005], [TEST, LAT], [TEST, LAT + 0.005]])]
    db.executemany("INSERT INTO lines VALUES (?,?,?)", [(s, m, json.dumps(c)) for s, m, c in lines])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", [(1, 2, LAT, SAMPLE, PC), (1, 3, LAT, TEST, PC)])
    db.execute("INSERT INTO landmarks VALUES (1, 'Example Park', 'park', '', ?, 41.002, -73.68, 'test', 'example park')",
               (PC,))
    db.commit()
    db.close()
    return path


@pytest.fixture()
def gaz(tmp_path):
    return Gazetteer(synthetic(tmp_path / "synthetic.sqlite"))


@pytest.fixture()
def cad(tmp_path, gaz):
    c = make_cad(tmp_path, geocoder=lambda q: [])
    c.place_source = lambda: PlaceIndex(gaz)
    return c


def xlsx(rows, shared=True) -> bytes:
    """A minimal .xlsx as a spreadsheet program writes it: shared strings (one cell as rich text), a number cell."""
    strings: list[str] = []
    cells_xml = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{'ABCDEFGHIJ'[c]}{r}"
            if v == "":
                continue
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            elif shared:
                strings.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>')
        cells_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

    def si(s: str) -> str:
        if " - " in s:                       # rich text: two runs
            a, b = s.split(" - ", 1)
            return f'<si><r><rPr><b/></rPr><t xml:space="preserve">{a} - </t></r><r><t>{b}</t></r></si>'
        return f"<si><t>{s}</t></si>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets><sheet name="Places" sheetId="1" r:id="rId1"/>'
                                      "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="worksheet" Target="worksheets/data.xml"/></Relationships>')
        z.writestr("xl/worksheets/data.xml", f'<worksheet {ns}><sheetData>{"".join(cells_xml)}</sheetData></worksheet>')
        if shared:
            z.writestr("xl/sharedStrings.xml", f'<sst {ns}>{"".join(si(s) for s in strings)}</sst>')
    return buf.getvalue()


def b64(data: bytes) -> str:
    return "data:application/octet-stream;base64," + base64.b64encode(data).decode()


def imported(cad):
    return {r["key"]: r for r in cad.local_places() if cp.is_imported(r["key"], r)}


# --------------------------------------------------------------------------- the file

@pytest.mark.parametrize("shared", [True, False])
def test_an_xlsx_is_read_with_the_standard_library(shared):
    rows = cp.read_table(xlsx([HEADER, *ROWS, ["", "", ""], [12, "SAMPLE PIER", "RYE"]], shared), "list.xlsx")
    assert rows[0] == HEADER and rows[1] == ROWS[0] and rows[2] == ROWS[1]       # rich text joined
    assert rows[-1] == ["12", "SAMPLE PIER", "RYE"] and len(rows) == len(ROWS) + 2  # a number cell; the empty row dropped


def test_a_csv_and_its_columns_any_case():
    text = "town,COMMON PLACE,address\nPORT_CHESTER,Sample Marina,120 Example Ave\n"
    rows = cp.read_table(text.encode("utf-8-sig"), "list.csv")
    assert cp.find_columns(rows[0]) == {"address": 2, "name": 1, "town": 0}
    assert cp.find_columns(["Addr", "Place Name"]) == {"address": 0, "name": 1, "town": -1}
    assert cp.find_columns(["a", "b", "c"], {"address": 2, "name": 0, "town": -1}) == {"address": 2, "name": 0, "town": -1}
    for header, given in ((["a", "b"], None), (["Address", "x"], None), (HEADER, {"name": 0}), (HEADER, {"zip": 1}),
                          (HEADER, {"town": 9})):
        with pytest.raises(cp.TableError):
            cp.find_columns(header, given)
    for bad in (b"", b"PK\x03\x04 not a zip", b"\x00\x01\x02"):
        with pytest.raises(cp.TableError):
            cp.read_table(bad, "list.xlsx")


def test_the_town_code_is_the_cads_town():
    assert cp.town_code("PORT_CHESTER") == "port chester" and cp.town_code(" Rye ") == "rye"
    assert cp.name_key("COMMUNITY HALL - PORT CHESTER") == "community hall port chester"
    assert cp.entry_key("Sample Marina", "120 EXAMPLE AVENUE") == "county/sample marina @ 120 example ave"


def test_an_address_is_found_in_the_address_points(gaz):
    ix = PlaceIndex(gaz)
    hit = cp.locate_address(ix, "120 EXAMPLE AVE", PC)
    assert (hit["label"], hit["muni"], hit["precision"]) == ("120 Example Ave", PC, "exact")
    assert cp.locate_address(ix, "120 Example Avenue", "")["precision"] == "exact"          # the same normaliser
    assert cp.locate_address(ix, "122 EXAMPLE AVE", PC)["precision"] == "interpolated"
    assert cp.locate_address(ix, "120-124 EXAMPLE AVE", PC)["label"] == "120 Example Ave"   # the first number
    assert cp.locate_address(ix, "120 EXAMPLE", PC) is None            # never a prefix of a street ("Example Ave Ext")
    assert cp.locate_address(ix, "7 NOWHERE RD", PC) is None
    assert cp.locate_address(ix, "SAMPLE MARINA", PC) is None          # no number
    assert cp.locate_address(ix, "3 EXAMPLE AVE EXT", "")["muni"] == RB    # no town given, one town has it: that town
    assert cp.locate_address(ix, "3 EXAMPLE AVE EXT", PC) is None         # another town's, ~2.4 km off: not this row's


TOWNS = {"port chester": PC, "rye brook": RB, "rye": "Rye"}


def town_of(words: str) -> str:
    return TOWNS.get(words.lower(), words)


@pytest.fixture()
def towns_gaz(tmp_path):
    """The synthetic gazetteer plus: Quill Ln, filed under the neighbouring village but ~300 m from Port Chester's points
    (one ZIP code, its points right); Test Ct, in Port Chester without number 15 (a missing point) and with a 15 in Rye,
    ~8 km away."""
    path = synthetic(tmp_path / "towns.sqlite")
    db = sqlite3.connect(path)
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(6, "quill ln", "Quill Ln"), (7, "test ct", "Test Ct")])
    addrs = [(6, n, "", RB, LAT + 0.003 + k * 0.0002, SAMPLE - 0.004, 1) for k, n in enumerate(range(1, 20, 2))]
    addrs += [(7, n, "", PC, LAT - 0.002, SAMPLE + k * 0.0002, 1) for k, n in enumerate((2, 4, 6))]
    addrs += [(7, n, "", "Rye", LAT + 0.07 + k * 0.0002, SAMPLE, 1) for k, n in enumerate((11, 13, 15, 17))]
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)", addrs)
    db.commit()
    db.close()
    return Gazetteer(path)


def test_another_towns_point_only_when_it_is_the_same_place(towns_gaz):
    ix = PlaceIndex(towns_gaz)
    hit = cp.locate_address(ix, "7 QUILL LN", PC, town_of)        # filed under the neighbour, next to Port Chester
    assert (hit["label"], hit["muni"], hit["precision"]) == ("7 Quill Ln", PC, "exact")
    assert cp.locate_address(ix, "7 QUILL LN", RB, town_of)["muni"] == RB
    assert cp.locate_address(ix, "15 TEST CT", PC, town_of) is None     # a street of that name ~8 km away: never
    assert cp.locate_address(ix, "15 TEST CT", "Rye", town_of)["muni"] == "Rye"
    assert cp.locate_address(ix, "15 TEST CT", "", town_of)["muni"] == "Rye"    # no town given: the one town that has it
    assert cp.locate_address(ix, "7 QUILL LN", "Elsewhere", town_of) is None     # a town the gazetteer has no point of


def test_the_entrys_town_is_the_rows_never_the_gazetteers(towns_gaz):
    ix = PlaceIndex(towns_gaz)
    rows = [HEADER, ["7 QUILL LN", "SAMPLE LIBRARY", "PORT_CHESTER"], ["15 TEST CT", "SAMPLE ROW HOUSES", "PORT_CHESTER"],
            ["120 EXAMPLE AVE", "SAMPLE MARINA", ""]]
    p = cp.plan(rows, {"address": 0, "name": 1, "town": 2}, lambda a, t: cp.locate_address(ix, a, t, town_of), town_of,
                "2026-01-01", {})
    e = p["entries"]
    lib, houses, marina = (e["county/sample library @ 7 quill ln"], e["county/sample row houses @ 15 test ct"],
                           e["county/sample marina @ 120 example ave"])
    assert (lib["muni"], lib["located"], lib["lat"] is not None) == (PC, "exact", True)
    assert (houses["muni"], houses["located"], houses["lat"]) == (PC, "", None)        # unresolved, still its town
    assert p["unresolved"] == [{"name": "SAMPLE ROW HOUSES", "address": "15 TEST CT", "town": PC}]
    assert marina["muni"] == PC                                                        # no town column value: the hit's
    look = cp.Lookup(list(e.values()))
    assert look.find("SAMPLE LIBRARY", town="port chester")["address"] == "7 QUILL LN"     # the feed's town: found
    assert look.find("SAMPLE LIBRARY", town="rye brook") is None


@pytest.mark.parametrize("address, label, whole", [("23.5 SAMPLE ST", "23.5 Sample St", "23 SAMPLE ST"),
                                                   ("55-1/2 SAMPLE ST", "55-1/2 Sample St", "55 SAMPLE ST"),
                                                   ("55 1/2 SAMPLE ST", "55 1/2 Sample St", "55 SAMPLE ST"),
                                                   ("189-1/2 EXAMPLE AVE", "189-1/2 Example Ave", "189 EXAMPLE AVE")])
def test_a_fractional_house_number_is_found_by_its_whole_number(gaz, address, label, whole):
    ix = PlaceIndex(gaz)
    hit = cp.locate_address(ix, address, PC)
    whole = cp.locate_address(ix, whole, PC)
    assert (hit["label"], hit["precision"], hit["muni"]) == (label, "interpolated", PC)    # approximate, said as typed
    assert (hit["lat"], hit["lon"]) == (whole["lat"], whole["lon"])
    rows = [HEADER, [address, "SAMPLE COTTAGE", "PORT_CHESTER"]]
    p = cp.plan(rows, {"address": 0, "name": 1, "town": 2}, lambda a, t: cp.locate_address(ix, a, t), town_of,
                "2026-01-01", {})
    [entry] = p["entries"].values()
    assert (entry["address"], entry["at"], entry["located"]) == (address, label, "interpolated")   # the file's text kept
    assert p["counts"]["approximate"] == 1 and p["counts"]["unresolved"] == 0


# --------------------------------------------------------------------------- the import

def test_preview_counts_and_writes_nothing(cad):
    before = len(cad.settings.audit_log())
    r = post(cad, "common_places", action="preview", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    assert r["dry_run"] and r["columns"] == {"address": 0, "name": 1, "town": 2} and r["header"] == HEADER
    assert r["counts"] == {"rows": 6, "names": 6, "addresses": 4, "new": 6, "present": 0, "removed": 0, "unresolved": 1,
                           "unresolved_addresses": 1, "approximate": 1, "skipped": 0}
    assert r["unresolved"] == [{"name": "QUIET GROVE", "address": "7 NOWHERE RD", "town": PC}]
    assert imported(cad) == {} and len(cad.settings.audit_log()) == before


def test_apply_marks_the_source_and_audits_counts_only(cad):
    post(cad, "places", action="set", name="the Waterfront", place={"label": "Example Park", "lat": 41.002, "lon": -73.68})
    post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    got = imported(cad)
    assert len(got) == 6
    e = got["county/sample marina @ 120 example ave"]
    assert (e["name"], e["label"], e["address"], e["at"], e["muni"], e["source"], e["located"]) == \
        ("SAMPLE MARINA", "Sample Marina", "120 EXAMPLE AVE", "120 Example Ave", PC, cp.SOURCE, "exact")
    from datetime import datetime
    from sizeup.cad.store import TZ
    assert e["imported"] == datetime.fromtimestamp(cad.store.clock(), TZ).strftime("%Y-%m-%d")
    lost = got["county/quiet grove @ 7 nowhere rd"]
    assert lost["lat"] is None and lost["located"] == "" and lost["address"] == "7 NOWHERE RD"   # kept, flagged
    [a] = [x for x in cad.settings.audit_log() if "place names" in x["key"]]
    assert (a["section"], a["key"]) == ("lists", f"place names/{cp.SOURCE}")
    assert json.loads(a["new"]) == {"rows": 6, "names": 6, "new": 6, "present": 0, "removed": 0, "unresolved": 1}
    said = json.dumps([x for x in cad.settings.audit_log() if "Waterfront" not in json.dumps(x)]).upper()
    assert not any(w in said for w in ("MARINA", "EXAMPLE", "NOWHERE", "COMMUNITY", "DINER", "GROVE"))   # counts only
    page = cad.settings_get()["place_names"]
    assert [x["name"] for x in page["items"]] == ["the Waterfront"]            # the hand-made list alone
    assert {k: page["imported"][k] for k in ("names", "addresses", "located")} == {"names": 6, "addresses": 4, "located": 5}
    assert page["imported"]["unresolved"] == [{"name": "QUIET GROVE", "address": "7 NOWHERE RD", "town": PC}]


def test_a_re_import_replaces_the_source_and_keeps_hand_made_names(cad):
    post(cad, "places", action="set", name="the Waterfront", place={"label": "Example Park", "lat": 41.002, "lon": -73.68})
    post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    again = [HEADER, ROWS[0], ["40 SAMPLE ST", "SAMPLE HALL", "PORT_CHESTER"]]
    r = post(cad, "common_places", action="preview", file=b64(xlsx(again)), filename="list.xlsx")
    assert (r["counts"]["new"], r["counts"]["present"], r["counts"]["removed"]) == (1, 1, 5)
    post(cad, "common_places", action="apply", file=b64(xlsx(again)), filename="list.xlsx")
    assert sorted(imported(cad)) == ["county/sample hall @ 40 sample st", "county/sample marina @ 120 example ave"]
    assert [r["name"] for r in cad.local_places() if not cp.is_imported(r["key"], r)] == ["the Waterfront"]
    post(cad, "common_places", action="remove")
    assert imported(cad) == {} and len(cad.local_places()) == 1
    with pytest.raises(CadError, match="no imported"):
        post(cad, "common_places", action="remove")


def test_the_hand_made_limit_ignores_the_imported_names(cad, monkeypatch):
    from sizeup.cad import settings as settings_mod
    monkeypatch.setattr(settings_mod, "MAX_PLACE_NAMES", 1)
    post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    post(cad, "places", action="set", name="the Waterfront", place={"label": "Example Park", "lat": 41.002, "lon": -73.68})
    with pytest.raises(CadError, match="at most 1"):
        post(cad, "places", action="set", name="the Green", place={"label": "Example Park", "lat": 41.002, "lon": -73.68})


def test_columns_mapped_by_hand_and_bad_files(cad):
    rows = [["Where", "Called", "Code"], ["120 EXAMPLE AVE", "SAMPLE MARINA", "PORT_CHESTER"]]
    with pytest.raises(CadError, match="no address column"):
        post(cad, "common_places", action="preview", file=b64(xlsx(rows)), filename="list.xlsx")
    r = post(cad, "common_places", action="preview", file=b64(xlsx(rows)), filename="list.xlsx",
             columns={"address": 0, "name": 1, "town": 2})
    assert r["counts"]["names"] == 1 and r["counts"]["unresolved"] == 0
    for body, why in (({"file": ""}, "file"), ({"file": "@@@"}, "base64"), ({"file": b64(b"PK nope")}, "not an .xlsx"),
                      ({"file": b64(xlsx([HEADER]))}, "no rows"), ({"action": "nope", "file": "x"}, "action")):
        with pytest.raises(CadError, match=why):
            post(cad, "common_places", **{"action": "preview", "filename": "list.xlsx", **body})


def test_no_gazetteer_no_import(tmp_path):
    c = make_cad(tmp_path, geocoder=lambda q: [])
    with pytest.raises(CadError, match="gazetteer"):
        post(c, "common_places", action="preview", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")


def test_only_admins_import(cad):
    people(cad)
    body = json.dumps({"action": "preview", "file": b64(xlsx([HEADER, *ROWS])), "filename": "list.xlsx"}).encode()
    code, raw, _ = CadApi(cad).post("/api/cad/settings/common_places", body, Identity(email=VAL))
    assert code == 403 and "Admins only" in json.loads(raw)["error"]
    code, raw, _ = CadApi(cad).post("/api/cad/settings/common_places", body, Identity(email=CASEY))
    assert code == 200 and json.loads(raw)["counts"]["names"] == 6


def test_the_server_takes_a_spreadsheet_bigger_than_a_json_post(cad):
    import threading
    import urllib.error
    import urllib.request
    from sizeup.web import server
    assert server.MAX_COMMON_PLACES_BODY >= cp.MAX_BYTES * 4 // 3 > server.MAX_CAD_BODY
    ws = server.WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                          lambda iid: False, cad=CadApi(cad))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    text = "Address,Common Place,Mun\n" + "".join(f"{n} NOWHERE RD,SAMPLE SPOT {n},PORT_CHESTER\n" for n in range(2000))
    body = json.dumps({"action": "preview", "file": b64(text.encode()), "filename": "list.csv", "operator": "JD"}).encode()
    assert len(body) > server.MAX_CAD_BODY

    def send(path):
        req = urllib.request.Request(base + path, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        code, out = send(server.COMMON_PLACES_PATH)
        assert code == 200 and out["counts"]["names"] == 2000 and out["counts"]["unresolved"] == 2000
        assert send("/api/cad/settings/places")[0] == 400            # every other CAD post: 64 KB as before
    finally:
        ws.shutdown()


# --------------------------------------------------------------------------- (a) the place search, the location box

def test_imported_names_in_the_place_search(cad):
    post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    code, r = CadApi(cad).get("/api/cad/places", "q=sample+marina", None)[:2]
    first = json.loads(r)["places"][0]
    assert (first["label"], first["source"], first["sub"], first["detail"]) == ("Sample Marina", "local", PC,
                                                                                  "120 EXAMPLE AVE")
    hits = json.loads(CadApi(cad).get("/api/cad/places", "q=sample", None)[1])["places"]
    assert {"Sample Marina", "Sample Boat Club"} <= {h["label"] for h in hits}      # one address, both names
    assert json.loads(CadApi(cad).get("/api/cad/places", "q=quiet+grove", None)[1])["places"] == []   # no point: never offered
    # a public landmark typed exactly stays first; an imported name typed exactly is first
    assert json.loads(CadApi(cad).get("/api/cad/places", "q=example+park", None)[1])["places"][0]["label"] == "Example Park"
    assert json.loads(CadApi(cad).get("/api/cad/places", "q=testville+diner", None)[1])["places"][0]["label"] == \
        "Testville Diner"


def test_the_location_box_fills_the_place_and_its_address(cad):
    post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    m = cad.geocode("sample marina")["matches"][0]
    assert (m["label"], m["place"], m["place_address"]) == ("Sample Marina", "Sample Marina", "120 Example Ave")


# --------------------------------------------------------------------------- (b) a page

FAR = (41.03, -73.66)            # ~3 km from Example Ave: where a listed name would wrongly move a call
ENTRIES = [{"name": "QUILLBERRY MARINA", "label": "Quillberry Marina", "lat": LAT + 0.0002, "lon": -73.6834, "muni": PC},
           {"name": "COMMUNITY HALL - PORT CHESTER", "label": "Community Hall - Port Chester", "lat": LAT + 0.0041,
            "lon": SAMPLE + 0.0002, "muni": PC},
           {"name": "QUILLMART", "lat": FAR[0], "lon": FAR[1], "muni": PC},
           {"name": "GRAND APARTMENTS", "lat": FAR[0], "lon": FAR[1], "muni": PC},
           {"name": "BLUE WATER TOWER", "lat": FAR[0], "lon": FAR[1], "muni": PC},
           {"name": "SUNNY DINER", "lat": FAR[0], "lon": FAR[1], "muni": PC},
           {"name": "PORT CHESTER BUS DEPOT", "lat": FAR[0], "lon": FAR[1], "muni": PC},
           {"name": "RYE BUS DEPOT", "lat": 40.98, "lon": -73.69, "muni": "Rye"},
           {"name": "QUILLBERRY YACHT CLUB", "label": "Quillberry Yacht Club", "lat": FAR[0], "lon": FAR[1], "muni": PC},
           {"name": "QUILLBERRY YACHT CLUB", "label": "Quillberry Yacht Club", "lat": 40.97, "lon": -73.68, "muni": "Rye"},
           {"name": "QUIET GROVE", "lat": None, "lon": None, "muni": PC}]
OURS = ["Port Chester FD"]


gaz_now: list = []                # the gazetteer the named fixture set the names on


def label(text):
    """Where a page of ours saying text is placed (its best reading), or None."""
    g = locate_page(gaz_now[0], text, OURS)
    return g.best.match.label if g.best else None


@pytest.fixture()
def named(gaz):
    set_local_source(gaz, lambda: ENTRIES)
    gaz_now[:] = [gaz]
    yield gaz
    gaz_now.clear()


def test_a_page_reads_the_departments_names(named):
    g = locate_page(named, "Port Chester Fire, respond to the Quillberry Marina for a boat fire", OURS)
    assert g.best.match.label == "Quillberry Marina, Port Chester" and g.best.how == "named place" and g.confirmed
    for said in ("respond to the Port Chester community hall for an alarm",
                 "the community hall in Port Chester, fire alarm", "respond to community hall port chester"):
        assert label(said) == "Community Hall - Port Chester, Port Chester", said
    assert label("respond to the community hall for an alarm") is None          # generic words: its town said
    assert label("respond to the quiet grove") is None                          # no point
    assert label("respond to the Quillberrys Marina") == "Quillberry Marina, Port Chester"     # speech-to-text


def test_a_house_number_said_always_beats_a_listed_name(named):
    """The review's sentences (invented): the address said is where the call is; a name near it only backs it."""
    for said in ("respond to 120 Example Ave for a smoke condition, caller is at the Quillberry Marina across the street",
                 "water leak at the blue water tower office, 120 Example Ave",
                 "respond to the grand apartments, 120 Example Ave"):
        g = locate_page(named, said, OURS)
        assert g.best.match.label.startswith("120 Example Ave"), said
    g = locate_page(named, "respond to 120 Example Ave between Sample St and Test Rd", OURS)
    h = locate_page(named, "respond to the Quillberry Marina, 120 Example Ave between Sample St and Test Rd", OURS)
    assert g.confirmed and h.confirmed and h.best.match.label == g.best.match.label
    assert h.best.score >= g.best.score and h.best.place == "Quillberry Marina"       # backed, never moved


def test_common_words_and_one_word_names_need_their_town(named):
    assert label("accident at the Quillmart vehicle on Test Rd") != "QUILLMART, Port Chester"
    assert label("respond to the Quillmart for a smell of smoke") is None       # "to the" is no cue
    assert label("respond to the Quillmart in Port Chester") == "QUILLMART, Port Chester"
    assert label("respond to the grand apartments for an odor") is None          # common English words only
    assert label("the blue water tower, report of smoke") is None
    assert label("grand apartments in Port Chester, alarm") == "GRAND APARTMENTS, Port Chester"


def test_a_town_said_picks_that_towns_place_and_never_another(named):
    assert label("Quillberry Yacht Club, Rye, boat fire") == "Quillberry Yacht Club, Rye"
    assert label("Quillberry Yacht Club in Port Chester, boat fire") == "Quillberry Yacht Club, Port Chester"
    assert label("respond to the Quillberry Marina, Rye") is None                 # listed in Port Chester only


def test_no_names_pages_as_before(gaz):
    assert locate_page(gaz, "respond to the Quillberry Marina", OURS).best is None
    set_local_source(gaz, lambda: 1 / 0)                             # a failing source: as before, never an error
    assert locate_page(gaz, "respond to the Quillberry Marina", OURS).best is None
    set_local_source(gaz, lambda: ENTRIES)
    loc = PageLocator(gaz)
    assert loc.locals() is loc.locals()                               # built once while the names stay
    assert loc.local_said("units to the quillberry marina") and not loc.local_said("units to the corner")


# --------------------------------------------------------------------------- (d) a radio pre-alert

SCOPE = {"system": "mrrs", "service": "police", "pack": "police", "towns": [PC, "Rye", RB]}


def test_a_radio_pre_alert_reads_the_departments_names(gaz):
    assert P.Locator(gaz)("units to the Quillberry Marina, report of a fight", SCOPE) is None   # no cue, no names
    set_local_source(gaz, lambda: ENTRIES)
    p = P.Locator(gaz)("units to the Quillberry Marina, report of a fight", SCOPE)
    assert p is not None and p.precision == "landmark" and p.label.startswith("Quillberry Marina") and p.town == PC


def test_routine_radio_traffic_gets_no_place(named):
    for said in ("out of service at the diner", "returning to quarters from the bus depot",
                 "back in service, quillmart", "grand apartments all quiet"):
        assert P.Locator(named)(said, SCOPE) is None, said


# --------------------------------------------------------------------------- (c) the county feed

def test_a_feed_location_that_is_a_common_name_maps_to_its_address(tmp_path):
    from tests.test_incident_sources import Rig, T0
    rig = Rig(tmp_path)
    entry = {"name": "SAMPLE MARINA", "aliases": [], "label": "Sample Marina", "lat": 41.0002, "lon": -73.6834,
             "muni": PC, "note": "", "address": "120 EXAMPLE AVE", "at": "120 Example Ave", "source": cp.SOURCE,
             "imported": "2026-01-01", "located": "exact"}
    rig.cad.settings._commit([("places", "county/sample marina @ 120 example ave", entry)], "test", [])
    # a person's call at the address first: the feed's event naming the place attaches to it
    made = rig.cad.create({"type": "STRU", "address": "120 Example Ave", "operator": "JD"})
    rig.feed.put("A1", type="STRU", location="@SAMPLE MARINA", units=("E60",), times={"dispatched": T0})
    rig.feed.put("B1", type="EMS", location="SAMPLE MARINA PORT_CHESTER", units=("E62",), times={"dispatched": T0 + 3600})
    rig.at(0)
    rig.poll()
    reports = {r["ref"]: r["incident_id"] for r in rig.cad.store.q("SELECT ref, incident_id FROM source_reports")}
    assert reports["A1"] == made["id"]                    # matched at its address
    a = rig.cad.store.incident(made["id"])
    assert a["address"] == "120 Example Ave"              # a person's location stands
    others = [i for i in rig.incidents() if i["id"] != made["id"]]
    assert [i["id"] for i in others] == [reports["B1"]]
    for i in others:
        assert (i["address"], i["place"], i["place_address"]) == ("120 Example Ave @ Sample Marina", "Sample Marina",
                                                                  "120 Example Ave")
        assert i["address_raw"] == "SAMPLE MARINA PORT_CHESTER"                             # the feed's own text kept
        assert (i["lat"], i["lon"], i["geo_label"]) == (41.0002, -73.6834, "120 Example Ave, Port Chester")
    # an address, or a name nobody listed: as before
    assert rig.cad.sources.common_place("120 EXAMPLE AVE") is None
    assert rig.cad.sources.common_place("@OTHER PLACE") is None
    assert rig.cad.sources.common_place("@SAMPLE MARINA - PORT_CHESTER")["address"] == "120 EXAMPLE AVE"
    assert rig.cad.sources.common_place("SAMPLE MARINA, PORT_CHESTER")["address"] == "120 EXAMPLE AVE"
    # only a location that is nothing but the name: an intersection or address before the "@" stays as it is
    assert rig.cad.sources.common_place("EXAMPLE AVE/SAMPLE ST @SAMPLE MARINA") is None
    assert rig.cad.sources.common_place("EXAMPLE AVE @SAMPLE MARINA") is None
    # never another town's: a town in the text, or the feed's own town field, must be the name's
    assert rig.cad.sources.common_place("SAMPLE MARINA, RYE_BROOK") is None
    assert rig.cad.sources.common_place("SAMPLE MARINA RYE_BROOK") is None
    assert rig.cad.sources.common_place("@SAMPLE MARINA", {"extra": {"town": "RYE_BROOK"}}) is None
    assert rig.cad.sources.common_place("@SAMPLE MARINA", {"extra": {"town": "PORT_CHESTER"}}) is not None
    assert rig.cad.sources.common_place("@SAMPLE MARINA", {"extra": {"town": "SOMEWHERE_ELSE"}}) is not None


def test_one_name_at_two_addresses_is_no_place():
    look = cp.Lookup([{"name": "SAMPLE DELI", "address": "1 EXAMPLE AVE", "lat": 1.0},
                      {"name": "SAMPLE DELI", "address": "9 SAMPLE ST", "lat": 2.0},
                      {"name": "SAMPLE PIER", "address": "3 SAMPLE ST", "lat": None},
                      {"name": "NOWHERE", "address": "", "lat": None}])
    assert look.find("@SAMPLE DELI") is None
    assert look.find("SAMPLE PIER")["address"] == "3 SAMPLE ST"                 # no point: still its address
    assert look.find("NOWHERE") is None


def site_entries(rows: list[tuple]) -> dict[str, dict]:
    """(name, address, lat, lon, located) rows as the import's entries, grouped into sites."""
    out = {cp.entry_key(n, a): {"name": n, "aliases": [], "label": n.title(), "address": a, "lat": la, "lon": lo,
                                "muni": PC, "located": how, "source": cp.SOURCE} for n, a, la, lo, how in rows}
    cp.group_sites(out)
    return out


M = 0.0001                                                                               # ~11 m north


def test_one_name_at_neighbouring_addresses_is_one_site():
    """A venue that is several buildings at adjacent numbers ("145", "149", "149-151"), across-the-street buildings: one
    site, found by its name at its primary. A chain's stores far apart, or an address with no point off the site's
    street, stay apart: the name alone is no place."""
    e = site_entries([("SAMPLE HALL", "149-151 EXAMPLE AVE", LAT + 4 * M, SAMPLE, "exact"),
                      ("SAMPLE HALL", "145 EXAMPLE AVE", LAT, SAMPLE, "interpolated"),
                      ("SAMPLE HALL", "149 EXAMPLE AVE", LAT + 3 * M, SAMPLE, "exact"),
                      ("SAMPLE HALL", "153 EXAMPLE AVE", None, None, ""),             # no point, next door: joins
                      ("SAMPLE SCHOOL", "94 SAMPLE ST", LAT, TEST, "interpolated"),
                      ("SAMPLE SCHOOL", "95 SAMPLE ST", LAT + M, TEST + M, "interpolated"),
                      ("SAMPLE MART", "1 EXAMPLE AVE", LAT, SAMPLE, "exact"),
                      ("SAMPLE MART", "199 EXAMPLE AVE", LAT, TEST, "exact"),           # ~840 m away: another store
                      ("SAMPLE ANNEX", "21 SAMPLE ST", LAT, SAMPLE, "exact"),
                      ("SAMPLE ANNEX", "4 TEST RD", None, None, "")])                   # no point, another street
    hall = cp.entry_key("SAMPLE HALL", "149 EXAMPLE AVE")                               # the lowest exact number
    assert {k for k, v in e.items() if v.get("site") == hall} == {cp.entry_key("SAMPLE HALL", a) for a in (
        "145 EXAMPLE AVE", "149 EXAMPLE AVE", "149-151 EXAMPLE AVE", "153 EXAMPLE AVE")}
    assert e[hall]["site_addresses"] == ["145 EXAMPLE AVE", "149 EXAMPLE AVE", "149-151 EXAMPLE AVE", "153 EXAMPLE AVE"]
    school = cp.entry_key("SAMPLE SCHOOL", "94 SAMPLE ST")                               # none exact: the first
    assert e[school]["site"] == school and e[cp.entry_key("SAMPLE SCHOOL", "95 SAMPLE ST")]["site"] == school
    assert not any(v.get("site") for v in e.values() if v["name"] in ("SAMPLE MART", "SAMPLE ANNEX"))
    first = site_entries([("SAMPLE DOCK", "2 SAMPLE ST", None, None, ""),                # no point, first in the file
                          ("SAMPLE DOCK", "4 SAMPLE ST", LAT, SAMPLE, "interpolated")])
    assert {v["site"] for v in first.values()} == {cp.entry_key("SAMPLE DOCK", "4 SAMPLE ST")}   # the primary has a point
    look = cp.Lookup([dict(v, key=k) for k, v in e.items()])
    got = look.find("@SAMPLE HALL")
    assert (got["address"], got["lat"], len(got["site_addresses"])) == ("149 EXAMPLE AVE", LAT + 3 * M, 4)
    assert look.find("SAMPLE SCHOOL")["address"] == "94 SAMPLE ST"
    assert look.find("SAMPLE MART") is None and look.find("SAMPLE ANNEX") is None       # never picks one of two places
    assert cp.Lookup(list(e.values())).find("SAMPLE HALL")["address"] == "149 EXAMPLE AVE"   # without keys: the same


def test_sites_chain_and_are_made_again_on_each_import():
    rows = [("SAMPLE PIER", f"{n} SAMPLE ST", LAT + k * 12 * M, SAMPLE, "exact") for k, n in enumerate((1, 3, 5))]
    e = site_entries(rows)                                      # each ~130 m from the next: one site end to end
    assert {v["site"] for v in e.values()} == {cp.entry_key("SAMPLE PIER", "1 SAMPLE ST")}
    e[cp.entry_key("SAMPLE PIER", "3 SAMPLE ST")].update(lat=LAT + 0.01)        # moved away: 1 and 5 are ~265 m apart
    cp.group_sites(e)
    assert not any(v.get("site") or v.get("site_addresses") for v in e.values())
    assert cp.house("189-1/2 SAMPLE ST") == (189, "sample st") and cp.house("12A EXAMPLE AVE") == (12, "example ave")
    assert cp.house("QUILL SAMPLE") == (None, "quill sample")


def test_a_site_through_the_import_and_the_feed(cad):
    rows = [HEADER, ["120 EXAMPLE AVE", "SAMPLE HALL", "PORT_CHESTER"], ["124 EXAMPLE AVE", "SAMPLE HALL", "PORT_CHESTER"],
            ["122 EXAMPLE AVE", "SAMPLE HALL", "PORT_CHESTER"], ["7 NOWHERE RD", "SAMPLE HALL", "PORT_CHESTER"]]
    post(cad, "common_places", action="apply", file=b64(xlsx(rows)), filename="list.xlsx")
    got = imported(cad)
    primary = "county/sample hall @ 120 example ave"
    assert got[primary]["site_addresses"] == ["120 EXAMPLE AVE", "122 EXAMPLE AVE", "124 EXAMPLE AVE"]
    assert "site" not in got["county/sample hall @ 7 nowhere rd"]            # no point, another street: apart
    assert cp.Lookup(list(got.values())).find("SAMPLE HALL") is None          # so the bare name is still two places
    post(cad, "common_places", action="apply", file=b64(xlsx(rows[:4])), filename="list.xlsx")
    hit = cp.Lookup(cad.local_places()).find("@SAMPLE HALL", town="port chester")
    assert (hit["key"], hit["at"]) == (primary, "120 Example Ave")


# --------------------------------------------------------------------------- the settings file, a broken file

def test_the_settings_file_never_carries_or_touches_the_imported_names(cad):
    post(cad, "places", action="set", name="the Waterfront", place={"label": "Example Park", "lat": 41.002, "lon": -73.68})
    post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    exported = cad.settings_export()
    assert list(exported["overrides"]["places"]) == ["the waterfront"]           # the county's: their own import
    assert "QUILLBERRY" not in json.dumps(exported).upper() and "MARINA" not in json.dumps(exported).upper()
    # an older file that still has them is read without them
    old = json.loads(json.dumps(exported))
    old["overrides"]["places"]["county/old name @ 1 example ave"] = {"name": "OLD NAME", "source": cp.SOURCE}
    before = len(cad.settings.audit_log())
    dry = post(cad, "import", action="dry_run", settings=old)
    assert not any(c["store"] == "places" for c in dry["changes"])
    post(cad, "import", action="apply", settings=old)
    assert len(imported(cad)) == 6 and "county/old name @ 1 example ave" not in imported(cad)   # kept as they were
    said = json.dumps(cad.settings.audit_log()[:len(cad.settings.audit_log()) - before]).upper()
    assert not any(w in said for w in ("MARINA", "EXAMPLE AVE", "COMMUNITY", "OLD NAME"))       # no names audited
    assert [r["name"] for r in cad.local_places() if not cp.is_imported(r["key"], r)] == ["the Waterfront"]


def test_a_broken_or_hostile_file_is_a_clean_error(monkeypatch):
    big = io.BytesIO()
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:                      # a small zip, a huge part
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet>" + " " * (cp.MAX_XML + 1) + "</worksheet>")
    assert len(big.getvalue()) < 100_000
    with pytest.raises(cp.TableError, match="too large"):
        cp.read_table(big.getvalue(), "list.xlsx")
    with pytest.raises(cp.TableError, match="can't be read"):
        cp.read_table(("Address,Common Place\n1 EXAMPLE AVE," + "x" * 200_000 + "\n").encode(), "list.csv")   # csv.Error
    for err in (RuntimeError("encrypted"), NotImplementedError("compression"), zipfile.BadZipFile("bad")):
        def boom(data, err=err):
            raise err
        monkeypatch.setattr(cp, "_read_xlsx", boom)
        with pytest.raises(cp.TableError, match="can't be read"):
            cp.read_table(xlsx([HEADER, *ROWS]), "list.xlsx")


def test_the_place_search_prefers_our_town_for_a_listed_name(cad):
    rows = [HEADER, ["120 EXAMPLE AVE", "PORT CHESTER BUS DEPOT", "PORT_CHESTER"],
            ["3 EXAMPLE AVE EXT", "RYE BROOK BUS DEPOT", "RYE_BROOK"]]
    post(cad, "common_places", action="apply", file=b64(xlsx(rows)), filename="list.xlsx")
    hits = json.loads(CadApi(cad).get("/api/cad/places", "q=bus+depot", None)[1])["places"]
    assert [h["sub"] for h in hits[:2]] == [PC, RB]


# --------------------------------------------------------------------------- the Settings card, in the browser

def test_the_settings_card_previews_and_applies(cad, tmp_path):
    """Settings > Lists > Place names > Common places: pick the file, see the counts and what was not found, apply.
    In headless Chromium (skipped without Playwright); SIZEUP_SHOTS=<dir> saves its screenshots there."""
    import os
    import threading
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright (pip install playwright)")
    from sizeup.web.server import WebServer
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    post(cad, "places", action="set", name="the Waterfront", place={"label": "Example Park", "lat": 41.002, "lon": -73.68})
    sheet = tmp_path / "common places.xlsx"
    sheet.write_bytes(xlsx([HEADER, *ROWS]))
    shots = os.environ.get("SIZEUP_SHOTS", "")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
        pg = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(base), lambda route: route.abort())
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/cad#settings/lists")
        pg.fill("#operator", "JD")
        pg.click("button.tab:text-is('Place names')")
        pg.wait_for_selector("h2:text-is('Common places')")
        pg.set_input_files("input[type=file][accept='.xlsx,.csv']", str(sheet))
        pg.wait_for_selector("#s-common p:has-text('6 names')")
        assert "6 new · 0 already · 0 removed · 1 not found" in pg.inner_text("#s-common")
        if shots:
            pg.locator("#s-common").evaluate("e => e.closest('.s-card').scrollIntoView()")
            pg.locator("#s-common").evaluate("e => e.querySelector('details').open = true")
            pg.locator("#s-common").locator("xpath=..").screenshot(path=os.path.join(shots, "common-places-preview.png"))
        assert imported(cad) == {}                                            # a preview writes nothing
        pg.click("#s-common button:text-is('Apply 6 names')")
        pg.wait_for_selector("text=5/6 located")
        assert len(imported(cad)) == 6 and pg.inner_text("#s-common") == ""
        if shots:
            pg.locator("h2:text-is('Common places')").locator("xpath=../..").screenshot(
                path=os.path.join(shots, "common-places-applied.png"))
        assert errors == []
        browser.close()
    ws.shutdown()
