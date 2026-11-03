"""Other agencies' units go through statuses like our rigs (GitHub #219), and Assigned > Add unit takes several
units at once (#220).

#219, from a dispatcher: "Attaching an outside agency (e.g. RBPD) under Other agencies marked it On location
immediately. Other-agency units should go through statuses like our rigs (dispatched / responding / on location /
available), set by the dispatcher. In the right column, the groups EMS, ConEd, Keyholders, Knox box, PDs, BATs and
Other show no status control when an incident is selected, unlike our units."

    attach       an agency's unit goes on the event as its set's first status, Dispatched (a keyholder: Notified),
                 its Disp time stamped, "Attached RBPD" then "RBPD dispatched" on the timeline; never On location
    the sets     cad_lists.json agency_status_sets, by board: full (Port Chester EMS: Out of service too),
                 responder (PD, ConEd, BATs, Other: our rigs' run but Out of service), contact (Keyholders, Knox box:
                 Notified / En route / On location / Done, the labels shown and logged, the codes stored)
    controls     on the event the status_flow's next steps in the set; on the board (the right column) the whole
                 set, a working status asking which call with the incident being worked first

#220: "with '+ Add unit', type every unit wanted (e.g. 'E60 E62 L31 RBPD') and press Enter to add them all. Accept
spaces or commas, match our units and other agencies, show which matched, and refuse unknown ones with a clear note."
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cad import lists as lists_mod
from sizeup.cad.api import needs
from sizeup.cad.store import CadError
from tests.test_cad import make_cad, uid
from tests.test_roles import JULIAN, VAL, api, call  # noqa: F401  (fixture)
from tests.test_settings import post

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


@pytest.fixture
def ev():
    """A structure fire, E60 dispatched."""
    c = make_cad()
    iid = c.create({"type": "STRU", "address": "209 Westchester Ave"})["id"]
    c.dispatch(iid, {"unit_ids": [uid(c, "E60")], "operator": "JD"})
    c.iid = iid
    return c


def row(c, iid, name):
    return next(a for a in c.full(iid)["assignments"] if a["name"] == name and not a["cleared_at"])


def texts(c, iid):
    return [e["text"] for e in c.full(iid)["events"]]


# -- #219: the status flow of an outside agency's unit ------------------------------------------------------------

def test_attaching_rbpd_puts_it_on_dispatched_then_it_goes_through_its_statuses(ev):
    c, iid = ev, ev.iid
    rb = uid(c, "RBPD")
    t0 = c.store.clock()
    assert c.attach(iid, {"unit_ids": [rb], "operator": "JD"}) == {"ok": True, "moved": []}
    a = row(c, iid, "RBPD")
    assert a["status"] == "DP" and a["status_set"] == "responder"          # assigned, not yet responding
    assert a["dispatched_at"] >= t0 and a["enroute_at"] is None and a["onscene_at"] is None
    assert a["next"] == ["RS", "OL", "ST", "AV", "CN"]                       # our rigs' steps, no Out of service
    assert texts(c, iid)[-2:] == ["Attached RBPD", "RBPD dispatched"]
    assert "RBPD on location" not in texts(c, iid)
    assert len(c.full(iid)["dispatches"]) == 1                              # never in the dispatch log, never said
    for st, said, nxt in (("RS", "RBPD responding", ["OL", "ST", "AV", "CE"]), ("ST", "RBPD staging", ["OL", "AV"]),
                          ("OL", "RBPD on location", ["AV"])):
        c.unit_status(rb, {"status": st, "operator": "JD"})
        assert texts(c, iid)[-1] == said
        assert row(c, iid, "RBPD")["next"] == nxt
    a = row(c, iid, "RBPD")
    assert a["enroute_at"] and a["onscene_at"]
    c.unit_status(rb, {"status": "AV", "operator": "JD"})
    assert texts(c, iid)[-1] == "RBPD cleared"
    hist = c.store.q("SELECT old_status, new_status, incident_id, operator FROM unit_history WHERE unit_id = ? ORDER BY id", (rb,))
    assert [(h["old_status"], h["new_status"]) for h in hist][-5:] == [
        ("AV", "DP"), ("DP", "RS"), ("RS", "ST"), ("ST", "OL"), ("OL", "AV")]
    assert {h["incident_id"] for h in hist[-5:]} == {iid} and {h["operator"] for h in hist[-5:]} == {"JD"}
    # cleared, the call still open: it can go back to it, in its own set
    back = next(a for a in c.full(iid)["assignments"] if a["name"] == "RBPD")
    assert back["return"] == ["RS", "OL"]


def test_an_agency_unit_refuses_a_status_outside_its_set(ev):
    c, iid = ev, ev.iid
    rb, a4 = uid(c, "RBPD"), uid(c, "77A4")
    c.attach(iid, {"unit_ids": [rb, a4], "operator": "JD"})
    for code, st in (("RBPD", "OS"), ("RBPD", "IC"), ("RBPD", "ET"), ("77A4", "IC")):
        with pytest.raises(CadError, match=f"{code} is another agency's unit"):
            c.unit_status(uid(c, code), {"status": st, "operator": "JD"})
    # Port Chester EMS: an ambulance going out of service is the desk's business (its reason kept)
    assert row(c, iid, "77A4")["next"] == ["RS", "OL", "ST", "AV", "CN", "OS"]
    c.unit_status(a4, {"status": "OS", "note": "stretcher", "operator": "JD"})
    assert texts(c, iid)[-1] == "77A4 out of service (stretcher)"


def test_keyholders_and_the_knox_box_are_notified_en_route_on_location_done(ev):
    c, iid = ev, ev.iid
    kh, kb = uid(c, "KEYHOLDER"), uid(c, "KNOX_BOX")
    c.attach(iid, {"unit_ids": [kh, kb], "operator": "JD"})
    assert texts(c, iid)[-3:] == ["Attached KEYHOLDER, KNOX_BOX", "KEYHOLDER notified", "KNOX_BOX notified"]
    a = row(c, iid, "KEYHOLDER")
    assert (a["status"], a["status_set"], a["next"]) == ("DP", "contact", ["RS", "OL", "AV"])
    c.unit_status(kh, {"status": "RS", "operator": "JD"})
    assert texts(c, iid)[-1] == "KEYHOLDER en route"
    assert row(c, iid, "KEYHOLDER")["next"] == ["OL", "AV"]
    with pytest.raises(CadError, match="Notified, En route, On location or Done"):
        c.unit_status(kh, {"status": "ST", "operator": "JD"})
    c.unit_status(kh, {"status": "OL", "operator": "JD"})
    assert row(c, iid, "KEYHOLDER")["next"] == ["AV"]
    c.unit_status(kh, {"status": "AV", "operator": "JD"})
    assert texts(c, iid)[-1] == "KEYHOLDER cleared"
    # the codes are stored and exported as ever: the labels are the set's names for them
    assert [h["new_status"] for h in c.store.q("SELECT new_status FROM unit_history WHERE unit_id = ? ORDER BY id", (kh,))][-4:] == [
        "DP", "RS", "OL", "AV"]
    # closing the call clears whoever is still on it, an agency's too
    c.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    assert next(u for u in c.store.units() if u["id"] == kb)["status"] == "AV"


def test_the_right_column_puts_an_agency_unit_on_a_call_and_moves_it_through(ev):
    """The board: a working status asks which call (the incident worked first); the unit goes on it, then the status."""
    c, iid = ev, ev.iid
    pd = uid(c, "PCPD1")
    c.unit_status(pd, {"status": "RS", "incident_id": iid, "operator": "JD"})
    a = row(c, iid, "PCPD1")
    assert a["status"] == "RS" and a["dispatched_at"] and a["enroute_at"]
    assert texts(c, iid)[-2:] == ["PCPD1 dispatched", "PCPD1 responding"]
    c.unit_status(pd, {"status": "OL", "incident_id": iid, "operator": "JD"})
    assert texts(c, iid)[-1] == "PCPD1 on location"
    with pytest.raises(CadError, match="another agency's unit"):
        c.unit_status(pd, {"status": "OS", "incident_id": 0, "operator": "JD"})


def test_add_unit_puts_an_agency_unit_on_responding_and_a_keyholder_en_route(ev):
    c, iid = ev, ev.iid
    c.attach(iid, {"unit_ids": [uid(c, "RBPD"), uid(c, "E62")], "status": "RS", "operator": "JD"})
    c.attach(iid, {"unit_ids": [uid(c, "KEYHOLDER2")], "status": "RS", "operator": "JD"})
    assert texts(c, iid)[-3:] == ["RBPD added responding (not dispatched)", "E62 added responding (not dispatched)",
                                  "KEYHOLDER2 added en route (not dispatched)"]
    assert row(c, iid, "RBPD")["dispatched_at"] is None
    with pytest.raises(CadError, match="Another agency's unit, not Incident command: 77A1"):
        c.attach(iid, {"unit_ids": [uid(c, "77A1")], "status": "IC", "operator": "JD"})


def test_the_groups_and_their_sets():
    L = make_cad().lists
    got = {b["id"]: {L.agency_set(u) for u in b["units"] if u} for b in L.unit_boards}
    assert got["ems"] == {"full"}
    assert got["res-keyholder"] == got["res-knox"] == {"contact"}
    assert got["res-coned"] == got["res-pd"] == got["res-bat"] == got["agencies"] == {"responder"}
    assert got["main"] == got["ma-rye"] == got["ma-requests"] == {""}        # ours and mutual aid: every status
    assert L.unit_statuses_for("E60") == L.status_ids
    assert L.label("DP", "KNOX_BOX") == "Notified" and L.label("AV", "KEYHOLDER3") == "Done"
    assert L.label("DP", "RBPD") == "Dispatched" and L.label("DP", "E60") == "Dispatched"
    pub = L.public()["agency_status_sets"]
    assert pub["sets"]["contact"] == {"statuses": ["DP", "RS", "OL", "AV"],
                                      "labels": {"DP": "Notified", "RS": "En route", "AV": "Done"}}
    assert pub["boards"] == {"ems": "full", "res-keyholder": "contact", "res-knox": "contact"}
    # the units the page gets say their set
    by = {u["name"]: u["status_set"] for u in make_cad().store.units()}
    assert (by["RBPD"], by["KNOX_BOX"], by["77M1"], by["CONED-G2"], by["E60"], by["L25"]) == (
        "responder", "contact", "full", "responder", "", "")


def test_the_sets_are_checked_and_a_file_without_them_still_works():
    d = lists_mod.read()
    for bad, why in (({"sets": {"x": {"statuses": ["AV", "DP"]}}}, "first status"),
                     ({"sets": {"x": {"statuses": ["DP", "RS"]}}}, "free status"),
                     ({"sets": {"x": {"statuses": ["DP", "NOPE", "AV"]}}}, "NOPE not in unit_statuses"),
                     ({"sets": {"x": {"statuses": ["DP", "AV"], "labels": {"OL": "There"}}}}, "labels"),
                     ({"default": "y", "sets": {"x": {"statuses": ["DP", "AV"]}}}, "default"),
                     ({"sets": {"x": {"statuses": ["DP", "AV"]}}, "boards": {"nowhere": "x"}}, "boards")):
        with pytest.raises(lists_mod.ListsError, match=why):
            lists_mod.build(dict(d, agency_status_sets=bad), "test")
    old = lists_mod.build({k: v for k, v in d.items() if k != "agency_status_sets"}, "test")
    assert old.agency_set("RBPD") == "all" and old.unit_statuses_for("RBPD") == ("DP", "RS", "OL", "AQ")
    assert old.agency_attach_status("all") == "DP"


def test_a_status_retired_in_settings_drops_out_of_the_sets():
    c = make_cad()
    post(c, "unit_statuses", action="retire", code="CN")
    assert "CN" not in c.lists.unit_statuses_for("RBPD")
    assert c.state()["lists"]["agency_status_sets"]["sets"]["responder"]["statuses"] == ["DP", "RS", "OL", "ST", "AV", "CE"]


def test_run_cards_and_the_quick_add_buttons_are_as_they_were():
    c = make_cad()
    rec = c.recommend(c.create({"type": "STRU", "address": "1 Main St"})["id"], {})
    assert not any(u["response"] == "agency" for u in c.store.units() if u["name"] in {x["name"] for x in rec["units"]})
    jr = c.state()["lists"]["joint_response"]["Rye Brook"]
    assert jr["quick_add"] == ["E14", "L30", "2662"]
    assert c.state()["lists"]["full_response_kinds"]


# -- permissions: unchanged --------------------------------------------------------------------------------------

def test_permissions_are_unchanged(api):  # noqa: F811
    assert needs("/api/cad/incident/7/attach") == needs("/api/cad/unit/7/status") == "dispatch"
    c = api.cad
    iid = c.create({"type": "STRU", "address": "1 Main St"})["id"]
    rb = uid(c, "RBPD")
    code, _ = call(api, f"/api/cad/incident/{iid}/attach", {"unit_ids": [rb], "operator": "Val"}, VAL)
    assert code == 403
    assert next(u for u in c.store.units() if u["id"] == rb)["incident_id"] is None
    code, out = call(api, f"/api/cad/incident/{iid}/attach", {"unit_ids": [rb], "operator": "Julian"}, JULIAN)
    assert code == 200 and out["ok"]
    code, _ = call(api, f"/api/cad/unit/{rb}/status", {"status": "RS", "operator": "Val"}, VAL)
    assert code == 403
    code, _ = call(api, f"/api/cad/unit/{rb}/status", {"status": "RS", "operator": "Julian"}, JULIAN)
    assert code == 200
    assert next(u for u in c.store.units() if u["id"] == rb)["status"] == "RS"


# -- the page: status controls for the agency groups, and several units in Add unit (node) -----------------------

def grab_harness(names, body):
    """A node script with cad.js's functions `names` evaluated, a tiny DOM, then body."""
    return r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const grab = (name) => { const m = src.match(new RegExp("^(async )?function " + name + "\\(.*\\) \\{[\\s\\S]*?^\\}", "m"));
  if (!m) throw new Error("not found: " + name); return m[0]; };
