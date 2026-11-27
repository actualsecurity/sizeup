"""Notes and tags on radio intel events (sizeup/intel/notes.py, static/intel-notes.js).

The admin (2026-11-25): "a way to add notes, so we can take those notes per incident and modify in the future how we
look at or pull in that info and respond to it". Notes with tags and corrections; the author edits their own, an
admin any; every change audited; the tags are verdicts in intel_feedback (what tuning reads); the Intel page filters
by the notes' words, a tag and the review queue; an admin's export; the incident's view.

Invented scenes only (12 Example Road, Car 3, a man waving a machete outside the deli)."""

from __future__ import annotations

import csv
import io
import json

import pytest

from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.intel import notes as notes_mod
from tests.test_intel_page import (NODE, STATIC, T0, heard, listing, needs_node, run_node, save,  # noqa: F401
                                   site)
from tests.test_roles import CASEY, DEE, JORDAN, VAL, call

VALN, JORDANN, CASEYN = "Val <val@example.org>", "Jordan <jordan@example.org>", "Casey <casey@example.org>"


def get(api, path, who=CASEY, query=""):
    code, raw, *_ = api.get(path, query, Identity(email=who) if who else None)
    return code, raw


def jget(api, path, who=CASEY, query=""):
    code, raw = get(api, path, who, query)
    return code, json.loads(raw) if raw[:1] == b"{" else {}


def add(api, eid, who=VAL, **body):
    return call(api, f"/api/cad/intel/{eid}/notes", body, who)


# -- CRUD and who may ---------------------------------------------------------------------------------------------------

def test_anyone_signed_in_adds_the_author_edits_an_admin_any(site):
    cad, clock = site
    e = save(cad, suppressed="unplaced")
    api = CadApi(cad)
    code, out = add(api, e.id, VAL, text="a man waving a machete outside the deli", tags=["should_have_pushed"])
    assert code == 200 and out["note"]["author"] == "Val" and out["note"]["may_edit"] is True
    nid = out["note"]["id"]
    row = cad.store.one("SELECT author, author_email FROM intel_notes WHERE id = ?", (nid,))
    assert row == {"author": VALN, "author_email": VAL}                       # who signed in, not what the page said
    code, out = call(api, f"/api/cad/intel/notes/{nid}", {"text": "not mine"}, JORDAN)
    assert code == 403 and "author or an admin" in out["error"]
    refused = [a for a in cad.activity.rows(JORDAN) if a["kind"] == "refused"]
    assert refused and "author or an admin" in refused[0]["summary"]              # refused, in their activity
    clock.t += 30
    assert call(api, f"/api/cad/intel/notes/{nid}", {"text": "a man waving a machete outside the deli, Car 3 there"}, VAL)[0] == 200
    assert call(api, f"/api/cad/intel/notes/{nid}", {"tags": ["rule_to_add"]}, CASEY)[0] == 200
    code, d = jget(api, f"/api/cad/intel/{e.id}/notes", JORDAN)
    [n] = d["notes"]
    assert n["text"].endswith("Car 3 there") and n["tags"] == ["rule_to_add"] and n["edited"] and not n["may_edit"]
    assert "audit" not in d
    assert call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, JORDAN)[0] == 403
    assert call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, CASEY)[0] == 200
    assert jget(api, f"/api/cad/intel/{e.id}/notes", VAL)[1]["notes"] == []


def test_nobody_without_a_sign_in(site):
    cad, _clock = site
    e = save(cad)
    api = CadApi(cad)
    assert get(api, f"/api/cad/intel/{e.id}/notes", None)[0] == 403
    assert add(api, e.id, None, text="x")[0] == 400                 # refused (the sign-in gate), nothing written
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_notes")["n"] == 0


def test_a_deactivated_account_neither_notes_nor_votes(site):
    """#913 review F4: a note and a vote are writes: a signed-in person whose account is active."""
    cad, _clock = site
    e = save(cad)
    api = CadApi(cad)
    nid = add(api, e.id, VAL, text="Car 3 there")[1]["note"]["id"]
    assert add(api, e.id, DEE, text="x")[0] == 403
    assert call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "useful"}, DEE)[0] == 403
    assert call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, DEE)[0] == 403
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_notes")["n"] == 1


