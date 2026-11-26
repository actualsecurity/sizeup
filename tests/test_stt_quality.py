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
    ("Boat ramp lot, copy.", 2.5),
    ("Emergency traffic, emergency traffic, emergency traffic, emergency traffic", 4.0),
    ("All units all units all units all units evacuate the building now", 5.0),
    ("Get out, get out, get out, get out", 3.0), ("Help " * 6, 3.0), ("Mayday " * 6, 3.0), ("Priority " * 6, 4.0),
    ("Unit 7, Unit 7, Unit 7, Unit 7", 4.0), ("Engine 61 " * 4, 4.0), ("Go ahead " * 4, 3.0),
    ("Attention Port Chester, " * 4, 5.0), ("Chief 1 to command, " * 4, 6.0),
    ("Fire alarm. 1-2-3 Example Ave. " * 4, 10.0),     # a page read four times
    ("Medic 5, Medic 5, Medic 5, Medic 5", 1.9),        # quick, digits and all: under LOOP_WPS
    ("Chief 1 to command, " * 4, 3.9),
    ("Get out, get out, get out, get out", 1.2),       # a safety word: never judged by the fast-repeat rule
    ("Engine 61 respond El Camino Del Rio Las Palmas", 5.0),    # Spanish street names
    ("PC 4 El Paseo Los Altos", 3.0), ("Subject is José Peña Núñez, white jacket", 4.0),     # an accented surname
]


def test_a_loop_is_repeated():
    assert Q.assess(LOOP, 2.0) == "repeated"
    assert Q.assess(LOOP) == "repeated"                    # (whatever the clip's length)
    assert Q.longest_run(Q.words(LOOP)) == (60, 3)
    assert Q.assess("copy that " * 8, 5.0) == "repeated"   # eight times in a row: a loop at any pace
    assert Q.assess("copy that " * 4, 1.0) == "repeated"   # four, faster than a call is repeated
    assert Q.assess("copy that " * 4, 3.0) == ""           # four at a speaking pace: said on purpose
    assert Q.assess("ten four " * 12, 4.0) == ""           # numbers alone are never a loop ("ten four": a code)
    assert Q.assess("yes " * 12, 8.0) == "repeated" and Q.assess("yes " * 6, 0.8) == "repeated"


def test_the_loop_shapes_seen_on_the_gpu_worker():
    # invented equivalents of what large-v3-turbo wrote on 3-8 s airband and police clips
    assert Q.assess("six-car-pileup " * 60, 5.0) == "repeated"                          # a hyphenated word, 60 times
    assert Q.assess("six-car-pileup " * 10, None) == "repeated"                         # ... 10, any pace
    assert Q.assess("we will meet you at the gate " * 25, 8.0) == "repeated"            # a 7-word phrase, 25 times
    assert Q.assess("hold short runway " * 40 + "are you able to accept the visual " * 10, 6.0) == "repeated"
    assert Q.assess("Tower, Cessna four two lima is ten miles south inbound full stop with the numbers. "
                    + "and then the " * 20, 8.0) == "repeated"                          # real words, then a loop (the tail)
    assert Q.assess("we have the hydrant " * 4, 2.0) == "repeated"                     # 4 times, faster than said


def test_a_safety_word_never_excuses_a_loop():
    # the false pre-alert's shape with an urgent word in it: a loop is unclear whatever it says (the alarms read it)
    for t in ("help me at the kelm " * 30, "unit nine priority " * 30, "copy " + "help on the vell " * 30,
              "Mayday" + " mayday" * 40):
        assert Q.assess(t, 5.0) == "repeated", t[:30]
    assert Q.assess("Mayday " * 6, 2.0) == ""                                           # said six times: said
    for said in ("Engine 60, Engine 60", "Copy, copy", "Engine 60, Engine 60, Engine 60", "Copy, copy, copy, copy"):
        assert Q.assess(said, 2.5) == "", said


