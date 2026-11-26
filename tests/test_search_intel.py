"""Search's opt-in Intel events (#937; sizeup/intel/eventlog.py IntelLog.search, GET /api/cad/intel/search,
static/search-view.js, the "search" namespace of /api/me/prefs).

Admin request: "intel events should appear in the Search tab when a user wants them". Off unless turned on, the choice
the person's own on the hub; offered only to the Intel page's readers (signed in: sourcekeys.may_see_context); Search's
time, area and words applied on the hub, bounded (the range's newest SEARCH_SCAN looked through, SEARCH_MAX returned)
and on intel_events_first; the words heard never sent (only where the words were); marked Intel with the level, the
category and why it didn't push; a row or pin opens its More info.

Events come from the engine on the test_intel transcripts where the path is the engine's own, and otherwise in the
engine's exact shape (model.Event, SqlEvents.save). Invented places only (Sample St & Test Rd, 12 Example Road)."""

from __future__ import annotations

import json
import re

import pytest

from sizeup.cad import prefs as prefs_mod
from sizeup.cad.api import CadApi
from sizeup.cad.history import wait_idle
from sizeup.intel import eventlog
from tests.test_intel_page import STATIC, T0, get, heard, needs_node, run_node, save, site  # noqa: F401
from tests.test_roles import CASEY, JORDAN, VAL, people  # noqa: F401

DAY = 86400.0


def search(cad, who=VAL, **q):
    code, d = get(CadApi(cad), "/api/cad/intel/search", who, "&".join(f"{k}={v}" for k, v in q.items()))
    assert code == 200, d
    return d


def ids(d):
    return [e["id"] for e in d["events"]]


# -- the API -------------------------------------------------------------------------------------------------------------

def test_only_the_intel_pages_readers_search_it(site):
    cad, _clock = site
    save(cad, precision="address", lat=41.0, lon=-73.7, label_full="12 Example Road, Rye")
    api = CadApi(cad)
    assert get(api, "/api/cad/intel/search", None) == (403, {"error": "radio pre-alerts need a sign-in (Cloudflare Access)"})
    for who in (CASEY, JORDAN, VAL):
        code, d = get(api, "/api/cad/intel/search", who)
        assert code == 200 and len(d["events"]) == 1
    assert "/api/cad/intel/search" not in __import__("sizeup.cad.api", fromlist=["ADMIN_READS"]).ADMIN_READS


def test_the_time_filter_and_every_event_kept_by_default(site):
    cad, clock = site
    clock.t = T0 + 100 * DAY
    new = save(cad, first_at=T0 + 99 * DAY).id
    month = save(cad, first_at=T0 + 60 * DAY).id
    save(cad, first_at=T0)                                             # older than kept (SPAN_MAX_S): never looked at
    assert ids(search(cad)) == [new, month]
    day = __import__("datetime").datetime.fromtimestamp(T0 + 60 * DAY, __import__("sizeup.cad.store", fromlist=["TZ"]).TZ)
    one = day.strftime("%Y-%m-%d")
    assert ids(search(cad, **{"from": one, "to": one})) == [month]
    assert ids(search(cad, **{"from": one})) == [new, month]


def test_the_area_is_the_place_and_town_a_street_type_either_way(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)    # the engine's: "Sample St & Test Rd, Rye"
    road = save(cad, first_at=T0 - 60, precision="address", label_full="12 Example Road, Rye", town="Rye", lat=41.0,
                lon=-73.7).id
    rd = save(cad, first_at=T0 - 120, precision="block", label_full="Example Rd, 100 block, Port Chester",
              town="Port Chester", lat=41.0, lon=-73.66).id
    save(cad, first_at=T0 - 180, precision="none", town="Rye")          # unplaced: no area to match
    corner = search(cad)["events"][0]["id"]
    assert set(ids(search(cad, where="example road"))) == {road, rd}
    assert ids(search(cad, where="12 Example Rd")) == [road]
    assert set(ids(search(cad, where="Rye"))) == {corner, road}
    assert ids(search(cad, where="sample street & test road")) == [corner]
    assert ids(search(cad, where="Exampl")) == []                      # whole words (the address box's own rule)


