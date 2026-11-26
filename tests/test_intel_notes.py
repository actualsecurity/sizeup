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
from tests.test_roles import CASEY, JORDAN, VAL, call

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
    assert code == 400 and "author or an admin" in out["error"]
    clock.t += 30
    assert call(api, f"/api/cad/intel/notes/{nid}", {"text": "a man waving a machete outside the deli, Car 3 there"}, VAL)[0] == 200
    assert call(api, f"/api/cad/intel/notes/{nid}", {"tags": ["rule_to_add"]}, CASEY)[0] == 200
    code, d = jget(api, f"/api/cad/intel/{e.id}/notes", JORDAN)
    [n] = d["notes"]
    assert n["text"].endswith("Car 3 there") and n["tags"] == ["rule_to_add"] and n["edited"] and not n["may_edit"]
    assert "audit" not in d
    assert call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, JORDAN)[0] == 400
    assert call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, CASEY)[0] == 200
    assert jget(api, f"/api/cad/intel/{e.id}/notes", VAL)[1]["notes"] == []


def test_nobody_without_a_sign_in(site):
    cad, _clock = site
    e = save(cad)
    api = CadApi(cad)
    assert get(api, f"/api/cad/intel/{e.id}/notes", None)[0] == 403
    assert add(api, e.id, None, text="x")[0] == 400                 # refused (the sign-in gate), nothing written
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_notes")["n"] == 0


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
    assert [(r["verdict"], r["traffic_id"]) for r in rows] == [("garbled", "c1-t0"), ("garbled", "c1-t1"), ("wrong_type", None)]
    assert rows[2]["corrected_category"] == "mva_injury" and rows[0]["ruleset_hash"] == "abc123"
    assert json.loads(rows[0]["rules"]) == [["pd.shots", 1, 0.7]] and rows[0]["operator"] == VALN
    call(api, f"/api/cad/intel/notes/{nid}", {"tags": ["should_have_pushed"]}, VAL)
    assert [(r["verdict"], r["traffic_id"]) for r in fb()] == [("should_push", None)]
    call(api, f"/api/cad/intel/notes/{nid}", {"delete": True}, VAL)
    assert fb() == []
    assert set(notes_mod.TAG_VERDICT.values()) <= set(__import__("sizeup.intel.events", fromlist=["VERDICTS"]).VERDICTS)


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
    # the page's filters select what goes out
    code, raw, *_ = CadApi(cad).get("/api/cad/intel/export.jsonl", "tag=late", Identity(email=CASEY))
    assert code == 200 and raw == b""


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
