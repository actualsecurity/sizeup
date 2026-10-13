"""Per-channel record / transcribe / engine (sizeup/channels.py, Settings > Radios > Channels): the
defaults, all-in-one with Amazon Transcribe, the hub sending its choices to the station (a job) and
the station obeying them (no whisper for "aws", nothing kept with record off), "both" keeping the
local text to compare, the fallbacks (AWS refused, the monthly limit), and who may change them.
Amazon Transcribe is always the local fake (tests/fake_transcribe.py)."""

import json
import time

import numpy as np
import pytest

from sizeup import channels
from sizeup.app import App
from sizeup.cad.api import CadApi, needs
from sizeup.cad.store import CadError
from sizeup.cloud.access import Identity
from sizeup.radio.fm import Channel, Segment
from sizeup.transcribe import WebSocket
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import PAGE_TEXT, FakeWhisper, tone, voice, wait
from tests.test_cloud import CHANNELS, HubRun, all_traffic, base, call, fm_segment, hub_cfg, station, station_cfg

AWS_TEXT = "Engine 63 on location per AWS"


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    real = WebSocket.connect

    def local_only(url, *a, **kw):
        assert "127.0.0.1" in url, f"a test tried to reach {url}"
        return real(url, *a, **kw)
    monkeypatch.setattr(WebSocket, "connect", local_only)


class CountingWhisper(FakeWhisper):
    def __init__(self, texts):
        super().__init__(texts)
        self.calls = 0

    def transcribe(self, audio, prompt=""):
        self.calls += 1
        return super().transcribe(audio, prompt)


def aws_cfg(fake, **kw):
    return dict(stt_aws=True, stt_aws_endpoint=fake.url, stt_aws_pace=50.0, **kw)


def comms_row(app, tid):
    return next((r for r in app.cad.comms.query(limit=500) if r["id"] == tid), None)


def traffic(app, tid):
    return next((t for t in list(app.store.loose) + [t for i in app.store.incidents for t in i.traffic] if t.id == tid), None)


def hub_post(hub, section, body):
    return call(hub.url + f"/api/cad/settings/{section}", "POST", body, token=None)


# -- the model --------------------------------------------------------------------------------------------


def test_defaults_and_what_runs_without_aws(tmp_path):
    app = App(base(clips_dir=str(tmp_path / "clips")))
    assert app.channel_pref("FG1") == {"record": True, "transcribe": True, "engine": "aws"}
    assert app.channel_pref("South 2")["engine"] == "aws" and app.channel_pref("tg:307")["engine"] == "aws"
    assert app.channel_pref("tg:301") == {"record": True, "transcribe": True, "engine": "local"}
    # no AWS here: an AWS channel runs the local model, and says why
    assert app.stt_engine("FG1") == "local" and "isn't set up here" in app.aws_ok()[1]
    st = app.state()["channels"]
    assert st["aws_ok"] is False and st["aws_channels"] == ["South 2", "FG1", "tg:307"]
    assert [f["key"] for f in st["fm"]] == ["South 2", "FG1"] and st["aws"]["enabled"] is False
    assert channels.effective({"transcribe": False, "engine": "aws"}, True) == "off"
    assert channels.effective({"transcribe": True, "engine": "both"}, False) == "local"
    assert channels.sig({"FG1": {"engine": "local"}}, True) != channels.sig({"FG1": {"engine": "local"}}, False)
    with pytest.raises(ValueError):
        channels.clean("FG1", {"engine": "google"})
    with pytest.raises(ValueError):
        channels.clean("tg:abc", {"engine": "aws"})