def test_the_area_splits_a_label_as_its_words_do(site):
    """#944 review: the places.py shapes ("I-95 NB exit 21", a corner with "/", a name with a quote, "(rear)")."""
    cad, _clock = site
    made = {}
    for i, label in enumerate(("I-95 NB exit 21, Rye", "Main St/Elm St, Rye", "O'Neil Ave, Rye", "12 Example Road (rear), Rye")):
        made[label] = save(cad, first_at=T0 - i, precision="landmark", label_full=label, lat=41.0, lon=-73.7).id
    assert ids(search(cad, where="I-95")) == ids(search(cad, where="95 nb")) == [made["I-95 NB exit 21, Rye"]]
    assert ids(search(cad, where="elm street")) == [made["Main St/Elm St, Rye"]]
    assert ids(search(cad, where="o'neil")) == ids(search(cad, where="neil")) == [made["O'Neil Ave, Rye"]]
    assert ids(search(cad, where="rear")) == [made["12 Example Road (rear), Rye"]]
    assert ids(search(cad, text="neil")) == [made["O'Neil Ave, Rye"]]               # the words agree


def test_words_in_the_category_the_place_or_what_was_heard(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    edp = save(cad, first_at=T0 - 60, category="edp", precision="address", label_full="12 Example Road, Rye",
               lat=41.0, lon=-73.7, traffic_ids=["nothing-heard"]).id
    shots = search(cad)["events"][0]
    d = search(cad, text="fired")
    assert ids(d) == [shots["id"]] and d["events"][0]["matched"] == ["radio"]
    assert ids(search(cad, text="weap")) == [shots["id"]]              # the category's label, a word's start
    assert search(cad, text="shots")["events"][0]["matched"] == ["category"]     # settled: its transcripts not read
    assert ids(search(cad, text="example road")) == [edp] and search(cad, text="example")["events"][0]["matched"] == ["place"]
    assert ids(search(cad, text="edp example")) == [edp]               # each word somewhere
    assert ids(search(cad, text="fired example")) == []
    assert "fired" not in json.dumps(search(cad, text="fired"))       # the words heard never sent


def test_another_systems_transcripts_are_searched_though_never_indexed(site):
    """history.py never puts another P25 system's words in fts_radio (#419): the intel search reads them itself."""
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    wait_idle(cad.addrs)
    assert cad.store.q("SELECT rowid FROM fts_radio WHERE fts_radio MATCH ?", ('"fired"*',)) == []
    assert search(cad, text="fired")["events"][0]["matched"] == ["radio"]
    assert search(cad, text="ired")["events"] == []                    # a word's start, as Search's words


def test_the_second_transcript_and_the_corrected_one_count_too(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    with cad.store.tx() as db:
        db.execute("UPDATE radio_traffic SET alt_text = 'units responding', corr_text = 'perimeter set' WHERE id = 'c1-t0'")
    assert search(cad, text="respond")["events"][0]["matched"] == ["radio"]
    assert search(cad, text="perimeter")["events"][0]["matched"] == ["radio"]


def test_words_stop_reading_transcripts_once_past_the_matches_returned(site, monkeypatch):
    cad, _clock = site
    for i in range(12):
        cad.comms.record(__import__("tests.test_intel", fromlist=["ctx_row"]).ctx_row(f"w{i}", "units responding", T0 - i))
        save(cad, first_at=T0 - i, traffic_ids=[f"w{i}"])
    monkeypatch.setattr(eventlog, "SEARCH_MAX", 2)
    monkeypatch.setattr(eventlog, "WORD_CHUNK", 4)
    read = []
    real = cad.intel_admin.eventlog._heard
    monkeypatch.setattr(cad.intel_admin.eventlog, "_heard", lambda rows, words: (read.append(len(rows)), real(rows, words))[1])
    d = search(cad, text="responding")
    assert len(d["events"]) == 2 and d["total"] == 4 and d["more"] is True and read == [4]
    w = search(cad, text="weapon")
    assert (w["total"], w["more"], len(read)) == (4, True, 1)          # the label settles it: no transcript read


def test_a_result_is_marked_with_its_level_category_and_why_it_didnt_push(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    save(cad, first_at=T0 - 60, suppressed="out_of_area", precision="address", label_full="12 Example Road, Rye",
         lat=41.0, lon=-73.7, confirmed=False, radius_m=30.0)
    pushed, held = search(cad)["events"]
    assert {k: pushed[k] for k in ("level", "held", "label", "pack_label", "category", "place", "confirmed")} == {
        "level": "push", "held": None, "label": "Weapon / shots", "pack_label": "PD", "category": "weapons",
        "place": "Sample St & Test Rd, Rye", "confirmed": True}
    assert held["level"] == "log" and held["held"]["key"] == "out_of_area" and held["held"]["label"] == "out of area"
    assert (held["lat"], held["lon"], held["radius_m"], held["confirmed"]) == (41.0, -73.7, 30, False)


def test_bounded_and_says_so(site, monkeypatch):
    cad, _clock = site
    made = [save(cad, first_at=T0 - i).id for i in range(7)]
    monkeypatch.setattr(eventlog, "SEARCH_MAX", 2)
    d = search(cad)
    assert ids(d) == made[:2] and d["total"] == 7 and d["limit"] == 2 and d["capped"] is False
    monkeypatch.setattr(eventlog, "SEARCH_SCAN", 5)
    d = search(cad)
    assert d["total"] == 5 and d["scanned"] == 5 and d["capped"] is True


def test_it_reads_the_first_at_index(site, monkeypatch):
    cad, _clock = site
    save(cad, precision="address", lat=41.0, lon=-73.7, label_full="12 Example Road, Rye")
    seen = []
    real = cad.store.q
    monkeypatch.setattr(cad.store, "q", lambda sql, args=(): (seen.append((sql, args)), real(sql, args))[1])
    search(cad, where="example road")
    sql, args = next(x for x in seen if "LIMIT :scan" in x[0])
    plan = " ".join(r["detail"] for r in real(f"EXPLAIN QUERY PLAN {sql}", args))
    assert "intel_events_first" in plan and "TEMP B-TREE" not in plan


# -- the choice: off unless turned on, the person's own --------------------------------------------------------------------

def test_the_search_choice_is_a_known_shape_kept_per_person(tmp_path):
    from tests.test_cad import make_cad
    assert json.loads(prefs_mod.validate("search", {"intel": True})) == {"intel": True}
    assert json.loads(prefs_mod.validate("search", {})) == {}
    for bad in ({"intel": "1"}, {"intel": True, "calls": False}, [], True):
        with pytest.raises(prefs_mod.PrefsError):
            prefs_mod.validate("search", bad)
    cad = make_cad(tmp_path)
    who = people(cad)
    assert cad.prefs.all(who["Val"]) == {}                              # nothing kept: off
    cad.prefs.put(who["Val"], "search", {"intel": True})
    assert cad.prefs.all(who["Val"]) == {"search": {"intel": True}} and cad.prefs.all(who["Jordan"]) == {}


SEARCH_DOM = r"""
const calls = { get: [], changed: [] };
let PREFS = null;
const mem = {};
global.localStorage = { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); } };
global.MePrefs = { register: (ns, o) => { PREFS = [ns, o]; }, changed: (ns) => calls.changed.push(ns) };
global.addEventListener = () => {};
global.innerHeight = 900;
document.querySelectorAll = () => [];
const mkd = document.createElement;
global.cad = { scene: { timezone: "America/New_York" }, hooks: [], state: { intel: { open: [] } }, search: true };
global.L = () => ({});
global.canDo = () => false;
global.typeLabel = () => ""; global.typeColor = () => ""; global.nameOf = () => ""; global.openFromRecords = () => {};
global.history = { replaceState() {} };
let UNTIL = 0;
global.api = (m, path) => {                    // (the hub's range ends a minute from its clock: never the same twice)
  calls.get.push(path);
  const d = JSON.parse(JSON.stringify(path.startsWith("/api/cad/intel/search") ? IN.intel : { rows: [], total: 0, page: 0, size: 50 }));
  if (d.until) d.until += ++UNTIL;
  return Promise.resolve(d);
};
const flush = () => new Promise((r) => setTimeout(r, 5));
const $0 = global.$;
global.$ = (sel) => { const n = $0(sel); if (!n.style) n.style = { setProperty() {}, removeProperty() {} }; return n; };
"""


@needs_node
def test_the_page_offers_it_off_by_default_and_marks_what_it_finds(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    save(cad, first_at=T0 - 60, suppressed="out_of_area", precision="none")
    d = search(cad)
    script = f"""
{SEARCH_DOM}
{(STATIC / "search-view.js").read_text()}
(async () => {{
  const out = {{}};
  SearchView.open("");
  await flush();
  out.off = {{ gets: calls.get.filter((p) => p.includes("/intel/")).length, box: $("#sv-intel-box").hidden,
              btn: $("#sv-intel").hidden, pressed: $("#sv-intel").attrs["aria-pressed"], ns: PREFS[0], v: PREFS[1].get() }};
  $("#sv-intel").on.click[0]();                                       // turned on: the person's, on the hub
  await flush();
  out.on = {{ changed: calls.changed, v: PREFS[1].get(), stored: mem["sizeup.searchIntel"], box: $("#sv-intel-box").hidden,
             gets: calls.get.filter((p) => p.includes("/intel/search")).length }};
  const rows = $("#sv-intel-rows").children.slice();
  out.rows = rows.map((r) => r.children.map((c) => c.textContent));
  out.chips = rows.map((r) => r.children[1].children[0].className);
  out.n = $("#sv-intel-n").textContent;
  rows[1].on.click[0]();
  out.hash = location.hash;
  SearchView.open("");                                                 // the same answer: nothing redrawn
  await flush();
  out.same = $("#sv-intel-rows").children.every((r, i) => r === rows[i]);
  SearchView.open("unit=E1");                                         // a call-only filter: no intel read
  const g = calls.get.length;
  await flush();
  out.callsOnly = {{ gets: calls.get.slice(g).filter((p) => p.includes("/intel/")).length, n: $("#sv-intel-n").textContent }};
  PREFS[1].apply(null);                                                // the hub's: none (another person): off
  out.applied = {{ box: $("#sv-intel-box").hidden, pressed: $("#sv-intel").attrs["aria-pressed"] }};
  cad.state = {{}};                                                    // intel taken away: no button
  cad.hooks.forEach((h) => h());
  out.gone = $("#sv-intel").hidden;
  console.log(JSON.stringify(out));
}})();
"""
    out = run_node(script, {"intel": d})
    assert out["off"] == {"gets": 0, "box": True, "btn": False, "pressed": "false", "ns": "search", "v": {"intel": False}}
    assert out["on"] == {"changed": ["search"], "v": {"intel": True}, "stored": "1", "box": False, "gets": 1}
    pushed, held = out["rows"]
    assert pushed[1] == "Intel Weapon / shots" and pushed[2] == "push" and pushed[3] == "Sample St & Test Rd, Rye"
    assert pushed[4] == "—" and held[2] == "log" and held[3] == "unplaced" and held[4] == "out of area"
    assert out["chips"] == ["chip sv-ichip", "chip sv-ichip"] and out["n"] == "2 events"
    assert out["hash"] == f"#prealert/intel/{d['events'][1]['id']}"
    assert out["same"] is True
    assert out["callsOnly"] == {"gets": 0, "n": "Not with a type, unit or disposition"}
    assert out["applied"] == {"box": True, "pressed": "false"}
    assert out["gone"] is True


def test_the_page_wires_it_once():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/me-prefs.js"') < html.index('src="/static/intel-map.js"') < html.index('src="/static/search-view.js"')
    form = html[html.index('<form id="sv-filters"'):html.index("</form>", html.index('<form id="sv-filters"'))]
    assert re.search(r'<button id="sv-intel" type="button" aria-pressed="false" hidden title="[^"]+">Intel</button>', form)
    js = (STATIC / "search-view.js").read_text()
    assert js.count('MePrefs.register("search"') == 1 and 'MePrefs.changed("search")' in js
    assert 'IntelMap.attach(st.map, "sv-intel"' in js and "/api/cad/intel/search?" in js
    assert "requestAnimationFrame" not in js[js.index("// ---- Intel events (#937)"):js.index("// ---- address notes")]
    for m in re.finditer(r"localStorage\.\w+Item", js):
        before = js[:m.start()]
        assert before.rfind("try {") > before.rfind("} catch"), m.group(0)
