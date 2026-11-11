"""#414: the hub's audio bytes in S3 (sizeup/audiostore.py), audio.sqlite keeping only their metadata
(sizeup/cloud/hubstore.py AudioDb); the retention per channel / talkgroup (Settings > System > Audio retention).

Synthetic audio only: tones and silence made here, in the stations' real shape (8 kHz mono 16-bit PCM WAV, a few
seconds, as measured on the hub's clips). S3 is a fake bucket that checks every request is signed (SigV4) and
answers PUT / GET / DeleteObjects as S3 does; nothing reaches AWS, every database is a fresh one under tmp_path."""

import base64
import hashlib
import io
import math
import re
import sqlite3
import struct
import time
import wave
from urllib.parse import unquote, urlsplit

import pytest

from sizeup import audiostore, aws
from sizeup.app import make_app
from sizeup.audiostore import DiskCache, FsAudioStore, S3AudioStore, object_key
from sizeup.cad.store import CadError
from sizeup.cloud.hubstore import AUDIO_SCHEMA, AudioDb, wav_seconds
from tests.test_access import AUD, TEAM, jwks, jwt, keys, req  # noqa: F401 (fixtures)
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
    DeleteObjects (POST ?delete, Content-MD5, at most 1000 keys, Quiet). Every request must be SigV4-signed with the
    task role's credentials in headers; a presigned (query-signed) URL is refused. fail(method, key) -> True: a 500."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str, int]] = []      # (method, key or "?delete", keys in a delete)
        self.urls: list[str] = []
        self.fail = None
        self.refuse_delete: set[str] = set()             # keys DeleteObjects reports as an <Error>

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        self.urls.append(url)
        key = unquote(u.path[1:])
        h = {k.lower(): v for k, v in headers.items()}
        assert h["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") and "x-amz-date" in h
        assert "X-Amz-Signature" not in u.query, "never a presigned URL"
        assert timeout <= 30
        if self.fail is not None and self.fail(method, key):
            self.calls.append((method, key, 0))
            return 500, {}, b"<Error><Code>InternalError</Code></Error>"
        if method == "PUT":
            assert h["x-amz-server-side-encryption"] == "AES256"
            assert base64.b64decode(h["content-md5"]) == hashlib.md5(body).digest()
            assert base64.b64decode(h["x-amz-checksum-sha256"]) == hashlib.sha256(body).digest()
            self.objects[key] = bytes(body)
            self.calls.append(("PUT", key, 0))
            return 200, {"ETag": '"' + hashlib.md5(body).hexdigest() + '"'}, b""
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
            errs = ""
            for k in keys_:
                if k in self.refuse_delete:
                    errs += f"<Error><Key>{k}</Key><Code>AccessDenied</Code></Error>"
                else:
                    self.objects.pop(k, None)
            return 200, {}, f"<DeleteResult>{errs}</DeleteResult>".encode()
        raise AssertionError((method, url))


def s3store(bucket: FakeBucket) -> S3AudioStore:
    return S3AudioStore("sizeup-audio-test", region="us-east-1", send=bucket,
                        creds=lambda: aws.Credentials("AKIDTEST", "secret"))


def db(tmp_path, store=None, cache=True, name="audio.sqlite", **kw) -> AudioDb:
    return AudioDb(tmp_path / name, store=store, cache=DiskCache(tmp_path / "cache") if cache else None,
                   log=lambda m: None, **kw)


def cold(a: AudioDb) -> None:
    """Forget what the process has in memory and on its disk cache (a new task after a deploy)."""
    a._recent.clear()
    a._recent_bytes = 0
    if a.cache is not None:
        a.cache.drop(list(r[0] for r in a.db.execute("SELECT key FROM audio")))


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
    row = a.db.execute("SELECT kind, at, LENGTH(wav), channel, talkgroup, duration, bytes, key, etag, stored "
                       "FROM audio WHERE id = 'FG1-1'").fetchone()
    assert row[:7] == ("clip", at, 0, "Fire/EMS 15", 307, 3.0, len(wav))
    assert row[7] == object_key("clip", at, "FG1-1") and row[8] == '"' + hashlib.md5(wav).hexdigest() + '"'
    assert row[9] == "store" and a.bytes() == 0 and a.has("FG1-1")
    assert a.get("FG1-1") == wav                               # the newest: from memory (the STT reads it at once)
    cold(a)
    assert a.get("FG1-1") == wav                               # from the store, then cached on the disk
    if kind == "s3":
        gets = [c for c in bucket.calls if c[0] == "GET"]
        assert len(gets) == 1
        a._recent.clear()
        assert a.get("FG1-1") == wav and len([c for c in bucket.calls if c[0] == "GET"]) == 1     # the disk cache
    assert a.get("nope") is None and not a.has("nope")
    assert wav_seconds(wav) == 3.0 and wav_seconds(b"not a wav") == 0.0


