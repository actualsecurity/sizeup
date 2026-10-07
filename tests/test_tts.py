"""The dispatch voice: the dispatch-style number reader, the Kokoro client with its `say` fallback
(server down, slow, erroring), the server supervisor (reuse / replace / never kill a stranger's),
the [dispatch] voice config, and the real model when it's installed."""

import http.server
import io
import json
import socket
import subprocess
import sys
import textwrap
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from sizeup import tts
from sizeup.cad import dispatcher
from sizeup.cad.speech import house, words
from sizeup.cad.service import Cad, Settings
from sizeup.config import ConfigError, load

A = ("Port Chester Fire, Engine 60, Ladder 31, Rescue 40, respond to 209 Westchester Avenue, cross of Pearl Street, "
     "for a structure fire. Time out 15:13.")
B = ("Port Chester Fire, Engine 61, added to the assignment. Engine 60 responding, Ladder 31 and 2398 on location. "
     "169 North Main Street, commercial fire alarm. Time out 15:20.")
TONES = [(349.1, 1.0), (311.1, 3.0)]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sine_wav(hz=440.0, secs=1.5, rate=24000) -> bytes:
    t = np.arange(int(secs * rate)) / rate
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((0.3 * np.sin(2 * np.pi * hz * t) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def peak_hz(x, rate=16000):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spec)]


# -- dispatch-style numbers ---------------------------------------------------------------------------

def test_house_numbers_said_the_dispatch_way():
    said = {n: house(n) for n in ("5", "15", "100", "209", "169", "1200", "4040", "2398")}
    assert said == {"5": "five", "15": "fifteen", "100": "one hundred", "209": "two oh nine", "169": "one sixty-nine",
                    "1200": "twelve hundred", "4040": "forty forty", "2398": "twenty-three ninety-eight"}
    assert words("5 King Street, 15 Pearl Street, 100 Main Street, 1200 King Street, 4040 Boston Post Road") == (
        "Five King Street, fifteen Pearl Street, one hundred Main Street, twelve hundred King Street, "
        "forty forty Boston Post Road")


def test_dispatch_texts_spoken():
    assert words(A) == ("Port Chester Fire, Engine sixty, Ladder thirty-one, Rescue forty, respond to two oh nine "
                        "Westchester Avenue, cross of Pearl Street, for a structure fire. Time out fifteen thirteen.")
    assert words(B) == ("Port Chester Fire, Engine sixty-one, added to the assignment. Engine sixty responding, "
                        "Ladder thirty-one and twenty-three ninety-eight on location. One sixty-nine North Main "
                        "Street, commercial fire alarm. Time out fifteen twenty.")
    assert "Ladder thirty-one and twenty-three ninety-eight on" in words(B.replace("2398", "23 98"))


def test_times_and_units():
    assert words("Time out 15:13.") == "Time out fifteen thirteen."
    assert words("Time out 15:00.") == "Time out fifteen hundred."
    assert words("Time out 07:05.") == "Time out oh seven oh five."
    assert words("Time out 10:30.") == "Time out ten thirty."
    # Unit codes by cad_lists.json's spoken names, and the phonetic alphabet for the rest.
    from sizeup.cad import lists
    spoken = {c: u.get("spoken", "") for c, u in lists.load().units.items()}
    assert words("77A4 on location", spoken) == "Seventy-seven Alpha four on location"
    assert words("77A4 and 77M2 responding") == "Seventy-seven Alpha four and seventy-seven Mike two responding"
    assert words("Engine 191, Tower Ladder 2, E60", spoken) == "Engine one ninety-one, Tower Ladder two, Engine sixty"
    # Left for the voice: ordinals, decimals, thousands separators.
    assert words("1st Assistant Chief, 4th Street, 3.5 miles, 1,200 gallons") == (
        "1st Assistant Chief, 4th Street, 3.5 miles, 1,200 gallons")


# -- the Kokoro client and the `say` fallback ---------------------------------------------------------

