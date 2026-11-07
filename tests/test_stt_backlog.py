"""The station's whisper falling 60-80 min behind (GitHub #300): strict priority on the whisper queue, the context
talkgroups shed when it is behind, one greedy pass for them meanwhile, AWS timeouts that don't flood whisper, the
thread cap, and a "Speech-to-text behind" alert that is one notice per episode with its cause. Local fakes only."""

import base64
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np

from sizeup import stt as stt_mod
from sizeup.stt import (BEHIND_S, HIGH, LIVE, LOW, LOW_MAX_WAIT_S, RERUN, STALE_S, URGENT, SttQueue, Transcriber,
                        WhisperClient)
from tests.test_health import T0, Rig, station_status
from tests.test_transcribe import fake_creds  # noqa: F401 (the autouse fixture: test credentials only, never AWS)


def q_at(clock):
    dropped = []
    return SttQueue(lambda item, why: dropped.append((item, why)), clock=lambda: clock["t"]), dropped


# -- the queue: strict priority ------------------------------------------------------------------------------------


def test_a_class_is_served_only_when_every_class_above_it_is_empty_stale_or_not():
    clock = {"t": 1000.0}
    q, _ = q_at(clock)
    q.put((URGENT, None, "page-old", None, "South 2"))
    q.put((LOW, None, "context-old", None, "tg:383"))
    clock["t"] += STALE_S + 5                       # both have waited long: a stale page still goes first
    for prio, item, ch in ((LOW, "context-new", "tg:383"), (RERUN, "rerun", None), (LIVE, "live", "tg:333"),
                           (HIGH, "tower", "HPN Tower"), (URGENT, "fe15", "tg:307")):
        q.put((prio, None, item, None, ch))
    order = [q.get_nowait()[2] for _ in range(7)]
    assert order == ["fe15", "page-old", "tower", "live", "rerun", "context-new", "context-old"]


def test_context_talkgroups_are_shed_once_they_waited_too_long_and_say_so():
    clock = {"t": 1000.0}
    q, dropped = q_at(clock)
    q.put((LOW, None, "context", None, "tg:383"))
    q.put((LIVE, None, "live", None, "tg:333"))
    clock["t"] += LOW_MAX_WAIT_S + 1
    assert q.stats()["oldest_s"] == LOW_MAX_WAIT_S + 1 and q.stats()["oldest_low_s"] == LOW_MAX_WAIT_S + 1
    assert q.get_nowait()[2] == "live"                                   # a live clip is never shed this early
    assert dropped == [("context", dropped[0][1])] and dropped[0][1].startswith("skipped: busy") \
        and "context talkgroup" in dropped[0][1]
    st = q.stats()
    assert st["shed"] == 1 and st["skipped"] == 0 and st["queued"] == 0


def test_the_queue_counts_what_comes_in_from_where_and_the_aws_fallback():
    clock = {"t": 1000.0}
    q, _ = q_at(clock)
    for i in range(6):
        q.put((LOW, None, f"c{i}", None, "tg:383"))
    q.put((URGENT, None, "p", None, "South 2"), fallback=True)
    q.put((RERUN, None, "r", None))                                      # a person's re-run: not traffic coming in
    st = q.stats()
    assert st["in_h"] == 14 and st["in_low_h"] == 12 and st["in_fallback_h"] == 2 and st["fallback"] == 1
    assert st["in_top"][0] == ("tg:383", 12) and st["top"][0] == ("tg:383", 6)
    assert st["live"] == 1 and st["low"] == 6 and st["urgent"] == 1
    assert not q.behind()
    clock["t"] += BEHIND_S
    assert q.behind()


class Recorder:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, prompt="", **kw):
        self.calls.append(kw.get("fast", False))
        return "words", 0.9


