"""Settings: edits kept in the CAD database over data/cad_lists.json, the audit log, operators,
other agencies' units (attach only), export / import, and the v5 migration."""

import json
import sqlite3

import numpy as np
import pytest

from sizeup.cad import dispatcher, runcards
from sizeup.cad.service import Cad, Settings
from sizeup.cad.store import SCHEMA_VERSION, SETTINGS_SCHEMA, CadError, Store
from tests.test_cad import make_cad, names, server, uid  # noqa: F401  (server: the HTTP fixture)


def post(cad, section, **body):
    return cad.settings_post(section, dict(body, operator=body.get("operator", "JD")))


def item(cad, sec, code):
    return next(x for x in cad.settings_get()["lists"][sec]["items"] if x["code"] == code)


# -- defaults + overrides --------------------------------------------------------------------------------

def test_overrides_are_laid_over_the_defaults_and_reset():
    cad = make_cad()
    assert item(cad, "event_types", "STRU")["state"] == "default"
    post(cad, "event_types", action="edit", code="STRU", item={"color": "#123456", "name": "Structure fire (working)"})
    t = next(x for x in cad.lists.event_types if x["code"] == "STRU")
    assert t == {"code": "STRU", "name": "Structure fire (working)", "color": "#123456"}
    assert cad.state()["lists"]["event_types"][-1]["color"] == "#123456"            # pushed to the consoles
    assert item(cad, "event_types", "STRU")["state"] == "edited"
    assert cad.store.q("SELECT section, key FROM settings_overrides") == [{"section": "event_types", "key": "STRU"}]
    # an edit back to the default is no edit at all
    post(cad, "event_types", action="edit", code="STRU", item={"color": "#980000", "name": "Structure fire"})
    assert cad.store.q("SELECT * FROM settings_overrides") == [] and item(cad, "event_types", "STRU")["state"] == "default"
    # add, reorder, then reset the section
    post(cad, "dispositions", action="add", code="REFERRED", item={"name": "Referred to PD"})
    post(cad, "dispositions", action="move", code="REFERRED", to=0)
    assert cad.lists.disposition_codes[0] == "REFERRED" and cad.lists.name("dispositions", "REFERRED") == "Referred to PD"
    post(cad, "unit_statuses", action="move", code="ON LOCATION", dir=-1)
    assert cad.lists.status_ids[3:6] == ("ACKNOWLEDGED", "ON LOCATION", "RESPONDING")
    post(cad, "dispositions", action="reset_section")
    assert "REFERRED" not in cad.lists.disposition_codes and cad.lists.disposition_codes[0] == "CANCELLED"
    # codes never change; bad colors and duplicates are refused with the reason
    with pytest.raises(CadError, match="codes don't change"):
        post(cad, "event_types", action="edit", code="STRU", item={"code": "FIRE", "name": "x"})
    with pytest.raises(CadError, match="#rrggbb"):
        post(cad, "event_types", action="edit", code="STRU", item={"color": "red"})
    with pytest.raises(CadError, match="already"):
        post(cad, "event_types", action="add", code="STRU", item={"name": "x", "color": "#000000"})
    with pytest.raises(CadError, match="role 'free'"):
        post(cad, "unit_statuses", action="edit", code="AVAILABLE", item={"role": ""})   # one status must free a unit
    assert cad.lists.role("AVAILABLE") == "free"


