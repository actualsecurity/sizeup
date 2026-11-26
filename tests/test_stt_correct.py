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
Marina
Brenlow Park
Lyrrow Park
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
    ("Engine ninety seven responding", "Engine 97 responding"),
    ("Ladder ninety three on location", "Ladder 93 on location"),
    ("tower ladder eight to ninety control", "Tower Ladder 8 to 90 Control"),     # (each the other's context)
    ("Rescue eighty-eight, copy", "Rescue 88, copy"),
    ("Engine ninety seven, Engine ninety eight, respond", "Engine 97, Engine 98, respond"),
    ("ladder ninety three and Engine 97 on location", "Ladder 93 and Engine 97 on location"),   # (context: in digits)
    ("units copy, ninety Control", "units copy, 90 Control"),
])
def test_spoken_unit_numbers_fold_to_the_lists_unit(heard, shown):
    assert fix(heard)[0] == shown


@pytest.mark.parametrize("heard", [
    "Engine ninety nine responding",          # not a unit of the list
    "Engine ninety seven one responding",     # the number words go on: never "Engine 97 one"
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
    ("working fire at Samplewerth Street", "working fire at Sampleworth Street", "sound"),
    ("respond to Holly Ridge Lane", "respond to Hollyridge Lane", "spell"),
    ("units to Glen varrow road", "units to Glenvarrow Road", "spell"),
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
    assert fix("Engine ninety seven fifteen fifty")[0] == "Engine 97 fifteen fifty"
    assert fix("Ladder twelve thirty")[1] == []
    assert fix("Sampleworth Street 1550, unit 97")[1] == []


def test_the_raw_text_is_unchanged_and_each_fix_says_where():
    text = "Engine ninety seven to Samplewerth street"
    out, fixes = V.correct(text)
    assert text == "Engine ninety seven to Samplewerth street"
    assert [(f.at, text[f.at:f.at + len(f.heard)]) for f in fixes] == [(0, "Engine ninety seven"),
                                                                        (23, "Samplewerth street")]
    assert all(0.0 < f.score <= 1.0 for f in fixes)


def test_sections_and_towns():
    secs = stt_terms.sections(PLACES)
    assert secs["sampleworth street"] == "streets: Zephyrton, Quillbrook (busiest first, as said)"
    assert secs["kestlewood street"] == "streets: Otherville" and "zephyrton" in secs
    assert stt_correct.towns_of(secs["sampleworth street"]) == ("zephyrton", "quillbrook")
    assert stt_correct.towns_of("public landmarks") == ()


# -- more false corrections left alone (#901 review) ---------------------------------------------------------------------

@pytest.mark.parametrize("heard", [
    "Marine 1 responding",                # a unit's prefix, and a number after it: never "Marina 1"
    "copy, Marine one is on the water",
    "units at the Marine Corps building",  # an everyday word the engine capitalised: never a one-word name
    "a Marine veteran at the scene",
])
def test_a_one_word_span_is_never_a_units_prefix_or_an_everyday_word(heard):
    assert fix(heard) == (heard, [])
    assert fix("units at Marena")[0] == "units at Marina"          # (the name itself still put back)


def test_a_possessive_anywhere_is_kept_and_a_plural_is_never_the_singular_name():
    assert fix("units to Brenlo Park")[0] == "units to Brenlow Park"
    assert fix("units to Brenlows Park") == ("units to Brenlows Park", [])        # (no apostrophe: another name)
    assert fix("units to Brenlo's Park") == ("units to Brenlo's Park", [])        # a possessive inside: as heard
    assert fix("units to Brenlo Park's lot")[0] == "units to Brenlow Park's lot"  # (the last word's: put back after)
    assert fix("units to Tarrowmede Commons")[1] == []                           # never "Tarrowmede Common"


@pytest.mark.parametrize("heard", [
    "lyrro park is closed.",              # no word capitalised by the engine
    "Lyrro park is closed.",              # only a sentence's first word
    "copy. Lyrro park is closed.",
])
def test_two_to_four_words_need_a_capital_the_engine_wrote(heard):
    assert fix(heard) == (heard, [])
    assert fix("units at Lyrro Park")[0] == "units at Lyrrow Park"
    assert fix("units at lyrro Park")[0] == "units at Lyrrow Park"


@pytest.mark.parametrize("heard", [
    "a ladder ninety three feet up",      # no capital, no other unit of the list: not a unit
    "Ladder ninety three feet up",        # a measure after the number
    "Engine ninety seven car accident",
    "ladder ninety three on location",    # no capital, nothing else of the list
    "rescue eighty eight years old",
])
def test_a_unit_fold_needs_a_capital_or_context_and_no_measure(heard):
    assert fix(heard) == (heard, [])


def test_corrections_json_is_cut_by_whole_corrections():
    many = tuple(stt_correct.Fix(f"heard words {i}", f"Name {i}", "spell", 0.9, i) for i in range(50))
    res = stt_correct.Result("x", many, "v")
    whole = json.loads(res.rules_json())
    assert len(whole) == 50
    cut = res.rules_json(200)
    assert len(cut) <= 200 and json.loads(cut) == whole[:len(json.loads(cut))] and json.loads(cut)
    assert stt_correct.Result("x", many[:1], "v").rules_json(5) == ""


# -- the corrector: per channel, the channel's own list -----------------------------------------------------------------

class FakeStore:
    def __init__(self, places=PLACES):
        lists = {"county_fire": stt_terms.parse(FIRE), "places": stt_terms.parse(places),
                 "pd_dispatch": stt_terms.parse(PD)}
        self._sets = stt_terms.build_sets(lists)
        self._secs = {"county_fire": stt_terms.sections(FIRE), "places": stt_terms.sections(places),
                      "pd_dispatch": stt_terms.sections(PD)}

    def get(self, own):
        return self._sets.get(own) if own else None

    def sections(self):
        return dict(self._secs)


OWN = {"tg:307": "county_fire", "tg:309": "county_fire", "p25:other:9001": "pd_dispatch"}


def corrector(log=None, channels=("tg:307",), store=None, warm=True):
    store = store or FakeStore()
    c = Corrector(lambda: store, OWN.get, channels, TOWNS, log=(log.append if log is not None else print))
    if warm:
        c.warm(channels)
    return c


def test_only_on_its_channels_and_only_with_the_channels_list():
    c = corrector()
    res = c.correct("Engine ninety seven to Samplewerth Street", "tg:307")
    assert res.text == "Engine 97 to Sampleworth Street" and res.n == 2
    assert res.v.startswith(stt_correct.RULE + "@county_fire@")
    assert json.loads(res.rules_json())[0] == ["Engine ninety seven", "Engine 97", "unit", 1.0]
    assert c.correct("Engine ninety seven", "tg:309") is None                       # its switch is off
    assert corrector(channels=("p25:other:9001",)).correct("Engine ninety seven", "p25:other:9001").n == 0  # not its list
    assert c.correct("Engine ninety seven", "") is None and c.correct("", "tg:307") is None
    nothing = c.correct("all units copy", "tg:307")
    assert (nothing.text, nothing.n, nothing.rules_json()) == ("", 0, "")
    assert Corrector(lambda: None, lambda k: "county_fire", ("tg:307",)).correct("x", "tg:307") is None


def test_pending_until_the_first_build_and_never_built_on_a_transcript():
    c = corrector(warm=False)
    res = c.correct("Engine ninety seven", "tg:307")
    assert res.pending and res.v == stt_correct.PENDING and (res.text, res.n) == ("", 0)
    assert c.vocab("tg:307") is None and c.builds == 0                              # (correct() and vocab() never build)
    c.warm(["tg:307"])
    assert c.builds == 1 and c.correct("Engine ninety seven", "tg:307").text == "Engine 97"
    c.warm(["tg:307", "tg:307"])
    assert c.builds == 1                                                           # the same version: nothing built


def test_a_deleted_list_stops_correcting_once_the_lists_are_read_again():
    log = []
    store = FakeStore()
    c = corrector(log=log, store=store)
    assert c.correct("Engine ninety seven", "tg:307").text == "Engine 97"
    del store._sets["county_fire"]                                                 # the channel's list is gone
    c.warm(["tg:307"])
    assert c.correct("Engine ninety seven", "tg:307") is None and c.vocab("tg:307") is None
    assert any("its list is gone" in m for m in log) and not any("ninety" in m for m in log)
    store.loaded = False                                                           # lists not read yet: nothing dropped
    store._sets["county_fire"] = FakeStore()._sets["county_fire"]
    c.warm(["tg:307"])
    del store._sets["county_fire"]
    c.warm(["tg:307"])
    assert c.vocab("tg:307") is not None


def test_one_build_at_a_time_and_the_one_before_serves_meanwhile(monkeypatch):
    import threading
    store = FakeStore()
    c = corrector(store=store)
    old_v = c.correct("Engine ninety seven", "tg:307").v
    gate, inside, most = threading.Event(), [0], [0]
    real = Vocab.__init__

    def slow(self, *a, **k):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        gate.wait(10)
        try:
            real(self, *a, **k)
        finally:
            inside[0] -= 1
    monkeypatch.setattr(Vocab, "__init__", slow)
    store._secs = {**store._secs, "places": {k: v + " (renamed)" for k, v in store._secs["places"].items()}}
    threads = [threading.Thread(target=c.warm, args=(["tg:307"],)) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    t0 = time.perf_counter()
    res = c.correct("Engine ninety seven", "tg:307")                              # (a build is running)
    assert time.perf_counter() - t0 < 0.5 and res.v == old_v and res.text == "Engine 97"
    gate.set()
    for t in threads:
        t.join(10)
    assert most[0] == 1 and c.builds == 2                                         # one build; the second found it built
    assert c.correct("Engine ninety seven", "tg:307").v != old_v


def test_a_header_changed_alone_is_a_new_version(tmp_path):
    from tests.test_stt_terms import write
    terms = write(tmp_path / "terms", county_fire=FIRE, places=PLACES)
    store = stt_terms.TermStore(stt_terms.DirFiles(terms), log=lambda m: None)
    store.refresh()
    c = Corrector(lambda: store, OWN.get, ("tg:307",), TOWNS, log=lambda m: None)
    c.warm(["tg:307"])
    v1 = c.correct("Engine ninety seven", "tg:307").v
    assert c.correct("Kestlewod Street", "tg:307").n == 0                          # Otherville's: not in scope
    write(terms, places=PLACES.replace("# streets: Otherville", "# streets: Zephyrton"))
    store.refresh()
    assert store.settling                                                        # (used once a later read finds it)
    store.refresh()
    c.warm(["tg:307"])
    v2 = c.correct("Engine ninety seven", "tg:307").v
    assert v2 != v1 and v2.split("@")[:3] == v1.split("@")[:3]                    # the same terms, other sections
    assert c.correct("Kestlewod Street", "tg:307").text == "Kestlewood Street"
    other = Corrector(lambda: store, OWN.get, ("tg:307",), ("Zephyrton",), log=lambda m: None)
    other.warm(["tg:307"])
    assert other.correct("Engine ninety seven", "tg:307").v != v2                 # (the towns are in it too)


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
    c = corrector(log, channels=("tg:307", "tg:999"))
    c.correct("Engine ninety seven to Samplewerth Street", "tg:307")
    text = " ".join(log)
    assert log and "county_fire" in text
    for secret in ("Sampleworth", "Samplewerth", "Engine 97", "ninety", "Zephyrton", "Quillbrook"):
        assert secret not in text


def test_a_failure_is_no_correction_and_never_logs_the_text(monkeypatch):
    log: list[str] = []
    c = corrector(log)
    monkeypatch.setattr(Vocab, "correct", lambda self, text: 1 / 0)
    assert c.correct("Engine ninety seven", "tg:307") is None
    assert log[-1] == "stt correct: ZeroDivisionError"


def test_a_row_takes_its_channel_key_and_shows_its_stored_correction():
    assert stt_correct.row_key({"channel": "Fire/EMS 15", "talkgroup": 307, "system": ""}) == "tg:307"
    assert stt_correct.row_key({"channel": "FG1", "talkgroup": None}) == "FG1"
    assert stt_correct.row_key({"channel": "x", "talkgroup": 9001, "system": "other"}) == "p25:other:9001"
    cols = stt_correct.columns(corrector().correct("Engine ninety seven", "tg:307"))
    row = {"text": "Engine ninety seven", **cols}
    assert stt_correct.shown_text(row) == "Engine 97"
    assert stt_correct.stored_fixes(row) == [{"heard": "Engine ninety seven", "to": "Engine 97", "rule": "unit"}]
    assert stt_correct.shown_text({"text": "as heard"}) == "as heard"                  # (a row from before)
    assert stt_correct.shown_text({"text": "as heard", "corr_text": ""}) == "as heard"
    assert stt_correct.stored_fixes({"corr_rules": "[[1"}) == []
    assert stt_correct.columns(None) == {"corr_text": "", "corr_rules": "", "corr_v": ""}


def test_fast_enough_for_every_transcript():
    c = corrector()
    texts = [f"Engine ninety seven to Samplewerth Street number {i}, smoke showing" for i in range(200)]
    t0 = time.perf_counter()
    for t in texts:
        c.correct(t, "tg:307")
    assert time.perf_counter() - t0 < 2.0


# -- stt_runs: kept beside the raw text ----------------------------------------------------------------------------------

def test_the_correction_is_kept_with_the_run(tmp_path):
    runs = sttruns.Runs(tmp_path / "stt.sqlite")
    res = corrector().correct("Engine ninety seven to Samplewerth Street", "tg:307")
    assert runs.add("c1", "aws", text="Engine ninety seven to Samplewerth Street", channel="tg:307", corr=res)
    assert runs.add("c2", "aws", text="all units copy", channel="tg:307",
                    corr=corrector().correct("all units copy", "tg:307"))
    assert runs.add("c3", "aws", text="Engine ninety seven", channel="tg:309")
    assert runs.add("c4", "aws", text="Engine ninety seven", channel="tg:307",
                    corr=corrector(warm=False).correct("Engine ninety seven", "tg:307"))
    r1, = runs.of("c1")
    assert r1["text"] == "Engine ninety seven to Samplewerth Street"              # the raw text as it came
    assert r1["corr_text"] == "Engine 97 to Sampleworth Street" and r1["corr_n"] == 2
    assert r1["corr_rules"][1][:3] == ["Samplewerth Street", "Sampleworth Street", "sound"]
    assert r1["corr_v"].startswith("vcorr/1@county_fire@")
    r2, = runs.of("c2")
    assert (r2["corr_text"], r2["corr_n"], r2["corr_rules"]) == ("", 0, [])
    r3, = runs.of("c3")
    assert (r3["corr_text"], r3["corr_n"], r3["corr_v"]) == ("", None, "")       # off there
    r4, = runs.of("c4")
    assert (r4["corr_text"], r4["corr_n"], r4["corr_v"]) == ("", None, "pending")  # on, its matcher not built yet


def test_a_runs_file_from_before_gets_the_correction_columns(tmp_path):
    import sqlite3
    path = tmp_path / "stt.sqlite"
    db = sqlite3.connect(path)
    db.executescript(sttruns.SCHEMA)
    db.execute("INSERT INTO stt_runs (clip_id, engine, text, done_at) VALUES ('old', 'aws', 'Engine 99.', 1.0)")
    db.commit()
    db.close()
    runs = sttruns.Runs(path)
    assert runs.add("new", "aws", text="x", corr=corrector().correct("Engine ninety seven", "tg:307"))
    assert runs.of("old")[0]["corr_n"] is None and runs.of("new")[0]["corr_text"] == "Engine 97"


# -- the hub: the text of record corrected before its row is written; every engine's run ----------------------------

def _event(seq, tid, text, end):
    from sizeup.cad.dispatcher import wav_bytes
    data = {"id": tid, "source": "Fire/EMS 15", "start": end - 4, "end": end, "text": text, "status": "done",
            "unit": "", "talkgroup": 307, "rig": "", "radio_id": "", "raw": text, "confidence": 0.9, "model": "m.bin",
            "attach": False, "pairs": None}
    wav = wav_bytes(np.zeros(1600, np.float32), 8000)
    return {"seq": seq, "at": end, "type": "traffic", "data": data, "audio": base64.b64encode(wav).decode()}


def test_the_hub_stores_the_correction_on_the_row_and_every_reader_takes_it(tmp_path, monkeypatch):
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
        tid = "307-1700000001_1"
        end = time.time() - 1
        text = "Engine ninety seven responding to Samplewerth Street"
        assert call(hub.url + "/api/station/events", "POST",
                    {"epoch": "e1", "events": [_event(1, tid, text, end)]})[0] == 200
        assert wait(lambda: app.stt_runs.of(tid), 10)
        run, = app.stt_runs.of(tid)
        assert run["text"] == text and run["corr_text"] == "Engine 97 responding to Sampleworth Street"
        assert run["corr_n"] == 2 and run["corr_v"].startswith("vcorr/1@county_fire@")
        stored = app.cad.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        assert stored["text"] == text                                            # the raw text as it came
        assert stored["corr_text"] == "Engine 97 responding to Sampleworth Street"   # beside it, as it was written
        assert stored["corr_v"] == run["corr_v"] and len(json.loads(stored["corr_rules"])) == 2
        held = next(t for t in app.store.loose if t.id == tid)
        assert held.text == text and held.corr_text == stored["corr_text"]        # (before store.on_traffic)
        row = next(r for r in app.cad.comms.query(limit=10) if r["id"] == tid)
        assert row["text"] == text and row["fixed_text"] == "Engine 97 responding to Sampleworth Street"
        assert [(f["heard"], f["to"]) for f in row["fixes"]] == [("Engine ninety seven", "Engine 97"),
                                                                ("Samplewerth Street", "Sampleworth Street")]
        assert any(r["id"] == tid for r in app.cad.comms.query(limit=10, text="Sampleworth"))   # the Radio filter
        app.cad.addrs.catch_up()                                                 # the search index: as shown too
        assert app.cad.store.one("SELECT rowid FROM fts_radio WHERE fts_radio MATCH ?", ('"Sampleworth"',))
        # the status parser reads the corrected text
        seen: list[str] = []
        real = app.cad.hints.phrases.repair
        monkeypatch.setattr(app.cad.hints.phrases, "repair", lambda t, *a, **k: (seen.append(t), real(t, *a, **k))[1])
        app.cad.hints._read(stored)
        assert seen == ["Engine 97 responding to Sampleworth Street"]
        # a new text (a re-run): its own correction written with it, the one before gone
        app._retext(tid, "Ladder ninety three on location", "done", 0.9, "m.bin")
        stored = app.cad.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        assert (stored["text"], stored["corr_text"]) == ("Ladder ninety three on location", "Ladder 93 on location")
        app._retext(tid, "all units copy", "done", 0.9, "m.bin")
        stored = app.cad.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        assert (stored["corr_text"], stored["corr_rules"]) == ("", "") and stored["corr_v"].startswith("vcorr/1@")
        assert "fixed_text" not in next(r for r in app.cad.comms.query(limit=10) if r["id"] == tid)
        # a GPU worker's text on another channel (its switch off): kept, never corrected
        app._stt_ran(tid, "gpu-worker", "large-v3-turbo", text, "done", 0.9, key="tg:309")
        assert app.stt_runs.of(tid)[-1]["corr_n"] is None
        # a standby corrects nothing for its runs (it writes none)
        calls: list = []
        monkeypatch.setattr(app.stt_correct, "correct", lambda *a: calls.append(a))
        monkeypatch.setattr(app, "_may_write_audio", lambda: False)
        assert app._vocab_fix("tg:307", text) is None and calls == []
    finally:
        hub.close()


def test_before_the_first_build_the_raw_text_is_stored_and_pending(tmp_path, monkeypatch):
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
        monkeypatch.setattr(app.stt_correct, "warm", lambda keys=(): None)      # (the matcher not built yet)
        app.stt_correct._vocab.clear()
        tid = "307-1700000002_1"
        text = "Engine ninety seven responding"
        assert call(hub.url + "/api/station/events", "POST",
                    {"epoch": "e1", "events": [_event(1, tid, text, time.time() - 1)]})[0] == 200
        assert wait(lambda: app.stt_runs.of(tid), 10)
        stored = app.cad.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        assert (stored["text"], stored["corr_text"], stored["corr_v"]) == (text, "", "pending")
        assert app.stt_runs.of(tid)[0]["corr_v"] == "pending"
        assert "fixed_text" not in next(r for r in app.cad.comms.query(limit=10) if r["id"] == tid)
    finally:
        hub.close()


def test_status_words_are_put_back_after_the_vocabulary_and_both_show_on_hover(tmp_path):
    from tests.test_dispatch_modes import AutoRig
    from tests.test_incident_sources import T0
    from tests.test_radios import tx
    rig = AutoRig(tmp_path)
    c = corrector()
    text = "Engine ninety seven to Samplewerth Street."
    for tid, at, said in (("307-31-call_31-t0", T0 + 20, text), ("307-32-call_32-t0", T0 + 21, "all units copy.")):
        rig.clock.t = max(rig.clock.t, at + 1)
        rig.cad.record_traffic({**tx(tid, at, source="Fire/EMS 15", tg=307, text=said),
                                **stt_correct.columns(c.correct(said, "tg:307"))})
    rows = {r["id"]: r for r in rig.cad.comms.query(limit=10)}
    r = rows["307-31-call_31-t0"]
    assert r["text"] == text
    assert r["fixed_text"] == "Engine 97 to Sampleworth Street."
    assert {f["rule"] for f in r["fixes"]} == {"unit", "sound"}
    assert "fixed_text" not in rows["307-32-call_32-t0"]
    # a row recorded again without one (its text written again): none, as heard
    rig.radio("307-31-call_31-t0", T0 + 20, text, rig="", unit="")
    assert "fixed_text" not in {r["id"]: r for r in rig.cad.comms.query(limit=10)}["307-31-call_31-t0"]


def test_the_app_wires_the_corrector_to_the_lists(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.test_app import cfg
    from tests.test_stt_terms import write
    from sizeup.app import App
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    write(tmp_path / "terms", county_fire=FIRE, places=PLACES)
    app = App(replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15"}, stt_terms_whisper=True,
                      stt_terms_dir=str(tmp_path / "terms"), stt_correct_towns=TOWNS))
    assert app.stt_correct.correct("Engine ninety seven", "tg:307").pending      # before the lists' first read
    app.stt_terms.refresh()                                                      # (its on_read builds the matcher)
    assert app.stt_correct.correct("Engine ninety seven", "tg:307").text == "Engine 97"
    off = App(replace(cfg(tmp_path / "b"), stt_terms_whisper=False, stt_terms_aws=False))
    assert off.stt_correct is None


def test_the_measure_picks_each_channels_list_as_the_hub_does():
    from sizeup.app import App
    from tools.stt_correct_eval import Channels
    info = {307: {"group": "Fire Dispatch"}, 303: {"group": "Fire Dispatch"}, 401: {"group": "Police Dispatch"},
            402: {"group": "Public Works"}}
    kinds = {"Paging A": "paging", "Fireground A": "traffic", "Tower A": "airband"}
    hub = SimpleNamespace(tg=SimpleNamespace(info=info), _kind=kinds, systems={},
                          cfg=SimpleNamespace(p25_incident_talkgroups=frozenset({307, 333})))
    hub._key_service = lambda k: App._key_service(hub, k)
    cfg = SimpleNamespace(channels=[SimpleNamespace(label=k, kind=v) for k, v in kinds.items()], p25_tg_info=info,
                          p25_incident_talkgroups=frozenset({307, 333}))
    keys = ["tg:307", "tg:303", "tg:401", "tg:402", "tg:333", "tg:999", "p25:westchester:303", "Paging A",
            "Fireground A", "Tower A", "Unknown A"]
    ch = Channels(cfg)
    got = {k: ch.own_of({"key": k}) for k in keys}
    assert got == {k: App._own_for_key(hub, k) for k in keys}
    assert got["tg:401"] == "pd_dispatch" and got["tg:303"] == "county_fire" and got["tg:402"] is None
    rows = Channels()                                                              # no config: the row's own group
    assert rows.own_of({"key": "tg:401", "group": "Police Dispatch"}) == "pd_dispatch"
    assert rows.own_of({"key": "tg:303", "service": "fire"}) == "county_fire"     # (the JSONL: its service)


# -- the offline measure (tools/stt_correct_eval.py) --------------------------------------------------------------------

def test_the_offline_measure_reports_identifier_recall_and_wer_before_and_after(tmp_path):
    from tests.test_stt_terms import write
    terms = write(tmp_path / "terms", county_fire=FIRE, places=PLACES)
    end = datetime(2026, 3, 4, 15, 50, 30, tzinfo=TZ).timestamp()

    def clip(i, heard, ref, tg=307):
        return {"clip_id": f"307-{i}", "channel": "Fire/EMS 15", "talkgroup": tg, "talker": "", "start": end - 5,
                "end": end, "asr": {"aws-transcribe": heard}, "fixed_text": heard,
                "correction": {"text": ref, "checked": True, "by": "x", "at": end, "based_on": ""}, "system": ""}
    lines = [clip(1, "Engine ninety seven responding to Samplewerth Street, fifteen fifty.",
                  "Engine 97 responding to Sampleworth Street, 1550."),
             clip(2, "Ladder ninety three on location", "Ladder 93 on location"),
             clip(3, "all units copy", "all units copy"),
             clip(4, "Engine ninety seven", "Engine 97", tg=999)]                  # off there: not counted
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
    for secret in ("Sampleworth", "Samplewerth", "ninety", "Engine 97", "Zephyrton"):     # aggregates only
        assert secret not in p.stdout and secret not in p.stderr
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "stt_correct_eval.py"), "--jsonl", str(jl), "--terms",
                        str(terms), "--json", str(ROOT / "x.json")], capture_output=True, text=True, cwd=str(ROOT),
                       timeout=120)
    assert p.returncode != 0 and not (ROOT / "x.json").exists()                  # never into the repo
