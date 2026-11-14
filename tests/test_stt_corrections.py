"""Hand-corrected transcripts (sizeup/cad/sttfix.py, radio intel plan PR 7, #428's corrections side): the table, the
admin API, the audit (never the words), retention with the transmission, another system's rows behind a sign-in, and
#428's JSONL export contract. Synthetic transcripts only (Example Ave, made-up units)."""

import json
import sys
from pathlib import Path

import pytest

from sizeup import sourcekeys
from sizeup.cad import sttfix
from sizeup.cad.api import ADMIN_READS, CadApi, needs
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_roles import CASEY, JORDAN, people

CONTRACT = {"clip_id", "channel", "talkgroup", "talker", "start", "end", "asr", "fixed_text", "correction"}
EXTENSIONS = {"system", "service"}                 # ours, beside #428's fields (and correction.based_on)


def tx(cad, tid, text, *, start=1_800_000_000.0, channel="Fire/EMS 15", talkgroup=307, system="westchester",
       service="fire", model="aws-transcribe", alt_text="", alt_model="", talker="60 Control"):
    with cad.store.tx() as db:
        db.execute("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, talker, text, model, alt_text, alt_model, "
                   "source, system, service, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
                   (tid, start, start + 4, channel, talkgroup, talker, text, model, alt_text, alt_model,
                    f"p25:{system}:{talkgroup}", system, service, start))


@pytest.fixture
def cad(tmp_path):
    return make_cad(tmp_path)


def test_table_created_in_cad_sqlite_without_a_schema_bump(cad):
    from sizeup.cad.store import SCHEMA_VERSION
    assert cad.store.one("SELECT name FROM sqlite_master WHERE name = 'stt_corrections'")
    assert cad.store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = {r["name"] for r in cad.store.q("PRAGMA table_info(stt_corrections)")}
    assert cols == {"traffic_id", "original_hash", "corrected", "domain", "tags", "by", "at", "reference", "based_on"}
    sttfix.SttCorrections(cad)                      # opening again changes nothing


def test_save_get_tags_and_stale_after_a_rerun(cad):
    tx(cad, "c1", "engine 60 respond to 4 56 example avenue", alt_text="engine 60 respond to 456 sample avenue",
       alt_model="ggml-large-v3-turbo.bin")
    assert cad.stt_fix.get("c1")["correction"] is None
    said = "Engine 60 respond to 456 Example Ave"
    out = cad.stt_fix.save("c1", {"corrected": f"  {said} ", "reference": True, "operator": "JD",
                                  "tags": [{"kind": "address", "start": 21, "end": 36}, {"kind": "callsign", "start": 0, "end": 9}]})
    c = out["correction"]
    assert c["corrected"] == said and c["reference"] is True and c["by"] == "JD" and c["domain"] == "fire"
    assert [said[t["start"]:t["end"]] for t in c["tags"]] == ["Engine 60", "456 Example Ave"]
    assert c["stale"] is False and out["alt_text"] and out["text"]
    with cad.store.tx() as db:                      # a re-run changed the text since
        db.execute("UPDATE radio_traffic SET text = 'engine 60 respond to 456 example ave' WHERE id = 'c1'")
    assert cad.stt_fix.get("c1")["correction"]["stale"] is True


@pytest.mark.parametrize("body, why", [
    ({"corrected": ""}, "corrected"),
    ({"corrected": "x" * 2001}, "corrected"),
    ({"corrected": "ok", "domain": "space"}, "domain"),
    ({"corrected": "ok", "reference": "yes"}, "reference"),
    ({"corrected": "ok", "tags": [{"kind": "address", "start": 0, "end": 9}]}, "tags"),     # past the end
    ({"corrected": "ok then", "tags": [{"kind": "name", "start": 0, "end": 2}]}, "tags"),  # not a kind
    ({"corrected": "ok then", "tags": [{"kind": "callsign", "start": 2, "end": 2}]}, "tags"),
    ({"corrected": "ok then", "tags": [{"kind": "callsign", "start": True, "end": 2}]}, "tags"),
])
def test_bad_bodies_are_refused(cad, body, why):
    from sizeup.cad.store import CadError
    tx(cad, "c1", "ok")
    with pytest.raises(CadError, match=why):
        cad.stt_fix.save("c1", body)
    assert cad.store.one("SELECT 1 FROM stt_corrections") is None


