"""No transcript, page text or call detail in a log line. The hub's stdout goes to CloudWatch (/ecs/sizeup-hub) and the
station's journal to /sizeup/station; the words stay in the hub's database, behind sign-in (sizeup/logscrub.py said()).

A marker word is put in every text, and each path that handles words is run with it: the station's whisper
(_transcribed_one on the station and the hub), Amazon Transcribe, the GPU worker pool (a transmission, a page, a shed
clip caught up), a re-run, every _retext, a page with our tones (the INCIDENT line), the CAD's alarms, status hints,
possible incoming and the dispatch voice with transmit off. Everything written while it runs is read through the shared
log sentinel (tests/log_sentinel.py: stdout and stderr at the file-descriptor level, so every thread's log(), print
and traceback, the logging module, a thread's uncaught exception) and the log hooks handed to the CAD. The marker
must be in none of it.

A guard for any new log line: a path that logs its words fails here. Invented words and places only."""

import logging
import time

import numpy as np
import pytest

from sizeup.cad import dispatcher
from sizeup.cad.service import Cad, Settings
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.stt import CATCHUP, URGENT
from tests.fake_transcribe import FakeTranscribe
from tests.log_sentinel import Captured, assert_no_sensitive
from tests.test_app import tone, voice, wait
from tests.test_channels import aws_cfg, fake_aws  # noqa: F401 (the fixture: test credentials, 127.0.0.1 only)
from tests.test_cloud import HubRun, fm_segment, hub_cfg, station, station_cfg
from tests.test_incident_sources import traffic as feed_traffic
from tests.test_alarms import tx as radio_tx
from tests.test_radios import Clock, uid
from tests.test_stt_pool import FakeWorker, pending_event, row, send

MARK = "Quillfeather"            # an invented word, nowhere in sizeup: in a log line it is a leak
PAGE = ("Port Chester Fire Department, respond to a reported structure fire. 12 Quillfeather Road, cross of Pearl "
        "Street, Port Chester. Time out 1432.")


class Logs:
    """Everything logged while a test runs: the shared log sentinel's capture (tests/log_sentinel.py) and the log hooks
    a test hands out (hook)."""

    def __init__(self, captured: Captured):
        self.captured = captured
        self.hooked: list[str] = []
        self.forbidden = [MARK]

    def hook(self, msg) -> None:
        self.hooked.append(str(msg))

    def forbid(self, *words: str) -> None:
        """More words that must never be logged (a status hint's phrase: the transmission's words, not the marker)."""
        self.forbidden += [w for w in words if w]

    def lines(self) -> list[str]:
        return [*self.captured.lines(), *self.hooked]

    def text(self) -> str:
        return "\n".join(self.lines())

    def leaks(self) -> list[str]:
        words = [w.casefold() for w in self.forbidden]
        return [line for line in self.lines() if any(w in line.casefold() for w in words)]

    def check(self) -> None:
        assert_no_sensitive(self.lines(), self.forbidden)


@pytest.fixture
def logs(log_sentinel):
    g = Logs(log_sentinel)
    yield g
    g.check()                       # and what came after the test's own check (threads finishing, a close)


def test_the_guard_sees_a_leak_in_the_output_the_logging_module_and_the_hooks():
    class Fd:                                       # (its own capture: the fixture's would fail this test at teardown)
        text = ""

        @staticmethod
        def readouterr():
            out, Fd.text = Fd.text, ""
            return out, ""

    class Caplog:
        records: list = []

        @staticmethod
        def clear():
            Caplog.records = []
    g = Logs(Captured(Fd, Caplog))
    assert g.leaks() == []
    Fd.text = "12:00:00 FG1: transmission FG1-1 (done, 4 words)\n"
    assert g.leaks() == []
    Fd.text = f"12:00:01 FG1: Engine 64 {MARK.lower()} on location\n"
    assert len(g.leaks()) == 1                      # (any case)
    g.hook(f"alarms: {MARK}")
    Caplog.records = [logging.makeLogRecord({"msg": f"x: {MARK}", "levelname": "WARNING", "name": "x"})]
    assert len(g.leaks()) == 3
    g.forbid("transmission")
    assert len(g.leaks()) == 4
    with pytest.raises(AssertionError, match="reached a log line"):
        g.check()


def test_a_thread_writing_through_log_is_captured(logs):
    import threading

    from sizeup.app import log
    t = threading.Thread(target=log, args=("transmission FG1-9 (done, 2 words)",))
    t.start()
    t.join()
    assert "transmission FG1-9 (done, 2 words)" in logs.text()
    logs.check()


def test_the_stations_whisper_a_rerun_every_retext_and_a_page_with_our_tones(tmp_path, logs):
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    first, second = f"Engine 64 {MARK} on location", f"Engine 64 {MARK} second try"
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: first, 6: PAGE})
        st._hub_aws = {"ok": False}                              # (no AWS: FG1 runs the station's whisper)
        tid = fm_segment(st)
        assert wait(lambda: row(hub, tid).get("text") == first, 20), row(hub, tid)
        # a re-run that changes the words: the station's whisper again, its retext on both
        st.stt.client.texts[3] = second
        assert hub.app.retranscribe([tid]) == 1
        assert wait(lambda: row(hub, tid).get("text") == second, 20), row(hub, tid)
        # every way new words reach _retext (pool worker, AWS, a re-run, the station's), with and without a change
        for i, why in enumerate(("gpu worker", "aws", "re-run", "station re-run", "catch-up")):
            hub.app._retext(tid, f"{MARK} words {i}", "done", 0.9, "m.bin", why=why)
            st._retext(tid, f"{MARK} words {i}", "done", 0.9, "m.bin", why=why)
        # a page with our tones: the station's whisper, the incident on both (the INCIDENT line)
        audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
        now = time.time()
        st._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
        queued = lambda cad: [i for i in cad.state()["incidents"] if i["status"] == "queued"]  # noqa: E731
        assert wait(lambda: queued(st.cad) and queued(hub.app.cad), 20)
        assert wait(lambda: any(p.raw == PAGE for p in hub.app.pages), 10)
        logs.forbid("structure fire")                            # the nature: never in the INCIDENT line either
        seen = logs.text()
        assert f"transmission {tid}" in seen and "INCIDENT" in seen and "text changed" in seen   # (the paths ran)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
    logs.check()


