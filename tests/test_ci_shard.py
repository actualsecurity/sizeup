"""tools/ci_shard.py: the split of the test files into parallel CI shards must never drop or repeat a file."""
from __future__ import annotations

import functools
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ci_shard", ROOT / "tools" / "ci_shard.py")
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


@functools.cache
def _real_files() -> tuple[str, ...]:
    return tuple(ci.test_files())


def real_files() -> list[str]:
    return list(_real_files())


def pytest_collects(root: Path) -> set[str]:
    """The files a real `pytest --collect-only` in `root` collects tests from (its `-qq` lines are "path: count")."""
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-qq", "-p", "no:cacheprovider"], cwd=root,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return {line.rsplit(":", 1)[0] for line in r.stdout.splitlines() if re.fullmatch(r"\S+\.py: \d+", line)}


def write_project(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


A_TEST = "def test_it():\n    assert True\n"


@pytest.mark.parametrize("total", [1, 2, 3, 4, 5, 7])
def test_every_test_file_is_in_exactly_one_shard(total):
    files = real_files()
    bins = ci.shards(files, ci.load_durations(), total)
    assert len(bins) == total
    flat = [f for b in bins for f in b]
    assert sorted(flat) == files and len(flat) == len(set(flat))          # none dropped, none twice


def test_the_shards_hold_every_file_pytest_itself_collects():
    """#751: a file pytest collects that no shard runs would never run in CI. The real collection is the reference,
    whatever the config says (testpaths, python_files, subdirectories, *_test.py)."""
    collected = pytest_collects(ROOT)
    assert "tests/test_ci_shard.py" in collected and "tests/conftest.py" not in collected
    flat = [f for b in ci.shards(real_files(), ci.load_durations(), 3) for f in b]
    assert not collected - set(flat), "collected by pytest but in no shard"
    assert len(flat) == len(set(flat))
    assert all((ROOT / f).is_file() for f in flat)                         # an extra is only ever a real file


def test_nested_and_star_test_files_are_found_like_pytest_finds_them(tmp_path):
    write_project(tmp_path, {
        "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "tests/conftest.py": "",
        "tests/helpers.py": A_TEST,                          # not a test file name: pytest skips it
        "tests/test_flat.py": A_TEST,
        "tests/sub/test_nested.py": A_TEST,
        "tests/sub/deeper/thing_test.py": A_TEST,            # pytest's other default pattern
        "tests/node_modules/test_skipped.py": A_TEST,        # norecursedirs
        "scripts/test_outside.py": A_TEST,                   # outside testpaths
    })
    expected = {"tests/test_flat.py", "tests/sub/test_nested.py", "tests/sub/deeper/thing_test.py"}
    assert pytest_collects(tmp_path) == expected
    files = ci.test_files(tmp_path)
    assert set(files) == expected and len(files) == len(expected)
    for total in (1, 2, 3, 4):
        flat = [f for b in ci.shards(files, {}, total) for f in b]
        assert sorted(flat) == sorted(expected)


def test_the_configured_python_files_patterns_are_followed(tmp_path):
    write_project(tmp_path, {
        "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["checks"]\npython_files = ["check_*.py"]\n',
        "checks/check_one.py": A_TEST,
        "checks/more/check_two.py": A_TEST,
        "checks/test_not_matched.py": A_TEST,
    })
    expected = {"checks/check_one.py", "checks/more/check_two.py"}
    assert pytest_collects(tmp_path) == expected == set(ci.test_files(tmp_path))


def test_a_file_that_fails_to_import_is_still_given_to_a_shard(tmp_path):
    # the tool never imports a test module, so a broken one still goes to a shard, whose pytest run reports it
    write_project(tmp_path, {
        "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "tests/test_ok.py": A_TEST,
        "tests/test_broken.py": "import a_module_that_does_not_exist\n" + A_TEST,
    })
    assert ci.test_files(tmp_path) == ["tests/test_broken.py", "tests/test_ok.py"]


def test_no_test_files_at_all_is_an_error_not_an_empty_split(tmp_path):
    write_project(tmp_path, {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
                             "tests/helpers.py": ""})
    with pytest.raises(RuntimeError, match="no test files"):
        ci.test_files(tmp_path)


def test_the_split_is_the_same_every_time():
    d = ci.load_durations()
    assert ci.shards(real_files(), d, 3) == ci.shards(list(reversed(real_files())), d, 3)


def test_a_new_file_with_no_recorded_duration_is_still_run():
    d = ci.load_durations()
    files = real_files() + ["tests/test_brand_new_file.py"]
    assert "tests/test_brand_new_file.py" not in d
    flat = [f for b in ci.shards(files, d, 3) for f in b]
    assert flat.count("tests/test_brand_new_file.py") == 1


def test_no_data_at_all_is_still_a_valid_split(tmp_path):
    assert ci.load_durations(tmp_path / "missing.json") == {}
    (tmp_path / "bad.json").write_text("not json")
    assert ci.load_durations(tmp_path / "bad.json") == {}
    flat = [f for b in ci.shards(real_files(), {}, 3) for f in b]
    assert sorted(flat) == real_files()


def test_the_shards_are_balanced():
    """The recorded durations must still describe the suite, by ci.stale()'s rule: no more than half of the collected
    files unrecorded, and the DEFAULT_S guesses for the unrecorded ones no more than half of the estimated time. A
    refresh records every collected file, so a new test file with no entry yet never fails this (the sharder gives it
    DEFAULT_S); only durations that are mostly missing do, and then the fix is a refresh (tools/ci_shard.py)."""
    d = ci.load_durations()
    files = real_files()
    assert ci.stale(files, d) is None, "refresh tools/ci_shard_durations.json (tools/ci_shard.py's docstring)"
    for total in (2, 3, 4):
        bins = ci.shards(files, d, total)
        loads = [sum(d.get(f, ci.DEFAULT_S) for f in b) for b in bins]
        biggest_file = max(d.get(f, ci.DEFAULT_S) for f in files)
        # longest-first greedy: the heaviest shard is at most the even share plus the largest single file
        assert max(loads) <= sum(loads) / total + biggest_file


def test_new_test_files_with_no_recorded_duration_do_not_make_the_data_stale():
    d = ci.load_durations()
    new = [f"tests/test_brand_new_{i}.py" for i in range(20)]
    assert not set(new) & set(d)
    assert ci.stale(real_files() + new[:1], d) is None
    assert ci.stale(real_files() + new, d) is None
    flat = [f for b in ci.shards(real_files() + new, d, 3) for f in b]
    assert all(flat.count(f) == 1 for f in new)


def test_durations_with_most_files_missing_are_stale():
    files = [f"tests/test_{i:03}.py" for i in range(100)]
    assert ci.stale(files, {f: 2.0 for f in files}) is None
    assert ci.stale(files, {f: 2.0 for f in files[:60]}) is None              # 40 missing: fine
    assert "61 of 100" in ci.stale(files, {f: 2.0 for f in files[:39]})      # most missing: stale
    # entries for files that are not collected any more do not count as recorded
    gone = {f"tests/test_deleted_{i}.py": 2.0 for i in range(100)}
    assert ci.stale(files, {**gone, **{f: 2.0 for f in files[:10]}})
    # few files missing, but their guesses outweigh what is recorded (the recorded files are all near zero): stale
    assert "estimated" in ci.stale(files, {f: 0.01 for f in files[:70]})
    # the real data with most of its files dropped is stale too
    real = real_files()
    d = ci.load_durations()
    assert ci.stale(real, {f: d[f] for f in real[: len(real) // 4] if f in d})


def test_a_shard_number_out_of_range_is_refused():
    for args in (["0", "3"], ["4", "3"], ["x", "3"], ["1"], []):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "ci_shard.py"), *args], capture_output=True, text=True)
        assert r.returncode == 2, args


def test_the_command_line_prints_the_files_of_one_shard():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "ci_shard.py"), "2", "3"], capture_output=True, text=True)
    assert r.returncode == 0
    mine = r.stdout.split()
    assert mine == ci.shards(real_files(), ci.load_durations(), 3)[1] and mine


