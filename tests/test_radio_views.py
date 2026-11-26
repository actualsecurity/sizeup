"""The Radio rail's views set in the app (sizeup/cad/radioviews.py, static/radio-views.js): the department's (admins,
audited, the coded ones by default so nothing disappears), each person's own and their hide / order (prefs namespace
radio_views), what a reader who may not see another P25 system is sent, and the hub's query for a view's filter (many
sources, several units, only what is on an incident). Every view, unit, talkgroup and channel here is invented."""

from __future__ import annotations

import json
import subprocess

import pytest

from sizeup import sourcekeys
from sizeup.cad import prefs as prefs_mod
from sizeup.cad import radioviews
from sizeup.cad.api import CadApi, needs
from sizeup.cad.comms import BRANCH_MAX
from sizeup.cad.store import CadError
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_comms_views import MAIN, c, ids, other, q  # noqa: F401  (the fixture c)
from tests.test_radio_emergency import NODE, STATIC
from tests.test_radios import _event_with, tx

MUTUAL = {"id": "u:mutual1", "name": "Mutual aid", "mark": "M", "color": "#C2410C",
          "filter": {"services": ["fire", "ems"], "sources": ["fm:FG2"], "units": ["E60"], "linked": True}}
METRO = {"id": "u:metro01", "name": "Metro PD", "mark": "9", "color": "", "filter": {"systems": ["metro"]}}


# ---- the shape --------------------------------------------------------------------------------------------------------

def test_a_view_is_checked_and_kept_compact():
    got = radioviews.clean([MUTUAL, {**METRO, "filter": {"systems": ["metro"], "ours": False, "units": [], "linked": False}}])
    assert got[0] == {**MUTUAL, "color": "#c2410c"}
    assert got[1]["filter"] == {"systems": ["metro"]}                     # nothing that says nothing is kept
    assert radioviews.clean_filter({"sources": ["tg:307"]}) == {"sources": ["p25:westchester:307"]}    # qualified
    for bad in ({**MUTUAL, "id": "nope"}, {**MUTUAL, "id": "p:mine01"}, {**MUTUAL, "name": ""}, {**MUTUAL, "name": "x" * 25},
                {**MUTUAL, "mark": "MM"}, {**MUTUAL, "mark": "m"}, {**MUTUAL, "color": "red"}, {**MUTUAL, "extra": 1},
                {**MUTUAL, "filter": {"services": ["cops"]}}, {**MUTUAL, "filter": {"systems": ["Metro PD"]}},
                {**MUTUAL, "filter": {"sources": ["FG 2"]}}, {**MUTUAL, "filter": {"ours": 1}},
                {**MUTUAL, "filter": {"units": ["E60;DROP"]}}, {**MUTUAL, "filter": {"units": [f"U{i}" for i in range(21)]}},
                {**MUTUAL, "filter": {"radio": ["x"]}}):
        with pytest.raises(CadError):
            radioviews.clean([bad])
    with pytest.raises(CadError, match="twice"):
        radioviews.clean([MUTUAL, MUTUAL])
    with pytest.raises(CadError, match="at most"):
        radioviews.clean([{**MUTUAL, "id": f"u:v{i:04d}"} for i in range(radioviews.MAX_VIEWS + 1)])
    # the coded views, as they were: the default
    assert [(v["id"], v["name"], v["mark"]) for v in radioviews.SEED] == [
        ("ours", "Our calls", "F"), ("police", "Police", "P"), ("ems", "EMS", "E"), ("transit", "Transit", "T"),
        ("air", "Air", "A"), ("all", "All", "∗")]
    assert radioviews.clean(radioviews.SEED) == radioviews.SEED


