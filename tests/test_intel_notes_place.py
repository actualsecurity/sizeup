"""A note's corrected place picked as the incident editor picks a location (sizeup/intel/notes.py, static/intel-notes.js):
an address, "@" one of the department's common places, a location qualifier in front; kept with the note, shown on the
event's Place card and the Intel page's map; a labelled example in intel_feedback; an admin's "add as alias"; the
misheard -> real pair for the speech-to-text side (intel_correction_candidates); an older free-text note still read.

Invented data only: the common place "Sample Diner" at 12 Sample Rd, heard as "Sampel Diner"."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.geo.gazetteer import Gazetteer
from sizeup.geo.pagegeo import locator, set_local_source
from sizeup.geo.places import PlaceIndex
from sizeup.intel import notes as notes_mod
from sizeup.intel.events import SqlEvents
from tests.test_intel import Clock as IntelClock
from tests.test_intel import FakeNotifier, ctx_row, example_locate
from tests.test_intel_page import STATIC, T0, needs_node, run_node, save
from tests.test_places import fixture_gazetteer
from tests.test_roles import CASEY, JORDAN, VAL, call, people
from tests.test_settings import post

DINER = {"label": "12 Sample Rd", "lat": 41.0016, "lon": -73.6657, "sub": "Port Chester"}


@pytest.fixture
def placed(tmp_path):
    from tests.test_cad import make_cad
    clock = IntelClock(T0)
    gaz = Gazetteer(fixture_gazetteer(tmp_path / "gaz.sqlite"))
    cad = make_cad(tmp_path, clock=clock, geocoder=lambda q: [])
    cad.place_source = lambda: PlaceIndex(gaz)
    people(cad)
    cad.intel.notifier, cad.intel.locate = FakeNotifier(), example_locate
    post(cad, "places", action="set", name="Sample Diner", place=DINER)
    return cad, clock, gaz


def jget(api, path, who=CASEY, query=""):
    code, raw, *_ = api.get(path, query, Identity(email=who) if who else None)
    return code, json.loads(raw) if raw[:1] == b"{" else {}


def suggestion(api, q):
    """The location box's first suggestion for q (GET /api/cad/geocode: what static/cad.js addressBox offers)."""
    code, d = jget(api, "/api/cad/geocode", VAL, "q=" + q.replace(" ", "+").replace("@", "%40"))
    assert code == 200
    return d["matches"][0] if d["matches"] else None


def pick_of(m):
    """What intel-notes.js sends for a suggestion taken (addressBox's picked)."""
    return {"lat": m["lat"], "lon": m["lon"], "label": m["label"], "kind": m["kind"], "place_key": m.get("place_key", "")}


def radio_row(cad, tid, text):
    cad.comms.record(ctx_row(tid, text, T0))


# -- the pick, kept and read back -------------------------------------------------------------------------------------

def test_a_common_place_picked_with_a_qualifier_is_kept_and_read_back(placed):
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    m = suggestion(api, "@ Sample Diner")
    assert m["place"] == "12 Sample Rd" and m["place_key"] == "sample diner"       # the common place, by its key
    e = save(cad, traffic_ids=["c1-t0"])
    radio_row(cad, "c1-t0", "car 3 respond to the sampel diner")
    code, out = call(api, f"/api/cad/intel/{e.id}/notes", {"tags": ["wrong_place"], "corrected_pick": pick_of(m),
                                                           "corrected_place": "@ Sample Diner", "corrected_qualifier": "IFO",
                                                           "heard": "Sampel  Diner"}, VAL)
    assert code == 200, out
    n = out["note"]
    assert n["corrected"] == {"lat": 41.0016, "lon": -73.6657, "precision": "landmark", "label": "12 Sample Rd",
                              "qualifier": "IFO", "place_key": "sample diner"}
    assert n["heard"] == "Sampel Diner" and n["corrected_place"] == "@ Sample Diner" and n["place_name"] == "Sample Diner"
    assert n["alias_ok"] is False                                                  # (a viewer: never offered)
    [back] = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["notes"]
    assert back["corrected"] == n["corrected"] and back["alias_ok"] is True        # an admin: offered
    # the Place card: the corrected place apart from the engine's
    code, b = jget(api, f"/api/cad/prealerts/intel/{e.id}", VAL)
    assert code == 200
    c = b["place"]["corrected"]
    assert (c["where"], c["lat"], c["lon"], c["heard"], c["by"]) == ("IFO @ Sample Diner", 41.0016, -73.6657,
                                                                     "Sampel Diner", "Val")
    # the Intel page's map: drawn apart from the pins (this event is unplaced by the engine)
    code, mp = jget(api, "/api/cad/intel/list", VAL, "map=1")
    assert code == 200 and mp["pins"] == [] and mp["corrected"] == [
        {"id": e.id, "lat": 41.0016, "lon": -73.6657, "label": "12 Sample Rd", "precision": "landmark"}]


