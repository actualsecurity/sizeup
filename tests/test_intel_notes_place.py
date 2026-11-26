"""A note's corrected place picked as the incident editor picks a location (sizeup/intel/notes.py, static/intel-notes.js):
an address, "@" one of the department's common places, a location qualifier in front; kept with the note, shown on the
event's Place card and the Intel page's map; a labelled example in intel_feedback; an admin's "add as alias"; the
misheard -> real pair for the speech-to-text side (intel_correction_candidates); an older free-text note still read.

Invented data only: the common place "Sample Diner" at 12 Sample Rd, heard as "Sampel Diner"."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.cad import commonplaces
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.geo.gazetteer import Gazetteer
from sizeup.geo.pagegeo import locator, set_local_source
from sizeup.geo.places import PlaceIndex
from sizeup.intel import notes as notes_mod
from sizeup.intel.events import SqlEvents
from tests.log_sentinel import assert_no_sensitive, exception_text_in_logs
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
    rev = cad.intel.events.map_rev                             # the Intel page's map reads again on a pick only
    assert call(api, f"/api/cad/intel/notes/{nid}", {"text": "x"}, VAL)[0] == 200 and cad.intel.events.map_rev == rev
    code, out = call(api, f"/api/cad/intel/notes/{nid}", {"corrected_pick": None}, VAL)
    assert code == 200 and out["note"]["corrected"] is None and out["note"]["corrected_place"] == "12 Sample Rd"
    assert cad.intel.events.map_rev > rev


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
    # since is exclusive: the last "when" read gives nothing again
    assert notes_mod.correction_candidates(since=T0, store=cad.store) == []
    assert notes_mod.correction_candidates(since=T0 - 1, store=cad.store) == [cand]
    # an edit by an admin that leaves the words alone: the row as it was (its who and when)
    clock.t += 30
    call(api, f"/api/cad/intel/notes/{nid}", {"text": "it was the diner"}, CASEY)
    assert notes_mod.correction_candidates(store=cad.store) == [cand]
    # taken by the speech-to-text side, as it read it: never rewritten nor written twice by an edit
    clock.t += 30
    assert notes_mod.mark_imported([cand], store=cad.store) == 1
    assert notes_mod.mark_imported([(cand["id"], "Sampel Diner", "Sample Diner")], store=cad.store) == 0
    with pytest.raises(TypeError):
        notes_mod.mark_imported([cand["id"]], store=cad.store)                # a bare id: never (what was read?)
    assert notes_mod.correction_candidates(store=cad.store) == []
    call(api, f"/api/cad/intel/notes/{nid}", {"text": "it was the diner, really"}, VAL)
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


def test_a_pair_changed_after_it_was_read_is_not_marked_and_ids_are_never_reused(placed):
    """#960 review 4: an edit changes the untaken row in place (never delete + insert), so the id the reader saw is the
    same row; mark_imported names the words it read, so a pair changed since is not taken; ids never come back."""
    cad, clock, _gaz = placed
    api = CadApi(cad)
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
               VAL)[1]["note"]["id"]
    [seen] = notes_mod.correction_candidates(store=cad.store)                  # what the reader read
    clock.t += 30
    call(api, f"/api/cad/intel/notes/{nid}", {"heard": "Simple Dinah"}, CASEY)
    [now] = notes_mod.correction_candidates(store=cad.store)
    assert now["id"] == seen["id"] and now["heard_phrase"] == "Simple Dinah" and now["when"] == T0 + 30
    assert now["who"] == "Val <val@example.org>"                                # the note's author, not the editor
    assert notes_mod.correction_candidates(since=seen["when"], store=cad.store) == [now]    # changed: read again
    assert notes_mod.mark_imported([seen], store=cad.store) == 0                # not what it read: left
    assert notes_mod.mark_imported([now], store=cad.store) == 1
    # a deleted row's id is never given again
    call(api, f"/api/cad/intel/notes/{nid}", {"heard": "Sampel Dyner"}, VAL)
    [new] = notes_mod.correction_candidates(store=cad.store)
    call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, VAL)
    nid2 = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
                VAL)[1]["note"]["id"]
    [again] = notes_mod.correction_candidates(store=cad.store)
    assert again["note_id"] == nid2 and again["id"] > new["id"]


def test_heard_words_alone_are_no_correction(placed):
    """#960 review 6: no pick, no tag, no other correction: no "corrected" verdict row."""
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"text": "x", "heard": "Sampel Diner"}, VAL)[1]["note"]["id"]
    assert cad.store.q("SELECT verdict FROM intel_feedback WHERE note_id = ?", (nid,)) == []


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