def test_amazon_transcribe_and_the_gpu_pool_a_transmission_and_a_page(tmp_path, logs):
    from tests.test_transcribe_watchdog import _page_event
    aws_text = f"Engine 63 {MARK} per AWS"
    fake = FakeTranscribe(phrases={500: aws_text})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    w = None
    try:
        send(hub, pending_event("FG1-1"))                         # no worker: Amazon Transcribe
        assert wait(lambda: row(hub, "FG1-1").get("text") == aws_text + ".", 20), row(hub, "FG1-1")
        token = hub.app.hub.workers.add("gpu-1")
        w = FakeWorker(hub.url, token, text=f"Port Chester engine 99 respond to {MARK} Road").start()
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        send(hub, pending_event("FG1-2", seq=2))                  # the pool: a transmission
        assert wait(lambda: row(hub, "FG1-2").get("text") == w.text, 30), row(hub, "FG1-2")
        send(hub, _page_event("South_2-900", seq=3))              # the pool: a page
        page = lambda: next((p for p in hub.app.pages if p.id == "South_2-900"), None)   # noqa: E731
        assert wait(lambda: page() is not None and page().status == "done" and MARK in page().raw, 20)
        assert wait(lambda: len(w.answers) == 2, 5)
        assert "FG1-2" in logs.text()
    finally:
        if w is not None:
            w.close()
        hub.close()
        fake.close()
    logs.check()


def test_a_shed_clip_caught_up_on_the_gpu_pool(tmp_path, logs):
    from tests.test_stt_catchup import PD, pd_alert, pd_event
    hub = HubRun(hub_cfg(tmp_path))
    token = hub.app.hub.workers.add("gpu-1")
    w = FakeWorker(hub.url, token, text=f"car 12 {MARK} on the scene").start()
    try:
        app, now, tid = hub.app, time.time(), "9211-1794-call_9"
        pd_alert(app)
        send(hub, pd_event(tid, now))
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]] == [tid], 10)
        assert wait(lambda: app.sttpool.ready(CATCHUP)[0], 5)
        assert app._catchup_route(PD) == "pool"
        app.catchup.pause = lambda: ""
        assert app.catchup.tick() == 1
        res = lambda: app.cad.store.one("SELECT text, rerun_result FROM radio_traffic WHERE id = ?", (tid,)) or {}  # noqa: E731
        assert wait(lambda: res().get("rerun_result"), 10), res()
        assert res()["text"] == w.text
    finally:
        w.close()
        hub.close()
    logs.check()


def test_the_cads_alarms_status_hints_possible_incoming_and_transmit_off(tmp_path, logs, monkeypatch):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    clock = Clock(time.time())
    c = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=logs.hook, clock=clock)
    c.s.tones = ()
    # the dispatch voice with transmit off: an address and a nature
    inc = c.create({"type": "STRU", "address": f"12 {MARK} Road", "operator": "Lt. Ruiz"})
    r = c.dispatch(inc["id"], {"unit_ids": [uid(c, "E64"), uid(c, "E62")], "operator": "Lt. Ruiz"})
    assert c.voice.wait(60) and MARK.lower() in c.transmitter.sent[-1][1].lower(), r
    clock.t += 30
    now = clock.t
    # an alarm: a mayday on FG1
    a = c.alarms.on_traffic(radio_tx(text=f"Engine 64 mayday mayday {MARK}", rig="E64", radio_id="6401", at=now - 5))
    assert a and a["level"] == "alarm"
    # a status hint: a unit on the call on location
    c.record_traffic({"id": "FG1-7", "start": now - 2, "end": now - 1, "source": "FG1", "talkgroup": None, "unit": "",
                      "rig": "", "radio_id": "", "text": f"Engine 64 on location, {MARK} showing", "status": "done"})
    [h] = c.state()["status_hints"]
    assert h["unit"] == "E64"
    logs.forbid(h["said"])                                       # its phrase: the words, not the marker
    # possible incoming: dispatch opening a call to us on the feed's talkgroup
    n = feed_traffic(c, "a-t0", f"Port Chester, incoming additional alarm, {MARK}", now)
    assert n and n["status"] == "open"
    seen = logs.text()
    assert "TRANSMIT OFF" in seen and "alarms:" in seen and "radio suggests" in seen and "possible incoming" in seen
    logs.check()


def test_a_late_aws_answer_kept_as_the_alternative(tmp_path, logs):
    """AWS's answer after the fallback already gave the clip its text: kept as the alternative, its words never logged."""
    import time
    from sizeup.app import App, AwsJob
    from sizeup.incidents import Traffic
    from tests.test_app import cfg
    app = App(cfg(tmp_path))
    t = Traffic("fg1-late", "FG1", time.time() - 5, time.time() - 2, "Engine 60 on location", status="done")
    app.store.on_traffic(t)
    app._aws_late(AwsJob("traffic", "fg1-late", "FG1"), f"{MARK} on location", 0.8)
    assert "a late answer, kept as the alternative" in logs.text()
    logs.check()
