"""[cloud]: the hub (no radios, fed by the station) and the station (all-in-one + the hub link).
Real HTTP on ephemeral ports, in-process; nothing opens an SDR or starts a radio program."""

import base64
import io
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sizeup.app import App, make_app, make_server
from sizeup.cad.dispatcher import wav_bytes
from sizeup.cloud import hub as hub_mod
from sizeup.cloud.hub import HubApp
from sizeup.cloud.outbox import Outbox
from sizeup.config import ChannelCfg, Config, ConfigError, load
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.radio.tones import ToneCode
from tests.test_app import PAGE_TEXT, FakeWhisper, tone, voice, wait

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-station-token-0123456789"
CHANNELS = (ChannelCfg("South 2", 470.2, "paging"), ChannelCfg("FG1", 453.0375, "traffic"))


def base(**kw) -> Config:
    return Config(**{"station_id": "t", "station_name": "Test", "host": "127.0.0.1", "port": 0, "rtl_fm": "rtl_fm",
                     "channels": CHANNELS, "tones": (ToneCode("Port Chester FD", 349.1, 311.1, "PCFD"),),
                     "ours": frozenset({"Port Chester FD", "Rye Brook FD"}), "stt_server": "whisper-server",
                     "stt_model": "m.bin", "stt_port": 1, "stt_threads": 1, "stt_prompt": "", "p25_dir": "", **kw})


def hub_cfg(tmp_path, **kw) -> Config:
    return base(**{"cad_db": str(tmp_path / "hub" / "cad.sqlite"), "cloud_role": "hub", "cloud_token": TOKEN, **kw})


def station_cfg(tmp_path, url: str, **kw) -> Config:
    return base(clips_dir=str(tmp_path / "st" / "clips"), cloud_role="station", cloud_hub_url=url, cloud_token=TOKEN,
                cloud_outbox=str(tmp_path / "st" / "outbox.sqlite"), **kw)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(url, method="GET", body=None, token=TOKEN, timeout=10.0):
    """(status, JSON or bytes)."""
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, ctype = r.read(), r.headers.get("Content-Type", "")
            return r.status, json.loads(raw) if "json" in ctype else raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


class HubRun:
    """A hub serving on an ephemeral port (or a given one), in this process."""

    def __init__(self, cfg: Config):
        self.app = make_app(cfg).start()
        assert isinstance(self.app, HubApp)
        self.server = make_server(self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.httpd.server_address[1]}"

    def get(self, path):
        code, body = call(self.url + path, token=None)
        assert code == 200, (path, code, body)
        return body

    def close(self):
        self.server.shutdown()
        self.app.stop()


def station(cfg: Config, texts: dict) -> App:
    """A station: the all-in-one app with the hub link; speech-to-text faked, no receivers started."""
    app = make_app(cfg)
    assert app.link is not None and app.events is not None
    app.stt.client = FakeWhisper(texts)
    app.stt.start()
    app.live.on_change = app.link.live_changed
    app.link.start()
    return app


def all_traffic(state: dict) -> list[dict]:
    return list(state["loose_traffic"]) + [t for i in state["incidents"] for t in i["traffic"]]


def uid(cad, name):
    return next(u["id"] for u in cad.store.units() if u["name"] == name)


def event_with(cad, units, operator="Lt. Ruiz"):
    inc = cad.create({"type": "STRU", "address": "209 Westchester Ave", "operator": operator})
    r = cad.dispatch(inc["id"], {"unit_ids": [uid(cad, u) for u in units], "operator": operator})
    return inc, r


def fm_segment(app, text_s=3.0):
    now = time.time()
    app._segment(Channel(453.0375, "FG1"), Segment(now, now + text_s, voice(text_s), 20.0))
    return f"FG1-{int(now * 1000)}"


# -- auth ----------------------------------------------------------------------------------------------


