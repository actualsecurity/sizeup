"""Re-transcribe one transmission (the ⋯ menu's Re-transcribe, POST /api/retranscribe {ids}): it runs the
way its channel says now (Settings > Radios > Engine: Amazon Transcribe, the local model, both), falls
back to the local model when AWS can't (said in the result), and always ends with a result on the
transmission (rerun_at, rerun_result: changed | same | failed: why), the same text included, on the
station and on the hub. user, 2026-10-08: "the retranscribes seem to be failing now when trying to
retranscribe a previous local, now aws transcription". Amazon Transcribe is the local fake."""

import time

from sizeup.app import App
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import wait
from tests.test_channels import AWS_TEXT, CountingWhisper, aws_cfg, comms_row, fake_aws, traffic  # noqa: F401 (fixture)
from tests.test_cloud import HubRun, base, call, fm_segment, hub_cfg, station, station_cfg


class BrokenWhisper:
    def transcribe(self, audio, prompt=""):
        raise RuntimeError("whisper-server is down")


def text(app, tid):
    t = traffic(app, tid)
    return t.text if t is not None else None


def rerun(app, tid, timeout=15):
    """Ask for one again; wait for its result. -> (queued, the comms row once it has a newer rerun_at)."""
    before = (comms_row(app, tid) or {}).get("rerun_at") or 0
    n = app.retranscribe([tid])
    assert wait(lambda: ((comms_row(app, tid) or {}).get("rerun_at") or 0) > before, timeout), comms_row(app, tid)
    return n, comms_row(app, tid)


def local_app(tmp_path, fake=None, texts=None):
    app = App(base(clips_dir=str(tmp_path / "clips"), **(aws_cfg(fake) if fake else {})))
    app.stt.client = CountingWhisper(texts or {3: "whisper heard this"})
    app.stt.start()
    return app


