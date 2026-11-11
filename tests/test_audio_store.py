"""#414: the hub's audio bytes in S3 (sizeup/audiostore.py), audio.sqlite keeping only their metadata
(sizeup/cloud/hubstore.py AudioDb); the retention per channel / talkgroup (Settings > System > Audio retention).

Synthetic audio only: tones made here, in the stations' real shape (8 kHz mono 16-bit PCM WAV, a few seconds, as
measured on the hub's clips). S3 is a fake bucket that checks every request is signed (SigV4) and answers PUT / GET /
DeleteObjects / ListObjectsV2 as S3 does; nothing reaches AWS, every database is a fresh one under tmp_path."""

import base64
import hashlib
import io
import math
import re
import sqlite3
import struct
import threading
import time
import wave
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from sizeup import attachments as att_mod
from sizeup import audiostore, aws
from sizeup.app import make_app
from sizeup.audiostore import DiskCache, FsAudioStore, Retention, S3AudioStore, StoreError, object_key
from sizeup.cad.store import CadError
from sizeup.cloud.hubstore import AUDIO_SCHEMA, AudioDb, wav_seconds
from tests.test_access import AUD, TEAM, jwks, jwt, keys, req  # noqa: F401 (fixtures)
from tests.test_attachments import FakeS3
from tests.test_cloud import HubRun, call, hub_cfg

DAY = 86400.0


def tone(seconds: float = 2.0, hz: float = 1000.0, rate: int = 8000) -> bytes:
    """A synthetic clip in the stations' shape: 8 kHz, mono, 16-bit PCM WAV."""
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * k / rate))) for k in range(n))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


class FakeBucket:
    """One private bucket, versioning off: PUT (SSE-S3, Content-MD5 and the SHA-256 checksum checked), GET,
    DeleteObjects (POST ?delete, Content-MD5, at most 1000 keys, Quiet), ListObjectsV2 (pages of `page` keys). Every
    request must be SigV4-signed with the task role's credentials in headers; a presigned (query-signed) URL is
    refused. fail(method, key) -> True: a 500; deny(method, key) -> True: a 403 AccessDenied."""

    def __init__(self, page: int = 1000):
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, float] = {}
        self.calls: list[tuple[str, str, int]] = []      # (method, key or "?delete" / "?list", keys in a delete)
        self.urls: list[str] = []
        self.fail = None
        self.deny = None
        self.refuse_delete: set[str] = set()             # keys DeleteObjects reports as an <Error>
        self.delete_body = None                          # an answer to DeleteObjects in place of S3's
        self.page = page
        self.clock = time.time

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        self.urls.append(url)
        key = unquote(u.path[1:])
        q = dict(parse_qsl(u.query, keep_blank_values=True))
        h = {k.lower(): v for k, v in headers.items()}
        assert h["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") and "x-amz-date" in h
        assert "X-Amz-Signature" not in u.query, "never a presigned URL"
        assert timeout <= 30
        if self.fail is not None and self.fail(method, key):
            self.calls.append((method, key, 0))
            return 500, {}, b"<Error><Code>InternalError</Code></Error>"
        if self.deny is not None and self.deny(method, key):
            self.calls.append((method, key, 0))
            return 403, {}, b"<Error><Code>AccessDenied</Code></Error>"
        if method == "PUT":
            assert h["x-amz-server-side-encryption"] == "AES256"
            assert base64.b64decode(h["content-md5"]) == hashlib.md5(body).digest()
            assert base64.b64decode(h["x-amz-checksum-sha256"]) == hashlib.sha256(body).digest()
            self.objects[key] = bytes(body)
            self.modified[key] = self.clock()
            self.calls.append(("PUT", key, 0))
            return 200, {"ETag": '"' + hashlib.md5(body).hexdigest() + '"'}, b""
        if method == "GET" and q.get("list-type") == "2":
            self.calls.append(("GET", "?list", 0))
            keys = sorted(k for k in self.objects if k.startswith(q.get("prefix", "")))
            after = q.get("continuation-token") or ""             # S3's token: where it stopped (a key), not an offset
            rest = [k for k in keys if k > after]
            page = rest[:self.page]
            more = len(rest) > self.page
            rows = "".join(f"<Contents><Key>{k}</Key><LastModified>"
                           f"{datetime.fromtimestamp(self.modified[k], timezone.utc).isoformat().replace('+00:00', 'Z')}"
                           f"</LastModified></Contents>" for k in page)
            tail = f"<NextContinuationToken>{page[-1]}</NextContinuationToken>" if more else ""
            return 200, {}, (f"<ListBucketResult><IsTruncated>{'true' if more else 'false'}</IsTruncated>{rows}{tail}"
                             "</ListBucketResult>").encode()
        if method == "GET":
            self.calls.append(("GET", key, 0))
            if key not in self.objects:
                return 404, {}, b"<Error><Code>NoSuchKey</Code></Error>"
            return 200, {}, self.objects[key]
        if method == "POST" and u.query == "delete=":
            assert base64.b64decode(h["content-md5"]) == hashlib.md5(body).digest()
            keys_ = [unquote(k) for k in re.findall(r"<Key>(.*?)</Key>", body.decode())]
            assert "<Quiet>true</Quiet>" in body.decode() and 1 <= len(keys_) <= 1000
            self.calls.append(("DELETE", "?delete", len(keys_)))
            if self.delete_body is not None:
                return 200, {}, self.delete_body
            errs = ""
            for k in keys_:
                if k in self.refuse_delete:
                    errs += f"<Error><Key>{k}</Key><Code>AccessDenied</Code></Error>"
                else:
                    self.objects.pop(k, None)
            return 200, {}, f'<?xml version="1.0"?><DeleteResult xmlns="x">{errs}</DeleteResult>'.encode()
        raise AssertionError((method, url))


def s3store(bucket: FakeBucket) -> S3AudioStore:
    return S3AudioStore("sizeup-audio-test", region="us-east-1", send=bucket,
                        creds=lambda: aws.Credentials("AKIDTEST", "secret"))


def db(tmp_path, store=None, cache=True, name="audio.sqlite", **kw) -> AudioDb:
    kw.setdefault("may_delete", kw.get("may_write") or (lambda: True))       # a lease: the hub's fence, ok
    return AudioDb(tmp_path / name, store=store, cache=DiskCache(tmp_path / "cache") if cache else None,
                   log=lambda m: None, **kw)


class Lease:
    """The hub's fence (sizeup/fence.py) as a test gives it: ours and fresh."""

    def __init__(self, ok: bool = True):
        self.okay = ok

    def ok(self) -> bool:
        return self.okay


def cold(a: AudioDb) -> None:
    """Forget what the process has in memory and on its disk cache (a new task after a deploy)."""
    with a._mem:
        a._recent.clear()
        a._recent_bytes = 0
        a._known.clear()
    if a.cache is not None:
        a.cache.drop([r[0] for r in a.db.execute("SELECT key FROM audio")])


