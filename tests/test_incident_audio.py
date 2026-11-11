"""#414 follow-up: an incident keeps its radio audio until an admin deletes it. Radio attachments never expire (no
expires_at, never 'expired', tagged kind=radio so the bucket's lifecycle leaves them; camera clips tagged kind=camera
still go after a year), and the live audio store never lets a linked clip go before its copy is stored. Synthetic
audio only; S3 is the fakes of tests/test_attachments.py and tests/test_audio_store.py."""

import json
import time

from sizeup import attachments as att_mod
from sizeup import audiostore, aws
from sizeup.app import make_app
from tests.test_attachments import FakeS3, radio_setup
from tests.test_audio_store import DAY, FakeBucket, cold, s3store, tone
from tests.test_cloud import hub_cfg
from tests.test_radios import tx


class Tagged(FakeS3):
    """FakeS3 that also keeps every PUT's x-amz-tagging."""

    def __init__(self):
        super().__init__()
        self.tags: dict[str, str] = {}

    def __call__(self, method, url, headers, body, timeout):
        from urllib.parse import unquote, urlsplit
        if method == "PUT":
            h = {k.lower(): v for k, v in headers.items()}
            self.tags[unquote(urlsplit(url).path[1:])] = h.get("x-amz-tagging", "")
        return super().__call__(method, url, headers, body, timeout)


def test_a_radio_copy_never_expires_and_is_tagged_radio(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    tagged = Tagged()
    a.s3 = att_mod.S3("sizeup-clips-test", region="us-east-1", send=tagged, creds=lambda: aws.Credentials("AKIDTEST", "s"))
    c.record_traffic(tx("FG1-2", clock.t, rig="E63", radio_id="6301"))
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 1
    row = a.store.one("SELECT id, expires_at, status FROM attachments WHERE kind = 'radio'")
    assert row["expires_at"] == att_mod.NEVER and row["status"] == "stored"
    assert set(tagged.tags.values()) == {"kind=radio"} and len(tagged.tags) == 3     # audio, transcript, manifest
    man = json.loads(next(v[0] for k, v in tagged.objects.items() if k.endswith("manifest.json")))
    assert man["expires_at"] is None
    clock.t += 10 * att_mod.RETENTION_S                       # ten years on: still stored, still plays
    a.sweep()
    assert a.store.one("SELECT status FROM attachments WHERE id = ?", (row["id"],))["status"] == "stored"


def test_radio_rows_from_before_never_expire_either(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    with c.store.lock:
        c.store.db.execute("INSERT INTO attachments (id, incident_id, kind, source, start, end, status, created_at, "
                           "expires_at) VALUES ('00000000000000cc', ?, 'radio', 'FG1', 0, 1, 'expired', 0, 5)", (inc["id"],))
    att_mod.Attachments(c.store, None, log=lambda m: None)    # the next start
    r = c.store.one("SELECT status, expires_at FROM attachments WHERE id = '00000000000000cc'")
    assert (r["status"], r["expires_at"]) == ("stored", 0)


def test_a_camera_clips_objects_are_tagged_camera(tmp_path):
    from tests.test_attachments import CAM, call, camera_setup
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    tagged = Tagged()
    a.s3 = att_mod.S3("sizeup-clips-test", region="us-east-1", send=tagged, creds=lambda: aws.Credentials("AKIDTEST", "s"))
    st = d.status(CAM)
    call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid})
    assert tagged.tags and set(tagged.tags.values()) == {"kind=camera"}
    r = a.store.one("SELECT expires_at, created_at FROM attachments WHERE kind = 'camera'")
    assert r["expires_at"] == r["created_at"] + att_mod.RETENTION_S                 # a camera clip: still a year


def test_the_live_store_never_lets_a_linked_clip_go_before_its_copy(tmp_path, monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setenv("SIZEUP_AUDIO_BUCKET", "sizeup-audio-test")
    monkeypatch.setattr(audiostore, "from_env", lambda log=print: s3store(bucket))
    app = make_app(hub_cfg(tmp_path, gis_layers=(), audio_keep_event_days=30))
    try:
        a = app.attachments
        clips = FakeS3()
        a.s3 = att_mod.S3("sizeup-clips-test", region="us-east-1", send=clips, creds=lambda: aws.Credentials("AKIDTEST", "s"))
        now = time.time()
        for tid in ("FG1-l", "FG1-m", "FG1-u"):
            app.apply("traffic", {"id": tid, "source": "FG1", "start": now - 60 * DAY, "end": now - 60 * DAY + 2,
                                  "text": "Engine 60 on scene", "status": "done"}, tone(0.5))
        app.audio.upload_pending()
        iid = app.cad.create({"type": "STRU", "address": "1 Main St", "operator": "x"})["id"]
        with app.cad.store.tx() as d:
            d.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES ('FG1-l', ?, 'manual', 'x')", (iid,))
            d.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES ('FG1-m', ?, 'mention', '')", (iid,))
        # 60 days old: past the clips' 12 h and the records' 30 days, but not copied yet: both linked ones are held
        assert app._audio_retention(now) == 1
        assert app.audio.has("FG1-l") and app.audio.has("FG1-m") and not app.audio.has("FG1-u")
        cold(app.audio)
        bucket.fail = lambda m, k: m == "GET"                 # the copy can't read the audio: it fails, they stay held
        assert a.sync_radio() == 0 and app._audio_retention(now) == 0 and app.audio.has("FG1-l")
        bucket.fail = None
        with a.store.lock:
            a.store.db.execute("UPDATE attachments SET retry_at = 0")
        cold(app.audio)
        assert a.sync_radio() == 2                            # every linked transmission is copied, a mention's too
        assert app._audio_retention(now) == 2                 # copied: the live clips follow the retention again
        stored = a.store.q("SELECT ref, expires_at FROM attachments WHERE kind = 'radio' AND status = 'stored'")
        assert {r["ref"] for r in stored} == {"FG1-l", "FG1-m"} and {r["expires_at"] for r in stored} == {0}
        assert sum(1 for k in clips.objects if k.endswith("/audio.wav")) == 2
    finally:
        app.stop()


def test_without_the_attachments_bucket_every_linked_clip_is_held(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_AUDIO_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("SIZEUP_ATTACHMENTS_BUCKET", raising=False)
    app = make_app(hub_cfg(tmp_path, gis_layers=(), audio_keep_event_days=30))
    try:
        assert not app.attachments.configured
        now = time.time()
        for tid in ("FG1-l", "FG1-u"):
            app.apply("traffic", {"id": tid, "source": "FG1", "start": now - 60 * DAY, "end": now - 60 * DAY + 2,
                                  "text": "Engine 60 on scene", "status": "done"}, tone(0.5))
        iid = app.cad.create({"type": "STRU", "address": "1 Main St", "operator": "x"})["id"]
        with app.cad.store.tx() as d:
            d.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES ('FG1-l', ?, 'manual', 'x')", (iid,))
        assert app._audio_retention(now) == 1 and app.audio.has("FG1-l") and not app.audio.has("FG1-u")
    finally:
        app.stop()
