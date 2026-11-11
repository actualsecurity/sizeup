"""Incident attachments (#323, sizeup/attachments.py): camera clips saved from the DVR and the transmissions linked to an
event, kept the same way in a private S3 bucket for a year; every attach, view, download, export and delete audited.

No network, no AWS: a fake S3 (FakeS3) that checks what the real one would (SigV4 headers, SSE, Content-MD5 and the
SHA-256 checksum, byte ranges). Camera segments are the synthetic MPEG-TS of tests/test_hlsseg.py, recorded by the real
DVR from the Thruway fixtures' moving chunklist; transmissions are the CAD's own (tests/test_radios.py).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from sizeup import attachments as att_mod
from sizeup import aws
from sizeup.attachments import Attachments, S3, Stream
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_dvr import CAM, setup, step
from tests.test_radios import Clock as RClock
from tests.test_radios import _event_with, cad, tx
from tests.test_roles import COREY, JULIAN, VAL, call, people


class FakeS3:
    """One versioned bucket under Object Lock (governance): PUT (SSE-S3 and both checksums required) makes a version;
    GET (a byte range) reads the current one; DELETE of a version needs x-amz-bypass-governance-retention (else
    AccessDenied, as S3 answers a locked version); a plain DELETE would only leave a delete marker."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}           # the current versions
        self.versions: dict[str, dict[str, bytes]] = {}           # key -> {version id: bytes}: every one kept
        self.calls: list[tuple[str, str]] = []
        self.bypassed: list[tuple[str, str]] = []
        self.fail = None
        self.n = 0

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        key = unquote(u.path[1:])
        q = dict(x.split("=", 1) for x in u.query.split("&") if x)
        self.calls.append((method, key))
        h = {k.lower(): v for k, v in headers.items()}
        assert h["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") and "x-amz-date" in h
        if self.fail is not None and self.fail(method, key):
            return 500, {}, b"<Error><Code>InternalError</Code></Error>"
        if method == "PUT":
            assert h["x-amz-server-side-encryption"] == "AES256"
            assert base64.b64decode(h["content-md5"]) == hashlib.md5(body).digest()
            assert base64.b64decode(h["x-amz-checksum-sha256"]) == hashlib.sha256(body).digest()
            assert h["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
            self.objects[key] = (bytes(body), h["content-type"])
            self.n += 1
            vid = f"v{self.n}"
            self.versions.setdefault(key, {})[vid] = bytes(body)
            return 200, {"ETag": '"e"', "x-amz-version-id": vid}, b""
        if method == "GET" and "versions" in q:
            pre = unquote(q.get("prefix", ""))
            rows = "".join(f"<Version><Key>{k}</Key><VersionId>{v}</VersionId></Version>"
                           for k in sorted(self.versions) if k.startswith(pre) for v in self.versions[k])
            return 200, {}, f"<ListVersionsResult><IsTruncated>false</IsTruncated>{rows}</ListVersionsResult>".encode()
        if method == "GET":
            if key not in self.objects:
                return 404, {}, b"<Error><Code>NoSuchKey</Code></Error>"
            b = self.objects[key][0]
            if "range" in h:
                first, last = (int(x) for x in h["range"].removeprefix("bytes=").split("-"))
                return 206, {}, b[first:last + 1]
            return 200, {}, b
        if method == "DELETE":
            vid = unquote(q.get("versionId", ""))
            assert vid, "a plain DELETE: only a delete marker under Object Lock (sizeup never sends one)"
            if h.get("x-amz-bypass-governance-retention") != "true":
                return 403, {}, b"<Error><Code>AccessDenied</Code></Error>"
            self.bypassed.append((key, vid))
            self.versions.get(key, {}).pop(vid, None)
            if not self.versions.get(key):
                self.versions.pop(key, None)
                self.objects.pop(key, None)
            return 204, {}, b""
        raise AssertionError(method)


class Now:
    """The upload pool, run at once (the tests see the result)."""

    def submit(self, fn, *a):
        fn(*a)

    def shutdown(self, **kw):
        pass


def store_for(store, clock=None, s3=True):
    fake = FakeS3()
    s = S3("sizeup-clips-test", region="us-east-1", send=fake, creds=lambda: aws.Credentials("AKIDTEST", "secret")) if s3 else None
    logs = []
    a = Attachments(store, s, log=logs.append, clock=clock)
    a._pool = Now()
    a.fake, a.logs = fake, logs
    return a


def camera_setup(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    step(d, f, clock, 8)                                                   # ~ 10 segments of 4 s
    c = make_cad(tmp_path)
    c.log = lambda m: None
    api = CadApi(c)
    people(c)
    iid = c.create({"type": "MVA", "address": "Boston Post Rd"})["id"]
    a = store_for(c.store, clock)
    a.dvr, a.camera = (lambda: d), r.camera
    c.attachments = a
    return api, c, a, d, iid, clock


def get(api, path, email=None, query=""):
    return api.get(path, query, Identity(email=email) if email else None)


def body_of(resp):
    b = resp[1]
    return b.read_all() if isinstance(b, Stream) else b


def test_a_dispatcher_saves_a_clip_and_it_is_stored_as_recorded(tmp_path):
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    t0, t1 = st["from"] + 6, st["to"] - 6
    path = f"/api/cad/cameras/{CAM}/dvr/clip"
    assert needs(path) == "dispatch"
    assert call(api, path, {"from": t0, "to": t1, "incident_id": iid}, VAL)[0] == 403        # a viewer: no
    code, out = call(api, path, {"from": t0, "to": t1, "incident_id": iid, "note": "the approach"}, JULIAN)
    assert code == 200 and out["ok"]
    row = out["attachment"]
    assert row["kind"] == "camera" and row["source"] == CAM and row["status"] == "saving"     # (answered before the upload)
    stored = a.public(a.row(row["id"]))
    assert stored["status"] == "stored" and stored["note"] == "the approach" and "Julian" in stored["created_by"]
    # the segments that overlap the range, concatenated as they are: no re-encode
    segs = d.segments(CAM, t0, t1)
    want = b"".join(Path(s["path"]).read_bytes() for s in segs)
    names = [o["name"] for o in stored["objects"]]
    assert names == ["part-0000.ts", "manifest.json"]
    keys = sorted(a.fake.objects)
    assert all(k.startswith(f"attachments/{iid}/{row['id']}/") for k in keys)
    part = a.fake.objects[f"attachments/{iid}/{row['id']}/part-0000.ts"]
    assert part == (want, "video/MP2T") and stored["bytes"] == len(want)
    assert stored["objects"][0]["sha256"] == hashlib.sha256(want).hexdigest()
    man = json.loads(a.fake.objects[f"attachments/{iid}/{row['id']}/manifest.json"][0])
    assert man["kind"] == "camera" and man["source"]["camera"] == CAM and man["incident"]["id"] == iid
    assert man["note"] == "the approach" and len(man["segments"]) == len(segs) and "Julian" in man["created_by"]
    assert man["objects"][0]["sha256"] == hashlib.sha256(want).hexdigest()
    assert hashlib.sha256(a.fake.objects[f"attachments/{iid}/{row['id']}/manifest.json"][0]).hexdigest() == \
        stored["objects"][1]["sha256"]
    # audited, and in the person's activity
    assert [x["action"] for x in a.audit_rows(iid=iid)] == ["attach"]
    assert any(x["summary"].startswith(f"Saved a clip of camera {CAM} to ") for x in c.activity.rows(JULIAN, "", None, 20))
    # on the incident
    code, raw, _ = get(api, f"/api/cad/incident/{iid}/attachments", VAL)
    lst = json.loads(raw)
    assert code == 200 and lst["configured"] and [x["id"] for x in lst["attachments"]] == [row["id"]]
    assert json.loads(get(api, f"/api/cad/incident/{iid}", VAL)[1])["attachments"][0]["id"] == row["id"]


def test_the_range_is_checked(tmp_path):
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    path = f"/api/cad/cameras/{CAM}/dvr/clip"
    for body, why in (({"from": st["from"], "to": st["from"] + 3601, "incident_id": iid}, "at most 60 min"),
                      ({"from": st["to"], "to": st["from"], "incident_id": iid}, "after from"),
                      ({"from": st["from"] - 60, "to": st["to"], "incident_id": iid}, "outside what is recorded"),
                      ({"from": st["from"], "to": st["to"] + 60, "incident_id": iid}, "outside what is recorded"),
                      ({"from": "x", "to": st["to"], "incident_id": iid}, "unix seconds"),
                      ({"from": st["from"], "to": st["to"], "incident_id": 9999}, "no event"),
                      ({"from": st["from"], "to": st["to"]}, "incident_id")):
        code, out = call(api, path, body, COREY)
        assert code == 400 and why in out["error"], (body, out)
    code, out = call(api, "/api/cad/cameras/nope/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid}, COREY)
    assert code == 400 and "nothing recorded" in out["error"]
    assert a.fake.objects == {} and a.audit_rows() == []


def test_playback_through_the_hub_and_the_download_as_one_ts(tmp_path, monkeypatch):
    monkeypatch.setattr(att_mod, "PART_BYTES", 6000)                     # several parts, a segment never split
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    out = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid}, JULIAN)[1]
    aid = out["attachment"]["id"]
    row = a.public(a.row(aid))
    segs = d.segments(CAM, st["from"], st["to"])
    assert len([o for o in row["objects"] if o["name"].endswith(".ts")]) > 1 and row["segments"] == len(segs)
    code, pl, ctype = get(api, f"/api/cad/attachments/{aid}/index.m3u8", VAL)[:3]
    assert code == 200 and ctype == "application/vnd.apple.mpegurl"
    text = pl.decode()
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and "#EXT-X-ENDLIST" in text and text.count("#EXT-X-PROGRAM-DATE-TIME") == len(segs)
    for n, s in enumerate(segs):                                           # every segment, byte for byte, from its part
        code, b, ctype, _ = get(api, f"/api/cad/attachments/{aid}/{n}.ts", VAL)
        assert code == 200 and ctype == "video/MP2T" and b == Path(s["path"]).read_bytes()
    assert get(api, f"/api/cad/attachments/{aid}/{len(segs)}.ts", VAL)[0] == 404
    get(api, f"/api/cad/attachments/{aid}/index.m3u8", VAL)                # again: one view audited per person a while
    resp = get(api, f"/api/cad/attachments/{aid}/download", VAL)
    assert resp[0] == 200 and resp[2] == "video/MP2T" and "attachment; filename=" in resp[3]["Content-Disposition"]
    assert len(resp[1]) == row["bytes"] and body_of(resp) == b"".join(Path(s["path"]).read_bytes() for s in segs)
    assert [x["action"] for x in a.audit_rows(aid=aid)] == ["download", "view", "attach"]
    assert any(x["summary"] == f"Downloaded the camera attachment {aid}" for x in c.activity.rows(VAL, "", None, 20))
    assert get(api, "/api/cad/attachments/0123456789abcdef/index.m3u8", VAL)[0] == 404
    assert get(api, "/api/cad/attachments/../x", VAL)[0] == 404


def test_only_an_admin_deletes_and_the_record_stays(tmp_path):
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    aid = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid},
               JULIAN)[1]["attachment"]["id"]
    objs = a.public(a.row(aid))["objects"]
    path = f"/api/cad/attachments/{aid}/delete"
    assert needs(path) == "admin"
    assert call(api, path, {}, JULIAN)[0] == 403 and call(api, path, {}, VAL)[0] == 403
    assert [m for m, _ in a.fake.calls if m == "DELETE"] == []                   # nobody else deletes anything
    code, out = call(api, path, {}, COREY)
    assert code == 200 and out["attachment"]["status"] == "deleted" and "Corey" in out["attachment"]["deleted_by"]
    # really gone: every version, each by its id past the governance lock
    assert a.fake.objects == {} and a.fake.versions == {} and len(a.fake.bypassed) == len(objs)
    audit = a.audit_rows(aid=aid)
    assert audit[0]["action"] == "delete" and all(o["sha256"] in audit[0]["detail"] for o in objs)
    assert any(x["summary"].startswith(f"Deleted the camera attachment {aid}") for x in c.activity.rows(COREY, "", None, 20))
    assert a.list(iid) == [] and [x["id"] for x in a.list(iid, deleted=True)] == [aid]
    assert get(api, f"/api/cad/attachments/{aid}/index.m3u8", VAL)[0] == 404
    assert call(api, path, {}, COREY)[0] == 400                            # deleted already