class N {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this.textContent = ""; this.title = "";
    this.dataset = {}; this.style = { setProperty() {} }; this.disabled = false;
    this.classList = { add: (c) => { this.className += " " + c; } }; }
  append(...c) { this.children.push(...c); } addEventListener() {} setAttribute() {} focus() {}
}
global.document = { createElement: (t) => new N(t), activeElement: null };
global.setTimeout = () => 0;
global.cad = { state: { lists: data.lists, units: data.units }, ask: null, pick: null, sel: data.sel, extra: new Map() };
global.BOARD = false;
global.incidents = () => data.incidents;
global.incTitle = (i) => i.number; global.typeLabel = () => "Structure fire";
const kids = (n) => n.children.map((c) => c.tag === "form" ? "[" + kids(c).join("|") + "]" : c.tag === "select"
  ? "<" + c.children.map((o) => o.textContent).join("/") + ">" : c.textContent);
""" + "\n".join(f'eval(grab("{n}").replace(/^(async )?function (\\w+)/, "global.$2 = $1function $2"));' for n in names) + body


def page_data(c, iid):
    units = c.store.units()
    return {"lists": c.lists.public(), "units": units, "sel": iid,
            "incidents": [{"id": i["id"], "number": i["number"], "status": i["status"], "address": i["address"],
                           "assignments": i["assignments"]} for i in c.store.incidents()]}


def run_node(tmp_path, script, data):
    h, d = tmp_path / "h.js", tmp_path / "d.json"
    h.write_text(script)
    d.write_text(json.dumps(data, default=str))
    out = subprocess.run([NODE, str(h), str(STATIC / "cad.js"), str(d)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


BUTTONS = r"""
const byName = (n) => data.units.find((u) => u.name === n);
const out = {};
for (const n of ["E60", "RBPD", "77A4", "CONED-G", "KEYHOLDER", "KNOX_BOX", "BAT10", "DPW"]) out[n] = kids(statusButtons(byName(n), "board"));
// on the event: the next steps, labelled
const a = data.incidents.find((i) => i.id === data.sel).assignments;
for (const n of ["RBPD", "KEYHOLDER"]) {
  const r = a.find((x) => x.name === n);
  out["inc " + n] = kids(statusButtons({ id: r.unit_id, status: r.status, next: r.next, status_set: r.status_set }, "inc"));
}
// a working status from the right column asks which call: the incident being worked first; no "No call" for an agency
cad.pick = { uid: byName("PCPD1").id, status: "RS" };
out.pick = kids(statusButtons(byName("PCPD1"), "board"));
cad.pick = { uid: byName("E61").id, status: "RS" };
out.pickOurs = kids(statusButtons(byName("E61"), "board"));
cad.pick = null;
out.chip = statusChip("DP", statusLabel(byName("KEYHOLDER"), "DP")).textContent;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_status_controls_for_every_agency_group(ev, tmp_path):
    c, iid = ev, ev.iid
    c.attach(iid, {"unit_ids": [uid(c, "RBPD"), uid(c, "KEYHOLDER")], "operator": "JD"})
    other = c.create({"type": "ALARM", "subtype": "COMM", "address": "1 Main St"})["id"]
    c.dispatch(other, {"unit_ids": [uid(c, "E63")], "operator": "JD"})
    names = ["el", "unitStatus", "roleOf", "statusChip", "statusColors", "agencySet", "statusLabel", "L", "units",
             "unitById", "statusChoices", "statusButtons", "callPicker"]
    out = run_node(tmp_path, grab_harness(names, BUTTONS), page_data(c, iid))
    full_ = ["Dispatched", "Responding", "On location", "Staging", "Available", "Cancelled", "Cancelled enroute"]
    assert out["RBPD"] == out["CONED-G"] == out["BAT10"] == out["DPW"] == full_        # like our rigs, no OOS
    assert out["77A4"] == full_ + ["Out of service"]
    assert out["KEYHOLDER"] == out["KNOX_BOX"] == ["Notified", "En route", "On location", "Done"]
    assert len(out["E60"]) == len(c.lists.status_ids)                                   # ours: as before
    assert out["inc RBPD"] == ["Responding", "On location", "Staging", "Available", "Cancelled"]
    assert out["inc KEYHOLDER"] == ["En route", "On location", "Done"]
    inc = c.store.incident(iid)["number"]
    pick = out["pick"][0]
    assert pick.startswith("[PCPD1 Responding on|<") and "No call" not in pick
    assert pick.index(f"{inc} ") < pick.index(c.store.incident(other)["number"])       # the incident worked first
    assert "(this incident)" in pick
    assert "No call" in out["pickOurs"][0]
    assert out["chip"] == "Notified"


