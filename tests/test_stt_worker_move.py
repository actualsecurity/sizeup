"""The GPU STT worker's move from tools/stt_worker to deploy/stt-worker, and its self-update across it.

A worker installed before the move (its worker.py from install.ps1 pinned at f13acff2 or earlier) knows only
tools/stt_worker/worker.py: the hub names a commit and the SHA-256 of deploy/stt-worker/worker.py in its image, and the
copy at the old path, byte for byte the same, gives that worker the same hash at the same commit. The version it updates
to downloads from deploy/stt-worker/worker.py, and from the old path only for a commit from before the move (a pin).
GitHub is tests/test_stt_worker_update.py's fake; commits and hashes are made up; no network."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.stt_worker_module import W
from tests.test_stt_worker_update import (COMMIT, IDLE, NEW_PATH, OLD_PATH, TOKEN, Clock, FakeGitHub, install, offer,
                                          pull)

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "stt-worker"
COMPAT = ROOT / "tools" / "stt_worker"
NEW = (DEPLOY / "worker.py").read_bytes()
NEW_SHA = hashlib.sha256(NEW).hexdigest()
BEFORE = "5e0c7a91d3b2f4e6a8c0b1d3e5f7a9c2b4d6e8f0"    # a made-up commit from before the move (a pin to it)
AFTER = "7f1d3c5b9a2e4f6d8b0c1a3e5d7f9b2c4a6e8d0f"     # a made-up commit after the compatibility copy is gone
PINNED_INSTALLER = "f13acff29be3623cfd4f9410300c1886364ac817"   # the installer the first worker was installed from


# --- the files ------------------------------------------------------------------------------------------------------

def test_the_compatibility_copy_is_byte_identical():
    """tools/stt_worker/worker.py must stay exactly deploy/stt-worker/worker.py: a worker from before the move
    downloads it and checks it against the hash of the new one. cp deploy/stt-worker/worker.py tools/stt_worker/"""
    assert (COMPAT / "worker.py").read_bytes() == NEW, \
        "tools/stt_worker/worker.py differs: cp deploy/stt-worker/worker.py tools/stt_worker/worker.py"


def test_the_old_folder_holds_only_the_copy_and_why():
    assert sorted(p.name for p in COMPAT.iterdir() if p.name != "__pycache__") == ["README.md", "worker.py"]
    readme = (COMPAT / "README.md").read_text()
    assert "compatibility copy" in readme and "deploy/stt-worker" in readme


def test_the_worker_knows_both_paths_new_first():
    assert W.UPDATE_FILE == NEW_PATH == "deploy/stt-worker/worker.py"
    assert W.UPDATE_FILES == (NEW_PATH, OLD_PATH)
    assert (ROOT / W.UPDATE_FILE).read_bytes() == NEW and (ROOT / W.UPDATE_FILE_BEFORE).read_bytes() == NEW


def test_the_hub_announces_the_new_path_and_its_hash_is_the_copys_too():
    from sizeup.cloud import sttworkers as S
    assert S.WORKER_FILE == (DEPLOY / "worker.py").resolve()
    v = S.worker_build(COMMIT)
    assert v == {"commit": COMMIT, "sha256": NEW_SHA,
                 "url": f"https://raw.githubusercontent.com/actualsecurity/sizeup/{COMMIT}/deploy/stt-worker/worker.py"}
    assert hashlib.sha256((COMPAT / "worker.py").read_bytes()).hexdigest() == v["sha256"]


def test_the_hub_image_has_the_worker_at_the_new_path_only():
    docker = (ROOT / "Dockerfile").read_text()
    assert "COPY deploy/stt-worker/worker.py ./deploy/stt-worker/worker.py" in docker and "tools/stt_worker" not in docker
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "tools/" in ignore and "deploy/stt-worker/*" in ignore and "!deploy/stt-worker/worker.py" in ignore
    assert not any("stt_worker" in x for x in ignore)


def test_deploy_stt_worker_keeps_deploy_stations_conventions():
    for name in ("README.md", "worker.py", "install.ps1", "pins.json", ".gitattributes", "llm_eval/eval.py"):
        assert (DEPLOY / name).is_file(), name
    assert (ROOT / "deploy" / "station" / ".gitattributes").is_file()
    assert re.search(r"^install\.ps1 -text$", (DEPLOY / ".gitattributes").read_text(), re.M)


# --- the self-update across the move -----------------------------------------------------------------------------

def _updater(tmp_path, gh, mod=W, data=b"# the worker.py installed before\n", selftest=None):
    cur = install(tmp_path, data)
    cfg = mod.Config(hub_url="https://hub.example.org", token=TOKEN, state_dir=str(tmp_path / "state"), log_file="-")
    return mod.Updater(cfg, path=cur, fetch=gh, clock=Clock(), selftest=selftest or (lambda new, sha: "")), cur


def _raw(gh) -> list[str]:
    return [p for h, p in gh.calls if h == W.GITHUB_RAW]


def test_the_new_worker_downloads_from_the_new_path(tmp_path):
    gh = FakeGitHub()
    up, _ = _updater(tmp_path, gh)
    up.offer(offer(sha=NEW_SHA))
    assert up.step(IDLE) is True
    assert _raw(gh) == [f"/actualsecurity/sizeup/{COMMIT}/{NEW_PATH}"]


def test_the_new_worker_after_the_copy_is_gone(tmp_path):
    """Once tools/stt_worker/worker.py is deleted, the new path alone serves every worker from the move on."""
    gh = FakeGitHub()
    gh.compare[AFTER], gh.pulls[AFTER] = ("behind", AFTER), [pull(AFTER)]
    gh.paths[AFTER] = {NEW_PATH: NEW}
    up, _ = _updater(tmp_path, gh)
    up.offer(offer(commit=AFTER, sha=NEW_SHA))
    assert up.step(IDLE) is True and _raw(gh) == [f"/actualsecurity/sizeup/{AFTER}/{NEW_PATH}"]


def test_a_commit_from_before_the_move_comes_from_the_old_path(tmp_path):
    """A pin to a version from before the move: GitHub has no deploy/stt-worker/worker.py there (404), so the old path."""
    old = b"# worker.py as it was before the move\nVERSION = '2'\n"
    gh = FakeGitHub()
    gh.compare[BEFORE], gh.pulls[BEFORE] = ("behind", BEFORE), [pull(BEFORE)]
    gh.before[BEFORE] = old
    up, _ = _updater(tmp_path, gh)
    up.offer(offer(mode="pin", commit=BEFORE, sha=hashlib.sha256(old).hexdigest()))
    assert up.step(IDLE) is True
    assert _raw(gh) == [f"/actualsecurity/sizeup/{BEFORE}/{NEW_PATH}", f"/actualsecurity/sizeup/{BEFORE}/{OLD_PATH}"]
    assert up.new_path.read_bytes() == old


def test_neither_path_is_a_final_refusal(tmp_path):
    gh = FakeGitHub()
    gh.paths[COMMIT] = {}
    up, cur = _updater(tmp_path, gh)
    up.offer(offer(sha=NEW_SHA))
    assert up.step(IDLE) is False and up.status == f"refused {NEW_SHA[:12]}: no worker.py at {COMMIT[:12]}"
    assert len(_raw(gh)) == 2 and not up.new_path.exists()


@pytest.mark.parametrize("what", ["wrong hash", "GitHub down"])
def test_the_old_path_only_after_a_404(tmp_path, what):
    """A file at the new path that is wrong, or GitHub out of reach: never another path."""
    gh = FakeGitHub()
    if what == "wrong hash":
        gh.paths[COMMIT] = {NEW_PATH: b"# something else\n", OLD_PATH: NEW}
    else:
        gh.raw_status = 503
    up, _ = _updater(tmp_path, gh)
    up.offer(offer(sha=NEW_SHA))
    assert up.step(IDLE) is False
    assert _raw(gh) == [f"/actualsecurity/sizeup/{COMMIT}/{NEW_PATH}"] and not up.new_path.exists()


def test_an_old_worker_updates_through_the_compatibility_copy(tmp_path):
    """The installed worker's own knowledge (UPDATE_FILE tools/stt_worker/worker.py, nothing else): it finds the hub's
    hash at the old path at the move's commit, swaps, and the version it now runs uses the new path."""
    gh = FakeGitHub()
    gh.files[COMMIT] = NEW
    old_files = (OLD_PATH,)
    saved = W.UPDATE_FILES
    W.UPDATE_FILES = old_files                       # (download_worker reads it at call time)
    try:
        up, cur = _updater(tmp_path, gh)
        up.offer(offer(sha=NEW_SHA))
        assert up.step(IDLE) is True and up.swap() is True
    finally:
        W.UPDATE_FILES = saved
    assert _raw(gh) == [f"/actualsecurity/sizeup/{COMMIT}/{OLD_PATH}"]
    assert cur.read_bytes() == NEW
    # the file it runs now: kept once it reaches the hub, and its next update comes from the new path only
    gh2 = FakeGitHub()
    gh2.compare[AFTER], gh2.pulls[AFTER] = ("behind", AFTER), [pull(AFTER)]
    nxt = NEW + b"\n# a later version\n"
    gh2.paths[AFTER] = {NEW_PATH: nxt}
    again = W.Updater(up.cfg, path=cur, fetch=gh2, clock=Clock(), selftest=lambda n, s: "")
    assert again.own == NEW_SHA and again.pending() is not None
    assert again.boot() is False
    again.confirm()
    assert again.pending() is None and again.doc()["last"]["event"] == "updated"
    again.offer(offer(commit=AFTER, sha=hashlib.sha256(nxt).hexdigest()))
    assert again.step(IDLE) is True and _raw(gh2) == [f"/actualsecurity/sizeup/{AFTER}/{NEW_PATH}"]