def test_hub_refuses_a_missing_or_wrong_token_and_everything_without_one_configured(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        for path, method in (("/api/station/events", "POST"), ("/api/station/state", "POST"),
                             ("/api/station/jobs?wait=0", "GET"), ("/api/station/jobs/0123456789abcdef", "POST")):
            body = {} if method == "POST" else None
            assert call(hub.url + path, method, body, token=None)[0] == 401
            assert call(hub.url + path, method, body, token="wrong-token-wrong-token")[0] == 401
            assert call(hub.url + path, method, body, token=TOKEN + "x")[0] == 401
        assert call(hub.url + "/api/station/jobs?wait=0")[0] == 200
        assert hub.app.hub.connected()                   # a good token counts as the station being there
    finally:
        hub.close()
    nohub = HubRun(hub_cfg(tmp_path / "b", cloud_token=""))
    try:
        code, body = call(nohub.url + "/api/station/events", "POST", {"epoch": "x", "events": []})
        assert code == 403 and "no station token" in body["error"]
        row = nohub.get("/api/state")["radios"][0]
        assert row["label"] == "Station" and row["state"] == "down" and "SIZEUP_STATION_TOKEN" in row["detail"]
    finally:
        nohub.close()


# -- traffic, transcripts, talking now -------------------------------------------------------------------


def test_station_traffic_shows_on_the_hub_with_its_audio_and_on_the_cad_event(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        inc, r = event_with(hub.app.cad, ["E63"])
        # No station yet: the dispatch goes out as text, and says why (never a hung request).
        assert r["audio_url"] is None and r["audio_note"].startswith("Voice unavailable") and "station" in r["audio_note"]
        st = station(station_cfg(tmp_path, hub.url), {3: "Engine 63 on location nothing showing"})
        tid = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid for t in all_traffic(hub.get("/api/state"))), 15)
        t = next(t for t in all_traffic(hub.get("/api/state")) if t["id"] == tid)
        assert t["text"] == "Engine 63 on location nothing showing" and t["has_audio"] and t["source"] == "FG1"
        # The CAD of record: on the event (an unidentified radio on FG1, one call with units on it).
        radio = hub.get(f"/api/cad/incident/{inc['id']}")["radio"]
        assert [(x["id"], x["why"], x["has_audio"]) for x in radio] == [(tid, "channel", True)]
        wav = hub.get(f"/api/audio/{tid}.wav")
        with wave.open(io.BytesIO(wav)) as w:
            assert w.getframerate() == 8000 and w.getnframes() == pytest.approx(3.0 * 8000, abs=10)
        # The station's own display is unchanged (all-in-one), plus its "Hub" row.
        assert any(x.id == tid for x in st.store.loose) or any(x.id == tid for i in st.store.incidents for x in i.traffic)
        assert wait(lambda: st.link.status_row()["state"] == "ok", 10)
        # A transcript that changes afterwards (a re-run) follows it up, into the CAD's log too.
        st._retext(tid, "Engine 63 on location, investigating", "done", 0.95, "m2.bin")
        assert wait(lambda: next(x for x in all_traffic(hub.get("/api/state")) if x["id"] == tid)["text"]
                    == "Engine 63 on location, investigating", 10)
        row = next(x for x in hub.get("/api/cad/comms")["rows"] if x["id"] == tid)
        assert row["text"] == "Engine 63 on location, investigating" and row["model"] == "m2.bin"
        # The station's status on the hub: its rows as reported, after the Station row.
        assert wait(lambda: [r["label"] for r in hub.get("/api/state")["radios"]][:1] == ["Station"]
                    and "FG1" in [r["label"] for r in hub.get("/api/state")["radios"]], 10)
        assert hub.get("/api/state")["radios"][0]["state"] == "ok"
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
    # A redeploy: everything comes back from the hub's SQLite (no captures/, no JSON files).
    again = HubApp(hub_cfg(tmp_path))
    assert any(x.id == tid for x in list(again.store.loose) + [x for i in again.store.incidents for x in i.traffic])
    assert again.has_clip(tid) and again.clip(tid)[:4] == b"RIFF"
    assert [x["id"] for x in again.cad.full(inc["id"])["radio"]] == [tid]
    assert not list((tmp_path / "hub").glob("*.json")) and not (tmp_path / "hub" / "captures").exists()


