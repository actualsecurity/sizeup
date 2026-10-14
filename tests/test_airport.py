"""Westchester County Airport (HPN) in the CAD (GitHub #87): the Airport map layer and its data file, the
airport's places, the AIRPORT event type and its run cards, a page or dispatch naming the airport or an
alert level, the Airport Incident talkgroup's auto-link, and an Airport event's default location."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

from sizeup.cad import airport, lists, runcards
from sizeup.cad.api import CadApi
from sizeup.cad.comms import why_text
from sizeup.cad.store import CadError
from sizeup.dispatch import parse
from sizeup.geo.gazetteer import Gazetteer
from sizeup.geo.places import PlaceIndex
from tests.test_cad import make_cad, monitor_incident, uid
from tests.test_places import fixture_gazetteer
from tests.test_radios import Clock, cad, tx

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
TOOLS = ROOT / "tools"
LAYER = ROOT / "data" / "khpn_airport.geojson"


@pytest.fixture(scope="module")
def fa():
    spec = importlib.util.spec_from_file_location("fetch_airport", TOOLS / "fetch_airport.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def layer():
    return json.loads(LAYER.read_text())


# --------------------------------------------------------------------------- the layer's data

def test_layer_file_is_built_from_the_checked_in_sources_with_each_feature_sourced(fa, layer):
    built = fa.build_layer(fa.read("khpn_surface.geojson"), fa.read("khpn_procedures.geojson"), layer["built"])
    assert built["features"] == layer["features"] and built["airport"] == layer["airport"]       # the file is current
    a = layer["airport"]
    assert (a["icao"], a["iata"], a["address"], a["town"]) == ("KHPN", "HPN", "240 Airport Rd", "Rye Brook")
    assert a["bbox"][0] < a["lon"] < a["bbox"][2] and a["bbox"][1] < a["lat"] < a["bbox"][3] and a["near_km"] == 1.5
    codes = {s["code"] for s in layer["sources"]}
    assert codes == {"khpn-osm", "khpn-faa", "khpn-nys", "khpn-tiger"}
    assert all(s.get("license") for s in layer["sources"])
    assert any("OpenStreetMap" in s["attribution"] for s in layer["sources"] if s.get("attribution"))
    for f in layer["features"]:
        p = f["properties"]
        assert p["source"] in codes and p["layer"] in ("airport", "approaches") and p["kind"], p
    kinds = {}
    for f in layer["features"]:
        kinds.setdefault(f["properties"]["kind"], []).append(f)
    assert sorted(x["properties"]["label"] for x in kinds["runway"]) == ["11/29", "16/34"]
    assert sorted(x["properties"]["label"] for x in kinds["runway_end"]) == ["11", "16", "29", "34"]
    assert {x["properties"]["label"] for x in kinds["taxiway"]} >= set("ABCDEFGHJKL")
    assert len(kinds["apron"]) >= 5 and len(kinds["building"]) >= 50 and len(kinds["aerodrome"]) == 1
    pois = {x["properties"]["label"] for x in kinds["poi"]}
    assert pois >= {"Terminal", "Airport Command Post / EOC", "Million Air", "Signature (FBO)", "Control tower",
                    "Entrance: Airport Rd at Rye Lake Ave", "Entrance: Tower Rd at Purchase St"}
    staging = next(x for x in kinds["poi"] if x["properties"].get("staging"))
    assert staging["geometry"]["coordinates"] == [-73.712696, 41.068747]            # the NYS address point, to confirm
    # the approaches: the finals to 16 and 34 and the fixes on them, each labelled
    finals = kinds["approach"]
    assert {x["properties"]["runway"] for x in finals} == {"16", "34"} and all(x["properties"]["layer"] == "approaches" for x in finals)
    assert all(x["properties"]["label"].endswith(" final") for x in finals)
    fixes = {x["properties"]["label"]: x["properties"] for x in kinds["fix"]}
    assert {"CZIMR", "ISLET", "KIBGE", "FARAN", "HAARP"} <= set(fixes)
    assert fixes["CZIMR"]["faf"] and fixes["CZIMR"]["alt_ft"] == 2000 and not fixes["FARAN"]["faf"]
    assert "ALIXX" not in fixes                                                       # an IAF far out: not drawn
    # coordinates are rounded (the file stays small) and every geometry lies around the field or its approaches
    assert LAYER.stat().st_size < 150_000
    for f in layer["features"]:
        s = json.dumps(f["geometry"]["coordinates"])
        assert not re.search(r"\d\.\d{7}", s), f["properties"]
        assert re.search(r"-7[34]\.\d+", s) and re.search(r"4[01]\.\d+", s)


def test_the_api_serves_the_layer_and_the_module_knows_where_the_field_is(layer):
    assert airport.layer() == layer and airport.layer(LAYER) == layer
    assert airport.layer(LAYER.parent / "nope.geojson") == {}
    c = make_cad()
    code, raw, ctype = CadApi(c).get("/api/cad/airport", "")
    assert code == 200 and ctype.startswith("application/json") and json.loads(raw)["airport"] == layer["airport"]
    assert airport.near(41.0688, -73.7043)                   # the terminal
    assert airport.near(41.0605, -73.7190)                   # Tower Rd at Purchase St
    assert not airport.near(41.0016, -73.6657)               # Port Chester
    assert not airport.near(None, None) and not airport.near("x", 1)
    assert round(airport.km(*airport.ADDRESS_LL, *airport.ARP), 2) == pytest.approx(0.35, abs=0.05)


# --------------------------------------------------------------------------- the page

def test_the_airport_layer_is_in_the_layers_box_drawn_by_its_own_module():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/airport.js"') < html.index('src="/static/maplayers.js"')   # registered before the box
    ml = (STATIC / "maplayers.js").read_text()
    assert re.search(r'key: "airport", label: "Airport", feed: false, minzoom: 0, on: false, plugin: "AirportLayer"', ml)
    assert re.search(r'key: "approaches", label: "Approaches", feed: false, minzoom: 0, on: false, plugin: "AirportLayer", sub: "airport"', ml)
    assert "AirportLayer.init(map, before, glyphs)" in ml and "AirportLayer.apply(this.map" in ml
    assert "focus(inc)" in ml and "AirportLayer.near(inc)" in ml and "AirportLayer.credit()" in ml
    ap = (STATIC / "airport.js").read_text()
    assert ap.startswith("// The Airport layer (GitHub #87)") and "window.AirportLayer" in ap
    assert 'fetch(URL, { credentials: "same-origin" })' in ap and 'const URL = "/api/cad/airport"' in ap
    # the design's colours (style.css tokens), not raw ones; the label halo is the one constant
    hexes = set(re.findall(r"#[0-9A-Fa-f]{6}\b", ap))
    for hx in hexes:
        assert re.search(rf'token\("--[\w-]+", "{hx}"\)', ap) or hx == "#15181B", hx
    for tok in ("--info", "--warn", "--dim", "--text-2", "--text"):
        assert f'token("{tok}"' in ap, tok
    # nothing but the outline below zoom 12; runways from 11, the rest from 12 and 13; gates only close up
    for layer_id, mz in (("ap-apron", 12), ("ap-building", 13), ("ap-taxiway", 12), ("ap-taxiway-label", 14), ("ap-runway", 11),
                         ("ap-runway-end", 12), ("ap-gate", 16), ("ap-poi", 13), ("ap-poi-label", 13)):
        assert re.search(rf'id: "{layer_id}", type: "\w+", source: SRC, minzoom: {mz},', ap), layer_id
    assert re.search(r'id: "ap-outline", type: "line", source: SRC, filter', ap)
    assert 'visibility: "none"' in ap                     # every layer starts off: the Layers box turns it on
    assert "approachesOn" in ap and "airportOn && approachesOn" in ap    # the sub-toggle needs the airport on
    assert "openstreetmap.org/copyright" in ap            # ODbL attribution in the map's corner
    cm = (STATIC / "cadmap.js").read_text()
    assert "this.roads.focus(this.focus)" in cm and "AirportLayer.focusBounds(this.focus)" in cm
    css = (STATIC / "cadmap.css").read_text()
    assert ".ml-list .ml-sub {" in css


# --------------------------------------------------------------------------- the places

def test_airport_places_rows_for_the_search(fa, tmp_path):
    rows = fa.place_rows(fa.read("khpn_surface.geojson"))
    names = {r["name"] for r in rows}
    assert {"Airport Terminal (HPN)", "Airport Command Post / EOC (staging)", "Million Air (FBO, HPN)", "Signature (FBO hangar, HPN)",
            "Control tower (HPN)", "Hangar 26 (HPN)", "Hangar V (HPN)", "Gate A (HPN)", "Gate D (HPN)", "Runway 16 end (HPN)",
            "Runway 34 end (HPN)", "Runway 11 end (HPN)", "Runway 29 end (HPN)", "Taxiway A (HPN)", "Taxiway L (HPN)",
            "Airport entrance, Airport Rd at Rye Lake Ave (HPN)", "Airport entrance, Tower Rd at Purchase St (HPN)"} <= names
    assert all(r["kind"] == "place" and r["category"] == "airport" and r["source"].startswith("khpn-") and r["ref"] for r in rows)
    assert all("hpn" in r["aliases"].split("|") and "airport" in r["aliases"].split("|") for r in rows)
    cp = next(r for r in rows if r["name"].startswith("Airport Command Post"))
    assert (cp["lat"], cp["lon"], cp["address"], cp["source"]) == (41.068747, -73.712696, "1 Airport Emergency Staging Way, Harrison", "khpn-nys")
    assert {"Airport - Command Post/EOC", "airport command post", "airport EOC", "airport staging", "staging way"} <= set(cp["aliases"].split("|"))
    term = next(r for r in rows if r["name"] == "Airport Terminal (HPN)")
    assert (term["address"], term["lat"], term["lon"]) == ("240 Airport Rd, Rye Brook", 41.068787, -73.704333)
    assert next(r for r in rows if r["name"] == "Runway 16 end (HPN)")["muni"] == "Harrison"      # west of Rye Brook's edge
    # into a gazetteer: the "@" search finds them by name and alias, and only these rows change
    path = fixture_gazetteer(tmp_path / "gaz.sqlite")
    db = sqlite3.connect(path)
    before = db.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    n = fa.add_places(db, rows, "2026-10-13")
    assert n == len(rows) == db.execute("SELECT COUNT(*) FROM places WHERE source LIKE 'khpn-%'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM places").fetchone()[0] == before + n
    assert fa.add_places(db, rows, "2026-10-14") == n and db.execute("SELECT COUNT(*) FROM places").fetchone()[0] == before + n
    meta = dict(db.execute("SELECT key, value FROM meta"))
    src = next(s for s in json.loads(meta["sources"]) if s.get("code") == "khpn")
    assert src["part"] == "places" and src["fetched"] == "2026-10-14" and "OpenStreetMap" in src["name"] and "CC0" in src["name"]
    assert json.loads(meta["counts"])["places"]["khpn"] == n
    db.close()
    ix = PlaceIndex(Gazetteer(path))
    top = lambda q: ix.search(q)[0]
    assert top("staging way")["label"] == "Airport Command Post / EOC (staging)"
    assert top("airport command post")["label"] == "Airport Command Post / EOC (staging)"
    assert top("airport eoc")["sub"] == "Harrison"
    assert top("runway 16")["label"] == "Runway 16 end (HPN)"
    assert top("taxiway b")["label"] == "Taxiway B (HPN)"
    assert top("million air")["label"] == "Million Air (FBO, HPN)"
    assert top("hpn terminal")["label"] == "Airport Terminal (HPN)"
    assert top("gate c")["label"] == "Gate C (HPN)"
    assert {h["label"] for h in ix.search("hpn", limit=40)} >= {"Airport Terminal (HPN)", "Runway 34 end (HPN)"}
    assert all(h["kind"] == "place" and h["category"] == "airport" for h in ix.search("hpn", limit=10))


def test_the_live_gazetteer_has_the_airport_places():
    db = sqlite3.connect(f"file:{ROOT / 'data' / 'gazetteer.sqlite'}?mode=ro", uri=True)
    rows = db.execute("SELECT name, muni, address FROM places WHERE source LIKE 'khpn-%' AND category = 'airport'").fetchall()
    db.close()
    assert len(rows) == 28
    assert ("Airport Command Post / EOC (staging)", "Harrison", "1 Airport Emergency Staging Way, Harrison") in rows
    assert ("Airport Terminal (HPN)", "Rye Brook", "240 Airport Rd, Rye Brook") in rows


# --------------------------------------------------------------------------- the type, its phrases and run cards

def test_airport_type_subtypes_phrases_and_the_confirm_notes():
    L = lists.load()
    assert "AIRPORT" in L.type_codes and L.name("event_types", "AIRPORT") == "Airport"
    assert L.subtypes_for("AIRPORT") == ("ALERT-1", "ALERT-2", "ALERT-3", "ACFT-OFF", "ACFT-WATER", "FUEL", "ACFT-FIRE",
                                         "ACFT-STRU", "DISABLED", "SECURITY", "HIJACK", "OTHER")
    assert L.type_label("AIRPORT", "ALERT-2") == "Airport / Alert 2 (full emergency)"
    assert L.name("event_subtypes", "ACFT-FIRE") == "Aircraft fire / ground emergency"
    assert L.name("event_subtypes", "ACFT-WATER") == "Aircraft in the water / water rescue"
    # the spoken phrases: the FAA alert wording, terse
    assert L.phrase("AIRPORT", "ALERT-1") == "an Alert 1, local standby, at Westchester County Airport"
    assert L.phrase("AIRPORT", "ALERT-2") == "an Alert 2, full emergency, at Westchester County Airport"
    assert L.phrase("AIRPORT", "ALERT-3") == "an Alert 3, aircraft accident, at Westchester County Airport"
    assert L.phrase("AIRPORT", "ACFT-OFF") == "an aircraft accident near the airport"
    assert L.phrase("AIRPORT", "ACFT-WATER") == "an aircraft in the water"
    assert L.phrase("AIRPORT", "FUEL") == "a fuel spill at the airport"
    assert L.phrase("AIRPORT", "ACFT-STRU") == "a structure fire at Westchester County Airport"
    assert L.phrase("AIRPORT", "DISABLED") == "a disabled aircraft at the airport"
    assert L.phrase("AIRPORT", "HIJACK") == "a hijacking at Westchester County Airport"
    assert L.phrase("AIRPORT") == "an incident at Westchester County Airport"
    assert L.is_emergency("AIRPORT", "ALERT-2") and L.is_emergency("AIRPORT", "ACFT-WATER") and L.is_emergency("AIRPORT")
    L.check_type("AIRPORT", "OTHER")
    with pytest.raises(ValueError, match="AIRPORT can't be COMM"):
        L.check_type("AIRPORT", "COMM")
    with pytest.raises(ValueError, match="STRU can't be ALERT-2"):
        L.check_type("STRU", "ALERT-2")
    # a proposal until the department confirms: marked like the other guesses, with the FAA definition as help
    assert "AIRPORT" in L.type_subtypes_partial["confirm"] and "AIRPORT" not in L.type_subtypes_partial["given"]
    t = next(t for t in L.event_types if t["code"] == "AIRPORT")
    assert t["confirm"] and "AC 150/5200-31C" in t["help"]
    subs = {x["code"]: x for x in L.event_subtypes}
    for c in L.subtypes_for("AIRPORT"):
        if c != "OTHER":
            assert subs[c]["confirm"].startswith("proposed"), c
            assert subs[c]["help"], c
    assert "Alert I, local standby" in subs["ALERT-1"]["help"] and "PCFD normally not due" in subs["ALERT-1"]["help"]
    assert "Alert II, full emergency" in subs["ALERT-2"]["help"] and "para 7-1-4a" in subs["ALERT-2"]["help"]
    assert "Alert III, aircraft accident" in subs["ALERT-3"]["help"] and "on or in the vicinity of the airport" in subs["ALERT-3"]["help"]
    assert "Rye Lake" in subs["ACFT-OFF"]["help"] and "confirm" in subs["SECURITY"]["help"]
    assert "help" in L.public()["event_subtypes"][0] or any("help" in x for x in L.public()["event_subtypes"])
    # an edit in Settings keeps the help text (the definition), drops the "to confirm" note
    from sizeup.cad.settings import overlay
    d = overlay(lists.read(), {"event_subtypes": {"ALERT-2": {"name": "Alert 2"}}})
    x = next(x for x in d["event_subtypes"] if x["code"] == "ALERT-2")
    assert x["name"] == "Alert 2" and "Alert II" in x["help"] and "confirm" not in x


def test_the_departments_airport_run_cards_alert_2_named_units_alert_3_full_department():
    """The department, 2026-10-13: "put Engine 64, 62 and Rescue 40 on the Alert Level 2, and FDR on the 3 and up"."""
    c = make_cad()
    cards = {x["type"]: x for x in runcards.cards(c.store)}
    a2, a3 = cards["AIRPORT/ALERT-2"], cards["AIRPORT/ALERT-3"]
    assert a2["slots"] == [{"unit": "E64"}, {"unit": "E62"}, {"unit": "R40"}]
    assert a3["slots"] == [{"all": "Port Chester FD"}]
    # the department's own: not a stand-in (no "not PCFD policy" banner for them), and said so
    assert not a2["is_default"] and not a3["is_default"]
    assert a2["confirmed"].startswith("the department, 2026-10-13") and a3["confirmed"].startswith("the department, 2026-10-13")
    assert a2["remarks"].startswith("Confirmed by the department 2026-10-13: Engine 64, Engine 62, Rescue 40.")
    assert "Staging: Airport Command Post / EOC, 1 Airport Emergency Staging Way" in a2["remarks"]
    assert "Staging: Airport Command Post / EOC, 1 Airport Emergency Staging Way" in a3["remarks"]
    assert "Mutual aid" in a3["remarks"] and "Not yet PCFD policy" not in a2["remarks"] and "Not yet PCFD policy" not in a3["remarks"]
    assert a2["phrase"] == "an Alert 2, full emergency, at Westchester County Airport"
    assert a3["phrase"] == "an Alert 3, aircraft accident, at Westchester County Airport"
    # "and up": an accident off the airport and an aircraft in the water take Alert 3's card as a proposal
    off, water = cards["AIRPORT/ACFT-OFF"], cards["AIRPORT/ACFT-WATER"]
    assert off["slots"] == [{"all": "Port Chester FD"}] and off["is_default"] and not off["confirmed"]
    assert water["slots"] == [{"unit": "M40"}, {"all": "Port Chester FD"}] and water["is_default"]
    assert "to confirm" in off["remarks"] and "to confirm" in water["remarks"]
    assert cards["STRU"]["remarks"] == "" and cards["STRU"]["is_default"] and not cards["STRU"]["confirmed"]
    # the recommendation: exactly E64, E62, R40 on an Alert 2; the full department on an Alert 3; nothing on an Alert 1
    iid = c.create({"type": "AIRPORT", "subtype": "ALERT-2", "operator": "JD"})["id"]
    r = runcards.recommend(c.store, "AIRPORT", "ALERT-2", iid)
    assert r["card"] == "AIRPORT/ALERT-2" and [u["name"] for u in r["units"]] == ["E64", "E62", "R40"]
    assert r["full"] == "" and r["unfilled"] == [] and r["remarks"] == a2["remarks"]
    r3 = runcards.recommend(c.store, "AIRPORT", "ALERT-3", iid)
    assert r3["card"] == "AIRPORT/ALERT-3" and r3["full"] == "Port Chester FD"
    assert {"E64", "E62", "R40", "E60", "L31"} <= {u["name"] for u in r3["units"]}       # every PCFD apparatus
    assert runcards.recommend(c.store, "AIRPORT", "ALERT-1", iid)["card"] is None     # no card: PCFD not due
    assert runcards.recommend(c.store, "AIRPORT", "ALERT-1", iid)["remarks"] == ""
    # the spoken dispatch for an Alert 2, the three units by name
    c.update(iid, {"address": "", "operator": "JD"})
    units = [uid(c, n) for n in ("E64", "E62", "R40")]
    d = c.dispatch(iid, {"unit_ids": units, "operator": "JD"})
    assert d["text"].startswith("Port Chester Fire, Engine 64, Engine 62, Rescue 40, respond to 240 Airport Road, "
                                "for an Alert 2, full emergency, at Westchester County Airport.")
    # a Settings edit makes it an edited card, no longer the department's; Reset brings the department's back
    e = c.save_run_card({"type": "AIRPORT/ALERT-2", "slots": [{"kind": "engine", "count": 2}], "remarks": "  Stage at  the EOC. "})
    assert e["remarks"] == "Stage at the EOC." and not e["is_default"] and not e["confirmed"]
    with pytest.raises(CadError, match="remarks: text"):
        c.save_run_card({"type": "AIRPORT/ALERT-2", "slots": [{"kind": "engine", "count": 2}], "remarks": 7})
    back = runcards.reset(c.store, "AIRPORT/ALERT-2")
    assert back["slots"] == a2["slots"] and back["confirmed"] and not back["is_default"] and back["remarks"] == a2["remarks"]
    # exported and imported, the remarks follow the card
    out = c.settings_export()
    assert next(x for x in out["run_cards"] if x["type"] == "AIRPORT/ALERT-3")["remarks"] == a3["remarks"]
    assert runcards.save(c.store, "AIRPORT/ALERT-3", a3["slots"], remarks="x" * 400)["remarks"] == "x" * runcards.MAX_REMARKS


def test_a_database_seeded_with_96s_stand_in_cards_gets_the_departments_cards(tmp_path):
    """The live database has #96's Alert 2 / Alert 3 stand-ins (duty officer + full department, is_default): on the
    next open the department's cards replace them, as long as nobody edited them."""
    c = make_cad(tmp_path)
    with c.store.tx() as db:
        for k, slots in (("AIRPORT/ALERT-2", [{"unit": "2398"}, {"all": "Port Chester FD"}]),
                         ("AIRPORT/ALERT-3", [{"unit": "2398"}, {"all": "Port Chester FD"}])):
            cid = db.execute("SELECT id FROM run_cards WHERE type = ?", (k,)).fetchone()[0]
            db.execute("UPDATE run_cards SET is_default = 1, remarks = 'old' WHERE id = ?", (cid,))
            runcards._write_slots(db, cid, slots)
        cid = db.execute("SELECT id FROM run_cards WHERE type = 'STRU'").fetchone()[0]
        db.execute("UPDATE run_cards SET is_default = 0 WHERE id = ?", (cid,))          # an edited card: untouched
        runcards._write_slots(db, cid, [{"kind": "engine", "count": 1}])
    assert runcards.seed(c.store, fill=True) == 2
    cards = {x["type"]: x for x in runcards.cards(c.store)}
    assert cards["AIRPORT/ALERT-2"]["slots"] == [{"unit": "E64"}, {"unit": "E62"}, {"unit": "R40"}]
    assert cards["AIRPORT/ALERT-3"]["slots"] == [{"all": "Port Chester FD"}] and cards["AIRPORT/ALERT-3"]["confirmed"]
    assert cards["STRU"]["slots"] == [{"kind": "engine", "count": 1}] and not cards["STRU"]["is_default"]
    assert runcards.seed(c.store, fill=True) == 0
    # an edited Alert 2 (a person's) is left alone
    with c.store.tx() as db:
        cid = db.execute("SELECT id FROM run_cards WHERE type = 'AIRPORT/ALERT-2'").fetchone()[0]
        db.execute("UPDATE run_cards SET is_default = 0 WHERE id = ?", (cid,))
        runcards._write_slots(db, cid, [{"unit": "E60"}])
    assert runcards.seed(c.store, fill=True) == 0
    assert runcards.card(c.store, "AIRPORT/ALERT-2")["slots"] == [{"unit": "E60"}]


