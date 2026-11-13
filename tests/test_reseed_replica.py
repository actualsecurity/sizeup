"""tools/reseed_replica.py: an operator's reseed of a replica from a verified file (docs/cloud.md "Reseeding a
replica"). Read-only unless --apply; never run by the hub; keeps the old history restorable; holds the lock while it
works and releases it with the new position. With a local file replica and the lock in a fake S3; Litestream
>= 0.5.18 on PATH for the end-to-end ones. Every row here is invented."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from sizeup.handoff import Env
from tests.test_handoff import FakeS3, lock_for

ROOT = Path(__file__).resolve().parent.parent


def tool():
    spec = importlib.util.spec_from_file_location("reseed_replica", ROOT / "tools" / "reseed_replica.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ls_version() -> tuple:
    if shutil.which("litestream") is None:
        return ()
    out = subprocess.run(["litestream", "version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else ()


needs_ls = pytest.mark.skipif(_ls_version() < (0, 5, 18), reason="needs litestream >= 0.5.18 on PATH")


def test_nothing_runs_the_reseed_by_itself():
    for path in [ROOT / "sizeup" / "handoff.py", ROOT / "sizeup" / "app.py", ROOT / "deploy" / "hub" / "entrypoint.sh",
                 *ROOT.glob(".github/workflows/*.yml"), *ROOT.glob("deploy/aws/*.json")]:
        text = path.read_text()
        assert "reseed_replica" not in text and "reseed-replica" not in text, path


def test_a_file_with_a_wal_beside_it_or_a_bad_integrity_check_is_refused(tmp_path, capsys):
    t = tool()
    f = tmp_path / "cad.sqlite"
    c = sqlite3.connect(f)
    c.execute("CREATE TABLE radio_traffic (id TEXT PRIMARY KEY, text TEXT)")
    c.commit()
    c.close()
    Path(f"{f}-wal").write_bytes(b"x")
    with pytest.raises(t.Refused, match="cad.sqlite-wal beside cad.sqlite"):
        t.check_file(f)
    Path(f"{f}-wal").unlink()
    assert t.check_file(f)["tables"] == 1
    data = bytearray(f.read_bytes())
    data[100:4096] = b"\xff" * (4096 - 100)                       # the schema page wrecked
    f.write_bytes(bytes(data))
    with pytest.raises((t.Refused, sqlite3.DatabaseError)):
        t.check_file(f)


def _replica(tmp_path: Path) -> tuple[Path, Path, int]:
    """A hub's replica of cad.sqlite (a file replica): rows 0..999, then 1000..1999. Its last position."""
    live = tmp_path / "hub" / "cad.sqlite"
    live.parent.mkdir(parents=True)
    rep = tmp_path / "replica"
    cfg = tmp_path / "hub" / "ls.yml"
    sock = Path("/tmp") / f"rs{time.monotonic_ns() % 10**9}.sock"
    cfg.write_text(f"socket:\n  enabled: true\n  path: {sock}\nlogging:\n  stderr: true\ndbs:\n  - path: {live}\n"
                   f"    replica:\n      type: file\n      path: {rep}/cad.sqlite\n")
    c = sqlite3.connect(live, isolation_level=None)
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("CREATE TABLE radio_traffic (id TEXT PRIMARY KEY, start REAL, text TEXT)")
    c.close()
    env = Env("cfg", live.parent, str(cfg), str(sock), 0)
    ls = env.replicate()
    try:
        c = sqlite3.connect(live, isolation_level=None)
        for part in range(2):
            c.executemany("INSERT INTO radio_traffic VALUES (?, ?, ?)",
                          [(f"fg-{i}", float(i), f"made-up words {i}") for i in range(part * 1000, part * 1000 + 1000)])
            time.sleep(1.2)
        c.close()
        end = time.monotonic() + 30
        while True:
            try:
                n = env.sync("cad", 30)
                break
            except RuntimeError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.2)
    finally:
        ls.stop(30)
        sock.unlink(missing_ok=True)
    return rep, live, n


def _verified(tmp_path: Path, live: Path) -> Path:
    """The operator's verified file: the hub's, with a row put right (a stand-in for a repaired copy)."""
    f = tmp_path / "verified" / "cad.sqlite"
    f.parent.mkdir()
    shutil.copyfile(live, f)
    c = sqlite3.connect(f, isolation_level=None)
    c.execute("UPDATE radio_traffic SET text = 'made-up words, repaired' WHERE id = 'fg-7'")
    c.execute("PRAGMA journal_mode = DELETE")                   # a plain file: no WAL beside it
    c.close()
    return f