def test_not_configured_says_so(tmp_path):
    r, d, f, clock = setup(tmp_path)
    c = make_cad(tmp_path)
    api = CadApi(c)
    path = f"/api/cad/cameras/{CAM}/dvr/clip"
    code, out = call(api, path, {"from": 1, "to": 2, "incident_id": 1})
    assert code == 400 and "not configured" in out["error"]                # no attachments at all
    assert json.loads(get(api, "/api/cad/incident/1/attachments")[1]) == {"configured": False, "attachments": []}
    c.attachments = store_for(c.store, clock, s3=False)
    c.attachments.dvr = lambda: d
    code, out = call(api, path, {"from": 1, "to": 2, "incident_id": 1})
    assert code == 400 and out["error"] == att_mod.NOT_CONFIGURED
    assert json.loads(get(api, "/api/cad/incident/1/attachments")[1]) == {"configured": False, "attachments": []}
    assert c.attachments.sync_radio() == 0
    assert get(api, "/api/cad/incident/1/export.zip")[0] == 400


def test_a_failed_upload_says_why_and_is_audited(tmp_path):
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    a.fake.fail = lambda m, k: m == "PUT" and k.endswith("manifest.json")
    st = d.status(CAM)
    aid = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid},
               JULIAN)[1]["attachment"]["id"]
    row = a.row(aid)
    assert row["status"] == "failed" and "InternalError" in row["error"]
    assert [x["action"] for x in a.audit_rows(aid=aid)] == ["failed", "attach"]
    assert get(api, f"/api/cad/attachments/{aid}/download", VAL)[0] == 404