def test_a_note_needs_something_and_only_known_tags_and_values(site):
    cad, _clock = site
    e = save(cad)
    api = CadApi(cad)
    assert add(api, e.id, VAL, text="")[0] == 400
    assert add(api, e.id, VAL, tags=["louder"])[0] == 400
    assert add(api, e.id, VAL, corrected_category="parade")[0] == 400
    assert add(api, e.id, VAL, corrected_level="loud")[0] == 400
    assert add(api, 99999, VAL, text="x")[0] == 400
    code, out = add(api, e.id, VAL, tags=["wrong_place"], corrected_place="12 Example Road, Rye", corrected_level="push",
                    corrected_category="mva_injury")
    assert code == 200 and out["note"]["corrected_place"] == "12 Example Road, Rye"


def test_every_change_is_audited_with_before_and_after(site):
    cad, _clock = site
    e = save(cad)
    api = CadApi(cad)
    nid = add(api, e.id, VAL, text="first", tags=["late"])[1]["note"]["id"]
    call(api, f"/api/cad/intel/notes/{nid}", {"text": "second"}, VAL)
    call(api, f"/api/cad/intel/notes/{nid}/done", {"done": "PR #123"}, CASEY)
    call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, VAL)
    call(api, f"/api/cad/intel/notes/{nid}", {"text": "second"}, VAL)                # the same again: no row
    a = jget(api, f"/api/cad/intel/{e.id}/notes", CASEY)[1]["audit"]
    assert [(x["action"], x["operator"]) for x in a] == [("add", "Val"), ("edit", "Val"), ("done", "Casey"), ("delete", "Val")]
    assert json.loads(a[1]["before"])["text"] == "first" and json.loads(a[1]["after"])["text"] == "second"
    assert json.loads(a[2]["after"]) == {"done": "PR #123"}
    assert json.loads(a[3]["before"])["text"] == "second" and a[3]["after"] == ""


# -- tags into the feedback store -------------------------------------------------------------------------------------

def test_tags_are_verdicts_tuning_reads_rewritten_on_edit_removed_on_delete(site):
    cad, _clock = site
    e = save(cad, traffic_ids=["c1-t0", "c1-t1"], ruleset_hash="abc123")
    api = CadApi(cad)
    nid = add(api, e.id, VAL, text="audio cut out", tags=["false_garbled_audio", "wrong_category"],
              corrected_category="mva_injury")[1]["note"]["id"]
    fb = lambda: cad.store.q("SELECT verdict, traffic_id, corrected_category, rules, ruleset_hash, operator FROM intel_feedback "  # noqa: E731
                             "WHERE note_id = ? ORDER BY id", (nid,))
    rows = fb()
    # each tag once per note; the clips per transmission under their own verdict (the speech-to-text side's)
    assert [(r["verdict"], r["traffic_id"]) for r in rows] == [("garbled", None), ("wrong_type", None),
                                                              ("garbled_tx", "c1-t0"), ("garbled_tx", "c1-t1")]
    assert rows[1]["corrected_category"] == "mva_injury" and rows[0]["ruleset_hash"] == "abc123"
    assert json.loads(rows[0]["rules"]) == [["pd.shots", 1, 0.7]] and rows[0]["operator"] == VALN
    call(api, f"/api/cad/intel/notes/{nid}", {"tags": ["should_have_pushed"]}, VAL)
    assert [(r["verdict"], r["traffic_id"]) for r in fb()] == [("should_push", None)]
    call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, VAL)
    assert fb() == []
    assert set(notes_mod.TAG_VERDICT.values()) <= set(__import__("sizeup.intel.events", fromlist=["VERDICTS"]).VERDICTS)
    # corrections and no tag: one "corrected" row
    nid2 = add(api, e.id, VAL, corrected_place="12 Example Road, Rye")[1]["note"]["id"]
    [r] = cad.store.q("SELECT verdict, corrected FROM intel_feedback WHERE note_id = ?", (nid2,))
    assert r["verdict"] == "corrected" and json.loads(r["corrected"]) == {"place": "12 Example Road, Rye"}


