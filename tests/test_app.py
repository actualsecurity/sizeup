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


def voice(s, seed=0):
    """Speech stand-in: harmonics with syllable-rate bursts over quiet dead air (carrier up)."""
    t = np.arange(int(RATE * s)) / RATE
    ph = 2 * np.pi * np.cumsum(130 + 20 * np.sin(2 * np.pi * 1.1 * t)) / RATE
    syll = (np.sin(2 * np.pi * 3 * t) > 0.2).astype(float)
    x = 0.3 * syll * sum(np.sin(k * ph) / k for k in range(1, 12))
    return (x + 0.003 * np.random.default_rng(seed).standard_normal(len(t))).astype(np.float32)


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
    audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: app.store.active() is not None)
    inc = app.store.active()
    assert inc.incident_type == "Fire alarm" and inc.departments == ["Port Chester FD"] and inc.time_out == "14:32"
    fg = voice(3.0, seed=1)
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


def test_mic_clicks_dropped_weak_speech_kept(tmp_path):
    # FG1 bench: whisper says "Thank you."/"Yes." for dead air (6 of 6); real speech at only
    # 2-6 dB over the dead air ("Testing one, two.") must still come through.
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    rng = np.random.default_rng(2)
    dead_air = (0.003 * rng.standard_normal(int(RATE * 1.5))).astype(np.float32)
    weak = (0.003 * rng.standard_normal(int(RATE * 1.5))).astype(np.float32)   # energy gate can't tell
    replies = iter(["Thank you.", "Testing one, two."])
    app.stt.client = type("W", (), {"transcribe": lambda self, a, prompt="": (next(replies), 0.6)})()
    app.stt.start()
    now = time.time()
    app._segment(Channel(453.0375, "FG1"), Segment(now, now + 1.5, dead_air, 20.0))
    app._segment(Channel(453.0375, "FG1"), Segment(now + 5, now + 6.5, weak, 20.0))
    assert wait(lambda: len(app.state()["loose_traffic"]) == 1)
    time.sleep(0.2)
    assert [t["text"] for t in app.state()["loose_traffic"]] == ["Testing one, two."]
    assert len(list((tmp_path / "clips" / "skipped").glob("*.wav"))) == 1   # the click, kept for review


def test_mdc_unit_id_names_the_talker(tmp_path):
    # Real FG1 burst from the bench: MDC-1200 post-ID 0611 = Engine 61 portable 1.
    import wave
    from dataclasses import replace
    from pathlib import Path
    with wave.open(str(Path(__file__).parent / "fixtures" / "mdc" / "fg1_165566.wav")) as w:
        burst = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    app = App(replace(cfg(tmp_path), mdc_aliases={"0611": "Engine 61 Portable 1"}))
    assert app._units(Segment(0, 1, np.zeros(RATE, np.float32), 20.0, [burst])) == ["Engine 61 Portable 1"]
    app = App(cfg(tmp_path))
    assert app._units(Segment(0, 1, np.zeros(RATE, np.float32), 20.0, [burst])) == ["0611"]  # no alias: the ID


def test_page_naming_our_unit_opens_incident_with_unknown_tones(tmp_path):
    from dataclasses import replace
    app = App(replace(cfg(tmp_path), unit_departments={"Engine 61": "Port Chester FD"}))
    app._kind = {"South 2": "paging"}
    text = "10 Charlie 2, Engine 61, 209 Westchester Avenue, Pearl Street and Gilbert Place"
    app.stt.client = FakeWhisper({6: text})
    app.stt.start()
    audio = np.concatenate([tone(741.3, 1.0), tone(623.7, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: app.store.active() is not None)
    inc = app.store.active()
    assert inc.departments == ["Port Chester FD"] and inc.incident_type == "Chest pain"
    assert inc.location == "209 westchester avenue"


def test_pages_and_incidents_survive_a_restart(tmp_path):
    # User: "it disappeared? can we store these for at least 1 hr" (a restart wiped the screen).
    from dataclasses import replace
    c = replace(cfg(tmp_path), unit_departments={"Engine 61": "Port Chester FD"})
    app = App(c)
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: "10 Charlie 2, Engine 61, 209 Westchester Avenue, Pearl Street and Gilbert Place"})
    app.stt.start()
    audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: app.store.active() is not None)
    page_id = app.state()["pages"][0]["id"]
    again = App(c)                                   # a restart
    st = again.state()
    assert [p["id"] for p in st["pages"]] == [page_id] and st["pages"][0]["dispatch"]["incident_type"] == "Chest pain"
    assert st["incidents"][0]["location"] == "209 westchester avenue" and st["incidents"][0]["status"] == "active"
    assert again.clip(page_id)[:4] == b"RIFF"