def test_bad_picks_are_refused_and_a_pick_cleared_keeps_the_text(placed):
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    e = save(cad)
    for pick, why in (({"lat": 1, "lon": 2, "label": "x"}, "in the area"), ({"lat": 41.0, "lon": -73.66}, "label"),
                      ({"lat": "x", "lon": -73.66, "label": "x"}, "lat and lon"),
                      ({"lat": 41.0, "lon": -73.66, "label": "x", "place_key": "nope"}, "no such common place"),
                      ("12 Sample Rd", "or null")):
        code, out = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick}, VAL)
        assert code == 400 and why in out["error"], (pick, out)
    assert call(api, f"/api/cad/intel/{e.id}/notes", {"text": "x", "corrected_qualifier": "SIDEWAYS"}, VAL)[0] == 400
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": {"lat": 41.0, "lon": -73.66, "label": "12 Sample Rd",
                                                                         "kind": "address"}}, VAL)[1]["note"]["id"]
    n = cad.store.one("SELECT * FROM intel_notes WHERE id = ?", (nid,))
    assert (n["corrected_place"], n["corrected_precision"]) == ("12 Sample Rd", "address")     # the label as written
    code, out = call(api, f"/api/cad/intel/notes/{nid}", {"corrected_pick": None}, VAL)
    assert code == 200 and out["note"]["corrected"] is None and out["note"]["corrected_place"] == "12 Sample Rd"


# -- what it feeds back -----------------------------------------------------------------------------------------------

