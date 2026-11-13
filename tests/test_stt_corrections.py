"""Hand-corrected transcripts (sizeup/cad/sttfix.py, radio intel plan PR 7, #428's corrections side): the table, the
admin API, the audit (never the words), retention with the transmission, another system's rows behind a sign-in, and
#428's JSONL export contract. Synthetic transcripts only (Example Ave, made-up units)."""

import json

import pytest

from sizeup import sourcekeys
from sizeup.cad import sttfix
from sizeup.cad.api import ADMIN_READS, CadApi, needs
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, people

CONTRACT = {"clip_id", "channel", "talkgroup", "talker", "start", "end", "asr", "fixed_text", "correction"}


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
    assert cols == {"traffic_id", "original_hash", "corrected", "domain", "tags", "by", "at", "reference"}
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
    for w in ("ladder", "example", "location"):
        assert w not in blob
    first = json.loads(rows[-1]["new"])
    assert first["words"] == 7 and first["tags"] == {"callsign": 1} and first["hash"] == sttfix.text_hash(said)
    assert rows[0]["new"] in (None, "", "null")          # the delete
    assert cad.stt_fix.get("c1")["correction"] is None


def test_a_correction_goes_with_its_transmission(cad):
    """Retention follows the transcript's: the radio log's prune deletes the row, the correction goes with it."""
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
    tx(cad, "p1", "car 266 respond to 100 example avenue", channel="PD Dispatch", talkgroup=9211, system="mrrs",
       service="police", talker="")
    with pytest.raises(CadError):
        cad.stt_fix.get("p1")
    with pytest.raises(CadError):
        cad.stt_fix.save("p1", {"corrected": "Car 266 respond to 100 Example Ave"})
    with sourcekeys.request_context(Identity(email=COREY)):
        cad.stt_fix.save("p1", {"corrected": "Car 266 respond to 100 Example Ave", "reference": True})
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
    data, n = cad.stt_fix.export_jsonl({})
    lines = [json.loads(x) for x in data.decode().splitlines()]
    assert n == 2 and [x["clip_id"] for x in lines] == ["c1", "c2"]
    for x in lines:
        assert set(x) == CONTRACT
        assert set(x["correction"]) == {"text", "checked", "by", "at"}
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
    assert api.post("/api/cad/stt/correction/c1", body, Identity(email=JULIAN))[0] == 403
    assert api.get("/api/cad/stt/correction/c1", "", Identity(email=JULIAN))[0] == 403
    assert api.get("/api/cad/stt/corrections", "", Identity(email=JULIAN))[0] == 403
    assert api.get("/api/cad/stt/corrections.jsonl", "", Identity(email=JULIAN))[0] == 403
    code, raw, _ = api.post("/api/cad/stt/correction/c1", body, Identity(email=COREY))
    assert code == 200 and json.loads(raw)["correction"]["by"].startswith("Corey")
    assert json.loads(api.get("/api/cad/stt/corrections", "", Identity(email=COREY))[1])["reference"] == 1
    out = api.get("/api/cad/stt/corrections.jsonl", "reference=1", Identity(email=COREY))
    assert out[0] == 200 and out[2].startswith("application/x-ndjson") and out[3]["Cache-Control"] == "no-store"
    line = json.loads(out[1].decode().splitlines()[0])
    assert line["clip_id"] == "c1" and line["correction"]["by"] == "Corey"      # the name: never the sign-in email
    assert api.get("/api/cad/stt/correction/nope", "", Identity(email=COREY))[0] == 400
    acts = api.cad.activity.rows(COREY, "search", None, 10)
    assert any("transcript corrections: 1" in (a.get("summary") or "") for a in acts)


def test_the_editor_builds_no_markup_from_text():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "stt-fix.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js
    assert "/api/cad/stt/correction/" in js and '"/api/cad/stt/corrections"' in js
    cad_js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "cad.js").read_text()
    assert "SttFix.open(t)" in cad_js
