"""A dispatch answers before its voice is rendered (#426).

Production, 24 h of the access log: POST .../dispatch took 2.6-3.0 s and .../upgrade about 2.3 s, nearly everything
else 1-20 ms: Cad._send rendered the voice (Polly, or the station's) before it logged the dispatch and answered.

    at once      the units are assigned, the dispatch is logged ("pending") and the state pushed; no audio waited for
    the voice    a job on one worker (sizeup/cad/voicequeue.py), in the order sent: rendered, keyed, attached to its
                 row (ready | failed, with the same notes as before), one more push
    failure      no voice, an error, a full queue: the row is failed with why; the dispatch stays logged
    too late     a voice not started 45 s after its dispatch isn't rendered; one render gets 25 s at most
    preview      the text only; {"audio": true} renders it (the page's second call)
    stop/start   a drain gives the voices a moment and leaves the rest pending; a start renders those still young
                 (a deploy seconds after a dispatch keeps its audio) and fails the older ones
    the fence    not the writer any more: nothing is keyed or written, in SQLite or the audio blobs
    the page     shows the dispatch with "Voice…"; each voice waited for plays once, in order, whatever is
                 selected by then; never one that comes too late
"""

import io
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import dispatcher, service
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.cad.voicequeue import VoiceQueue
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.fence import FencedWrite

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
FAST_S = 2.0            # generous (a slow CI): main waited for the voice, held 5 s here. The proof that nothing waits
                        # is the answer coming back while the voice is still held (held()), not this clock.


class SlowVoice:
    """dispatcher.tts held until release() (5 s at most, so a dispatch that waits for it fails, not hangs)."""

    def __init__(self):
        self.gate, self.started, self.said = threading.Event(), threading.Event(), []

    def __call__(self, text, voice="", rate_wpm=0):
        self.said.append(text)
        self.started.set()
        self.gate.wait(5)
        return np.full(8000, 0.1, np.float32)

    def release(self):
        self.gate.set()

    def held(self):
        return not self.gate.is_set()


@pytest.fixture
def voice(monkeypatch):
    v = SlowVoice()
    monkeypatch.setattr(dispatcher, "tts", v)
    yield v
    v.release()


def make_cad(tmp_path=None, **kw):
    """A Cad with its pushes counted (on_dispatch: the web server's poke) and the voice worker's writes too
    (cad.writes: each transaction it commits; other background work, the radio log's, isn't the voice's)."""
    cad = Cad(Settings(db=str(tmp_path / "cad.sqlite") if tmp_path else "", **kw), log=lambda m: None)
    cad.pushes, cad.writes = [], []
    cad.on_dispatch = lambda: cad.pushes.append(threading.current_thread().name)
    tx = cad.store.tx

    def counted():
        if threading.current_thread().name == "dispatch-voice":
            cad.writes.append(1)
        return tx()
    cad.store.tx = counted
    return cad


def uid(cad, name):
    return next(u["id"] for u in cad.store.units() if u["name"] == name)


def call(cad, address="100 Example Ave"):
    return cad.create({"type": "ALARM", "subtype": "CO-RESD", "address": address})["id"]


def rows(cad, iid):
    return cad.store.full(iid)["dispatches"]


def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, time.monotonic() - t0


# -- at once ----------------------------------------------------------------------------------------------------------