def test_each_persons_own_views_hide_and_order_in_prefs():
    v = {"hidden": ["police", "p:mine01"], "order": ["p:mine01", "ours", "u:mutual1"],
         "mine": [{"id": "p:mine01", "name": "Engine 60", "mark": "6", "color": "", "filter": {"units": ["E60"]}}]}
    assert json.loads(prefs_mod.validate("radio_views", v)) == v
    for bad in ({"hidden": ["no such"]}, {"order": "ours"}, {"mine": [{**MUTUAL}]},            # u: is the department's
                {"mine": [{"id": "p:a1b2", "name": "x", "mark": "X", "filter": {"services": ["cops"]}}]},
                {"mine": [{"id": f"p:m{i:04d}", "name": "x", "mark": "X", "filter": {}} for i in range(radioviews.MAX_MINE + 1)]},
                {"theme": "dark"}, []):
        with pytest.raises(prefs_mod.PrefsError) as e:
            prefs_mod.validate("radio_views", bad)
        assert e.value.code == 400, bad


# ---- the department's: admins, audited, in the tree and the state ---------------------------------------------------

def test_the_departments_views_are_an_admin_setting_audited_exported_and_in_the_tree(tmp_path):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True
    assert needs("/api/cad/settings/radio_views") == "admin"
    assert cad.state()["radio_views"] == ""
    tree = cad.comms_sources()
    assert tree["views"] == {"rev": "", "custom": False, "views": radioviews.SEED}       # nothing disappears
    views = [radioviews.SEED[0], MUTUAL, *radioviews.SEED[1:]]
    out = cad.settings_post("radio_views", {"action": "set", "base": "", "views": views, "operator": "Lt. Test"})
    kept = radioviews.clean(views)
    rev = radioviews.rev(kept)
    assert out["radio_views"] == {"rev": rev, "custom": True, "views": kept}
    assert cad.state()["radio_views"] == rev and cad.comms_sources()["views"]["views"] == kept
    assert cad.comms_sources()["rev"] != tree["rev"]                       # the tree's rev moves with them

    def audit():
        return [r for r in cad.settings.audit_log("radios") if r["key"] == "radio_views/views"]
    (a,) = audit()
    assert a["operator"] == "Lt. Test" and a["old"] == "default"
    assert a["new"].startswith("Our calls (F), Mutual aid (M, fire/ems E60 1 ch linked), Police (P)")
    cad.settings_post("radio_views", {"action": "set", "base": rev, "views": views, "operator": "Lt. Test"})
    assert len(audit()) == 1                                                # the same again: nothing written
    with pytest.raises(CadError, match="changed meanwhile"):
        cad.settings_post("radio_views", {"action": "set", "base": "", "views": [], "operator": "Lt. Other"})
    with pytest.raises(CadError, match="action"):
        cad.settings_post("radio_views", {"action": "rename", "operator": "Lt. Test"})
    with pytest.raises(CadError):
        cad.settings_post("radio_views", {"action": "set", "views": [{**MUTUAL, "mark": "??"}], "operator": "Lt. Test"})
    assert len(audit()) == 1
    # every view deleted: kept as none (the rail then has no VIEWS), not the default
    cad.settings_post("radio_views", {"action": "set", "base": rev, "views": [], "operator": "Lt. Test"})
    assert cad.comms_sources()["views"] == {"rev": radioviews.rev([]), "custom": True, "views": []}
    # exported and imported with the rest
    exp = cad.settings_export()
    assert exp["overrides"]["radio_views"] == {"views": []}
    # reset: the coded ones again; the coded ones saved as they are: the default too
    cad.settings_post("radio_views", {"action": "reset", "operator": "Lt. Test"})
    assert cad.state()["radio_views"] == "" and cad.comms_sources()["views"]["custom"] is False
    cad.settings_post("radio_views", {"action": "set", "views": views, "operator": "Lt. Test"})
    cad.settings_post("radio_views", {"action": "set", "views": radioviews.SEED, "operator": "Lt. Test"})
    assert cad.state()["radio_views"] == "" and audit()[0]["new"] == "default"
    cad.settings_post("import", {"action": "apply", "settings": exp, "operator": "Lt. Test"})
    assert cad.comms_sources()["views"]["views"] == []


