"""Amazon Transcribe streaming (sizeup/transcribe.py) and the AWS plumbing (sizeup/aws.py), against
local fakes only (tests/fake_transcribe.py): SigV4 against AWS's published examples, the event-stream
framing, the WebSocket client, one stream per channel with results mapped back to each transmission,
idle close / reopen, errors, the monthly budget and the custom vocabulary."""

import json
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from sizeup import aws
from sizeup import transcribe as tr
from sizeup.transcribe import (OP_BINARY, OP_CLOSE, OP_PING, OP_PONG, AwsTranscriber, EventStreamError, Settings, Usage,
                               Vocabulary, WebSocket, audio_event, build_vocabulary, decode_message, encode_message,
                               respell, spoken_number, street_spoken, to_aws)
from tests.fake_transcribe import FakeTranscribe, FakeTranscribeApi, tone8k


@pytest.fixture(autouse=True)
def fake_creds(monkeypatch):
    """Test credentials only (never the machine's); and never a real AWS host."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "test-session-token")
    real = WebSocket.connect

    def local_only(url, *a, **kw):
        assert urlsplit(url).hostname == "127.0.0.1", f"a test tried to reach {url}"
        return real(url, *a, **kw)
    monkeypatch.setattr(WebSocket, "connect", local_only)


def wait(cond, s=8.0):
    end = time.time() + s
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


class Results:
    def __init__(self):
        self.done, self.failed = {}, {}

    def ok(self, item, text, conf):
        self.done[item] = (text, conf)

    def fail(self, item, why):
        self.failed[item] = why


def service(fake, res, kv=None, **kw):
    kv = {} if kv is None else kv
    s = Settings(endpoint=fake.url, pace=50, quiet_s=0.4, settle_s=0.4, flush_s=0.6, **kw)
    return AwsTranscriber(s, Usage(kv.get, kv.__setitem__), res.ok, res.fail, log=lambda m: None)


# -- SigV4 ------------------------------------------------------------------------------------------------


def test_sigv4_matches_the_aws_examples():
    # AWS's S3 presigned-URL example (query authentication).
    c = aws.Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    u = aws.presign("https://examplebucket.s3.amazonaws.com/test.txt", c, "us-east-1", "s3", 86400,
                    datetime(2013, 5, 24, tzinfo=timezone.utc), "UNSIGNED-PAYLOAD")
    assert u.endswith("&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in u
    # The SigV4 test suite's get-vanilla (header authentication).
    h = aws.sign("GET", "https://example.amazonaws.com/", {}, b"", aws.Credentials("AKIDEXAMPLE",
                 "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"), "us-east-1", "service", datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc))
    assert h["Authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                                  "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


def test_the_streaming_url_is_presigned_with_the_session_token_inside_the_signature():
    t = AwsTranscriber(Settings(region="us-east-2"), Usage(lambda k: None, lambda k, v: None), print, print)
    t.vocabulary = "sizeup-pcfd"
    u = urlsplit(t.url())
    q = parse_qs(u.query)
    assert (u.scheme, u.hostname, u.port, u.path) == ("wss", "transcribestreaming.us-east-2.amazonaws.com", 8443,
                                                       "/stream-transcription-websocket")
    assert q["language-code"] == ["en-US"] and q["media-encoding"] == ["pcm"] and q["sample-rate"] == ["8000"]
    assert q["vocabulary-name"] == ["sizeup-pcfd"] and q["X-Amz-SignedHeaders"] == ["host"]
    assert q["X-Amz-Security-Token"] == ["test-session-token"] and q["X-Amz-Expires"] == ["300"]
    assert q["X-Amz-Credential"][0].endswith("/us-east-2/transcribe/aws4_request")
    # the signature covers everything but itself: re-signing the same URL at the same time gives the same one
    at = datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    base = "wss://transcribestreaming.us-east-2.amazonaws.com:8443/stream-transcription-websocket?" + "&".join(
        f"{k}={v[0]}" for k, v in q.items() if not k.startswith("X-Amz-"))
    again = aws.presign(base, aws.credentials(), "us-east-2", "transcribe", 300, at)
    assert parse_qs(urlsplit(again).query)["X-Amz-Signature"] == q["X-Amz-Signature"]


def test_credentials_from_the_ecs_container_endpoint_are_cached_and_refreshed(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            exp = datetime.fromtimestamp(time.time() + 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = json.dumps({"AccessKeyId": f"ASIA{len(hits)}", "SecretAccessKey": "s", "Token": "t", "Expiration": exp}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", f"http://127.0.0.1:{srv.server_address[1]}/creds")
        monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN", "auth-token")
        aws.forget_credentials()
        c = aws.credentials()
        assert (c.access_key, c.token) == ("ASIA1", "t") and aws.credentials().access_key == "ASIA1"
        assert hits == [("/creds", "auth-token")]                       # cached
        assert aws.credentials(now=lambda: time.time() + 3500).access_key == "ASIA2"   # refreshed before expiry
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://example.com/creds")
        aws.forget_credentials()
        with pytest.raises(aws.AwsError):
            aws.credentials()
    finally:
        aws.forget_credentials()
        srv.shutdown()


# -- event stream --------------------------------------------------------------------------------------------


def test_event_stream_round_trip_and_crc():
    pcm = np.arange(-400, 400, dtype=np.int16).tobytes()
    msg = audio_event(pcm)
    headers, payload, n = decode_message(msg)
    assert headers == {":content-type": "application/octet-stream", ":event-type": "AudioEvent", ":message-type": "event"}
    assert payload == pcm and n == len(msg)
    total, hlen = struct.unpack(">II", msg[:8])
    assert total == len(msg) and hlen == len(msg) - 16 - len(pcm)
    for i in (5, 20, len(msg) - 10):                       # a flipped byte anywhere: refused
        bad = bytearray(msg)
        bad[i] ^= 0xFF
        with pytest.raises(EventStreamError):
            decode_message(bytes(bad))
    with pytest.raises(EventStreamError):
        decode_message(msg[:-3])
    # the header types AWS may send
    hb = (b"\x02:a\x00" + b"\x02:b\x01" + b"\x02:c\x02\x7f" + b"\x02:d\x03\x01\x00" + b"\x02:e\x04\x00\x00\x01\x00"
          + b"\x02:f\x05" + (7).to_bytes(8, "big") + b"\x02:g\x06\x00\x02hi" + b"\x02:h\x09" + bytes(range(16)))
    import zlib
    prelude = struct.pack(">II", 16 + len(hb) + 3, len(hb))
    raw = prelude + struct.pack(">I", zlib.crc32(prelude)) + hb + b"xyz"
    raw += struct.pack(">I", zlib.crc32(raw))
    h, p, _ = decode_message(raw)
    assert h == {":a": True, ":b": False, ":c": 127, ":d": 256, ":e": 256, ":f": 7, ":g": b"hi", ":h": bytes(range(16)).hex()}
    assert p == b"xyz"
    assert decode_message(encode_message({"k": "v"}, b""))[:2] == ({"k": "v"}, b"")


# -- the WebSocket client --------------------------------------------------------------------------------------


def _server_frame(op, payload, fin=True):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
    return head + payload


def test_websocket_frames_masking_ping_pong_fragments_and_close():
    a, b = socket.socketpair()
    ws = WebSocket(a)
    try:
        ws.send_binary(b"hello" * 40)                              # 200 bytes: 16-bit length, masked
        data = b.recv(4096)
        assert data[0] == 0x80 | OP_BINARY and data[1] == 0x80 | 126 and struct.unpack(">H", data[2:4])[0] == 200
        key, body = data[4:8], data[8:]
        assert body != b"hello" * 40 and bytes(x ^ key[i % 4] for i, x in enumerate(body)) == b"hello" * 40
        # a ping in between two fragments of one message: answered, and the message comes whole
        b.sendall(_server_frame(OP_BINARY, b"abc", fin=False) + _server_frame(OP_PING, b"p1") + _server_frame(0, b"def"))
        assert ws.recv() == (OP_BINARY, b"abcdef")
        pong = b.recv(4096)
        assert pong[0] == 0x80 | OP_PONG
        assert bytes(x ^ pong[2 + i % 4] for i, x in enumerate(pong[6:])) == b"p1"
        # bytes arriving one at a time are kept until the frame is whole
        frame = _server_frame(OP_BINARY, b"slow")
        b.settimeout(1)
        a.settimeout(0.05)
        for i, x in enumerate(frame[:-1]):
            b.sendall(bytes([x]))
            with pytest.raises(TimeoutError):
                ws.recv()
        b.sendall(frame[-1:])
        assert ws.recv() == (OP_BINARY, b"slow")
        b.sendall(_server_frame(OP_CLOSE, struct.pack(">H", 1000) + b"bye"))
        assert ws.recv() == (OP_CLOSE, struct.pack(">H", 1000) + b"bye")
    finally:
        ws.close()
        b.close()


def test_websocket_handshake_refusal_says_why():
    fake = FakeTranscribe(refuse=403)
    try:
        with pytest.raises(tr.WsHandshakeError) as e:
            WebSocket.connect(fake.url + "/stream-transcription-websocket?x=1")
        assert e.value.status == 403 and "refused" in e.value.body
    finally:
        fake.close()


# -- streams --------------------------------------------------------------------------------------------------


def test_one_stream_feeds_several_transmissions_and_maps_results_back():
    fake = FakeTranscribe(merge=True)        # results that run over two transmissions: split by word time
    res, kv = Results(), {}
    t = service(fake, res, kv)
    try:
        t.submit("tg:307", "a", tone8k(400, 1.2))
        t.submit("tg:307", "b", tone8k(600, 0.8))
        t.submit("tg:307", "c", tone8k(800, 1.0))
        t.submit("FG1", "x", tone8k(1000, 1.0))                 # another channel: its own stream
        assert wait(lambda: len(res.done) == 4), res.__dict__
        assert res.done["a"] == ("Engine 60 on location nothing showing.", 0.9)
        assert res.done["b"][0] == "Ladder 31 responding."
        assert res.done["c"][0] == "60 Control copy."
        assert res.done["x"][0] == "Port Chester Fire respond to an activated fire alarm."
        assert len(fake.conns) == 2 and {c.query["sample-rate"] for c in fake.conns} == {"8000"}
        tg = next(c for c in fake.conns if len(c.bursts) == 3)
        # back to back: the three transmissions + 0.3 s gaps (+ a little silence to close the last result)
        assert [round(b - a, 1) for a, b, _ in tg.bursts] == [1.2, 0.8, 1.0]
        assert tg.bursts[1][0] - tg.bursts[0][1] == pytest.approx(0.3, abs=0.02)
        assert 3.9 <= tg.audio_s <= 3.9 + 0.6 + 0.2
        assert not res.failed
    finally:
        t.stop()
        fake.close()
    m = Usage(kv.get, kv.__setitem__).month()
    assert m["streams"] == 2 and m["billed_s"] == 30.0          # 15 s minimum each
    assert m["audio_s"] == pytest.approx(sum(c.audio_s for c in fake.conns), abs=0.01)


def test_an_idle_stream_closes_and_a_new_one_opens_on_demand():
    fake = FakeTranscribe()
    res, kv = Results(), {}
    t = service(fake, res, kv, idle_s=0.5)
    try:
        t.submit("FG1", "a", tone8k(400, 0.6))
        assert wait(lambda: "a" in res.done)
        assert wait(lambda: fake.conns[0].ended and fake.conns[0].closed_at)       # ended cleanly (empty AudioEvent)
        assert t.spent()["streams_open"] == 0 and Usage(kv.get, kv.__setitem__).month()["streams"] == 1
        t.submit("FG1", "b", tone8k(600, 0.6))
        assert wait(lambda: "b" in res.done) and res.done["b"][0] == "Ladder 31 responding."
        assert len(fake.conns) == 2
    finally:
        t.stop()
        fake.close()


def test_a_waiting_stream_is_kept_open_with_a_little_silence(monkeypatch):
    monkeypatch.setattr(tr, "KEEPALIVE_S", 0.3)
    fake = FakeTranscribe()
    res = Results()
    t = service(fake, res, idle_s=60)
    try:
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.done)
        before = fake.conns[0].audio_s
        time.sleep(1.5)
        after = fake.conns[0].audio_s
        assert not fake.conns[0].ended and 0.2 <= after - before <= 0.7       # ~0.1 s every 0.3 s, not real time
        t.submit("FG1", "b", tone8k(800, 0.5))
        assert wait(lambda: "b" in res.done) and len(fake.conns) == 1          # the same stream
    finally:
        t.stop()
        fake.close()


def test_a_stream_near_the_four_hour_limit_is_replaced_between_transmissions(monkeypatch):
    monkeypatch.setattr(tr, "MAX_STREAM_S", 2.0)
    fake = FakeTranscribe()
    res = Results()
    t = service(fake, res)
    try:
        t.submit("FG1", "a", tone8k(400, 1.2))
        t.submit("FG1", "b", tone8k(600, 1.2))
        assert wait(lambda: len(res.done) == 2)
        assert len(fake.conns) == 2 and fake.conns[0].ended
        assert res.done["b"][0] == "Ladder 31 responding."
    finally:
        t.stop()
        fake.close()


def test_errors_are_retried_once_then_handed_back_never_dropped():
    fake = FakeTranscribe(fail_after_s=0.3)          # ServiceUnavailableException mid-stream, every time
    res = Results()
    t = service(fake, res)
    try:
        t.submit("FG1", "a", tone8k(400, 1.0))
        assert wait(lambda: "a" in res.failed, 12)
        assert "ServiceUnavailableException" in res.failed["a"] and len(fake.conns) == 2
        assert t.status()["last_error"].startswith("AWS Transcribe: ServiceUnavailableException")
    finally:
        t.stop()
        fake.close()
    denied = FakeTranscribe(refuse=403)               # no permission: at once, no retry
    res = Results()
    t = service(denied, res)
    try:
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.failed) and "AccessDenied" in res.failed["a"]
        assert not res.done
    finally:
        t.stop()
        denied.close()


def test_the_monthly_budget_stops_new_audio():
    fake = FakeTranscribe()
    res, kv = Results(), {}
    usage = Usage(kv.get, kv.__setitem__)
    usage.add(25 / 0.024 * 60 - 10, 0)                # $25 less 10 s worth spent this month
    t = AwsTranscriber(Settings(endpoint=fake.url, pace=50), usage, res.ok, res.fail, log=lambda m: None,
                       limit_usd=lambda: 25.0)
    try:
        ok, why = t.available()
        assert not ok and "monthly AWS limit" in why and "$25" in why
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.failed) and "monthly AWS limit" in res.failed["a"]
        assert not fake.conns                         # nothing was sent
        t.limit_usd = lambda: 30.0                    # Settings raised the limit
        assert t.available() == (True, "")
        # a new month starts from zero
        usage.now = lambda: time.time() + 40 * 86400
        assert usage.month()["billed_s"] == 0
    finally:
        t.stop()
        fake.close()


def test_no_credentials_means_not_available(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
              "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        monkeypatch.delenv(k, raising=False)
    t = AwsTranscriber(Settings(), Usage(lambda k: None, lambda k, v: None), print, print)
    ok, why = t.available()
    assert not ok and "no AWS credentials" in why


# -- the custom vocabulary ----------------------------------------------------------------------------------


def test_phrases_follow_the_aws_rules():
    assert [spoken_number(x) for x in ("7", "15", "60", "31", "05", "401", "300", "2398", "1500", "2304", "12345")] == [
        "seven", "fifteen", "sixty", "thirty one", "oh five", "four oh one", "three hundred", "twenty three ninety eight",
        "fifteen hundred", "twenty three oh four", "one two three four five"]
    assert to_aws("Engine 60") == ("Engine-sixty", "")
    assert to_aws("23 98") == ("twenty-three-ninety-eight", "")
    assert to_aws("Fire/EMS 15") == ("Fire-EMS-fifteen", "")
    assert to_aws("77 Alpha 4") == ("seventy-seven-Alpha-four", "")
    assert to_aws("St. Mary's & E.M.S.") == ("St-Mary's-and-E.M.S.", "")
    assert to_aws("?!") == (None, "nothing left to say")
    assert to_aws("x " * 200)[0] is None
    b = build_vocabulary(["Engine 60", "engine 60", "Rye Brook", "", "#", "Tower Ladder 2"])
    assert b["phrases"] == ["Engine-sixty", "Rye-Brook", "Tower-Ladder-two"]
    assert b["skipped"] == [{"phrase": "#", "why": "nothing left to say"}]
    assert all(not any(c.isdigit() or c == " " for c in p) for p in b["phrases"])
    assert respell("engine sixty on location, Tower Ladder two responding", b["respell"]) == \
        "Engine 60 on location, Tower Ladder 2 responding"
    assert street_spoken("N Main St") == "North Main Street" and street_spoken("Doral Greens Dr W") == "Doral Greens Drive West"


def test_vocabulary_create_then_update_polled_until_ready():
    api = FakeTranscribeApi(pending_polls=1)
    kv, ready = {}, []
    v = Vocabulary("us-east-1", kv.get, kv.__setitem__, endpoint=api.url, log=lambda m: None, poll_s=0.05,
                   on_ready=ready.append)
    try:
        assert v.state()["state"] == "none"
        st = v.push(["Engine 60", "Rye Brook", "23 98"])
        assert st["state"] == "PENDING" and st["phrases"] == 3
        assert wait(lambda: v.state()["state"] == "READY") and ready == ["sizeup-pcfd"]
        assert [c[0] for c in api.calls[:2]] == ["GetVocabulary", "CreateVocabulary"]
        create = api.calls[1][1]
        assert create == {"VocabularyName": "sizeup-pcfd", "LanguageCode": "en-US",
                          "Phrases": ["Engine-sixty", "Rye-Brook", "twenty-three-ninety-eight"]}
        assert v.state()["respell"] == {"engine sixty": "Engine 60", "twenty three ninety eight": "23 98"}
        v.push(["Engine 61"])                             # it exists now: Update
        assert "UpdateVocabulary" in [c[0] for c in api.calls]
        assert wait(lambda: v.state()["state"] == "READY")
        assert api.vocabularies["sizeup-pcfd"]["phrases"] == ["Engine-sixty-one"]
        # AWS refusing says why, nothing pretends to be ready
        v2 = Vocabulary("us-east-1", kv.get, kv.__setitem__, name="sizeup-x", endpoint=api.url + "/nope", log=lambda m: None)
        v2.call = lambda *a, **k: (_ for _ in ()).throw(aws.AwsError("AccessDeniedException: no", "AccessDeniedException", 400))
        assert v2.push(["Engine 60"])["state"] == "error"
    finally:
        api.close()


def test_the_vocabulary_is_used_by_new_streams_once_ready_and_respelled():
    fake = FakeTranscribe(phrases={400: "engine sixty on location"})
    res = Results()
    kv = {}
    t = AwsTranscriber(Settings(endpoint=fake.url, pace=50, quiet_s=0.4), Usage(kv.get, kv.__setitem__), res.ok, res.fail,
                       log=lambda m: None, respell=lambda s: respell(s, {"engine sixty": "Engine 60"}))
    try:
        t.vocabulary = "sizeup-pcfd"
        t.submit("FG1", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.done)
        assert res.done["a"][0] == "Engine 60 on location." and fake.conns[0].query["vocabulary-name"] == "sizeup-pcfd"
    finally:
        t.stop()
        fake.close()
