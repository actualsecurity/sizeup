"""The radio pre-alert push's "Listen" link (sizeup/intel/listen.py): synthetic tones only (no real audio), a fake bucket,
fake credentials, a fake ntfy."""

from __future__ import annotations

import io
import json
import re
import wave
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


def listen(radio, bucket, clock, **cfg):
    return L.Listen(bucket, radio.query, radio.audio.get, settings=lambda: cfg, clock=clock, log=lambda m: None,
                    ffmpeg="")


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
    assert url
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
    assert lk.items["intel:1"].clips == ("pre", "hit")


def test_later_transmissions_restitch_to_the_same_key():
    r = Radio()
    r.add("hit", 1000, 1.0, 500)
    b = FakeBucket()
    clock = Clock(1002)
    lk = listen(r, b, clock)
    url = lk.link("intel:7", ["p25:mrrs:9211"], ["hit"], 1000, 1000)
    assert lk.refresh(1010) == 0                          # nothing new: no upload
    r.add("readback", 1030, 1.5, 650)
    clock.t = 1035
    assert lk.refresh(1035) == 1
    assert [k for k, _b, _c in b.puts] == [b.puts[0][0]] * 2 and segments(b.puts[-1][1]) == [1.0, 1.5]
    assert lk.link("intel:7", ["p25:mrrs:9211"], ["hit", "readback"], 1000, 1030).split("?")[0] == url.split("?")[0]
    clock.t = 1000 + 60 + L.LATE_S + 20 * 60 + 1
    lk.refresh(clock.t)
    assert lk.items == {}


def test_keys_are_random_and_carry_nothing_of_the_event():
    r = Radio()
    r.add("9211-1790000000.1_0.0-call_5", 1000, 1.0, 500)
    b = FakeBucket()
    lk = listen(r, b, Clock(1001))
    lk.link("intel:42", ["p25:mrrs:9211"], ["9211-1790000000.1_0.0-call_5"], 1000, 1000)
    lk.link("intel:43", ["p25:mrrs:9211"], ["9211-1790000000.1_0.0-call_5"], 1000, 1000)
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
    assert bad.link("i", ["p25:mrrs:9211"], ["hit"], 1000, 1000) is None
    st = bad.status()
    assert not st["configured"] and "27" in st["why"]
    assert listen(r, FakeBucket(), Clock(1001)).link("i", ["p25:mrrs:9999"], ["gone"], 1000, 1000) is None  # no audio


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
        p = notify.Pushes(ntfy.url, "pcfd-xmpl-alerts", "", log=lambda m: None)
        e = cad.intel
        e.notifier, e.locate = p.notifier(notify.INTEL), example_locate
        e.listen = listen(r, b, Clock(now + 1))
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
        assert body["message"].endswith(f"Listen: {act['url']}") and body["topic"] == "pcfd-xmpl-alerts"
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
