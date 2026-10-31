"""Nothing is left "Transcribing" for good (GitHub #195): Amazon Transcribe's deadline, watchdog and circuit
breaker (sizeup/transcribe.py), AWS's reasons (AccessDenied, billing), the budget's alerts and reserve, the
whisper queue that never drops a clip without a word (sizeup/stt.py SttQueue), and the hub's sweep, restart
recovery and resend (sizeup/cloud/hub.py). Local fakes only: no network, never AWS."""

import base64
import sqlite3
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import datasources as ds
from sizeup.stt import LIVE, RERUN, STALE_S, URGENT, SttQueue
from sizeup.transcribe import (AwsTranscriber, ChannelWorker, Settings, Usage, WebSocket, WsError, _handshake_error,
                               classify, deadline_for)
from tests.fake_transcribe import FakeTranscribe, tone8k
from tests.test_channels import AWS_TEXT, aws_cfg
from tests.test_cloud import HubRun, all_traffic, call, fm_segment, hub_cfg, station, station_cfg
from tests.test_health import T0, Rig
from tests.test_transcribe import fake_creds, wait  # noqa: F401 (the autouse fixture: test credentials only)
from tests.test_app import voice


class Results:
    def __init__(self):
        self.done, self.failed, self.late = {}, {}, {}

    def ok(self, item, text, conf):
        self.done[item] = (text, conf)

    def fail(self, item, why):
        self.failed[item] = why

    def late_(self, item, text, conf):
        self.late[item] = (text, conf)


class HangingWs:
    """A stream on a dead connection: sends block (no error, no answer) until the stream is cut (abort or
    close); ignore_abort: stuck where cutting the stream doesn't reach it, until release."""

    def __init__(self, ignore_abort=False):
        self.cut, self.release = threading.Event(), threading.Event()
        self.ignore_abort, self.aborted, self.closed = ignore_abort, 0, False
        self.sock = SimpleNamespace(settimeout=lambda t: None)

    def send_binary(self, data):
        (self.release if self.ignore_abort else self.cut).wait(10)
        if self.cut.is_set():
            raise WsError("connection cut")

    def recv(self):
        self.cut.wait(15)
        raise WsError("connection closed")

    def abort(self):
        self.aborted += 1
        if not self.ignore_abort:
            self.cut.set()

    def close(self, code=1000):
        self.closed = True
        self.cut.set()


def local(url, timeout=10.0):
    return WebSocket.connect(url, timeout)      # (the fixture's guard: 127.0.0.1 only)


def hangs_first(hung):
    """connect(): the hanging stream first, then the fake behind the endpoint."""
    calls = []

    def connect(url, timeout=10.0):
        calls.append(url)
        return hung if len(calls) == 1 else local(url, timeout)
    connect.calls = calls
    return connect


def quick(fake, res, logs=None, connect=local, kv=None, **kw):
    kv = {} if kv is None else kv
    kw = {"pace": 50, "quiet_s": 0.4, "settle_s": 0.4, "flush_s": 0.6, "watchdog_s": 0.05, **kw}
    t = AwsTranscriber(Settings(endpoint=fake.url, **kw), Usage(kv.get, kv.__setitem__), res.ok, res.fail,
                       log=(logs.append if logs is not None else lambda m: None), connect=connect, on_late=res.late_)
    return t


# -- AWS's reasons ---------------------------------------------------------------------------------------------


