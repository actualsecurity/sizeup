"""The department's own codes (its code sheet, 2026-10-07): unit statuses AQ ... OS, every unit by
department, the MA and misc codes, the strict type -> subtype map, and the v10 migration of a database
written with the old status codes."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sizeup.cad import lists, runcards
from sizeup.cad.store import SCHEMA_VERSION, V10_STATUS, CadError, Store, v10_layout
from tests.test_cad import make_cad, monitor_incident, names, texts, uid
from tests.test_settings import item, post

LIVE_DB = Path(__file__).resolve().parent.parent.parent / "live" / "state" / "cad.sqlite"
OLD = {v: k for k, v in V10_STATUS.items()}          # AV -> AVAILABLE ...


def dump(db) -> str:
    return hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()


# -- statuses ----------------------------------------------------------------------------------------------

def test_status_roles_and_the_next_steps():
    cad = make_cad()
    L = cad.lists
    assert L.free == ("AQ", "AV", "FR", "NR", "CN", "CE") and L.status_for("free") == "AV"
    assert L.next_statuses("DP") == L.next_statuses("AK") == ("RS", "OL", "ST", "AV", "CN", "OS")
    assert L.next_statuses("RS") == L.next_statuses("ET") == ("OL", "ST", "AV", "CE", "OS")
    assert L.next_statuses("ST") == ("OL", "AV", "OS")
    assert L.next_statuses("OL") == ("IC", "AV", "OS") and L.next_statuses("IC") == ("OL", "AV", "OS")
    for other in ("AQ", "OA", "CB", "DO", "FR", "UNMANNED"):
        assert L.next_statuses(other) == ("AV", "OS")
    # Cancelled clears the unit off its call like Available, logged as itself; back in service.
    iid = cad.create({"type": "ALARM", "subtype": "COMM", "address": "1 Main St"})["id"]
    e60, e61, l31 = uid(cad, "E60"), uid(cad, "E61"), uid(cad, "L31")
    cad.dispatch(iid, {"unit_ids": [e60, e61, l31], "operator": "JD"})
    cad.unit_status(e60, {"status": "CN", "operator": "JD"})
    cad.unit_status(e61, {"status": "ET", "operator": "JD"})
    cad.unit_status(l31, {"status": "IC", "operator": "JD"})
    inc = cad.store.full(iid)
    a = {x["name"]: x for x in inc["assignments"]}
    assert a["E60"]["cleared_at"] and a["E61"]["enroute_at"] and a["L31"]["onscene_at"]
    t = [e["text"] for e in inc["events"]]
    assert "E60 cancelled" in t and "E61 enroute to" in t and "Command: L31" in t      # one commander: tests/test_assigned_panel.py
    assert cad.store.unit(e60)["status"] == "CN" and cad.store.unit(e60)["incident_id"] is None
    runcards.save(cad.store, "SERVICE", [{"kind": "engine", "count": 10}])
    rec = runcards.recommend(cad.store, "SERVICE", "", cad.create({"type": "SERVICE"})["id"])
    assert "E60" in names(rec["units"])                                   # cancelled: free to go again
    # Closing the call sets free_status (AV); out of service keeps its reason; UNMANNED is still there.
    cad.close(iid, {"disposition": "FALSE", "operator": "JD"})
    assert cad.store.unit(l31)["status"] == "AV" and cad.store.unit(e61)["status"] == "AV"
    cad.unit_status(e61, {"status": "OS", "note": "pump", "operator": "JD"})
    cad.unit_status(l31, {"status": "UNMANNED", "note": "no crew", "operator": "JD"})
    assert (cad.store.unit(e61)["out_of_service"], cad.store.unit(l31)["out_of_service"]) == ("pump", "no crew")
    # The add-on says the units already there by these statuses.
    assert L.already_statuses == ("DP", "AK", "RS", "ST", "OL")


def test_status_colors_are_readable_on_the_dark_theme():
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)
    for s in lists.load().unit_statuses:
        fill = "#" + "".join(f"{round(int(s['bg'][i:i + 2], 16) * 0.88):02X}" for i in (1, 3, 5))   # cad.css mini.on
        assert ratio(fill, s["fg"]) >= 4.5, s["code"]
        mix = lambda p, other: "#" + "".join(                          # color-mix(in srgb, bg p, other)
            f"{round(int(s['bg'][i:i + 2], 16) * p + int(other[i:i + 2], 16) * (1 - p)):02X}" for i in (1, 3, 5))
        assert ratio(mix(0.62, "#FFFFFF"), mix(0.16, "#1F2328")) >= 4.5, s["code"]   # the outline chip's text on --raised


# -- units and boards ------------------------------------------------------------------------------------

def test_ems_and_agencies_are_never_dispatched_and_mutual_aid_never_by_kind():
    cad = make_cad()
    L = cad.lists
    iid = cad.create({"type": "STRU", "address": "1 Main St"})["id"]
    for code in ("77A1", "7713", "DPT7700", "KEYHOLDER", "KNOB_BOX1", "CONED-E5", "BAT12", "SPCON", "WCPD1"):
        with pytest.raises(CadError, match="attach, don't dispatch"):
            cad.dispatch(iid, {"unit_ids": [uid(cad, "E60"), uid(cad, code)], "operator": "JD"})
    # By kind: only PCFD's and Rye Brook's apparatus, whatever the count.
    runcards.save(cad.store, "SERVICE", [{"kind": "engine", "count": 10}, {"kind": "ladder|tower", "count": 10},
                                         {"kind": "rescue", "count": 10}, {"kind": "utility", "count": 10},
                                         {"kind": "marine", "count": 10}])
    r = runcards.recommend(cad.store, "SERVICE", "", cad.create({"type": "SERVICE"})["id"])
    depts = {cad.lists.unit(u["name"])["department"] for u in r["units"]}
    assert depts == {"", "Rye Brook"} and "E191" not in names(r["units"]) and "TL24" not in names(r["units"])
    # ... "other" and the MA requests can't be asked for by kind at all; named, they go.
    for kind in ("other", "ma-request", "ems"):
        with pytest.raises(CadError):
            runcards.save(cad.store, "MVA", [{"kind": kind, "count": 1}])
    runcards.save(cad.store, "STRU/COMM", [{"kind": "engine", "count": 1}, {"unit": "TL24"}, {"unit": "239_Twl"}])
    r = runcards.recommend(cad.store, "STRU", "COMM", iid)
    assert names(r["units"]) == ["E58", "TL24", "239_Twl"]
    d = cad.dispatch(iid, {"unit_ids": [u["id"] for u in r["units"]], "operator": "JD"})
    assert "Engine 58, Tower Ladder 24, 239_Twl" in d["text"]
    # The board: main grouped by kind; everything else folded until opened.
    pub = cad.state()["lists"]["unit_boards"]
    assert [b.get("folded", False) for b in pub][:3] == [False, True, True] and pub[0]["group_by"] == "kind"
    assert {u["name"]: u["board"] for u in cad.store.units()}["E191"] == "ma-rye"
    assert L.response("2421") == "mutual_aid" and L.unit("2421")["kind"] == "chief"


def test_codes_show_as_themselves_until_named():
    cad = make_cad()
    L = cad.lists
    assert L.name("unit_statuses", "ZZ") == "ZZ" and L.name("event_subtypes", "NOPE") == "NOPE"
    built = lists.build(dict(lists.read(), unit_statuses=lists.read()["unit_statuses"] + [{"code": "XX", "role": "free"}]), "t")
    assert built.name("unit_statuses", "XX") == "XX"                       # a status with no name: its code
    rows = {r["code"]: r for r in cad.settings_get()["units"]["rows"]}
    assert not rows["DPT239"]["named"] and rows["DPT239"]["spoken"] == "" and rows["E58"]["named"]
    assert not rows["242_eNG"]["named"] and not rows["KNOB_BOX1"]["named"] and rows["2398"]["named"]
    st = {u["name"]: u for u in cad.state()["units"]}
    assert st["DPT239"]["spoken"] == "DPT239" and st["MA3"]["title"] == ""
    # Named in Settings: no longer to confirm; codes the department wrote in mixed case are found in any case.
    post(cad, "units", action="edit", code="dpt239", unit={"title": "Department page"})
    post(cad, "units", action="edit", code="242_ENG", unit={"spoken": "242 engine"})
    rows = {r["code"]: r for r in cad.settings_get()["units"]["rows"]}
    assert rows["DPT239"]["named"] and rows["242_eNG"]["named"] and rows["242_eNG"]["spoken"] == "242 engine"
    assert "242_ENG" not in rows
    with pytest.raises(CadError, match="already a unit"):
        post(cad, "units", action="add", code="242_ENG", unit={"kind": "agency"})


def test_guessed_status_roles_are_marked_until_confirmed():
    cad = make_cad()
    page = cad.settings_get()
    st = {x["code"]: x for x in page["lists"]["unit_statuses"]["items"]}
    assert st["CN"]["confirm"].startswith("role guessed") and "confirm" not in st["AV"]
    assert st["UNMANNED"]["confirm"] == "not in the department's list: keep or retire?"
    assert item(cad, "event_subtypes", "ECHO")["confirm"] == "meaning not given"
    post(cad, "unit_statuses", action="edit", code="CN", item={})          # Confirm: right as it is
    assert "confirm" not in item(cad, "unit_statuses", "CN") and cad.lists.role("CN") == "free"
    assert item(cad, "unit_statuses", "CN")["state"] == "edited"
    post(cad, "unit_statuses", action="edit", code="OA", item={"role": "oos"})   # changed: confirmed too
    assert "confirm" not in item(cad, "unit_statuses", "OA") and cad.lists.role("OA") == "oos"
    post(cad, "unit_statuses", action="reset", code="CN")                   # back to the guess, marked again
    assert item(cad, "unit_statuses", "CN")["confirm"]


# -- the type -> subtype map (strict) ------------------------------------------------------------------------

LIVE_CARDS = ("STRU", "ALARM/COMM", "ALARM/RESD", "ALARM/CO-COMM", "ALARM/CO-RESD", "HAZARD/GAS", "HAZARD/WIRES", "BRUSH",
              "MVA", "MVA/EXTRI", "RESCUE/WATER", "SERVICE", "ALS", "BLS", "MA", "INVEST")     # the live database's cards


def test_every_type_is_mapped_and_only_its_subtypes_are_offered():
    L = lists.load()
    assert L.map_gaps() == [] and set(L.type_subtypes) == set(L.type_codes)
    for t, subs in L.type_subtypes.items():
        assert subs == L.subtypes_for(t) and all(s in L.subtype_codes for s in subs)     # listed: real and active
    assert L.subtypes_for("ALARM") == ("COMM", "RESD", "CO-COMM", "CO-RESD", "GAS", "MED", "TROUB")
    assert L.retired("event_subtypes", "GAS-ATMOS") and L.name("event_subtypes", "GAS-ATMOS") == "Atmospheric gas alarm"
    assert L.phrase("ALARM", "GAS-ATMOS") == "an atmospheric gas alarm"          # still said on an old event
    with pytest.raises(ValueError, match="retired"):
        L.check_type("ALARM", "GAS-ATMOS")
    assert L.subtypes_for("ALS") == ("ECHO",)
    L.check_type("ALS")
    L.check_type("ALS", "ECHO")
    with pytest.raises(ValueError, match=r"ALS can't be BOMB: ECHO or none"):
        L.check_type("ALS", "BOMB")
    for t in ("BLS", "BRUSH", "DRILL", "RECALL"):
        assert L.subtypes_for(t) == ()
        L.check_type(t)
        with pytest.raises(ValueError, match="takes no subtype"):
            L.check_type(t, "ECHO")
    assert L.subtypes_for("MVA") == ("EXTRI", "INJURIES", "MULTINJU", "NOINJ")
    assert L.subtypes_for("STRU") == ("COMM", "RESD")
    with pytest.raises(ValueError, match="STRU can't be FIRE"):
        L.check_type("STRU", "FIRE")
    with pytest.raises(ValueError, match="'NOPE' is not an event subtype"):
        L.check_type("STRU", "NOPE")
    for t, s in (("HAZARD", "GAS-LEAK"), ("HAZARD", "GAS-LINE"), ("INVEST", "GAS-ODOR"), ("INVEST", "GAS-OUT"),
                 ("MVF", "TRUCK"), ("NOTIF", "NOTIF-PD"), ("RESCUE", "ELEVATOR"), ("SERVICE", "LIFT")):
        L.check_type(t, s)
    for t, s in (("HAZARD", "GAS-ODOR"), ("INVEST", "GAS-LEAK"), ("SERVICE", "ELEVATOR"), ("RESCUE", "LIFT")):
        with pytest.raises(ValueError):
            L.check_type(t, s)
    # Department-given vs proposed: Settings marks the proposals "to confirm".
    tsp = L.type_subtypes_partial
    assert tsp["given"] == ["ALARM", "ALS", "BLS", "BRUSH", "DRILL", "HAZARD", "INVEST", "MA", "MVA"]
    assert set(tsp["confirm"]) == {"MVA", "MVF", "NOTIF", "RECALL", "RESCUE", "SERVICE", "STRU"}
    # Every default card fits the map, and so does every card on the live database.
    for c in L.run_cards:
        L.check_key(c["type"])
    for k in LIVE_CARDS:
        L.check_key(k)
    # What a 60 Control page maps to fits too.
    for t, s in (("ALARM", "RESD"), ("ALARM", "COMM"), ("ALARM", "CO-COMM"), ("ALARM", "CO-RESD"), ("HAZARD", "GAS"),
                 ("HAZARD", "WIRES"), ("HAZARD", "HAZMAT"), ("MVF", "TRUCK"), ("MVF", "CAR"), ("MVA", "EXTRI"),
                 ("RESCUE", "WATER")):
        L.check_type(t, s)


def test_the_server_rejects_a_pair_the_map_does_not_allow():
    cad = make_cad()
    with pytest.raises(CadError, match=r"ALS can't be BOMB"):
        cad.create({"type": "ALS", "subtype": "BOMB", "address": "1 Main St"})
    with pytest.raises(CadError, match="BRUSH can't be COMM: takes no subtype"):
        cad.create({"type": "BRUSH", "subtype": "COMM"})
    iid = cad.create({"type": "ALS", "address": "1 Main St"})["id"]
    with pytest.raises(CadError, match="ALS can't be BOMB"):
        cad.update(iid, {"subtype": "BOMB", "operator": "JD"})
    with pytest.raises(CadError, match="BOMB"):                    # a new type with a subtype it doesn't allow
        cad.update(iid, {"type": "BLS", "subtype": "BOMB"})
    assert cad.update(iid, {"type": "HAZARD", "subtype": "BOMB"})["subtype"] == "BOMB"
    with pytest.raises(CadError, match="ALARM can't be BOMB"):     # type changed, subtype kept: checked as a pair
        cad.update(iid, {"type": "ALARM"})
    # The console gets the map: ALARM offers its list, BRUSH none.
    ts = cad.state()["lists"]["type_subtypes"]
    assert ts["ALARM"] == ["COMM", "RESD", "CO-COMM", "CO-RESD", "GAS", "MED", "TROUB"] and ts["BRUSH"] == []
    # Run cards: a pair the map doesn't allow is refused.
    with pytest.raises(CadError, match="run card ALS/BOMB: ALS can't be BOMB"):
        cad.save_run_card({"type": "ALS/BOMB", "slots": [{"kind": "engine", "count": 1}], "operator": "JD"})
    with pytest.raises(CadError, match="can't be"):
        cad.save_run_card({"type": "ALS/BOMB", "rename_from": "ALS", "slots": [{"kind": "engine", "count": 1}]})
    assert cad.save_run_card({"type": "ALS/ECHO", "slots": [{"kind": "engine", "count": 1}]})["type"] == "ALS/ECHO"
    have = {c["type"] for c in cad.run_cards()}
    assert set(LIVE_CARDS) - {"MA", "INVEST"} <= have          # every default card seeded (all fit)


def test_an_old_event_with_a_pair_off_the_map_still_works(tmp_path):
    cad = make_cad(tmp_path)
    iid = cad.create({"type": "STRU", "address": "1 Main St", "operator": "JD"})["id"]
    with cad.store.tx() as db:                                      # as the live database has it (STRU/FIRE)
        db.execute("UPDATE incidents SET subtype = 'FIRE' WHERE id = ?", (iid,))
    cad2 = make_cad(tmp_path)
    inc = next(i for i in cad2.state()["incidents"] if i["id"] == iid)
    assert (inc["type"], inc["subtype"]) == ("STRU", "FIRE")
    assert cad2.lists.type_label("STRU", "FIRE") == "Structure fire / Fire"
    cad2.update(iid, {"type": "STRU", "subtype": "FIRE", "cross_street": "Pearl St", "remarks": "smoke showing", "operator": "JD"})
    cad2.note(iid, {"text": "2nd alarm requested", "operator": "JD"})
    cad2.dispatch(iid, {"unit_ids": [uid(cad2, "E60")], "operator": "JD"})
    cad2.upgrade(iid, {"info": "fire through the roof", "operator": "JD"})          # no new type: kept
    with pytest.raises(CadError, match="MVF can't be FIRE"):          # a change has to fit
        cad2.update(iid, {"type": "MVF", "remarks": "car fire"})
    assert cad2.store.incident(iid)["remarks"] == "smoke showing"                     # nothing of it saved
    assert cad2.update(iid, {"subtype": "COMM"})["subtype"] == "COMM"
    with cad2.store.tx() as db:
        db.execute("UPDATE incidents SET subtype = 'FIRE' WHERE id = ?", (iid,))
    closed = cad2.close(iid, {"disposition": "FIRE", "operator": "JD"})
    assert (closed["status"], closed["type"], closed["subtype"]) == ("closed", "STRU", "FIRE")
    assert texts(cad2, iid)[-1].startswith("Event closed")
    # An old card under a pair off the map: its units can still be edited, the key can't be made again.
    with cad2.store.tx() as db:
        db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES ('SERVICE/BOAT', '', 0, 99)")
    assert cad2.save_run_card({"type": "SERVICE/BOAT", "slots": [{"kind": "engine", "count": 2}]})["slots"][0]["count"] == 2
    with pytest.raises(CadError, match="SERVICE can't be BOAT"):
        cad2.save_run_card({"type": "SERVICE/BOAT", "rename_from": "SERVICE", "slots": [{"kind": "engine", "count": 1}]})


def test_a_60_control_page_with_a_pair_off_the_map_keeps_the_type(monkeypatch):
    cad = make_cad()
    _, inc = monitor_incident("Port Chester Fire, respond to a residential fire alarm at 5 King Street")
    monkeypatch.setattr(runcards, "from_page", lambda *a: ("ALS", "BOMB", ""))
    p = cad.from_monitor(inc)
    assert (p["status"], p["type"], p["subtype"]) == ("queued", "ALS", "")
    assert p["type_note"] == "subtype heard as BOMB, not one for ALS: pick the subtype"
    _, inc2 = monitor_incident("Port Chester Fire, respond to a residential fire alarm at 6 King Street")
    monkeypatch.setattr(runcards, "from_page", lambda *a: ("VOLCANO", "BOMB", ""))
    q = cad.from_monitor(inc2)
    assert (q["type"], q["subtype"], q["type_note"]) == ("", "", "type heard: Fire alarm")
    a = cad.accept(p["id"], {"operator": "JD"})
    assert (a["status"], a["type"], a["subtype"]) == ("pending", "ALS", "")


def test_settings_edit_the_map_audited():
    cad = make_cad()
    rows = {r["type"]: r for r in cad.settings_get()["type_subtypes"]["rows"]}
    assert rows["ALARM"]["source"] == "given" and "confirm" not in rows["ALARM"] and rows["ALARM"]["state"] == "default"
    assert rows["STRU"]["source"] == "proposed" and rows["STRU"]["confirm"] and rows["STRU"]["subtypes"] == ["COMM", "RESD"]
    assert rows["MVA"]["source"] == "given" and "NOINJ" in rows["MVA"]["confirm"]
    assert cad.settings_get()["type_subtypes"]["gaps"] == []
    # Confirm STRU as it is: kept (no longer marked), audited.
    post(cad, "type_subtypes", action="set", type="STRU", subtypes=["COMM", "RESD"])
    rows = {r["type"]: r for r in cad.settings_get()["type_subtypes"]["rows"]}
    assert rows["STRU"]["state"] == "edited" and "confirm" not in rows["STRU"]
    # BRUSH gets OUTSIDE; ALS loses ECHO.
    post(cad, "type_subtypes", action="set", type="BRUSH", subtypes=["OUTSIDE"], operator="Pat")
    post(cad, "type_subtypes", action="set", type="ALS", subtypes=[])
    assert cad.lists.subtypes_for("BRUSH") == ("OUTSIDE",) and cad.lists.subtypes_for("ALS") == ()
    assert cad.state()["lists"]["type_subtypes"]["BRUSH"] == ["OUTSIDE"]
    cad.create({"type": "BRUSH", "subtype": "OUTSIDE"})
    with pytest.raises(CadError, match="ALS can't be ECHO"):
        cad.create({"type": "ALS", "subtype": "ECHO"})
    log = [(r["operator"], r["section"], r["key"], r["old"], r["new"]) for r in cad.settings.audit_log("lists")]
    assert log[:3] == [("JD", "lists", "type_subtypes/ALS", '["ECHO"]', "[]"),
                       ("Pat", "lists", "type_subtypes/BRUSH", "[]", '["OUTSIDE"]'),
                       ("JD", "lists", "type_subtypes/STRU", '["COMM","RESD"]', '["COMM","RESD"]')]
    # Checked: real codes, not retired, and no run card left for a pair the map drops.
    with pytest.raises(CadError, match="NOPE not in the event subtypes"):
        post(cad, "type_subtypes", action="set", type="ALS", subtypes=["NOPE"])
    with pytest.raises(CadError, match="GAS-ATMOS retired"):
        post(cad, "type_subtypes", action="set", type="ALARM", subtypes=["COMM", "GAS-ATMOS"])
    with pytest.raises(CadError, match="run cards ALARM/CO-COMM, ALARM/CO-RESD would no longer fit"):
        post(cad, "type_subtypes", action="set", type="ALARM", subtypes=["COMM", "RESD"])
    # Reset: the data file's again.
    post(cad, "type_subtypes", action="reset", type="ALS")
    assert cad.lists.subtypes_for("ALS") == ("ECHO",)
    assert cad.settings.export()["overrides"]["type_subtypes"] == {"BRUSH": ["OUTSIDE"], "STRU": ["COMM", "RESD"]}
    post(cad, "type_subtypes", action="reset_section")
    assert cad.lists.subtypes_for("BRUSH") == () and "type_subtypes" not in cad.settings.overrides()


def test_a_type_added_in_settings_takes_no_subtype_until_mapped():
    cad = make_cad()
    post(cad, "event_types", action="add", code="WATERCOND", item={"name": "Water condition", "color": "#3366CC"})
    assert cad.lists.subtypes_for("WATERCOND") == () and "WATERCOND" not in cad.state()["lists"]["type_subtypes"]
    assert cad.settings_get()["type_subtypes"]["gaps"] == ["WATERCOND: not in the map (no subtype)"]
    with pytest.raises(CadError, match="WATERCOND can't be WATER"):
        cad.create({"type": "WATERCOND", "subtype": "WATER"})
    cad.create({"type": "WATERCOND"})
    post(cad, "type_subtypes", action="set", type="WATERCOND", subtypes=["WATER", "INSIDE"])
    assert cad.lists.subtypes_for("WATERCOND") == ("INSIDE", "WATER")        # in the subtype list's order
    cad.create({"type": "WATERCOND", "subtype": "WATER"})
    # A retired subtype drops off what is offered (old events keep it) and shows as a gap.
    post(cad, "event_subtypes", action="retire", code="INSIDE")
    assert cad.lists.subtypes_for("WATERCOND") == ("WATER",) and "INVEST: INSIDE is retired" in cad.settings_get()["type_subtypes"]["gaps"]


# -- the v10 migration --------------------------------------------------------------------------------------

def make_old_db(tmp_path) -> Path:
    """A database as the old codes left it: statuses AVAILABLE ..., Settings edits keyed by them, a saved
    board layout on the old boards (main / agencies / other), units now off the department's sheet."""
    cad = make_cad(tmp_path)
    st = cad.store
    iid = cad.create({"type": "STRU", "address": "1 Main St"})["id"]
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60"), uid(cad, "E61")], "operator": "JD"})
    cad.unit_status(uid(cad, "E60"), {"status": "RS", "operator": "JD"})
    cad.unit_status(uid(cad, "E61"), {"status": "OL", "operator": "JD"})
    cad.unit_status(uid(cad, "L31"), {"status": "OS", "note": "tires", "operator": "JD"})
    runcards.save(st, "STRU", [{"kind": "engine", "count": 5}, {"kind": "ladder|tower", "count": 2},
                               {"kind": "rescue", "count": 1}])
    db = st.db
    for new, old in OLD.items():
        db.execute("UPDATE units SET status = ? WHERE status = ?", (old, new))
        db.execute("UPDATE unit_history SET old_status = ? WHERE old_status = ?", (old, new))
        db.execute("UPDATE unit_history SET new_status = ? WHERE new_status = ?", (old, new))
    for code, board in (("CODE_ENFORCE", "other"), ("60CAD", "other"), ("E99", "main")):
        db.execute("INSERT INTO units (name, kind, status, status_at, board) VALUES (?, 'agency', 'AVAILABLE', 0, ?)", (code, board))
    old_main = ["2391", "2392", "2393", "2395", "2398", "E59", "E61", "E60", "E62", "E63", "E64", "L31", "TL2", "R40", "R41",
                "U65", "U67", "U239", "M40", "M60", "2662", "L30", "E14", "U42", "E99", "PCPD"]      # E60/E61 swapped, E99 added, PCPD moved here
    layout = [{"id": "main", "title": "Unit status", "units": old_main},
              {"id": "agencies", "title": "Other agencies", "units": ["PCPD1", "RBPD", "RBPD1", None, "77A1", "77A2"]},
              {"id": "other", "title": "Other / mutual aid units", "units": ["CONED-G", "CODE_ENFORCE", "60CAD", "L25", "DPT239"]}]
    ov = {("unit_statuses", "AVAILABLE"): {"name": "In service", "role": "free", "bg": "#2672D8", "fg": "#FFFFFF"},
          ("unit_statuses", "_order"): ["AVAILABLE", "ON LOCATION", "DISPATCHED", "UNMANNED"],
          ("status_flow", "DISPATCHED"): ["RESPONDING", "AVAILABLE", "OUT OF SERVICE"],
          ("status_flow", "ON LOCATION"): None,
          ("units", "E99"): {"kind": "engine", "spoken": "Engine 99", "response": "fire"},
          ("units", "E59"): {"kind": "engine", "spoken": "Engine 59", "response": "fire"},      # no longer a spare
          ("units", "CODE_ENFORCE"): {"kind": "agency", "spoken": "Code enforcement", "response": "agency"},
          ("units", "_boards"): layout,
          ("dispatch", "voice"): "am_liam"}
    for (sec, key), val in ov.items():
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) VALUES (?, ?, ?, 0, 'JD')",
                   (sec, key, json.dumps(val)))
    db.execute("PRAGMA user_version = 5")
    db.close()
    return tmp_path / "cad.sqlite"


