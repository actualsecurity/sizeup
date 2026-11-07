"""The replay tooling itself (GitHub #283), on synthetic data only: the LTX reader (tools/ltx.py), the sync tool's
refusals and checks (tools/replay_sync.py), the cache and the skip (tests/replay.py). No network, no S3."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

from tests import replay

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import capture_fixture  # noqa: E402
import ltx  # noqa: E402
import replay_sync  # noqa: E402


def test_lz4_block_literals_and_an_overlapping_match():
    # "abc" then a match of 9 at offset 3 (overlapping: abcabcabc), then the last literals "xyz"
    block = bytes([0x35]) + b"abc" + struct.pack("<H", 3) + bytes([0x30]) + b"xyz"
    assert ltx.lz4_block(block, 15) == b"abcabcabcabcxyz"
    with pytest.raises(ltx.LtxError):
        ltx.lz4_block(block, 16)


def ltx_file(pages: dict[int, bytes], commit: int, lo: int, hi: int, page_size: int = 512) -> bytes:
    head = b"LTX1" + struct.pack(">IIIQQqQqqIIQ", 2, page_size, commit, lo, hi, 0, 0, 0, 0, 0, 0, 0)
    out = head.ljust(ltx.HEADER, b"\0")
    for pgno, data in pages.items():
        out += struct.pack(">IHI", pgno, 0, len(data)) + data
    return out + struct.pack(">IH", 0, 0) + b"\0" * 24


def test_ltx_header_and_pages():
    b = ltx_file({1: b"A" * 512, 3: b"C" * 512}, commit=3, lo=5, hi=7)
    h = ltx.header(b)
    assert (h["page_size"], h["commit"], h["min_txid"], h["max_txid"]) == (512, 3, 5, 7)
    assert [(p, ltx.page_data(f, d, 512)[:1]) for p, f, d in ltx.pages(b)] == [(1, b"A"), (3, b"C")]
    with pytest.raises(ltx.LtxError):
        ltx.header(b"SQLite format 3\0" + b"\0" * 100)


def test_ltx_restore_plan_the_snapshot_then_the_furthest_files():
    files = [(9, 1, 100, "0009/a"), (9, 1, 50, "0009/old"), (3, 101, 200, "0003/b"), (1, 101, 120, "0001/x"),
             (0, 201, 201, "0000/c"), (0, 202, 202, "0000/d"), (1, 201, 202, "0001/cd"), (0, 204, 204, "0000/gap")]
    assert [f[3] for f in ltx.plan(files)] == ["0009/a", "0003/b", "0001/cd"]


def test_ltx_restore_applies_the_newest_copy_of_each_page(tmp_path, monkeypatch):
    blobs = {"0009/0000000000000001-0000000000000002.ltx": ltx_file({1: b"1" * 512, 2: b"2" * 512, 3: b"3" * 512}, 3, 1, 2),
             "0000/0000000000000003-0000000000000003.ltx": ltx_file({2: b"N" * 512}, 2, 3, 3)}
    calls = []

    def fake_aws(*args):
        calls.append(args)
        if args[0] == "ls":
            return "".join(f"2026-11-07 10:00:00 512 db/x.sqlite/{k}\n" for k in blobs)
        assert args[0] == "cp" and args[2].startswith("s3://bucket/db/x.sqlite/"), args   # downloads only
        Path(args[3]).write_bytes(blobs[args[2].split("x.sqlite/")[1]])
        return ""
    monkeypatch.setattr(ltx, "_aws", fake_aws)
    info = ltx.restore("s3://bucket/db/x.sqlite", tmp_path / "out.db", work=tmp_path / "w", log=lambda m: None)
    assert info["txid"] == 3 and info["pages"] == 2
    assert (tmp_path / "out.db").read_bytes() == b"1" * 512 + b"N" * 512      # page 2 the newest, cut to 2 pages
    assert all(c[0] in ("ls", "cp") for c in calls)


def make_set(root: Path, kind: str, name: str, files: dict[str, bytes]) -> Path:
    d = root / kind / name
    for rel, body in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(body)
    replay.write_manifest(d, kind, source="test", how="test", captured_from=0, captured_to=1,
                          files={k: "test" for k in files})
    return d


def test_find_sets_only_whole_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    assert replay.cache_dir() == tmp_path and replay.find_sets("adsb") == []
    make_set(tmp_path, "adsb", "a", {"snapshots.jsonl.gz": b""})
    d = make_set(tmp_path, "adsb", "b", {"snapshots.jsonl.gz": b""})
    (d / "snapshots.jsonl.gz").unlink()
    assert [s.path.name for s in replay.find_sets("adsb")] == ["a"]


def test_push_refuses_without_the_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    monkeypatch.setattr(replay_sync.subprocess, "run", lambda *a, **k: pytest.fail("ran aws"))
    assert replay_sync.main(["push"]) == 2 and replay_sync.main(["push", "--dryrun"]) == 2
    assert "refused" in capsys.readouterr().err


WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 28
TS = b"\x47" + b"\0" * 187
AIR = {"id": "HPNTower-1794066000000", "channel": "HPNTower", "at": 1, "bytes": len(WAV), "file": "clips/a.wav"}


def audio_set(root: Path, name: str = "ok", clips=None, files=None) -> Path:
    clips = [AIR] if clips is None else clips
    return make_set(root, "audio", name, {"clips.json": json.dumps(clips).encode(), "clips/a.wav": WAV, **(files or {})})


def ran_aws(monkeypatch) -> list:
    ran = []
    monkeypatch.setattr(replay_sync.subprocess, "run",
                        lambda args, **k: ran.append(args) or type("R", (), {"returncode": 0})())
    return ran


def test_a_good_cache_passes_and_is_pushed_set_by_set_without_following_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    audio_set(tmp_path)
    make_set(tmp_path, "camera", "c", {"streams/1/a.ts": TS, "streams/1/playlist.m3u8": b"#EXTM3U\n", "x.xml": b"<a/>"})
    assert replay_sync.check(tmp_path) == []
    ran = ran_aws(monkeypatch)
    assert replay_sync.main(["push", "--write-to-s3"]) == 0
    dest = replay_sync.DEFAULT_S3
    assert [(a[3], a[4]) for a in ran] == [(f"{tmp_path}/audio/ok/", f"{dest}/audio/ok/"),
                                          (f"{tmp_path}/camera/c/", f"{dest}/camera/c/")]
    assert all("--no-follow-symlinks" in a and a[a.index("--sse") + 1] == "AES256" for a in ran)


def test_check_files_outside_a_set(tmp_path):
    audio_set(tmp_path)
    (tmp_path / "hub.sqlite").write_bytes(b"SQLite format 3\0")
    (tmp_path / "audio" / "Dispatch-3.wav").write_bytes(WAV)
    (tmp_path / "other").mkdir()
    problems = replay_sync.check(tmp_path)
    assert any(p.startswith("hub.sqlite: outside") for p in problems)
    assert any(p.startswith("audio/Dispatch-3.wav: outside") for p in problems)
    assert any(p.startswith("other/: not a known kind") for p in problems)


def test_check_audio_only_airband_clips_from_clips_json(tmp_path):
    # a WAV in the manifest but not in clips.json
    audio_set(tmp_path, "extra", files={"clips/Dispatch-1.wav": WAV})
    # a clips.json entry that isn't airband
    audio_set(tmp_path, "dispatch", clips=[dict(AIR, id="Dispatch-1794066000000", channel="Dispatch")])
    # an extra key (a transcript) in clips.json
    audio_set(tmp_path, "text", clips=[dict(AIR, text="words")])
    problems = replay_sync.check(tmp_path)
    assert "audio/extra: clips/Dispatch-1.wav: not an airband clip in clips.json" in problems
    assert any(p.startswith("audio/dispatch: 'Dispatch-1794066000000': only airband clips") for p in problems)
    assert "audio/dispatch: clips/a.wav: not an airband clip in clips.json" in problems
    assert any(p.startswith("audio/text: 'HPNTower-") for p in problems)


def test_check_sniffs_content(tmp_path):
    audio_set(tmp_path, "db", files={"clips/b.wav": b"SQLite format 3\0" + b"\0" * 100})
    audio_set(tmp_path, "ltx", files={"clips/c.wav": b"LTX1" + b"\0" * 100})
    make_set(tmp_path, "camera", "ts", {"a.ts": b"\0" * 188, "b.json": b"{nope", "c.sqlite": b"x"})
    problems = replay_sync.check(tmp_path)
    assert "audio/db: clips/b.wav: a SQLite database" in problems
    assert "audio/ltx: clips/c.wav: a Litestream LTX file" in problems
    assert "camera/ts: a.ts: not MPEG-TS (no 0x47 sync byte)" in problems
    assert "camera/ts: b.json: not JSON" in problems
    assert any(p.startswith("camera/ts: c.sqlite: not a camera file") for p in problems)


def test_check_rejects_symlinks(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.wav").write_bytes(WAV)
    root = tmp_path / "cache"
    d = audio_set(root)
    (d / "clips" / "linked").symlink_to(outside, target_is_directory=True)
    (root / "audio" / "linkset").symlink_to(d, target_is_directory=True)
    (d / "clips" / "l.wav").symlink_to(outside / "secret.wav")
    problems = replay_sync.check(root)
    assert "audio/ok/clips/linked: a symlink (never followed, never shared)" in problems
    assert "audio/linkset: a symlink (never followed, never shared)" in problems
    assert "audio/ok/clips/l.wav: a symlink (never followed, never shared)" in problems
    assert not any("secret" in p for p in problems)                  # never looked through


def test_push_checks_the_cache_first(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    monkeypatch.setattr(replay_sync.subprocess, "run", lambda *a, **k: pytest.fail("ran aws"))
    audio_set(tmp_path, clips=[dict(AIR, id="Dispatch-1794066000000")])
    assert replay_sync.main(["push", "--write-to-s3"]) == 1


@pytest.mark.parametrize("url", [
    "s3://sizeup-hub-backups-504804195561/sizeup-hub", "s3://sizeup-hub-backups-504804195561/sizeup-hub/replay-fixtures/v1",
    "s3://sizeup-hub-backups-504804195561/replay-fixtures", "s3://sizeup-hub-backups-504804195561/replay-fixtures/v1/../x",
    "s3://sizeup-hub-backups-504804195561/v1", "s3://sizeup-replay-fixtures-504804195561/sizeup-hub",
    "s3://sizeup-replay-fixtures-504804195561", "s3://another-bucket/replay-fixtures/v1", "/tmp/x"])
def test_push_and_pull_only_to_a_replay_fixtures_location(tmp_path, monkeypatch, url):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    monkeypatch.setattr(replay_sync.subprocess, "run", lambda *a, **k: pytest.fail("ran aws"))
    assert replay_sync.main(["--s3", url, "push", "--write-to-s3"]) == 2
    assert replay_sync.main(["--s3", url, "pull"]) == 2
    monkeypatch.setenv("SIZEUP_REPLAY_S3", url)
    assert replay_sync.main(["pull"]) == 2


def test_pull_only_reads_from_s3(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    ran = ran_aws(monkeypatch)
    assert replay_sync.main(["pull"]) == 0
    assert replay_sync.main(["--s3", "s3://sizeup-hub-backups-504804195561/replay-fixtures/v2", "pull"]) == 0
    assert ran == [["aws", "s3", "sync", f"{replay_sync.DEFAULT_S3}/", f"{tmp_path}/", "--only-show-errors",
                    "--exact-timestamps"],
                   ["aws", "s3", "sync", "s3://sizeup-hub-backups-504804195561/replay-fixtures/v2/", f"{tmp_path}/",
                    "--only-show-errors", "--exact-timestamps"]]
    assert replay_sync.DEFAULT_S3 == "s3://sizeup-replay-fixtures-504804195561/v1"


# ---- capture_fixture: names from a source, 511NY's limit, the LTX downloads ----------------------------------------


@pytest.mark.parametrize("name", ["../x.ts", "a/b.ts", ".hidden.ts", "..", "x..y.ts", "", "/etc/passwd", "a\\b.ts"])
def test_names_from_a_source_never_escape_the_set(name):
    with pytest.raises(ValueError):
        capture_fixture.safe_name(name)


def test_plain_names_from_a_source_pass():
    for name in ("media_w999076166_7748.ts", "chunklist_w1.m3u8", "36003", "R8_039"):
        assert capture_fixture.safe_name(name) == name


def test_the_511ny_stamp_keeps_captures_5_minutes_apart(tmp_path):
    stamp = tmp_path / ".ny511-last-request"
    assert capture_fixture.ny511_wait(stamp, now=1000.0) == 0.0             # never asked
    stamp.write_text("1000\n")
    assert capture_fixture.ny511_wait(stamp, now=1100.0) == 200.0
    assert capture_fixture.ny511_wait(stamp, now=1300.0) == 0.0


def test_a_second_camera_capture_within_5_minutes_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    (tmp_path / capture_fixture.NY511_STAMP).write_text(f"{__import__('time').time():.0f}\n")
    monkeypatch.setattr(capture_fixture, "keychain", lambda s: pytest.fail("asked the Keychain"))
    with pytest.raises(SystemExit) as e:
        capture_fixture.main(["camera"])
    assert "5 minutes" in str(e.value)


def test_ltx_downloads_never_inside_the_repo(monkeypatch):
    import ltx as ltx_mod
    monkeypatch.setattr(ltx_mod, "restore", lambda *a, **k: pytest.fail("restored"))
    with pytest.raises(SystemExit) as e:
        capture_fixture.main(["audio", "--ltx-cache", str(capture_fixture.ROOT / "ltx-here")])
    assert "inside the repo" in str(e.value)


# ---- ltx: malformed input is an LtxError, bounded ------------------------------------------------------------------


def test_lz4_malformed_blocks_are_ltx_errors():
    with pytest.raises(ltx.LtxError):
        ltx.lz4_block(bytes([0xF0]), 4096)                          # a literal length that runs off the end
    with pytest.raises(ltx.LtxError):
        ltx.lz4_block(bytes([0x10]) + b"a" + struct.pack("<H", 1) + bytes([0x0F, 255, 255, 255]), 64)   # too long
    with pytest.raises(ltx.LtxError):
        ltx.lz4_block(bytes([0x50]) + b"abcde", 3)                      # more literals than the page
    with pytest.raises(ltx.LtxError):
        ltx.lz4_block(bytes([0x10]) + b"a" + struct.pack("<H", 5) + bytes([0x00]), 64)    # an offset before the start


@pytest.mark.parametrize("page_size,commit", [(1000, 3), (256, 3), (131072, 3), (4096, 0), (4096, 1 << 30)])
def test_ltx_header_bounds(page_size, commit):
    with pytest.raises(ltx.LtxError):
        ltx.header(ltx_file({}, commit=commit, lo=1, hi=1, page_size=page_size))


def test_ltx_truncated_page_header():
    b = ltx_file({1: b"A" * 512}, commit=1, lo=1, hi=1)
    with pytest.raises(ltx.LtxError):
        list(ltx.pages(b[:ltx.HEADER + 8]))


def test_ltx_plan_warns_on_a_gap():
    said = []
    chain = ltx.plan([(9, 1, 10, "a"), (0, 11, 11, "b"), (0, 13, 13, "c")], said.append)
    assert [f[3] for f in chain] == ["a", "b"]
    assert said and "gap after TXID 0xb" in said[0] and "0xd" in said[0]
