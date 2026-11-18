"""The dispatcher's trailing time (#727, sizeup/stt_time.py): the normaliser, its record with each STT run
(sizeup/sttruns.py), the match rate on Health, and the hub's hook. Synthetic text only: made-up units and times."""

from __future__ import annotations

import base64
import csv
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from sizeup import stt_time, sttruns
from sizeup.config import ConfigError, load
from sizeup.stt_time import AUTO_LABEL, MATCH, MISMATCH, NO_TIME, REVIEW, applies, check, readings
from sizeup.tz import TZ

ROOT = Path(__file__).resolve().parent.parent


def at(h, m, s=30, day=(2026, 3, 4)):
    """An epoch time at h:m:s local (America/New_York) on day."""
    return datetime(*day, h, m, s, tzinfo=TZ).timestamp()


# -- reading the time -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, said", [
    ("Engine 99 respond to the alarm, 1550", "1550"),
    ("Engine 99 respond to the alarm, 1550.", "1550"),
    ("Engine 99 respond to the alarm. 15:50.", "1550"),
    ("Engine 99 respond to the alarm, 15-49.", "1549"),
    ("Engine 99 respond to the alarm, 15.50", "1550"),
    ("Engine 99 respond to the alarm, 1,550.", "1550"),
    ("Engine 99 respond to the alarm, 15 50.", "1550"),
    ("Engine 99 respond to the alarm, fifteen fifty.", "1550"),
    ("Engine 99 respond to the alarm, fifteen forty nine", "1549"),
    ("Engine 99 respond to the alarm, Fifteen forty-nine.", "1549"),
    ("Engine 99 respond to the alarm, fifteen fourty-nine.", "1549"),      # (a misspelling an engine writes)
    ("Engine 99 respond to the alarm, 15 fifty.", "1550"),
    ("Engine 99 respond to the alarm, one five five zero.", "1550"),
    ("Engine 99 respond to the alarm, 1550 hours.", "1550"),
    ("Engine 99 respond to the alarm, 1550 hrs", "1550"),
    ("Engine 99 respond to the alarm, 3:50 p.m.", "1550"),
])
def test_a_trailing_time_is_read_in_every_shape_an_engine_writes_it(text, said):
    c = check(text, at(15, 50))
    assert c.result == MATCH and c.said == said and c.flag == AUTO_LABEL, c
    assert c.rule.startswith(stt_time.RULE + ":") and 0 < c.conf <= 1


@pytest.mark.parametrize("text, end, said", [
    ("Ladder 99 to quarters, 0940.", at(9, 40), "0940"),
    ("Ladder 99 to quarters, 940.", at(9, 40), "0940"),                   # (the leading zero dropped)
    ("Ladder 99 to quarters, 09:40", at(9, 40), "0940"),
    ("Ladder 99 to quarters, zero nine forty.", at(9, 40), "0940"),
    ("Ladder 99 to quarters, oh nine oh five.", at(9, 5), "0905"),
    ("Ladder 99 to quarters, nine forty.", at(9, 40), "0940"),
    ("Ladder 99 to quarters, twenty three hundred.", at(23, 0), "2300"),
    ("Ladder 99 to quarters, twenty-one fifteen.", at(21, 15), "2115"),
    ("Ladder 99 to quarters, twenty hundred hours.", at(20, 0), "2000"),
])
def test_morning_and_evening_times(text, end, said):
    c = check(text, end)
    assert c.result == MATCH and c.said == said, c


def test_the_corrected_text_is_canonical_and_the_raw_text_is_never_changed():
    raw = "Engine 99, Ladder 98 respond to the alarm, fifteen forty-nine."
    c = check(raw, at(15, 49))
    assert c.text == "Engine 99, Ladder 98 respond to the alarm, 1549."
    assert raw == "Engine 99, Ladder 98 respond to the alarm, fifteen forty-nine."
    c = check("Engine 99 respond, 1549.", at(15, 49))
    assert (c.result, c.flag, c.text) == (MATCH, AUTO_LABEL, "")       # (already canonical: nothing new to keep)
    assert check("Engine 99 respond, 15:49 hours.", at(15, 49)).text == "Engine 99 respond, 1549 hours."
    # an engine's 12-hour reading: the "p.m." goes with it
    assert check("Engine 99 respond, 3:50 p.m.", at(15, 50)).text == "Engine 99 respond, 1550."
    assert check("Engine 99 respond, 3:50 pm", at(15, 50)).text == "Engine 99 respond, 1550"