def test_aws_reasons_are_classified_billing_included():
    assert classify("OptInRequired", "The AWS access key ID needs a subscription for the service") == "billing"
    assert classify("SubscriptionRequiredException") == "billing"
    assert classify("AccessDenied", "HTTP 403: Your account is suspended: update the payment method") == "billing"
    assert classify("AccessDeniedException", "User: arn:aws:sts::1:assumed-role/hub is not authorized") == "permission"
    assert classify("UnrecognizedClientException", "The security token included in the request is invalid") == "credentials"
    assert classify("NoCredentials") == "credentials" and classify("ExpiredTokenException") == "credentials"
    assert classify("LimitExceededException", "too many streams") == "throttling"
    assert classify("ServiceUnavailableException") == "service" and classify("HTTP 503") == "service"
    assert classify("Connection", "TimeoutError: timed out") == "network"
    assert classify("Timeout") == "timeout" and classify("Internal", "OperationalError") == "internal"
    assert classify("BadRequestException", "bad audio") == "other"
    # the handshake's answer: AWS's type from the header, the JSON body or an XML body
    e = _handshake_error(403, {}, b"<Response><Errors><Error><Code>OptInRequired</Code><Message>You are not "
                                  b"subscribed to this service.</Message></Error></Errors></Response>")
    assert e.code == "OptInRequired" and "not subscribed" in e.detail
    e = _handshake_error(403, {"x-amzn-errortype": "AccessDeniedException:http://internal"}, b'{"message":"no"}')
    assert e.code == "AccessDeniedException" and e.detail == "AccessDeniedException: no"
    assert deadline_for(5.0) == 60.0 and deadline_for(40.0) == 120.0


@pytest.mark.parametrize("body,typ,reason,said", [
    ({"message": "User is not authorized to perform: transcribe:StartStreamTranscriptionWebSocket"},
     "AccessDeniedException", "permission", "AccessDeniedException"),
    ({"__type": "SubscriptionRequiredException", "message": "The AWS Access Key Id needs a subscription for the service"},
     "", "billing", "needs a subscription"),
    ({"message": "Your account is suspended: the payment method on file failed"}, "", "billing", "suspended"),
])
def test_a_refusal_fails_at_once_with_awss_own_reason(body, typ, reason, said):
    fake = FakeTranscribe(refuse=403, refuse_body=body, refuse_type=typ)
    res = Results()
    t = quick(fake, res)
    try:
        t.submit("tg:307", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.failed)
        assert said in res.failed["a"] and fake.handshakes == 1          # no retry: it can't help
        h = t.health()
        assert h["state"] == "failing" and h["recent"][0]["reason"] == reason and said in h["last_error"]
    finally:
        t.stop()
        fake.close()


# -- the deadline -------------------------------------------------------------------------------------------------


def test_a_hung_stream_times_out_falls_back_and_the_channel_goes_on():
    fake = FakeTranscribe(phrases={400: "Engine 60 on location"})
    hung, res, logs = HangingWs(), Results(), []
    t = quick(fake, res, logs, connect=hangs_first(hung), deadline_min_s=0.8, deadline_factor=0.0, stall_s=0.3)
    try:
        t.submit("tg:307", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.failed, 5)
        assert res.failed["a"].startswith("AWS timed out") and not res.done
        assert hung.aborted and any("cutting its stream" in m for m in logs)       # the stuck send was cut
        t.submit("tg:307", "b", tone8k(400, 0.5))                                  # the channel goes on
        assert wait(lambda: "b" in res.done, 8) and res.done["b"][0] == "Engine 60 on location."
        h = t.health()
        assert h["timeouts"] == 1 and h["state"] == "ok" and h["last_ok"] and "a" not in res.late
    finally:
        t.stop()
        fake.close()


def test_an_answer_after_the_deadline_goes_to_on_late_never_on_done():
    fake = FakeTranscribe(phrases={400: "Engine 60 on location"}, late_s=2.0)
    res = Results()
    t = quick(fake, res, deadline_min_s=1.0, deadline_factor=0.0)
    try:
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.failed, 4) and "timed out" in res.failed["a"]
        assert wait(lambda: "a" in res.late, 12)
        assert res.late["a"][0] == "Engine 60 on location." and "a" not in res.done
        assert t.health()["late"] == 1
    finally:
        t.stop()
        fake.close()


# -- the channel's thread can't die silently ----------------------------------------------------------------------


