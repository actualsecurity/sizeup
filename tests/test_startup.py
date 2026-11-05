"""#251: the hub serves the CAD before its map layers and its audio are loaded; audio retention; cachesync uploads
only what changed; /healthz. Real HTTP on ephemeral ports, fakes with delays for the slow parts; nothing reaches
AWS (Litestream is a fake here) and no production data is touched: every database is a fresh one under tmp_path."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sizeup import gis as gis_mod
from sizeup.app import init_state, make_app, make_server
from sizeup.cloud import lateaudio
from sizeup.cloud.hubstore import AudioDb
from sizeup.cloud.lateaudio import LateAudio
from tests.test_cloud import ROOT, hub_cfg
from tests.test_gis import BY_KEY
from tests.test_radios import Clock, _event_with, cad, tx

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url: str):
    """(status, headers, body)."""
    try:
        with DIRECT.open(url, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def until(fn, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


class Gate:
    """A slow first load: blocks until opened (or 10 s)."""

    def __init__(self):
        self.entered, self.open = threading.Event(), threading.Event()

    def __call__(self, *a, **k):
        self.entered.set()
        assert self.open.wait(10)


def serve(app):
    server = make_server(app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.httpd.server_address[:2]
    return server, f"http://127.0.0.1:{port}"


# -- serve first ------------------------------------------------------------------------------------------------------


def test_the_hub_serves_the_cad_and_healthz_before_the_gis_layers_load(tmp_path, monkeypatch):
    gate = Gate()
    real = gis_mod.Gis._load

    def slow_load(self):
        gate()
        real(self)
    monkeypatch.setattr(gis_mod.Gis, "_load", slow_load)
    monkeypatch.setattr(gis_mod.Gis, "_run", lambda self: None)          # no county fetches in a test
    app = make_app(hub_cfg(tmp_path, gis_layers=(BY_KEY["hydrants"],)))
    server, url = serve(app)
    try:
        t0 = time.monotonic()
        app.start()
        assert time.monotonic() - t0 < 2.0                                  # start() never waits for the layers
        assert gate.entered.wait(5)                                         # the loader is inside the GIS load now
        code, _, body = get(url + "/healthz")
        assert code == 200
        h = json.loads(body)
        assert h["ok"] and h["serving"] and h["loading"] == ["map layers"]
        assert get(url + "/api/cad/state")[0] == 200                       # the CAD works meanwhile
        code, _, body = get(url + "/api/cad/gis")
        assert code == 200 and all(l["loading"] for l in json.loads(body)["layers"])
        code, hdr, body = get(url + "/api/cad/gis/hydrants")
        assert code == 503 and hdr.get("Retry-After") == "5" and json.loads(body)["loading"] is True
        assert "no-store" in hdr.get("Cache-Control", "")
        assert not app.layers_ready.is_set() and app.gis._thread is None    # the providers start after the load
        gate.open.set()
        assert app.layers_ready.wait(10)
        assert until(lambda: json.loads(get(url + "/healthz")[2])["loading"] == [])
        assert until(lambda: app.gis._thread is not None)
        code, _, body = get(url + "/api/cad/gis/hydrants")
        assert code == 200                                                  # (empty here: nothing cached), not loading
    finally:
        gate.open.set()
        server.shutdown()
        app.stop()


def test_stop_layers_while_they_load_leaves_the_providers_stopped(tmp_path, monkeypatch):
    gate = Gate()
    monkeypatch.setattr(gis_mod.Gis, "_load", lambda self: gate())
    app = make_app(hub_cfg(tmp_path, gis_layers=(BY_KEY["hydrants"],)))
    try:
        app.start_layers()
        assert gate.entered.wait(5)
        app.stop_layers()
        gate.open.set()
        assert app.layers_ready.wait(10)
        time.sleep(0.1)
        assert app.gis._thread is None and app.weather._thread is None
    finally:
        gate.open.set()
        app.stop()


def test_init_state_makes_only_the_databases_and_leaves_audio_to_the_app(tmp_path, monkeypatch):
    cfg = hub_cfg(tmp_path)
    monkeypatch.setenv("SIZEUP_AUDIO_LITESTREAM", "/nonexistent/litestream-audio.yml")
    made = init_state(cfg)
    names = sorted(Path(p).name for p in made)
    assert names == ["cad.sqlite", "hub.sqlite"]
    assert not (Path(cfg.cad_db).parent / "audio.sqlite").exists()        # else its restore would be skipped
    assert not (Path(cfg.cad_db).parent / "gis").exists()                  # no app built: no layers read
    monkeypatch.delenv("SIZEUP_AUDIO_LITESTREAM")
    assert sorted(Path(p).name for p in init_state(cfg)) == ["audio.sqlite", "cad.sqlite", "hub.sqlite"]


# -- audio after the CAD -----------------------------------------------------------------------------------------------


class FakeLitestream:
    """`litestream restore` (blocks until released; can fail first) and `litestream replicate` (a fake process)."""

    def __init__(self, fail: int = 0, path: Path | None = None, restored: bytes | None = None):
        self.calls: list[list[str]] = []
        self.release = threading.Event()
        self.fail, self.path = fail, path
        self.procs: list = []

    def run(self, cmd):
        self.calls.append(cmd)
        assert self.release.wait(10)
        if self.fail:
            self.fail -= 1
            return subprocess.CompletedProcess(cmd, 1, "", "cannot fetch generations: AccessDenied")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def popen(self, cmd):
        self.calls.append(cmd)
        p = FakeProc()
        self.procs.append(p)
        return p


class FakeProc:
    def __init__(self):
        self.signals: list[int] = []
        self.code = None

    def poll(self):
        return self.code

    def send_signal(self, s):
        self.signals.append(s)
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def late(tmp_path, ls, **kw):
    path = tmp_path / "audio.sqlite"
    return LateAudio(lambda: AudioDb(path), path, "/app/deploy/hub/litestream-audio.yml", run=ls.run, popen=ls.popen,
                     log=lambda m: None, **kw)


def test_audio_is_restored_then_replicated_and_what_came_meanwhile_is_kept(tmp_path):
    ls = FakeLitestream()
    a = late(tmp_path, ls).start()
    assert until(lambda: ls.calls)
    assert ls.calls[0][:2] == ["litestream", "restore"] and "-if-db-not-exists" in ls.calls[0] and "-if-replica-exists" in ls.calls[0]
    assert not a.ready.is_set() and a.db is None
    a.put("c1", b"RIFF-one", "clip", at=100.0)                    # the station's clip while audio.sqlite is restored
    assert a.get("c1") == b"RIFF-one" and a.has("c1") and a.has("anything")   # ▶ shows; the clip route says loading
    assert a.prune_clips(1e12) == 0 and a.bytes() == 0            # retention waits
    assert len(ls.calls) == 1                                     # no replication before the restore is over
    ls.release.set()
    assert a.ready.wait(5)
    assert [c[:2] for c in ls.calls] == [["litestream", "restore"], ["litestream", "replicate"]]
    assert a.get("c1") == b"RIFF-one" and a.has("c1") and not a.has("nope")
    assert AudioDb(tmp_path / "audio.sqlite").get("c1") == b"RIFF-one"     # written to the file, after the restore
    a.stop()
    assert ls.procs[0].signals                                    # SIGTERM: Litestream's own final sync


def test_a_failed_audio_restore_is_retried_and_nothing_is_made_or_replicated_meanwhile(tmp_path, monkeypatch):
    monkeypatch.setattr(lateaudio, "RETRY_S", (0.05,))
    ls = FakeLitestream(fail=2)
    ls.release.set()
    a = late(tmp_path, ls).start()
    assert a.ready.wait(5)
    assert [c[1] for c in ls.calls] == ["restore", "restore", "restore", "replicate"]


def test_the_standby_restores_audio_but_does_not_replicate_it(tmp_path):
    ls = FakeLitestream()
    ls.release.set()
    a = late(tmp_path, ls, replicate=False).start()
    assert a.ready.wait(5)
    assert [c[1] for c in ls.calls] == ["restore"]


def test_the_pending_audio_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(lateaudio, "PENDING_MAX", 10)
    a = late(tmp_path, FakeLitestream())
    a.put("a", b"123456")
    a.put("b", b"123456")
    assert a.get("a") is None and a.get("b") == b"123456"


def test_the_hub_says_audio_loading_until_audio_sqlite_is_back(tmp_path, monkeypatch):
    ls = FakeLitestream()
    monkeypatch.setenv("SIZEUP_AUDIO_LITESTREAM", "/app/deploy/hub/litestream-audio.yml")
    monkeypatch.setattr(lateaudio.subprocess, "run", lambda cmd, **k: ls.run(cmd))
    monkeypatch.setattr(lateaudio.subprocess, "Popen", lambda cmd, **k: ls.popen(cmd))
    monkeypatch.setattr(gis_mod.Gis, "_run", lambda self: None)
    app = make_app(hub_cfg(tmp_path, gis_layers=()))
    server, url = serve(app)
    try:
        app.start()
        assert until(lambda: ls.calls)
        assert get(url + "/api/cad/state")[0] == 200                     # the CAD, without audio
        code, hdr, body = get(url + "/api/audio/p-1.wav")
        assert code == 503 and body == b"audio loading\n" and hdr.get("Retry-After") == "5"
        assert "audio" in json.loads(get(url + "/healthz")[2])["loading"]
        app.audio.put("p-2", b"RIFFxxxx", "clip")
        assert get(url + "/api/audio/p-2.wav")[0] == 200                 # what came meanwhile plays
        ls.release.set()
        assert app.audio.ready.wait(5)
        assert get(url + "/api/audio/p-1.wav")[0] == 404                 # ready: a clip that isn't there is a 404
        assert get(url + "/api/audio/p-2.wav")[0] == 200
        assert "audio" not in json.loads(get(url + "/healthz")[2])["loading"]
    finally:
        ls.release.set()
        server.shutdown()
        app.stop()
    assert ls.procs and ls.procs[0].signals                              # the audio replicator stopped with the app


# -- audio retention ----------------------------------------------------------------------------------------------------


def audio_db(tmp_path, clips: dict[str, float], size: int = 100, cap: int = 10**9) -> AudioDb:
    db = AudioDb(tmp_path / "a.sqlite", cap)
    for cid, at in clips.items():
        db.put(cid, b"x" * size, "clip", at=at)
    return db


def test_retention_keeps_records_longer_and_never_touches_dispatch_audio(tmp_path):
    now = 1_800_000_000.0
    h, d = 3600, 86400
    db = audio_db(tmp_path, {"loose-new": now - 1 * h, "loose-old": now - 13 * h, "event-old": now - 30 * d,
                             "event-ancient": now - 400 * d})
    db.put("dsp", b"wav", "dispatch", at=now - 900 * d)
    kept = lambda ids: {i for i in ids if i.startswith("event")}
    n = db.prune_clips(now - 12 * h, kept=kept, kept_older_than=now - 365 * d)
    assert n == 2
    assert {r[0] for r in db.db.execute("SELECT id FROM audio")} == {"loose-new", "event-old", "dsp"}
    assert db.prune_clips(now - 12 * h, kept=kept, kept_older_than=None) == 0     # 0 days = forever: none go
    assert db.has("event-old") and not db.has("loose-old")


def test_retention_deletes_in_small_transactions_and_at_most_max_rows(tmp_path):
    now = 1_800_000_000.0
    db = audio_db(tmp_path, {f"c{k:03d}": now - 10 * 86400 - k for k in range(25)})
    begins: list[str] = []
    db.db.set_trace_callback(lambda sql: begins.append(sql) if sql.strip().upper() == "BEGIN" else None)
    assert db.prune_clips(now, batch=10, max_rows=15) == 15
    assert len(begins) == 2                                         # 10 + 5: no long write lock
    assert db.db.execute("SELECT COUNT(*) FROM audio").fetchone()[0] == 10
    begins.clear()
    assert db.prune_clips(now, batch=4) == 10 and len(begins) == 3
    oldest_first = audio_db(tmp_path / "x", {"a": now - 30, "b": now - 20, "c": now - 10})
    assert oldest_first.prune_clips(now, max_rows=1) == 1 and not oldest_first.has("a")


def test_the_size_cap_drops_only_clips_that_are_not_records(tmp_path):
    now = 1_800_000_000.0
    db = audio_db(tmp_path, {"event-1": now - 50, "loose-1": now - 40, "loose-2": now - 30}, size=100, cap=150)
    kept = lambda ids: {i for i in ids if i.startswith("event")}
    assert db.prune_clips(now - 86400, kept=kept) == 2              # nothing is old: the cap, oldest first, not the record
    assert db.has("event-1") and not db.has("loose-1") and not db.has("loose-2")
    db.put("event-2", b"x" * 100, "clip", at=now)
    assert db.prune_clips(now - 86400, kept=kept) == 0 and db.over_cap == 200   # records over the cap: kept, said


def test_a_new_audio_db_vacuums_incrementally_an_old_one_is_left_alone(tmp_path):
    now = 1_800_000_000.0
    db = audio_db(tmp_path, {f"c{k}": now - 10 * 86400 for k in range(50)}, size=20_000)
    assert db.auto_vacuum() == 2
    db.prune_clips(now)
    assert db.free_pages() > 0
    freed = db.incremental_vacuum(max_pages=10_000, step=16)
    assert freed > 0 and db.free_pages() == 0
    old = sqlite3.connect(tmp_path / "old.sqlite")
    old.execute("CREATE TABLE t (x)")
    old.close()
    legacy = AudioDb(tmp_path / "old.sqlite")
    assert legacy.auto_vacuum() == 0 and legacy.incremental_vacuum() == 0      # never a whole VACUUM by itself


def test_records_are_clips_on_an_event_an_alarm_or_an_emergency(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    for tid in ("on-event", "loose", "emerg"):
        c.record_traffic(tx(tid, clock.t))
    with c.store.tx() as db:
        db.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES ('on-event', ?, 'manual', 'x')", (a["id"],))
        db.execute("UPDATE radio_traffic SET emergency = 1 WHERE id = 'emerg'")
    assert c.comms.kept_for_events(["on-event", "loose", "emerg", "unknown"]) == {"on-event", "emerg"}


def test_the_hub_prunes_off_peak_in_batches_and_vacuums_only_then(tmp_path, monkeypatch):
    monkeypatch.setattr(gis_mod.Gis, "_run", lambda self: None)
    app = make_app(hub_cfg(tmp_path, gis_layers=(), audio_prune_batch=5))
    try:
        from datetime import datetime
        from sizeup import tz
        noon = datetime(2026, 11, 4, 12, 0, tzinfo=tz.TZ).timestamp()
        three = datetime(2026, 11, 5, 3, 0, tzinfo=tz.TZ).timestamp()
        assert not app.offpeak(noon) and app.offpeak(three)
        for k in range(80):
            app.audio.put(f"old-{k}", b"x" * 5000, "clip", at=noon - 2 * 86400)
        vac = []
        monkeypatch.setattr(app.audio.db, "incremental_vacuum", lambda **k: vac.append(k) or 0)
        assert app._audio_retention(noon, background=True) == 50          # by day: at most 10 batches a pass
        assert vac == []
        assert app._audio_retention(three, background=True) == 30         # at night: the rest, then the vacuum
        assert len(vac) == 1
    finally:
        app.stop()


def test_retention_config_defaults_and_checks(tmp_path):
    from sizeup.config import ConfigError, load
    p = tmp_path / "c.toml"
    p.write_text("[station]\nid = 't'\n")
    cfg = load(p)
    assert (cfg.audio_keep_hours, cfg.audio_keep_event_days, cfg.audio_prune_batch, cfg.audio_offpeak) == (12.0, 365.0, 200, (2, 5))
    p.write_text("[storage]\naudio_keep_event_days = 0\naudio_prune_batch = 50\naudio_offpeak = [1, 4]\n")
    cfg = load(p)
    assert (cfg.audio_keep_event_days, cfg.audio_prune_batch, cfg.audio_offpeak) == (0.0, 50, (1, 4))
    p.write_text("[storage]\naudio_keep_event_days = -1\n")
    with pytest.raises(ConfigError):
        load(p)


# -- the health check ---------------------------------------------------------------------------------------------------


def test_the_task_health_check_is_quick_and_keeps_its_start_window():
    td = json.loads((ROOT / "deploy/aws/taskdef.json").read_text())
    hc = next(c for c in td["containerDefinitions"] if c["name"] == "hub")["healthCheck"]
    assert (hc["interval"], hc["timeout"], hc["retries"], hc["startPeriod"]) == (10, 5, 3, 300)
    assert "/healthz" in " ".join(hc["command"])


def test_the_entrypoint_restores_cad_and_hub_first_and_leaves_audio_to_the_app():
    sh = (ROOT / "deploy/hub/entrypoint.sh").read_text()
    assert "for db in cad hub; do" in sh and "SIZEUP_AUDIO_LITESTREAM=/app/deploy/hub/litestream-audio.yml" in sh
    assert sh.index("SIZEUP_AUDIO_LITESTREAM=") < sh.index("--init-state")
    main_ls = (ROOT / "deploy/hub/litestream.yml").read_text()
    audio_ls = (ROOT / "deploy/hub/litestream-audio.yml").read_text()
    assert "/audio.sqlite" not in main_ls.split("# audio.sqlite has its own file")[0]
    assert "${LITESTREAM_PATH}/audio.sqlite" in audio_ls                 # the same replica as before


# -- cachesync: only what changed -----------------------------------------------------------------------------------


def test_cachesync_uploads_only_what_changed_across_restarts(tmp_path):
    from tests.test_gis_cache import FakeS3, mirror
    s3 = FakeS3()
    root = tmp_path / "state"
    (root / "gis").mkdir(parents=True)
    (root / "gis" / "hydrants.geojson").write_text('{"features": [1]}')
    (root / "roads511.json").write_text('{"bbox": []}')
    assert mirror(root, s3).scan() == 2
    puts = lambda: [c for c in s3.calls if c[0] == "PUT"]
    assert len(puts()) == 2
    m = mirror(root, s3)                                            # a restart on the same disk: the sha256s kept
    assert m.scan() == 0 and len(puts()) == 2
    (root / "cachesync.json").unlink()                              # a disk without them: S3's ETag says the same
    m = mirror(root, s3)
    m.listing()
    assert m.scan() == 0 and len(puts()) == 2 and m.skipped == 2
    (root / "gis" / "hydrants.geojson").write_text('{"features": [1, 2]}')
    assert mirror(root, s3).scan() == 1 and len(puts()) == 3         # changed: sent
    assert "cachesync.json" not in {p for p in mirror(root, s3).candidates()}


def test_cachesync_restored_copies_are_not_sent_back(tmp_path):
    from tests.test_gis_cache import FakeS3, mirror
    s3 = FakeS3()
    old = tmp_path / "old"
    (old / "gis").mkdir(parents=True)
    (old / "gis" / "hydrants.geojson").write_text('{"features": [1]}')
    mirror(old, s3).scan()
    new = tmp_path / "new"
    m = mirror(new, s3)
    assert m.restore() == 1
    n_puts = len([c for c in s3.calls if c[0] == "PUT"])
    assert mirror(new, s3).scan() == 0                               # (a new process: cachesync.json remembers)
    assert len([c for c in s3.calls if c[0] == "PUT"]) == n_puts
