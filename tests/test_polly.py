"""Amazon Polly, the hub's dispatch voice (sizeup/polly.py). No network: a fake Polly and a fake
container credentials endpoint on 127.0.0.1, fake credentials."""

import http.server
import json
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from sizeup import polly, tts
from sizeup.cad import dispatcher
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cad.service import VOICE_SAMPLE, Cad, Settings
from sizeup.cad.store import CadError
from sizeup.config import ConfigError, load
from tests.test_tts import TONES, A, peak_hz, read16k

ROOT = Path(__file__).resolve().parent.parent
AKID, SECRET = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def pcm(secs=1.0, hz=440.0) -> bytes:
    t = np.arange(int(secs * 16000)) / 16000
    return (0.3 * np.sin(2 * np.pi * hz * t) * 32767).astype("<i2").tobytes()


def serve(handler) -> tuple[http.server.ThreadingHTTPServer, str]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def fake_polly(monkeypatch):
    """A Polly stand-in: checks each request's SigV4 signature, answers 1 s of 440 Hz PCM per request.
    state: deny (403 AccessDenied), unsupported {engine: error type} (400), bad_ssml {engines} (400)."""
    state = {"requests": [], "deny": False, "unsupported": {}, "bad_ssml": set(), "bad_sig": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(data)
            state["requests"].append(body)
            at = datetime.strptime(self.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            want = polly.sign("POST", f"http://{self.headers['Host']}{self.path}", {"Content-Type": self.headers["Content-Type"]},
                              data, polly.Credentials(AKID, SECRET, self.headers.get("X-Amz-Security-Token", "")),
                              "us-east-1", "polly", at)["Authorization"]
            if self.path != "/v1/speech" or self.headers["Authorization"] != want:
                state["bad_sig"] += 1
                return self._answer(403, {"message": "The request signature we calculated does not match"},
                                    "InvalidSignatureException")
            if state["deny"]:
                return self._answer(403, {"Message": "User: arn:aws:sts::1:assumed-role/sizeup-hub-task/x is not "
                                          "authorized to perform: polly:SynthesizeSpeech"}, "AccessDeniedException")
            if body["Engine"] in state["unsupported"]:
                return self._answer(400, {"message": f"This voice does not support the selected engine: {body['Engine']}"},
                                    state["unsupported"][body["Engine"]])
            if body["TextType"] == "ssml" and body["Engine"] in state["bad_ssml"]:
                return self._answer(400, {"message": "Unsupported SSML tag: prosody"}, "InvalidSsmlException")
            self._answer(200, pcm(), "")

        def _answer(self, code, obj, err):
            out = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "audio/pcm" if code == 200 else "application/json")
            if err:
                self.send_header("x-amzn-ErrorType", f"{err}:http://internal.amazon.com/coral/com.amazonaws.polly/")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv, url = serve(H)
    for k in ("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
              "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIZEUP_POLLY_ENDPOINT", url)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", AKID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(polly, "_works", {})
    polly.forget_credentials()
    yield state
    srv.shutdown()
    polly.forget_credentials()


def voice(name="Matthew", speed=0.92):
    return tts.VoiceCfg("polly", name, speed)


# -- signing ------------------------------------------------------------------------------------------

def test_sigv4_matches_the_aws_test_suite():
    """AWS's published SigV4 test suite (aws-sig-v4-test-suite: get-vanilla,
    get-vanilla-query-order-key-case, post-vanilla)."""
    c = polly.Credentials(AKID, SECRET)
    at = datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc)
    for method, url, sig in [
            ("GET", "https://example.amazonaws.com/", "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"),
            ("GET", "https://example.amazonaws.com/?Param2=value2&Param1=value1",
             "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"),
            ("POST", "https://example.amazonaws.com/", "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b")]:
        h = polly.sign(method, url, {}, b"", c, "us-east-1", "service", at)
        assert h["Authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                                      f"SignedHeaders=host;x-amz-date, Signature={sig}")
        assert h["X-Amz-Date"] == "20150830T123600Z" and h["Host"] == "example.amazonaws.com"


def test_sigv4_polly_request_matches_the_aws_cli():
    """A SynthesizeSpeech request with a session token, as the AWS CLI (botocore) signed it."""
    body = (b'{"Engine": "generative", "OutputFormat": "pcm", "SampleRate": "16000", "Text": "Engine 60 & <x>", '
            b'"VoiceId": "Matthew"}')
    h = polly.sign("POST", "http://127.0.0.1:18765/v1/speech", {"Content-Type": "application/json"}, body,
                   polly.Credentials(AKID, SECRET, "TOKEN123"), "us-east-1", "polly",
                   datetime(2026, 10, 8, 19, 1, 9, tzinfo=timezone.utc))
    assert h["X-Amz-Security-Token"] == "TOKEN123"
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261008/us-east-1/polly/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token, "
        "Signature=e269534393eae83086f4e8015485efe71378eeced69b4299eb1e95f5c7347c66")