def test_one_shared_dongle_switches_between_channels_and_p25(tmp_path, monkeypatch):
    # User: "add the ability to manually switch between each of the 3 since we only have one sdr".
    from dataclasses import replace
    import sizeup.app as appmod
    from sizeup.config import ChannelCfg
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    c = replace(cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                channels=(ChannelCfg("South 2", 470.2, "paging", "fire-page"), ChannelCfg("FG1", 453.0375, "traffic", "fire-fg")),
                shared_device="00000001", start_mode="South 2", p25_dir=str(tmp_path / "p25"),
                p25_talkgroups={307: "Fire/EMS 15"}, p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=x",
                p25_recorder_dir=str(tmp_path))
    app = App(c).start()
    try:
        st = app.state()
        assert st["sdr"]["modes"] == ["South 2", "FG1", "Fire/EMS 15"] and st["sdr"]["mode"] == "South 2"
        radios = {r["label"]: r for r in st["radios"]}
        assert radios["FG1"]["state"] == "off" and radios["FG1"]["detail"] == "SDR on South 2"
        assert all(r.device == "00000001" for r in app.receivers)          # everything on the one dongle
        assert app.set_mode("FG1") is None and app.state()["sdr"]["mode"] == "FG1"
        assert {r["label"]: r["state"] for r in app.state()["radios"]}["South 2"] == "off"
        assert app.set_mode("Fire/EMS 15") is None
        assert wait(lambda: app.recorder.state == "absent")                 # not installed yet: says so
        assert app.set_mode("Nope") is not None
        assert app.set_mode("South 2") is None and app.recorder.state == "off"
    finally:
        app.stop()


def test_other_battalions_talkgroups_are_context_not_incident_traffic(tmp_path):
    # Every fire/EMS talkgroup is recorded; only Fire/EMS 15 (307) + 60 Control (301) join our incident.
    from dataclasses import replace
    app = App(replace(cfg(tmp_path), unit_departments={"Engine 61": "Port Chester FD"},
                      p25_incident_talkgroups=frozenset({307, 301})))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: "10 Charlie 2, Engine 61, 209 Westchester Avenue", 2: "Engine 61 responding",
                                  3: "Battalion 10 traffic"})
    app.stt.start()
    audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: app.store.active() is not None)
    app._call(Call("111-x", 111, "Fire/EMS 10", now + 30, now + 33, [], np.zeros(RATE * 3, np.float32)))
    app._call(Call("307-x", 307, "Fire/EMS 15", now + 40, now + 42, ["2391"], np.zeros(RATE * 2, np.float32)))
    inc = app.store.active()
    assert wait(lambda: len(inc.traffic) == 1 and len(app.store.loose) == 1)
    assert inc.traffic[0].source == "Fire/EMS 15" and app.store.loose[0].source == "Fire/EMS 10"


def test_trunk_recorder_missing_dongle_is_reported_plainly(tmp_path):
    # Bench 2026-10-01: the V4 dropped off USB; trunk-recorder exits "Wrong rtlsdr device index given".
    import stat
    from sizeup.radio.recorder import TrunkRecorder
    exe = tmp_path / "fake-trunk-recorder"
    exe.write_text("#!/bin/sh\necho '(error)   Failed parsing Config: Wrong rtlsdr device index given.'\nexit 1\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    events = []
    r = TrunkRecorder(str(exe), str(tmp_path), events.append).start()
    try:
        assert wait(lambda: r.state == "absent")
        assert "not connected" in r.detail and r.restarts == 0 and events == []
    finally:
        r.stop()


def test_talkgroup_record_and_main_switches(tmp_path):
    # User: poll all talkgroups, group the others, toggle which are recorded from the UI.
    from dataclasses import replace
    info = {307: {"name": "Fire/EMS 15", "group": "Fire/EMS", "encrypted": False},
            383: {"name": "WCPD Ch 1", "group": "County Police", "encrypted": False},
            391: {"name": "WCPD Hotline", "group": "County Police", "encrypted": True}}
    c = replace(cfg(tmp_path), p25_dir=str(tmp_path / "p25"), p25_talkgroups={k: v["name"] for k, v in info.items()},
                p25_tg_info=info, p25_incident_talkgroups=frozenset({307}))
    app = App(c)
    sent = []
    app.stt.submit = lambda item, audio: sent.append(item)
    rows = {r["tg"]: r for r in app.state()["talkgroups"]}
    assert rows[307]["main"] and rows[307]["record"] and not rows[383]["main"] and rows[383]["record"]
    assert not rows[391]["record"]                                   # encrypted: off by default
    now = time.time()
    app._call(Call("383-a", 383, "WCPD Ch 1", now, now + 2, [], np.zeros(RATE * 2, np.float32)))
    assert len(sent) == 1
    assert app.set_talkgroup(383, record=False) is None
    app._call(Call("383-b", 383, "WCPD Ch 1", now + 5, now + 7, [], np.zeros(RATE * 2, np.float32)))
    assert len(sent) == 1                                            # dropped on arrival
    assert app.set_talkgroup(383, main=True) is None and app.tg.main(383)
    again = App(c)                                                   # choices survive a restart
    assert not again.tg.record(383) and again.tg.main(383)
    assert {r["tg"]: r for r in again.state()["talkgroups"]}[383]["dropped"] == 0