def test_the_audit_never_holds_the_words(cad):
    tx(cad, "c1", "ladder 31 on location 12 example court")
    said = "Ladder 31 on location, 12 Example Ct"
    cad.stt_fix.save("c1", {"corrected": said, "tags": [{"kind": "callsign", "start": 0, "end": 9}], "operator": "JD"})
    cad.stt_fix.save("c1", {"corrected": said, "reference": True, "operator": "JD"})
    cad.stt_fix.save("c1", {"delete": True, "operator": "JD"})
    rows = cad.settings.audit_log(sttfix.SECTION)
    assert len(rows) == 3 and {r["key"] for r in rows} == {"c1"} and {r["operator"] for r in rows} == {"JD"}
    blob = json.dumps(rows).lower()
    for w in ("ladder", "example", "location", "hash", sttfix.text_hash("ladder 31 on location 12 example court"),
              sttfix.text_hash(said)):
        assert w not in blob                             # no words, and no hash of them or of the transcript
    first = json.loads(rows[-1]["new"])
    assert first == {"words": 7, "domain": "fire", "tags": {"callsign": 1}, "reference": False, "based_on": ""}
    assert rows[0]["new"] in (None, "", "null")          # the delete
    assert cad.stt_fix.get("c1")["correction"] is None


def test_a_correction_goes_with_its_transmission(cad):
    """A transmission deleted (an admin's, or the prune with "Keep corrected clips" off): its correction goes with it."""
    tx(cad, "c1", "engine 61 available")
    tx(cad, "c2", "engine 62 available")
    for t in ("c1", "c2"):
        cad.stt_fix.save(t, {"corrected": "Engine 6x available"})
    with cad.store.tx() as db:
        db.execute("DELETE FROM radio_traffic WHERE id = 'c1'")
    assert [r["traffic_id"] for r in cad.store.q("SELECT traffic_id FROM stt_corrections")] == ["c2"]


def test_another_systems_row_only_behind_a_sign_in(cad):
    """A police talkgroup's transmission (another P25 system: context, #419) is never served without a verified
    person; with one it is, like the Radio page."""
    from sizeup.cad.store import CadError
    tx(cad, "p1", "car 12 respond to 100 example avenue", channel="PD Dispatch", talkgroup=9211, system="mrrs",
       service="police", talker="")
    with pytest.raises(CadError):
        cad.stt_fix.get("p1")
    with pytest.raises(CadError):
        cad.stt_fix.save("p1", {"corrected": "Car 12 respond to 100 Example Ave"})
    with sourcekeys.request_context(Identity(email=CASEY)):
        cad.stt_fix.save("p1", {"corrected": "Car 12 respond to 100 Example Ave", "reference": True})
        assert cad.stt_fix.get("p1")["correction"]["domain"] == "police"
        assert cad.stt_fix.counts()["n"] == 1
    assert cad.stt_fix.counts()["n"] == 0             # not counted (nor exported) without a sign-in
    assert cad.stt_fix.export_rows({}) == []


