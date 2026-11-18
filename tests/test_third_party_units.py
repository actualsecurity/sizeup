"""Third-party units: keyholders, utilities, police (PCFD, 2026-11-17). Units and calls invented; the codes are the board's.

"we don't dispatch PCPD, and keep their times, we mark the PCPD code as 'DO' because we requested them, but they get
dispatched by another agency ... We want them to default to Dispatched (and have the option for Dispatch Other) when added
to a call, not responding when we add them, and let's get rid of adding them [under Other agencies] and only allow them to
be added [from Assigned > Add unit]." Then: "in some cases these statuses we don't want for other types of units, just 3rd
party units", and "like NR & FR we want to make those available for keyholder units".

    added      Assigned > Add unit puts a third party on Dispatched whatever button was pressed (CONED-G went on
               Responding: Add unit's Enter is Responding, and Responding was in its set), or Dispatch other when picked;
               its Disp time stamped
    DO         Dispatch other: a third party's only (agency_status_sets.third_party_only), never offered or set on ours
               or on EMS; on the timeline with its code
    automation the county feed, the pages and the radio follow the fire response only: never a third party
    class      the status set is the class: by board, or per unit in Settings > Units (admins, audited)
    reports    incidents.csv's Dispatch / First Enroute / First On Scene are the fire response's; the unit history
               keeps DO
"""
import csv
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cad.feed import load_cfg
from sizeup.cad.store import CadError
from tests.test_agency_status import grab_harness, page_data, row, run_node, texts
from tests.test_cad import make_cad, uid
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0
from tests.test_roles import CASEY, JORDAN, api, call  # noqa: F401  (fixture)
from tests.test_settings import post

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
ADDR = "40 Sample Rd"


@pytest.fixture
def ev():
    """A commercial alarm, E61 dispatched."""
    c = make_cad()
    iid = c.create({"type": "ALARM", "subtype": "COMM", "address": ADDR})["id"]
    c.dispatch(iid, {"unit_ids": [uid(c, "E61")], "operator": "JD"})
    c.iid = iid
    return c


# -- added from Assigned > Add unit -------------------------------------------------------------------------------------

def test_add_unit_responding_puts_a_third_party_on_dispatched_never_responding(ev):
    """The CONED-G case: typed in Add unit and Enter (Responding) put it on Responding. Now: Dispatched, its time kept."""
    c, iid = ev, ev.iid
    t0 = c.store.clock()
    out = c.attach(iid, {"unit_ids": [uid(c, "CONED-G")], "status": "RS", "operator": "JD"})
    assert out["as"] == {"CONED-G": "DP"} and out["status"] == "RS"
    a = row(c, iid, "CONED-G")
    assert a["status"] == "DP" and a["dispatched_at"] >= t0 and a["enroute_at"] is None
    assert texts(c, iid)[-1] == "CONED-G added: dispatched"
    for st in ("OL", "ST", "IC", "ET"):                          # whatever was pressed: Dispatched
        n = {"OL": "PCPD", "ST": "KEYHOLDER1", "IC": "RBPD2", "ET": "DPW"}[st]
        assert c.attach(iid, {"unit_ids": [uid(c, n)], "status": st, "operator": "JD"})["as"] == {n: "DP"}
    assert not any("responding" in t or "on location" in t for t in texts(c, iid) if t.split()[0] in
                   {"CONED-G", "PCPD", "KEYHOLDER1", "RBPD2", "DPW"})


def test_dispatch_other_when_picked_and_mixed_with_our_units(ev):
    c, iid = ev, ev.iid
    out = c.attach(iid, {"unit_ids": [uid(c, "PCPD1"), uid(c, "KNOX_BOX2")], "status": "DO", "operator": "JD"})
    assert out["as"] == {"PCPD1": "DO", "KNOX_BOX2": "DO"}
    a = row(c, iid, "PCPD1")
    assert a["status"] == "DO" and a["dispatched_at"]                 # Dispatch other: the time it was requested
    assert texts(c, iid)[-2:] == ["PCPD1 added: dispatch other (DO)", "KNOX_BOX2 added: dispatch other (DO)"]
    # with our units on Responding: ours Responding (not dispatched), the third party Dispatched
    out = c.attach(iid, {"unit_ids": [uid(c, "E62"), uid(c, "CONED-E1")], "status": "RS", "operator": "JD"})
    assert out["as"] == {"E62": "RS", "CONED-E1": "DP"}
    assert row(c, iid, "E62")["dispatched_at"] is None and row(c, iid, "E62")["enroute_at"]
    # our units are never put on as Dispatch other (nor Dispatched from here: the dispatch does that)
    for st in ("DO", "DP"):
        with pytest.raises(CadError, match="status: RS"):
            c.attach(iid, {"unit_ids": [uid(c, "E63")], "status": st, "operator": "JD"})


