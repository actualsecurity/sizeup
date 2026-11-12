"""The county-wide custom vocabulary (sizeup/stt_vocab.py): generated from the repo's public files only (the county's
fire mutual aid plan in data/westchester_*.csv, data/cad_lists.json, data/gazetteer.sqlite)."""

import csv

import pytest

from sizeup import stt_vocab as V
from sizeup.cad.lists import load
from sizeup.channels import vocabulary_suggestions
from sizeup.transcribe import MAX_BYTES, MAX_PHRASES, build_vocabulary, respell

LISTS = load()


def plan_rows():
    lines = [ln for ln in (V.DATA / "westchester_apparatus.csv").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    return list(csv.DictReader(lines))


def suggestions(battalions=V.SUGGEST_BATTALIONS):
    county = V.county_phrases(LISTS.units, LISTS.geo_town_aliases, battalions=battalions)
    return vocabulary_suggestions(LISTS.units, [], county=county)


def test_the_plans_rigs_as_said_without_reserved_numbers_or_antiques():
    rigs = set(V.apparatus())
    rows = plan_rows()
    for r in rows:
        notes = r["notes"].lower()
        if r["unit"] in ("Engine 163", "Tower Ladder 50"):          # RESERVED numbers in the plan (Ardsley)
            assert "reserved" in notes and r["unit"] not in rigs
        if "antique" in notes:
            assert r["unit"] not in rigs or any(x["unit"] == r["unit"] and "antique" not in x["notes"].lower()
                                                for x in rows)
    antiques = {r["unit"] for r in rows if "antique" in r["notes"].lower()}
    assert len(antiques) >= 10 and {"Engine 242", "Engine 220", "Engine 6"} <= antiques   # the plan's (Ardsley ...)
    assert not antiques & rigs                                     # no antique's number is a working rig's too
    assert {"Engine 164", "Ladder 50", "Engine 34", "Marine 34"} <= rigs
    assert "S.S.U. 15" in rigs and "SSU 15" not in rigs             # the plan's abbreviations, said letter by letter
    assert not any(r.startswith(("(", "74-")) for r in rigs)        # nothing the plan doesn't number


def test_chief_cars_by_the_countys_numbering_and_the_home_ones_from_cad_lists():
    have = {c for c in LISTS.units if c.isdigit() and len(c) == 4}
    cars = V.chief_cars(have=have)
    assert {"2011", "2015", "2301", "2305"} <= set(cars)               # Ardsley 201, New Rochelle 230 ...
    assert "2016" not in cars                                         # ranks 1 .. CHIEF_RANKS
    assert not any(c.startswith(("239", "242", "266")) for c in cars)  # cad_lists has Port Chester's, Rye's, Rye Brook's


def test_callsigns_said_the_way_dispatchers_say_them_and_written_back():
    built = build_vocabulary(suggestions(None))
    sent = set(built["phrases"])
    for said in ("twenty-three-oh-one", "twenty-four-twenty-five", "seventy-seven-Alpha-three", "Engine-sixty",
                 "Ladder-thirty-one", "Tower-Ladder-two", "Rescue-forty", "Engine-thirty-four", "sixty-Control",
                 "Fire-EMS-eighteen", "Battalion-nineteen", "to-sixty-Control"):
        assert said in sent, said
    assert respell("twenty three oh one to sixty Control, Engine thirty four on location", built["respell"]) \
        == "2301 to 60 Control, Engine 34 on location"


def test_departments_towns_and_statuses_are_in():
    v = set(suggestions())
    for p in ("Port Chester", "Rye Brook", "Rye", "Harrison", "Mamaroneck", "Town of Mamaroneck", "Larchmont",
              "New Rochelle", "White Plains", "Greenwich", "Ardsley", "Byram", "Port Chester EMS", "EVAC",
              "en route", "responding", "on location", "on scene", "in the area", "staging", "back in service",
              "in quarters", "all units back in service", "Fire EMS 15", "60 Control", "Boston Post Road"):
        assert p in v, p
    assert "LMVAC" not in v                                           # an acronym said otherwise: left out


def test_the_suggested_battalions_only_for_callsigns():
    near = set(V.county_phrases(LISTS.units, battalions=V.SUGGEST_BATTALIONS))
    county = set(V.county_phrases(LISTS.units))
    assert "Engine 34" in near and "2301" in near                     # Larchmont: Battalion 15, New Rochelle: 18
    assert "Engine 270" in county and "Engine 270" not in near        # Yorktown: Battalion 17
    assert "Yorktown" in near                                         # every department's name either way
    assert near < county


def test_the_lists_fit_aws_and_settings():
    for battalions in (V.SUGGEST_BATTALIONS, None):
        v = suggestions(battalions)
        built = build_vocabulary(v)
        assert not built["skipped"], built["skipped"][:3]
        assert len(built["phrases"]) <= MAX_PHRASES
        assert sum(len(p.encode()) + 1 for p in built["phrases"]) <= MAX_BYTES
        assert all(len(p) <= 256 for p in built["phrases"])
    from sizeup.cad.settings import MAX_VOCAB
    assert len(suggestions()) <= MAX_VOCAB


def test_over_50_kb_is_skipped_not_sent():
    many = [f"Phrase {chr(97 + i % 26)}{chr(97 + i // 26 % 26)}{chr(97 + i // 676)} " + "x" * 200 for i in range(400)]
    built = build_vocabulary(many)
    assert sum(len(p.encode()) + 1 for p in built["phrases"]) <= MAX_BYTES
    assert built["skipped"] and "50 KB" in built["skipped"][0]["why"]


def test_the_table_form_has_display_as_and_sounds_like():
    rows = [ln.split("\t") for ln in V.table(["2301", "Mamaroneck", "Engine 34", "S.S.U. 8", "Port Chester"])
            .splitlines()]
    assert rows[0] == ["Phrase", "IPA", "SoundsLike", "DisplayAs"]
    t = {r[0]: r for r in rows[1:]}
    assert t["twenty-three-oh-one"][3] == "2301"
    assert t["Engine-thirty-four"][3] == "Engine 34"
    assert t["S.S.U.-eight"][3] == "S.S.U. 8"
    assert t["Mamaroneck"][2] == V.SOUNDS_LIKE["Mamaroneck"] and t["Mamaroneck"][3] == ""
    assert t["Port-Chester"][1:] == ["", "", ""]
    assert all(len(r) == 4 for r in rows)


@pytest.mark.parametrize("said, kept", [
    ("working fire two story frame", "working fire two story frame"),
    ("structure fire four floors", "structure fire four floors"),
    ("brush fire eight acres", "brush fire eight acres"),
    ("Battalion ten four", "Battalion ten four"),
    ("back in twenty twelve", "back in twenty twelve"),
    ("go to sixty and hold", "go to sixty and hold"),
])
def test_respell_leaves_plain_speech_alone(said, kept):
    for battalions in (V.SUGGEST_BATTALIONS, None):
        built = build_vocabulary(suggestions(battalions))
        assert respell(said, built["respell"]) == kept
        assert {"Fire-two", "Battalion-ten", "to-sixty"} <= set(built["phrases"])


def test_respell_still_writes_callsigns_and_to_60_control():
    built = build_vocabulary(suggestions(None))
    assert "twenty eleven" not in built["respell"] and "2011" in suggestions(None)   # a chief car that is also a year
    assert respell("Engine thirty four to sixty Control, Fire EMS eighteen", built["respell"]) \
        == "Engine 34 to 60 Control, Fire EMS 18"


def test_an_edited_settings_list_survives_the_new_suggestion(tmp_path):
    from sizeup.app import App
    from tests.test_cloud import base
    app = App(base(clips_dir=str(tmp_path / "clips")))
    s = app.cad.settings
    assert "Fire EMS 18" in s.stt_page()["vocabulary"]["suggestions"]          # the county's, suggested
    s.post("stt", {"action": "set", "key": "vocabulary", "value": ["Engine 60", "Rye Brook"]})
    page = s.stt_page()["vocabulary"]
    assert s.vocabulary() == ["Engine 60", "Rye Brook"] and page["edited"] and page["aws"] == ["Engine-sixty", "Rye-Brook"]
    assert "Fire EMS 18" in page["suggestions"] and "Fire EMS 18" not in s.vocabulary()


def test_settings_refuses_a_list_over_awss_size(tmp_path):
    from sizeup.app import App
    from sizeup.cad.settings import MAX_VOCAB, MAX_VOCAB_BYTES
    from sizeup.cad.store import CadError
    from tests.test_cloud import base
    s = App(base(clips_dir=str(tmp_path / "clips"))).cad.settings
    big = [f"Phrase {i:04d} " + "x" * 80 for i in range(MAX_VOCAB)]
    assert sum(len(x) + 1 for x in big) > MAX_VOCAB_BYTES
    with pytest.raises(CadError, match="50 KB"):
        s.post("stt", {"action": "set", "key": "vocabulary", "value": big})
    assert "vocabulary" not in s.stt_values()