def test_only_admins_may_change_them(tmp_path):
    from tests.test_roles import CASEY, JORDAN, VAL, call, people
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    body = {"action": "set", "views": [MUTUAL]}
    for who in (JORDAN, VAL):                     # a dispatcher, a viewer
        code, out = call(api, "/api/cad/settings/radio_views", body, who)
        assert code == 403, (who, out)
    assert cad.radio_views is None
    code, out = call(api, "/api/cad/settings/radio_views", body, CASEY)
    assert code == 200 and [v["id"] for v in cad.radio_views] == ["u:mutual1"]


def test_a_reader_who_may_not_see_another_system_gets_none_of_it(tmp_path):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True
    views = [radioviews.SEED[0], METRO, {**MUTUAL, "filter": {"sources": ["fm:FG2", "p25:metro:9001"], "systems": ["metro", "analog"]}}]
    cad.settings_post("radio_views", {"action": "set", "views": views, "operator": "Lt. Test"})
    api = CadApi(cad)

    def tree(who=None):
        code, raw, _ = api.get("/api/cad/comms/sources", "", who)
        assert code == 200
        return json.loads(raw)["views"]
    signed = tree(Identity(email="someone@example.org"))
    assert [v["id"] for v in signed["views"]] == ["ours", "u:metro01", "u:mutual1"]
    anon = tree()
    # Metro PD named only the other system: not sent (stripped it would read as All); the mixed one without it
    assert [v["id"] for v in anon["views"]] == ["ours", "u:mutual1"]
    assert anon["views"][1]["filter"] == {"systems": ["analog"], "sources": ["fm:FG2"]}
    assert "metro" not in json.dumps(anon).lower()
    # an admin who may not see them saves what they saw: what they were never shown stays
    with sourcekeys.request_context(None):
        cad.settings_post("radio_views", {"action": "set", "views": [
            {**anon["views"][1], "name": "Mutual"}, anon["views"][0]], "operator": "Lt. Test"})
    # (Metro PD back after the view it followed)
    assert [v["id"] for v in cad.radio_views] == ["u:mutual1", "ours", "u:metro01"]
    assert cad.radio_views[0]["filter"] == {"systems": ["analog", "metro"], "sources": ["fm:FG2", "p25:metro:9001"]}
    assert cad.radio_views[0]["name"] == "Mutual"


# ---- the hub's query for a view's filter ------------------------------------------------------------------------------

def test_several_units_and_only_what_is_on_an_incident(c):  # noqa: F811
    assert ids(q(c, view="all", unit="E63,E60")) == [f"{MAIN}-1-call_1-t0", "fg-1"]
    assert ids(q(c, view="all", unit="E60")) == [f"{MAIN}-1-call_1-t0"]
    with pytest.raises(CadError, match="unit"):
        q(c, view="all", unit=",".join(f"U{i}" for i in range(21)))
    assert ids(q(c, view="all", linked="1")) == []
    inc = _event_with(c, ["E63"], c.clockref)
    assert c.comms.link("pd-2", inc["id"], "op").get("error") is None
    assert ids(q(c, view="all", linked="1")) == ["pd-2"]
    assert ids(q(c, view="police", linked="1")) == ["pd-2"]
    assert ids(q(c, view="all", linked="1", unit="E60")) == []


def test_a_view_resolved_to_many_sources_is_one_walk(c):  # noqa: F811
    t = c.clockref.t
    for i in range(BRANCH_MAX + 6):
        c.record_traffic(other(f"x-{i}", t - 100 - i, 7000 + i, f"Ops {i}", "other", "Ops"))
    keys = [f"p25:metro:{7000 + i}" for i in range(BRANCH_MAX + 6)] + ["fm:FG1"]
    got = ids(q(c, view="all", source=",".join(keys), limit="200"))
    assert got[0] == "fg-1" and len(got) == BRANCH_MAX + 7 and set(got) == {"fg-1", *(f"x-{i}" for i in range(BRANCH_MAX + 6))}
    assert ids(q(c, view="all", source=",".join(keys), limit="3")) == ["fg-1", "x-0", "x-1"]
    with pytest.raises(CadError, match="source"):
        q(c, view="all", source=",".join(f"p25:metro:{i}" for i in range(201)))