def test_export_follows_428s_contract(cad):
    tx(cad, "c1", "engine 60 respond to 4 56 example avenue", alt_text="engine 60 respond to 456 example avenue",
       alt_model="ggml-large-v3-turbo.bin", start=1_800_000_000.0)
    tx(cad, "c2", "rescue 40 in quarters", start=1_800_000_100.0, channel="FG1", talkgroup=None, system="analog")
    tx(cad, "c3", "not corrected", start=1_800_000_200.0)
    cad.stt_fix.save("c1", {"corrected": "Engine 60 respond to 456 Example Ave", "reference": True, "operator": "JD"})
    cad.stt_fix.save("c2", {"corrected": "Rescue 40 in quarters", "operator": "JD"})
    data, n, more = cad.stt_fix.export_jsonl({})
    lines = [json.loads(x) for x in data.decode().splitlines()]
    assert n == 2 and more == {} and [x["clip_id"] for x in lines] == ["c1", "c2"]
    for x in lines:
        assert set(x) == CONTRACT | EXTENSIONS
        assert set(x["correction"]) == {"text", "checked", "by", "at", "based_on"}
    assert [(x["system"], x["service"]) for x in lines] == [("westchester", "fire"), ("analog", "fire")]
    a = lines[0]
    assert a["asr"] == {"aws-transcribe": "engine 60 respond to 4 56 example avenue",
                        "ggml-large-v3-turbo.bin": "engine 60 respond to 456 example avenue"}
    assert a["correction"]["checked"] is True and a["correction"]["by"] == "JD" and a["talkgroup"] == 307
    assert a["fixed_text"] == a["asr"]["aws-transcribe"] and a["talker"] == "60 Control"
    assert lines[1]["talkgroup"] is None and lines[1]["correction"]["checked"] is False
    assert [x["clip_id"] for x in cad.stt_fix.export_rows({"reference": ["1"]})] == ["c1"]
    assert [x["clip_id"] for x in cad.stt_fix.export_rows({"since": ["1800000050"]})] == ["c2"]
    assert [x["clip_id"] for x in cad.stt_fix.export_rows({"channel": ["FG1"]})] == ["c2"]
    assert sttfix.record({"id": "x", "text": "", "corrected": None})["correction"] is None   # #428: null when none


def test_admin_only_api(tmp_path):
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    tx(api.cad, "c1", "engine 60 en route")
    assert needs("/api/cad/stt/correction/c1") == "admin"
    assert {"/api/cad/stt/corrections", "/api/cad/stt/corrections.jsonl"} <= set(ADMIN_READS)
    body = json.dumps({"corrected": "Engine 60 en route", "reference": True}).encode()
    assert api.post("/api/cad/stt/correction/c1", body, Identity(email=JORDAN))[0] == 403
    assert api.get("/api/cad/stt/correction/c1", "", Identity(email=JORDAN))[0] == 403
    assert api.get("/api/cad/stt/corrections", "", Identity(email=JORDAN))[0] == 403
    assert api.get("/api/cad/stt/corrections.jsonl", "", Identity(email=JORDAN))[0] == 403
    code, raw, _ = api.post("/api/cad/stt/correction/c1", body, Identity(email=CASEY))
    assert code == 200 and json.loads(raw)["correction"]["by"].startswith("Casey")
    assert json.loads(api.get("/api/cad/stt/corrections", "", Identity(email=CASEY))[1])["reference"] == 1
    out = api.get("/api/cad/stt/corrections.jsonl", "reference=1", Identity(email=CASEY))
    assert out[0] == 200 and out[2].startswith("application/x-ndjson") and out[3]["Cache-Control"] == "no-store"
    line = json.loads(out[1].decode().splitlines()[0])
    assert line["clip_id"] == "c1" and line["correction"]["by"] == "Casey"      # the name: never the sign-in email
    assert api.get("/api/cad/stt/correction/nope", "", Identity(email=CASEY))[0] == 400
    acts = api.cad.activity.rows(CASEY, "search", None, 10)
    assert any("transcript corrections: 1" in (a.get("summary") or "") for a in acts)


def test_the_editor_builds_no_markup_from_text():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "stt-fix.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js
    assert "/api/cad/stt/correction/" in js and '"/api/cad/stt/corrections"' in js
    cad_js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "cad.js").read_text()
    assert "SttFix.open(t)" in cad_js


def test_an_unnamed_admin_is_never_recorded_by_email(cad):
    """An admin without a name yet signs in as their bare email: the correction keeps no name rather than the email,
    in the row, the dialog's answer and the export."""
    tx(cad, "c1", "engine 60 en route")
    out = cad.stt_fix.save("c1", {"corrected": "Engine 60 en route", "operator": "new.admin@example.org"})
    assert out["correction"]["by"] == ""
    assert cad.store.one('SELECT "by" FROM stt_corrections')["by"] == ""
    cad.stt_fix.save("c1", {"corrected": "Engine 60 en route", "operator": "Pat <pat@example.org>"})
    assert cad.stt_fix.get("c1")["correction"]["by"] == "Pat"
    with cad.store.tx() as db:                     # a row written before this rule: still never shown or exported
        db.execute('UPDATE stt_corrections SET "by" = ? WHERE traffic_id = ?', ("old.admin@example.org", "c1"))
    assert cad.stt_fix.get("c1")["correction"]["by"] == ""
    assert cad.stt_fix.export_rows({})[0]["correction"]["by"] == ""
    assert sttfix.name_only("Sam Sample") == "Sam Sample" and sttfix.name_only("x@example.org") == ""