def test_settings_store_only_what_differs_audited_exported(tmp_path):
    app = App(base(clips_dir=str(tmp_path / "clips")))
    s = app.cad.settings
    s.post("channels", {"action": "set", "key": "tg:301", "engine": "both", "operator": "Corey"})
    s.post("channels", {"action": "set", "key": "FG1", "transcribe": False, "operator": "Corey"})
    s.post("channels", {"action": "set", "key": "FG1", "engine": "aws", "operator": "Corey"})     # the default: nothing new
    assert s.channel_overrides() == {"tg:301": {"engine": "both"}, "FG1": {"transcribe": False}}
    assert app.stt_engine("FG1") == "off" and app.channel_pref("tg:301")["engine"] == "both"
    audit = s.audit_log("radios")
    assert [(a["key"], a["new"]) for a in audit][:2] == [("channel/FG1", '{"transcribe":false}'), ("channel/tg:301", '{"engine":"both"}')]
    assert audit[0]["operator"] == "Corey"
    s.post("channels", {"action": "set_many", "keys": ["tg:301", "tg:302"], "engine": "local", "operator": "Corey"})
    assert s.channel_overrides() == {"FG1": {"transcribe": False}}
    with pytest.raises(CadError):
        s.post("channels", {"action": "set", "key": "FG1", "engine": "whisper"})
    # export / import carries them
    exp = s.export()
    assert exp["overrides"]["channels"] == {"FG1": {"transcribe": False}}
    s.post("channels", {"action": "reset", "key": "FG1"})
    assert app.stt_engine("FG1") == "local"
    s.post("import", {"action": "apply", "settings": exp})
    assert app.channel_pref("FG1")["transcribe"] is False
    bad = dict(exp, overrides={"channels": {"FG1": {"engine": "nope"}}})
    with pytest.raises(CadError):
        s.post("import", {"action": "dry_run", "settings": bad})
    # the vocabulary and the limit
    s.post("stt", {"action": "set", "key": "vocabulary", "value": ["Engine 60", "engine 60", "Rye Brook"]})
    assert s.vocabulary() == ["Engine 60", "Rye Brook"]
    s.post("stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 40})
    assert s.aws_limit() == 40.0
    with pytest.raises(CadError):
        s.post("stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": -1})
    page = s.stt_page()
    assert page["vocabulary"]["aws"] == ["Engine-sixty", "Rye-Brook"] and page["limit"]["value"] == 40.0
    assert "Engine 60" in page["vocabulary"]["suggestions"] and page["vocabulary"]["state"]["state"] == "none"
    # pushing without AWS here says where it runs
    out = s.post("stt", {"action": "push"})
    assert out["ok"] is False and "hub" in out["error"]


def test_a_talkgroups_record_is_its_own_switch(tmp_path):
    app = App(base(clips_dir=str(tmp_path / "clips"), p25_talkgroups={301: "60 Control", 307: "Fire/EMS 15"}))
    app.p25 = object()                                # set_talkgroup needs P25 configured
    app.cad.settings.post("channels", {"action": "set", "key": "tg:301", "record": False, "engine": "both"})
    assert app.tg.record(301) is False and app.channel_pref("tg:301") == {"record": False, "transcribe": True, "engine": "both"}
    assert "record" not in app.cad.settings.channel_overrides()["tg:301"]


# -- all-in-one with Amazon Transcribe -------------------------------------------------------------------------


