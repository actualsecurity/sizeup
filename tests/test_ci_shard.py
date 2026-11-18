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
    d = ci.load_durations()
    files = real_files()
    assert sum(1 for f in files if f in d) > len(files) // 2                  # the recorded durations are in use
    for total in (2, 3, 4):
        bins = ci.shards(files, d, total)
        loads = [sum(d.get(f, ci.DEFAULT_S) for f in b) for b in bins]
        biggest_file = max(d.get(f, ci.DEFAULT_S) for f in files)
        # longest-first greedy: the heaviest shard is at most the even share plus the largest single file
        assert max(loads) <= sum(loads) / total + biggest_file


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
    ci.refresh(report, out)
    assert json.loads(out.read_text()) == {"tests/test_cad.py": 12.0, "tests/test_perf.py": 59.4}


def test_refresh_refuses_a_report_with_no_durations(tmp_path):
    report = tmp_path / "empty.txt"
    report.write_text("nothing here\n")
    with pytest.raises(SystemExit):
        ci.refresh(report, tmp_path / "out.json")
    assert not (tmp_path / "out.json").exists()


def test_the_workflow_hands_the_tool_a_one_based_shard_number():
    """GitHub's strategy.job-index counts from 0 and the tool's INDEX from 1: passing the raw index sent shard 0 to a
    refusal and left the last shard untested (the first run of this workflow)."""
    wf = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "tools/ci_shard.py" in wf and "strategy.job-total" in wf          # the count is the matrix's own size
    assert "$(( ${{ strategy.job-index }} + 1 ))" in wf                       # one-based
    assert 'ci_shard.py "${{ strategy.job-index }}"' not in wf                # never the raw zero-based index