def test_the_dispatch_answers_before_its_voice_and_the_row_goes_pending_then_ready(voice):
    cad = make_cad()
    iid = call(cad)
    r, took = timed(lambda: cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"}))
    assert voice.held() and took < FAST_S, f"the dispatch waited {took:.2f} s"
    assert r["audio"] == "pending" and r["audio_url"] is None and r["dispatch_id"] and r["transmitted"] is False
    assert r["text"].startswith("Port Chester Fire, Engine 60, respond to 100 Example Avenue")
    # everything but the audio is there: the unit assigned, the dispatch in the log and the chronology, one push
    full = cad.store.full(iid)
    assert [a["name"] for a in full["assignments"]] == ["E60"] and full["status"] == "active"
    d = full["dispatches"][0]
    assert (d["id"], d["audio"], d["has_audio"], d["audio_note"]) == (r["dispatch_id"], "pending", False, "")
    assert any(e["kind"] == "dispatch" and "not transmitted (transmit is off" in e["text"] for e in full["events"])
    assert len(cad.pushes) == 1 and cad.dispatch_audio(d["id"]) is None
    assert voice.started.wait(2) and cad.writes == []          # the worker has it meanwhile
    voice.release()
    assert cad.voice.wait(5)
    d = rows(cad, iid)[0]
    assert (d["audio"], d["has_audio"]) == ("ready", True) and d["audio_note"].startswith("Voice")
    assert cad.dispatch_audio(d["id"])[:4] == b"RIFF"
    assert len(cad.writes) == 1 and cad.pushes == ["MainThread", "dispatch-voice"]      # one change, one push each
    assert cad.transmitter.sent[0][:2] == ("FG1", r["text"]) and cad.transmitter.sent[0][2] > 0    # "keyed" with the audio
    assert cad.state()["incidents"][0]["dispatches"][0]["audio"] == "ready"


def test_an_upgrade_and_an_add_on_answer_at_once_too(voice):
    cad = make_cad()
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    add = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    up = cad.upgrade(iid, {"info": "smoke in the basement", "operator": "JD"})
    assert voice.held()                                          # both answered, the voice not let go yet
    assert (add["kind"], add["audio"], up["kind"], up["audio"]) == ("add-on", "pending", "upgrade", "pending")
    assert [d["audio"] for d in rows(cad, iid)] == ["pending"] * 3
    voice.release()
    assert cad.voice.wait(5) and [d["audio"] for d in rows(cad, iid)] == ["ready"] * 3


def test_over_http_the_request_does_not_wait_for_a_slow_voice(voice):
    from sizeup.web.server import WebServer
    cad = make_cad()
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
        iid = call(cad)

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        d = post(f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
        assert voice.held() and d["audio"] == "pending"          # answered while the voice is still being made
        with pytest.raises(urllib.error.HTTPError) as e:              # no audio yet: 404, not a hang
            urllib.request.urlopen(f"{base}/api/cad/dispatch/{d['dispatch_id']}.wav", timeout=5)
        assert e.value.code == 404
        voice.release()
        assert cad.voice.wait(5)
        with urllib.request.urlopen(f"{base}/api/cad/dispatch/{d['dispatch_id']}.wav", timeout=5) as r:
            assert r.read()[:4] == b"RIFF"
    finally:
        ws.shutdown()


def test_two_quick_dispatches_are_rendered_keyed_and_attached_in_the_order_sent(voice):
    cad = make_cad()
    a, b = call(cad), call(cad, "200 Example Ave")
    first = cad.dispatch(a, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    second = cad.dispatch(b, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    third = cad.dispatch(a, {"unit_ids": [uid(cad, "L31")], "operator": "JD"})
    done = []
    was = cad.store.dispatch_voice
    cad.store.dispatch_voice = lambda did, **kw: done.append(did) or was(did, **kw)
    voice.release()
    assert cad.voice.wait(5)
    ids = [first["dispatch_id"], second["dispatch_id"], third["dispatch_id"]]
    assert done == ids                                                           # attached in order
    assert [t for _, t, _ in cad.transmitter.sent] == [first["text"], second["text"], third["text"]]   # keyed in order
    said = [s for s in voice.said if "Example Avenue" in s]
    assert len(said) == 3 and ["Engine sixty" in said[0], "Engine sixty-one" in said[1], "Ladder thirty-one" in said[2]] == [True] * 3
    assert [d["audio"] for d in rows(cad, a) + rows(cad, b)] == ["ready"] * 3


def test_a_voice_that_is_ready_before_the_answer_comes_with_it(monkeypatch):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    cad = make_cad()
    iid = call(cad)
    put = cad.voice.put
    cad.voice.put = lambda job: put(job) and cad.voice.wait(5)       # the worker wins the race (a cached voice)
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert r["audio"] == "ready" and r["audio_url"] == f"/api/cad/dispatch/{r['dispatch_id']}.wav"   # the page plays it now
    assert r["audio_note"].startswith("Voice") and cad.dispatch_audio(r["dispatch_id"])[:4] == b"RIFF"


# -- failure ----------------------------------------------------------------------------------------------------------

def test_no_voice_fails_the_row_with_the_reason_and_the_dispatch_stays_logged(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)                      # no `say` on this machine
    cad = make_cad()
    iid = call(cad)
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert r["audio"] in ("pending", "failed") and r["audio_url"] is None and cad.voice.wait(5)   # (it fails fast)
    d = rows(cad, iid)[0]
    assert (d["audio"], d["has_audio"]) == ("failed", False) and "say" in d["audio_note"]
    assert [a["name"] for a in cad.store.full(iid)["assignments"]] == ["E60"] and len(cad.pushes) == 2
    assert cad.transmitter.sent == [("FG1", r["text"], 0.0)]                     # still "sent": the text, no audio


def test_the_station_voice_not_answering_and_a_crash_fail_the_row_not_the_dispatch(voice):
    cad = make_cad()
    cad.audio_dir = Path(cad.store.path).parent if cad.store.path != ":memory:" else None

    def offline(args):
        raise AudioUnavailable("Voice unavailable — station offline: text only")
    cad.remote_voice = offline                                                   # the hub, its station not there
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert cad.voice.wait(5)
    assert (rows(cad, iid)[0]["audio"], rows(cad, iid)[0]["audio_note"]) == ("failed", "Voice unavailable — station offline: text only")
    cad.remote_voice = lambda args: 1 / 0                                        # anything else going wrong
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert cad.voice.wait(5)
    d = rows(cad, iid)[1]
    assert d["audio"] == "failed" and d["audio_note"].startswith("Voice failed — ZeroDivisionError") and d["audio_note"].endswith("text only")
    cad.remote_voice = None                                                      # and the next one still gets its voice
    voice.release()
    cad.dispatch(iid, {"unit_ids": [uid(cad, "L31")], "operator": "JD"})
    assert cad.voice.wait(5) and rows(cad, iid)[2]["audio"] == "ready"


def test_a_full_queue_fails_the_row_at_once_and_never_blocks(voice):
    cad = make_cad()
    cad.voice = VoiceQueue(cad._voice_job, lambda m: None, size=1)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert voice.started.wait(2)                                                 # being rendered: the queue is empty
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})          # waits its turn
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "L31")], "operator": "JD"})
    assert voice.held() and r["audio"] == "failed" and r["audio_note"] == service.VOICE_BUSY
    assert [a["name"] for a in cad.store.full(iid)["assignments"]] == ["E60", "E61", "L31"]
    voice.release()
    assert cad.voice.wait(5) and [d["audio"] for d in rows(cad, iid)] == ["ready", "ready", "failed"]