def test_the_pick_is_a_labelled_example_and_a_correction_candidate(placed):
    cad, clock, _gaz = placed
    api = CadApi(cad)
    m = suggestion(api, "@ Sample Diner")
    e = save(cad, traffic_ids=["c1-t0", "c1-t1"])
    radio_row(cad, "c1-t0", "car 3 on the way")
    radio_row(cad, "c1-t1", "at the sampel diner now")
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
               VAL)[1]["note"]["id"]
    [ex] = cad.store.q("SELECT traffic_id, corrected FROM intel_feedback WHERE note_id = ? AND verdict = ?",
                       (nid, notes_mod.PLACE_EXAMPLE))
    c = json.loads(ex["corrected"])
    assert ex["traffic_id"] == "c1-t1" and c["heard"] == "Sampel Diner"             # where it was heard
    assert c["point"]["place_key"] == "sample diner" and c["point"]["lat"] == 41.0016
    [cand] = notes_mod.correction_candidates(store=cad.store)
    assert {k: cand[k] for k in ("note_id", "clip_id", "heard_phrase", "real_phrase", "kind", "source", "who", "when")} == {
        "note_id": nid, "clip_id": "c1-t1", "heard_phrase": "Sampel Diner", "real_phrase": "Sample Diner",
        "kind": "place", "source": "intel-note", "who": "Val <val@example.org>", "when": T0}
    assert cand["imported_at"] is None
    assert notes_mod.correction_candidates(store=cad.store) == notes_mod.correction_candidates()   # the hub's by default
    assert notes_mod.correction_candidates(since=T0 + 1, store=cad.store) == []
    # taken by the speech-to-text side: never rewritten nor written twice by an edit
    clock.t += 60
    assert notes_mod.mark_imported([cand["id"]], store=cad.store) == 1
    assert notes_mod.mark_imported([cand["id"]], store=cad.store) == 0
    assert notes_mod.correction_candidates(store=cad.store) == []
    call(api, f"/api/cad/intel/notes/{nid}", {"text": "it was the diner"}, VAL)
    rows = notes_mod.correction_candidates(unimported_only=False, store=cad.store)
    assert [(r["heard_phrase"], r["imported_at"]) for r in rows] == [("Sampel Diner", T0 + 60)]
    # heard the same as the name: no pair; a new heard: a new pair; deleted: the untaken pair goes
    call(api, f"/api/cad/intel/notes/{nid}", {"heard": "sample diner"}, VAL)
    assert notes_mod.correction_candidates(store=cad.store) == []
    call(api, f"/api/cad/intel/notes/{nid}", {"heard": "Simple Dinah"}, VAL)
    assert [r["heard_phrase"] for r in notes_mod.correction_candidates(store=cad.store)] == ["Simple Dinah"]
    call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, VAL)
    assert notes_mod.correction_candidates(store=cad.store) == []
    assert len(notes_mod.correction_candidates(unimported_only=False, store=cad.store)) == 1     # the one taken stays


def test_an_admin_adds_the_heard_words_as_an_alias_and_the_next_transmission_resolves(placed):
    cad, _clock, gaz = placed
    api = CadApi(cad)
    set_local_source(gaz, cad.local_places)                    # the radio's page locator, as the hub wires it (app.py)
    assert suggestion(api, "@ Sampel Diner") is None
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
               VAL)[1]["note"]["id"]
    # not an admin: refused, nothing added, refused in their activity
    for who in (VAL, JORDAN):
        code, out = call(api, f"/api/cad/intel/notes/{nid}/alias", {}, who)
        assert code == 403, out
    assert [p["aliases"] for p in cad.local_places() if p["key"] == "sample diner"] == [[]]
    assert any(a["kind"] == "refused" for a in cad.activity.rows(JORDAN))
    code, out = call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
    assert code == 200 and out["added"] is True and out["place"] == "Sample Diner"
    assert out["note"]["alias_ok"] is False                                      # one of its names now
    assert [p["aliases"] for p in cad.local_places() if p["key"] == "sample diner"] == [["Sampel Diner"]]
    # the next one saying it resolves: the location box and the radio's page locator
    hit = suggestion(api, "@ Sampel Diner")
    assert hit and hit["place_key"] == "sample diner" and (hit["lat"], hit["lon"]) == (41.0016, -73.6657)
    assert locator(gaz).local_said("car 3 at the sampel diner")
    # audited: the note's audit and the lists' (Settings)
    a = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["audit"]
    assert [x["action"] for x in a] == ["add", "alias"] and json.loads(a[1]["after"])["aliases"] == ["Sampel Diner"]
    assert cad.settings_audit_log({"section": ["lists"]})["audit"][0]["key"] == "place/Sample Diner"
    # again: nothing added, no audit row
    code, out = call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
    assert code == 200 and out["added"] is False
    assert len(jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["audit"]) == 2


def test_an_alias_needs_heard_words_and_a_common_place(placed):
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": {"lat": 41.0, "lon": -73.66, "label": "12 Sample Rd",
                                                                         "kind": "address"}, "heard": "twelve sample"},
               VAL)[1]["note"]["id"]
    code, out = call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
    assert code == 400 and "common place" in out["error"]


