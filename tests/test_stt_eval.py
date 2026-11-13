"""tools/stt_eval.py (radio intel plan, PR 7): WER, key-token recall, house-number / street / right-block accuracy and
prompt insertions per engine, against hand-corrected references, from a cad.sqlite opened read-only or #428's JSONL.
Synthetic transcripts only (Example Ave, Sample St, made-up units)."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from sizeup import sourcekeys
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_stt_corrections import tx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import stt_eval  # noqa: E402

WHISPER = "ggml-large-v3-turbo.bin"


def test_words_and_wer():
    assert stt_eval.tokens("Respond to 456 Example Avenue") == stt_eval.tokens("respond to four five six example ave")
    assert stt_eval.wer("engine 60 en route", "Engine 60 en route") == (0, 5)
    assert stt_eval.wer("engine sixty route", "engine 60 en route")[0] == 1      # "sixty" is 60: only "en" missing
    assert stt_eval.wer("", "engine 60 en route") == (5, 5)                     # nothing heard: all deletions


def test_address_scores():
    assert stt_eval.score_address("respond to 456 example avenue", "456", ["example", "ave"]) == (True, True, True)
    assert stt_eval.score_address("respond to 420 example avenue", "456", ["example", "ave"]) == (False, True, True)
    assert stt_eval.score_address("respond to 4 5 6 sample avenue", "456", ["example", "ave"]) == (False, False, False)
    assert stt_eval.score_address("respond to 4 5 6 example avenue", "456", ["example", "ave"]) == (True, True, True)
    assert stt_eval.score_address("respond to 1456 example ave", "456", ["example", "ave"]) == (False, True, False)


def test_prompt_insertions():
    ph = [stt_eval.tokens("Willow Lane"), stt_eval.tokens("Example Ave")]
    assert stt_eval.prompt_insertions("check willow lane and example avenue", "check example ave", ph) == 1
    assert stt_eval.prompt_insertions("check example avenue", "check example ave", ph) == 0


@pytest.fixture
def restored(tmp_path):
    """A cad.sqlite with three corrections (two references), as a local restore would be."""
    cad = make_cad(tmp_path)
    tx(cad, "c1", "engine 60 respond to 456 example avenue", alt_text="engine 60 respond to 420 example avenue",
       alt_model=WHISPER, start=1_800_000_000.0)
    tx(cad, "c2", "car 12 check 300 sample street", alt_text="car 12 check 800 sample street willow lane",
       alt_model=WHISPER, start=1_800_090_000.0, channel="PD Dispatch", talkgroup=9211, system="mrrs", service="police")
    tx(cad, "c3", "ladder 31 available", start=1_800_100_000.0)
    with sourcekeys.request_context(Identity(email="admin@example.org")):
        said = "Engine 60 respond to 456 Example Ave"
        cad.stt_fix.save("c1", {"corrected": said, "reference": True,
                                "tags": [{"kind": "callsign", "start": 0, "end": 9}, {"kind": "address", "start": 21, "end": 36}]})
        cad.stt_fix.save("c2", {"corrected": "Car 12 check 300 Sample St", "reference": True})
        cad.stt_fix.save("c3", {"corrected": "Ladder 31 available"})
    cad.store.db.close()
    return tmp_path / "cad.sqlite", cad


def test_engines_compared_on_the_references(restored, tmp_path, capsys):
    db, _ = restored
    words = tmp_path / "prompt.txt"
    words.write_text("Willow Lane\nExample Ave\n")
    before = db.read_bytes()
    out = tmp_path / "eval.json"
    assert stt_eval.main(["--db", str(db), "--prompt-words", str(words), "--json", str(out)]) == 0
    assert db.read_bytes() == before                               # read-only
    res = json.loads(out.read_text())
    assert res["references"] == 2                                  # c3 is not a reference
    aws, wh = res["all"]["aws"], res["all"]["whisper"]
    assert aws["clips"] == wh["clips"] == 2 and res["paired"]["aws"]["clips"] == 2
    assert aws["wer"] == 0 and wh["wer"] > 0
    assert aws["house_number"] == 1 and wh["house_number"] == 0
    assert aws["street"] == wh["street"] == 1
    assert aws["right_block"] == 1 and wh["right_block"] == 0.5     # 420 for 456: right block; 800 for 300: not
    assert aws["key_recall"] == 1 and wh["key_recall"] < 1
    assert aws["prompt_insertion"] == 0 and wh["prompt_insertion"] == 0.5
    printed = capsys.readouterr().out
    for w in ("example", "sample", "willow", "c1", "ladder"):        # aggregates only
        assert w not in printed.lower()
    res = stt_eval.evaluate(stt_eval.from_db(db, True, "", None, None))
    assert res["references"] == 3 and res["all"]["aws"]["clips"] == 3
    assert stt_eval.evaluate(stt_eval.from_db(db, False, "police", None, None))["references"] == 1


def test_variants_split_by_time(restored):
    db, _ = restored
    refs = stt_eval.from_db(db, False, "", None, None)
    v = stt_eval.parse_variants(["base=..1800050000", "prompt=1800050000.."])
    res = stt_eval.evaluate(refs, v)
    assert set(res["all"]) == {"aws/base", "aws/prompt", "whisper/base", "whisper/prompt"}
    assert res["all"]["whisper/base"]["clips"] == res["all"]["whisper/prompt"]["clips"] == 1
    with pytest.raises(SystemExit):
        stt_eval.parse_variants(["nope"])


def test_from_the_jsonl_export(restored, tmp_path):
    _db, _ = restored
    cad = make_cad(tmp_path)                                       # the same file, opened again
    with sourcekeys.request_context(Identity(email="admin@example.org")):
        data, n = cad.stt_fix.export_jsonl({"reference": ["1"]})
    p = tmp_path / "stt-corrections.jsonl"
    p.write_bytes(data)
    refs = stt_eval.from_jsonl(p, False, None, None)
    assert n == len(refs) == 2 and all(set(r["hyps"]) == {"aws", "whisper"} for r in refs)
    res = stt_eval.evaluate(refs)
    assert res["all"]["aws"]["wer"] == 0 and res["all"]["whisper"]["house_number"] == 0


def test_the_db_is_opened_read_only(restored):
    db, _ = restored
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM stt_corrections")
    con.close()


@pytest.mark.parametrize("said, written", [
    ("four fifty six example avenue", ["456", "example", "ave"]),
    ("one hundred", ["100"]),
    ("sixty three", ["63"]),
    ("four hundred fifty six", ["456"]),
    ("one two three", ["123"]),
    ("route one oh one", ["route", "101"]),
    ("oh no", ["oh", "no"]),                                   # "oh" alone is a word
    ("first ave", ["1st", "ave"]),
    ("twenty first street", ["21st", "st"]),
    ("twelve thirty four", ["1234"]),
    ("4 5 6 sample st", ["456", "sample", "st"]),
])
def test_number_words(said, written):
    assert stt_eval.words(said) == written


def test_only_the_number_before_the_street_is_the_house_number():
    street = ["example", "ave"]
    # a unit's number elsewhere in the clip is never the house number
    assert stt_eval.score_address("engine 12 respond to 34 example ave", "12", street) == (False, True, None)
    assert stt_eval.score_address("engine 34 respond to 12 example ave", "12", street)[0] is True
    # a callsign's digits run into the house number after a unit word: its last digits are the house number
    assert stt_eval.score_address("rescue one four five six example avenue", "456", street) == (True, True, True)
    assert stt_eval.score_address("rescue one four five six example avenue", "1456", street)[0] is True
    assert stt_eval.score_address("respond to 1456 example ave", "456", street) == (False, True, False)
    # under 100 there is no block: not counted
    assert stt_eval.score_address("respond to 34 example ave", "36", street) == (False, True, None)
    assert stt_eval.split_address("100 Route 1") == ("100", ["route", "1"])
    assert stt_eval.split_address("100 Example Avenue") == ("100", ["example", "ave"])


def test_no_phrases_no_prompt_rates():
    refs = [{"start": 1.0, "ref": "Engine 60 en route", "tags": [], "domain": "fire", "hyps": {"aws": "engine 60 en route"}}]
    r = stt_eval.evaluate(refs, phrases=[])["all"]["aws"]
    assert r["prompt_insertion"] is None and r["insertions_per_100w"] is None
    r = stt_eval.evaluate(refs, phrases=["willow lane"])["all"]["aws"]
    assert r["prompt_insertion"] == 0


def test_an_engine_that_heard_nothing_scores_as_deletions(tmp_path):
    cad = make_cad(tmp_path)
    tx(cad, "c1", "", model="aws-transcribe", alt_text="engine 60 en route", alt_model=WHISPER)
    cad.stt_fix.save("c1", {"corrected": "Engine 60 en route", "reference": True})
    cad.store.db.close()
    res = stt_eval.evaluate(stt_eval.from_db(tmp_path / "cad.sqlite", False, "", None, None))
    assert res["all"]["aws"]["wer"] == 1.0 and res["all"]["whisper"]["wer"] == 0
    assert res["paired"]["aws"]["clips"] == 1


def test_json_never_inside_the_repo(restored):
    db, _ = restored
    with pytest.raises(SystemExit):
        stt_eval.main(["--db", str(db), "--json", str(stt_eval.ROOT / "x.stt-eval.json")])
    assert not (stt_eval.ROOT / "x.stt-eval.json").exists()