def test_a_clip_is_readable_for_speech_to_text_before_and_without_its_upload(tmp_path):
    bucket = FakeBucket()
    bucket.fail = lambda m, k: m == "PUT"                     # S3 down
    a = db(tmp_path, s3store(bucket))
    wav = tone(1.5)
    a.put("FG1-2", wav, "clip", channel="FG1")
    assert a.get("FG1-2") == wav                               # at once, from memory
    cold(a)
    assert a.get("FG1-2") == wav                               # and from the row, which holds the bytes until uploaded
    assert a.db.execute("SELECT stored, LENGTH(wav) FROM audio WHERE id = 'FG1-2'").fetchone() == ("db", len(wav))


def test_a_crash_between_the_upload_and_the_row_leaves_an_orphan_object_never_a_row_pointing_at_nothing(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    wav = tone(2.0)
    real = a.db

    class Crash:                                              # the process dies at the INSERT, after the PUT
        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO audio"):
                raise SystemExit("killed")
            return real.execute(sql, *args)

    a.db = Crash()
    with pytest.raises(SystemExit):
        a.put("FG1-3", wav, "clip", at=1794364979.0, channel="FG1")
    a.db = real
    key = object_key("clip", 1794364979.0, "FG1-3")
    assert bucket.objects[key] == wav                          # uploaded ...
    b = db(tmp_path, s3store(bucket))                          # ... a new process: no row, so no clip to play
    assert not b.has("FG1-3") and b.get("FG1-3") is None
    assert b.db.execute("SELECT COUNT(*) FROM audio WHERE stored = 'store'").fetchone()[0] == 0
    # the station never got its ack, so it sends the event again: the same key, the same bytes (idempotent)
    b.put("FG1-3", wav, "clip", at=1794364979.0, channel="FG1")
    assert set(bucket.objects) == {key} and b.db.execute("SELECT key FROM audio").fetchone()[0] == key
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
    assert len([c for c in bucket.calls if c[0] == "PUT"]) == 1      # after the first failure: straight to the database
    assert a.upload_pending() == 0                             # still down: kept, backed off
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


def test_only_the_leader_writes_or_deletes(tmp_path):
    bucket = FakeBucket()
    leader = {"ok": False}
    a = db(tmp_path, s3store(bucket), may_write=lambda: leader["ok"])
    a.put("FG1-5", tone(), "clip", at=time.time() - 10 * DAY, channel="FG1")
    assert bucket.calls == [] and a.pending()[0] == 1          # not the leader: no PUT (kept in the row)
    assert a.upload_pending() == 0 and a.prune_clips(time.time()) == 0 and a.has("FG1-5")
    leader["ok"] = True
    assert a.upload_pending() == 1
    leader["ok"] = False
    assert a.prune_clips(time.time()) == 0 and bucket.objects
    leader["ok"] = True
    assert a.prune_clips(time.time()) == 1 and not bucket.objects


# -- the retention ----------------------------------------------------------------------------------------------------

def test_retention_per_channel_and_talkgroup_and_a_change_applies_to_clips_already_kept(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    now = time.time()
    clips = [("fg1-old", "FG1", None, 3), ("fg1-new", "FG1", None, 0.2), ("tg307-old", "Fire/EMS 15", 307, 20),
             ("tg307-mid", "Fire/EMS 15", 307, 5), ("hpn-old", "HPN Tower", None, 2), ("plain-old", "South 2", None, 1)]
    for cid, ch, tg, days in clips:
        a.put(cid, tone(0.5), "clip", at=now - days * DAY, channel=ch, talkgroup=tg)
    ov = {"_default": 0.5, "FG1": 7, "tg:307": 10, "HPN Tower": 1}

    def prune(o):
        return a.prune_clips(now - audiostore.shortest_days(o, 0.5) * DAY, now=now,
                             keep_for=lambda ch, tg: audiostore.keep_days(o, ch, tg, 0.5) * DAY)

    assert prune(ov) == 3                                      # tg 307 past 10 d, HPN past 1 d, South 2 past the default
    assert {r[0] for r in a.db.execute("SELECT id FROM audio")} == {"fg1-old", "fg1-new", "tg307-mid"}
    assert len(bucket.objects) == 3
    ov["FG1"] = 1                                              # an admin shortens FG1: the clip already kept goes
    ov["tg:307"] = 30                                          # ... and lengthens 307: the kept one stays
    assert prune(ov) == 1
    assert {r[0] for r in a.db.execute("SELECT id FROM audio")} == {"fg1-new", "tg307-mid"}
    assert audiostore.keep_days({"FG1": 9}, "Fire/EMS 15", 307, 0.5) == 0.5          # a talkgroup isn't its name's
    assert audiostore.keep_days({"tg:307": 3, "Fire/EMS 15": 9}, "Fire/EMS 15", 307, 0.5) == 3


def test_records_on_an_event_outlive_their_channel_and_attached_ones_are_never_pruned_early(tmp_path):
    a = db(tmp_path, FsAudioStore(tmp_path / "objects"))
    now = time.time()
    for cid, days in (("attached", 40), ("loose", 40), ("attached-ancient", 400)):
        a.put(cid, tone(0.3), "clip", at=now - days * DAY, channel="FG1")
    kept = lambda ids: {i for i in ids if i.startswith("attached")}  # noqa: E731 (Comms.kept_for_events)
    n = a.prune_clips(now - 1 * DAY, kept=kept, kept_older_than=now - 365 * DAY, now=now,
                      keep_for=lambda ch, tg: 1 * DAY)
    assert n == 2 and {r[0] for r in a.db.execute("SELECT id FROM audio")} == {"attached"}


def test_deletes_go_in_batches_of_1000_and_a_refused_one_keeps_its_row(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket), cache=False)
    now = time.time()
    wav = tone(0.1)
    for k in range(2500):
        a.put(f"c{k:05d}", wav, "clip", at=now - 3 * DAY + k, channel="FG1")
    a._recent.clear()
    refused = a.db.execute("SELECT key FROM audio WHERE id = 'c00007'").fetchone()[0]
    bucket.refuse_delete = {refused}
    begins = []
    real = a.db

    class Count:
        def execute(self, sql, *args):
            if sql == "BEGIN":
                begins.append(1)
            return real.execute(sql, *args)

        def executemany(self, *args):
            return real.executemany(*args)

    a.db = Count()
    assert a.prune_clips(now, batch=200) == 2499
    a.db = real
    deletes = [c for c in bucket.calls if c[0] == "DELETE"]
    assert [c[2] for c in deletes] == [1000, 1000, 500]
    assert len(begins) == 13                                   # 999 + 1000 + 500 rows, 200 a transaction
    assert [r[0] for r in a.db.execute("SELECT id FROM audio")] == ["c00007"] and bucket.objects == {refused: wav}
    bucket.refuse_delete = set()
    assert a.prune_clips(now) == 1 and not bucket.objects      # tried again next time


def test_a_store_that_cannot_delete_leaves_every_row(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    for k in range(3):
        a.put(f"d{k}", tone(0.1), "clip", at=time.time() - 3 * DAY, channel="FG1")
    bucket.fail = lambda m, k: m == "POST"
    assert a.prune_clips(time.time()) == 0 and len(list(a.db.execute("SELECT id FROM audio"))) == 3


def test_dispatch_audio_keeps_its_own_rule(tmp_path):
    bucket = FakeBucket()
    a = db(tmp_path, s3store(bucket))
    now = time.time()
    a.put("a" * 20, tone(), "dispatch", "Polly", at=now - 400 * DAY)      # the dispatch log's: kept
    a.put("b" * 20, tone(), "dispatch", "Polly", at=now - 2 * DAY)        # a preview nobody sent
    assert a.prune_clips(now, keep_for=lambda ch, tg: 0.5 * DAY, now=now) == 0
    assert a.prune_dispatch({"a" * 20}, now - DAY) == 1
    assert [k.split("/")[1] for k in bucket.objects] == ["dispatch"] and a.note("a" * 20) == "Polly"
    cold(a)
    assert a.get("a" * 20) == tone()


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
    # the task is replaced mid-way (a deploy): a new process carries on where the rows say
    a.db.close()
    b = db(tmp_path, s3store(bucket), may_write=lambda: lease["ok"])
    assert b.pending()[0] == 20
    # the lease goes while an object is uploading: the object is there, the row is left for the next leader
    real_put = b.store.put

    def put_then_lose(key, body):
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
    # backfilled channels and the one-time rewrite: small, incremental vacuum from now on
    assert c.backfill_channels(lambda ids: {i: ("FG1", None) for i in ids}) == 30
    assert c.compact() and c.auto_vacuum() == 2
    c.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert (tmp_path / "audio.sqlite").stat().st_size < before / 10
    cold(c)
    assert all(c.get(cid) == w for cid, (_, w) in clips.items())
    assert not c.compact()                                     # once


def test_no_compaction_while_bytes_are_still_in_the_database_or_without_a_store(tmp_path):
    legacy(tmp_path / "audio.sqlite", {"x": (time.time(), tone())})
    assert not db(tmp_path, None).compact()
    a = db(tmp_path, FsAudioStore(tmp_path / "o"), may_write=lambda: False)
    assert not a.compact()


def test_the_release_before_reads_the_new_rows_without_crashing(tmp_path):
    """Rollback: the old code's own statements (sizeup/cloud/hubstore.py before #414) on a migrated file."""
    a = db(tmp_path, FsAudioStore(tmp_path / "o"))
    a.put("FG1-new", tone(), "clip", at=time.time() - 30 * DAY, channel="FG1")
    a.db.close()
    old = sqlite3.connect(tmp_path / "audio.sqlite", isolation_level=None)
    old.executescript(AUDIO_SCHEMA)                                                      # its CREATE IF NOT EXISTS
    ids = {r[0] for r in old.execute("SELECT id FROM audio")}
    assert ids == {"FG1-new"}
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


# -- Settings > System > Audio retention ------------------------------------------------------------------------------

@pytest.fixture
def hub_fs(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_AUDIO_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("SIZEUP_AUDIO_BUCKET", raising=False)
    app = make_app(hub_cfg(tmp_path, gis_layers=()))
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
    assert app.audio.db.execute("SELECT channel, talkgroup, stored FROM audio WHERE id = 'TG-a'").fetchone() == \
        ("Fire/EMS 15", 307, "store")
    for bad in ({"key": "_other", "value": 3}, {"key": "tg:abc", "value": 3}, {"key": "FG1", "value": 0.1},
                {"key": "FG1", "value": 366}, {"key": "FG1", "value": True}, {"key": "FG1", "value": "7"},
                {"key": "", "value": 3}, {"key": " FG1", "value": 3}):
        with pytest.raises(CadError):
            ed.post("audio_retention", {"action": "set", "operator": "Admin", **bad})
    for key in ("_default", "FG1", "tg:307"):
        ed.post("audio_retention", {"action": "set", "key": key, "value": 30, "operator": "Admin"})
    assert app._audio_retention(now) == 0                      # 30 days: both kept
    page = ed.page()["audio_retention"]
    assert page["default"] == {"value": 30.0, "default": 0.5, "edited": True}
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
    assert audit[0] == ("system", "audio_retention/FG1", '30 d', 'default')
    assert ("system", "audio_retention/tg:307", '30 d', '2 d') in audit and len(audit) == 6
    with pytest.raises(CadError):
        ed.post("audio_retention", {"action": "set", "key": "FG1", "value": 1, "operator": "x"} | {"action": "wipe"})


def test_an_import_with_a_bad_retention_is_refused(hub_fs):
    ed = hub_fs.cad.settings
    good = ed.export()
    good["overrides"]["audio_retention"] = {"FG1": 14}
    assert ed.post("import", {"action": "dry_run", "settings": good, "operator": "Admin"})["ok"]
    bad = ed.export()
    bad["overrides"]["audio_retention"] = {"FG1": 9999}
    with pytest.raises(CadError, match="audio_retention"):
        ed.post("import", {"action": "dry_run", "settings": bad, "operator": "Admin"})


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


# -- playback: through the hub only -----------------------------------------------------------------------------------

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