def test_behind_a_context_talkgroup_gets_one_greedy_pass_and_the_pace_is_measured(monkeypatch):
    clock = {"t": 1000.0}
    rec, done = Recorder(), []
    tr = Transcriber(rec, done.append)
    tr._q.clock = lambda: clock["t"]
    item = lambda: SimpleNamespace(fm=False, low_energy=False)          # noqa: E731
    tr.submit(item(), np.zeros(16000, np.float32), LOW, "tg:383")
    tr.submit(item(), np.zeros(16000, np.float32), LOW, "tg:383")
    tr.submit(item(), np.zeros(16000, np.float32), URGENT, "tg:307")
    clock["t"] += BEHIND_S + 1                                           # they have waited: behind
    tr.start()
    deadline = time.time() + 10
    while len(done) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert rec.calls == [False, True, False]   # the page as always; the first context clip while one more still waits
    assert tr.stats()["service_s"] is not None


def test_the_greedy_pass_asks_whisper_server_for_no_temperature_fallback(monkeypatch):
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"text": "x", "segments": []}).encode()

    def urlopen(req, timeout=None):
        sent.append(req.data)
        return Resp()
    monkeypatch.setattr(stt_mod.urllib.request, "urlopen", urlopen)
    c = WhisperClient("http://127.0.0.1:1/inference")
    c.transcribe(np.zeros(1600, np.float32))
    c.transcribe(np.zeros(1600, np.float32), fast=True)
    assert b'name="temperature_inc"' not in sent[0] and b'name="temperature_inc"\r\n\r\n0.0' in sent[1]


# -- the classes, from the channel -----------------------------------------------------------------------------


def test_each_channel_gets_its_class(tmp_path, monkeypatch):
    from sizeup import app as app_mod
    from sizeup.app import App, Rerun
    from sizeup.incidents import Traffic
    from tests.test_app import cfg
    app = App(replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15", 333: "Airport Incident", 383: "WCPD Ch 1"},
                      p25_incident_talkgroups=(307, 333)))
    monkeypatch.setattr(app_mod, "AIRBAND_FIRST", {"HPN Tower"})
    t = Traffic("x", "src", 0.0, 1.0, "")
    assert app._stt_priority(SimpleNamespace(kind="page"), "South 2") == URGENT
    assert app._stt_priority(t, "tg:307") == URGENT and app._stt_priority(t, "FG1") == URGENT
    assert app._stt_priority(t, "HPN Tower") == HIGH
    assert app._stt_priority(t, "tg:333") == LIVE                          # a main talkgroup
    assert app._stt_priority(t, "tg:383") == LOW                           # context only
    app.tg.set(383, main=True)
    assert app._stt_priority(t, "tg:383") == LIVE                          # made main in Settings > Radios
    assert app._stt_priority(Rerun("p", "page", True), "") == URGENT


def test_whisper_never_gets_more_threads_than_the_cpus_less_one(monkeypatch):
    from sizeup import app as app_mod
    monkeypatch.setattr(app_mod, "cpus", lambda: 4)
    assert app_mod.WhisperService("w", "m", 1, 8).argv[-1] == "3"
    assert app_mod.WhisperService("w", "m", 1, 2).argv[-1] == "2"
    assert app_mod.WhisperService("w", "m", 1, 0).argv[-1] == "3"


def test_readsb_runs_below_whisper():
    from pathlib import Path
    unit = (Path(__file__).parent.parent / "deploy" / "station" / "systemd" / "sizeup-adsb.service").read_text()
    assert "Nice=5" in unit and "CPUWeight=50" in unit


# -- the hub: an AWS timeout doesn't flood the station's whisper ----------------------------------------------------


def _event(tid, source, seq):
    from sizeup.cad.dispatcher import wav_bytes
    from tests.test_app import voice
    now = time.time()
    return {"seq": seq, "at": now, "type": "traffic",
            "data": {"id": tid, "source": source, "start": now - 3, "end": now, "text": "", "status": "remote",
                     "transcript_pending": "aws", "stt": "aws", "model": "aws-transcribe"},
            "audio": base64.b64encode(wav_bytes((voice(3.0)[::2] * 32767).astype(np.int16), 8000)).decode()}


def test_an_aws_timeout_falls_back_to_whisper_for_pages_fire_ems_15_firegrounds_and_airband(monkeypatch):
    from sizeup.app import AwsJob
    from sizeup.cloud import hub as hub_mod
    monkeypatch.setattr(hub_mod, "AIRBAND_FIRST", {"HPN Tower"})
    ok = hub_mod.HubApp._whisper_fallback
    assert ok(AwsJob("page", "p", "South 2")) and ok(AwsJob("traffic", "t", "tg:307"))
    assert ok(AwsJob("traffic", "t", "FG2")) and ok(AwsJob("traffic", "t", "HPN Tower"))
    assert not ok(AwsJob("traffic", "t", "Marine 16"))