def test_only_the_leader_writes(tmp_path):
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    a.may_write = lambda: False
    st = d.status(CAM)
    code, out = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid}, JULIAN)
    assert code == 400 and "leader" in out["error"] and a.fake.calls == []


# -- radio ---------------------------------------------------------------------------------------------------------

WAV = b"RIFF" + b"\0" * 40 + b"radio audio"


def radio_setup(tmp_path):
    clock = RClock()
    c = cad(tmp_path, clock)
    a = store_for(c.store)
    audio = {"FG1-2": WAV, "FG1-9": WAV + b"2"}
    a.audio = audio.get
    c.attachments = a
    inc = _event_with(c, ["E63"], clock)
    clock.t += 10
    return c, a, inc, clock


def test_a_linked_transmission_is_copied_once_it_settles_and_only_once(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    t0 = clock.t
    assert c.record_traffic(tx("FG1-2", t0, rig="E63", radio_id="6301", text="engine 63 on scene")) == [inc["id"]]
    with c.store.lock:
        c.store.db.execute("UPDATE radio_traffic SET alt_text = 'engine 63 on scene.', alt_model = 'aws' WHERE id = 'FG1-2'")
    assert a.sync_radio() == 0                                              # not settled: its transcript may still come
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 1 and a.sync_radio() == 0                      # once
    rows = a.list(inc["id"])
    assert len(rows) == 1 and rows[0]["kind"] == "radio" and rows[0]["source"] == "FG1" and rows[0]["ref"] == "FG1-2"
    assert rows[0]["created_by"] == "auto (unit)" and rows[0]["play"].endswith("/audio.wav")
    pre = f"attachments/{inc['id']}/{rows[0]['id']}/"
    assert a.fake.objects[pre + "audio.wav"] == (WAV, "audio/wav")
    meta = json.loads(a.fake.objects[pre + "transmission.json"][0])
    assert (meta["text"], meta["alt_text"], meta["rig"], meta["radio_id"], meta["channel"]) == (
        "engine 63 on scene", "engine 63 on scene.", "E63", "6301", "FG1")
    man = json.loads(a.fake.objects[pre + "manifest.json"][0])
    assert man["kind"] == "radio" and man["source"]["transmission"] == "FG1-2" and man["audio"] is True
    assert {o["name"]: o["sha256"] for o in man["objects"]} == {
        "audio.wav": hashlib.sha256(WAV).hexdigest(),
        "transmission.json": hashlib.sha256(a.fake.objects[pre + "transmission.json"][0]).hexdigest()}
    assert [x["action"] for x in a.audit_rows(iid=inc["id"])] == ["attach"]
    # the audio DB's own retention doesn't matter now: the copy plays from the bucket
    a.audio = lambda tid: None
    api = CadApi(c)
    code, b, ctype, _ = get(api, f"/api/cad/attachments/{rows[0]['id']}/audio.wav")
    assert (code, b, ctype) == (200, WAV, "audio/wav")


def test_a_manual_link_is_copied_as_its_persons_and_without_audio_the_transcript_still_is(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("307-x-call_1", clock.t, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "60 Control to Port Chester"))
    assert c.link_traffic(inc["id"], {"traffic_id": "307-x-call_1", "operator": "Lt. Ruiz"})["linked"]
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 1
    r = a.list(inc["id"])[0]
    assert r["created_by"] == "Lt. Ruiz" and r["source"] == "Fire/EMS 15" and r["talkgroup"] == 307
    assert [o["name"] for o in r["objects"]] == ["transmission.json", "manifest.json"] and "no audio" in r["error"]
    assert "play" not in r and r["download"].endswith("/download")


def test_a_deleted_copy_is_never_copied_again_and_a_failed_one_is_tried_again(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("FG1-2", clock.t, rig="E63", radio_id="6301"))
    c.record_traffic(tx("FG1-9", clock.t + 1, rig="E63", radio_id="6301"))
    a.fake.fail = lambda m, k: m == "PUT" and "audio.wav" in k
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 0
    rows = a.list(inc["id"])
    assert [r["status"] for r in rows] == ["failed"]                        # the batch stopped at the first failure
    a.fake.fail = None
    assert a.sync_radio() == 1                                              # the other one; the failed one waits
    clock.t += att_mod.RETRY_S[0] + 1
    assert a.sync_radio() == 1 and {r["status"] for r in a.list(inc["id"])} == {"stored"}
    first = a.list(inc["id"])[0]["id"]
    a.delete(first, "Corey <corey@example.org>")
    assert a.sync_radio() == 0 and len(a.list(inc["id"], deleted=True)) == 2


def test_s3_trouble_backs_off_and_never_counts_but_a_refusal_does(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("FG1-10", clock.t, rig="E63", radio_id="6301"))
    clock.t += att_mod.SETTLE_S + 30
    a.fake.fail = lambda m, k: m == "PUT"                                   # a 500: S3's own trouble
    waits = []
    for _ in range(att_mod.TRIES + 4):
        a.sync_radio()
        row = c.store.one("SELECT tries, attempts, retry_at, status FROM attachments WHERE ref = 'FG1-10'")
        waits.append(row["retry_at"] - clock.t)
        assert a.sync_radio() == 0                                          # not before its time
        clock.t = row["retry_at"] + 1
    assert row["tries"] == 0 and row["attempts"] == att_mod.TRIES + 4 and row["status"] == "failed"
    assert waits[:3] == [60.0, 120.0, 240.0] and max(waits) <= att_mod.RETRY_S[1]
    # a refusal (a 400) counts: after TRIES it stops
    real = a.fake.__call__
    a.s3.send = lambda m, u, h, b, t: (400, {}, b"<Error><Code>InvalidRequest</Code></Error>") if m == "PUT" else real(m, u, h, b, t)
    for _ in range(att_mod.TRIES + 3):
        a.sync_radio()
        clock.t += att_mod.RETRY_S[1] + 1
    row = c.store.one("SELECT tries, status FROM attachments WHERE ref = 'FG1-10'")
    assert row == {"tries": att_mod.TRIES, "status": "failed"}


def test_the_export_is_one_zip_with_every_hash_checked(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("FG1-2", clock.t, rig="E63", radio_id="6301"))
    c.record_traffic(tx("FG1-9", clock.t + 3, rig="E63", radio_id="6301"))
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 2
    api = CadApi(c)
    resp = get(api, f"/api/cad/incident/{inc['id']}/export.zip")
    assert resp[0] == 200 and resp[2] == "application/zip" and inc["number"] in resp[3]["Content-Disposition"]
    assert resp[1].length is None                                           # streamed as it is made: no length ahead
    code, out, _ = get(api, f"/api/cad/incident/{inc['id']}/export.zip")    # one at a time
    assert code == 400 and "another export" in json.loads(out)["error"]
    z = zipfile.ZipFile(io.BytesIO(body_of(resp)))
    assert get(api, f"/api/cad/incident/{inc['id']}/export.zip")[1].read_all()[:2] == b"PK"   # free again
    man = json.loads(z.read("manifest.json"))
    assert man["verified"] and man["incident"]["number"] == inc["number"] and len(man["attachments"]) == 2
    assert len(man["files"]) == 6 and all(f["verified"] for f in man["files"])
    for f in man["files"]:
        assert hashlib.sha256(z.read(f["path"])).hexdigest() == f["sha256"]
    assert [x["action"] for x in a.audit_rows(iid=inc["id"])][0] == "export"
    # an object changed in the bucket: the export says so
    k = next(k for k in a.fake.objects if k.endswith("audio.wav"))
    a.fake.objects[k] = (b"tampered", "audio/wav")
    man = json.loads(zipfile.ZipFile(io.BytesIO(body_of(get(api, f"/api/cad/incident/{inc['id']}/export.zip")))).read("manifest.json"))
    assert man["verified"] is False and [f["path"] for f in man["files"] if not f["verified"]] == \
        [f["path"] for f in man["files"] if f["path"].endswith(f"{k.split('/')[-2]}/audio.wav")]


def test_expired_and_interrupted_rows_are_swept(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("FG1-2", clock.t, rig="E63", radio_id="6301"))
    clock.t += att_mod.SETTLE_S + 30
    a.sync_radio()
    with c.store.lock:
        c.store.db.execute("INSERT INTO attachments (id, incident_id, kind, source, start, end, status, created_at, expires_at) "
                           "VALUES ('00000000000000aa', ?, 'camera', 'x', 0, 1, 'saving', ?, ?)",
                           (inc["id"], clock.t - att_mod.SAVING_STALE_S - 1, clock.t + 1))
        c.store.db.execute("INSERT INTO attachments (id, incident_id, kind, source, start, end, status, created_at, expires_at) "
                           "VALUES ('00000000000000bb', ?, 'camera', 'x', 0, 1, 'stored', ?, ?)",
                           (inc["id"], clock.t, clock.t + att_mod.RETENTION_S))
    clock.t += att_mod.RETENTION_S + 1
    a.sweep()
    st = {r["id"]: r["status"] for r in a.list(inc["id"], deleted=True)}
    # a camera clip expires after a year; the radio copy never (an incident keeps its audio until an admin deletes it)
    assert st["00000000000000aa"] == "failed" and st["00000000000000bb"] == "expired"
    assert sorted(st.values()) == ["expired", "failed", "stored"]


def test_the_server_streams_a_download_and_closes_it(tmp_path):
    """Real HTTP (sizeup/web/server.py): a body too big to hold goes out in pieces, with its length, and is closed."""
    import http.client
    import threading
    from sizeup.web.server import WebServer
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    aid = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid},
               JULIAN)[1]["attachment"]["id"]
    closed = []
    real = a.download

    def download(x):
        s, name, ctype = real(x)
        s._on_close = lambda: closed.append(x)
        return s, name, ctype
    a.download = download
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
        conn.request("GET", f"/api/cad/attachments/{aid}/download")
        r = conn.getresponse()
        body = r.read()
        want = b"".join(Path(s["path"]).read_bytes() for s in d.segments(CAM, st["from"], st["to"]))
        assert r.status == 200 and body == want and int(r.getheader("Content-Length")) == len(want)
        assert r.getheader("Content-Type") == "video/MP2T" and r.getheader("Content-Disposition").startswith("attachment;")
        assert closed == [aid]
    finally:
        srv.httpd.shutdown()