def test_an_old_database_gets_the_remarks_column(tmp_path):
    from sizeup.cad.store import Store
    p = tmp_path / "cad.sqlite"
    s = Store(str(p))
    with s.tx() as db:
        db.execute("ALTER TABLE run_cards DROP COLUMN remarks")
    s.db.close()
    s2 = Store(str(p))
    assert "remarks" in {r["name"] for r in s2.db.execute("PRAGMA table_info(run_cards)")}
    assert all(c["remarks"] == "" for c in runcards.cards(s2))                 # old cards keep ''
    s2.db.close()


# --------------------------------------------------------------------------- a page or dispatch naming the airport

NOT_STATED = "airport named on the page: alert level not stated"


@pytest.mark.parametrize("text, want", [
    # the alerts, every way they are said
    ("Port Chester Fire, respond to Westchester County Airport for an Alert 2", ("AIRPORT", "ALERT-2", "")),
    ("alert two at the airport, Rye Brook", ("AIRPORT", "ALERT-2", "")),
    ("Alert II, Gulfstream declared an emergency", ("AIRPORT", "ALERT-2", "")),
    ("Alert III, aircraft down", ("AIRPORT", "ALERT-3", "")),
    ("ALERT-1 standby, HPN tower reports gear problem", ("AIRPORT", "ALERT-1", "")),
    ("Alert I at the airport", ("AIRPORT", "ALERT-1", "")),
    ("alert level 2 at the county airport", ("AIRPORT", "ALERT-2", "")),
    ("Alert 2 Level 3 at the airport", ("AIRPORT", "ALERT-2", "")),             # both: the Alert is the alert
    ("full emergency, Airbus A320 inbound with an engine fire", ("AIRPORT", "ALERT-2", "")),
    ("local standby at the airport, Cessna with a rough engine", ("AIRPORT", "ALERT-1", "")),
    ("aircraft accident alert, Westchester County Airport", ("AIRPORT", "ALERT-3", "")),
    # "Level N" alone is the souls Level, never an alert
    ("Port Chester Fire, respond to the airport for a level 1", ("AIRPORT", "", NOT_STATED + "; Level 1 heard: the souls-on-board level, not an alert")),
    ("respond to the airport, Level 3, engine out, Learjet inbound 5 minutes out",
     ("AIRPORT", "", "engine out: alert level not stated; Level 3 heard: the souls-on-board level, not an alert")),
    # an aircraft emergency with no alert said: the nature, never an alert guessed
    ("aircraft emergency, declared an emergency, N123AB", ("AIRPORT", "", "declared an emergency: alert level not stated")),
    ("Cessna inbound with an emergency, unsafe gear, HPN", ("AIRPORT", "", "unsafe gear: alert level not stated")),
    ("plane inbound with smoke in the cockpit", ("AIRPORT", "", "smoke in the cockpit: alert level not stated")),
    ("aircraft with a bird strike returning to the field", ("AIRPORT", "", "bird strike: alert level not stated")),
    ("emergency landing, hydraulic failure, Falcon 2000", ("AIRPORT", "", "hydraulic failure: alert level not stated")),
    # accidents: on or near the field an Alert 3, off the airport its own subtype, in the water its own
    ("Alert 3, aircraft down, runway 16", ("AIRPORT", "ALERT-3", "")),
    ("plane crash off the end of runway 34", ("AIRPORT", "ALERT-3", "")),
    ("aircraft crash at the county airport", ("AIRPORT", "ALERT-3", "")),
    ("plane down", ("AIRPORT", "ALERT-3", "aircraft accident: on or off the airport not stated")),
    ("respond to Rye Lake for an aircraft accident, small plane in the water", ("AIRPORT", "ACFT-WATER", "")),
    ("helicopter crashed into the woods near the airport off King Street", ("AIRPORT", "ACFT-OFF", "")),
    ("plane down, Kensico reservoir", ("AIRPORT", "ACFT-OFF", "")),
    ("aircraft in the water, Blind Brook", ("AIRPORT", "ACFT-WATER", "")),
    ("water rescue at the airport, plane in the lake", ("AIRPORT", "ACFT-WATER", "")),
    # the other calls at the airport by name
    ("fuel spill on the ramp at Westchester County Airport", ("AIRPORT", "FUEL", "")),
    ("fuel tanker rollover on the field at the county airport, hazmat", ("AIRPORT", "FUEL", "")),
    ("aircraft fire on the ground, county airport", ("AIRPORT", "ACFT-FIRE", "")),
    ("APU fire on a jet at the gate, HPN", ("AIRPORT", "ACFT-FIRE", "")),
    ("hangar fire at the county airport, Million Air", ("AIRPORT", "ACFT-STRU", "")),
    ("working fire in the terminal, Westchester County Airport", ("AIRPORT", "ACFT-STRU", "")),
    ("aircraft off the runway at the county airport, runway 16, no injuries", ("AIRPORT", "DISABLED", "")),
    ("disabled aircraft on the taxiway, collapsed gear, HPN", ("AIRPORT", "DISABLED", "")),
    ("respond to the airport for a bomb threat at the terminal", ("AIRPORT", "SECURITY", "")),
    ("suspicious package, Westchester County Airport", ("AIRPORT", "SECURITY", "")),
    ("hijack, aircraft on the ground at HPN", ("AIRPORT", "HIJACK", "")),
    ("respond to the airport, Signature ramp, for an investigation", ("AIRPORT", "", NOT_STATED)),
    ("HPN, airport incident", ("AIRPORT", "", NOT_STATED)),
    # not the airport
    ("activated fire alarm, 240 Airport Road, Rye Brook", None),         # the terminal's address alone: an alarm there
    ("structure fire 12 Airport Rd", None),
    ("engine fire, car fully involved on King Street", None),             # a car's engine
    ("water rescue, Byram River behind 100 Abendroth", None),             # no aircraft, no airport
    ("smoke in the cabin", None),
    ("MVA with injuries, level 2 trauma", None),
    ("", None),
])
def test_what_a_page_says_about_the_airport(text, want):
    assert airport.heard(text) == want