def test_talking_now_reaches_the_hub_in_well_under_a_second(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {})
    try:
        assert wait(lambda: hub.app.hub.status is not None, 10)     # first status (and heartbeat) in
        t0 = time.time()
        st.live.fm_open("FG1", t0)
        assert wait(lambda: [e["channel"] for e in hub.app.live.state()] == ["FG1"], 3)
        assert time.time() - t0 < 1.0
        assert [e["channel"] for e in hub.app.cad.state()["talking"]] == ["FG1"]
        st.live.fm_ids("FG1", ["6301"])
        assert wait(lambda: [e["radio_id"] for e in hub.app.live.state()] == ["6301"], 3)
        st.live.fm_close("FG1")
        assert wait(lambda: hub.app.live.state() == [], 3)
    finally:
        st.link.stop()
        hub.close()


def test_a_talking_snapshot_from_a_station_that_went_quiet_is_dropped():
    clock = [1000.0]
    live = hub_mod.RemoteLive(lambda s, r, a: None, clock=lambda: clock[0])
    live.update([{"key": "fm:FG1", "system": "fm", "channel": "FG1", "radio_id": "", "ended": False}])
    assert len(live.state()) == 1
    clock[0] += hub_mod.LIVE_STALE_S + 1
    assert live.state() == []


def test_a_60_control_page_makes_the_same_pending_cad_event_on_the_hub(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {6: PAGE_TEXT})
    try:
        audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
        now = time.time()
        st._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))

        def queued(cad):
            return [{k: i[k] for k in ("type", "subtype", "address", "cross_street", "status", "remarks", "lat", "lon")}
                    for i in cad.state()["incidents"] if i["status"] == "queued"]
        assert wait(lambda: queued(st.cad), 15)
        assert wait(lambda: queued(hub.app.cad), 15)
        assert queued(hub.app.cad) == queued(st.cad)
        assert wait(lambda: [p["raw"] for p in hub.get("/api/state")["pages"]] == [PAGE_TEXT], 5)   # pushed once a second
        page = hub.get("/api/state")["pages"][0]
        assert page["raw"] == PAGE_TEXT and page["dispatch"] and page["has_audio"]
        assert hub.get("/api/state")["incidents"][0]["departments"] == st.state()["incidents"][0]["departments"]
    finally:
        st.link.stop()
        hub.close()


# -- delivery ----------------------------------------------------------------------------------------------


def _traffic_event(seq, tid, at):
    data = {"id": tid, "source": "FG1", "start": at, "end": at + 2, "text": "Engine 60 responding", "status": "done",
            "unit": "", "talkgroup": None, "rig": "", "radio_id": "", "raw": "Engine 60 responding", "confidence": 0.9,
            "model": "m.bin", "attach": True, "pairs": None}
    wav = wav_bytes(np.zeros(1600, np.float32), 8000)
    return {"seq": seq, "at": at, "type": "traffic", "data": data, "audio": base64.b64encode(wav).decode()}


