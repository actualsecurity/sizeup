"""Reseed one of the hub's Litestream replicas from a verified local SQLite file (docs/cloud.md "Reseeding a
replica"). An operator's tool for a replica that no longer restores to a good file (2026-11: every takeover
refused it at quick_check). NEVER run by the hub, its supervisor or any workflow: only by a person, by hand.

    python tools/reseed_replica.py --db cad --file /path/to/verified/cad.sqlite            # read-only: checks, the plan
    python tools/reseed_replica.py --db cad --file /path/to/verified/cad.sqlite --apply    # does it (asks first)

Read-only by default: it checks the file (no -wal / -shm / -journal beside it, a full PRAGMA integrity_check), reads
the lock and the replica's position (`litestream restore -dry-run`), and prints exactly what --apply would write.

--apply: takes the hub's leader lock (so no hub can write meanwhile; the hub must be stopped: desired count 0),
copies the file into a scratch dir, runs `litestream replicate` on that copy until `litestream sync -wait` says it
is in the replica, stops it, restores the replica into the scratch dir and compares it with the file, and releases
the lock with the new position. Litestream (no local state of its own) finds the copy "behind" the replica and
writes ONE NEW full snapshot of it (every page) after the replica's last position: a restore of the latest is then
exactly the file. Nothing is deleted or overwritten by this tool: the old history stays, and `litestream restore
-txid <old position>` still restores the state before. (Litestream applies its usual retention while it runs, as
the hub's own does; with the bucket's versioning anything it removes stays recoverable as a noncurrent version.)

Needs Litestream >= 0.5.18 (its snapshot after a stale WAL position holds every frame: litestream#1547), the
hub's LITESTREAM_BUCKET / LITESTREAM_PATH / LITESTREAM_REGION, and AWS credentials in the environment
(`eval "$(aws configure export-credentials --format env)"`) for both the lock and Litestream.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup import leaderlock  # noqa: E402
from sizeup.handoff import DBS, SIDECARS, last_json, sidecars  # noqa: E402
from sizeup.leaderlock import is_free, txid  # noqa: E402

MIN_LITESTREAM = (0, 5, 18)
SYNC_S = 600.0


class Refused(Exception):
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(path: Path) -> dict:
    """The file to reseed from: a plain SQLite file, nothing beside it, integrity_check ok."""
    if not path.is_file():
        raise Refused(f"{path}: no such file")
    there = sidecars(path)
    if there:
        raise Refused(f"{', '.join(there)} beside {path.name}: checkpoint it into the file first, on a copy "
                      f"(sqlite3 COPY 'PRAGMA wal_checkpoint(TRUNCATE)' with nothing else open), then verify that copy")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        res = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
        pages = con.execute("PRAGMA page_count").fetchone()[0]
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    finally:
        con.close()
        for s in SIDECARS:                     # (a read-only open of a WAL-mode file makes empty ones)
            p = Path(f"{path}{s}")
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
    if res != ["ok"]:
        raise Refused(f"{path.name}: integrity_check: {'; '.join(res[:5])}")
    return {"size": path.stat().st_size, "pages": pages, "page_size": page_size, "sha256": sha256(path),
            "tables": len(tables)}


def litestream_version() -> tuple[int, ...]:
    r = subprocess.run(["litestream", "version"], capture_output=True, text=True, timeout=30)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout + r.stderr)
    if r.returncode != 0 or not m:
        raise Refused(f"litestream version: {(r.stderr or r.stdout).strip()[-200:]}")
    return tuple(int(x) for x in m.groups())


def config_text(scratch: Path, db: str, replica: dict, sock: Path) -> str:
    rep = "".join(f"      {k}: {v}\n" for k, v in replica.items())
    return (f"socket:\n  enabled: true\n  path: {sock}\nlogging:\n  stderr: true\n"
            f"dbs:\n  - path: {scratch / (db + '.sqlite')}\n    replica:\n{rep}")


def replica_position(cfg: Path, scratch: Path, db: str) -> int | None:
    """The replica's last position (None: there is none)."""
    r = subprocess.run(["litestream", "restore", "-dry-run", "-json", "-config", str(cfg), "-o",
                        str(scratch / "plan.sqlite"), str(scratch / f"{db}.sqlite")],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "no matching backup files" in err or "no snapshots available" in err:
            return None
        raise Refused(f"litestream restore -dry-run: {err[-300:]}")
    return txid(last_json(r.stdout).get("max_txid"))


def content_digest(path: Path) -> str:
    """The file's content as SQL (Litestream's own bookkeeping tables left out): equal for a restored copy."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    h = hashlib.sha256()
    try:
        for line in con.iterdump():
            if "_litestream_" not in line:
                h.update(line.encode())
                h.update(b"\n")
    finally:
        con.close()
    return h.hexdigest()


def where(replica: dict) -> str:
    if replica["type"] == "s3":
        return f"s3://{replica['bucket']}/{replica['path']}"
    return replica["path"]


def main(argv: list[str] | None = None, lock=None, confirm=input) -> int:
    ap = argparse.ArgumentParser(prog="reseed_replica.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, choices=DBS)
    ap.add_argument("--file", required=True, type=Path, help="the verified SQLite file to reseed from")
    ap.add_argument("--apply", action="store_true", help="do it (default: read-only, print the plan)")
    ap.add_argument("--yes", action="store_true", help="with --apply: don't ask (an operator's script)")
    ap.add_argument("--file-replica", type=Path, help="a local file replica instead of S3 (rehearsals, tests)")
    ap.add_argument("--scratch", type=Path, help="the scratch dir (default: a new temp dir)")
    args = ap.parse_args(argv)
    try:
        return run(args, lock, confirm)
    except Refused as e:
        say(f"REFUSED: {e}")
        return 2


def run(args, lock, confirm) -> int:
    db = args.db
    if args.file_replica is not None:
        replica = {"type": "file", "path": str(args.file_replica / f"{db}.sqlite")}
    else:
        bucket = os.environ.get("LITESTREAM_BUCKET", "").strip()
        if not bucket:
            raise Refused("LITESTREAM_BUCKET is not set (the hub's backups bucket), nor --file-replica")
        prefix = (os.environ.get("LITESTREAM_PATH", "").strip() or "sizeup-hub").strip("/")
        replica = {"type": "s3", "bucket": bucket, "path": f"{prefix}/{db}.sqlite",
                   "region": os.environ.get("LITESTREAM_REGION", "").strip() or os.environ.get("AWS_REGION", "")}
        if lock is None:
            lock = leaderlock.from_env(log=say)
    if lock is None:
        raise Refused("no lock to hold while reseeding")

    ver = litestream_version()
    if ver < MIN_LITESTREAM:
        raise Refused(f"litestream {'.'.join(map(str, ver))}: needs >= {'.'.join(map(str, MIN_LITESTREAM))}")
    info = check_file(args.file)
    say(f"file      {args.file}: {info['size']} bytes, {info['pages']} pages of {info['page_size']}, "
        f"{info['tables']} tables, integrity_check ok, sha256 {info['sha256']}")

    scratch = args.scratch or Path(tempfile.mkdtemp(prefix=f"reseed-{db}-"))
    scratch.mkdir(parents=True, exist_ok=True)
    cfg = scratch / "litestream.yml"
    sock = Path(tempfile.mkdtemp(prefix="reseed", dir="/tmp")) / "ls.sock"   # (a unix socket path must be short)
    cfg.write_text(config_text(scratch, db, replica, sock))
    have = replica_position(cfg, scratch, db)
    doc, _etag = lock.read()
    free = is_free(doc, time.time())
    say(f"replica   {where(replica)}: last position {have if have is not None else 'none (no replica)'}")
    say(f"lock      {leaderlock.short((doc or {}).get('holder', '')) or 'none'}: {(doc or {}).get('state', 'no lock')}, "
        f"generation {(doc or {}).get('generation')}, positions {(doc or {}).get('positions')}"
        + ("" if free else "  <- HELD: stop the hub first (desired count 0) and wait for it to release"))

    new = (have or 0) + 1
    positions = dict((doc or {}).get("positions") or {})
    positions[db] = f"{new:016x}"
    say()
    say("--apply would, in this order:")
    say(f"  1. take the lock s3 leader.json as {lock.holder} (generation {int((doc or {}).get('generation') or 0) + 1}), "
        "renewed every 10 s while it works: no hub can write meanwhile")
    say(f"  2. copy {args.file} to {scratch / (db + '.sqlite')} (sha256 checked)")
    say(f"  3. run `litestream replicate -config {cfg}`:")
    for line in cfg.read_text().splitlines():
        say(f"       {line}")
    say(f"     which writes NEW objects only under {where(replica)}/: one full snapshot of the file (every page) "
        f"as position {new} (ltx/0/{new:016x}-{new:016x}.ltx), and its usual compaction / snapshot files up to "
        f"it; nothing deleted, the history up to {have} kept (restore -txid {have} gives the state before)")
    say(f"  4. `litestream sync -wait` until position {new} is in the replica, then stop Litestream")
    say(f"  5. restore the replica into {scratch / 'verify.sqlite'}: integrity_check, and its content equal to the file's")
    say(f"  6. release the lock with positions {positions}")
    if not args.apply:
        say()
        say("read-only: nothing written. Run again with --apply to do it.")
        return 0
    if not free:
        raise Refused("the lock is held: stop the hub (desired count 0) and wait for its release")
    if have is None:
        raise Refused("there is no replica to reseed (a first start creates it)")
    if not args.yes and confirm(f"type '{db}' to reseed {where(replica)} from {args.file.name}: ").strip() != db:
        say("not confirmed: nothing written")
        return 1
    return apply(args, db, scratch, cfg, sock, lock, info, have)


def apply(args, db, scratch, cfg, sock, lock, info, have) -> int:
    prev = lock.try_take(phase="reseeding")
    if prev is None:
        raise Refused("someone else took the lock first")
    stop = threading.Event()

    def renew():
        while not stop.wait(leaderlock.RENEW_S):
            try:
                lock.renew()
            except Exception as e:  # noqa: BLE001 - said; held() runs out by itself
                say(f"lock: renew failed ({type(e).__name__}: {e})")
    threading.Thread(target=renew, name="renew", daemon=True).start()
    ls = None
    released = False
    try:
        copy = scratch / f"{db}.sqlite"
        for s in ("", *SIDECARS):
            Path(f"{copy}{s}").unlink(missing_ok=True)
        shutil.rmtree(scratch / f".{db}.sqlite-litestream", ignore_errors=True)
        shutil.copyfile(args.file, copy)
        if sha256(copy) != info["sha256"]:
            raise Refused("the copy's sha256 differs from the file's")
        say(f"copied: {copy}")
        ls = subprocess.Popen(["litestream", "replicate", "-config", str(cfg)])
        end = time.monotonic() + SYNC_S
        got = None
        while got is None:
            if not lock.held():
                raise Refused("the lock wasn't renewed in time: stopped")
            if ls.poll() is not None:
                raise Refused(f"litestream replicate exited {ls.poll()}")
            r = subprocess.run(["litestream", "sync", "-wait", "-json", "-timeout", "60", "-socket",
                                str(sock), str(copy)], capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                d = last_json(r.stdout)
                local, rep = txid(d.get("txid")), txid(d.get("replica_txid"))
                if local is not None and rep is not None and rep >= local > have:
                    got = local
                    break
                raise Refused(f"Litestream's position {local} (replica {rep}) doesn't follow the replica's {have}")
            if time.monotonic() > end:
                raise Refused(f"litestream sync: {(r.stderr or r.stdout).strip()[-300:]}")
            time.sleep(1.0)
        say(f"in the replica: position {got}")
        ls.terminate()
        ls.wait(60)
        ls = None
        verify = scratch / "verify.sqlite"
        for s in ("", *SIDECARS, "-txid"):
            Path(f"{verify}{s}").unlink(missing_ok=True)
        r = subprocess.run(["litestream", "restore", "-config", str(cfg), "-o", str(verify), str(copy)],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise Refused(f"litestream restore (verify): {(r.stderr or r.stdout).strip()[-300:]}")
        con = sqlite3.connect(f"file:{verify}?mode=ro", uri=True)
        try:
            res = [x[0] for x in con.execute("PRAGMA integrity_check").fetchall()]
        finally:
            con.close()
        if res != ["ok"]:
            raise Refused(f"the restored replica: integrity_check: {'; '.join(res[:5])}. Its latest position is "
                          f"this attempt; `litestream restore -txid {have}` still gives the state before it")
        if content_digest(verify) != content_digest(args.file):
            raise Refused("the restored replica's content differs from the file's. Its latest position is this "
                          f"attempt; `litestream restore -txid {have}` still gives the state before it")
        say("verified: the replica restores to the file's content, integrity_check ok")
        positions = dict(prev.get("positions") or {}) if isinstance(prev.get("positions"), dict) else {}
        positions[db] = f"{got:016x}"
        stop.set()
        lock.release(positions, True)                               # every change of the reseed is in the replica
        released = True
        say(f"released the lock: positions {positions}")
        say("next: set the hub's desired count back to 1; its takeover restores the reseeded replica")
        return 0
    finally:
        stop.set()
        if ls is not None and ls.poll() is None:
            ls.kill()
        if not released and lock.generation > 0:
            try:
                lock.release(None, False)
                say("released the lock unclean (no positions)")
            except Exception as e:  # noqa: BLE001
                say(f"lock: not released ({type(e).__name__}: {e}): it expires by itself in {leaderlock.TTL_S:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
