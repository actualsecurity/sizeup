#!/usr/bin/env python3
"""A read-only, pure-Python reader for Litestream's LTX backups (GitHub #283): restore a SQLite database from
s3://<bucket>/<path>/<db>/ without the litestream binary.

    .venv/bin/python tools/ltx.py restore s3://sizeup-hub-backups-504804195561/sizeup-hub/audio.sqlite out.sqlite
    .venv/bin/python tools/ltx.py info FILE.ltx

Reads only: `aws s3 ls` and `aws s3 cp` from S3 to a local folder (your AWS CLI credentials). Never writes to S3.

The layout Litestream (0.5) keeps: <db>/<level>/<min txid>-<max txid>.ltx, level 0000 (each transaction) up to
0009 (snapshots: the whole database, from TXID 1). One LTX file (github.com/superfly/ltx):

    header   100 bytes, big-endian: "LTX1", flags u32, page size u32, commit u32 (the database's size in pages
             after this file), min TXID u64, max TXID u64, timestamp i64 (ms), pre-apply checksum u64,
             WAL offset i64, WAL size i64, WAL salts u32 x 2, node id u64, zero padding
    pages    each: pgno u32, flags u16, size u32, then `size` bytes: the page, LZ4-compressed (a block) when
             flags & 1; a page header with pgno 0 ends them
    index    the pages' offsets (varints), its size, then the trailer (post-apply and file checksums): not needed

Restore: the newest snapshot, then from its max TXID + 1 on, at each step the file that starts there and reaches
furthest (any level), until none does. The pages are applied newest file first and each page only once (its newest
copy wins), so most of the snapshot is never decompressed; the database is cut to the last file's commit size.
"""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Callable

HEADER = 100
MAGIC = b"LTX1"
PAGE_LZ4 = 1
NAME = re.compile(r"^([0-9a-f]{16})-([0-9a-f]{16})\.ltx$")


class LtxError(ValueError):
    pass


def lz4_block(src: bytes, size: int) -> bytes:
    """Decompress one LZ4 block (no frame) of known decompressed size. A malformed block (truncated, an offset
    before the start, more output than `size`) is an LtxError, never an IndexError or unbounded output."""
    try:
        return _lz4_block(src, size)
    except IndexError:
        raise LtxError("lz4: truncated block") from None


def _lz4_block(src: bytes, size: int) -> bytes:
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if len(out) + lit > size or i + lit > n:
            raise LtxError("lz4: literals past the page or the block")
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off == 0:
            raise LtxError("lz4: zero offset")
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        if len(out) + ml > size:
            raise LtxError("lz4: match past the page")
        start = len(out) - off
        if start < 0:
            raise LtxError("lz4: offset before the start")
        if off >= ml:
            out += out[start:start + ml]
        else:                                           # overlapping: the pattern repeats
            chunk = out[start:]
            while ml > 0:
                take = chunk[:ml]
                out += take
                ml -= len(take)
                chunk = out[start:]
    if len(out) != size:
        raise LtxError(f"lz4: {len(out)} bytes, expected {size}")
    return bytes(out)


MAX_COMMIT = 1 << 22            # pages: 16 GiB at 4 KiB, far past the hub's databases (audio.sqlite ~270 MB)


def header(b: bytes) -> dict:
    if len(b) < HEADER or b[:4] != MAGIC:
        raise LtxError("not an LTX file")
    (flags, page_size, commit, min_tx, max_tx, ts, pre, wal_off, wal_size, s1, s2,
     node) = struct.unpack(">IIIQQqQqqIIQ", b[4:80])
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise LtxError(f"page size {page_size}: not a power of two from 512 to 65536")
    if not 0 < commit <= MAX_COMMIT:
        raise LtxError(f"commit {commit} pages: out of range (1 to {MAX_COMMIT})")
    return {"flags": flags, "page_size": page_size, "commit": commit, "min_txid": min_tx, "max_txid": max_tx,
            "timestamp_ms": ts, "pre_apply_checksum": pre, "wal_offset": wal_off, "wal_size": wal_size}


def pages(b: bytes):
    """(pgno, flags, compressed bytes) for each page in an LTX file, in file order."""
    off = HEADER
    while True:
        if off + 6 > len(b):
            raise LtxError("truncated page header")
        pgno, flags = struct.unpack(">IH", b[off:off + 6])
        if pgno == 0:
            return
        if off + 10 > len(b):
            raise LtxError(f"page {pgno}: truncated page header")
        (size,) = struct.unpack(">I", b[off + 6:off + 10])
        off += 10
        if off + size > len(b):
            raise LtxError(f"page {pgno}: truncated")
        yield pgno, flags, b[off:off + size]
        off += size