def test_refresh_sums_pytests_duration_lines_per_file(tmp_path):
    report = tmp_path / "durations.txt"
    report.write_text(
        "============ slowest durations ============\n"
        "57.25s call     tests/test_perf.py::test_a\n"
        "1.50s setup    tests/test_perf.py::test_a\n"
        "0.60s teardown tests/test_perf.py::test_a\n"
        "12.00s call     tests/test_cad.py::test_b[47-0.75]\n"
        "noise that is not a duration line\n")
    out = tmp_path / "out.json"
    ci.refresh(report, out, files=["tests/test_cad.py", "tests/test_perf.py"])
    assert json.loads(out.read_text()) == {"tests/test_cad.py": 12.0, "tests/test_perf.py": 59.35}


def test_refresh_reads_the_verbose_report_and_its_sub_5ms_lines(tmp_path):
    """-vv (or --durations-min=0) shows the phases under 0.005 s that -q hides; -vv also adds progress lines."""
    report = tmp_path / "durations.txt"
    report.write_text(
        "[gw0] [ 50%] PASSED tests/test_fast.py::test_a\n"
        "tests/test_slow.py::test_b PASSED                    [100%]\n"
        "============================= slowest durations =============================\n"
        "3.21s call     tests/test_slow.py::test_b\n"
        "0.00s setup    tests/test_fast.py::test_a\n"
        "0.00s call     tests/test_fast.py::test_a\n"
        "0.03s call     tests/test_fast.py::test_c[x-1]\n"
        "0.00s teardown tests/test_fast.py::test_a\n"
        "============================== 2 passed in 3.40s ==============================\n")
    out = tmp_path / "out.json"
    got = ci.refresh(report, out, files=["tests/test_fast.py", "tests/test_slow.py"])
    assert got == json.loads(out.read_text()) == {"tests/test_fast.py": 0.03, "tests/test_slow.py": 3.21}