def test_edits_persist_across_a_restart_and_survive_a_changed_data_file(tmp_path):
    cad = make_cad(tmp_path)
    post(cad, "event_types", action="edit", code="ALARM", item={"color": "#00FF00"})
    post(cad, "event_class", action="set", key="SERVICE/LIFT", value="non-emergency")
    post(cad, "geo_town_order", action="set", towns=["Rye Brook", "Port Chester", "Rye"])
    post(cad, "dispatch", action="set", key="channels", value=["FG1", "FG2"])
    post(cad, "dispatch", action="set", key="tones", value=[[800, 1], [1000, 2]])
    cad2 = make_cad(tmp_path)
    L = cad2.lists
    assert next(x for x in L.event_types if x["code"] == "ALARM")["color"] == "#00FF00"
    assert not L.is_emergency("SERVICE", "LIFT") and L.geo_town_order == ("Rye Brook", "Port Chester", "Rye")
    assert cad2.s.channels == ("FG1", "FG2") and cad2.s.tones == ((800.0, 1.0), (1000.0, 2.0))
    post(cad2, "dispatch", action="set", key="tones", value=[[[800, 1], [1000, 2]], [[349.1, 1], [311.1, 3]]])
    assert cad2.s.tones == (((800.0, 1.0), (1000.0, 2.0)), ((349.1, 1.0), (311.1, 3.0)))       # one group per department
    assert cad2.settings_get()["dispatch"]["tones"]["value"] == [[[800.0, 1.0], [1000.0, 2.0]], [[349.1, 1.0], [311.1, 3.0]]]
    assert cad2.state()["transmit"]["channels"] == ["FG1", "FG2"]
    post(cad2, "dispatch", action="reset", key="tones")
    assert cad2.s.tones == ((349.1, 1.0), (311.1, 3.0))
    # edits that no longer fit the data file (a code it dropped) are left out, said in the log, not lost
    cad2.store.db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES "
                          "('event_class', 'NOPE', '\"emergency\"', 0)")
    logged = []
    cad3 = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=logged.append)
    assert any("settings edits not applied" in m and "NOPE" in m for m in logged)
    assert next(x for x in cad3.lists.event_types if x["code"] == "ALARM")["color"] == "#CC0000"


def test_every_change_is_in_the_audit_log_with_who_did_it():
    cad = make_cad()
    post(cad, "event_types", action="edit", code="BRUSH", item={"name": "Brush / grass fire"}, operator="Corey")
    post(cad, "units", action="edit", code="E60", unit={"title": "First due"}, operator="Pat")
    cad.save_run_card({"type": "SERVICE/LIFT", "slots": [{"kind": "engine", "count": 1}], "operator": "Pat"})
    post(cad, "users", action="add", name="Dana", role="admin", operator="Pat")
    log = [r for r in cad.settings.audit_log() if r["section"] != "radios"]      # (the radio seed's own rows)
    assert [(r["operator"], r["section"], r["key"]) for r in log] == [
        ("Pat", "users", "Dana"), ("Pat", "run cards", "SERVICE/LIFT"), ("Pat", "units", "E60"),
        ("Corey", "lists", "event_types/BRUSH")]
    assert json.loads(log[3]["old"])["name"] == "Brush fire" and json.loads(log[3]["new"])["name"] == "Brush / grass fire"
    assert log[2]["old"] and json.loads(log[2]["new"])["title"] == "First due"
    assert [r["key"] for r in cad.settings_audit_log({"section": ["lists"]})["audit"]] == ["event_types/BRUSH"]


def test_retire_keeps_old_events_named_delete_only_when_unused():
    cad = make_cad()
    iid = cad.create({"type": "BRUSH", "received_via": "RADIO"})["id"]
    with pytest.raises(CadError, match="used by 1 event and run card BRUSH. Retire it instead"):
        post(cad, "event_types", action="delete", code="BRUSH")
    post(cad, "event_types", action="retire", code="BRUSH")
    L = cad.lists
    assert "BRUSH" not in L.type_codes and L.type_label("BRUSH") == "Brush fire"          # old events still read
    assert next(x for x in cad.state()["lists"]["event_types"] if x["code"] == "BRUSH")["retired"] is True
    with pytest.raises(CadError, match="retired"):
        cad.create({"type": "BRUSH"})
    assert cad.store.incident(iid)["type"] == "BRUSH"
    cad.close(iid, {"disposition": "FIRE"})
    post(cad, "event_types", action="restore", code="BRUSH")
    assert "BRUSH" in cad.lists.type_codes and item(cad, "event_types", "BRUSH")["state"] == "default"
    # unused: delete (a default one comes back with reset; an added one is simply gone)
    post(cad, "received_via", action="delete", code="WALK-IN")
    assert "WALK-IN" not in cad.lists.received_codes and item(cad, "received_via", "WALK-IN")["state"] == "deleted"
    post(cad, "received_via", action="reset", code="WALK-IN")
    assert "WALK-IN" in cad.lists.received_codes
    post(cad, "received_via", action="add", code="EMAIL", item={"name": "Email"})
    post(cad, "received_via", action="delete", code="EMAIL")
    assert "EMAIL" not in cad.lists.received_codes and cad.store.q("SELECT * FROM settings_overrides") == []
    with pytest.raises(CadError, match="used by"):
        post(cad, "received_via", action="delete", code="RADIO")
    # a status units are in can't be retired; a deleted subtype takes its phrases and rules with it
    with pytest.raises(CadError, match="units are Available now"):
        post(cad, "unit_statuses", action="retire", code="AVAILABLE")
    post(cad, "unit_statuses", action="retire", code="ACKNOWLEDGED")
    assert "ACKNOWLEDGED" not in cad.lists.status_ids and cad.lists.name("unit_statuses", "ACKNOWLEDGED") == "Acknowledged"
    with pytest.raises(CadError):
        cad.unit_status(uid(cad, "E60"), {"status": "ACKNOWLEDGED"})
    with pytest.raises(CadError, match="run card RESCUE/WATER"):
        post(cad, "event_subtypes", action="delete", code="WATER")
    post(cad, "event_subtypes", action="delete", code="NOTIF-PD")
    assert "*/NOTIF-PD" not in cad.lists.event_class and "NOTIF-PD" not in cad.lists.subtype_codes


