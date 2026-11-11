"""#440: an incident keeps the radio audio attached to it (a confirmed link) until an admin deletes it: no expires_at,
never 'expired', its objects tagged keep=forever, which the bucket's lifecycle leaves alone; an unconfirmed link (a
mention) keeps the year it had (keep=1y), and becomes forever once confirmed; camera clips keep=1y. The live audio
store never lets a linked clip go before its copy is stored, unless it will never be copied. Synthetic audio only;
S3 is the fakes of tests/test_attachments.py and tests/test_audio_store.py."""

import json
import time
from urllib.parse import unquote, urlsplit

from sizeup import attachments as att_mod
from sizeup import audiostore, aws
from sizeup.app import make_app
from tests.test_attachments import FakeS3, radio_setup
from tests.test_audio_store import DAY, FakeBucket, Lease, cold, s3store, tone
from tests.test_cloud import hub_cfg
from tests.test_radios import tx


class Tagged(FakeS3):
    """FakeS3 that keeps every object's keep tag (x-amz-tagging on a PUT, PutObjectTagging after) and can refuse
    tagging (403, as S3 does without s3:PutObjectTagging)."""

    def __init__(self):
        super().__init__()
        self.tags: dict[str, str] = {}
        self.deny_tagging = False

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        key = unquote(u.path[1:])
        h = {k.lower(): v for k, v in headers.items()}
        if method == "PUT" and u.query.startswith("tagging"):
            if self.deny_tagging:
                return 403, {}, b"<Error><Code>AccessDenied</Code></Error>"
            assert key in self.objects
            self.tags[key] = "keep=" + body.decode().split("<Value>")[1].split("</Value>")[0]
            return 200, {}, b""
        if method == "PUT":
            if "x-amz-tagging" in h and self.deny_tagging:
                return 403, {}, b"<Error><Code>AccessDenied</Code></Error>"
            self.tags[key] = h.get("x-amz-tagging", "")
        return super().__call__(method, url, headers, body, timeout)


def tagged_s3(fake):
    return att_mod.S3("sizeup-clips-test", region="us-east-1", send=fake, creds=lambda: aws.Credentials("AKIDTEST", "s"))


def link(c, tid, iid, why="manual"):
    with c.store.tx() as d:
        d.execute("INSERT OR REPLACE INTO radio_links (traffic_id, incident_id, why, unit) VALUES (?, ?, ?, 'x')",
                  (tid, iid, why))


