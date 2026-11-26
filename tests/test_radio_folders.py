"""The Radio page rail's folders (#607): the admins' layout for the department (sizeup/cad/radiofolders.py, the
settings section radio_folders: stored, checked, audited, exported, sent with the tree), the rail drawing it over the
automatic grouping (static/radio-nav.js: nothing ever drops off, a new talkgroup lands in its default folder), each
viewer's folded folders, and Edit (admins).

No radios, no network. Every source, talkgroup number, folder and name here is invented."""

from __future__ import annotations

import json
import subprocess

import pytest

from sizeup import sourcekeys
from sizeup.cad import radiofolders
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_radio_emergency import NODE, STATIC
from tests.test_radio_nav import HARNESS

LAYOUT = {"folders": [
    {"id": "u:fg01", "name": "Fireground", "section": "", "items": ["fm:FG1", "p25:metro:9100"]},
    {"id": "d:metro:Police", "name": "PD", "section": "Metro", "items": ["p25:metro:9003", "p25:metro:9001"]}]}


# ---- the setting (Python) ------------------------------------------------------------------------------------------

def test_clean_keeps_the_shape_and_drops_what_isnt_a_channel():
    got = radiofolders.clean({"folders": [
        {"id": "u:ab12", "name": "  Fire   ground ", "section": "County", "items": ["fm:FG1", "nonsense", 7, "fm:FG1"]},
        {"id": "d:westchester:Fire", "name": "Fire", "items": ["fm:FG1", "p25:westchester:1234", "tg:55"]},
        {"id": "u:cd34", "name": "Empty", "section": ""}]})
    assert got == {"folders": [
        {"id": "u:ab12", "name": "Fire ground", "section": "County", "items": ["fm:FG1"]},     # each channel once
        {"id": "d:westchester:Fire", "name": "Fire", "section": "", "items": ["p25:westchester:1234", "tg:55"]},
        {"id": "u:cd34", "name": "Empty", "section": "", "items": []}]}
    for bad, why in (([], "layout"), ({"folders": [{"id": "x:1", "name": "A"}]}, "id"),
                     ({"folders": [{"id": "u:ab12", "name": ""}]}, "name: required"),
                     ({"folders": [{"id": "u:ab12", "name": "A" * 41}]}, "at most 40"),
                     ({"folders": [{"id": "u:ab12", "name": "A"}, {"id": "u:ab12", "name": "B"}]}, "twice"),
                     ({"folders": [{"id": "u:ab12", "name": "A", "items": "fm:FG1"}]}, "items: a list"),
                     ({"folders": [{"id": "u:ab12", "name": "A", "color": "red"}]}, "folders\\[0\\]"),
                     ({"folders": [{"id": f"u:a{i:04d}", "name": "A"} for i in range(201)]}, "at most 200")):
        with pytest.raises(Exception, match=why):
            radiofolders.clean(bad)


def test_names_are_checked_and_dropped_quietly_for_what_isnt_a_channel():
    got = radiofolders.clean({"folders": [], "names": {"fm:FG1": "  Fire   ground ", "nonsense": "X", "p25:westchester:1234": "",
                                                       "air:Tower": "Tower cab"}})
    assert got == {"folders": [], "names": {"air:Tower": "Tower cab", "fm:FG1": "Fire ground"}}
    assert radiofolders.clean({"folders": [], "names": {}}) == {"folders": []}          # none: no key at all
    for bad, why in (({"folders": [], "names": ["fm:FG1"]}, "names"), ({"folders": [], "names": {"fm:FG1": 7}}, "text"),
                     ({"folders": [], "names": {"fm:FG1": "A" * 41}}, "at most 40")):
        with pytest.raises(Exception, match=why):
            radiofolders.clean(bad)