def test_a_crash_in_a_channel_thread_is_logged_and_its_job_retried_not_lost():
    fake = FakeTranscribe(phrases={400: "Ladder 31 responding"})
    res, logs, kv = Results(), [], {}
    n = {"get": 0}

    def get(k):                         # the CAD database locked once, right as the job is fed
        n["get"] += 1
        if n["get"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return kv.get(k)

    def put(k, v):                      # ... and billing never gets written
        raise sqlite3.OperationalError("disk I/O error")
    t = AwsTranscriber(Settings(endpoint=fake.url, pace=50, quiet_s=0.4, settle_s=0.4, flush_s=0.6, idle_s=0.5),
                       Usage(get, put), res.ok, res.fail, log=logs.append, connect=local)
    try:
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.done, 10) and res.done["a"][0] == "Ladder 31 responding."
        assert any("OperationalError: database is locked" in m for m in logs)
        assert wait(lambda: any("could not count" in m for m in logs), 5)            # the idle stream ended, billed
        w = t._workers["FG1"]
        assert w.is_alive()
        t.submit("FG1", "b", tone8k(400, 0.5))
        assert wait(lambda: "b" in res.done, 10) and t._workers["FG1"] is w
    finally:
        t.stop()
        fake.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")    # (the thread dies on purpose)
def test_a_channel_thread_that_died_is_replaced_and_its_job_goes_on(monkeypatch):
    fake = FakeTranscribe(phrases={400: "Ladder 31 responding"})
    res, logs = Results(), []
    real, n = ChannelWorker._feed, {"k": 0}

    def dies_once(self, job):
        n["k"] += 1
        if n["k"] == 1:
            raise SystemExit             # not an Exception: what `except Exception` can't catch kills the thread
        return real(self, job)
    monkeypatch.setattr(ChannelWorker, "_feed", dies_once)
    t = quick(fake, res, logs)
    try:
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.done, 10) and res.done["a"][0] == "Ladder 31 responding."
        assert t.replaced_n == 1 and any("its thread died: a new thread takes its 1 job(s)" in m for m in logs)
    finally:
        t.stop()
        fake.close()


def test_a_thread_stuck_past_its_cut_is_replaced_and_its_queue_goes_on():
    fake = FakeTranscribe(phrases={400: "Ladder 31 responding"})
    hung, res, logs = HangingWs(ignore_abort=True), Results(), []
    t = quick(fake, res, logs, connect=hangs_first(hung), deadline_min_s=6.0, deadline_factor=0.0, stall_s=0.3,
              kick_grace_s=0.3)
    try:
        t.submit("FG1", "a", tone8k(400, 0.5))
        t.submit("FG1", "b", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.done and "b" in res.done, 10), (res.done, res.failed)
        assert not res.failed and t.replaced_n == 1 and any("still stuck" in m for m in logs)
    finally:
        hung.release.set()
        t.stop()
        fake.close()


# -- the circuit breaker -------------------------------------------------------------------------------------------


def test_the_breaker_opens_while_aws_keeps_failing_and_closes_when_it_answers():
    bad = FakeTranscribe(refuse=403, refuse_type="AccessDeniedException")
    good = FakeTranscribe(phrases={400: "Ladder 31 responding"})
    target = {"url": bad.url}

    def connect(url, timeout=10.0):
        return local(url.replace(bad.url, target["url"]), timeout)
    res, logs, states = Results(), [], []
    t = quick(bad, res, logs, connect=connect, breaker_after_s=0.3, breaker_min_fails=2, breaker_retry_s=0.6)
    t.on_change = lambda: states.append(t.breaker["state"])
    try:
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.failed)
        assert t.available()[0] and t.health()["state"] == "failing"         # one failure: not yet
        time.sleep(0.35)
        t.submit("FG1", "b", tone8k(400, 0.5))
        assert wait(lambda: "b" in res.failed)
        ok, why = t.available()                                                  # failing 2 min (here 0.3 s): open
        assert not ok and why.startswith("Amazon Transcribe failing since") and "no permission" in why
        assert states == ["open"] and t.health()["state"] == "down"
        n = bad.handshakes
        t.submit("FG1", "c", tone8k(400, 0.5))                                   # straight to the fallback
        assert wait(lambda: "c" in res.failed) and "failing since" in res.failed["c"] and bad.handshakes == n
        time.sleep(0.65)                                                         # tried again: half-open
        assert t.available()[0] and t.health()["state"] == "retrying" and states[-1] == "half-open"
        t.submit("FG1", "d", tone8k(400, 0.5))                                   # still refused: open again at once
        assert wait(lambda: "d" in res.failed) and not t.available()[0] and states[-1] == "open"
        time.sleep(0.65)
        assert t.available()[0]
        target["url"] = good.url                                                 # AWS answers again
        t.submit("FG1", "e", tone8k(400, 0.5))
        assert wait(lambda: "e" in res.done, 8)
        h = t.health()
        assert h["state"] == "ok" and h["breaker"]["state"] == "closed" and states[-1] == "closed"
        assert any("breaker open" in m for m in logs) and any("answering again" in m for m in logs)
    finally:
        t.stop()
        bad.close()
        good.close()