# -- credentials and region ------------------------------------------------------------------------------

def test_credentials_from_the_environment_then_the_container_endpoint(monkeypatch):
    hits = []
    expires = ["2026-10-08T12:00:00Z"]

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            out = json.dumps({"AccessKeyId": f"ASIA{len(hits)}", "SecretAccessKey": "s", "Token": f"tok{len(hits)}",
                              "Expiration": expires[0]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv, url = serve(H)
    try:
        polly.forget_credentials()
        for k in ("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDENV")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", url + "/creds")
        c = polly.credentials()
        assert (c.access_key, c.token) == ("AKIDENV", "") and hits == []            # the environment wins
        monkeypatch.delenv("AWS_ACCESS_KEY_ID")
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
        monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN", "auth-token")
        t0 = datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc).timestamp()
        c = polly.credentials(lambda: t0)
        assert (c.access_key, c.token) == ("ASIA1", "tok1") and hits == [("/creds", "auth-token")]
        assert polly.credentials(lambda: t0 + 3000).access_key == "ASIA1" and len(hits) == 1      # cached
        expires[0] = "2026-10-08T18:00:00Z"
        c = polly.credentials(lambda: t0 + 3301)                                       # within 5 min of expiry
        assert (c.access_key, len(hits)) == ("ASIA2", 2)
        assert polly.credentials(lambda: t0 + 3400).access_key == "ASIA2" and len(hits) == 2
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://example.com/creds")
        polly.forget_credentials()
        with pytest.raises(polly.PollyError, match="container's own endpoint"):
            polly.credentials()
        monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/abc")
        assert polly._container_uri() == ("http://169.254.170.2/v2/credentials/abc", {})   # what ECS sets
        monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        with pytest.raises(polly.PollyError, match="no AWS credentials"):
            polly.credentials()
    finally:
        srv.shutdown()
        polly.forget_credentials()


def test_region(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert polly.region() == "us-east-1"
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert polly.region() == "us-west-2"
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    assert polly.region() == "us-east-2" and polly.region("eu-west-1") == "eu-west-1"
    monkeypatch.delenv("SIZEUP_POLLY_ENDPOINT", raising=False)
    assert polly.endpoint("us-east-1") == "https://polly.us-east-1.amazonaws.com"


# -- rendering --------------------------------------------------------------------------------------------

def test_polly_voice_after_the_tones_read_twice_time_out_once(tmp_path, fake_polly):
    path, label = dispatcher.render_dispatch(A, TONES, tmp_path, voice())
    assert label == "Voice: Polly Matthew" and fake_polly["bad_sig"] == 0
    body, tail = dispatcher.split_time_out(dispatcher.speech(A))
    r = fake_polly["requests"]
    assert [(x["Engine"], x["VoiceId"], x["OutputFormat"], x["SampleRate"], x["TextType"]) for x in r] == \
        [("generative", "Matthew", "pcm", "16000", "ssml")] * 2
    assert r[0]["Text"] == f'<speak><prosody rate="92%">{body}</prosody></speak>' and tail in r[1]["Text"]
    x = read16k(path)
    assert abs(peak_hz(x[1000:15000]) - 349.1) < 2 and abs(peak_hz(x[17000:63000]) - 311.1) < 2   # the tones
    start = int((4 + dispatcher.GAP_S) * 16000)
    first, second = x[start:start + 16000], x[start + 32000:start + 48000]
    assert abs(peak_hz(first) - 440) < 2 and abs(peak_hz(second) - 440) < 2
    assert len(x) == start + 3 * 16000 + 16000 + int(dispatcher.TAIL_GAP_S * 16000)     # twice, gap, the time out once
    assert np.max(np.abs(first)) == pytest.approx(dispatcher.AMPLITUDE, abs=0.02)
    assert dispatcher.render_dispatch(A, TONES, tmp_path, voice()) == (path, label) and len(r) == 2   # cached
    assert path.stem == dispatcher.dispatch_key(A, TONES, voice())
    plain = dispatcher.render_dispatch(A, TONES, tmp_path, voice(speed=1.0))[0]          # own pace: plain text
    assert plain != path and r[-1]["TextType"] == "text" and r[-1]["Text"] == tail


def test_cache_key_includes_engine_voice_and_speed():
    keys = {dispatcher.dispatch_key(A, TONES, v) for v in (
        voice("Matthew"), voice("Stephen"), voice("Matthew", 1.0), tts.VoiceCfg("kokoro", "Matthew", 0.92),
        tts.VoiceCfg("say", "Matthew", 0.92))}
    assert len(keys) == 5
    assert dispatcher.dispatch_key(A, TONES, voice()) == dispatcher.audio_key(
        dispatcher.speech(A) + "\x00time-out-once", dispatcher.tone_groups(TONES), "polly", "Matthew:generative", 0.92, 2)
    assert dispatcher.dispatch_key(A, TONES, voice("Gregory")) == dispatcher.audio_key(
        dispatcher.speech(A) + "\x00time-out-once", dispatcher.tone_groups(TONES), "polly", "Gregory:neural", 0.92, 2)


@pytest.mark.parametrize("err", ["ValidationException", "EngineNotSupportedException"])
def test_engine_falls_back_generative_to_neural_and_remembers(fake_polly, err):
    fake_polly["unsupported"] = {"generative": err}
    logged = []
    x = polly.synthesize("Engine sixty.", "Matthew", 0.92, log=logged.append)
    assert len(x) == 16000 and x.dtype == np.float32
    assert [(r["Engine"], r["TextType"]) for r in fake_polly["requests"]] == [
        ("generative", "ssml"), ("generative", "text"), ("neural", "ssml")]
    assert polly.engine_in_use("Matthew") == "neural" and "neural engine" in logged[0] and err in logged[0]
    polly.synthesize("Time out.", "Matthew", 0.92, log=logged.append)
    assert fake_polly["requests"][-1]["Engine"] == "neural" and len(fake_polly["requests"]) == 4 and len(logged) == 1
    fake_polly["unsupported"] = {"generative": err, "neural": err}
    polly.synthesize("Engine sixty.", "Joey", 1.0)                                    # Joey: neural, standard
    assert [r["Engine"] for r in fake_polly["requests"][4:]] == ["neural", "standard"]
    fake_polly["unsupported"]["standard"] = err
    with pytest.raises(polly.PollyError, match=err):
        polly.synthesize("Engine sixty.", "Stephen", 1.0)                              # nothing left to try


def test_ssml_refused_says_it_at_its_own_pace(fake_polly):
    fake_polly["bad_ssml"] = {"generative"}
    polly.synthesize("Engine sixty.", "Matthew", 0.92)
    polly.synthesize("Time out.", "Matthew", 0.92)
    assert [(r["Engine"], r["TextType"]) for r in fake_polly["requests"]] == [
        ("generative", "ssml"), ("generative", "text"), ("generative", "text")]


def test_access_denied_falls_back_locally_or_says_why(tmp_path, fake_polly, monkeypatch):
    fake_polly["deny"] = True
    with pytest.raises(AudioUnavailable, match=r"^Polly: AccessDeniedException: User: .* polly:SynthesizeSpeech"):
        dispatcher.render_dispatch(A, TONES, tmp_path, voice(), local_fallback=False)
    assert len(fake_polly["requests"]) == 1                                  # not an engine problem: no retries
    said = []
    monkeypatch.setattr(dispatcher, "tts", lambda text, v="", rate_wpm=0: said.append(v) or np.full(800, 0.1, np.float32))
    logged = []
    path, label = dispatcher.render_dispatch(A, TONES, tmp_path, voice(), log=logged.append)
    assert label == "Voice: Samantha (fallback)" and said == ["Samantha", "Samantha"] and "AccessDenied" in logged[0]


def test_network_errors_and_no_credentials(fake_polly, monkeypatch):
    monkeypatch.setenv("SIZEUP_POLLY_ENDPOINT", "http://127.0.0.1:9")                # nothing listens
    t0 = time.monotonic()
    with pytest.raises(polly.PollyError, match="can't reach"):
        polly.synthesize("Engine sixty.", "Matthew")
    assert time.monotonic() - t0 < polly.CONNECT_S + 1
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    with pytest.raises(polly.PollyError, match="no AWS credentials"):
        polly.synthesize("Engine sixty.", "Matthew")


def test_long_text_split_at_sentence_ends(fake_polly):
    sentences = [f"Sentence number {k} says something about the call at hand." for k in range(120)]
    text = " ".join(sentences)
    parts = polly.chunks(text)
    assert len(text) > 2 * polly.MAX_CHARS and len(parts) == 3
    assert all(len(p) <= polly.MAX_CHARS and p.endswith(".") for p in parts) and " ".join(parts) == text
    assert polly.chunks("word " * 1000, 100) and all(len(p) <= 100 for p in polly.chunks("word " * 1000, 100))
    x = polly.synthesize(text, "Matthew", 1.0)
    assert len(fake_polly["requests"]) == 3 and len(x) == 3 * 16000
    assert [r["Text"] for r in fake_polly["requests"]] == parts


# -- config, Settings ---------------------------------------------------------------------------------------

def test_config_polly_engine(tmp_path):
    def cfg(body: str):
        p = tmp_path / "c.toml"
        p.write_text("[dispatch]\n" + body)
        return load(p)
    c = cfg('tts_engine = "polly"\n')
    assert (c.dispatch_tts_engine, c.dispatch_voice, c.dispatch_polly_region) == ("polly", "Matthew", "")
    c = cfg('tts_engine = "polly"\nvoice = "Gregory"\npolly_region = "us-west-2"\n')
    assert (c.dispatch_voice, c.dispatch_polly_region) == ("Gregory", "us-west-2")
    for bad in ('tts_engine = "polly"\nvoice = "af_heart"', 'tts_engine = "polly"\nvoice = "Brian"',
                'polly_region = "mars"', 'tts_engine = "aws"'):
        with pytest.raises(ConfigError):
            cfg(bad + "\n")
    hub = load(ROOT / "deploy" / "hub" / "config.hub.toml")
    assert (hub.dispatch_tts_engine, hub.dispatch_voice, hub.dispatch_polly_region) == ("polly", "Matthew", "")
    assert hub.cloud_role == "hub"


def test_voices_offered_men_first_with_their_engines():
    assert list(polly.VOICES)[:4] == ["Matthew", "Stephen", "Gregory", "Joey"]
    assert all(polly.VOICES[v]["gender"] == "male" for v in list(polly.VOICES)[:4])
    for v, d in polly.VOICES.items():
        assert d["engines"] and all(e in polly.ENGINE_ORDER for e in d["engines"])
        assert list(d["engines"]) == sorted(d["engines"], key=polly.ENGINE_ORDER.index)
    assert polly.VOICES["Gregory"]["engines"] == ("neural",) and polly.DEFAULT_VOICE == "Matthew"


def test_settings_voice_picker_and_preview_with_polly(fake_polly):
    cad = Cad(Settings(tts_engine="polly", voice="Matthew", speed=0.92), log=lambda m: None)
    page = cad.settings_get()["dispatch"]["voice"]
    assert page["engine"] == "polly" and page["voices"] == list(polly.VOICES) and page["name"] == "Matthew"
    v = cad.tts_voices()
    assert v["voices"][0] == {"id": "Matthew", "name": "Matthew", "accent": "American", "gender": "male",
                              "engines": ["generative", "neural", "standard"]} and v["polly_engine"] == ""
    cad.settings_post("dispatch", {"action": "set", "key": "voice", "value": "Stephen", "operator": "JD"})
    assert cad.s.voice == "Stephen"
    with pytest.raises(CadError, match="voice: one of Matthew"):
        cad.settings_post("dispatch", {"action": "set", "key": "voice", "value": "af_heart", "operator": "JD"})
    r = cad.settings_post("dispatch", {"action": "voice_preview"})
    assert r["text"] == VOICE_SAMPLE == "Engine 60, respond to 1 Main Street, structure fire. Time out 14:32."
    assert r["audio_note"] == "Voice: Polly Stephen" and r["audio_url"].startswith("/api/cad/audio/")
    assert fake_polly["requests"][0]["VoiceId"] == "Stephen" and cad.tts_voices()["polly_engine"] == "generative"
    assert cad.preview_audio(r["audio_url"].split("/")[-1][:-4])[:4] == b"RIFF"
    assert cad.state()["audio"]["tts"] is True and cad.state()["audio"]["label"] == "Voice: Polly Stephen"
    kokoro = Cad(Settings(tts_engine="kokoro", voice="af_heart"), log=lambda m: None)
    assert "am_michael" in kokoro.settings_get()["dispatch"]["voice"]["voices"]


def test_voice_preview_is_not_logged_as_a_settings_change():
    from sizeup.cad import api
    assert api.summarize("/api/cad/settings/dispatch", {"action": "voice_preview"}, {}, None) is None


# -- the hub: Polly first, then the station, then text only ------------------------------------------------

def test_hub_renders_with_polly_without_the_station_then_falls_back(tmp_path, fake_polly):
    from tests.test_cloud import FakeStation, HubRun, call, hub_cfg, uid, wait
    hub = HubRun(hub_cfg(tmp_path, dispatch_tts_engine="polly", dispatch_voice="Matthew", cloud_offline_s=1.0))
    try:
        c = hub.app.cad
        assert c.s.tts_engine == "polly" and not hub.app.hub.connected()
        inc = c.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
        body = {"unit_ids": [uid(c, "E63")]}
        code, p = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", body, token=None)
        assert code == 200 and p["audio_note"] == "Voice: Polly Matthew" and p["audio_url"]       # no station needed
        assert len(fake_polly["requests"]) == 2 and fake_polly["bad_sig"] == 0
        wav = hub.get(p["audio_url"])
        assert wav[:4] == b"RIFF"
        key = p["audio_url"].split("/")[-1][:-4]
        assert hub.app.audio.get(key) == wav and hub.app.audio.note(key) == "Voice: Polly Matthew"   # kept in SQLite
        for f in c.audio_dir.glob("*.wav"):                                                # a redeploy: disk gone
            f.unlink()
        again = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", body, token=None)[1]
        assert again["audio_url"] == p["audio_url"] and again["audio_note"] == "Voice: Polly Matthew"
        assert len(fake_polly["requests"]) == 2                                            # cached, not re-rendered
        # Polly refuses (no permission yet), no station: text only, saying why.
        fake_polly["deny"] = True
        t0 = time.time()
        p2 = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", dict(body, remarks="two cars"), token=None)[1]
        assert p2["audio_url"] is None and time.time() - t0 < 2
        assert p2["audio_note"].startswith("Voice unavailable — Polly: AccessDeniedException: User: arn:aws:sts")
        assert p2["audio_note"].endswith("; no station connected: text only")
        # ... with a station: its Kokoro, a man's voice, and the note says Polly failed.
        fake = FakeStation(hub.url)
        try:
            assert wait(lambda: hub.app.hub.connected(), 5)
            p3 = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", dict(body, remarks="two cars"), token=None)[1]
            assert p3["audio_url"] and p3["audio_note"].startswith("Voice: Kokoro af_heart (station: Polly: AccessDeniedException")
            job = fake.jobs[-1]
            assert job["type"] == "tts" and job["args"]["voice"]["engine"] == "kokoro"
            assert job["args"]["voice"]["voice"] == polly.STATION_VOICE == "am_michael"
            assert hub.get(p3["audio_url"]) == fake.wav
            # Polly back: Polly again (the station's render isn't stuck in front of it).
            fake_polly["deny"] = False
            n = len(fake.jobs)
            p4 = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", dict(body, remarks="two cars"), token=None)[1]
            assert p4["audio_note"] == "Voice: Polly Matthew" and len(fake.jobs) == n
        finally:
            fake.stop.set()
            fake.thread.join(10)
        r = call(hub.url + "/api/cad/settings/dispatch", "POST", {"action": "voice_preview", "operator": "JD"}, token=None)
        assert r[0] == 200 and r[1]["audio_note"] == "Voice: Polly Matthew"
        row = lambda: hub.app.hub.station_row()["detail"]
        assert wait(lambda: "offline since" in row(), 5) and "dispatch voice" not in row()   # the voice is the hub's now
    finally:
        hub.close()