class Later:
    """The upload pool, run when the test says (something happens to the recording meanwhile)."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *a):
        self.jobs.append((fn, a))

    def run(self):
        for fn, a in self.jobs:
            fn(*a)
        self.jobs = []


def test_a_merged_cameras_old_id_and_a_folder_moved_mid_upload(tmp_path):
    """#324 (camera dedup): a clip asked for under an id merged away is the kept camera's; the camera is held while its
    clip uploads, and a segment whose folder moved meanwhile (a merge's follow()) is found where it is now."""
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    moved = {"to": CAM}
    d.roads.alias = lambda cid: moved["to"] if cid in ("old-id", CAM) else cid
    a._pool = Later()
    out = call(api, "/api/cad/cameras/old-id/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid}, JULIAN)[1]
    aid = out["attachment"]["id"]
    assert out["attachment"]["source"] == CAM and d.holding == {CAM: 1}
    want = b"".join(Path(s["path"]).read_bytes() for s in d.segments(CAM, st["from"], st["to"]))
    # the camera merged into another id: its folder renamed, its index moved (as #324's follow() does)
    (tmp_path / "dvr" / CAM).rename(tmp_path / "dvr" / "NEW1")
    with d._lock:
        d._cams["NEW1"] = d._cams.pop(CAM)
    moved["to"] = "NEW1"
    a._pool.run()
    row = a.row(aid)
    assert row["status"] == "stored", row["error"]
    part = next(v for k, v in a.fake.objects.items() if k.endswith("part-0000.ts"))
    assert part[0] == want and d.holding == {}


def test_a_new_stream_mark_is_a_discontinuity_in_the_clip_and_folders_stay_put_while_it_saves(tmp_path, monkeypatch):
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    segs = d.segments(CAM, st["from"], st["to"])
    third = segs[2]["key"]
    monkeypatch.setattr(d, "_new_stream", lambda cid, key: key == third)       # (#324 marks these in the file name)
    a._pool = Later()
    aid = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["to"], "incident_id": iid},
               JULIAN)[1]["attachment"]["id"]
    assert not d.may_move(CAM, "other") and not d.may_move("other", CAM) and d.may_move("x", "y")
    a._pool.run()
    assert d.may_move(CAM, "other")
    d.may_write = lambda: False
    assert not d.may_move(CAM, "other")                                         # never on a draining leader
    lines = get(api, f"/api/cad/attachments/{aid}/index.m3u8", VAL)[1].decode().splitlines()
    tss = [i for i, x in enumerate(lines) if x.endswith(".ts")]
    assert lines.count("#EXT-X-DISCONTINUITY") == 1 and lines[tss[1] + 1] == "#EXT-X-DISCONTINUITY"