def test_the_window_is_two_minutes_before_to_one_after_the_clip_end():
    text = "Engine 99 responding, 1550."
    assert [check(text, at(15, m)).result for m in (47, 48, 49, 50, 51, 52, 53)] == \
        [MISMATCH, MISMATCH, MATCH, MATCH, MATCH, MATCH, MISMATCH]
    # minute 1550 said at 15:51:59 is -1; at 15:52:00 it is -2; the edges cost a little confidence
    assert check(text, at(15, 51, 59)).off_min == -1 and check(text, at(15, 52, 0)).off_min == -2
    assert check(text, at(15, 52)).conf < check(text, at(15, 50)).conf
    assert check(text, at(15, 49)).off_min == 1


def test_a_time_outside_the_window_is_a_mismatch_flagged_for_review_and_left_alone():
    c = check("Engine 99 responding, 1520.", at(15, 50))
    assert (c.result, c.said, c.off_min, c.flag, c.text) == (MISMATCH, "1520", -30, REVIEW, "")


def test_midnight_is_crossed_the_short_way():
    assert check("Engine 99 responding, 2359.", at(0, 0, 40)).off_min == -1
    assert check("Engine 99 responding, 0000.", at(23, 59, 10)).result == MATCH
    assert check("Engine 99 responding, 0001.", at(23, 59, 10)).result == MISMATCH          # +2


def test_daylight_saving_time_is_the_local_clock():
    summer = datetime(2026, 7, 4, 15, 50, 20, tzinfo=TZ).timestamp()   # EDT
    winter = datetime(2026, 1, 4, 15, 50, 20, tzinfo=TZ).timestamp()   # EST
    assert check("Engine 99 responding, 1550.", summer).result == MATCH
    assert check("Engine 99 responding, 1550.", winter).result == MATCH


@pytest.mark.parametrize("text", [
    "Engine 99 responding.",
    "Ladder 99",                                  # (a unit number: two digits are never a time)
    "Engine 99, 60 Control, copy",
    "Engine 99 responding, thank you.",
    "Engine 99 respond to 99 Example Avenue",
    "Engine 99 responding, 1-5-5-0.",             # (something spelled out, never the time)
    "Engine 99, 10-22.",                          # (a ten-code)
    "Engine 99 to the units 15, 49.",             # (two numbers across a comma, never one time)
    "Engine 99 respond to the alarm, 15, 50.",
])
def test_no_trailing_time_is_no_time_never_a_failure(text):
    c = check(text, at(15, 50))
    assert c.result == NO_TIME and c.flag == "" and c.said == "" and c.text == ""


def test_nothing_to_check():
    assert check("", at(15, 50)) is None and check("   ", at(15, 50)) is None
    assert check("Engine 99 responding, 1550.", None) is None
    for end in (float("nan"), float("inf"), -float("inf"), True, 1e12, -5.0, "1550"):   # (never a clip's end)
        assert check("Engine 99 responding, 1550.", end) is None


def test_not_a_time_hours_or_minutes_out_of_range():
    assert readings("Engine 99 responding, 2575.") == []
    assert readings("Engine 99 responding, 9999.") == []


def test_the_best_reading_that_fits_wins():
    # "twenty one fifty" is 2150, or (its first word lost) 0150: the one that fits wins
    assert check("Engine 99 responding, twenty one fifty.", at(21, 50)).said == "2150"
    assert check("Engine 99 responding, twenty one fifty.", at(1, 50)).said == "0150"
    # a unit number before the time is never part of it
    assert check("Engine 99, 1550.", at(15, 50)).said == "1550"
    assert check("Engine 99, 10:22.", at(10, 22)).result == MATCH        # (a colon is a time; "10-22" a ten-code)


