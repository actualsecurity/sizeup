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
    assert L.subtypes_for("AIRPORT") == ("ALERT-1", "ALERT-2", "ALERT-3", "FUEL", "ACFT-FIRE", "OTHER")
    assert L.type_label("AIRPORT", "ALERT-2") == "Airport / Alert 2"
    assert L.name("event_subtypes", "ACFT-FIRE") == "Aircraft fire (ground)"
    assert L.phrase("AIRPORT", "ALERT-2") == "an Alert 2 at Westchester County Airport"
    assert L.phrase("AIRPORT", "ALERT-3") == "an Alert 3 at Westchester County Airport"
    assert L.phrase("AIRPORT", "ALERT-1") == "an Alert 1 at Westchester County Airport, standby"
    assert L.phrase("AIRPORT", "FUEL") == "a fuel spill at Westchester County Airport"
    assert L.phrase("AIRPORT") == "an incident at Westchester County Airport"
    assert L.is_emergency("AIRPORT", "ALERT-2") and L.is_emergency("AIRPORT")
    L.check_type("AIRPORT", "OTHER")
    with pytest.raises(ValueError, match="AIRPORT can't be COMM"):
        L.check_type("AIRPORT", "COMM")
    with pytest.raises(ValueError, match="STRU can't be ALERT-2"):
        L.check_type("STRU", "ALERT-2")
    # a proposal until the department confirms: marked like the other guesses
    assert "AIRPORT" in L.type_subtypes_partial["confirm"] and "AIRPORT" not in L.type_subtypes_partial["given"]
    assert next(t for t in L.event_types if t["code"] == "AIRPORT")["confirm"]
    for s in ("ALERT-1", "ALERT-2", "ALERT-3", "FUEL", "ACFT-FIRE"):
        assert next(x for x in L.event_subtypes if x["code"] == s)["confirm"].startswith("proposed")


def test_alert_2_and_3_run_cards_are_editable_defaults_with_remarks():
    c = make_cad()
    cards = {x["type"]: x for x in runcards.cards(c.store)}
    a2, a3 = cards["AIRPORT/ALERT-2"], cards["AIRPORT/ALERT-3"]
    assert a2["slots"] == [{"unit": "2398"}, {"all": "Port Chester FD"}] and a2["is_default"]
    assert a3["slots"] == [{"unit": "2398"}, {"all": "Port Chester FD"}] and a3["is_default"]
    assert a2["phrase"] == "an Alert 2 at Westchester County Airport" and a3["phrase"] == "an Alert 3 at Westchester County Airport"
    assert a2["remarks"].startswith("Staging: Airport Command Post / EOC, 1 Airport Emergency Staging Way")
    assert "Mutual aid" in a3["remarks"] and "Mutual aid" not in a2["remarks"]
    assert "Not yet PCFD policy" in a2["remarks"] and "Not yet PCFD policy" in a3["remarks"]
    assert cards["STRU"]["remarks"] == ""
    # the recommendation carries the remarks: the duty officer, then every PCFD apparatus
    iid = c.create({"type": "AIRPORT", "subtype": "ALERT-2", "operator": "JD"})["id"]
    r = runcards.recommend(c.store, "AIRPORT", "ALERT-2", iid)
    assert r["card"] == "AIRPORT/ALERT-2" and r["remarks"] == a2["remarks"] and r["full"] == "Port Chester FD"
    assert [u["name"] for u in r["units"]][:3] == ["2398", "2391", "2392"]
    assert runcards.recommend(c.store, "AIRPORT", "ALERT-1", iid)["card"] is None     # no card: PCFD not due
    assert runcards.recommend(c.store, "AIRPORT", "ALERT-1", iid)["remarks"] == ""
    # edited, reset, exported and imported, the remarks follow the card
    e = c.save_run_card({"type": "AIRPORT/ALERT-2", "slots": [{"kind": "engine", "count": 2}], "remarks": "  Stage at  the EOC. "})
    assert e["remarks"] == "Stage at the EOC." and not e["is_default"]
    assert runcards.recommend(c.store, "AIRPORT", "ALERT-2", iid)["remarks"] == "Stage at the EOC."
    with pytest.raises(CadError, match="remarks: text"):
        c.save_run_card({"type": "AIRPORT/ALERT-2", "slots": [{"kind": "engine", "count": 2}], "remarks": 7})
    out = c.settings_export()
    assert next(x for x in out["run_cards"] if x["type"] == "AIRPORT/ALERT-2")["remarks"] == "Stage at the EOC."
    assert runcards.reset(c.store, "AIRPORT/ALERT-2")["remarks"] == a2["remarks"]
    assert runcards.save(c.store, "AIRPORT/ALERT-3", a3["slots"], remarks="x" * 400)["remarks"] == "x" * runcards.MAX_REMARKS
    d = make_cad()
    r = d.settings_post("import", {"action": "dry_run", "settings": out, "operator": "JD"})
    assert [x for x in r["changes"] if x["key"] == "AIRPORT/ALERT-2"][0]["new"]["remarks"] == "Stage at the EOC."
    assert d.settings_post("import", {"action": "apply", "settings": out, "operator": "JD"})["ok"]
    assert runcards.card(d.store, "AIRPORT/ALERT-2")["remarks"] == "Stage at the EOC."
    assert d.settings_post("import", {"action": "dry_run", "settings": out, "operator": "JD"})["changes"] == []


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

