"""The radio pre-alert push's "Listen" link (sizeup/intel/listen.py): synthetic tones only (no real audio), a fake bucket,
fake credentials, a fake ntfy."""

from __future__ import annotations

import io
import json
import re
import wave
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from sizeup import aws, notify
from sizeup.intel import listen as L
from sizeup.intel.engine import IntelEngine
from sizeup.intel.events import MemoryEvents
from sizeup.intel.model import Place
from tests.test_intel import Clock, example_locate, row


def tone(hz: float, seconds: float, rate: int = 8000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    x = (0.5 * np.sin(2 * np.pi * hz * t) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.tobytes())
    return buf.getvalue()


class FakeBucket:
    def __init__(self, refuse=False):
        self.puts, self.refuse, self.signed = [], refuse, []

    def put(self, key, body, ctype):
        if self.refuse:
            raise aws.AwsError("S3 PUT: 403 AccessDenied", "AccessDenied", 403)
        self.puts.append((key, body, ctype))

    def presign(self, key, expires_s, ctype):
        self.signed.append((key, expires_s, ctype))
        return f"https://bucket.example/{key}?X-Amz-Expires={expires_s}"


class Radio:
    """radio_traffic rows and their clips."""

    def __init__(self):
        self.rows, self.audio = [], {}

    def add(self, tid, start, secs, hz, source="p25:mrrs:9211", tier="alert"):
        self.rows.append({"id": tid, "start": start, "tier": tier, "source": source})
        self.audio[tid] = tone(hz, secs)

    def query(self, sources, t0, t1):
        return [r for r in self.rows if r["source"] in sources and t0 <= r["start"] <= t1]

    def tiers(self, ids):
        return {r["id"]: r["tier"] for r in self.rows if r["id"] in ids}


def listen(radio, bucket, clock, **cfg):
    return L.Listen(bucket, radio.query, radio.audio.get, settings=lambda: cfg, clock=clock, log=lambda m: None,
                    ffmpeg="", tiers=radio.tiers, worker=False)


def segments(wav: bytes) -> list[float]:
    """The loud stretches' lengths in seconds, by 10 ms frames (the separators are short quiet ticks)."""
    x = L.read_wav(wav)
    f = L.RATE // 100
    rms = [float(np.sqrt(np.mean(x[i:i + f] ** 2))) for i in range(0, len(x) - f + 1, f)]
    out, n = [], 0
    for v in rms + [0.0]:
        if v > 0.2:
            n += 1
        elif n:
            out.append(n)
            n = 0
    return [round(k / 100, 1) for k in out if k > 10]


def test_clips_are_stitched_oldest_first_with_a_gap_and_a_tick_between():
    r = Radio()
    r.add("b", 1010, 1.0, 600)
    r.add("a", 1000, 2.0, 400)
    r.add("c", 1020, 0.5, 800)
    b = FakeBucket()
    lk = listen(r, b, Clock(1030))
    url = lk.link("intel:1", ["p25:mrrs:9211"], ["b"], 1010, 1010)
    assert url and b.puts == []                          # the link at once; the file on the listen thread
    assert lk.drain() == 1
    [(key, body, ctype)] = b.puts
    assert ctype == "audio/wav" and segments(body) == [2.0, 1.0, 0.5]
    x = L.read_wav(body)
    assert abs(len(x) / L.RATE - (3.5 + 2 * (L.GAP_S + L.TICK_S))) < 0.05


def test_the_window_takes_context_before_and_after_and_nothing_else():
    r = Radio()
    r.add("old", 900, 1.0, 300)                          # before the before-window
    r.add("pre", 980, 1.0, 400)
    r.add("hit", 1000, 1.0, 500)
    r.add("other", 1001, 1.0, 600, source="p25:mrrs:9999")   # another source
    r.add("enc", 1002, 1.0, 700, tier="activity")       # encrypted: never
    r.add("rec", 1003, 1.0, 750, tier="record")         # record only: never
    b = FakeBucket()
    lk = listen(r, b, Clock(1005), listen_before_s=45, listen_after_s=60)
    assert lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    assert lk.items["intel:1"].clips == ("pre", "hit")


def test_the_alerts_own_transmissions_are_checked_too():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    r.add("own-rec", 400, 1.0, 600, tier="record")       # its own, outside the window, record only: never
    r.audio["unknown"] = tone(700, 1.0)                  # no row at all: its tier unknown, never
    b = FakeBucket()
    lk = listen(r, b, Clock(1005))
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit", "own-rec", "unknown"], 1000, 1000)
    lk.drain()
    assert lk.items["intel:1"].clips == ("hit",)


def test_later_transmissions_restitch_to_the_same_key():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b = FakeBucket()
    clock = Clock(1002)
    lk = listen(r, b, clock)
    url = lk.link("intel:7", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    assert lk.drain() == 1
    lk.refresh(1010)
    assert lk.drain() == 0 and len(b.puts) == 1          # nothing new: no upload
    r.add("readback", 1030, 1.5, 650)
    clock.t = 1035
    lk.refresh(1035)
    assert b.puts[1:] == [] and lk.drain() == 1           # refresh only queues; the listen thread uploads
    assert [k for k, _b, _c in b.puts] == [b.puts[0][0]] * 2 and segments(b.puts[-1][1]) == [1.0, 1.5]
    assert lk.link("intel:7", ["p25:mrrs:9211"], ["hit", "readback"], 1000, 1030).split("?")[0] == url.split("?")[0]
    clock.t = 1035 + L.DEFAULTS["listen_expiry_min"] * 60 + 1     # the last link (signed at 1035) has expired
    lk.refresh(clock.t)
    assert lk.items == {}


def test_keys_are_random_and_carry_nothing_of_the_event():
    r = Radio()
    r.add("9211-1790000000.1_0.0-call_5", 1000, 1.0, 500)
    b = FakeBucket()
    lk = listen(r, b, Clock(1001))
    lk.link("intel:42", ["p25:mrrs:9211"], ["9211-1790000000.1_0.0-call_5"], 1000, 1000)
    lk.link("intel:43", ["p25:mrrs:9211"], ["9211-1790000000.1_0.0-call_5"], 1000, 1000)
    lk.drain()
    keys = [k for k, _b, _c in b.puts]
    assert len(set(keys)) == 2
    for k in keys:
        assert re.fullmatch(r"listen/[A-Za-z0-9_-]{32}\.wav", k) and "42" not in k[7:9] and "9211" not in k and "intel" not in k


def test_the_presigned_url_expires_and_plays_inline_without_metadata(monkeypatch):
    sent = []

    def send(method, url, headers, body, timeout):
        sent.append((method, url, headers))
        return 200, {}, b""
    creds = aws.Credentials("AKIDEXAMPLE", "secret", "token")
    st = L.S3ListenStore("audio-bucket-example", "us-east-1", send=send, creds=lambda: creds)
    st.put("listen/abc.wav", b"RIFF....", "audio/wav")
    [(method, url, headers)] = sent
    assert method == "PUT" and url.endswith("/listen/abc.wav")
    assert not [h for h in headers if h.lower().startswith("x-amz-meta")]
    u = st.presign("listen/abc.wav", 20 * 60, "audio/wav")
    q = parse_qs(urlsplit(u).query)
    assert urlsplit(u).scheme == "https" and q["X-Amz-Expires"] == ["1200"]
    assert q["response-content-disposition"] == ["inline"] and q["response-content-type"] == ["audio/wav"]
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b = FakeBucket()
    listen(r, b, Clock(1001), listen_expiry_min=7).link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    assert b.signed[0][1] == 420


def test_off_not_configured_and_refused():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    assert listen(r, FakeBucket(), Clock(1001), listen=False).link("i", ["p25:mrrs:9211"], ["hit"], 1000, 1000) is None
    none = L.Listen(None, r.query, r.audio.get, clock=Clock(1001), log=lambda m: None)
    assert none.link("i", ["p25:mrrs:9211"], ["hit"], 1000, 1000) is None and not none.status()["configured"]
    bad = listen(r, FakeBucket(refuse=True), Clock(1001))
    bad.link("i", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    bad.drain()
    st = bad.status()
    assert not st["configured"] and "27" in st["why"]
    assert bad.link("j", ["p25:mrrs:9211"], ["hit"], 1000, 1000) is None     # refused (403): an hour off


def test_only_a_403_turns_the_links_off_for_an_hour():
    class Flaky(FakeBucket):
        def put(self, key, body, ctype):
            raise aws.AwsError("S3 PUT: timed out", "Network", 0)
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    clock = Clock(1001)
    lk = listen(r, Flaky(), clock)
    lk.link("i", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    assert lk.status()["configured"] and lk.link("j", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    lk.refresh(1002)
    assert lk._jobs == []                                    # a minute before it is tried again
    clock.t = 1001 + L.RETRY_SOON_S + 1
    assert lk.refresh(clock.t) >= 1


def test_the_presign_uses_credentials_that_outlive_the_link(monkeypatch):
    now = [1000.0]
    issued = []

    def creds():
        c = aws.Credentials("AKIDEXAMPLE", "secret", "token", expires=now[0] + (600 if not issued else 6 * 3600))
        return c

    def fresh():
        issued.append(1)
    monkeypatch.setattr(aws, "forget_credentials", fresh)
    st = L.S3ListenStore("audio-bucket-example", "us-east-1", send=lambda *a: (200, {}, b""), creds=creds,
                         clock=lambda: now[0])
    st.own_creds = True                                     # as with the task role's
    st.presign("listen/abc.wav", 20 * 60, "audio/wav")
    assert issued == [1]                                    # 10 min left < 20 min + margin: fetched again


def test_the_push_carries_a_listen_action_and_a_plain_link(tmp_path):
    from tests.test_notify import FakeNtfy
    ntfy = FakeNtfy()
    try:
        from tests.test_cad import make_cad
        cad = make_cad(tmp_path)
        now = cad.clock()
        r = Radio()
        r.add("t1", now, 1.0, 500)
        b = FakeBucket()
        p = notify.Pushes(ntfy.url, "pcfd-xmpl-alerts", "", intel_topic="pcfd-xmpl-intel-thread", log=lambda m: None)
        e = cad.intel
        e.notifier, e.locate = p.notifier(notify.INTEL), example_locate
        e.listen = listen(r, b, Clock(now + 1))
        e.listen.worker = True                               # the listen thread writes the file after the push
        cad.comms.record({"id": "t1", "start": now, "end": now + 1, "source": "MRRS PD", "talkgroup": 9211, "system": "mrrs",
                          "source_key": "p25:mrrs:9211", "service": "police", "tier": "alert", "unit": "",
                          "text": "shots fired at Sample St and Test Rd", "status": "done", "model": "whisper"})
        e.consider("t1")
        e.drain()
        assert e.notifier.flush(5)
        [m] = ntfy.got
        body = m["body"]
        [act] = body["actions"]
        assert act["action"] == "view" and act["label"] == "Listen" and act["url"].startswith("https://bucket.example/listen/")
        assert body["message"].endswith(f"Listen: {act['url']}") and body["topic"] == "pcfd-xmpl-intel-thread"
    finally:
        ntfy.close()


def test_no_intel_topic_holds_the_push_and_makes_no_listen_clip(tmp_path):
    """Intel's own topic only (sizeup/notify.py): with none in effect the event is held (unsent) before any Listen link,
    so no clip is stitched, uploaded or presigned for a push that never goes."""
    from tests.test_notify import FakeNtfy
    ntfy = FakeNtfy()
    try:
        from tests.test_cad import make_cad
        cad = make_cad(tmp_path)
        now = cad.clock()
        r = Radio()
        r.add("t1", now, 1.0, 500)
        b = FakeBucket()
        logs = []
        p = notify.Pushes(ntfy.url, "pcfd-xmpl-alerts", "pcfd-xmpl-status", log=lambda m: None)    # no intel topic
        cad.pushes = p
        e = cad.intel
        e.notifier, e.locate, e.log = p.notifier(notify.INTEL), example_locate, logs.append
        e.listen = listen(r, b, Clock(now + 1))
        e.listen.worker = True
        cad.comms.record({"id": "t1", "start": now, "end": now + 1, "source": "MRRS PD", "talkgroup": 9211, "system": "mrrs",
                          "source_key": "p25:mrrs:9211", "service": "police", "tier": "alert", "unit": "",
                          "text": "shots fired at Sample St and Test Rd", "status": "done", "model": "whisper"})
        e.consider("t1")
        e.drain()
        [ev] = e.events.open_events()
        assert ev.suppressed == "unsent" and not ev.pushed_at
        assert b.puts == [] and b.signed == [] and ntfy.got == []
        assert any("no pre-alert topic in effect" in m for m in logs)
        assert cad.intel_admin.page(gate=False)["push_topic"] == {"set": False, "source": "none"}
        p.defaults[notify.INTEL] = ("pcfd-xmpl-intel-thread", "settings")
        assert cad.intel_admin.page(gate=False)["push_topic"] == {"set": True, "source": "settings"}
        assert "pcfd-xmpl" not in json.dumps(cad.intel_admin.page(gate=False)["push_topic"])
    finally:
        ntfy.close()


def test_ntfy_actions_are_https_only_and_at_most_three(monkeypatch):
    got = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"{}"

    def fake(req, timeout):
        got.update(json.loads(req.data))
        return R()
    monkeypatch.setattr(notify, "urlopen", fake)
    acts = [{"label": "Listen", "url": "https://a.example/x"}, {"label": "Bad", "url": "http://a.example/y"},
            {"label": "B", "url": "https://b.example"}, {"label": "C", "url": "https://c.example"},
            {"label": "D", "url": "https://d.example"}]
    notify.publish("https://ntfy.sh", "pcfd-xmpl-alerts", "t", "b", actions=acts)
    assert [a["label"] for a in got["actions"]] == ["Listen", "B", "C"]
    got.clear()
    notify.publish("https://ntfy.sh", "pcfd-xmpl-alerts", "t", "b")
    assert "actions" not in got


# -- the self-test of a pushed link -----------------------------------------------------------------------------------

SIG_XML = (b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>SignatureDoesNotMatch</Code><Message>The request '
           b'signature we calculated does not match the signature you provided.</Message></Error>')


class ProbeBucket(FakeBucket):
    """A bucket whose pushed link answers what the test says: (status, body), or an exception (unreachable)."""

    def __init__(self, answer=(206, b"R"), **kw):
        super().__init__(**kw)
        self.answer, self.probed = answer, []

    def probe(self, url, timeout):
        self.probed.append((url, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def checked(r, b, clock, logs, **cfg):
    """A Listen whose self-test thread is deferred: spawned holds what would run on it."""
    spawned = []
    lk = L.Listen(b, r.query, r.audio.get, settings=lambda: cfg, clock=clock, log=logs.append, ffmpeg="",
                  tiers=r.tiers, worker=False, spawn=spawned.append)
    return lk, spawned


def run(spawned):
    while spawned:
        spawned.pop(0)()


@pytest.mark.parametrize("status", [206, 200])
def test_the_self_test_gets_the_pushed_link_once_and_says_ok(status):
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b, logs = ProbeBucket((status, b"R")), []
    lk, spawned = checked(r, b, Clock(1001), logs)
    url = lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    assert b.probed == [] and spawned == []               # nothing in the push path: not even a thread yet
    lk.drain()
    assert b.puts and b.probed == [] and len(spawned) == 1   # after the upload, on a thread of its own
    run(spawned)
    assert b.probed == [(url, L.CHECK_TIMEOUT_S)]           # the very URL the push carries
    assert lk.status()["links"]["state"] == "ok" and lk.health()["text"] == "ok"
    assert logs == ["intel: listen links ok"]
    lk.link("intel:2", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    assert spawned == []                                    # ok: never again in this process


def test_a_refused_link_is_parsed_logged_once_and_rechecked_at_most_every_15_min():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b, logs, clock = ProbeBucket((403, SIG_XML)), [], Clock(1001)
    lk, spawned = checked(r, b, clock, logs)
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    run(spawned)
    ck = lk.status()["links"]
    assert ck["state"] == "failed" and ck["status"] == 403 and ck["code"] == "SignatureDoesNotMatch"
    h = lk.health()
    assert h["level"] == "warn" and h["text"] == "refused (SignatureDoesNotMatch)" and "403" in h["title"]
    assert logs == ["intel: listen links refused (SignatureDoesNotMatch)"]
    clock.t += 60                                           # another upload a minute later: not checked again yet
    lk.link("intel:2", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    assert spawned == [] and len(b.probed) == 1
    clock.t += L.CHECK_RETRY_S                              # past the rest: the next upload checks again
    lk.link("intel:3", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    run(spawned)
    assert len(b.probed) == 2 and logs == ["intel: listen links refused (SignatureDoesNotMatch)"]   # no change: no line
    b.answer = (206, b"R")                                  # fixed (sizeup/aws.py): ok, and a line saying so
    clock.t += L.CHECK_RETRY_S
    lk.link("intel:4", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    run(spawned)
    assert logs[-1] == "intel: listen links ok" and lk.status()["links"]["state"] == "ok"


def test_an_unreachable_link_is_unchecked_and_never_raises():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b, logs = ProbeBucket(TimeoutError("timed out")), []
    lk, spawned = checked(r, b, Clock(1001), logs)
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    run(spawned)
    ck = lk.status()["links"]
    assert ck["state"] == "unchecked" and ck["code"] == "TimeoutError" and ck["at"]
    assert lk.health()["level"] == "off" and logs == ["intel: listen links unchecked (TimeoutError)"]


def test_the_link_is_never_logged_nor_shown():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    logs = []
    for answer in [(206, b"R"), (403, SIG_XML), OSError("boom")]:
        b = ProbeBucket(answer)
        lk, spawned = checked(r, b, Clock(1001), logs)
        url = lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
        lk.drain()
        run(spawned)
        shown = json.dumps([lk.status(), lk.health()])
        key = urlsplit(url).path.lstrip("/")
        assert url not in shown and key not in shown and "X-Amz" not in shown
    assert logs and not [m for m in logs if "http" in m or "listen/" in m or "X-Amz" in m]


def test_the_s3_store_probes_with_a_ranged_get():
    sent = []

    def send(method, url, headers, body, timeout):
        sent.append((method, url, headers, timeout))
        return 403, {}, SIG_XML
    creds = aws.Credentials("AKIDEXAMPLE", "secret", "token")
    st = L.S3ListenStore("audio-bucket-example", "us-east-1", send=send, creds=lambda: creds)
    u = st.presign("listen/abc.wav", 3600, "audio/wav")
    status, body = st.probe(u, 5.0)
    [(method, url, headers, timeout)] = sent
    assert method == "GET" and url == u and headers == {"Range": "bytes=0-0"} and timeout == 5.0   # never HEAD
    assert status == 403 and L.s3_code(body) == "SignatureDoesNotMatch"
    assert L.s3_code(b"<Error><Code>Access<b>Denied</Code></Error>") == ""     # only a plain code
    assert L.s3_code(b"") == ""


# -- the cleanup: each file deleted once its last link has expired ----------------------------------------------------

class CleanBucket(FakeBucket):
    def __init__(self, listed=(), deny=False, fail=(), **kw):
        super().__init__(**kw)
        self.deleted, self.listed, self.deny, self.lists, self.batches = [], list(listed), deny, 0, []
        self.fail = set(fail)                              # keys S3 keeps failing (InternalError), not a 403

    def delete(self, keys):
        self.batches.append(list(keys))
        if self.deny:
            raise aws.AwsError("S3 DELETE: 403 AccessDenied", "AccessDenied", 403)
        self.deleted += [k for k in keys if k not in self.fail]
        return {k: "InternalError" for k in keys if k in self.fail}

    def list(self, prefix):
        self.lists += 1
        if self.deny:
            raise aws.AwsError("S3 LIST: 403 AccessDenied", "AccessDenied", 403)
        return [(k, t) for k, t in self.listed if k.startswith(prefix)]


def test_a_refreshed_key_is_deleted_only_after_its_last_link_expired():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b, clock = CleanBucket(), Clock(1002)
    lk = listen(r, b, clock, listen_expiry_min=60)
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    [key] = {k for k, _b, _c in b.puts}
    clock.t = 2000                                          # a repush of the same event: the same key signed again
    assert lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    first_expiry = 1002 + 3600
    clock.t = first_expiry + L.DELETE_MARGIN_S + 1          # the first link has expired, the second hasn't
    lk.refresh(clock.t)
    lk.drain()
    assert b.deleted == [] and "intel:1" in lk.items
    clock.t = 2000 + 3600 + 1                               # the last link expired: forgotten, its delete waiting
    lk.refresh(clock.t)
    lk.drain()
    assert lk.items == {} and b.deleted == [] and lk.status()["cleanup"]["pending"] == 1
    clock.t = 2000 + 3600 + L.DELETE_MARGIN_S
    lk.refresh(clock.t)
    assert b.deleted == []                                  # refresh() never deletes (the engine's thread)
    lk.drain()
    assert b.deleted == [key] and lk.status()["cleanup"] == {"state": "ok", "at": clock.t, "code": "", "pending": 0}


def test_the_push_path_never_deletes():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b, clock = CleanBucket(), Clock(1002)
    lk = listen(r, b, clock)
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    lk._deletes["listen/" + "x" * 32 + ".wav"] = 0.0         # long due
    lk.link("intel:2", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.refresh(clock.t)
    assert b.deleted == [] and b.lists == 0
    lk.drain()
    assert b.deleted == ["listen/" + "x" * 32 + ".wav"]


def test_a_process_that_never_leads_never_deletes_nor_sweeps():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b, clock = CleanBucket(listed=[("listen/" + "a" * 32 + ".wav", 1.0)]), Clock(1002)
    lk = L.Listen(b, r.query, r.audio.get, settings=lambda: {}, clock=clock, log=lambda m: None, ffmpeg="",
                  tiers=r.tiers, worker=False, spawn=lambda fn: None, leader=lambda: False)
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    for t in (1002 + 2 * 3600, 1002 + L.SWEEP_EXPIRY_S + 3600, 1002 + 2 * L.SWEEP_EXPIRY_S):
        clock.t = t
        lk.refresh(t)
        lk.drain()
        lk.clean(t)
    assert b.deleted == [] and b.lists == 0 and b.batches == [] and lk._led_since == 0.0


def test_the_sweep_waits_for_the_longest_expiry_the_setting_allows():
    """The last leader signed with 60 min; the setting is 20 now: its links still play for up to 60 (in general up to
    the range's max), so the sweep must not take them at 20."""
    from sizeup.intel import policy
    assert L.SWEEP_EXPIRY_S == policy.LIMITS["listen_expiry_min"][1] * 60
    old = "listen/" + "a" * 32 + ".wav"                    # signed at 49990 with 60 min: plays until 53590
    r = Radio()
    b, clock = CleanBucket(listed=[(old, 49990.0)]), Clock(50000)
    lk = listen(r, b, clock, listen_expiry_min=20)
    lk.refresh(clock.t)                                     # leads from 50000
    for t in (51320, 53590, 50000 + 3600 + L.DELETE_MARGIN_S, 50000 + L.SWEEP_EXPIRY_S):
        clock.t = t
        lk.refresh(t)
        lk.drain()
        assert b.lists == 0 and b.deleted == [], t
    clock.t = 50000 + L.SWEEP_EXPIRY_S + L.DELETE_MARGIN_S
    lk.refresh(clock.t)
    lk.drain()
    assert b.lists == 1 and b.deleted == [old]


def test_after_a_restart_the_sweep_deletes_only_older_files_it_does_not_know():
    old, newer = "listen/" + "a" * 32 + ".wav", "listen/" + "b" * 32 + ".wav"
    r = Radio()
    r.add("hit", 50000, 1.0, 500)
    b, clock = CleanBucket(listed=[(old, 40000.0), (newer, 50500.0), ("audio/clip/x.wav", 1.0)]), Clock(50000)
    lk = listen(r, b, clock, listen_expiry_min=60)
    lk.refresh(clock.t)                                     # leads from 50000
    lk.drain()
    assert b.lists == 0                                     # a link the last leader signed may still play
    clock.t = 50000 + L.SWEEP_EXPIRY_S + L.DELETE_MARGIN_S - 60
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], clock.t, clock.t)
    r.add("hit2", clock.t, 1.0, 500)
    lk.link("intel:2", ["p25:mrrs:9211"], ["hit2"], clock.t, clock.t)
    lk.drain()
    mine = b.puts[0][0]
    b.listed.append((mine, 49999.0))                       # even one listed as older: its own, never swept
    clock.t += 60
    lk.refresh(clock.t)
    lk.drain()
    assert b.lists == 1 and b.deleted == [old]              # not the one written since, not its own, nothing outside listen/
    clock.t += 3600
    lk.refresh(clock.t)
    lk.drain()
    assert b.lists == 1                                     # once a process


def test_deletes_go_in_batches_one_a_pass():
    b, clock = CleanBucket(), Clock(1000)
    lk = listen(Radio(), b, clock)
    lk.refresh(clock.t)
    keys = [f"listen/{i:032d}.wav" for i in range(L.DELETE_BATCH + 5)]
    lk._deletes.update({k: 1.0 for k in keys})
    assert lk.clean() == L.DELETE_BATCH and [len(x) for x in b.batches] == [L.DELETE_BATCH]
    assert lk.clean() == 5 and sorted(b.deleted) == sorted(keys) and lk._deletes == {}


def test_a_key_that_keeps_failing_goes_to_the_back_and_is_dropped_after_a_few_tries():
    bad, good = "listen/" + "a" * 32 + ".wav", "listen/" + "b" * 32 + ".wav"
    b, clock, logs = CleanBucket(fail=[bad]), Clock(1000), []
    lk = L.Listen(b, None, None, clock=clock, log=logs.append, worker=False, spawn=lambda fn: None)
    lk.refresh(clock.t)
    lk._deletes.update({bad: 1.0, good: 2.0})
    lk.clean()
    assert b.deleted == [good] and lk._deletes[bad] == clock.t + L.RETRY_SOON_S     # the good one not held up
    lk._deletes[good + "2"] = 3.0
    lk.clean()
    assert b.batches[-1] == [good + "2"]                    # the failing key waits at the back
    for _ in range(L.DELETE_TRIES):
        clock.t += L.RETRY_SOON_S
        lk.clean()
    assert bad not in lk._deletes and b.deleted == [good, good + "2"] and lk.cleanup["state"] == "ok"
    assert not [m for m in logs if "not permitted" in m]


def test_access_denied_logs_once_rests_and_the_health_row_says_so():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    logs = []
    b, clock = CleanBucket(deny=True), Clock(1002)
    lk = L.Listen(b, r.query, r.audio.get, settings=lambda: {"listen_expiry_min": 1}, clock=clock, log=logs.append,
                  ffmpeg="", tiers=r.tiers, worker=False, spawn=lambda fn: None)
    lk.link("intel:1", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    lk.drain()
    clock.t = 1002 + 60 + L.LATE_S + L.DELETE_MARGIN_S + 1
    lk.refresh(clock.t)
    lk.drain()
    assert logs == ["intel: listen cleanup not permitted, relying on the 1-day lifecycle"]
    h = lk.health()
    assert h["level"] == "warn" and h["text"].endswith("cleanup not permitted") and "lifecycle" in h["title"]
    assert lk.status()["cleanup"]["state"] == "denied" and lk.status()["cleanup"]["pending"] == 1
    tries = len(b.batches)
    clock.t += 60
    lk.refresh(clock.t)
    lk.drain()
    assert len(b.batches) == tries and len(logs) == 1       # resting: not tried again
    clock.t += L.CLEANUP_REST_S
    lk.refresh(clock.t)
    lk.drain()
    assert len(b.batches) == tries + 1 and len(logs) == 1   # tried again, refused again: no new line
    b.deny = False                                          # the grant (sizeup-aws 27) is there now
    clock.t += L.CLEANUP_REST_S
    lk.refresh(clock.t)
    lk.drain()
    assert b.deleted and logs[-1] == "intel: listen cleanup ok" and lk.health()["text"] == "unchecked"


def test_a_per_key_access_denied_in_a_batch_rests_the_cleanup_too():
    class PerKey(CleanBucket):
        def delete(self, keys):
            self.batches.append(list(keys))
            return {k: "AccessDenied" for k in keys}
    b, clock, logs = PerKey(), Clock(1000), []
    lk = L.Listen(b, None, None, clock=clock, log=logs.append, worker=False, spawn=lambda fn: None)
    lk.refresh(clock.t)
    lk._deletes["listen/" + "a" * 32 + ".wav"] = 1.0
    lk.clean()
    assert lk.cleanup["state"] == "denied" and lk._deletes and logs == [
        "intel: listen cleanup not permitted, relying on the 1-day lifecycle"]
    lk.clean()
    assert len(b.batches) == 1                              # resting


def test_the_s3_store_deletes_in_one_request_and_lists_listen_only():
    sent = []
    page = (b'<?xml version="1.0"?><ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>listen/abc.wav</Key>'
            b'<LastModified>2026-11-17T10:00:00.000Z</LastModified></Contents></ListBucketResult>')
    result = (b'<?xml version="1.0"?><DeleteResult><Error><Key>listen/bad.wav</Key><Code>InternalError</Code>'
              b'</Error></DeleteResult>')

    def send(method, url, headers, body, timeout):
        sent.append((method, url, headers, body))
        if method == "POST":
            if b"denied" in body:
                return 403, {}, b"<Error><Code>AccessDenied</Code></Error>"
            return 200, {}, result
        return 200, {}, page
    creds = aws.Credentials("AKIDEXAMPLE", "secret", "token")
    st = L.S3ListenStore("audio-bucket-example", "us-east-1", send=send, creds=lambda: creds)
    assert st.delete(["listen/abc.wav", "listen/bad.wav"]) == {"listen/bad.wav": "InternalError"}
    method, url, headers, body = sent[0]
    assert method == "POST" and url.endswith("/?delete=") and b"<Key>listen/abc.wav</Key>" in body
    assert headers.get("Content-MD5")
    with pytest.raises(aws.AwsError) as e:
        st.delete(["listen/denied.wav"])
    assert e.value.status == 403 and e.value.code == "AccessDenied" and L.refused(e.value)
    assert st.delete([]) == {} and len(sent) == 2
    [(key, at)] = st.list()
    assert key == "listen/abc.wav" and at == datetime(2026, 11, 17, 10, tzinfo=timezone.utc).timestamp()
    assert "prefix=listen%2F" in sent[-1][1] and "list-type=2" in sent[-1][1]