# --- the real worker that is installed: worker.py as install.ps1 at f13acff2 pinned it ----------------------------

def _git_show(rev_path: str) -> bytes | None:
    git = shutil.which("git")
    if not git:
        return None
    r = subprocess.run([git, "show", rev_path], cwd=ROOT, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _installed_worker_py() -> bytes | None:
    """worker.py as the installer at f13acff2 downloads it (its pins.json's URL: a commit and the old path)."""
    pins = _git_show(f"{PINNED_INSTALLER}:tools/stt_worker/pins.json")
    if pins is None:
        return None
    url = json.loads(pins)["artifacts"]["worker_py"]["url"]
    m = re.fullmatch(r"https://raw\.githubusercontent\.com/actualsecurity/sizeup/([0-9a-f]{40})/(tools/stt_worker/worker\.py)",
                     url)
    assert m, url
    return _git_show(f"{m.group(1)}:{m.group(2)}")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib (the installer writes worker.toml)")
def test_the_installed_worker_takes_this_version(tmp_path, monkeypatch):
    """The worker.py the PC runs (as f13acff2's installer pinned it; skipped where git hasn't those commits, a shallow
    checkout: CI fetches them), its own Updater with GitHub's fake serving this tree at the move's commit: it downloads the
    compatibility copy, the REAL self-check of this worker.py passes with the worker.toml that installer wrote, it
    swaps; then this version starts on it (boot), reaches the hub (confirm) and its next update uses the new path."""
    old = _installed_worker_py()
    if old is None:
        # CI fetches both commits (.github/workflows/test.yml): there it must run
        assert not os.environ.get("GITHUB_ACTIONS"), "CI must fetch f13acff2 and 39c4de4b for this test"
        pytest.skip("git has no f13acff2 here (a shallow checkout)")
    ps1 = _git_show(f"{PINNED_INSTALLER}:tools/stt_worker/install.ps1").decode()
    toml = re.search(r"^\$TomlStt = @'\n(.*?)\n'@$", ps1, re.S | re.M).group(1).replace("{{HUB_URL}}",
                                                                                     "https://hub.example.org")
    d = tmp_path / "sizeup-stt"
    d.mkdir()
    (d / "worker.toml").write_text(toml + f'\nstate_dir = "{(tmp_path / "state").as_posix()}"\n')
    cur = d / "worker.py"
    cur.write_bytes(old)
    for k in [k for k in os.environ if k.startswith("SIZEUP_STT_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("SIZEUP_STT_WORKER_TOKEN", TOKEN)
    o = _load(cur, "sizeup_stt_worker_installed")
    try:
        assert o.UPDATE_FILE == OLD_PATH and not hasattr(o, "UPDATE_FILES")      # (what it knows: the old path only)
        cfg = o.load_config(d / "worker.toml")
        gh = FakeGitHub()
        gh.files[COMMIT] = NEW                         # the move's commit: deploy/stt-worker/ and the copy
        up = o.Updater(cfg, path=cur, config_path=d / "worker.toml", fetch=gh, clock=Clock())   # (the real self-check)
        ans = {"ok": True, "pool_on": True, "worker_update": {"mode": "auto", "commit": COMMIT, "sha256": NEW_SHA}}
        up.offer(ans)
        assert up.step(lambda: "") is True, up.status
        assert [p for h, p in gh.calls if h == o.GITHUB_RAW] == [f"/actualsecurity/sizeup/{COMMIT}/{OLD_PATH}"]
        assert up.swap() is True and cur.read_bytes() == NEW
    finally:
        sys.modules.pop("sizeup_stt_worker_installed", None)
    n = _load(cur, "sizeup_stt_worker_swapped")
    try:
        cfg = n.load_config(d / "worker.toml")        # (the same worker.toml: no new key needed)
        assert cfg.log_ship is True
        gh2 = FakeGitHub()
        gh2.compare[AFTER], gh2.pulls[AFTER] = ("behind", AFTER), [pull(AFTER)]
        nxt = NEW + b"\n# a later version\n"
        gh2.paths[AFTER] = {NEW_PATH: nxt}
        new = n.Updater(cfg, path=cur, fetch=gh2, clock=Clock(), selftest=lambda p, s: "")
        assert new.boot() is False and new.status == "new: not confirmed yet"
        new.confirm()
        assert new.doc()["last"] == {**new.doc()["last"], "event": "updated", "to": NEW_SHA, "commit": COMMIT}
        new.offer(offer(commit=AFTER, sha=hashlib.sha256(nxt).hexdigest()))
        assert new.step(lambda: "") is True
        assert [p for h, p in gh2.calls if h == n.GITHUB_RAW] == [f"/actualsecurity/sizeup/{AFTER}/{NEW_PATH}"]
    finally:
        sys.modules.pop("sizeup_stt_worker_swapped", None)