@pytest.mark.parametrize("text, want", [
    ("Port Chester Fire, respond to Westchester County Airport for an Alert 2", ("AIRPORT", "ALERT-2", "")),
    ("alert two at the airport, Rye Brook", ("AIRPORT", "ALERT-2", "")),
    ("Alert 3, aircraft down, runway 16", ("AIRPORT", "ALERT-3", "")),
    ("plane crash off the end of runway 34", ("AIRPORT", "ALERT-3", "")),
    ("ALERT-1 standby, HPN tower reports gear problem", ("AIRPORT", "ALERT-1", "")),
    ("fuel spill on the ramp at Westchester County Airport", ("AIRPORT", "FUEL", "")),
    ("aircraft fire on the ground, county airport", ("AIRPORT", "ACFT-FIRE", "")),
    ("respond to the airport, Signature ramp, for an investigation", ("AIRPORT", "", "airport named on the page: alert level not stated")),
    ("HPN, airport incident", ("AIRPORT", "", "airport named on the page: alert level not stated")),
    ("activated fire alarm, 240 Airport Road, Rye Brook", None),         # the terminal's address alone: an alarm there
    ("structure fire 12 Airport Rd", None),
    ("", None),
])
def test_what_a_page_says_about_the_airport(text, want):
    assert airport.heard(text) == want


def test_a_60_control_page_naming_the_airport_opens_an_airport_event_with_the_level():
    d = parse("Port Chester Fire Department, respond to Westchester County Airport for an Alert 2, "
              "aircraft inbound with an engine fire. Time out 1432.")
    assert (d.incident_type, d.category, d.type_phrase) == ("Airport alert", "fire", "alert 2")
    assert runcards.from_page(d.incident_type, d.category, d.text) == ("AIRPORT", "ALERT-2", "")
    assert runcards.from_page("Fire alarm", "fire", "fire alarm 240 Airport Road") == ("ALARM", "", "commercial or residential not stated on the page")
    assert runcards.from_page("Hazmat", "hazard", "fuel spill at Westchester County Airport") == ("AIRPORT", "FUEL", "")
    assert runcards.from_page("MVA", "rescue", "mva, Alert 3 at the airport") == ("AIRPORT", "ALERT-3", "")
    c = make_cad()
    _, inc = monitor_incident("Port Chester Fire Department, respond to Westchester County Airport for an Alert 2. Time out 1432.")
    p = c.from_monitor(inc)
    assert (p["status"], p["type"], p["subtype"], p["type_note"]) == ("queued", "AIRPORT", "ALERT-2", "")
    assert "type heard: Airport alert" in p["remarks"]
    _, inc3 = monitor_incident("Port Chester Fire, respond to the airport, Alert three. Time out 0915.")
    q = c.from_monitor(inc3)
    assert (q["type"], q["subtype"]) == ("AIRPORT", "ALERT-3")
    _, inc4 = monitor_incident("Port Chester Fire, respond to 240 Airport Road, Rye Brook, for an activated fire alarm, commercial. Time out 0916.")
    assert (c.from_monitor(inc4)["type"], c.from_monitor(inc4)["subtype"]) == ("ALARM", "COMM")


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