# -- the budget ----------------------------------------------------------------------------------------------------


def test_past_the_limit_pages_and_fire_ems_15_keep_aws_within_the_reserve():
    fake = FakeTranscribe()
    res, kv = Results(), {}
    usage = Usage(kv.get, kv.__setitem__)
    usage.add(25 / 0.024 * 60 + 60, 0)                       # just over $25
    t = quick(fake, res, kv=kv)
    t.usage = usage
    try:
        assert not t.available()[0] and "monthly AWS limit" in t.available()[1]
        assert t.available("tg:307")[0] and t.available("South 2")[0] and not t.available("FG1")[0]
        b = t.budget()
        assert b["pct"] >= 100 and b["reserve"] == ["South 2", "tg:307"] and "reserve" in b["reserve_text"]
        usage.add(5 / 0.024 * 60, 0)                         # past $30: the reserve is used up too
        assert not t.available("tg:307")[0] and "reserve" in t.available("tg:307")[1] and t.budget()["reserve"] == []
    finally:
        t.stop()
        fake.close()


def test_the_budget_month_is_new_york_time_not_the_containers():
    import datetime as dt
    from zoneinfo import ZoneInfo
    at = dt.datetime(2026, 11, 1, 2, 30, tzinfo=dt.timezone.utc).timestamp()     # 22:30 on Oct 31 in Port Chester
    assert Usage(lambda k: None, lambda k, v: None, now=lambda: at).month_key() == "2026-10"
    later = dt.datetime(2026, 11, 1, 0, 5, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert Usage(lambda k: None, lambda k, v: None, now=lambda: later).month_key() == "2026-11"


def test_station_health_alerts_at_80_percent_at_the_limit_and_when_transcribe_fails():
    r = Rig()
    h = {"on": True, "state": "ok", "text": "last answer 12:00", "budget": {"pct": 50.0, "usd": 12.5, "limit_usd": 25,
                                                                          "month": "2026-10"}}
    r.hub.app = SimpleNamespace(aws_health=lambda: h)
    r.at(0, post=False)
    assert r.keys() == []
    h["budget"] = {"pct": 82.0, "usd": 20.5, "limit_usd": 25, "month": "2026-10"}
    r.at(5, post=False)                                       # (the station silent: the hub's own alerts still go)
    assert r.keys() == ["aws_budget80"] and "$20.50 of the $25 limit" in r.h.alerts()[0]["text"]
    assert any("80%" in m["title"] for m in r.n.msgs)          # pushed on our alerts topic
    h["budget"] = {"pct": 100.0, "usd": 25.0, "limit_usd": 25, "month": "2026-10",
                   "reserve_text": "pages and Fire/EMS 15 keep AWS up to $30 (the reserve)"}
    h.update(state="down", text="Amazon Transcribe failing since 12:05 (timed out: AWS timed out)", failing_since=T0,
             reason_text="timed out")
    r.at(10, post=False)
    assert set(r.keys()) == {"aws_budget80", "aws_budget", "transcribe"}
    crit = next(a for a in r.h.alerts() if a["key"] == "aws_budget")
    assert crit["severity"] == "critical" and "the reserve" in crit["text"]
    assert any(m["title"].startswith("Amazon Transcribe monthly limit reached") for m in r.n.msgs)
    h.update(state="ok", budget={"pct": 0.0, "usd": 0.0, "limit_usd": 25, "month": "2026-11"})
    r.at(15, post=False)
    assert r.keys() == []                                    # a new month, AWS answering: cleared
    assert r.h.model(T0 + 15)["transcribe"]["state"] == "ok"


def test_the_whisper_backlog_is_an_alert():
    r = Rig()
    from tests.test_health import station_status
    st = station_status()
    st["health"]["stt_queue"] = {"queued": 1800, "live": 1700, "urgent": 20, "rerun": 100, "oldest_s": 4 * 3600,
                                 "dropped": 12, "skipped": 30, "last_drop": {"why": "queue full"}, "max": 2000}
    r.at(0, status=st)
    assert "stt_backlog" in r.keys()
    a = next(a for a in r.h.alerts() if a["key"] == "stt_backlog")
    assert a["title"] == "Speech-to-text behind" and "oldest waiting clip" in a["text"]
    assert r.h.model(T0)["stt_queue"]["dropped"] == 12


def test_transcribe_is_a_row_in_data_sources_with_try_aws_now():
    tried = []
    h = {"on": True, "state": "down", "text": "Amazon Transcribe failing since 12:05 (timed out: ...)", "last_ok": T0,
         "done": 10, "timeouts": 4, "late": 1, "open_jobs": 2, "breaker": {"state": "open"}}
    app = SimpleNamespace(aws_health=lambda: h, aws=SimpleNamespace(retry_now=lambda: tried.append(1) or True,
                                                                    s=SimpleNamespace(breaker_retry_s=300.0)))
    r = ds.DataSources()
    r.register(ds.transcribe_source(app))
    row = r.row("transcribe")
    assert row["group"] == "Radio" and not row["ok"] and "failing since 12:05" in row["error"]
    assert row["action"] == "Try AWS now" and row["last_fetched"] == T0 and row["items"] == 2
    assert "breaker open" in row["note"] and "every 5 min" in row["builtin"]
    r.refresh("transcribe")
    assert wait(lambda: tried == [1])
    off = ds.DataSources()
    off.register(ds.transcribe_source(SimpleNamespace(aws_health=lambda: {"on": False, "why": "runs on the hub"})))
    assert off.row("transcribe")["on"] is False and off.row("transcribe")["why"] == "runs on the hub"


# -- the whisper queue: never a silent drop ----------------------------------------------------------------------------


def test_the_whisper_queue_drops_the_worst_not_the_newest_and_says_so():
    dropped, clock = [], {"t": 1000.0}
    q = SttQueue(lambda item, why: dropped.append((item, why)), clock=lambda: clock["t"], maxsize=5, channel_cap=2)
    for prio, item, ch in ((RERUN, "rerun-1", None), (LIVE, "fg2-old", "FG2"), (LIVE, "chatty-1", "tg:383"),
                           (LIVE, "chatty-2", "tg:383")):
        assert q.put((prio, None, item, None, ch))
    assert q.put((LIVE, None, "chatty-3", None, "tg:383"))            # its channel's cap: that channel's oldest goes
    assert dropped == [("chatty-1", "tg:383: 2 clips already waiting for speech-to-text")]
    assert q.put((URGENT, None, "page-1", None, "South 2"))
    assert q.put((LIVE, None, "fg1", None, "FG1"))                     # full: the re-run goes, not the newest clip
    assert dropped[-1] == ("rerun-1", "speech-to-text queue full (5 waiting)")
    assert q.put((RERUN, None, "rerun-2", None)) is False              # ... unless the newest is the worst
    assert dropped[-1][0] == "rerun-2" and q.stats()["dropped"] == 3
    assert [q.get_nowait()[2] for _ in range(5)] == ["page-1", "fg2-old", "chatty-2", "chatty-3", "fg1"]
    # what waited STALE_S goes behind fresh clips (the newest stale first)
    q.put((LIVE, None, "a", None, "FG1"))
    q.put((LIVE, None, "b", None, "FG1"))
    clock["t"] += STALE_S + 1
    q.put((LIVE, None, "fresh", None, "FG2"))
    assert q.stats()["stale"] == 2
    assert [q.get_nowait()[2] for _ in range(3)] == ["fresh", "b", "a"]


def test_the_whisper_queue_skips_what_waited_hours_and_says_so():
    dropped, clock = [], {"t": 1000.0}
    q = SttQueue(lambda item, why: dropped.append((item, why)), clock=lambda: clock["t"])
    q.put((LIVE, None, "old", None, "tg:383"))
    q.put((RERUN, None, "rerun", None))
    clock["t"] += 3 * 3600 + 60
    q.put((LIVE, None, "new", None, "tg:383"))
    assert q.stats()["oldest_s"] > 3 * 3600
    assert q.get_nowait()[2] == "new" and dropped and dropped[0][0] == "old" and "skipped" in dropped[0][1]
    assert q.get_nowait()[2] == "rerun"                                # a person's re-run is never skipped


def test_a_clip_the_full_queue_drops_says_transcription_failed_on_the_station(tmp_path, monkeypatch):
    from sizeup import stt as stt_mod
    from tests.test_cloud import base
    from sizeup.app import App
    monkeypatch.setattr(stt_mod, "QUEUE_MAX", 0)
    app = App(base(clips_dir=str(tmp_path / "clips")))
    app.stt._q.maxsize = 0                                            # (built before: full now)
    try:
        tid = fm_segment(app)
        assert wait(lambda: any(t.id == tid and t.status == "error" for t in app.store.loose), 5)
        t = next(t for t in app.store.loose if t.id == tid)
        assert "not transcribed: speech-to-text queue full" in t.model
    finally:
        app.stop()


# -- on the hub ------------------------------------------------------------------------------------------------------


def _shorten(aws, **kw):
    for k, v in {"deadline_min_s": 1.0, "deadline_factor": 0.0, "watchdog_s": 0.05, "stall_s": 0.3, **kw}.items():
        setattr(aws.s, k, v)


def test_on_the_hub_a_hung_aws_job_times_out_and_the_stations_whisper_answers(tmp_path):
    fake = FakeTranscribe()
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    try:
        hung = HangingWs()
        hub.app.aws.connect = lambda url, timeout=10.0: hung
        _shorten(hub.app.aws)
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        assert wait(lambda: st._hub_aws["ok"], 10)
        tid = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid and t["text"] == "whisper heard this"
                                for t in all_traffic(hub.get("/api/state"))), 20)
        assert hub.app.aws.health()["timeouts"] == 1
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()