def test_an_engine_that_heard_nothing_is_exported_as_empty(cad):
    tx(cad, "c1", "", model="aws-transcribe", alt_text="engine 60 en route", alt_model="ggml-large-v3-turbo.bin")
    cad.stt_fix.save("c1", {"corrected": "Engine 60 en route"})
    assert cad.stt_fix.export_rows({})[0]["asr"] == {"aws-transcribe": "", "ggml-large-v3-turbo.bin": "engine 60 en route"}


@pytest.mark.parametrize("service, system, domain", [
    ("fire", "westchester", "fire"), ("police", "mrrs", "police"), ("ems", "westchester", "ems"), ("air", "airband", "atc"),
    ("transit", "mrrs", "other"), ("", "westchester", "other"), ("", "analog", "other"), ("other", "mrrs", "other"),
])
def test_domain_of_a_rows_service(service, system, domain):
    assert sttfix.domain_of({"service": service, "system": system}) == domain


# -- the rules for a table in cad.sqlite (docs/cloud.md "Writing a migration", #501, #509) -------------------------------

def test_a_start_with_the_table_there_writes_nothing(tmp_path):
    """Created IF NOT EXISTS, once: no backfill, no rewrite. A start on a database that has the table (and corrections
    in it) writes no page for it, and opens no second connection to cad.sqlite."""
    import sqlite3
    from tests.test_startup_writes import wal_frames_written
    cad = make_cad(tmp_path)
    for i in range(200):
        tx(cad, f"c{i}", f"made-up transmission {i}")
        cad.stt_fix.save(f"c{i}", {"corrected": f"Made-up transmission {i}", "reference": i % 2 == 0})
    path = tmp_path / "cad.sqlite"
    opened, real = [], sqlite3.connect

    def connect(*a, **kw):
        opened.append(a[0] if a else kw.get("database"))
        return real(*a, **kw)
    sqlite3.connect = connect
    try:
        written = wal_frames_written([path], lambda: sttfix.SttCorrections(cad))
    finally:
        sqlite3.connect = real
    assert written == {"cad": 0}
    assert [str(o) for o in opened if "mode=ro" not in str(o)] == [str(path)]     # only the test's own counting reader
    assert cad.stt_fix.counts()["n"] == 200


def test_a_fenced_task_writes_no_correction_and_says_so(tmp_path):
    """Not the leader (sizeup/fence.py): a save or a delete raises FencedWrite out of the API (the web server answers
    503 + Retry-After: nothing written, nothing audited, never a 200); reading and the export go on (the export's
    activity row is bookkeeping: skipped)."""
    from sizeup.fence import FencedWrite
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    store = api.cad.store
    tx(api.cad, "c1", "engine 60 en route")
    tx(api.cad, "c2", "ladder 30 on location")
    me = Identity(email=CASEY)
    assert api.post("/api/cad/stt/correction/c1", json.dumps({"corrected": "Engine 60 en route"}).encode(), me)[0] == 200
    audits = store.one("SELECT COUNT(*) AS n FROM settings_audit WHERE section = ?", (sttfix.SECTION,))["n"]
    acts = len(api.cad.activity.rows(CASEY, None, None, 100))
    rev, ours = store.rev, [False]
    store.may_write = lambda: ours[0]
    with pytest.raises(FencedWrite):
        api.post("/api/cad/stt/correction/c2", json.dumps({"corrected": "Ladder 30 on location"}).encode(), me)
    with pytest.raises(FencedWrite):
        api.post("/api/cad/stt/correction/c1", json.dumps({"delete": True}).encode(), me)
    assert not store.db.in_transaction and store.rev == rev
    assert json.loads(api.get("/api/cad/stt/correction/c2", "", me)[1])["correction"] is None
    assert json.loads(api.get("/api/cad/stt/correction/c1", "", me)[1])["correction"]["corrected"] == "Engine 60 en route"
    out = api.get("/api/cad/stt/corrections.jsonl", "", me)
    assert out[0] == 200 and len(out[1].decode().splitlines()) == 1
    assert store.one("SELECT COUNT(*) AS n FROM settings_audit WHERE section = ?", (sttfix.SECTION,))["n"] == audits
    assert len(api.cad.activity.rows(CASEY, None, None, 100)) == acts
    ours[0] = True                                                             # the leader again: it writes
    assert api.post("/api/cad/stt/correction/c2", json.dumps({"corrected": "Ladder 30 on location"}).encode(), me)[0] == 200
    assert api.cad.stt_fix.counts()["n"] == 2