def test_a_confirmed_links_copy_never_expires_an_unconfirmed_ones_keeps_a_year(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    fake = Tagged()
    a.s3 = tagged_s3(fake)
    c.record_traffic(tx("FG1-2", clock.t))
    c.record_traffic(tx("FG1-9", clock.t))
    link(c, "FG1-2", inc["id"])                                # by hand: confirmed
    link(c, "FG1-9", inc["id"], "mention")                     # a mention nobody confirmed
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 2
    rows = {r["ref"]: r for r in a.store.q("SELECT ref, id, expires_at, status, tagged FROM attachments WHERE kind = 'radio'")}
    assert rows["FG1-2"]["expires_at"] == att_mod.NEVER and rows["FG1-9"]["expires_at"] > clock.t
    assert {r["tagged"] for r in rows.values()} == {1}
    by = {ref: {v for k, v in fake.tags.items() if f"/{rows[ref]['id']}/" in k} for ref in rows}
    assert by == {"FG1-2": {"keep=forever"}, "FG1-9": {"keep=1y"}}       # audio, transcript and manifest each
    man = json.loads(next(v[0] for k, v in fake.objects.items() if k.endswith("manifest.json") and rows["FG1-2"]["id"] in k))
    assert man["expires_at"] is None
    # the mention is confirmed later (kept by a person): its copy becomes forever, objects retagged
    with c.store.tx() as d:
        d.execute("INSERT INTO radio_link_kept (traffic_id, incident_id, at) VALUES ('FG1-9', ?, 0)", (inc["id"],))
    assert a.retag() == 1
    assert a.store.one("SELECT expires_at FROM attachments WHERE ref = 'FG1-9'")["expires_at"] == att_mod.NEVER
    assert {v for k, v in fake.tags.items() if f"/{rows['FG1-9']['id']}/" in k} == {"keep=forever"}
    clock.t += 10 * att_mod.RETENTION_S                       # ten years on: both stored, both play
    a.sweep()
    assert {r["status"] for r in a.store.q("SELECT status FROM attachments WHERE kind = 'radio'")} == {"stored"}


def test_an_unconfirmed_copy_expires_after_its_year(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    a.s3 = tagged_s3(Tagged())
    c.record_traffic(tx("FG1-9", clock.t))
    link(c, "FG1-9", inc["id"], "mention")
    clock.t += att_mod.SETTLE_S + 30
    a.sync_radio()
    clock.t += att_mod.RETENTION_S + 1
    a.sweep()
    assert a.store.one("SELECT status FROM attachments WHERE ref = 'FG1-9'")["status"] == "expired"


def test_tagging_refused_loses_nothing_and_is_done_later(tmp_path):
    """Merged before sizeup-aws step 19 ran (no s3:PutObjectTagging): saved untagged, tagged once allowed."""
    c, a, inc, clock = radio_setup(tmp_path)
    fake = Tagged()
    fake.deny_tagging = True
    a.s3 = tagged_s3(fake)
    c.record_traffic(tx("FG1-2", clock.t))
    link(c, "FG1-2", inc["id"])
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 1
    r = a.store.one("SELECT id, status, tagged FROM attachments WHERE ref = 'FG1-2'")
    assert r["status"] == "stored" and r["tagged"] == 0 and set(fake.tags.values()) == {""}
    assert a.retag() == 0                                      # still refused: tried again later
    fake.deny_tagging = False
    assert a.retag() == 1 and set(fake.tags.values()) == {"keep=forever"}
    assert a.store.one("SELECT tagged FROM attachments WHERE id = ?", (r["id"],))["tagged"] == 1


def test_a_camera_clip_is_tagged_a_year_even_when_tagging_is_refused_first(tmp_path):
    from tests.test_attachments import CAM, call, camera_setup
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    fake = Tagged()
    fake.deny_tagging = True
    a.s3 = tagged_s3(fake)
    st = d.status(CAM)
    call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid})
    r = a.store.one("SELECT status, tagged, expires_at, created_at FROM attachments WHERE kind = 'camera'")
    assert r["status"] == "stored" and r["tagged"] == 0                     # saved, not failed
    assert r["expires_at"] == r["created_at"] + att_mod.RETENTION_S
    fake.deny_tagging = False
    assert a.retag() == 1 and fake.tags and set(fake.tags.values()) == {"keep=1y"}


def test_radio_rows_from_before_on_a_confirmed_link_never_expire(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("FG1-2", clock.t))
    c.record_traffic(tx("FG1-9", clock.t))
    link(c, "FG1-2", inc["id"])
    link(c, "FG1-9", inc["id"], "mention")
    with c.store.lock:
        for aid, ref in (("00000000000000cc", "FG1-2"), ("00000000000000dd", "FG1-9")):
            c.store.db.execute("INSERT INTO attachments (id, incident_id, kind, source, ref, start, end, status, created_at, "
                               "expires_at) VALUES (?, ?, 'radio', 'FG1', ?, 0, 1, 'expired', 0, 5)", (aid, inc["id"], ref))
    att_mod.Attachments(c.store, None, log=lambda m: None)    # the next start
    rows = {r["ref"]: (r["status"], r["expires_at"]) for r in c.store.q("SELECT ref, status, expires_at FROM attachments")}
    assert rows == {"FG1-2": ("stored", 0), "FG1-9": ("expired", 5)}


def hub_with(tmp_path, monkeypatch, bucket, **kw):
    monkeypatch.setenv("SIZEUP_AUDIO_BUCKET", "sizeup-audio-test")
    monkeypatch.setattr(audiostore, "from_env", lambda log=print: s3store(bucket))
    app = make_app(hub_cfg(tmp_path, gis_layers=(), audio_keep_event_days=30, **kw))
    app.fence = Lease()
    return app