# ---- the page's side (node): a view's filter, its query, the rail's views ---------------------------------------------

SCRIPT = r"""
global.window = global;
global.localStorage = { getItem: () => null, setItem: () => {} };
eval(require("fs").readFileSync(process.argv[1], "utf8"));
const R = window.RadioViews;
const SRC = [
  { key: "fm:FG1", system: "analog", service: "fire", ours: true },
  { key: "fm:FG2", system: "analog", service: "fire", ours: false },
  { key: "p25:westchester:307", system: "westchester", service: "fire", ours: true },
  { key: "p25:westchester:555", system: "westchester", service: "other", ours: false },
  { key: "p25:metro:9001", system: "metro", service: "police", ours: false },
  { key: "p25:metro:9100", system: "metro", service: "transit", ours: false },
  { key: "air:Tower", system: "airband", service: "air", ours: false },
];
const v = (filter, id = "u:test01") => ({ id, name: "X", mark: "X", color: "", filter });
const keys = (f) => SRC.filter((s) => R.matches(s, f)).map((s) => s.key);
const out = {
  seedQueries: R.SEED.map((s) => R.resolve(s, SRC)),
  matches: {
    ours: keys({ ours: true }), police: keys({ services: ["police"] }), all: keys({}),
    metroOrFg2: keys({ systems: ["metro"], sources: ["fm:FG2"] }),
    fireInCounty: keys({ services: ["fire"], systems: ["westchester"] }),
    oursPlusTower: keys({ ours: true, sources: ["air:Tower"] }),
  },
  q: {
    fireCounty: R.resolve(v({ services: ["fire"], systems: ["westchester"] }), SRC),
    fireOnly: R.resolve(v({ services: ["fire"] }), SRC),
    twoServices: R.resolve(v({ services: ["police", "transit"] }), SRC),
    oursPlus: R.resolve(v({ ours: true, sources: ["air:Tower"], units: ["E60", "L31"], linked: true }), SRC),
    unitsOnly: R.resolve(v({ units: ["E60"] }), SRC),
    oneSystem: R.resolve(v({ systems: ["gone"] }), SRC),
    nothing: R.resolve(v({ systems: ["gone", "nope"] }), SRC),
  },
  shows: [R.shows(R.SEED[2], SRC), R.shows(R.SEED[1], SRC), R.shows(v({ systems: ["gone"] }), SRC), R.shows(R.SEED[0], [])],
  badge: [R.service(R.SEED[0]), R.service(R.SEED[1]), R.service(R.SEED[5]), R.service(v({ services: ["fire", "ems"] }))],
};
// this person's: their order, their own, hidden ones left out; every one hidden: none
const mine = { id: "p:mine01", name: "Mine", mark: "M", color: "", filter: { units: ["E60"] } };
const p = R.prefsOf({ hidden: ["police"], order: ["p:mine01", "all", "ours"], mine: [mine] });
out.order = R.list(R.SEED, p).map((x) => [x.id, x.scope]);
out.every = R.all(R.SEED, p).filter((x) => x.hidden).map((x) => x.id);
out.allHidden = R.list(R.SEED, R.prefsOf({ hidden: R.SEED.map((x) => x.id) })).length;
out.moved = R.moved(["a", "b", "c"], "c", -1);
out.checked = [R.checked({ name: "  Mutual   aid ", mark: "m", color: "#ABCDEF", filter: { services: ["fire"], units: [" E60 ", ""], linked: true } }),
               R.checked({ name: "", mark: "M", filter: {} })[1], R.checked({ name: "X", mark: "MM", filter: {} })[1],
               R.checked({ name: "X", mark: "*", filter: {} })[0].mark];
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def page():
    if NODE is None:
        pytest.skip("node not installed")
    p = subprocess.run([NODE, "-e", SCRIPT, str(STATIC / "radio-views.js")], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_coded_views_ask_the_hub_as_before(page):
    # ours, the four services and All: the same fast paths as the coded views (Our calls with its notes)
    assert page["seedQueries"] == [{"view": "ours"}, {"view": "police"}, {"view": "ems"}, {"view": "transit"},
                                   {"view": "air"}, {"view": "all", "all": True}]
    assert page["shows"] == [False, True, True, True]          # no EMS source: no EMS view, as before; one made here: shown
    assert page["badge"] == ["fire", "police", "", ""]


def test_a_filter_any_within_all_across_and_channels_added(page):
    m = page["matches"]
    assert m["ours"] == ["fm:FG1", "p25:westchester:307"]
    assert m["police"] == ["p25:metro:9001"]
    assert len(m["all"]) == 7
    assert m["metroOrFg2"] == ["fm:FG2", "p25:metro:9001", "p25:metro:9100"]
    assert m["fireInCounty"] == ["p25:westchester:307"]
    assert m["oursPlusTower"] == ["fm:FG1", "p25:westchester:307", "air:Tower"]


def test_a_filter_is_the_hubs_query(page):
    qs = page["q"]
    assert qs["fireCounty"] == {"view": "all", "service": "fire", "system": "westchester"}
    assert qs["fireOnly"] == {"view": "all", "service": "fire"}
    assert qs["twoServices"] == {"view": "all", "source": "p25:metro:9001,p25:metro:9100"}
    assert qs["oursPlus"] == {"view": "all", "source": "air:Tower,fm:FG1,p25:westchester:307", "unit": "E60,L31", "linked": "1"}
    assert qs["unitsOnly"] == {"view": "all", "unit": "E60", "all": True}
    assert qs["oneSystem"] == {"view": "all", "system": "gone"}
    assert qs["nothing"] is None                                # nothing can match: no request


def test_this_persons_order_hidden_and_own(page):
    assert page["order"] == [["p:mine01", "mine"], ["all", "dept"], ["ours", "dept"], ["ems", "dept"], ["transit", "dept"],
                             ["air", "dept"]]
    assert page["every"] == ["police"] and page["allHidden"] == 0
    assert page["moved"] == ["a", "c", "b"]
    good, no_name, bad_mark, star = page["checked"]
    assert good == [{"name": "Mutual aid", "mark": "M", "color": "#abcdef",
                     "filter": {"services": ["fire"], "units": ["E60"], "linked": True}}, ""]
    assert no_name == "Name required" and bad_mark.startswith("Badge") and star == "∗"


# ---- the rail (node, the Radio page's harness) -----------------------------------------------------------------------

RAIL = r"""
(async () => {
  const R = window.RadioViews;
  const names = () => $("#rn-views").children.map((b) => [b.dataset.view, b.text]);
  N.open(""); await drain();
  out.coded = { views: names(), view: st.f.view, asked: last("view"), header: !$("#rn-views-h").hidden };
  // the department's set changed: a view of its own badge and colour, Police gone; the rail follows
  R.setShared({ rev: "r1", custom: true, views: [R.SEED[0], { id: "u:metro01", name: "Metro", mark: "9", color: "#c2410c",
                                                             filter: { systems: ["metro"], units: ["E71"] } }, R.SEED[5]] });
  await drain();
  out.dept = { views: names(), color: $("#rn-views").children[1].children[0].style.props["--c"] };
  T.set({ view: "u:metro01" }); await drain();
  out.metro = { view: last("view"), system: last("system"), unit: last("unit"), crumb: $("#rn-crumb").textContent,
                n: $("#rn-views").children.find((b) => b.dataset.view === "u:metro01").text };
  // this person hides it: the view on screen is gone, their first view instead
  R.setMine({ hidden: ["u:metro01"], order: [], mine: [] }); await drain();
  out.hidden = { views: names().map((x) => x[0]), view: st.f.view };
  // their own view, first in their order: two services, resolved to the tree's sources
  R.setMine({ hidden: [], order: ["p:mine01"], mine: [{ id: "p:mine01", name: "PD+Rail", mark: "R", color: "",
                                                         filter: { services: ["police", "transit"] } }] }); await drain();
  T.set({ view: "p:mine01" }); await drain();
  out.mine = { first: names()[0][0], view: last("view"), source: last("source") };
  // nothing can match: none on screen, no request
  R.setMine({ hidden: [], order: [], mine: [{ id: "p:mine01", name: "Gone", mark: "G", color: "", filter: { systems: ["a", "b"] } }] });
  await drain();
  const n = comms().length;
  T.set({ view: "p:mine01" }); await drain();
  out.none = { asked: comms().length - n, rows: st.rows.length, empty: $("#rn-list").children.length };
  // every view hidden: no VIEWS header, no tabs; everything on screen
  R.setMine({ hidden: ["ours", "u:metro01", "all", "p:mine01"], order: [], mine: [{ id: "p:mine01", name: "Gone", mark: "G", color: "", filter: {} }] });
  await drain();
  out.allHidden = { header: $("#rn-views-h").hidden, tabs: $("#rn-views").hidden, n: $("#rn-views").children.length, view: st.f.view,
                    asked: last("view"), source: last("source") };
  // an old link to a view that is no more: the first view
  R.setMine({ hidden: [], order: [], mine: [] }); await drain();
  T.fromHash("view=police"); out.oldLink = st.f.view;
  // the ✎: the editor in the views' place, and back
  T.vedit(true); out.edit = { box: !$("#rn-vbox").hidden, tabs: $("#rn-views").hidden, expanded: $("#rn-vedit").attrs["aria-expanded"] };
  T.vedit(false); out.edited = { box: !$("#rn-vbox").hidden, tabs: $("#rn-views").hidden };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack); process.exit(1); });
