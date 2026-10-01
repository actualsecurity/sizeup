import time
from dataclasses import replace

import numpy as np

from sizeup.app import App
from sizeup.config import Config
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.radio.p25 import Call
from sizeup.radio.tones import ToneCode

PAGE_TEXT = ("Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester Avenue, "
             "cross of Pearl Street, Port Chester. Time out 1432.")


class FakeWhisper:
    """Stands in for whisper-server: returns fixed text by audio length."""

    def __init__(self, texts):
        self.texts = texts

    def transcribe(self, audio, prompt=""):
        return self.texts[round(len(audio) / RATE)], 0.9


def cfg(tmp_path):
    return Config("t", "Test", "127.0.0.1", 0, "rtl_fm", (), (ToneCode("Port Chester FD", 349.1, 311.1, "PCFD"),),
                  frozenset({"Port Chester FD", "Rye Brook FD"}), "whisper-server", "m.bin", 1, 1, "",
                  "", clips_dir=str(tmp_path / "clips"))


def tone(hz, s):
    return 0.5 * np.sin(2 * np.pi * hz * np.arange(int(RATE * s)) / RATE)


def wait(cond, s=5.0):
    end = time.time() + s
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_page_opens_incident_and_traffic_attaches(tmp_path):
    app = App(cfg(tmp_path))
    app._kind = {"South 2": "paging", "FG1": "traffic"}
    app.stt.client = FakeWhisper({6: PAGE_TEXT, 3: "Engine 60 on location nothing showing", 2: "2391 responding"})
    app.stt.start()
    voice = 0.3 * np.random.default_rng(0).standard_normal(RATE * 6)
    audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: app.store.active() is not None)
    inc = app.store.active()
    assert inc.incident_type == "Fire alarm" and inc.departments == ["Port Chester FD"] and inc.time_out == "14:32"
    fg = (0.2 * np.random.default_rng(1).standard_normal(RATE * 3)).astype(np.float32)
    app._segment(Channel(453.0375, "FG1"), Segment(now + 60, now + 63, fg, 20.0))
    app._call(Call("307-x-call_1", 307, "Fire/EMS 15", now + 70, now + 72, ["2391"],
                   np.zeros(RATE * 2, np.float32)))
    assert wait(lambda: len(inc.traffic) == 2)
    assert [(t.source, t.text, t.unit) for t in inc.traffic] == [
        ("FG1", "Engine 60 on location nothing showing", ""), ("Fire/EMS 15", "2391 responding", "2391")]
    st = app.state()
    assert st["incidents"][0]["id"] == inc.id and st["pages"][0]["dispatch"]["incident_type"] == "Fire alarm"
    page_id = st["pages"][0]["id"]
    assert app.clip(page_id)[:4] == b"RIFF" and app.clip("../etc") is None