def test_v10_migrates_an_old_database_keeping_every_edit(tmp_path):
    path = make_old_db(tmp_path)
    before = sqlite3.connect(path)
    counts = {t: before.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("incidents", "assignments", "events", "unit_history", "run_cards", "run_card_slots", "dispatches")}
    chrono = before.execute("SELECT id, text FROM events ORDER BY id").fetchall()
    before.close()
    cad = make_cad(tmp_path)
    st = cad.store
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
    assert any(m.startswith("schema v10") for m in st.migrated)
    for t, n in counts.items():
        assert st.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] == n, t
    assert st.q("SELECT id, text FROM events ORDER BY id") == [{"id": i, "text": x} for i, x in chrono]   # text untouched
    old = list(V10_STATUS)
    marks = ",".join("?" * len(old))
    assert not st.q(f"SELECT 1 FROM units WHERE status IN ({marks})", old)
    assert not st.q(f"SELECT 1 FROM unit_history WHERE old_status IN ({marks}) OR new_status IN ({marks})", old + old)
    u = {x["name"]: x for x in st.units()}
    assert (u["E60"]["status"], u["E61"]["status"], u["L31"]["status"], u["L31"]["out_of_service"]) == ("RS", "OL", "OS", "tires")
    assert u["E60"]["incident_number"] and u["E62"]["status"] == "AV"
    hist = [(h["old_status"], h["new_status"]) for h in st.q("SELECT * FROM unit_history WHERE unit_id = ? ORDER BY id", (u["E60"]["id"],))]
    assert hist == [("AV", "DP"), ("DP", "RS")]
    # Settings edits follow their status; the rest are kept as they were.
    ov = cad.settings.overrides()
    assert ov["unit_statuses"]["AV"]["name"] == "In service" and "AVAILABLE" not in ov["unit_statuses"]
    assert ov["unit_statuses"]["_order"] == ["AV", "OL", "DP", "UNMANNED"]
    assert ov["status_flow"] == {"DP": ["RS", "AV", "OS"], "OL": None}
    assert cad.lists.name("unit_statuses", "AV") == "In service" and cad.lists.status_ids[:3] == ("AV", "OL", "DP")
    assert cad.lists.next_statuses("DP") == ("RS", "AV", "OS") and cad.lists.next_statuses("OL") == ("AV", "OS")
    assert ov["dispatch"]["voice"] == "am_liam" and not cad.lists.unit("E59")["spare"]
    # Off the department's sheet: retired, history kept; the edited one too.
    assert ov["units"]["CODE_ENFORCE"]["retired"] and u["CODE_ENFORCE"]["board"] == "" and u["60CAD"]["board"] == ""
    retired = {r["code"] for r in cad.settings_get()["units"]["rows"] if r["retired"]}
    assert {"CODE_ENFORCE", "60CAD", "60CAD1", "60CAD2"} <= retired
    # The saved layout, on the new boards: main keeps its order and its additions; the rest as the file says.
    boards = {b["id"]: b["units"] for b in cad.lists.unit_boards}
    assert boards["main"].index("E61") < boards["main"].index("E60") and boards["main"][-2:] == ["E99", "PCPD"]
    assert "2394" in boards["main"] and "DPT239" in boards["main"] and "L25" in boards["ma-rye"]
    assert "77A1" in boards["ems"] and "PCPD1" in boards["res-pd"] and "CONED-G" in boards["res-coned"]
    assert u["E99"]["board"] == "main" and u["PCPD"]["board"] == "main"
    assert all(b.get("folded") for b in cad.lists.unit_boards[1:])         # how a board shows: from the file
    # The user's STRU card.
    assert runcards.card(st, "STRU")["slots"] == [{"kind": "engine", "count": 5}, {"kind": "ladder|tower", "count": 2},
                                                  {"kind": "rescue", "count": 1}]
    audit = st.q("SELECT * FROM settings_audit WHERE key = 'migration v10'")
    assert len(audit) == 1 and "unit statuses" in audit[0]["new"]
    # Idempotent: the step again, and a restart, change nothing.
    h = dump(st.db)
    st.db.execute("BEGIN IMMEDIATE")
    st._v9_to_v10(st.db)
    st.db.execute("COMMIT")
    assert dump(st.db) == h
    st.db.close()
    again = make_cad(tmp_path)
    assert again.store.migrated == [] and dump(again.store.db) == h