def test_a_delete_removes_every_version_even_a_retrys_and_a_failed_delete_can_be_asked_again(tmp_path):
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("FG1-2", clock.t, rig="E63", radio_id="6301"))
    clock.t += att_mod.SETTLE_S + 30
    a.fake.fail = lambda m, k: m == "PUT" and k.endswith("transmission.json")
    assert a.sync_radio() == 0                                              # audio.wav written, then a failure
    a.fake.fail = None
    clock.t += att_mod.RETRY_S[0] + 1
    assert a.sync_radio() == 1                                              # audio.wav written again: two versions
    aid = a.list(inc["id"])[0]["id"]
    wav = next(k for k in a.fake.versions if k.endswith("audio.wav"))
    assert len(a.fake.versions[wav]) == 2
    # a delete that fails part way: audited, the row as it was, what is left deleted when asked again
    real = a.fake.__call__
    calls = {"n": 0}

    def flaky(method, url, headers, body, timeout):
        if method == "DELETE":
            calls["n"] += 1
            if calls["n"] == 2:
                return 500, {}, b"<Error><Code>InternalError</Code></Error>"
        return real(method, url, headers, body, timeout)
    a.s3.send = flaky
    with pytest.raises(att_mod.AttachError, match="not deleted"):
        a.delete(aid, "Corey <corey@example.org>")
    assert a.row(aid)["status"] == "stored" and a.audit_rows(aid=aid)[0]["action"] == "failed"
    a.delete(aid, "Corey <corey@example.org>")
    assert a.fake.versions == {} and a.fake.objects == {} and a.row(aid)["status"] == "deleted"
    with pytest.raises(att_mod.AttachError, match="deleted already"):
        a.delete(aid, "Corey <corey@example.org>")


