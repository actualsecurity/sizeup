#!/usr/bin/env python3
"""Share the private replay fixtures through S3 (GitHub #283; tests/replay.py says what they are).

    .venv/bin/python tools/replay_sync.py status                  the local cache: each set, its files checked
    .venv/bin/python tools/replay_sync.py pull [--dryrun]         S3 -> the local cache (read-only on S3)
    .venv/bin/python tools/replay_sync.py push --write-to-s3 [--dryrun]
                                                                  the local cache -> S3 (refuses without the flag)

Where:  $SIZEUP_REPLAY_S3 or --s3, else s3://sizeup-replay-fixtures-504804195561/v1. Only two places are accepted
        (DESTINATIONS), for pull and push alike:
          s3://sizeup-replay-fixtures-504804195561/v<N>              a dedicated private bucket (the default)
          s3://sizeup-hub-backups-504804195561/replay-fixtures/v<N>  a prefix in the hub's backup bucket (the
                                                                     hub's task role can read and write it there)
        Never the hub's own sizeup-hub/ prefix (Litestream, the cache copies, the leader lock), nor anything else.
Local:  $SIZEUP_REPLAY_DIR, else ~/.cache/sizeup-replay (outside the repo; never committed: the repo is public).

The dedicated bucket is not created by this tool. Its setup, for whoever approves it (us-east-1):
    aws s3api create-bucket --bucket sizeup-replay-fixtures-504804195561
    aws s3api put-public-access-block --bucket sizeup-replay-fixtures-504804195561 --public-access-block-configuration \\
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-encryption --bucket sizeup-replay-fixtures-504804195561 --server-side-encryption-configuration \\
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
    aws s3api put-bucket-versioning --bucket sizeup-replay-fixtures-504804195561 --versioning-configuration Status=Enabled
    aws s3api put-bucket-ownership-controls --bucket sizeup-replay-fixtures-504804195561 \\
        --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
  No bucket policy granting anyone else, and the hub's roles (sizeup-hub-task, sizeup-hub-exec) get no statement for
  it: only the developer's own credentials (IAM identity policies in this account) reach it. Optionally a lifecycle
  rule expiring noncurrent versions after 30 days, like the backup bucket's.

pull only reads from S3 (aws s3 sync S3 -> local: downloads, never deletes local files). push writes only with
--write-to-s3, set by set (<dest>/<kind>/<set>/), each object with SSE-S3 (AES256), symlinks never followed, never
deleting anything there; it first checks the whole cache (check()): nothing outside <kind>/<set>/, no symlinks,
every file in its set's manifest with its sha256, every file's content what its name says (WAV, MPEG-TS, gzip,
JSON, XML, a playlist; never a SQLite database or an LTX file), and audio only as clips.json's airband entries.
Exit 0: done; 1: a check failed; 2: refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.replay import KINDS, MANIFEST, cache_dir, sha256  # noqa: E402

DEDICATED = "sizeup-replay-fixtures-504804195561"
BACKUPS = "sizeup-hub-backups-504804195561"
DEFAULT_S3 = f"s3://{DEDICATED}/v1"
DESTINATIONS = {DEDICATED: re.compile(r"^v\d+$"), BACKUPS: re.compile(r"^replay-fixtures/v\d+$")}
AIRBAND = re.compile(r"^(HPNTower|HPNGround|NYApproach)-\d+$")
CLIP_KEYS = {"id", "channel", "at", "bytes", "file"}
# what each kind may hold, by name ending; the content of each is sniffed (sniff())
ALLOWED = {"adsb": (".jsonl.gz", ".json"), "camera": (".xml", ".json", ".m3u8", ".ts"), "audio": (".wav", ".json")}
SAFE_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")


class Refused(SystemExit):
    def __init__(self, why: str):
        print(f"refused: {why}", file=sys.stderr)
        super().__init__(2)


def destination(args) -> str:
    """The S3 prefix, only one of DESTINATIONS (pull and push alike): never the hub's Litestream prefix."""
    url = (args.s3 or os.environ.get("SIZEUP_REPLAY_S3", "") or DEFAULT_S3).strip().rstrip("/")
    m = re.fullmatch(r"s3://([a-z0-9.-]+)/(.+)", url)
    if not m:
        raise Refused(f"not an S3 prefix: {url} (s3://bucket/prefix)")
    bucket, prefix = m.group(1), m.group(2)
    rule = DESTINATIONS.get(bucket)
    if rule is None or not rule.fullmatch(prefix) or ".." in prefix:
        raise Refused(f"{url} is not a replay-fixtures location (s3://{DEDICATED}/v<N> or "
                      f"s3://{BACKUPS}/replay-fixtures/v<N>)")
    return url


def sniff(rel: str, p: Path) -> str:
    """'' when the file's content is what its name says, else why not."""
    with open(p, "rb") as f:
        head = f.read(16)
    if head.startswith(b"SQLite format 3\0"):
        return "a SQLite database"
    if head.startswith(b"LTX1"):
        return "a Litestream LTX file"
    if rel.endswith(".wav"):
        return "" if head[:4] == b"RIFF" and head[8:12] == b"WAVE" else "not a RIFF/WAVE file"
    if rel.endswith(".ts"):
        return "" if head[:1] == b"\x47" else "not MPEG-TS (no 0x47 sync byte)"
    if rel.endswith(".gz"):
        return "" if head[:2] == b"\x1f\x8b" else "not gzip"
    if rel.endswith(".m3u8"):
        return "" if head.startswith(b"#EXTM3U") else "not an HLS playlist"
    if rel.endswith(".json"):
        try:
            json.loads(p.read_bytes())
        except ValueError:
            return "not JSON"
        return ""
    if rel.endswith(".xml"):
        return "" if head.lstrip()[:1] == b"<" else "not XML"
    return "an unexpected kind of file"