def test_the_live_store_never_lets_a_linked_clip_go_before_its_copy(tmp_path, monkeypatch):
    bucket = FakeBucket()
    app = hub_with(tmp_path, monkeypatch, bucket)
    try:
        a = app.attachments
        a.s3 = tagged_s3(FakeS3())
        now = time.time()
        for tid in ("FG1-l", "FG1-m", "FG1-u"):
            app.apply("traffic", {"id": tid, "source": "FG1", "start": now - 60 * DAY, "end": now - 60 * DAY + 2,
                                  "text": "Engine 60 on scene", "status": "done"}, tone(0.5))
        app.audio.upload_pending()
        iid = app.cad.create({"type": "STRU", "address": "1 Main St", "operator": "x"})["id"]
        link(app.cad, "FG1-l", iid)
        link(app.cad, "FG1-m", iid, "mention")
        # 60 days old: past the clips' 12 h and the records' 30 days, but not copied yet: both linked ones are held
        assert app._audio_retention(now) == 1
        assert app.audio.has("FG1-l") and app.audio.has("FG1-m") and not app.audio.has("FG1-u")
        cold(app.audio)
        bucket.fail = lambda m, k: m == "GET"                 # the copy can't read the audio: it fails, they stay held
        assert a.sync_radio() == 0 and app._audio_retention(now) == 0 and app.audio.has("FG1-l")
        bucket.fail = None
        with a.store.lock:
            a.store.db.execute("UPDATE attachments SET retry_at = 0")
        assert a.sync_radio() == 2                            # every linked transmission is copied
        assert app._audio_retention(now) == 2                 # copied: the live clips follow the retention again
        stored = {r["ref"]: r["expires_at"] for r in a.store.q("SELECT ref, expires_at FROM attachments WHERE "
                                                               "kind = 'radio' AND status = 'stored'")}
        assert stored["FG1-l"] == 0 and stored["FG1-m"] > now  # forever / a year
    finally:
        app.stop()


def test_a_linked_clip_that_will_never_be_copied_is_released(tmp_path, monkeypatch):
    bucket = FakeBucket()
    app = hub_with(tmp_path, monkeypatch, bucket)
    try:
        a = app.attachments
        a.s3 = tagged_s3(FakeS3())
        now = time.time()
        for tid, days in (("FG1-r", 60), ("FG1-old", 380)):
            app.apply("traffic", {"id": tid, "source": "FG1", "start": now - days * DAY, "end": now - days * DAY + 2,
                                  "text": "Engine 60 on scene", "status": "done"}, tone(0.5))
        app.audio.upload_pending(limit=10)
        iid = app.cad.create({"type": "STRU", "address": "1 Main St", "operator": "x"})["id"]
        link(app.cad, "FG1-r", iid)
        link(app.cad, "FG1-old", iid)                          # linked a year after it ended: never copied
        with a.store.lock:                                     # FG1-r: refused for good (TRIES)
            a.store.db.execute("INSERT INTO attachments (id, incident_id, kind, source, ref, start, end, status, tries, "
                               "created_at, expires_at) VALUES ('00000000000000ee', ?, 'radio', 'FG1', 'FG1-r', 0, 1, "
                               "'failed', ?, 0, 0)", (iid, att_mod.TRIES))
        logs = []
        app.hub.log = logs.append
        assert app.audio.prune_clips(now - 0.5 * DAY, now=now, held=app._audio_held, policy=app.audio_keep()) == 2
        assert any("will not be copied" in m for m in logs)
    finally:
        app.stop()


def test_without_the_attachments_bucket_every_linked_clip_is_held(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_AUDIO_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("SIZEUP_ATTACHMENTS_BUCKET", raising=False)
    app = make_app(hub_cfg(tmp_path, gis_layers=(), audio_keep_event_days=30))
    app.fence = Lease()
    try:
        assert not app.attachments.configured
        now = time.time()
        for tid in ("FG1-l", "FG1-u"):
            app.apply("traffic", {"id": tid, "source": "FG1", "start": now - 60 * DAY, "end": now - 60 * DAY + 2,
                                  "text": "Engine 60 on scene", "status": "done"}, tone(0.5))
        iid = app.cad.create({"type": "STRU", "address": "1 Main St", "operator": "x"})["id"]
        link(app.cad, "FG1-l", iid)
        assert app._audio_retention(now) == 1 and app.audio.has("FG1-l") and not app.audio.has("FG1-u")
    finally:
        app.stop()