def test_a_reading_cut_from_longer_number_words_is_a_match_but_never_a_label_or_a_fix():
    c = check("Engine 99 responding, twenty one fifty.", at(1, 50))
    assert (c.result, c.said, c.flag, c.text) == (MATCH, "0150", "", "")
    assert c.rule.endswith(":part")
    c = check("Engine 99 responding, twenty one fifty.", at(21, 50))           # (the whole run: a label and a fix)
    assert (c.flag, c.text) == (AUTO_LABEL, "Engine 99 responding, 2150.")


def test_a_run_whose_prompt_carried_the_clip_time_is_prompted_never_a_label():
    for text in ("Engine 99 responding, 1550.", "Engine 99 responding, 1520.", "Engine 99 responding."):
        c = check(text, at(15, 50), prompted=True)
        assert c.result == stt_time.PROMPTED and c.flag == "" and c.text == ""
    end = at(15, 50, 40)
    assert stt_time.prompted("77 Alpha 4, 60 Control, 1550.", end)          # talker_prompt "ids+time"'s shape
    assert stt_time.prompted("77 Alpha 4, 60 Control, 1549.", end)
    assert not stt_time.prompted("77 Alpha 4, 60 Control.", end)              # "ids"
    assert not stt_time.prompted("2391 Duty Officer, 60 Control.", end)       # (a callsign's digits, no time)
    assert not stt_time.prompted("1442 Duty Officer, 60 Control.", end)       # (four digits far from the clip's time)
    assert stt_time.prompted("1442 Duty Officer.")                            # (no end: any time-like number)
    assert not stt_time.prompted("") and not stt_time.prompted("Engine 99, 10-22.", end)


def test_the_stations_whisper_marks_a_prompt_that_carried_the_time():
    from sizeup.app import Rerun
    from sizeup.incidents import Traffic
    from sizeup.stt import Transcriber, WhisperClient
    from sizeup.talkers import talker_prompt
    end = at(15, 50, 40)
    tr = Transcriber(WhisperClient("http://127.0.0.1:9/inference"), lambda item: None,
                     prompt=lambda item: talker_prompt(item.unit, item.start, with_time=True))
    t = Traffic(id="307-1", start=end - 4, end=end, source="Fire/EMS 15", text="", unit="60 Ctrl OP 7", talkgroup=307)
    assert tr.prompt(t).endswith("1550.") and t.stt_prompt_time is True
    assert t.as_dict()["stt_prompt_time"] is True                             # (sent with the transmission)
    u = Traffic(id="307-2", start=end - 4, end=end, source="Fire/EMS 15", text="", unit="Engine 99", talkgroup=307)
    assert tr.prompt(u) == "Engine 99." and u.stt_prompt_time is False       # (the time only on a "Control" talker)
    r = Rerun(id="307-1", kind="traffic", fm=False, unit="60 Ctrl OP 7", start=end - 4)
    tr.prompt(r)
    assert r.stt_prompt_time is True                                          # (a re-run: no end, its start)


def test_a_long_transcript_is_read_only_at_its_end():
    text = "Engine 99 respond to the alarm " * 40 + "1550."
    t0 = time.perf_counter()
    for _ in range(200):
        assert check(text, at(15, 50)).result == MATCH
    assert time.perf_counter() - t0 < 1.0            # cheap: the hub runs it as each transcript arrives


# -- which clips ----------------------------------------------------------------------------------------------------


def test_only_the_configured_channels_and_the_dispatchers_clips():
    chans = stt_time.DEFAULT_CHANNELS
    assert applies("tg:307", "60 Control", chans) and applies("tg:307", "", chans)
    assert applies("tg:307", "60 Control, 60 Ctrl", chans)
    assert not applies("tg:307", "Engine 99", chans)             # a unit's own transmission
    assert not applies("tg:307", "60 Control, Engine 99", chans)
    assert not applies("FG1", "", chans) and not applies("p25:mrrs:9211", "", chans) and not applies("", "", chans)
    assert not applies("tg:307", "", ())                          # [] = off