def test_on_the_hub_a_late_aws_answer_is_kept_as_the_alternative_not_over_whispers_text(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT}, late_s=2.0)
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    try:
        _shorten(hub.app.aws, stall_s=30.0)              # (its stream waits for the late answer: no stall)
        hub.app.aws.s.quiet_s = hub.app.aws.s.settle_s = hub.app.aws.s.flush_s = 0.4
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        assert wait(lambda: st._hub_aws["ok"], 10)
        tid = fm_segment(st)

        def row():
            return next((r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid), None)
        assert wait(lambda: (row() or {}).get("text") == "whisper heard this", 20)
        assert wait(lambda: (row() or {}).get("alt_text") == AWS_TEXT + ".", 20)
        r = row()
        assert r["alt_model"] == "aws-transcribe (late)" and r["text"] == "whisper heard this" and r["model"] == "m.bin"
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()


def _pending_event(tid, seq=1):
    from sizeup.cad.dispatcher import wav_bytes
    now = time.time()
    return {"seq": seq, "at": now, "type": "traffic",
            "data": {"id": tid, "source": "FG1", "start": now - 3, "end": now, "text": "", "status": "remote",
                     "transcript_pending": "aws", "stt": "aws", "model": "aws-transcribe"},
            "audio": base64.b64encode(wav_bytes((voice(3.0)[::2] * 32767).astype(np.int16), 8000)).decode()}