# -- units: fire focus; other agencies attach only -------------------------------------------------------

def test_units_add_edit_move_retire():
    cad = make_cad()
    post(cad, "units", action="add", code="E65", unit={"spoken": "Engine 65", "kind": "engine", "board": "main"})
    e65 = next(u for u in cad.store.units() if u["name"] == "E65")
    assert e65["board"] == "main" and e65["kind"] == "engine" and e65["response"] == "fire" and e65["spoken"] == "Engine 65"
    post(cad, "units", action="move", code="E65", dir=-1)
    main = cad.lists.unit_boards[0]["units"]
    assert main.index("E65") == main.index("U42") - 1
    post(cad, "units", action="edit", code="E59", unit={"spare": False, "spoken": "Engine Fifty-nine"})
    assert cad.lists.unit("E59") ["spoken"] == "Engine Fifty-nine" and not cad.lists.unit("E59")["spare"]
    # retire: off the boards (history kept); not while on an event, not while a run card names it
    iid = cad.create({"type": "SERVICE"})["id"]
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E62")], "operator": "JD"})
    with pytest.raises(CadError, match="clear it first"):
        post(cad, "units", action="retire", code="E62")
    with pytest.raises(CadError, match="named on run card"):
        post(cad, "units", action="retire", code="2398")
    post(cad, "units", action="retire", code="E64")
    e64 = next(u for u in cad.store.units() if u["name"] == "E64")
    assert e64["board"] == "" and "E64" not in [c for c, _, _ in cad.lists.board_units()]
    assert next(r for r in cad.settings_get()["units"]["rows"] if r["code"] == "E64")["retired"]
    post(cad, "units", action="restore", code="E64")
    assert next(u for u in cad.store.units() if u["name"] == "E64")["board"] == "main"
    # delete only a unit with no history
    with pytest.raises(CadError, match="Retire it instead"):
        post(cad, "units", action="delete", code="E62")
    post(cad, "units", action="delete", code="E65")
    assert "E65" not in names(cad.store.units())
    post(cad, "units", action="reset_section")
    assert cad.store.q("SELECT * FROM settings_overrides") == []