def test_the_default_channels_are_the_county_fire_ems_dispatch_talkgroups():
    with open(ROOT / "deploy/trunk-recorder/talkgroups.csv", newline="") as f:
        rows = list(csv.reader(f))
    want = {f"tg:{int(r[0])}" for r in rows[1:] if len(r) > 2 and r[2].startswith("Fire/EMS ") and r[2][9:].isdigit()}
    assert want and set(stt_time.DEFAULT_CHANNELS) == want


def test_config_time_channels(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    assert load(p).stt_time_channels == stt_time.DEFAULT_CHANNELS
    p.write_text('[stt]\ntime_channels = [307, "tg:303"]\n')
    assert load(p).stt_time_channels == ("tg:307", "tg:303")
    p.write_text("[stt]\ntime_channels = []\n")
    assert load(p).stt_time_channels == ()
    p.write_text('[stt]\ntime_channels = "307"\n')
    with pytest.raises(ConfigError):
        load(p)


# -- kept with the run, and the rate --------------------------------------------------------------------------------


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_the_check_is_kept_with_the_run_and_the_raw_text_beside_it(tmp_path):
    runs = sttruns.Runs(tmp_path / "stt.sqlite")
    raw = "Engine 99 responding, fifteen fifty."
    c = check(raw, at(15, 50))
    assert runs.add("c1", "aws", text=raw, status="done", model="aws-transcribe", channel="tg:307", check=c)
    assert runs.add("c2", "station-whisper", text="Engine 99 responding.", channel="FG1")     # not checked
    r1, = runs.of("c1")
    assert r1["text"] == raw and r1["time_fixed_text"] == "Engine 99 responding, 1550."
    assert (r1["time_check"], r1["time_said"], r1["time_off"], r1["label_flag"]) == (MATCH, "1550", 0, AUTO_LABEL)
    assert r1["fix_rule"].startswith("ttime/1:") and r1["time_conf"] == c.conf
    r2, = runs.of("c2")
    assert (r2["time_check"], r2["time_fixed_text"], r2["time_off"], r2["time_conf"]) == ("", "", None, None)


def test_a_file_from_before_gets_the_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / "stt.sqlite"
    db = sqlite3.connect(path)
    db.executescript(sttruns.SCHEMA)
    db.execute("INSERT INTO stt_runs (clip_id, engine, text, done_at) VALUES ('old', 'aws', 'Engine 99.', 1.0)")
    db.commit()
    db.close()
    runs = sttruns.Runs(path)
    assert runs.of("old")[0]["text"] == "Engine 99." and runs.of("old")[0]["time_check"] == ""
    assert runs.add("new", "aws", text="Engine 99, 1550.", channel="tg:307", check=check("Engine 99, 1550.", at(15, 50)))
    assert runs.of("new")[0]["time_check"] == MATCH


def test_the_match_rate_per_channel_engine_and_model_by_day(tmp_path):
    clock = Clock(at(15, 50, 0))
    runs = sttruns.Runs(tmp_path / "stt.sqlite", clock=clock)
    now = clock.t

    def put(cid, engine, model, text, done, chan="tg:307"):
        runs.add(cid, engine, model=model, text=text, channel=chan, done_at=done, check=check(text, done))

    for i in range(3):                                      # today: AWS 2 of 3 said times match, 1 no time
        put(f"a{i}", "aws", "aws-transcribe", "Engine 99 responding, " + ("1550." if i < 2 else "1520."), now - 10)
    put("a3", "aws", "aws-transcribe", "Engine 99 responding.", now - 10)
    put("w0", "station-whisper", "m.bin", "Engine 99 responding, 1550.", now - 10)
    put("w1", "station-whisper", "m.bin", "Engine 99 responding, 1550.", now - 3 * 86400)   # three days ago
    put("w2", "station-whisper", "m.bin", "Engine 99 responding, 1520.", now - 3 * 86400)
    put("old", "station-whisper", "m.bin", "Engine 99 responding, 1550.", now - 9 * 86400)  # before the window
    runs.add("x", "aws", text="Engine 99 responding, 1550.", channel="FG1", done_at=now - 10)  # not checked
    rows = runs.time_rates()
    assert [(r["engine"], r["model"]) for r in rows] == [("aws", "aws-transcribe"), ("station-whisper", "m.bin")]
    a, w = rows
    assert (a["n_24h"], a["said_24h"], a["match_24h"], a["rate_24h"]) == (4, 3, 2, 66.7)
    assert a["channel"] == "tg:307" and a["vocab_v"] == "" and a["daily"] == [None] * 6 + [66.7]
    assert (w["n_24h"], w["n"], w["said"], w["match"], w["rate_24h"], w["rate"]) == (1, 3, 3, 2, 100.0, 66.7)
    assert w["daily"] == [None, None, None, 50.0, None, None, 100.0]
    put("a4", "aws", "aws-transcribe", "Engine 99 responding, 1550.", now - 5)
    assert runs.time_rates()[0]["n_24h"] == 4                        # (worked out once a minute) ...
    clock.t += sttruns.TIME_RATES_S + 1
    assert runs.time_rates()[0]["n_24h"] == 5                        # ... then again


def test_the_rate_splits_by_vocab_v_once_runs_carry_it(tmp_path):
    path = tmp_path / "stt.sqlite"
    db = sqlite3.connect(path)
    db.executescript(sttruns.SCHEMA)
    if "vocab_v" not in {r[1] for r in db.execute("PRAGMA table_info(stt_runs)")}:   # (#659's column: its SCHEMA has it)
        db.execute("ALTER TABLE stt_runs ADD COLUMN vocab_v TEXT NOT NULL DEFAULT ''")
    sttruns.Runs(path)._open()                       # (a file this release made before vocab_v came: its index)
    db.execute("DROP INDEX IF EXISTS stt_runs_time_v")
    db.execute(f"CREATE INDEX IF NOT EXISTS stt_runs_time ON stt_runs ({sttruns.TIME_INDEX[1]}) WHERE time_check != ''")
    db.commit()
    db.close()
    clock = Clock(at(15, 50, 0))
    runs = sttruns.Runs(path, clock=clock)
    for cid, voc in (("a", "v1"), ("b", "v2"), ("c", "v2")):
        runs.add(cid, "aws", model="aws-transcribe", text="Engine 99, 1550.", channel="tg:307", done_at=clock.t - 5,
                 check=check("Engine 99, 1550.", clock.t))
        runs._open().execute("UPDATE stt_runs SET vocab_v = ? WHERE clip_id = ?", (voc, cid))
    assert sorted((r["vocab_v"], r["n_24h"]) for r in runs.time_rates()) == [("v1", 1), ("v2", 2)]
    idx = {r[0] for r in runs._open().execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "stt_runs_time_v" in idx and "stt_runs_time" not in idx        # (the index with vocab_v, the old one gone)


def test_rates_count_first_passes_only_never_a_prompted_run(tmp_path):
    clock = Clock(at(15, 50, 0))
    runs = sttruns.Runs(tmp_path / "stt.sqlite", clock=clock)
    text, done = "Engine 99 responding, 1520.", clock.t - 10
    for cid, role in (("t", "text"), ("a", "alt"), ("l", "late"), ("r", "rerun"), ("c", "catchup")):
        runs.add(cid, "aws", model="aws-transcribe", text=text, channel="tg:307", role=role, done_at=done,
                 check=check(text, done))
    runs.add("p", "aws", model="aws-transcribe", text=text, channel="tg:307", done_at=done,
             check=check(text, done, prompted=True))
    assert runs.of("p")[0]["time_check"] == "prompted" and runs.of("p")[0]["label_flag"] == ""
    row, = runs.time_rates()
    assert (row["n_24h"], row["said_24h"], row["match_24h"]) == (3, 3, 0)


def test_the_rates_read_only_their_index_and_never_wait_on_a_write(tmp_path):
    import threading
    clock = Clock(at(15, 50, 0))
    runs = sttruns.Runs(tmp_path / "stt.sqlite", clock=clock)
    runs.add("a", "aws", text="Engine 99, 1550.", channel="tg:307", done_at=clock.t - 5,
             check=check("Engine 99, 1550.", clock.t))
    plan = " ".join(str(r[-1]) for r in runs._reader().execute(
        "EXPLAIN QUERY PLAN SELECT channel, engine, model, '', CAST((? - done_at) / 86400 AS INTEGER), time_check, "
        "COUNT(*) FROM stt_runs WHERE time_check != '' AND done_at > ? AND role IN ('text', 'alt', 'late') AND "
        "time_check IN ('match', 'time_mismatch', 'no_time') GROUP BY 1, 2, 3, 4, 5, 6", (clock.t, clock.t - 7 * 86400)))
    assert "COVERING INDEX stt_runs_time" in plan, plan
    held, done = threading.Event(), threading.Event()

    def writer():                                   # (a long write on the transcript thread)
        with runs._lock:
            held.set()
            done.wait(5)
    threading.Thread(target=writer, daemon=True).start()
    held.wait(2)
    t0 = time.monotonic()
    try:
        assert runs.time_rates()[0]["n_24h"] == 1
        assert time.monotonic() - t0 < 1.0
    finally:
        done.set()


def test_no_file_no_rates():
    assert sttruns.Runs().time_rates() == []


# -- on the hub: every engine's transcript, as it arrives --------------------------------------------------------------


def _county_event(seq, tid, text, unit, end):
    from sizeup.cad.dispatcher import wav_bytes
    data = {"id": tid, "source": "Fire/EMS 15", "start": end - 4, "end": end, "text": text, "status": "done",
            "unit": unit, "talkgroup": 307, "rig": "", "radio_id": "", "raw": text, "confidence": 0.9, "model": "m.bin",
            "attach": False, "pairs": None}
    wav = wav_bytes(np.zeros(1600, np.float32), 8000)
    return {"seq": seq, "at": end, "type": "traffic", "data": data, "audio": base64.b64encode(wav).decode()}


def test_the_hub_checks_the_dispatchers_clips_as_they_arrive_and_health_shows_the_rate(tmp_path):
    from tests.test_app import wait
    from tests.test_cloud import HubRun, call, hub_cfg
    hub = HubRun(hub_cfg(tmp_path, p25_talkgroups={307: "Fire/EMS 15"}))
    try:
        end = time.time() - 1
        said = datetime.fromtimestamp(end, TZ).strftime("%H%M")
        h, m = int(said[:2]), int(said[2:])
        words = f"{h:02d}:{m:02d}"
        events = [_county_event(1, "307-1700000001_1", f"Engine 99 respond to the alarm, {words}.", "60 Control", end),
                  _county_event(2, "307-1700000002_1", "Engine 99 responding, 0101.", "Engine 99", end),
                  _county_event(3, "307-1700000003_1", "Engine 99, copy.", "60 Control", end),
                  _county_event(4, "307-1700000004_1", f"Engine 98 respond, {said}.", "60 Control", end)]
        events[3]["data"]["stt_prompt_time"] = True            # (whisper's prompt had the clip's own time)
        if said == "0101":       # (the unit's made-up number happens to be the time: another one)
            events[1]["data"]["text"] = events[1]["data"]["raw"] = "Engine 99 responding, 0102."
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": events})[0] == 200
        app = hub.app
        assert wait(lambda: all(app.stt_runs.of(e["data"]["id"]) for e in events), 10)
        run, = app.stt_runs.of("307-1700000001_1")
        assert run["engine"] == "station-whisper" and run["text"] == f"Engine 99 respond to the alarm, {words}."
        assert run["time_check"] == MATCH and run["time_fixed_text"] == f"Engine 99 respond to the alarm, {said}."
        assert run["label_flag"] == AUTO_LABEL
        stored = app.cad.store.one("SELECT text FROM radio_traffic WHERE id = ?", ("307-1700000001_1",))
        assert stored["text"] == f"Engine 99 respond to the alarm, {words}."      # the radio log's text as it came
        assert app.stt_runs.of("307-1700000002_1")[0]["time_check"] == ""            # a unit's: not checked
        assert app.stt_runs.of("307-1700000003_1")[0]["time_check"] == NO_TIME
        prompted, = app.stt_runs.of("307-1700000004_1")
        assert (prompted["time_check"], prompted["label_flag"], prompted["time_fixed_text"]) == ("prompted", "", "")
        # the station's re-run of a clip, its prompt carrying the time: prompted too (and a re-run: never counted)
        retext = {"seq": 5, "at": end, "type": "retext",
                  "data": {"id": "307-1700000003_1", "text": f"Engine 99, copy, {said}.", "status": "done",
                           "confidence": 0.9, "model": "m.bin", "stt_prompt_time": True}}
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [retext]})[0] == 200
        assert wait(lambda: len(app.stt_runs.of("307-1700000003_1")) == 2, 10)
        rerun = app.stt_runs.of("307-1700000003_1")[1]
        assert (rerun["role"], rerun["time_check"]) == ("rerun", "prompted")
        app.stt_runs._rates = None
        st = app.health.model()["stt_time"]
        row, = st["rows"]
        assert (row["channel"], row["name"], row["engine"], row["n_24h"], row["said_24h"], row["rate_24h"]) == \
            ("tg:307", "Fire/EMS 15", "station-whisper", 2, 1, 100.0)
        assert st["rule"] == stt_time.RULE and st["window_min"] == [-2, 1]
    finally:
        hub.close()