def test_v10_layout_is_idempotent_and_a_fresh_database_needs_nothing(tmp_path):
    L = lists.load()
    old = [{"id": "main", "units": ["E61", "E60", "X1"]}, {"id": "other", "units": ["L25", "X2"]}]
    once = v10_layout(old, L.unit_boards, {})
    assert v10_layout(once, L.unit_boards, {}) == once
    assert once[0]["units"][-1] == "X1" and once[-1]["id"] == "agencies" and once[-1]["units"][-1] == "X2"
    st = Store(tmp_path / "fresh.sqlite")
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == 10 and st.migrated == []
    h = dump(st.db)
    st.db.execute("BEGIN IMMEDIATE")
    st._v9_to_v10(st.db)
    st.db.execute("COMMIT")
    assert dump(st.db) == h and st.q("SELECT * FROM settings_audit") == []


@pytest.mark.skipif(not LIVE_DB.exists(), reason="no live database next to this worktree")
def test_v10_on_a_copy_of_the_live_database(tmp_path):
    """The console's own database, copied (read only, sqlite's backup API) and migrated here."""
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    copy = sqlite3.connect(tmp_path / "cad.sqlite")
    src.backup(copy)
    src.close()
    if copy.execute("PRAGMA user_version").fetchone()[0] >= 10:
        pytest.skip("the live database is already at v10")
    tables = ("incidents", "assignments", "events", "unit_history", "run_cards", "run_card_slots", "dispatches",
              "settings_overrides", "radios", "radio_traffic")
    counts = {t: copy.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    units_before = {r[0]: r[1] for r in copy.execute("SELECT name, status FROM units")}
    cards_sql = ("SELECT c.type, c.spoken, c.is_default, s.pos, s.kind, s.count, s.unit FROM run_cards c "
                 "JOIN run_card_slots s ON s.card_id = c.id ORDER BY c.type, s.pos")
    cards_before = copy.execute(cards_sql).fetchall()
    stru = [r[3:] for r in cards_before if r[0] == "STRU"]
    copy.close()
    cad = make_cad(tmp_path)
    st = cad.store
    for t, n in counts.items():
        assert st.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] == n, t
    now = {u["name"]: u["status"] for u in st.units()}
    for name, old in units_before.items():
        assert now[name] == V10_STATUS.get(old, old), name
    assert set(now) >= set(units_before) and len(now) >= 250
    assert set(now.values()) <= set(cad.lists.status_ids)
    old = list(V10_STATUS)
    marks = ",".join("?" * len(old))
    assert not st.q(f"SELECT 1 FROM unit_history WHERE old_status IN ({marks}) OR new_status IN ({marks})", old + old)
    assert [tuple(r) for r in st.db.execute(cards_sql)] == cards_before          # every run card as it was
    print("live copy: STRU", stru, "units", len(units_before), "->", len(now), counts)