# -- the review's fixes ---------------------------------------------------------------------------------------------------

def test_the_audit_names_the_admin_as_every_section_does(tmp_path):
    """The Settings audit log's operator is the signed-in admin, "Name <email>" (what the API puts in the body), as for
    every other section; the correction's own `by` and the export keep the name only."""
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    tx(api.cad, "c1", "engine 60 en route")
    me = Identity(email=CASEY)
    assert api.post("/api/cad/stt/correction/c1", json.dumps({"corrected": "Engine 60 en route"}).encode(), me)[0] == 200
    assert api.post("/api/cad/stt/correction/c1", json.dumps({"delete": True}).encode(), me)[0] == 200
    standing = api.cad.settings.standing(CASEY)["operator"]
    ops = {r["operator"] for r in api.cad.settings.audit_log(sttfix.SECTION)}
    assert ops == {standing} and CASEY in standing
    api.cad.stt_fix.save("c1", {"corrected": "Engine 60 en route", "operator": standing})
    assert api.cad.stt_fix.get("c1")["correction"]["by"] == sttfix.name_only(standing) and "@" not in sttfix.name_only(standing)


def test_another_systems_corrections_are_redacted_in_the_audit_for_a_reader_without_a_sign_in(cad):
    """The Settings audit is read more widely than police transcripts are: without a verified sign-in a correction of
    another system's transmission is only "a correction changed" (no clip id, domain, counts), like a restricted
    premise note's rows; ours are shown as they are; behind a sign-in, all of it."""
    tx(cad, "c1", "engine 60 en route")
    tx(cad, "p1", "car 12 respond to 100 example avenue", channel="PD Dispatch", talkgroup=9211, system="mrrs",
       service="police", talker="")
    tx(cad, "p2", "car 14 on location", channel="PD Dispatch", talkgroup=9211, system="mrrs", service="police", talker="")
    cad.stt_fix.save("c1", {"corrected": "Engine 60 en route", "operator": "JD"})
    with sourcekeys.request_context(Identity(email=CASEY)):
        cad.stt_fix.save("p1", {"corrected": "Car 12 respond to 100 Example Ave", "reference": True, "operator": "JD",
                                "tags": [{"kind": "address", "start": 18, "end": 33}]})
        cad.stt_fix.save("p2", {"corrected": "Car 14 on location", "operator": "JD"})
        cad.stt_fix.save("p2", {"delete": True, "operator": "JD"})
        full = cad.settings_audit_log({"section": [sttfix.SECTION]})["audit"]
    assert {r["key"] for r in full} == {"c1", "p1", "p2"} and json.loads(full[-2]["new"])["context"] is True
    with cad.store.tx() as db:
        db.execute("DELETE FROM radio_traffic WHERE id = 'p2'")              # gone since: its audit rows say whose it was
    for q in ({"section": [sttfix.SECTION]}, {}):
        seen = [r for r in cad.settings_audit_log(q)["audit"] if r["section"] == sttfix.SECTION]
        assert len(seen) == 4
        ours = [r for r in seen if r["key"] == "c1"]
        assert len(ours) == 1 and json.loads(ours[0]["new"])["words"] == 4
        hidden = [r for r in seen if r["key"] != "c1"]
        assert {r["key"] for r in hidden} == {sttfix.REDACTED}
        assert {r["new"] for r in hidden} <= {"", '{"changed":true}'} and {r["old"] for r in hidden} <= {"", '{"changed":true}'}
        blob = json.dumps(hidden)
        for w in ("p1", "p2", "police", "address", "words", "reference", "hash"):
            assert w not in blob
        assert all(r["operator"] == "JD" and r["at"] for r in hidden)     # who and when stay
    with cad.store.tx() as db:                                              # a row whose transmission can't be told: hidden
        db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (1, 'JD', ?, 'gone-1', '', ?)",
                   (sttfix.SECTION, json.dumps({"words": 3, "domain": "other"})))
    assert "gone-1" not in json.dumps(cad.settings_audit_log({})["audit"])


