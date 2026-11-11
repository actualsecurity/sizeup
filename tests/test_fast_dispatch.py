"""A dispatch answers before its voice is rendered (#426).

Production, 24 h of the access log: POST .../dispatch took 2.6-3.0 s and .../upgrade about 2.3 s, nearly everything
else 1-20 ms: Cad._send rendered the voice (Polly, or the station's) before it logged the dispatch and answered.

    at once      the units are assigned, the dispatch is logged ("pending") and the state pushed; no audio waited for
    the voice    a job on one worker (sizeup/cad/voicequeue.py), in the order sent: rendered, keyed, attached to its
                 row (ready | failed, with the same notes as before), one more push
    failure      no voice, an error, a full queue: the row is failed with why; the dispatch stays logged
    preview      the text only; {"audio": true} renders it (the page's second call)
    stop/start   a drain gives the voices a moment, then fails what is left; a start fails rows left pending
    the fence    not the writer any more: nothing is written (the next leader's start fails the row)
    the page     shows the dispatch with "Voice…", plays the audio once when the state brings it
"""

import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import dispatcher, service
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.cad.voicequeue import VoiceQueue
from sizeup.cad.dispatcher import AudioUnavailable

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
FAST_S = 0.2            # the goal: "well under 200 ms"


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
    assert took < FAST_S, f"the dispatch waited {took:.2f} s"
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
    add, t_add = timed(lambda: cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"}))
    up, t_up = timed(lambda: cad.upgrade(iid, {"info": "smoke in the basement", "operator": "JD"}))
    assert t_add < FAST_S and t_up < FAST_S
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
        d, took = timed(lambda: post(f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [uid(cad, "E60")], "operator": "JD"}))
        assert took < FAST_S and d["audio"] == "pending"
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
    assert r["audio"] == "pending" and cad.voice.wait(5)
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
    r, took = timed(lambda: cad.dispatch(iid, {"unit_ids": [uid(cad, "L31")], "operator": "JD"}))
    assert took < FAST_S and r["audio"] == "failed" and r["audio_note"] == service.VOICE_BUSY
    assert [a["name"] for a in cad.store.full(iid)["assignments"]] == ["E60", "E61", "L31"]
    voice.release()
    assert cad.voice.wait(5) and [d["audio"] for d in rows(cad, iid)] == ["ready", "ready", "failed"]


# -- preview ----------------------------------------------------------------------------------------------------------

def test_a_preview_is_the_text_only_and_its_audio_an_explicit_second_call(voice):
    cad = make_cad()
    iid = call(cad)
    body = {"unit_ids": [uid(cad, "E60")]}
    pv, took = timed(lambda: cad.preview(iid, body))
    assert took < FAST_S and voice.said == [] and pv["audio_url"] is None and pv["audio_note"] == ""
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


# -- stop, start, the fence -------------------------------------------------------------------------------------------

def test_a_drain_finishes_the_voice_it_can_and_fails_the_rest(voice, tmp_path):
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert voice.started.wait(2)
    cad.stop_voice(0.05)                                         # neither made it: the one rendering, the one queued
    got = rows(cad, iid)
    assert [(d["audio"], d["audio_note"]) for d in got] == [("failed", service.VOICE_RESTARTED)] * 2
    voice.release()                                              # the render ends late: its row is not touched again
    assert cad.voice.wait(5) and rows(cad, iid) == got and cad.writes == [] and len(cad.pushes) == 2
    assert cad.voice.put((1, "x", [], "")) is False              # stopped: nothing new
    other = make_cad()                                           # in time: ready, nothing failed
    oid = call(other)
    other.dispatch(oid, {"unit_ids": [uid(other, "E60")], "operator": "JD"})
    other.stop_voice(5)
    assert rows(other, oid)[0]["audio"] == "ready"


def test_a_start_fails_the_rows_a_dead_process_left_pending(voice, tmp_path):
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert voice.started.wait(2) and rows(cad, iid)[0]["audio"] == "pending"     # the process dies here
    said = []
    again = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=said.append)
    d = rows(again, iid)[0]
    assert (d["audio"], d["has_audio"], d["audio_note"]) == ("failed", False, service.VOICE_RESTARTED)
    assert any("1 dispatch(es) left waiting for their voice" in m for m in said)
    assert [a["name"] for a in again.store.full(iid)["assignments"]] == ["E60"]   # the dispatch itself: all there


def test_a_hub_that_lost_the_lease_writes_nothing(voice, tmp_path):
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert voice.started.wait(2)
    cad.may_write = lambda: False                                # fenced while the voice was rendering
    voice.release()
    assert cad.voice.wait(5)
    assert rows(cad, iid)[0]["audio"] == "pending" and cad.writes == [] and len(cad.pushes) == 1
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    cad.stop_voice(0)                                            # and nothing at its stop either
    assert [d["audio"] for d in rows(cad, iid)] == ["pending"] * 2


