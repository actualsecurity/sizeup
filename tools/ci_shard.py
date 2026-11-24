#!/usr/bin/env python3
"""Split the test files into parallel continuous integration (CI) shards.

    python tools/ci_shard.py INDEX TOTAL        the test files of shard INDEX (1..TOTAL), one per line
    python tools/ci_shard.py --files            every test file pytest collects here, one per line
    python tools/ci_shard.py --refresh REPORT   rewrite tools/ci_shard_durations.json from pytest's own report

The split is by whole file, so everything the suite relies on stays as it is under `--dist loadfile` (a file's module
fixtures and its tests run on one worker). Files are dealt out longest first, each to the shard with the least work so
far (longest-processing-time-first, which is within 4/3 of the best possible split). Durations come from
tools/ci_shard_durations.json; a file not listed there (a new test file) gets DEFAULT_S, so it is always assigned
to some shard and can never be skipped, and a few such files never matter (see stale(): only data that is mostly
missing does).

The test files are the ones pytest itself would collect (#751): the tool runs pytest's own collection with the repo's
config (testpaths, python_files, norecursedirs, conftest ignores, subdirectories, *_test.py), stopping at each file's
collector so no test module is imported (about a second). No test is ever left out: tests/test_ci_shard.py checks
that the shards together hold every file a real `pytest --collect-only` collects, each exactly once.

To refresh the durations after the suite has changed a lot (the whole suite, as CI runs it):
    python -m pytest -q -p no:cacheprovider -n auto --dist loadfile --durations=0 --durations-min=0 > /tmp/durations.txt
    python tools/ci_shard.py --refresh /tmp/durations.txt
`--durations-min=0` (or -vv) matters: by default pytest hides every phase under 0.005 s, so a file of fast tests
never appears in the report. The refresh records EVERY file pytest collects anyway: one with no reported time gets
FLOOR_S, and a reported file pytest no longer collects is dropped.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = Path(__file__).resolve().parent / "ci_shard_durations.json"
DEFAULT_S = 2.0          # a file with no recorded duration (a new one): a little under the median recorded (3.5 s)
FLOOR_S = 0.01           # a collected file the refresh's report gave no time (all of its phases too fast to show)
STALE_SHARE = 0.5        # stale(): the most of the files, or of the estimated time, that may be unrecorded guesses


class _FileRecorder:
    """A pytest plugin: notes every file pytest makes a collector for, then drops that collector (nothing imported)."""

    def __init__(self) -> None:
        self.paths: set[Path] = set()
        self.errors: list[str] = []

    @pytest.hookimpl(wrapper=True)
    def pytest_collect_file(self, file_path, parent):
        made = yield
        if made:
            self.paths.add(Path(file_path))
        return []

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(f"{report.nodeid}: {report.longreprtext}")


def collect_files(root: Path = ROOT) -> list[str]:
    """In this process: the files pytest would collect when run with no arguments in `root`, relative to it."""
    rec = _FileRecorder()
    here, out = os.getcwd(), os.dup(1)
    sys.stdout.flush()
    os.dup2(2, 1)                                   # pytest's own output goes to stderr: stdout is the file list
    try:
        os.chdir(root)
        code = pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider"], plugins=[rec])
    finally:
        sys.stdout.flush()
        os.dup2(out, 1)
        os.close(out)
        os.chdir(here)
    if code not in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED) or rec.errors:
        raise SystemExit(f"pytest's collection failed (exit {int(code)}): " + "\n".join(rec.errors))
    files = sorted(p.resolve().relative_to(root.resolve()).as_posix() for p in rec.paths)
    if not files:
        raise SystemExit(f"pytest found no test files under {root}")
    return files


def test_files(root: Path = ROOT) -> list[str]:
    """collect_files() in a fresh interpreter: safe to call from inside a pytest run (the tool's own tests)."""
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--files"], cwd=root, capture_output=True,
                       text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ci_shard.py --files failed in {root}:\n{r.stderr}")
    return r.stdout.split()


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


def stale(files: list[str], durations: dict[str, float]) -> str | None:
    """Why the recorded durations are too stale to balance `files` by, or None when they are fine.

    Only the collected files count (an entry for a deleted file is ignored). The data is stale when more than
    STALE_SHARE of the collected files have no recorded time, or when the DEFAULT_S guesses for those files make up
    more than STALE_SHARE of the estimated total time. A refresh records every file, so an ordinary new test file
    (or dozens of them) never gets near either limit; only data that is mostly missing does."""
    if not files:
        return None
    missing = [f for f in files if f not in durations]
    if len(missing) > STALE_SHARE * len(files):
        return f"{len(missing)} of {len(files)} collected test files have no recorded duration"
    guessed = DEFAULT_S * len(missing)
    total = guessed + sum(durations[f] for f in files if f in durations)
    if total and guessed > STALE_SHARE * total:
        return f"guesses for unrecorded files are {guessed / total:.0%} of the estimated {total:.0f} s"
    return None


def refresh(report: Path, out: Path = DURATIONS, files: list[str] | None = None) -> dict[str, float]:
    """Sum pytest's `--durations` lines (setup, call and teardown) per test file, for every collected file.

    `files` are the collected test files (collect_files() when None). Each one is written: a file the report gives
    no time (its phases all hidden as too fast, or no test ran) gets FLOOR_S, so the next new file is the only one
    missing. A reported file that is not collected any more is dropped. A report that covers too few of the files
    (a partial run, or the hidden fast phases of a run without --durations-min=0) is refused."""
    per: dict[str, float] = {}
    for m in re.finditer(r"^\s*([0-9.]+)s (?:setup|call|teardown)\s+([^:\s]+\.py)::", report.read_text(), re.M):
        per[m.group(2)] = per.get(m.group(2), 0.0) + float(m.group(1))
    if not per:
        raise SystemExit("no pytest --durations lines found in " + str(report))
    if files is None:
        files = collect_files()
    reported = sum(1 for f in files if f in per)
    if reported <= STALE_SHARE * len(files):
        raise SystemExit(f"{report} has durations for only {reported} of the {len(files)} collected test files: run "
                         "the whole suite with --durations=0 --durations-min=0 (tools/ci_shard.py's docstring)")
    data = {f: max(round(per.get(f, 0.0), 2), FLOOR_S) for f in sorted(files)}
    out.write_text(json.dumps(data, indent=1) + "\n")
    return data


def main(argv: list[str]) -> int:
    if argv == ["--files"]:
        print("\n".join(collect_files(Path.cwd())))
        return 0
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
    mine = shards(collect_files(), load_durations(), total)[index - 1]
    if not mine:
        print("no test files for this shard", file=sys.stderr)
        return 1                                    # a shard with nothing to run is a mistake, not a pass
    print("\n".join(mine))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