def test_a_correction_makes_its_clip_a_keeper(tmp_path):
    """Retention: a corrected transmission is never pruned from the radio log and its audio is a record (kept as a clip
    on a CAD event is), while "Keep corrected clips" is on (the default). Off: both go by their channel's retention
    and the correction goes with its transmission. An explicit delete makes the clip an ordinary one at once."""
    from sizeup.cad.comms import KEEP_RECORD_S, KEEP_UNLINKED_S

    class Clock:
        t = 1_800_000_000.0

        def __call__(self):
            return self.t
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    for tid in ("c1", "c2", "c3"):
        tx(cad, tid, f"engine 60 made-up message {tid}", start=clock.t)
    cad.stt_fix.save("c1", {"corrected": "Engine 60 made-up message", "reference": True})
    cad.stt_fix.save("c2", {"corrected": "Engine 60 made-up message"})
    assert cad.stt_fix.counts() | {"last": 0, "domains": 0} == {"n": 2, "reference": 1, "keep": True, "kept": 2,
                                                                 "last": 0, "domains": 0}
    assert cad.comms.kept_for_events(["c1", "c2", "c3"]) == {"c1", "c2"}       # the audio prune's records
    clock.t += max(KEEP_UNLINKED_S, KEEP_RECORD_S) + 86400
    assert cad.comms.prune() == 1                                              # c3 only
    assert {r["id"] for r in cad.store.q("SELECT id FROM radio_traffic")} == {"c1", "c2"}
    cad.stt_fix.save("c2", {"delete": True})                                   # explicit: an ordinary clip again
    assert cad.comms.kept_for_events(["c1", "c2"]) == {"c1"}
    assert cad.comms.prune() == 1 and cad.store.one("SELECT COUNT(*) AS n FROM radio_traffic")["n"] == 1
    out = cad.settings_post("stt_corrections", {"action": "set", "key": "keep", "value": False, "operator": "JD"})
    assert out["ok"] and out["stt_corrections"]["keep"] is False and out["stt_corrections"]["kept"] == 0
    assert cad.stt_fix.counts()["n"] == 1 and cad.comms.kept_for_events(["c1"]) == set()
    last = cad.settings.audit_log("system")[0]
    assert (last["key"], last["old"], last["new"]) == ("stt_corrections/keep", "on", "off")
    assert cad.settings_post("stt_corrections", {"action": "reset", "key": "keep"})["stt_corrections"]["keep"] is True
    assert cad.comms.prune() == 0                                              # on again: kept
    cad.settings_post("stt_corrections", {"action": "set", "key": "keep", "value": False})
    assert cad.comms.prune() == 1                                              # off: by its channel's retention ...
    assert cad.store.one("SELECT COUNT(*) AS n FROM stt_corrections")["n"] == 0    # ... and the correction with it
    from sizeup.cad.store import CadError
    for bad in ({"action": "set", "key": "keep", "value": "no"}, {"action": "set", "key": "other", "value": True},
                {"action": "toggle", "key": "keep"}):
        with pytest.raises(CadError):
            cad.settings_post("stt_corrections", bad)


