"""Known flaky tests (@pytest.mark.flaky, tests/conftest.py) get one retry when they fail, and the retry is reported;
every other test fails at once. Run as a separate pytest over a small suite, serially and under pytest-xdist like CI."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SUITE = '''
import pytest
from pathlib import Path

HERE = Path(__file__).parent


def tries(name):
    f = HERE / name
    n = int(f.read_text()) if f.exists() else 0
    f.write_text(str(n + 1))
    return n


@pytest.fixture
def made():
    tries("made")                        # each try sets it up afresh, and tears it down
    yield []
    tries("torn")


@pytest.mark.flaky(issue=101)
def test_flaky_passes_on_the_retry(tmp_path, made):
    assert not list(tmp_path.iterdir()) and made == []      # a fresh tmp_path and fixture on the retry
    (tmp_path / "x").write_text("x")
    made.append(1)
    assert tries("once") >= 1, "the first try fails"


@pytest.mark.flaky(issue=102)
def test_flaky_fails_twice():
    tries("twice")
    assert False, "always"


def test_plain_fails_at_once():
    tries("plain")
    assert False, "not retried"


def test_plain_passes():
    pass
'''

CONFTEST = '''
from tests.conftest import pytest_report_teststatus, pytest_runtest_protocol, pytest_terminal_summary  # noqa: F401


def pytest_configure(config):
    config.addinivalue_line("markers", "flaky(issue): a known flaky test")
'''


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_a_flaky_test_is_retried_once_and_said_so_and_no_other(tmp_path, workers):
    if workers:
        pytest.importorskip("xdist")
    (tmp_path / "test_suite.py").write_text(SUITE)
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "GITHUB_ACTIONS": "true"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(tmp_path / "pytest.ini"),
                        "--rootdir", str(tmp_path), *workers, str(tmp_path)],
                       capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    out = r.stdout
    assert r.returncode == 1, out + r.stderr
    assert "2 failed, 2 passed, 2 rerun" in out, out
    count = {n: int((tmp_path / n).read_text()) for n in ("once", "twice", "plain", "made", "torn")}
    assert count == {"once": 2, "twice": 2, "plain": 1, "made": 2, "torn": 2}
    assert "RERUN test_suite.py::test_flaky_passes_on_the_retry (#101): AssertionError: the first try fails" in out
    assert "::warning title=Flaky test retried (#102)::test_suite.py::test_flaky_fails_twice failed once" in out
    assert "FAILED test_suite.py::test_flaky_fails_twice" in out and "FAILED test_suite.py::test_plain_fails_at_once" in out
    assert "test_plain_fails_at_once (" not in out.split("flaky tests retried once")[1].split("short test summary")[0]