def test_the_phrases_are_never_in_the_log(placed, log_sentinel):
    cad, _clock, _gaz = placed
    cad.log = log_sentinel.log
    api = CadApi(cad)
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
               VAL)[1]["note"]["id"]
    call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
    notes_mod.mark_imported(notes_mod.correction_candidates(store=cad.store), store=cad.store)
    assert_no_sensitive(log_sentinel, ["Sampel Diner", "Sample Diner", "12 Sample Rd"])
    assert exception_text_in_logs([notes_mod.__file__]) == []


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


# -- the alias: never another place's name, a street or junk (#960 review 1-3, 7) -------------------------------------

def alias_by_settings(cad, key, alias, who=CASEY):
    code, raw, _ = CadApi(cad).post("/api/cad/settings/places", json.dumps(
        {"action": "alias", "key": key, "alias": alias}).encode(), Identity(email=who))
    return code, json.loads(raw)


def test_an_alias_never_takes_another_places_name_or_alias(placed):
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    post(cad, "places", action="set", name="Sample Market", aliases="Sample Mart",
         place={"label": "40 Example Ave", "lat": 41.0030, "lon": -73.6650, "sub": "Port Chester"})
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    for heard, other in (("Sample  market", "Sample Market"), ("sample mart", "Sample Market")):
        nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": heard},
                   VAL)[1]["note"]["id"]
        n = next(x for x in jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["notes"] if x["id"] == nid)
        assert n["alias_ok"] is False and f"already a name of {other}" in n["alias_why"]      # said before a click
        code, out = call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
        assert code == 409 and f"already a name of {other}" in out["error"]
    assert [p["aliases"] for p in cad.local_places() if p["key"] == "sample diner"] == [[]]
    found = commonplaces.Lookup(cad.local_places()).find("SAMPLE MARKET")
    assert found and found["key"] == "sample market"                            # still the market's name
    assert alias_by_settings(cad, "sample diner", "Sample Market")[0] == 409    # the Settings path: the same


@pytest.mark.parametrize("alias, why", [
    ("", "empty"), ("   ", "empty"), ("Sampel Diner by the old railroad bridge underpass", "at most 40"), ("12", "a number"),
    ("12 34", "a number"), ("King St", "a street's name"), ("N Main St", "a street's name"), ("Arbor", "a street's name"),
    ("Joe", "too short"), ("the deli", "too common"), ("Market", "too common"), ("Garden", "too common"),
    ("Kfc", "too short")])
def test_a_street_a_number_or_junk_is_never_an_alias(placed, alias, why):
    cad, _clock, _gaz = placed
    code, out = alias_by_settings(cad, "sample diner", alias)
    assert code == 400 and why in out["error"], out
    assert [p["aliases"] for p in cad.local_places() if p["key"] == "sample diner"] == [[]]


def test_an_alias_comes_off_again_from_the_note_and_from_settings(placed):
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
               VAL)[1]["note"]["id"]
    assert call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)[1]["added"] is True
    [n] = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["notes"]
    assert n["alias_on"] is True and n["alias_ok"] is False
    assert call(api, f"/api/cad/intel/notes/{nid}/alias", {"remove": True}, JORDAN)[0] == 403
    code, out = call(api, f"/api/cad/intel/notes/{nid}/alias", {"remove": True}, CASEY)
    assert code == 200 and out["removed"] is True and out["note"]["alias_ok"] is True and not out["note"]["alias_on"]
    assert [p["aliases"] for p in cad.local_places() if p["key"] == "sample diner"] == [[]]
    a = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["audit"]
    assert [x["action"] for x in a] == ["add", "alias", "unalias"]
    assert json.loads(a[2]["before"])["aliases"] == ["Sampel Diner"] and json.loads(a[2]["after"])["aliases"] == []
    # Settings: every place's aliases listed, each with its "−"
    alias_by_settings(cad, "sample diner", "Sampel Diner")
    assert cad.settings_get()["place_names"]["aliased"] == [
        {"key": "sample diner", "name": "Sample Diner", "town": "Port Chester", "aliases": ["Sampel Diner"], "imported": False}]
    post(cad, "places", action="unalias", key="sample diner", alias="sampel  diner")
    assert cad.settings_get()["place_names"]["aliased"] == []
    last = cad.settings_audit_log({"section": ["lists"]})["audit"][0]
    assert last["key"] == "place/Sample Diner" and json.loads(last["new"]) == {"aliases": []}