def test_on_the_hub_an_aws_timeout_goes_to_whisper_only_for_priority_channels(tmp_path):
    from sizeup.app import AwsJob
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_channels import aws_cfg
    from tests.test_cloud import HubRun, call, hub_cfg
    from tests.test_transcribe import wait
    from tests.test_transcribe_watchdog import _row
    fake = FakeTranscribe()
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake, stt_aws_channels=("South 2", "FG1", "FG2", "tg:307", "Marine 16"))))
    asked = []
    try:
        hub.app.aws.submit = lambda *a, **k: None                       # AWS never answers in time
        hub.app.hub.remote = lambda name, args, timeout=0: asked.append((name, list(args))) or 1
        events = [_event("M16-1", "Marine 16", 1), _event("FG1-1", "FG1", 2)]
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": events})[0] == 200
        assert wait(lambda: all((_row(hub, t) or {}).get("status") == "pending" for t in ("M16-1", "FG1-1")), 5)
        hub.app._aws_failed(AwsJob("traffic", "M16-1", "Marine 16"), "AWS timed out: no answer in 60 s")
        hub.app._aws_failed(AwsJob("traffic", "FG1-1", "FG1"), "AWS timed out: no answer in 60 s")
        assert wait(lambda: asked == [("transcribe_now", ["FG1-1"])], 5)  # the fireground: the station's whisper
        time.sleep(0.2)
        assert asked == [("transcribe_now", ["FG1-1"])]                 # the airband clip: not
        assert "M16-1" in hub.app._aws_late_wait
        # AWS's late answer is its text, not an alternative
        hub.app._aws_late(AwsJob("traffic", "M16-1", "Marine 16"), "late words", 0.8)
        assert wait(lambda: (_row(hub, "M16-1") or {}).get("text") == "late words", 5)
        assert _row(hub, "M16-1")["status"] == "done" and not _row(hub, "M16-1").get("alt_text")
        # no late answer at all: flagged after FALLBACK_WAIT_S like the rest, saying why
        events = [_event("M16-2", "Marine 16", 3)]
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": events})[0] == 200
        assert wait(lambda: (_row(hub, "M16-2") or {}).get("status") == "pending", 5)
        hub.app._aws_failed(AwsJob("traffic", "M16-2", "Marine 16"), "AWS timed out: no answer in 60 s")
        hub.app._aws_asked["M16-2"] -= 1000
        hub.app._aws_sweep()
        r = _row(hub, "M16-2")
        assert r["status"] == "error" and "no late answer" in r["model"]
        assert ("transcribe_now", ["M16-2"]) not in asked
        # left pending at a shutdown (#292's drain) and recovered at the next start: whisper as before, any channel
        events = [_event("M16-3", "Marine 16", 4)]
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": events})[0] == 200
        assert wait(lambda: (_row(hub, "M16-3") or {}).get("status") == "pending", 5)
        hub.app._aws_failed(AwsJob("traffic", "M16-3", "Marine 16", recovered=True),
                            "AWS timed out: the hub restarted (waiting over 60 min)")
        assert wait(lambda: ("transcribe_now", ["M16-3"]) in asked, 10)
        assert "M16-3" not in hub.app._aws_late_wait
    finally:
        hub.close()
        fake.close()


# -- the alert: one notice per episode, with its cause -------------------------------------------------------------


def backlog(oldest_s, **kw):
    st = station_status()
    st["health"]["stt_queue"] = dict({"queued": 312, "urgent": 0, "live": 20, "low": 292, "rerun": 0, "oldest_s": oldest_s,
                                      "service_s": 16.4, "in_h": 290, "in_fallback_h": 0,
                                      "in_top": [["tg:383", 120], ["tg:105", 50]], "dropped": 0, "skipped": 0,
                                      "max": 2000}, **kw)
    return st