def test_the_keep_setting_is_an_admins(tmp_path):
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    body = json.dumps({"action": "set", "key": "keep", "value": False}).encode()
    assert needs("/api/cad/settings/stt_corrections") == "admin"
    assert api.post("/api/cad/settings/stt_corrections", body, Identity(email=JORDAN))[0] == 403
    assert api.post("/api/cad/settings/stt_corrections", body, Identity(email=CASEY))[0] == 200
    assert json.loads(api.get("/api/cad/stt/corrections", "", Identity(email=CASEY))[1])["keep"] is False
    exported = api.cad.settings_export()
    assert exported["overrides"]["stt_corrections"] == {"keep": False}        # and it travels with a settings backup


def test_the_export_says_when_it_stopped_at_the_limit(tmp_path):
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    for i in range(5):
        tx(api.cad, f"c{i}", f"made-up message {i}", start=1_800_000_000.0 + 10 * i)
        api.cad.stt_fix.save(f"c{i}", {"corrected": f"Made-up message {i}"})
    data, n, more = api.cad.stt_fix.export_jsonl({}, limit=3)
    lines = [json.loads(x) for x in data.decode().splitlines()]
    assert n == 3 and [x.get("clip_id") for x in lines] == ["c0", "c1", "c2", None]
    assert lines[-1] == {"truncated": True, "limit": 3, "next_since": 1_800_000_030.0}
    assert more == {"X-Sizeup-Truncated": "3", "X-Sizeup-Next-Since": "1800000030.0"}
    data, n, more = api.cad.stt_fix.export_jsonl({"since": [more["X-Sizeup-Next-Since"]]}, limit=3)
    assert n == 2 and more == {} and [json.loads(x)["clip_id"] for x in data.decode().splitlines()] == ["c3", "c4"]
    old = sttfix.EXPORT_MAX
    sttfix.EXPORT_MAX = 2
    try:
        out = api.get("/api/cad/stt/corrections.jsonl", "", Identity(email=CASEY))
    finally:
        sttfix.EXPORT_MAX = old
    assert out[0] == 200 and out[3]["X-Sizeup-Truncated"] == "2" and out[1].decode().count("\n") == 3
    assert any("(truncated)" in (a.get("summary") or "") for a in api.cad.activity.rows(CASEY, "search", None, 10))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import stt_eval                                                             # the evaluation skips the last line
    f = tmp_path / "x.jsonl"
    f.write_bytes(out[1])
    assert len(stt_eval.from_jsonl(f, True, None, None)) == 2


def test_the_editor_starts_from_the_rows_engine_and_the_correction_says_which(cad):
    tx(cad, "c1", "engine 60 en root", model="aws-transcribe", alt_text="engine 60 en route", alt_model="ggml-large-v3-turbo.bin")
    tx(cad, "c2", "", model="aws-transcribe", alt_text="engine 61 en route", alt_model="ggml-large-v3-turbo.bin")
    assert cad.stt_fix.get("c1")["prefill"] == {"text": "engine 60 en root", "engine": "aws-transcribe"}
    assert cad.stt_fix.get("c2")["prefill"] == {"text": "engine 61 en route", "engine": "ggml-large-v3-turbo.bin"}
    out = cad.stt_fix.save("c1", {"corrected": "Engine 60 en route", "based_on": "aws-transcribe"})
    assert out["correction"]["based_on"] == "aws-transcribe"
    assert cad.stt_fix.save("c1", {"corrected": "Engine 60, en route"})["correction"]["based_on"] == "aws-transcribe"   # kept
    assert cad.stt_fix.export_rows({})[0]["correction"]["based_on"] == "aws-transcribe"
    assert json.loads(cad.settings.audit_log(sttfix.SECTION)[0]["new"])["based_on"] == "aws-transcribe"
    from sizeup.cad.store import CadError
    with pytest.raises(CadError):
        cad.stt_fix.save("c2", {"corrected": "Engine 61 en route", "based_on": "some-other-engine"})