def test_too_many_words_for_the_clip():
    text = " ".join(f"word{i}" for i in range(150))                                   # 150 words, no loop
    assert Q.assess(text, 3.0) == "too_long" and Q.assess(text, 40.0) == ""


def test_another_language_is_unclear_english_with_a_few_accents_or_ids_is_not():
    for t in ("Ég fer heim og það er ekki gott veður í dag hjá okkur",                    # Icelandic-looking
              "Yo creo que el carro está por aquí pero no sé dónde",                     # Spanish-looking
              "Ich bin hier und das ist nicht gut für uns heute",                        # German-looking
              "eg veit ekki hvar hann er og thad er mjog kalt nuna"):                    # Icelandic-like, no accents
        assert Q.assess(t, 4.0) == "not_english", t
    assert Q.assess("Engine 61 on location", 2.0, language="is") == "not_english"     # whisper's own detection
    for t, lang in (("Engine 61 on location", "en"), ("Engine 61 on location", None),
                    ("Engine 61 respond to 12 Avenida Café, by the Peñasco Road bridge", None),
                    ("Car 3 to HQ, the subject is at the bodega on Main, he says que pasa", None),
                    ("Rescue 40 to 60 Control, 10-4, en route with E63 and L31", None)):
        assert Q.assess(t, 5.0, language=lang) == "", t


def test_one_phrase_over_half_the_text_is_repeated_even_with_breaks():
    text = " ".join(f"we need the ladder {x}" for x in ("uh", "um", "so", "and", "now", "ok", "ah", "well")) + " here"
    assert Q.longest_run(Q.words(text))[0] < 4 and Q.cover(Q.words(text)) > 0.5
    assert Q.assess(text, 20.0) == "repeated"
    assert Q.assess(" ".join(f"we need the ladder {x}" for x in ("uh", "um", "so", "and")), 8.0) == ""   # four: said


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
    hub.app._station_rerun = lambda ids, as_alt=False: tried.append(list(ids)) or len(ids)
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
    hub.app._station_rerun = lambda ids, as_alt=False: 0
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


def test_the_alarms_still_hear_an_unclear_row_and_only_a_loop_is_asked_again_once_never_a_late_one(tmp_path):
    from sizeup.incidents import Traffic
    hub = HubRun(hub_cfg(tmp_path))
    heard, asked = [], []
    hub.app.cad.alarms.on_traffic = lambda d: heard.append((d["id"], d["text"]))
    hub.app._station_rerun = lambda ids, as_alt=False: asked.append(list(ids)) or len(ids)
    hub.app.aws = None
    try:
        def tx(tid, text, **kw):
            t = Traffic(tid, "FG1", 1000.0, 1002.0, "", status="done")
            t.raw, t.model = text, "large-v3-turbo-q5_0 (GPU gpu-1)"
            for k, v in kw.items():
                setattr(t, k, v)
            hub.app._transcribed(t)
            return t
        t = tx("FG1-a", LOOP)
        assert (t.status, t.text, t.quality) == ("unclear", "", "repeated")
        assert heard == [("FG1-a", LOOP)]                   # the alarms read the words (a looped mayday is never lost)
        assert wait(lambda: asked == [["FG1-a"]], 5)
        hub.app._unclear(t, "FG1")
        assert asked == [["FG1-a"]]                         # once
        tx("FG1-b", LOOP, late=True)                        # a call from while sizeup was down: never asked again
        tx("FG1-c", "Thanks for watching!")                 # whisper's stock phrase on noise: no speech, not again
        import time
        time.sleep(0.3)
        assert asked == [["FG1-a"]]
        assert hub.app._find("FG1-c")[0].quality == "stock_phrase"
    finally:
        hub.close()


