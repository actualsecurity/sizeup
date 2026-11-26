"""Post-ASR vocabulary correction (#879 part 3, sizeup/stt_correct.py): unit IDs folded to digits, names put back by
spelling and sound, the safety rules, the record in stt_runs, the Radio rows' shown text and the offline measure.
Every street, town, place and unit here is made up."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import stt_correct, stt_terms, stt_time, sttruns
from sizeup.config import ConfigError, load
from sizeup.dmetaphone import double_metaphone
from sizeup.stt_correct import Corrector, Vocab
from sizeup.tz import TZ

ROOT = Path(__file__).resolve().parent.parent

# a made-up channel list (as said, the way the private lists are written) and a made-up places list
FIRE = """# county_fire: made up for the tests
# units as said
Engine ninety seven
Engine ninety eight
Ladder ninety three
Tower Ladder eight
Rescue eighty eight
ninety Control
Fire four
Battalion forty
# phrasing
Fireground
working fire
smoke condition
second alarm
# ten-codes as said
ten four
ten seventy five
"""
PLACES = """# places: made up
# towns and hamlets
Zephyrton
Quillbrook
Tarrowmede
# streets: Zephyrton, Quillbrook (busiest first, as said)
Sampleworth Street
Marrowind Avenue
Larkspire Lane
Hollyridge Lane
Glenvarrow Road
Brindlecomb Avenue
Westerhaven Avenue
# streets: Otherville
Kestlewood Street
Brindlecombe Avenue
Mapleford Drive
# public landmarks
Quillbrook Commons
Tarrowmede Common
Eastbrindle
Zephyrton Fire Department
"""
PD = """# pd_dispatch: made up
Halvorsen Plaza
ten four
"""
TOWNS = ("Zephyrton", "Quillbrook")


def vocab(towns=TOWNS) -> Vocab:
    lists = {"county_fire": stt_terms.parse(FIRE), "places": stt_terms.parse(PLACES)}
    ts = stt_terms.build_sets(lists)["county_fire"]
    secs = {**stt_terms.sections(PLACES), **stt_terms.sections(FIRE)}
    return Vocab(ts.terms, secs, towns, ts.vocab_v, names=set(stt_terms.sections(PLACES)))


V = vocab()


def fix(text: str, v: Vocab = V) -> tuple[str, list[tuple[str, str, str]]]:
    out, fixes = v.correct(text)
    return out, [(f.heard, f.to, f.rule) for f in fixes]


# -- Double Metaphone ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("word, keys", [
    ("Smith", ("SM0", "XMT")), ("Schmidt", ("XMT", "SMT")), ("Xavier", ("SF", "SFR")), ("Cagney", ("KKN", "KKN")),
    ("Jose", ("HS", "HS")), ("Caesar", ("SSR", "SSR")), ("Thomas", ("TMS", "TMS")), ("Knight", ("NT", "NT")),
    ("Philips", ("FLPS", "FLPS")), ("laugh", ("LF", "LF")), ("Michael", ("MKL", "MXL")), ("Edge", ("AJ", "AJ")),
])
def test_double_metaphone_keys(word, keys):
    assert double_metaphone(word) == keys


def test_sound_alikes_share_a_key_and_the_whole_word_can_be_kept():
    assert double_metaphone("ladder")[0] == double_metaphone("latter")[0]
    assert double_metaphone("Wasserman")[1] == double_metaphone("Vasserman")[0]
    assert double_metaphone("Brindlecomb", None)[0] == "PRNTLKMP" and len(double_metaphone("Brindlecomb")[0]) == 4
    assert double_metaphone("") == ("", "") and double_metaphone("1234") == ("", "")


# -- unit IDs: spoken numbers folded, into the list's own terms only --------------------------------------------------

@pytest.mark.parametrize("heard, shown", [
    ("engine ninety seven responding", "Engine 97 responding"),
    ("Ladder ninety three on location", "Ladder 93 on location"),
    ("tower ladder eight to ninety control", "Tower Ladder 8 to 90 Control"),
    ("rescue eighty-eight, copy", "Rescue 88, copy"),
    ("Engine ninety seven, Engine ninety eight, respond", "Engine 97, Engine 98, respond"),
])
def test_spoken_unit_numbers_fold_to_the_lists_unit(heard, shown):
    assert fix(heard)[0] == shown


@pytest.mark.parametrize("heard", [
    "engine ninety nine responding",          # not a unit of the list
    "engine ninety seven one responding",     # the number words go on: never "Engine 97 one"
    "fire four is out",                       # "Fire N", "Battalion N": never folded (transcribe.NO_RESPELL)
    "battalion forty on scene",
    "Engine 97 responding",                   # digits already
    "engine oh seven",
])
def test_a_unit_fold_needs_the_whole_number_and_a_listed_unit(heard):
    assert fix(heard) == (heard, [])


def test_ten_codes_fold_at_a_clause_end_only():
    assert fix("ten four.") == ("10-4.", [("ten four", "10-4", "ten")])
    assert fix("copy, ten seventy five") == ("copy, 10-75", [("ten seventy five", "10-75", "ten")])
    assert fix("ten four the address")[1] == []                     # (a house number, maybe)


# -- names: spelling and sound, with the suffix heard --------------------------------------------------------------------

@pytest.mark.parametrize("heard, shown, rule", [
    ("working fire at samplewerth street", "working fire at Sampleworth Street", "sound"),
    ("respond to Holly Ridge Lane", "respond to Hollyridge Lane", "spell"),
    ("glen varrow road", "Glenvarrow Road", "spell"),
    ("units to Marrowand Avenue", "units to Marrowind Avenue", "sound"),
    ("respond to Quilbrook", "respond to Quillbrook", "spell"),
    ("Western Haven Ave", "Westerhaven Avenue", "spell"),
    ("going to Tarrow Mead", "going to Tarrowmede", "sound"),
])
def test_a_misheard_name_is_put_back(heard, shown, rule):
    out, fixes = fix(heard)
    assert out == shown and fixes[0][2] == rule


@pytest.mark.parametrize("heard", [
    "Sampleworth Street",                 # a term already
    "sampleworth street",                 # (case: not a correction)
    "Sampleworth St",                     # (the suffix abbreviated)
    "smoke conditions on two",            # a phrase of the list is never a target (Title Case names only)
    "the patient is fine",
    "Sampleworth Road",                   # another suffix: never moved to the street of the list
    "samplewerth",                        # a street needs its suffix heard
])
def test_left_alone(heard):
    assert fix(heard) == (heard, [])


@pytest.mark.parametrize("heard", [
    "Tarrowmede coming",                  # word for word, each must be alike: "coming" is no "Common"
    "West Brindle",                       # another first letter: only when spelled nearly the same
    "fire department",                    # never a word put in ("Zephyrton Fire Department")
    "fire ground",                        # the channel's own list names are no targets: places only
    "respond to Quillbrook J",            # a one-letter word is never taken in
])
def test_more_left_alone(heard):
    assert fix(heard) == (heard, [])


def test_a_possessive_stays_as_heard():
    assert fix("Quillbrook's engine") == ("Quillbrook's engine", [])
    assert fix("units to Tarrow Mead's") == ("units to Tarrowmede's", [("Tarrow Mead's", "Tarrowmede's", "sound")])


def test_another_towns_street_is_never_a_target_and_a_close_rival_blocks():
    assert fix("Kestlewod Street")[1] == []                         # Otherville's: never corrected into
    v = vocab(towns=("Zephyrton", "Quillbrook", "Otherville"))
    assert fix("Kestlewod Street", v)[0] == "Kestlewood Street"     # (that town on: it is)
    # "Brindlecomb Avenue" (ours) and "Brindlecombe Avenue" (Otherville's) are as close to what was heard: ambiguous
    assert fix("Brindlecom Avenue")[1] == []
    assert fix("Brindlecom Avenue", vocab(towns=("Zephyrton",)))[1] == []


def test_a_single_heard_word_needs_a_one_word_name_and_more_letters():
    assert fix("respond to Quilbrook")[0] == "respond to Quillbrook"
    assert fix("respond to quilbrook")[1] == []                     # one word: only a name as the engine wrote one ...
    assert fix("Quilbrook units respond")[1] == []                  # ... and never a sentence's first word
    assert fix("copy. Quilbrook units respond")[1] == []
    assert fix("Zefyrton")[1] == []                                 # one word: same sound, two letters off (0.87)
    assert fix("Quilbrok Commons")[0] == "Quillbrook Commons"
    assert fix("commons")[1] == []                                  # never one word into "Quillbrook Commons"


def test_the_trailing_time_and_digits_are_never_touched():
    text = "Engine ninety seven responding to 12 Samplewerth Street, fifteen fifty."
    out, fixes = fix(text)
    assert out == "Engine 97 responding to 12 Sampleworth Street, fifteen fifty."
    end = datetime(2026, 3, 4, 15, 50, 30, tzinfo=TZ).timestamp()
    assert stt_time.check(text, end).said == stt_time.check(out, end).said == "1550"
    # a unit's number words that run into the time: left for stt_time
    assert fix("engine ninety seven fifteen fifty")[0] == "Engine 97 fifteen fifty"
    assert fix("ladder twelve thirty")[1] == []
    assert fix("Sampleworth Street 1550, unit 97")[1] == []


def test_the_raw_text_is_unchanged_and_each_fix_says_where():
    text = "engine ninety seven to samplewerth street"
    out, fixes = V.correct(text)
    assert text == "engine ninety seven to samplewerth street"
    assert [(f.at, text[f.at:f.at + len(f.heard)]) for f in fixes] == [(0, "engine ninety seven"),
                                                                        (23, "samplewerth street")]
    assert all(0.0 < f.score <= 1.0 for f in fixes)


def test_sections_and_towns():
    secs = stt_terms.sections(PLACES)
    assert secs["sampleworth street"] == "streets: Zephyrton, Quillbrook (busiest first, as said)"
    assert secs["kestlewood street"] == "streets: Otherville" and "zephyrton" in secs
    assert stt_correct.towns_of(secs["sampleworth street"]) == ("zephyrton", "quillbrook")
    assert stt_correct.towns_of("public landmarks") == ()


# -- the corrector: per channel, the channel's own list -----------------------------------------------------------------

class FakeStore:
    def __init__(self):
        lists = {"county_fire": stt_terms.parse(FIRE), "places": stt_terms.parse(PLACES),
                 "pd_dispatch": stt_terms.parse(PD)}
        self._sets = stt_terms.build_sets(lists)
        self._secs = {"county_fire": stt_terms.sections(FIRE), "places": stt_terms.sections(PLACES),
                      "pd_dispatch": stt_terms.sections(PD)}

    def get(self, own):
        return self._sets.get(own) if own else None

    def sections(self):
        return dict(self._secs)


def corrector(log=None, channels=("tg:307",)):
    own = {"tg:307": "county_fire", "tg:309": "county_fire", "p25:other:9001": "pd_dispatch"}
    return Corrector(lambda: FakeStore(), own.get, channels, TOWNS, log=(log.append if log is not None else print))


def test_only_on_its_channels_and_only_with_the_channels_list():
    c = corrector()
    res = c.correct("engine ninety seven to samplewerth street", "tg:307")
    assert res.text == "Engine 97 to Sampleworth Street" and res.n == 2
    assert res.v.startswith(stt_correct.RULE + "@county_fire@")
    assert json.loads(res.rules_json())[0] == ["engine ninety seven", "Engine 97", "unit", 1.0]
    assert c.correct("engine ninety seven", "tg:309") is None                       # its switch is off
    assert corrector(channels=("p25:other:9001",)).correct("engine ninety seven", "p25:other:9001").n == 0  # not its list
    assert c.correct("engine ninety seven", "") is None and c.correct("", "tg:307") is None
    nothing = c.correct("all units copy", "tg:307")
    assert (nothing.text, nothing.n, nothing.rules_json()) == ("", 0, "")
    assert Corrector(lambda: None, lambda k: "county_fire", ("tg:307",)).correct("x", "tg:307") is None


def test_the_default_switch_is_the_county_fire_ems_dispatch_only(tmp_path):
    assert stt_correct.DEFAULT_CHANNELS == stt_time.DEFAULT_CHANNELS and "tg:307" in stt_correct.DEFAULT_CHANNELS
    assert not any(k.startswith("p25:") or k.startswith("air") for k in stt_correct.DEFAULT_CHANNELS)
    base = tmp_path / "c.toml"
    base.write_text("")
    c = load(base)
    assert c.stt_correct_channels == stt_time.DEFAULT_CHANNELS and c.stt_correct_towns == stt_correct.DEFAULT_TOWNS
    base.write_text('[stt]\ncorrect_channels = [307, "p25:other:9001"]\ncorrect_towns = ["Zephyrton"]\n')
    c = load(base)
    assert c.stt_correct_channels == ("tg:307", "p25:other:9001") and c.stt_correct_towns == ("Zephyrton",)
    base.write_text("[stt]\ncorrect_channels = []\n")
    assert load(base).stt_correct_channels == ()
    for bad in ('correct_channels = "307"', "correct_towns = [1]", 'correct_towns = [""]'):
        base.write_text(f"[stt]\n{bad}\n")
        with pytest.raises(ConfigError):
            load(base)


def test_logs_never_carry_a_term_or_a_transcript():
    log: list[str] = []
    c = corrector(log)
    c.warm(["tg:307", "tg:999"])
    c.correct("engine ninety seven to samplewerth street", "tg:307")
    text = " ".join(log)
    assert log and "county_fire" in text
    for secret in ("Sampleworth", "samplewerth", "Engine 97", "ninety", "Zephyrton", "Quillbrook"):
        assert secret not in text


def test_a_failure_is_no_correction_and_never_logs_the_text(monkeypatch):
    log: list[str] = []
    c = corrector(log)
    monkeypatch.setattr(Vocab, "correct", lambda self, text: 1 / 0)
    assert c.correct("engine ninety seven", "tg:307") is None
    assert log[-1] == "stt correct: ZeroDivisionError"


def test_a_row_takes_its_channel_key():
    assert stt_correct.row_key({"channel": "Fire/EMS 15", "talkgroup": 307, "system": ""}) == "tg:307"
    assert stt_correct.row_key({"channel": "FG1", "talkgroup": None}) == "FG1"
    assert stt_correct.row_key({"channel": "x", "talkgroup": 9001, "system": "other"}) == "p25:other:9001"
    text, fixes = corrector().row({"text": "engine ninety seven", "talkgroup": 307})
    assert text == "Engine 97" and fixes == [{"heard": "engine ninety seven", "to": "Engine 97", "rule": "unit"}]
    assert corrector().row({"text": "engine ninety seven", "talkgroup": 309}) == ("engine ninety seven", [])


def test_fast_enough_for_the_radio_rows():
    c = corrector()
    c.warm(["tg:307"])
    texts = [f"engine ninety seven to samplewerth street number {i}, smoke showing" for i in range(200)]
    t0 = time.perf_counter()
    for t in texts:
        c.correct(t, "tg:307")
    assert time.perf_counter() - t0 < 2.0


# -- stt_runs: kept beside the raw text ----------------------------------------------------------------------------------

def test_the_correction_is_kept_with_the_run(tmp_path):
    runs = sttruns.Runs(tmp_path / "stt.sqlite")
    res = corrector().correct("engine ninety seven to samplewerth street", "tg:307")
    assert runs.add("c1", "aws", text="engine ninety seven to samplewerth street", channel="tg:307", corr=res)
    assert runs.add("c2", "aws", text="all units copy", channel="tg:307",
                    corr=corrector().correct("all units copy", "tg:307"))
    assert runs.add("c3", "aws", text="engine ninety seven", channel="tg:309")
    r1, = runs.of("c1")
    assert r1["text"] == "engine ninety seven to samplewerth street"              # the raw text as it came
    assert r1["corr_text"] == "Engine 97 to Sampleworth Street" and r1["corr_n"] == 2
    assert r1["corr_rules"][1][:3] == ["samplewerth street", "Sampleworth Street", "sound"]
    assert r1["corr_v"].startswith("vcorr/1@county_fire@")
    r2, = runs.of("c2")
    assert (r2["corr_text"], r2["corr_n"], r2["corr_rules"]) == ("", 0, [])
    r3, = runs.of("c3")
    assert (r3["corr_text"], r3["corr_n"], r3["corr_v"]) == ("", None, "")


def test_a_runs_file_from_before_gets_the_correction_columns(tmp_path):
    import sqlite3
    path = tmp_path / "stt.sqlite"
    db = sqlite3.connect(path)
    db.executescript(sttruns.SCHEMA)
    db.execute("INSERT INTO stt_runs (clip_id, engine, text, done_at) VALUES ('old', 'aws', 'Engine 99.', 1.0)")
    db.commit()
    db.close()
    runs = sttruns.Runs(path)
    assert runs.add("new", "aws", text="x", corr=corrector().correct("engine ninety seven", "tg:307"))
    assert runs.of("old")[0]["corr_n"] is None and runs.of("new")[0]["corr_text"] == "Engine 97"


# -- the hub: every engine's text as it arrives; the Radio rows ---------------------------------------------------------

def _event(seq, tid, text, end):
    from sizeup.cad.dispatcher import wav_bytes
    data = {"id": tid, "source": "Fire/EMS 15", "start": end - 4, "end": end, "text": text, "status": "done",
            "unit": "", "talkgroup": 307, "rig": "", "radio_id": "", "raw": text, "confidence": 0.9, "model": "m.bin",
            "attach": False, "pairs": None}
    wav = wav_bytes(np.zeros(1600, np.float32), 8000)
    return {"seq": seq, "at": end, "type": "traffic", "data": data, "audio": base64.b64encode(wav).decode()}


def test_the_hub_records_each_engines_correction_and_the_radio_row_shows_it(tmp_path, monkeypatch):
    from tests.test_app import wait
    from tests.test_cloud import HubRun, call, hub_cfg
    from tests.test_stt_terms import write
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    write(tmp_path / "terms", county_fire=FIRE, places=PLACES)
    hub = HubRun(hub_cfg(tmp_path, p25_talkgroups={307: "Fire/EMS 15"}, stt_terms_whisper=True,
                         stt_terms_dir=str(tmp_path / "terms"), stt_correct_towns=TOWNS))
    try:
        app = hub.app
        app.stt_terms.refresh()
        end = time.time() - 1
        text = "engine ninety seven responding to samplewerth street"
        assert call(hub.url + "/api/station/events", "POST",
                    {"epoch": "e1", "events": [_event(1, "307-1700000001_1", text, end)]})[0] == 200
        assert wait(lambda: app.stt_runs.of("307-1700000001_1"), 10)
        run, = app.stt_runs.of("307-1700000001_1")
        assert run["text"] == text and run["corr_text"] == "Engine 97 responding to Sampleworth Street"
        assert run["corr_n"] == 2 and run["corr_v"].startswith("vcorr/1@county_fire@")
        stored = app.cad.store.one("SELECT text FROM radio_traffic WHERE id = ?", ("307-1700000001_1",))
        assert stored["text"] == text                                            # the radio log's row as it came
        row = next(r for r in app.cad.comms.query(limit=10) if r["id"] == "307-1700000001_1")
        assert row["text"] == text and row["fixed_text"] == "Engine 97 responding to Sampleworth Street"
        assert [(f["heard"], f["to"]) for f in row["fixes"]] == [("engine ninety seven", "Engine 97"),
                                                                ("samplewerth street", "Sampleworth Street")]
        # a GPU worker's text on another channel (its switch off): kept, never corrected
        app._stt_ran("307-1700000001_1", "gpu-worker", "large-v3-turbo", text, "done", 0.9, key="tg:309")
        assert app.stt_runs.of("307-1700000001_1")[-1]["corr_n"] is None
    finally:
        hub.close()


def test_status_words_are_put_back_after_the_vocabulary_and_both_show_on_hover(tmp_path):
    from tests.test_dispatch_modes import AutoRig
    from tests.test_incident_sources import T0
    rig = AutoRig(tmp_path)
    rig.cad.hints.vocab = corrector().row
    rig.radio("307-31-call_31-t0", T0 + 20, "engine ninety seven to samplewerth street.", rig="", unit="")
    rig.radio("307-32-call_32-t0", T0 + 21, "all units copy.", rig="", unit="")
    rows = {r["id"]: r for r in rig.cad.comms.query(limit=10)}
    r = rows["307-31-call_31-t0"]
    assert r["text"] == "engine ninety seven to samplewerth street."
    assert r["fixed_text"] == "Engine 97 to Sampleworth Street."
    assert {f["rule"] for f in r["fixes"]} == {"unit", "sound"}
    assert "fixed_text" not in rows["307-32-call_32-t0"]
    rig.cad.hints.vocab = lambda row: 1 / 0                                     # a failure: the row as heard
    assert "fixed_text" not in {r["id"]: r for r in rig.cad.comms.query(limit=10)}["307-31-call_31-t0"]


def test_the_app_wires_the_corrector_to_the_lists_and_the_rows(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.test_app import cfg
    from tests.test_stt_terms import write
    from sizeup.app import App
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    write(tmp_path / "terms", county_fire=FIRE, places=PLACES)
    app = App(replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15"}, stt_terms_whisper=True,
                      stt_terms_dir=str(tmp_path / "terms"), stt_correct_towns=TOWNS))
    app.stt_terms.refresh()
    assert app.cad.hints.vocab == app.stt_correct.row
    assert app.stt_correct.correct("engine ninety seven", "tg:307").text == "Engine 97"
    off = App(replace(cfg(tmp_path / "b"), stt_terms_whisper=False, stt_terms_aws=False))
    assert off.stt_correct is None and off.cad.hints.vocab is None


# -- the offline measure (tools/stt_correct_eval.py) --------------------------------------------------------------------

def test_the_offline_measure_reports_identifier_recall_and_wer_before_and_after(tmp_path):
    from tests.test_stt_terms import write
    terms = write(tmp_path / "terms", county_fire=FIRE, places=PLACES)
    end = datetime(2026, 3, 4, 15, 50, 30, tzinfo=TZ).timestamp()

    def clip(i, heard, ref, tg=307):
        return {"clip_id": f"307-{i}", "channel": "Fire/EMS 15", "talkgroup": tg, "talker": "", "start": end - 5,
                "end": end, "asr": {"aws-transcribe": heard}, "fixed_text": heard,
                "correction": {"text": ref, "checked": True, "by": "x", "at": end, "based_on": ""}, "system": ""}
    lines = [clip(1, "engine ninety seven responding to samplewerth street, fifteen fifty.",
                  "Engine 97 responding to Sampleworth Street, 1550."),
             clip(2, "ladder ninety three on location", "Ladder 93 on location"),
             clip(3, "all units copy", "all units copy"),
             clip(4, "engine ninety seven", "Engine 97", tg=999)]                  # off there: not counted
    jl = tmp_path / "export.jsonl"
    jl.write_text("".join(json.dumps(x) + "\n" for x in lines))
    out = tmp_path / "out.json"
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "stt_correct_eval.py"), "--jsonl", str(jl), "--terms",
                        str(terms), "--towns", ",".join(TOWNS), "--json", str(out)],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    assert p.returncode == 0, p.stderr
    got = json.loads(out.read_text())
    a = got["all"]
    assert a["clips"] == 3 and a["changed"] == 2 and a["fixes"] == 3
    assert a["ids"] == 3 and a["id_recall_before"] == 0.0 and a["id_recall_after"] == 100.0
    assert a["wer_after"] < a["wer_before"] and a["hurt"] == 0 and a["helped"] == 2
    assert a["time_same"] == 3 and a["time_changed"] == 0
    for secret in ("Sampleworth", "samplewerth", "ninety", "Engine 97", "Zephyrton"):     # aggregates only
        assert secret not in p.stdout and secret not in p.stderr
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "stt_correct_eval.py"), "--jsonl", str(jl), "--terms",
                        str(terms), "--json", str(ROOT / "x.json")], capture_output=True, text=True, cwd=str(ROOT),
                       timeout=120)
    assert p.returncode != 0 and not (ROOT / "x.json").exists()                  # never into the repo
