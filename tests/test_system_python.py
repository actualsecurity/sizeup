"""The station scripts that run with the SYSTEM python3 (no venv, stdlib only): adsbd.py, trunkrecd.py, autoupdate.py,
configupdate.py.

The station's python3 is the distro's: Ubuntu 24.04 (WSL) has 3.12, Debian 12 / Raspberry Pi OS 3.11 (install-linux.sh
supports both), so these must load on 3.11 with the standard library alone. install-linux.sh's preflight loads each with
the system python3 before a release is switched to; these tests keep that list complete, check the scripts statically
for anything newer than 3.11 or outside the stdlib, and load each exactly as the preflight does (919cfc2 was refused by a
preflight check that could never pass: `adsbd.py help | grep -q usage` under pipefail, where usage exits 2)."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATION = ROOT / "deploy" / "station"
INSTALLER = STATION / "install-linux.sh"
MIN = (3, 11)
SYSTEM_PY = ("sizeup/radio/adsbd.py", "sizeup/radio/trunkrecd.py", "sizeup/cloud/autoupdate.py", "sizeup/cloud/configupdate.py",
             "tools/stt_diag.py")

# stdlib names that are newer than MIN (ast.parse's feature_version catches new syntax, not new library names)
NEWER_MODULES = {"annotationlib", "compression", "string.templatelib", "concurrent.interpreters"}
NEWER_NAMES = {  # module -> names added after 3.11
    "typing": {"override", "TypeAliasType", "TypeIs", "ReadOnly", "NoDefault", "get_protocol_members", "is_protocol"},
    "itertools": {"batched"},
    "warnings": {"deprecated"},
    "copy": {"replace"},
    "calendar": {"Month", "Day", "JANUARY", "MONDAY"},
    "sys": {"monitoring", "activate_stack_trampoline", "_is_gil_enabled"},
    "os": {"process_cpu_count", "grantpt", "unlockpt", "posix_openpt", "ptsname"},
    "pathlib": {"UnsupportedOperation"},
}
NEWER_METHODS = {"walk": "Path.walk (3.12)", "full_match": "PurePath.full_match (3.13)", "from_uri": "Path.from_uri (3.13)",
                 "copy_into": "Path.copy_into (3.14)", "move_into": "Path.move_into (3.14)"}


def preflight_loader() -> str:
    """The Python the preflight runs for each script (python3 -I -B -c '...' FILE), straight from install-linux.sh."""
    m = re.search(r"python3 -I -B -c '(import importlib\.util.*?)' \"\$rel/\$f\"", INSTALLER.read_text(), re.S)
    assert m, "install-linux.sh's preflight no longer loads the system-python3 scripts with python3 -I -B -c '...'"
    return m.group(1)


def test_the_preflight_checks_every_system_python_script():
    sh = INSTALLER.read_text()
    m = re.search(r"for f in ((?:(?:sizeup|tools)/\S+\.py ?)+); do", sh)
    assert m, "install-linux.sh's preflight has no loop over the system-python3 scripts"
    assert tuple(m.group(1).split()) == SYSTEM_PY
    assert "| grep -q 'usage: adsbd.py'" not in sh       # could never pass under pipefail (usage exits 2)


def test_every_script_run_with_the_system_python_is_listed():
    """Anything deploy/station runs as `python3 -I something.py` (directly, via $LIB or $UPDATER) is in SYSTEM_PY."""
    texts = [INSTALLER.read_text(), (STATION / "sizeup-station").read_text()]
    texts += [p.read_text() for p in (STATION / "systemd").glob("*.service")]
    run = set()
    for text in texts:
        for line in text.splitlines():
            if "python3 -I" in line:
                run |= set(re.findall(r"(\w+)\.py\b", line.split("python3 -I", 1)[1]))
            run |= set(re.findall(r"^\s*\w+=/usr/local/lib/sizeup/(\w+)\.py\b", line))
    assert run, "found no system-python3 scripts at all: the pattern is out of date"
    assert run <= {Path(p).stem for p in SYSTEM_PY}, f"run with the system python3 but not checked: {run}"


@pytest.mark.parametrize("rel", SYSTEM_PY)
def test_stdlib_only_and_nothing_newer_than_the_minimum(rel):
    src = (ROOT / rel).read_text()
    tree = ast.parse(src, filename=rel, feature_version=MIN)           # syntax newer than 3.11 fails here
    stdlib = set(sys.stdlib_module_names)
    imported: dict[str, str] = {}                                       # local name -> module
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] in stdlib, f"{rel}: import {a.name} isn't the standard library"
                assert a.name not in NEWER_MODULES, f"{rel}: {a.name} is newer than {MIN}"
                imported[(a.asname or a.name).split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{rel}: a relative import (it runs as a lone file in /usr/local/lib/sizeup)"
            mod = node.module or ""
            if mod == "__future__":
                continue
            assert mod.split(".")[0] in stdlib, f"{rel}: from {mod} import ... isn't the standard library"
            assert mod not in NEWER_MODULES, f"{rel}: {mod} is newer than {MIN}"
            for a in node.names:
                assert a.name not in NEWER_NAMES.get(mod, ()), f"{rel}: {mod}.{a.name} is newer than {MIN}"
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            mod = imported.get(node.value.id)
            if mod:
                assert node.attr not in NEWER_NAMES.get(mod, ()), f"{rel}: {mod}.{node.attr} is newer than {MIN}"
        if (isinstance(node, ast.Attribute) and node.attr in NEWER_METHODS
                and not (isinstance(node.value, ast.Name) and node.value.id in imported)):   # os.walk is fine
            pytest.fail(f"{rel}: .{node.attr}(): {NEWER_METHODS[node.attr]} is newer than {MIN}")


def interpreters():
    """This one, plus any 3.11 / 3.12 on PATH (the station's own versions), each once."""
    seen, out = set(), []
    for exe in (sys.executable, shutil.which("python3.11"), shutil.which("python3.12")):
        if exe and Path(exe).resolve() not in seen:
            seen.add(Path(exe).resolve())
            out.append(exe)
    return out


@pytest.mark.parametrize("rel", SYSTEM_PY)
def test_each_loads_exactly_as_the_preflight_loads_it(rel, tmp_path):
    loader = preflight_loader()
    for exe in interpreters():
        ver = subprocess.run([exe, "-c", "import sys; print(sys.version_info[:2] >= (3, 11))"],
                             capture_output=True, text=True).stdout.strip()
        if ver != "True":
            continue                                    # too old to be a station python (no tomllib)
        r = subprocess.run([exe, "-I", "-B", "-c", loader, str(ROOT / rel)], capture_output=True, text=True,
                           cwd=tmp_path, timeout=60)
        assert r.returncode == 0, f"{rel} doesn't load with {exe}: {r.stderr.strip().splitlines()[-1:]}"


def test_the_preflight_passes_under_pipefail_and_says_why_when_not(tmp_path):
    """The preflight's own loop, run by bash with the installer's set -euo pipefail, against this tree and a broken copy."""
    sh = INSTALLER.read_text()
    loop = re.search(r"\n(  local f\n  for f in .*?\n  done\n)", sh, re.S)
    assert loop, "install-linux.sh's preflight loop moved"
    rel = tmp_path / "rel"
    for p in SYSTEM_PY:
        (rel / p).parent.mkdir(parents=True, exist_ok=True)
        (rel / p).write_text((ROOT / p).read_text())
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    script = ("set -euo pipefail\nsay() { echo \"$*\"; }\nwarn() { echo \"WARNING: $*\"; }\n"
              f"pf() {{\n  local rel=$1 out bad=0\n{loop.group(1)}  echo \"bad=$bad\"\n}}\npf \"$1\"\n")
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    r = subprocess.run(["bash", "-c", script, "-", str(rel)], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "bad=0" in r.stdout, r.stdout + r.stderr
    assert r.stdout.count("loads with the system python3") == len(SYSTEM_PY)
    # a script that needs something the system python3 lacks: refused, and the push says what
    (rel / SYSTEM_PY[0]).write_text("import sizeup_not_stdlib\n")
    r = subprocess.run(["bash", "-c", script, "-", str(rel)], capture_output=True, text=True, env=env, timeout=60)
    assert "bad=1" in r.stdout, r.stdout + r.stderr
    assert "adsbd.py doesn't load with the system Python 3." in r.stdout
    assert "ModuleNotFoundError: No module named 'sizeup_not_stdlib'" in r.stdout