def test_a_county_entrys_alias_survives_a_reimport():
    from sizeup.cad import commonplaces
    rows = [["Address", "Common Place", "Mun"], ["12 SAMPLE RD", "SAMPLE DINER", "PORT_CHESTER"]]
    cols = commonplaces.find_columns(rows[0], None)
    loc = lambda a, t: {"label": "12 Sample Rd", "lat": 41.0016, "lon": -73.6657, "muni": "Port Chester",
                        "precision": "exact"}
    first = commonplaces.plan(rows, cols, loc, lambda w: "Port Chester", "2026-01-01", {})["entries"]
    [key] = first
    current = {key: dict(first[key], aliases=["Sampel Diner"])}
    again = commonplaces.plan(rows, cols, loc, lambda w: "Port Chester", "2026-02-01", current)["entries"]
    assert again[key]["aliases"] == ["Sampel Diner"]


# -- an older note -----------------------------------------------------------------------------------------------------

OLD_NOTES = """
CREATE TABLE intel_notes (
  id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
  corrected_category TEXT NOT NULL DEFAULT '', corrected_place TEXT NOT NULL DEFAULT '',
  corrected_level TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', author_email TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, done TEXT NOT NULL DEFAULT '', done_by TEXT NOT NULL DEFAULT '',
  done_at REAL);
"""


def test_an_older_free_text_note_is_read_and_edited_after_the_columns_are_added(placed, tmp_path):
    cad, _clock, _gaz = placed
    db = cad.store.db                                          # the table as #913 made it, one note in it
    with cad.store.lock:
        db.execute("DROP TABLE intel_notes")
        db.executescript(OLD_NOTES)
    e = save(cad)
    db.execute("INSERT INTO intel_notes (event_id, text, tags, corrected_place, author, author_email, created_at, "
               "updated_at) VALUES (?, 'old one', '[\"wrong_place\"]', '@ Sample Diner', ?, ?, ?, ?)",
               (e.id, "Val <val@example.org>", VAL, T0, T0))
    SqlEvents(cad.store)                                       # the next start: the columns added, nothing dropped
    cols = {r[1] for r in db.execute("PRAGMA table_info(intel_notes)")}
    assert {"corrected_lat", "corrected_label", "corrected_qualifier", "corrected_place_key", "heard", "text"} <= cols
    api = CadApi(cad)
    [n] = jget(api, f"/api/cad/intel/{e.id}/notes", VAL)[1]["notes"]
    assert (n["text"], n["corrected_place"], n["corrected"], n["heard"]) == ("old one", "@ Sample Diner", None, "")
    code, out = call(api, f"/api/cad/intel/notes/{n['id']}", {"text": "old one, edited"}, VAL)    # its text: kept
    assert code == 200 and out["note"]["corrected_place"] == "@ Sample Diner" and out["note"]["corrected"] is None
    m = suggestion(api, "@ Sample Diner")                      # then picked: the place joins the text
    code, out = call(api, f"/api/cad/intel/notes/{n['id']}", {"corrected_pick": pick_of(m)}, VAL)
    assert code == 200 and out["note"]["corrected"]["place_key"] == "sample diner"
    assert out["note"]["corrected_place"] == "@ Sample Diner"
    SqlEvents(cad.store)                                       # again: nothing to add, nothing lost
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_notes")["n"] == 1


def test_the_phrases_are_never_in_the_log(placed, capsys):
    cad, _clock, _gaz = placed
    said: list = []
    cad.log = said.append
    api = CadApi(cad)
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
               VAL)[1]["note"]["id"]
    call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
    out = " ".join(map(str, said)) + capsys.readouterr().out
    assert "Sampel" not in out and "sampel" not in out


def test_the_candidates_table_is_made_at_start(tmp_path):
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    assert cad.store.one("SELECT name FROM sqlite_master WHERE name = 'intel_correction_candidates'")
    with pytest.raises(sqlite3.IntegrityError):                # the phrases are required
        cad.store.db.execute('INSERT INTO intel_correction_candidates (heard_phrase, real_phrase, "when") '
                             "VALUES (NULL, 'x', 1)")