def test_ems_keeps_its_ways_and_never_gets_dispatch_other(ev):
    c, iid = ev, ev.iid
    assert c.attach(iid, {"unit_ids": [uid(c, "77A2")], "status": "RS", "operator": "JD"})["as"] == {"77A2": "RS"}
    assert texts(c, iid)[-1] == "77A2 added responding (not dispatched)"
    assert c.attach(iid, {"unit_ids": [uid(c, "77A3")], "status": "DP", "operator": "JD"})["as"] == {"77A3": "DP"}
    with pytest.raises(CadError, match="Another agency's unit, not Dispatch other: 77A5"):
        c.attach(iid, {"unit_ids": [uid(c, "77A5")], "status": "DO", "operator": "JD"})
    with pytest.raises(CadError, match="another agency's unit"):
        c.unit_status(uid(c, "77A2"), {"status": "DO", "operator": "JD"})
    assert "DO" not in row(c, iid, "77A3")["next"]


def test_dispatch_other_is_never_ours(ev):
    c, iid = ev, ev.iid
    L = c.lists
    assert L.third_party_only == ("DO",)
    for code in ("E61", "L31", "2392", "E14", "L25", "239_Eng"):             # ours, Rye Brook's, mutual aid
        assert "DO" not in L.unit_statuses_for(code) and not L.third_party(code)
        assert all("DO" not in L.next_statuses(s, code) for s in L.status_ids)
    with pytest.raises(CadError, match="Dispatch other is for third-party units"):
        c.unit_status(uid(c, "E61"), {"status": "DO", "operator": "JD"})
    assert row(c, iid, "E61")["status"] == "DP"


def test_keyholders_get_notified_not_responding_and_failure_to_respond(ev):
    c, iid = ev, ev.iid
    kh = uid(c, "KEYHOLDER3")
    c.attach(iid, {"unit_ids": [kh], "operator": "JD"})
    assert row(c, iid, "KEYHOLDER3")["next"] == ["DO", "OL", "NR", "FR", "AV", "CN"]
    c.unit_status(kh, {"status": "FR", "operator": "JD"})
    assert texts(c, iid)[-1] == "KEYHOLDER3 failure to respond"          # its own words, not "cleared"
    a = next(x for x in c.full(iid)["assignments"] if x["name"] == "KEYHOLDER3")
    assert a["cleared_at"] and a["dispatched_at"]
    # utilities and police: no NR / FR
    assert "NR" not in c.lists.unit_statuses_for("CONED-G3") and "FR" not in c.lists.unit_statuses_for("WCPD")


def test_a_unit_put_on_under_the_old_other_agencies_card_still_shows():
    """An assignment from before (Attach: Dispatched, then Responding under the keyholders' old set): shown, its times
    kept, the buttons of its set from wherever it is now."""
    c = make_cad()
    iid = c.create({"type": "ALARM", "address": ADDR})["id"]
    kh, pd = uid(c, "KEYHOLDER4"), uid(c, "RBPD1")
    c.store.assign(iid, [kh, pd], "JD")                     # as the old Attach stored them: on the call, Dispatched
    with c.store.tx() as db:                                # ... and moved on under the old sets (no longer offered)
        db.execute("UPDATE units SET status = 'RS' WHERE id IN (?, ?)", (kh, pd))
        db.execute("UPDATE assignments SET enroute_at = dispatched_at + 60 WHERE unit_id IN (?, ?)", (kh, pd))
    for n in ("KEYHOLDER4", "RBPD1"):
        a = row(c, iid, n)
        assert a["status"] == "RS" and a["dispatched_at"] and a["enroute_at"] and a["next"] == ["AV"]
    c.unit_status(kh, {"status": "OL", "operator": "JD"})
    assert row(c, iid, "KEYHOLDER4")["onscene_at"]


# -- automation never moves a third party -------------------------------------------------------------------------------