def row(a: AudioDb, aid: str):
    return a.db.execute("SELECT stored, LENGTH(wav), key FROM audio WHERE id = ?", (aid,)).fetchone()


# -- write / read ------------------------------------------------------------------------------------------------------

def test_object_keys_by_kind_and_the_clips_own_date():
    at = 1794278579.0                                         # 2026-11-10 02:42 UTC
    assert object_key("clip", at, "FG1-1794364979-1") == "audio/clip/2026/11/10/FG1-1794364979-1.wav"
    assert object_key("dispatch", at, "ab12" * 5) == "audio/dispatch/2026/11/10/" + "ab12" * 5 + ".wav"
    for bad in ("../x", "a/b", "", "x" * 200):
        with pytest.raises(ValueError):
            object_key("clip", at, bad)
    with pytest.raises(ValueError):
        object_key("other", at, "x")


@pytest.mark.parametrize("kind", ["s3", "folder"])
def test_round_trip_metadata_only_in_the_database(tmp_path, kind):
    bucket = FakeBucket()
    store = s3store(bucket) if kind == "s3" else FsAudioStore(tmp_path / "objects")
    a = db(tmp_path, store)
    wav = tone(3.0)
    at = time.time() - 60
    a.put("FG1-1", wav, "clip", at=at, channel="Fire/EMS 15", talkgroup=307)
    assert row(a, "FG1-1")[:2] == ("db", len(wav)) and a.wake.is_set()          # the spool first, the uploader woken
    assert a.upload_pending() == 1
    r = a.db.execute("SELECT kind, at, LENGTH(wav), channel, talkgroup, duration, bytes, key, etag, stored "
                     "FROM audio WHERE id = 'FG1-1'").fetchone()
    assert r[:7] == ("clip", at, 0, "Fire/EMS 15", 307, 3.0, len(wav))
    assert r[7] == object_key("clip", at, "FG1-1") and r[8] == '"' + hashlib.md5(wav).hexdigest() + '"'
    assert r[9] == "store" and a.bytes() == 0 and a.has("FG1-1")
    assert a.get("FG1-1") == wav                               # the newest: from memory (the STT reads it at once)
    cold(a)
    assert a.has("FG1-1") and a.get("FG1-1") == wav            # from the store, then cached on the disk
    if kind == "s3":
        assert len([c for c in bucket.calls if c[0] == "GET"]) == 1
        a._recent.clear()
        assert a.get("FG1-1") == wav and len([c for c in bucket.calls if c[0] == "GET"]) == 1     # the disk cache
    assert a.get("nope") is None and not a.has("nope")
    assert wav_seconds(wav) == 3.0 and wav_seconds(b"not a wav") == 0.0


def test_ingest_never_waits_on_the_store(tmp_path):
    """A page or a transmission is in audio.sqlite before any S3 call: a hanging S3 doesn't hold up put()."""
    class Hanging(FsAudioStore):
        def put(self, key, body):
            time.sleep(5)
            raise StoreError("timed out")

    a = db(tmp_path, Hanging(tmp_path / "o"))
    t = time.monotonic()
    for k in range(20):
        a.put(f"P-{k}", tone(0.5), "clip", channel="South 2")
    assert time.monotonic() - t < 1.0 and a.pending()[0] == 20
    assert all(a.get(f"P-{k}") == tone(0.5) for k in range(20))


def test_a_clip_is_readable_for_speech_to_text_before_and_without_its_upload(tmp_path):
    bucket = FakeBucket()
    bucket.fail = lambda m, k: m == "PUT"                     # S3 down
    a = db(tmp_path, s3store(bucket))
    wav = tone(1.5)
    a.put("FG1-2", wav, "clip", channel="FG1")
    assert a.get("FG1-2") == wav                               # at once, from memory
    assert a.upload_pending() == 0
    cold(a)
    assert a.get("FG1-2") == wav and row(a, "FG1-2")[:2] == ("db", len(wav))   # from the row until uploaded