def _row(hub, tid):
    return next((r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid), None)


def test_the_hubs_sweep_fails_over_what_no_job_answers_for_and_flags_what_whisper_never_sent(tmp_path):
    fake = FakeTranscribe()
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        hub.app.aws.submit = lambda *a, **k: None                  # the job is lost (a thread gone with it)
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [_pending_event("FG1-1")]})[0] == 200
        assert wait(lambda: (_row(hub, "FG1-1") or {}).get("status") == "pending", 5)
        assert hub.app._aws_sweep() == 0                            # within its deadline: left alone
        hub.app._aws_since["FG1-1"] -= 1000
        assert hub.app._aws_sweep() == 1                            # overdue: the station's whisper (offline here)
        assert wait(lambda: (_row(hub, "FG1-1") or {}).get("status") == "error", 20)
        assert "failed" in _row(hub, "FG1-1")["model"]
        # the station's whisper asked and never answering: flagged, not "Transcribing" for good
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [_pending_event("FG1-2", 2)]})[0] == 200
        assert wait(lambda: (_row(hub, "FG1-2") or {}).get("status") == "pending", 5)
        hub.app._aws_asked["FG1-2"] = time.time() - 1000
        assert hub.app._aws_sweep() == 1
        r = _row(hub, "FG1-2")
        assert r["status"] == "error" and "sent no text" in r["model"]
        # its text still comes up if the station's whisper sends it after all
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [
            {"seq": 3, "at": time.time(), "type": "retext",
             "data": {"id": "FG1-2", "text": "whisper got there", "status": "done", "model": "m.bin"}}]})[0] == 200
        assert wait(lambda: (_row(hub, "FG1-2") or {}).get("text") == "whisper got there", 5)
    finally:
        hub.close()
        fake.close()