def test_the_county_feed_and_the_radio_never_move_a_third_party(tmp_path):
    rig = AutoRig(tmp_path)
    assert rig.cad.auto.mode() == "60control"
    rig.created(units=("E60", "PCPD", "CONED-G"))                   # the feed lists them: ours only are put on
    assert {a["name"] for a in rig.inc()["assignments"]} == {"E60"}
    rig.cad.attach(rig.iid(), {"unit_ids": [rig.uid("CONED-G")], "status": "RS", "operator": "Lt. Test"})
    assert rig.status("CONED-G") == "DP"
    rig.at(60)
    rig.change(units=("E60", "PCPD", "CONED-G"), unit_times={"CONED-G": {"responding": T0 + 55},
                                                             "E60": {"responding": T0 + 55}})
    assert rig.status("E60") == "RS" and rig.status("CONED-G") == "DP"
    rig.radio("307-31-call_31-t0", T0 + 90, "CONED-G on location")
    rig.radio("307-32-call_32-t0", T0 + 95, "Con Ed on location")
    assert rig.status("CONED-G") == "DP" and rig.here("CONED-G")["onscene_at"] is None
    assert not [h for h in rig.cad.status_hints() if h.get("unit") in ("CONED-G", "PCPD")]
    assert not any(s[2] in ("CONED-G", "PCPD") for s in rig.steps())


def test_the_radio_and_feed_unit_lists_hold_no_third_party_even_one_given_an_apparatus_kind():
    c = make_cad()
    post(c, "units", action="edit", code="U66", unit={"response": "agency", "status_set": "third_party"})
    assert c.lists.third_party("U66")
    hint_units = load_cfg(c.lists).units
    assert "U66" not in hint_units and "E60" in hint_units
    assert not {"PCPD", "CONED-G", "KEYHOLDER", "U66"} & {u["name"] for u in c.auto._ours().values()}


# -- Settings > Units: the class -----------------------------------------------------------------------------------------

def test_settings_sets_a_units_class_audited(api):  # noqa: F811
    c = api.cad
    rows = {r["code"]: r for r in c.settings_get()["units"]["rows"]}
    assert (rows["PCPD"]["status_set"], rows["PCPD"]["third_party"]) == ("third_party", True)
    assert (rows["77A1"]["status_set"], rows["77A1"]["third_party"]) == ("full", False)
    assert rows["E60"]["status_set"] == "" and rows["SPCON"]["status_set"] == "responder"
    sets = {s["id"]: s for s in c.settings_get()["units"]["status_sets"]}
    assert sets["third_party"]["statuses"] == ["DP", "DO", "OL", "AV", "CN"] and sets["keyholder"]["third_party"]
    body = {"action": "edit", "code": "SPCON", "unit": {"status_set": "third_party"}, "operator": "Jordan"}
    code, _ = call(api, "/api/cad/settings/units", body, JORDAN)          # a dispatcher: no
    assert code == 403 and not c.lists.third_party("SPCON")
    code, _ = call(api, "/api/cad/settings/units", dict(body, operator="Casey"), CASEY)
    assert code == 200 and c.lists.third_party("SPCON") and c.lists.unit_statuses_for("SPCON") == ("DP", "DO", "OL", "AV", "CN")
    log = c.settings_audit_log({"section": ["units"]})["audit"]
    assert log[0]["key"] == "SPCON" and "third_party" in json.dumps(log[0]["new"]) and log[0]["operator"].startswith("Casey")
    with pytest.raises(CadError, match="status_set: one of"):
        post(c, "units", action="edit", code="SPCON", unit={"status_set": "nobody"})
    post(c, "units", action="edit", code="SPCON", unit={"status_set": "responder"})
    assert not c.lists.third_party("SPCON")
    post(c, "units", action="edit", code="E63", unit={"status_set": "third_party"})   # ours: no set (fire)
    assert c.lists.agency_set("E63") == ""


def test_a_status_set_whose_dispatch_other_is_retired_or_misplaced():
    from sizeup.cad import lists as lists_mod
    d = lists_mod.read()
    ag = json.loads(json.dumps(d["agency_status_sets"]))
    ag["sets"]["full"]["statuses"].append("DO")
    with pytest.raises(lists_mod.ListsError, match="third-party only"):
        lists_mod.build(dict(d, agency_status_sets=ag), "test")
    ag = json.loads(json.dumps(d["agency_status_sets"]))
    ag["sets"]["third_party"]["flow"]["DP"].append("RS")
    with pytest.raises(lists_mod.ListsError, match="flow"):
        lists_mod.build(dict(d, agency_status_sets=ag), "test")
    c = make_cad()
    post(c, "unit_statuses", action="retire", code="DO")
    assert c.lists.unit_statuses_for("PCPD") == ("DP", "OL", "AV", "CN") and c.lists.next_statuses("DP", "PCPD") == ("OL", "AV", "CN")


# -- reports ------------------------------------------------------------------------------------------------------------