def test_a_retried_delivery_is_applied_once(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        now = time.time()
        batch = {"epoch": "e1", "events": [_traffic_event(1, "FG1-1", now),
                                           {"seq": 2, "at": now, "type": "heard",
                                            "data": {"system": "mdc", "radio_id": "6301", "alias": "", "at": now}}]}
        assert call(hub.url + "/api/station/events", "POST", batch)[1] == {"ok": True, "acked": 2, "applied": 2}
        assert call(hub.url + "/api/station/events", "POST", batch)[1] == {"ok": True, "acked": 2, "applied": 0}
        # A new outbox (deleted on the station) starts over at seq 1: the same transmission is still one row.
        again = dict(batch, epoch="e2")
        assert call(hub.url + "/api/station/events", "POST", again)[1]["acked"] == 2
        assert [t["id"] for t in all_traffic(hub.app.state())] == ["FG1-1"]
        assert [r["id"] for r in hub.get("/api/cad/comms?type=radio")["rows"]] == ["FG1-1"]
        assert hub.app.radios.get("mdc", "6301") is not None
        # Remembered across a restart of the hub (hub.sqlite), so a late retry after a redeploy is skipped too.
        hub.close()
        hub = HubRun(hub_cfg(tmp_path))
        assert call(hub.url + "/api/station/events", "POST", again)[1]["applied"] == 0
        # One bad event doesn't block the ones behind it.
        bad = {"epoch": "e2", "events": [{"seq": 3, "type": "traffic", "data": {"id": "../etc"}},
                                         _traffic_event(4, "FG1-4", now)]}
        assert call(hub.url + "/api/station/events", "POST", bad)[1]["acked"] == 4
        assert {t["id"] for t in all_traffic(hub.app.state())} == {"FG1-1", "FG1-4"}
        assert hub.app.hub.errors == 1
    finally:
        hub.close()


def test_the_outbox_survives_a_restart_and_drains_when_the_hub_is_back(tmp_path):
    port = free_port()
    cfg = station_cfg(tmp_path, f"http://127.0.0.1:{port}")
    st = station(cfg, {3: "Engine 63 responding"})
    tid = fm_segment(st)
    assert wait(lambda: st.link.outbox.pending()[0] >= 1, 10)
    assert wait(lambda: st.link.status_row()["state"] == "down", 10)        # the hub isn't there: said so
    assert "nothing is lost" in st.link.status_row()["detail"]
    st.link.stop()
    epoch = st.link.outbox.epoch
    st.link.outbox.close()
    left = Outbox(cfg.cloud_outbox)
    assert left.pending()[0] >= 1 and left.epoch == epoch                    # on disk, same outbox
    left.close()
    hub = HubRun(hub_cfg(tmp_path, port=port))
    st2 = station(cfg, {})                                                   # the station restarted
    try:
        assert wait(lambda: any(t["id"] == tid for t in all_traffic(hub.get("/api/state"))), 15)
        assert wait(lambda: st2.link.outbox.pending()[0] == 0, 10)
        assert hub.get(f"/api/audio/{tid}.wav")[:4] == b"RIFF"
    finally:
        st2.link.stop()
        hub.close()


def test_the_outbox_drops_the_oldest_audio_first_never_the_events(tmp_path):
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=2500)
    for k in range(3):
        ob.add("traffic", {"id": f"FG1-{k}"}, b"x" * 1000)
    rows = ob.batch()
    assert [r["data"]["id"] for r in rows] == ["FG1-0", "FG1-1", "FG1-2"]
    assert [(r["audio"] is None, r["audio_dropped"]) for r in rows] == [(True, True), (False, False), (False, False)]
    ob.ack(rows[1]["seq"])
    assert ob.pending()[0] == 1 and ob.audio_bytes == 1000


# -- jobs: the dispatch voice and the switches --------------------------------------------------------------


class FakeStation:
    """Long-polls the hub's jobs like the station does; answers tts with a WAV, calls with None."""

    def __init__(self, url, answer=True):
        self.url, self.answer, self.jobs, self.stop = url, answer, [], threading.Event()
        self.wav = wav_bytes(0.1 * np.sin(np.arange(16000) / 3).astype(np.float32))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            code, body = call(self.url + "/api/station/jobs?wait=1", timeout=5)
            for job in (body.get("jobs") or []) if code == 200 else []:
                self.jobs.append(job)
                if not self.answer:
                    continue
                res = ({"ok": True, "label": "Voice: Kokoro af_heart", "audio": base64.b64encode(self.wav).decode()}
                       if job["type"] == "tts" else {"ok": True, "result": None})
                call(self.url + f"/api/station/jobs/{job['id']}", "POST", res)