def test_without_the_bypass_a_locked_version_stays(tmp_path):
    """The governance lock is real in the fake: a version delete without the bypass header is refused, so only the
    admin delete (which sends it) removes anything."""
    s3 = S3("b", send=FakeS3(), creds=lambda: aws.Credentials("AKIDTEST", "secret"))
    v = s3.put("attachments/1/x/a.bin", b"data")
    code, _, _ = s3._call("DELETE", "attachments/1/x/a.bin", {}, query=f"versionId={v}")
    assert code == 403 and s3.get("attachments/1/x/a.bin") == b"data"
    s3.delete_version("attachments/1/x/a.bin", v)
    with pytest.raises(aws.AwsError):
        s3.get("attachments/1/x/a.bin")


def test_a_version_whose_put_answer_was_lost_is_deleted_too(tmp_path):
    """A PUT that went through but whose answer never came (no version recorded): the admin delete lists the
    attachment's prefix and removes it as well."""
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("FG1-2", clock.t, rig="E63", radio_id="6301"))
    clock.t += att_mod.SETTLE_S + 30
    real = a._wrote
    a._wrote = lambda aid, key, v: None if key.endswith("audio.wav") else real(aid, key, v)
    assert a.sync_radio() == 1
    a._wrote = real
    aid = a.list(inc["id"])[0]["id"]
    assert not any(k.endswith("audio.wav") for k, _ in json.loads(a.row(aid)["versions"]))
    a.delete(aid, "Corey <corey@example.org>")
    assert a.fake.versions == {} and "3 versions (listed)" in a.audit_rows(aid=aid)[0]["detail"]