def test_names_are_saved_with_the_folders_audited_and_only_shown(tmp_path):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True
    cad.settings_post("radio_folders", {"action": "set", "base": "", "folders": LAYOUT["folders"],
                                        "names": {"fm:FG1": "Fire ground 1", "p25:metro:9001": "Dispatch North",
                                                  "p25:metro:9999": "Not heard lately", "junk": "x"}, "operator": "Lt. Test"})
    names = cad.comms_sources()["folders"]["names"]
    assert names == {"fm:FG1": "Fire ground 1", "p25:metro:9001": "Dispatch North", "p25:metro:9999": "Not heard lately"}
    a = [r for r in cad.settings.audit_log("radios") if r["key"] == "radio_folders/layout"][0]
    assert "fm:FG1 → Fire ground 1" in a["new"]
    # the sources themselves (and so the log, the alarms, the rules, the exports) keep their own names
    assert all(s["name"] != "Fire ground 1" for s in cad.comms_sources()["sources"])
    # a rename alone, from an edit started before it: refused
    with pytest.raises(Exception, match="changed meanwhile"):
        cad.settings_post("radio_folders", {"action": "set", "base": "", "folders": LAYOUT["folders"], "names": {"fm:FG1": "Y"},
                                            "operator": "Lt. Other"})
    assert cad.settings_export()["overrides"]["radio_folders"]["layout"]["names"]["fm:FG1"] == "Fire ground 1"
    # a reader who may not see another system: none of its names
    code, raw, _ = CadApi(cad).get("/api/cad/comms/sources", "", None)
    assert json.loads(raw)["folders"]["names"] == {"fm:FG1": "Fire ground 1"}
    rev = radiofolders.rev(cad.radio_folders)
    cad.settings_post("radio_folders", {"action": "reset", "base": rev, "operator": "Lt. Test"})
    assert cad.comms_sources()["folders"] is None


def test_the_folders_are_an_admin_setting_audited_exported_and_in_the_tree(tmp_path):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True          # as on the hub behind Access: every reader is signed in (#419)
    assert needs("/api/cad/settings/radio_folders") == "admin"
    assert cad.state()["radio_folders"] == "" and cad.comms_sources()["folders"] is None       # the automatic grouping
    out = cad.settings_post("radio_folders", {"action": "set", "base": "", "folders": LAYOUT["folders"], "operator": "Lt. Test"})
    rev = radiofolders.rev(LAYOUT)
    assert out["radio_folders"] == {"rev": rev, **LAYOUT}
    assert cad.state()["radio_folders"] == rev
    tree = cad.comms_sources()
    assert tree["folders"] == {"rev": rev, **LAYOUT} and tree["rev"].endswith(rev)       # the tree's rev moves with them

    def audit():
        return [r for r in cad.settings.audit_log("radios") if r["key"] == "radio_folders/layout"]
    (a,) = audit()
    assert (a["operator"], a["old"], a["new"]) == ("Lt. Test", "default", "Fireground (2), PD (2)")
    # the same again: nothing written, nothing audited
    cad.settings_post("radio_folders", {"action": "set", "base": rev, "folders": LAYOUT["folders"], "operator": "Lt. Test"})
    assert len(audit()) == 1
    # an edit started before someone else saved: refused, nothing written
    with pytest.raises(Exception, match="changed meanwhile"):
        cad.settings_post("radio_folders", {"action": "set", "base": "", "folders": [], "operator": "Lt. Other"})
    with pytest.raises(Exception, match="action"):
        cad.settings_post("radio_folders", {"action": "rename", "operator": "Lt. Test"})
    # exported, and checked again on the way in
    exp = cad.settings_export()
    assert exp["overrides"]["radio_folders"] == {"layout": LAYOUT}
    assert cad.settings_post("import", {"action": "dry_run", "settings": exp, "operator": "Lt. Test"})["ok"]
    bad = json.loads(json.dumps(exp))
    bad["overrides"]["radio_folders"] = {"layout": {"folders": [{"id": "nope", "name": "X"}]}}
    with pytest.raises(Exception, match="radio_folders"):
        cad.settings_post("import", {"action": "dry_run", "settings": bad, "operator": "Lt. Test"})
    bad["overrides"]["radio_folders"] = {"other": 1}
    with pytest.raises(Exception, match="radio_folders"):
        cad.settings_post("import", {"action": "dry_run", "settings": bad, "operator": "Lt. Test"})
    # reset: the automatic grouping again
    cad.settings_post("radio_folders", {"action": "reset", "base": rev, "operator": "Lt. Test"})
    assert cad.state()["radio_folders"] == "" and cad.comms_sources()["folders"] is None
    assert audit()[0]["new"] == "default"
    # an import brings them back
    cad.settings_post("import", {"action": "apply", "settings": exp, "operator": "Lt. Test"})
    assert cad.comms_sources()["folders"]["folders"] == LAYOUT["folders"]