@pytest.fixture
def county(tmp_path):
    """The county's list imported through Settings (tests/test_common_places.py's file, gazetteer and rows)."""
    from tests.test_cad import make_cad
    from tests.test_common_places import HEADER, ROWS, b64, synthetic, xlsx
    gaz = Gazetteer(synthetic(tmp_path / "synthetic.sqlite"))
    cad = make_cad(tmp_path, geocoder=lambda q: [])
    cad.place_source = lambda: PlaceIndex(gaz)
    people(cad)
    post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])), filename="list.xlsx")
    keys = {r["name"]: r["key"] for r in cad.local_places() if commonplaces.is_imported(r["key"], r)}
    return cad, keys, lambda: post(cad, "common_places", action="apply", file=b64(xlsx([HEADER, *ROWS])),
                                   filename="list.xlsx")


def test_a_county_entry_takes_an_alias_through_settings_shows_it_and_gives_it_back(county):
    cad, keys, reimport = county
    diner = keys["TESTVILLE DINER"]
    assert diner.startswith("county/")
    # another county name: refused (both are the county's, at different addresses: the name would turn ambiguous)
    code, out = alias_by_settings(cad, diner, "Sample Marina")
    assert code == 409 and "already a name of Sample Marina" in out["error"]
    assert alias_by_settings(cad, diner, "Testvile Diner", JORDAN)[0] == 403
    code, out = alias_by_settings(cad, diner, "Testvile Diner")
    assert code == 200 and out["added"] is True and out["key"] == diner and out["had"] == []
    assert commonplaces.Lookup(cad.local_places()).find("TESTVILE DINER")["key"] == diner
    assert commonplaces.Lookup(cad.local_places()).find("SAMPLE MARINA")["key"] == keys["SAMPLE MARINA"]
    hit = jget(CadApi(cad), "/api/cad/geocode", VAL, "q=Testvile+Diner")[1]["matches"][0]
    assert hit["place_key"] == diner
    [row] = cad.settings_get()["place_names"]["aliased"]
    assert row == {"key": diner, "name": "Testville Diner", "town": "Port Chester", "aliases": ["Testvile Diner"],
                   "imported": True}
    reimport()                                                                 # kept by a re-import
    assert [r["aliases"] for r in cad.local_places() if r["key"] == diner] == [["Testvile Diner"]]
    code, raw, _ = CadApi(cad).post("/api/cad/settings/places", json.dumps(
        {"action": "unalias", "key": diner, "alias": "Testvile Diner"}).encode(), Identity(email=CASEY))
    assert code == 200 and json.loads(raw)["removed"] is True
    assert cad.settings_get()["place_names"]["aliased"] == []
    assert cad.settings_audit_log({"section": ["lists"]})["audit"][0]["key"] == "place/TESTVILLE DINER"


def test_a_sites_alias_goes_on_its_primary_and_the_note_audit_says_that_entry(placed):
    """#960 review 7: a note picked a site's other address; the alias is the primary's, and the audit's before and
    after are both the primary's."""
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    base = {"name": "SAMPLE PLAZA", "label": "Sample Plaza", "lat": 41.0040, "lon": -73.6660, "muni": "Port Chester",
            "note": "", "source": commonplaces.SOURCE, "imported": "2026-11-01", "located": "exact"}
    a, b = "county/sample plaza @ 10 example ave", "county/sample plaza @ 12 example ave"
    cad.settings._commit([("places", a, dict(base, aliases=["Sample Plaza Mall"], address="10 EXAMPLE AVE", site=a,
                                             site_addresses=["10 EXAMPLE AVE", "12 EXAMPLE AVE"])),
                          ("places", b, dict(base, aliases=[], address="12 EXAMPLE AVE", lat=41.0041, site=a))], "t", [])
    e = save(cad)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"heard": "Sampel Plaza", "corrected_pick": {
        "lat": 41.0041, "lon": -73.6660, "label": "Sample Plaza", "kind": "place", "place_key": b}}, VAL)[1]["note"]["id"]
    code, out = call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
    assert code == 200 and out["added"] is True
    places = {r["key"]: r for r in cad.local_places()}
    assert places[a]["aliases"] == ["Sample Plaza Mall", "Sampel Plaza"] and places[b]["aliases"] == []
    audit = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["audit"][-1]
    assert json.loads(audit["before"]) == {"place_key": a, "aliases": ["Sample Plaza Mall"]}
    assert json.loads(audit["after"]) == {"place_key": a, "aliases": ["Sample Plaza Mall", "Sampel Plaza"]}