# -- preview ----------------------------------------------------------------------------------------------------------

def test_a_preview_is_the_text_only_and_its_audio_an_explicit_second_call(voice):
    cad = make_cad()
    iid = call(cad)
    body = {"unit_ids": [uid(cad, "E60")]}
    pv = cad.preview(iid, body)
    assert voice.said == [] and pv["audio_url"] is None and pv["audio_note"] == ""
    assert pv["text"].startswith("Port Chester Fire, Engine 60, respond to 100 Example Avenue") and pv["tones"]
    cad.dispatch(iid, dict(body, operator="JD"))
    voice.release()
    assert cad.voice.wait(5)
    n = len(voice.said)
    up = cad.upgrade(iid, {"info": "smoke in the basement", "preview": True})
    assert up["audio_url"] is None and len(voice.said) == n and len(rows(cad, iid)) == 1
    heard = cad.upgrade(iid, {"info": "smoke in the basement", "preview": True, "audio": True})
    assert heard["audio_url"].startswith("/api/cad/audio/") and len(voice.said) > n and heard["text"] == up["text"]
    add = cad.preview(iid, {"unit_ids": [uid(cad, "E61")], "audio": True})
    assert add["kind"] == "add_on" and cad.preview_audio(add["audio_url"].rsplit("/", 1)[1][:-4])[:4] == b"RIFF"
    with pytest.raises(service.CadError):
        cad.preview(iid, {"unit_ids": [uid(cad, "E61")], "audio": "yes"})


# -- too late, too slow -----------------------------------------------------------------------------------------------