def test_a_reader_who_may_not_see_another_system_gets_none_of_its_keys(tmp_path):
    """#419: another P25 system's talkgroups are only for a signed-in reader; a folder holding only those goes too."""
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True
    cad.source_systems = lambda: {"westchester": "County", "metro": "Metro"}
    layout = {"folders": [{"id": "u:pd01", "name": "Tactical", "section": "", "items": ["p25:metro:9001"]},
                          {"id": "u:mx01", "name": "Mixed", "section": "Metro", "items": ["fm:FG1", "p25:metro:9003"]},
                          {"id": "d:metro:Transit", "name": "Rail", "section": "", "items": []},
                          {"id": "u:em01", "name": "Spare", "section": "", "items": []}]}
    cad.settings_post("radio_folders", {"action": "set", "folders": layout["folders"], "operator": "Lt. Test"})
    api = CadApi(cad)

    def tree(who=None):
        code, raw, _ = api.get("/api/cad/comms/sources", "", who)
        assert code == 200
        return json.loads(raw)["folders"]
    signed = tree(Identity(email="someone@example.org"))
    assert [f["name"] for f in signed["folders"]] == ["Tactical", "Mixed", "Rail", "Spare"]
    anon = tree()
    # no other system's keys, no folder of only those, none of its default folders, no heading of its name
    assert [(f["name"], f["section"], f["items"]) for f in anon["folders"]] == [("Mixed", "", ["fm:FG1"]), ("Spare", "", [])]
    assert "metro" not in json.dumps(anon).lower()
    # an admin who may not see them (no sign-in) saves what they saw: the keys they were never shown stay put
    with sourcekeys.request_context(None):
        out = cad.settings_post("radio_folders", {"action": "set", "folders": [
            {"id": "u:em01", "name": "Spare", "section": "", "items": []},
            {"id": "u:mx01", "name": "Mixed up", "section": "", "items": ["fm:FG1"]}], "operator": "Lt. Test"})
    assert "metro" not in json.dumps(out).lower()                     # the answer to that admin: the same filter
    # (the folders they were never shown back after the one they followed; their keys back in their folder)
    assert cad.radio_folders["folders"] == [
        {"id": "u:pd01", "name": "Tactical", "section": "", "items": ["p25:metro:9001"]},
        {"id": "u:em01", "name": "Spare", "section": "", "items": []},
        {"id": "u:mx01", "name": "Mixed up", "section": "", "items": ["fm:FG1", "p25:metro:9003"]},
        {"id": "d:metro:Transit", "name": "Rail", "section": "", "items": []}]


# ---- the rail (node) -----------------------------------------------------------------------------------------------