def test_a_table_from_before_based_on_gets_the_column_once(tmp_path):
    cad = make_cad(tmp_path)
    tx(cad, "c1", "engine 60 en route")
    with cad.store.lock:
        cad.store.db.executescript("DROP TABLE stt_corrections; CREATE TABLE stt_corrections (traffic_id TEXT PRIMARY KEY "
                                   "REFERENCES radio_traffic(id) ON DELETE CASCADE, original_hash TEXT NOT NULL DEFAULT '', "
                                   "corrected TEXT NOT NULL, domain TEXT NOT NULL DEFAULT 'other', tags TEXT NOT NULL DEFAULT '[]', "
                                   '"by" TEXT NOT NULL DEFAULT \'\', at REAL NOT NULL, reference INTEGER NOT NULL DEFAULT 0); '
                                   "INSERT INTO stt_corrections (traffic_id, corrected, at) VALUES ('c1', 'Engine 60 en route', 1)")
    sttfix.SttCorrections(cad)
    assert cad.stt_fix.get("c1")["correction"]["based_on"] == ""
    from tests.test_startup_writes import wal_frames_written
    assert wal_frames_written([tmp_path / "cad.sqlite"], lambda: sttfix.SttCorrections(cad)) == {"cad": 0}


# -- the editor's tags follow their words by offset (stt-fix.js refit) -----------------------------------------------------

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = __import__("shutil").which("node") or __import__("shutil").which("node", path="/opt/homebrew/bin:/usr/local/bin")
REFIT_HARNESS = r"""
const fs = require("fs");
const window = {};
new Function("window", fs.readFileSync(process.argv[2], "utf8"))(window);
const refit = window.SttFix.refit;
const cases = JSON.parse(process.argv[3]);
const out = cases.map(([before, tags, edits]) => {
  let text = before;
  let gs = tags.map(([kind, start, end]) => ({ kind, start, end, text: before.slice(start, end) }));
  for (const next of edits) { gs = refit(gs, next, text); text = next; }       // one input event each
  return gs.map((g) => [g.kind, g.start, g.end, text.slice(g.start, g.end), g.text]);
});
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_tags_follow_their_words_by_offset_through_edits(tmp_path):
    import subprocess
    said = "Engine 61 to 61 Example Ave"
    house = ["address", 13, 15]                                   # the SECOND 61: the house number
    cases = [
        (said, [house], ["P" + said, "PD" + said, "PD " + said]),                       # typing in front
        (said, [house], [said[:7] + said[9:]]),                                         # the first 61 deleted
        (said, [house], [said + " rear"]),                                              # typing behind
        (said, [house], [said[:14] + said[15:], said[:14] + "3" + said[15:]]),          # 61 -> 6 -> 63: its own digit
        (said, [house], [said[:13] + said[16:]]),                                       # its words deleted: dropped
        (said, [["address", 13, 27]], [said + "n", said + "nue"]),                      # typed on at its end: it grows
        (said, [["callsign", 0, 9], house], ["Engine 61 responding to 61 Example Ave"]),   # a word put in between
        (said, [house], ["Ladder 30 to 61 Example Ave"]),                               # the words in front replaced
    ]
    h = tmp_path / "refit.js"
    h.write_text(REFIT_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "stt-fix.js"), json.dumps(cases)], capture_output=True,
                                    text=True, check=True, timeout=30).stdout)
    assert out[0] == [["address", 16, 18, "61", "61"]]            # still the house number, not "Engine 61"'s
    assert out[1] == [["address", 11, 13, "61", "61"]]
    assert out[2] == [["address", 13, 15, "61", "61"]]
    assert out[3] == [["address", 13, 15, "63", "63"]]
    assert out[4] == []
    assert out[5] == [["address", 13, 30, "61 Example Avenue", "61 Example Avenue"]]
    assert out[6] == [["callsign", 0, 9, "Engine 61", "Engine 61"], ["address", 24, 26, "61", "61"]]
    assert out[7] == [["address", 13, 15, "61", "61"]]
    assert "indexOf" not in (STATIC / "stt-fix.js").read_text()


def test_the_editor_script_sends_the_engine_and_shows_what_is_kept():
    src = (STATIC / "stt-fix.js").read_text()
    assert "d.prefill" in src and "based_on: first.engine" in src
    assert '"/api/cad/settings/stt_corrections"' in src and "corrected clips kept" in src