def test_a_voice_not_started_in_time_is_not_rendered_and_one_render_cannot_hold_the_queue(voice):
    cad = make_cad()
    cad.voice_stale_s = 0.3
    iid = call(cad)
    first = cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert voice.started.wait(2)
    late = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})     # waits behind a slow one
    time.sleep(0.4)
    n = len(voice.said)
    voice.release()
    assert cad.voice.wait(5)
    got = rows(cad, iid)
    assert (got[0]["audio"], got[1]["audio"], got[1]["audio_note"]) == ("ready", "failed", service.VOICE_LATE)
    assert len(voice.said) == n + 1                              # the first one's time out; nothing said for the late one
    assert [t for _, t, _ in cad.transmitter.sent] == [first["text"], late["text"]] and cad.transmitter.sent[1][2] == 0.0
    assert [a["name"] for a in cad.store.full(iid)["assignments"]] == ["E60", "E61"]   # the dispatch: all there
    assert len(service.VOICE_LATE) < 30                          # terse

    slow = make_cad()                                            # one render: its budget, then text only
    slow.voice_budget_s = 0.2
    sid = call(slow)
    voice.gate.clear()
    slow.dispatch(sid, {"unit_ids": [uid(slow, "E60")], "operator": "JD"})
    assert slow.voice.wait(3) and voice.held()                   # given up while the voice is still at it
    d = rows(slow, sid)[0]
    assert (d["audio"], d["audio_note"]) == ("failed", service.VOICE_SLOW)


def test_a_stopped_queue_says_so_and_a_radio_transmitters_line_reads_right(voice):
    from sizeup.cad.transmit import Transmitter, TxResult
    cad = make_cad()
    iid = call(cad)

    class Radio(Transmitter):                                    # M2's shape: only the key-up tells
        name, enabled, keyed = "radio", True, []

        def transmit(self, wav, channel, text):
            self.keyed.append((wav is not None, channel))
            return TxResult(True, "keyed 8.0 s")
    cad.transmitter = Radio()
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    line = next(e["text"] for e in cad.store.full(iid)["events"] if e["kind"] == "dispatch")
    assert line.startswith("Dispatch, to transmit on FG1: Port Chester Fire") and "waiting" not in line
    assert rows(cad, iid)[0]["transmitted"] is False and Radio.keyed == []      # not keyed before its audio
    voice.release()
    assert cad.voice.wait(5) and Radio.keyed == [(True, "FG1")]
    assert rows(cad, iid)[0]["transmitted"] is True
    assert any(e["text"] == "Dispatch #1 transmitted on FG1" for e in cad.store.full(iid)["events"])
    cad.voice.stop(0)
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert (r["audio"], r["audio_note"]) == ("failed", service.VOICE_STOPPING)


# -- stop, start, the fence -------------------------------------------------------------------------------------------

def test_a_drain_leaves_what_it_cannot_finish_for_the_next_start_which_renders_it(voice, tmp_path):
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert voice.started.wait(2)
    cad.stop_voice(0.05)                                         # neither made it: the one rendering, the one queued
    assert [d["audio"] for d in rows(cad, iid)] == ["pending"] * 2 and cad.writes == []
    assert cad.voice.put((1, "x", [], "", 0)) is False           # stopped: nothing new
    cad.may_write = lambda: False                                # ... and the process is gone (the lease with it)
    voice.release()
    assert cad.voice.wait(5)
    said = []
    again = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=said.append)       # the next task, seconds later
    assert [d["audio"] for d in rows(again, iid)] == ["pending"] * 2            # young: not failed at the open
    wavs = sorted(p.name for p in cad.audio_dir.glob("*.wav"))                  # what the first process rendered
    assert again.resume_voice() == 2 and again.voice.wait(5)
    got = rows(again, iid)
    assert [(d["audio"], d["has_audio"]) for d in got] == [("ready", True)] * 2
    assert any("2 dispatch voice(s) left by a restart" in m for m in said)
    # the same words and tones as the first time: the first one's WAV is the one already there
    assert Path(again.store.dispatch_row(got[0]["id"])["audio_path"]).name in wavs
    assert again.dispatch_audio(got[1]["id"])[:4] == b"RIFF"
    assert [t for _, t, _ in again.transmitter.sent] == [d["text"] for d in got]     # in the order sent
    other = make_cad()                                           # in time: ready at the stop
    oid = call(other)
    other.dispatch(oid, {"unit_ids": [uid(other, "E60")], "operator": "JD"})
    other.stop_voice(5)
    assert rows(other, oid)[0]["audio"] == "ready"