SCENARIO = r"""
const LAYOUT = %s;
const fold = (rows, custom) => N.folders(rows, custom).map((g) => [g.id, g.name, g.section, g.sources.map((s) => s.key)]);
(async () => {
  // ---- pure: the admins' folders over the automatic grouping ----
  out.auto = fold(SRC, null);
  out.custom = fold(SRC, LAYOUT);
  // a talkgroup heard for the first time: its default folder, made again next to its heading's last folder
  const fresh = SRC.concat([{ ...SRC[3], key: "p25:metro:9200", name: "Bus Ops", group: "Bus" }]);
  out.fresh = fold(fresh, LAYOUT);
  // listed twice: the first folder has it; a key with no source now: not shown; an empty folder: in Edit, not the tree
  out.twice = fold(SRC, { folders: [{ id: "u:a1", name: "A", section: "", items: ["fm:FG1", "p25:gone:1"] },
                                    { id: "u:b1", name: "B", section: "", items: ["fm:FG1"] }] });
  const m = N.model(SRC, N.prefs(null), NOW, new Set(["p25:metro:9100"]), {}, ["fire", "ems", "other"], LAYOUT);
  out.sections = m.sections.map((x) => [x.label, x.groups.map((g) => [g.id, g.name, g.service, g.live])]);
  // a folder's emergency (what a folded one shows): ours first, someone else's prominent, else quiet; muted: no live dot
  const em = (key, kind, loud) => N.model(SRC.map((s) => (s.key === key ? { ...s, emergency: kind } : s)), { ...N.prefs(null), mutes: ["p25:metro:9100"] },
                                          NOW, new Set(["p25:metro:9100"]), {}, loud, LAYOUT).sections.map((x) => x.groups.map((g) => [g.em, g.live]));
  out.em = [em("p25:metro:9003", "other", ["police"]), em("p25:metro:9003", "other", ["fire"]), em("p25:metro:9001", "ours", [])];
  out.draft = N.draftOf(SRC, { folders: LAYOUT.folders.concat([{ id: "u:e1", name: "Spare", section: "", items: [] }]) }).map((f) => [f.id, f.items]);
  out.treeSkipsEmpty = N.model(SRC, N.prefs(null), NOW, new Set(), {}, [], { folders: [{ id: "u:e1", name: "Spare", section: "", items: [] }] })
    .sections.flatMap((x) => x.groups.map((g) => g.id)).includes("u:e1");
  // Edit's moves
  const d = () => N.draftOf(SRC, LAYOUT);
  const ks = (x) => x.map((f) => [f.id, f.items]);
  out.moved = ks(N.moveItem(d(), "air:Tower", "u:fg01", "p25:metro:9100"));
  out.movedEnd = ks(N.moveItem(d(), "fm:FG1", "d:metro:Police"));
  out.stepAcross = ks(N.step(d(), "item", "p25:metro:9100", 1));          // the last of its folder: to the next one's top
  out.stepFirst = ks(N.step(d(), "item", "fm:FG1", -1));                  // the very first: stays
  out.folderUp = N.step(d(), "folder", "d:metro:Police", -1).map((f) => f.id);
  // ▲ ▼ into another heading's run joins it, as a drop does
  const three = [{ id: "a", section: "X", items: [] }, { id: "b", section: "X", items: [] }, { id: "c", section: "Y", items: [] }];
  out.stepJoins = N.step(three, "folder", "c", -1).map((f) => [f.id, f.section]);
  out.folderMoved = N.moveFolder(d(), "u:fg01", "").map((f) => [f.id, f.section]);

  // ---- the page: the admins' folders come with the tree ----
  const posted = [];
  // what the hub holds now: Fireground also has a talkgroup not in the tree now (not heard lately)
  let current = { rev: "r1", folders: LAYOUT.folders.map((f) => (f.id === "u:fg01" ? { ...f, items: f.items.concat(["p25:metro:9999"]) } : f)) };
  const real = global.api;
  global.api = (method, path, body) => {
    if (method === "POST") { posted.push([path, body]); current = body.action === "reset" ? null : { rev: "r2", folders: body.folders }; return Promise.resolve({ ok: true, radio_folders: current }); }
    if (path.startsWith("/api/cad/comms/sources")) { calls.push(path); return Promise.resolve({ rev: "s" + calls.length, now: NOW, minutes: 60, live: ["p25:metro:9100"], sources: SRC, folders: current }); }
    return real(method, path, body);
  };
  global.canDo = () => false;
  N.open(""); await drain();
  const walk = (n) => [n, ...(n.children || []).flatMap(walk)];
  const heads = () => walk($("#rn-tree")).filter((c) => c.dataset && c.dataset.fold).map((c) => [c.dataset.fold, c.getAttribute("aria-expanded"), c.text]);
  const rowsIn = () => walk($("#rn-tree")).filter((c) => c.className && c.className.startsWith("rn-ch")).map((c) => c.children[0].dataset.source);
  out.page = { heads: heads(), rows: rowsIn(), edit: $("#rn-edit").hidden, folds: $("#rn-folds").hidden, allText: $("#rn-folds").textContent };
  // folded: the head with how many channels and the live dot; the channel on screen stays under it
  T.set({ source: "p25:metro:9100" }); await drain();
  const click = (b) => $("#rn-rail").handlers.click[0]({ target: b });
  click(walk($("#rn-tree")).find((c) => c.dataset && c.dataset.fold === "u:fg01")); await drain();
  out.folded = { heads: heads(), rows: rowsIn(), stored: N.prefs(stored.v).folds,
                 dot: walk($("#rn-tree")).find((c) => c.dataset && c.dataset.fold === "u:fg01").children.some((c) => c.className === "rn-dot live") };
  $("#rn-folds").fire("click");
  out.allFolded = { heads: heads().map((h) => h[1]), rows: rowsIn(), text: $("#rn-folds").textContent };
  $("#rn-folds").fire("click");
  out.allShown = { heads: heads().map((h) => h[1]), stored: N.prefs(stored.v).folds };
  // ---- Edit: admins only ----
  st.railSig = ""; T.render();
  out.notAdmin = $("#rn-edit").hidden;
  global.canDo = (need) => need === "admin";
  st.railSig = ""; T.render();
  out.admin = $("#rn-edit").hidden;
  $("#rn-edit").fire("click");
  const editTree = () => walk($("#rn-tree"));
  out.editing = { boxes: editTree().filter((c) => c.className === "rn-edf").map((c) => c.dataset.f),
                  rows: editTree().filter((c) => c.className === "rn-edi").map((c) => c.dataset.k), sig: st.railSig !== "" };
  // a push meanwhile never redraws the draft
  const before = $("#rn-tree").children;
  cad.hooks.forEach((h) => h());
  out.pushKeeps = $("#rn-tree").children === before;
  const btn = (ed, id) => editTree().find((c) => c.dataset && c.dataset.ed === ed && (id === undefined || c.dataset.id === id));
  click(btn("add"));
  const nf = st.edit.folders[0];
  out.added = [nf.id.startsWith("u:"), nf.name, st.edit.folders.length];
  const name = editTree().find((c) => c.dataset && c.dataset.name === nf.id);
  name.value = "Staging"; $("#rn-tree").fire("input", { target: name });
  const to = editTree().find((c) => c.dataset && c.dataset.move === "air:Tower");
  to.value = nf.id; $("#rn-tree").fire("change", { target: to });
  click(btn("fdown", nf.id));
  out.delOff = btn("fdel", nf.id).disabled;
  // drag and drop: a channel onto another folder's channel; a folder dropped on another
  const grip = editTree().find((c) => c.dataset && c.dataset.drag === "k:fm:FG1");
  $("#rn-tree").fire("dragstart", { target: grip });
  const target = editTree().find((c) => c.className === "rn-edi" && c.dataset.k === "p25:metro:9001").children[0];
  $("#rn-tree").fire("dragover", { target });
  $("#rn-tree").fire("drop", { target });
  out.afterEdits = st.edit.folders.map((f) => [f.id, f.name, f.items]);
  const askedBefore = calls.filter((c) => c.includes("/sources")).length;
  click(btn("save")); await drain();
  out.saved = { posted: posted.map(([p, b]) => [p, b.action, b.base, (b.folders || []).map((f) => [f.name, f.items])]),
                edit: st.edit, layout: st.layout && st.layout.rev, asked: calls.filter((c) => c.includes("/sources")).length - askedBefore };
  // Reset, then Save: the setting dropped (reset), not the automatic grouping written out
  $("#rn-edit").fire("click"); click(btn("reset")); click(btn("save")); await drain();
  out.reset = posted.slice(-1)[0][1];
  // a save refused (someone else saved meanwhile): the draft stays, the reason shown
  global.api = (method, path, body) => (method === "POST" ? Promise.reject(new Error("the folders were changed meanwhile")) : real(method, path, body));
  $("#rn-edit").fire("click"); click(btn("save")); await drain();
  out.refused = { editing: !!st.edit, error: editTree().find((c) => c.className === "error").textContent };
  click(btn("cancel"));
  out.cancelled = st.edit;
  // another admin saved: the push carries the new rev, the tree is asked again once
  global.api = (method, path, body) => { if (path.startsWith("/api/cad/comms/sources")) { calls.push(path); return Promise.resolve({ rev: "x", now: NOW, minutes: 60, live: [], sources: SRC, folders: { rev: "r9", folders: [] } }); } return real(method, path, body); };
  const n0 = calls.filter((c) => c.includes("/sources")).length;
  cad.state.radio_folders = "r9";
  cad.hooks.forEach((h) => h()); await drain(); cad.hooks.forEach((h) => h()); await drain();
  out.pushed = { asked: calls.filter((c) => c.includes("/sources")).length - n0, rev: st.layout.rev };

  // ---- a channel's name on the rail (display only) ----
  current = { rev: "r9", folders: [] };
  global.api = (method, path, body) => {
    if (method === "POST") { posted.push([path, body]); current = body.action === "reset" ? null : { rev: "r5", folders: body.folders, ...(body.names ? { names: body.names } : {}) };
                             return Promise.resolve({ ok: true, radio_folders: current }); }
    if (path.startsWith("/api/cad/comms/sources")) { calls.push(path); return Promise.resolve({ rev: "y" + calls.length, now: NOW, minutes: 60, live: [],
      sources: SRC.map((x) => (x.key === "fm:FG1" ? { ...x, mhz: "154.1" } : x.key === "p25:metro:9001" ? { ...x, talkgroup: 9001 } : x)), folders: current }); }
    return real(method, path, body);
  };
  global.RadioView = { ...RadioView, row: (t) => { const li = new El("li", "rt"); const meta = new El("div", "meta"); meta.append(new El("span", "chip src", t.channel)); li.append(meta); return li; } };
  T.sources(true); await drain();
  $("#rn-edit").fire("click");
  const chname = (k) => editTree().find((c) => c.dataset && c.dataset.chname === k);
  out.nameField = { value: chname("p25:metro:9001").value, placeholder: chname("p25:metro:9001").placeholder, title: chname("fm:FG1").title };
  const typed = (k, v) => { const n = chname(k); n.value = v; $("#rn-tree").fire("input", { target: n }); };
  typed("p25:metro:9001", "  Dispatch North "); typed("fm:FG1", "Fire Ground"); typed("fm:FG1", "   ");
  click(btn("save")); await drain();
  out.namesPosted = posted.slice(-1)[0][1];
  T.set({ view: "police", source: "" }); await drain();
  const railRow = walk($("#rn-tree")).find((c) => c.dataset && c.dataset.source === "p25:metro:9001");
  const fgRow = walk($("#rn-tree")).find((c) => c.dataset && c.dataset.source === "fm:FG1");
  const chips = walk($("#rn-list")).filter((c) => c.className === "chip src");
  out.named = { rail: railRow.text, title: railRow.title, fgTitle: fgRow.title, chips: chips.map((c) => [c.textContent, c.title]),
                kept: st.rows.filter((t) => t.source === "p25:metro:9001").map((t) => t.channel)[0], asked: last("view") };
  T.set({ source: "p25:metro:9001" }); await drain();
  out.crumb = $("#rn-crumb").textContent;
  out.sourceAsked = last("source");
  // Reset: the names go too, and the setting is dropped
  $("#rn-edit").fire("click"); click(btn("reset"));
  out.resetField = chname("p25:metro:9001").value;
  click(btn("save")); await drain();
  out.namesReset = posted.slice(-1)[0][1];
  // a name set meanwhile from Settings > Radios > Channels (the folders as they were): merged, saved once more
  const mergeRun = async (folders2) => {
    current = { rev: "m1", folders: [], names: { "fm:FG1": "Ground" } };
    let first = true;
    global.api = (method, path, body) => {
      if (method === "POST") {
        posted.push([path, body]);
        if (first) { first = false; current = { rev: "m2", folders: folders2, names: { "fm:FG1": "Ground", "air:Tower": "Cab" } };
                     return Promise.reject(new Error("the folders were changed meanwhile")); }
        current = { rev: "m3", folders: body.folders, names: body.names };
        return Promise.resolve({ ok: true, radio_folders: current });
      }
      if (path.startsWith("/api/cad/comms/sources")) { calls.push(path); return Promise.resolve({ rev: "z" + calls.length, now: NOW, minutes: 60, live: [], sources: SRC, folders: current }); }
      return real(method, path, body);
    };
    T.sources(true); await drain();
    $("#rn-edit").fire("click");
    typed("p25:metro:9001", "North"); typed("fm:FG1", "Ground 1");
    const n = posted.length;
    click(btn("save")); await drain();
    return { posts: posted.slice(n).map(([, b]) => [b.base, b.names]), editing: !!st.edit,
             error: st.edit ? editTree().find((c) => c.className === "error").textContent : "" };
  };
  out.merged = await mergeRun([]);
  if (st.edit) click(btn("cancel"));
  // the folders changed meanwhile too: refused as before, nothing merged
  out.notMerged = await mergeRun([{ id: "u:zz99", name: "Theirs", section: "", items: ["fm:FG1"] }]);
  if (st.edit) click(btn("cancel"));
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack); process.exit(1); });
"""