def test_the_souls_on_board_level():
    assert [airport.level(n) for n in (0, 1, 2, 3, 10, 11, 25, 26, 50, 51, 140)] == [None, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert airport.level(None) is None and airport.level("12") == 3 and airport.level("x") is None


@pytest.mark.parametrize("text, want", [
    ("Boeing 737 inbound with unsafe gear, 12 minutes out, runway 16, 140 souls on board, 12,000 pounds of fuel, no dangerous goods",
     {"acft_type": "Boeing 737", "acft_nature": "unsafe gear", "acft_eta": 12, "acft_runway": "16", "acft_souls": 140,
      "acft_level": 5, "acft_fuel": "12,000 lb", "acft_cargo": "none"}),
    ("Alert II, Gulfstream G550 declared an emergency, hydraulic failure, landing runway 34 in 8 minutes, 9 souls",
     {"acft_type": "Gulfstream G550", "acft_nature": "hydraulic failure", "acft_eta": 8, "acft_runway": "34", "acft_souls": 9, "acft_level": 2}),
    ("Alert 2 Level 3 at the airport, Cessna Citation, smoke in the cockpit",
     {"acft_type": "Cessna Citation", "acft_nature": "smoke in the cockpit", "acft_level": 3}),       # the Level as said
    ("Alert 2, 140 souls, Level 3", {"acft_souls": 140, "acft_level": 3}),                            # said beats derived
    ("alert level 2, Learjet 45, five minutes out, runway one six", {"acft_type": "Learjet 45", "acft_eta": 5, "acft_runway": "16"}),
    ("single engine Cessna 172 with a rough engine, 1 soul on board, two hours of fuel",
     {"acft_type": "Cessna 172", "acft_nature": "engine out", "acft_souls": 1, "acft_level": 1}),
    ("hijack, aircraft on the ground at HPN, 48 pax 4 crew", {"acft_souls": 52, "acft_level": 5}),
    ("airbus a320 inbound with an engine fire 15 minutes out, 162 souls, 18,000 lbs fuel",
     {"acft_type": "Airbus A320", "acft_nature": "engine fire", "acft_eta": 15, "acft_souls": 162, "acft_level": 5, "acft_fuel": "18,000 lb"}),
    ("a Beechcraft King Air 350 N350KA, 6 pob, 300 gallons of fuel, dangerous goods on board lithium batteries, 3 minutes out",
     {"acft_type": "Beechcraft King Air 350 N350KA", "acft_eta": 3, "acft_fuel": "300 gal", "acft_souls": 6, "acft_level": 2,
      "acft_cargo": "lithium batteries"}),
    ("helicopter down in Rye Lake, two persons on board", {"acft_type": "helicopter", "acft_souls": 2, "acft_level": 1}),
    ("plane inbound, fuel on board 4,500 pounds, souls on board 12, ETA 6 minutes", {"acft_fuel": "4,500 lb", "acft_souls": 12, "acft_level": 3, "acft_eta": 6}),
    ("fuel spill on the ramp at the airport", {}),
    ("activated fire alarm, 240 Airport Road", {}),
    ("", {}),
])
def test_the_towers_facts_as_the_page_says_them(text, want):
    assert airport.fields(text) == want


def test_the_one_line_summary_for_the_boards_and_the_export():
    inc = {"subtype": "ALERT-2", "acft_type": "Boeing 737", "acft_nature": "unsafe gear", "acft_eta": 12, "acft_runway": "16",
           "acft_souls": 140, "acft_level": 5, "acft_fuel": "12,000 lb", "acft_cargo": "none"}
    assert airport.summary(inc) == "Alert 2 · Boeing 737 · unsafe gear · 12 min · RWY 16 · 140 souls (Level 5) · 12,000 lb fuel · DG: none"
    assert airport.summary({"subtype": "ACFT-WATER", "acft_souls": 2}, "Aircraft in the water") == "Aircraft in the water · 2 souls (Level 1)"
    assert airport.summary({"subtype": "ALERT-3", "acft_level": 3}) == "Alert 3 · Level 3"
    assert airport.summary({"subtype": "ALERT-3"}) == "" and airport.summary({}) == ""


def test_an_airport_events_fields_are_stored_edited_logged_exported_and_old_events_have_none(tmp_path):
    c = make_cad(tmp_path)
    inc = c.create({"type": "AIRPORT", "subtype": "ALERT-2", "operator": "JD", "acft_type": " Boeing 737 ", "acft_nature": "unsafe gear",
                    "acft_eta": 12, "acft_runway": "rwy 16", "acft_souls": "140", "acft_fuel": "12,000 lb", "acft_cargo": "none"})
    assert (inc["acft_type"], inc["acft_nature"], inc["acft_eta"], inc["acft_runway"]) == ("Boeing 737", "unsafe gear", 12, "16")
    assert (inc["acft_souls"], inc["acft_level"], inc["acft_fuel"], inc["acft_cargo"]) == (140, 5, "12,000 lb", "none")
    assert inc["address"] == "240 Airport Rd"                               # no location given: the terminal
    # the Level follows the souls unless said; an edit of one field leaves the rest
    e = c.update(inc["id"], {"acft_souls": 8, "operator": "JD"})
    assert (e["acft_souls"], e["acft_level"], e["acft_type"]) == (8, 2, "Boeing 737")
    e = c.update(inc["id"], {"acft_souls": 8, "acft_level": 3, "operator": "JD"})
    assert (e["acft_souls"], e["acft_level"]) == (8, 3)
    e = c.update(inc["id"], {"acft_souls": None, "operator": "JD"})
    assert (e["acft_souls"], e["acft_level"]) == (None, 3)
    e = c.update(inc["id"], {"acft_eta": "", "acft_runway": "", "operator": "JD"})
    assert e["acft_eta"] is None and e["acft_runway"] == ""
    texts = [x["text"] for x in c.store.full(inc["id"])["events"]]
    assert any("souls on board: 8" in t and "level: 2" in t for t in texts)
    assert any("minutes out: (none)" in t and "runway: (none)" in t for t in texts)
    for bad in ({"acft_runway": "20"}, {"acft_eta": -1}, {"acft_souls": 1000}, {"acft_level": 6}, {"acft_eta": "soon"}, {"acft_type": 5}):
        with pytest.raises(CadError):
            c.update(inc["id"], {**bad, "operator": "JD"})
    # the state and the API carry them; an event that isn't the airport's has none
    st = c.state()
    me = next(i for i in st["incidents"] if i["id"] == inc["id"])
    assert me["acft_type"] == "Boeing 737" and me["acft_level"] == 3
    other = c.create({"type": "STRU", "subtype": "RESD", "address": "1 Main St", "operator": "JD"})
    assert (other["acft_type"], other["acft_eta"], other["acft_souls"], other["acft_level"]) == ("", None, None, None)
    # exported: one column at the end, the one-line summary; "" for the rest
    import csv, io
    rows = list(csv.reader(io.StringIO(c.export("incidents.csv").decode("utf-8-sig"))))
    assert rows[0][-1] == "Airport"
    by = {r[0]: r for r in rows[1:]}
    assert by[inc["number"]][-1] == "Alert 2 · Boeing 737 · unsafe gear · Level 3 · 12,000 lb fuel · DG: none"
    assert by[other["number"]][-1] == ""


def test_an_old_database_gets_the_airport_columns(tmp_path):
    from sizeup.cad.store import Store
    p = tmp_path / "cad.sqlite"
    s = Store(str(p))
    with s.tx() as db:
        for col, _ in __import__("sizeup.cad.store", fromlist=["AIRPORT_COLUMNS"]).AIRPORT_COLUMNS:
            db.execute(f"ALTER TABLE incidents DROP COLUMN {col}")
        db.execute("INSERT INTO incidents (type, subtype, source, status, created_at) VALUES ('AIRPORT', 'ALERT-2', 'manual', 'pending', 1)")
    s.db.close()
    s2 = Store(str(p))
    cols = {r["name"] for r in s2.db.execute("PRAGMA table_info(incidents)")}
    assert {"acft_type", "acft_eta", "acft_souls", "acft_level", "acft_cargo"} <= cols
    old = s2.one("SELECT * FROM incidents")
    assert old["acft_type"] == "" and old["acft_souls"] is None and old["acft_level"] is None     # old events have none
    s2.db.close()


def test_a_60_control_page_naming_the_airport_opens_an_airport_event_with_the_level():
    d = parse("Port Chester Fire Department, respond to Westchester County Airport for an Alert 2, "
              "aircraft inbound with an engine fire. Time out 1432.")
    assert (d.incident_type, d.category, d.type_phrase) == ("Airport alert", "fire", "alert 2")
    assert runcards.from_page(d.incident_type, d.category, d.text) == ("AIRPORT", "ALERT-2", "")
    assert runcards.from_page("Fire alarm", "fire", "fire alarm 240 Airport Road") == ("ALARM", "", "commercial or residential not stated on the page")
    assert runcards.from_page("Hazmat", "hazard", "fuel spill at Westchester County Airport") == ("AIRPORT", "FUEL", "")
    assert runcards.from_page("MVA", "rescue", "mva, Alert 3 at the airport") == ("AIRPORT", "ALERT-3", "")
    for text, phrase in (("Port Chester Fire, respond to the airport, full emergency, Cessna inbound. Time out 0900.", "full emergency"),
                         ("Port Chester Fire, respond to Rye Lake for an aircraft accident, plane in the water. Time out 0900.", "aircraft accident"),
                         ("Port Chester Fire, respond to the airport, aircraft declared an emergency, unsafe gear. Time out 0900.", "declared an emergency"),
                         ("Port Chester Fire, respond to the airport for an aircraft off the runway. Time out 0900.", "off the runway")):
        dd = parse(text)
        assert (dd.incident_type, dd.type_phrase) == ("Airport alert", phrase), text
    c = make_cad()
    _, inc = monitor_incident("Port Chester Fire Department, respond to Westchester County Airport for an Alert 2. Time out 1432.")
    p = c.from_monitor(inc)
    assert (p["status"], p["type"], p["subtype"], p["type_note"]) == ("queued", "AIRPORT", "ALERT-2", "")
    assert "type heard: Airport alert" in p["remarks"]
    assert (p["acft_type"], p["acft_souls"], p["acft_level"]) == ("", None, None)
    _, inc3 = monitor_incident("Port Chester Fire, respond to the airport, Alert three. Time out 0915.")
    q = c.from_monitor(inc3)
    assert (q["type"], q["subtype"]) == ("AIRPORT", "ALERT-3")
    _, inc4 = monitor_incident("Port Chester Fire, respond to 240 Airport Road, Rye Brook, for an activated fire alarm, commercial. Time out 0916.")
    assert (c.from_monitor(inc4)["type"], c.from_monitor(inc4)["subtype"]) == ("ALARM", "COMM")


@pytest.mark.parametrize("text, want_sub, want_note, want_fields", [
    # 2012: the engine-out standby
    ("Port Chester Fire Department, respond to Westchester County Airport for an Alert 2, full emergency, a Cessna Citation "
     "inbound with an engine out, 10 minutes out, landing runway 16, 7 souls on board. Time out 1412.",
     "ALERT-2", "", {"acft_type": "Cessna Citation", "acft_nature": "engine out", "acft_eta": 10, "acft_runway": "16", "acft_souls": 7, "acft_level": 2}),
    # 2021: off the runway
    ("Port Chester Fire, respond to Westchester County Airport, aircraft off the runway, runway 34, Gulfstream, no injuries reported. Time out 1550.",
     "DISABLED", "", {"acft_type": "Gulfstream", "acft_runway": "34"}),
    # 2023: the crash at Rye Lake
    ("Port Chester Fire Department, respond to Rye Lake off Lincoln Avenue for an aircraft accident, small plane in the water, "
     "two souls on board. Time out 1708.",
     "ACFT-WATER", "", {"acft_type": "small plane", "acft_souls": 2, "acft_level": 1}),
    # December 2025: the fuel-tanker rollover
    ("Port Chester Fire Department, respond to Westchester County Airport, Tower Road entrance, for a fuel tanker rollover on the "
     "field, hazmat condition, jet fuel leaking. Time out 0712.",
     "FUEL", "", {}),
    # the tower's full notification, every field
    ("Port Chester Fire Department, respond to Westchester County Airport for an Alert 2, full emergency, Level 4: a Boeing 737 "
     "inbound with unsafe gear, 12 minutes out, runway 16, 48 souls on board, 12,000 pounds of fuel, no dangerous goods. Time out 1432.",
     "ALERT-2", "", {"acft_type": "Boeing 737", "acft_nature": "unsafe gear", "acft_eta": 12, "acft_runway": "16", "acft_souls": 48,
                     "acft_level": 4, "acft_fuel": "12,000 lb", "acft_cargo": "none"}),
    # "Level 3" alone: the souls Level, the alert left for the dispatcher
    ("Port Chester Fire Department, respond to the airport, Level 3, Learjet inbound with smoke in the cabin, 5 minutes out. Time out 2101.",
     "", "smoke in the cabin: alert level not stated; Level 3 heard: the souls-on-board level, not an alert",
     {"acft_type": "Learjet", "acft_nature": "smoke in the cabin", "acft_eta": 5, "acft_level": 3}),
])
def test_pages_from_the_airports_incidents_open_events_with_the_facts_filled_in(text, want_sub, want_note, want_fields):
    c = make_cad()
    _, inc = monitor_incident(text)
    p = c.from_monitor(inc)
    assert (p["type"], p["subtype"], p["type_note"]) == ("AIRPORT", want_sub, want_note)
    got = {k: p[k] for k in airport.FIELDS if p[k] not in ("", None)}
    assert got == want_fields
    # a repeat of the page with more said fills what was empty, never overwrites
    _, again = monitor_incident(text + " 2 crew.")
    again.id = inc.id
    q = c.from_monitor(again)
    assert q["id"] == p["id"] and {k: q[k] for k in airport.FIELDS} == {k: p[k] for k in airport.FIELDS}


def test_the_airports_arff_units_resolve_when_heard_on_the_airport_talkgroup():
    """Airport 7 / 11 / 17 and Car 2591 (dept 259, data/westchester_apparatus.csv): a P25 talker alias "AP7" or
    "Airport 7" names the unit, so the boards show "Airport 7" talking on TG 333. Their P25 radio IDs aren't known:
    nothing guessed, the row is "heard" until a person maps it."""
    from sizeup.talkers import spoken
    c = make_cad()
    L = c.lists
    units = {u["name"]: u for u in c.store.units()}
    for name, said in (("AP7", "Airport 7"), ("AP11", "Airport 11"), ("AP17", "Airport 17"), ("2591", "25 91")):
        assert units[name]["spoken"] == said and units[name]["mutual_aid"] and units[name]["board"] == "ma-airport"
        assert units[name]["department"] == "Westchester County Airport" and units[name]["response"] == "mutual_aid"
        assert L.response(name) == "mutual_aid"
    assert c.radios.unit_for_alias("AP7") == ("AP7", "") and c.radios.unit_for_alias("Airport 7") == ("AP7", "")
    assert c.radios.unit_for_alias("AIRPORT 11 PORT 1") == ("AP11", "PORT 1") and c.radios.unit_for_alias("2591") == ("2591", "")
    assert c.radios.rig_p25([("4259007", "AP7")]) == "AP7" and c.radios.rig_p25([("4259007", "")]) == ""   # no ID guessed
    assert spoken("AP7") == "Airport 7" and spoken("Airport 11 Portable 1") == "Airport 11" and spoken("2591") == "2591"
    assert all(r["radio_id"] != "4259007" for r in c.radios.rows())
    assert not any(u["name"].startswith("AP") for u in runcards.recommend(c.store, "AIRPORT", "ALERT-3", None)["units"])


def test_the_airport_incident_talkgroup_links_to_the_one_open_airport_event(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    air = lambda tid, t, **kw: tx(tid, t, "Airport Incident", tg=333, unit="60 Ctrl OP 3", text="Port Chester stage at the EOC", **kw)
    assert c.record_traffic(air("333-a-call_1-t0", clock.t)) == []                   # no airport event open: nothing
    stru = c.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "JD"})
    clock.t += 5
    assert c.record_traffic(air("333-a-call_2-t0", clock.t)) == []                   # another type open: not that one
    a = c.create({"type": "AIRPORT", "subtype": "ALERT-2", "operator": "JD"})
    clock.t += 5
    assert c.record_traffic(air("333-a-call_3-t0", clock.t)) == [a["id"]]
    assert c.record_traffic(tx("307-a-call_4-t0", clock.t + 1, "Fire/EMS 15", tg=307, unit="60 Ctrl OP 3")) == []   # Fire/EMS 15: no
    assert c.record_traffic(tx("FG1-5", clock.t + 2)) == []                            # FG1 with nobody dispatched: no
    radio = {r["id"]: r["why"] for r in c.full(a["id"])["radio"]}
    assert radio == {"333-a-call_3-t0": "airport"} and why_text("airport") == "Airport Incident talkgroup, the open airport event"
    # a rig on the event keeps its unit link; a known radio on the talkgroup links too (the whole talkgroup is the incident's)
    c.dispatch(a["id"], {"unit_ids": [uid(c, "E60")], "operator": "JD"})
    clock.t += 5
    assert c.record_traffic(air("333-a-call_6-t0", clock.t, rig="E60", radio_id="6001")) == [a["id"]]
    assert {r["id"]: r["why"] for r in c.full(a["id"])["radio"]}["333-a-call_6-t0"] == "unit"
    assert c.record_traffic(air("333-a-call_6-t1", clock.t + 1, rig="L31", radio_id="3101")) == [a["id"]]
    assert {r["id"]: r["why"] for r in c.full(a["id"])["radio"]}["333-a-call_6-t1"] == "airport"
    # taken off by a person: not put back; two open airport events: can't tell whose
    code, raw, _ = CadApi(c).post(f"/api/cad/incident/{a['id']}/unlink", json.dumps({"traffic_id": "333-a-call_6-t1", "operator": "JD"}).encode())
    assert code == 200 and json.loads(raw)["excluded"]
    assert c.record_traffic(air("333-a-call_6-t1", clock.t + 1, rig="L31", radio_id="3101")) == []
    b = c.create({"type": "AIRPORT", "subtype": "ALERT-3", "operator": "JD"})
    clock.t += 5
    assert c.record_traffic(air("333-b-call_7-t0", clock.t)) == []
    c.close(b["id"], {"disposition": "CANCELLED", "operator": "JD"})
    clock.t += 5
    assert c.record_traffic(air("333-b-call_8-t0", clock.t)) == [a["id"]]
    c.close(a["id"], {"disposition": "COMPLETE", "operator": "JD"})
    clock.t += 5
    assert c.record_traffic(air("333-b-call_9-t0", clock.t)) == []                   # closed: nothing links any more
    assert c.record_traffic(air("333-b-call_0-t0", clock.t - 7)) == [a["id"]]        # a late transcript from while only a was open
    assert stru["id"] not in c.record_traffic(air("333-b-call_9-t1", clock.t))


