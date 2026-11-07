#!/usr/bin/env python3
"""Share the private replay fixtures through S3 (GitHub #283; tests/replay.py says what they are).

    .venv/bin/python tools/replay_sync.py status                  the local cache: each set, its files checked
    .venv/bin/python tools/replay_sync.py pull [--dryrun]         S3 -> the local cache (read-only on S3)
    .venv/bin/python tools/replay_sync.py push --write-to-s3 [--dryrun]
                                                                  the local cache -> S3 (refuses without the flag)

Where:  $SIZEUP_REPLAY_S3, else s3://sizeup-hub-backups-504804195561/replay-fixtures/v1 (a private prefix in the hub's
        backup bucket: Block Public Access on, SSE-S3, versioned; outside the hub's own sizeup-hub/ prefix, so
        Litestream, the cache copies and the leader lock never see it). --s3 URL for another place.
Local:  $SIZEUP_REPLAY_DIR, else ~/.cache/sizeup-replay (outside the repo; never committed: the repo is public).

pull only reads from S3 (aws s3 sync S3 -> local: downloads, never deletes local files). push writes only under the
prefix, only with --write-to-s3, each object with SSE-S3 (AES256), never deleting anything there; it first checks
every set: a manifest whose files are all present with their sha256, a known kind, and the data-minimisation rules
(audio: airband clips only; no SQLite databases; nothing from the CAD). The AWS CLI and your credentials do the
transfer. Exit 0: done; 1: a check failed; 2: refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.replay import KINDS, MANIFEST, cache_dir, sha256  # noqa: E402

DEFAULT_S3 = "s3://sizeup-hub-backups-504804195561/replay-fixtures/v1"
AIRBAND = re.compile(r"^(HPNTower|HPNGround|NYApproach)-\d+$")
FORBIDDEN = re.compile(r"\.(sqlite|sqlite3|db|db-wal|db-shm|ltx)$|(^|/)cad[._/-]", re.I)


def s3_url(args) -> str:
    url = (args.s3 or os.environ.get("SIZEUP_REPLAY_S3", "") or DEFAULT_S3).rstrip("/")
    if not url.startswith("s3://") or url.count("/") < 3:
        raise SystemExit(f"not an S3 prefix: {url} (s3://bucket/prefix)")
    return url


def check(root: Path) -> list[str]:
    """Every problem with the local cache, as lines; [] when every set is whole and allowed."""
    problems = []
    if not root.is_dir():
        return [f"{root}: no replay cache"]
    for kind_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if kind_dir.name not in KINDS:
            problems.append(f"{kind_dir.name}/: not a known kind ({', '.join(KINDS)})")
            continue
        for d in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            name = f"{kind_dir.name}/{d.name}"
            m = d / MANIFEST
            try:
                doc = json.loads(m.read_text())
            except (OSError, ValueError) as e:
                problems.append(f"{name}: no readable manifest ({type(e).__name__})")
                continue
            if doc.get("kind") != kind_dir.name:
                problems.append(f"{name}: manifest kind {doc.get('kind')!r}")
            listed = {f["path"] for f in doc.get("files", [])}
            for f in doc.get("files", []):
                p = d / f["path"]
                if not p.is_file():
                    problems.append(f"{name}: {f['path']} missing")
                elif p.stat().st_size != f.get("bytes") or sha256(p) != f.get("sha256"):
                    problems.append(f"{name}: {f['path']} differs from its manifest")
            for p in d.rglob("*"):
                rel = str(p.relative_to(d))
                if p.is_file() and rel != MANIFEST and rel not in listed:
                    problems.append(f"{name}: {rel} is not in the manifest")
                if p.is_file() and FORBIDDEN.search(rel):
                    problems.append(f"{name}: {rel}: databases, LTX files and CAD data are never shared")
            if kind_dir.name == "audio":
                try:
                    clips = json.loads((d / "clips.json").read_text())
                except (OSError, ValueError):
                    clips = None
                if not isinstance(clips, list):
                    problems.append(f"{name}: no clips.json")
                else:
                    for c in clips:
                        if not AIRBAND.match(str(c.get("id", ""))) or set(c) - {"id", "channel", "at", "bytes", "file"}:
                            problems.append(f"{name}: {c.get('id')!r}: only airband clips (id, channel, at, bytes, file)")
    return problems


def sets(root: Path) -> list[tuple[str, dict]]:
    out = []
    for m in sorted(root.glob(f"*/*/{MANIFEST}")):
        try:
            out.append((f"{m.parent.parent.name}/{m.parent.name}", json.loads(m.read_text())))
        except (OSError, ValueError):
            continue
    return out


def aws(*args: str) -> int:
    print("+ aws " + " ".join(args), flush=True)
    return subprocess.run(["aws", *args]).returncode


def status(args) -> int:
    root = cache_dir()
    print(f"cache: {root}")
    for name, doc in sets(root):
        size = sum(f.get("bytes", 0) for f in doc.get("files", []))
        print(f"  {name}: {len(doc.get('files', []))} files, {size / 1e6:.1f} MB, captured {doc.get('captured_from')}")
    problems = check(root)
    for p in problems:
        print(f"  ! {p}")
    return 1 if problems else 0


def pull(args) -> int:
    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    rc = aws("s3", "sync", s3_url(args) + "/", str(root) + "/", "--only-show-errors", "--exact-timestamps",
             *(["--dryrun"] if args.dryrun else []))
    if rc != 0:
        return rc
    if args.dryrun:
        return 0
    return status(args)


def push(args) -> int:
    if not args.write_to_s3:
        print("refused: push writes to S3. Run it only once the private location is approved, with --write-to-s3 "
              f"(it writes under {s3_url(args)}/, SSE-S3, never deleting there); add --dryrun to see what it would send first.",
              file=sys.stderr)
        return 2
    root = cache_dir()
    problems = check(root)
    if problems:
        print("not pushed: the cache has problems\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    return aws("s3", "sync", str(root) + "/", s3_url(args) + "/", "--only-show-errors", "--sse", "AES256",
               "--exclude", ".*", "--exclude", "*/.*", *(["--dryrun"] if args.dryrun else []))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--s3", default="", help=f"the S3 prefix (default $SIZEUP_REPLAY_S3, else {DEFAULT_S3})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pl = sub.add_parser("pull")
    pl.add_argument("--dryrun", action="store_true")
    ps = sub.add_parser("push")
    ps.add_argument("--write-to-s3", action="store_true", help="required: this writes to S3")
    ps.add_argument("--dryrun", action="store_true", help="say what would be sent, send nothing")
    args = p.parse_args(argv)
    return {"status": status, "pull": pull, "push": push}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