@pytest.fixture(scope="module")
def nav(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    head, _ = HARNESS.split("(async () => {", 1)
    h = tmp_path_factory.mktemp("folders") / "folders.js"
    h.write_text(head + SCENARIO % json.dumps(LAYOUT))
    p = subprocess.run([NODE, str(h), str(STATIC / "radio-nav.js"), "-", "all"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_automatic_grouping_is_the_default(nav):
    assert nav["auto"] == [["d:analog:Analog", "Analog", "Analog", ["fm:FG1"]],
                           ["d:metro:Police", "Police", "Metro", ["p25:metro:9001", "p25:metro:9003"]],
                           ["d:metro:Transit", "Transit", "Metro", ["p25:metro:9100"]],
                           ["d:airband:Airband", "Airband", "Airband", ["air:Tower"]]]


def test_the_admins_folders_and_nothing_ever_drops_off(nav):
    assert nav["custom"] == [["u:fg01", "Fireground", "", ["fm:FG1", "p25:metro:9100"]],
                             ["d:metro:Police", "PD", "Metro", ["p25:metro:9003", "p25:metro:9001"]],
                             ["d:airband:Airband", "Airband", "Airband", ["air:Tower"]]]     # never placed: its default
    # a new talkgroup: its default folder, made again after the last folder under its heading
    assert nav["fresh"][2] == ["d:metro:Bus", "Bus", "Metro", ["p25:metro:9200"]] and nav["fresh"][3][0] == "d:airband:Airband"
    assert nav["twice"][:2] == [["u:a1", "A", "", ["fm:FG1"]], ["u:b1", "B", "", []]]
    assert nav["sections"] == [["", [["u:fg01", "Fireground", "", True]]],                 # fire + transit: no one mark
                               ["Metro", [["d:metro:Police", "PD", "police", False]]],
                               ["Airband", [["d:airband:Airband", "Airband", "air", False]]]]
    assert ["u:e1", []] in nav["draft"] and nav["treeSkipsEmpty"] is False
    assert nav["em"] == [[[["", False]], [["other", False]], [["", False]]],      # Rail Ops muted: no live dot
                         [[["", False]], [["quiet", False]], [["", False]]],
                         [[["", False]], [["ours", False]], [["", False]]]]


def test_edits_moves(nav):
    assert nav["moved"][0] == ["u:fg01", ["fm:FG1", "air:Tower", "p25:metro:9100"]] and nav["moved"][2] == ["d:airband:Airband", []]
    assert nav["movedEnd"][:2] == [["u:fg01", ["p25:metro:9100"]], ["d:metro:Police", ["p25:metro:9003", "p25:metro:9001", "fm:FG1"]]]
    assert nav["stepAcross"][:2] == [["u:fg01", ["fm:FG1"]], ["d:metro:Police", ["p25:metro:9100", "p25:metro:9003", "p25:metro:9001"]]]
    assert nav["stepFirst"][0] == ["u:fg01", ["fm:FG1", "p25:metro:9100"]]
    assert nav["folderUp"] == ["d:metro:Police", "u:fg01", "d:airband:Airband"]
    assert nav["stepJoins"] == [["a", "X"], ["c", "X"], ["b", "X"]]
    assert nav["folderMoved"] == [["d:metro:Police", "Metro"], ["d:airband:Airband", "Airband"], ["u:fg01", ""]]


def test_folders_fold_per_viewer_and_the_channel_on_screen_stays(nav):
    p = nav["page"]
    assert [h[0] for h in p["heads"]] == ["u:fg01", "d:metro:Police", "d:airband:Airband"]
    assert all(h[1] == "true" for h in p["heads"]) and p["edit"] is True and p["folds"] is False and p["allText"] == "⊟"
    assert p["rows"] == ["fm:FG1", "p25:metro:9100", "p25:metro:9003", "p25:metro:9001", "air:Tower"]
    f = nav["folded"]
    fg = f["heads"][0]
    assert fg[1] == "false" and "2" in fg[2].split() and f["stored"] == ["u:fg01"]             # its name, 2 channels
    assert f["dot"] is True                                                                     # Rail Ops is live
    assert f["rows"] == ["p25:metro:9100", "p25:metro:9003", "p25:metro:9001", "air:Tower"]   # FG1 folded away; Rail Ops on screen
    assert nav["allFolded"]["heads"] == ["false"] * 3 and nav["allFolded"]["text"] == "⊞"
    assert nav["allFolded"]["rows"] == ["p25:metro:9100"]
    assert nav["allShown"] == {"heads": ["true"] * 3, "stored": []}


def test_edit_is_for_admins_and_saves_once_for_everyone(nav):
    assert nav["notAdmin"] is True and nav["admin"] is False
    e = nav["editing"]
    assert e["boxes"] == ["u:fg01", "d:metro:Police", "d:airband:Airband"]
    assert e["rows"] == ["fm:FG1", "p25:metro:9100", "p25:metro:9003", "p25:metro:9001", "air:Tower"]
    assert nav["pushKeeps"] is True
    assert nav["added"] == [True, "New folder", 4] and nav["delOff"] is True                    # not empty: can't delete
    ids = [f[0] for f in nav["afterEdits"]]
    assert ids[0] == "u:fg01" and nav["afterEdits"][1][1:] == ["Staging", ["air:Tower"]]       # moved down, renamed, filled
    assert nav["afterEdits"][0][2] == ["p25:metro:9100"]
    assert nav["afterEdits"][2][2] == ["p25:metro:9003", "fm:FG1", "p25:metro:9001"]           # dropped on a row: before it
    (path, action, base, folders), = nav["saved"]["posted"][:1]
    assert (path, action, base) == ("/api/cad/settings/radio_folders", "set", "r1")
    assert folders[1] == ["Staging", ["air:Tower"]]
    assert folders[0] == ["Fireground", ["p25:metro:9100", "p25:metro:9999"]]                 # not in the tree now: kept
    assert nav["saved"]["edit"] is None and nav["saved"]["layout"] == "r2"
    assert nav["saved"]["asked"] == 1                     # the tree asked again after the save
    assert nav["reset"]["action"] == "reset" and "folders" not in nav["reset"]
    assert nav["refused"] == {"editing": True, "error": "the folders were changed meanwhile"}
    assert nav["cancelled"] is None
    assert nav["pushed"] == {"asked": 1, "rev": "r9"}


def test_a_channels_name_on_the_rail_is_display_only(nav):
    f = nav["nameField"]
    assert f["value"] == "" and f["placeholder"] == "PD Dispatch" and f["title"].startswith("FG1 (154.1 MHz)")
    p = nav["namesPosted"]
    assert p["action"] == "set" and p["names"] == {"p25:metro:9001": "Dispatch North"}       # trimmed; blank: its own
    n = nav["named"]
    assert "Dispatch North" in n["rail"] and "PD Dispatch" not in n["rail"]
    # the hover: the name in full (a narrow rail cuts it short), the channel's own name and number beside it
    assert n["title"].startswith("Dispatch North (PD Dispatch, TG 9001) · ") and n["fgTitle"].startswith("FG1 (154.1 MHz) · ")
    # the list's channel chip: the row's own (cad.js radioRow asks radio-names.js, the one resolver: tests/test_radio_names.py),
    # never a name of the rail's own put over it; the row itself (filters, links) untouched
    assert n["chips"] and all(c == [n["kept"], ""] for c in n["chips"])
    assert n["kept"] == "p25:metro:9001" and n["asked"] == "police"
    assert nav["crumb"].endswith("› Dispatch North") and nav["sourceAsked"] == "p25:metro:9001"    # the hub asked by key
    assert nav["resetField"] == "" and nav["namesReset"]["action"] == "reset"


def test_a_save_refused_for_a_name_set_meanwhile_merges_the_names_and_saves_again(nav):
    # this edit: 9001 -> North, FG1 Ground -> Ground 1; meanwhile Settings named air:Tower; the folders unchanged
    assert nav["merged"] == {"posts": [["m1", {"fm:FG1": "Ground 1", "p25:metro:9001": "North"}],
                                       ["m2", {"fm:FG1": "Ground 1", "air:Tower": "Cab", "p25:metro:9001": "North"}]],
                             "editing": False, "error": ""}
    # the folders changed too: no merge, the refusal shown, the draft kept
    assert nav["notMerged"]["editing"] and "changed meanwhile" in nav["notMerged"]["error"]
    assert [b for b, _ in nav["notMerged"]["posts"]] == ["m1"]


def test_a_channels_frequency_and_talkgroup_number_come_with_the_tree(tmp_path):
    """For the hover beside a renamed channel: the FM / airband channel's MHz (the app's config), a talkgroup's number."""
    from tests.test_radios import Clock, cad, tx
    clock = Clock()
    c = cad(tmp_path, clock, airband=("Tower",))
    c.main_tg = lambda tg: tg == 307
    c.record_traffic(tx("fg-1", clock.t - 50, rig="E63"))
    c.record_traffic(dict(tx("twr-1", clock.t - 30, "Tower"), mhz=119.7))
    c.record_traffic(dict(tx("307-1-call_1-t0", clock.t - 40, "Fire/EMS 15", "E60", "E60", 307), ours=True, group="Fire"))
    c.source_freqs = lambda: {"FG1": "154.1", "Tower": "118.575"}
    by = {s["key"]: s for s in c.comms_sources()["sources"]}
    assert by["fm:FG1"]["mhz"] == "154.1" and by["air:Tower"]["mhz"] == "118.575"
    assert by["p25:westchester:307"]["mhz"] is None and by["p25:westchester:307"]["talkgroup"] == 307