def test_a_notes_tags_are_never_votes(site):
    """#913 review F1: one note tagged garbled + should_have_pushed on a 3-transmission event is no vote of its author's."""
    cad, _clock = site
    e = save(cad, traffic_ids=["c1-t0", "c1-t1", "c1-t2"])
    api = CadApi(cad)
    add(api, e.id, VAL, text="cut out", tags=["false_garbled_audio", "should_have_pushed"])
    call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "useful"}, JORDAN)
    [row] = listing(cad, who=VAL)["events"]
    assert row["votes"] == {"useful": 1} and row["mine"] == []
    assert row["notes"]["tags"] == ["false_garbled_audio", "should_have_pushed"]
    b = jget(api, f"/api/cad/prealerts/intel/{e.id}", VAL)[1]["outcome"]
    assert [f["verdict"] for f in b["feedback"]] == ["useful"]
    assert b["note_tags"] == {"false_garbled_audio": 1, "should_have_pushed": 1}
    # Val's own vote is still one of its own, the note untouched
    assert call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "should_push"}, VAL)[1]["removed"] is False
    assert call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "should_push"}, VAL)[1]["removed"] is True
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_feedback WHERE note_id IS NOT NULL AND verdict = 'should_push'")["n"] == 1


# -- the review queue and the page's filters ---------------------------------------------------------------------------

def test_the_review_queue_spans_every_date_until_done(site):
    cad, clock = site
    clock.t = T0 + 200 * 86400
    old = save(cad, first_at=T0, last_at=T0)                               # 200 days ago, kept by its note
    new = save(cad, first_at=clock.t - 60, last_at=clock.t - 60)
    plain = save(cad, first_at=clock.t - 120, last_at=clock.t - 120)
    api = CadApi(cad)
    n1 = add(api, old.id, VAL, text="a rule should catch this", tags=["rule_to_add"])[1]["note"]["id"]
    add(api, new.id, VAL, text="read back late", tags=["late"])                # not a review tag
    add(api, plain.id, VAL, text="Car 3 handled it")
    assert [e["id"] for e in listing(cad, review=1)["events"]] == [old.id]
    assert old.id not in [e["id"] for e in listing(cad)["events"]]            # (the default 24 h)
    assert call(api, f"/api/cad/intel/notes/{n1}/done", {"done": "PR #123"}, JORDAN)[0] == 403     # admins only
    assert call(api, f"/api/cad/intel/notes/{n1}/done", {"done": "PR #123"}, CASEY)[0] == 200
    assert listing(cad, review=1)["events"] == []
    cad.intel.events.prune(clock.t)                                          # 200 days: kept, its note keeps it
    assert cad.store.one("SELECT id FROM intel_events WHERE id = ?", (old.id,)) is not None
    # by the note's time: an old event noted now is in a review of the last day (#913 review F3)
    n2 = add(api, old.id, VAL, text="and again", tags=["wrong_category"])[1]["note"]["id"]
    day = __import__("datetime").datetime.fromtimestamp(clock.t, __import__("sizeup.cad.store", fromlist=["TZ"]).TZ)
    one_day = {"from": day.strftime("%Y-%m-%d"), "review": 1}
    assert [e["id"] for e in listing(cad, **one_day)["events"]] == [old.id]
    call(api, f"/api/cad/intel/notes/{n2}/done", {"done": "PR #124"}, CASEY)
    assert listing(cad, **one_day)["events"] == []
    # the notes' words and tags reach it too, past the 92 days of the plain view (F2)
    assert [e["id"] for e in listing(cad, q="rule")["events"]] == [old.id]
    assert [e["id"] for e in listing(cad, tag="rule_to_add")["events"]] == [old.id]


def test_note_words_tags_and_the_rows_count_chip(site):
    cad, _clock = site
    a = save(cad)
    b = save(cad, first_at=T0 - 60)
    save(cad, first_at=T0 - 120)
    api = CadApi(cad)
    add(api, a.id, VAL, text="a man waving a machete outside the deli", tags=["should_have_pushed"])
    add(api, a.id, JORDAN, text="Car 3 confirmed", tags=["late"])
    add(api, b.id, VAL, text="100% wrong_place?", tags=["wrong_place"])
    ids = lambda **q: [e["id"] for e in listing(cad, **q)["events"]]      # noqa: E731
    assert ids(q="machete") == [a.id] and ids(q="MACHETE+deli") == [a.id] and ids(q="car") == [a.id]
    assert ids(q="100%25") == [b.id] and ids(q="wrong_") == [b.id]           # % and _ are words, not wildcards
    assert ids(tag="wrong_place") == [b.id] and ids(tag="late,wrong_place") == [a.id, b.id]
    rows = {e["id"]: e["notes"] for e in listing(cad)["events"]}
    assert rows[a.id] == {"n": 2, "tags": ["late", "should_have_pushed"], "review": 1}
    assert rows[b.id] == {"n": 1, "tags": ["wrong_place"], "review": 0}
    assert jget(api, "/api/cad/intel/list", VAL, "tag=bogus")[0] == 400


