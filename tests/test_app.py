import time
from dataclasses import replace

import numpy as np
import pytest

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
    assert inc.incident_type == "Fire alarm" and inc.time_out == "14:32"
    assert inc.departments == ["Port Chester · Fire", "Port Chester FD"]        # the tones' entity (#122), the text's
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


def test_mic_clicks_shown_as_key_ups_weak_speech_kept(tmp_path):
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
    assert wait(lambda: len(app.state()["loose_traffic"]) == 2)
    rows = app.state()["loose_traffic"]
    # User: "log key presses even when they don't have audio... radio id clicked but no transcription".
    assert [(t["status"], t["text"]) for t in rows] == [("no_speech", ""), ("done", "Testing one, two.")]
    assert rows[0]["has_audio"]                                              # the click can still be played


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


def test_unknown_tones_are_a_page_row_until_an_admin_names_them(tmp_path):
    """#80: a page whose tones nobody's code matches is never an event, even naming our unit at our address; it
    stays a page row ("unknown tones") in the 60 Control feed. "Name these tones" (Settings, section tones) puts
    the pair in the plan, re-reads that page, and when the name is ours it becomes a pending event."""
    from dataclasses import replace
    app = App(replace(cfg(tmp_path), unit_departments={"Engine 61": "Port Chester FD"}))
    app._kind = {"South 2": "paging"}
    text = "10 Charlie 2, Engine 61, 209 Westchester Avenue, Pearl Street and Gilbert Place"
    app.stt.client = FakeWhisper({6: text})
    app.stt.start()
    audio = np.concatenate([tone(741.3, 1.0), tone(623.7, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    pid = app.state()["pages"][0]["id"]
    assert wait(lambda: any(r["id"] == pid and r["text"] for r in app.cad.feed.rows(limit=20)))
    assert app.store.active() is None and app.cad.store.incidents() == []           # a page, not an event
    row = next(r for r in app.cad.feed.rows(limit=20) if r["id"] == pid)
    assert row["page"] == "unknown" and row["pc"] == "" and row["events"] == [] and row["tones"][0]["names"] == []
    assert row["voice_at"] > 0 and row["channel"] == "South 2"
    names = app.tone_names()
    assert names["named"] == [] and names["ours"] == ["Port Chester · Fire", "Rye Brook · Fire"]
    assert "Port Chester · Fire" in names["names"] and "Port Chester" in names["places"]
    # Named as another department: in the plan, the row says who, still not an event
    app.cad.settings.post("tones", {"action": "set", "a_hz": 741.3, "b_hz": 623.7, "name": "Purchase", "page": pid, "operator": "Dana"})
    row = next(r for r in app.cad.feed.rows(limit=20) if r["id"] == pid)
    assert row["page"] == "other" and row["tones"][0]["names"] == ["Purchase · Fire"] and app.store.active() is None
    assert app.state()["pages"][0]["departments"] == ["Purchase · Fire"]      # a #84 name, read as place + service
    assert app.cad.settings.audit_log("radios")[0]["key"] == "tones/741.3/623.7"
    # Renamed as ours: the page is read again, the incident opens and the CAD has it queued
    app.cad.settings.post("tones", {"action": "set", "a_hz": 741.3, "b_hz": 623.7, "name": "Port Chester FD", "page": pid, "operator": "Dana"})
    inc = app.store.active()
    assert inc is not None and inc.tone_departments == ["Port Chester · Fire"] and inc.incident_type == "Chest pain"
    assert inc.location == "209 westchester avenue" and inc.pages == [pid]
    ev = app.cad.store.incidents()
    assert len(ev) == 1 and ev[0]["status"] == "queued" and ev[0]["source_ref"] == f"monitor:{inc.id}"
    row = next(r for r in app.cad.feed.rows(limit=20) if r["id"] == pid)
    assert row["page"] == "ours" and row["pc"] == "Port Chester · Fire" and [e["why"] for e in row["events"]] == ["page"]
    named = app.tone_names()["named"]
    assert [(t["key"], t["name"], t["ours"], t["operator"]) for t in named] == [("741.3/623.7", "Port Chester FD", True, "Dana")]
    assert app.cad.settings_export()["overrides"]["tones"]["741.3/623.7"]["name"] == "Port Chester FD"
    # The next page with that pair is ours from the air (the live plan and the page hold share the plan)
    app.stt.client = FakeWhisper({6: "Engine 61, 1 Main Street, structure fire"})
    later = now + 900
    app._segment(Channel(470.2, "South 2"), Segment(later, later + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: len(app.cad.store.incidents()) == 2)
    assert app.state()["pages"][0]["tones"][0]["ours"] is True
    # Forgotten: unknown again on the next page
    app.cad.settings.post("tones", {"action": "delete", "key": "741.3/623.7", "operator": "Dana"})
    assert app.plan.names() == ["Port Chester · Fire"] and app.tone_names()["named"] == []


def test_a_town_is_claimed_only_when_the_gazetteer_is_sure():
    # #80: "34 Longdale Avenue" matched Port Chester at 0.34; the point and label stay for the map, no town.
    from types import SimpleNamespace as M
    from sizeup.app import geo_of
    weak = geo_of(M(lat=41.0, lon=-73.66, label="34 Longdale Ave, Port Chester", kind="address", municipality="Port Chester", confidence=0.34))
    assert weak["town"] == "" and weak["label"] == "34 Longdale Ave, Port Chester" and weak["confidence"] == 0.34
    sure = geo_of(M(lat=41.0, lon=-73.66, label="209 Westchester Ave", kind="address", municipality="Port Chester", confidence=0.91))
    assert sure["town"] == "Port Chester"
    assert geo_of(None) is None


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
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: [])
    c = replace(cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                channels=(ChannelCfg("South 2", 470.2, "paging", "fire-page"), ChannelCfg("FG1", 453.0375, "traffic", "fire-fg")),
                shared_device="00000001", start_mode="South 2", p25_dir=str(tmp_path / "p25"),
                p25_talkgroups={307: "Fire/EMS 15"}, p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=x",
                p25_recorder_dir=str(tmp_path))
    app = App(c).start()
    try:
        st = app.state()
        assert st["sdr"]["modes"] == ["South 2", "FG1", "Fire/EMS 15", "Off"] and st["sdr"]["mode"] == "South 2"
        radios = {r["label"]: r for r in st["radios"]}
        assert radios["FG1"]["state"] == "off" and radios["FG1"]["detail"] == "SDR on South 2"
        south = app._receiver("South 2")
        assert south.device.serial == "00000001" and south.device() is None   # looked up by serial at start; not plugged in here
        assert app.set_mode("FG1") is None and app.state()["sdr"]["mode"] == "FG1"
        assert {r["label"]: r["state"] for r in app.state()["radios"]}["South 2"] == "off"
        assert app.set_mode("Fire/EMS 15") is None
        assert wait(lambda: app.recorder.state == "absent")                 # not installed yet: says so
        assert app.set_mode("Nope") is not None
        assert app.set_mode("South 2") is None and app.recorder.state == "off"
        # Off: release the dongle for other programs; nothing of ours holds it.
        assert app.set_mode("Off") is None
        radios = {r["label"]: r for r in app.state()["radios"]}
        assert all(radios[k]["state"] == "off" and radios[k]["detail"] == "SDR released (Off)"
                   for k in ("South 2", "FG1", "Fire/EMS 15"))
        assert all(r._proc is None or r._proc.poll() is not None for r in app.receivers)
        assert app.set_mode("FG1") is None and app.state()["sdr"]["mode"] == "FG1"
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


def test_talkgroup_counts_survive_a_restart(tmp_path):
    from dataclasses import replace
    from sizeup.incidents import Traffic
    info = {329: {"name": "Fire/EMS 22", "group": "Fire/EMS", "encrypted": False}}
    c = replace(cfg(tmp_path), p25_dir=str(tmp_path / "p25"), p25_tg_info=info, p25_talkgroups={329: "Fire/EMS 22"})
    app = App(c)
    now = time.time()
    app.store.on_traffic(Traffic("329-a", "Fire/EMS 22", now - 60, now - 58, "7911", talkgroup=329), attach=False)
    again = App(c)
    row = {r["tg"]: r for r in again.state()["talkgroups"]}[329]
    assert row["count"] == 1 and abs(row["last"] - (now - 58)) < 1


def test_whole_talkgroup_section_on_off_persists(tmp_path):
    # User: "add the ability to turn on and off a whole section too, and let these settings persist restarts".
    from dataclasses import replace
    info = {383: {"name": "WCPD Ch 1", "group": "County Police", "encrypted": False},
            385: {"name": "WCPD Ch 3", "group": "County Police", "encrypted": False},
            391: {"name": "WCPD Hotline", "group": "County Police", "encrypted": True},
            307: {"name": "Fire/EMS 15", "group": "Fire/EMS", "encrypted": False}}
    c = replace(cfg(tmp_path), p25_dir=str(tmp_path / "p25"), p25_tg_info=info,
                p25_talkgroups={k: v["name"] for k, v in info.items()}, p25_incident_talkgroups=frozenset({307}))
    app = App(c)
    assert app.set_talkgroup_group("County Police", record=False) is None
    assert not any(app.tg.record(t) for t in (383, 385, 391)) and app.tg.record(307)
    again = App(c)                                           # persists across a restart
    assert not again.tg.record(383) and not again.tg.record(385) and again.tg.record(307)
    assert again.set_talkgroup_group("County Police", record=True) is None
    assert again.tg.record(383) and again.tg.record(385) and not again.tg.record(391)   # encrypted stays off
    assert App(c).tg.record(385)
    assert again.set_talkgroup_group("Nope", record=False) is not None


def test_rerun_one_or_all_replaces_text_keeps_history_and_survives_restart(tmp_path):
    from sizeup.stt import RERUN
    c = replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15"})
    app = App(c)
    app.stt.client = FakeWhisper({3: "77 Alpha 4 joe evac 1937", 2: "Engine 60 on location"})
    app.stt.start()
    now = time.time()
    a, b = voice(3.0, 1), voice(2.0, 2)
    app._call(Call("307-a", 307, "Fire/EMS 15", now - 30, now - 27, ["77A4"], a, ""))
    app._call(Call("307-b", 307, "Fire/EMS 15", now - 10, now - 8, ["E60"], b, ""))
    assert wait(lambda: len(app.store.loose) == 2 and all(t.status == "done" for t in app.store.loose))
    assert (tmp_path / "clips" / "307-a.src.wav").is_file()            # STT input kept for re-runs

    # One recording, after the model got better.
    app.stt.client = FakeWhisper({3: "77 Alpha 4, Sixty Control, 1937", 2: "Engine 60 on location"})
    assert app.retranscribe(["307-a"]) == 1
    assert app.retranscribe(["307-a"]) in (0, 1)                        # already queued: not doubled
    assert wait(lambda: app.state()["rerun"]["pending"] == 0)
    t = next(t for t in app.store.loose if t.id == "307-a")
    assert t.text == "77 Alpha 4, Sixty Control, 1937"
    assert t.history[-1]["text"] == "77 Alpha 4 joe evac 1937" and t.history[-1]["model"] == "m.bin"   # who wrote the old text
    assert t.model == "m.bin"

    # Everything: the unchanged one gets no history entry.
    assert app.retranscribe(None) == 2
    assert wait(lambda: app.state()["rerun"]["pending"] == 0)
    st = app.state()["rerun"]
    assert st["total"] == 2 and st["done"] == 2 and st["changed"] == 0
    b_row = next(t for t in app.store.loose if t.id == "307-b")
    assert not getattr(b_row, "history", None)
    assert b_row.rerun_at                                               # ran, same text: the display still hears about it

    # The saved history comes back after a restart.
    app2 = App(c)
    t2 = next(t for t in app2.store.loose if t.id == "307-a")
    assert t2.text == "77 Alpha 4, Sixty Control, 1937" and t2.history[-1]["text"] == "77 Alpha 4 joe evac 1937"


def test_rerun_waits_behind_live_traffic():
    import queue
    from sizeup.stt import LIVE, RERUN, Transcriber
    tr = Transcriber(client=None, on_done=lambda i: None)
    tr.submit("rerun-1", np.zeros(1), priority=RERUN)
    tr.submit("live-1", np.zeros(1))
    tr.submit("rerun-2", np.zeros(1), priority=RERUN)
    tr.submit("live-2", np.zeros(1), priority=LIVE)
    order = [tr._q.get_nowait()[2] for _ in range(4)]
    assert order == ["live-1", "live-2", "rerun-1", "rerun-2"]


def test_talker_prompt_reaches_whisper_only_when_switched_on(tmp_path):
    seen = []

    class Spy(FakeWhisper):
        def transcribe(self, audio, prompt=""):
            seen.append(prompt)
            return "77 Alpha 3, arrival Stamford", 0.9

    now = time.time()
    for mode, want in (("off", ""), ("ids", "77 Alpha 3, 60 Control.")):  # talkers in talk order
        seen.clear()
        app = App(replace(cfg(tmp_path / mode), p25_talkgroups={307: "Fire/EMS 15"}, stt_talker_prompt=mode))
        app.stt.client = Spy({})
        app.stt.start()
        app._call(Call(f"307-{mode}", 307, "Fire/EMS 15", now, now + 2, ["77A3", "60 Ctrl OP 7"], voice(2.0), ""))
        assert wait(lambda: seen)
        assert seen[0] == want


def test_call_is_split_into_one_row_per_talker(tmp_path):
    from sizeup.app import split_turns
    now = 1000.0
    audio = np.arange(13 * RATE, dtype=np.float32)
    call = Call("307-x", 307, "Fire/EMS 15", now, now + 13, ["60 Ctrl OP 8", "45M6", "60 Ctrl OP 8"], audio, "",
                [(0.0, "60 Ctrl OP 8"), (5.84, "45M6"), (11.76, "60 Ctrl OP 8"), (12.9, "45M6")])
    turns = split_turns(call)
    # The 0.1 s 45M6 key-up at the end is a real key-up (named, with sound): its own row (issue #62),
    # no longer folded into the dispatcher's last turn.
    assert [(t.id, t.talker) for t in turns] == [
        ("307-x-t0", "60 Ctrl OP 8"), ("307-x-t1", "45M6"), ("307-x-t2", "60 Ctrl OP 8"), ("307-x-t3", "45M6")]
    assert turns[1].start == now + 5.84 and turns[3].audio[-1] == audio[-1] and turns[3].stt
    assert sum(len(t.audio) for t in turns) == len(audio)                 # nothing lost, nothing doubled
    one = Call("307-y", 307, "Fire/EMS 15", now, now + 2, ["77A4"], audio[:2 * RATE], "", [(0.0, "77A4")])
    assert [t.id for t in split_turns(one)] == ["307-y"]                    # one talker: the call's own id

    seen = []

    class Spy(FakeWhisper):
        def transcribe(self, audio, prompt=""):
            seen.append(prompt)
            return "words", 0.9

    app = App(replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15"}, stt_talker_prompt="ids"))
    app.stt.client = Spy({})
    app.stt.start()
    app._call(Call("307-z", 307, "Fire/EMS 15", time.time(), time.time() + 6, ["60 Ctrl OP 6", "77A3"],
                   np.concatenate([voice(3.0, 1), voice(3.0, 2)]), "", [(0.0, "60 Ctrl OP 6"), (3.0, "77A3")]))
    assert wait(lambda: len(seen) == 2)
    assert sorted(seen) == ["60 Control.", "77 Alpha 3."]                   # each piece hinted with its own talker
    assert sorted(t.unit for t in app.store.loose) == ["60 Ctrl OP 6", "77A3"]


def test_p25_clip_ids_with_dots_play_and_paths_dont(tmp_path):
    app = App(replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15"}))
    app.stt.submit = lambda item, audio, priority=0: True
    cid = "307-1790954731.970_476112500.0-call_11"
    app._call(Call(cid, 307, "Fire/EMS 15", time.time(), time.time() + 2, ["77A4"], voice(2.0), ""))
    app.clips.clear()                                   # served from disk, as after a restart
    assert app.clip(cid)[:4] == b"RIFF"
    for bad in ("../incidents", "..", "a/../../x", "/etc/passwd", ".hidden", ""):
        assert app.clip(bad) is None



def test_sdr_rows_follow_the_dongles_plugged_in(tmp_path, monkeypatch):
    # User: "select multiple at the same time" + "those should dynamically appear when we plug in or
    # unplug different sdrs, not be static".
    from dataclasses import replace
    import sizeup.app as appmod
    from sizeup.config import ChannelCfg
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    plugged = [(0, "Blog V4", "v4"), (1, "RTL2838UHIDIR", "keep-out")]
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(appmod.sdr_lookup, "resolve",
                        lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
    c = replace(cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                channels=(ChannelCfg("South 2", 470.2, "paging", "fire-page"), ChannelCfg("FG1", 453.0375, "traffic", "fire-fg")),
                sdr_auto=True, sdr_names={"adsb": "V3"}, sdr_ignore=("keep-out",),
                p25_dir=str(tmp_path / "p25"), p25_talkgroups={307: "Fire/EMS 15"},
                p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=x", p25_recorder_dir=str(tmp_path))
    app = App(c).start()
    rows = lambda: [(x["name"], x["mode"], x["present"]) for x in app.state()["sdr"]["sdrs"]]
    try:
        assert rows() == [("v4", "Off", True)]                              # ignored dongle never shown; nothing grabbed
        plugged.append((2, "RTL2838UHIDIR", "adsb"))                         # plug one in: a row appears, Off
        assert rows() == [("v4", "Off", True), ("V3", "Off", True)]
        assert app.set_mode("Fire/EMS 15", "v4") is None and app.set_mode("FG1", "adsb") is None
        assert {r.channel.label: r.argv(r.device())[-2] for r in app.receivers if r.channel.label == "FG1"} == {"FG1": "2"}
        assert app.recorder.device_fn() == "0"                              # trunk-recorder on the V4, by index
        radios = {r["label"]: r for r in app.state()["radios"]}
        assert radios["FG1"]["state"] != "off" and radios["Fire/EMS 15"]["state"] != "off"   # both at once
        assert app.set_mode("FG1", "v4") is None                            # moves: V3 goes Off, P25 stops
        assert app.assign["adsb"] == "Off" and app.assign["v4"] == "FG1"
        assert app.set_mode("South 2", "adsb") is None
        assert app.set_mode("FG1", "keep-out") is not None                  # ignored: never used
        assert app.set_mode("FG1", "nope") is not None                      # not plugged in
        plugged.pop()                                                        # V3 unplugged while running South 2: stays, marked
        assert rows() == [("v4", "FG1", True), ("V3", "South 2", False)]
        assert app.set_mode("Off", "adsb") is None and rows() == [("v4", "FG1", True)]   # idle + unplugged: gone
    finally:
        app.stop()
    plugged.append((2, "RTL2838UHIDIR", "adsb"))
    again = App(c)                                                          # choices survive a restart
    assert again.assign == {"v4": "FG1", "adsb": "Off"}


def test_trunk_recorder_gets_the_dongle_index_not_the_serial(tmp_path):
    import json
    from sizeup.radio.recorder import TrunkRecorder
    (tmp_path / "config.json").write_text(json.dumps({"sources": [{"device": "rtl=00000001", "rate": 2400000}]}))
    exe = tmp_path / "trunk-recorder"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    r = TrunkRecorder(f"{exe} --config=config.json", str(tmp_path))
    argv = r._argv_for(0)
    assert argv[-1] == "--config=config.runtime.json"
    assert json.loads((tmp_path / "config.runtime.json").read_text())["sources"][0]["device"] == "rtl=0"
    assert json.loads((tmp_path / "config.json").read_text())["sources"][0]["device"] == "rtl=00000001"   # untouched
    r.device_fn, r.device_name = (lambda: None), "v4"
    r.start()
    try:
        assert wait(lambda: r.state == "absent") and "v4" in r.detail and "not plugged in" in r.detail
    finally:
        r.stop()


def test_trunk_recorder_runtime_config_runs_the_unit_script_by_absolute_path(tmp_path):
    # The talker at the grant (sizeup/radio/live.py) needs trunk-recorder's unit_script plugin on:
    # every generated config.runtime.json names deploy/trunk-recorder/unit-event.sh absolutely.
    import json
    import shutil
    import subprocess
    from pathlib import Path
    from sizeup.radio.recorder import UNIT_SCRIPT_MAX, TrunkRecorder, unit_script_command
    import tempfile
    deploy = Path(__file__).parent.parent / "deploy" / "trunk-recorder"
    short = Path(tempfile.mkdtemp(prefix="tr-", dir="/tmp"))     # pytest's tmp_path is too long a path here
    work = short / "trunk-recorder"
    work.mkdir()
    for f in ("config.json", "unit-event.sh"):
        shutil.copy2(deploy / f, work / f)                       # keeps the script's x bit
    r = TrunkRecorder("trunk-recorder --config=config.json", str(work))
    assert r._argv_for(3)[-1] == "--config=config.runtime.json"
    conf = json.loads((work / "config.runtime.json").read_text())
    script = str((work / "unit-event.sh").resolve())
    assert [s["unitScript"] for s in conf["systems"]] == [script] and conf["sources"][0]["device"] == "rtl=3"
    # ... which trunk-recorder runs with system("<unitScript> <shortName> <radioID> call <tg> <patches> &").
    out = subprocess.run(["/bin/sh", "-c", f"{conf['systems'][0]['unitScript']} westchester 41228 call 307  &"],
                         capture_output=True, text=True, timeout=10).stdout
    assert out == "SIZEUP-UNIT westchester 41228 call 307\n"
    # No dongle handed over (not a shared dongle): still generated, sources as configured.
    assert r._argv_for()[-1] == "--config=config.runtime.json"
    conf = json.loads((work / "config.runtime.json").read_text())
    assert conf["systems"][0]["unitScript"] == script and conf["sources"][0]["device"] == "rtl=v4"
    # "unitScript": "" in config.json turns it off; no script next to the config: none.
    (work / "config.json").write_text(json.dumps(dict(conf, systems=[dict(conf["systems"][0], unitScript="")])))
    r._argv_for(0)
    assert "unitScript" not in json.loads((work / "config.runtime.json").read_text())["systems"][0]
    assert unit_script_command(tmp_path) == ""
    # A path with a space is quoted (the plugin hands it to a shell); one too long for the plugin's
    # 200-byte command line is run as ./unit-event.sh (trunk-recorder's cwd is that directory).
    spaced = short / "a dir"
    spaced.mkdir()
    shutil.copy2(deploy / "unit-event.sh", spaced / "unit-event.sh")
    assert unit_script_command(spaced) == f"'{(spaced / 'unit-event.sh').resolve()}'"
    deep = short / ("d" * (UNIT_SCRIPT_MAX - len(str(short))))
    deep.mkdir()
    shutil.copy2(deploy / "unit-event.sh", deep / "unit-event.sh")
    assert unit_script_command(deep) == "./unit-event.sh"
    (deep / "unit-event.sh").chmod(0o644)
    assert unit_script_command(deep) == "/bin/sh ./unit-event.sh"
    shutil.rmtree(short)


def test_unreadable_mdc_burst_is_noted_and_kept(tmp_path):
    # User: "are you not getting a radio key from this radio? these are our new Motorola 6000s".
    # A hissy transmission: a burst is cut off but fails to decode -> said so on the row, burst kept.
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    app.stt.client = FakeWhisper({2: "Testing one two"})
    app.stt.start()
    rng = np.random.default_rng(5)
    burst = (0.3 * rng.standard_normal(RATE // 4)).astype(np.float32)          # noise where the ID was
    now = time.time()
    app._segment(Channel(453.0375, "FG1"), Segment(now, now + 2, voice(2.0), 20.0, [burst]))
    app._segment(Channel(453.0375, "FG1"), Segment(now + 5, now + 7, voice(2.0, 3), 20.0, []))
    assert wait(lambda: len(app.state()["loose_traffic"]) == 2)
    rows = app.state()["loose_traffic"]
    assert [t.get("id_note") for t in rows] == ["ID burst unreadable", "no ID burst"]
    assert (tmp_path / "clips" / f"{rows[0]['id']}.mdc.wav").is_file()


def test_dongle_held_by_shortfinal_is_left_alone(tmp_path, monkeypatch):
    import json, os
    from dataclasses import replace
    import sizeup.app as appmod
    import sizeup.radio.claims as claims
    from sizeup.config import ChannelCfg
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    plugged = [(0, "Blog V4", "v4"), (1, "RTL2838UHIDIR", "adsb")]
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(appmod.sdr_lookup, "resolve", lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "adsb.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": "TWR", "since": 1.0}))
    (tmp_path / "sdr_modes.json").write_text(json.dumps({"v4": "Off", "adsb": "FG1"}))   # saved, but now taken
    c = replace(cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"), clips_dir=str(tmp_path / "clips"),
                channels=(ChannelCfg("South 2", 470.2, "paging", "p"), ChannelCfg("FG1", 453.0375, "traffic", "f")), sdr_auto=True)
    (tmp_path / "clips").mkdir(exist_ok=True)
    app = App(c).start()
    try:
        assert app.assign["adsb"] == "Off"                                   # didn't fight shortfinal for it
        rows = {x["serial"]: x for x in app.state()["sdr"]["sdrs"]}
        assert rows["adsb"]["held_by"] == {"app": "shortfinal", "mode": "TWR"} and rows["v4"]["held_by"] is None
        assert "in use by shortfinal (TWR)" in app.set_mode("FG1", "adsb")
        assert app.set_mode("FG1", "v4") is None
        mine = json.loads((claims.DIR / "v4.json").read_text())
        assert (mine["app"], mine["mode"], mine["pid"]) == ("sizeup", "FG1", os.getpid())
        assert app.set_mode("Off", "v4") is None and not (claims.DIR / "v4.json").exists()   # released
        assert app.set_mode("South 2", "v4") is None
    finally:
        app.stop()
    assert not (claims.DIR / "v4.json").exists()                            # released on shutdown
    assert (claims.DIR / "adsb.json").exists()                              # shortfinal's untouched


def test_apx6000_id_sent_late_in_the_transmission_is_found(tmp_path):
    # FG1 2026-10-07 14:34:56: an APX 6000 (6301) whose post-IDs sit 0.3-0.8 s in. The 0.3 s edge
    # search missed them and the row said "no ID burst". Real clip.
    import wave
    from pathlib import Path
    from sizeup.radio.fm import NoiseSquelch
    with wave.open(str(Path(__file__).parent / "fixtures" / "mdc" / "apx6000_6301_late_burst.wav")) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    app = App(cfg(tmp_path))
    seg = Segment(0, len(audio) / RATE, audio, 20.0, [])                 # as stored: nothing cut
    assert app._units(seg) == ["6301"] and app._id_note("FG1", seg, ["6301"]) == ""
    hiss = (0.05 * np.random.default_rng(1).standard_normal(len(audio))).astype(np.float32)
    assert app._id_note("FG1", Segment(0, 1, hiss, 20.0, []), []) == "no ID burst"


def test_a_page_that_fades_and_comes_back_is_one_page_sent_once(tmp_path, capsys):
    # GitHub #61: the squelch closed in a fade and the page came back 2 s later as a continuation. The
    # page shows as pending at once, goes to the hub and speech-to-text once, after the window, whole.
    app = App(cfg(tmp_path))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({10: PAGE_TEXT})
    app.stt.start()
    sent = []
    app.events = lambda kind, data, clip: sent.append((kind, data, clip))
    head = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(4.0)]).astype(np.float32)
    tail = voice(4.0, seed=1)
    ch, now = Channel(470.2, "South 2"), time.time()
    first = Segment(now, now + len(head) / RATE, head, 25.0, reason="quiet", hold_until=now + len(head) / RATE + 4.5)
    app._segment(ch, first)
    page = app.pages[0]
    assert page.kind == "page" and page.status == "pending" and sent == []     # shown, not sent on yet
    cont = Segment(first.end + 2.0, first.end + 6.0, tail, 9.0, reason="quiet", cont=True, hold_until=first.end + 10.5)
    app._segment(ch, cont)
    assert list(app.pages) == [page] and sent == []
    app._release_pages(now=cont.hold_until - 1.0)
    assert sent == []
    app._release_pages(now=cont.hold_until + 1.0)
    assert [k for k, _d, _c in sent] == ["page"] and sent[0][2] == page.id
    assert sent[0][1]["departments"] == ["Port Chester · Fire"] and sent[0][1]["end"] == pytest.approx(cont.end, abs=0.01)
    assert wait(lambda: page.status == "done") and page.raw == PAGE_TEXT       # the voice, transcribed whole
    assert len(page.audio) / RATE == pytest.approx(cont.end - first.start, abs=0.01)
    assert app.clip(page.id) is not None and len(app.clip(page.id)) > 2 * RATE * 13
    assert wait(lambda: app.store.active() is not None)
    out = capsys.readouterr().out
    assert "South 2 recording end: page 14.2 s · quiet after hang" in out and "1 continuation(s) joined (gap 2.0 s)" in out
    app._release_pages(now=cont.hold_until + 60)
    assert [k for k, _d, _c in sent].count("page") == 1