def test_hub_dispatch_voice_comes_from_the_station_and_falls_back_to_text_without_it(tmp_path, monkeypatch):
    hub = HubRun(hub_cfg(tmp_path, cloud_offline_s=1.0))
    fake = FakeStation(hub.url)
    try:
        c = hub.app.cad
        inc = c.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
        assert wait(lambda: hub.app.hub.connected(), 5)
        body = {"unit_ids": [uid(c, "E63")]}
        code, p = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", body, token=None)
        assert code == 200 and p["audio_url"] and p["audio_note"] == "Voice: Kokoro af_heart"
        assert [j["type"] for j in fake.jobs] == ["tts"]
        job = fake.jobs[0]["args"]
        assert job["text"] == p["text"] and job["voice"]["engine"] == c.s.tts_engine and job["tones"]
        assert hub.get(p["audio_url"]) == fake.wav
        # The same dispatch again: from the hub's cache, no second render; and after a redeploy (disk gone) from SQLite.
        assert call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", body, token=None)[1]["audio_url"] == p["audio_url"]
        assert len(fake.jobs) == 1
        for f in c.audio_dir.glob("*.wav"):
            f.unlink()
        assert hub.get(p["audio_url"]) == fake.wav
        # The station's switches, from the hub's monitor.
        assert call(hub.url + "/api/mode", "POST", {"mode": "FG1", "sdr": "v4"}, token=None)[0] == 200
        assert fake.jobs[-1]["type"] == "call" and fake.jobs[-1]["args"] == {"fn": "set_mode", "args": ["FG1", "v4"]}
        # A station that takes the job but never answers: text only after the wait, said so.
        fake.answer = False
        monkeypatch.setattr(hub_mod, "TTS_WAIT_S", 1.0)
        t0 = time.time()
        p2 = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", dict(body, remarks="two cars"), token=None)[1]
        assert p2["audio_url"] is None and "didn't answer" in p2["audio_note"] and time.time() - t0 < 5
        # No station at all: at once, text only, "station offline"; switches refused with the reason.
        fake.stop.set()
        fake.thread.join(10)
        assert wait(lambda: not hub.app.hub.connected(), 5)
        t0 = time.time()
        p3 = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST", dict(body, remarks="three cars"), token=None)[1]
        assert p3["audio_url"] is None and time.time() - t0 < 1.0
        assert p3["audio_note"].startswith("Voice unavailable — station offline")
        code, err = call(hub.url + "/api/mode", "POST", {"mode": "FG1"}, token=None)
        assert code == 400 and "offline" in err["error"]
        assert wait(lambda: hub.get("/api/state")["radios"][0]["state"] == "down", 3)
        st = hub.get("/api/state")
        assert st["sdr"] is None and st["radios"][0]["word"].startswith("offline")
        assert hub.get("/api/cad/state")["cloud"]["state"] == "down"
    finally:
        fake.stop.set()
        hub.close()


def test_station_runs_the_hubs_jobs(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {})
    try:
        assert wait(lambda: hub.app.hub.connected(), 5)
        # set_mode on a station without switchable dongles: the station's own refusal comes back.
        assert "nothing to switch" in hub.app.set_mode("FG1")
        res = st.link._call({"fn": "stop", "args": []})
        assert res["ok"] is False and "not allowed" in res["error"]
    finally:
        st.link.stop()
        hub.close()


# -- the hub has no radios ----------------------------------------------------------------------------------


def test_hub_never_opens_an_sdr_or_starts_a_program(tmp_path, monkeypatch):
    import subprocess as sp
    from sizeup.radio import claims, sdr

    def boom(*a, **k):
        raise AssertionError("the hub tried to open a radio or start a program")
    monkeypatch.setattr(sp, "Popen", boom)
    monkeypatch.setattr(sdr, "devices", boom)
    monkeypatch.setattr(sdr, "resolve", boom)
    monkeypatch.setattr(claims, "claim", boom)
    cfg = replace(hub_cfg(tmp_path), rtl_fm="/nonexistent/rtl_fm", stt_server="/nonexistent/whisper-server",
                  p25_dir=str(tmp_path / "p25"), p25_recorder="/nonexistent/trunk-recorder", sdr_auto=True,
                  dispatch_tts_engine="kokoro", dispatch_tts_python="/nonexistent/python")
    app = make_app(cfg).start()
    try:
        st = app.state()
        assert [r["label"] for r in st["radios"]] == ["Station"] and st["sdr"] is None and st["talking"] == []
        assert app.receivers == [] and app.recorder is None and app.p25 is None and app.tts is None
        assert app.cad.state()["cloud"]["role"] == "hub"
    finally:
        app.stop()


