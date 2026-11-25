"""Unclear transcripts (sizeup/stt_quality.py): a repetition loop, more words than the clip holds, or whisper's stock
phrase on noise, from ANY engine, is stored as "unclear" with no text (its words kept apart, shown dimmed), recorded
with the engine that wrote it, and asked once of another engine; real radio shapes (a unit called three times, a radio
ID said digit by digit, a quick sentence) stand. Invented text only (the shapes of what was seen, never its words)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sizeup import stt_quality as Q
from sizeup.stt import URGENT
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws  # noqa: F401 (the fixture: test credentials only)
from tests.test_cloud import HubRun, hub_cfg
from tests.test_stt_pool import FakeWorker, pending_event, send

# whisper's decoder stuck on a phrase: a few words, then the same three again and again (a 2 s clip)
LOOP = "So then we were" + " out of the" * 60
CLEAN = [
    ("Port Chester Engine 61, Port Chester Engine 61, Port Chester Engine 61, respond to a fire alarm", 7.0),
    ("Car 7-7-7-7-7-7 to headquarters, show me out", 4.0),          # a radio ID said digit by digit
    ("Mayday, mayday, mayday, Engine 61, firefighter down second floor", 5.0),
    ("Okay, you got a white van off the roadway. Let me show you the plate.", 2.4),   # quick, but said
    ("No, no, no, no, the other side of the building", 3.0),
    ("Thank you", 0.8), ("Okay", 0.5),
    ("Water. 10-6, copy.", 2.5),
]


def test_a_loop_is_repeated():
    assert Q.assess(LOOP, 2.0) == "repeated"
    assert Q.assess(LOOP) == "repeated"                    # (whatever the clip's length)
    assert Q.longest_run(Q.words(LOOP)) == (60, 3)
    assert Q.assess("Copy that, copy that, copy that, copy that", 3.0) == "repeated"      # four times in a row
    assert Q.assess("ten four " * 8, 9.0) == ""            # numbers alone are never a loop ("ten four": a code)
    assert Q.assess("yes " * 6, 4.0) == "repeated"          # one word: six in a row


def test_one_phrase_over_half_the_text_is_repeated_even_with_breaks():
    text = " ".join(f"we need the ladder {x}" for x in ("uh", "um", "so", "and", "now")) + " over here"
    assert Q.longest_run(Q.words(text))[0] < 4 and Q.cover(Q.words(text)) > 0.5
    assert Q.assess(text, 20.0) == "repeated"


def test_more_words_than_the_clip_holds_is_too_long():
    text = "engine sixty one respond to the north end for a report of smoke showing from a two story frame house"
    assert Q.assess(text, 1.0) == "too_long"               # 20 words in a second
    assert Q.assess(text, 4.0) == ""                       # 5 words a second: said
    assert Q.assess(text, None) == "" and Q.assess(text, 0) == ""     # no length known: not judged on it


def test_whispers_stock_phrases_and_bare_punctuation():
    for t in ("Thanks for watching!", "Thank you for watching.", "you", "You.", "Please subscribe to my channel",
              "Engine 61 on location. Thanks for watching!"):
        assert Q.assess(t, 5.0) == "stock_phrase", t
    assert Q.assess("...", 2.0) == "no_words" and Q.assess("", 2.0) == ""


@pytest.mark.parametrize("text,seconds", CLEAN)
def test_real_radio_shapes_stand(text, seconds):
    assert Q.assess(text, seconds) == ""


def test_the_engine_behind_a_model_label():
    assert Q.engine_of("large-v3-turbo-q5_0 (GPU gpu-1)") == "gpu-worker"
    assert Q.engine_of("aws-transcribe") == "aws"
    assert Q.engine_of("whisper.bin") == Q.engine_of("small.en-q5_1 (station st-1)") == "station-whisper"
    assert Q.label("repeated") == "Unclear (repeated)" and Q.label("?") == "Unclear"


# -- the hub: a GPU worker's loop is unclear, kept apart, recorded, and asked of AWS once ------------------------------

def row(hub, tid):
    return hub.app.cad.store.one("SELECT text, status, model, quality, unclear_text, rerun_result FROM radio_traffic "
                                 "WHERE id = ?", (tid,)) or {}


def test_a_gpu_workers_loop_is_unclear_never_the_text_and_aws_is_asked_once(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    w = FakeWorker(hub.url, token, text=LOOP).start()
    asked = []
    real = hub.app.aws.submit
    hub.app.aws.submit = lambda key, job, pcm: asked.append((job.id, job.rerun)) or real(key, job, pcm)
    try:
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        send(hub, pending_event("FG1-9", seconds=2.0))
        # the worker's loop lands unclear: no text, its words apart, then AWS's answer (asked once) is the text
        assert wait(lambda: row(hub, "FG1-9").get("text") == AWS_TEXT + ".", 30), row(hub, "FG1-9")
        assert asked == [("FG1-9", True)]
        r = row(hub, "FG1-9")
        assert r["status"] == "done" and r["quality"] == "" and r["unclear_text"] == ""    # clean words clear the flag
        assert wait(lambda: len(hub.app.stt_runs.of("FG1-9")) == 2, 5)
        runs = hub.app.stt_runs.of("FG1-9")
        assert [(x["engine"], x["status"]) for x in runs] == [("gpu-worker", "unclear"), ("aws", "done")]
        assert runs[0]["text"].startswith("So then we were out of the") and runs[0]["worker"] == "gpu-1"
        db = sqlite3.connect(Path(hub.app.cfg.cad_db))
        hist = db.execute("SELECT COUNT(*) FROM radio_traffic WHERE text LIKE '%out of the out of the%'").fetchone()[0]
        assert hist == 0                                    # the loop was never a row's text
    finally:
        w.close()
        hub.close()
        fake.close()


def test_with_aws_out_the_unclear_row_stays_unclear_and_the_stations_whisper_is_asked_once(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    w = FakeWorker(hub.url, token, text=LOOP).start()
    tried = []
    hub.app._rerun_station = lambda ids, as_alt=False: tried.append(list(ids))
    try:
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        hub.app.aws.available = lambda key=None: (False, "test: AWS can't take audio now")
        send(hub, pending_event("FG1-8", seconds=2.0))
        assert wait(lambda: row(hub, "FG1-8").get("status") == "unclear", 30), row(hub, "FG1-8")
        r = row(hub, "FG1-8")
        assert r["text"] == "" and r["quality"] == "repeated" and r["unclear_text"].startswith("So then we were")
        assert r["model"] == "large-v3-turbo-q5_0 (GPU gpu-1)"             # which engine wrote it
        assert wait(lambda: tried == [["FG1-8"]], 5)        # AWS out: the station's whisper, once
        item = hub.app._find("FG1-8")[0]
        hub.app._unclear(item, "FG1")                       # (a second unclear answer: never asked again)
        assert tried == [["FG1-8"]]
    finally:
        w.close()
        hub.close()
        fake.close()


def test_a_pages_looped_voice_is_never_parsed_as_a_dispatch(tmp_path):
    from tests.test_transcribe_watchdog import _page_event
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, text="Port Chester engine 99 respond to" + " a structure fire at" * 20).start()
    hub.app.aws.available = lambda key=None: (False, "test: AWS can't take audio now")
    hub.app._rerun_station = lambda ids, as_alt=False: None
    try:
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        send(hub, _page_event("South_2-901"))
        page = lambda: next((p for p in hub.app.pages if p.id == "South_2-901"), None)   # noqa: E731
        assert wait(lambda: page() is not None and page().status == "unclear", 20)
        p = page()
        assert p.raw == "" and getattr(p, "dispatch", None) is None and p.quality == "repeated"
        assert p.unclear_text.startswith("Port Chester engine 99 respond to a structure fire at")
        r = hub.app.cad.store.one("SELECT text, status, quality FROM radio_traffic WHERE id = ?", ("South_2-901",))
        assert dict(r) == {"text": "", "status": "unclear", "quality": "repeated"}
    finally:
        w.close()
        hub.close()
        fake.close()