"""


@pytest.fixture(scope="module")
def rail(tmp_path_factory):
    from tests.test_radio_nav import HARNESS
    if NODE is None:
        pytest.skip("node not installed")
    head, _ = HARNESS.split("(async () => {", 1)
    h = tmp_path_factory.mktemp("views") / "views.js"
    # (the editor's DOM is the page's: a stand-in here, its own browser test draws it)
    h.write_text(head.replace("eval(fs.readFileSync(process.argv[2], \"utf8\"));",
                              "window.RadioViews.editor = () => ({ stop() {} });\neval(fs.readFileSync(process.argv[2], \"utf8\"));")
                 + RAIL)
    p = subprocess.run([NODE, str(h), str(STATIC / "radio-nav.js"), "-", "all"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_rail_shows_the_coded_views_by_default(rail):
    assert [v for v, _ in rail["coded"]["views"]] == ["ours", "police", "transit", "air", "all"]   # no EMS source: no EMS
    assert rail["coded"]["view"] == "ours" and rail["coded"]["asked"] == "ours" and rail["coded"]["header"]


def test_the_rail_follows_the_departments_and_this_persons_views(rail):
    assert [v for v, _ in rail["dept"]["views"]] == ["ours", "u:metro01", "all"] and rail["dept"]["color"] == "#c2410c"
    m = rail["metro"]
    assert (m["view"], m["system"], m["unit"], m["crumb"]) == ("all", "metro", "E71", "Metro")
    assert rail["hidden"] == {"views": ["ours", "all"], "view": "ours"}
    assert rail["mine"] == {"first": "p:mine01", "view": "all", "source": "p25:metro:9001,p25:metro:9003,p25:metro:9100"}
    assert rail["none"]["asked"] == 0 and rail["none"]["rows"] == 0
    assert rail["oldLink"] == "ours"


def test_every_view_hidden_takes_the_views_away(rail):
    a = rail["allHidden"]
    assert a["header"] and a["tabs"] and a["n"] == 0
    assert a["view"] == "" and a["asked"] == "all" and a["source"] is None


def test_the_views_editor_opens_in_their_place(rail):
    assert rail["edit"] == {"box": True, "tabs": True, "expanded": "true"}
    assert rail["edited"] == {"box": False, "tabs": False}