# -- the export (admins) -------------------------------------------------------------------------------------------------

def test_the_export_is_the_admins_with_notes_hits_model_and_transcripts(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    [e] = cad.intel.events.open_events()
    api = CadApi(cad)
    add(api, e.id, VAL, text="a man waving a machete outside the deli", tags=["rule_to_add"], corrected_place="12 Example Road")
    call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "useful"}, JORDAN)
    for who in (VAL, JORDAN, None):
        assert get(api, "/api/cad/intel/export.csv", who)[0] == 403
    code, raw, ctype, hdr = CadApi(cad).get("/api/cad/intel/export.csv", "", Identity(email=CASEY))
    assert code == 200 and ctype.startswith("text/csv") and "attachment" in hdr["Content-Disposition"]
    [row] = list(csv.DictReader(io.StringIO(raw.decode())))
    assert row["category"] == "weapons" and row["level"] == "push" and row["source"] == "MRRS PD"
    assert "Val: a man waving a machete outside the deli [rule_to_add] (place -> 12 Example Road)" in row["notes"]
    assert "shots fired at Sample St and Test Rd" in row["transcripts"] and "c1-t0:pd.shots" in row["hits"]
    assert "useful" in row["feedback"] and "rule_to_add" in row["feedback"]
    code, raw, ctype, _h = CadApi(cad).get("/api/cad/intel/export.jsonl", "", Identity(email=CASEY))
    [j] = [json.loads(x) for x in raw.decode().splitlines()]
    assert ctype.startswith("application/x-ndjson")
    assert j["notes"][0]["tags"] == ["rule_to_add"] and j["transcripts"][0]["text"] == "shots fired at Sample St and Test Rd"
    assert j["rules"] == [["pd.shots", 1, 0.75]] and j["model"] is None and j["hits"][0]["rule_id"] == "pd.shots"
    acts = [a for a in reversed(cad.activity.rows(CASEY)) if "intel/export" in a["route"]]
    assert [(a["kind"], a["summary"]) for a in acts] == [("search", "Intel export (CSV): 1 event"),
                                                         ("search", "Intel export (JSONL): 1 event")]
    assert "machete" not in json.dumps(acts)                                  # the count only, never the notes
    added = [a for a in cad.activity.rows(VAL) if "note" in a["summary"]]
    assert [a["summary"] for a in added] == [f"Added a note on radio pre-alert {e.id} (rule_to_add)"]
    # the page's filters select what goes out
    code, raw, *_ = CadApi(cad).get("/api/cad/intel/export.jsonl", "tag=late", Identity(email=CASEY))
    assert code == 200 and raw == b""


def test_the_csv_never_carries_a_formula(site):
    """#913 review F7: export.py's guard on every cell (a place or a source as heard could start with "=")."""
    cad, _clock = site
    save(cad, precision="address", label_full="=HYPERLINK(\"http://example.org\")", source_key="@example", lat=41.0, lon=-73.7)
    _c, raw, *_ = CadApi(cad).get("/api/cad/intel/export.csv", "", Identity(email=CASEY))
    [row] = list(csv.DictReader(io.StringIO(raw.decode())))
    assert row["place"].startswith("'=") and row["source_key"] == "'@example"
    assert row["lon"] == "-73.7"                                      # a number stays a number


def test_a_noted_events_transmissions_are_kept_while_the_note_is(site):
    """#913 review F8: the note's pointers never go stale: its transmissions are keepers, their audio a record."""
    cad, clock = site
    from tests.test_intel import ctx_row
    for tid in ("n1", "n2", "x1"):
        cad.comms.record(ctx_row(tid, "test transmission", T0 - 400 * 86400))
    e = save(cad, traffic_ids=["n1", "n2"])
    api = CadApi(cad)
    nid = add(api, e.id, VAL, text="garbled", tags=["false_garbled_audio"])[1]["note"]["id"]
    assert cad.comms.kept_for_events(["n1", "n2", "x1"]) >= {"n1", "n2"} and "x1" not in cad.comms.kept_for_events(["x1"])
    cad.comms.prune()
    left = {r["id"] for r in cad.store.q("SELECT id FROM radio_traffic WHERE id IN ('n1', 'n2', 'x1')")}
    assert left == {"n1", "n2"}
    call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, VAL)
    assert "n1" not in cad.comms.kept_for_events(["n1"])