def test_the_backlog_alert_is_one_push_per_episode_with_its_cause_and_hysteresis():
    r = Rig()
    r.hub.app = SimpleNamespace(tg=SimpleNamespace(name=lambda tg: {383: "WCPD Ch 1"}.get(tg, f"TG {tg}")))
    r.at(0, status=backlog(300))
    assert r.keys() == []
    r.at(310, status=backlog(610))                                        # 10 min: an alert on the panel ...
    assert r.keys() == ["stt_backlog"] and r.n.msgs == []                 # ... not pushed yet
    a = r.h.alerts()[0]
    assert a["text"] == "312 clips waiting; cause: 290 clips/h in, 220/h out (16 s each), most from WCPD Ch 1 (41%)"
    r.at(910, status=backlog(1210))                                       # 20 min: pushed, once
    assert len(r.n.msgs) == 1 and r.n.msgs[0]["title"].startswith("Speech-to-text behind 20 min")
    assert "most from WCPD Ch 1" in r.n.msgs[0]["body"]
    r.at(1000, status=backlog(60))                                        # caught up ...
    r.at(1300, status=backlog(700))                                       # ... and behind again: the same episode
    r.at(1400, status=backlog(30))
    r.at(1400 + 899, status=backlog(30))
    assert r.keys() == ["stt_backlog"] and len(r.n.msgs) == 1             # under 2 min, but not for 15 min yet
    r.at(1400 + 900, status=backlog(30))
    assert r.keys() == [] and len(r.n.msgs) == 2
    assert r.n.msgs[1]["title"].startswith("Recovered: Speech-to-text behind")
    assert r.h.history[-1]["cleared"] == T0 + 1400                        # when it caught up, not 15 min later


def test_a_short_backlog_is_never_pushed_nor_recovered():
    r = Rig()
    r.at(0, status=backlog(650))
    assert r.keys() == ["stt_backlog"]
    r.at(300, status=backlog(30))
    r.at(300 + 900, status=backlog(30))
    assert r.keys() == [] and r.n.msgs == []


def test_the_cause_names_the_aws_fallback_and_a_slow_whisper():
    r = Rig()
    r.at(0, status=backlog(700, in_h=100, in_fallback_h=60, urgent=40))
    assert r.h.alerts()[0]["text"] == ("312 clips waiting (40 pages / Fire/EMS 15 / firegrounds); "
                                       "cause: Amazon Transcribe falling back, 60 of 100 clips/h")
    r = Rig()
    r.at(0, status=backlog(700, in_h=100, service_s=10.0, in_top=[]))
    assert r.h.alerts()[0]["text"].endswith("cause: catching up, 100 clips/h in, 360/h out (10 s each)")


# -- the station-side diagnostics (tools/stt_diag.py) ---------------------------------------------------------------


def test_the_diagnostics_read_whispers_pace_and_what_was_shed_from_the_journals(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("stt_diag", Path(__file__).parent.parent / "tools" / "stt_diag.py")
    diag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diag)
    t0 = 1794060000.0
    whisper_log = "\n".join(
        f"{t0 + i * 16.4:.6f} host whisper-server[1]: Received request: tx.wav\n"
        f"{t0 + i * 16.4 + 0.1:.6f} host whisper-server[1]: whisper_full: processing 'x' (48000 samples, 3.0 sec), 3 threads"
        for i in range(30))
    station_log = "\n".join([
        "speech-to-text: 383-1794-call_1 not transcribed: skipped: busy: speech-to-text 15 min behind, a context "
        "talkgroup (Re-transcribe it if it matters)",
        "speech-to-text: 105-1794-call_2 not transcribed: waited 180 min for speech-to-text: skipped, too far behind",
        "speech-to-text re-run: 2 recording(s) queued"])
    monkeypatch.setattr(diag, "run", lambda cmd, timeout=60.0: whisper_log if "sizeup-whisper" in cmd else station_log)
    pace = "\n".join(diag.pace(6))
    assert "30 clips" in pace and "gap median  16.4 s" in pace and "clip median  3.0 s" in pace
    q = "\n".join(diag.queue_log(6))
    assert "2 clips not transcribed; 2 re-runs" in q and "shed (busy: context talkgroup)" in q and "383 (1)" in q