def test_ems_and_pd_never_on_run_cards_tick_all_or_the_air_but_can_be_attached():
    cad = make_cad()
    L = cad.lists
    agency = {u["name"] for u in cad.store.units() if u["response"] == "agency"}
    assert {"77A1", "77M2", "7711", "PCPD", "RBPD1", "CONED-G", "DPW", "60CAD"} <= agency
    assert {"L25", "E191", "DPT266", "DPT239"} == {u["name"] for u in cad.store.units() if u["response"] == "mutual_aid"}
    assert not set(L.full_response_kinds) & set(L.attach_kinds)          # "Tick all" ticks these kinds only
    assert cad.lists.unit_boards[1]["id"] == "agencies"
    # run cards: not by kind, not by name
    for slots in ([{"kind": "ems", "count": 1}], [{"unit": "77A4"}], [{"kind": "engine|pd", "count": 1}]):
        with pytest.raises(CadError, match="never on a run card"):
            runcards.save(cad.store, "ALS", slots)
    with pytest.raises(CadError, match="never on a run card"):
        post(cad, "units", action="edit", code="2398", unit={"response": "agency"})
    for t in ("ALS", "BLS", "MVA", "STRU"):
        rec = runcards.recommend(cad.store, t)
        assert not {u["name"] for u in rec["units"]} & agency, t
    # a unit changed to "agency" stops being picked by kind
    post(cad, "units", action="edit", code="E60", unit={"response": "agency"})
    assert "E60" not in names(runcards.recommend(cad.store, "SERVICE")["units"])
    post(cad, "units", action="reset", code="E60")
    # never dispatched, previewed or upgraded with
    iid = cad.create({"type": "ALS", "address": "209 Westchester Ave"})["id"]
    a4 = uid(cad, "77A4")
    for fn in (cad.preview, cad.dispatch):
        with pytest.raises(CadError, match="never dispatched.*77A4"):
            fn(iid, {"unit_ids": [uid(cad, "E60"), a4], "operator": "JD"})
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    # attached when it shows up: "77A4 on location", not "dispatched"
    cad.attach(iid, {"unit_ids": [a4], "operator": "JD"})
    u = next(x for x in cad.store.units() if x["id"] == a4)
    assert u["status"] == "ON LOCATION" and u["incident_id"] == iid
    ev = [e["text"] for e in cad.store.full(iid)["events"]]
    assert "77A4 on location" in ev and not any("77A4" in t and "dispatched" in t for t in ev)
    assert not any(t.startswith("Attached") for t in ev)
    assert cad.store.full(iid)["dispatches"][0]["unit_ids"] == [uid(cad, "E60")]
    # an add-on doesn't say it on the air
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert "77" not in r["text"] and "Engine 60 dispatched" in r["text"]
    with pytest.raises(CadError, match="only On location or Available"):
        cad.unit_status(a4, {"status": "RESPONDING"})
    cad.unit_status(a4, {"status": "AVAILABLE", "operator": "JD"})
    assert "77A4 cleared" in [e["text"] for e in cad.store.full(iid)["events"]]
    assert cad.state()["lists"]["agency_statuses"] == ["ON LOCATION", "AVAILABLE"]


def test_mutual_aid_only_by_name_and_said_when_sent():
    cad = make_cad()
    assert "L25" not in names(runcards.recommend(cad.store, "STRU")["units"])
    runcards.save(cad.store, "STRU/COMM", [{"unit": "L25"}])
    assert names(runcards.recommend(cad.store, "STRU", "COMM")["units"]) == ["L25"]
    iid = cad.create({"type": "STRU", "subtype": "COMM", "address": "1 Main St"})["id"]
    assert "Ladder 25" in cad.dispatch(iid, {"unit_ids": [uid(cad, "L25")], "operator": "JD"})["text"]


# -- dispatch wording, tones -------------------------------------------------------------------------------

def test_dispatch_wording_edits_and_live_preview(monkeypatch):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(800, 0.1, np.float32))
    cad = make_cad()
    p = post(cad, "dispatch", action="preview", wording={"new": "{department}: {units}, go to {location} for {type}."})
    assert p["texts"]["new"] == ("Port Chester Fire: Engine 60, Ladder 31, Rescue 40, 23 98, go to 209 Westchester "
                                 "Avenue, cross of Pearl Street for a structure fire.")
    assert "Engine 61, added to the assignment" in p["texts"]["add_on"] and "upgrade on your last alarm" in p["texts"]["upgrade"]
    assert "full department response" in p["texts"]["full"]
    assert post(cad, "dispatch", action="preview", wording={"new": "{nope}"})["errors"] == {"new": "unknown or broken placeholder ('nope')"}
    with pytest.raises(CadError, match="placeholder"):
        post(cad, "dispatch", action="set", key="new", value="{units} respond to {where}")
    post(cad, "dispatch", action="set", key="full_response", value="all hands")
    post(cad, "dispatch", action="set", key="new", value="{department}, {units}, go to {location}, for {type}. Time out {time}.")
    iid = cad.create({"type": "SERVICE", "address": "209 Westchester Ave"})["id"]
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD", "full": True})
    assert r["text"].startswith("Port Chester Fire, all hands, go to 209 Westchester Avenue")
    assert cad.state()["lists"]["full_response"] == "all hands"
    with pytest.raises(CadError, match="50 to 4000 Hz"):
        post(cad, "dispatch", action="set", key="tones", value=[[10, 1]])
    page = cad.settings_get()["dispatch"]
    assert page["transmit"]["enabled"] is False and page["voice"]["name"] == "Samantha"
    assert next(w for w in page["wording"] if w["key"] == "new")["state"] == "edited"