def _listing(d: Path) -> dict:
    return {str(p.relative_to(d)): p.stat().st_mtime_ns for p in d.rglob("*") if p.is_file()}


def _held_then_released(s3: FakeS3, n: int) -> None:
    hub = lock_for(s3, "hub-task")
    hub.try_take()
    hub.release({"cad": f"{n:016x}", "hub": "0000000000000009", "audio": "0000000000000004"}, clean=True)


@needs_ls
def test_read_only_by_default_prints_the_plan_and_writes_nothing(tmp_path, capsys):
    rep, live, n = _replica(tmp_path)
    f = _verified(tmp_path, live)
    s3 = FakeS3()
    _held_then_released(s3, n)
    before, lock_before = _listing(rep), s3.doc()
    code = tool().main(["--db", "cad", "--file", str(f), "--file-replica", str(rep), "--scratch",
                        str(tmp_path / "scratch")], lock=lock_for(s3, "operator"))
    out = capsys.readouterr().out
    assert code == 0
    assert "integrity_check ok" in out and f"last position {n}" in out
    assert f"ltx/0/{n + 1:016x}-{n + 1:016x}.ltx" in out and "nothing deleted" in out
    assert "read-only: nothing written" in out
    assert _listing(rep) == before and s3.doc() == lock_before            # nothing written anywhere


@needs_ls
def test_a_held_lock_is_refused(tmp_path, capsys):
    rep, live, n = _replica(tmp_path)
    s3 = FakeS3()
    lock_for(s3, "hub-task").try_take()                                    # a hub is running
    code = tool().main(["--db", "cad", "--file", str(_verified(tmp_path, live)), "--file-replica", str(rep),
                        "--scratch", str(tmp_path / "scratch"), "--apply", "--yes"], lock=lock_for(s3, "operator"))
    assert code == 2 and "REFUSED: the lock is held" in capsys.readouterr().out
    assert s3.doc()["holder"] == "hub-task"


@needs_ls
def test_apply_reseeds_keeps_the_old_history_and_releases_the_new_position(tmp_path, capsys):
    rep, live, n = _replica(tmp_path)
    f = _verified(tmp_path, live)
    s3 = FakeS3()
    _held_then_released(s3, n)
    before = _listing(rep)
    code = tool().main(["--db", "cad", "--file", str(f), "--file-replica", str(rep), "--scratch",
                        str(tmp_path / "scratch"), "--apply"], lock=lock_for(s3, "operator"), confirm=lambda q: "cad")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "verified: the replica restores to the file's content" in out
    after = _listing(rep)
    assert all(k in after and after[k] == v for k, v in before.items())   # nothing deleted or rewritten
    doc = s3.doc()
    assert doc["state"] == "released" and doc["clean"] is True and doc["holder"] == "operator"
    new = int(doc["positions"]["cad"], 16)
    assert new > n and doc["positions"]["hub"] == "0000000000000009"     # the others as the hub released them
    cfg = tmp_path / "scratch" / "litestream.yml"
    for want, at in (("made-up words, repaired", None), ("made-up words 7", n)):
        out_db = tmp_path / f"r{at}.sqlite"
        r = subprocess.run(["litestream", "restore", "-config", str(cfg), *(["-txid", f"{at:016x}"] if at else []),
                            "-o", str(out_db), str(tmp_path / "scratch" / "cad.sqlite")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-300:]
        c = sqlite3.connect(out_db)
        assert c.execute("SELECT text FROM radio_traffic WHERE id = 'fg-7'").fetchone()[0] == want
        assert c.execute("SELECT count(*) FROM radio_traffic").fetchone()[0] == 2000
    # the next hub's takeover reads that position and is not behind the release
    env = Env("cfg", tmp_path / "scratch", str(cfg), "sock", 0)
    assert env.plan("cad") == new


@needs_ls
def test_apply_asks_first(tmp_path, capsys):
    rep, live, n = _replica(tmp_path)
    s3 = FakeS3()
    _held_then_released(s3, n)
    before = _listing(rep)
    code = tool().main(["--db", "cad", "--file", str(_verified(tmp_path, live)), "--file-replica", str(rep),
                        "--scratch", str(tmp_path / "scratch"), "--apply"], lock=lock_for(s3, "operator"),
                       confirm=lambda q: "no")
    assert code == 1 and "not confirmed: nothing written" in capsys.readouterr().out
    assert _listing(rep) == before and s3.doc()["holder"] == "hub-task"