def test_a_loop_from_a_later_engine_never_blanks_clean_words_and_the_hubs_re_ask_is_nobodys_re_run(tmp_path):
    from sizeup.incidents import Traffic
    hub = HubRun(hub_cfg(tmp_path))
    finished, asked = [], []
    hub.app._rerun_finish = lambda cid, result, model="": finished.append((cid, result))
    hub.app._station_rerun = lambda ids, as_alt=False: asked.append(list(ids)) or len(ids)
    hub.app.aws = None
    try:
        t = Traffic("FG1-r", "FG1", 1000.0, 1004.0, "", status="done")
        t.raw, t.model = "Engine 61 on location, nothing showing", "aws-transcribe"
        hub.app._transcribed(t)
        assert hub.app._retext("FG1-r", LOOP, "done", None, "large-v3-turbo-q5_0 (GPU gpu-1)") is False
        r = hub.app.cad.store.one("SELECT text, status, quality FROM radio_traffic WHERE id = ?", ("FG1-r",))
        assert dict(r) == {"text": "Engine 61 on location, nothing showing", "status": "done", "quality": ""}
        assert t.status == "done" and not getattr(t, "quality", "")
        # a GPU loop with AWS out: the station's whisper, once, and its "re-run over" is nobody's re-run
        u = Traffic("FG1-s", "FG1", 1000.0, 1002.0, "", status="done")
        u.raw, u.model = LOOP, "large-v3-turbo-q5_0 (GPU gpu-1)"
        hub.app._transcribed(u)
        assert wait(lambda: asked == [["FG1-s"]], 5)
        hub.app.apply("rerun", {"id": "FG1-s", "result": "same", "model": "whisper.bin"}, None)
        assert finished == []
        hub.app.apply("rerun", {"id": "FG1-s", "result": "same", "model": "whisper.bin"}, None)
        assert finished == [("FG1-s", "same")]               # (only the re-ask's own answer is kept off the row)
    finally:
        hub.close()


def test_the_re_ask_stops_short_of_the_aws_budget(tmp_path):
    from sizeup.incidents import Traffic
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    sent, asked = [], []
    hub.app.aws.submit = lambda key, job, pcm: sent.append(job.id)
    hub.app._station_rerun = lambda ids, as_alt=False: asked.append(list(ids)) or len(ids)
    hub.app._rerun_pcm = lambda item: b"pcm"
    hub.app.aws.available = lambda key=None: (True, "")
    try:
        hub.app.aws.spent = lambda pool="": {"usd": 9.5, "limit_usd": 10.0}       # 95 % spent
        u = Traffic("FG1-b1", "FG1", 1000.0, 1002.0, "", status="done")
        u.raw, u.model = LOOP, "large-v3-turbo-q5_0 (GPU gpu-1)"
        hub.app._transcribed(u)
        assert wait(lambda: asked == [["FG1-b1"]], 5) and sent == []
        hub.app.aws.spent = lambda pool="": {"usd": 5.0, "limit_usd": 10.0}       # half
        v = Traffic("FG1-b2", "FG1", 1000.0, 1002.0, "", status="done")
        v.raw, v.model = LOOP, "large-v3-turbo-q5_0 (GPU gpu-1)"
        hub.app._transcribed(v)
        assert sent == ["FG1-b2"]
    finally:
        hub.close()
        fake.close()


def test_another_systems_row_turning_unclear_is_told_to_the_context_hook(tmp_path):
    from tests.test_context_text_hook import Clock, cad, ctx
    clock = Clock()
    c = cad(tmp_path, clock)
    c.radiolog.inline = True
    calls = {"context": []}
    c.comms.on_context_text = lambda tid: calls["context"].append(tid)
    c.record_traffic(ctx("metro-u1", clock.t - 5, "zorbly flimmet quandle"))
    c.record_traffic(dict(ctx("metro-u1", clock.t - 5, ""), status="unclear", quality="repeated",
                          unclear_text="flim flam " * 20))
    c.record_traffic(ctx("metro-u2", clock.t - 5, ""))                                  # no text, not unclear: nothing
    assert calls["context"] == ["metro-u1", "metro-u1"]