def test_run_cards_reset_one_or_all_to_the_defaults():
    cad = make_cad()
    cad.save_run_card({"type": "STRU", "slots": [{"kind": "engine", "count": 5}], "operator": "JD"})
    cad.save_run_card({"type": "SERVICE/LIFT", "slots": [{"kind": "engine", "count": 1}]})
    assert not runcards.card(cad.store, "STRU")["is_default"]
    c = post(cad, "runcards", action="reset", type="STRU")
    assert c["is_default"] and c["slots"][0] == {"kind": "engine", "count": 3}
    with pytest.raises(CadError, match="delete it instead"):
        post(cad, "runcards", action="reset", type="SERVICE/LIFT")
    post(cad, "runcards", action="reset_section")
    assert runcards.card(cad.store, "SERVICE/LIFT") is None and all(c["is_default"] for c in runcards.cards(cad.store))


# -- users ---------------------------------------------------------------------------------------------------

def test_operators_add_edit_deactivate_and_the_picker():
    cad = make_cad()
    assert cad.state()["operators"] == [] and cad.settings_get()["operators"] == []
    a = post(cad, "users", action="add", name="Corey Smith", role="admin")
    b = post(cad, "users", action="add", name="Pat Jones")
    assert (a["role"], a["active"], b["role"]) == ("admin", True, "dispatcher")
    with pytest.raises(CadError, match="already"):
        post(cad, "users", action="add", name="corey smith")
    with pytest.raises(CadError, match="role"):
        post(cad, "users", action="add", name="X", role="chief")
    post(cad, "users", action="edit", id=b["id"], role="viewer", name="Pat J.")
    post(cad, "users", action="deactivate", id=a["id"])
    assert cad.state()["operators"] == ["Pat J."]
    assert [(o["name"], o["role"], o["active"]) for o in cad.settings_get()["operators"]] == [
        ("Pat J.", "viewer", True), ("Corey Smith", "admin", False)]
    cols = {r["name"] for r in cad.store.q("PRAGMA table_info(operators)")}
    assert cols == {"id", "name", "role", "active", "created_at", "updated_at"}     # sign-in later: add a column


# -- export / import --------------------------------------------------------------------------------------------

def test_settings_export_import_round_trip_with_a_dry_run(tmp_path):
    a = make_cad(tmp_path / "a")
    post(a, "event_types", action="edit", code="STRU", item={"color": "#111111"})
    post(a, "dispositions", action="add", code="REFERRED", item={"name": "Referred"})
    post(a, "units", action="add", code="E66", unit={"kind": "engine", "spoken": "Engine 66"})
    post(a, "units", action="retire", code="E64")
    post(a, "dispatch", action="set", key="channels", value=["FG1"])
    post(a, "users", action="add", name="Dana", role="admin")
    a.save_run_card({"type": "STRU", "slots": [{"kind": "engine", "count": 4}, {"unit": "E66"}]})
    data = json.loads(json.dumps(a.settings_export()))
    assert data["format"] == "sizeup-cad-settings" and data["defaults_version"] == "2026-10-07.2"

    b = make_cad(tmp_path / "b")
    dry = post(b, "import", action="dry_run", settings=data)
    assert dry["dry_run"] and {(c["section"], c["key"], c["change"]) for c in dry["changes"]} >= {
        ("lists", "STRU", "added"), ("units", "E66", "added"), ("run cards", "STRU", "changed"), ("users", "Dana", "added"),
        ("dispatch", "channels", "added")}
    assert b.store.q("SELECT * FROM settings_overrides") == [] and b.settings.operators() == []    # nothing yet
    post(b, "import", action="apply", settings=data)
    again = b.settings_export()
    for k in ("overrides", "run_cards", "operators"):
        assert again[k] == data[k], k
    assert "E66" in names(b.store.units()) and b.s.channels == ("FG1",)
    assert any(r["section"] == "system" and r["key"] == "import" for r in b.settings.audit_log())
    assert post(b, "import", action="dry_run", settings=data)["changes"] == []
    # checked before anything is replaced
    with pytest.raises(CadError, match="not a settings file"):
        post(b, "import", action="dry_run", settings={"overrides": {}})
    iid = b.create({"type": "SERVICE", "received_via": "RADIO"})["id"]
    bad = json.loads(json.dumps(data))
    bad["overrides"]["received_via"] = {"RADIO": {"deleted": True}}
    with pytest.raises(CadError, match="RADIO are used here"):
        post(b, "import", action="dry_run", settings=bad)
    bad = json.loads(json.dumps(data))
    bad["run_cards"] = [{"type": "ALS", "slots": [{"unit": "77A4"}]}]
    with pytest.raises(CadError, match="not a fire unit"):
        post(b, "import", action="dry_run", settings=bad)
    assert b.store.incident(iid)["received_via"] == "RADIO"