def test_a_transcript_only_download_over_http_is_not_gzipped_and_an_export_streams(tmp_path):
    """Real HTTP: a streamed JSON body (a radio copy with no audio) goes as it is, not gzipped (it raised); an export
    goes without a length, to the end of the connection, a whole zip."""
    import http.client
    import threading
    from sizeup.web.server import WebServer
    c, a, inc, clock = radio_setup(tmp_path)
    c.record_traffic(tx("307-x-call_1", clock.t, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "words " * 400))
    assert c.link_traffic(inc["id"], {"traffic_id": "307-x-call_1", "operator": "Lt. Ruiz"})["linked"]
    clock.t += att_mod.SETTLE_S + 30
    assert a.sync_radio() == 1
    aid = a.list(inc["id"])[0]["id"]
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=CadApi(c))
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    try:
        port = srv.httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/api/cad/attachments/{aid}/download", headers={"Accept-Encoding": "gzip"})
        r = conn.getresponse()
        body = r.read()
        assert r.status == 200 and r.getheader("Content-Encoding") is None and len(body) > 1024
        assert json.loads(body)["text"].startswith("words")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/api/cad/incident/{inc['id']}/export.zip")
        r = conn.getresponse()
        body = r.read()
        assert r.status == 200 and r.getheader("Content-Length") is None and r.getheader("Connection") == "close"
        z = zipfile.ZipFile(io.BytesIO(body))
        assert json.loads(z.read("manifest.json"))["verified"] and z.testzip() is None
    finally:
        srv.httpd.shutdown()