def test_a_start_fails_the_rows_left_pending_too_long_ago(voice, tmp_path):
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert voice.started.wait(2) and rows(cad, iid)[0]["audio"] == "pending"     # the process dies here
    cad.may_write = lambda: False
    said = []
    later = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=said.append, clock=lambda: time.time() + 60)
    d = rows(later, iid)[0]
    assert (d["audio"], d["has_audio"], d["audio_note"]) == ("failed", False, service.VOICE_RESTARTED)
    assert any("1 dispatch(es) left waiting for their voice" in m for m in said) and later.resume_voice() == 0
    assert [a["name"] for a in later.store.full(iid)["assignments"]] == ["E60"]   # the dispatch itself: all there
    # young at the open, too old by the time the voice is wired: failed then, not rendered (the fenced process
    # writes nothing at all, #501: the next one dispatches it)
    later.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    now = [time.time() + 60]                                     # (later's clock)
    third = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=said.append, clock=lambda: now[0])
    assert rows(third, iid)[1]["audio"] == "pending"
    now[0] += 60
    assert third.resume_voice() == 0 and rows(third, iid)[1]["audio_note"] == service.VOICE_RESTARTED


def test_a_hub_that_lost_the_lease_keys_and_writes_nothing(voice, tmp_path):
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert voice.started.wait(2)
    cad.may_write = lambda: False                                # fenced while the voice was rendering
    voice.release()
    assert cad.voice.wait(5)
    assert rows(cad, iid)[0]["audio"] == "pending" and cad.writes == [] and len(cad.pushes) == 1
    assert cad.transmitter.sent == []                            # checked before the key-up, not after
    cad.may_write = lambda: True
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert cad.voice.wait(5)
    cad.store.fail_pending_voice("gone", [rows(cad, iid)[0]["id"]])
    n = len(cad.transmitter.sent)
    cad._voice_job((rows(cad, iid)[0]["id"], "x", [], "FG1", time.monotonic() + 60))     # its row no longer pending
    assert len(cad.transmitter.sent) == n and rows(cad, iid)[0]["audio_note"] == "gone"


def test_the_audio_blobs_are_the_writers_only(tmp_path):
    cad = make_cad(tmp_path)

    class Blobs:                                                 # the hub's audio.sqlite (sizeup/cloud/hubstore.py)
        puts = []

        def get(self, key): return None
        def note(self, key): return ""
        def put(self, key, wav, kind, label): self.puts.append((kind, label))
        def prune_dispatch(self, keep, cutoff): pass
    cad.audio_blobs = Blobs()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\0\0" * 1600)
    riff = buf.getvalue()
    back = threading.Event()

    def remote(args):
        back.wait(5)
        return riff, "Voice: Kokoro am_michael"                  # the station's voice, as the hub gets it
    cad.remote_voice = remote
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})        # logged by the leader ...
    cad.may_write = lambda: False                                # ... fenced before its voice is back (#501: the
    back.set()                                                   # fenced process writes nothing at all)
    assert cad.voice.wait(5) and Blobs.puts == [] and rows(cad, iid)[0]["audio"] == "pending"
    # (the next leader's start renders it while young: resume_voice). A dispatch on the fenced task itself: refused out
    # loud (the web server's 503: the console sends it again), never logged as sent
    with pytest.raises(FencedWrite):
        cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert len(rows(cad, iid)) == 1
    cad.may_write = lambda: True
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert cad.voice.wait(5) and Blobs.puts == [("dispatch", "Voice: Kokoro am_michael")]
    assert rows(cad, iid)[1]["audio"] == "ready"


def test_an_older_database_gets_the_column_and_its_rows_read_as_before(tmp_path, voice):
    voice.release()
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert cad.voice.wait(5)
    old = tmp_path / "old.sqlite"                                # a copy (the live one has threads on it), made older
    db = sqlite3.connect(old)
    with cad.store.lock:
        cad.store.db.backup(db)
    db.execute("INSERT INTO dispatches (incident_id, at, operator, text, audio_note) VALUES (?, 1, 'JD', 'no voice then', 'no say')", (iid,))
    db.execute("ALTER TABLE dispatches DROP COLUMN audio_state")           # as a release before #426 left it
    db.execute("ALTER TABLE dispatches DROP COLUMN audio_tones")
    db.commit()
    db.close()
    again = Cad(Settings(db=str(old), audio_dir=str(cad.audio_dir)), log=lambda m: None)
    got = sorted((d["text"] == "no voice then", d["audio"], d["has_audio"]) for d in rows(again, iid))
    assert got == [(False, "ready", True), (True, "", False)]


# -- the page ---------------------------------------------------------------------------------------------------------

def js_function(src, name):
    m = re.search(rf"^(?:async )?function {name}\(", src, re.M)
    assert m, name
    i = src.index("{", src.index(")", m.end()))
    depth, j = 1, i + 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        j += 1
    return src[m.start():j]