def test_all_in_one_aws_both_off_and_record_off(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    app = App(base(clips_dir=str(tmp_path / "clips"), **aws_cfg(fake)))
    whisper = CountingWhisper({3: "whisper heard this", 2: "whisper two"})
    app.stt.client = whisper
    app.stt.start()
    try:
        assert app.stt_engine("FG1") == "aws"
        tid = fm_segment(app)
        t = traffic(app, tid)
        assert t is not None and t.status == "pending" and comms_row(app, tid)["status"] == "pending"   # shown at once
        assert wait(lambda: traffic(app, tid).text == AWS_TEXT + ".", 10)
        assert traffic(app, tid).model == "aws-transcribe" and whisper.calls == 0
        assert wait(lambda: comms_row(app, tid)["status"] == "done", 10)   # the stored row follows the live one
        row = comms_row(app, tid)
        assert (row["text"], row["model"], row["status"]) == (AWS_TEXT + ".", "aws-transcribe", "done")
        # both: whisper first, then AWS's text with whisper's kept to compare
        app.cad.settings.post("channels", {"action": "set", "key": "FG1", "engine": "both"})
        time.sleep(0.01)
        tid2 = fm_segment(app)
        assert wait(lambda: (traffic(app, tid2) is not None and traffic(app, tid2).text == AWS_TEXT + "."), 10)
        assert wait(lambda: comms_row(app, tid2)["alt_text"] == "whisper heard this", 10)
        row = comms_row(app, tid2)
        assert (row["alt_text"], row["alt_model"], row["model"]) == ("whisper heard this", "m.bin", "aws-transcribe")
        assert traffic(app, tid2).alt_text == "whisper heard this" and whisper.calls == 1
        # transcribe off: kept, no text, no model run
        app.cad.settings.post("channels", {"action": "set", "key": "FG1", "transcribe": False})
        time.sleep(0.01)
        tid3 = fm_segment(app)
        assert traffic(app, tid3).status == "off" and comms_row(app, tid3)["status"] == "off"
        assert app.has_clip(tid3) and whisper.calls == 1
        # record off: the receiver's side stays live ("talking now"), nothing is kept
        app.cad.settings.post("channels", {"action": "set", "key": "FG1", "record": False})
        time.sleep(0.01)
        seen = []
        real = app.live.fm_segment
        app.live.fm_segment = lambda *a: (seen.append(a[0]), real(*a))
        tid4 = fm_segment(app)
        assert traffic(app, tid4) is None and not app.has_clip(tid4) and comms_row(app, tid4) is None
        assert seen == ["FG1"]                             # talking now / MDC IDs still go to the live view
        assert whisper.calls == 1
        cost = app.state()["channels"]["aws"]
        assert cost["streams_open"] + cost["streams"] >= 1 and cost["billed_s"] >= 15 and cost["usd"] >= 0.01
    finally:
        app.stop()
        fake.close()


def test_a_page_on_aws_opens_the_incident_from_amazon_transcribes_text(tmp_path):
    fake = FakeTranscribe(phrases={500: PAGE_TEXT})
    app = App(base(clips_dir=str(tmp_path / "clips"), **aws_cfg(fake)))
    app.stt.client = CountingWhisper({})
    app.stt.start()
    try:
        audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(3200), voice(6.0)]).astype(np.float32)
        now = time.time()
        app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / 16000, audio, 25.0))
        assert wait(lambda: len(app.store.incidents) == 1, 12)
        inc = app.store.incidents[0]
        assert "Westchester" in (inc.location or "") or "209" in (inc.location or "")
        page = app.pages[0]
        assert page.status == "done" and page.model == "aws-transcribe" and app.stt.client.calls == 0
        # only the voice went to AWS (the tones were cut off), back to back in one stream
        assert len(fake.conns) == 1 and fake.conns[0].audio_s < 7.5
    finally:
        app.stop()
        fake.close()