def test_the_hub_config_starts_with_no_radio_programs_on_the_path(tmp_path):
    port = free_port()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SIZEUP_STATE_DIR": str(tmp_path / "data"),
           "SIZEUP_PORT": str(port), "SIZEUP_STATION_TOKEN": TOKEN, "SIZEUP_PUBLIC_URL": "https://sizeup.example.org"}
    proc = subprocess.Popen([sys.executable, "-m", "sizeup", "--config", str(ROOT / "deploy/hub/config.hub.toml")],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        url = f"http://127.0.0.1:{port}"
        assert wait(lambda: call(url + "/healthz", token=None)[0] == 200 if proc.poll() is None else True, 30), \
            proc.stdout.read().decode() if proc.poll() is not None else ""
        assert proc.poll() is None
        health = call(url + "/healthz", token=None)[1]
        assert health["ok"] and health["role"] == "hub" and health["public_url"] == "https://sizeup.example.org"
        code, page = call(url + "/cad", token=None)
        assert code == 200 and b"cloud-pill" in page
        assert sorted(p.name for p in (tmp_path / "data").glob("*.sqlite")) == ["audio.sqlite", "cad.sqlite", "hub.sqlite"]
        kids = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
        assert kids == []                                         # no whisper-server, no voice server, no rtl_fm
    finally:
        proc.terminate()
        proc.wait(10)


def test_init_state_creates_the_databases_and_exits(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SIZEUP_STATE_DIR": str(tmp_path / "data")}
    r = subprocess.run([sys.executable, "-m", "sizeup", "--config", str(ROOT / "deploy/hub/config.hub.toml"), "--init-state"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.name for p in (tmp_path / "data").glob("*.sqlite")) == ["audio.sqlite", "cad.sqlite", "hub.sqlite"]


# -- config, and all-in-one unchanged ----------------------------------------------------------------------------


def _toml(tmp_path, text: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(text)
    return p


def test_cloud_config(tmp_path, monkeypatch):
    for k in ("SIZEUP_STATION_TOKEN", "SIZEUP_STATE_DIR", "SIZEUP_HUB_URL", "SIZEUP_PORT", "SIZEUP_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError, match="needs a token"):
        load(_toml(tmp_path, '[cloud]\nrole = "station"\nhub_url = "https://hub.example.org"\n'))
    with pytest.raises(ConfigError, match="hub_url"):
        load(_toml(tmp_path, f'[cloud]\nrole = "station"\nhub_url = "http://hub.example.org"\ntoken = "{TOKEN}"\n'))
    with pytest.raises(ConfigError, match="role"):
        load(_toml(tmp_path, '[cloud]\nrole = "cloud"\n'))
    with pytest.raises(ConfigError, match="unknown key"):
        load(_toml(tmp_path, '[cloud]\nrole = "hub"\ntokn = "x"\n'))
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path / "data"))
    cfg = load(_toml(tmp_path, '[cloud]\nrole = "station"\nhub_url = "https://hub.example.org/"\ntoken = "not-this-one"\n'))
    assert (cfg.cloud_role, cfg.cloud_hub_url, cfg.cloud_token) == ("station", "https://hub.example.org", TOKEN)
    assert cfg.cloud_outbox == str((tmp_path / "data" / "state" / "outbox.sqlite").resolve())
    assert cfg.cad_db == str((tmp_path / "data" / "state" / "cad.sqlite").resolve())
    monkeypatch.setenv("SIZEUP_PORT", "9123")
    hub = load(ROOT / "deploy/hub/config.hub.toml")
    assert hub.cloud_role == "hub" and hub.port == 9123 and hub.cad_db == str((tmp_path / "data" / "cad.sqlite").resolve())
    assert hub.p25_dir == "" and hub.cloud_token == TOKEN


def test_all_in_one_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)        # set by mistake: no [cloud], no effect
    cfg = load(ROOT / "config.example.toml")
    assert (cfg.cloud_role, cfg.cloud_token, cfg.cloud_outbox) == ("", "", "")
    app = make_app(base(clips_dir=str(tmp_path / "clips")))
    assert type(app) is App and app.events is None and app.link is None
    assert "Hub" not in [r["label"] for r in app.radio_status()]
    assert app.cad.state()["cloud"] is None
    app._emit("traffic", {"id": "x"}, None)                     # a no-op
    server = make_server(app)
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.httpd.server_address[1]}"
        assert call(url + "/api/station/events", "POST", {})[0] == 404     # no ingest without the hub role
        assert call(url + "/healthz", token=None)[1]["role"] == "all-in-one"
    finally:
        server.shutdown()