@pytest.fixture
def fake_server():
    """A stand-in TTS server: /tts answers with a 440 Hz tone (after `delay` s, or `fail`s)."""
    state = {"delay": 0.0, "fail": False, "requests": []}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True, "voices": ["af_heart", "am_michael"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            state["requests"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            time.sleep(state["delay"])
            body, code = (b'{"error": "synthesis failed"}', 500) if state["fail"] else (sine_wav(), 200)
            try:
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["port"] = srv.server_address[1]
    yield state
    srv.shutdown()


@pytest.fixture
def fake_say(monkeypatch):
    """`say` stand-in (a 220 Hz tone), so the fallback is testable anywhere."""
    calls = []

    def say(text, voice="", rate_wpm=0):
        calls.append((text, voice))
        return (0.5 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.float32)
    monkeypatch.setattr(dispatcher, "tts", say)
    return calls


def read16k(path):
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def test_kokoro_voice_after_tones_twice_at_16k(tmp_path, fake_server, fake_say):
    v = tts.VoiceCfg("kokoro", "af_heart", 0.92, fake_server["port"])
    path, label = dispatcher.render_dispatch(A, TONES, tmp_path, v)
    assert label == "Voice: Kokoro af_heart" and fake_say == []
    assert fake_server["requests"] == [{"text": dispatcher.speech(A), "voice": "af_heart", "speed": 0.92}]
    x = read16k(path)
    assert abs(peak_hz(x[1000:15000]) - 349.1) < 2 and abs(peak_hz(x[17000:63000]) - 311.1) < 2   # the tones
    start = int((4 + dispatcher.GAP_S) * 16000)
    assert np.max(np.abs(x[64500:start - 100])) < 0.01                                       # the gap
    first, second = x[start:start + 24000], x[start + 24000 + 16000:]
    assert len(second) == 24000 and abs(peak_hz(first) - 440) < 2 and abs(peak_hz(second) - 440) < 2
    assert np.max(np.abs(first)) == pytest.approx(dispatcher.AMPLITUDE, abs=0.02)            # same level as the tones
    again = dispatcher.render_dispatch(A, TONES, tmp_path, v)
    assert again == (path, label) and len(fake_server["requests"]) == 1                      # cached
    other = dispatcher.render_dispatch(A, TONES, tmp_path, tts.VoiceCfg("kokoro", "am_michael", 0.92, fake_server["port"]))
    assert other[0] != path and other[1] == "Voice: Kokoro am_michael"


def test_falls_back_to_say_when_the_server_is_down(tmp_path, fake_say):
    logged = []
    v = tts.VoiceCfg("kokoro", "af_heart", 0.92, free_port(), fallback_voice="Samantha")
    path, label = dispatcher.render_dispatch(A, TONES, tmp_path, v, log=logged.append)
    assert label == "Voice: Samantha (fallback)"
    assert fake_say == [(dispatcher.speech(A), "Samantha")] and "not reachable" in logged[0]
    x = read16k(path)
    assert abs(peak_hz(x[int(4.6 * 16000):int(5.6 * 16000)]) - 220) < 2


def test_falls_back_on_timeout_and_on_errors(tmp_path, fake_server, fake_say):
    fake_server["delay"] = 1.5
    v = tts.VoiceCfg("kokoro", "af_heart", 0.92, fake_server["port"], timeout_s=0.3)
    t0 = time.monotonic()
    path, label = dispatcher.render_dispatch(A, TONES, tmp_path, v)
    assert label == "Voice: Samantha (fallback)" and time.monotonic() - t0 < 1.2
    fake_server.update(delay=0.0, fail=True)
    with pytest.raises(tts.TtsError, match="synthesis failed"):
        tts.synthesize("Engine sixty.", "af_heart", port=fake_server["port"])
    assert dispatcher.render_dispatch(B, TONES, tmp_path, v)[1] == "Voice: Samantha (fallback)"
    fake_server["fail"] = False                                     # back up: Kokoro again, not the cached fallback
    assert dispatcher.render_dispatch(A, TONES, tmp_path, v)[1] == "Voice: Kokoro af_heart"


def test_say_engine_and_labels(tmp_path, fake_say):
    path, label = dispatcher.render_dispatch(A, TONES, tmp_path, tts.VoiceCfg("say", "Alex"))
    assert label == "Voice: Alex" and fake_say[-1][1] == "Alex"
    assert dispatcher.render_audio(A, TONES, tmp_path, voice="Alex") == path


def test_cad_notes_the_voice_on_preview_and_dispatch(fake_server, fake_say):
    cad = Cad(Settings(tts_engine="kokoro", voice="af_heart", speed=0.92, tts_port=fake_server["port"]),
              log=lambda m: None)
    path, note = cad._render(A)
    assert note == "Voice: Kokoro af_heart" and path.is_file()
    cad.s.tts_port = free_port()
    assert cad._render(B)[1] == "Voice: Samantha (fallback)"
    voices = Cad(Settings(tts_port=fake_server["port"]), log=lambda m: None).tts_voices()
    assert [v["id"] for v in voices["voices"]] == ["af_heart", "am_michael"]
    assert voices["voices"][0] == {"id": "af_heart", "name": "Heart", "accent": "American", "gender": "female"}
    assert {v["id"] for v in cad.tts_voices()["voices"]} >= {"af_heart", "bm_george"}   # server down: the list


def test_resample_24k_to_16k_keeps_pitch_and_level():
    x = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(24000) / 24000)
    y = tts.resample(x, 24000, 16000)
    assert len(y) == 16000 and abs(peak_hz(y[1000:-1000]) - 1000) < 2
    assert np.max(np.abs(y[1000:-1000])) == pytest.approx(0.5, abs=0.01)
    hi = tts.resample(0.5 * np.sin(2 * np.pi * 10000 * np.arange(24000) / 24000), 24000, 16000)
    assert np.max(np.abs(hi[1000:-1000])) < 0.01                     # above 8 kHz: filtered, not aliased


# -- the supervisor -------------------------------------------------------------------------------------

FAKE_SERVER = textwrap.dedent('''
    import argparse, json, http.server
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int); ap.add_argument("--voice")
    a = ap.parse_args()
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps({"ok": True, "voice": a.voice, "voices": [a.voice]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def log_message(self, *x): pass
    http.server.HTTPServer(("127.0.0.1", a.port), H).serve_forever()
''')


@pytest.fixture
def fake_env(tmp_path):
    srv = tmp_path / "tts_server.py"      # the name the supervisor looks for in `ps`
    srv.write_text(FAKE_SERVER)
    procs = []

    def service(port, voice="af_heart"):
        return tts.TtsService(sys.executable, port, voice, tmp_path / "tts-server.json", log=lambda m: None, server=srv)

    def launch(svc):
        p = subprocess.Popen(svc.argv)
        procs.append(p)
        wait_up(svc.port)
        return p
    yield type("E", (), {"service": staticmethod(service), "launch": staticmethod(launch), "state": tmp_path / "tts-server.json"})
    for p in procs:
        if p.poll() is None:
            p.kill()


def wait_up(port, secs=10.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if tts.health(port, 0.5):
            return True
        time.sleep(0.05)
    raise AssertionError(f"nothing answered on {port}")


def test_supervisor_starts_reuses_and_replaces(fake_env):
    port = free_port()
    svc = fake_env.service(port)
    assert svc.check() is False and svc.state == "starting"         # not up: started
    wait_up(port)
    assert svc.check() and svc.state == "ok" and svc.voices == ["af_heart"]
    pid = json.loads(fake_env.state.read_text())["pid"]
    assert pid == svc._proc.pid
    # sizeup restarts: the server it started is still there with the same config -> reused
    again = fake_env.service(port)
    again._replace_stale()
    assert again.check() and again._proc is None and svc._proc.poll() is None
    # the voice changed in the config -> the old one is replaced
    changed = fake_env.service(port, "am_michael")
    changed._replace_stale()
    assert svc._proc.wait(5) is not None
    changed.check()
    wait_up(port)
    assert changed.check() and tts.health(port)["voice"] == "am_michael"
    changed.stop()
    assert changed._proc.wait(5) is not None


def test_supervisor_never_kills_a_server_it_did_not_start(fake_env):
    port = free_port()
    stranger = fake_env.launch(fake_env.service(port, "bf_emma"))   # not recorded in the state file
    svc = fake_env.service(port)                                     # a different voice
    svc._replace_stale()
    assert svc.check() and svc._proc is None and stranger.poll() is None
    svc.stop()
    assert stranger.poll() is None


def test_supervisor_down_without_the_environment(tmp_path):
    svc = tts.TtsService(str(tmp_path / "nope" / "python"), free_port(), state_path=tmp_path / "s.json", log=lambda m: None)
    assert svc.check() is False and svc.state == "down" and "tools/setup_tts.sh" in svc.detail
    assert svc.status()["label"] == "Voice"


# -- config ---------------------------------------------------------------------------------------------

def test_dispatch_voice_config(tmp_path):
    def cfg(body: str):
        p = tmp_path / "c.toml"
        p.write_text("[dispatch]\n" + body)
        return load(p)
    c = cfg("")
    assert (c.dispatch_tts_engine, c.dispatch_voice, c.dispatch_speed, c.dispatch_tts_port, c.dispatch_fallback_voice,
            c.dispatch_tts_python) == ("kokoro", "af_heart", 0.92, 8093, "Samantha", tts.DEFAULT_PYTHON)
    old = cfg('voice = "Alex"\n')                          # an older config's `say` voice: kept, as the fallback
    assert (old.dispatch_tts_engine, old.dispatch_voice, old.dispatch_fallback_voice) == ("kokoro", "af_heart", "Alex")
    say = cfg('tts_engine = "say"\nvoice = "Alex"\n')
    assert (say.dispatch_tts_engine, say.dispatch_voice) == ("say", "Alex")
    c = cfg('voice = "bm_george"\nspeed = 1.1\ntts_port = 8095\nfallback_voice = "Daniel"\ntts_python = "~/tts/python"\n')
    assert (c.dispatch_voice, c.dispatch_speed, c.dispatch_tts_port, c.dispatch_fallback_voice) == (
        "bm_george", 1.1, 8095, "Daniel")
    assert c.dispatch_tts_python == str(Path.home() / "tts" / "python")
    for bad in ('tts_engine = "piper"', 'tts_engine = "kokoro"\nvoice = "Samantha"', 'voice = "af_hart"',
                "speed = 3", "speed = 0.2", "tts_port = 80", "tts_port = 8179", 'fallback_voice = "a;rm"'):
        with pytest.raises(ConfigError):
            cfg(bad + "\n")


def test_app_status_row_for_the_voice(tmp_path):
    from tests.test_app import cfg
    from sizeup.app import App
    app = App(cfg(tmp_path))                                  # built directly: `say`, no server
    assert app.tts is None
    row = app.radios()[-1]
    assert row["label"] == "Voice" and row["state"] in ("ok", "down")


# -- the real model (when tools/setup_tts.sh has been run) ---------------------------------------------

@pytest.mark.skipif(not Path(tts.DEFAULT_PYTHON).is_file(), reason="no TTS environment (tools/setup_tts.sh)")
def test_real_kokoro_server(tmp_path):
    port = free_port()
    svc = tts.TtsService(tts.DEFAULT_PYTHON, port, "af_heart", tmp_path / "tts-server.json", log=lambda m: None)
    try:
        svc.check()
        end = time.monotonic() + 90
        while not svc.check():
            assert svc.state == "starting" and time.monotonic() < end, svc.detail
            time.sleep(0.5)
        x = tts.synthesize(words("Engine 60, respond to 209 Westchester Avenue."), "af_heart", 0.92, port, 30)
        assert 1.5 < len(x) / 16000 < 8 and np.sqrt(np.mean(x ** 2)) > 0.01
        path, label = dispatcher.render_dispatch(A, TONES, tmp_path, tts.VoiceCfg("kokoro", "af_heart", 0.92, port, timeout_s=30))
        assert label == "Voice: Kokoro af_heart" and len(read16k(path)) / 16000 > 4.6 + 2 * 5
    finally:
        svc.stop()
        if svc._proc:
            svc._proc.wait(10)