def test_the_monthly_limit_sends_aws_channels_to_the_local_model(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    app = App(base(clips_dir=str(tmp_path / "clips"), **aws_cfg(fake)))
    app.stt.client = CountingWhisper({3: "whisper heard this"})
    app.stt.start()
    try:
        app.cad.settings.post("stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0})
        ok, why = app.aws_ok()
        assert not ok and "monthly AWS limit" in why and app.stt_engine("FG1") == "local"
        tid = fm_segment(app)
        assert wait(lambda: traffic(app, tid) is not None and traffic(app, tid).text == "whisper heard this")
        assert not fake.conns
        st = app.state()["channels"]
        assert st["aws_ok"] is False and "monthly AWS limit" in st["aws_why"]
    finally:
        app.stop()
        fake.close()


def test_aws_refused_falls_back_to_the_local_model_never_dropped(tmp_path):
    fake = FakeTranscribe(refuse=403)
    app = App(base(clips_dir=str(tmp_path / "clips"), **aws_cfg(fake)))
    app.stt.client = CountingWhisper({3: "whisper heard this"})
    app.stt.start()
    try:
        tid = fm_segment(app)
        assert wait(lambda: traffic(app, tid).text == "whisper heard this", 10)
        assert comms_row(app, tid)["text"] == "whisper heard this" and app.stt.client.calls == 1
    finally:
        app.stop()
        fake.close()


# -- the hub's choices on the station ------------------------------------------------------------------------


def test_the_hub_sends_its_choices_and_the_station_obeys(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        whisper = st.stt.client = CountingWhisper({3: "whisper heard this"})
        # the hub can take AWS audio: it says so in the set_channel_prefs job; the station reports it applied
        assert wait(lambda: st._hub_aws["ok"], 10)
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        assert st.stt_engine("FG1") == "aws"
        assert "hub" in st.channels_locked()                               # edits belong on the hub
        with pytest.raises(CadError):
            st.cad.settings.post("channels", {"action": "set", "key": "FG1", "engine": "local"})
        # aws: the station skips whisper, the hub transcribes the clip
        tid = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid and t["text"] == AWS_TEXT + "." for t in all_traffic(hub.get("/api/state"))), 15)
        assert whisper.calls == 0 and traffic(st, tid).status == "remote"
        row = next(r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid)
        assert (row["model"], row["status"]) == ("aws-transcribe", "done")
        # both: the station's whisper text first, then AWS's, whisper's kept to compare
        assert hub_post(hub, "channels", {"action": "set", "key": "FG1", "engine": "both"})[0] == 200
        assert wait(lambda: st.channel_pref("FG1")["engine"] == "both", 10)
        time.sleep(0.01)
        tid2 = fm_segment(st)
        assert wait(lambda: any(r["id"] == tid2 and r["text"] == AWS_TEXT + "." for r in hub.get("/api/cad/comms")["rows"]), 15)
        row = next(r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid2)
        assert (row["alt_text"], row["alt_model"]) == ("whisper heard this", "m.bin") and whisper.calls == 1
        # record off on the hub: the station keeps nothing (and forwards nothing)
        assert hub_post(hub, "channels", {"action": "set", "key": "FG1", "record": False})[0] == 200
        assert wait(lambda: st.channel_pref("FG1")["record"] is False, 10)
        time.sleep(0.01)
        tid3 = fm_segment(st)
        time.sleep(1.0)
        assert traffic(st, tid3) is None and not st.has_clip(tid3)
        assert not any(t["id"] == tid3 for t in all_traffic(hub.get("/api/state")))
        assert st.cad.settings.audit_log("radios")[0]["operator"] == "hub"
        # the monthly limit on the hub: the station is told, and runs whisper again
        assert hub_post(hub, "channels", {"action": "reset", "key": "FG1"})[0] == 200
        assert hub_post(hub, "stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0})[0] == 200
        assert wait(lambda: not st._hub_aws["ok"] and st.channel_pref("FG1")["record"], 10)
        assert "monthly AWS limit" in st._hub_aws["why"] and st.stt_engine("FG1") == "local"
        time.sleep(0.01)
        tid4 = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid4 and t["text"] == "whisper heard this" for t in all_traffic(hub.get("/api/state"))), 15)
        cost = hub.get("/api/state")["channels"]["aws"]
        assert cost["enabled"] and cost["limit_usd"] == 0 and cost["billed_s"] >= 15 and not cost["ok"]
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()


def test_a_station_that_was_offline_gets_the_choices_when_it_connects(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        assert hub_post(hub, "channels", {"action": "set", "key": "tg:333", "engine": "both"})[0] == 200
        sync = hub.get("/api/state")["channels"]["sync"]
        assert sync["state"] == "offline" and "apply when" in sync["detail"]
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: st.channel_pref("tg:333")["engine"] == "both", 10)
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        assert st.stt_engine("tg:333") == "local"            # the hub has no AWS: both runs the local model
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_aws_failing_on_the_hub_asks_the_stations_whisper(tmp_path):
    fake = FakeTranscribe(refuse=403)
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "whisper heard this"})
        assert wait(lambda: st._hub_aws["ok"], 10)
        tid = fm_segment(st)
        assert wait(lambda: any(t["id"] == tid and t["text"] == "whisper heard this" for t in all_traffic(hub.get("/api/state"))), 20)
        row = next(r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid)
        assert row["model"] == "m.bin" and row["status"] == "done"
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()