def test_airband_is_shed_after_five_minutes_so_live_talkgroups_never_wait_hours_behind_it():
    from sizeup.stt import HIGH_MAX_WAIT_S
    clock = {"t": 1000.0}
    q, dropped = q_at(clock)
    for i in range(3):
        q.put((HIGH, None, f"tower-{i}", None, "HPN Tower"))
    q.put((LIVE, None, "main-tg", None, "tg:333"))
    clock["t"] += HIGH_MAX_WAIT_S + 1
    assert q.get_nowait()[2] == "main-tg"
    assert [d[0] for d in dropped] == ["tower-0", "tower-1", "tower-2"]
    assert all(d[1].startswith("skipped: busy") and "airband" in d[1] for d in dropped) and q.stats()["shed"] == 3


def test_a_shed_clip_says_skipped_busy_not_transcription_failed(tmp_path):
    from sizeup.app import App
    from sizeup.incidents import Traffic
    from tests.test_app import cfg
    app = App(cfg(tmp_path))
    t = Traffic("383-x", "WCPD Ch 1", 0.0, 1.0, "")
    app._stt_dropped(t, "skipped: busy: speech-to-text 15 min behind, a context talkgroup (Re-transcribe it if it matters)")
    assert t.status == "skipped" and t.model.startswith("m.bin (skipped: busy:")
    u = Traffic("383-y", "WCPD Ch 1", 0.0, 1.0, "")
    app._stt_dropped(u, "speech-to-text queue full (2000 waiting)")
    assert u.status == "error" and "not transcribed: speech-to-text queue full" in u.model


def test_the_backlog_is_not_pushed_once_it_has_nearly_caught_up():
    r = Rig()
    r.at(0, status=backlog(900))                                          # 15 min behind: on the panel
    r.at(400, status=backlog(200))                                        # 20 min after its since, but caught up
    assert r.keys() == ["stt_backlog"] and r.n.msgs == []
    r.at(700, status=backlog(1300))                                       # far behind again: pushed now
    assert len(r.n.msgs) == 1


def test_the_bench_wants_what_sizeup_sends_whisper(tmp_path):
    import importlib.util
    import wave
    from pathlib import Path
    import pytest
    spec = importlib.util.spec_from_file_location("stt_diag", Path(__file__).parent.parent / "tools" / "stt_diag.py")
    diag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diag)
    p = tmp_path / "x.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 800)
    with pytest.raises(SystemExit, match="16 kHz"):
        diag._wav16k(str(p))


def test_a_late_aws_answer_on_a_skipped_clip_is_its_text(tmp_path):
    from sizeup.app import App, AwsJob
    from sizeup.incidents import Traffic
    from tests.test_app import cfg
    app = App(cfg(tmp_path))
    t = Traffic("tower-1", "HPN Tower", time.time() - 5, time.time() - 2, "")
    app._stt_dropped(t, "skipped: busy: speech-to-text 5 min behind, an airband clip is no use late (Re-transcribe it)")
    assert t.status == "skipped"
    app._aws_late(AwsJob("traffic", "tower-1", "HPN Tower"), "late words", 0.8)
    t = app._find("tower-1")[0]
    assert t.text == "late words" and t.status == "done" and not getattr(t, "alt_text", "")


def test_when_aws_is_back_a_skipped_clip_goes_to_it(tmp_path):
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_channels import AWS_TEXT, aws_cfg
    from tests.test_cloud import HubRun, call, hub_cfg
    from tests.test_transcribe import wait
    from tests.test_transcribe_watchdog import _pending_event, _row
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_monthly_limit_usd",
                                                                 "value": 0}, token=None)[0] == 200
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [_pending_event("FG1-7")]})[0] == 200
        assert wait(lambda: (_row(hub, "FG1-7") or {}).get("status") == "error", 20)
        item = hub.app._find("FG1-7")[0]
        item.status = "skipped"                                         # the station's whisper was busy: shed
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_monthly_limit_usd",
                                                                 "value": 40}, token=None)[0] == 200
        assert hub.app._aws_resend() == 1
        assert wait(lambda: (_row(hub, "FG1-7") or {}).get("text") == AWS_TEXT + ".", 20)
    finally:
        hub.close()
        fake.close()