def test_reports_keep_do_and_the_first_times_are_the_fire_responses(ev):
    c, iid = ev, ev.iid
    c.attach(iid, {"unit_ids": [uid(c, "PCPD2")], "status": "DO", "operator": "JD"})
    c.unit_status(uid(c, "PCPD2"), {"status": "OL", "operator": "JD"})   # PD there first
    c.unit_status(uid(c, "E61"), {"status": "OL", "operator": "JD"})
    rows = list(csv.DictReader(io.StringIO(c.export("incidents.csv").decode("utf-8-sig"))))
    r = next(x for x in rows if x["Incident #"] == c.store.incident(iid)["number"])
    assert "PCPD2" in r["Assigned Units"]
    e61 = row(c, iid, "E61")
    from sizeup.cad.export import ts
    assert r["Dispatch"] == ts(e61["dispatched_at"]) and r["First On Scene"] == ts(e61["onscene_at"])
    hist = list(csv.DictReader(io.StringIO(c.export("unit-history.csv").decode("utf-8-sig"))))
    assert [h["New Status"] for h in hist if h["Unit"] == "PCPD2"] == ["DO", "OL"]
    log = c.export("event-log.csv").decode("utf-8-sig")
    assert "PCPD2 added: dispatch other (DO)" in log


# -- the page (node) ----------------------------------------------------------------------------------------------------

ADD = r"""
const byName = (n) => data.units.find((u) => u.name === n);
const b = (names) => addStatuses(names.map(byName)).map((s) => s.code);
const sugg = (q) => addMatches(data.units.filter((u) => u.board), q).map((u) => u.name + " " + boardTitle(u));
console.log(JSON.stringify({
  none: b([]), ours: b(["E62"]), third: b(["CONED-G", "PCPD1"]), keyholder: b(["KEYHOLDER"]), mixed: b(["E62", "PCPD"]),
  ems: b(["77A4"]), emsThird: b(["77A4", "PCPD"]), bat: b(["BAT10"]),
  pd: sugg("pd"), coned: sugg("coned").slice(0, 3), key: sugg("keyholder").length, e6: sugg("e6"),
  e60: sugg("e60")[0], boardE60: statusChoices(byName("E60"), "board").map((s) => s.code),
  boardPd: statusChoices(byName("PCPD"), "board").map((s) => s.code),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_add_unit_buttons_and_suggestions(ev, tmp_path):
    names = ["el", "L", "units", "unitStatus", "roleOf", "agencySet", "statusLabel", "addStatuses", "boardTitle", "unitKey",
             "addMatches", "statusChoices"]
    out = run_node(tmp_path, grab_harness(names, "const ADD_SUGG_MAX = 12;\n" + ADD), page_data(ev, ev.iid))
    assert out["none"] == out["ours"] == out["mixed"] == ["RS", "OL"]          # ours: Responding (Enter), On location
    assert out["third"] == out["keyholder"] == ["DP", "DO"]                   # a third party alone: Dispatched (Enter), DO
    assert out["ems"] == ["RS", "OL", "DP"] and out["bat"] == ["RS", "OL", "DP"]   # EMS / agency: as before, and Dispatched
    assert out["emsThird"] == ["RS", "OL", "DP"]                              # PCPD with them: Dispatched
    # other agencies found by any part of the code or their group's name, grouped by board
    assert out["pd"][:3] == ["PCPD Agencies: PD", "PCPD1 Agencies: PD", "PCPD2 Agencies: PD"]
    assert len(out["pd"]) == 12 and {x.split(" ", 1)[1] for x in out["pd"]} == {"Agencies: PD"}
    assert {x.split()[0] for x in out["pd"]} >= {"RBPD", "WCPD2"}
    assert out["coned"] == ["CONED-G Agencies: CONED-*", "CONED-G1 Agencies: CONED-*", "CONED-G2 Agencies: CONED-*"]
    assert out["key"] == 6
    assert all(x.split()[0].upper().startswith("E6") for x in out["e6"])     # ours: by the start of the code only
    assert out["e60"] == "E60 Fire"
    assert "DO" not in out["boardE60"] and out["boardPd"] == ["DP", "DO", "OL", "AV", "CN"]


def test_the_other_agencies_card_is_gone():
    js, html = (STATIC / "cad.js").read_text(), (STATIC / "cad.html").read_text()
    assert '"Other agencies"' not in js and 'u.response === "agency"' not in js[js.index("function renderAttach"):][:600]
    assert 'show($("#d-attach"), open && inc.status !== "queued" && !inc.emergency);' in js
    assert '<h2 id="a-title">Attach</h2>' in html
