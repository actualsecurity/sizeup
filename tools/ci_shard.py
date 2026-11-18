#!/usr/bin/env python3
"""Split the test files into parallel continuous integration (CI) shards.

    python tools/ci_shard.py INDEX TOTAL        the test files of shard INDEX (1..TOTAL), one per line
    python tools/ci_shard.py --refresh OUT      rewrite tools/ci_shard_durations.json from pytest's own report

The split is by whole file, so everything the suite relies on stays as it is under `--dist loadfile` (a file's module
fixtures and its tests run on one worker). Files are dealt out longest first, each to the shard with the least work so
far (longest-processing-time-first, which is within 4/3 of the best possible split). Durations come from
tools/ci_shard_durations.json; a file not listed there (a new test file) gets DEFAULT_S, so it is always assigned
to some shard and can never be skipped. Standard library only. No test is ever left out: tests/test_ci_shard.py checks
that every shard split covers every test file exactly once.

To refresh the durations after the suite has changed a lot:
    python -m pytest -q -n auto --durations=0 > /tmp/durations.txt
    python tools/ci_shard.py --refresh /tmp/durations.txt
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = Path(__file__).resolve().parent / "ci_shard_durations.json"
DEFAULT_S = 2.0          # a file with no recorded duration (a new one): about the median of the recorded ones


def test_files(root: Path = ROOT) -> list[str]:
    """What pytest collects from tests/ (testpaths in pyproject.toml, python_files test_*.py), relative to the root."""
    return sorted(p.relative_to(root).as_posix() for p in (root / "tests").glob("test_*.py"))


def load_durations(path: Path = DURATIONS) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}                                   # no data: every file counts the same, still a valid split
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and v >= 0}


def shards(files: list[str], durations: dict[str, float], total: int) -> list[list[str]]:
    """`total` lists that together hold every file once. Deterministic: the same inputs give the same split."""
    if total < 1:
        raise ValueError("total must be at least 1")
    bins: list[list[str]] = [[] for _ in range(total)]
    load = [0.0] * total
    for f in sorted(files, key=lambda f: (-durations.get(f, DEFAULT_S), f)):
        i = min(range(total), key=lambda i: (load[i], i))
        bins[i].append(f)
        load[i] += durations.get(f, DEFAULT_S)
    return [sorted(b) for b in bins]


def refresh(report: Path, out: Path = DURATIONS) -> dict[str, float]:
    """Sum pytest's `--durations` lines (setup, call and teardown) per test file."""
    per: dict[str, float] = {}
    for m in re.finditer(r"^\s*([0-9.]+)s (?:setup|call|teardown)\s+(tests/test_[^:\s]+\.py)::", report.read_text(), re.M):
        per[m.group(2)] = per.get(m.group(2), 0.0) + float(m.group(1))
    if not per:
        raise SystemExit("no pytest --durations lines found in " + str(report))
    out.write_text(json.dumps({k: round(v, 1) for k, v in sorted(per.items())}, indent=1) + "\n")
    return per


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--refresh":
        print(f"{len(refresh(Path(argv[1])))} files written to {DURATIONS.relative_to(ROOT)}", file=sys.stderr)
        return 0
    if len(argv) != 2 or not all(a.isdigit() for a in argv):
        print(__doc__, file=sys.stderr)
        return 2
    index, total = int(argv[0]), int(argv[1])
    if not 1 <= index <= total:
        print(f"INDEX must be 1..{total}", file=sys.stderr)
        return 2
    mine = shards(test_files(), load_durations(), total)[index - 1]
    if not mine:
        print("no test files for this shard", file=sys.stderr)
        return 1                                    # a shard with nothing to run is a mistake, not a pass
    print("\n".join(mine))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
