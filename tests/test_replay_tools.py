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


def test_push_checks_the_cache_first(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    monkeypatch.setattr(replay_sync.subprocess, "run", lambda *a, **k: pytest.fail("ran aws"))
    clips = [{"id": "Dispatch-1794066000000", "channel": "Dispatch", "at": 1, "bytes": 1, "file": "clips/a.wav"}]
    make_set(tmp_path, "audio", "x", {"clips.json": json.dumps(clips).encode(), "clips/a.wav": b"RIFF"})
    make_set(tmp_path, "camera", "y", {"hub.sqlite": b"SQLite"})
    problems = replay_sync.check(tmp_path)
    assert any("only airband clips" in p for p in problems) and any("never shared" in p for p in problems)
    assert replay_sync.main(["push", "--write-to-s3"]) == 1


def test_pull_only_reads_from_s3(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(tmp_path))
    ran = []
    monkeypatch.setattr(replay_sync.subprocess, "run",
                        lambda args, **k: ran.append(args) or type("R", (), {"returncode": 0})())
    assert replay_sync.main(["--s3", "s3://b/p", "pull"]) == 0
    assert ran == [["aws", "s3", "sync", "s3://b/p/", f"{tmp_path}/", "--only-show-errors", "--exact-timestamps"]]