def test_refresh_records_every_collected_file_with_a_floor(tmp_path):
    """A collected file the report has no time for (every phase hidden as too fast) still gets an entry, FLOOR_S, so
    it is never counted as missing; a reported file that is not collected any more is dropped."""
    report = tmp_path / "durations.txt"
    report.write_text(
        "1.00s call     tests/test_a.py::test_x\n"
        "0.00s call     tests/test_b.py::test_y\n"
        "2.00s call     tests/test_deleted.py::test_z\n"
        "(3 durations < 0.005s hidden.  Use -vv to show these durations.)\n")
    out = tmp_path / "out.json"
    files = ["tests/test_a.py", "tests/test_b.py", "tests/sub/test_c.py"]
    ci.refresh(report, out, files=files)
    assert json.loads(out.read_text()) == {
        "tests/sub/test_c.py": ci.FLOOR_S, "tests/test_a.py": 1.0, "tests/test_b.py": ci.FLOOR_S}
    assert ci.stale(files, ci.load_durations(out)) is None


def test_refresh_lists_the_files_pytest_collects_when_not_given(tmp_path, monkeypatch):
    report = tmp_path / "durations.txt"
    report.write_text("0.50s call     tests/test_a.py::test_x\n0.20s call     tests/test_b.py::test_y\n")
    monkeypatch.setattr(ci, "collect_files", lambda: ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])
    out = tmp_path / "out.json"
    ci.refresh(report, out)
    assert json.loads(out.read_text()) == {"tests/test_a.py": 0.5, "tests/test_b.py": 0.2, "tests/test_c.py": 0.01}


def test_refresh_refuses_a_report_of_a_partial_run(tmp_path):
    report = tmp_path / "durations.txt"
    report.write_text("1.00s call     tests/test_a.py::test_x\n")
    with pytest.raises(SystemExit, match="1 of the 3"):
        ci.refresh(report, tmp_path / "out.json", files=["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])
    assert not (tmp_path / "out.json").exists()


def test_refresh_refuses_a_report_with_no_durations(tmp_path):
    report = tmp_path / "empty.txt"
    report.write_text("nothing here\n")
    with pytest.raises(SystemExit):
        ci.refresh(report, tmp_path / "out.json", files=["tests/test_a.py"])
    assert not (tmp_path / "out.json").exists()


def test_the_workflow_hands_the_tool_a_one_based_shard_number():
    """GitHub's strategy.job-index counts from 0 and the tool's INDEX from 1: passing the raw index sent shard 0 to a
    refusal and left the last shard untested (the first run of this workflow)."""
    wf = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "tools/ci_shard.py" in wf and "strategy.job-total" in wf          # the count is the matrix's own size
    assert "$(( ${{ strategy.job-index }} + 1 ))" in wf                       # one-based
    assert 'ci_shard.py "${{ strategy.job-index }}"' not in wf                # never the raw zero-based index