def test_a_check_never_costs_a_run_and_a_clip_the_hub_cant_place_is_not_checked(tmp_path, monkeypatch):
    from tests.test_app import wait
    from tests.test_cloud import HubRun, call, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app, end = hub.app, time.time() - 1
        said = datetime.fromtimestamp(end, TZ).strftime("%H%M")
        tid = "307-1700000001_1"
        ev = _county_event(1, tid, "Engine 99, copy.", "60 Control", end)
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})[0] == 200
        assert wait(lambda: app.stt_runs.of(tid), 10)
        text = f"Engine 99, copy, {said}."
        last = lambda cid: app.stt_runs.of(cid)[-1]  # noqa: E731
        # a GPU worker's run: its prompt carried the clip's time
        app._stt_ran(tid, "gpu-worker", "large-v3-turbo", text, "done", 0.9,
                     prompted=stt_time.prompted(f"60 Control, {said}.", end))
        assert last(tid)["time_check"] == "prompted"
        app._stt_ran(tid, "gpu-worker", "large-v3-turbo", text, "done", 0.9, prompted=False)
        assert last(tid)["time_check"] == MATCH
        app._stt_ran(tid, "aws", "aws-transcribe", text, "done", 0.9, clip_end=float("nan"))     # (no end to read)
        assert last(tid)["text"] == text and last(tid)["time_check"] == ""
        app._stt_ran("307-1700000099_1", "aws", "aws-transcribe", text, "done", 0.9, key="tg:307", clip_end=end)
        assert last("307-1700000099_1")["time_check"] == ""             # (no transmission here: not checked)

        def boom(*a, **k):
            raise ValueError("a bad reading")
        monkeypatch.setattr(stt_time, "check", boom)
        n = len(app.stt_runs.of(tid))
        app._stt_ran(tid, "aws", "aws-transcribe", text, "done", 0.9)
        assert len(app.stt_runs.of(tid)) == n + 1 and last(tid)["time_check"] == ""    # the run kept all the same
    finally:
        hub.close()