PARSE = r"""
const list = data.units.filter((u) => u.board || u.incident_id);
const here = new Set(["E60"]);
const p = (t) => parseUnitList(t, list, here).map((g) => [g.text, g.unit ? g.unit.name : null, g.why]);
console.log(JSON.stringify({
  spaces: p("E60 E62 L31 RBPD"),
  commas: p("e62,l31, rbpd ,77a4"),
  mixed: p("e62 L31 Rbpd knox_box coned-g1"),
  unknown: p("E62 E99 zz L31"),
  dupes: p("E62 e62 E62"),
  here: p("e60 E61"),
  typing: p("E62 L3"),
  typedDone: p("E62 L3 "),
  aliases: p("engine 62, 23 98, 77 alpha 4, knoxbox, coned g1"),
  takenAsIs: p("U10 1"),
  empty: p("  , ,"),
  single: p("E63"),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_add_unit_parses_and_matches_several_units(ev, tmp_path):
    names = ["unitWords", "unitKey", "unitIndex", "matchWord", "parseUnitList"]
    out = run_node(tmp_path, grab_harness(names, PARSE), page_data(ev, ev.iid))
    assert out["spaces"] == [["E60", "E60", "here"], ["E62", "E62", ""], ["L31", "L31", ""], ["RBPD", "RBPD", ""]]
    assert out["commas"] == [["e62", "E62", ""], ["l31", "L31", ""], ["rbpd", "RBPD", ""], ["77a4", "77A4", ""]]
    assert out["mixed"] == [["e62", "E62", ""], ["L31", "L31", ""], ["Rbpd", "RBPD", ""], ["knox_box", "KNOX_BOX", ""],
                            ["coned-g1", "CONED-G1", ""]]
    assert out["unknown"] == [["E62", "E62", ""], ["E99", None, "unknown"], ["zz", None, "unknown"], ["L31", "L31", ""]]
    assert out["dupes"] == [["E62", "E62", ""], ["e62", "E62", "twice"], ["E62", "E62", "twice"]]
    assert out["here"] == [["e60", "E60", "here"], ["E61", "E61", ""]]
    assert out["typing"] == [["E62", "E62", ""], ["L3", None, "typing"]]               # L30, L31 ... still typing
    assert out["typedDone"] == [["E62", "E62", ""], ["L3", None, "unknown"]]
    assert out["aliases"] == [["engine 62", "E62", ""], ["23 98", "2398", ""], ["77 alpha 4", "77A4", ""],
                              ["knoxbox", "KNOX_BOX", ""], ["coned g1", "CONED-G1", ""]]
    assert out["takenAsIs"] == [["U10", "U10", ""], ["1", None, "unknown"]]            # a unit as typed isn't joined on
    assert out["empty"] == []
    assert out["single"] == [["E63", "E63", ""]]


def test_add_unit_keeps_its_single_unit_ways_and_says_what_it_skips():
    js = (STATIC / "cad.js").read_text()
    html = (STATIC / "cad.html").read_text()
    sub = js[js.index("async function submitAddUnit"):js.index("async function setUnitStatus")]
    # one unit: the same messages and the same confirm as before
    assert "`No unit ${unknown[0]}`" in sub and "Enter a unit" in sub
    assert "is on ${away[0].incident_number || \"another call\"}. Move it here?" in sub
    # several: one POST, the unknown words kept in the box with a note, the skipped said
    assert sub.count('api("POST"') == 1 and "unit_ids: take.map((u) => u.id)" in sub
    assert "Not found: ${unknown.join(\", \")} (fix or clear)" in sub and "Already on this event" in sub
    # the autocomplete works on the word being typed, Tab / Enter take a suggestion as before
    keys = js[js.index("function addUnitKeys"):js.index("async function submitAddUnit")]
    assert '(e.key === "Tab" || (e.key === "Enter" && !exact))' in keys
    assert re.search(r'<input id="o-unit"[^>]*maxlength="200"', html)
    assert '<div id="o-preview" class="chips add-chips"' in html