def test_a_crash_after_the_upload_before_the_switch_leaves_the_row_holding_its_bytes(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    wav = tone(2.0)
    a.put("FG1-3", wav, "clip", at=1794278579.0, channel="FG1")
    real = a.db

    class Crash:                                              # the process dies at the switch, after the PUT
        def execute(self, sql, *args):
            if sql.lstrip().startswith("UPDATE audio SET wav = x''"):
                raise SystemExit("killed")
            return real.execute(sql, *args)

    a.db = Crash()
    with pytest.raises(SystemExit):
        a.upload_pending()
    a.db = real
    key = object_key("clip", 1794278579.0, "FG1-3")
    assert bucket.objects[key] == wav and row(a, "FG1-3") == ("db", len(wav), key)   # the row still has the bytes
    a.close()
    b = db(tmp_path, s3store(bucket))                          # a new process: the same key again (idempotent)
    assert b.upload_pending() == 1 and set(bucket.objects) == {key} and row(b, "FG1-3") == ("store", 0, key)
    cold(b)
    assert b.get("FG1-3") == wav


def test_a_failed_upload_waits_in_the_database_and_is_retried_never_dropped(tmp_path):
    bucket = FakeBucket()
    bucket.fail = lambda m, k: m == "PUT"
    a = db(tmp_path, s3store(bucket))
    clips = {f"FG1-{k}": tone(1.0 + k / 10, 500 + 100 * k) for k in range(5)}
    for k, (cid, wav) in enumerate(clips.items()):
        a.put(cid, wav, "clip", at=time.time() - 100 + k, channel="FG1")
    assert a.pending() == (5, sum(map(len, clips.values())))
    assert a.upload_pending() == 0                             # down: kept, backed off
    assert a.pending()[0] == 5 and a._upload_after > time.monotonic()
    bucket.fail = None
    assert a.upload_pending() == 0                             # the backoff is respected
    a._upload_after = 0.0
    assert a.upload_pending(limit=3) == 3                      # newest first
    assert a.upload_pending() == 2 and a.pending() == (0, 0)
    for cid, wav in clips.items():
        assert bucket.objects[a.db.execute("SELECT key FROM audio WHERE id = ?", (cid,)).fetchone()[0]] == wav
    cold(a)
    assert all(a.get(cid) == wav for cid, wav in clips.items())


def test_new_bytes_put_during_an_upload_are_never_switched_to_the_old_object(tmp_path):
    """The compare-and-set on the bytes (`AND wav = ?`): without it the row would point at the old object."""
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    old, new = tone(1.0, 400), tone(1.5, 900)
    a.put("FG1-r", old, "clip", at=1794278579.0, channel="FG1")
    real_put = a.store.put

    def put_and_reput(key, body):
        etag = real_put(key, body)
        a.put("FG1-r", new, "clip", at=1794278579.0, channel="FG1")      # the station sends it again, other bytes
        return etag

    a.store.put = put_and_reput
    assert a.upload_pending() == 0                             # not switched: the row holds other bytes now
    assert row(a, "FG1-r")[:2] == ("db", len(new))
    a.store.put = real_put
    assert a.upload_pending() == 1
    cold(a)
    assert a.get("FG1-r") == new and bucket.objects[row(a, "FG1-r")[2]] == new


def test_one_uploader_at_a_time(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    for k in range(40):
        a.put(f"U-{k}", tone(0.2, 300 + k), "clip", channel="FG1")
    ts = [threading.Thread(target=a.upload_pending, kwargs={"limit": 40}) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    puts = [k for m, k, _ in bucket.calls if m == "PUT"]
    assert len(puts) == len(set(puts)) == 40 and a.pending()[0] == 0


def test_only_the_leader_uploads_or_deletes(tmp_path):
    bucket = FakeBucket()
    leader = {"ok": False}
    a = db(tmp_path, s3store(bucket), may_write=lambda: leader["ok"])
    a.put("FG1-5", tone(), "clip", at=time.time() - 10 * DAY, channel="FG1")
    assert a.upload_pending() == 0 and a.prune_clips(time.time()) == 0 and a.has("FG1-5") and bucket.calls == []
    leader["ok"] = True
    assert a.upload_pending() == 1
    leader["ok"] = False
    assert a.prune_clips(time.time()) == 0 and bucket.objects and a.sweep_orphans(min_age_s=0) == 0
    leader["ok"] = True
    assert a.prune_clips(time.time()) == 1 and not bucket.objects


def test_a_403_is_an_error_not_missing_and_keeps_the_credentials(tmp_path, monkeypatch):
    bucket = FakeBucket()
    forgot = []
    monkeypatch.setattr(aws, "forget_credentials", lambda: forgot.append(1))
    a = db(tmp_path, s3store(bucket), cache=False)
    a.put("FG1-d", tone(), "clip", channel="FG1")
    assert a.upload_pending() == 1
    cold(a)
    bucket.deny = lambda m, k: m == "GET"
    assert a.get("FG1-d") is None                              # the player: no audio now
    with pytest.raises(StoreError):
        a.get("FG1-d", strict=True)                            # the attachments copy: try again later
    assert forgot == [] and a.has("FG1-d")                     # AccessDenied on one key: the credentials stay
    bucket.deny = None
    bucket.fail = None
    expired = FakeBucket()
    expired.objects = bucket.objects

    def expired_token(method, url, headers, body, timeout):
        return 403, {}, b"<Error><Code>ExpiredToken</Code></Error>"

    s = S3AudioStore("b", region="us-east-1", send=expired_token, creds=lambda: aws.Credentials("AKIDTEST", "secret"))
    with pytest.raises(StoreError):
        s.get("audio/clip/x.wav")
    assert forgot == [1]                                       # an expired token: fetched again
    assert a.get("nope", strict=True) is None                  # no such clip: None, not an error


# -- the retention ----------------------------------------------------------------------------------------------------

def put_stored(a: AudioDb, cid, days_old, now, channel="FG1", talkgroup=None, wav=None):
    a.put(cid, wav or tone(0.2), "clip", at=now - days_old * DAY, channel=channel, talkgroup=talkgroup)


def test_retention_per_channel_and_talkgroup_and_a_change_applies_to_clips_already_kept(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    now = time.time()
    for cid, ch, tg, days in [("fg1-old", "FG1", None, 3), ("fg1-new", "FG1", None, 0.2),
                              ("tg307-old", "Fire/EMS 15", 307, 20), ("tg307-mid", "Fire/EMS 15", 307, 5),
                              ("hpn-old", "HPN Tower", None, 2), ("plain-old", "South 2", None, 1)]:
        put_stored(a, cid, days, now, ch, tg)
    while a.upload_pending():
        pass
    ov = {"_default": 0.5, "FG1": 7, "tg:307": 10, "HPN Tower": 1}

    def prune(o):
        p = Retention(o, 0.5)
        return a.prune_clips(now - p.shortest_days() * DAY, now=now, policy=p)

    assert prune(ov) == 3                                      # tg 307 past 10 d, HPN past 1 d, South 2 past the default
    assert {r[0] for r in a.db.execute("SELECT id FROM audio")} == {"fg1-old", "fg1-new", "tg307-mid"}
    assert len(bucket.objects) == 3
    ov["FG1"] = 1                                              # an admin shortens FG1: the clip already kept goes
    ov["tg:307"] = 30                                          # ... and lengthens 307: the kept one stays
    assert prune(ov) == 1
    assert {r[0] for r in a.db.execute("SELECT id FROM audio")} == {"fg1-new", "tg307-mid"}
    assert audiostore.keep_days({"FG1": 9}, "Fire/EMS 15", 307, 0.5) == 0.5          # a talkgroup isn't its name's
    assert audiostore.keep_days({"tg:307": 3, "Fire/EMS 15": 9}, "Fire/EMS 15", 307, 0.5) == 3
    assert audiostore.keep_days({"Fire/EMS 15": 9}, "Fire/EMS 15", 307, 0.5) == 9    # no talkgroup's own: the channel's


def test_the_sql_retention_matches_keep_days_for_every_clip(tmp_path):
    a = db(tmp_path, None)
    now = time.time()
    ov = {"_default": 2, "FG1": 7, "tg:307": 10, "tg:333": 1, "Fire/EMS 15": 4, "HPN Tower": 0.5}
    p = Retention(ov, 0.5)
    combos = [("FG1", None), ("Fire/EMS 15", 307), ("Fire/EMS 15", 999), ("Other", 333), ("HPN Tower", None),
              ("", None), ("South 2", None)]
    k = 0
    for ch, tg in combos:
        for days in (0.4, 0.6, 1.5, 3, 5, 8, 12):
            k += 1
            put_stored(a, f"c{k}", days, now, ch, tg)
    expect = {aid for aid, at, ch, tg in a.db.execute("SELECT id, at, channel, talkgroup FROM audio")
              if at < now - p.keep_days(ch, tg) * DAY}
    before = {r[0] for r in a.db.execute("SELECT id FROM audio")}
    a.prune_clips(now - p.shortest_days() * DAY, now=now, policy=p)
    assert before - {r[0] for r in a.db.execute("SELECT id FROM audio")} == expect and expect


def test_deletes_go_in_batches_of_1000_and_a_refused_one_keeps_its_row(tmp_path, monkeypatch):
    from sizeup.cloud import hubstore
    monkeypatch.setattr(hubstore, "SCAN_PAGE", 700)            # the scan's pages don't line up with the deletes'
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket), cache=False)
    now = time.time()
    wav = tone(0.1)
    for k in range(2500):
        a.put(f"c{k:05d}", wav, "clip", at=now - 3 * DAY + k, channel="FG1")
    while a.upload_pending(limit=1000):
        pass
    refused = a.db.execute("SELECT key FROM audio WHERE id = 'c00007'").fetchone()[0]
    bucket.refuse_delete = {refused}
    bucket.calls.clear()
    assert a.prune_clips(now, batch=200) == 2499
    deletes = [c[2] for c in bucket.calls if c[0] == "DELETE"]
    assert max(deletes) <= 1000 and sum(deletes) == 2500
    assert [r[0] for r in a.db.execute("SELECT id FROM audio")] == ["c00007"] and bucket.objects == {refused: wav}
    bucket.refuse_delete = set()
    assert a.prune_clips(now) == 1 and not bucket.objects      # tried again next time


def test_a_delete_answer_that_isnt_a_delete_result_keeps_every_row(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    for k in range(3):
        put_stored(a, f"d{k}", 3, time.time())
    a.upload_pending()
    for body in (b"", b"<html>proxy error</html>", b"<Error><Code>SlowDown</Code></Error>"):
        bucket.delete_body = body
        assert a.prune_clips(time.time()) == 0 and len(list(a.db.execute("SELECT id FROM audio"))) == 3
    bucket.delete_body = None
    bucket.fail = lambda m, k: m == "POST"
    assert a.prune_clips(time.time()) == 0 and len(list(a.db.execute("SELECT id FROM audio"))) == 3


def test_deleting_a_row_still_holding_its_bytes_deletes_an_object_uploaded_before_a_crash(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    put_stored(a, "FG1-x", 3, time.time())
    key = row(a, "FG1-x")[2]
    bucket.objects[key] = tone(0.2)                            # uploaded, the switch never happened (a crash)
    assert row(a, "FG1-x")[0] == "db"
    assert a.prune_clips(time.time()) == 1 and key not in bucket.objects


def sweep_rig(tmp_path, rows=60, page=7, **kw):
    """A bucket and an audio.sqlite that agree: `rows` clips a few days old, all uploaded; plus dispatch audio."""
    bucket = FakeBucket(page=page)
    a = db(tmp_path, s3store(bucket), **kw)
    now = time.time()
    for k in range(rows):
        put_stored(a, f"c{k:03d}", 3 + k / rows, now)
    a.put("d" * 20, tone(0.2), "dispatch", "Polly", at=now - 30 * DAY)
    while a.upload_pending(limit=100):
        pass
    for k in bucket.modified:
        bucket.modified[k] = now - 3 * DAY
    return a, bucket, now


def test_the_orphan_sweep_deletes_only_old_clip_objects_no_row_names(tmp_path):
    a, bucket, now = sweep_rig(tmp_path)
    day = object_key("clip", now - 3 * DAY, "x").rsplit("/", 1)[0]
    orphans = [f"{day}/orphan-{k}.wav" for k in range(3)]
    for k in orphans + [f"{day}/young.wav", "audio/dispatch/2026/11/01/" + "e" * 20 + ".wav", "audio/clip/2020/01/01/ancient.wav"]:
        bucket.objects[k] = b"x"
        bucket.modified[k] = now - 2 * DAY
    bucket.modified[f"{day}/young.wav"] = now - 3600
    before = set(bucket.objects)
    assert a.sweep_orphans(now=now) == 3 and a.sweep_status == ""
    # dispatch audio never; a folder older than the oldest clip the database knows: the lifecycle's; a young one: kept
    assert before - set(bucket.objects) == set(orphans)
    assert {c[1] for c in bucket.calls if c[0] == "GET"} == {"?list"}


def test_the_orphan_sweep_never_empties_the_bucket(tmp_path):
    """The wipe a wrong audio.sqlite would cause: an empty one, an old one, no lease: nothing deleted, said why."""
    a, bucket, now = sweep_rig(tmp_path)
    keys = set(bucket.objects)
    # an empty audio.sqlite (a restore that didn't happen) with the same bucket
    empty = db(tmp_path, s3store(bucket), name="empty.sqlite")
    assert empty.sweep_orphans(now=now) == 0 and "no clips" in empty.sweep_status and set(bucket.objects) == keys
    # an old audio.sqlite: it knows only the oldest few clips
    old = db(tmp_path, s3store(bucket), name="old.sqlite")
    rows = a.db.execute("SELECT id, kind, at, key FROM audio WHERE kind = 'clip' ORDER BY at LIMIT 5").fetchall()
    old.db.executemany("INSERT INTO audio (id, kind, at, note, wav, key, stored) VALUES (?, ?, ?, '', x'', ?, 'store')", rows)
    assert old.sweep_orphans(now=now) == 0 and "too many" in old.sweep_status and set(bucket.objects) == keys
    # no lease (no fence: a local hub with the bucket set), whatever the database
    for x in (db(tmp_path, s3store(bucket), name="nofence.sqlite", may_delete=lambda: False), AudioDb(
            tmp_path / "plain.sqlite", store=s3store(bucket), log=lambda m: None)):
        assert x.sweep_orphans(now=now, min_age_s=0) == 0 and set(bucket.objects) == keys
    # ... and its retention deletes nothing from the store either
    nofence = AudioDb(tmp_path / "audio.sqlite", store=s3store(bucket), log=lambda m: None)
    assert nofence.prune_clips(now) == 0 and set(bucket.objects) == keys
    assert [c for c in bucket.calls if c[0] == "DELETE"] == []


def test_the_hub_sweeps_only_with_a_lease_held_a_day_and_the_migration_done(hub_s3, monkeypatch):
    from sizeup.cloud import hub as hub_mod
    app = hub_s3
    now = time.time()
    app.apply("traffic", {"id": "FG1-s", "source": "FG1", "start": now - 60, "end": now - 58, "text": "x", "status": "done"},
              tone(0.2))
    swept = []
    monkeypatch.setattr(app.audio, "sweep_orphans", lambda **k: swept.append(1) or 0)
    app._swept_at = 0.0
    assert not app._sweep_due(now)                             # just took the lease
    app._lead_since = time.monotonic() - hub_mod.SWEEP_LEADER_S - 1
    assert not app._sweep_due(now)                             # a clip still to upload (the migration isn't done)
    app.audio.upload_pending()
    assert app._sweep_due(now)
    app.fence = None
    assert not app._sweep_due(now) and app._lead_since is None   # no fence: never
    app.fence = Lease(False)
    assert not app._sweep_due(now)


def test_records_on_an_event_outlive_their_channel(tmp_path):
    a = db(tmp_path, FsAudioStore(tmp_path / "objects"))
    now = time.time()
    for cid, days in (("attached", 40), ("loose", 40), ("attached-ancient", 400)):
        put_stored(a, cid, days, now)
    kept = lambda ids: {i for i in ids if i.startswith("attached")}  # noqa: E731
    p = Retention({"FG1": 1}, 0.5)
    n = a.prune_clips(now - 0.5 * DAY, kept=kept, kept_older_than=now - 365 * DAY, now=now, policy=p)
    assert n == 2 and {r[0] for r in a.db.execute("SELECT id FROM audio")} == {"attached"}


def test_dispatch_audio_keeps_its_own_rule(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    now = time.time()
    a.put("a" * 20, tone(), "dispatch", "Polly", at=now - 400 * DAY)      # the dispatch log's: kept
    a.put("b" * 20, tone(), "dispatch", "Polly", at=now - 2 * DAY)        # a preview nobody sent
    a.upload_pending()
    assert a.prune_clips(now, policy=Retention({}, 0.5), now=now) == 0
    assert a.prune_dispatch({"a" * 20}, now - DAY) == 1
    assert [k.split("/")[1] for k in bucket.objects] == ["dispatch"] and a.note("a" * 20) == "Polly"
    cold(a)
    assert a.get("a" * 20) == tone()


def test_the_disk_cache_keeps_its_lru_order_across_a_restart(tmp_path):
    t = {"now": 1000.0}
    c = DiskCache(tmp_path / "c", max_bytes=4 * 100, clock=lambda: t["now"])
    for k in ("a", "b", "c"):
        t["now"] += 10
        c.put(k, bytes(90))
    t["now"] += 10
    assert c.get("a") == bytes(90)                             # a used last: b is the oldest now
    c2 = DiskCache(tmp_path / "c", max_bytes=4 * 100, clock=lambda: t["now"])    # a restart
    t["now"] += 10
    c2.put("d", bytes(90))
    t["now"] += 10
    c2.put("e", bytes(90))
    assert c2.get("b") is None and c2.get("a") == bytes(90) and c2.get("e") == bytes(90)


# -- the migration ----------------------------------------------------------------------------------------------------

def legacy(path, clips: dict[str, tuple[float, bytes]]) -> None:
    """An audio.sqlite as the release before #414 (and before #251: auto_vacuum off) wrote it."""
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(AUDIO_SCHEMA)
    for cid, (at, wav) in clips.items():
        con.execute("INSERT INTO audio (id, kind, at, note, wav) VALUES (?, 'clip', ?, '', ?)", (cid, at, wav))
    con.close()


def test_the_migration_is_resumable_safe_across_a_handover_and_ends_small(tmp_path):
    now = time.time()
    clips = {f"FG1-{k}": (now - 3600 - k, tone(2.0, 300 + 10 * k)) for k in range(30)}
    legacy(tmp_path / "audio.sqlite", clips)
    before = (tmp_path / "audio.sqlite").stat().st_size
    bucket = FakeBucket()
    lease = {"ok": True}
    a = db(tmp_path, s3store(bucket), may_write=lambda: lease["ok"])
    assert a.auto_vacuum() == 0 and a.pending()[0] == 30
    assert all(a.get(c) == w for c, (_, w) in clips.items())   # readable all along
    assert a.upload_pending(limit=10) == 10
    a.close()                                                  # the task is replaced mid-way (a deploy)
    b = db(tmp_path, s3store(bucket), may_write=lambda: lease["ok"])
    assert b.pending()[0] == 20
    real_put = b.store.put

    def put_then_lose(key, body):                              # the lease goes while an object is uploading
        etag = real_put(key, body)
        lease["ok"] = False
        return etag

    b.store.put = put_then_lose
    assert b.upload_pending(limit=10) == 0 and b.pending()[0] == 20
    b.store.put = real_put
    lease["ok"] = True
    c = db(tmp_path, s3store(bucket), may_write=lambda: lease["ok"])
    while c.upload_pending(limit=7):
        pass
    assert c.pending() == (0, 0) and len(bucket.objects) == 30
    puts = [k for m, k, _ in bucket.calls if m == "PUT"]
    assert len(puts) == 31 and len(set(puts)) == 30            # one object PUT twice (the handover), same bytes
    assert c.backfill_channels(lambda ids: {i: ("FG1", None) for i in ids}) == 30
    assert c.compact(min_free_bytes=64 * 1024) and c.auto_vacuum() == 2     # (32 MB free in production)
    c.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert (tmp_path / "audio.sqlite").stat().st_size < before / 10
    cold(c)
    assert all(c.get(cid) == w for cid, (_, w) in clips.items())
    assert not c.compact()                                     # once


def test_compaction_only_when_worth_it(tmp_path):
    legacy(tmp_path / "audio.sqlite", {"x": (time.time(), tone())})
    assert not db(tmp_path, None).compact()                    # no store
    a = db(tmp_path, FsAudioStore(tmp_path / "o"), may_write=lambda: False)
    assert not a.compact()                                     # not the leader
    a.may_write = lambda: True
    assert not a.compact()                                     # bytes still in the database
    a.upload_pending()
    assert not a.compact()                                     # little free: not worth a VACUUM
    assert a.compact(min_free_bytes=1)                         # free >= live: rewritten


def test_the_release_before_reads_the_new_rows_without_crashing(tmp_path):
    """Rollback: the old code's own statements (sizeup/cloud/hubstore.py before #414) on a migrated file."""
    a = db(tmp_path, FsAudioStore(tmp_path / "o"))
    a.put("FG1-new", tone(), "clip", at=time.time() - 30 * DAY, channel="FG1")
    a.upload_pending()
    a.close()
    old = sqlite3.connect(tmp_path / "audio.sqlite", isolation_level=None)
    old.executescript(AUDIO_SCHEMA)                                                      # its CREATE IF NOT EXISTS
    assert {r[0] for r in old.execute("SELECT id FROM audio")} == {"FG1-new"}
    wav = bytes(old.execute("SELECT wav FROM audio WHERE id = ?", ("FG1-new",)).fetchone()[0])
    assert wav == b""                                          # server.py: `if wav` -> 404 "no such clip", no crash
    old.execute("INSERT INTO audio (id, kind, at, note, wav) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "kind = excluded.kind, at = excluded.at, note = excluded.note, wav = excluded.wav",
                ("FG1-old-code", "clip", time.time(), "", tone(1.0)))
    assert old.execute("SELECT COALESCE(SUM(LENGTH(wav)), 0) FROM audio").fetchone()[0] == len(tone(1.0))
    old.execute("DELETE FROM audio WHERE id = ?", ("FG1-new",))                         # its prune: the row only
    old.close()
    b = db(tmp_path, FsAudioStore(tmp_path / "o"))             # rolled forward again: the old code's row migrates
    assert b.pending()[0] == 1 and b.upload_pending() == 1 and b.get("FG1-old-code") == tone(1.0)
    # the object the old release left behind (its row pruned, not its object): the sweep's, if it is dated within
    # what the database knows (here the old row was 30 days back, before the oldest clip left: the lifecycle's)
    assert b.sweep_orphans(min_age_s=0) == 0 and len(list((tmp_path / "o").rglob("*.wav"))) == 2


# -- the hub: Settings, the attachments copy, playback ----------------------------------------------------------------

@pytest.fixture
def hub_fs(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_AUDIO_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("SIZEUP_AUDIO_BUCKET", raising=False)
    app = make_app(hub_cfg(tmp_path, gis_layers=()))
    app.fence = Lease()
    yield app
    app.stop()


def test_settings_validate_audit_and_apply_retroactively_on_the_hub(hub_fs):
    app = hub_fs
    ed = app.cad.settings
    now = time.time()
    app.apply("traffic", {"id": "FG1-a", "source": "FG1", "start": now - 3 * DAY, "end": now - 3 * DAY + 2,
                          "text": "Engine 60 responding", "status": "done"}, tone())
    app.apply("traffic", {"id": "TG-a", "source": "Fire/EMS 15", "talkgroup": 307, "start": now - 3 * DAY,
                          "end": now - 3 * DAY + 2, "text": "Engine 60 on scene", "status": "done"}, tone(1.0))
    assert app.audio.upload_pending() == 2
    assert app.audio.db.execute("SELECT channel, talkgroup, stored FROM audio WHERE id = 'TG-a'").fetchone() == \
        ("Fire/EMS 15", 307, "store")
    for bad in ({"key": "_other", "value": 3}, {"key": "tg:abc", "value": 3}, {"key": "FG1", "value": 0.1},
                {"key": "FG1", "value": 366}, {"key": "FG1", "value": True}, {"key": "FG1", "value": "7"},
                {"key": "", "value": 3}, {"key": " FG1", "value": 3}):
        with pytest.raises(CadError):
            ed.post("audio_retention", {"action": "set", "operator": "Admin", **bad})
    for key in ("_default", "FG1", "tg:0307"):                 # tg:0307 is tg:307
        ed.post("audio_retention", {"action": "set", "key": key, "value": 30, "operator": "Admin"})
    assert set(ed.section("audio_retention")) == {"_default", "FG1", "tg:307"}
    assert app._audio_retention(now) == 0                      # 30 days: both kept
    page = ed.page()["audio_retention"]
    assert {k: page["default"][k] for k in ("value", "default", "edited")} == {"value": 30.0, "default": 0.5, "edited": True}
    assert {r["key"]: (r["clips"], r["days"], r["edited"]) for r in page["rows"]} == {"FG1": (1, 30.0, True),
                                                                                     "tg:307": (1, 30.0, True)}
    ed.post("audio_retention", {"action": "set", "key": "tg:307", "value": 2, "operator": "Admin"})
    assert app._audio_retention(now) == 1                      # shortened: the talkgroup's clip already kept goes
    assert {r[0] for r in app.audio.db.execute("SELECT id FROM audio")} == {"FG1-a"}
    ed.post("audio_retention", {"action": "reset", "key": "_default", "operator": "Admin"})
    ed.post("audio_retention", {"action": "reset", "key": "FG1", "operator": "Admin"})
    assert app._audio_retention(now) == 1                      # back to the default, 12 h
    audit = [(r["section"], r["key"], r["old"], r["new"]) for r in ed.audit_log("system")
             if r["key"].startswith("audio_retention/")]
    assert audit[0] == ("system", "audio_retention/FG1", "30 d", "default")
    assert ("system", "audio_retention/tg:307", "30 d", "2 d") in audit and len(audit) == 6
    with pytest.raises(CadError):
        ed.post("audio_retention", {"action": "wipe", "key": "FG1", "operator": "x"})


def test_the_settings_page_shows_each_talkgroups_days_as_the_prune_uses_them(hub_fs):
    app = hub_fs
    ed = app.cad.settings
    now = time.time()
    app.apply("traffic", {"id": "TG-b", "source": "Fire/EMS 15", "talkgroup": 307, "start": now - 60, "end": now - 58,
                          "text": "Engine 60 on scene", "status": "done"}, tone(1.0))
    ed.post("audio_retention", {"action": "set", "key": "Fire/EMS 15", "value": 9, "operator": "Admin"})
    ed.post("audio_retention", {"action": "set", "key": "_default", "value": 2, "operator": "Admin"})
    rows = {r["key"]: r for r in ed.page()["audio_retention"]["rows"]}
    # the talkgroup has no days of its own: its channel's name's (9), as the prune does, not the default (2)
    assert rows["tg:307"]["days"] == 9.0 and not rows["tg:307"]["edited"]
    assert rows["tg:307"]["days"] == app.audio_keep().keep_days("Fire/EMS 15", 307)
    ed.post("audio_retention", {"action": "set", "key": "tg:307", "value": 3, "operator": "Admin"})
    assert {r["key"]: r["days"] for r in ed.page()["audio_retention"]["rows"]}["tg:307"] == 3.0


def test_an_import_with_a_bad_retention_is_refused(hub_fs):
    ed = hub_fs.cad.settings
    good = ed.export()
    good["overrides"]["audio_retention"] = {"FG1": 14, "tg:307": 3}
    assert ed.post("import", {"action": "dry_run", "settings": good, "operator": "Admin"})["ok"]
    for bad in ({"FG1": 9999}, {"tg:0307": 3}):
        b = ed.export()
        b["overrides"]["audio_retention"] = bad
        with pytest.raises(CadError, match="audio_retention"):
            ed.post("import", {"action": "dry_run", "settings": b, "operator": "Admin"})


def test_event_days_over_the_buckets_hard_max_are_refused(tmp_path):
    from sizeup.config import ConfigError, load
    p = tmp_path / "c.toml"
    p.write_text("[storage]\naudio_keep_event_days = 400\n")
    assert load(p).audio_keep_event_days == 400.0
    p.write_text("[storage]\naudio_keep_event_days = 401\n")
    with pytest.raises(ConfigError, match="0..400"):
        load(p)


def test_retention_settings_are_for_admins_only(tmp_path, keys, jwks, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SIZEUP_AUDIO_DIR", str(tmp_path / "objects"))
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD))
    try:
        h.server.access.certs_url = jwks.url
        h.server.access.log = lambda m: None
        admin = {"Cf-Access-Jwt-Assertion": jwt(keys[0])}
        disp = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email="val@example.org")}
        body = {"action": "set", "key": "FG1", "value": 3}
        assert req(h.url + "/api/cad/settings/audio_retention", "POST", body)[0] == 401
        cad = h.app.cad
        req(h.url + "/api/cad/whoami", headers=admin)
        ops = cad.settings.operators()
        cad.settings.post("users", {"action": "edit", "id": ops[0]["id"], "role": "admin", "operator": "x"})
        cad.settings.post("users", {"action": "add", "name": "Val", "email": "val@example.org", "role": "dispatcher",
                                    "operator": "x"})
        assert req(h.url + "/api/cad/settings/audio_retention", "POST", body, disp)[0] == 403
        code, out = req(h.url + "/api/cad/settings/audio_retention", "POST", body, admin)
        assert code == 200 and next(r for r in out["audio_retention"]["rows"] if r["key"] == "FG1")["days"] == 3.0
    finally:
        h.close()


@pytest.fixture
def hub_s3(tmp_path, monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setenv("SIZEUP_AUDIO_BUCKET", "sizeup-audio-test")
    monkeypatch.setattr(audiostore, "from_env", lambda log=print: s3store(bucket))
    app = make_app(hub_cfg(tmp_path, gis_layers=()))
    app.fence = Lease()
    app.bucket = bucket
    yield app
    app.stop()


def test_the_incident_copy_waits_for_the_store_and_never_saves_without_its_audio(hub_s3):
    """The real path: a transmission on the hub, linked to an incident, copied by sizeup/attachments.py from the
    audio store; while the store can't be read the copy fails (tried again later), never "no audio kept"; and the
    hub's retention (Comms.kept_for_events, not a stub) keeps the linked clip past its channel's days."""
    app, bucket = hub_s3, hub_s3.bucket
    a = app.attachments
    clips_fake = FakeS3()
    a.s3 = att_mod.S3("sizeup-clips-test", region="us-east-1", send=clips_fake,
                      creds=lambda: aws.Credentials("AKIDTEST", "secret"))
    now = time.time()
    wav = tone(2.0)
    for tid in ("FG1-linked", "FG1-loose"):
        app.apply("traffic", {"id": tid, "source": "FG1", "start": now - 2 * DAY, "end": now - 2 * DAY + 2,
                              "text": "Engine 60 on scene", "status": "done"}, wav)
    assert app.audio.upload_pending() == 2
    cold(app.audio)
    iid = app.cad.create({"type": "STRU", "address": "1 Main St", "operator": "x"})["id"]
    with app.cad.store.tx() as d:
        d.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES ('FG1-linked', ?, 'manual', 'x')",
                  (iid,))
    bucket.fail = lambda m, k: m == "GET"                      # the audio store can't be read now
    assert a.sync_radio() == 0
    r = a.store.one("SELECT status, tries, error FROM attachments WHERE ref = 'FG1-linked'")
    assert r["status"] == "failed" and r["tries"] == 0 and "no audio" not in r["error"]
    assert not any(k.endswith("/audio.wav") for k in clips_fake.objects)
    bucket.fail = None
    with a.store.lock:
        a.store.db.execute("UPDATE attachments SET retry_at = 0")
    assert a.sync_radio() == 1
    r = a.store.one("SELECT status, error FROM attachments WHERE ref = 'FG1-linked'")
    assert r["status"] == "stored" and r["error"] == ""
    assert [v[0] for k, v in clips_fake.objects.items() if k.endswith("/audio.wav")] == [wav]
    # FG1 kept a day: the loose clip goes, the incident's stays (a record: audio_keep_event_days)
    app.cad.settings.post("audio_retention", {"action": "set", "key": "FG1", "value": 1, "operator": "Admin"})
    assert app._audio_retention(now) == 1
    assert app.audio.has("FG1-linked") and not app.audio.has("FG1-loose")


def test_playback_streams_through_the_hub_behind_access_never_a_public_url(tmp_path, keys, jwks, monkeypatch):  # noqa: F811
    bucket = FakeBucket()
    monkeypatch.setenv("SIZEUP_AUDIO_BUCKET", "sizeup-audio-test")
    monkeypatch.setattr(audiostore, "from_env", lambda log=print: s3store(bucket))
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD))
    try:
        h.server.access.certs_url = jwks.url
        h.server.access.log = lambda m: None
        wav = tone(2.5)
        now = time.time()
        batch = {"epoch": "e1", "events": [{"seq": 1, "at": now, "type": "traffic", "audio": base64.b64encode(wav).decode(),
                                            "data": {"id": "FG1-p", "source": "FG1", "start": now - 3, "end": now,
                                                     "text": "Engine 60 responding", "status": "done"}}]}
        assert call(h.url + "/api/station/events", "POST", batch)[1]["applied"] == 1
        deadline = time.monotonic() + 10                       # the uploader, woken by the clip
        while h.app.audio.pending()[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [m for m, *_ in bucket.calls] == ["PUT"]
        cold(h.app.audio)
        assert req(h.url + "/api/audio/FG1-p.wav")[0] == 401                     # not signed in: nothing
        code, body = req(h.url + "/api/audio/FG1-p.wav", headers={"Cf-Access-Jwt-Assertion": jwt(keys[0])})
        assert code == 200 and body == wav                                     # the bytes, through the hub
        assert [m for m, *_ in bucket.calls] == ["PUT", "GET"]
        assert all("X-Amz-Signature" not in u and "X-Amz-Credential" not in u for u in bucket.urls)
        assert b"sizeup-audio-test" not in body and not hasattr(audiostore.S3AudioStore, "presign")
    finally:
        h.close()


# -- 30 days by default; the related data lasts as long as the audio (#414) -------------------------------------------

def test_the_hub_keeps_every_channels_clips_30_days_by_default():
    from sizeup.config import load
    from tests.test_cloud import ROOT
    cfg = load(ROOT / "deploy" / "hub" / "config.hub.toml")
    assert cfg.audio_keep_hours == 720 and cfg.audio_keep_event_days >= 30
    p = Retention({}, cfg.audio_keep_hours / 24)
    assert p.keep_days("FG1", None) == p.keep_days("Fire/EMS 15", 307) == p.keep_days("HPN Tower", None) == 30.0


def test_transcripts_have_their_own_retention_never_shorter_than_the_audio(tmp_path):
    """The radio log (cad.sqlite radio_traffic: transcript, talker, radio ID, channel): a year by default, per channel
    / talkgroup in Settings (retroactive), never pruned before its audio; a record not before the bucket's hard max;
    a transmission linked to an event never."""
    from sizeup.cad import comms as comms_mod
    from tests.test_radios import Clock, cad, tx
    clock = Clock()
    c = cad(tmp_path, clock)
    c.comms.audio_default_days = 30                            # the hub's (deploy/hub/config.hub.toml)
    t0 = clock.t

    def put(tid, days, source="FG1", tg=None):
        c.record_traffic(tx(tid, t0 - days * DAY, source=source, tg=tg))

    def left():
        return {r["id"] for r in c.store.q("SELECT id FROM radio_traffic")}

    def setting(section, key, days):
        with c.store.tx() as d:
            d.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES (?, ?, ?, 0) "
                      "ON CONFLICT(section, key) DO UPDATE SET value = excluded.value", (section, key, str(days)))

    for tid, days in (("fg1-400", 400), ("fg1-300", 300), ("fg1-40", 40), ("rec-390", 390), ("rec-410", 410)):
        put(tid, days)
    put("tg-40", 40, "Fire/EMS 15", 307)
    with c.store.tx() as d:
        d.execute("UPDATE radio_traffic SET emergency = 1 WHERE id IN ('rec-390', 'rec-410')")
    assert comms_mod.KEEP_UNLINKED_S == 365 * DAY
    assert c.comms.prune() == 2                                # a year: fg1-400 and rec-410 (past the hard max)
    assert left() == {"fg1-300", "fg1-40", "rec-390", "tg-40"}
    setting("transcript_retention", "tg:307", 30)              # an admin: 307's transcripts a month (retroactive)
    setting("transcript_retention", "FG1", 30)
    setting("audio_retention", "FG1", 60)                      # ... but FG1's audio is kept 60 days: its text too
    assert c.comms.prune() == 2 and left() == {"fg1-40", "rec-390"}   # fg1-300 (past 60 d) and tg-40 (past 30 d)
    setting("audio_retention", "FG1", 30)
    put("fg1-35", 35)
    assert c.comms.prune() == 2 and left() == {"rec-390"}
    assert comms_mod.KEEP_RECORD_S >= audiostore.HARD_MAX_DAYS * DAY


def test_transcript_settings_validate_audit_and_show_the_longer_of_the_two(hub_fs):
    app = hub_fs
    ed = app.cad.settings
    now = time.time()
    app.apply("traffic", {"id": "TG-t", "source": "Fire/EMS 15", "talkgroup": 307, "start": now - 60, "end": now - 58,
                          "text": "Engine 60 on scene", "status": "done"}, tone(1.0))
    for bad in ({"key": "_format", "value": "wav"}, {"key": "FG1", "value": 0.5}, {"key": "FG1", "value": 4000},
                {"key": "tg:x", "value": 3}):
        with pytest.raises(CadError):
            ed.post("transcript_retention", {"action": "set", "operator": "Admin", **bad})
    page = ed.page()["audio_retention"]
    assert page["default"]["t_value"] == 365.0 and page["t_min"] == 1.0 and page["t_max"] == 3650.0
    ed.post("transcript_retention", {"action": "set", "key": "tg:0307", "value": 10, "operator": "Admin"})
    ed.post("audio_retention", {"action": "set", "key": "tg:307", "value": 60, "operator": "Admin"})
    row = next(r for r in ed.page()["audio_retention"]["rows"] if r["key"] == "tg:307")
    assert (row["days"], row["t_days"], row["edited"], row["t_edited"]) == (60.0, 60.0, True, True)   # text >= audio
    audit = [(r["key"], r["old"], r["new"]) for r in ed.audit_log("system") if r["key"].startswith("transcript_")]
    assert audit == [("transcript_retention/tg:307", "default", "10 d")]
    totals = ed.page()["audio_retention"]["totals"]
    assert totals["audio"]["clips"] == 1 and totals["transcripts"]["rows"] >= 1 and totals["incident"] == {
        "clips": 0, "gb": 0.0, "usd_month": 0.0} and totals["usd_gb_month"] == 0.023
    b = ed.export()
    b["overrides"]["transcript_retention"] = {"FG1": 0.1}
    with pytest.raises(CadError, match="transcript_retention"):
        ed.post("import", {"action": "dry_run", "settings": b, "operator": "Admin"})


def test_unused_dispatch_previews_are_kept_with_the_clips_on_the_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_AUDIO_DIR", str(tmp_path / "objects"))
    app = make_app(hub_cfg(tmp_path, gis_layers=(), audio_keep_hours=720))     # deploy/hub/config.hub.toml's 30 days
    app.fence = Lease()
    request_stop = app.stop
    now = time.time()
    app.audio.put("c" * 20, tone(0.5), "dispatch", "Polly", at=now - 10 * DAY)
    app.audio.put("e" * 20, tone(0.5), "dispatch", "Polly", at=now - 40 * DAY)
    app.cad.audio_dir.mkdir(parents=True, exist_ok=True)
    try:
        app.cad._prune_audio()
        assert app.audio.has("c" * 20) and not app.audio.has("e" * 20)
    finally:
        request_stop()


def test_the_audio_format_is_wav_until_another_is_written(hub_fs):
    ed = hub_fs.cad.settings
    assert ed.page()["audio_retention"]["format"] == {"value": "wav", "choices": ["wav"], "edited": False}
    for bad in ("flac", "opus", "WAV", 1):
        with pytest.raises(CadError, match="_format"):
            ed.post("audio_retention", {"action": "set", "key": "_format", "value": bad, "operator": "Admin"})
    ed.post("audio_retention", {"action": "set", "key": "_format", "value": "wav", "operator": "Admin"})
    assert ed.page()["audio_retention"]["format"]["edited"] and Retention(ed.section("audio_retention"), 30).ch == {}
    b = ed.export()
    b["overrides"]["audio_retention"] = {"_format": "flac"}
    with pytest.raises(CadError, match="audio_retention"):
        ed.post("import", {"action": "dry_run", "settings": b, "operator": "Admin"})


def test_a_short_transcript_setting_never_outruns_the_default_audio(tmp_path, monkeypatch):
    from sizeup.cad import comms as comms_mod
    from tests.test_radios import Clock, _event_with, cad, tx
    clock = Clock()
    c = cad(tmp_path, clock)
    inc = _event_with(c, ["E63"], clock)
    monkeypatch.setattr(comms_mod.Comms, "PRUNE_BATCH", 3)       # several batches, each its own short transaction
    t0 = clock.t
    for k, days in enumerate((2, 10, 20, 29, 31, 40, 50, 60, 70, 80)):
        c.record_traffic(tx(f"FG1-{k}", t0 - days * DAY))
    with c.store.tx() as d:
        d.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES "
                  "('transcript_retention', 'FG1', '5', 0)")
        d.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES ('FG1-9', ?, 'manual', 'x')",
                  (inc["id"],))
    assert c.comms.prune() == 0                                # before the hub says its audio default: kept a year
    c.comms.audio_default_days = 30                            # the hub's: FG1's audio 30 days, its text not before
    assert c.comms.prune() == 5
    assert {r["id"] for r in c.store.q("SELECT id FROM radio_traffic")} == {f"FG1-{k}" for k in (0, 1, 2, 3, 9)}


def test_a_clip_whose_object_is_gone_is_not_retried_forever(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket), cache=False)
    a.put("FG1-g", tone(), "clip", channel="FG1")
    a.upload_pending()
    cold(a)
    bucket.objects.clear()                                     # really gone: a 404
    assert a.get("FG1-g", strict=True) is None                 # no error: the copy saves the transcript only
    bucket.fail = lambda m, k: m == "GET"                      # a 500: an error, tried again
    with pytest.raises(StoreError):
        a.get("FG1-g", strict=True)