# -- the v5 migration ---------------------------------------------------------------------------------------------

CAD_V4_AUDIT = ("CREATE TABLE IF NOT EXISTS settings_audit (id INTEGER PRIMARY KEY, at REAL NOT NULL, operator TEXT, "
                "section TEXT NOT NULL, key TEXT, old TEXT, new TEXT)")


def test_v5_migration_is_idempotent_over_the_radio_branchs_v4_tables(tmp_path):
    path = tmp_path / "cad.sqlite"
    cad = make_cad(tmp_path)
    iid = cad.create({"type": "STRU"})["id"]
    db = cad.store.db
    for t in ("settings_overrides", "operators", "settings_audit"):
        db.execute(f"DROP TABLE {t}")
    db.execute(CAD_V4_AUDIT)                          # as the radio-ID work (v4) leaves it, with a row in it
    db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (1, 'JD', 'radios', 'mdc:0611/unit', '', 'E60')")
    db.execute("PRAGMA user_version = 4")
    st = Store(path)
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
    assert any("schema v5" in m for m in st.migrated)
    assert st.q("SELECT section, key, new FROM settings_audit") == [{"section": "radios", "key": "mdc:0611/unit", "new": "E60"}]
    assert st.incident(iid)["type"] == "STRU" and st.q("SELECT COUNT(*) AS n FROM radios")[0]["n"] >= 0
    for stmt in SETTINGS_SCHEMA.split(";"):               # again: nothing happens, nothing breaks
        if stmt.strip():
            st.db.execute(stmt)
    st._v4_to_v5(st.db)
    cols = [r["name"] for r in st.q("PRAGMA table_info(settings_audit)")]
    assert cols == ["id", "at", "operator", "section", "key", "old", "new"]
    st.db.execute("PRAGMA user_version = 3")         # an older file through the whole chain, twice
    Store(path)
    assert Store(path).db.execute("PRAGMA user_version").fetchone()[0] == 5
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SETTINGS_SCHEMA + SETTINGS_SCHEMA)
    assert make_cad().store.q("SELECT COUNT(*) AS n FROM settings_overrides")[0]["n"] == 0


# -- HTTP ----------------------------------------------------------------------------------------------------------

def test_settings_api(server):
    code, page = server.j("GET", "/api/cad/settings")
    assert code == 200 and {"lists", "units", "dispatch", "operators", "system", "event_class", "geo_town_order"} <= set(page)
    assert page["system"]["data_file"] == "data/cad_lists.json" and page["system"]["schema"] == 5
    assert page["lists"]["event_types"]["items"][0]["state"] == "default"
    code, r = server.j("POST", "/api/cad/settings/event_types", {"action": "edit", "code": "MVA", "item": {"color": "#0000FF"},
                                                                 "operator": "JD"})
    assert code == 200 and r == {"ok": True}
    assert server.j("POST", "/api/cad/settings/event_types", {"action": "edit", "code": "MVA", "item": {"color": "blue"}}) == \
        (400, {"error": "color: a color #rrggbb"})
    assert server.j("POST", "/api/cad/settings/nope", {"action": "x"})[0] == 400
    code, st = server.j("GET", "/api/cad/state")
    assert next(t for t in st["lists"]["event_types"] if t["code"] == "MVA")["color"] == "#0000FF"
    code, a = server.j("GET", "/api/cad/settings/audit?section=lists")
    assert code == 200 and a["audit"][0]["key"] == "event_types/MVA" and a["audit"][0]["operator"] == "JD"
    code, exp = server.j("GET", "/api/cad/settings/export")
    assert code == 200 and exp["overrides"] == {"event_types": {"MVA": {"name": "MVA", "color": "#0000FF"}}}
    code, dry = server.j("POST", "/api/cad/settings/import", {"action": "dry_run", "settings": exp})
    assert code == 200 and dry["changes"] == []