def test_a_channel_set_switched_off_checks_nothing(tmp_path):
    from tests.test_app import wait
    from tests.test_cloud import HubRun, call, hub_cfg
    hub = HubRun(hub_cfg(tmp_path, stt_time_channels=()))
    try:
        end = time.time() - 1
        said = datetime.fromtimestamp(end, TZ).strftime("%H%M")
        ev = _county_event(1, "307-1700000001_1", f"Engine 99, {said}.", "60 Control", end)
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})[0] == 200
        assert wait(lambda: hub.app.stt_runs.of("307-1700000001_1"), 10)
        assert hub.app.stt_runs.of("307-1700000001_1")[0]["time_check"] == ""
        assert hub.app.health.model()["stt_time"] is None
    finally:
        hub.close()


# -- Health in a browser (health.js; Playwright + Chromium, skipped without them; SIZEUP_SHOTS=<dir>: the PR's shot) ---


class _Health:
    def panel(self):
        def row(ch, eng, model, n, said, match, rate, daily):
            return {"channel": ch, "name": {"tg:307": "Fire/EMS 15", "tg:303": "Fire/EMS 12"}[ch], "engine": eng, "model": model, "vocab_v": "", "n_24h": n, "said_24h": said,
                    "match_24h": match, "rate_24h": round(100 * match / said, 1), "n": n * 7, "said": said * 7,
                    "match": int(said * 7 * rate / 100), "rate": rate, "daily": daily}
        tt = {"rule": stt_time.RULE, "window_min": [-2, 1], "days": 7, "rows": [
            row("tg:307", "aws", "aws-transcribe", 210, 70, 55, 76.0, [74, 77, None, 75, 78, 76, 78.6]),
            row("tg:303", "station-whisper", "m.bin", 160, 44, 31, 69.4, [70, 68, 71, 66, 72, 69, 70.5]),
            row("tg:307", "gpu-worker", "large-v3-turbo", 12, 4, 4, 81.0, [None, None, None, None, None, 80, 100])]}
        return {"health": {"stt_time": tt, "hints": [], "dongles": [], "channels": []}, "alerts": [], "history": [],
                "notify": {"on": False, "muted": []}, "thresholds": {}}