def test_an_aws_channel_runs_again_on_aws_and_the_same_text_still_ends(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    app = local_app(tmp_path, fake)
    try:
        tid = fm_segment(app)
        assert wait(lambda: text(app, tid) == AWS_TEXT + ".", 10)
        n, row = rerun(app, tid)
        assert n == 1 and row["rerun_result"] == "same" and row["text"] == AWS_TEXT + "."
        assert app.stt.client.calls == 0                                   # not the local model
        assert sum(c.audio_s > 0 for c in fake.conns) >= 1
        # the channel moved to the local model: the next re-run is whisper's, a new text
        app.cad.settings.post("channels", {"action": "set", "key": "FG1", "engine": "local"})
        n, row = rerun(app, tid)
        assert n == 1 and row["rerun_result"] == "changed" and row["text"] == "whisper heard this"
        assert app.stt.client.calls == 1 and traffic(app, tid).history[-1]["text"] == AWS_TEXT + "."
        # both: AWS's text, the local model's to compare
        app.cad.settings.post("channels", {"action": "set", "key": "FG1", "engine": "both"})
        n, row = rerun(app, tid)
        assert row["rerun_result"] == "changed" and row["text"] == AWS_TEXT + "."
        assert wait(lambda: comms_row(app, tid)["alt_text"] == "whisper heard this", 10)
        assert comms_row(app, tid)["alt_model"] == "m.bin" and traffic(app, tid).alt_text == "whisper heard this"
    finally:
        app.stop()
        fake.close()


def test_the_monthly_limit_reruns_on_the_local_model_and_says_so(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    app = local_app(tmp_path, fake)
    try:
        tid = fm_segment(app)
        assert wait(lambda: text(app, tid) == AWS_TEXT + ".", 10)
        conns = len(fake.conns)
        app.cad.settings.post("stt", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0})
        n, row = rerun(app, tid)
        assert n == 1 and row["text"] == "whisper heard this"
        assert row["rerun_result"].startswith("changed; AWS unavailable (") and "monthly AWS limit" in row["rerun_result"]
        assert "the local model ran" in row["rerun_result"] and len(fake.conns) == conns
    finally:
        app.stop()
        fake.close()


def test_a_failed_rerun_ends_too(tmp_path):
    app = local_app(tmp_path)                                           # no AWS: FG1 runs the local model
    try:
        tid = fm_segment(app)
        assert wait(lambda: text(app, tid) == "whisper heard this", 10)
        n, row = rerun(app, tid)
        assert n == 1 and row["rerun_result"] == "same"                   # the same text: still an answer
        app.stt.client = BrokenWhisper()
        n, row = rerun(app, tid, timeout=25)                               # (three tries, 15 s)
        assert row["rerun_result"] == "failed: speech-to-text error" and row["text"] == "whisper heard this"
        assert app.retranscribe(["no-such-id"]) == 0                     # nothing to re-run: said at once (queued 0)
    finally:
        app.stop()


def hub_row(hub, tid):
    return next((r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid), None)


def hub_rerun(hub, tid, timeout=20):
    before = (hub_row(hub, tid) or {}).get("rerun_at") or 0
    code, r = call(hub.url + "/api/retranscribe", "POST", {"ids": [tid]}, token=None)
    assert code == 200, r
    assert wait(lambda: ((hub_row(hub, tid) or {}).get("rerun_at") or 0) > before, timeout), hub_row(hub, tid)
    return r["queued"], hub_row(hub, tid)


def test_on_the_hub_an_aws_channel_reruns_on_the_hub_a_local_one_on_the_station(tmp_path):
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        whisper = st.stt.client = CountingWhisper({3: "whisper heard this"})
        assert wait(lambda: st._hub_aws["ok"], 10)
        tid = fm_segment(st)
        assert wait(lambda: (hub_row(hub, tid) or {}).get("text") == AWS_TEXT + ".", 15)
        # aws: Amazon Transcribe on the hub's clip, the station's whisper not asked; the same text still ends
        n, row = hub_rerun(hub, tid)
        assert n == 1 and row["rerun_result"] == "same" and whisper.calls == 0
        # local: the station's whisper; its answer (here a new text) reaches the hub with the result
        assert call(hub.url + "/api/cad/settings/channels", "POST", {"action": "set", "key": "FG1", "engine": "local"},
                    token=None)[0] == 200
        n, row = hub_rerun(hub, tid)
        assert n == 1 and row["rerun_result"] == "changed" and row["text"] == "whisper heard this" and whisper.calls == 1
        # again, the same text: the hub still sees it end (the station sends the result either way)
        n, row = hub_rerun(hub, tid)
        assert row["rerun_result"] == "same" and whisper.calls == 2
        # the monthly limit on the hub: an AWS channel goes to the station's whisper, said so
        assert call(hub.url + "/api/cad/settings/channels", "POST", {"action": "reset", "key": "FG1"}, token=None)[0] == 200
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_monthly_limit_usd", "value": 0},
                    token=None)[0] == 200
        n, row = hub_rerun(hub, tid)
        assert row["rerun_result"].startswith("same; AWS unavailable (") and "monthly AWS limit" in row["rerun_result"]
        assert whisper.calls == 3
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
        fake.close()


def test_on_the_hub_aws_failing_with_the_station_gone_ends_failed(tmp_path):
    fake = FakeTranscribe(refuse=403)
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        from sizeup.cad.dispatcher import wav_bytes
        import base64
        import numpy as np
        from tests.test_app import voice
        ev = {"seq": 1, "at": time.time(), "type": "traffic",
              "data": {"id": "FG1-77", "source": "FG1", "start": time.time() - 3, "end": time.time(), "text": "words",
                       "status": "done", "model": "m.bin", "raw": "words"},
              "audio": base64.b64encode(wav_bytes((voice(3.0)[::2] * 32767).astype(np.int16), 8000)).decode()}
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})[0] == 200
        assert wait(lambda: hub_row(hub, "FG1-77") is not None, 10)
        hub.app.hub.last_seen = time.time() - 3600                          # the station went away
        n, row = hub_rerun(hub, "FG1-77")
        assert n == 1 and row["rerun_result"].startswith("failed: speech-to-text runs on the station")
        assert "; AWS failed (" in row["rerun_result"] and "model ran" not in row["rerun_result"] and row["text"] == "words"
        # a local channel with no station: refused at once (the page says why)
        assert call(hub.url + "/api/cad/settings/channels", "POST", {"action": "set", "key": "FG1", "engine": "local"},
                    token=None)[0] == 200
        code, r = call(hub.url + "/api/retranscribe", "POST", {"ids": ["FG1-77"]}, token=None)
        assert code == 400 and "runs on the station" in r["error"]
    finally:
        hub.close()
        fake.close()