def test_after_a_restart_what_waited_for_aws_goes_to_it_again(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        hub.app.aws.submit = lambda *a, **k: None                  # the hub goes down with the job in flight
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [_pending_event("FG1-9")]})[0] == 200
        assert wait(lambda: (_row(hub, "FG1-9") or {}).get("status") == "pending", 5)
        hub.close()
        hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))           # back: _aws_recover
        assert wait(lambda: (_row(hub, "FG1-9") or {}).get("text") == AWS_TEXT + ".", 20)
    finally:
        hub.close()
        fake.close()


def test_after_a_restart_without_aws_the_stations_whisper_takes_what_was_waiting(tmp_path):
    fake = FakeTranscribe()
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        assert wait(lambda: st._hub_aws["ok"], 10)
        hub.app.aws.submit = lambda *a, **k: None                  # the hub goes down with the job in flight
        tid = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid and t["status"] == "pending" for t in all_traffic(hub.app.state())), 15)
        # it comes back without AWS (the restart from hub.sqlite itself: the test above): _aws_recover at start
        aws, hub.app.aws = hub.app.aws, None
        hub.app._aws_since.clear()
        assert hub.app._aws_recover() == 1
        assert wait(lambda: any(t["id"] == tid and t["text"] == "whisper heard this"
                                for t in all_traffic(hub.app.state())), 20)
        hub.app.aws = aws
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()


def test_when_aws_can_take_audio_again_what_failed_meanwhile_goes_to_it(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_monthly_limit_usd",
                                                                 "value": 0}, token=None)[0] == 200
        assert not hub.app.aws.available()[0]
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [_pending_event("FG1-5")]})[0] == 200
        assert wait(lambda: (_row(hub, "FG1-5") or {}).get("status") == "error", 20)    # the limit, no station: failed
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_monthly_limit_usd",
                                                                 "value": 40}, token=None)[0] == 200
        assert hub.app._aws_resend() == 1                                                  # (the sweep does it)
        assert wait(lambda: (_row(hub, "FG1-5") or {}).get("text") == AWS_TEXT + ".", 20)
    finally:
        hub.close()
        fake.close()