def _safe_rel(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return bool(parts) and not rel.startswith("/") and all(SAFE_PART.fullmatch(x) for x in parts)


def check(root: Path) -> list[str]:
    """Every problem with the local cache, as lines; [] when every file is in a whole, allowed set. Covers all that
    `aws s3 sync` of the cache could send: the whole tree is walked, symlinks not followed."""
    problems: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return [f"{root}: no replay cache (or a symlink)"]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for n in dirnames + filenames:
            p = here / n
            rel = str(p.relative_to(root))
            if p.is_symlink():
                problems.append(f"{rel}: a symlink (never followed, never shared)")
            elif p.is_file() and len(PurePosixPath(rel).parts) < 3:
                problems.append(f"{rel}: outside <kind>/<set>/")
            elif p.is_dir() and len(PurePosixPath(rel).parts) == 1 and n not in KINDS:
                problems.append(f"{rel}/: not a known kind ({', '.join(KINDS)})")
        dirnames[:] = [n for n in dirnames if not (here / n).is_symlink()]
    for kind in KINDS:
        kind_dir = root / kind
        if not kind_dir.is_dir() or kind_dir.is_symlink():
            continue
        for d in sorted(p for p in kind_dir.iterdir() if p.is_dir() and not p.is_symlink()):
            problems += check_set(kind, d)
    return problems


def check_set(kind: str, d: Path) -> list[str]:
    name = f"{kind}/{d.name}"
    out: list[str] = []
    try:
        doc = json.loads((d / MANIFEST).read_text())
    except (OSError, ValueError) as e:
        return [f"{name}: no readable manifest ({type(e).__name__})"]
    if doc.get("kind") != kind:
        out.append(f"{name}: manifest kind {doc.get('kind')!r}")
    listed = {}
    for f in doc.get("files", []):
        rel = str(f.get("path", ""))
        if not _safe_rel(rel):
            out.append(f"{name}: manifest path {rel!r} is not a plain relative path")
            continue
        listed[rel] = f
    present = []
    for p in d.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue                                     # (symlinks: reported by check())
        rel = p.relative_to(d).as_posix()
        present.append(rel)
        if rel != MANIFEST and rel not in listed:
            out.append(f"{name}: {rel} is not in the manifest")
        if not rel.endswith(ALLOWED[kind]):
            out.append(f"{name}: {rel}: not a {kind} file ({', '.join(ALLOWED[kind])})")
        why = sniff(rel, p)
        if why:
            out.append(f"{name}: {rel}: {why}")
    for rel, f in listed.items():
        p = d / rel
        if not p.is_file() or p.is_symlink():
            out.append(f"{name}: {rel} missing")
        elif p.stat().st_size != f.get("bytes") or sha256(p) != f.get("sha256"):
            out.append(f"{name}: {rel} differs from its manifest")
    if kind == "audio":
        try:
            clips = json.loads((d / "clips.json").read_text())
        except (OSError, ValueError):
            clips = None
        if not isinstance(clips, list):
            return out + [f"{name}: no clips.json"]
        airband = set()
        for c in clips:
            if not isinstance(c, dict) or not AIRBAND.match(str(c.get("id", ""))) or set(c) - CLIP_KEYS:
                out.append(f"{name}: {c.get('id') if isinstance(c, dict) else c!r}: only airband clips "
                           f"({', '.join(sorted(CLIP_KEYS))})")
                continue
            airband.add(str(c.get("file", "")))
        for rel in present:
            if rel not in (MANIFEST, "clips.json") and rel not in airband:
                out.append(f"{name}: {rel}: not an airband clip in clips.json")
    return out


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
    url = destination(args)
    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    rc = aws("s3", "sync", url + "/", str(root) + "/", "--only-show-errors", "--exact-timestamps",
             *(["--dryrun"] if args.dryrun else []))
    if rc != 0:
        return rc
    if args.dryrun:
        return 0
    return status(args)


def push(args) -> int:
    url = destination(args)
    if not args.write_to_s3:
        print("refused: push writes to S3. Run it only once the private location is approved, with --write-to-s3 "
              f"(it writes under {url}/, SSE-S3, never deleting there); add --dryrun to see what it would send first.",
              file=sys.stderr)
        return 2
    root = cache_dir()
    problems = check(root)
    if problems:
        print("not pushed: the cache has problems\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    rc = 0
    for name, _doc in sets(root):                  # set by set: only <kind>/<set>/ is ever sent
        rc = aws("s3", "sync", str(root / name) + "/", f"{url}/{name}/", "--only-show-errors", "--sse", "AES256",
                 "--no-follow-symlinks", "--exclude", ".*", "--exclude", "*/.*",
                 *(["--dryrun"] if args.dryrun else [])) or rc
    return rc


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
    try:
        return {"status": status, "pull": pull, "push": push}[args.cmd](args)
    except Refused as e:
        return e.code


if __name__ == "__main__":
    sys.exit(main())