def test_an_airport_event_with_no_location_is_placed_at_the_terminal():
    c = make_cad()
    a = c.create({"type": "AIRPORT", "subtype": "ALERT-2", "operator": "JD"})
    assert (a["address"], a["lat"], a["lon"], a["geo_label"]) == ("240 Airport Rd", 41.068787, -73.704333, "240 Airport Rd, Rye Brook")
    assert airport.near(a["lat"], a["lon"])
    b = c.create({"type": "AIRPORT", "subtype": "ALERT-3", "address": "Runway 16 end", "operator": "JD"})
    assert b["address"] == "Runway 16 end" and b["lat"] is None                      # a location given: kept as typed
    g = make_cad(geocoder=lambda q: [{"lat": 41.0, "lon": -73.7, "label": "elsewhere", "confidence": 0.99, "kind": "address"}])
    assert g.create({"type": "AIRPORT", "operator": "JD"})["geo_label"] == "240 Airport Rd, Rye Brook"   # not re-geocoded
    s = c.create({"type": "SERVICE", "subtype": "LIFT", "operator": "JD"})
    assert s["address"] == "" and s["lat"] is None
    u = c.update(s["id"], {"type": "AIRPORT", "subtype": "FUEL", "operator": "JD"})
    assert (u["address"], u["lat"]) == ("240 Airport Rd", 41.068787)
    v = c.update(b["id"], {"type": "AIRPORT", "subtype": "OTHER", "address": "", "operator": "JD"})     # emptied: the terminal
    assert (v["address"], v["lat"]) == ("240 Airport Rd", 41.068787)
    w = c.update(a["id"], {"address": "1 Airport Emergency Staging Way", "operator": "JD"})
    assert w["address"] == "1 Airport Emergency Staging Way" and w["lat"] is None     # moved by hand: as typed