def test_a_clip_interrupted_by_a_deploy_is_uploaded_again_by_the_next_leader(tmp_path):
    api, c, a, d, iid, clock = camera_setup(tmp_path)
    st = d.status(CAM)
    a._pool = Later()                                                      # the old leader: SIGTERM, its queue gone
    aid = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"] + 6, "to": st["to"], "incident_id": iid,
                                                          "note": "approach"}, JULIAN)[1]["attachment"]["id"]
    gone = call(api, f"/api/cad/cameras/{CAM}/dvr/clip", {"from": st["from"], "to": st["from"] + 3, "incident_id": iid},
                JULIAN)[1]["attachment"]["id"]
    assert a.row(aid)["status"] == "saving" and a.fake.objects == {}
    # the new leader (the same store and bucket, the DVR read from the shared disk)
    b = Attachments(c.store, a.s3, log=lambda m: None, clock=clock)
    b._pool = Now()
    b.dvr, b.camera = (lambda: d), d.roads.camera
    first = min(d._cams[CAM]["segs"])
    d._remove(CAM, first)                                                  # the short one's range: evicted meanwhile
    assert b.resume() == 1
    row = b.row(aid)
    assert row["status"] == "stored" and row["note"] == "approach" and "Julian" in row["created_by"]
    want = b"".join(Path(s["path"]).read_bytes() for s in d.segments(CAM, row["start"], row["end"]))
    part = a.fake.objects[f"attachments/{iid}/{aid}/part-0000.ts"][0]
    assert part == want                                                    # the same id, the same range
    assert b.row(gone)["status"] == "failed" and "no longer recorded" in b.row(gone)["error"]
    assert b._resumed                                                      # once per process