PAGE = r"""
// The dispatch box and the voices waited for, driven as the page drives them: dispatches answered "pending", then
// state pushes. Players are fakes that record what they play; "ended" is fired by hand.
const played = [];
class FakeAudio {
  constructor(name) { this.name = name || "unseen"; this.src = ""; this.paused = true; this.hidden = true; this.ls = {}; }
  addEventListener(e, f) { (this.ls[e] = this.ls[e] || new Set()).add(f); }
  removeEventListener(e, f) { if (this.ls[e]) this.ls[e].delete(f); }
  fire(e) { this.paused = true; for (const f of [...(this.ls[e] || [])]) f(); }
  play() { played.push(`${this.name}:${this.src}`); this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
  removeAttribute(k) { this[k] = ""; }
}
globalThis.Audio = FakeAudio;
const nodes = { "#r-audio": new FakeAudio("box") };
const $ = (id) => nodes[id] || (nodes[id] = { id, hidden: true, textContent: "", title: "", className: "",
  removeAttribute(k) { this[k] = ""; } });
const setText = (n, t) => { n.textContent = t ?? ""; };
const show = (n, on) => { n.hidden = !on; };
const cad = { state: { incidents: [] }, voiceWaits: new Map(), boxDispatch: null };
eval(process.argv[2].replace(/^(const|let) /gm, "var "));     // (top-level names, as in the page)
const tick = () => new Promise((r) => setImmediate(r));
const box = () => ({ audio: !nodes["#r-audio"].hidden, note: $("#r-audio-note").textContent, tip: $("#r-audio-note").title,
                     title: $("#r-preview-title").textContent, played: played.slice(), waiting: [...cad.voiceWaits.keys()] });
const row = (id, audio, extra = {}) => ({ id, audio, has_audio: audio === "ready", audio_note: "", ...extra });
const push = async (rows, now) => { cad.state = { incidents: rows.length ? [{ id: 3, dispatches: rows }] : [] }; syncVoice(now); await tick(); };
const sent = (id) => showSent(`Dispatch #${id}: logged, not transmitted`, { dispatch_id: id, audio: "pending", audio_url: null, audio_note: "", text: "Port Chester Fire" });
const fresh = () => { played.length = 0; cad.voiceWaits.clear(); voiceAudio.fire("ended"); nodes["#r-audio"].fire("ended"); };
(async () => {
  const out = {};
  sent(7);
  out.sent = box();
  await push([row(7, "pending")]);
  out.pending = box();
  await push([row(7, "ready", { audio_note: "Voice: Polly Matthew" })]);
  out.ready = box();
  await push([row(7, "ready", { audio_note: "Voice: Polly Matthew" })]);
  await push([row(7, "ready", { audio_note: "Voice: Polly Matthew" })]);
  out.again = box();

  fresh(); await tick();                     // another event selected (or a preview started) before it came: still played
  sent(9);
  show($("#r-preview-box"), false); cad.boxDispatch = null;
  await push([row(9, "pending")]);
  await push([row(9, "ready")]);
  await push([row(9, "ready")]);
  out.elsewhere = box();

  fresh(); await tick();                     // two sent, both there in one push: one after the other, in order
  sent(11); sent(12);
  await push([row(11, "ready"), row(12, "ready", { audio_note: "Voice: Polly Matthew" })]);
  out.first = box();
  voiceAudio.fire("ended"); await tick();
  out.second = box();

  fresh(); await tick();                     // too late: the player is there, nothing plays by itself
  sent(13);
  await push([row(13, "pending")]);
  await push([row(13, "ready", { audio_note: "Voice: Polly Matthew" })], Date.now() + 46000);
  out.stale = box();

  fresh(); await tick();                     // failed: why, no player
  sent(14);
  await push([row(14, "failed", { audio_note: "Voice unavailable — station offline: text only" })]);
  out.failed = box();

  fresh(); await tick();                     // its event left the live state while pending: "Voice…" goes
  sent(15);
  await push([row(15, "pending")]);
  await push([]);
  out.gone = box();

  fresh(); await tick();                     // not in the state yet (the push is on its way): still waited for
  sent(16);
  await push([]);
  out.early = box();
  await push([], Date.now() + 46000);        // ... but not for ever
  out.never = box();

  showSent("Dispatch #17: logged, not transmitted", { dispatch_id: 17, audio: "failed", audio_url: null, audio_note: "Voice not rendered", text: "x" });
  out.refused = box();
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_plays_each_voice_it_waits_for_once_in_order_whatever_is_selected(tmp_path):
    js = (STATIC / "cad.js").read_text()
    consts = "\n".join(re.search(rf"^(?:const|let) {n} = [^\n]*;", js, re.M).group(0)
                       for n in ("VOICE_PENDING", "VOICE_PENDING_TIP", "VOICE_STALE_MS", "voiceAudio", "voiceChain"))
    code = consts + "\n" + "\n".join(js_function(js, n) for n in ("voicePending", "voiceArrived", "showAudio", "voicePlay",
                                                                     "syncVoice", "showPreview", "showSent"))
    h = tmp_path / "page.js"
    h.write_text(PAGE)
    out = json.loads(subprocess.run([NODE, str(h), code], capture_output=True, text=True, check=True, timeout=30).stdout)
    # at once: the dispatch, no player, "Voice…" with the explanation in its hover
    assert out["sent"]["title"] == "Dispatch #7: logged, not transmitted" and out["sent"]["waiting"] == [7]
    assert (out["sent"]["audio"], out["sent"]["note"], out["sent"]["played"]) == (False, "Voice…", [])
    assert "rendered" in out["sent"]["tip"] and len(out["sent"]["note"]) < 10
    assert out["pending"] == out["sent"]                                    # a push with the row still pending: nothing
    # the state brings it: the box's player, played once, the voice's note; later pushes play nothing again
    assert out["ready"] == dict(out["sent"], audio=True, note="Voice: Polly Matthew", tip="", waiting=[],
                                played=["box:/api/cad/dispatch/7.wav"])
    assert out["again"] == out["ready"]
    # the box gone to another event or a preview: played all the same, once, by the unseen player
    assert out["elsewhere"]["played"] == ["unseen:/api/cad/dispatch/9.wav"] and out["elsewhere"]["waiting"] == []
    # two voices at once: the first, then (when it ends) the second, which is the box's
    assert out["first"]["played"] == ["unseen:/api/cad/dispatch/11.wav"] and out["first"]["note"] == "Voice…"
    assert out["second"]["played"] == ["unseen:/api/cad/dispatch/11.wav", "box:/api/cad/dispatch/12.wav"]
    assert (out["second"]["audio"], out["second"]["note"]) == (True, "Voice: Polly Matthew")
    # too late (45 s): there to be played by hand, never by itself
    assert (out["stale"]["audio"], out["stale"]["played"], out["stale"]["waiting"]) == (True, [], [])
    # failed: no player, why
    assert (out["failed"]["audio"], out["failed"]["note"], out["failed"]["played"], out["failed"]["waiting"]) == (
        False, "Voice unavailable — station offline: text only", [], [])
    # the event left the live state: no "Voice…" left behind; one not in the state yet is waited for, 45 s at most
    assert (out["gone"]["note"], out["gone"]["tip"], out["gone"]["waiting"], out["gone"]["played"]) == ("", "", [], [])
    assert (out["early"]["note"], out["early"]["waiting"]) == ("Voice…", [16])
    assert (out["never"]["note"], out["never"]["waiting"]) == ("", [])
    assert (out["refused"]["note"], out["refused"]["waiting"], out["refused"]["played"]) == ("Voice not rendered", [], [])


def test_the_page_wires_it_to_the_state_and_asks_for_a_previews_audio_separately():
    js = (STATIC / "cad.js").read_text()
    render = js_function(js, "render")
    assert "syncVoice();" in render                                         # every state push
    assert "showSent(" in js_function(js, "dispatch") and "showSent(" in js_function(js, "upgrade")
    then = js_function(js, "showPreviewThenAudio")
    assert then.index('await api("POST", path, body)') < then.index("{ ...body, audio: true }")    # text, then audio
    assert "showPreviewThenAudio(" in js_function(js, "preview")
    # the log's row and the Timeline's play button follow the row's audio (their lists re-fill on it)
    assert 'd.audio === "pending"' in js_function(js, "renderDispatches") and "${d.audio" in js_function(js, "renderDispatches")
    assert "playForEvent(inc, e)" in js_function(js, "renderEvents").split("sig:")[1].split("\n")[0]
    # selecting an event or starting a preview takes the box, never a voice still waited for
    assert "voiceWaits" not in js_function(js, "selectIncident") and "voiceWaits" not in then
    assert "cad.boxDispatch = null;" in js_function(js, "selectIncident") and "cad.boxDispatch = null;" in then