def test_health_shows_the_trailing_time_match_rate(tmp_path):
    import os
    import threading

    from sizeup.cad.api import CadApi
    from sizeup.web.server import WebServer
    from tests.test_cad import make_cad
    from tests.test_roles import CASEY, people
    from tests.test_stations_card import Access, browser_page
    cad = make_cad(tmp_path)
    people(cad)
    cad.cloud = lambda: {"role": "hub", "state": "ok", "word": "", "detail": "connected"}
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad), access=Access(), role="hub", station_health=_Health())
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    site = type("Site", (), {"base": f"http://127.0.0.1:{ws.httpd.server_address[1]}", "cad": cad})()
    try:
        pw, browser, pg = browser_page(site, CASEY)
        try:
            pg.goto(site.base + "/cad#settings/station/health")
            head = pg.get_by_text("Trailing time", exact=True)
            head.wait_for()
            panel = pg.locator("#s-station-health")
            assert "Rule ttime/1" in head.get_attribute("title")
            tr = panel.locator("tr", has_text="aws-transcribe")
            assert tr.locator("td").all_inner_texts()[:7] == ["Fire/EMS 15", "aws", "aws-transcribe", "210", "33%", "79%", "76%"]
            assert tr.locator("svg.sg-spark").count() == 1
            shots = os.environ.get("SIZEUP_SHOTS", "")
            if shots:
                pg.evaluate("el => el.scrollIntoView({block: 'start'})", head.element_handle())
                box = head.bounding_box()
                Path(shots).mkdir(parents=True, exist_ok=True)
                pg.screenshot(path=str(Path(shots) / "health-trailing-time.png"),
                              clip={"x": box["x"] - 8, "y": box["y"] - 8, "width": 1000, "height": 170})
            assert pg.errors == []
        finally:
            browser.close()
            pw.stop()
    finally:
        ws.shutdown()