def test_the_prune_reads_the_noted_transmissions_once_never_a_subquery_a_row(site, monkeypatch):
    """#913 re-check B1: with thousands of notes, the per-row DELETE under the write lock never rebuilds the noted list
    (it was 3-9 ms a row): the ids are read once per prune and skipped before the delete."""
    import time
    from tests.test_intel import ctx_row
    cad, _clock = site
    old = T0 - 400 * 86400
    with cad.store.tx() as db:                     # 3000 noted events, two transmissions each (none in the log)
        for i in range(3000):
            db.execute("INSERT INTO intel_events (domain, pack, category, subject, first_at, last_at, until, status, "
                       "traffic_ids, updated_at) VALUES ('police', 'police', 'weapons', 'x', ?, ?, ?, 'ended', ?, ?)",
                       (T0, T0, T0, json.dumps([f"k{i}a", f"k{i}b"]), T0))
            db.execute("INSERT INTO intel_notes (event_id, text, tags, created_at, updated_at) VALUES (?, 'n', '[]', ?, ?)",
                       (i + 1, T0, T0))
    for k in range(1500):                          # 1500 old transmissions to go, and two of the noted ones to stay
        cad.comms.record(ctx_row(f"old{k}", "test transmission", old + k))
    for tid in ("k5a", "k2999b"):
        cad.comms.record(ctx_row(tid, "test transmission", old + 2000))
    seen = []
    real = cad.store.q
    monkeypatch.setattr(cad.store, "q", lambda sql, args=(): (seen.append(sql), real(sql, args))[1])
    t = time.perf_counter()
    gone = cad.comms.prune()
    took = time.perf_counter() - t
    monkeypatch.undo()
    assert gone >= 1500
    left = {r["id"] for r in cad.store.q("SELECT id FROM radio_traffic WHERE start < ?", (old + 3000,))}
    assert left == {"k5a", "k2999b"}
    assert sum("FROM intel_notes" in x for x in seen) == 1                 # the noted ids: one read
    assert took < 10.0, took                     # (generous: about 2 s here; the per-row check took 3-9 ms a row)


# -- the incident's view -------------------------------------------------------------------------------------------------

def test_the_incidents_view_has_the_notes_of_its_linked_events(site):
    cad, _clock = site
    inc = cad.create({"type": "STRU", "address": "40 Example Place", "operator": "Casey"})
    near = save(cad, category="fire_report", near_incident=inc["id"], suppressed="fd_on_scene")
    took = save(cad, first_at=T0 + 60, incident_id=inc["id"])
    other = save(cad, first_at=T0 + 120)
    api = CadApi(cad)
    add(api, near.id, JORDAN, text="Car 3 confirmed smoke from the rear", tags=["late"])
    add(api, took.id, VAL, text="accepted from the card")
    add(api, other.id, VAL, text="not this call")
    code, d = jget(api, f"/api/cad/incident/{inc['id']}/intel", VAL)
    assert code == 200 and [e["id"] for e in d["events"]] == [near.id, took.id]
    assert [n["text"] for n in d["notes"]] == ["Car 3 confirmed smoke from the rear", "accepted from the card"]
    assert d["events"][0]["level"] == "log"
    assert get(api, f"/api/cad/incident/{inc['id']}/intel", None)[0] == 403


# -- the page ------------------------------------------------------------------------------------------------------------

def test_the_notes_are_on_more_info_the_incident_and_the_page():
    html = (STATIC / "cad.html").read_text()
    assert '<section id="pi-notes"' in html and '<section id="d-intel"' in html
    assert html.index('src="/static/intel-notes.js"') < html.index('src="/static/intel-page.js"')
    for f in ("il-q", "il-tag", "il-review", "il-csv", "il-jsonl"):
        assert f'id="{f}"' in html, f
    assert 'IntelNotes.mount($("#pi-notes")' in (STATIC / "prealert-info.js").read_text()
    assert "IntelNotes.incident(inc)" in (STATIC / "cad.js").read_text()
    js = (STATIC / "intel-notes.js").read_text()
    assert "setInterval" not in js and "STALE_MS = 120000" in js