def page_data(flags: int, data: bytes, page_size: int) -> bytes:
    if flags & PAGE_LZ4:
        return lz4_block(data, page_size)
    if len(data) != page_size:
        raise LtxError(f"uncompressed page of {len(data)} bytes, page size {page_size}")
    return data


# ---- S3, read-only (the AWS CLI) ---------------------------------------------------------------------------------


def _aws(*args: str) -> str:
    out = subprocess.run(["aws", "s3", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"aws s3 {args[0]}: {out.stderr.strip()[:400]}")
    return out.stdout


def listing(url: str) -> list[tuple[int, int, int, str]]:
    """(level, min txid, max txid, key under url) for every LTX file under url (s3://bucket/path/db)."""
    files = []
    for line in _aws("ls", "--recursive", url.rstrip("/") + "/").splitlines():
        key = line.split()[-1] if line.strip() else ""
        parts = key.split("/")
        if len(parts) < 2:
            continue
        m = NAME.match(parts[-1])
        if not m or not re.fullmatch(r"\d{4}", parts[-2]):
            continue
        files.append((int(parts[-2]), int(m.group(1), 16), int(m.group(2), 16), f"{parts[-2]}/{parts[-1]}"))
    return files


def plan(files: list[tuple[int, int, int, str]], log: Callable[[str], None] = print) -> list[tuple[int, int, int, str]]:
    """The newest snapshot (from TXID 1), then the files that carry it on, each the one reaching furthest. A file
    beyond a gap (no file starts right after the chain's end) is left out, and the gap is said."""
    snaps = [f for f in files if f[1] == 1]
    if not snaps:
        raise SystemExit("no snapshot (no file from TXID 1)")
    chain = [max(snaps, key=lambda f: (f[2], f[0]))]
    starts: dict[int, list] = {}
    for f in files:
        starts.setdefault(f[1], []).append(f)
    while True:
        nxt = starts.get(chain[-1][2] + 1)
        if not nxt:
            later = [f for f in files if f[1] > chain[-1][2] + 1]
            if later:
                lo = min(f[1] for f in later)
                log(f"ltx: warning: a gap after TXID {chain[-1][2]:#x} (the next file starts at {lo:#x}); "
                    f"restoring only up to {chain[-1][2]:#x}, {len(later)} later files left out")
            return chain
        chain.append(max(nxt, key=lambda f: (f[2], f[0])))


def restore(url: str, out: Path, work: Path | None = None, log: Callable[[str], None] = print) -> dict:
    """The database under url (s3://bucket/path/db) restored to out, as of its newest TXID in the backup.
    work: where the LTX files are downloaded (kept: a second restore reuses them). {txid, timestamp_ms, pages}."""
    url = url.rstrip("/")
    work = Path(work) if work else out.parent / (out.name + ".ltx")
    work.mkdir(parents=True, exist_ok=True)
    chain = plan(listing(url), log)
    total = sum(1 for _ in chain)
    log(f"ltx: {total} files, TXID {chain[0][1]:#x} to {chain[-1][2]:#x}")
    local = []
    for level, lo, hi, key in chain:
        p = work / key.replace("/", "_")
        if not p.exists():
            tmp = p.with_suffix(".part")
            _aws("cp", "--only-show-errors", f"{url}/{key}", str(tmp))
            tmp.rename(p)
        local.append(p)
    last = header(local[-1].read_bytes()[:HEADER])
    page_size, commit = last["page_size"], last["commit"]
    done: set[int] = set()
    with open(out, "wb") as db:
        db.truncate(commit * page_size)
        for p in reversed(local):                       # newest first: each page's newest copy, decompressed once
            b = p.read_bytes()
            h = header(b)
            if h["page_size"] != page_size:
                raise LtxError(f"{p.name}: page size {h['page_size']}, expected {page_size}")
            for pgno, flags, data in pages(b):
                if pgno in done or pgno > commit:
                    continue
                db.seek((pgno - 1) * page_size)
                db.write(page_data(flags, data, page_size))
                done.add(pgno)
    log(f"ltx: {out}: {commit} pages of {page_size} bytes ({len(done)} written), TXID {last['max_txid']:#x}")
    return {"txid": last["max_txid"], "timestamp_ms": last["timestamp_ms"], "pages": commit}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore")
    r.add_argument("url")
    r.add_argument("out")
    r.add_argument("--work", default="")
    i = sub.add_parser("info")
    i.add_argument("file")
    a = p.parse_args(argv)
    if a.cmd == "restore":
        restore(a.url, Path(a.out), Path(a.work) if a.work else None)
    else:
        b = Path(a.file).read_bytes()
        h = header(b)
        n = sum(1 for _ in pages(b))
        print({**h, "pages_in_file": n})


if __name__ == "__main__":
    main()