# -- the form: the incident editor's location box, reused --------------------------------------------------------------

@needs_node
def test_the_form_uses_the_incident_editors_location_box_and_sends_a_pick_only_when_it_changed(placed):
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "corrected_place": "@ Sample Diner",
                                               "corrected_qualifier": "IFO", "heard": "Sampel Diner"}, VAL)
    d = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]
    script = f"""
{(STATIC / "intel-notes.js").read_text()}
const boxes = [], posts = [];
// cad.js's own (the incident editor's): stubbed here to see that the form calls them, never a copy of its own
global.addressBox = (input, geo, list) => {{
  const b = {{ input, geo, list, picked: null, set(v, p) {{ input.value = v || ""; b.picked = p; }} }};
  boxes.push(b);
  return b;
}};
global.qualSelect = (sel, code) => {{ sel.value = code || ""; return sel; }};
let typed = 0;
global.qualTyped = () => {{ typed++; }};
global.qualText = (c, t) => [c, t].filter(Boolean).join(" ");
global.UI = {{ toast() {{}}, confirm: async () => true }};
global.api = async (m, p, b) => {{ if (m === "POST") {{ posts.push([p, b]); return {{ ok: true }}; }} return JSON.parse(JSON.stringify(IN)); }};
const flush = () => new Promise((r) => setTimeout(r, 5));
(async () => {{
  const host = $("#pi-notes");
  await IntelNotes.mount(host, {e.id});
  const row = host.children[1].children[0];
  const buttons = row.all().filter((x) => x.tagName === "BUTTON").map((x) => x.textContent);
  const fix = row.find("nt-fix")[0].textContent;
  row.all().find((x) => x.textContent === "Edit").on.click[0]();
  const form = host.children[1].children[0];
  const b = boxes[boxes.length - 1];
  const restored = {{ value: b.input.value, key: b.picked && b.picked.place_key, kind: b.picked && b.picked.kind,
                     heard: form.find("nt-heard")[0].value, qual: form.find("loc-qual")[0].value, typed }};
  form.on.submit[0]({{ preventDefault() {{}} }});                       // saved as it was: no pick sent
  await flush();
  b.picked = {{ lat: 41.0, lon: -73.66, label: "12 Sample Rd", kind: "address", confidence: 1 }};   // another taken
  form.on.submit[0]({{ preventDefault() {{}} }});
  await flush();
  b.picked = null;                                                     // typed over, nothing taken: cleared
  form.on.submit[0]({{ preventDefault() {{}} }});
  await flush();
  await IntelNotes.mount(host, {e.id}, true);
  host.children[1].children[0].all().find((x) => (x.textContent || "").startsWith("+ Alias")).on.click[0]();
  await flush();
  console.log(JSON.stringify({{ buttons, fix, restored, posts }}));
}})();
"""
    out = run_node(script, d)
    assert "+ Alias of Sample Diner" in out["buttons"]
    assert out["fix"] == '→ place: IFO @ Sample Diner ◆ ← "Sampel Diner"'
    assert out["restored"] == {"value": "@ Sample Diner", "key": "sample diner", "kind": "current", "heard": "Sampel Diner",
                               "qual": "IFO", "typed": 1}
    (p1, same), (_p2, other), (_p3, cleared), (p4, _alias) = out["posts"]
    assert p1 == f"/api/cad/intel/notes/{d['notes'][0]['id']}" and "corrected_pick" not in same
    assert same["corrected_place"] == "@ Sample Diner" and same["corrected_qualifier"] == "IFO" and same["heard"] == "Sampel Diner"
    assert other["corrected_pick"] == {"lat": 41.0, "lon": -73.66, "label": "12 Sample Rd", "kind": "address", "place_key": ""}
    assert cleared["corrected_pick"] is None
    assert p4 == f"/api/cad/intel/notes/{d['notes'][0]['id']}/alias"