@needs_node
def test_more_info_draws_the_notes_once_and_only_its_authors_may_edit(site):
    cad, _clock = site
    e = save(cad)
    api = CadApi(cad)
    add(api, e.id, VAL, text="a man waving a machete outside the deli", tags=["should_have_pushed", "rule_to_add"])
    add(api, e.id, JORDAN, text="Car 3 confirmed")
    d = jget(api, f"/api/cad/intel/{e.id}/notes", VAL)[1]
    script = f"""
{(STATIC / "intel-notes.js").read_text()}
let calls = 0;
global.api = async () => {{ calls++; return JSON.parse(JSON.stringify(IN)); }};
global.fmtTime = (u) => `T${{Math.round(u)}}`;
(async () => {{
  const host = $("#pi-notes");
  await IntelNotes.mount(host, {e.id});
  const first = host.children[1];
  await IntelNotes.mount(host, {e.id});                 // the bundle's Refresh: the same id, not read again
  const items = first.children.map((li) => li.textContent);
  const edits = first.children.map((li) => li.all().filter((x) => x.tagName === "BUTTON").map((b) => b.textContent));
  console.log(JSON.stringify({{ calls, same: host.children[1] === first, items, edits, hidden: host.hidden,
    chip: IntelNotes.chip({{ n: 2, tags: ["rule_to_add"], review: 1 }}).textContent, none: IntelNotes.chip({{ n: 0, tags: [] }}) }}));
}})();
"""
    out = run_node(script, d)
    assert out["calls"] == 1 and out["same"] is True and out["hidden"] is False
    assert "should push" in out["items"][0] and "rule to add" in out["items"][0] and "review" in out["items"][0]
    assert "a man waving a machete outside the deli" in out["items"][0] and "Car 3 confirmed" in out["items"][1]
    assert out["edits"] == [["Edit", "Delete"], []]                            # Val's own, not Jordan's
    assert out["chip"] == "✎2" and out["none"] is None


def test_the_list_row_carries_the_done_count_and_texts(site):
    cad, _clock = site
    a = save(cad)
    b = save(cad, first_at=T0 - 60)
    api = CadApi(cad)
    n1 = add(api, a.id, VAL, text="one", tags=["rule_to_add"])[1]["note"]["id"]
    n2 = add(api, a.id, VAL, text="two", tags=["late"])[1]["note"]["id"]
    add(api, b.id, VAL, text="three")
    call(api, f"/api/cad/intel/notes/{n1}/done", {"done": "PR #123"}, CASEY)
    call(api, f"/api/cad/intel/notes/{n2}/done", {"done": "rule added"}, CASEY)
    rows = {e["id"]: e["notes"] for e in listing(cad)["events"]}
    assert rows[a.id]["done"] == 2 and sorted(rows[a.id]["done_texts"]) == ["PR #123", "rule added"]
    assert rows[a.id]["review"] == 0 and "done" not in rows[b.id]


@needs_node
def test_a_done_text_links_its_pr_numbers_and_leaves_the_rest_as_text():
    script = f"""
{(STATIC / "intel-notes.js").read_text()}
const view = (t) => IntelNotes.linkify(t).map((x) => typeof x === "string" ? x
  : [x.textContent, x.attrs.href, x.attrs.target, x.attrs.rel]);
const chip = IntelNotes.chip({{ n: 1, tags: [], review: 0, done: 1, done_texts: ["PR #123", "rule added"] }});
console.log(JSON.stringify({{ a: view("PR #12, #34 and rule x"), b: view('<img src=x onerror=alert(1)> #5'),
  c: view("plain"), ok: chip.children[0].textContent, tip: chip.children[0].title }}));
"""
    out = run_node(script, {})
    pr = lambda n, t: [t, f"https://github.com/actualsecurity/sizeup/pull/{n}", "_blank", "noopener"]   # noqa: E731
    assert out["a"] == [pr(12, "PR #12"), ", ", pr(34, "#34"), " and rule x"]
    assert out["b"] == ["<img src=x onerror=alert(1)> ", pr(5, "#5")] and out["c"] == ["plain"]
    assert out["ok"] == " ✓" and out["tip"] == "Done: PR #123 · rule added"