def test_aws_failing_with_the_station_gone_says_transcription_failed(tmp_path):
    fake = FakeTranscribe(refuse=403)
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        from sizeup.cad.dispatcher import wav_bytes
        ev = {"seq": 1, "at": time.time(), "type": "traffic",
              "data": {"id": "FG1-123", "source": "FG1", "start": time.time() - 3, "end": time.time(), "text": "",
                       "status": "remote", "transcript_pending": "aws", "stt": "aws", "model": "aws-transcribe"},
              "audio": __import__("base64").b64encode(wav_bytes((voice(3.0)[::2] * 32767).astype(np.int16), 8000)).decode()}
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})[0] == 200
        assert wait(lambda: any(r["id"] == "FG1-123" and r["status"] == "error" for r in hub.get("/api/cad/comms")["rows"]), 20)
        row = next(r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == "FG1-123")
        assert "failed" in row["model"]
    finally:
        hub.close()
        fake.close()


# -- who may change them ----------------------------------------------------------------------------------------


def test_only_an_admin_changes_channels_on_the_hub(tmp_path):
    from tests.test_cad import make_cad
    from tests.test_roles import COREY, JULIAN, people
    api = CadApi(make_cad(tmp_path))
    api.cad.log = lambda m: None
    people(api.cad)
    assert needs("/api/cad/settings/channels") == needs("/api/cad/settings/stt") == "admin"
    for path, body in (("/api/cad/settings/channels", {"action": "set", "key": "FG1", "engine": "local"}),
                       ("/api/cad/settings/stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 5}),
                       ("/api/cad/settings/stt", {"action": "push"})):
        code, raw, _ = api.post(path, json.dumps(body).encode(), Identity(email=JULIAN))
        assert code == 403 and json.loads(raw)["error"] == "Admins only (you: dispatcher)"
    assert api.cad.settings.channel_overrides() == {}
    code, raw, _ = api.post("/api/cad/settings/channels", json.dumps({"action": "set", "key": "FG1", "engine": "local"}).encode(),
                            Identity(email=COREY))
    assert code == 200 and api.cad.settings.channel_overrides() == {"FG1": {"engine": "local"}}
    acts = api.cad.activity.rows(COREY, "", None, 10)
    assert acts[0]["summary"] == "Channel FG1: engine local"
    code, raw, _ = api.get("/api/cad/stt", "", Identity(email=JULIAN))           # reading is for everyone
    assert code == 200 and "vocabulary" in json.loads(raw)


def test_named_tones_reach_the_station_with_the_channel_choices(tmp_path):
    """#80: a pair named on the hub goes to the station in the set_channel_prefs job (the signature covers it),
    so its live paging display names the pair next time; a name removed goes too."""
    from sizeup.radio.tones import ToneEvent
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        assert hub_post(hub, "tones", {"action": "set", "a_hz": 832.5, "b_hz": 1201.0, "name": "Fairview"})[0] == 200
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: st.cad.settings.tone_overrides().get("832.5/1201.0", {}).get("name") == "Fairview", 10)
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        assert st.plan.match(ToneEvent("two_tone", 832.5, 1201.0, 0, 4)).names == ("Fairview",)
        assert st.cad.settings.audit_log("radios")[0]["operator"] == "hub"
        assert hub_post(hub, "tones", {"action": "set", "a_hz": 669.9, "b_hz": 1092.4, "name": "Rye Brook FD"})[0] == 200
        assert wait(lambda: st.paging.is_ours(st.plan.match(ToneEvent("two_tone", 669.9, 1092.4, 0, 4)).names), 10)
        assert hub_post(hub, "tones", {"action": "delete", "key": "832.5/1201.0"})[0] == 200
        assert wait(lambda: st.plan.match(ToneEvent("two_tone", 832.5, 1201.0, 0, 4)).names == (), 10)
        assert "832.5/1201.0" not in st.cad.settings.tone_overrides()
        assert hub.get("/api/cad/tones")["named"][0]["name"] == "Rye Brook FD"
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