@needs_node
def test_the_note_says_why_an_alias_is_refused_and_offers_to_take_one_off(placed):
    cad, _clock, _gaz = placed
    api = CadApi(cad)
    post(cad, "places", action="set", name="Sample Market",
         place={"label": "40 Example Ave", "lat": 41.0030, "lon": -73.6650, "sub": "Port Chester"})
    m = suggestion(api, "@ Sample Diner")
    e = save(cad)
    call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sample Market"}, VAL)
    nid = call(api, f"/api/cad/intel/{e.id}/notes", {"corrected_pick": pick_of(m), "heard": "Sampel Diner"},
               VAL)[1]["note"]["id"]
    call(api, f"/api/cad/intel/notes/{nid}/alias", {}, CASEY)
    d = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]
    script = f"""
{(STATIC / "intel-notes.js").read_text()}
const posts = [];
global.UI = {{ toast() {{}}, confirm: async () => true }};
global.api = async (m, p, b) => {{ if (m === "POST") {{ posts.push([p, b]); return {{ ok: true }}; }} return JSON.parse(JSON.stringify(IN)); }};
(async () => {{
  const host = $("#pi-notes");
  await IntelNotes.mount(host, {e.id});
  const rows = host.children[1].children;
  const btns = (li) => li.all().filter((x) => x.tagName === "BUTTON");
  const taken = btns(rows[0]).find((b) => b.textContent.startsWith("+ Alias"));
  const off = btns(rows[1]).find((b) => b.textContent === "− Alias");
  off.on.click[0]();
  await new Promise((r) => setTimeout(r, 5));
  console.log(JSON.stringify({{ disabled: taken.disabled, why: taken.title, second: btns(rows[1]).map((b) => b.textContent), posts }}));
}})();
"""
    out = run_node(script, d)
    assert out["disabled"] is True and "already a name of Sample Market" in out["why"]
    assert "+ Alias of Sample Diner" not in out["second"] and "− Alias" in out["second"]
    assert out["posts"] == [[f"/api/cad/intel/notes/{nid}/alias", {"remove": True, "operator": ""}]]


@pytest.mark.parametrize("alias", ["Port Chester", "port  chester", "Village of Rye Brook", "Rye", "Harrison", "Chester",
                                   "Greenwich"])
def test_a_town_is_never_an_alias(placed, alias):
    """#960 re-review 1: "Port Chester" as the diner's alias would pin a county event located only by its town there."""
    cad, _clock, _gaz = placed
    code, out = alias_by_settings(cad, "sample diner", alias)
    assert code == 409 and "town" in out["error"], out
    assert commonplaces.Lookup(cad.local_places()).find("PORT CHESTER") is None


@pytest.mark.parametrize("alias", ["Lyon Park", "lyon  park", "Rye Playland", "Playland"])
def test_a_place_on_the_map_is_never_an_alias(placed, alias):
    """#960 re-review 2: the map's own place names (and their aliases) stay theirs."""
    cad, _clock, _gaz = placed
    code, out = alias_by_settings(cad, "sample diner", alias)
    assert code == 409 and "on the map" in out["error"], out


@pytest.mark.parametrize("alias", ["Golden Star Diner", "Lucky Garden", "Royal Palace", "Grand Union", "King St Diner",
                                   "Mobile Station", "CVS", "KFC", "BJs", "BJ's"])
def test_business_names_and_acronyms_may_be_aliases(placed, alias):
    """#960 re-review 3: common words refuse a single word only; several need one that isn't generic; an acronym."""
    cad, _clock, _gaz = placed
    code, out = alias_by_settings(cad, "sample diner", alias)
    assert code == 200 and out["added"] is True, out