def test_an_older_database_gets_the_column_and_its_rows_read_as_before(tmp_path, voice):
    voice.release()
    cad = make_cad(tmp_path)
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert cad.voice.wait(5)
    cad.store.db.close()
    db = sqlite3.connect(tmp_path / "cad.sqlite")
    db.execute("INSERT INTO dispatches (incident_id, at, operator, text, audio_note) VALUES (?, 1, 'JD', 'no voice then', 'no say')", (iid,))
    db.execute("ALTER TABLE dispatches DROP COLUMN audio_state")           # as a release before #426 left it
    db.commit()
    db.close()
    again = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=lambda m: None)
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
// The dispatch box's audio, driven as the page drives it: a dispatch answered "pending", then state pushes.
const played = [];
const nodes = {};
const node = (id) => nodes[id] || (nodes[id] = { id, hidden: true, textContent: "", title: "", className: "", paused: true, src: "",
  removeAttribute(k) { this[k] = ""; }, play() { played.push(this.src); this.paused = false; return Promise.resolve(); },
  pause() { this.paused = true; } });
const $ = node;
const setText = (n, t) => { n.textContent = t ?? ""; };
const show = (n, on) => { n.hidden = !on; };
const cad = { state: { incidents: [] }, voiceWait: null };
eval(process.argv[2]);
const box = () => ({ audio: !nodes["#r-audio"].hidden, note: nodes["#r-audio-note"].textContent, tip: nodes["#r-audio-note"].title,
                     title: nodes["#r-preview-title"].textContent, played: played.slice(), waiting: !!cad.voiceWait });
const out = {};
const row = (audio, extra = {}) => ({ id: 7, audio, has_audio: audio === "ready", audio_note: "", ...extra });
const push = (d) => { cad.state = { incidents: [{ id: 3, dispatches: [{ id: 6, audio: "ready", has_audio: true }, d] }] }; syncVoice(); };
showSent("Dispatch #7: logged, not transmitted", { dispatch_id: 7, audio: "pending", audio_url: null, audio_note: "", text: "Port Chester Fire" });
out.sent = box();
push(row("pending"));
out.pending = box();
push(row("ready", { audio_note: "Voice: Polly Matthew" }));
out.ready = box();
push(row("ready", { audio_note: "Voice: Polly Matthew" }));
push(row("ready", { audio_note: "Voice: Polly Matthew" }));
out.again = box();
played.length = 0;
showSent("Dispatch #9: logged, not transmitted", { dispatch_id: 9, audio: "pending", audio_url: null, audio_note: "", text: "x" });
push(row("failed", { id: 9, audio_note: "Voice unavailable — station offline: text only" }));
out.failed = box();
showSent("Dispatch #8: logged, not transmitted", { dispatch_id: 8, audio: "failed", audio_url: null, audio_note: "Voice not rendered", text: "x" });
out.refused = box();
out.arrived = [voiceArrived(null, cad.state.incidents), voiceArrived({ id: 99 }, cad.state.incidents),
               voiceArrived({ id: 6 }, cad.state.incidents)];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_shows_the_dispatch_at_once_and_plays_its_voice_once_when_the_state_brings_it(tmp_path):
    js = (STATIC / "cad.js").read_text()
    consts = "\n".join(re.search(rf"^const {n} = [^\n]*;", js, re.M).group(0) for n in ("VOICE_PENDING", "VOICE_PENDING_TIP"))
    code = consts + "\n" + "\n".join(js_function(js, n) for n in ("voicePending", "voiceArrived", "showAudio", "syncVoice",
                                                                     "showPreview", "showSent"))
    h = tmp_path / "page.js"
    h.write_text(PAGE)
    out = json.loads(subprocess.run([NODE, str(h), code], capture_output=True, text=True, check=True, timeout=30).stdout)
    # at once: the dispatch, no player, "Voice…" with the explanation in its hover
    assert out["sent"]["title"] == "Dispatch #7: logged, not transmitted" and out["sent"]["waiting"]
    assert (out["sent"]["audio"], out["sent"]["note"], out["sent"]["played"]) == (False, "Voice…", [])
    assert "rendered" in out["sent"]["tip"] and len(out["sent"]["note"]) < 10
    assert out["pending"] == out["sent"]                                    # a push with the row still pending: nothing
    # the state brings it: the player, played once, the voice's note; later pushes play nothing again
    assert out["ready"] == dict(out["sent"], audio=True, note="Voice: Polly Matthew", tip="", played=["/api/cad/dispatch/7.wav"],
                                waiting=False)
    assert out["again"] == out["ready"]
    # failed: no player, why; a dispatch refused its voice at once is never waited for
    assert (out["failed"]["audio"], out["failed"]["note"], out["failed"]["played"], out["failed"]["waiting"]) == (
        False, "Voice unavailable — station offline: text only", [], False)
    assert (out["refused"]["note"], out["refused"]["waiting"], out["refused"]["played"]) == ("Voice not rendered", False, [])
    assert out["arrived"] == [None, None, {"url": "/api/cad/dispatch/6.wav", "note": ""}]


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
    assert "cad.voiceWait = null;" in js_function(js, "selectIncident")     # another event: not played into a hidden box
